"""One cooperative monotonic deadline across an operation and its worker thread."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .errors import CommandTimeout, DomainError, ExecutionOutcome, error_payload

P = ParamSpec("P")
R = TypeVar("R")


def validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        valid = False
    else:
        try:
            valid = math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
    if not valid:
        raise DomainError(
            "invalid_arguments",
            "timeout must be a finite positive number, not a boolean.",
            phase="validation",
            execution_outcome="not_executed",
        )
    return float(value)


@dataclass(slots=True)
class Deadline:
    expires_at: float
    session_id: str | None = None
    request_id: str | None = None
    phase: str = "acquisition"
    execution_outcome: ExecutionOutcome = "not_executed"

    def remaining(self, phase: str | None = None) -> float:
        if phase is not None:
            self.phase = phase
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise CommandTimeout(
                "command_timeout",
                f"Operation deadline expired during {self.phase}.",
                phase=self.phase,
                execution_outcome=self.execution_outcome,
                session_id=self.session_id,
                request_id=self.request_id,
                command_completed=(
                    True
                    if self.execution_outcome == "completed" and self.request_id is not None
                    else None
                ),
                command_request_id=(
                    self.request_id if self.execution_outcome == "completed" else None
                ),
                recovery=(
                    "Timeout is not cancellation. Do not replay a possibly executed mutation. "
                    "Inspect session status; use verified recovery stop if ownership is unresolved."
                ),
            )
        return remaining

    def check(self, phase: str | None = None) -> None:
        self.remaining(phase)

    def begin(self, phase: str) -> None:
        self.request_id = None
        self.execution_outcome = "not_executed"
        self.check(phase)


_CURRENT: ContextVar[Deadline | None] = ContextVar("mgba_operation_deadline", default=None)


def current_deadline() -> Deadline | None:
    return _CURRENT.get()


def check_deadline(phase: str | None = None) -> None:
    deadline = current_deadline()
    if deadline is not None:
        deadline.check(phase)


def remaining_timeout(phase: str | None = None) -> float:
    deadline = current_deadline()
    if deadline is None:
        raise RuntimeError("A timed operation must own its deadline.")
    return deadline.remaining(phase)


@contextmanager
def operation_timeout(timeout: Any, *, session_id: str | None = None) -> Iterator[Deadline]:
    started = time.monotonic()
    try:
        duration = validate_timeout(timeout)
    except DomainError as exc:
        if session_id is not None:
            exc.context.setdefault("session_id", session_id)
        raise
    deadline = current_deadline()
    if deadline is not None:
        deadline.expires_at = min(deadline.expires_at, started + duration)
        if deadline.session_id is None:
            deadline.session_id = session_id
        deadline.check()
        yield deadline
        deadline.check()
        return
    deadline = Deadline(started + duration, session_id=session_id)
    token = _CURRENT.set(deadline)
    try:
        deadline.check()
        yield deadline
        deadline.execution_outcome = "completed"
        deadline.check()
    except DomainError as exc:
        if deadline.session_id is not None:
            exc.context.setdefault("session_id", deadline.session_id)
        if deadline.request_id is not None:
            exc.context.setdefault("request_id", deadline.request_id)
        raise
    except Exception as exc:
        if deadline.execution_outcome == "completed":
            failure = error_payload(exc)["error"]
            raise DomainError(
                failure["code"],
                failure["message"],
                phase=deadline.phase,
                execution_outcome="completed",
                session_id=deadline.session_id,
                request_id=deadline.request_id,
                command_completed=True if deadline.request_id is not None else None,
                command_request_id=deadline.request_id,
            ) from exc
        raise
    finally:
        _CURRENT.reset(token)


@contextmanager
def suspend_deadline() -> Iterator[None]:
    """Allow independently bounded recovery/ownership cleanup after caller expiry."""
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def timed(
    default: float = 20.0, *, argument: str = "timeout"
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
            session = kwargs.get("session", kwargs.get("session_id"))
            with operation_timeout(
                kwargs.get(argument, default),
                session_id=session if isinstance(session, str) else None,
            ):
                return function(*args, **kwargs)

        return invoke

    return decorate
