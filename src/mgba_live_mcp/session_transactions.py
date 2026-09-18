from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .errors import DomainError

_JOURNAL = "transaction.json"
_OPERATION_LOCK = ".operation.lock"
_STATE_LOCK = ".state.lock"
_STOP_LOCK = ".stop.lock"
_CURRENT: ContextVar[tuple[Transaction, ...]] = ContextVar("session_transactions", default=())


def _error(code: str, directory: Path, stage: str, detail: str, **context: Any) -> DomainError:
    return DomainError(
        code,
        f"session={directory.name} stage={stage} {detail}",
        phase=stage,
        execution_outcome="not_started"
        if stage
        in {
            "acquire",
            "reserve",
            "admission",
            "reconcile",
            "publish",
            _OPERATION_LOCK,
            _STOP_LOCK,
        }
        or stage.endswith(".initialization.lock")
        else "unknown",
        session_id=directory.name,
        **context,
    )


def _execution() -> tuple[int, int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), task


class _Directory:
    def __init__(self, path: Path, *, dir_fd: int | None = None) -> None:
        self.path = path
        self.fd = os.open(
            path if dir_fd is None else path.name or ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
        info = os.fstat(self.fd)
        self.identity = [info.st_dev, info.st_ino]

    def close(self) -> None:
        os.close(self.fd)
        self.fd = -1

    def check(self) -> None:
        if self.fd < 0:
            raise _error(
                "session_generation_changed", self.path, "directory", "directory lease is closed"
            )
        try:
            info = self.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is None or [info.st_dev, info.st_ino] != self.identity:
            raise _error(
                "session_generation_changed",
                self.path,
                "directory",
                "directory was removed/replaced",
            )

    def mkdir(self, name: str) -> None:
        self.check()
        os.mkdir(name, 0o700, dir_fd=self.fd)
        self.check()

    @contextmanager
    def child(self, name: str, *, create: bool = False) -> Iterator[_Directory]:
        self.check()
        if create:
            self.mkdir(name)
        directory = _Directory(self.path / name, dir_fd=self.fd)
        try:
            directory.check()
            yield directory
        finally:
            directory.close()

    @contextmanager
    def create_file(self, name: str) -> Iterator[BinaryIO]:
        self.check()
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        with os.fdopen(fd, "wb") as stream:
            self.check()
            yield stream
            self.check()

    def copy_file(self, source: Path, name: str) -> Path:
        self.check()
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as input_file:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "Input is not a regular file", str(source))
            with self.create_file(name) as output_file:
                shutil.copyfileobj(input_file, output_file)
        return self.path / name

    @contextmanager
    def lock(self, name: str, *, blocking: bool = False) -> Iterator[None]:
        self.check()
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=self.fd)
        except FileNotFoundError:
            # Concurrent nonexclusive O_CREAT opens can return ENOENT on macOS.
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
            except FileExistsError:
                fd = os.open(name, flags, dir_fd=self.fd)
        try:
            # A suspended publisher must not trap recovery in a blocking flock.
            deadline = time.monotonic() + 0.5 if blocking else 0.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic() if blocking else 0.0
                    if remaining <= 0:
                        detail = (
                            "ownership acquisition timed out" if blocking else "ownership is held"
                        )
                        raise _error("session_busy", self.path, name, detail) from exc
                    time.sleep(min(0.01, remaining))
            self.check()
            yield
        finally:
            os.close(fd)

    def read_json(self, name: str) -> Any:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.fd)
        with os.fdopen(fd) as stream:
            return json.load(stream)

    @contextmanager
    def _writer(self, name: str) -> Iterator[TextIO]:
        self.check()
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yield stream
                stream.flush()
                os.fsync(stream.fileno())
            self.check()
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
            self.check()
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def write_json(self, name: str, payload: Any) -> None:
        with self._writer(name) as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")

    def write_text(self, name: str, text: str) -> None:
        with self._writer(name) as stream:
            stream.write(text)

    def remove(self, parent: _Directory) -> None:
        """Remove only this leased tree; callers retain initialization, stop, and state locks."""
        metadata = (_OPERATION_LOCK, _STATE_LOCK, _STOP_LOCK, _JOURNAL)
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if entry.name in metadata:
                    continue
                parent.check()
                self.check()
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.name, dir_fd=self.fd)
                else:
                    os.unlink(entry.name, dir_fd=self.fd)
        # Keep the journal until all staging files have gone, so partial failure stays fenced.
        for name in metadata:
            parent.check()
            self.check()
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        parent.check()
        self.check()
        os.rmdir(self.path.name, dir_fd=parent.fd)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically persist JSON without creating its parent or following a replacement directory."""
    directory = _Directory(Path(os.path.abspath(path.parent)))
    try:
        directory.write_json(path.name, payload)
    finally:
        directory.close()


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically persist exact UTF-8 text without creating or following a replaced parent."""
    directory = _Directory(Path(os.path.abspath(path.parent)))
    try:
        directory.write_text(path.name, text)
    finally:
        directory.close()


def _initialization_lock_name(path: Path) -> str:
    # Derived coordination names must not shorten the filesystem's valid ID space.
    return f".{hashlib.sha256(os.fsencode(path.name)).hexdigest()}.initialization.lock"


@contextmanager
def _leased_directory(
    path: Path, *, create: bool, lock: str
) -> Iterator[tuple[_Directory, _Directory, _Directory]]:
    # This short, per-session parent lock closes mkdir -> operation-lock admission races.
    # Unlike the directory's operation lock, it is never held across emulator execution.
    try:
        grandparent = _Directory(path.parent.parent)
    except FileNotFoundError as exc:
        raise _error("session_not_found", path, "acquire", "directory is missing") from exc
    parent = None
    directory = None
    lease = None
    created_identity = None
    try:
        try:
            parent = _Directory(path.parent, dir_fd=grandparent.fd)
        except FileNotFoundError as exc:
            raise _error("session_not_found", path, "acquire", "directory is missing") from exc
        with parent.lock(_initialization_lock_name(path)):
            if create:
                try:
                    os.mkdir(path.name, dir_fd=parent.fd)
                except FileExistsError as exc:
                    raise _error(
                        "session_exists", path, "reserve", "Session already exists"
                    ) from exc
            try:
                if create:
                    info = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
                    created_identity = [info.st_dev, info.st_ino]
                try:
                    directory = _Directory(path, dir_fd=parent.fd)
                except FileNotFoundError as exc:
                    raise _error(
                        "session_not_found", path, "acquire", "directory is missing"
                    ) from exc
                if create and directory.identity != created_identity:
                    raise _error(
                        "session_generation_changed", path, "reserve", "directory was replaced"
                    )
                lease = directory.lock(lock)
                lease.__enter__()
            except BaseException as exc:
                if create:
                    try:
                        if isinstance(exc, DomainError) and exc.code == "session_busy":
                            raise exc
                        parent.check()
                        info = os.stat(path.name, dir_fd=parent.fd, follow_symlinks=False)
                        if [info.st_dev, info.st_ino] != created_identity:
                            raise _error(
                                "session_generation_changed",
                                path,
                                "rollback",
                                "directory was replaced",
                            )
                        if directory is None:
                            # Opening failed: only an empty, still-identical reservation is safe.
                            os.rmdir(path.name, dir_fd=parent.fd)
                        else:
                            _remove_prelaunch(
                                directory, parent, None, None, allow_uninitialized=True
                            )
                    except (RuntimeError, OSError) as cleanup_error:
                        exc.add_note(f"Prelaunch rollback failed for {path}: {cleanup_error}")
                raise
        yield grandparent, parent, directory
    except DomainError as exc:
        # The parent initialization lock belongs to this session, not its parent directory.
        if exc.phase == _initialization_lock_name(path):
            exc.context["session_id"] = path.name
        raise
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)
        if directory is not None:
            directory.close()
        if parent is not None:
            parent.close()
        grandparent.close()


def _new_state(directory: _Directory, status: str) -> dict[str, Any]:
    return {
        "version": 1,
        "generation": uuid.uuid4().hex,
        "directory": directory.identity,
        "state": status,
        "operation": None,
    }


def _valid_screenshot_artifact(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and "\0" not in value["path"]
        and Path(value["path"]).is_absolute()
        and Path(value["path"]).name.startswith(".mgba-screenshot-")
        and Path(value["path"]).suffix == ".png"
        and all(
            isinstance(value.get(key), list)
            and len(value[key]) == 2
            and all(type(part) is int and part >= 0 for part in value[key])
            for key in ("directory", "file")
        )
        and "request_id" in value
        and (value.get("request_id") is None or isinstance(value["request_id"], str))
    )


@contextmanager
def _screenshot_directory(artifact: dict[str, Any]) -> Iterator[_Directory]:
    path = Path(artifact["path"])
    directory = _Directory(path.parent)
    try:
        if directory.identity != artifact["directory"]:
            raise OSError(errno.ESTALE, "Screenshot directory was replaced", str(path.parent))
        directory.check()
        yield directory
    finally:
        directory.close()


def _check_screenshot_file(directory: _Directory, artifact: dict[str, Any]) -> None:
    path = Path(artifact["path"])
    info = os.stat(path.name, dir_fd=directory.fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != artifact["file"]:
        raise OSError(errno.ESTALE, "Owned screenshot staging file was replaced", str(path))


def _cleanup_screenshots(directory: _Directory, state: dict[str, Any]) -> None:
    operation = state["operation"]
    if operation is None or not operation.get("artifacts"):
        return
    retained = []
    errors = []
    for artifact in operation["artifacts"]:
        try:
            with _screenshot_directory(artifact) as parent:
                try:
                    _check_screenshot_file(parent, artifact)
                    os.unlink(Path(artifact["path"]).name, dir_fd=parent.fd)
                except FileNotFoundError:
                    pass
        except (OSError, DomainError) as exc:
            retained.append(artifact)
            errors.append(str(exc))
    operation["artifacts"] = retained
    if errors:
        journal_error = None
        try:
            directory.write_json(_JOURNAL, state)
        except (OSError, DomainError) as exc:
            journal_error = str(exc)
        raise DomainError(
            "snapshot_failed",
            "Owned screenshot staging files could not be removed.",
            phase="snapshot",
            stage="cleanup",
            execution_outcome=(
                "unknown"
                if operation["pending_request"]
                else "partial"
                if operation["started"]
                else "not_started"
            ),
            session_id=directory.path.name,
            request_id=retained[0].get("request_id"),
            retained_artifacts=[item["path"] for item in retained],
            cleanup_errors=errors,
            journal_error=journal_error,
        )


def _read_state(directory: _Directory, *, initialize: bool = False) -> dict[str, Any] | None:
    directory.check()
    try:
        state = directory.read_json(_JOURNAL)
    except FileNotFoundError:
        if not initialize:
            return None
        state = _new_state(directory, "ready")
        directory.write_json(_JOURNAL, state)
        return state
    except (ValueError, OSError) as exc:
        raise _error("session_state_corrupt", directory.path, "journal", str(exc)) from exc

    valid = (
        isinstance(state, dict)
        and type(state.get("version")) is int
        and state["version"] == 1
        and isinstance(state.get("generation"), str)
        and bool(state["generation"])
        and state.get("state") in ("ready", "stopping", "stopped")
        and "operation" in state
        and isinstance(state.get("directory"), list)
        and len(state["directory"]) == 2
        and all(type(value) is int for value in state["directory"])
    )
    if valid and state["operation"] is not None:
        operation = state["operation"]
        valid = (
            isinstance(operation, dict)
            and isinstance(operation.get("id"), str)
            and bool(operation["id"])
            and type(operation.get("pid")) is int
            and operation["pid"] > 0
            and type(operation.get("composite")) is bool
            and type(operation.get("started")) is bool
            and type(operation.get("uncertain")) is bool
            and "pending_request" in operation
            and isinstance(operation.get("artifacts", []), list)
            and all(_valid_screenshot_artifact(item) for item in operation.get("artifacts", []))
            and (
                operation["pending_request"] is None
                or (
                    isinstance(operation["pending_request"], str)
                    and bool(operation["pending_request"])
                    and operation["started"]
                )
            )
        )
    if not valid:
        raise _error(
            "session_state_corrupt", directory.path, "journal", "invalid transaction journal"
        )
    if state["directory"] != directory.identity:
        raise _error(
            "session_generation_changed",
            directory.path,
            "journal",
            "journal belongs to another inode",
        )
    directory.check()
    return state


def _require_ready(directory: _Directory, state: dict[str, Any]) -> None:
    if state["state"] != "ready":
        raise _error(
            f"session_{state['state']}",
            directory.path,
            "admission",
            f"generation={state['generation']} recovery stop is required or already completed",
        )


def _reclaimable(directory: _Directory, operation: dict[str, Any]) -> bool:
    if operation["composite"] and operation["started"]:
        return False
    request_id = operation["pending_request"]
    if request_id is None:
        return not operation["uncertain"]
    try:
        response = directory.read_json("response.json")
    except (OSError, ValueError):
        return False
    # A vanished command is not completion. Only this abandoned request's response is evidence.
    return isinstance(response, dict) and response.get("id") == request_id


def _owned_operation(
    directory: _Directory,
    generation: str | None,
    operation_id: str | None,
    *,
    allow_uninitialized: bool = False,
    require_ready: bool = True,
) -> dict[str, Any] | None:
    state = _read_state(directory)
    if state is None and allow_uninitialized:
        return None
    if state is None or state["generation"] != generation:
        raise _error(
            "session_generation_changed", directory.path, "ownership", "generation changed"
        )
    if require_ready:
        _require_ready(directory, state)
    if state["operation"] is None or state["operation"]["id"] != operation_id:
        raise _error("session_generation_changed", directory.path, "ownership", "operation retired")
    return state


def _remove_prelaunch(
    directory: _Directory,
    parent: _Directory,
    generation: str | None,
    operation_id: str | None,
    *,
    allow_uninitialized: bool = False,
) -> None:
    with directory.lock(_STOP_LOCK), directory.lock(_STATE_LOCK, blocking=True):
        state = _owned_operation(
            directory, generation, operation_id, allow_uninitialized=allow_uninitialized
        )
        if state is not None:
            operation = state["operation"]
            if (
                operation["pending_request"] is not None
                or operation["uncertain"]
                or (allow_uninitialized and operation["started"])
            ):
                raise _error(
                    "session_busy",
                    directory.path,
                    "rollback",
                    "reservation has unresolved execution; use recovery stop",
                    request_id=operation["pending_request"],
                )
        try:
            directory.remove(parent)
        except BaseException as exc:
            if state is not None:
                try:
                    remaining = _read_state(directory)
                    if remaining is None or (
                        remaining["generation"] == generation
                        and remaining["operation"] is not None
                        and remaining["operation"]["id"] == operation_id
                        and remaining["state"] == "ready"
                    ):
                        state["operation"]["uncertain"] = True
                        directory.write_json(_JOURNAL, state)
                except (RuntimeError, OSError) as restore_error:
                    exc.add_note(f"Could not preserve rollback fence: {restore_error}")
            raise


def _rollback_prelaunch(
    directory: _Directory,
    parent: _Directory,
    generation: str | None,
    operation_id: str | None,
    *,
    allow_uninitialized: bool = False,
) -> None:
    with parent.lock(_initialization_lock_name(directory.path), blocking=True):
        _remove_prelaunch(
            directory,
            parent,
            generation,
            operation_id,
            allow_uninitialized=allow_uninitialized,
        )


class Transaction:
    """Synchronous ownership retained by the actual worker, not its awaiting asyncio caller."""

    def __init__(
        self,
        directory: _Directory,
        parent: _Directory,
        grandparent: _Directory,
        generation: str,
        operation_id: str,
        *,
        created: bool,
    ) -> None:
        self.generation = generation
        self._directory = directory
        self._parent = parent
        self._grandparent = grandparent
        self._operation_id = operation_id
        self._created = created
        self._owner = _execution()
        self._active = True
        self._rolled_back = False
        self._startup_guarded = False
        self._startup_finalization: AbstractContextManager[None] | None = None

    def _check_owner(self) -> None:
        if not self._active or self._owner != _execution():
            raise _error(
                "session_busy",
                self._directory.path,
                "ownership",
                "transaction is not this execution's",
            )

    def _owned_state(self) -> dict[str, Any]:
        self._check_owner()
        state = _owned_operation(self._directory, self.generation, self._operation_id)
        assert state is not None
        return state

    def stage_screenshot(self, directory: Path) -> Path:
        """Reserve and journal an owned file before the native writer sees its path."""
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            operation = state["operation"]
            if operation["pending_request"] is not None or operation["uncertain"]:
                raise _error("session_busy", self._directory.path, "snapshot", "work is unresolved")
            parent = _Directory(directory)
            artifact: dict[str, Any] | None = None
            try:
                parent.check()
                while True:
                    path = directory / f".mgba-screenshot-{uuid.uuid4().hex}.png"
                    try:
                        fd = os.open(
                            path.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent.fd,
                        )
                        break
                    except FileExistsError:
                        continue
                with os.fdopen(fd, "wb") as stream:
                    info = os.fstat(stream.fileno())
                    artifact = {
                        "path": str(path),
                        "directory": parent.identity,
                        "file": [info.st_dev, info.st_ino],
                        "request_id": None,
                    }
                parent.check()
                operation.setdefault("artifacts", []).append(artifact)
                self._directory.write_json(_JOURNAL, state)
                return path
            except BaseException as exc:
                # No command can reference this file until this method returns.
                if artifact is not None:
                    try:
                        _check_screenshot_file(parent, artifact)
                        os.unlink(Path(artifact["path"]).name, dir_fd=parent.fd)
                    except OSError as cleanup_error:
                        artifacts = operation.setdefault("artifacts", [])
                        if artifact not in artifacts:
                            artifacts.append(artifact)
                        journal_error = None
                        try:
                            self._directory.write_json(_JOURNAL, state)
                        except (OSError, DomainError) as retention_error:
                            journal_error = str(retention_error)
                        raise DomainError(
                            "snapshot_failed",
                            str(exc),
                            phase="snapshot",
                            stage="capture",
                            execution_outcome="partial" if operation["started"] else "not_started",
                            session_id=self._directory.path.name,
                            staging_path=artifact["path"],
                            retained_artifacts=[artifact["path"]],
                            cleanup_errors=[str(cleanup_error)],
                            journal_error=journal_error,
                        ) from exc
                raise
            finally:
                parent.close()

    def _screenshot_artifact(self, state: dict[str, Any], path: Path) -> dict[str, Any]:
        operation = state["operation"]
        if operation["pending_request"] is not None or operation["uncertain"]:
            raise _error(
                "session_busy", self._directory.path, "snapshot", "native writer is unresolved"
            )
        for artifact in operation.get("artifacts", []):
            if artifact["path"] == str(path):
                return artifact
        raise _error(
            "session_generation_changed",
            self._directory.path,
            "snapshot",
            "staging file is not owned",
        )

    def read_screenshot(self, path: Path) -> bytes:
        with ExitStack() as resources:
            with self._directory.lock(_STATE_LOCK, blocking=True):
                artifact = self._screenshot_artifact(self._owned_state(), path)
                parent = resources.enter_context(_screenshot_directory(artifact))
                fd = os.open(
                    path.name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent.fd,
                )
                stream = resources.enter_context(os.fdopen(fd, "rb"))
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != artifact["file"]:
                    raise OSError(
                        errno.ESTALE, "Owned screenshot staging file was replaced", str(path)
                    )
                parent.check()
            return stream.read()

    def publish_screenshot(self, path: Path, destination: Path | None) -> Path:
        """Fence the short filesystem commit against recovery; never replace implicit names."""
        with self._directory.lock(_STATE_LOCK, blocking=True):
            artifact = self._screenshot_artifact(self._owned_state(), path)
            with _screenshot_directory(artifact) as parent:
                _check_screenshot_file(parent, artifact)
                if destination is not None:
                    if destination.parent != path.parent:
                        raise ValueError(
                            "Screenshot publication must stay on the staging filesystem."
                        )
                    os.replace(
                        path.name, destination.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd
                    )
                    return destination
                while True:
                    destination = path.parent / f"screenshot-{uuid.uuid4().hex}.png"
                    try:
                        os.link(
                            path.name,
                            destination.name,
                            src_dir_fd=parent.fd,
                            dst_dir_fd=parent.fd,
                            follow_symlinks=False,
                        )
                        return destination
                    except FileExistsError:
                        continue

    def check(self) -> None:
        with self._directory.lock(_STATE_LOCK, blocking=True):
            self._owned_state()

    @contextmanager
    def startup_guard(self) -> Iterator[None]:
        """Fence short spawn/identity registration work, never readiness or emulator execution."""
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            state["operation"]["started"] = True
            self._directory.write_json(_JOURNAL, state)
            self._startup_guarded = True
            try:
                yield
                self._owned_state()
            finally:
                self._startup_guarded = False

    def write_metadata(self, data: dict[str, Any]) -> None:
        """Publish session metadata through this inode, including same-owner stop diagnostics."""
        self._check_owner()
        guard = (
            nullcontext()
            if self._startup_guarded
            else self._directory.lock(_STATE_LOCK, blocking=True)
        )
        with guard:
            _owned_operation(
                self._directory,
                self.generation,
                self._operation_id,
                require_ready=False,
            )
            self._directory.write_json("session.json", data)

    def rollback(self) -> None:
        """Remove this reservation only when its worker knows Popen never returned."""
        if self._rolled_back and self._owner == _execution():
            return
        self._check_owner()
        if not self._created:
            raise _error(
                "session_busy", self._directory.path, "rollback", "reservation was not created here"
            )
        _rollback_prelaunch(self._directory, self._parent, self.generation, self._operation_id)
        self._rolled_back = True
        self._active = False

    def publish(self, request_id: str, callback: Callable[[], None]) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a nonempty string")
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            operation = state["operation"]
            if operation["pending_request"] is not None or operation["uncertain"]:
                raise _error(
                    "session_busy",
                    self._directory.path,
                    "publish",
                    f"request={operation['pending_request']} unresolved; use recovery stop",
                    request_id=request_id,
                    pending_request_id=operation["pending_request"],
                )
            operation["started"] = True
            operation["pending_request"] = request_id
            for artifact in operation.get("artifacts", []):
                if artifact["request_id"] is None:
                    artifact["request_id"] = request_id
            self._directory.write_json(_JOURNAL, state)
            # Persist before the side effect, under the same guard that fences recovery stop.
            # Callback failure is ambiguous: publication may have happened before it raised.
            callback()
            self._owned_state()

    def complete(self, request_id: str) -> None:
        """Record a response already correlated by the caller, before deleting that response."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a nonempty string")
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            if state["operation"]["pending_request"] != request_id:
                raise _error(
                    "session_busy",
                    self._directory.path,
                    "complete",
                    f"request={request_id} is not owned",
                    request_id=request_id,
                )
            state["operation"]["pending_request"] = None
            state["operation"]["uncertain"] = False
            self._directory.write_json(_JOURNAL, state)

    def mark_uncertain(self) -> None:
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            state["operation"]["uncertain"] = True
            self._directory.write_json(_JOURNAL, state)

    def _nest(self, *, create: bool, composite: bool) -> None:
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            if create and (not self._created or state["operation"]["started"]):
                raise _error(
                    "session_exists", self._directory.path, "reserve", "Session already exists"
                )
            if composite and not state["operation"]["composite"]:
                state["operation"]["composite"] = True
                self._directory.write_json(_JOURNAL, state)

    def _finish(self, *, failed: bool = False) -> None:
        if self._rolled_back:
            return
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            operation = state["operation"]
            complete = operation["pending_request"] is None and not operation["uncertain"]
            staged = bool(operation.get("artifacts"))
            if complete:
                _cleanup_screenshots(self._directory, state)
            if failed and operation["composite"] and operation["started"]:
                if complete and staged:
                    self._directory.write_json(_JOURNAL, state)
                return
            if complete:
                state["operation"] = None
                try:
                    self._directory.write_json(_JOURNAL, state)
                except BaseException as exc:
                    # Publication may have succeeded before its durability check failed.
                    # Preserve this still-leased owner for startup failure diagnostics.
                    state["operation"] = operation
                    operation["uncertain"] = True
                    try:
                        self._directory.write_json(_JOURNAL, state)
                    except (RuntimeError, OSError) as restore_error:
                        exc.add_note(f"Could not preserve finalization fence: {restore_error}")
                    raise


@contextmanager
def transaction(
    directory: Path, *, create: bool = False, composite: bool = False
) -> Iterator[Transaction]:
    """Acquire without waiting; reclaim only provably resolved abandoned work.

    create=True reserves a new directory. Nested creation is permitted only inside that
    same reservation. Pre-yield setup failures roll back; after yielding, only the worker
    may explicitly roll back a reservation that it knows has never spawned a process.
    """
    path = Path(os.path.abspath(directory))
    for current in _CURRENT.get():
        if current._directory.path == path and current._active and current._owner == _execution():
            current._nest(create=create, composite=composite)
            yield current
            return

    operation_id = uuid.uuid4().hex
    with _leased_directory(path, create=create, lock=_OPERATION_LOCK) as (
        grandparent,
        parent,
        owned,
    ):
        generation = None
        try:
            with owned.lock(_STATE_LOCK, blocking=True):
                state = _read_state(owned, initialize=not create)
                if state is None:
                    state = _new_state(owned, "ready")
                elif create:
                    raise _error(
                        "session_generation_changed",
                        path,
                        "reserve",
                        "reservation was initialized by another owner",
                    )
                generation = state["generation"]
                _require_ready(owned, state)
                abandoned = state["operation"]
                if abandoned is not None and not _reclaimable(owned, abandoned):
                    raise _error(
                        "session_busy",
                        path,
                        "reconcile",
                        f"generation={state['generation']} operation={abandoned['id']} "
                        f"request={abandoned['pending_request']} unresolved; use recovery stop",
                        pending_request_id=abandoned["pending_request"],
                        generation=state["generation"],
                    )
                if abandoned is not None:
                    _cleanup_screenshots(owned, state)
                state["operation"] = {
                    "id": operation_id,
                    "pid": os.getpid(),
                    "composite": composite,
                    "started": False,
                    "pending_request": None,
                    "uncertain": False,
                }
                owned.write_json(_JOURNAL, state)
                current = Transaction(
                    owned, parent, grandparent, state["generation"], operation_id, created=create
                )
            token = _CURRENT.set((*_CURRENT.get(), current))
        except BaseException as exc:
            if create:
                try:
                    _rollback_prelaunch(
                        owned, parent, generation, operation_id, allow_uninitialized=True
                    )
                except (RuntimeError, OSError) as cleanup_error:
                    exc.add_note(f"Prelaunch rollback failed for {path}: {cleanup_error}")
            raise
        try:
            try:
                yield current
            except BaseException:
                # Preserve the worker's original error. Fencing/corruption must not cause
                # cleanup to overwrite state or obscure that original failure.
                try:
                    current._finish(failed=True)
                except (RuntimeError, OSError):
                    pass
                raise
            else:
                with current._startup_finalization or nullcontext():
                    current._finish()
        finally:
            current._active = False
            _CURRENT.reset(token)


class Recovery:
    def __init__(self, directory: _Directory, generation: str) -> None:
        self.generation = generation
        self._directory = directory
        self._owner = _execution()
        self._active = True

    def finish(self) -> None:
        """Retire work only after the caller has verified process/group termination."""
        if not self._active or self._owner != _execution():
            raise _error(
                "session_busy", self._directory.path, "recovery", "recovery lease is not owned"
            )
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = _read_state(self._directory)
            if (
                state is None
                or state["generation"] != self.generation
                or state["state"] not in ("stopping", "stopped")
            ):
                raise _error(
                    "session_generation_changed",
                    self._directory.path,
                    "recovery",
                    "generation changed",
                )
            _cleanup_screenshots(self._directory, state)
            state["state"] = "stopped"
            state["operation"] = None
            self._directory.write_json(_JOURNAL, state)


@contextmanager
def recovery(directory: Path) -> Iterator[Recovery]:
    """Fence immediately without the operation lock; failure deliberately leaves stopping."""
    path = Path(os.path.abspath(directory))
    with _leased_directory(path, create=False, lock=_STOP_LOCK) as (_, _, owned):
        with owned.lock(_STATE_LOCK, blocking=True):
            try:
                state = _read_state(owned)
            except DomainError as exc:
                if exc.code not in {"session_state_corrupt", "session_generation_changed"}:
                    raise
                # A broken journal can be replaced only on this still-owned inode.
                # Its new generation starts fenced; it is never exposed as ready.
                owned.check()
                state = _new_state(owned, "stopping")
                state["recovery_diagnostic"] = str(exc)[:512]
            if state is None:
                state = _new_state(owned, "stopping")
            state["state"] = "stopping"
            owned.write_json(_JOURNAL, state)
            lease = Recovery(owned, state["generation"])
        try:
            yield lease
        finally:
            lease._active = False


def transaction_status(directory: Path) -> dict[str, Any] | None:
    """Read an atomic journal snapshot without creating files or claiming ownership."""
    try:
        owned = _Directory(Path(os.path.abspath(directory)))
    except FileNotFoundError:
        return None
    try:
        try:
            return _read_state(owned)
        except RuntimeError as exc:
            return {"state": "unresolved", "error": str(exc)}
    finally:
        owned.close()


def archive_session(directory: Path, destination: Path) -> bool:
    """Move a caller-verified dead session only when no operation or recovery owns it."""
    path = Path(os.path.abspath(directory))
    try:
        with _leased_directory(path, create=False, lock=_OPERATION_LOCK) as (_, _, owned):
            with owned.lock(_STOP_LOCK), owned.lock(_STATE_LOCK, blocking=True):
                owned.check()
                os.rename(path, destination)
                return True
    except FileNotFoundError:
        return False
    except DomainError as exc:
        if exc.code in {"session_busy", "session_not_found", "session_generation_changed"}:
            return False
        raise
