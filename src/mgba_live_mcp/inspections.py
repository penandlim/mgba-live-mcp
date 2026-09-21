"""Fixed inspection budgets and validation shared by CLI/MCP manager calls."""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn

from .errors import DomainError

# Keep the packaged Lua bridge in sync. Together these also bound JSON expansion
# below its existing 10,000-entry limit, including worst-case entity/delta output.
MAX_READ_BYTES = 4096
MAX_ITEMS = 1024
MAX_RESPONSE_BYTES = 32_768
MAX_ADDRESS = 0xFFFFFFFF
INSPECTION_ERROR_CODES = frozenset(
    {"invalid_arguments", "inspection_limit", "inspection_unsupported", "inspection_read_failed"}
)


def _invalid(message: str) -> NoReturn:
    raise DomainError(
        "invalid_arguments", message, phase="validation", execution_outcome="not_executed"
    )


def _positive(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        _invalid(f"{name} must be a positive integer.")
    return value


def _address(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = int(value, 0)
        except ValueError:
            _invalid("Addresses must be unsigned 32-bit integers (hex or decimal).")
    if type(value) is not int or not 0 <= value <= MAX_ADDRESS:
        _invalid("Addresses must be unsigned 32-bit integers (hex or decimal).")
    return value


def _limit(value: int, maximum: int, name: str) -> None:
    if value > maximum:
        raise DomainError(
            "inspection_limit",
            f"Inspection exceeds the {name} limit ({maximum}); request a smaller range or "
            "split it into explicitly addressed chunks, which may observe different frames.",
            phase="validation",
            execution_outcome="not_executed",
            limit_name=name,
            limit=maximum,
            max_read_bytes=MAX_READ_BYTES,
            max_items=MAX_ITEMS,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )


def _response_limit(payload_bytes: int, session_id: str) -> None:
    if not isinstance(session_id, str):
        _invalid("session must be a string.")
    _limit(len(session_id), MAX_RESPONSE_BYTES, "response_bytes")
    try:
        metadata = json.dumps(
            {"id": "0" * 32, "session_id": session_id},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeError:
        _invalid("session must be valid UTF-8.")
    # Match the bridge's actual escaped IDs; reserve the remaining shape headers.
    _limit(max(2048, 256 + len(metadata)) + payload_bytes, MAX_RESPONSE_BYTES, "response_bytes")


def validate_inspection(kind: str, payload: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    """Normalize a fresh command payload before session acquisition or publication.

    The bridge independently enforces these limits and its actual platform's bus
    bounds. Host metadata does not identify a trustworthy emulated platform yet.
    """
    if not isinstance(payload, dict):
        _invalid("Inspection arguments must be an object.")
    if kind == "read_memory":
        addresses = payload.get("addresses")
        if not isinstance(addresses, list) or not addresses:
            _invalid("addresses must be a non-empty array of bus addresses.")
        _limit(len(addresses), MAX_ITEMS, "items")
        payload["addresses"] = [_address(address) for address in addresses]
        _response_limit(17 * len(addresses), session_id)
        return payload

    if kind == "read_range":
        start_key = "start"
        length = _positive(payload.get("length"), "length")
        _limit(length, MAX_READ_BYTES, "read_bytes")
        response_bytes = 4 * length
    elif kind in ("dump_pointers", "dump_entities"):
        start_key = "start" if kind == "dump_pointers" else "base"
        size_key = "width" if kind == "dump_pointers" else "size"
        size = _positive(payload.get(size_key, 4 if kind == "dump_pointers" else 24), size_key)
        if kind == "dump_pointers" and size > 6:
            _invalid("Pointer width must be an integer from 1 to 6 bytes.")
        _limit(size, MAX_READ_BYTES, "read_bytes")
        count = _positive(payload.get("count", 10 if kind == "dump_entities" else None), "count")
        _limit(count, MAX_ITEMS, "items")
        length = count * size
        _limit(length, MAX_READ_BYTES, "read_bytes")
        response_bytes = 60 * count if kind == "dump_pointers" else 48 * count + 4 * length
    else:
        raise ValueError(f"Unknown inspection kind: {kind}")

    start = payload[start_key] = _address(
        payload.get(start_key, 0xC200 if kind == "dump_entities" else None)
    )
    if length - 1 > MAX_ADDRESS - start:
        _invalid("Inspection end address exceeds the unsigned 32-bit bus; choose a smaller range.")

    if kind == "read_range":
        encoding = payload.get("encoding", "bytes")
        if encoding not in ("bytes", "hex", "delta"):
            _invalid("encoding must be bytes, hex, or delta.")
        baseline = payload.get("baseline")
        if encoding == "delta":
            if not isinstance(baseline, dict) or baseline.keys() != {"start", "data"}:
                _invalid(
                    "delta requires a baseline object with exactly start and hexadecimal data."
                )
            baseline_start = _address(baseline["start"])
            data = baseline["data"]
            if (
                baseline_start != start
                or not isinstance(data, str)
                or len(data) != 2 * length
                or re.fullmatch("[0-9a-fA-F]+", data) is None
            ):
                _invalid(
                    "baseline must contain exactly the requested region's start and hex bytes."
                )
            payload["baseline"] = {"start": baseline_start, "data": data}
            # At most ceil(length/2) spans; each costs 26 bytes plus its hex data.
            response_bytes = 26 * ((length + 1) // 2) + 2 * length
        elif baseline is not None:
            _invalid("baseline is only accepted with encoding=delta.")
        elif encoding == "hex":
            response_bytes = 2 * length
    _response_limit(response_bytes, session_id)
    return payload
