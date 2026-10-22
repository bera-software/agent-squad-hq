"""Resolve which skills are enabled for a ``squadhq.yml``.

Precedence (later wins): the manifest's ``default_enabled``; the top-level
``agents:`` / ``skills:`` maps; the selected project's ``agents:`` / ``skills:``
maps. Keys may be the skill id (``threat-modeler``) or its snake_case form
(``threat_modeler``). Values are ``true``/``false`` or ``{enabled: bool}``.
If ``edition:`` is set (``core`` | ``pro`` | ``enterprise``), skills from a
higher tier are unavailable whatever the switches say.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from squadhq.skills.loader import Skill
from squadhq.skills.schema import TIERS


@dataclass
class Resolution:
    skill: Skill
    enabled: bool
    reason: str
    missing_requires: list[str] = field(default_factory=list)


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: config must be a mapping")
    return data


def _switch(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    return None


def _lookup(section: Any, skill_id: str) -> bool | None:
    if not isinstance(section, dict):
        return None
    for key in (skill_id, skill_id.replace("-", "_")):
        if key in section:
            found = _switch(section[key])
            if found is not None:
                return found
    return None


def resolve(skills: dict[str, Skill], config: dict[str, Any], project: str | None = None) -> dict[str, Resolution]:
    edition = config.get("edition")
    if edition is not None and edition not in TIERS:
        raise ValueError(f"edition must be one of {', '.join(TIERS)} (got {edition!r})")
    proj_cfg: dict[str, Any] = {}
    if project:
        for p in config.get("projects") or []:
            if isinstance(p, dict) and project in (p.get("name"), p.get("repo")):
                proj_cfg = p
                break
        else:
            raise ValueError(f"project {project!r} not found in config")

    out: dict[str, Resolution] = {}
    for sid, skill in sorted(skills.items()):
        enabled, reason = skill.default_enabled, "default"
        for where, section in (("agents", config.get("agents")), ("skills", config.get("skills")),
                               ("project.agents", proj_cfg.get("agents")),
                               ("project.skills", proj_cfg.get("skills"))):
            found = _lookup(section, sid)
            if found is not None:
                enabled, reason = found, where
        if edition is not None and TIERS.index(skill.tier) > TIERS.index(edition):
            enabled, reason = False, f"needs {skill.tier} edition"
        out[sid] = Resolution(skill=skill, enabled=enabled, reason=reason)
    for res in out.values():
        if res.enabled:
            res.missing_requires = [r for r in res.skill.requires if not (r in out and out[r].enabled)]
    return out
