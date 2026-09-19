from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from mgba_live_mcp import deadlines, session_manager, session_transactions
from mgba_live_mcp.errors import CommandTimeout, DomainError
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91Jpz"
    "AAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)
MUTATE = "counter = (counter or 0) + 1; return true"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.wall = 1_800_000_000.0
        self.wall_jump = 0.0
        self.events: list[tuple[float, Any]] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def sleep(self, duration: float) -> None:
        self.now = round(self.now + duration, 9)
        self.wall += duration + self.wall_jump
        ready = [event for event in self.events if event[0] <= self.now + 1e-9]
        self.events = [event for event in self.events if event not in ready]
        for _, callback in ready:
            callback()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = _Clock()
    # Replace module references, not the global clock used by asyncio and pytest.
    for module in (deadlines, session_manager, session_transactions):
        monkeypatch.setattr(module, "time", clock)
    manager = SessionManager(runtime_root=tmp_path / "runtime")
    manager.ensure_runtime_dirs()
    directory = manager.session_dir("budget")
    directory.mkdir()
    target = {
        "id": "budget",
        "pid": 1234567,
        "session_dir": str(directory),
        "command_path": str(directory / "command.lua"),
        "response_path": str(directory / "response.json"),
        "heartbeat_path": str(directory / "heartbeat.json"),
    }
    (directory / "session.json").write_text(json.dumps(target))
    (directory / "heartbeat.json").write_text('{"command_claim":"rename-v1"}')
    monkeypatch.setattr(manager, "_process_state", lambda _: "alive")
    return SimpleNamespace(
        manager=manager,
        target=target,
        directory=directory,
        clock=clock,
        commands=[],
        effect=tmp_path / "effect",
        count=0,
    )


def _native_bridge(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    macro_finished: Event | None = None,
) -> None:
    write_command = runtime.manager.write_command

    def publish(path: Path, command: dict[str, Any], **kwargs: Any) -> None:
        write_command(path, command, **kwargs)
        runtime.commands.append(command)
        running = path.with_name("command.lua.running")
        path.rename(running)
        if command.get("code") == MUTATE or command["kind"] == "tap_key":
            runtime.count += 1
            runtime.effect.write_text(str(runtime.count))

        def respond() -> None:
            data: dict[str, Any] = {"result": True}
            if macro_finished is not None:
                data["result"] = (
                    {"macro_key": "deadline_macro"}
                    if command.get("code") == MUTATE
                    else macro_finished.is_set()
                )
            if command["kind"] == "screenshot":
                Path(command["path"]).write_bytes(PNG)
                data = {"path": command["path"]}
            elif command["kind"] == "tap_key":
                data = {"duration": command["duration"]}
            (runtime.directory / "response.json").write_text(
                json.dumps(
                    {
                        "id": command["id"],
                        "ok": True,
                        "frame": round(runtime.clock.now * 60),
                        "data": data,
                    }
                )
            )
            running.unlink()

        runtime.clock.events.append((runtime.clock.now + 0.2, respond))

    monkeypatch.setattr(runtime.manager, "write_command", publish)


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        ("prepublication", "not_executed"),
        ("pending", "not_executed"),
        ("claimed", "unknown"),
        ("responded", "completed"),
        ("legacy", "unknown"),
    ],
)
def test_expiry_reconciles_only_proven_native_outcomes(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    outcome: str,
) -> None:
    write_command = runtime.manager.write_command
    if phase == "legacy":
        (runtime.directory / "heartbeat.json").write_text('{"frame":1}')

    def publish(path: Path, command: dict[str, Any], **kwargs: Any) -> None:
        runtime.commands.append(command)
        if phase == "prepublication":
            runtime.clock.sleep(0.5)
        write_command(path, command, **kwargs)
        if phase in {"claimed", "responded"}:
            path.rename(path.with_name("command.lua.running"))
            runtime.effect.write_text("1")
        if phase == "responded":
            (runtime.directory / "response.json").write_text(
                json.dumps({"id": command["id"], "ok": True, "data": {}})
            )
        runtime.clock.sleep(0.5)

    monkeypatch.setattr(runtime.manager, "write_command", publish)
    with pytest.raises(CommandTimeout) as raised:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=0.5)
    failure = raised.value
    assert failure.execution_outcome == outcome
    assert failure.phase == "dispatch"
    assert failure.context["session_id"] == "budget"
    assert failure.context["request_id"] == runtime.commands[0]["id"]
    assert len(runtime.commands) == 1
    assert runtime.effect.exists() is (phase in {"claimed", "responded"})
    if outcome == "completed":
        assert failure.context["command_completed"] is True
        assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    if outcome == "unknown":
        with pytest.raises(DomainError) as busy:
            runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
        assert busy.value.code == "session_busy"
        assert len(runtime.commands) == 1
    else:
        with runtime.manager.transaction("budget"):
            pass
        assert not (runtime.directory / "command.lua").exists()


@pytest.mark.parametrize("wall_jump", [-3_600_000.0, 3_600_000.0])
def test_composite_spends_one_budget_and_keeps_completed_mutation(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    wall_jump: float,
) -> None:
    _native_bridge(runtime, monkeypatch)
    runtime.clock.wall_jump = wall_jump
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=0.5)
    failure = raised.value
    assert runtime.clock.now == pytest.approx(0.5)
    assert failure.code == "snapshot_failed"
    assert failure.execution_outcome == "completed"
    assert failure.context["command_completed"] is True
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert failure.context["request_id"] == runtime.commands[2]["id"]
    assert failure.context["cause_code"] == "command_timeout"
    assert failure.context["cause_execution_outcome"] == "unknown"
    assert [command["kind"] for command in runtime.commands] == [
        "run_lua_inline",
        "run_lua_inline",
        "screenshot",
    ]
    assert runtime.effect.read_text() == "1"
    with pytest.raises(DomainError) as busy:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
    assert busy.value.code == "session_busy"
    assert runtime.effect.read_text() == "1"
    # A late capture does not clear an interrupted composite's recovery fence.
    capture = Path(runtime.commands[2]["path"])
    runtime.clock.sleep(0.2)
    assert capture.read_bytes() == PNG
    with pytest.raises(DomainError) as still_busy:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
    assert still_busy.value.code == "session_busy"
    assert runtime.effect.read_text() == "1"


def test_finalization_io_error_preserves_completed_primary_and_original_cause(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native_bridge(runtime, monkeypatch)
    write_json = session_transactions._Directory.write_json
    failed = False

    def write(directory, name, payload):
        nonlocal failed
        if (
            name == session_transactions._JOURNAL
            and payload["operation"] is None
            and runtime.effect.exists()
            and not failed
        ):
            failed = True
            raise OSError("journal device failed")
        return write_json(directory, name, payload)

    monkeypatch.setattr(session_transactions._Directory, "write_json", write)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=2)
    failure = raised.value
    assert failure.code == "io_error"
    assert isinstance(failure.__cause__, OSError)
    assert failure.execution_outcome == "completed"
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert runtime.effect.read_text() == "1"


def test_expired_worker_queue_cannot_start_mutation(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    to_thread = asyncio.to_thread

    async def queued(function, *args, **kwargs):
        runtime.clock.sleep(0.5)
        return await to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", queued)
    _native_bridge(runtime, monkeypatch)
    client = LiveControllerClient(manager=runtime.manager)
    with pytest.raises(CommandTimeout) as raised:
        asyncio.run(client.run_lua(session="budget", code=MUTATE, timeout=0.5))
    failure = raised.value
    assert failure.phase == "acquisition"
    assert failure.execution_outcome == "not_executed"
    assert not runtime.effect.exists()
    assert runtime.commands == []
    assert not (runtime.directory / "command.lua").exists()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "1", None, 10**400])
def test_invalid_budget_is_rejected_before_startup_allocation(tmp_path: Path, timeout: Any) -> None:
    manager = SessionManager(runtime_root=tmp_path / "untouched")
    with pytest.raises(DomainError) as raised:
        manager.start(rom="missing.gb", session_id="invalid", ready_timeout=timeout)
    assert raised.value.code == "invalid_arguments"
    assert raised.value.execution_outcome == "not_executed"
    assert raised.value.context["session_id"] == "invalid"
    assert not manager.runtime_root.exists()


@pytest.mark.parametrize("late_response", [False, True])
def test_native_failure_survives_expired_response_completion(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    late_response: bool,
) -> None:
    write_command = runtime.manager.write_command
    complete = session_transactions.Transaction.complete

    def publish(path, command, **kwargs):
        write_command(path, command, **kwargs)
        path.unlink()
        runtime.commands.append(command)
        (runtime.directory / "response.json").write_text(
            json.dumps(
                {
                    "id": command["id"],
                    "ok": False,
                    "code": "serialization_failed",
                    "error": "native result contains a cycle",
                    "serialization_reason": "cycle",
                    "command_completed": True,
                    "execution_outcome": "completed",
                }
            )
        )
        if late_response:
            runtime.clock.sleep(0.5)

    def finish(operation, request):
        complete(operation, request)
        if not late_response:
            runtime.clock.sleep(0.5)

    monkeypatch.setattr(runtime.manager, "write_command", publish)
    monkeypatch.setattr(session_transactions.Transaction, "complete", finish)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua(session="budget", code="return cyclic", timeout=0.5)
    failure = raised.value
    assert failure.code == "serialization_failed"
    assert failure.execution_outcome == "completed"
    assert failure.context["serialization_reason"] == "cycle"
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert failure.context["completion_error"]["code"] == "command_timeout"


def test_committed_attachment_is_completed_when_result_misses_deadline(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.target.update(rom="game.gb", fps_target=60, mgba_path="mgba")
    (runtime.directory / "session.json").write_text(json.dumps(runtime.target))
    activate = runtime.manager.set_active_session

    def delayed_release(session):
        activate(session)
        runtime.clock.sleep(0.5)

    monkeypatch.setattr(runtime.manager, "set_active_session", delayed_release)
    client = LiveControllerClient(manager=runtime.manager)
    with pytest.raises(CommandTimeout) as raised:
        asyncio.run(client.attach(session="budget", timeout=0.5))
    assert raised.value.execution_outcome == "completed"
    assert "command_completed" not in raised.value.context
    assert "command_request_id" not in raised.value.context
    assert runtime.manager.get_active_session_id() == "budget"


def test_withdrawal_journal_failure_does_not_erase_nonexecution_proof(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json = session_transactions._Directory.write_json

    def fail_after_withdrawal(directory, name, payload):
        operation = payload.get("operation")
        if (
            name == session_transactions._JOURNAL
            and operation
            and operation["pending_request"] is None
            and runtime.clock.now >= 0.5
        ):
            raise OSError("withdrawal journal failed")
        return write_json(directory, name, payload)

    monkeypatch.setattr(session_transactions._Directory, "write_json", fail_after_withdrawal)
    with pytest.raises(CommandTimeout) as raised:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=0.5)
    assert raised.value.execution_outcome == "not_executed"
    assert raised.value.context["request_withdrawn"] is True
    assert "recovery_error" in raised.value.context
    assert not (runtime.directory / "command.lua").exists()


@pytest.mark.parametrize("filename", ["session.json", session_transactions._JOURNAL])
def test_acquisition_timeout_is_not_misreported_as_corrupt_state(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    read_json = session_transactions._Directory.read_json

    def delayed_read(directory, name):
        if name == filename:
            runtime.clock.sleep(0.5)
        return read_json(directory, name)

    monkeypatch.setattr(session_transactions._Directory, "read_json", delayed_read)
    with pytest.raises(CommandTimeout) as raised:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=0.5)
    assert raised.value.execution_outcome == "not_executed"
    assert raised.value.phase == "acquisition"
    assert not (runtime.directory / "command.lua").exists()


@pytest.mark.parametrize(
    ("state", "phase", "outcome"),
    [
        ("dead", "command", "unknown"),
        ("identity_mismatch", "command", "unknown"),
        ("identity_unverified", "admission", "not_executed"),
        ("permission_denied", "admission", "not_executed"),
    ],
)
def test_known_process_failure_is_not_replaced_by_timeout(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    phase: str,
    outcome: str,
) -> None:
    from mgba_live_mcp import process_control

    def inspect(*args, **kwargs):
        if phase == "admission" or (runtime.directory / "command.lua").exists():
            runtime.clock.sleep(0.5)
            return state
        return "alive"

    monkeypatch.setattr(
        runtime.manager, "_process_state", SessionManager._process_state.__get__(runtime.manager)
    )
    monkeypatch.setattr(process_control, "process_state", inspect)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=0.5)
    assert raised.value.code == ("session_dead" if state == "dead" else state)
    assert raised.value.phase == phase
    assert raised.value.execution_outcome == outcome


def test_settle_timeout_remains_primary_when_uncertainty_journaling_fails(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native_bridge(runtime, monkeypatch)

    def fail_journal(operation):
        raise OSError("uncertainty journal failed")

    monkeypatch.setattr(session_transactions.Transaction, "mark_uncertain", fail_journal)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=0.3)
    failure = raised.value
    assert failure.code == "settle_failed"
    assert isinstance(failure.__cause__, CommandTimeout)
    assert failure.execution_outcome == "unknown"
    assert failure.context["command_completed"] is True
    assert failure.context["cause_code"] == "command_timeout"
    assert "journal_error" in failure.context
    assert runtime.effect.read_text() == "1"


def test_expiry_before_capture_does_not_attribute_readiness_request_to_capture(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native_bridge(runtime, monkeypatch)
    capture = runtime.manager.get_view

    def delayed_capture(**kwargs):
        runtime.clock.sleep(kwargs["timeout"])
        return capture(**kwargs)

    monkeypatch.setattr(runtime.manager, "get_view", delayed_capture)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=0.5)
    failure = raised.value
    assert failure.execution_outcome == "completed"
    assert failure.context["cause_execution_outcome"] == "not_executed"
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert "request_id" not in failure.context
    assert runtime.effect.read_text() == "1"


def test_macro_settle_timeout_keeps_completion_evidence_and_recovery_fence(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    macro_finished = Event()
    _native_bridge(runtime, monkeypatch, macro_finished=macro_finished)
    with pytest.raises(DomainError) as raised:
        runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=0.5)
    failure = raised.value
    assert failure.code == "settle_failed"
    assert failure.phase == "settle"
    assert failure.execution_outcome == "unknown"
    assert failure.context["command_completed"] is True
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert failure.context["cause_code"] == "command_timeout"
    assert failure.context["cause_execution_outcome"] == "unknown"
    assert failure.context["request_id"] == runtime.commands[-1]["id"]
    assert failure.context["request_id"] != failure.context["command_request_id"]
    assert not macro_finished.is_set()
    assert all(command["kind"] != "screenshot" for command in runtime.commands)
    assert runtime.effect.read_text() == "1"
    with pytest.raises(DomainError) as busy:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
    assert busy.value.code == "session_busy"
    # Even late macro completion cannot silently reclaim an interrupted composite.
    macro_finished.set()
    runtime.clock.sleep(0.2)
    with pytest.raises(DomainError) as still_busy:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
    assert still_busy.value.code == "session_busy"
    assert runtime.effect.read_text() == "1"


@pytest.mark.parametrize("operation", ["lua", "tap"])
@pytest.mark.parametrize("settle_started", [False, True])
def test_expiry_before_settle_attempt_does_not_reuse_completed_request(
    runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    settle_started: bool,
) -> None:
    _native_bridge(runtime, monkeypatch, macro_finished=Event() if operation == "lua" else None)
    if settle_started:
        sleep = runtime.clock.sleep

        def expire_after_poll_response(duration):
            if len(runtime.commands) == 2 and not runtime.clock.events:
                runtime.clock.now = 0.5
            else:
                sleep(duration)

        monkeypatch.setattr(runtime.clock, "sleep", expire_after_poll_response)
    else:
        primitive_name = "run_lua" if operation == "lua" else "input_tap"
        primary = getattr(runtime.manager, primitive_name)

        def return_at_expiry(**kwargs):
            result = primary(**kwargs)
            runtime.clock.now = 0.5
            return result

        monkeypatch.setattr(runtime.manager, primitive_name, return_at_expiry)

    with pytest.raises(DomainError) as raised:
        if operation == "lua":
            runtime.manager.run_lua_and_view(session="budget", code=MUTATE, timeout=0.5)
        else:
            runtime.manager.input_tap_and_view(session="budget", key="A", frames=30, timeout=0.5)
    failure = raised.value
    assert failure.code == "settle_failed"
    assert failure.execution_outcome == "unknown"
    assert failure.context["command_completed"] is True
    assert failure.context["command_request_id"] == runtime.commands[0]["id"]
    assert failure.context["cause_code"] == "command_timeout"
    assert failure.context["cause_phase"] == "settle"
    assert failure.context["cause_execution_outcome"] == "not_executed"
    assert "request_id" not in failure.context
    assert len(runtime.commands) == (2 if settle_started else 1)
    assert all(command["kind"] != "screenshot" for command in runtime.commands)
    with pytest.raises(DomainError) as busy:
        runtime.manager.run_lua(session="budget", code=MUTATE, timeout=1)
    assert busy.value.code == "session_busy"
    assert runtime.effect.read_text() == "1"
