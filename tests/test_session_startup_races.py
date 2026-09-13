from __future__ import annotations

import asyncio
import errno
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import types

from mgba_live_mcp import process_control, server, session_manager, session_transactions
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

# Only the emulator is substituted: startup, command publication, native birth identity,
# metadata, recovery and termination all run normally. Polling belongs to the bridge's
# file protocol; the race tests below coordinate their boundaries with events.
_PING_CHILD = r"""
import json
import os
import re
import time
from pathlib import Path

command = Path(os.environ["MGBA_LIVE_COMMAND"])
response = Path(os.environ["MGBA_LIVE_RESPONSE"])
deadline = time.monotonic() + 60
print(f"owned startup helper {os.getpid()}", flush=True)
ready_fd = int(os.environ["MGBA_LIVE_TEST_READY_FD"])
os.write(ready_fd, b"R")
os.close(ready_fd)
while time.monotonic() < deadline:
    try:
        text = command.read_text()
    except FileNotFoundError:
        time.sleep(0.005)
        continue
    request = re.search(r'\bid\s*=\s*"([0-9a-f]+)"', text)
    assert request is not None and re.search(r'\bkind\s*=\s*"ping"', text)
    command.unlink()
    temporary = response.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "id": request.group(1), "ok": True, "data": {"pid": os.getpid()}
    }))
    temporary.replace(response)
"""


class _NativeManager(SessionManager):
    def build_start_command(self, **kwargs: Any) -> list[str]:
        return [sys.executable, "-u", "-c", _PING_CHILD]


@dataclass
class _Runtime:
    manager: _NativeManager
    options: dict[str, Any]
    children: list[subprocess.Popen[Any]]
    signals: list[tuple[int, int]]

    def start(self, session_id: str | None = None) -> dict[str, Any]:
        return self.manager.start(**self.options, session_id=session_id)

    def peer(self) -> _NativeManager:
        return _NativeManager(
            runtime_root=self.manager.runtime_root, bridge_script=self.manager.bridge_script
        )


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Runtime]:
    bridge = tmp_path / "bridge.lua"
    bridge.write_text("-- startup race fixture\n")
    rom = tmp_path / "game.gb"
    rom.write_bytes(b"fixture ROM")
    manager = _NativeManager(runtime_root=tmp_path / "runtime", bridge_script=bridge)
    children: list[subprocess.Popen[Any]] = []
    signals: list[tuple[int, int]] = []
    popen, kill, killpg = subprocess.Popen, os.kill, os.killpg

    def spawn(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        assert command == [sys.executable, "-u", "-c", _PING_CHILD]
        assert kwargs.get("start_new_session") is True
        read_fd, write_fd = os.pipe()
        try:
            kwargs["env"] = {**kwargs["env"], "MGBA_LIVE_TEST_READY_FD": str(write_fd)}
            child = popen(command, pass_fds=(write_fd,), **kwargs)
            children.append(child)
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                assert selector.select(timeout=5), "Owned startup helper did not become ready"
            assert os.read(read_fd, 1) == b"R"
            return child
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def checked_kill(pid: int, sig: int) -> None:
        assert any(child.pid == pid for child in children), "Signal targeted an unowned PID"
        if sig:
            signals.append((pid, sig))
        kill(pid, sig)

    def checked_killpg(pgid: int, sig: int) -> None:
        assert any(child.pid == pgid for child in children), "Signal targeted an unowned group"
        if sig:
            signals.append((pgid, sig))
        killpg(pgid, sig)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(process_control.os, "kill", checked_kill)
    monkeypatch.setattr(process_control.os, "killpg", checked_killpg)
    try:
        yield _Runtime(
            manager,
            {"rom": str(rom), "mgba_path": sys.executable, "ready_timeout": 3},
            children,
            signals,
        )
    finally:
        # These helpers never fork. Popen retains ownership of every unreaped child;
        # fallback teardown never sends a group signal or uses a PID from stored metadata.
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def _snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    result = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_mtime_ns,
            content,
        )
    return result


def _wait(event: threading.Event) -> None:
    assert event.wait(10), "Startup race did not reach or release its event barrier"


def _stop(runtime: _Runtime, session_id: str) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        stopped = executor.submit(runtime.manager.stop, session=session_id, grace=0.05).result(
            timeout=5
        )
    assert stopped["session_id"] == session_id
    assert stopped["alive_after"] is False
    assert stopped["outcome"] == "stopped"


def test_frozen_clock_implicit_starts_reserve_distinct_live_sessions(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 9, 11, 12, tzinfo=UTC)

    monkeypatch.setattr(session_manager, "datetime", FrozenDatetime)
    staged = threading.Barrier(2, timeout=10)
    original = _NativeManager.prepare_bridge_script

    def stage(manager: _NativeManager, directory: session_transactions._Directory) -> Path:
        bridge = original(manager, directory)
        # Both exclusive reservations must coexist, even with an identical wall clock.
        staged.wait()
        return bridge

    monkeypatch.setattr(_NativeManager, "prepare_bridge_script", stage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        starts = [
            executor.submit(manager.start, **runtime.options)
            for manager in (runtime.manager, runtime.peer())
        ]
        try:
            results = [start.result(timeout=15) for start in starts]
        finally:
            staged.abort()

    ids = {result["session_id"] for result in results}
    assert len(ids) == 2
    assert {result["pid"] for result in results} == {child.pid for child in runtime.children}
    assert len(runtime.children) == 2
    directories = [runtime.manager.session_dir(session_id) for session_id in ids]
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in directories}) == 2
    for result in results:
        record = runtime.manager.load_session(result["session_id"])
        status = runtime.manager.status(session=result["session_id"])
        assert isinstance(status, dict)
        assert record["id"] == result["session_id"]
        assert status["pid"] == result["pid"]
        assert status["process_state"] == "alive"
        assert status["identity_verified"] is True
        assert status["startup"]["state"] == "ready"
    assert runtime.manager.get_active_session_id() in ids
    assert runtime.signals == []
    for session_id in ids:
        _stop(runtime, session_id)


def test_concurrent_explicit_creators_never_change_the_winner(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = runtime.start("unrelated")
    other_tree = _snapshot(runtime.manager.session_dir("unrelated"))
    active = runtime.manager.active_session_file.read_bytes()
    staged, release = threading.Event(), threading.Event()
    original = runtime.manager.prepare_bridge_script

    def stage(directory: session_transactions._Directory) -> Path:
        bridge = original(directory)
        staged.set()
        _wait(release)
        return bridge

    monkeypatch.setattr(runtime.manager, "prepare_bridge_script", stage)
    with ThreadPoolExecutor(max_workers=3) as executor:
        winner = executor.submit(runtime.start, "contended")
        try:
            _wait(staged)
            before = _snapshot(runtime.manager.session_dir("contended"))
            losers = [
                executor.submit(runtime.peer().start, **runtime.options, session_id="contended")
                for _ in range(2)
            ]
            for loser in losers:
                with pytest.raises(DomainError) as failure:
                    loser.result(timeout=3)
                assert failure.value.code in {"session_exists", "session_busy"}
                assert failure.value.execution_outcome == "not_started"
            assert _snapshot(runtime.manager.session_dir("contended")) == before
            assert _snapshot(runtime.manager.session_dir("unrelated")) == other_tree
            assert runtime.manager.active_session_file.read_bytes() == active
            assert [child.pid for child in runtime.children] == [unrelated["pid"]]
            assert runtime.signals == []
        finally:
            release.set()
        result = winner.result(timeout=10)

    assert result["session_id"] == "contended"
    assert {child.pid for child in runtime.children} == {unrelated["pid"], result["pid"]}
    assert len(runtime.children) == 2
    before = _snapshot(runtime.manager.runtime_root)
    with pytest.raises(DomainError) as failure:
        runtime.peer().start(**runtime.options, session_id="contended")
    assert failure.value.code == "session_exists"
    assert _snapshot(runtime.manager.runtime_root) == before
    assert all(child.poll() is None for child in runtime.children)
    assert runtime.signals == []
    _stop(runtime, "contended")
    assert _snapshot(runtime.manager.session_dir("unrelated")) == other_tree
    assert runtime.children[0].poll() is None
    _stop(runtime, "unrelated")


def test_prelaunch_cancellation_rolls_back_only_its_reservation(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = runtime.start("unrelated")
    before = _snapshot(runtime.manager.session_dir("unrelated"))
    active = runtime.manager.active_session_file.read_bytes()
    staged, cancel = threading.Event(), threading.Event()
    original = runtime.manager.prepare_bridge_script

    def cancelled_stage(directory: session_transactions._Directory) -> Path:
        original(directory)
        staged.set()
        _wait(cancel)
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(runtime.manager, "prepare_bridge_script", cancelled_stage)
        with ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(runtime.start, "cancelled")
            try:
                _wait(staged)
                assert runtime.manager.session_dir("cancelled").is_dir()
            finally:
                cancel.set()
            with pytest.raises(asyncio.CancelledError):
                worker.result(timeout=5)

    assert not runtime.manager.session_dir("cancelled").exists()
    assert _snapshot(runtime.manager.session_dir("unrelated")) == before
    assert runtime.manager.active_session_file.read_bytes() == active
    assert [child.pid for child in runtime.children] == [unrelated["pid"]]
    assert runtime.signals == []
    assert runtime.start("cancelled")["session_id"] == "cancelled"
    _stop(runtime, "cancelled")
    _stop(runtime, "unrelated")


def test_cancelled_old_creator_cannot_remove_a_replacement_winner(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged, cancel = threading.Event(), threading.Event()
    original = runtime.manager.prepare_bridge_script

    def cancelled_stage(directory: session_transactions._Directory) -> Path:
        original(directory)
        staged.set()
        _wait(cancel)
        raise asyncio.CancelledError()

    monkeypatch.setattr(runtime.manager, "prepare_bridge_script", cancelled_stage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(runtime.start, "replaced")
        try:
            _wait(staged)
            directory = runtime.manager.session_dir("replaced")
            directory.rename(runtime.manager.runtime_root / "retired-reservation")
            winner = executor.submit(
                runtime.peer().start, **runtime.options, session_id="replaced"
            ).result(timeout=10)
            before = _snapshot(directory)
            active = runtime.manager.active_session_file.read_bytes()
        finally:
            cancel.set()
        with pytest.raises(asyncio.CancelledError):
            old.result(timeout=5)

    assert _snapshot(directory) == before
    assert runtime.manager.active_session_file.read_bytes() == active
    assert [child.pid for child in runtime.children] == [winner["pid"]]
    assert runtime.children[0].poll() is None
    assert runtime.signals == []
    _stop(runtime, "replaced")


@pytest.mark.parametrize("boundary", ["staging", "readiness"])
def test_cancelled_async_caller_does_not_release_the_startup_worker(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    method = "prepare_bridge_script" if boundary == "staging" else "send_command"
    original_boundary = getattr(runtime.manager, method)
    original_start = runtime.manager.start

    def paused(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        _wait(release)
        return original_boundary(*args, **kwargs)

    def observed_start(**kwargs: Any) -> dict[str, Any]:
        try:
            return original_start(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.manager, method, paused)
    monkeypatch.setattr(runtime.manager, "start", observed_start)
    peer = runtime.peer()

    def contend() -> None:
        with peer.transaction("async-cancel"):
            pytest.fail("The cancelled awaiter released a still-running startup worker")

    async def scenario() -> None:
        client = LiveControllerClient(manager=runtime.manager)
        caller = asyncio.create_task(client.start(**runtime.options, session_id="async-cancel"))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            directory = runtime.manager.session_dir("async-cancel")
            before = _snapshot(directory)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            assert not finished.is_set()
            with pytest.raises(DomainError) as failure:
                await asyncio.wait_for(asyncio.to_thread(contend), 3)
            assert failure.value.code == "session_busy"
            assert _snapshot(directory) == before
            assert len(runtime.children) == (0 if boundary == "staging" else 1)
            assert all(child.poll() is None for child in runtime.children)
            assert runtime.signals == []
            release.set()
            assert await asyncio.to_thread(finished.wait, 10)
            status = await asyncio.wait_for(client.status(session="async-cancel"), 5)
            assert isinstance(status, dict)
            assert status["startup"]["state"] == "ready"
            assert status["process_state"] == "alive"
            assert status["is_active"] is True
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    peer.send_command, peer.load_session("async-cancel"), "ping", timeout=3
                ),
                5,
            )
            assert response["data"]["pid"] == status["pid"]
            assert len(runtime.children) == 1
        finally:
            release.set()
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 10)

    asyncio.run(scenario())
    _stop(runtime, "async-cancel")


def test_recovery_winning_before_popen_fences_launch(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = runtime.start("unrelated")
    before = _snapshot(runtime.manager.session_dir("unrelated"))
    active = runtime.manager.active_session_file.read_bytes()
    staged, release = threading.Event(), threading.Event()
    original = runtime.manager.prepare_bridge_script

    def stage(directory: session_transactions._Directory) -> Path:
        bridge = original(directory)
        staged.set()
        _wait(release)
        return bridge

    def recover() -> None:
        with session_transactions.recovery(runtime.manager.session_dir("fenced")) as stopping:
            stopping.finish()

    monkeypatch.setattr(runtime.manager, "prepare_bridge_script", stage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(runtime.start, "fenced")
        try:
            _wait(staged)
            executor.submit(recover).result(timeout=3)
            assert not worker.done()
        finally:
            release.set()
        with pytest.raises(DomainError) as failure:
            worker.result(timeout=5)

    assert failure.value.code == "session_stopped"
    assert failure.value.execution_outcome == "not_started"
    assert [child.pid for child in runtime.children] == [unrelated["pid"]]
    assert runtime.signals == []
    assert _snapshot(runtime.manager.session_dir("unrelated")) == before
    assert runtime.manager.active_session_file.read_bytes() == active
    with pytest.raises(DomainError) as admission:
        with runtime.peer().transaction("fenced"):
            pytest.fail("A fenced prelaunch worker reopened admission")
    assert admission.value.code == "session_stopped"
    _stop(runtime, "unrelated")


@pytest.mark.parametrize("boundary", ["popen", "identity", "metadata"])
def test_startup_guard_bounds_stop_until_registration_but_not_readiness(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    unrelated = runtime.start("unrelated")
    other_tree = _snapshot(runtime.manager.session_dir("unrelated"))
    registered, register = threading.Event(), threading.Event()
    readiness, finish = threading.Event(), threading.Event()
    original_send = runtime.manager.send_command

    def pause_registration() -> None:
        registered.set()
        _wait(register)

    if boundary == "popen":
        original_popen = subprocess.Popen

        def paused_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
            child = original_popen(*args, **kwargs)
            pause_registration()
            return child

        monkeypatch.setattr(subprocess, "Popen", paused_popen)
    elif boundary == "identity":
        original_capture = process_control.capture_identity

        def paused_capture(pid: int) -> dict[str, Any]:
            identity = original_capture(pid)
            pause_registration()
            return identity

        monkeypatch.setattr(process_control, "capture_identity", paused_capture)
    else:
        original_metadata = session_transactions.Transaction.write_metadata

        def paused_metadata(operation: Any, data: dict[str, Any]) -> None:
            if data.get("startup", {}).get("state") == "starting":
                pause_registration()
            original_metadata(operation, data)

        monkeypatch.setattr(session_transactions.Transaction, "write_metadata", paused_metadata)

    def wait_readiness(*args: Any, **kwargs: Any) -> dict[str, Any]:
        readiness.set()
        _wait(finish)
        return original_send(*args, **kwargs)

    monkeypatch.setattr(runtime.manager, "send_command", wait_readiness)
    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(runtime.start, "guarded")
        try:
            _wait(registered)
            with pytest.raises(DomainError) as refusal:
                executor.submit(runtime.peer().stop, session="guarded", grace=0.05).result(
                    timeout=3
                )
            assert refusal.value.code == "session_busy"
            assert len(runtime.children) == 2
            child = runtime.children[1]
            assert child.poll() is None
            assert runtime.signals == []
            assert not worker.done()
            register.set()
            _wait(readiness)
            record = runtime.manager.load_session("guarded")
            assert record["startup"]["state"] == "starting"
            stopped = executor.submit(runtime.peer().stop, session="guarded", grace=0.05).result(
                timeout=5
            )
            assert stopped["outcome"] == "stopped"
            assert stopped["alive_after"] is False
            assert not worker.done()
            assert {pid for pid, _ in runtime.signals} == {child.pid}
            assert unrelated["pid"] != child.pid
            assert _snapshot(runtime.manager.session_dir("unrelated")) == other_tree
        finally:
            register.set()
            finish.set()
        with pytest.raises(DomainError) as failure:
            worker.result(timeout=5)

    assert failure.value.code == "session_stopped"
    assert failure.value.execution_outcome != "not_started"
    assert process_control.process_state(child.pid, record["process_identity"]) == "dead"
    # Explicit stop already retired the operation. The late worker may not publish
    # readiness or reactivate it; its original logs survive even though it is stopped.
    retained = runtime.manager.load_session("guarded")
    assert retained["ready"] is False
    with pytest.raises(DomainError) as admission:
        with runtime.peer().transaction("guarded"):
            pytest.fail("A late startup worker reopened its stopped session")
    assert admission.value.code == "session_stopped"
    assert runtime.manager.get_active_session_id() == "unrelated"
    assert Path(record["stdout_log"]).is_file()
    assert Path(record["stderr_log"]).is_file()
    assert runtime.children[0].poll() is None
    _stop(runtime, "unrelated")


@pytest.mark.parametrize("name_kind", ["ascii255", "bmp-limit", "unicode-whitespace"])
def test_filesystem_valid_ids_roundtrip_through_start_stop_archive_and_rollback(
    runtime: _Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name_kind: str
) -> None:
    if name_kind == "ascii255":
        session_id = "a" * 255
    elif name_kind == "bmp-limit":
        session_id = "界" * 255
    else:
        session_id = " \t雪 legacy session Ω \n"

    # Ask this filesystem, not an application byte cap. APFS accepts 255 BMP code
    # points (765 UTF-8 bytes); Linux byte-counted filesystems accept 85 here.
    probe = tmp_path / "name-probe"
    probe.mkdir()
    try:
        (probe / session_id).mkdir()
    except OSError as exc:
        if name_kind != "bmp-limit" or exc.errno != errno.ENAMETOOLONG:
            raise  # Ordinary ASCII255 is mandatory and is never skipped.
        session_id = "界" * 85
        (probe / session_id).mkdir()
    (probe / session_id).rmdir()

    started = runtime.start(session_id)
    manager = runtime.manager
    assert started["session_id"] == session_id
    record = manager.load_session(session_id)
    record["boundary_note"] = "metadata remains writable at the filesystem limit"
    manager.write_session(record)
    assert manager.load_session(session_id) == record
    manager.set_active_session(session_id)
    assert manager.active_session_file.read_bytes() == session_id.encode("utf-8")
    assert manager.get_active_session_id() == session_id
    assert manager.attach(session=session_id)["session_id"] == session_id
    status = manager.status(session=session_id)
    assert isinstance(status, dict)
    assert status["session_id"] == session_id
    assert status["is_active"] is True
    assert status["process_state"] == "alive"
    assert status["startup"]["state"] == "ready"
    logs = {name: Path(record[name]).read_bytes() for name in ("stdout_log", "stderr_log")}
    _stop(runtime, session_id)
    assert manager.prune_dead_sessions() == [session_id]
    assert not manager.session_dir(session_id).exists()
    assert manager.get_active_session_id() is None
    archives = list(manager.archived_sessions_dir.iterdir())
    assert len(archives) == 1
    archived = archives[0]
    assert json.loads((archived / "session.json").read_text())["id"] == session_id
    for name, content in logs.items():
        assert (archived / Path(record[name]).name).read_bytes() == content
    archive_before = _snapshot(archived)
    original = manager.prepare_bridge_script

    def cancel_staging(directory: session_transactions._Directory) -> Path:
        original(directory)
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(manager, "prepare_bridge_script", cancel_staging)
        with pytest.raises(asyncio.CancelledError):
            runtime.start(session_id)
    assert not manager.session_dir(session_id).exists()
    assert len(runtime.children) == 1
    assert _snapshot(archived) == archive_before
    assert manager.get_active_session_id() is None


def test_nested_second_start_cannot_roll_back_an_already_running_reservation(
    runtime: _Runtime,
) -> None:
    manager = runtime.manager
    manager.ensure_runtime_dirs()
    with manager.transaction("nested", create=True, composite=True):
        started = runtime.start("nested")
        before = _snapshot(manager.session_dir("nested"))
        with pytest.raises(DomainError) as failure:
            runtime.start("nested")
        assert failure.value.code == "session_exists"
        assert failure.value.execution_outcome == "not_started"
        assert _snapshot(manager.session_dir("nested")) == before
        assert manager.load_session("nested")["pid"] == started["pid"]
        assert [child.pid for child in runtime.children] == [started["pid"]]
        assert runtime.children[0].poll() is None
        assert runtime.signals == []
    _stop(runtime, "nested")


def test_composite_timeout_is_validated_before_reservation_with_separate_readiness_timeout(
    runtime: _Runtime,
) -> None:
    with pytest.raises(DomainError) as failure:
        runtime.manager.start_with_lua(
            **runtime.options,
            session_id="invalid-timeout",
            code="return true",
            timeout=float("nan"),
        )
    assert runtime.children == []
    assert not runtime.manager.session_dir("invalid-timeout").exists()
    assert failure.value.code == "invalid_arguments"
    assert failure.value.phase == "validation"
    assert failure.value.execution_outcome == "not_started"


def test_staging_never_writes_through_a_replaced_session_directory(
    runtime: _Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = runtime.manager
    directory = manager.session_dir("replaced-during-staging")
    retired = tmp_path / "retired-reservation"
    outside = tmp_path / "outside"
    (outside / "scripts").mkdir(parents=True)
    (outside / "scripts" / manager.bridge_script.name).write_text("another owner's script")
    before = _snapshot(outside)
    prepare = manager.prepare_bridge_script

    def replace_before_copy(scripts: Any) -> Path:
        directory.rename(retired)
        directory.symlink_to(outside, target_is_directory=True)
        return prepare(scripts)

    monkeypatch.setattr(manager, "prepare_bridge_script", replace_before_copy)
    with pytest.raises(DomainError) as failure:
        runtime.start("replaced-during-staging")
    assert _snapshot(outside) == before
    assert runtime.children == []
    assert failure.value.execution_outcome == "not_started"
    assert "rollback_error" in failure.value.context
    assert retired.is_dir()


def test_log_creation_is_bound_to_the_reserved_inode(
    runtime: _Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = runtime.manager
    directory = manager.session_dir("replaced-at-log")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stdout.log").write_text("another owner's log")
    before = _snapshot(outside)
    original_open = os.open

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "stdout.log" and flags & os.O_CREAT:
            directory.rename(tmp_path / "retired-reservation")
            directory.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(DomainError) as failure:
        runtime.start("replaced-at-log")
    assert _snapshot(outside) == before
    assert runtime.children == []
    assert failure.value.execution_outcome == "not_started"


def test_registration_losing_its_directory_still_cleans_up_the_owned_child(
    runtime: _Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = runtime.manager
    directory = manager.session_dir("moved-after-registration")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "session.json").write_text("another owner's metadata")
    before = _snapshot(outside)
    original_write = session_transactions.Transaction.write_metadata

    def move_after_write(operation: Any, payload: dict[str, Any]) -> None:
        original_write(operation, payload)
        directory.rename(tmp_path / "retired-reservation")
        directory.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(session_transactions.Transaction, "write_metadata", move_after_write)
    with pytest.raises(DomainError) as failure:
        runtime.start("moved-after-registration")
    assert _snapshot(outside) == before
    assert len(runtime.children) == 1
    assert runtime.children[0].poll() is not None
    assert failure.value.execution_outcome != "not_started"
    assert failure.value.context["cleanup"]["confirmed"] is True
    assert failure.value.context["metadata_persisted"] is False


def test_public_mcp_distinguishes_whitespace_components_from_empty_ids(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = " \t\u3000\n"
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager=runtime.manager))

    async def call(
        name: str, arguments: dict[str, Any], *, failure: bool = False
    ) -> dict[str, Any]:
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments),
        )
        response = (await server.server.request_handlers[types.CallToolRequest](request)).root
        assert isinstance(response, types.CallToolResult)
        assert bool(response.isError) == failure
        assert response.structuredContent is not None
        return response.structuredContent

    async def roundtrip() -> None:
        refused = await call("mgba_live_status", {"session": "", "all": True}, failure=True)
        assert refused["error"]["code"] == "invalid_arguments"
        assert refused["error"]["execution_outcome"] == "not_started"
        assert not runtime.manager.runtime_root.exists()
        started = await call(
            "mgba_live_start",
            {
                "rom": runtime.options["rom"],
                "mgba_path": runtime.options["mgba_path"],
                "session_id": session,
                "timeout": 3,
            },
        )
        assert started["session_id"] == session
        assert runtime.manager.load_session(session)["pid"] == runtime.children[0].pid
        for name in ("mgba_live_attach", "mgba_live_status"):
            result = await call(name, {"session": session})
            assert result["session_id"] == session
        assert runtime.manager.active_session_file.read_text() == session
        assert runtime.manager.get_active_session_id() == session
        stopped = await call("mgba_live_stop", {"session": session, "grace": 0.05})
        assert stopped["alive_after"] is False
        assert runtime.children[0].poll() is not None

    asyncio.run(roundtrip())


@pytest.mark.parametrize("readiness_fails", [False, True])
def test_external_stop_confirms_exit_while_startup_parent_stays_alive(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, readiness_fails: bool
) -> None:
    if readiness_fails:

        def reject_readiness(*args: Any, **kwargs: Any) -> None:
            raise DomainError(
                "bridge_error", "Readiness failed after native registration.", phase="readiness"
            )

        monkeypatch.setattr(runtime.manager, "handle_response", reject_readiness)
        with pytest.raises(DomainError) as failure:
            runtime.start("external-stop")
        assert failure.value.code == "bridge_error"
    else:
        runtime.start("external-stop")
    child = runtime.children[0]
    record = runtime.manager.load_session("external-stop")
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from mgba_live_mcp.session_manager import SessionManager\n"
        "manager = SessionManager(runtime_root=Path(sys.argv[1]))\n"
        "print(json.dumps(manager.stop(session='external-stop', grace=0.2)))\n"
    )
    # Only the isolated stop caller uses the original constructor. The parent
    # neither polls nor waits for mGBA until external exit confirmation returns.
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", type(child))
        result = subprocess.run(
            [sys.executable, "-c", code, str(runtime.manager.runtime_root)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    stopped = json.loads(result.stdout)
    assert stopped["outcome"] == "stopped"
    assert stopped["alive_after"] is False
    assert process_control.process_state(child.pid, record["process_identity"]) == "dead"
    assert child.wait(timeout=1) == -signal.SIGTERM


def test_external_stop_reaps_child_while_readiness_response_read_is_blocked(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading, release = threading.Event(), threading.Event()
    response_path = runtime.manager.session_dir("blocked-readiness") / "response.json"
    read_text = Path.read_text

    def blocked_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == response_path:
            reading.set()
            _wait(release)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from mgba_live_mcp.errors import DomainError, error_payload\n"
        "from mgba_live_mcp.session_manager import SessionManager\n"
        "manager = SessionManager(runtime_root=Path(sys.argv[1]))\n"
        "try:\n"
        "    print(json.dumps(manager.stop(session='blocked-readiness', grace=0.2)))\n"
        "except DomainError as exc:\n"
        "    print(json.dumps(error_payload(exc)))\n"
        "    sys.exit(1)\n"
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(runtime.start, "blocked-readiness")
        try:
            _wait(reading)
            child = runtime.children[0]
            record = runtime.manager.load_session("blocked-readiness")
            assert record["startup"]["state"] == "starting"
            # The startup worker stays inside the actual response-read boundary.
            # No parent poll/wait helps the independent recovery process reap it.
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "Popen", type(child))
                result = subprocess.run(
                    [sys.executable, "-c", code, str(runtime.manager.runtime_root)],
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(session_manager.__file__).resolve().parent.parent),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            assert not worker.done()
            stopped = json.loads(result.stdout)
            assert result.returncode == 0, stopped
            assert stopped["outcome"] == "stopped"
            assert stopped["alive_after"] is False
            assert process_control.process_state(child.pid, record["process_identity"]) == "dead"
            assert child.wait(timeout=1) == -signal.SIGTERM
        finally:
            release.set()
        with pytest.raises(DomainError) as failure:
            worker.result(timeout=5)

    assert failure.value.code == "session_stopped"
    assert runtime.manager.load_session("blocked-readiness")["ready"] is False
    assert runtime.manager.get_active_session_id() is None
