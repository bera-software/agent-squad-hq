"""Deterministic choice of exactly ONE implementer for an ``agent:implement``
dispatch.

Specialist engineer skills (``skills/<id>/skill.yml``) declare an
``agent:implement`` trigger whose filter starts with ``specialist route:`` —
they *replace* the generic Engineer for that run. When a ticket carries hints
for several specialists, two agents must never both start on it, so the
choice is made here, in code, in a fixed priority order (most
risk-sensitive first):

    database-migration-engineer > iac-devops-engineer > data-engineer >
    ml-engineer > integration-engineer > frontend-engineer > engineer

A specialist is eligible only when its skill is enabled AND declares the
specialist route AND the ticket matches its rule below (labels, or file paths
named in the ticket body). Anything else — no hint, specialist disabled,
catalogue unreadable — is the default ``engineer``.

CLI (for the router's implement job)::

    python -m squadhq.policy.implementer --labels "agent:implement,dept:frontend" [--body-file F]
                                         [--enabled frontend-engineer,data-engineer]

prints the chosen skill id on stdout.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

from squadhq.policy import risk

DEFAULT = "engineer"
SPECIALIST_MARK = "specialist route"

_PATH_RE = re.compile(r"(?<![\w@])(?:\.{0,2}/)?[\w.-]+(?:/[\w.*-]+)+|\b[\w.-]+\.(?:tf|tfvars|sql|hcl|bicep)\b")


@dataclass(frozen=True)
class Rule:
    skill: str
    labels: frozenset[str]
    globs: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule("database-migration-engineer", frozenset({"dept:db", "dept:database"}),
         tuple(g for g in risk.CORE_GLOBS["schema migration"])),
    Rule("iac-devops-engineer", frozenset({"dept:infra", "dept:devops"}),
         ("**/*.tf", "**/*.tfvars", "**/terraform/**", "**/terragrunt/**", "**/pulumi/**", "**/pulumi*.yaml",
          "**/helm/**", "**/charts/**")),
    Rule("data-engineer", frozenset({"dept:data"}),
         ("**/dbt/**", "**/dbt_project.yml", "**/airflow/**", "**/dags/**")),
    Rule("ml-engineer", frozenset({"dept:ml"}), ()),
    Rule("integration-engineer", frozenset({"dept:integrations"}), ()),
    Rule("frontend-engineer", frozenset({"dept:frontend"}), ()),
)


def paths_in(text: str | None) -> list[str]:
    """File-path-looking tokens in a ticket body (``db/migrations/V3.sql``)."""
    return [m.group(0).strip("`'\"()[],.") for m in _PATH_RE.finditer(text or "")]


def matches(rule: Rule, labels: Collection[str], body: str | None) -> bool:
    if rule.labels & set(labels):
        return True
    return any(risk.matches(p, g) for p in paths_in(body) for g in rule.globs)


def choose(labels: Iterable[str], body: str | None, enabled: Collection[str]) -> str:
    """The single implementer skill id for this ticket."""
    labels = set(labels)
    for rule in RULES:
        if rule.skill in enabled and matches(rule, labels, body):
            return rule.skill
    return DEFAULT


def enabled_specialists(loader: Callable[[], dict[str, object]] | None = None) -> set[str]:
    """Specialist skills that are enabled (manifest ``default_enabled``, or
    the hub's ``squadhq.yml`` switches) and declare the specialist route.
    Any failure reading the catalogue means "no specialists"."""
    try:
        skills = loader() if loader else _default_loader()
    except Exception:
        return set()
    out = set()
    for sid, meta in skills.items():
        if not isinstance(meta, dict) or not meta.get("enabled"):
            continue
        for t in meta.get("trigger") or []:
            if (isinstance(t, dict) and t.get("label") == "agent:implement"
                    and str(t.get("filter", "")).startswith(SPECIALIST_MARK)):
                out.add(sid)
    return out


def _default_loader() -> dict[str, object]:  # pragma: no cover - exercised via the skills package
    import glob
    import os

    import yaml

    from squadhq.paths import hub_path

    switches: dict[str, object] = {}
    cfg = hub_path("squadhq.yml")
    if os.path.exists(cfg):
        with open(cfg) as fh:
            data = yaml.safe_load(fh) or {}
        for section in ("agents", "skills"):
            switches.update({str(k).replace("_", "-"): v for k, v in (data.get(section) or {}).items()})
    out: dict[str, object] = {}
    for f in glob.glob(hub_path("skills", "*", "skill.yml")):
        with open(f) as fh:
            m = yaml.safe_load(fh) or {}
        sid = str(m.get("id") or "")
        sw = switches.get(sid)
        if isinstance(sw, dict):
            sw = sw.get("enabled")
        enabled = sw if isinstance(sw, bool) else bool(m.get("default_enabled"))
        out[sid] = {"enabled": enabled, "trigger": m.get("trigger")}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m squadhq.policy.implementer")
    ap.add_argument("--labels", default="", help="comma-separated ticket labels")
    ap.add_argument("--body-file", help="file holding the ticket body ('-' = stdin)")
    ap.add_argument("--enabled", help="comma-separated enabled specialist ids (default: read the skill catalogue)")
    args = ap.parse_args(argv)
    body = ""
    if args.body_file == "-":
        body = sys.stdin.read()
    elif args.body_file:
        with open(args.body_file) as fh:
            body = fh.read()
    enabled = ({e.strip() for e in args.enabled.split(",") if e.strip()} if args.enabled is not None
               else enabled_specialists())
    print(choose([x.strip() for x in args.labels.split(",") if x.strip()], body, enabled))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
