from __future__ import annotations

import asyncio
import base64
import errno
import json
import multiprocessing
import os
import struct
import time
import warnings
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, tzinfo
from io import BytesIO
from itertools import chain, repeat
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from mcp import types
from PIL import Image

from mgba_live_mcp import deadlines, process_control, server, session_manager, session_transactions
from mgba_live_mcp.errors import CommandTimeout, DomainError
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.screenshots import validate_png
from mgba_live_mcp.session_manager import SessionManager
from mgba_live_mcp.session_transactions import atomic_write_json


@pytest.fixture
def pngs() -> tuple[bytes, bytes]:
    images = []
    for color in ("red", "blue"):
        output = BytesIO()
        with Image.new("RGB", (2, 2), color) as image:
            image.save(output, format="PNG")
        images.append(output.getvalue())
    return images[0], images[1]


def test_concurrent_validation_preserves_callers_warning_policy(pngs, monkeypatch):
    inside = [Event(), Event()]
    release = [Event(), Event()]
    open_image = Image.open

    def gated_open(stream, *args, **kwargs):
        index = 0 if stream.getvalue() == pngs[0] else 1
        inside[index].set()
        assert release[index].wait(5)
        return open_image(stream, *args, **kwargs)

    monkeypatch.setattr(Image, "open", gated_open)
    with warnings.catch_warnings(record=True) as observed, ThreadPoolExecutor(2) as workers:
        warnings.simplefilter("always", Image.DecompressionBombWarning)
        try:
            first = workers.submit(validate_png, pngs[0])
            assert inside[0].wait(2)
            second = workers.submit(validate_png, pngs[1])
            assert inside[1].wait(2)
            release[0].set()
            first.result(3)
            release[1].set()
            second.result(3)
            warnings.warn("Unrelated caller warning", Image.DecompressionBombWarning, stacklevel=2)
        finally:
            for event in release:
                event.set()
    assert [item.category for item in observed] == [Image.DecompressionBombWarning]


def test_png_pixel_limit_is_checked_without_emitting_warnings(pngs, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 3)
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        with pytest.raises(ValueError):
            validate_png(pngs[0])
    assert not observed


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime(2026, 9, 15, 12, 0, tzinfo=tz)


def _manager(root: Path, patch: pytest.MonkeyPatch, *sessions: str) -> SessionManager:
    manager = SessionManager(runtime_root=root)
    manager.ensure_runtime_dirs()
    patch.setattr(manager, "_process_state", lambda target: "alive")
    for session in sessions or ("s1",):
        directory = manager.session_dir(session)
        directory.mkdir(exist_ok=True)
        manager.write_session(
            {
                "id": session,
                "pid": os.getpid(),
                "session_dir": str(directory),
                "command_path": str(directory / "command.lua"),
                "response_path": str(directory / "response.json"),
            }
        )
    return manager


def _reply(manager: SessionManager, command: dict[str, Any], path: Path | None = None) -> None:
    atomic_write_json(
        manager.session_dir(command["session_id"]) / "response.json",
        {
            "id": command["id"],
            "session_id": command["session_id"],
            "ok": True,
            "frame": 1,
            "data": {"path": str(path or command["path"])},
        },
    )


def _write_png(manager: SessionManager, command: dict[str, Any], png: bytes) -> None:
    Path(command["path"]).write_bytes(png)
    _reply(manager, command)


@contextmanager
def _bridge(
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
    native: Callable[[dict[str, Any]], None],
    *,
    expire_after: Event | None = None,
) -> Iterator[tuple[list[dict[str, Any]], list[Future[None]]]]:
    """Substitute native execution, not command publication, ownership, or correlation."""
    original = manager.write_command
    commands: list[dict[str, Any]] = []
    writers: list[Future[None]] = []
    with ThreadPoolExecutor(max_workers=2) as pool, monkeypatch.context() as patch:

        def publish(path: Path, command: dict[str, Any], **kwargs: Any) -> None:
            original(path, command, **kwargs)
            path.unlink()  # Native bridge claims the command before executing it.
            commands.append(command)
            writers.append(pool.submit(native, command))
            if expire_after is not None and len(commands) == 1:
                assert expire_after.wait(2)
                budget = deadlines.current_deadline()
                assert budget is not None
                budget.expires_at = 0.0

        patch.setattr(manager, "write_command", publish)
        try:
            yield commands, writers
        finally:
            for writer in writers:
                writer.result(timeout=6)


def _eventually(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Native worker did not finish its owned operation"
        Event().wait(0.01)


def _invoke(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    async def call() -> types.CallToolResult:
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = (await server.server.request_handlers[types.CallToolRequest](request)).root
        assert isinstance(result, types.CallToolResult)
        return result

    return asyncio.run(call())


def test_frozen_clock_implicit_exports_preserve_both_images(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    monkeypatch.setattr(session_manager, "datetime", FrozenDateTime)
    allocator = SimpleNamespace(uuid4=lambda: UUID(int=0))
    monkeypatch.setattr(session_transactions, "uuid", allocator)
    values = iter(pngs)
    with _bridge(manager, monkeypatch, lambda command: _write_png(manager, command, next(values))):
        first = manager.screenshot(session="s1")
        # Force real filename collisions before allowing a fresh allocation.
        names = chain(repeat(UUID(int=0), 100), repeat(UUID(int=1)))
        allocator.uuid4 = lambda: next(names)
        second = manager.screenshot(session="s1")
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == pngs[0]
    assert Path(second["path"]).read_bytes() == pngs[1]


def _export_in_process(root: str, png: bytes, start: Any, result: Connection) -> None:
    with pytest.MonkeyPatch.context() as patch:
        manager = SessionManager(runtime_root=Path(root))
        patch.setattr(manager, "_process_state", lambda target: "alive")
        patch.setattr(session_manager, "datetime", FrozenDateTime)
        with _bridge(manager, patch, lambda command: _write_png(manager, command, png)):
            assert start.wait(5)
            deadline = time.monotonic() + 5
            while True:
                try:
                    shot = manager.screenshot(session="s1", timeout=2)
                except DomainError as exc:
                    if exc.code != "session_busy" or time.monotonic() >= deadline:
                        raise
                    Event().wait(0.01)  # Test caller retries a read after nonblocking admission.
                else:
                    result.send(shot["path"])
                    break
        result.close()


def test_cross_process_implicit_exports_do_not_overwrite(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    pipes = [context.Pipe(duplex=False) for _ in pngs]
    processes = [
        context.Process(
            target=_export_in_process,
            args=(str(manager.runtime_root), png, start, sender),
        )
        for png, (_, sender) in zip(pngs, pipes, strict=True)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        paths = []
        for receiver, _ in pipes:
            assert receiver.poll(10), "Capture process did not return an artifact"
            paths.append(Path(receiver.recv()))
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        assert paths[0] != paths[1]
        assert [path.read_bytes() for path in paths] == list(pngs)
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        for pair in pipes:
            for pipe in pair:
                pipe.close()


def test_explicit_path_resolution_and_returned_bytes_are_preserved(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    destination = tmp_path / "original.png"
    destination.write_bytes(pngs[0])
    link = tmp_path / "linked.png"
    link.symlink_to(destination)
    with _bridge(manager, monkeypatch, lambda command: _write_png(manager, command, pngs[1])):
        shot = manager.screenshot(session="s1", out=str(link))
    assert link.is_symlink()
    assert shot["path"] == str(destination.resolve())
    assert destination.read_bytes() == shot.png == pngs[1]
    assert json.loads(json.dumps(shot)) == {
        "session_id": "s1",
        "frame": 1,
        "path": str(destination.resolve()),
    }


def test_replace_failure_preserves_previous_explicit_image(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    destination = tmp_path / "previous.png"
    destination.write_bytes(pngs[0])
    replace = os.replace

    def fail_replace(source, target, **kwargs):
        if Path(target).name == destination.name:
            raise OSError(errno.ENOSPC, "No space for publication")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", fail_replace)
    with _bridge(manager, monkeypatch, lambda command: _write_png(manager, command, pngs[1])) as (
        commands,
        _,
    ):
        with pytest.raises(DomainError) as failure:
            manager.screenshot(session="s1", out=str(destination))
    assert failure.value.code == "snapshot_failed"
    assert failure.value.context["stage"] == "persistence"
    assert failure.value.context["request_id"] == commands[0]["id"]
    assert destination.read_bytes() == pngs[0]
    assert not Path(commands[0]["path"]).exists()


def _bad_deflate_png(png: bytes) -> bytes:
    data = b"invalid deflate stream"
    chunk = b"IDAT" + data
    return (
        png[:33]
        + struct.pack(">I", len(data))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + png[-12:]
    )


@pytest.mark.parametrize("damage", ["missing", "unreadable", "empty", "truncated", "bad_deflate"])
def test_registered_handler_rejects_bad_native_images(tmp_path, monkeypatch, pngs, damage):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    destination = tmp_path / "previous.png"
    destination.write_bytes(pngs[0])
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))
    stage_names: set[str] = set()
    open_file = os.open

    def unreadable(path, flags, *args, **kwargs):
        if (
            damage == "unreadable"
            and Path(path).name in stage_names
            and flags & os.O_ACCMODE == os.O_RDONLY
        ):
            raise PermissionError(errno.EACCES, "Screenshot became unreadable")
        return open_file(path, flags, *args, **kwargs)

    def native(command):
        path = Path(command["path"])
        stage_names.add(path.name)
        if damage == "missing":
            path.unlink()
        else:
            raw = {
                "unreadable": pngs[0],
                "empty": b"",
                "truncated": pngs[0][:-5],
                "bad_deflate": _bad_deflate_png(pngs[0]),
            }[damage]
            path.write_bytes(raw)
        _reply(manager, command)

    monkeypatch.setattr(os, "open", unreadable)
    with _bridge(manager, monkeypatch, native) as (commands, _):
        result = _invoke("mgba_live_export_screenshot", {"session": "s1", "out": str(destination)})
    assert result.isError
    assert not any(isinstance(item, types.ImageContent) for item in result.content)
    assert result.structuredContent is not None
    error = result.structuredContent["error"]
    assert error["code"] == "snapshot_failed"
    assert error["session_id"] == "s1" and error["request_id"] == commands[0]["id"]
    assert error["stage"] == ("capture" if damage in {"missing", "unreadable"} else "validation")
    assert not Path(commands[0]["path"]).exists()
    assert destination.read_bytes() == pngs[0]


def test_unexpected_bridge_path_never_deletes_foreign_image(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    foreign = tmp_path / "foreign.png"
    foreign.write_bytes(pngs[0])

    def native(command):
        Path(command["path"]).write_bytes(pngs[1])
        _reply(manager, command, foreign)

    with _bridge(manager, monkeypatch, native) as (commands, _):
        with pytest.raises(DomainError) as failure:
            manager.get_view(session="s1")
    assert failure.value.code == "snapshot_failed"
    assert foreign.read_bytes() == pngs[0]
    assert not Path(commands[0]["path"]).exists()


def test_replaced_staging_inode_is_not_deleted(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    foreign = tmp_path / "foreign.png"
    foreign.write_bytes(pngs[0])

    def native(command):
        path = Path(command["path"])
        path.unlink()
        os.link(foreign, path)
        _reply(manager, command)

    with _bridge(manager, monkeypatch, native) as (commands, _):
        with pytest.raises(DomainError) as failure:
            manager.get_view(session="s1")
    assert failure.value.code == "snapshot_failed"
    assert foreign.read_bytes() == pngs[0]
    assert Path(commands[0]["path"]).samefile(foreign)


def test_cleanup_permission_failure_retains_then_reconciles_owned_file(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    unlink = os.unlink
    refuse_cleanup = True
    values = iter(pngs)

    def remove(path, *args, **kwargs):
        if refuse_cleanup and Path(path).name.startswith(".mgba-screenshot-"):
            raise PermissionError(errno.EACCES, "Directory became unwritable")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", remove)
    with _bridge(
        manager, monkeypatch, lambda command: _write_png(manager, command, next(values))
    ) as (commands, _):
        with pytest.raises(DomainError) as failure:
            manager.get_view(session="s1")
        retained = Path(commands[0]["path"])
        assert retained.read_bytes() == pngs[0]
        assert failure.value.code == "snapshot_failed"
        assert failure.value.execution_outcome == "completed"
        assert failure.value.context["stage"] == "cleanup"
        assert failure.value.context["request_id"] == commands[0]["id"]
        assert failure.value.context["retained_artifacts"] == [str(retained)]
        refuse_cleanup = False
        view = manager.get_view(session="s1")
    assert not retained.exists()
    assert not Path(commands[1]["path"]).exists()
    assert view.png == pngs[1]


@pytest.mark.parametrize(
    ("journal_recovers", "cleanup_recovers"), [(True, False), (False, False), (True, True)]
)
def test_staging_journal_and_rollback_failures_preserve_recovery_details(
    tmp_path, monkeypatch, pngs, journal_recovers, cleanup_recovers
):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))
    destination = tmp_path / "previous.png"
    destination.write_bytes(pngs[0])
    write_json = session_transactions._Directory.write_json
    unlink = os.unlink
    fail_journal = fail_cleanup = True

    def record(directory, name, value):
        nonlocal fail_journal
        operation = value.get("operation")
        if fail_journal and isinstance(operation, dict) and operation.get("artifacts"):
            fail_journal = not journal_recovers
            raise OSError(errno.ENOSPC, "No space for staging journal")
        return write_json(directory, name, value)

    def remove(path, *args, **kwargs):
        nonlocal fail_cleanup
        if fail_cleanup and Path(path).name.startswith(".mgba-screenshot-"):
            fail_cleanup = not cleanup_recovers
            raise PermissionError(errno.EACCES, "Cannot remove staging file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(session_transactions._Directory, "write_json", record)
    monkeypatch.setattr(os, "unlink", remove)
    with _bridge(manager, monkeypatch, lambda command: _write_png(manager, command, pngs[1])) as (
        commands,
        _,
    ):
        result = _invoke("mgba_live_export_screenshot", {"session": "s1", "out": str(destination)})
        assert result.isError and result.structuredContent is not None
        error = result.structuredContent["error"]
        assert error["code"] == "snapshot_failed"
        assert error["session_id"] == "s1" and error["stage"] == "capture"
        assert error["execution_outcome"] == "not_executed"
        assert not commands  # No native side effects before staging was journaled.
        assert destination.read_bytes() == pngs[0]
        staged = Path(error["staging_path"])
        assert staged.exists() is not cleanup_recovers
        assert error.get("retained_artifacts", []) == ([] if cleanup_recovers else [str(staged)])
        assert error["cleanup_errors"]
        if not journal_recovers:
            assert error["journal_error"]
        else:
            fail_cleanup = False
            recovered = _invoke(
                "mgba_live_export_screenshot", {"session": "s1", "out": str(destination)}
            )
            assert not recovered.isError
            assert destination.read_bytes() == pngs[1]
            assert not staged.exists()


def test_mcp_reuses_capture_bytes_after_destination_is_replaced(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    destination = tmp_path / "shared.png"
    values = iter(pngs)

    async def export(**kwargs):
        first = manager.screenshot(**kwargs)
        manager.screenshot(**kwargs)
        return first

    monkeypatch.setattr(server, "_controller", SimpleNamespace(export_screenshot=export))
    with _bridge(manager, monkeypatch, lambda command: _write_png(manager, command, next(values))):
        result = _invoke("mgba_live_export_screenshot", {"session": "s1", "out": str(destination)})
    assert not result.isError
    image = next(item for item in result.content if isinstance(item, types.ImageContent))
    assert base64.b64decode(image.data) == pngs[0]
    assert destination.read_bytes() == pngs[1]
    assert all(
        image.data not in item.text
        for item in result.content
        if isinstance(item, types.TextContent)
    )


def test_concurrent_explicit_captures_publish_last_complete_image(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch, "s1", "s2")
    destination = tmp_path / "shared.png"
    started = {session: Event() for session in ("s1", "s2")}
    release = {session: Event() for session in started}

    def native(command):
        session = command["session_id"]
        started[session].set()
        assert release[session].wait(5)
        _write_png(manager, command, pngs[0 if session == "s1" else 1])

    with _bridge(manager, monkeypatch, native), ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(manager.screenshot, session="s1", out=str(destination))
        second = callers.submit(manager.screenshot, session="s2", out=str(destination))
        try:
            assert started["s1"].wait(2) and started["s2"].wait(2)
            assert not destination.exists()
            release["s1"].set()
            first_result = first.result(3)
            assert destination.read_bytes() == pngs[0]
            release["s2"].set()
            second_result = second.result(3)
        finally:
            for event in release.values():
                event.set()
    assert first_result.png == pngs[0] and second_result.png == pngs[1]
    assert destination.read_bytes() == pngs[1]


@pytest.mark.anyio
@pytest.mark.parametrize("during_write", [False, True])
async def test_cancelled_caller_does_not_remove_active_writer_file(
    tmp_path, monkeypatch, pngs, during_write
):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    destination = tmp_path / "previous.png"
    destination.write_bytes(pngs[0])
    started, release = Event(), Event()

    def native(command):
        if during_write:
            Path(command["path"]).write_bytes(pngs[1][:20])
        started.set()
        assert release.wait(5)
        _write_png(manager, command, pngs[1])

    with _bridge(manager, monkeypatch, native) as (commands, _):
        caller = asyncio.create_task(
            LiveControllerClient(manager).export_screenshot(
                session="s1", out=str(destination), timeout=3
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            staged = Path(commands[0]["path"])
            assert staged.exists() and destination.read_bytes() == pngs[0]
            with pytest.raises(DomainError, match="session_busy"):
                await asyncio.to_thread(manager.get_view, session="s1")
            release.set()
            await asyncio.to_thread(_eventually, lambda: not staged.exists())
            assert destination.read_bytes() == pngs[1]
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("during_write", [False, True])
def test_timeout_retains_stage_until_correlated_response_reconciliation(
    tmp_path, monkeypatch, pngs, during_write
):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    started, release = Event(), Event()

    def native(command):
        if during_write:
            Path(command["path"]).write_bytes(pngs[0][:20])
        started.set()
        assert release.wait(5)
        _write_png(manager, command, pngs[0])

    with _bridge(manager, monkeypatch, native, expire_after=started) as (commands, writers):
        try:
            with pytest.raises(CommandTimeout) as failure:
                manager.get_view(session="s1")
            assert started.wait(2)
            staged = Path(commands[0]["path"])
            assert staged.exists()
            assert failure.value.context["retained_artifacts"] == [str(staged)]
            assert failure.value.context["request_id"] == commands[0]["id"]
            with pytest.raises(DomainError, match="session_busy"):
                manager.get_view(session="s1")
            release.set()
            writers[0].result(3)
            recovered = manager.get_view(session="s1")
            assert recovered.png == pngs[0]
            assert all(not Path(command["path"]).exists() for command in commands)
        finally:
            release.set()


def test_reconciliation_reports_cleanup_and_journal_failures(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))
    started, release = Event(), Event()
    values = iter(pngs)
    unlink = os.unlink
    write_json = session_transactions._Directory.write_json
    refuse_cleanup = False

    def native(command):
        png = next(values)
        Path(command["path"]).write_bytes(png[:20])
        started.set()
        assert release.wait(5)
        _write_png(manager, command, png)

    def remove(path, *args, **kwargs):
        if refuse_cleanup and Path(path).name.startswith(".mgba-screenshot-"):
            raise PermissionError(errno.EACCES, "Cannot remove completed capture")
        return unlink(path, *args, **kwargs)

    def record(directory, name, value):
        operation = value.get("operation")
        if refuse_cleanup and isinstance(operation, dict) and operation.get("artifacts"):
            raise OSError(errno.ENOSPC, "Cannot update cleanup journal")
        return write_json(directory, name, value)

    monkeypatch.setattr(os, "unlink", remove)
    monkeypatch.setattr(session_transactions._Directory, "write_json", record)
    with _bridge(manager, monkeypatch, native, expire_after=started) as (commands, writers):
        try:
            with pytest.raises(CommandTimeout):
                manager.get_view(session="s1")
            assert started.wait(2)
            staged = Path(commands[0]["path"])
            unrelated = staged.with_name("unrelated.png")
            unrelated.write_bytes(pngs[0])
            release.set()
            writers[0].result(3)
            refuse_cleanup = True
            result = _invoke("mgba_live_get_view", {"session": "s1"})
            assert result.isError and result.structuredContent is not None
            error = result.structuredContent["error"]
            assert error["retained_artifacts"] == [str(staged)]
            assert error["code"] == "snapshot_failed" and error["stage"] == "cleanup"
            assert error["session_id"] == "s1" and error["request_id"] == commands[0]["id"]
            assert error["cleanup_errors"] and error["journal_error"]
            assert staged.read_bytes() == pngs[0]
            assert len(commands) == 1
            refuse_cleanup = False
            recovered = _invoke("mgba_live_get_view", {"session": "s1"})
            assert not recovered.isError
            image = next(item for item in recovered.content if isinstance(item, types.ImageContent))
            assert base64.b64decode(image.data) == pngs[1]
            assert all(not Path(command["path"]).exists() for command in commands)
            assert unrelated.read_bytes() == pngs[0]
        finally:
            refuse_cleanup = False
            release.set()


def test_verified_recovery_cleans_only_owned_staging_after_writer_exit(tmp_path, monkeypatch, pngs):
    manager = _manager(tmp_path / "runtime", monkeypatch)
    views = manager.session_dir("s1") / ".views"
    views.mkdir()
    foreign = views / "unrelated.png"
    foreign.write_bytes(pngs[0])
    started, release, stopped = Event(), Event(), Event()

    def native(command):
        Path(command["path"]).write_bytes(pngs[1][:20])
        started.set()
        assert release.wait(5)
        if not stopped.is_set():
            _write_png(manager, command, pngs[1])

    with _bridge(manager, monkeypatch, native, expire_after=started) as (commands, writers):

        def terminate(pid, identity, **kwargs):
            assert pid == os.getpid()
            assert Path(commands[0]["path"]).exists()
            stopped.set()
            release.set()
            writers[0].result(3)
            return "stopped"

        monkeypatch.setattr(process_control, "terminate_owned_process", terminate)
        try:
            with pytest.raises(CommandTimeout):
                manager.get_view(session="s1")
            assert started.wait(2)
            result = manager.stop(session="s1")
        finally:
            release.set()
    assert result["alive_after"] is False
    assert not Path(commands[0]["path"]).exists()
    assert foreign.read_bytes() == pngs[0]
