"""Shared helpers for tracker tests (fixtures are recorded-shape API bodies)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def fx(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
