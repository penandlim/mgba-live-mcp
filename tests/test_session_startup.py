from __future__ import annotations

import errno
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from typing import Any

import pytest

from mgba_live_mcp import process_control, session_transactions
from mgba_live_mcp.errors import DomainError, error_payload
from mgba_live_mcp.session_manager import SessionManager

pytestmark = pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"}, reason="POSIX process birth identity support"
)

_CHILD = """\
import os
import sys
import time
from pathlib import Path

directory = Path(os.environ["MGBA_LIVE_SESSION_DIR"])
print("owned child stdout", flush=True)
print("owned child stderr", file=sys.stderr, flush=True)
(directory / "child.ready").touch()
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    if (directory / "exit-seven").exists():
        sys.exit(7)
    time.sleep(0.01)
sys.exit(99)
"""


class _PythonManager(SessionManager):
    def __init__(self, root: Path) -> None:
        bridge = root / "bridge.lua"
        bridge.write_text("-- isolated bridge fixture\n")
        self.rom = root / "game.gb"
        self.rom.write_bytes(b"startup fixture")
        self.children: dict[str, subprocess.Popen[Any]] = {}
        super().__init__(runtime_root=root / "runtime", bridge_script=bridge)

    def build_start_command(self, **kwargs: Any) -> list[str]:
        return [sys.executable, "-u", "-c", _CHILD]

    def send_command(
        self,
        session: dict[str, Any],
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
        _startup_process: subprocess.Popen[Any] | None = None,
    ) -> dict[str, Any]:
        assert kind in {"ping", "run_lua_file", "run_lua_inline"}
        return {"ok": True, "data": {}}


def _start(
    manager: _PythonManager, *, session_id: str = "candidate", **kwargs: Any
) -> dict[str, Any]:
    return manager.start(
        rom=str(manager.rom), mgba_path=sys.executable, session_id=session_id, **kwargs
    )


def _composite(manager: _PythonManager, **kwargs: Any) -> dict[str, Any]:
    return manager.start_with_lua(
        rom=str(manager.rom), mgba_path=sys.executable, session_id="candidate", **kwargs
    )


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_PythonManager]:
    manager = _PythonManager(tmp_path)
    popen = subprocess.Popen

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = popen(*args, **kwargs)
        directory = Path(kwargs["cwd"])
        manager.children[directory.name] = child
        # Registration failures must happen after the real child has flushed its logs.
        deadline = time.monotonic() + 5
        while not (directory / "child.ready").exists():
            assert child.poll() is None, "Owned child exited before its startup barrier"
            assert time.monotonic() < deadline, "Owned child missed its bounded startup barrier"
            time.sleep(0.01)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        _start(manager, session_id="previous")
        previous = manager.load_session("previous")
        screenshot = manager.session_dir("previous") / "screenshots" / "keep.png"
        screenshot.write_bytes(b"previous screenshot")
        preserved = {
            path: path.read_bytes()
            for path in (
                manager.active_session_file,
                manager.session_file("previous"),
                Path(previous["stdout_log"]),
                Path(previous["stderr_log"]),
                screenshot,
            )
        }
        yield manager
        assert manager.get_active_session_id() == "previous"
        assert manager.children["previous"].poll() is None
        assert (
            process_control.process_state(previous["pid"], previous["process_identity"]) == "alive"
        )
        for path, original in preserved.items():
            assert path.read_bytes() == original
    finally:
        # These handles are exclusively owned direct children; never signal a discovered PID.
        for child in manager.children.values():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def _assert_prelaunch(manager: _PythonManager, error: dict[str, Any]) -> None:
    assert error["execution_outcome"] == "not_started"
    assert not manager.session_dir("candidate").exists()
    assert "candidate" not in manager.children
    assert {session["id"] for session in manager.iter_sessions()} == {"previous"}


def _assert_logs(manager: _PythonManager, error: dict[str, Any]) -> None:
    directory = manager.session_dir("candidate")
    assert error["execution_outcome"] in {"partial", "unknown"}
    assert error["session_id"] == "candidate"
    assert error["pid"] == manager.children["candidate"].pid
    assert Path(error["session_dir"]) == directory
    for field, filename, text in (
        ("stdout_log", "stdout.log", "owned child stdout\n"),
        ("stderr_log", "stderr.log", "owned child stderr\n"),
    ):
        path = Path(error[field])
        assert path == directory / filename
        assert path.read_text() == text


def _assert_discoverable(manager: _PythonManager, error: dict[str, Any], state: str) -> None:
    assert error["process_state"] == state
    assert manager.prune_dead_sessions() == []
    status = manager.status(session="candidate")
    assert isinstance(status, dict)
    assert status["process_state"] == state
    assert status["alive"] is (state == "alive")
    assert status["is_active"] is False
    assert status["startup"]["state"] == "failed"
    diagnostic = status["startup"].get("error") or status["startup"]["post_start_error"]
    assert diagnostic["execution_outcome"] in {"partial", "unknown"}
    sessions = manager.status(all=True)
    assert isinstance(sessions, list)
    assert {session["session_id"] for session in sessions} == {"previous", "candidate"}
    record = manager.load_session("candidate")
    assert process_control.process_state(record["pid"], record.get("process_identity")) == state
    assert (manager.children["candidate"].poll() is None) is (state == "alive")
    _assert_logs(manager, error)


def _stop_and_archive(manager: _PythonManager, outcome: str) -> None:
    directory = manager.session_dir("candidate")
    logs = {name: (directory / name).read_bytes() for name in ("stdout.log", "stderr.log")}
    stopped = manager.stop(session="candidate", grace=0.05)
    assert stopped["outcome"] == outcome
    assert stopped["alive_after"] is False
    manager.children["candidate"].wait(timeout=5)
    assert manager.load_session("candidate")["startup"]["state"] == "stopped"
    assert manager.prune_dead_sessions() == ["candidate"]
    assert not directory.exists()
    archived = list(manager.archived_sessions_dir.glob("candidate-*"))
    assert len(archived) == 1
    record = json.loads((archived[0] / "session.json").read_text())
    assert record["id"] == "candidate"
    assert record["startup"]["state"] == "stopped"
    for name, original in logs.items():
        assert (archived[0] / name).read_bytes() == original


def _fail_registration(
    monkeypatch: pytest.MonkeyPatch, manager: _PythonManager, *, persistent: bool = False
) -> None:
    write_json = session_transactions._Directory.write_json
    directory = manager.session_dir("candidate")
    failed = False

    def write(self: session_transactions._Directory, name: str, payload: Any) -> None:
        nonlocal failed
        if self.path == directory and name == "session.json" and (persistent or not failed):
            failed = True
            raise OSError(errno.ENOSPC, "injected session metadata disk full")
        write_json(self, name, payload)

    monkeypatch.setattr(session_transactions._Directory, "write_json", write)


@pytest.mark.parametrize("source", ["startup", "composite"])
def test_missing_script_twice_never_reserves_a_session(
    manager: _PythonManager, tmp_path: Path, source: str
) -> None:
    missing = str(tmp_path / "missing.lua")
    for _ in range(2):
        with pytest.raises(DomainError) as raised:
            if source == "startup":
                _start(manager, script=[missing])
            else:
                _composite(manager, file=missing)
        error = error_payload(raised.value)["error"]
        assert error["code"] == "resource_not_found"
        assert error["phase"] == "validation"
        _assert_prelaunch(manager, error)
        assert manager.get_active_session_id() == "previous"


@pytest.mark.parametrize("source", ["startup", "composite"])
@pytest.mark.parametrize("failure", ["partial_copy", "removed_after_validation"])
def test_staging_failure_rolls_back_partial_files_and_releases_the_name(
    manager: _PythonManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    failure: str,
) -> None:
    script = tmp_path / "source.lua"
    script.write_text("return true\n")
    copy = session_transactions._Directory.copy_file

    def fail_copy(directory: session_transactions._Directory, src: Path, name: str) -> Path:
        if Path(src) == script:
            if failure == "removed_after_validation":
                script.unlink()
            else:
                with directory.create_file(name) as output:
                    output.write(b"partial Lua source")
                raise OSError(errno.ENOSPC, "injected staging disk full")
        return copy(directory, src, name)

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions._Directory, "copy_file", fail_copy)
        with pytest.raises(DomainError) as raised:
            if source == "startup":
                _start(manager, script=[str(script)])
            else:
                _composite(manager, file=str(script))
        error = error_payload(raised.value)["error"]
        assert error["code"] == "io_error"
        assert error["phase"] == "staging"
        _assert_prelaunch(manager, error)
    script.write_text("return true\n")
    _start(manager, script=[str(script)], _activate=False)
    assert manager.require_session("candidate")["pid"] == manager.children["candidate"].pid


def test_second_log_open_failure_rolls_back_without_spawning(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_file = session_transactions._Directory.create_file

    def open_log(directory: session_transactions._Directory, name: str) -> Any:
        if name == "stderr.log":
            raise PermissionError(errno.EACCES, "injected stderr open refusal")
        return create_file(directory, name)

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions._Directory, "create_file", open_log)
        with pytest.raises(DomainError) as raised:
            _start(manager)
        error = error_payload(raised.value)["error"]
        assert error["code"] == "io_error"
        assert error["phase"] == "spawn"
        _assert_prelaunch(manager, error)
    _start(manager, _activate=False)
    assert manager.require_session("candidate")["pid"] == manager.children["candidate"].pid


def test_popen_failure_removes_only_the_unlaunched_session(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_spawn(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOEXEC, "injected executable launch failure")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", refuse_spawn)
        with pytest.raises(DomainError) as raised:
            _start(manager)
        error = error_payload(raised.value)["error"]
        assert error["code"] == "io_error"
        assert error["phase"] == "spawn"
        _assert_prelaunch(manager, error)
    _start(manager, _activate=False)
    assert manager.require_session("candidate")["pid"] == manager.children["candidate"].pid


@pytest.mark.parametrize("persistent", [False, True], ids=["first-write", "all-writes"])
def test_registration_failure_confirms_native_cleanup_and_reports_durability(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    with monkeypatch.context() as patch:
        _fail_registration(patch, manager, persistent=persistent)
        with pytest.raises(DomainError) as raised:
            _start(manager)
        error = error_payload(raised.value)["error"]
        assert error["code"] == "startup_failed"
        assert error["phase"] == "registration"
        assert error["cleanup"] == {"confirmed": True, "outcome": "stopped"}
        assert error["metadata_persisted"] is (not persistent)
        assert error["generation"] != manager.load_session("previous")["generation"]
        assert isinstance(error["process_identity"], dict)
        assert process_control.process_state(error["pid"], error["process_identity"]) == "dead"
        assert manager.children["candidate"].poll() is not None
        _assert_logs(manager, error)
        if persistent:
            assert error["process_state"] == "dead"
            assert "injected session metadata disk full" in error["metadata_error"]
            assert not manager.session_file("candidate").exists()
            assert {session["id"] for session in manager.iter_sessions()} == {"previous"}
            assert manager.prune_dead_sessions() == []
            _assert_logs(manager, error)
        else:
            _assert_discoverable(manager, error, "dead")
    if not persistent:
        _stop_and_archive(manager, "already_exited")


def test_child_waiter_creation_failure_confirms_cleanup_before_activation(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_thread(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("No native waiter thread is available.")

    monkeypatch.setattr(process_control.threading.Thread, "start", refuse_thread)
    with pytest.raises(DomainError) as raised:
        _start(manager)
    error = error_payload(raised.value)["error"]
    assert error["phase"] == "registration"
    assert error["cleanup"] == {"confirmed": True, "outcome": "stopped"}
    assert error["metadata_persisted"] is True
    _assert_discoverable(manager, error, "dead")
    _stop_and_archive(manager, "already_exited")


def test_failed_native_cleanup_keeps_the_owned_child_and_recoverable_metadata(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    killpg = process_control.os.killpg

    def deny_owned_signal(pgid: int, sig: int) -> None:
        child = manager.children.get("candidate")
        if child is not None and pgid == child.pid and sig != 0:
            raise PermissionError(errno.EPERM, "injected owned-child signal refusal")
        killpg(pgid, sig)

    with monkeypatch.context() as patch:
        _fail_registration(patch, manager)
        patch.setattr(process_control.os, "killpg", deny_owned_signal)
        with pytest.raises(DomainError) as raised:
            _start(manager)
        error = error_payload(raised.value)["error"]
        assert error["phase"] == "registration"
        assert error["cleanup"]["confirmed"] is False
        assert "permission_denied" in error["cleanup"]["error"]
        assert error["metadata_persisted"] is True
        assert process_control.process_state(error["pid"], error["process_identity"]) == "alive"
        _assert_discoverable(manager, error, "alive")
    _stop_and_archive(manager, "stopped")


def test_readiness_timeout_retains_a_live_session_until_explicit_stop(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager, "send_command", SessionManager.send_command.__get__(manager))
    with pytest.raises(DomainError) as raised:
        _start(manager, ready_timeout=0.05)
    error = error_payload(raised.value)["error"]
    assert error["code"] == "command_timeout"
    assert error["execution_outcome"] == "unknown"
    assert error["metadata_persisted"] is True
    assert "cleanup" not in error
    _assert_discoverable(manager, error, "alive")
    _stop_and_archive(manager, "stopped")


def test_early_child_exit_keeps_code_seven_and_both_log_files(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exit_at_readiness(session: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        (Path(session["session_dir"]) / "exit-seven").touch()
        return SessionManager.send_command(manager, session, *args, **kwargs)

    monkeypatch.setattr(manager, "send_command", exit_at_readiness)
    with pytest.raises(DomainError) as raised:
        _start(manager, ready_timeout=5)
    error = error_payload(raised.value)["error"]
    assert manager.children["candidate"].wait(timeout=5) == 7
    assert error["exit_code"] == 7
    assert "exit code 7" in error["message"]
    assert "owned child stderr" in error["message"]
    assert error["metadata_persisted"] is True
    _assert_discoverable(manager, error, "dead")
    _stop_and_archive(manager, "already_exited")


@pytest.mark.parametrize("boundary", ["birth-lookup", "metadata-publication", "ready-publication"])
def test_exit_seven_survives_native_registration_and_concurrent_status(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    def exit_without_reaping(pid: int) -> None:
        (manager.session_dir("candidate") / "exit-seven").touch()
        deadline = time.monotonic() + 5
        while True:
            exited = (
                process_control._darwin_lifetime(pid)[1]
                if sys.platform == "darwin"
                else process_control._inspect_process(pid)[1]
            )
            if exited:
                return
            assert time.monotonic() < deadline, "Owned child did not exit at registration."
            time.sleep(0.01)

    if boundary == "birth-lookup":
        capture = process_control.capture_identity

        def capture_after_exit(pid: int) -> dict[str, Any]:
            exit_without_reaping(pid)
            return capture(pid)

        monkeypatch.setattr(process_control, "capture_identity", capture_after_exit)
    else:
        write_metadata = session_transactions.Transaction.write_metadata
        state = "ready" if boundary == "ready-publication" else "starting"

        def inspect_after_publication(
            operation: session_transactions.Transaction, payload: dict[str, Any]
        ) -> None:
            write_metadata(operation, payload)
            if payload["id"] == "candidate" and payload["startup"]["state"] == state:
                exit_without_reaping(payload["pid"])
                with ThreadPoolExecutor(max_workers=1) as pool:
                    observed = pool.submit(manager.status, session="candidate").result(timeout=5)
                assert isinstance(observed, dict)
                assert observed["startup"]["state"] == state
                if state == "ready":
                    raise OSError(errno.EIO, "Injected post-readiness publication failure.")

        monkeypatch.setattr(
            session_transactions.Transaction, "write_metadata", inspect_after_publication
        )
    if boundary != "ready-publication":
        monkeypatch.setattr(manager, "send_command", SessionManager.send_command.__get__(manager))
    with pytest.raises(DomainError) as raised:
        _start(manager, ready_timeout=5)
    error = error_payload(raised.value)["error"]
    assert error["exit_code"] == 7
    assert manager.children["candidate"].wait(timeout=1) == 7
    _assert_discoverable(manager, error, "dead")
    _stop_and_archive(manager, "already_exited")


def test_post_start_lua_failure_keeps_live_recovery_and_survives_later_child_exit(
    manager: _PythonManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "post-start.lua"
    source.write_text("error('post-start Lua failure')\n")
    dispatch = manager.send_command

    def fail_lua(session: dict[str, Any], kind: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kind == "run_lua_file":
            return {"ok": False, "error": "post-start Lua failure"}
        return dispatch(session, kind, *args, **kwargs)

    monkeypatch.setattr(manager, "send_command", fail_lua)
    with pytest.raises(DomainError) as raised:
        _composite(manager, file=str(source))
    error = error_payload(raised.value)["error"]
    assert error["code"] == "bridge_error"
    _assert_discoverable(manager, error, "alive")
    (manager.session_dir("candidate") / "exit-seven").touch()
    assert manager.children["candidate"].wait(timeout=5) == 7
    assert manager.prune_dead_sessions() == []
    status = manager.status(session="candidate")
    assert isinstance(status, dict)
    assert status["alive"] is False
    assert status["startup"]["state"] == "failed"
    assert status["startup"]["post_start_error"]["code"] == "bridge_error"
    _assert_logs(manager, error)
    _stop_and_archive(manager, "already_exited")


@pytest.mark.parametrize("composite", [False, True], ids=["start", "start-with-lua"])
def test_atomic_active_publication_failure_preserves_previous_ready_session(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch, composite: bool
) -> None:
    replace = session_transactions.os.replace

    def refuse_active(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if dst == "active_session":
            raise OSError(errno.ENOSPC, "injected active marker publication failure")
        replace(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions.os, "replace", refuse_active)
        with pytest.raises(DomainError) as raised:
            if composite:
                _composite(manager, code="return true")
            else:
                _start(manager)
        error = error_payload(raised.value)["error"]
        assert error["code"] == "startup_failed"
        assert manager.active_session_file.read_text() == "previous"
        _assert_discoverable(manager, error, "alive")
    _stop_and_archive(manager, "stopped")


@pytest.mark.parametrize("composite", [False, True], ids=["start", "start-with-lua"])
@pytest.mark.parametrize("boundary", ["directory-fsync", "guard-exit", "transaction-finish"])
def test_post_activation_failure_preserves_previous_ready_session(
    manager: _PythonManager,
    monkeypatch: pytest.MonkeyPatch,
    composite: bool,
    boundary: str,
) -> None:
    replace = session_transactions.os.replace
    fsync = session_transactions.os.fsync
    owned_state = session_transactions.Transaction._owned_state
    finish = session_transactions.Transaction._finish
    active_replaced = False
    injected = False

    def fail_at(stage: str) -> None:
        nonlocal injected
        if stage == boundary and active_replaced and not injected:
            injected = True
            raise OSError(errno.EIO, f"injected failure after activation: {stage}")

    def publish_active(src: Any, dst: Any, **kwargs: Any) -> None:
        nonlocal active_replaced
        replace(src, dst, **kwargs)
        if dst == "active_session":
            active_replaced = True

    def sync(fd: int) -> None:
        fail_at("directory-fsync")
        fsync(fd)

    def check(operation: session_transactions.Transaction) -> dict[str, Any]:
        fail_at("guard-exit")
        return owned_state(operation)

    def finalize(operation: session_transactions.Transaction, *, failed: bool = False) -> None:
        if not failed:
            fail_at("transaction-finish")
        finish(operation, failed=failed)

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions.os, "replace", publish_active)
        patch.setattr(session_transactions.os, "fsync", sync)
        patch.setattr(session_transactions.Transaction, "_owned_state", check)
        patch.setattr(session_transactions.Transaction, "_finish", finalize)
        with pytest.raises((DomainError, OSError)) as raised:
            if composite:
                _composite(manager, code="return true")
            else:
                _start(manager)
        observed = {
            "active": manager.get_active_session_id(),
            "startup": manager.load_session("candidate")["startup"],
            "error": error_payload(raised.value)["error"],
        }
    # Restore the test fixture only after recording the actual post-failure state.
    manager.set_active_session("previous")
    stopped = manager.stop(session="candidate", grace=0.05)
    assert stopped["alive_after"] is False
    manager.children["candidate"].wait(timeout=5)
    manager.prune_dead_sessions()
    assert injected
    assert observed["active"] == "previous", observed


def test_later_attachment_survives_a_failed_activation(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(manager, session_id="later", _activate=False)
    published, contender_blocked, release_failure = Event(), Event(), Event()
    replace = session_transactions.os.replace
    flock = session_transactions.fcntl.flock
    later_thread = 0

    def replace_then_pause(src: Any, dst: Any, **kwargs: Any) -> None:
        replace(src, dst, **kwargs)
        if dst == "active_session" and manager.get_active_session_id() == "candidate":
            published.set()
            assert release_failure.wait(5), "The competing activation was not released"
            raise OSError(errno.EIO, "injected post-publication failure")

    def observe_contention(fd: int, flags: int) -> None:
        try:
            flock(fd, flags)
        except BlockingIOError:
            if get_ident() == later_thread:
                contender_blocked.set()
            raise

    def attach_later() -> dict[str, Any]:
        nonlocal later_thread
        later_thread = get_ident()
        return manager.attach(session="later")

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions.os, "replace", replace_then_pause)
        patch.setattr(session_transactions.fcntl, "flock", observe_contention)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(_start, manager)
            try:
                assert published.wait(5), "The first activation did not publish"
                later = executor.submit(attach_later)
                assert contender_blocked.wait(5), "A later writer did not wait for activation"
            finally:
                release_failure.set()
            with pytest.raises(DomainError) as failed:
                first.result(timeout=5)
            attached = later.result(timeout=5)
    observed = manager.get_active_session_id()
    restoration = failed.value.context["active_marker_restore"]
    manager.stop(session="candidate", grace=0.05)
    manager.stop(session="later", grace=0.05)
    assert attached["session_id"] == "later"
    assert restoration["confirmed"] is True
    assert observed == "later"


def test_activation_failure_restores_an_absent_marker(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    with manager._active_marker() as directory:
        manager._write_active_marker(directory, None)
    replace = session_transactions.os.replace
    injected = False

    def publish_then_fail(src: Any, dst: Any, **kwargs: Any) -> None:
        nonlocal injected
        replace(src, dst, **kwargs)
        if dst == "active_session" and not injected:
            injected = True
            raise OSError(errno.EIO, "injected post-publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions.os, "replace", publish_then_fail)
        with pytest.raises(DomainError) as failed:
            _start(manager)
        missing = not manager.active_session_file.exists()
    manager.set_active_session("previous")
    _stop_and_archive(manager, "stopped")
    assert missing
    assert failed.value.context["active_marker_restore"] == {
        "previous_session": None,
        "confirmed": True,
    }


def test_failed_active_restoration_reports_the_remaining_marker(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace = session_transactions.os.replace
    published = False

    def fail_publication_and_restore(src: Any, dst: Any, **kwargs: Any) -> None:
        nonlocal published
        if dst == "active_session" and published:
            raise OSError(errno.ENOSPC, "injected restore refusal")
        replace(src, dst, **kwargs)
        if dst == "active_session":
            published = True
            raise OSError(errno.EIO, "injected post-publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions.os, "replace", fail_publication_and_restore)
        with pytest.raises(DomainError) as failed:
            _start(manager)
        observed = manager.get_active_session_id()
        metadata = manager.load_session("candidate")
    manager.set_active_session("previous")
    _stop_and_archive(manager, "stopped")
    error = error_payload(failed.value)["error"]
    assert error["code"] == "startup_failed"
    assert error["active_marker_restore"]["confirmed"] is False
    assert error["active_marker_restore"]["previous_session"] == "previous"
    assert error["active_session"] == observed == "candidate"
    assert metadata["startup"]["state"] == "failed"


def test_retirement_publication_failure_retains_failed_startup_diagnostics(
    manager: _PythonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = session_transactions._Directory.write_json
    injected = False

    def fail_after_retirement(
        directory: session_transactions._Directory, name: str, payload: Any
    ) -> None:
        nonlocal injected
        write(directory, name, payload)
        if (
            directory.path == manager.session_dir("candidate")
            and name == "transaction.json"
            and payload["operation"] is None
            and not injected
        ):
            injected = True
            raise OSError(errno.EIO, "injected failure after retirement publication")

    with monkeypatch.context() as patch:
        patch.setattr(session_transactions._Directory, "write_json", fail_after_retirement)
        with pytest.raises(DomainError) as failed:
            _start(manager)
    metadata = manager.load_session("candidate")
    assert injected
    assert failed.value.context["metadata_persisted"] is True
    assert manager.get_active_session_id() == "previous"
    assert metadata["startup"]["state"] == "failed"
    with pytest.raises(DomainError) as blocked:
        with manager.transaction("candidate"):
            pytest.fail("retirement failure must retain the unresolved startup fence")
    assert blocked.value.code == "session_busy"
    _stop_and_archive(manager, "stopped")
