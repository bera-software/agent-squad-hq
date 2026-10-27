"""Hub upgrades: the files a hub should hold for this package version, and the
changes that take an existing hub there.

A hub is a copy of the template plus its owner's ``squadhq.yml``. An upgrade:

* replaces the engine files (the template items and ``src/squadhq``) with this
  version's copies;
* renders ``config/*.json`` and the workflow schedule lines again from the
  hub's own ``squadhq.yml``, so its settings carry over;
* never touches ``squadhq.yml``, ``README.md``, ``.gitignore`` or any file
  outside the template, except that it removes the project's own CI and release
  workflows (``scaffold.PROJECT_ONLY``) that a hub made from the GitHub template carries;
* keeps, and reports, an engine file the owner changed since the last managed
  upgrade. :data:`MANIFEST_PATH` records the git blob of every file an upgrade
  wrote, which is how a local change is recognised. A hub without the manifest
  (created from the GitHub template, before its first managed upgrade) has its
  engine files replaced; the pull request diff shows any local change undone.

Nothing here talks to GitHub: callers pass the hub's current blobs (path to
git blob SHA, as a recursive tree listing gives them) and apply the result.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from squadhq import __version__
from squadhq.config import loader, render, scaffold

__all__ = ["MANIFEST_PATH", "OWNER_FILES", "Change", "HubFiles", "UpgradeError", "desired", "git_blob_sha",
           "manifest_bytes", "parse_manifest", "plan"]

MANIFEST_PATH = ".github/squadhq-manifest.json"
# Written once by scaffolding (or by the owner) and never replaced by an upgrade.
OWNER_FILES = frozenset({"squadhq.yml", "README.md", ".gitignore"})
_JUNK = frozenset({".DS_Store"})


class UpgradeError(ValueError):
    """The hub's ``squadhq.yml`` cannot be loaded, so the hub cannot be rendered."""


def git_blob_sha(data: bytes) -> str:
    """The SHA git (and the GitHub trees API) gives a blob with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class HubFiles:
    """Every managed file of a hub at :attr:`version`, rendered from one ``squadhq.yml``."""
    version: str
    files: dict[str, bytes]
    rendered: frozenset[str]  # paths whose content comes from squadhq.yml: always rewritten


@dataclass
class Change:
    writes: dict[str, bytes] = field(default_factory=dict)  # path -> new content (includes the manifest)
    deletes: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # changed locally: left as the owner has them
    first_managed: bool = False  # no manifest yet: engine files were replaced without a local-change check

    @property
    def empty(self) -> bool:
        return not self.deletes and set(self.writes) <= {MANIFEST_PATH}


def desired(config_text: str, *, ref: date | None = None) -> HubFiles:
    """Scaffold this version's template, render it with ``config_text`` and return its managed files."""
    with tempfile.TemporaryDirectory(prefix="squadhq-upgrade-") as root:
        scaffold.scaffold(root, git=False)
        with open(os.path.join(root, "squadhq.yml"), "w", encoding="utf-8") as f:
            f.write(config_text)
        cfg, issues = loader.load_text(config_text, "squadhq.yml", **loader.hub_context(root))
        if cfg is None:
            errors = [str(i) for i in issues if i.level == "error"]
            raise UpgradeError("; ".join(errors)[:1000] or "squadhq.yml is not valid")
        try:
            rp = render.plan(cfg, root, ref)
        except ValueError as e:
            raise UpgradeError(str(e)) from e
        render.write(rp)
        files: dict[str, bytes] = {}
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".git"))
            for name in names:
                if name in _JUNK or name.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                if rel in OWNER_FILES:
                    continue
                with open(os.path.join(dirpath, name), "rb") as fb:
                    files[rel] = fb.read()
    return HubFiles(__version__, files, frozenset(f.path for f in rp.files))


def parse_manifest(text: str | bytes | None) -> dict[str, str] | None:
    """``path -> blob SHA`` from a manifest; None when it is missing or unreadable."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    blobs = data.get("files") if isinstance(data, dict) else None
    if not isinstance(blobs, dict):
        return None
    return {str(k): str(v) for k, v in blobs.items() if isinstance(v, str)}


def manifest_bytes(hub: HubFiles) -> bytes:
    blobs = {p: git_blob_sha(b) for p, b in sorted(hub.files.items())}
    return (json.dumps({"version": hub.version, "files": blobs}, indent=2) + "\n").encode()


def plan(current: Mapping[str, str], hub: HubFiles, manifest: Mapping[str, str] | None) -> Change:
    """The change that takes a hub whose files have the blob SHAs ``current`` to ``hub``.

    ``manifest`` is the hub's parsed :data:`MANIFEST_PATH` (None when absent).
    """
    ch = Change(first_managed=manifest is None)
    for path, data in sorted(hub.files.items()):
        new, have = git_blob_sha(data), current.get(path)
        if have == new:
            continue
        locally_changed = (manifest is not None and have is not None and path in manifest
                           and have != manifest[path])
        if locally_changed and path not in hub.rendered:
            ch.kept.append(path)
            continue
        ch.writes[path] = data
    for path, recorded in sorted((manifest or {}).items()):
        if path in hub.files or path in OWNER_FILES or path not in current:
            continue
        if current[path] == recorded:
            ch.deletes.append(path)  # removed from the template and untouched locally
        else:
            ch.kept.append(path)
    # The project's own CI and release workflows: a hub created from the GitHub template carries
    # them, a scaffolded one never does. They do not belong in a hub.
    for path in sorted(p.replace(os.sep, "/") for p in scaffold.PROJECT_ONLY):
        if path in current and path not in hub.files and path not in ch.deletes:
            ch.deletes.append(path)
    if not ch.empty or current.get(MANIFEST_PATH) != git_blob_sha(manifest_bytes(hub)):
        ch.writes[MANIFEST_PATH] = manifest_bytes(hub)
    return ch
