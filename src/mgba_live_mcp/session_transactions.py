from __future__ import annotations

import asyncio
import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_JOURNAL = "transaction.json"
_OPERATION_LOCK = ".operation.lock"
_STATE_LOCK = ".state.lock"
_STOP_LOCK = ".stop.lock"
_CURRENT: ContextVar[tuple[Transaction, ...]] = ContextVar("session_transactions", default=())


def _error(code: str, directory: Path, stage: str, detail: str) -> RuntimeError:
    return RuntimeError(f"{code}: session={directory.name} stage={stage} {detail}")


def _execution() -> tuple[int, int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), task


class _Directory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
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

    @contextmanager
    def lock(self, name: str, *, blocking: bool = False) -> Iterator[None]:
        self.check()
        fd = os.open(
            name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )
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

    def write_json(self, name: str, payload: Any) -> None:
        self.check()
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
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


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically persist JSON without creating its parent or following a replacement directory."""
    directory = _Directory(Path(os.path.abspath(path.parent)))
    try:
        directory.write_json(path.name, payload)
    finally:
        directory.close()


@contextmanager
def _leased_directory(path: Path, *, create: bool, lock: str) -> Iterator[_Directory]:
    # This short, per-session parent lock closes mkdir -> operation-lock admission races.
    # Unlike the directory's operation lock, it is never held across emulator execution.
    parent = _Directory(path.parent)
    directory = None
    lease = None
    try:
        with parent.lock(f".{path.name}.initialization.lock"):
            if create:
                try:
                    os.mkdir(path.name, dir_fd=parent.fd)
                except FileExistsError as exc:
                    raise _error(
                        "session_exists", path, "reserve", "Session already exists"
                    ) from exc
            try:
                directory = _Directory(path)
            except FileNotFoundError as exc:
                raise _error("session_not_found", path, "acquire", "directory is missing") from exc
            lease = directory.lock(lock)
            lease.__enter__()
        yield directory
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)
        if directory is not None:
            directory.close()
        parent.close()


def _new_state(directory: _Directory, status: str) -> dict[str, Any]:
    return {
        "version": 1,
        "generation": uuid.uuid4().hex,
        "directory": directory.identity,
        "state": status,
        "operation": None,
    }


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


class Transaction:
    """Synchronous ownership retained by the actual worker, not its awaiting asyncio caller."""

    def __init__(
        self, directory: _Directory, generation: str, operation_id: str, *, created: bool
    ) -> None:
        self.generation = generation
        self._directory = directory
        self._operation_id = operation_id
        self._created = created
        self._owner = _execution()
        self._active = True

    def _owned_state(self) -> dict[str, Any]:
        if not self._active or self._owner != _execution():
            raise _error(
                "session_busy",
                self._directory.path,
                "ownership",
                "transaction is not this execution's",
            )
        state = _read_state(self._directory)
        if state is None or state["generation"] != self.generation:
            raise _error(
                "session_generation_changed",
                self._directory.path,
                "ownership",
                "generation changed",
            )
        _require_ready(self._directory, state)
        if state["operation"] is None or state["operation"]["id"] != self._operation_id:
            raise _error(
                "session_generation_changed", self._directory.path, "ownership", "operation retired"
            )
        return state

    def check(self) -> None:
        with self._directory.lock(_STATE_LOCK, blocking=True):
            self._owned_state()

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
                )
            operation["started"] = True
            operation["pending_request"] = request_id
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
            if create and not self._created:
                raise _error(
                    "session_exists", self._directory.path, "reserve", "Session already exists"
                )
            if composite and not state["operation"]["composite"]:
                state["operation"]["composite"] = True
                self._directory.write_json(_JOURNAL, state)

    def _finish(self) -> None:
        with self._directory.lock(_STATE_LOCK, blocking=True):
            state = self._owned_state()
            operation = state["operation"]
            if operation["pending_request"] is None and not operation["uncertain"]:
                state["operation"] = None
                self._directory.write_json(_JOURNAL, state)


@contextmanager
def transaction(
    directory: Path, *, create: bool = False, composite: bool = False
) -> Iterator[Transaction]:
    """Acquire without waiting; reclaim only provably resolved abandoned work.

    create=True reserves a new directory. Nested creation is permitted only inside that
    same reservation. Even failed creation keeps the directory for inspection/recovery.
    """
    path = Path(os.path.abspath(directory))
    for current in _CURRENT.get():
        if current._directory.path == path and current._active and current._owner == _execution():
            current._nest(create=create, composite=composite)
            yield current
            return

    with _leased_directory(path, create=create, lock=_OPERATION_LOCK) as owned:
        with owned.lock(_STATE_LOCK, blocking=True):
            state = _read_state(owned, initialize=True)
            assert state is not None
            _require_ready(owned, state)
            abandoned = state["operation"]
            if abandoned is not None and not _reclaimable(owned, abandoned):
                raise _error(
                    "session_busy",
                    path,
                    "reconcile",
                    f"generation={state['generation']} operation={abandoned['id']} "
                    f"request={abandoned['pending_request']} unresolved; use recovery stop",
                )
            operation_id = uuid.uuid4().hex
            state["operation"] = {
                "id": operation_id,
                "pid": os.getpid(),
                "composite": composite,
                "started": False,
                "pending_request": None,
                "uncertain": False,
            }
            owned.write_json(_JOURNAL, state)
            current = Transaction(owned, state["generation"], operation_id, created=create)
        token = _CURRENT.set((*_CURRENT.get(), current))
        try:
            try:
                yield current
            except BaseException:
                # Preserve the worker's original error. Fencing/corruption must not cause
                # cleanup to overwrite state or obscure that original failure.
                try:
                    current._finish()
                except (RuntimeError, OSError):
                    pass
                raise
            else:
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
            state["state"] = "stopped"
            state["operation"] = None
            self._directory.write_json(_JOURNAL, state)


@contextmanager
def recovery(directory: Path) -> Iterator[Recovery]:
    """Fence immediately without the operation lock; failure deliberately leaves stopping."""
    path = Path(os.path.abspath(directory))
    with _leased_directory(path, create=False, lock=_STOP_LOCK) as owned:
        with owned.lock(_STATE_LOCK, blocking=True):
            try:
                state = _read_state(owned)
            except RuntimeError as exc:
                if not str(exc).startswith(
                    ("session_state_corrupt:", "session_generation_changed:")
                ):
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
        with _leased_directory(path, create=False, lock=_OPERATION_LOCK) as owned:
            with owned.lock(_STOP_LOCK), owned.lock(_STATE_LOCK, blocking=True):
                owned.check()
                os.rename(path, destination)
                return True
    except FileNotFoundError:
        return False
    except RuntimeError as exc:
        if str(exc).startswith(
            ("session_busy:", "session_not_found:", "session_generation_changed:")
        ):
            return False
        raise
