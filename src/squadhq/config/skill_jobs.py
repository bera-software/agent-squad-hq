"""Job names contributed by the skill catalogue (``skills/*/skill.yml``).

Every skill with ``model_tier: heavy|light`` is a job: its ``id`` (the
``squadhq_job`` value ``squadhq-agent.yml`` receives) and, when set, its
``role`` both map to that tier. Deterministic skills (``model_tier: none``)
never call a model and are skipped. If two skills share a role with
different tiers, heavy wins.

Read with plain YAML (not :mod:`squadhq.skills`) so the config layer stays
independent of the skills package; malformed manifests are ignored here and
reported by ``squadhq skills validate``.
"""
from __future__ import annotations

import os

import yaml

TIERS = ("heavy", "light")


def skill_job_tiers(hub_root: str) -> dict[str, str] | None:
    """``{job: tier}`` sorted by job name, or None when there is no skills/ dir."""
    base = os.path.join(hub_root, "skills")
    if not os.path.isdir(base):
        return None
    out: dict[str, str] = {}
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name, "skill.yml")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or data.get("model_tier") not in TIERS:
            continue
        tier = str(data["model_tier"])
        for job in (data.get("id"), data.get("role")):
            if isinstance(job, str) and job:
                if out.get(job) != "heavy":
                    out[job] = tier
    return dict(sorted(out.items()))
