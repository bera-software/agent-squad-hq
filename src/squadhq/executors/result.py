"""JSON (de)serialisation of :class:`squadhq.interfaces.ExecutionResult`.

``execution-result.json`` is a flat object holding every ``ExecutionResult``
field plus ``schema_version`` (currently ``"1"``). ``error_class`` is written
as its string value. Unknown keys found on read are kept in ``extra``.
"""
from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, cast

from squadhq.interfaces import ErrorClass, ExecutionResult

SCHEMA_VERSION = "1"

_FIELDS = tuple(f.name for f in dataclasses.fields(ExecutionResult))
_OUTCOMES = ("success", "failure", "skipped")


def _json_safe(v: Any) -> Any:
    if isinstance(v, ErrorClass):
        return v.value
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def to_dict(result: ExecutionResult) -> dict[str, Any]:
    """Return a JSON-safe dict (enum values as strings, ``schema_version`` added)."""
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for name in _FIELDS:
        out[name] = _json_safe(getattr(result, name))
    return out


def _opt_float(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _opt_int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def from_dict(data: dict[str, Any]) -> ExecutionResult:
    """Inverse of :func:`to_dict`; tolerant of missing/unknown keys."""
    try:
        error_class = ErrorClass(str(data.get("error_class", "none")))
    except ValueError:
        error_class = ErrorClass.UNKNOWN
    outcome = data.get("outcome")
    if outcome not in _OUTCOMES:
        outcome = "failure"
    extra = dict(data.get("extra") or {})
    for k, v in data.items():
        if k not in _FIELDS and k != "schema_version":
            extra.setdefault(k, v)
    transcript = data.get("transcript_path")
    return ExecutionResult(
        executor=str(data.get("executor", "")),
        role=str(data.get("role", "")),
        model=str(data.get("model", "")),
        outcome=cast(Any, outcome),
        error_class=error_class,
        error_message=str(data.get("error_message", "")),
        cost_usd=_opt_float(data.get("cost_usd")),
        input_tokens=_opt_int(data.get("input_tokens")),
        output_tokens=_opt_int(data.get("output_tokens")),
        turns=_opt_int(data.get("turns")),
        duration_s=_opt_float(data.get("duration_s")),
        capacity_tripped=bool(data.get("capacity_tripped", False)),
        transcript_path=str(transcript) if transcript else None,
        extra=extra,
        account=str(data.get("account") or ""),
    )


def write_result(result: ExecutionResult, path: str | os.PathLike[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_dict(result), f, indent=2, sort_keys=True)
        f.write("\n")


def read_result(path: str | os.PathLike[str]) -> ExecutionResult:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: execution result must be a JSON object")
    return from_dict(data)
