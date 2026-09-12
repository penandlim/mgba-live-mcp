from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp.session_transactions import (
    archive_session,
    atomic_write_json,
    recovery,
    transaction,
    transaction_status,
)


def _attempt(directory: Path) -> str:
    try:
        with transaction(directory):
            return "entered"
    except RuntimeError as exc:
        return str(exc).split(":", 1)[0]


def _response(directory: Path, request_id: str) -> None:
    response = {"id": request_id, "ok": True, "data": {"owner": request_id}}
    atomic_write_json(directory / "response.json", response)


def _hold_owner(directory_name: str, stage: str, connection: Connection) -> None:
    directory = Path(directory_name)

    def publish() -> None:
        (directory / "command.lua").write_text("mutation")

    try:
        with transaction(
            directory, composite=stage in ("between_phases", "composite_response")
        ) as lease:
            if stage != "before_publish":
                lease.publish("owner-request", publish)
                # Model the bridge consuming the file before running arbitrary user Lua.
                (directory / "command.lua").unlink()
                if stage in (
                    "after_response",
                    "after_complete",
                    "between_phases",
                    "composite_response",
                ):
                    _response(directory, "owner-request")
                if stage in ("after_complete", "between_phases"):
                    lease.complete("owner-request")
            connection.send("ready")
            if connection.recv() == "finish":
                if stage == "executing":
                    _response(directory, "owner-request")
                if stage not in ("before_publish", "after_complete", "between_phases"):
                    response = json.loads((directory / "response.json").read_text())
                    lease.complete("owner-request")
                    connection.send(response)
    except RuntimeError as exc:
        connection.send({"error": str(exc)})
    finally:
        connection.close()


def _contender(connection: Connection) -> None:
    try:
        while (directory := connection.recv()) is not None:
            connection.send(_attempt(Path(directory)))
    finally:
        connection.close()


def _reserve_before_owner_lock(directory_name: str, connection: Connection) -> None:
    original_mkdir = os.mkdir

    def paused_mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        connection.send("reserved")
        connection.recv()

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "mkdir", paused_mkdir)
            with transaction(Path(directory_name), create=True):
                connection.send("entered")
    finally:
        connection.close()


def _hold_publication(directory_name: str, connection: Connection) -> None:
    def publish() -> None:
        connection.send("publishing")
        connection.recv()

    try:
        with transaction(Path(directory_name)) as owner:
            owner.publish("unknown-publication", publish)
    finally:
        connection.close()


@contextmanager
def _child(target: Callable[..., None], *args: Any) -> Iterator[tuple[BaseProcess, Connection]]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        process.close()


def _receive(connection: Connection) -> Any:
    assert connection.poll(10), "child did not reach the bounded event barrier"
    return connection.recv()


def test_process_contention_preserves_unread_response_and_other_sessions(tmp_path: Path) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    with _child(_hold_owner, str(directory), "after_response") as (process, connection):
        assert _receive(connection) == "ready"
        original = (directory / "response.json").read_bytes()
        assert _attempt(directory) == "session_busy"
        with transaction(tmp_path / "independent", create=True) as other:
            other.publish("other-request", lambda: None)
            other.complete("other-request")
        assert (directory / "response.json").read_bytes() == original
        connection.send("finish")
        assert _receive(connection) == {
            "id": "owner-request",
            "ok": True,
            "data": {"owner": "owner-request"},
        }
        process.join(10)
        assert process.exitcode == 0
    with transaction(directory) as successor:
        successor.publish("next-request", lambda: _response(directory, "next-request"))
        successor.complete("next-request")


@pytest.mark.parametrize(
    ("stage", "reclaimable"),
    [
        ("before_publish", True),
        ("executing", False),
        ("after_response", True),
        ("after_complete", True),
        ("between_phases", False),
        ("composite_response", False),
    ],
)
def test_crashed_owner_is_reclaimed_only_at_safe_boundaries(
    tmp_path: Path, stage: str, reclaimable: bool
) -> None:
    directory = tmp_path / "session"
    directory.mkdir()
    with _child(_hold_owner, str(directory), stage) as (process, connection):
        assert _receive(connection) == "ready"
        process.kill()
        process.join(10)
        assert not process.is_alive()

        if reclaimable:
            published = []
            with transaction(directory) as successor:
                successor.publish("new-request", lambda: published.append("new mutation"))
                _response(directory, "new-request")
                successor.complete("new-request")
            assert published == ["new mutation"]
        else:
            assert not (directory / "command.lua").exists()
            assert _attempt(directory) == "session_busy"
            with recovery(directory) as stop:
                stop.finish()
            assert _attempt(directory) == "session_stopped"


def test_creation_reservation_excludes_contenders_before_operation_lock(tmp_path: Path) -> None:
    directory = tmp_path / "new-session"
    with _child(_reserve_before_owner_lock, str(directory)) as (process, connection):
        assert _receive(connection) == "reserved"
        assert _attempt(directory) == "session_busy"
        process.kill()
        process.join(10)
        assert not process.is_alive()
    # A creator dying before any publication leaves no unknown bridge execution.
    assert _attempt(directory) == "entered"


def test_only_matching_abandoned_response_permits_admission(tmp_path: Path) -> None:
    with transaction(tmp_path) as owner:
        owner.publish("abandoned", lambda: None)
    _response(tmp_path, "another-owner")
    assert _attempt(tmp_path) == "session_busy"
    (tmp_path / "response.json").write_text('{"id":"abandoned"')
    assert _attempt(tmp_path) == "session_busy"
    _response(tmp_path, "abandoned")
    assert _attempt(tmp_path) == "entered"
    # Reconciliation neither synthesizes a caller result nor consumes an abandoned response.
    assert json.loads((tmp_path / "response.json").read_text()) == {
        "id": "abandoned",
        "ok": True,
        "data": {"owner": "abandoned"},
    }


def test_nested_startup_composite_owns_every_phase_and_cleanup(tmp_path: Path) -> None:
    directory = tmp_path / "new-session"
    with _child(_contender) as (process, connection):
        with transaction(directory, create=True, composite=True) as outer:
            connection.send(str(directory))
            assert _receive(connection) == "session_busy"
            with transaction(directory, create=True, composite=True) as startup:
                startup.publish("startup", lambda: None)
                startup.complete("startup")
            connection.send(str(directory))
            assert _receive(connection) == "session_busy"
            with transaction(directory) as capture:
                capture.publish("capture", lambda: None)
                capture.complete("capture")
            outer.check()
            connection.send(str(directory))
            assert _receive(connection) == "session_busy"
        connection.send(str(directory))
        assert _receive(connection) == "entered"
        connection.send(None)
        process.join(10)
        assert process.exitcode == 0
    with pytest.raises(RuntimeError, match="session_exists"):
        with transaction(directory, create=True):
            pytest.fail("a completed reservation must not recreate the same session")


def test_copied_context_does_not_grant_another_thread_ownership(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor, transaction(tmp_path):
        assert _attempt(tmp_path) == "entered"
        inherited = copy_context()
        assert (
            executor.submit(inherited.run, _attempt, tmp_path).result(timeout=10) == "session_busy"
        )


def test_child_async_task_is_not_reentrant_despite_context_inheritance(tmp_path: Path) -> None:
    async def contender() -> str:
        return _attempt(tmp_path)

    async def scenario() -> None:
        with transaction(tmp_path):
            assert await contender() == "entered"
            assert await asyncio.create_task(contender()) == "session_busy"

    asyncio.run(scenario())


def test_recovery_fences_process_owner_without_waiting_for_its_operation(tmp_path: Path) -> None:
    with _child(_hold_owner, str(tmp_path), "executing") as (process, connection):
        assert _receive(connection) == "ready"
        with recovery(tmp_path) as stop:
            assert _attempt(tmp_path) == "session_busy"
            stop.finish()
            # The original worker still owns its OS operation lock when stop completes.
            assert process.is_alive()
        connection.send("finish")
        late_result = _receive(connection)
        assert late_result["error"].startswith("session_stopped:")
        process.join(10)
        assert process.exitcode == 0
    assert _attempt(tmp_path) == "session_stopped"


def test_stalled_publication_gets_bounded_recovery_refusal(tmp_path: Path) -> None:
    def attempt_stop() -> str:
        try:
            with recovery(tmp_path):
                return "entered"
        except RuntimeError as exc:
            return str(exc).split(":", 1)[0]

    with _child(_hold_publication, str(tmp_path)) as (process, connection):
        assert _receive(connection) == "publishing"
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                assert executor.submit(attempt_stop).result(timeout=5) == "session_busy"
            finally:
                # Also release the OS lock if a broken implementation blocks forever,
                # so the regression fails rather than hanging the suite.
                process.kill()
                process.join(10)
                assert not process.is_alive()
    assert _attempt(tmp_path) == "session_busy"
    with recovery(tmp_path) as stop:
        stop.finish()
    assert _attempt(tmp_path) == "session_stopped"


def test_failed_recovery_and_explicit_uncertainty_never_reopen_admission(tmp_path: Path) -> None:
    with transaction(tmp_path) as owner:
        owner.mark_uncertain()
    assert _attempt(tmp_path) == "session_busy"
    with pytest.raises(PermissionError):
        with recovery(tmp_path):
            raise PermissionError("termination could not be verified")
    assert _attempt(tmp_path) == "session_stopping"
    with recovery(tmp_path) as stop:
        stop.finish()
    assert _attempt(tmp_path) == "session_stopped"


def test_publication_failure_keeps_unknown_execution_until_a_real_response(tmp_path: Path) -> None:
    def publish_then_fail() -> None:
        (tmp_path / "command.lua").write_text("mutation")
        raise OSError("publication result is ambiguous")

    with pytest.raises(OSError, match="ambiguous"):
        with transaction(tmp_path) as owner:
            owner.publish("possibly-running", publish_then_fail)
    (tmp_path / "command.lua").unlink()
    assert _attempt(tmp_path) == "session_busy"
    _response(tmp_path, "possibly-running")
    assert _attempt(tmp_path) == "entered"


def test_completing_another_request_cannot_retire_owned_execution(tmp_path: Path) -> None:
    with transaction(tmp_path) as owner:
        owner.publish("owned", lambda: None)
        with pytest.raises(RuntimeError, match="session_busy"):
            owner.complete("foreign")
    assert _attempt(tmp_path) == "session_busy"
    _response(tmp_path, "owned")
    assert _attempt(tmp_path) == "entered"


def test_inode_replacement_fences_old_worker_without_touching_new_generation(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"

    def replace_generation() -> None:
        with transaction(directory, create=True) as new:
            new.publish("new-request", lambda: _response(directory, "new-request"))
            new.complete("new-request")

    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(directory, create=True) as old:
            old.publish("old-request", lambda: None)
            directory.rename(tmp_path / "old-session")
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(replace_generation).result(timeout=3)
            published = []
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                old.publish("late-request", lambda: published.append("late mutation"))
            assert published == []
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                old.complete("old-request")
    assert _attempt(directory) == "entered"
    assert json.loads((directory / "response.json").read_text())["id"] == "new-request"


def test_generation_replacement_on_same_inode_cannot_be_cleared_by_old_worker(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(tmp_path) as old:
            old.publish("old-request", lambda: None)
            replacement = transaction_status(tmp_path)
            assert replacement is not None
            replacement["generation"] = "replacement-generation"
            replacement["operation"]["pending_request"] = "new-generation-request"
            atomic_write_json(tmp_path / "transaction.json", replacement)
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                old.complete("old-request")
    assert _attempt(tmp_path) == "session_busy"
    _response(tmp_path, "new-generation-request")
    assert _attempt(tmp_path) == "entered"


def test_missing_directory_is_not_recreated_by_late_cleanup(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(directory, create=True) as owner:
            owner.publish("old-request", lambda: None)
            shutil.rmtree(directory)
            owner.check()
    assert not directory.exists()
    assert transaction_status(directory) is None
    assert not directory.exists()


@pytest.mark.parametrize("damage", ["malformed", "different_inode"])
def test_corrupt_journal_recovery_fences_old_owner_without_reopening_admission(
    tmp_path: Path, damage: str
) -> None:
    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(tmp_path) as old:
            old.publish("owned-request", lambda: None)
            if damage == "malformed":
                (tmp_path / "transaction.json").write_text("{broken")
                expected_error = "session_state_corrupt"
            else:
                damaged = transaction_status(tmp_path)
                assert damaged is not None
                damaged["directory"] = [0, 0]
                atomic_write_json(tmp_path / "transaction.json", damaged)
                expected_error = "session_generation_changed"
            assert _attempt(tmp_path) == expected_error
            status = transaction_status(tmp_path)
            assert status is not None
            assert status["error"].startswith(expected_error)
            with recovery(tmp_path):
                with pytest.raises(RuntimeError, match="session_generation_changed"):
                    old.check()
                with pytest.raises(RuntimeError, match="session_generation_changed"):
                    old.complete("owned-request")
    # Neither replacement of the corrupt journal nor leaving an unfinished recovery
    # grants admission. Only a separately verified termination may finish recovery.
    assert _attempt(tmp_path) == "session_stopping"
    with recovery(tmp_path) as stop:
        stop.finish()
    assert _attempt(tmp_path) == "session_stopped"


def test_archive_excludes_active_operations_and_recovery(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    destination = tmp_path / "archived"
    with transaction(directory, create=True):
        assert not archive_session(directory, destination)
    with recovery(directory):
        assert not archive_session(directory, destination)
    assert directory.exists()
    assert archive_session(directory, destination)
    assert not directory.exists()
    assert _attempt(destination) == "session_stopping"
    assert not archive_session(directory, tmp_path / "missing-archive")
    assert not directory.exists()


def test_late_recovery_cannot_retire_a_replacement_generation(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    directory.mkdir()
    with recovery(directory) as old_stop:
        directory.rename(tmp_path / "old-session")
        with transaction(directory, create=True) as new:
            new.publish("new-request", lambda: None)
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                old_stop.finish()
    assert _attempt(directory) == "session_busy"
    _response(directory, "new-request")
    assert _attempt(directory) == "entered"
