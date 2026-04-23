"""Shared in-process runtime manager for live mGBA sessions."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        self.session_file(data["id"]).write_text(json.dumps(data, indent=2))

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

    def pid_alive(self, pid: int) -> bool:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        except OSError:
            return False
        else:
            if waited_pid == pid:
                return False
            if waited_pid == 0:
                return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

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
                except Exception:
                    active_session = None
                if isinstance(active_session, dict) and self.pid_alive(int(active_session["pid"])):
                    return

        for candidate in self.iter_sessions():
            try:
                if self.pid_alive(int(candidate["pid"])):
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
                pid = int(session["pid"])
            except Exception:
                continue

            if self.pid_alive(pid):
                continue

            session_id = str(session.get("id") or candidate.parent.name)
            try:
                archived = self.archive_session_destination(session_id)
                shutil.move(str(candidate.parent), str(archived))
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
        if require_alive and not self.pid_alive(int(session["pid"])):
            raise RuntimeError(
                f"session_dead: Session exists but process is not alive: {session_id}"
            )
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

    def command_file_matches_request_id(self, command_path: Path, request_id: str) -> bool:
        try:
            lua_doc = command_path.read_text()
        except OSError:
            return False
        pattern = rf'\bid\s*=\s*"{re.escape(request_id)}"'
        return re.search(pattern, lua_doc) is not None

    def send_command(
        self,
        session: dict[str, Any],
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        payload = payload or {}
        command_path = Path(session["command_path"])
        response_path = Path(session["response_path"])

        request_id = uuid.uuid4().hex
        command = {"id": request_id, "kind": kind, **payload}

        start = time.time()
        while command_path.exists():
            if time.time() - start > timeout:
                raise RuntimeError("session_busy: bridge is busy (command.lua still present).")
            time.sleep(0.02)

        if response_path.exists():
            response_path.unlink()

        self.write_command(command_path, command)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text())
                except json.JSONDecodeError:
                    time.sleep(0.02)
                    continue
                if response.get("id") != request_id:
                    time.sleep(0.02)
                    continue
                return response
            time.sleep(0.02)

        if command_path.exists() and self.command_file_matches_request_id(command_path, request_id):
            try:
                command_path.unlink()
            except OSError:
                pass
        raise TimeoutError(f"Timed out waiting for response to command '{kind}'.")

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
        self.ensure_runtime_dirs()
        self.prune_dead_sessions()

        rom_path = Path(rom).resolve()
        if not rom_path.exists():
            raise RuntimeError(f"ROM not found: {rom_path}")

        resolved_session_id = session_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        sdir = self.session_dir(resolved_session_id)
        if sdir.exists():
            raise RuntimeError(f"Session already exists: {resolved_session_id}")
        sdir.mkdir(parents=True, exist_ok=False)
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
        resolved_mgba_path = mgba_path or self.detect_mgba_binary()
        startup_scripts = self.resolve_startup_scripts(script or [])
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

        stdout_f = stdout_log.open("w")
        stderr_f = stderr_log.open("w")
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(sdir),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
        finally:
            stdout_f.close()
            stderr_f.close()

        session = {
            "id": resolved_session_id,
            "pid": proc.pid,
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
        self.set_active_session(resolved_session_id)

        ready_deadline = time.time() + ready_timeout
        while time.time() < ready_deadline:
            poll = getattr(proc, "poll", None)
            returncode = poll() if callable(poll) else getattr(proc, "returncode", None)
            if returncode is not None:
                stderr_excerpt = self.read_log_excerpt(stderr_log)
                stdout_excerpt = self.read_log_excerpt(stdout_log)
                detail_lines = [
                    f"mGBA process exited early with {format_process_exit(returncode)}.",
                    f"Session dir: {sdir}",
                ]
                if stderr_excerpt:
                    detail_lines.append(f"stderr:\n{stderr_excerpt}")
                elif stdout_excerpt:
                    detail_lines.append(f"stdout:\n{stdout_excerpt}")
                else:
                    detail_lines.append("No stdout/stderr was captured before exit.")
                raise RuntimeError("\n".join(detail_lines))
            try:
                response = self.send_command(session, "ping", timeout=1.0)
            except Exception:
                time.sleep(0.2)
                continue
            if response.get("ok"):
                return {
                    "status": "started",
                    "session_id": resolved_session_id,
                    "pid": proc.pid,
                    "fps_target": resolved_fps_target,
                    "session_dir": str(sdir),
                }
        raise RuntimeError("Session created but bridge did not become ready before timeout.")

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
        return {
            "session_id": session["id"],
            "pid": session["pid"],
            "alive": self.pid_alive(int(session["pid"])),
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
                if not self.pid_alive(int(candidate["pid"])):
                    continue
                payloads.append(self._status_payload(candidate))
            return payloads
        return self._status_payload(self.require_session(session, require_alive=True))

    def terminate_session_process(self, pid: int, *, grace: float = 1.0) -> None:
        def ignore_if_target_exited(exc: PermissionError) -> None:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if not self.pid_alive(pid):
                    return
                time.sleep(0.05)
            if not self.pid_alive(pid):
                return
            raise exc

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            ignore_if_target_exited(exc)
            return
        deadline = time.time() + grace
        while time.time() < deadline:
            if not self.pid_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            ignore_if_target_exited(exc)

    def stop(self, *, session: str, grace: float = 1.0) -> dict[str, Any]:
        target = self.require_session(session, require_alive=False)
        pid = int(target["pid"])
        alive_before = self.pid_alive(pid)
        if alive_before:
            self.terminate_session_process(pid, grace=grace)
        alive_after = self.pid_alive(pid)
        if self.get_active_session_id() == target["id"] and not alive_after:
            self._refresh_active_session()
        return {
            "session_id": target["id"],
            "pid": pid,
            "alive_before": alive_before,
            "alive_after": alive_after,
            "stopped": alive_before and not alive_after,
        }

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
        target = self.require_session(session, require_alive=True)
        if no_save and out:
            raise ValueError("Use either out or no_save, not both.")

        if no_save:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                out_path = Path(tmp.name).resolve()
            result_path = out_path
            try:
                response = self.send_command(
                    target, "screenshot", {"path": str(out_path)}, timeout=timeout
                )
                data = self.handle_response(response)
                if isinstance(data, dict) and isinstance(data.get("path"), str):
                    result_path = Path(data["path"])
                png_bytes = result_path.read_bytes()
                return {
                    "session_id": target["id"],
                    "frame": response.get("frame"),
                    "png_base64": base64.b64encode(png_bytes).decode(),
                }
            finally:
                try:
                    out_path.unlink()
                except OSError:
                    pass
                if result_path != out_path:
                    try:
                        result_path.unlink()
                    except OSError:
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
