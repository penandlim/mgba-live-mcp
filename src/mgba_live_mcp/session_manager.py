"""Shared in-process runtime manager for live mGBA sessions."""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import process_control, session_transactions

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
        return session_transactions.transaction(
            self.session_dir(session), create=create, composite=composite
        )

    def _process_state(self, session: dict[str, Any]) -> str:
        return process_control.process_state(
            int(session["pid"]),
            session.get("process_identity"),
            reap=session.get("ready") is True,
        )

    def ensure_runtime_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.archived_sessions_dir.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def session_file(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def archive_session_destination(self, session_id: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = self.archived_sessions_dir / f"{session_id}-{stamp}"
        if not base.exists():
            return base
        for idx in range(1, 1000):
            candidate = self.archived_sessions_dir / f"{session_id}-{stamp}-{idx}"
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Unable to allocate archive destination for session: {session_id}")

    def load_session(self, session_id: str) -> dict[str, Any]:
        return json.loads(self.session_file(session_id).read_text())

    def write_session(self, data: dict[str, Any]) -> None:
        session_transactions.atomic_write_json(self.session_file(data["id"]), data)

    def iter_sessions(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.sessions_dir.exists():
            return items
        for candidate in sorted(
            self.sessions_dir.glob("*/session.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            try:
                items.append(json.loads(candidate.read_text()))
            except Exception:
                continue
        return items

    def read_log_excerpt(self, path: Path, max_chars: int = 4000) -> str:
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            return ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    def set_active_session(self, session_id: str) -> None:
        self.active_session_file.write_text(session_id)

    def get_active_session_id(self) -> str | None:
        if not self.active_session_file.exists():
            return None
        value = self.active_session_file.read_text().strip()
        return value or None

    def _refresh_active_session(self) -> None:
        active = self.get_active_session_id()
        if active:
            active_path = self.session_file(active)
            if active_path.exists():
                try:
                    active_session = json.loads(active_path.read_text())
                    active_state = self._process_state(active_session)
                except Exception:
                    active_state = None
                if active_state is not None and active_state != "dead":
                    return

        for candidate in self.iter_sessions():
            try:
                if self._process_state(candidate) != "dead":
                    self.set_active_session(candidate["id"])
                    return
            except Exception:
                continue

        if self.active_session_file.exists():
            self.active_session_file.unlink()

    def prune_dead_sessions(self) -> list[str]:
        removed: list[str] = []
        if not self.sessions_dir.exists():
            self._refresh_active_session()
            return removed

        self.archived_sessions_dir.mkdir(parents=True, exist_ok=True)
        for candidate in self.sessions_dir.glob("*/session.json"):
            try:
                session = json.loads(candidate.read_text())
                state = self._process_state(session)
            except Exception:
                continue

            if state != "dead":
                continue

            session_id = str(session.get("id") or candidate.parent.name)
            try:
                archived = self.archive_session_destination(session_id)
                if session_transactions.archive_session(candidate.parent, archived):
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
        raise RuntimeError("No mGBA binary found in PATH (expected mgba-qt/mgba/mGBA).")

    def require_session(
        self, session_id: str | None, *, require_alive: bool = True
    ) -> dict[str, Any]:
        self.ensure_runtime_dirs()
        if not session_id:
            raise ValueError("session_required: session is required.")

        path = self.session_file(session_id)
        if not path.exists():
            raise RuntimeError(f"session_not_found: Session not found: {session_id}")

        session = json.loads(path.read_text())
        if require_alive:
            state = self._process_state(session)
            if state != "alive":
                code = "session_dead" if state == "dead" else state
                raise RuntimeError(f"{code}: session '{session_id}' process is {state}.")
        return session

    def resolve_attach_target(
        self,
        *,
        session: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_runtime_dirs()
        if pid is not None:
            for candidate in self.iter_sessions():
                if int(candidate["pid"]) == pid:
                    session = candidate["id"]
                    break
            if not session:
                raise RuntimeError(
                    "session_not_found: PID is not a managed live session started by mgba-live-mcp."
                )
        if not session:
            raise ValueError("session_required: provide session or pid.")
        return self.require_session(session, require_alive=True)

    def write_command(self, command_path: Path, command: dict[str, Any]) -> None:
        tmp_path = command_path.with_suffix(".tmp")
        lua_doc = "return " + to_lua_value(command) + "\n"
        tmp_path.write_text(lua_doc)
        tmp_path.replace(command_path)

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
        command_path = Path(session["command_path"])
        response_path = Path(session["response_path"])
        with session_transactions.transaction(command_path.parent) as operation:
            if session.get("generation", operation.generation) != operation.generation:
                raise RuntimeError("session_generation_changed: session metadata is stale.")
            if "pid" in session:
                state = self._process_state(session)
                if state in {"dead", "identity_mismatch"}:
                    raise RuntimeError(
                        f"session_{state}: session '{session.get('id')}' "
                        f"process is {state} before publishing '{kind}'."
                    )
            request_id = uuid.uuid4().hex
            command = {"id": request_id, "kind": kind, **(payload or {})}

            def publish() -> None:
                if command_path.exists():
                    raise RuntimeError("session_busy: an unclaimed bridge command still exists.")
                response_path.unlink(missing_ok=True)
                self.write_command(command_path, command)

            operation.publish(request_id, publish)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                operation.check()
                if _startup_process is not None and _startup_process.poll() is not None:
                    raise RuntimeError("session_dead: process exited before bridge readiness.")
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
                        raise RuntimeError(
                            f"session_{state}: session '{session.get('id')}' "
                            f"process became {state} during '{kind}'."
                        )
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            raise TimeoutError(
                f"Timed out waiting for response to command '{kind}' "
                f"(request_id={request_id}; execution outcome unknown). "
                "The session remains busy until the response arrives or recovery stop succeeds."
            )

    def handle_response(self, response: dict[str, Any]) -> Any:
        if not response.get("ok"):
            raise RuntimeError(f"bridge_error: {response.get('error', 'unknown')}")
        return response.get("data")

    def resolve_startup_scripts(self, script_paths: list[str]) -> list[str]:
        resolved: list[str] = []
        for script in script_paths:
            path = Path(script).resolve()
            if not path.exists():
                raise RuntimeError(f"Script not found: {path}")
            resolved.append(str(path))
        return resolved

    def prepare_bridge_script(self, session_scripts_dir: Path) -> Path:
        if not self.bridge_script.exists():
            raise RuntimeError(f"Bridge script missing: {self.bridge_script}")

        session_bridge = session_scripts_dir / self.bridge_script.name
        try:
            shutil.copy2(self.bridge_script, session_bridge)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to stage bridge script in session dir: {session_bridge}"
            ) from exc
        return session_bridge

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
    ) -> dict[str, Any]:
        if not math.isfinite(ready_timeout) or ready_timeout <= 0:
            raise ValueError("ready_timeout must be a finite positive number")
        rom_path = Path(rom).resolve()
        if not rom_path.exists():
            raise RuntimeError(f"ROM not found: {rom_path}")
        resolved_mgba_path = mgba_path or self.detect_mgba_binary()
        startup_scripts = self.resolve_startup_scripts(script or [])
        self.ensure_runtime_dirs()
        self.prune_dead_sessions()
        resolved_session_id = session_id or self._new_session_id()
        with self.transaction(resolved_session_id, create=True, composite=True) as operation:
            sdir = self.session_dir(resolved_session_id)
            (sdir / "screenshots").mkdir(exist_ok=True)
            scripts_dir = sdir / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            session_bridge_script = self.prepare_bridge_script(scripts_dir)
            command_path = sdir / "command.lua"
            response_path = sdir / "response.json"
            heartbeat_path = sdir / "heartbeat.json"
            stdout_log = sdir / "stdout.log"
            stderr_log = sdir / "stderr.log"
            resolved_fps_target = (
                fps_target if fps_target is not None else (600.0 if fast else default_fps_target())
            )
            command = self.build_start_command(
                mgba_path=resolved_mgba_path,
                fps_target=resolved_fps_target,
                config_overrides=list(config or []),
                savestate=savestate,
                startup_scripts=startup_scripts,
                bridge_script=session_bridge_script,
                log_level=log_level,
                rom=rom_path,
            )
            env = os.environ.copy()
            env["MGBA_LIVE_SESSION_DIR"] = str(sdir)
            env["MGBA_LIVE_COMMAND"] = str(command_path)
            env["MGBA_LIVE_RESPONSE"] = str(response_path)
            env["MGBA_LIVE_HEARTBEAT"] = str(heartbeat_path)
            env["MGBA_LIVE_HEARTBEAT_INTERVAL"] = str(heartbeat_interval)
            with stdout_log.open("w") as stdout_f, stderr_log.open("w") as stderr_f:
                proc = subprocess.Popen(
                    command,
                    cwd=str(sdir),
                    env=env,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    start_new_session=True,
                )
            session: dict[str, Any] = {
                "id": resolved_session_id,
                "generation": operation.generation,
                "pid": proc.pid,
                "ready": False,
                "rom": str(rom_path),
                "fps_target": resolved_fps_target,
                "mgba_path": resolved_mgba_path,
                "startup_scripts": startup_scripts,
                "created_at": now_utc(),
                "session_dir": str(sdir),
                "command_path": str(command_path),
                "response_path": str(response_path),
                "heartbeat_path": str(heartbeat_path),
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            }
            self.write_session(session)
            try:
                session["process_identity"] = process_control.capture_identity(proc.pid)
                self.write_session(session)
                self.handle_response(
                    self.send_command(session, "ping", timeout=ready_timeout, _startup_process=proc)
                )
                operation.check()
                session["ready"] = True
                self.write_session(session)
            except Exception as exc:
                returncode = proc.poll()
                details = [
                    (
                        f"mGBA process exited early with {format_process_exit(returncode)}."
                        if returncode is not None
                        else f"Session '{resolved_session_id}' bridge startup failed: {exc}"
                    ),
                    f"Session dir: {sdir}",
                ]
                stderr_excerpt = self.read_log_excerpt(stderr_log)
                stdout_excerpt = self.read_log_excerpt(stdout_log)
                if stderr_excerpt:
                    details.append(f"stderr:\n{stderr_excerpt}")
                elif stdout_excerpt:
                    details.append(f"stdout:\n{stdout_excerpt}")
                else:
                    details.append("No stdout/stderr was captured before failure.")
                raise RuntimeError("\n".join(details)) from exc
            self.set_active_session(resolved_session_id)
            return {
                "status": "started",
                "session_id": resolved_session_id,
                "pid": proc.pid,
                "fps_target": resolved_fps_target,
                "session_dir": str(sdir),
            }

    def _new_session_id(self) -> str:
        return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"

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
        self.ensure_runtime_dirs()
        self.prune_dead_sessions()
        if all:
            payloads: list[dict[str, Any]] = []
            for candidate in self.iter_sessions():
                if self._process_state(candidate) == "dead":
                    continue
                payloads.append(self._status_payload(candidate))
            return payloads
        return self._status_payload(self.require_session(session, require_alive=False))

    def stop(self, *, session: str, grace: float = 1.0) -> dict[str, Any]:
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("grace must be a finite non-negative number")
        with session_transactions.recovery(self.session_dir(session)) as recovery:
            target = self.require_session(session, require_alive=False)
            pid = int(target["pid"])
            try:
                outcome = process_control.terminate_owned_process(
                    pid, target.get("process_identity"), grace=grace
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    f"{exc}; session={session} generation={recovery.generation}"
                ) from exc
            recovery.finish()
            views_dir = self.session_dir(session) / ".views"
            cleanup_errors = []
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
                raise RuntimeError(f"Lua file not found: {script_path}")
            response = self.send_command(
                target, "run_lua_file", {"path": str(script_path)}, timeout=timeout
            )
        else:
            response = self.send_command(
                target, "run_lua_inline", {"code": str(code)}, timeout=timeout
            )
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
            views_dir = self.session_dir(session) / ".views"
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
                data = self.handle_response(response)
                if isinstance(data, dict) and isinstance(data.get("path"), str):
                    result_path = Path(data["path"])
                if result_path != out_path:
                    raise RuntimeError(
                        "snapshot_failed: bridge returned an unexpected output path."
                    )
                png_bytes = result_path.read_bytes()
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
            out_path = self.session_dir(target["id"]) / "screenshots" / f"screenshot-{ts}.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.send_command(target, "screenshot", {"path": str(out_path)}, timeout=timeout)
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
        data = self.handle_response(response)
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
            raise RuntimeError("settle_failed: frame polling did not return a frame.")
        return int(frame)

    def _wait_for_frame(self, session: str, target_frame: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"settle_failed: timed out waiting for frame >= {target_frame}.")
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
                raise RuntimeError("settle_failed: Lua macro did not complete.")
            result = self.run_lua(session=session, code=code, timeout=min(remaining, 5.0))
            if self._lua_result(result) is True:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def run_lua_and_view(
        self, *, session: str, timeout: float = 20.0, **kwargs: Any
    ) -> dict[str, Any]:
        with self.transaction(session, composite=True) as operation:
            result = self.run_lua(session=session, timeout=timeout, **kwargs)
            try:
                self._settle_lua(session, result, timeout)
            except Exception as exc:
                operation.mark_uncertain()
                raise RuntimeError(f"settle_failed: session '{session}': {exc}") from exc
            try:
                view = self.get_view(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(f"snapshot_failed: session '{session}': {exc}") from exc
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
                raise RuntimeError(f"settle_failed: session '{session}': {exc}") from exc
            try:
                view = self.get_view(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(f"snapshot_failed: session '{session}': {exc}") from exc
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
        file, code = kwargs.get("file"), kwargs.get("code")
        if bool(file) == bool(code):
            raise ValueError("Exactly one of file or code is required.")
        start_kwargs = {key: value for key, value in kwargs.items() if key not in {"file", "code"}}
        session = start_kwargs.get("session_id") or self._new_session_id()
        start_kwargs["session_id"] = session
        start_kwargs.setdefault("ready_timeout", timeout)
        self.ensure_runtime_dirs()
        with self.transaction(session, create=True, composite=True) as operation:
            started = self.start(**start_kwargs)
            try:
                result = self.run_lua(session=session, file=file, code=code, timeout=timeout)
                if include_view:
                    try:
                        self._settle_lua(session, result, timeout)
                    except Exception:
                        operation.mark_uncertain()
                        raise
                    view = self.get_view(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(
                    f"Session '{session}' post-start operation failed: {exc}"
                    " Inspect status before retrying."
                ) from exc
            payload = {
                "session_id": session,
                "pid": started.get("pid"),
                "lua": self._lua_result(result),
            }
            if include_view:
                payload["screenshot"] = {"frame": view.get("frame")}
                payload["png_base64"] = view.get("png_base64")
            return payload
