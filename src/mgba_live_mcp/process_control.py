"""Birth-verified control of dedicated POSIX process groups.

Linux uses /proc start ticks plus the kernel boot UUID. macOS uses libproc birth
times and kernel boot UUID, including birth metadata available for zombies.
Other platforms fail closed; a missing leader does not establish group absence.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import signal
import sys
import time
from functools import cache
from pathlib import Path
from typing import Any

from .errors import DomainError, ExecutionOutcome

# macOS SDK: sys/proc_info.h (proc_bsdinfo, PROC_PIDTBSDINFO,
# PROC_FLAG_SLEADER), sys/param.h (MAXCOMLEN), sys/proc.h (SZOMB), and
# libproc.h (proc_pidinfo). uid_t and gid_t are uint32_t in sys/_types.h.
_PROC_PIDTBSDINFO = 3
_PROC_FLAG_SLEADER = 0x20
_SZOMB = 5


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _RusageInfoV0(ctypes.Structure):
    # sys/resource.h; proc_pid_rusage supports both live and zombie processes.
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


@cache
def _libproc() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    library.proc_pid_rusage.restype = ctypes.c_int
    return library


@cache
def _darwin_boot_id() -> str:
    libc = ctypes.CDLL(None, use_errno=True)
    query = libc.sysctlbyname
    query.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    query.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(37)
    size = ctypes.c_size_t(len(buffer))
    if query(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code))
    value = buffer.value.decode("ascii")
    if not value:
        raise ValueError("Missing kernel boot identity")
    return value


def _darwin_lifetime(pid: int) -> tuple[int, int]:
    info = _RusageInfoV0()
    ctypes.set_errno(0)
    if _libproc().proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code))
    if not info.ri_proc_start_abstime:
        raise ValueError("Missing native process birth time")
    return info.ri_proc_start_abstime, info.ri_proc_exit_abstime


def _inspect_process(pid: int) -> tuple[dict[str, Any], bool]:
    if sys.platform == "linux":
        stat = Path(f"/proc/{pid}/stat").read_text()
        prefix, separator, suffix = stat.rpartition(")")
        if not separator or int(prefix.split("(", 1)[0]) != pid:
            raise ValueError("Invalid process stat record")
        fields = suffix.split()
        pgid, sid = int(fields[2]), int(fields[3])
        birth = {
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": int(fields[19]),
        }
        zombie = fields[0] in {"Z", "X", "x"}
    elif sys.platform == "darwin":
        start_abstime, _ = _darwin_lifetime(pid)
        info = _ProcBsdInfo()
        ctypes.set_errno(0)
        size = _libproc().proc_pidinfo(
            pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if size <= 0:
            code = ctypes.get_errno() or errno.EIO
            raise OSError(code, os.strerror(code))
        if size != ctypes.sizeof(info) or info.pbi_pid != pid:
            raise ValueError("Incomplete native process identity")
        pgid = info.pbi_pgid
        # A dedicated session leader's SID is its PID. This flag and the birth
        # time come from the same native snapshot, avoiding a getsid/PID race.
        sid = pid if info.pbi_flags & _PROC_FLAG_SLEADER else os.getsid(pid)
        confirmed_start, exited_at = _darwin_lifetime(pid)
        if start_abstime != confirmed_start:
            raise ValueError("Process changed during native identity inspection")
        birth = {
            "start_seconds": info.pbi_start_tvsec,
            "start_microseconds": info.pbi_start_tvusec,
            "boot_id": _darwin_boot_id(),
            "start_abstime": start_abstime,
        }
        zombie = info.pbi_status == _SZOMB or exited_at != 0
    else:
        raise NotImplementedError(f"Process identity is unsupported on {sys.platform}")
    return {
        "version": 1,
        "platform": sys.platform,
        "pid": pid,
        "pgid": pgid,
        "sid": sid,
        "birth": birth,
    }, zombie


def _valid_pid(pid: object) -> bool:
    return type(pid) is int and 0 < pid <= 0x7FFFFFFF


def _valid_identity(identity: dict[str, Any] | None) -> bool:
    if not isinstance(identity, dict):
        return False
    if type(identity.get("version")) is not int or identity["version"] != 1:
        return False
    if identity.get("platform") != sys.platform:
        return False
    if not all(_valid_pid(identity.get(key)) for key in ("pid", "pgid", "sid")):
        return False
    birth = identity.get("birth")
    if not isinstance(birth, dict):
        return False
    if sys.platform == "linux":
        return (
            isinstance(birth.get("boot_id"), str)
            and bool(birth["boot_id"])
            and type(birth.get("start_ticks")) is int
            and birth["start_ticks"] > 0
        )
    if sys.platform == "darwin":
        return (
            type(birth.get("start_seconds")) is int
            and birth["start_seconds"] > 0
            and type(birth.get("start_microseconds")) is int
            and 0 <= birth["start_microseconds"] < 1_000_000
            and isinstance(birth.get("boot_id"), str)
            and bool(birth["boot_id"])
            and type(birth.get("start_abstime")) is int
            and birth["start_abstime"] > 0
        )
    return False


def _owned_group(pid: int, identity: dict[str, Any]) -> bool:
    return (
        identity["pid"] == identity["pgid"] == identity["sid"] == pid
        and pid != os.getpid()
        and pid != os.getpgrp()
    )


def _failure(
    code: str,
    pid: int,
    stage: str,
    detail: str = "",
    *,
    execution_outcome: ExecutionOutcome = "not_started",
) -> DomainError:
    return DomainError(
        code,
        f"pid={pid} stage={stage}; {detail}".rstrip("; "),
        phase=stage,
        execution_outcome=execution_outcome,
        pid=pid,
    )


def capture_identity(pid: int) -> dict[str, Any]:
    """Capture a JSON-safe birth identity immediately after start_new_session.

    Only a dedicated session/group leader outside our own group is accepted.
    Missing native metadata is a refusal, never permission to use PID-only stop.
    """
    if not _valid_pid(pid):
        raise _failure("identity_unverified", pid, "capture", "Invalid process ID")
    try:
        identity, _ = _inspect_process(pid)
    except (OSError, ValueError, IndexError, NotImplementedError) as exc:
        code = (
            "permission_denied"
            if isinstance(exc, OSError) and exc.errno in {errno.EPERM, errno.EACCES}
            else "identity_unverified"
        )
        raise _failure(code, pid, "capture", str(exc)) from exc
    if not _valid_identity(identity):
        raise _failure("identity_unverified", pid, "capture", "Incomplete birth identity")
    if not _owned_group(pid, identity):
        raise _failure("identity_mismatch", pid, "capture", "Not an owned dedicated group")
    return identity


def _group_absence(pgid: int) -> str:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "termination_unconfirmed"
    return "termination_unconfirmed"


def _inspection_failure(pid: int, exc: Exception) -> str:
    # Missing /proc/libproc data does not establish absence (e.g. hidepid).
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _group_absence(pid)
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "identity_unverified"
    if isinstance(exc, OSError) and exc.errno in {errno.EPERM, errno.EACCES}:
        return "permission_denied"
    return "identity_unverified"


def process_state(pid: int, identity: dict[str, Any] | None, *, reap: bool = False) -> str:
    """Return alive/dead or a conservative ownership/inspection failure code.

    Legacy records can be confirmed dead, but cannot be confirmed owned/alive.
    Reaping is opt-in for recovery or maintenance after readiness. Startup
    observers must preserve their Popen owner's exit status.
    """
    if sys.platform not in {"linux", "darwin"} or not _valid_pid(pid):
        return "identity_unverified"
    valid = _valid_identity(identity)
    if valid and identity is not None and not _owned_group(pid, identity):
        return "identity_mismatch"
    try:
        current, zombie = _inspect_process(pid)
    except (OSError, ValueError, IndexError, NotImplementedError) as exc:
        if (
            reap
            and valid
            and identity is not None
            and sys.platform == "darwin"
            and isinstance(exc, ProcessLookupError)
        ):
            # BSD metadata omits Darwin zombies. Rusage still exposes birth and
            # exit times: verify them before collecting any child by PID.
            try:
                start_abstime, exited_at = _darwin_lifetime(pid)
                if (
                    identity["birth"]["boot_id"] != _darwin_boot_id()
                    or identity["birth"]["start_abstime"] != start_abstime
                ):
                    return "identity_mismatch"
                if not exited_at:
                    return "identity_unverified"
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            except (OSError, ValueError) as native_error:
                return _inspection_failure(pid, native_error)
        return _inspection_failure(pid, exc)
    if not valid:
        return "identity_unverified"
    if not _valid_identity(current):
        return "identity_unverified"
    if current != identity:
        return "identity_mismatch"
    if not _owned_group(pid, current):
        return "identity_mismatch"
    if not zombie:
        return "alive"
    if not reap:
        return _group_absence(pid)
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except PermissionError:
        return "permission_denied"
    except OSError:
        return "termination_unconfirmed"
    return _group_absence(pid)


def _wait_for_exit(pid: int, identity: dict[str, Any] | None, deadline: float) -> str:
    while True:
        state = process_state(pid, identity, reap=True)
        if state in {"dead", "identity_mismatch"}:
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        time.sleep(min(0.05, remaining))


def terminate_owned_process(
    pid: int,
    identity: dict[str, Any] | None,
    *,
    grace: float = 1.0,
    confirmation_timeout: float = 1.0,
) -> str:
    """TERM, optionally KILL, then confirm the whole dedicated group is gone.

    The monotonic overall budget is grace + confirmation_timeout seconds.
    Permission/ESRCH races get at most confirmation_timeout within that budget;
    only confirmed group absence suppresses a signal error. Every destructive
    call gets a fresh birth/group check. An exited leader never authorizes KILL
    of remaining members on group number alone. There are no signal retries.
    """
    if not math.isfinite(grace) or grace < 0:
        raise ValueError("grace must be finite and nonnegative")
    if not math.isfinite(confirmation_timeout) or confirmation_timeout < 0:
        raise ValueError("confirmation_timeout must be finite and nonnegative")
    deadline = time.monotonic() + grace + confirmation_timeout
    state = process_state(pid, identity, reap=True)
    if state == "dead":
        return "already_exited"
    if state != "alive":
        if state in {"permission_denied", "termination_unconfirmed"}:
            state = _wait_for_exit(
                pid, identity, min(deadline, time.monotonic() + confirmation_timeout)
            )
            if state == "dead":
                return "already_exited"
        raise _failure(state, pid, "inspect", "Ownership or group exit could not be verified")

    attempted = False
    for sig, stage, duration in (
        (signal.SIGTERM, "TERM", grace),
        (signal.SIGKILL, "KILL", confirmation_timeout),
    ):
        # Last observation before signaling: never reuse an earlier live check.
        state = process_state(pid, identity, reap=True)
        if state != "alive":
            if state in {"permission_denied", "termination_unconfirmed"}:
                state = _wait_for_exit(
                    pid, identity, min(deadline, time.monotonic() + confirmation_timeout)
                )
            if state == "dead":
                return "stopped" if attempted else "already_exited"
            raise _failure(
                state,
                pid,
                stage,
                "Pre-signal ownership check failed",
                execution_outcome="unknown" if attempted else "not_started",
            )
        attempted = True
        try:
            os.killpg(pid, sig)
        except OSError as exc:
            state = _wait_for_exit(
                pid, identity, min(deadline, time.monotonic() + confirmation_timeout)
            )
            if state == "dead":
                return "stopped"
            code = (
                "permission_denied"
                if exc.errno in {errno.EPERM, errno.EACCES}
                else "termination_unconfirmed"
            )
            if state == "identity_mismatch":
                code = state
            raise _failure(
                code, pid, stage, f"{exc}; observed={state}", execution_outcome="unknown"
            ) from exc
        state = _wait_for_exit(pid, identity, min(deadline, time.monotonic() + duration))
        if state == "dead":
            return "stopped"
        if sig == signal.SIGTERM and state == "alive":
            continue
        if sig == signal.SIGTERM and state in {"permission_denied", "termination_unconfirmed"}:
            state = _wait_for_exit(pid, identity, deadline)
            if state == "dead":
                return "stopped"
        code = "termination_unconfirmed" if state == "alive" else state
        raise _failure(
            code,
            pid,
            stage,
            "Exit confirmation expired; session remains unresolved",
            execution_outcome="unknown",
        )
    raise AssertionError("Unreachable termination state")
