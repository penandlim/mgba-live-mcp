from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

import mgba_live_mcp.live_cli as live_cli
from mgba_live_mcp import process_control
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.session_manager import SessionManager


def test_resolve_session_requires_explicit_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager(runtime_root=tmp_path)
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)
    with pytest.raises(DomainError) as error:
        live_cli.resolve_session(argparse.Namespace(session=None))
    assert error.value.code == "session_required"
    assert error.value.execution_outcome == "not_executed"


def test_parser_requires_session_for_existing_session_commands() -> None:
    parser = live_cli.build_parser()
    assert parser.parse_args(["status", "--all"]).all is True
    with pytest.raises(SystemExit):
        parser.parse_args(["stop"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run-lua", "--code", "return true"])
    with pytest.raises(SystemExit):
        parser.parse_args(["input-set", "--keys", "A"])


def test_cli_stop_reports_already_exited_without_archiving_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = SessionManager(runtime_root=tmp_path)
    manager.ensure_runtime_dirs()
    with manager.transaction("session-dead", create=True):
        manager.write_session({"id": "session-dead", "pid": 424242})
    manager.set_active_session("session-dead")
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)
    monkeypatch.setattr(process_control, "process_state", lambda *_, **__: "dead")
    monkeypatch.setattr(sys, "argv", ["mgba_live.py", "stop", "--session", "session-dead"])

    for _ in range(2):
        live_cli.main()
        result = json.loads(capsys.readouterr().out)
        assert result["session_id"] == "session-dead"
        assert result["outcome"] == "already_exited"
        assert result["alive_after"] is False
    assert manager.get_active_session_id() is None


@pytest.mark.parametrize("timeout", [False, 0, -1, float("nan"), float("inf")])
def test_cli_timeout_rejected_before_manager_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    manager = SessionManager(runtime_root=tmp_path)
    called = False

    def fail_manager():
        nonlocal called
        called = True
        return manager

    monkeypatch.setattr(live_cli, "_manager", fail_manager)
    args = argparse.Namespace(session="missing", pid=None, timeout=timeout)
    with pytest.raises((DomainError, ValueError)):
        live_cli.cmd_attach(args)
    assert called is False
