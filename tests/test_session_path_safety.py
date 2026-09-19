from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp import live_cli, process_control
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.session_manager import SessionManager


def _unexpected_process_action(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("Invalid session state reached process creation or termination")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    bridge = tmp_path / "bridge.lua"
    bridge.write_text("-- bridge\n")
    (tmp_path / "game.gba").write_bytes(b"dummy ROM")
    monkeypatch.setattr(process_control, "process_state", lambda *args, **kwargs: "alive")
    monkeypatch.setattr(process_control, "terminate_owned_process", _unexpected_process_action)
    monkeypatch.setattr(
        "mgba_live_mcp.session_manager.subprocess.Popen", _unexpected_process_action
    )
    return SessionManager(runtime_root=tmp_path / "runtime", bridge_script=bridge)


def _session(directory: Path, session_id: str, pid: int = 1111) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": session_id,
        "pid": pid,
        "ready": True,
        "rom": str(directory.parent / "game.gba"),
        "fps_target": 120.0,
        "mgba_path": sys.executable,
        "session_dir": str(directory),
        "command_path": str(directory / "command.lua"),
        "response_path": str(directory / "response.json"),
        "heartbeat_path": str(directory / "heartbeat.json"),
        "stdout_log": str(directory / "stdout.log"),
        "stderr_log": str(directory / "stderr.log"),
        "started_at": "2026-04-23T00:00:00+00:00",
    }
    (directory / "session.json").write_text(json.dumps(data))
    return data


def _snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    # Include directory identities and timestamps, but never follow a symlink.
    result = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        content: bytes | str | None = None
        if stat.S_ISLNK(info.st_mode):
            content = str(path.readlink())
        elif stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
        result[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_mtime_ns,
            content,
        )
    return result


def _assert_refusal(error: DomainError, *, corrupt: bool = False) -> None:
    codes = {"invalid_arguments", "session_state_corrupt"} if corrupt else {"invalid_arguments"}
    assert error.code in codes
    assert error.execution_outcome == "not_executed"
    if error.code == "invalid_arguments":
        assert error.phase == "validation"


def _refuses_without_changes(
    root: Path, operation: Callable[[], Any], *, corrupt: bool = False
) -> None:
    before = _snapshot(root)
    with pytest.raises(DomainError) as error:
        operation()
    _assert_refusal(error.value, corrupt=corrupt)
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "session_id",
    ["/outside", "../outside", "nested/session", "nested\\session", "", ".", "..", "bad\0id"],
)
def test_session_directory_rejects_every_unsafe_component(
    manager: SessionManager, tmp_path: Path, session_id: str
) -> None:
    if session_id == "/outside":
        session_id = str(tmp_path / "outside")
    _refuses_without_changes(tmp_path, lambda: manager.session_dir(session_id))
    assert not manager.runtime_root.exists()


@pytest.mark.parametrize(
    ("entrypoint", "session_id"),
    [
        ("start", "../outside"),
        ("start", ""),
        ("session_file", "."),
        ("load_session", "/outside"),
        ("require_session", ""),
        ("attach", "nested\\session"),
        ("status", "nested/session"),
        ("status_all", "../outside"),
        ("stop", ".."),
        ("run_lua", "bad\0id"),
        ("send_command", "../../outside"),
        ("write_session", "/outside"),
        ("set_active_session", ""),
        ("archive_session_destination", "../../outside"),
        ("transaction", "nested/session"),
    ],
)
def test_public_id_boundaries_refuse_before_creating_runtime_or_touching_outside(
    manager: SessionManager, tmp_path: Path, entrypoint: str, session_id: str
) -> None:
    outside = _session(tmp_path / "outside", "outside")
    if session_id == "/outside":
        session_id = str(tmp_path / "outside")
    supplied = {**outside, "id": session_id}

    def reserve() -> None:
        with manager.transaction(session_id, create=True):
            pass

    operations: dict[str, Callable[[], Any]] = {
        "start": lambda: manager.start(
            rom=str(tmp_path / "game.gba"), mgba_path=sys.executable, session_id=session_id
        ),
        "session_file": lambda: manager.session_file(session_id),
        "load_session": lambda: manager.load_session(session_id),
        "require_session": lambda: manager.require_session(session_id),
        "attach": lambda: manager.attach(session=session_id),
        "status": lambda: manager.status(session=session_id),
        "status_all": lambda: manager.status(session=session_id, all=True),
        "stop": lambda: manager.stop(session=session_id, grace=0),
        "run_lua": lambda: manager.run_lua(session=session_id, code="return 7", timeout=0.05),
        "send_command": lambda: manager.send_command(supplied, "ping", timeout=0.05),
        "write_session": lambda: manager.write_session(supplied),
        "set_active_session": lambda: manager.set_active_session(session_id),
        "archive_session_destination": lambda: manager.archive_session_destination(session_id),
        "transaction": reserve,
    }
    _refuses_without_changes(tmp_path, operations[entrypoint])
    assert not manager.runtime_root.exists()


@pytest.mark.parametrize("entrypoint", ["start", "status", "attach", "set_active_session"])
def test_invalid_id_does_not_prune_existing_sessions_or_replace_active_state(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    manager.ensure_runtime_dirs()
    _session(manager.sessions_dir / "winner", "winner")
    _session(manager.sessions_dir / "dead", "dead", pid=2222)
    _session(tmp_path / "outside", "outside")
    manager.set_active_session("winner")
    monkeypatch.setattr(
        process_control,
        "process_state",
        lambda pid, *args, **kwargs: "dead" if pid == 2222 else "alive",
    )
    session_id = "../../outside"
    operations: dict[str, Callable[[], Any]] = {
        "start": lambda: manager.start(
            rom=str(tmp_path / "game.gba"), mgba_path=sys.executable, session_id=session_id
        ),
        "status": lambda: manager.status(session=session_id),
        "attach": lambda: manager.attach(session=session_id),
        "set_active_session": lambda: manager.set_active_session(session_id),
    }
    _refuses_without_changes(tmp_path, operations[entrypoint])


@pytest.mark.parametrize(
    "arguments",
    [
        ["start", "--session-id", ""],
        ["status", "--session", "../outside"],
        ["run-lua", "--session", "nested\\session", "--code", "return 7"],
    ],
)
def test_cli_invalid_id_returns_domain_failure_without_eager_runtime_creation(
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    argv = ["mgba-live-cli", *arguments]
    if arguments[0] == "start":
        argv.extend(["--rom", str(tmp_path / "game.gba"), "--mgba-path", sys.executable])
    monkeypatch.setattr(live_cli, "RUNTIME_ROOT", manager.runtime_root)
    monkeypatch.setattr(live_cli, "BRIDGE_SCRIPT", manager.bridge_script)
    monkeypatch.setattr(sys, "argv", argv)
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        live_cli.main()
    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "invalid_arguments"
    assert error["phase"] == "validation"
    assert error["execution_outcome"] == "not_executed"
    assert _snapshot(tmp_path) == before
    assert not manager.runtime_root.exists()


@pytest.mark.parametrize("stored_id", ["../../outside", "", "bad\0id"])
def test_corrupt_active_session_id_is_refused_without_rewriting_it(
    manager: SessionManager, tmp_path: Path, stored_id: str
) -> None:
    manager.ensure_runtime_dirs()
    _session(tmp_path / "outside", "outside")
    manager.active_session_file.write_text(stored_id)
    _refuses_without_changes(tmp_path, manager.get_active_session_id, corrupt=True)


@pytest.mark.parametrize("link_kind", ["directory", "metadata"])
def test_session_and_metadata_symlink_escapes_are_refused_before_reads_writes_or_stop(
    manager: SessionManager, tmp_path: Path, link_kind: str
) -> None:
    manager.ensure_runtime_dirs()
    outside = tmp_path / "outside"
    data = _session(outside, "linked")
    directory = manager.sessions_dir / "linked"
    if link_kind == "directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "session.json").symlink_to(outside / "session.json")
    for operation in (
        lambda: manager.load_session("linked"),
        lambda: manager.write_session(data),
        lambda: manager.stop(session="linked", grace=0),
    ):
        _refuses_without_changes(tmp_path, operation, corrupt=True)


@pytest.mark.parametrize("stored_id", ["winner", "../../outside"])
def test_persisted_id_cannot_redirect_lookup_or_stop_to_another_session(
    manager: SessionManager, tmp_path: Path, stored_id: str
) -> None:
    manager.ensure_runtime_dirs()
    winner = _session(manager.sessions_dir / "winner", "winner")
    manager.set_active_session("winner")
    _session(tmp_path / "outside", "outside")
    directory = manager.sessions_dir / "corrupt"
    directory.mkdir()
    (directory / "session.json").write_text(json.dumps({**winner, "id": stored_id}))
    _refuses_without_changes(tmp_path, lambda: manager.load_session("corrupt"), corrupt=True)
    _refuses_without_changes(
        tmp_path, lambda: manager.stop(session="corrupt", grace=0), corrupt=True
    )


@pytest.mark.parametrize(
    "field",
    ["session_dir", "command_path", "response_path", "heartbeat_path", "stdout_log", "stderr_log"],
)
def test_stored_runtime_paths_cannot_escape_on_write_load_or_use(
    manager: SessionManager, tmp_path: Path, field: str
) -> None:
    data = _session(manager.sessions_dir / "owned", "owned")
    outside = tmp_path / "outside"
    outside.mkdir()
    if field == "session_dir":
        data[field] = str(outside)
    else:
        destination = outside / Path(data[field]).name
        destination.write_text(json.dumps({"frame": 9182, "secret": "outside"}))
        data[field] = str(destination)

    _refuses_without_changes(tmp_path, lambda: manager.write_session(data), corrupt=True)
    if field in {"command_path", "response_path"}:
        _refuses_without_changes(
            tmp_path, lambda: manager.send_command(data, "ping", timeout=0.05), corrupt=True
        )
    (manager.sessions_dir / "owned" / "session.json").write_text(json.dumps(data))

    def operation() -> Any:
        if field in {"command_path", "response_path"}:
            return manager.run_lua(session="owned", code="return 7", timeout=0.05)
        if field == "heartbeat_path":
            return manager.status(session="owned")
        return manager.load_session("owned")

    _refuses_without_changes(tmp_path, operation, corrupt=True)


@pytest.mark.parametrize("entrypoint", ["iter_sessions", "status", "prune", "attach_by_pid"])
def test_enumeration_and_pruning_do_not_adopt_corrupt_entries_or_modify_winner_or_outside(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    manager.ensure_runtime_dirs()
    winner_dir = manager.sessions_dir / "winner"
    winner = _session(winner_dir, "winner")
    (winner_dir / "command.lua").write_text("return {id='winner-request',kind='ping'}\n")
    manager.set_active_session("winner")
    outside = tmp_path / "outside"
    _session(outside, "external", pid=2222)
    (outside / "keep.txt").write_text("outside data must not move or change\n")
    (manager.sessions_dir / "external").symlink_to(outside, target_is_directory=True)
    alias = manager.sessions_dir / "alias"
    alias.mkdir()
    (alias / "session.json").write_text(json.dumps({**winner, "pid": 2222}))
    metadata = manager.sessions_dir / "metadata"
    metadata.mkdir()
    (metadata / "session.json").symlink_to(outside / "session.json")
    _session(manager.sessions_dir / "traversal", "../../outside", pid=2222)
    monkeypatch.setattr(
        process_control,
        "process_state",
        lambda pid, *args, **kwargs: "alive" if pid == 1111 else "dead",
    )
    winner_before = _snapshot(winner_dir)
    outside_before = _snapshot(outside)
    active_before = manager.active_session_file.read_bytes()

    if entrypoint == "attach_by_pid":
        with pytest.raises(DomainError) as error:
            manager.attach(pid=2222)
        if error.value.code == "session_not_found":
            assert error.value.execution_outcome == "not_executed"
        else:
            _assert_refusal(error.value, corrupt=True)
    else:
        try:
            if entrypoint == "iter_sessions":
                result = [item["id"] for item in manager.iter_sessions()]
            elif entrypoint == "status":
                payload = manager.status(all=True)
                assert isinstance(payload, list)
                result = [item["session_id"] for item in payload]
            else:
                result = manager.prune_dead_sessions()
        except DomainError as error:
            _assert_refusal(error, corrupt=True)
        else:
            assert result == ([] if entrypoint == "prune" else ["winner"])

    assert _snapshot(winner_dir) == winner_before
    assert _snapshot(outside) == outside_before
    assert manager.active_session_file.read_bytes() == active_before


@pytest.mark.parametrize("root_name", ["sessions", "archived_sessions"])
def test_managed_root_symlinks_cannot_redirect_lookup_or_archive_destinations(
    manager: SessionManager, tmp_path: Path, root_name: str
) -> None:
    outside = tmp_path / "outside"
    _session(outside / "escaped", "escaped")
    manager.runtime_root.mkdir()
    (manager.runtime_root / root_name).symlink_to(outside, target_is_directory=True)

    def operation() -> Any:
        if root_name == "sessions":
            return manager.load_session("escaped")
        return manager.archive_session_destination("escaped")

    _refuses_without_changes(tmp_path, operation, corrupt=True)


@pytest.mark.parametrize(
    "session_id",
    [" old playtest ", "   ", "line\r\nname", "세션-é.1", "!@#$%^&()[]{}=+,-_:?*", ".legacy.."],
)
def test_legacy_single_component_ids_remain_usable_without_rewriting_their_names(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch, session_id: str
) -> None:
    directory = manager.sessions_dir / session_id
    data = _session(directory, session_id)
    heartbeat = {"frame": 77}
    Path(data["heartbeat_path"]).write_text(json.dumps(heartbeat))
    manager.write_session({**data, "fps_target": 90.0})
    assert manager.require_session(session_id)["fps_target"] == 90.0

    manager.set_active_session(session_id)
    assert manager.active_session_file.read_bytes() == session_id.encode("utf-8")
    assert manager.get_active_session_id() == session_id
    manager.active_session_file.unlink()
    manager.attach(session=session_id)
    assert manager.get_active_session_id() == session_id
    status = manager.status(session=session_id)
    assert isinstance(status, dict)
    assert status["heartbeat"] == heartbeat
    destination = manager.archive_session_destination(session_id)
    assert destination.parent == manager.archived_sessions_dir
    assert not destination.exists()

    monkeypatch.setattr(process_control, "process_state", lambda *args, **kwargs: "dead")
    assert manager.prune_dead_sessions() == [session_id]
    assert not directory.exists()
    archived = list(manager.archived_sessions_dir.glob("*/session.json"))
    assert [json.loads(path.read_text())["id"] for path in archived] == [session_id]
    assert manager.get_active_session_id() is None


def test_final_activation_cannot_touch_a_replaced_runtime_root(
    manager: SessionManager, tmp_path: Path
) -> None:
    manager.ensure_runtime_dirs()
    retired = tmp_path / "retired-runtime"
    with pytest.raises(DomainError):
        with manager.transaction("candidate", create=True, composite=True) as operation:
            operation._startup_finalization = manager._activate_startup(
                operation,
                "candidate",
                lambda exc: DomainError("startup_failed", str(exc), phase="activation"),
            )
            manager.runtime_root.rename(retired)
            manager.runtime_root.mkdir()
            marker = manager.runtime_root / "active_session"
            marker.write_text("winner")
            before = _snapshot(manager.runtime_root)
    assert _snapshot(manager.runtime_root) == before
    assert (retired / "sessions" / "candidate" / "transaction.json").is_file()
