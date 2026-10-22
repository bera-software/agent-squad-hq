"""Filesystem locations of the hub checkout.

The hub root is the directory holding ``config/``, ``prompts/``, ``agents/`` and
``.github/``. It resolves, in order:

1. ``SQUADHQ_HOME`` — set by the ``setup-hub`` composite action in workflows,
   and by the server image.
2. The source-tree root, when the package is imported from ``src/squadhq``
   (an editable install or ``PYTHONPATH=src``).
3. The current working directory.
"""
from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_SOURCE_ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))


def hub_root() -> str:
    env = os.environ.get("SQUADHQ_HOME")
    if env:
        return os.path.abspath(env)
    if os.path.isdir(os.path.join(_SOURCE_ROOT, "config")):
        return _SOURCE_ROOT
    return os.getcwd()


def hub_path(*parts: str) -> str:
    return os.path.join(hub_root(), *parts)
