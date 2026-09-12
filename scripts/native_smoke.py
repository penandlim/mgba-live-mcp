"""Explicit real Qt/Lua smoke; never imported by offline tests."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent
from PIL import Image

from mgba_live_mcp import process_control
from mgba_live_mcp.session_manager import SessionManager
from mgba_live_mcp.test_rom import TEST_ROM_SHA256, TEST_ROM_URL, verify_test_rom

MGBA_COMMIT = "543a197582c30364584d773a974d7f991892fa43"
SESSION = "native-smoke"
PROBE = "return {keys=emu:getKeys(), frame=emu:currentFrame(), smoke=native_smoke}"
MONITOR = """
assert(emu.getKeys and emu.addKey and emu.clearKey and emu.screenshot)
assert(callbacks.add and callbacks.remove)
native_smoke = {ticks=0, pressed=false, released=false, macro_calls=0,
                macro_done=false, macro_pressed=false, macro_released=false,
                post_done_pressed=false}
callbacks:add("frame", function()
  local s = native_smoke
  s.ticks = s.ticks + 1
  local keys = emu:getKeys()
  if not s.macro_done and s.macro_calls == 0 then
    if emu:getKey(C.GBA_KEY.A) == 1 then s.pressed = true end
    if s.pressed and keys == 0 then s.released = true end
  end
  if s.macro_calls > 0 then
    if emu:getKey(C.GBA_KEY.B) == 1 then s.macro_pressed = true end
    if s.macro_pressed and keys == 0 then s.macro_released = true end
  end
  if s.macro_done and keys ~= 0 then s.post_done_pressed = true end
end)
return {lua=_VERSION, keys=emu:getKeys(), frame=emu:currentFrame(),
        platform=emu:platform(), platforms={GB=C.PLATFORM.GB, GBA=C.PLATFORM.GBA},
        oam={base=emu.memory.oam:base(), bound=emu.memory.oam:bound(), size=emu.memory.oam:size()},
        state_api={saveFile=emu.saveStateFile ~= nil, loadFile=emu.loadStateFile ~= nil,
                   saveBuffer=emu.saveStateBuffer ~= nil, loadBuffer=emu.loadStateBuffer ~= nil},
        state_flags={ALL=C.SAVESTATE.ALL, SAVEDATA=C.SAVESTATE.SAVEDATA,
                     SCREENSHOT=C.SAVESTATE.SCREENSHOT, RTC=C.SAVESTATE.RTC,
                     CHEATS=C.SAVESTATE.CHEATS, METADATA=C.SAVESTATE.METADATA}}
"""
MACRO = """
local id
id = callbacks:add("frame", function()
  local s = native_smoke
  s.macro_calls = s.macro_calls + 1
  if s.macro_calls == 1 then emu:addKey(C.GBA_KEY.B) end
  if s.macro_calls == 5 then emu:clearKey(C.GBA_KEY.B) end
  if s.macro_calls == 8 then
    s.macro_done = true
    s.done_tick = s.ticks
    callbacks:remove(id)
  end
end)
return {registered=true}
"""


class ControlledFailure(Exception):
    """Raised only after the full sequence, while the owned emulator is alive."""


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def metadata_only(value: Any) -> None:
    if isinstance(value, dict):
        require(not ({"png_base64", "image", "screenshot"} & value.keys()), "Unexpected image")
        for child in value.values():
            metadata_only(child)
    elif isinstance(value, list):
        for child in value:
            metadata_only(child)


async def scenario(root: Path, binary: Path, rom: Path, *, inject_failure: bool) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    home = root / "home"
    home.mkdir()
    runtime = home / ".mgba-live-mcp" / "runtime"
    manager = SessionManager(runtime_root=runtime)
    # A private ROM copy also isolates battery saves from the verified download cache.
    private_rom = home / "ucity.gbc"
    shutil.copyfile(rom, private_rom)
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config")}
    result: dict[str, Any] = {"injected_failure": inject_failure, "session_id": SESSION}
    session_record: dict[str, Any] | None = None
    counter = 0

    def record(name: str, value: Any) -> None:
        nonlocal counter
        counter += 1
        save(root / f"{counter:02d}-{name}.json", value)
        heartbeat = manager.session_dir(SESSION) / "heartbeat.json"
        if heartbeat.exists():
            shutil.copyfile(heartbeat, root / f"{counter:02d}-heartbeat.json")

    def cli(*args: str) -> dict[str, Any]:
        command = [sys.executable, "-m", "mgba_live_mcp.live_cli", *args]
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=25)
        record(
            "cli",
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        )
        require(completed.returncode == 0, f"CLI failed: {completed.stderr}")
        payload = json.loads(completed.stdout)
        require(payload.get("session_id") == SESSION, "CLI session identity mismatch")
        metadata_only(payload)
        return payload

    try:
        started = cli(
            "start",
            "--rom",
            str(private_rom),
            "--mgba-path",
            str(binary),
            "--session-id",
            SESSION,
            "--fps-target",
            "60",
            "--ready-timeout",
            "15",
            "--heartbeat-interval",
            "1",
            "--log-level",
            "7",
            "--config",
            "audioSync=0",
            "--config",
            "videoSync=0",
            "--config",
            "volume=0",
        )
        require(started.get("status") == "started", "Start did not reach bridge readiness")
        session_record = manager.load_session(SESSION)
        require(session_record.get("process_identity"), "Missing owned process identity")
        record("owned-session", session_record)
        status = cli("status", "--session", SESSION)
        require(status.get("alive") and status.get("heartbeat"), "Missing live heartbeat")
        cli("run-lua", "--session", SESSION, "--code", MONITOR, "--timeout", "10")
        cli("input-tap", "--session", SESSION, "--key", "A", "--frames", "12", "--timeout", "10")
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mgba_live_mcp.server"], env=env
        )
        with (root / "mcp-stderr.log").open("w") as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=15)
                ) as mcp:
                    record("mcp-initialize", (await mcp.initialize()).model_dump(mode="json"))

                    async def call(name: str, arguments: dict[str, Any], *, visual: bool = False):
                        response = await mcp.call_tool(name, {"session": SESSION, **arguments})
                        record(name, response.model_dump(mode="json"))
                        require(not response.isError, f"MCP tool failed: {response}")
                        texts = [c for c in response.content if isinstance(c, TextContent)]
                        images = [c for c in response.content if isinstance(c, ImageContent)]
                        require(len(texts) == 1, "Expected exactly one JSON metadata block")
                        payload = json.loads(texts[0].text)
                        require(
                            payload.get("session_id") == SESSION, "MCP session identity mismatch"
                        )
                        if not visual:
                            metadata_only(payload)
                        require(len(images) == (1 if visual else 0), "Unexpected MCP image count")
                        return payload, images

                    async def probe():
                        payload, _ = await call("mgba_live_run_lua", {"code": PROBE, "timeout": 10})
                        return payload["data"]["result"]

                    deadline = time.monotonic() + 10
                    while True:
                        observed = await probe()
                        if observed["smoke"]["released"]:
                            break
                        require(time.monotonic() < deadline, "Bounded input never pressed/released")
                        await asyncio.sleep(0.05)
                    require(
                        observed["smoke"]["pressed"] and observed["keys"] == 0,
                        "Input press/release was not observed by the native callback",
                    )
                    await call("mgba_live_run_lua", {"code": MACRO, "timeout": 10})
                    deadline = time.monotonic() + 10
                    while True:
                        observed = await probe()
                        state = observed["smoke"]
                        if state["macro_done"] and state["ticks"] >= state["done_tick"] + 30:
                            break
                        require(time.monotonic() < deadline, "Macro did not complete and settle")
                        await asyncio.sleep(0.05)
                    require(
                        state["macro_pressed"] and state["macro_released"],
                        "Native observer did not see macro press and release",
                    )
                    require(
                        state["macro_calls"] == 8
                        and not state["post_done_pressed"]
                        and observed["keys"] == 0,
                        "Macro continued input after completion",
                    )
                    record("observed-input-and-macro", observed)
                    _, images = await call("mgba_live_get_view", {"timeout": 10}, visual=True)
                    image_path = root / "view.png"
                    image_path.write_bytes(base64.b64decode(images[0].data, validate=True))
                    with Image.open(image_path) as image:
                        require(image.format == "PNG", "Visual result is not PNG")
                        image.load()  # Decode every scanline, not merely the signature/header.
                        require(
                            image.size == (160, 144), f"Unexpected GBC image size: {image.size}"
                        )
                        pixels = image.convert("RGB")
                        extrema = pixels.getextrema()
                        require(any(lo != hi for lo, hi in extrema), "Rendered image is blank")
                        record(
                            "decoded-image",
                            {
                                "size": image.size,
                                "mode": image.mode,
                                "extrema": extrema,
                                "pixel_sha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
                            },
                        )
                    if not inject_failure:
                        stopped, _ = await call("mgba_live_stop", {"grace": 1})
                        require(
                            stopped.get("alive_after") is False, "MCP stop did not confirm exit"
                        )
        if inject_failure:
            raise ControlledFailure("Injected after decoded image, before normal stop")
        result["sequence"] = "passed"
    except ControlledFailure as exc:
        result["sequence"] = "expected_failure"
        result["error"] = str(exc)
    except BaseException as exc:
        result["sequence"] = "failed"
        result["error"] = str(exc)
        (root / "failure.log").write_text(traceback.format_exc())
    finally:
        # Preserve the exact owned runtime before stop/archive/view cleanup can mutate it.
        try:
            if runtime.exists():
                shutil.copytree(runtime, root / "before-cleanup")
        except OSError as exc:
            result["diagnostics_error"] = str(exc)
        try:
            if session_record is None and manager.session_file(SESSION).exists():
                session_record = manager.load_session(SESSION)
            if session_record is None:
                raise RuntimeError("No owned emulator record was created")
            state = process_control.process_state(
                int(session_record["pid"]), session_record.get("process_identity")
            )
            result["before_cleanup_state"] = state
            if state != "dead":
                record("finally-stop", manager.stop(session=SESSION, grace=1))
            state = process_control.process_state(
                int(session_record["pid"]), session_record.get("process_identity")
            )
            require(state == "dead", f"Owned process did not exit: {state}")
            result["cleanup"] = "confirmed_dead"
        except BaseException as exc:
            result["cleanup"] = "failed"
            result["cleanup_error"] = str(exc)
        save(root / "result.json", result)
    require(result.get("cleanup") == "confirmed_dead", f"Scoped cleanup failed: {result}")
    require("diagnostics_error" not in result, f"Could not preserve diagnostics: {result}")
    require(
        result["sequence"] == ("expected_failure" if inject_failure else "passed"),
        f"Native sequence failed: {result}",
    )
    if inject_failure:
        require(result["before_cleanup_state"] == "alive", "Failure did not exercise live cleanup")
    return result


async def run(args: argparse.Namespace, root: Path) -> None:
    binary = args.mgba.resolve(strict=True)
    rom = verify_test_rom(args.rom)
    require(
        os.environ.get("QT_QPA_PLATFORM") in {"xcb", "cocoa"},
        "Set QT_QPA_PLATFORM=xcb with Xvfb (Linux), or cocoa (macOS)",
    )
    if os.environ["QT_QPA_PLATFORM"] == "xcb":
        require(os.environ.get("DISPLAY"), "DISPLAY is required for the xcb backend")
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
    require(
        version.returncode == 0 and MGBA_COMMIT in version.stdout,
        f"Expected pinned Qt mGBA ({MGBA_COMMIT}): {version.stdout}{version.stderr}",
    )
    provenance = {
        "mgba_commit": MGBA_COMMIT,
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": version.stdout.strip(),
        "platform": platform.platform(),
        "python": sys.version,
        "display": os.environ.get("DISPLAY"),
        "qt_backend": os.environ["QT_QPA_PLATFORM"],
        "rom_url": TEST_ROM_URL,
        "rom_sha256": TEST_ROM_SHA256,
        "rom_attribution": "µCity 1.3 by Antonio Niño Díaz (AntonioND/SkyLyrac)",
        "rom_license": "GPL-3.0-or-later; graphics/music CC-BY-SA-4.0; GBT Player BSD-2-Clause",
        "rom_source_and_notices": "https://github.com/AntonioND/ucity/tree/v1.3",
    }
    build = json.loads(args.build_provenance.read_text())
    require(build["commit"] == MGBA_COMMIT, "Build source commit mismatch")
    require(build["binary_sha256"] == provenance["binary_sha256"], "Build binary checksum mismatch")
    provenance["build"] = build
    save(root / "provenance.json", provenance)
    outcomes = []
    for name, injected in (("success", False), ("controlled-failure", True)):
        outcomes.append(await scenario(root / name, binary, rom, inject_failure=injected))
    save(root / "result.json", {"passed": True, "scenarios": outcomes})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mgba", required=True, type=Path)
    parser.add_argument("--rom", type=Path, default=Path("roms/ucity.gbc"))
    parser.add_argument("--artifacts", required=True, type=Path, help="New directory; never reused")
    parser.add_argument("--build-provenance", required=True, type=Path)
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True, exist_ok=False)

    def deadline(_signum, _frame):
        raise TimeoutError("Native smoke interrupted or exceeded 120-second deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, deadline)
    signal.alarm(120)
    try:
        asyncio.run(run(args, root))
    except BaseException as exc:
        save(root / "result.json", {"passed": False, "error": str(exc)})
        (root / "failure.log").write_text(traceback.format_exc())
        print(f"Native smoke FAILED; retained diagnostics: {root}", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    print(f"Native smoke passed (including controlled failure cleanup): {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
