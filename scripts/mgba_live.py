#!/usr/bin/env python3
"""Persistent live-control wrapper for mGBA playtest/debug sessions.

This script keeps a running mgba-qt process alive and sends commands to a
Lua bridge script over command/response files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
SKILL_DIR = SCRIPT_PATH.parents[1]
BRIDGE_SCRIPT = SCRIPT_PATH.with_name("mgba_live_bridge.lua")
RUNTIME_ROOT = SKILL_DIR / ".runtime"
SESSIONS_DIR = RUNTIME_ROOT / "sessions"
ACTIVE_SESSION_FILE = RUNTIME_ROOT / "active_session"


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def parse_int(value: str) -> int:
    return int(value, 0)


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


def ensure_runtime_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def session_file(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def load_session(session_id: str) -> dict[str, Any]:
    data = json.loads(session_file(session_id).read_text())
    return data


def write_session(data: dict[str, Any]) -> None:
    path = session_file(data["id"])
    path.write_text(json.dumps(data, indent=2))


def iter_sessions() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not SESSIONS_DIR.exists():
        return items
    for candidate in sorted(
        SESSIONS_DIR.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            items.append(json.loads(candidate.read_text()))
        except Exception:
            continue
    return items


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _refresh_active_session() -> None:
    active = get_active_session_id()
    if not active:
        return

    active_path = session_file(active)
    if active_path.exists():
        try:
            active_session = json.loads(active_path.read_text())
            if pid_alive(int(active_session["pid"])):
                return
        except Exception:
            pass

    for candidate in iter_sessions():
        try:
            if pid_alive(int(candidate["pid"])):
                set_active_session(candidate["id"])
                return
        except Exception:
            continue

    if ACTIVE_SESSION_FILE.exists():
        ACTIVE_SESSION_FILE.unlink()


def prune_dead_sessions() -> list[str]:
    removed: list[str] = []
    if not SESSIONS_DIR.exists():
        _refresh_active_session()
        return removed

    for candidate in SESSIONS_DIR.glob("*/session.json"):
        try:
            session = json.loads(candidate.read_text())
            pid = int(session["pid"])
        except Exception:
            continue

        if pid_alive(pid):
            continue

        session_id = str(session.get("id") or candidate.parent.name)
        try:
            shutil.rmtree(candidate.parent)
            removed.append(session_id)
        except OSError:
            continue

    _refresh_active_session()
    return removed


def set_active_session(session_id: str) -> None:
    ACTIVE_SESSION_FILE.write_text(session_id)


def get_active_session_id() -> str | None:
    if not ACTIVE_SESSION_FILE.exists():
        return None
    value = ACTIVE_SESSION_FILE.read_text().strip()
    return value or None


def default_fps_target() -> float:
    # Default to 120 so script mode avoids common half-speed behavior at 60.
    return 120.0


def detect_mgba_binary() -> str:
    for candidate in ("mgba-qt", "mgba", "mGBA"):
        path = shutil.which(candidate)
        if path:
            return path
    raise SystemExit("No mGBA binary found in PATH (expected mgba-qt/mgba/mGBA).")


def resolve_session(args: argparse.Namespace, require_alive: bool = True) -> dict[str, Any]:
    ensure_runtime_dirs()
    session_id = args.session if hasattr(args, "session") else None
    if not session_id:
        session_id = get_active_session_id()
    if not session_id:
        for s in iter_sessions():
            if pid_alive(int(s["pid"])):
                session_id = s["id"]
                break
    if not session_id:
        raise SystemExit("No session specified and no active running session found.")

    path = session_file(session_id)
    if not path.exists():
        raise SystemExit(f"Session not found: {session_id}")
    session = json.loads(path.read_text())
    if require_alive and not pid_alive(int(session["pid"])):
        for candidate in iter_sessions():
            if candidate["id"] == session_id:
                continue
            if pid_alive(int(candidate["pid"])):
                set_active_session(candidate["id"])
                return candidate
        raise SystemExit(f"Session exists but process is not alive: {session_id}")
    return session


def write_command(command_path: Path, command: dict[str, Any]) -> None:
    tmp_path = command_path.with_suffix(".tmp")
    lua_doc = "return " + to_lua_value(command) + "\n"
    tmp_path.write_text(lua_doc)
    tmp_path.replace(command_path)


def send_command(
    session: dict[str, Any], kind: str, payload: dict[str, Any] | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    payload = payload or {}
    command_path = Path(session["command_path"])
    response_path = Path(session["response_path"])

    request_id = uuid.uuid4().hex
    command = {"id": request_id, "kind": kind, **payload}

    start = time.time()
    while command_path.exists():
        if time.time() - start > timeout:
            raise TimeoutError("Bridge is busy (command.lua still present).")
        time.sleep(0.02)

    if response_path.exists():
        response_path.unlink()

    write_command(command_path, command)

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
    raise TimeoutError(f"Timed out waiting for response to command '{kind}'.")


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def resolve_startup_scripts(script_paths: list[str]) -> list[str]:
    resolved: list[str] = []
    for script in script_paths:
        path = Path(script).resolve()
        if not path.exists():
            raise SystemExit(f"Script not found: {path}")
        resolved.append(str(path))
    return resolved


def build_start_command(
    *,
    mgba_path: str,
    fps_target: float,
    config_overrides: list[str],
    savestate: str | None,
    startup_scripts: list[str],
    log_level: int,
    rom: Path,
) -> list[str]:
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
    cmd.extend(["--script", str(BRIDGE_SCRIPT), "-l", str(log_level), str(rom)])
    return cmd


def cmd_start(args: argparse.Namespace) -> None:
    ensure_runtime_dirs()
    rom = Path(args.rom).resolve()
    if not rom.exists():
        raise SystemExit(f"ROM not found: {rom}")
    if not BRIDGE_SCRIPT.exists():
        raise SystemExit(f"Bridge script missing: {BRIDGE_SCRIPT}")

    session_id = args.session_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    sdir = session_dir(session_id)
    if sdir.exists():
        raise SystemExit(f"Session already exists: {session_id}")
    sdir.mkdir(parents=True, exist_ok=False)
    (sdir / "screenshots").mkdir(exist_ok=True)
    (sdir / "scripts").mkdir(exist_ok=True)

    command_path = sdir / "command.lua"
    response_path = sdir / "response.json"
    heartbeat_path = sdir / "heartbeat.json"
    stdout_log = sdir / "stdout.log"
    stderr_log = sdir / "stderr.log"

    fps_target = (
        args.fps_target
        if args.fps_target is not None
        else (600.0 if args.fast else default_fps_target())
    )

    mgba_path = args.mgba_path or detect_mgba_binary()
    startup_scripts = resolve_startup_scripts(args.script)
    cmd = build_start_command(
        mgba_path=mgba_path,
        fps_target=fps_target,
        config_overrides=args.config,
        savestate=args.savestate,
        startup_scripts=startup_scripts,
        log_level=args.log_level,
        rom=rom,
    )

    env = os.environ.copy()
    env["MGBA_LIVE_SESSION_DIR"] = str(sdir)
    env["MGBA_LIVE_COMMAND"] = str(command_path)
    env["MGBA_LIVE_RESPONSE"] = str(response_path)
    env["MGBA_LIVE_HEARTBEAT"] = str(heartbeat_path)
    env["MGBA_LIVE_HEARTBEAT_INTERVAL"] = str(args.heartbeat_interval)

    stdout_f = stdout_log.open("w")
    stderr_f = stderr_log.open("w")
    try:
        proc = subprocess.Popen(
            cmd,
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
        "id": session_id,
        "pid": proc.pid,
        "rom": str(rom),
        "fps_target": fps_target,
        "mgba_path": mgba_path,
        "startup_scripts": startup_scripts,
        "created_at": now_utc(),
        "session_dir": str(sdir),
        "command_path": str(command_path),
        "response_path": str(response_path),
        "heartbeat_path": str(heartbeat_path),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    write_session(session)
    set_active_session(session_id)

    ready_deadline = time.time() + args.ready_timeout
    while time.time() < ready_deadline:
        if not pid_alive(proc.pid):
            raise SystemExit("mGBA process exited early. Check stderr.log in session dir.")
        try:
            response = send_command(session, "ping", timeout=1.0)
            if response.get("ok"):
                print_json(
                    {
                        "status": "started",
                        "session_id": session_id,
                        "pid": proc.pid,
                        "fps_target": fps_target,
                        "session_dir": str(sdir),
                    }
                )
                return
        except Exception:
            time.sleep(0.2)
    raise SystemExit("Session created but bridge did not become ready before timeout.")


def cmd_attach(args: argparse.Namespace) -> None:
    ensure_runtime_dirs()
    target_id = args.session
    if args.pid is not None:
        for s in iter_sessions():
            if int(s["pid"]) == args.pid:
                target_id = s["id"]
                break
        if not target_id:
            raise SystemExit(
                "PID is not a managed live session. "
                "Only processes started with mgba_live.py can be live-controlled."
            )
    if not target_id:
        raise SystemExit("Provide --session or --pid.")

    session = load_session(target_id)
    if not pid_alive(int(session["pid"])):
        raise SystemExit(f"Session found but process is not alive: {target_id}")
    set_active_session(target_id)
    print_json(
        {
            "status": "attached",
            "session_id": target_id,
            "pid": session["pid"],
            "rom": session["rom"],
            "fps_target": session["fps_target"],
        }
    )


def cmd_status(args: argparse.Namespace) -> None:
    ensure_runtime_dirs()
    if args.all:
        out = []
        for s in iter_sessions():
            alive = pid_alive(int(s["pid"]))
            if not alive:
                continue
            hb_path = Path(s["heartbeat_path"])
            heartbeat = None
            if hb_path.exists():
                try:
                    heartbeat = json.loads(hb_path.read_text())
                except Exception:
                    heartbeat = None
            out.append(
                {
                    "session_id": s["id"],
                    "pid": s["pid"],
                    "alive": alive,
                    "rom": s["rom"],
                    "fps_target": s["fps_target"],
                    "heartbeat": heartbeat,
                }
            )
        print_json(out)
        return

    session = resolve_session(args, require_alive=True)
    heartbeat = None
    hb_path = Path(session["heartbeat_path"])
    if hb_path.exists():
        try:
            heartbeat = json.loads(hb_path.read_text())
        except Exception:
            heartbeat = None
    print_json(
        {
            "session_id": session["id"],
            "pid": session["pid"],
            "alive": pid_alive(int(session["pid"])),
            "rom": session["rom"],
            "fps_target": session["fps_target"],
            "heartbeat": heartbeat,
            "session_dir": session["session_dir"],
        }
    )


def terminate_session_process(pid: int, grace: float = 1.0) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


def cmd_stop(args: argparse.Namespace) -> None:
    session = resolve_session(args, require_alive=False)
    pid = int(session["pid"])
    alive_before = pid_alive(pid)
    if alive_before:
        terminate_session_process(pid, grace=args.grace)
    alive_after = pid_alive(pid)
    active = get_active_session_id()
    if active == session["id"] and not alive_after and ACTIVE_SESSION_FILE.exists():
        ACTIVE_SESSION_FILE.unlink()
    print_json(
        {
            "session_id": session["id"],
            "pid": pid,
            "alive_before": alive_before,
            "alive_after": alive_after,
            "stopped": alive_before and not alive_after,
        }
    )


def handle_response(response: dict[str, Any]) -> Any:
    if not response.get("ok"):
        raise SystemExit(f"Bridge error: {response.get('error', 'unknown')}")
    return response.get("data")


def cmd_run_lua(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    if args.file and args.code:
        raise SystemExit("Use either --file or --code, not both.")
    if not args.file and not args.code:
        raise SystemExit("Provide --file or --code.")
    if args.file:
        script_path = Path(args.file).resolve()
        if not script_path.exists():
            raise SystemExit(f"Lua file not found: {script_path}")
        response = send_command(
            session, "run_lua_file", {"path": str(script_path)}, timeout=args.timeout
        )
    else:
        response = send_command(
            session, "run_lua_inline", {"code": args.code}, timeout=args.timeout
        )
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "data": data})


def cmd_input_tap(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(
        session,
        "tap_key",
        {"key": args.key, "duration": args.frames},
        timeout=args.timeout,
    )
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "data": data})


def cmd_input_set(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(session, "set_keys", {"keys": args.keys}, timeout=args.timeout)
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "data": data})


def cmd_input_clear(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    payload: dict[str, Any] = {}
    if args.keys:
        payload["keys"] = args.keys
    response = send_command(session, "clear_keys", payload, timeout=args.timeout)
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "data": data})


def cmd_screenshot(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    if args.out:
        out_path = Path(args.out).resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = Path(session["session_dir"]) / "screenshots" / f"screenshot-{ts}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    response = send_command(session, "screenshot", {"path": str(out_path)}, timeout=args.timeout)
    data = handle_response(response)
    result_path = Path(data.get("path") if isinstance(data, dict) else str(out_path))
    print_json({"frame": response.get("frame"), "path": str(result_path)})


def cmd_read_memory(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    addresses = [parse_int(v) for v in args.addresses]
    response = send_command(session, "read_memory", {"addresses": addresses}, timeout=args.timeout)
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "memory": data})


def cmd_read_range(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(
        session,
        "read_range",
        {"start": parse_int(args.start), "length": args.length},
        timeout=args.timeout,
    )
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "range": data})


def cmd_dump_pointers(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(
        session,
        "dump_pointers",
        {
            "start": parse_int(args.start),
            "count": args.count,
            "width": args.width,
        },
        timeout=args.timeout,
    )
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "pointers": data})


def cmd_dump_oam(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(session, "dump_oam", {"count": args.count}, timeout=args.timeout)
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "oam": data})


def cmd_dump_entities(args: argparse.Namespace) -> None:
    session = resolve_session(args)
    response = send_command(
        session,
        "dump_entities",
        {
            "base": parse_int(args.base),
            "size": args.size,
            "count": args.count,
        },
        timeout=args.timeout,
    )
    data = handle_response(response)
    print_json({"frame": response.get("frame"), "entities": data})


def add_session_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session",
        help="Session id. Defaults to active session, then most recent live session.",
    )


def add_timeout_arg(parser: argparse.ArgumentParser, default: float = 10.0) -> None:
    parser.add_argument(
        "--timeout", type=float, default=default, help="Command timeout in seconds."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live controller for persistent mgba-qt playtest sessions."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start a managed mgba-qt session.")
    p_start.add_argument("--rom", required=True, help="Path to ROM file.")
    p_start.add_argument("--savestate", help="Optional savestate to load on start.")
    p_start.add_argument("--fps-target", type=float, help="Explicit FPS target.")
    p_start.add_argument("--fast", action="store_true", help="Shortcut for --fps-target 600.")
    p_start.add_argument("--mgba-path", help="Path to mgba-qt binary.")
    p_start.add_argument("--session-id", help="Optional explicit session id.")
    p_start.add_argument(
        "--script",
        action="append",
        default=[],
        help="Optional startup Lua script path passed to mGBA with --script (repeatable).",
    )
    p_start.add_argument("--log-level", type=int, default=0, help="mGBA log level mask.")
    p_start.add_argument(
        "--heartbeat-interval", type=int, default=30, help="Heartbeat write interval in frames."
    )
    p_start.add_argument(
        "--ready-timeout", type=float, default=20.0, help="Wait time for bridge readiness."
    )
    p_start.add_argument(
        "--config",
        action="append",
        default=[],
        help="Extra mGBA config override, e.g. --config audioSync=0 (repeatable).",
    )
    p_start.set_defaults(func=cmd_start)

    p_attach = sub.add_parser("attach", help="Attach to a managed running session.")
    p_attach.add_argument("--session", help="Session id to attach.")
    p_attach.add_argument("--pid", type=int, help="PID of a managed session.")
    p_attach.set_defaults(func=cmd_attach)

    p_status = sub.add_parser("status", help="Show session status.")
    add_session_arg(p_status)
    p_status.add_argument("--all", action="store_true", help="List all known sessions.")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop a running session.")
    add_session_arg(p_stop)
    p_stop.add_argument(
        "--grace", type=float, default=1.0, help="SIGTERM grace period before SIGKILL."
    )
    p_stop.set_defaults(func=cmd_stop)

    p_run_lua = sub.add_parser("run-lua", help="Run additional Lua in an existing live session.")
    add_session_arg(p_run_lua)
    p_run_lua.add_argument("--file", help="Lua file to execute in-process.")
    p_run_lua.add_argument("--code", help="Inline Lua code to execute in-process.")
    add_timeout_arg(p_run_lua, default=20.0)
    p_run_lua.set_defaults(func=cmd_run_lua)

    p_input_tap = sub.add_parser("input-tap", help="Tap a button for N frames.")
    add_session_arg(p_input_tap)
    p_input_tap.add_argument(
        "--key", required=True, help="Button key name (A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R)."
    )
    p_input_tap.add_argument("--frames", type=int, default=1, help="Tap duration in frames.")
    add_timeout_arg(p_input_tap)
    p_input_tap.set_defaults(func=cmd_input_tap)

    p_input_set = sub.add_parser("input-set", help="Set currently held keys exactly.")
    add_session_arg(p_input_set)
    p_input_set.add_argument("--keys", nargs="+", required=True, help="List of held keys.")
    add_timeout_arg(p_input_set)
    p_input_set.set_defaults(func=cmd_input_set)

    p_input_clear = sub.add_parser("input-clear", help="Clear some keys or all keys.")
    add_session_arg(p_input_clear)
    p_input_clear.add_argument(
        "--keys", nargs="*", help="Optional keys to clear. Omit to clear all."
    )
    add_timeout_arg(p_input_clear)
    p_input_clear.set_defaults(func=cmd_input_clear)

    p_screenshot = sub.add_parser("screenshot", help="Capture screenshot from running session.")
    add_session_arg(p_screenshot)
    p_screenshot.add_argument("--out", help="Optional output PNG path.")
    add_timeout_arg(p_screenshot, default=20.0)
    p_screenshot.set_defaults(func=cmd_screenshot)

    p_read_memory = sub.add_parser("read-memory", help="Read sparse memory addresses.")
    add_session_arg(p_read_memory)
    p_read_memory.add_argument(
        "--addresses", nargs="+", required=True, help="Addresses (hex or decimal)."
    )
    add_timeout_arg(p_read_memory)
    p_read_memory.set_defaults(func=cmd_read_memory)

    p_read_range = sub.add_parser("read-range", help="Read a contiguous memory range.")
    add_session_arg(p_read_range)
    p_read_range.add_argument("--start", required=True, help="Start address (hex or decimal).")
    p_read_range.add_argument("--length", type=int, required=True, help="Number of bytes.")
    add_timeout_arg(p_read_range)
    p_read_range.set_defaults(func=cmd_read_range)

    p_dump_pointers = sub.add_parser(
        "dump-pointers", help="Dump pointer table entries (little-endian)."
    )
    add_session_arg(p_dump_pointers)
    p_dump_pointers.add_argument("--start", required=True, help="Pointer table start address.")
    p_dump_pointers.add_argument("--count", type=int, required=True, help="Number of entries.")
    p_dump_pointers.add_argument(
        "--width", type=int, default=4, help="Pointer width in bytes (default: 4)."
    )
    add_timeout_arg(p_dump_pointers)
    p_dump_pointers.set_defaults(func=cmd_dump_pointers)

    p_dump_oam = sub.add_parser("dump-oam", help="Dump GBA OAM entries.")
    add_session_arg(p_dump_oam)
    p_dump_oam.add_argument("--count", type=int, default=40, help="How many OAM entries (max 128).")
    add_timeout_arg(p_dump_oam)
    p_dump_oam.set_defaults(func=cmd_dump_oam)

    p_dump_entities = sub.add_parser(
        "dump-entities", help="Dump structured entity bytes from memory."
    )
    add_session_arg(p_dump_entities)
    p_dump_entities.add_argument("--base", default="0xC200", help="Entity array base address.")
    p_dump_entities.add_argument("--size", type=int, default=24, help="Entity struct byte size.")
    p_dump_entities.add_argument("--count", type=int, default=10, help="Entity count.")
    add_timeout_arg(p_dump_entities)
    p_dump_entities.set_defaults(func=cmd_dump_entities)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ensure_runtime_dirs()
    prune_dead_sessions()
    args.func(args)


if __name__ == "__main__":
    main()
