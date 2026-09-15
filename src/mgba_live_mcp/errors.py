"""Domain-owned failures shared by the manager, CLI and MCP adapters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

ExecutionOutcome = Literal["not_started", "partial", "unknown"]

# Authoritative inventory, also rendered in the generated MCP reference.
ERROR_CODES = {
    "unknown_tool": "The requested tool is not in the catalog; nothing was invoked.",
    "invalid_arguments": "Arguments are malformed or violate an operation's input rules.",
    "session_required": "An explicit session (or supported PID/all selector) is required.",
    "session_not_found": "The requested managed session does not exist.",
    "session_dead": "The managed process/group has exited.",
    "session_exists": "The requested session directory is already reserved.",
    "session_busy": "Another operation or unresolved request owns the session.",
    "session_stopping": "Recovery has fenced the session while termination is unresolved.",
    "session_stopped": "Recovery has retired this session generation.",
    "session_generation_changed": "The session directory or ownership generation changed.",
    "session_state_corrupt": "The transaction journal cannot establish safe ownership.",
    "identity_unverified": "Native process ownership could not be established.",
    "identity_mismatch": "The process identity is not the managed process/group.",
    "permission_denied": "OS permissions prevent the requested operation or inspection.",
    "termination_unconfirmed": "Process/group exit could not be confirmed.",
    "bridge_error": "The bridge reported a command error; side effects may have occurred.",
    "serialization_failed": (
        "Lua response could not be serialized as JSON; inspect command_completed before retrying."
    ),
    "command_timeout": "No correlated bridge response arrived; execution remains unknown.",
    "settle_failed": "The command ran, but settling could not be confirmed.",
    "snapshot_failed": "Required visual content is unavailable after the requested operation.",
    "startup_failed": "Startup/readiness failed; inspect the retained session before retrying.",
    "resource_not_found": "A requested ROM, Lua file, bridge script or executable is unavailable.",
    "io_error": "A filesystem or OS operation failed.",
    "invalid_result": "An operation returned content inconsistent with its success contract.",
    "internal_error": "An unexpected implementation failure occurred; outcome is not established.",
}


class DomainError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        execution_outcome: ExecutionOutcome = "unknown",
        **context: Any,
    ) -> None:
        self.code = code
        self.message = message
        self.phase = phase
        self.execution_outcome = execution_outcome
        self.context = {key: value for key, value in context.items() if value is not None}
        super().__init__(f"{code}: {message}")

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class CommandTimeout(DomainError, TimeoutError):
    """A published bridge request timed out, not a confirmed cancellation."""


@contextmanager
def error_context(phase: str, **context: Any) -> Iterator[None]:
    """Keep known domain context even when an OS/journal operation raises."""
    try:
        yield
    except Exception as exc:
        if isinstance(exc, DomainError):
            exc.context = {
                **{key: value for key, value in context.items() if value is not None},
                **exc.context,
            }
            raise
        code = (
            "permission_denied"
            if isinstance(exc, PermissionError)
            else "io_error"
            if isinstance(exc, OSError)
            else "internal_error"
        )
        raise DomainError(code, str(exc), phase=phase, **context) from exc


def error_payload(exc: Exception, **context: Any) -> dict[str, Any]:
    """Adapt without parsing exception text or guessing a domain failure's phase."""
    if not isinstance(exc, DomainError):
        if isinstance(exc, ValueError):
            exc = DomainError(
                "invalid_arguments", str(exc), phase="validation", execution_outcome="not_started"
            )
        elif isinstance(exc, PermissionError):
            exc = DomainError("permission_denied", str(exc), phase="io")
        elif isinstance(exc, OSError):
            exc = DomainError("io_error", str(exc), phase="io")
        else:
            exc = DomainError("internal_error", str(exc), phase="internal")
    return {
        "error": {
            **{key: value for key, value in context.items() if value is not None},
            **exc.context,
            "code": exc.code,
            "message": exc.message,
            "phase": exc.phase,
            "execution_outcome": exc.execution_outcome,
        }
    }
