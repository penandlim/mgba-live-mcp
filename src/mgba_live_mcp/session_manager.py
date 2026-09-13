"""Shared in-process runtime manager for live mGBA sessions."""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import process_control, session_transactions
from .errors import CommandTimeout, DomainError, error_context, error_payload

MODULE_PATH = Path(__file__).resolve()
PACKAGE_DIR = MODULE_PATH.parent
DEFAULT_BRIDGE_SCRIPT = PACKAGE_DIR / "resources" / "mgba_live_bridge.lua"
DEFAULT_RUNTIME_ROOT = Path.home() / ".mgba-live-mcp" / "runtime"


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def parse_int(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def to_lua_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def to_lua_value(value: Any) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return to_lua_string(value)
    if isinstance(value, (list, tuple)):
        inner = ", ".join(to_lua_value(v) for v in value)
        return f"{{{inner}}}"
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value.keys(), key=str):
            k = str(key)
            if k.isidentifier():
                parts.append(f"{k} = {to_lua_value(value[key])}")
            else:
                parts.append(f"[{to_lua_string(k)}] = {to_lua_value(value[key])}")
        return "{ " + ", ".join(parts) + " }"
    raise TypeError(f"Unsupported command value type: {type(value)}")


def default_fps_target() -> float:
    # Default to 120 so script mode avoids common half-speed behavior at 60.
    return 120.0


def format_process_exit(returncode: int | None) -> str:
    if returncode is None:
        return "unknown exit status"
    if returncode < 0:
        signum = -returncode
        try:
            return f"signal {signum} ({signal.Signals(signum).name})"
        except ValueError:
            return f"signal {signum}"
    return f"exit code {returncode}"


class SessionManager:
    """Manage live mGBA sessions and bridge commands in-process."""

    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        bridge_script: Path | None = None,
    ) -> None:
        self.runtime_root = (runtime_root or DEFAULT_RUNTIME_ROOT).resolve()
        self.sessions_dir = self.runtime_root / "sessions"
        self.archived_sessions_dir = self.runtime_root / "archived_sessions"
        self.active_session_file = self.runtime_root / "active_session"
        self.bridge_script = (bridge_script or DEFAULT_BRIDGE_SCRIPT).resolve()

    def transaction(
        self, session: str, *, create: bool = False, composite: bool = False
    ) -> AbstractContextManager[session_transactions.Transaction]:
        if not create and self.session_file(session).exists():
            self.load_session(session)
        return session_transactions.transaction(
            self.session_dir(session), create=create, composite=composite
        )

    def _process_state(self, session: dict[str, Any]) -> str:
        return process_control.process_state(
            int(session["pid"]),
            session.get("process_identity"),
            reap=session.get("ready") is True,
        )

    @staticmethod
    def validate_session_id(session_id: Any) -> str:
        if (
            not isinstance(session_id, str)
            or not session_id
            or session_id in {".", ".."}
            or any(character in session_id for character in ("/", "\\", "\0"))
            or Path(session_id).is_absolute()
        ):
            raise DomainError(
                "invalid_arguments",
                "Session IDs must be nonempty single path components, not dot names or paths.",
                phase="validation",
                execution_outcome="not_started",
            )
        return session_id

    def _managed_path(self, path: Path) -> Path:
        relative = path.relative_to(self.runtime_root)
        current = self.runtime_root
        for component in ("", *relative.parts):
            current /= component
            if current.is_symlink():
                raise DomainError(
                    "invalid_arguments",
                    f"Managed session paths cannot be symlinks: {current}",
                    phase="validation",
                    execution_outcome="not_started",
                )
        return path

    def ensure_runtime_dirs(self) -> None:
        sessions = self._managed_path(self.sessions_dir)
        archives = self._managed_path(self.archived_sessions_dir)
        sessions.mkdir(parents=True, exist_ok=True)
        archives.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self._managed_path(self.sessions_dir / self.validate_session_id(session_id))

    def _session_path(self, session_id: str, name: str) -> Path:
        return self._managed_path(self.session_dir(session_id) / name)

    def session_file(self, session_id: str) -> Path:
        return self._session_path(session_id, "session.json")

    def archive_session_destination(self, session_id: str) -> Path:
        self.validate_session_id(session_id)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        # Archive names are internal; do not append a suffix to a maximum-length valid ID.
        name = f"{session_id[:32]}-{stamp}-{uuid.uuid4().hex}"
        return self._managed_path(self.archived_sessions_dir / name)

    def _validate_session_metadata(self, data: Any, session_id: str) -> dict[str, Any]:
        self.validate_session_id(session_id)
        if not isinstance(data, dict) or data.get("id") != session_id:
            raise DomainError(
                "session_state_corrupt",
                "Stored session ID does not match its managed directory.",
                phase="admission",
                execution_outcome="not_started",
                session_id=session_id,
            )
        expected = {
            "session_dir": self.session_dir(session_id),
            **{
                field: self._session_path(session_id, filename)
                for field, filename in (
                    ("command_path", "command.lua"),
                    ("response_path", "response.json"),
                    ("heartbeat_path", "heartbeat.json"),
                    ("stdout_log", "stdout.log"),
                    ("stderr_log", "stderr.log"),
                )
            },
        }
        for field, path in expected.items():
            if field in data and (not isinstance(data[field], str) or Path(data[field]) != path):
                raise DomainError(
                    "session_state_corrupt",
                    f"Stored {field} does not name the managed session path.",
                    phase="admission",
                    execution_outcome="not_started",
                    session_id=session_id,
                )
        return data

    def load_session(self, session_id: str) -> dict[str, Any]:
        self.session_file(session_id)
        directory = session_transactions._Directory(self.session_dir(session_id))
        try:
            data = directory.read_json("session.json")
            directory.check()
        finally:
            directory.close()
        return self._validate_session_metadata(data, session_id)

    def write_session(self, data: dict[str, Any]) -> None:
        session_id = self.validate_session_id(data.get("id"))
        self._validate_session_metadata(data, session_id)
        session_transactions.atomic_write_json(self.session_file(session_id), data)

    def iter_sessions(self) -> list[dict[str, Any]]:
        sessions = self._managed_path(self.sessions_dir)
        if not sessions.exists():
            return []
        items: list[tuple[float, dict[str, Any]]] = []
        for directory in sessions.iterdir():
            try:
                if directory.is_symlink() or not directory.is_dir():
                    continue
                session = self.load_session(directory.name)
                modified = self.session_file(directory.name).stat(follow_symlinks=False).st_mtime
                items.append((modified, session))
            except (OSError, ValueError, DomainError):
                continue
        return [session for _, session in sorted(items, key=lambda item: item[0], reverse=True)]

    def read_log_excerpt(self, path: Path, max_chars: int = 4000) -> str:
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            return ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    @staticmethod
    def _read_active_marker(directory: session_transactions._Directory) -> str | None:
        directory.check()
        try:
            fd = os.open(
                "active_session",
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory.fd,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8", newline="") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DomainError(
                    "io_error",
                    "Active session marker is not a regular file.",
                    phase="active_marker",
                )
            value = stream.read()
        directory.check()
        return value

    @staticmethod
    def _write_active_marker(
        directory: session_transactions._Directory, session_id: str | None
    ) -> None:
        if session_id is not None:
            directory.write_text("active_session", session_id)
        else:
            directory.check()
            try:
                os.unlink("active_session", dir_fd=directory.fd)
            except FileNotFoundError:
                return
            os.fsync(directory.fd)
            directory.check()

    @contextmanager
    def _active_marker(
        self,
        *,
        startup: bool = False,
        directory: session_transactions._Directory | None = None,
    ) -> Iterator[session_transactions._Directory]:
        close_directory = directory is None
        if directory is None:
            directory = session_transactions._Directory(self._managed_path(self.runtime_root))
        try:
            with directory.lock(".active_session.lock", blocking=True):
                previous = self._read_active_marker(directory)
                try:
                    yield directory
                except BaseException as exc:
                    restoration: dict[str, Any] = {"previous_session": previous}
                    try:
                        self._write_active_marker(directory, previous)
                        restoration["confirmed"] = True
                    except (OSError, DomainError) as restore_error:
                        restoration.update(confirmed=False, error=str(restore_error))
                    if not isinstance(exc, Exception):
                        exc.add_note(f"Active marker restoration: {restoration}")
                        raise
                    failure = (
                        exc
                        if isinstance(exc, DomainError)
                        else DomainError(
                            "startup_failed" if startup else "io_error",
                            str(exc),
                            phase="activation",
                            execution_outcome="unknown",
                        )
                    )
                    failure.context["active_marker_restore"] = restoration
                    try:
                        failure.context["active_session"] = self._read_active_marker(directory)
                    except (OSError, DomainError, ValueError) as observation:
                        failure.context["active_session_error"] = str(observation)
                    if failure is exc:
                        raise
                    raise failure from exc
        finally:
            if close_directory:
                directory.close()

    def set_active_session(self, session_id: str) -> None:
        self.session_dir(session_id)
        with self._active_marker() as directory:
            self._write_active_marker(directory, session_id)

    def get_active_session_id(self) -> str | None:
        try:
            directory = session_transactions._Directory(self._managed_path(self.runtime_root))
        except FileNotFoundError:
            return None
        try:
            value = self._read_active_marker(directory)
        finally:
            directory.close()
        return self.validate_session_id(value) if value is not None else None

    def _refresh_active_session(self) -> None:
        if not self._managed_path(self.runtime_root).exists():
            return
        with self._active_marker() as directory:
            active = self._read_active_marker(directory)
            if active:
                active_path = self.session_file(active)
                if active_path.exists():
                    try:
                        active_session = self.load_session(active)
                        active_state = (
                            self._process_state(active_session)
                            if active_session.get("startup", {}).get("state") != "failed"
                            else "dead"
                        )
                    except Exception:
                        active_state = None
                    if active_state is not None and active_state != "dead":
                        return

            for candidate in self.iter_sessions():
                try:
                    eligible = (
                        candidate.get("ready") is not False
                        and candidate.get("startup", {}).get("state") not in {"starting", "failed"}
                        and self._process_state(candidate) != "dead"
                    )
                except Exception:
                    continue
                if eligible:
                    self._write_active_marker(directory, candidate["id"])
                    return
            self._write_active_marker(directory, None)

    def prune_dead_sessions(self) -> list[str]:
        removed: list[str] = []
        for session in self.iter_sessions():
            if self._process_state(session) != "dead":
                continue
            session_id = session["id"]
            directory = self.session_dir(session_id)
            if session.get("startup", {}).get("state") == "failed":
                continue
            try:
                self._managed_path(self.archived_sessions_dir).mkdir(parents=True, exist_ok=True)
                archived = self.archive_session_destination(session_id)
                if session_transactions.archive_session(directory, archived):
                    removed.append(session_id)
            except OSError:
                continue
        self._refresh_active_session()
        return removed

    def detect_mgba_binary(self) -> str:
        for candidate in ("mgba-qt", "mgba", "mGBA"):
            path = shutil.which(candidate)
            if path:
                return path
        raise DomainError(
            "resource_not_found",
            "No mGBA binary found in PATH (expected mgba-qt/mgba/mGBA).",
            phase="startup",
            execution_outcome="not_started",
        )

    def require_session(
        self, session_id: str | None, *, require_alive: bool = True
    ) -> dict[str, Any]:
        if session_id is None:
            raise DomainError(
                "session_required",
                "session is required.",
                phase="validation",
                execution_outcome="not_started",
            )

        path = self.session_file(session_id)
        if not path.exists():
            raise DomainError(
                "session_not_found",
                f"Session not found: {session_id}",
                phase="admission",
                execution_outcome="not_started",
                session_id=session_id,
            )

        try:
            session = self.load_session(session_id)
        except (OSError, ValueError) as exc:
            raise DomainError(
                "session_state_corrupt",
                f"Cannot read session metadata: {exc}",
                phase="admission",
                execution_outcome="not_started",
                session_id=session_id,
            ) from exc
        if require_alive:
            state = self._process_state(session)
            if state != "alive":
                code = "session_dead" if state == "dead" else state
                raise DomainError(
                    code,
                    f"session '{session_id}' process is {state}.",
                    phase="admission",
                    execution_outcome="not_started",
                    session_id=session_id,
                    pid=session.get("pid"),
                )
        return session

    def resolve_attach_target(
        self,
        *,
        session: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.session_dir(session)
        if pid is not None:
            for candidate in self.iter_sessions():
                if int(candidate["pid"]) == pid:
                    session = candidate["id"]
                    break
            if not session:
                raise DomainError(
                    "session_not_found",
                    "PID is not a managed live session started by mgba-live-mcp.",
                    phase="admission",
                    execution_outcome="not_started",
                    pid=pid,
                )
        if not session:
            raise DomainError(
                "session_required",
                "provide session or pid.",
                phase="validation",
                execution_outcome="not_started",
            )
        return self.require_session(session, require_alive=True)

    def write_command(
        self,
        command_path: Path,
        command: dict[str, Any],
        *,
        _directory: session_transactions._Directory | None = None,
    ) -> None:
        expected = self._session_path(command_path.parent.name, "command.lua")
        if command_path != expected:
            raise DomainError(
                "invalid_arguments",
                "Bridge commands must use their managed session path.",
                phase="validation",
                execution_outcome="not_started",
            )
        lua_doc = "return " + to_lua_value(command) + "\n"
        if _directory is None:
            session_transactions.atomic_write_text(command_path, lua_doc)
        else:
            _directory.write_text(command_path.name, lua_doc)

    def send_command(
        self,
        session: dict[str, Any],
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
        _startup_process: subprocess.Popen[Any] | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self._validate_session_metadata(session, self.validate_session_id(session.get("id")))
        command_path = Path(session["command_path"])
        response_path = Path(session["response_path"])
        request_id = uuid.uuid4().hex
        with (
            error_context("command", session_id=session.get("id"), request_id=request_id),
            session_transactions.transaction(command_path.parent) as operation,
        ):
            if session.get("generation", operation.generation) != operation.generation:
                raise DomainError(
                    "session_generation_changed",
                    "session metadata is stale.",
                    phase="admission",
                    execution_outcome="not_started",
                    session_id=session.get("id"),
                )
            if "pid" in session:
                state = self._process_state(session)
                if state in {"dead", "identity_mismatch"}:
                    raise DomainError(
                        "session_dead" if state == "dead" else state,
                        f"Process is {state} before publishing '{kind}'.",
                        phase="admission",
                        execution_outcome="not_started",
                        session_id=session.get("id"),
                        pid=session["pid"],
                    )
            command = {"id": request_id, "kind": kind, **(payload or {})}

            def publish() -> None:
                if command_path.exists():
                    raise DomainError(
                        "session_busy",
                        "an unclaimed bridge command still exists.",
                        phase="publish",
                        execution_outcome="not_started",
                        session_id=session.get("id"),
                        request_id=request_id,
                    )
                try:
                    os.unlink(response_path.name, dir_fd=operation._directory.fd)
                except FileNotFoundError:
                    pass
                self.write_command(command_path, command, _directory=operation._directory)

            operation.publish(request_id, publish)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                operation.check()
                if _startup_process is not None and _startup_process.poll() is not None:
                    raise DomainError(
                        "session_dead",
                        "process exited before bridge readiness.",
                        phase="startup",
                        session_id=session.get("id"),
                        request_id=request_id,
                    )
                try:
                    response = json.loads(response_path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    response = None
                if isinstance(response, dict) and response.get("id") == request_id:
                    operation.complete(request_id)
                    return response
                if "pid" in session:
                    state = self._process_state(session)
                    if state in {"dead", "identity_mismatch"}:
                        raise DomainError(
                            "session_dead" if state == "dead" else state,
                            f"Session process became {state} during '{kind}'.",
                            phase="command",
                            session_id=session.get("id"),
                            request_id=request_id,
                        )
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            raise CommandTimeout(
                "command_timeout",
                f"Timed out waiting for response to command '{kind}' "
                f"(request_id={request_id}; execution outcome unknown). "
                "The session remains busy until the response arrives or recovery stop succeeds.",
                phase="command",
                session_id=session.get("id"),
                request_id=request_id,
            )

    def handle_response(self, response: dict[str, Any], *, session_id: str | None = None) -> Any:
        if not response.get("ok"):
            raise DomainError(
                "bridge_error",
                str(response.get("error", "unknown")),
                phase="command",
                request_id=response.get("id"),
                session_id=session_id,
            )
        return response.get("data")

    @staticmethod
    def _local_file(value: Any, label: str) -> Path:
        if not isinstance(value, (str, os.PathLike)) or not str(value) or "\0" in str(value):
            raise DomainError(
                "invalid_arguments",
                f"{label} must be a nonempty file path.",
                phase="validation",
                execution_outcome="not_started",
            )
        path = Path(value)
        try:
            path = path.resolve()
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise DomainError(
                        "invalid_arguments",
                        f"{label} is not a regular file: {path}",
                        phase="validation",
                        execution_outcome="not_started",
                    )
            finally:
                os.close(fd)
        except DomainError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise DomainError(
                "resource_not_found" if isinstance(exc, FileNotFoundError) else "io_error",
                f"Cannot read {label}: {path}: {exc}",
                phase="validation",
                execution_outcome="not_started",
            ) from exc
        return path

    def resolve_startup_scripts(self, script_paths: list[str]) -> list[str]:
        return [str(self._local_file(script, "startup script")) for script in script_paths]

    def _validate_start_options(self, supplied: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {
            "savestate": None,
            "fps_target": None,
            "fast": False,
            "mgba_path": None,
            "session_id": None,
            "script": None,
            "log_level": 0,
            "heartbeat_interval": 30,
            "ready_timeout": 20.0,
            "config": None,
            "_activate": True,
        }
        unknown = supplied.keys() - options.keys() - {"rom"}
        if unknown:
            raise DomainError(
                "invalid_arguments",
                f"Unknown startup options: {', '.join(sorted(unknown))}",
                phase="validation",
                execution_outcome="not_started",
            )
        options.update(supplied)
        session_id = options["session_id"]
        if session_id is not None:
            self.session_dir(session_id)
        for name in ("fast", "_activate"):
            if not isinstance(options[name], bool):
                raise DomainError(
                    "invalid_arguments",
                    f"{name} must be a boolean.",
                    phase="validation",
                    execution_outcome="not_started",
                )
        if options["fps_target"] is None:
            options["fps_target"] = 600.0 if options["fast"] else default_fps_target()
        for name in ("fps_target", "ready_timeout", "heartbeat_interval", "log_level"):
            value = options[name]
            integer = name in {"heartbeat_interval", "log_level"}
            try:
                finite = math.isfinite(value)
            except (TypeError, OverflowError):
                finite = False
            if (
                isinstance(value, bool)
                or not isinstance(value, int if integer else (int, float))
                or not finite
                or (value < 0 if name == "log_level" else value <= 0)
            ):
                raise DomainError(
                    "invalid_arguments",
                    f"{name} must be a finite "
                    f"{'nonnegative' if name == 'log_level' else 'positive'} "
                    f"{'integer' if integer else 'number'}.",
                    phase="validation",
                    execution_outcome="not_started",
                )
        for name in ("script", "config"):
            values = options[name]
            if values is not None and (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value or "\0" in value for value in values)
            ):
                raise DomainError(
                    "invalid_arguments",
                    f"{name} must be a list of nonempty strings without NUL bytes.",
                    phase="validation",
                    execution_outcome="not_started",
                )
            options[name] = (
                self.resolve_startup_scripts(values or []) if name == "script" else values or []
            )
        options["rom"] = str(self._local_file(options.get("rom"), "ROM"))
        if options["savestate"] is not None:
            options["savestate"] = str(self._local_file(options["savestate"], "savestate"))
        self._local_file(self.bridge_script, "bridge script")
        executable = options["mgba_path"]
        if executable is None:
            executable = self.detect_mgba_binary()
        if not isinstance(executable, str) or not executable or "\0" in executable:
            raise DomainError(
                "invalid_arguments",
                "mgba_path must name an executable.",
                phase="validation",
                execution_outcome="not_started",
            )
        resolved = shutil.which(executable)
        if resolved is None or not Path(resolved).is_file():
            raise DomainError(
                "resource_not_found",
                f"mGBA executable not found or not executable: {executable}",
                phase="validation",
                execution_outcome="not_started",
            )
        options["mgba_path"] = str(Path(resolved).resolve())
        return options

    def _validate_lua_source(self, file: str | None, code: str | None) -> str | None:
        if bool(file) == bool(code):
            raise DomainError(
                "invalid_arguments",
                "Exactly one of file or code is required.",
                phase="validation",
                execution_outcome="not_started",
            )
        if file:
            return str(self._local_file(file, "Lua source"))
        elif not isinstance(code, str):
            raise DomainError(
                "invalid_arguments",
                "Lua code must be a string.",
                phase="validation",
                execution_outcome="not_started",
            )

    def prepare_bridge_script(self, directory: session_transactions._Directory) -> Path:
        return directory.copy_file(self.bridge_script, self.bridge_script.name)

    def build_start_command(
        self,
        *,
        mgba_path: str,
        fps_target: float,
        config_overrides: list[str],
        savestate: str | None,
        startup_scripts: list[str],
        bridge_script: Path | None = None,
        log_level: int,
        rom: Path,
    ) -> list[str]:
        bridge_path = self.bridge_script if bridge_script is None else bridge_script
        cmd = [
            mgba_path,
            "-C",
            f"fpsTarget={fps_target:g}",
            "-s",
            "0",
        ]
        for override in config_overrides:
            cmd.extend(["-C", override])
        if savestate:
            cmd.extend(["-t", str(Path(savestate).resolve())])
        for startup_script in startup_scripts:
            cmd.extend(["--script", startup_script])
        cmd.extend(["--script", str(bridge_path), "-l", str(log_level), str(rom)])
        return cmd

    def start(
        self,
        *,
        rom: str,
        savestate: str | None = None,
        fps_target: float | None = None,
        fast: bool = False,
        mgba_path: str | None = None,
        session_id: str | None = None,
        script: list[str] | None = None,
        log_level: int = 0,
        heartbeat_interval: int = 30,
        ready_timeout: float = 20.0,
        config: list[str] | None = None,
        _activate: bool = True,
    ) -> dict[str, Any]:
        options = self._validate_start_options(
            {
                "rom": rom,
                "savestate": savestate,
                "fps_target": fps_target,
                "fast": fast,
                "mgba_path": mgba_path,
                "session_id": session_id,
                "script": script,
                "log_level": log_level,
                "heartbeat_interval": heartbeat_interval,
                "ready_timeout": ready_timeout,
                "config": config,
                "_activate": _activate,
            }
        )
        resolved_session_id = session_id if session_id is not None else self._new_session_id()
        self.ensure_runtime_dirs()
        # Release the status-reader gate only after transaction finalization and diagnostics.
        with (
            ExitStack() as child_cleanup,
            error_context("startup", session_id=resolved_session_id),
            self.transaction(resolved_session_id, create=True, composite=True) as operation,
        ):
            proc: subprocess.Popen[Any] | None = None
            registered = False
            release_reaper = None
            phase = "staging"
            try:
                sdir = self.session_dir(resolved_session_id)
                session: dict[str, Any] = {
                    "id": resolved_session_id,
                    "generation": operation.generation,
                    "ready": False,
                    "rom": options["rom"],
                    "fps_target": options["fps_target"],
                    "mgba_path": options["mgba_path"],
                    "startup_scripts": options["script"],
                    "startup": {"state": "starting"},
                    "created_at": now_utc(),
                    "session_dir": str(sdir),
                    **{
                        field: str(self._session_path(resolved_session_id, filename))
                        for field, filename in (
                            ("command_path", "command.lua"),
                            ("response_path", "response.json"),
                            ("heartbeat_path", "heartbeat.json"),
                            ("stdout_log", "stdout.log"),
                            ("stderr_log", "stderr.log"),
                        )
                    },
                }
                directory = operation._directory
                directory.mkdir("screenshots")
                with directory.child("scripts", create=True) as scripts_dir:
                    session_bridge = self.prepare_bridge_script(scripts_dir)
                    staged_scripts = [
                        str(scripts_dir.copy_file(Path(source), f"startup-{index}.lua"))
                        for index, source in enumerate(options["script"])
                    ]
                staged_savestate = options["savestate"]
                if staged_savestate is not None:
                    staged_savestate = str(
                        directory.copy_file(Path(staged_savestate), "initial.ss")
                    )
                command = self.build_start_command(
                    mgba_path=options["mgba_path"],
                    fps_target=options["fps_target"],
                    config_overrides=options["config"],
                    savestate=staged_savestate,
                    startup_scripts=staged_scripts,
                    bridge_script=session_bridge,
                    log_level=options["log_level"],
                    rom=Path(options["rom"]),
                )
                env = os.environ.copy()
                env["MGBA_LIVE_SESSION_DIR"] = str(sdir)
                env["MGBA_LIVE_COMMAND"] = session["command_path"]
                env["MGBA_LIVE_RESPONSE"] = session["response_path"]
                env["MGBA_LIVE_HEARTBEAT"] = session["heartbeat_path"]
                env["MGBA_LIVE_HEARTBEAT_INTERVAL"] = str(options["heartbeat_interval"])
                phase = "spawn"
                with (
                    directory.create_file("stdout.log") as stdout_f,
                    directory.create_file("stderr.log") as stderr_f,
                    operation.startup_guard(),
                ):
                    proc = subprocess.Popen(
                        command,
                        cwd=str(sdir),
                        env=env,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        start_new_session=True,
                    )
                    session["pid"] = proc.pid
                    phase = "registration"
                    session["process_identity"] = process_control.capture_identity(proc.pid)
                    process_control.retain_child(proc, session["process_identity"])
                    operation.write_metadata(session)
                    release_reaper = process_control.watch_child(proc)
                    child_cleanup.callback(release_reaper.set)
                    registered = True
                phase = "readiness"
                self.handle_response(
                    self.send_command(
                        session,
                        "ping",
                        timeout=options["ready_timeout"],
                        _startup_process=proc,
                    ),
                    session_id=resolved_session_id,
                )
                with operation.startup_guard():
                    session["ready"] = True
                    session["startup"] = {"state": "ready"}
                    operation.write_metadata(session)
                if options["_activate"]:
                    operation._startup_finalization = self._activate_startup(
                        operation,
                        resolved_session_id,
                        lambda exc: self._postspawn_failure(
                            exc, operation, session, proc, "activation", registered
                        ),
                    )
            except BaseException as exc:
                if proc is None:
                    failure = self._prelaunch_failure(exc, operation, resolved_session_id, phase)
                else:
                    failure = self._postspawn_failure(
                        exc, operation, session, proc, phase, registered
                    )
                if isinstance(exc, Exception):
                    raise failure from exc
                exc.add_note(str(failure))
                raise
            finally:
                if release_reaper is None and proc is not None:
                    process_control.forget_child(proc)
            return {
                "status": "started",
                "session_id": resolved_session_id,
                "pid": proc.pid,
                "fps_target": options["fps_target"],
                "session_dir": str(sdir),
            }

    @contextmanager
    def _activate_startup(
        self,
        operation: session_transactions.Transaction,
        session_id: str,
        on_failure: Callable[[BaseException], DomainError],
    ) -> Iterator[None]:
        try:
            # Lock order is singleton marker first, then the short session state guard.
            with self._active_marker(startup=True, directory=operation._grandparent) as directory:
                with operation.startup_guard():
                    self._write_active_marker(directory, session_id)
                yield
        except BaseException as exc:
            failure = on_failure(exc)
            if isinstance(exc, Exception):
                if failure is exc:
                    raise
                raise failure from exc
            exc.add_note(str(failure))
            raise

    @staticmethod
    def _prelaunch_failure(
        exc: BaseException,
        operation: session_transactions.Transaction,
        session_id: str,
        phase: str,
    ) -> DomainError:
        failure = (
            exc
            if isinstance(exc, DomainError)
            else DomainError(
                "io_error" if isinstance(exc, OSError) else "startup_failed",
                str(exc),
                phase=phase,
                execution_outcome="not_started",
            )
        )
        failure.execution_outcome = "not_started"
        failure.context["session_id"] = session_id
        try:
            operation.rollback()
        except BaseException as cleanup:
            failure.context["rollback_error"] = str(cleanup)
        return failure

    def _postspawn_failure(
        self,
        exc: BaseException,
        operation: session_transactions.Transaction,
        session: dict[str, Any],
        proc: subprocess.Popen[Any],
        phase: str,
        registered: bool,
    ) -> DomainError:
        returncode = proc.poll()
        details = [
            (
                f"mGBA process exited early with {format_process_exit(returncode)}."
                if returncode is not None
                else f"Session '{session['id']}' {phase} failed: {exc}"
            ),
            f"Session dir: {session['session_dir']}",
        ]
        for name in ("stderr_log", "stdout_log"):
            try:
                excerpt = self.read_log_excerpt(self._managed_path(Path(session[name])))
                if excerpt:
                    details.append(f"{name}:\n{excerpt}")
                    break
            except (OSError, DomainError):
                continue
        failure = (
            exc
            if isinstance(exc, DomainError)
            else DomainError("startup_failed", str(exc), phase=phase)
        )
        failure.message = "\n".join(details)
        if failure.execution_outcome == "not_started":
            failure.execution_outcome = "partial"
        failure.context.update(
            session_id=session["id"],
            generation=operation.generation,
            pid=proc.pid,
            session_dir=session["session_dir"],
            stdout_log=session["stdout_log"],
            stderr_log=session["stderr_log"],
            exit_code=returncode,
            process_identity=session.get("process_identity"),
        )
        if registered:
            try:
                operation._directory.check()
            except (OSError, DomainError):
                # An inaccessible registration cannot be recovered by session id.
                registered = False
        if not registered:
            try:
                outcome = process_control.terminate_owned_process(
                    proc.pid, session.get("process_identity"), grace=1.0
                )
                failure.context["cleanup"] = {"confirmed": True, "outcome": outcome}
            except BaseException as cleanup:
                failure.context["cleanup"] = {
                    "confirmed": False,
                    "error": str(cleanup),
                }
        failure.context["process_state"] = self._process_state(session)
        session["startup"] = {"state": "failed", "error": error_payload(failure)["error"]}
        try:
            operation.write_metadata(session)
        except BaseException as publication:
            failure.context["metadata_persisted"] = False
            failure.context["metadata_error"] = str(publication)
        else:
            failure.context["metadata_persisted"] = True
        return failure

    def _new_session_id(self) -> str:
        return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex}"

    def attach(
        self,
        *,
        session: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        target = self.resolve_attach_target(session=session, pid=pid)
        target_id = str(target["id"])
        self.set_active_session(target_id)
        return {
            "status": "attached",
            "session_id": target_id,
            "pid": target["pid"],
            "rom": target["rom"],
            "fps_target": target["fps_target"],
            "mgba_path": target.get("mgba_path"),
        }

    def _status_payload(self, session: dict[str, Any]) -> dict[str, Any]:
        heartbeat = None
        hb_path = Path(session["heartbeat_path"])
        if hb_path.exists():
            try:
                heartbeat = json.loads(hb_path.read_text())
            except Exception:
                heartbeat = None
        state = self._process_state(session)
        return {
            "session_id": session["id"],
            "pid": session["pid"],
            "alive": 0 < int(session["pid"]) <= 0x7FFFFFFF and state != "dead",
            "process_state": state,
            "identity_verified": state == "alive",
            "transaction": session_transactions.transaction_status(self.session_dir(session["id"])),
            "startup": session.get("startup"),
            "rom": session["rom"],
            "fps_target": session["fps_target"],
            "mgba_path": session.get("mgba_path"),
            "heartbeat": heartbeat,
            "is_active": self.get_active_session_id() == session["id"],
            "session_dir": session["session_dir"],
        }

    def status(
        self, *, session: str | None = None, all: bool = False
    ) -> dict[str, Any] | list[dict[str, Any]]:
        if session is not None:
            self.session_dir(session)
            if not all:
                self.load_session(session)
        elif not all:
            self.require_session(None)
        self.prune_dead_sessions()
        if all:
            payloads: list[dict[str, Any]] = []
            for candidate in self.iter_sessions():
                if (
                    self._process_state(candidate) == "dead"
                    and candidate.get("startup", {}).get("state") != "failed"
                ):
                    continue
                payloads.append(self._status_payload(candidate))
            return payloads
        return self._status_payload(self.require_session(session, require_alive=False))

    def stop(self, *, session: str, grace: float = 1.0) -> dict[str, Any]:
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("grace must be a finite non-negative number")
        if self.session_file(session).exists():
            self.load_session(session)
        with session_transactions.recovery(self.session_dir(session)) as recovery:
            target = self.require_session(session, require_alive=False)
            pid = int(target["pid"])
            try:
                outcome = process_control.terminate_owned_process(
                    pid, target.get("process_identity"), grace=grace
                )
            except DomainError as exc:
                exc.context.update(session_id=session, generation=recovery.generation)
                raise
            recovery.finish()
            views_dir = self._session_path(session, ".views")
            cleanup_errors = []
            if target.get("startup", {}).get("state") == "failed":
                target["startup"]["state"] = "stopped"
                try:
                    self.write_session(target)
                except (OSError, DomainError) as exc:
                    cleanup_errors.append(f"Failed to update startup diagnostics: {exc}")
            for pending_view in views_dir.glob("*.png"):
                try:
                    pending_view.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(f"{pending_view}: {exc}")
            if self.get_active_session_id() == target["id"]:
                self._refresh_active_session()
        payload: dict[str, Any] = {
            "session_id": target["id"],
            "pid": pid,
            "alive_before": outcome != "already_exited",
            "alive_after": False,
            "stopped": outcome == "stopped",
            "outcome": outcome,
        }
        if cleanup_errors:
            payload["cleanup_errors"] = cleanup_errors
        return payload

    def run_lua(
        self,
        *,
        session: str,
        file: str | None = None,
        code: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        if bool(file) == bool(code):
            raise ValueError("Exactly one of file or code is required.")
        if file:
            script_path = Path(file).resolve()
            if not script_path.exists():
                raise DomainError(
                    "resource_not_found",
                    f"Lua file not found: {script_path}",
                    phase="validation",
                    execution_outcome="not_started",
                    session_id=session,
                )
            response = self.send_command(
                target, "run_lua_file", {"path": str(script_path)}, timeout=timeout
            )
        else:
            response = self.send_command(
                target, "run_lua_inline", {"code": str(code)}, timeout=timeout
            )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "data": data,
        }

    def input_tap(
        self,
        *,
        session: str,
        key: str,
        frames: int = 1,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(
            target,
            "tap_key",
            {"key": key, "duration": frames},
            timeout=timeout,
        )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "data": data,
        }

    def input_set(
        self,
        *,
        session: str,
        keys: list[str],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(target, "set_keys", {"keys": keys}, timeout=timeout)
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "data": data,
        }

    def input_clear(
        self,
        *,
        session: str,
        keys: list[str] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        payload: dict[str, Any] = {}
        if keys:
            payload["keys"] = keys
        response = self.send_command(target, "clear_keys", payload, timeout=timeout)
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "data": data,
        }

    def screenshot(
        self,
        *,
        session: str,
        out: str | None = None,
        no_save: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        with self.transaction(session):
            return self._screenshot(session=session, out=out, no_save=no_save, timeout=timeout)

    def _screenshot(
        self,
        *,
        session: str,
        out: str | None = None,
        no_save: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        if no_save and out:
            raise ValueError("Use either out or no_save, not both.")

        if no_save:
            views_dir = self._session_path(session, ".views")
            views_dir.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=views_dir, suffix=".png", delete=False) as tmp:
                out_path = Path(tmp.name).resolve()
            result_path = out_path
            completed = False
            try:
                response = self.send_command(
                    target, "screenshot", {"path": str(out_path)}, timeout=timeout
                )
                completed = True
                data = self.handle_response(response, session_id=target["id"])
                if isinstance(data, dict) and isinstance(data.get("path"), str):
                    result_path = Path(data["path"])
                if result_path != out_path:
                    raise DomainError(
                        "snapshot_failed",
                        "bridge returned an unexpected output path.",
                        phase="snapshot",
                        execution_outcome="partial",
                        session_id=session,
                        request_id=response.get("id"),
                    )
                try:
                    png_bytes = result_path.read_bytes()
                except OSError as exc:
                    raise DomainError(
                        "snapshot_failed",
                        str(exc),
                        phase="snapshot",
                        execution_outcome="partial",
                        session_id=session,
                        request_id=response.get("id"),
                    ) from exc
                return {
                    "session_id": target["id"],
                    "frame": response.get("frame"),
                    "png_base64": base64.b64encode(png_bytes).decode(),
                }
            finally:
                if completed or ("pid" in target and self._process_state(target) == "dead"):
                    try:
                        out_path.unlink()
                    except FileNotFoundError:
                        pass

        if out:
            out_path = Path(out).resolve()
        else:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_path = self._session_path(target["id"], f"screenshots/screenshot-{ts}.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.send_command(target, "screenshot", {"path": str(out_path)}, timeout=timeout)
        data = self.handle_response(response, session_id=target["id"])
        result_path = Path(data.get("path") if isinstance(data, dict) else str(out_path))
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "path": str(result_path),
        }

    def get_view(self, *, session: str, timeout: float = 20.0) -> dict[str, Any]:
        return self.screenshot(session=session, no_save=True, timeout=timeout)

    def read_memory(
        self,
        *,
        session: str,
        addresses: list[int | str],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(
            target,
            "read_memory",
            {"addresses": [parse_int(address) for address in addresses]},
            timeout=timeout,
        )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "memory": data,
        }

    def read_range(
        self,
        *,
        session: str,
        start: int | str,
        length: int,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(
            target,
            "read_range",
            {"start": parse_int(start), "length": length},
            timeout=timeout,
        )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "range": data,
        }

    def dump_pointers(
        self,
        *,
        session: str,
        start: int | str,
        count: int,
        width: int = 4,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(
            target,
            "dump_pointers",
            {"start": parse_int(start), "count": count, "width": width},
            timeout=timeout,
        )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "pointers": data,
        }

    def dump_oam(
        self,
        *,
        session: str,
        count: int = 40,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(target, "dump_oam", {"count": count}, timeout=timeout)
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "oam": data,
        }

    def dump_entities(
        self,
        *,
        session: str,
        base: int | str = "0xC200",
        size: int = 24,
        count: int = 10,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        target = self.require_session(session, require_alive=True)
        response = self.send_command(
            target,
            "dump_entities",
            {"base": parse_int(base), "size": size, "count": count},
            timeout=timeout,
        )
        data = self.handle_response(response, session_id=target["id"])
        return {
            "session_id": target["id"],
            "frame": response.get("frame"),
            "entities": data,
        }

    @staticmethod
    def _lua_result(payload: dict[str, Any]) -> Any:
        data = payload.get("data")
        return data.get("result", data) if isinstance(data, dict) else data

    @staticmethod
    def _response_frame(payload: dict[str, Any]) -> int:
        frame = payload.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, (int, float)):
            raise DomainError(
                "settle_failed", "frame polling did not return a frame.", phase="settle"
            )
        return int(frame)

    def _wait_for_frame(self, session: str, target_frame: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError(
                    "settle_failed",
                    f"timed out waiting for frame >= {target_frame}.",
                    phase="settle",
                    session_id=session,
                )
            result = self.run_lua(session=session, code="return true", timeout=min(remaining, 5.0))
            if self._response_frame(result) >= target_frame:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _settle_lua(self, session: str, result: dict[str, Any], timeout: float) -> None:
        value = self._lua_result(result)
        macro_key = value.get("macro_key") if isinstance(value, dict) else None
        if not isinstance(macro_key, str) or not macro_key:
            self.run_lua(session=session, code="return true", timeout=min(timeout, 5.0))
            return
        code = (
            f"local macro = _G[{to_lua_string(macro_key)}]; "
            "if macro == nil then return true end; "
            "local active = macro.active; "
            "if active == nil then return true end; "
            "return active == false"
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError(
                    "settle_failed",
                    "Lua macro did not complete.",
                    phase="settle",
                    session_id=session,
                )
            result = self.run_lua(session=session, code=code, timeout=min(remaining, 5.0))
            if self._lua_result(result) is True:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _composite_error(exc: Exception, code: str, session: str) -> DomainError:
        # The original command has completed; never imply a safe retry of the composite.
        context = dict(exc.context) if isinstance(exc, DomainError) else {}
        context["session_id"] = session
        if isinstance(exc, DomainError):
            context["cause_code"] = exc.code
            context["cause_phase"] = exc.phase
            context["cause_execution_outcome"] = exc.execution_outcome
        return DomainError(
            code,
            str(exc),
            phase="settle" if code == "settle_failed" else "snapshot",
            execution_outcome="partial",
            **context,
        )

    def run_lua_and_view(
        self, *, session: str, timeout: float = 20.0, **kwargs: Any
    ) -> dict[str, Any]:
        with self.transaction(session, composite=True) as operation:
            result = self.run_lua(session=session, timeout=timeout, **kwargs)
            try:
                self._settle_lua(session, result, timeout)
            except Exception as exc:
                operation.mark_uncertain()
                raise self._composite_error(exc, "settle_failed", session) from exc
            try:
                view = self.get_view(session=session, timeout=timeout)
            except Exception as exc:
                raise self._composite_error(exc, "snapshot_failed", session) from exc
            return {
                **result,
                "screenshot": {"frame": view.get("frame")},
                "png_base64": view.get("png_base64"),
            }

    def input_tap_and_view(
        self,
        *,
        session: str,
        key: str,
        frames: int = 1,
        wait_frames: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        with self.transaction(session, composite=True) as operation:
            result = self.input_tap(session=session, key=key, frames=frames, timeout=timeout)
            try:
                tap_frame = self._response_frame(result)
                data = result.get("data")
                duration = data.get("duration") if isinstance(data, dict) else None
                if (
                    isinstance(duration, bool)
                    or not isinstance(duration, (int, float))
                    or duration < 1
                ):
                    raise RuntimeError("input_tap did not return a valid duration.")
                self._wait_for_frame(session, tap_frame + int(duration) + wait_frames, timeout)
            except Exception as exc:
                operation.mark_uncertain()
                raise self._composite_error(exc, "settle_failed", session) from exc
            try:
                view = self.get_view(session=session, timeout=timeout)
            except Exception as exc:
                raise self._composite_error(exc, "snapshot_failed", session) from exc
            return {
                **result,
                "screenshot": {"frame": view.get("frame")},
                "png_base64": view.get("png_base64"),
            }

    def start_with_lua(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        return self._start_with_lua(timeout=timeout, include_view=False, **kwargs)

    def start_with_lua_and_view(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        return self._start_with_lua(timeout=timeout, include_view=True, **kwargs)

    def _start_with_lua(
        self, *, timeout: float, include_view: bool, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            valid_timeout = (
                not isinstance(timeout, bool)
                and isinstance(timeout, (int, float))
                and math.isfinite(timeout)
                and timeout > 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise DomainError(
                "invalid_arguments",
                "timeout must be a finite positive number.",
                phase="validation",
                execution_outcome="not_started",
            )
        file, code = kwargs.get("file"), kwargs.get("code")
        file = self._validate_lua_source(file, code)
        start_kwargs = {key: value for key, value in kwargs.items() if key not in {"file", "code"}}
        start_kwargs.setdefault("ready_timeout", timeout)
        start_kwargs = self._validate_start_options(start_kwargs)
        session = start_kwargs["session_id"]
        if session is None:
            session = self._new_session_id()
        start_kwargs["session_id"] = session
        start_kwargs["_activate"] = False
        self.ensure_runtime_dirs()
        with self.transaction(session, create=True, composite=True) as operation:
            if file:
                try:
                    file = str(operation._directory.copy_file(Path(file), "startup-lua.lua"))
                except BaseException as exc:
                    failure = self._prelaunch_failure(exc, operation, session, "staging")
                    if isinstance(exc, Exception):
                        raise failure from exc
                    exc.add_note(str(failure))
                    raise
            try:
                started = self.start(**start_kwargs)
            except DomainError as exc:
                if exc.execution_outcome == "not_started":
                    self._prelaunch_failure(exc, operation, session, exc.phase)
                raise
            try:
                result = self.run_lua(session=session, file=file, code=code, timeout=timeout)
                if include_view:
                    try:
                        self._settle_lua(session, result, timeout)
                    except Exception as exc:
                        operation.mark_uncertain()
                        raise self._composite_error(exc, "settle_failed", session) from exc
                    try:
                        view = self.get_view(session=session, timeout=timeout)
                    except Exception as exc:
                        raise self._composite_error(exc, "snapshot_failed", session) from exc
                operation._startup_finalization = self._activate_startup(
                    operation,
                    session,
                    lambda exc: self._post_start_failure(
                        exc, operation, session, started.get("pid")
                    ),
                )
            except BaseException as exc:
                failure = self._post_start_failure(exc, operation, session, started.get("pid"))
                if isinstance(exc, Exception):
                    raise failure from exc
                exc.add_note(str(failure))
                raise
            payload = {
                "session_id": session,
                "pid": started.get("pid"),
                "lua": self._lua_result(result),
            }
            if include_view:
                payload["screenshot"] = {"frame": view.get("frame")}
                payload["png_base64"] = view.get("png_base64")
            return payload

    def _post_start_failure(
        self,
        exc: BaseException,
        operation: session_transactions.Transaction,
        session: str,
        pid: int | None,
    ) -> DomainError:
        failure = (
            exc
            if isinstance(exc, DomainError)
            else DomainError(
                "startup_failed",
                f"Session '{session}' post-start operation failed: {exc} "
                "Inspect status before retrying.",
                phase="post_start",
                execution_outcome="partial",
            )
        )
        failure.context.update(session_id=session, pid=pid)
        if failure.execution_outcome == "not_started":
            failure.execution_outcome = "partial"
        try:
            target = self.load_session(session)
            failure.context.update(
                process_state=self._process_state(target),
                session_dir=target["session_dir"],
                stdout_log=target["stdout_log"],
                stderr_log=target["stderr_log"],
            )
            target.setdefault("startup", {}).update(
                state="failed", post_start_error=error_payload(failure)["error"]
            )
            operation.write_metadata(target)
        except (OSError, DomainError, KeyError) as publication:
            failure.context["metadata_error"] = str(publication)
        return failure
