"""Coding-agent executors: neutral ``ExecutionResult`` normalisers and tool profiles.

Registry::

    from squadhq.executors import get_normalizer, normalize_output
    result = normalize_output("claude-code", "/path/to/execution_file.json", role="implement")

See :mod:`squadhq.executors.normalize` and :mod:`squadhq.executors.tool_profiles`
for the CLIs used by the ``run-agent`` workflow steps. Submodules are loaded
lazily so ``python -m squadhq.executors.<module>`` does not import itself twice.
(``squadhq.executors.normalize`` is the submodule; the function is exported
here as ``normalize_output``.)
"""
from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

from squadhq.interfaces import ExecutionResult

if TYPE_CHECKING:  # pragma: no cover
    from squadhq.executors.normalize import (
        NORMALIZERS,
        Normalizer,
        canonical_executor,
        classify_error,
        detect_capacity,
        normalize_aider,
        normalize_claude_code,
        normalize_codex,
        normalize_gemini_cli,
    )
    from squadhq.executors.result import SCHEMA_VERSION, from_dict, read_result, to_dict, write_result
    from squadhq.executors.tool_profiles import (
        PROFILES,
        InvalidToolError,
        aider_flags,
        claude_code_allowed_tools,
        claude_code_disallowed_tools,
        codex_flags,
        gemini_cli_flags,
        validate_extra,
    )

_LAZY = {
    "squadhq.executors.normalize": (
        "NORMALIZERS", "Normalizer", "canonical_executor", "classify_error", "detect_capacity",
        "normalize_aider", "normalize_claude_code", "normalize_codex", "normalize_gemini_cli",
    ),
    "squadhq.executors.result": ("SCHEMA_VERSION", "from_dict", "read_result", "to_dict", "write_result"),
    "squadhq.executors.tool_profiles": (
        "PROFILES", "InvalidToolError", "aider_flags", "claude_code_allowed_tools",
        "claude_code_disallowed_tools", "codex_flags", "gemini_cli_flags", "validate_extra",
    ),
}
_WHERE = {name: mod for mod, names in _LAZY.items() for name in names}


def __getattr__(name: str) -> Any:
    mod = _WHERE.get(name)
    if mod is None:
        raise AttributeError(f"module 'squadhq.executors' has no attribute {name!r}")
    return getattr(importlib.import_module(mod), name)


def get_normalizer(executor: str) -> Normalizer:
    """Return the normaliser for ``executor`` (aliases accepted)."""
    from squadhq.executors.normalize import NORMALIZERS, canonical_executor

    return NORMALIZERS[canonical_executor(executor)]


def normalize_output(executor: str, native_path: str | os.PathLike[str], *, role: str = "", model: str = "",
                     duration_s: float | None = None) -> ExecutionResult:
    """:func:`squadhq.executors.normalize.normalize` (never raises on bad input)."""
    from squadhq.executors.normalize import normalize

    return normalize(executor, native_path, role=role, model=model, duration_s=duration_s)


def list_executors() -> list[str]:
    from squadhq.executors.normalize import NORMALIZERS

    return sorted(NORMALIZERS)


__all__ = [
    "NORMALIZERS", "PROFILES", "SCHEMA_VERSION", "InvalidToolError", "Normalizer",
    "aider_flags", "canonical_executor", "claude_code_allowed_tools", "claude_code_disallowed_tools",
    "classify_error", "codex_flags", "detect_capacity", "from_dict", "gemini_cli_flags", "get_normalizer",
    "list_executors", "normalize_aider", "normalize_output", "normalize_claude_code", "normalize_codex",
    "normalize_gemini_cli", "read_result", "to_dict", "validate_extra", "write_result",
]
