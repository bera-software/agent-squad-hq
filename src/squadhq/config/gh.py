"""The single seam through which the CLI talks to GitHub: the ``gh`` CLI.

Every call is a subprocess with a list of arguments (never a shell string)
and a timeout. Tests replace the runner with :class:`FakeGh`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout or "null")
        except json.JSONDecodeError:
            return None


class Gh:
    """Real ``gh`` runner."""

    def __init__(self, binary: str = "gh", timeout: float = 60.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> Result:
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, text=True, input=input_text,
                               timeout=timeout or self.timeout, check=False)
        except FileNotFoundError:
            return Result(127, "", f"{self.binary}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{self.binary} {' '.join(args[:3])}: timed out after {timeout or self.timeout}s")
        return Result(p.returncode, p.stdout, p.stderr)

    def authenticated(self) -> bool:
        return self.available() and self.run(["auth", "status"], timeout=20).ok


Handler = Callable[[list[str], str | None], Result]


@dataclass
class FakeGh(Gh):
    """Scripted runner for tests: ``routes`` maps an argument prefix (tuple)
    to a :class:`Result` or a callable. The longest matching prefix wins;
    unmatched calls return exit 1. Every call is recorded in ``calls``."""

    routes: dict[tuple[str, ...], Result | Handler] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)
    present: bool = True

    def __post_init__(self) -> None:
        super().__init__()

    def available(self) -> bool:
        return self.present

    def run(self, args: list[str], *, input_text: str | None = None, timeout: float | None = None) -> Result:
        self.calls.append(list(args))
        self.inputs.append(input_text)
        best: tuple[str, ...] | None = None
        for prefix in self.routes:
            if tuple(args[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            return Result(1, "", f"FakeGh: no route for {args}")
        r = self.routes[best]
        return r(args, input_text) if callable(r) else r
