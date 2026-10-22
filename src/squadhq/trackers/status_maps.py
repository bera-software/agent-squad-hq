"""Flow-state labels <-> tracker-native workflow states.

The flow engine (``config/flows.yml`` ``label_states.issue``) reasons in
labels. Trackers with native workflow states express some of those labels as
a status instead. Defaults (``status_maps.yml``, matched case-insensitively):

========================  =====================  ===========  ===============
flow label                Jira status            Linear state  Trello list
========================  =====================  ===========  ===============
``proposal:pending``      (label)                Triage        (label)
``triaged:ready``         Selected for Dev.      Todo          Ready
``status:in-progress``    In Progress            In Progress   In Progress/Doing
``status:in-review``      In Review/Code Review  In Review     In Review/Review
``needs-human``           Blocked                Blocked       Blocked
(closed, completed)       Done                   Done          Done (archived)
(closed, not planned)     Won't Do               Canceled      Done (archived)
========================  =====================  ===========  ===============

GitHub and GitLab carry flow labels natively: their maps are empty and
every label stays a label. Any flow label without a native state mapping
stays a plain tracker label/tag on every tracker.

``overrides`` (same shape as one tracker's YAML entry) are merged over the
defaults: ``{"states": {...}, "label_to_state": {...}, "open": ..., "done": ...}``.
"""
from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from importlib import resources
from typing import Any

import yaml

StatusMap = dict[str, Any]


@functools.lru_cache(maxsize=1)
def _defaults() -> dict[str, StatusMap]:
    text = resources.files("squadhq.trackers").joinpath("status_maps.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {str(k): dict(v or {}) for k, v in data.items()}


def status_map(tracker: str, overrides: Mapping[str, Any] | None = None) -> StatusMap:
    """The effective map for ``tracker`` (defaults merged with ``overrides``)."""
    base = dict(_defaults().get(tracker, {}))
    states: dict[str, list[str]] = {str(k): list(v or []) for k, v in (base.get("states") or {}).items()}
    rev: dict[str, str] = {str(k): str(v) for k, v in (base.get("label_to_state") or {}).items()}
    if overrides:
        states.update({str(k): list(v or []) for k, v in (overrides.get("states") or {}).items()})
        rev.update({str(k): str(v) for k, v in (overrides.get("label_to_state") or {}).items()})
        for key in ("open", "done", "not_planned"):
            if overrides.get(key):
                base[key] = overrides[key]
    base["states"] = states
    base["label_to_state"] = rev
    return base


def native_to_labels(tracker: str, native_state: str | None, overrides: Mapping[str, Any] | None = None) -> list[str]:
    """Flow labels implied by a native state (``[]`` when unmapped)."""
    if not native_state:
        return []
    low = native_state.strip().lower()
    for name, labels in status_map(tracker, overrides)["states"].items():
        if name.lower() == low:
            return list(labels)
    return []


def label_to_native(tracker: str, label: str, overrides: Mapping[str, Any] | None = None) -> str | None:
    """Preferred native state for a flow label, or ``None`` if it stays a plain label."""
    smap = status_map(tracker, overrides)
    if label in smap["label_to_state"]:
        return str(smap["label_to_state"][label])
    for name, labels in smap["states"].items():
        if label in labels:
            return str(name)
    return None


def candidate_states(tracker: str, label: str, overrides: Mapping[str, Any] | None = None) -> list[str]:
    """Every native state that expresses ``label``, preferred first (for boards whose names vary)."""
    smap = status_map(tracker, overrides)
    out: list[str] = []
    pref = smap["label_to_state"].get(label)
    if pref:
        out.append(str(pref))
    for name, labels in smap["states"].items():
        if label in labels and name not in out:
            out.append(name)
    return out


def is_state_label(tracker: str, label: str, overrides: Mapping[str, Any] | None = None) -> bool:
    return label_to_native(tracker, label, overrides) is not None


def split_labels(
    tracker: str, labels: Iterable[str], overrides: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Split into ``(state_labels, plain_labels)`` for this tracker."""
    state: list[str] = []
    plain: list[str] = []
    for lb in labels:
        (state if is_state_label(tracker, lb, overrides) else plain).append(lb)
    return state, plain


def special_state(tracker: str, which: str, overrides: Mapping[str, Any] | None = None) -> str | None:
    """``which`` is ``open`` / ``done`` / ``not_planned``."""
    v = status_map(tracker, overrides).get(which)
    return str(v) if v else None


def match_name(candidates: Iterable[str], available: Iterable[str]) -> str | None:
    """First candidate present in ``available`` (case-insensitive); returns the available spelling."""
    avail = {a.lower(): a for a in available}
    for c in candidates:
        hit = avail.get(c.lower())
        if hit is not None:
            return hit
    return None
