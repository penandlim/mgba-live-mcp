from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mgba_live_mcp import session_transactions
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.session_transactions import transaction, transaction_status


def _command(path: Path, request_id: str) -> None:
    quoted = json.dumps(request_id)
    path.write_text(f"-- mgba-live-request={quoted}\nreturn {{ id = {quoted} }}\n")


def test_pending_request_can_be_withdrawn_before_native_claim(tmp_path: Path) -> None:
    (tmp_path / "heartbeat.json").write_text('{"command_claim":"rename-v1"}')
    with transaction(tmp_path) as owner:
        owner.publish("pending", lambda: _command(tmp_path / "command.lua", "pending"))
        assert owner.withdraw("pending") is True
        assert not (tmp_path / "command.lua").exists()
    state = transaction_status(tmp_path)
    assert state is not None and state["operation"] is None


def test_claimed_request_remains_fenced_and_foreign_file_survives(tmp_path: Path) -> None:
    (tmp_path / "heartbeat.json").write_text('{"command_claim":"rename-v1"}')
    with transaction(tmp_path) as owner:
        owner.publish("claimed", lambda: _command(tmp_path / "command.lua", "claimed"))
        (tmp_path / "command.lua").rename(tmp_path / "command.lua.running")
        (tmp_path / "command.lua").write_text("foreign")
        assert owner.withdraw("claimed") is False
        assert (tmp_path / "command.lua").read_text() == "foreign"
        state = transaction_status(tmp_path)
        assert state is not None
        assert state["operation"]["pending_request"] == "claimed"


@pytest.mark.parametrize("newer_pending", [False, True])
def test_withdrawal_race_never_deletes_or_overwrites_foreign_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newer_pending: bool,
) -> None:
    (tmp_path / "heartbeat.json").write_text('{"command_claim":"rename-v1"}')
    rename, link = os.rename, os.link

    def native_wins(source, destination, **kwargs):
        if source == "command.lua" and str(destination).startswith(".command.lua.withdraw."):
            rename(source, "command.lua.running", **kwargs)
            (tmp_path / "command.lua").write_text("foreign replacement")
        return rename(source, destination, **kwargs)

    def replace_before_restore(source, destination, **kwargs):
        if newer_pending and str(source).startswith(".command.lua.withdraw."):
            (tmp_path / "command.lua").write_text("newer pending")
        return link(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", native_wins)
    monkeypatch.setattr(os, "link", replace_before_restore)
    with transaction(tmp_path) as owner:
        owner.publish("owned", lambda: _command(tmp_path / "command.lua", "owned"))
        if newer_pending:
            with pytest.raises(DomainError) as raised:
                owner.withdraw("owned")
            failure = raised.value
            assert failure.execution_outcome == "unknown"
            assert (
                Path(failure.context["retained_command_path"]).read_text() == "foreign replacement"
            )
            assert (tmp_path / "command.lua").read_text() == "newer pending"
        else:
            assert owner.withdraw("owned") is False
            assert (tmp_path / "command.lua").read_text() == "foreign replacement"
        assert '"owned"' in (tmp_path / "command.lua.running").read_text()
    with pytest.raises(DomainError) as busy:
        with transaction(tmp_path):
            pass
    assert busy.value.code == "session_busy"


def test_withdrawal_proof_survives_journal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "heartbeat.json").write_text('{"command_claim":"rename-v1"}')
    write_json = session_transactions._Directory.write_json
    with transaction(tmp_path) as owner:
        owner.publish("owned", lambda: _command(tmp_path / "command.lua", "owned"))

        def fail_after_withdrawal(directory, name, payload):
            operation = payload.get("operation")
            if (
                name == session_transactions._JOURNAL
                and operation
                and operation["pending_request"] is None
            ):
                raise OSError("journal disk failed after withdrawal")
            return write_json(directory, name, payload)

        monkeypatch.setattr(session_transactions._Directory, "write_json", fail_after_withdrawal)
        with pytest.raises(DomainError) as raised:
            owner.withdraw("owned")
        assert raised.value.execution_outcome == "not_executed"
        assert raised.value.context["request_withdrawn"] is True
        assert not (tmp_path / "command.lua").exists()
        assert not (tmp_path / "command.lua.running").exists()
