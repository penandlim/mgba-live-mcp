from __future__ import annotations

import ctypes
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mgba_live_mcp import process_control as pc

_PID = 424242
_DARWIN_BIRTH = {
    "boot_id": "fixture-boot",
    "start_seconds": 1_700_000_000,
    "start_microseconds": 123456,
    "start_abstime": 100,
}


def _identity() -> dict[str, Any]:
    return {
        "version": 1,
        "platform": "linux",
        "pid": _PID,
        "pgid": _PID,
        "sid": _PID,
        "birth": {
            "boot_id": "d88bc669-38f2-48da-8655-ff1eb5d0b85f",
            "start_ticks": 100,
        },
    }


class _Process:
    """An owned group whose only signal destinations are in-memory recorders."""

    def __init__(self) -> None:
        self.current = _identity()
        self.leader = True
        self.group = True
        self.zombie = False
        self.child = True
        self.survivors = False
        self.inspect_error: OSError | None = None
        self.probe_error: OSError | None = None
        self.group_error: OSError | None = None
        self.signals: list[int] = []
        self.reaped: list[int] = []
        self.clock = 0.0
        self.on_signal: Callable[[int], None] = lambda sig: None
        self.on_sleep: Callable[[], None] = lambda: None
        self.on_inspect: Callable[[], None] = lambda: None

    def inspect(self, pid: int) -> tuple[dict[str, Any], bool]:
        assert pid == _PID
        self.on_inspect()
        if self.inspect_error:
            raise self.inspect_error
        if not self.leader:
            raise ProcessLookupError(errno.ESRCH, "leader absent")
        return self.current, self.zombie

    def kill(self, pid: int, sig: int) -> None:
        assert (pid, sig) == (_PID, 0), "PID probes must never send destructive signals"
        if self.probe_error:
            raise self.probe_error
        if not self.leader:
            raise ProcessLookupError(errno.ESRCH, "leader absent")

    def killpg(self, pgid: int, sig: int) -> None:
        assert pgid == _PID, "Never even probe an unrelated group"
        if sig:
            self.signals.append(sig)
            self.on_signal(sig)
        elif self.group_error:
            raise self.group_error
        if not self.group:
            raise ProcessLookupError(errno.ESRCH, "group absent")

    def waitpid(self, pid: int, options: int) -> tuple[int, int]:
        assert (pid, options) == (_PID, os.WNOHANG)
        if not self.child:
            raise ChildProcessError(errno.ECHILD, "not our child")
        if not self.zombie:
            return 0, 0
        self.reaped.append(pid)
        self.leader = False
        self.group = self.survivors
        return pid, 0

    def sleep(self, duration: float) -> None:
        assert duration > 0
        self.clock += duration
        self.on_sleep()

    def exit(self) -> None:
        self.leader = False
        self.group = False


@pytest.fixture
def process(monkeypatch: pytest.MonkeyPatch) -> _Process:
    fake = _Process()
    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc, "_inspect_process", fake.inspect)
    monkeypatch.setattr(pc.os, "getpid", lambda: 17)
    monkeypatch.setattr(pc.os, "getpgrp", lambda: 17)
    monkeypatch.setattr(pc.os, "kill", fake.kill)
    monkeypatch.setattr(pc.os, "killpg", fake.killpg)
    monkeypatch.setattr(pc.os, "waitpid", fake.waitpid)
    monkeypatch.setattr(pc.time, "monotonic", lambda: fake.clock)
    monkeypatch.setattr(pc.time, "sleep", fake.sleep)
    return fake


def test_birth_reuse_refuses_status_and_stop_without_signaling(process: _Process) -> None:
    process.current["birth"]["start_ticks"] += 1

    assert pc.process_state(_PID, _identity()) == "identity_mismatch"
    with pytest.raises(RuntimeError, match=r"^identity_mismatch:.*stage=inspect"):
        pc.terminate_owned_process(_PID, _identity())
    assert process.signals == []


@pytest.mark.parametrize("field", ["pgid", "sid"])
def test_unrelated_group_or_session_never_receives_signals(process: _Process, field: str) -> None:
    process.current[field] = _PID + 1

    with pytest.raises(RuntimeError, match="^identity_mismatch:"):
        pc.terminate_owned_process(_PID, _identity())
    assert process.signals == []


def test_caller_group_is_not_owned_even_with_matching_birth(
    process: _Process, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pc.os, "getpgrp", lambda: _PID)

    with pytest.raises(RuntimeError, match="^identity_mismatch:"):
        pc.terminate_owned_process(_PID, _identity())
    assert process.signals == []


@pytest.mark.parametrize("pid", [0, -1, True, 0x80000000])
def test_invalid_pid_never_reaches_native_or_signal_calls(process: _Process, pid: int) -> None:
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.terminate_owned_process(pid, _identity())
    assert process.signals == []


@pytest.mark.parametrize("identity", [None, {}, {"pid": _PID}])
def test_live_legacy_or_malformed_record_cannot_authorize_signals(
    process: _Process, identity: dict[str, Any] | None
) -> None:
    assert pc.process_state(_PID, identity) == "identity_unverified"
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.terminate_owned_process(_PID, identity)
    assert process.signals == []


def test_legacy_absence_requires_leader_and_group_to_be_gone(process: _Process) -> None:
    process.leader = False

    assert pc.process_state(_PID, None) == "termination_unconfirmed"
    with pytest.raises(RuntimeError, match="^termination_unconfirmed:"):
        pc.terminate_owned_process(_PID, None, grace=0, confirmation_timeout=0.1)
    assert process.signals == []

    process.group = False
    assert pc.process_state(_PID, None) == "dead"
    assert pc.terminate_owned_process(_PID, None) == "already_exited"


def test_missing_native_metadata_is_not_absence(process: _Process) -> None:
    process.inspect_error = FileNotFoundError(errno.ENOENT, "procfs unavailable")

    assert pc.process_state(_PID, _identity()) == "identity_unverified"
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.terminate_owned_process(_PID, _identity())
    assert process.signals == []


def test_permission_denied_inspection_is_not_death(process: _Process) -> None:
    process.inspect_error = PermissionError(errno.EPERM, "native inspection denied")
    process.probe_error = PermissionError(errno.EPERM, "probe denied")

    assert pc.process_state(_PID, _identity()) == "permission_denied"
    with pytest.raises(RuntimeError, match=r"^permission_denied:.*stage=inspect"):
        pc.terminate_owned_process(_PID, _identity(), grace=0.1, confirmation_timeout=0.2)
    assert process.signals == []
    assert process.clock == pytest.approx(0.2)


def test_permission_denied_inspection_can_resolve_only_on_confirmed_exit(process: _Process) -> None:
    process.inspect_error = PermissionError(errno.EPERM, "inspection exit race")
    process.on_sleep = process.exit

    assert (
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.2)
        == "already_exited"
    )
    assert process.signals == []


@pytest.mark.parametrize("stage", [signal.SIGTERM, signal.SIGKILL])
def test_permission_signal_race_waits_for_confirmed_group_exit(
    process: _Process, stage: int
) -> None:
    def signal_error(sig: int) -> None:
        if sig == stage:
            process.on_sleep = process.exit
            raise PermissionError(errno.EPERM, "signal raced with exit")

    process.on_signal = signal_error

    assert (
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.2)
        == "stopped"
    )
    assert process.signals == (
        [signal.SIGTERM] if stage == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL]
    )
    assert process.clock > 0


@pytest.mark.parametrize("stage", [signal.SIGTERM, signal.SIGKILL])
def test_persistent_signal_permission_denial_has_a_bounded_stage_error(
    process: _Process, stage: int
) -> None:
    def signal_error(sig: int) -> None:
        if sig == stage:
            raise PermissionError(errno.EPERM, "persistent denial")

    process.on_signal = signal_error
    expected_stage = "TERM" if stage == signal.SIGTERM else "KILL"

    with pytest.raises(RuntimeError, match=rf"^permission_denied:.*stage={expected_stage}"):
        pc.terminate_owned_process(_PID, _identity(), grace=0.1, confirmation_timeout=0.2)
    assert process.signals == (
        [signal.SIGTERM] if stage == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL]
    )
    assert process.clock <= 0.3 + 1e-9


def test_esrch_from_signal_is_not_success_while_group_survives(process: _Process) -> None:
    def leader_exit(sig: int) -> None:
        process.leader = False
        raise ProcessLookupError(errno.ESRCH, "leader exited")

    process.on_signal = leader_exit

    with pytest.raises(RuntimeError, match=r"^termination_unconfirmed:.*stage=TERM"):
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.1)
    assert process.signals == [signal.SIGTERM]


def test_esrch_signal_race_succeeds_only_after_group_absence(process: _Process) -> None:
    def exit_race(sig: int) -> None:
        process.exit()
        raise ProcessLookupError(errno.ESRCH, "group exited")

    process.on_signal = exit_race

    assert pc.terminate_owned_process(_PID, _identity()) == "stopped"
    assert process.signals == [signal.SIGTERM]


def test_matching_zombie_child_is_reaped_before_group_exit_is_reported(process: _Process) -> None:
    process.zombie = True

    assert pc.terminate_owned_process(_PID, _identity()) == "already_exited"
    assert process.reaped == [_PID]
    assert process.signals == []


def test_unrelated_zombie_is_not_reaped(process: _Process) -> None:
    process.zombie = True
    process.current["birth"]["start_ticks"] += 1

    assert pc.process_state(_PID, _identity()) == "identity_mismatch"
    assert process.reaped == []


def test_nonchild_zombie_and_surviving_group_are_not_reported_stopped(process: _Process) -> None:
    process.zombie = True
    process.child = False

    with pytest.raises(RuntimeError, match="^termination_unconfirmed:"):
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.1)
    assert process.signals == []
    assert process.reaped == []


def test_owned_nonchild_does_not_require_waitpid_to_confirm_exit(process: _Process) -> None:
    process.child = False
    process.on_signal = lambda sig: process.exit()

    assert pc.process_state(_PID, _identity()) == "alive"
    assert pc.terminate_owned_process(_PID, _identity()) == "stopped"
    assert process.signals == [signal.SIGTERM]


def test_post_kill_confirmation_waits_and_reaps_instead_of_sampling_once(process: _Process) -> None:
    def delay_kill_exit(sig: int) -> None:
        if sig == signal.SIGKILL:
            process.on_sleep = lambda: setattr(process, "zombie", True)

    process.on_signal = delay_kill_exit

    assert (
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.2)
        == "stopped"
    )
    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.reaped == [_PID]
    assert process.clock > 0


def test_post_kill_survival_is_unconfirmed_within_the_overall_budget(process: _Process) -> None:
    with pytest.raises(RuntimeError, match=r"^termination_unconfirmed:.*stage=KILL"):
        pc.terminate_owned_process(_PID, _identity(), grace=0.1, confirmation_timeout=0.2)
    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.clock == pytest.approx(0.3)


def test_leader_first_exit_never_authorizes_kill_of_surviving_group(process: _Process) -> None:
    def leave_survivor(sig: int) -> None:
        process.zombie = True
        process.survivors = True

    process.on_signal = leave_survivor

    with pytest.raises(RuntimeError, match=r"^termination_unconfirmed:.*stage=TERM"):
        pc.terminate_owned_process(_PID, _identity(), grace=0.1, confirmation_timeout=0.2)
    assert process.signals == [signal.SIGTERM]
    assert process.reaped == [_PID]
    assert process.group
    assert process.clock == pytest.approx(0.3)


def test_uninspectable_group_after_leader_exit_is_not_dead(process: _Process) -> None:
    process.leader = False
    process.group_error = PermissionError(errno.EPERM, "group probe denied")

    assert pc.process_state(_PID, _identity()) == "permission_denied"
    with pytest.raises(RuntimeError, match="^permission_denied:"):
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.1)
    assert process.signals == []


@pytest.mark.parametrize("change_at", [2, 4])
def test_birth_is_rechecked_immediately_before_each_destructive_signal(
    process: _Process, change_at: int
) -> None:
    observations = 0

    def replace_at_signal_boundary() -> None:
        nonlocal observations
        observations += 1
        # Initial inspection, pre-TERM, zero-grace observation, pre-KILL.
        if observations == change_at:
            process.current["birth"]["start_ticks"] += 1

    process.on_inspect = replace_at_signal_boundary

    with pytest.raises(RuntimeError, match="^identity_mismatch:"):
        pc.terminate_owned_process(_PID, _identity(), grace=0, confirmation_timeout=0.1)
    assert process.signals == ([] if change_at == 2 else [signal.SIGTERM])


def test_unsupported_platform_refuses_without_probing_or_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: Any) -> None:
        pytest.fail("Unsupported platforms must not invoke native process operations")

    monkeypatch.setattr(pc.sys, "platform", "win32")
    monkeypatch.setattr(pc.os, "kill", unexpected)
    monkeypatch.setattr(pc.os, "killpg", unexpected)

    assert pc.process_state(_PID, None) == "identity_unverified"
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.capture_identity(_PID)
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.terminate_owned_process(_PID, None)


def test_linux_birth_is_stable_across_states_and_changes_across_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc, "Path", lambda path: tmp_path / path.removeprefix("/proc/"))
    monkeypatch.setattr(pc.os, "getpid", lambda: 17)
    monkeypatch.setattr(pc.os, "getpgrp", lambda: 17)
    stat = tmp_path / str(_PID) / "stat"
    stat.parent.mkdir()
    boot = tmp_path / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text(_identity()["birth"]["boot_id"])
    fields = ["S", "17", str(_PID), str(_PID)] + ["0"] * 15 + ["100"]
    stat.write_text(f"{_PID} (command with ) parentheses\nand spaces) {' '.join(fields)}")

    identity = json.loads(json.dumps(pc.capture_identity(_PID)))
    assert pc.process_state(_PID, identity) == "alive"
    fields[0] = "Z"
    stat.write_text(f"{_PID} (changed name) {' '.join(fields)}")
    assert pc.capture_identity(_PID) == identity

    boot.write_text("a5479459-d159-47dc-83df-0f6a4d5a812a")
    assert pc.process_state(_PID, identity) == "identity_mismatch"


def test_macos_birth_uses_microseconds_and_excludes_process_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc.sys, "platform", "darwin")
    monkeypatch.setattr(pc.os, "getpid", lambda: 17)
    monkeypatch.setattr(pc.os, "getpgrp", lambda: 17)
    info = pc._ProcBsdInfo()
    info.pbi_pid = _PID
    info.pbi_pgid = _PID
    info.pbi_flags = 0x20
    info.pbi_status = 2
    info.pbi_start_tvsec = 1_700_000_000
    info.pbi_start_tvusec = 123456
    usage = pc._RusageInfoV0()
    usage.ri_proc_start_abstime = 100

    def pidinfo(pid: int, flavor: int, arg: int, buffer: Any, size: int) -> int:
        ctypes.memmove(buffer, ctypes.byref(info), size)
        return size

    def rusage(pid: int, flavor: int, buffer: Any) -> int:
        ctypes.memmove(buffer, ctypes.byref(usage), ctypes.sizeof(usage))
        return 0

    monkeypatch.setattr(
        pc, "_libproc", lambda: SimpleNamespace(proc_pidinfo=pidinfo, proc_pid_rusage=rusage)
    )
    monkeypatch.setattr(pc, "_darwin_boot_id", lambda: "fixture-boot")
    identity = json.loads(json.dumps(pc.capture_identity(_PID)))
    assert pc.process_state(_PID, identity) == "alive"
    info.pbi_status = 5
    assert pc.capture_identity(_PID) == identity
    info.pbi_status = 2
    info.pbi_start_tvusec += 1
    assert pc.process_state(_PID, identity) == "identity_mismatch"

    info.pbi_start_tvusec -= 1
    usage.ri_proc_start_abstime += 1
    assert pc.process_state(_PID, identity) == "identity_mismatch"

    # Reuse between the rusage and BSD snapshots must not mint a mixed identity.
    births = iter([(100, 0), (101, 0)])
    monkeypatch.setattr(pc, "_darwin_lifetime", lambda _: next(births))
    with pytest.raises(RuntimeError, match="^identity_unverified:"):
        pc.capture_identity(_PID)


@pytest.mark.parametrize("child", [True, False])
def test_macos_missing_zombie_metadata_requires_confirmed_group_exit(
    process: _Process, monkeypatch: pytest.MonkeyPatch, child: bool
) -> None:
    monkeypatch.setattr(pc.sys, "platform", "darwin")
    process.current["platform"] = "darwin"
    process.current["birth"] = dict(_DARWIN_BIRTH)
    process.zombie = True
    process.child = child
    process.inspect_error = ProcessLookupError(errno.ESRCH, "Darwin omits zombie metadata")
    monkeypatch.setattr(pc, "_darwin_boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(pc, "_darwin_lifetime", lambda _: (100, 200))

    assert pc.process_state(_PID, process.current) == "identity_unverified"
    if child:
        assert pc.terminate_owned_process(_PID, process.current) == "already_exited"
    else:
        with pytest.raises(RuntimeError, match="^identity_unverified:"):
            pc.terminate_owned_process(_PID, process.current)
    assert process.signals == []
    assert process.reaped == ([_PID] if child else [])


@pytest.mark.parametrize(
    ("birth", "exited", "boot", "error"),
    [
        (101, 200, "fixture-boot", "identity_mismatch"),
        (100, 200, "replacement-boot", "identity_mismatch"),
        (100, 0, "fixture-boot", "identity_unverified"),
        (None, None, "fixture-boot", "permission_denied"),
    ],
    ids=["reused-pid", "reboot", "not-exited", "birth-unavailable"],
)
def test_macos_unverified_zombie_is_neither_reaped_nor_signaled(
    process: _Process,
    monkeypatch: pytest.MonkeyPatch,
    birth: int | None,
    exited: int | None,
    boot: str,
    error: str,
) -> None:
    monkeypatch.setattr(pc.sys, "platform", "darwin")
    process.current["platform"] = "darwin"
    process.current["birth"] = dict(_DARWIN_BIRTH)
    process.zombie = True
    process.inspect_error = ProcessLookupError(errno.ESRCH, "BSD metadata unavailable")
    monkeypatch.setattr(pc, "_darwin_boot_id", lambda: boot)

    def lifetime(pid: int) -> tuple[int, int]:
        if birth is None or exited is None:
            raise PermissionError(errno.EPERM, "rusage unavailable")
        return birth, exited

    monkeypatch.setattr(pc, "_darwin_lifetime", lifetime)
    with pytest.raises(RuntimeError, match=f"^{error}:"):
        pc.terminate_owned_process(_PID, process.current, grace=0, confirmation_timeout=0)
    assert process.reaped == []
    assert process.signals == []


@contextmanager
def _owned_child(ignore_term: bool) -> Iterator[subprocess.Popen[str]]:
    code = (
        "import signal, sys, time\n"
        "if sys.argv[1] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(10)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code, "ignore" if ignore_term else "exit"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "Owned subprocess did not become ready"
        assert proc.stdout.readline() == "ready\n"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX identity support")
@pytest.mark.parametrize("ignore_term", [False, True], ids=["term-exit", "kill-required"])
def test_native_owned_group_returns_only_after_confirmed_exit(ignore_term: bool) -> None:
    with _owned_child(ignore_term) as proc:
        identity = json.loads(json.dumps(pc.capture_identity(proc.pid)))
        assert pc.process_state(proc.pid, identity) == "alive"
        assert (
            pc.terminate_owned_process(proc.pid, identity, grace=0.05, confirmation_timeout=1)
            == "stopped"
        )
        assert pc.process_state(proc.pid, identity) == "dead"
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
