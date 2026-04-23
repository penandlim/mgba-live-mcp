"""CLI adapter for the shared live mGBA session manager."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .session_manager import (
    DEFAULT_BRIDGE_SCRIPT,
    DEFAULT_RUNTIME_ROOT,
    SessionManager,
)

MODULE_PATH = Path(__file__).resolve()
PACKAGE_DIR = MODULE_PATH.parent
BRIDGE_SCRIPT = DEFAULT_BRIDGE_SCRIPT
RUNTIME_ROOT = DEFAULT_RUNTIME_ROOT
SESSIONS_DIR = RUNTIME_ROOT / "sessions"
ARCHIVED_SESSIONS_DIR = RUNTIME_ROOT / "archived_sessions"
ACTIVE_SESSION_FILE = RUNTIME_ROOT / "active_session"


def _manager() -> SessionManager:
    return SessionManager(runtime_root=RUNTIME_ROOT, bridge_script=BRIDGE_SCRIPT)


def ensure_runtime_dirs() -> None:
    _manager().ensure_runtime_dirs()


def session_dir(session_id: str) -> Path:
    return _manager().session_dir(session_id)


def session_file(session_id: str) -> Path:
    return _manager().session_file(session_id)


def archive_session_destination(session_id: str) -> Path:
    return _manager().archive_session_destination(session_id)


def load_session(session_id: str) -> dict[str, Any]:
    return _manager().load_session(session_id)


def write_session(data: dict[str, Any]) -> None:
    _manager().write_session(data)


def iter_sessions() -> list[dict[str, Any]]:
    return _manager().iter_sessions()


def pid_alive(pid: int) -> bool:
    return _manager().pid_alive(pid)


def read_log_excerpt(path: Path, max_chars: int = 4000) -> str:
    return _manager().read_log_excerpt(path, max_chars=max_chars)


def prune_dead_sessions() -> list[str]:
    return _manager().prune_dead_sessions()


def set_active_session(session_id: str) -> None:
    _manager().set_active_session(session_id)


def get_active_session_id() -> str | None:
    return _manager().get_active_session_id()


def detect_mgba_binary() -> str:
    return _manager().detect_mgba_binary()


def resolve_session(args: argparse.Namespace, require_alive: bool = True) -> dict[str, Any]:
    session_id = args.session if hasattr(args, "session") else None
    return _manager().require_session(session_id, require_alive=require_alive)


def send_command(
    session: dict[str, Any],
    kind: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return _manager().send_command(session, kind, payload, timeout=timeout)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2))


def resolve_startup_scripts(script_paths: list[str]) -> list[str]:
    return _manager().resolve_startup_scripts(script_paths)


def prepare_bridge_script(session_scripts_dir: Path) -> Path:
    return _manager().prepare_bridge_script(session_scripts_dir)


def build_start_command(
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
    return _manager().build_start_command(
        mgba_path=mgba_path,
        fps_target=fps_target,
        config_overrides=config_overrides,
        savestate=savestate,
        startup_scripts=startup_scripts,
        bridge_script=bridge_script,
        log_level=log_level,
        rom=rom,
    )


def cmd_start(args: argparse.Namespace) -> None:
    payload = _manager().start(
        rom=args.rom,
        savestate=args.savestate,
        fps_target=args.fps_target,
        fast=args.fast,
        mgba_path=args.mgba_path,
        session_id=args.session_id,
        script=list(args.script),
        log_level=args.log_level,
        heartbeat_interval=args.heartbeat_interval,
        ready_timeout=args.ready_timeout,
        config=list(args.config),
    )
    print_json(payload)


def cmd_attach(args: argparse.Namespace) -> None:
    print_json(_manager().attach(session=args.session, pid=args.pid))


def cmd_status(args: argparse.Namespace) -> None:
    print_json(_manager().status(session=args.session, all=args.all))


def cmd_stop(args: argparse.Namespace) -> None:
    print_json(_manager().stop(session=args.session, grace=args.grace))


def cmd_run_lua(args: argparse.Namespace) -> None:
    print_json(
        _manager().run_lua(
            session=args.session,
            file=args.file,
            code=args.code,
            timeout=args.timeout,
        )
    )


def cmd_input_tap(args: argparse.Namespace) -> None:
    print_json(
        _manager().input_tap(
            session=args.session,
            key=args.key,
            frames=args.frames,
            timeout=args.timeout,
        )
    )


def cmd_input_set(args: argparse.Namespace) -> None:
    print_json(
        _manager().input_set(
            session=args.session,
            keys=list(args.keys),
            timeout=args.timeout,
        )
    )


def cmd_input_clear(args: argparse.Namespace) -> None:
    keys = None if args.keys is None else list(args.keys)
    print_json(_manager().input_clear(session=args.session, keys=keys, timeout=args.timeout))


def cmd_screenshot(args: argparse.Namespace) -> None:
    print_json(
        _manager().screenshot(
            session=args.session,
            out=args.out,
            no_save=args.no_save,
            timeout=args.timeout,
        )
    )


def cmd_read_memory(args: argparse.Namespace) -> None:
    print_json(
        _manager().read_memory(
            session=args.session,
            addresses=list(args.addresses),
            timeout=args.timeout,
        )
    )


def cmd_read_range(args: argparse.Namespace) -> None:
    print_json(
        _manager().read_range(
            session=args.session,
            start=args.start,
            length=args.length,
            timeout=args.timeout,
        )
    )


def cmd_dump_pointers(args: argparse.Namespace) -> None:
    print_json(
        _manager().dump_pointers(
            session=args.session,
            start=args.start,
            count=args.count,
            width=args.width,
            timeout=args.timeout,
        )
    )


def cmd_dump_oam(args: argparse.Namespace) -> None:
    print_json(_manager().dump_oam(session=args.session, count=args.count, timeout=args.timeout))


def cmd_dump_entities(args: argparse.Namespace) -> None:
    print_json(
        _manager().dump_entities(
            session=args.session,
            base=args.base,
            size=args.size,
            count=args.count,
            timeout=args.timeout,
        )
    )


def add_session_arg(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--session", required=required, help="Explicit session id.")


def add_timeout_arg(parser: argparse.ArgumentParser, default: float = 10.0) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=default,
        help="Command timeout in seconds.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live controller for persistent mGBA playtest sessions."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start a managed mGBA session.")
    p_start.add_argument("--rom", required=True, help="Path to ROM file.")
    p_start.add_argument("--savestate", help="Optional savestate to load on start.")
    p_start.add_argument("--fps-target", type=float, help="Explicit FPS target.")
    p_start.add_argument("--fast", action="store_true", help="Shortcut for --fps-target 600.")
    p_start.add_argument("--mgba-path", help="Path to mGBA binary.")
    p_start.add_argument("--session-id", help="Optional explicit session id.")
    p_start.add_argument(
        "--script",
        action="append",
        default=[],
        help="Optional startup Lua script path passed to mGBA with --script (repeatable).",
    )
    p_start.add_argument("--log-level", type=int, default=0, help="mGBA log level mask.")
    p_start.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
        help="Heartbeat write interval in frames.",
    )
    p_start.add_argument(
        "--ready-timeout",
        type=float,
        default=20.0,
        help="Wait time for bridge readiness.",
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
    add_session_arg(p_status, required=False)
    p_status.add_argument("--all", action="store_true", help="List all known sessions.")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop a running session.")
    add_session_arg(p_stop)
    p_stop.add_argument(
        "--grace",
        type=float,
        default=1.0,
        help="SIGTERM grace period before SIGKILL.",
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
        "--key",
        required=True,
        help="Button key name (A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R).",
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
        "--keys",
        nargs="*",
        help="Optional keys to clear. Omit to clear all.",
    )
    add_timeout_arg(p_input_clear)
    p_input_clear.set_defaults(func=cmd_input_clear)

    p_screenshot = sub.add_parser("screenshot", help="Capture screenshot from running session.")
    add_session_arg(p_screenshot)
    p_screenshot.add_argument("--out", help="Optional output PNG path.")
    p_screenshot.add_argument(
        "--no-save",
        action="store_true",
        help="Capture screenshot without persisting to disk; returns png_base64.",
    )
    add_timeout_arg(p_screenshot, default=20.0)
    p_screenshot.set_defaults(func=cmd_screenshot)

    p_read_memory = sub.add_parser("read-memory", help="Read sparse memory addresses.")
    add_session_arg(p_read_memory)
    p_read_memory.add_argument(
        "--addresses",
        nargs="+",
        required=True,
        help="Addresses (hex or decimal).",
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
        "dump-pointers",
        help="Dump pointer table entries (little-endian).",
    )
    add_session_arg(p_dump_pointers)
    p_dump_pointers.add_argument("--start", required=True, help="Pointer table start address.")
    p_dump_pointers.add_argument("--count", type=int, required=True, help="Number of entries.")
    p_dump_pointers.add_argument(
        "--width",
        type=int,
        default=4,
        help="Pointer width in bytes (default: 4).",
    )
    add_timeout_arg(p_dump_pointers)
    p_dump_pointers.set_defaults(func=cmd_dump_pointers)

    p_dump_oam = sub.add_parser("dump-oam", help="Dump GBA OAM entries.")
    add_session_arg(p_dump_oam)
    p_dump_oam.add_argument("--count", type=int, default=40, help="How many OAM entries (max 128).")
    add_timeout_arg(p_dump_oam)
    p_dump_oam.set_defaults(func=cmd_dump_oam)

    p_dump_entities = sub.add_parser(
        "dump-entities",
        help="Dump structured entity bytes from memory.",
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
