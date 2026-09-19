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
from threading import Event
from typing import Any

import pytest

from mgba_live_mcp import session_transactions
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.session_transactions import (
    archive_session,
    atomic_write_json,
    atomic_write_text,
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


def _hold_startup(directory_name: str, connection: Connection) -> None:
    try:
        with transaction(Path(directory_name), create=True, composite=True) as owner:
            with owner.startup_guard():
                connection.send("guarded")
                connection.recv()
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
        with pytest.raises(DomainError) as refused:
            with transaction(directory):
                pytest.fail("a competing operation must not be admitted")
        assert refused.value.code == "session_busy"
        assert refused.value.execution_outcome == "not_executed"
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


def test_publish_refusal_does_not_start_replacement_request(tmp_path: Path) -> None:
    with transaction(tmp_path) as owner:
        owner.publish("first", (tmp_path / "first").touch)
        with pytest.raises(DomainError) as refused:
            owner.publish("second", (tmp_path / "second").touch)
        assert refused.value.code == "session_busy"
        assert refused.value.phase == "publish"
        assert refused.value.execution_outcome == "not_executed"
        assert refused.value.context["request_id"] == "second"
        assert refused.value.context["pending_request_id"] == "first"
        assert not (tmp_path / "second").exists()
        journal = transaction_status(tmp_path)
        assert journal is not None
        assert journal["operation"]["pending_request"] == "first"


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


def test_prelaunch_rollback_preserves_failure_and_allows_nested_reservation_reuse(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("unrelated")
    spawn_failure = OSError("spawn did not return")
    with transaction(directory, create=True, composite=True) as outer:
        with pytest.raises(OSError) as failed:
            with transaction(directory, create=True, composite=True) as startup:
                screenshots = directory / "screenshots"
                screenshots.mkdir()
                (screenshots / "staged.png").write_bytes(b"staged")
                (directory / "outside").symlink_to(outside, target_is_directory=True)
                try:
                    with startup.startup_guard():
                        raise spawn_failure
                except OSError:
                    startup.rollback()
                    raise
        assert failed.value is spawn_failure
        assert not directory.exists()
        assert (outside / "keep").read_text() == "unrelated"
        with transaction(directory, create=True) as successor:
            replacement_generation = successor.generation
            outer.rollback()
            successor.check()
    state = transaction_status(directory)
    assert state is not None
    assert state["generation"] == replacement_generation
    assert _attempt(directory) == "entered"


@pytest.mark.parametrize("failure_stage", ["initialization", "before_publish", "after_publish"])
def test_failed_reservation_setup_removes_prelaunch_directory(
    tmp_path: Path, failure_stage: str
) -> None:
    directory = tmp_path / "session"
    failure = OSError("journal setup failed")
    original_write = session_transactions._Directory.write_json

    def fail_write(owned: Any, name: str, payload: Any) -> None:
        if failure_stage == "after_publish":
            original_write(owned, name, payload)
        raise failure

    def fail_initialize(owned: Any, status: str) -> dict[str, Any]:
        raise failure

    with pytest.MonkeyPatch.context() as patch:
        if failure_stage == "initialization":
            patch.setattr(session_transactions, "_new_state", fail_initialize)
        else:
            patch.setattr(session_transactions._Directory, "write_json", fail_write)
        with pytest.raises(OSError) as failed:
            with transaction(directory, create=True):
                pytest.fail("failed setup must not yield an operation")
    assert failed.value is failure
    assert not directory.exists()
    with transaction(directory, create=True) as successor:
        successor.publish("retry", lambda: _response(directory, "retry"))
        successor.complete("retry")
    assert _attempt(directory) == "entered"


@pytest.mark.parametrize("failure_stage", ["open_directory", "open_operation_lock"])
def test_failed_reservation_lease_acquisition_removes_only_its_directory(
    tmp_path: Path, failure_stage: str
) -> None:
    directory = tmp_path / "session"
    failure = PermissionError("reservation could not be opened")
    original_open = os.open

    def fail_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        target = directory.name if failure_stage == "open_directory" else ".operation.lock"
        if path == target:
            raise failure
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", fail_open)
        with pytest.raises(PermissionError) as failed:
            with transaction(directory, create=True):
                pytest.fail("failed acquisition must not yield an operation")
    assert failed.value is failure
    assert not directory.exists()
    with transaction(directory, create=True):
        pass
    assert _attempt(directory) == "entered"


def test_failed_reservation_setup_retains_original_error_and_cleanup_diagnostic(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"
    setup_failure = OSError("journal storage unavailable")
    cleanup_failure = PermissionError("removing reservation denied")
    original_rmdir = os.rmdir

    def fail_write(owned: Any, name: str, payload: Any) -> None:
        raise setup_failure

    def fail_remove(path: Any, *, dir_fd: int | None = None) -> None:
        if path == directory.name and dir_fd is not None:
            raise cleanup_failure
        original_rmdir(path, dir_fd=dir_fd)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_transactions._Directory, "write_json", fail_write)
        patch.setattr(os, "rmdir", fail_remove)
        with pytest.raises(OSError) as failed:
            with transaction(directory, create=True):
                pytest.fail("failed setup must not yield an operation")
    assert failed.value is setup_failure
    assert any(str(cleanup_failure) in note for note in failed.value.__notes__)
    assert directory.exists()
    with pytest.raises(RuntimeError, match="session_exists"):
        with transaction(directory, create=True):
            pytest.fail("failed removal must not free the session name")


def test_rollback_removal_failure_remains_actionable_and_fenced(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    cleanup_failure = PermissionError("removing reservation denied")
    original_rmdir = os.rmdir

    def fail_remove(path: Any, *, dir_fd: int | None = None) -> None:
        if path == directory.name and dir_fd is not None:
            raise cleanup_failure
        original_rmdir(path, dir_fd=dir_fd)

    with pytest.raises(PermissionError) as failed:
        with transaction(directory, create=True, composite=True) as owner:
            generation = owner.generation
            with owner.startup_guard():
                pass
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(os, "rmdir", fail_remove)
                owner.rollback()
    assert failed.value is cleanup_failure
    state = transaction_status(directory)
    assert state is not None
    assert state["generation"] == generation
    assert _attempt(directory) == "session_busy"
    with pytest.raises(RuntimeError, match="session_exists"):
        with transaction(directory, create=True):
            pytest.fail("failed removal must not free the session name")
    with recovery(directory) as stop:
        stop.finish()
    assert _attempt(directory) == "session_stopped"


def test_replaced_inode_fences_rollback_and_startup_without_touching_winner(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"
    retired = tmp_path / "retired"

    def reserve_winner() -> None:
        with transaction(directory, create=True) as winner:
            (directory / "winner").write_text("keep")
            winner.write_metadata({"owner": "winner"})

    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(directory, create=True, composite=True) as owner:
            (directory / "staged").write_text("old")
            directory.rename(retired)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(reserve_winner).result(timeout=3)
            winner_journal = (directory / "transaction.json").read_bytes()
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                owner.rollback()
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                with owner.startup_guard():
                    pytest.fail("an old worker must not spawn in a replacement")
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                owner.write_metadata({"owner": "stale"})
            assert json.loads((directory / "session.json").read_text()) == {"owner": "winner"}
            assert (directory / "transaction.json").read_bytes() == winner_journal
            assert (directory / "winner").read_text() == "keep"
            assert (retired / "staged").read_text() == "old"
    assert _attempt(directory) == "entered"


@pytest.mark.parametrize("replacement", ["generation", "operation"])
def test_replaced_journal_owner_cannot_rollback_or_prepare_spawn(
    tmp_path: Path, replacement: str
) -> None:
    directory = tmp_path / "session"
    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(directory, create=True, composite=True) as owner:
            owner.write_metadata({"owner": "original"})
            state = transaction_status(directory)
            assert state is not None
            if replacement == "generation":
                state["generation"] = "replacement-generation"
            else:
                state["operation"]["id"] = "replacement-operation"
            atomic_write_json(directory / "transaction.json", state)
            winner_journal = (directory / "transaction.json").read_bytes()
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                owner.rollback()
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                with owner.startup_guard():
                    pytest.fail("a replaced operation must not spawn")
            with pytest.raises(RuntimeError, match="session_generation_changed"):
                owner.write_metadata({"owner": "stale"})
            assert json.loads((directory / "session.json").read_text()) == {"owner": "original"}
            assert (directory / "transaction.json").read_bytes() == winner_journal


def test_recovery_owner_excludes_rollback_and_fences_startup(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    with pytest.raises(RuntimeError, match="session_stopped"):
        with transaction(directory, create=True, composite=True) as owner:
            with owner.startup_guard():
                owner.write_metadata({"phase": "starting"})
            assert json.loads((directory / "session.json").read_text()) == {"phase": "starting"}
            with recovery(directory) as stop:
                recovery_journal = (directory / "transaction.json").read_bytes()
                with pytest.raises(RuntimeError, match="session_busy"):
                    owner.rollback()
                with pytest.raises(RuntimeError, match="session_stopping"):
                    with owner.startup_guard():
                        pytest.fail("recovery already fenced startup")
                owner.write_metadata({"phase": "failed", "error": "startup was stopped"})
                assert json.loads((directory / "session.json").read_text()) == {
                    "phase": "failed",
                    "error": "startup was stopped",
                }
                assert (directory / "transaction.json").read_bytes() == recovery_journal
                stop.finish()
                with pytest.raises(RuntimeError, match="session_generation_changed"):
                    owner.write_metadata({"phase": "stale"})
    assert _attempt(directory) == "session_stopped"


@pytest.mark.parametrize("unresolved", ["pending_request", "uncertain"])
def test_rollback_rejects_unresolved_bridge_work(tmp_path: Path, unresolved: str) -> None:
    directory = tmp_path / "session"
    with transaction(directory, create=True, composite=True) as owner:
        if unresolved == "pending_request":
            owner.publish("pending", lambda: None)
        else:
            owner.mark_uncertain()
        original = (directory / "transaction.json").read_bytes()
        with pytest.raises(RuntimeError, match="session_busy"):
            owner.rollback()
        assert (directory / "transaction.json").read_bytes() == original
    assert _attempt(directory) == "session_busy"


def test_rollback_requires_the_creating_worker(tmp_path: Path) -> None:
    with transaction(tmp_path) as existing:
        with pytest.raises(RuntimeError, match="session_busy"):
            existing.rollback()
        existing.check()
    directory = tmp_path / "session"
    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction(directory, create=True) as owner:
            inherited = copy_context()
            with pytest.raises(RuntimeError, match="session_busy"):
                executor.submit(inherited.run, owner.rollback).result(timeout=3)
            with owner.startup_guard():
                with pytest.raises(RuntimeError, match="session_busy"):
                    executor.submit(
                        inherited.run, owner.write_metadata, {"owner": "foreign"}
                    ).result(timeout=3)
            owner.check()
            owner.rollback()
    assert not directory.exists()


def test_startup_guard_journals_before_spawn_and_bounds_recovery(tmp_path: Path) -> None:
    directory = tmp_path / "session"

    def attempt_stop() -> str:
        try:
            with recovery(directory):
                return "entered"
        except RuntimeError as exc:
            return str(exc).split(":", 1)[0]

    with _child(_hold_startup, str(directory)) as (process, connection):
        assert _receive(connection) == "guarded"
        state = transaction_status(directory)
        assert state is not None
        assert state["operation"]["started"] is True
        assert state["operation"]["pending_request"] is None
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                assert executor.submit(attempt_stop).result(timeout=5) == "session_busy"
            finally:
                process.kill()
                process.join(10)
                assert not process.is_alive()
    assert _attempt(directory) == "session_busy"
    with recovery(directory) as stop:
        stop.finish()
    assert _attempt(directory) == "session_stopped"


def test_failed_startup_journaling_prevents_spawn_and_allows_rollback(tmp_path: Path) -> None:
    directory = tmp_path / "session"

    def fail_write(owned: Any, name: str, payload: Any) -> None:
        raise OSError("startup journal unavailable")

    with transaction(directory, create=True, composite=True) as owner:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_transactions._Directory, "write_json", fail_write)
            with pytest.raises(OSError, match="startup journal unavailable"):
                with owner.startup_guard():
                    pytest.fail("spawn must not run without a durable started marker")
        owner.rollback()
    assert not directory.exists()


def test_postspawn_failure_keeps_startup_composite_recoverable(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    failure = OSError("process registration failed after spawn")
    with pytest.raises(OSError) as failed:
        with transaction(directory, create=True, composite=True) as owner:
            with owner.startup_guard():
                raise failure
    assert failed.value is failure
    assert _attempt(directory) == "session_busy"
    with recovery(directory) as stop:
        stop.finish()
    assert _attempt(directory) == "session_stopped"


def test_atomic_text_preserves_plain_session_id_and_previous_value_on_failure(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "active_session"
    session_id = "세션 with spaces!?"
    atomic_write_text(marker, session_id)
    assert marker.read_bytes() == session_id.encode("utf-8")

    def fail_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        raise PermissionError("marker publication denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(PermissionError, match="marker publication denied"):
            atomic_write_text(marker, "next-session")
    assert marker.read_bytes() == session_id.encode("utf-8")


def test_atomic_text_does_not_publish_into_a_replacement_parent(tmp_path: Path) -> None:
    directory = tmp_path / "runtime"
    directory.mkdir()
    marker = directory / "active_session"
    atomic_write_text(marker, "original")
    original_fsync = os.fsync
    retired = tmp_path / "retired"

    def replace_parent(fd: int) -> None:
        directory.rename(retired)
        directory.mkdir()
        (directory / "active_session").write_text("winner")
        original_fsync(fd)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", replace_parent)
        with pytest.raises(RuntimeError, match="session_generation_changed"):
            atomic_write_text(marker, "stale")
    assert marker.read_text() == "winner"
    assert (retired / "active_session").read_text() == "original"


def test_failed_reservation_setup_cannot_remove_replacement_inode(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    retired = tmp_path / "retired"
    failure = OSError("reservation publication interrupted")
    original_write = session_transactions._Directory.write_json
    replaced = False

    def replace_then_fail(owned: Any, name: str, payload: Any) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            directory.rename(retired)
            with transaction(directory, create=True) as winner:
                winner.write_metadata({"owner": "winner"})
            raise failure
        original_write(owned, name, payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_transactions._Directory, "write_json", replace_then_fail)
        with pytest.raises(OSError) as failed:
            with transaction(directory, create=True):
                pytest.fail("failed setup must not yield an operation")
    assert failed.value is failure
    assert json.loads((directory / "session.json").read_text()) == {"owner": "winner"}
    assert retired.exists()
    assert _attempt(directory) == "entered"


def test_rollback_removes_children_through_its_fd_not_a_replacement_path(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    retired = tmp_path / "retired"
    original_remove = shutil.rmtree

    def replace_then_remove(path: Any, *, dir_fd: int | None = None) -> None:
        directory.rename(retired)
        directory.mkdir()
        staged = directory / "screenshots"
        staged.mkdir()
        (staged / "winner.png").write_bytes(b"keep")
        (directory / "session.json").write_text(json.dumps({"owner": "winner"}))
        original_remove(path, dir_fd=dir_fd)

    with pytest.raises(RuntimeError, match="session_generation_changed"):
        with transaction(directory, create=True) as owner:
            staged = directory / "screenshots"
            staged.mkdir()
            (staged / "old.png").write_bytes(b"old")
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(shutil, "rmtree", replace_then_remove)
                with pytest.raises(RuntimeError, match="session_generation_changed"):
                    owner.rollback()
    assert (directory / "screenshots" / "winner.png").read_bytes() == b"keep"
    assert json.loads((directory / "session.json").read_text()) == {"owner": "winner"}
    assert _attempt(directory) == "entered"


def test_parent_namespace_replacement_cannot_redirect_child_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()
    directory = parent / "session"
    directory.mkdir()
    retired = tmp_path / "retired-sessions"
    outside = tmp_path / "outside"
    (outside / "session").mkdir(parents=True)
    sentinel = outside / "session" / "keep"
    sentinel.write_bytes(b"outside owner")
    acquired, resume = Event(), Event()
    original_lock = session_transactions._Directory.lock

    @contextmanager
    def pause_after_parent_lock(owned: Any, name: str, **kwargs: Any) -> Iterator[None]:
        with original_lock(owned, name, **kwargs):
            if owned.path == parent:
                acquired.set()
                assert resume.wait(5), "Parent replacement was not released"
            yield

    def acquire() -> str:
        try:
            with transaction(directory):
                return "entered"
        except DomainError as exc:
            return exc.code

    monkeypatch.setattr(session_transactions._Directory, "lock", pause_after_parent_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(acquire)
        try:
            assert acquired.wait(5), "The original parent lock was not acquired"
            parent.rename(retired)
            parent.symlink_to(outside, target_is_directory=True)
        finally:
            resume.set()
        outcome = worker.result(timeout=5)

    touched = sorted(
        str(path.relative_to(outside)) for path in outside.rglob("*") if path.is_file()
    )
    assert touched == ["session/keep"]
    assert sentinel.read_bytes() == b"outside owner"
    assert outcome == "session_generation_changed"


def test_parent_namespace_replacement_cannot_redirect_rollback_lock(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    parent = runtime / "sessions"
    parent.mkdir(parents=True)
    directory = parent / "session"
    retired = tmp_path / "retired-runtime"
    outside = tmp_path / "outside"
    (outside / "sessions" / "session").mkdir(parents=True)
    sentinel = outside / "sessions" / "session" / "keep"
    sentinel.write_bytes(b"outside owner")
    reserved, resume = Event(), Event()

    def reserve_and_rollback() -> str:
        try:
            with transaction(directory, create=True) as owner:
                reserved.set()
                assert resume.wait(5), "Parent replacement was not released"
                owner.rollback()
                return "removed"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(reserve_and_rollback)
        try:
            assert reserved.wait(5), "The original reservation was not acquired"
            runtime.rename(retired)
            runtime.symlink_to(outside, target_is_directory=True)
        finally:
            resume.set()
        outcome = worker.result(timeout=5)

    touched = sorted(
        str(path.relative_to(outside)) for path in outside.rglob("*") if path.is_file()
    )
    assert touched == ["sessions/session/keep"]
    assert sentinel.read_bytes() == b"outside owner"
    assert (retired / "sessions" / "session" / "transaction.json").is_file()
    assert outcome == "session_generation_changed"


def test_initialization_lock_refusal_preserves_the_requested_session_id(tmp_path: Path) -> None:
    directory = tmp_path / " session \n\t"
    directory.mkdir()
    parent = session_transactions._Directory(tmp_path)
    try:
        with parent.lock(session_transactions._initialization_lock_name(directory)):
            with pytest.raises(DomainError) as refused:
                with transaction(directory):
                    pytest.fail("another initialization owner must prevent admission")
    finally:
        parent.close()
    assert refused.value.code == "session_busy"
    assert refused.value.context["session_id"] == directory.name
