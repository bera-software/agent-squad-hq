"""Turn ``squadhq.yml`` ``schedules:`` into timezone-aware cron entries and queued jobs.

Schedule phrases are parsed by the config package's grammar
(``squadhq.config.schedules.parse``: ``"weekdays 07:30"``,
``"every 30m during working_hours"``, ``"hourly :15"``, ``"monthly 1st 05:00"``,
``"quarterly 1st 07:00"``, raw cron …) whenever it is importable. Unlike
Actions mode, which must pick a single UTC offset, the server evaluates the
resulting *local* wall times in ``org.timezone`` with real DST handling.

A small built-in translator (:func:`translate`) covers the common phrases
when ``squadhq.config`` is unavailable.
"""
from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from squadhq.scheduler.cron import CronError, CronExpr, from_parts, parse

__all__ = ["Schedule", "ScheduleConfig", "translate", "parse_spec", "build", "job_for", "load_schedule_config",
           "HUB_LOOP_KEYS"]

# schedule key -> agent_runner mode (hub loops: one prompt, one reply)
HUB_LOOP_KEYS: dict[str, str] = {
    "chief_of_staff": "cos", "cos": "cos",
    "daily_brief": "brief", "brief": "brief",
    "finance": "finance", "finance_weekly": "finance",
    "growth": "growth", "growth_nightly": "growth",
    "seo": "seo", "seo_weekly": "seo",
    "legal": "legal", "legal_weekly": "legal",
    "weekly_rollup": "rollup", "rollup": "rollup",
}
SWEEP_KEYS = {"stuck_sweeper", "sweep", "sweeper"}
JANITOR_KEYS = {"hub_janitor", "janitor"}

# used only when squadhq.config (which carries the canonical defaults) is unavailable
FALLBACK_DEFAULTS: dict[str, str] = {
    "stuck_sweeper": "*/15 * * * *",
    "hub_janitor": "45 6 * * *",
}

_DAYSETS = {"weekdays": "1-5", "weekends": "0,6", "daily": "*", "everyday": "*"}
_DAYNAMES = {"sun", "mon", "tue", "wed", "thu", "fri", "sat"}
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hours?)(\s+during\s+working_hours)?$")


@dataclass(frozen=True)
class Schedule:
    key: str
    cron: CronExpr
    source: str
    tz: str | None = None  # overrides the org timezone (raw cron lines are UTC)


@dataclass
class ScheduleConfig:
    timezone: str = "UTC"
    working_hours: str = "00:00-24:00"
    working_days: str = "daily"
    schedules: list[Schedule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    hub_repo: str = ""
    budget_usd: float | None = None  # budgets.monthly_usd
    budget_by_project: dict[str, float] = field(default_factory=dict)  # repo -> USD, "default" expanded

    def tz_for(self, s: Schedule) -> str:
        return s.tz or self.timezone


def _hours_window(working_hours: str) -> str:
    m = re.match(r"^(\d{1,2}):\d{2}\s*-\s*(\d{1,2}):\d{2}$", working_hours.strip())
    if not m:
        raise CronError(f"bad working_hours {working_hours!r}")
    start, end = int(m.group(1)), min(int(m.group(2)), 24)
    if end <= start:
        raise CronError(f"working_hours end must be after start: {working_hours!r}")
    return f"{start}-{end - 1}"


def translate(value: str, working_hours: str = "00:00-24:00") -> str | None:
    """Built-in fallback: friendly phrase -> local cron line; ``None`` when disabled."""
    v = " ".join(str(value).strip().lower().split())
    if v in ("", "off", "false", "disabled", "none"):
        return None
    if v.startswith("@") or len(v.split()) == 5:
        parse(v)
        return v
    m = _EVERY.match(v)
    if m:
        n, unit, during = int(m.group(1)), m.group(2), m.group(3)
        hours = _hours_window(working_hours) if during else "*"
        if unit.startswith("m"):
            if not 1 <= n <= 59:
                raise CronError(f"minute interval must be 1-59: {value!r}")
            return f"*/{n} {hours} * * *"
        if not 1 <= n <= 23:
            raise CronError(f"hour interval must be 1-23: {value!r}")
        return f"0 {hours}/{n} * * *" if during else f"0 */{n} * * *"
    parts = v.split()
    hm = _HHMM.match(parts[1]) if len(parts) == 2 else None
    if hm:
        hh, mm = int(hm.group(1)), int(hm.group(2))
        if hh > 23 or mm > 59:
            raise CronError(f"bad time {parts[1]!r}")
        days_tok = parts[0]
        if days_tok in _DAYSETS:
            dow = _DAYSETS[days_tok]
        elif all(d in _DAYNAMES for d in re.split(r"[,-]", days_tok)):
            dow = days_tok
        else:
            raise CronError(f"bad day spec {days_tok!r}")
        line = f"{mm} {hh} * * {dow}"
        parse(line)
        return line
    raise CronError(f"unrecognised schedule {value!r}")


def parse_spec(spec: str, working_hours: str = "00:00-24:00",
               working_days: str = "daily") -> tuple[CronExpr, str | None] | None:
    """Spec -> (expression in local time, tz override) or ``None`` when disabled."""
    text = str(spec).strip()
    if text.lower() in ("", "off", "false", "disabled", "none"):
        return None
    try:
        from squadhq.config import schedules as cfg_sched
    except ImportError:
        cfg_sched = None  # type: ignore[assignment]
    if cfg_sched is not None and not text.startswith("@"):
        try:
            local = cfg_sched.parse(text, working_hours, working_days)
        except ValueError as e:  # ScheduleError subclasses ValueError
            raise CronError(str(e)) from e
        if local.raw_utc:
            return parse(local.raw_utc), "UTC"
        return from_parts(text, times=tuple(local.times), weekdays=local.dows, days=local.doms,
                          months=local.months), None
    line = translate(text, working_hours)
    return (parse(line), None) if line else None


def build(raw: Mapping[str, Any], *, timezone: str = "UTC", working_hours: str = "00:00-24:00",
          working_days: str = "daily", defaults: Mapping[str, str] | None = None) -> ScheduleConfig:
    cfg = ScheduleConfig(timezone=timezone or "UTC", working_hours=working_hours or "00:00-24:00",
                         working_days=working_days or "daily")
    merged: dict[str, Any] = dict(defaults or {})
    merged.update(raw or {})
    for key, value in merged.items():
        if value is False or value is None:
            continue
        try:
            got = parse_spec(str(value), cfg.working_hours, cfg.working_days)
        except CronError as e:
            cfg.errors.append(f"{key}: {e}")
            continue
        if got is None:
            continue
        expr, tz = got
        cfg.schedules.append(Schedule(key=key, cron=expr, source=str(value), tz=tz))
    return cfg


def _project_dict(p: Any) -> dict[str, Any]:
    if isinstance(p, Mapping):
        return dict(p)
    if dataclasses.is_dataclass(p) and not isinstance(p, type):
        return dataclasses.asdict(p)
    return {"name": str(getattr(p, "name", "")), "repo": str(getattr(p, "repo", ""))}


def load_schedule_config(path: str | None = None, inline: Mapping[str, Any] | None = None,
                         text: str | None = None) -> ScheduleConfig:
    """Load schedules, timezone and projects.

    Order: ``inline`` (an already-parsed ``squadhq.yml`` mapping; used verbatim,
    no defaults) > ``text`` (YAML content, e.g. ``SQUADHQ_CONFIG_YAML`` on
    platforms without a config mount) > ``squadhq.config.load(path)``
    (validated, with the canonical defaults) > the raw YAML file with
    :data:`FALLBACK_DEFAULTS`.
    """
    if inline is not None:
        org = inline.get("org") or {}
        cfg = build(dict(inline.get("schedules") or {}), timezone=str(org.get("timezone") or "UTC"),
                    working_hours=str(org.get("working_hours") or "00:00-24:00"),
                    working_days=str(org.get("working_days") or "daily"))
        cfg.projects = [_project_dict(p) for p in inline.get("projects") or []]
        cfg.hub_repo = str((inline.get("hub") or {}).get("repo") or "")
        _set_budgets(cfg, inline.get("budgets"))
        return cfg
    errors: list[str] = []
    if text:
        return _from_text(text)
    if path and os.path.exists(path):
        try:
            from squadhq.config import load as load_config
        except ImportError:
            load_config = None  # type: ignore[assignment]
        if load_config is not None:
            try:
                c = load_config(path)
            except Exception as e:  # ConfigError: report, then fall back to raw YAML
                errors.append(f"squadhq.yml invalid: {e}")
            else:
                return _from_config(c)
        doc = _load_yaml(path)
    else:
        doc = {}
    org = doc.get("org") or {}
    cfg = build(dict(doc.get("schedules") or {}), timezone=str(org.get("timezone") or "UTC"),
                working_hours=str(org.get("working_hours") or "00:00-24:00"),
                working_days=str(org.get("working_days") or "daily"), defaults=FALLBACK_DEFAULTS)
    cfg.projects = [_project_dict(p) for p in doc.get("projects") or []]
    cfg.hub_repo = str((doc.get("hub") or {}).get("repo") or "")
    _set_budgets(cfg, doc.get("budgets"))
    cfg.errors = errors + cfg.errors
    return cfg


def project_budgets(per_project: Mapping[str, Any], projects: list[dict[str, Any]]) -> dict[str, float]:
    """Per-project monthly budgets keyed by repo.

    Precedence matches ``squadhq.config.render``: the project's own
    ``budget_usd`` > ``per_project_usd[<name>]`` > ``per_project_usd[<repo>]`` >
    ``per_project_usd.default``. Keys naming no known project are kept as-is.
    """
    out: dict[str, float] = {}
    known: set[str] = set()
    default = per_project.get("default")
    for p in projects:
        repo, name = str(p.get("repo") or ""), str(p.get("name") or "")
        known.update({repo, name})
        val = p.get("budget_usd")
        if val is None:
            val = per_project.get(name, per_project.get(repo, default))
        if repo and val is not None:
            out[repo] = float(val)
    for k, v in per_project.items():
        if k != "default" and k not in known and v is not None:
            out[k] = float(v)
    return out


def _set_budgets(cfg: ScheduleConfig, budgets: Mapping[str, Any] | None) -> None:
    b = budgets or {}
    monthly = b.get("monthly_usd")
    cfg.budget_usd = float(monthly) if monthly is not None else None
    cfg.budget_by_project = project_budgets(dict(b.get("per_project_usd") or {}), cfg.projects)


def _from_config(c: Any) -> ScheduleConfig:
    cfg = build(dict(c.schedules), timezone=c.org.timezone, working_hours=c.org.working_hours,
                working_days=c.org.working_days)
    cfg.projects = [_project_dict(p) for p in c.projects]
    cfg.hub_repo = c.hub.repo
    _set_budgets(cfg, {"monthly_usd": c.budgets.monthly_usd, "per_project_usd": dict(c.budgets.per_project_usd)})
    return cfg


def _from_text(text: str) -> ScheduleConfig:
    try:
        from squadhq.config import load_text
    except ImportError:
        load_text = None  # type: ignore[assignment]
    errors: list[str] = []
    if load_text is not None:
        c, issues = load_text(text, "SQUADHQ_CONFIG_YAML")
        if c is not None:
            return _from_config(c)
        errors.append("squadhq.yml invalid: " + "; ".join(str(i) for i in issues))
    import yaml

    doc = yaml.safe_load(text) or {}
    doc = doc if isinstance(doc, dict) else {}
    org = doc.get("org") or {}
    cfg = build(dict(doc.get("schedules") or {}), timezone=str(org.get("timezone") or "UTC"),
                working_hours=str(org.get("working_hours") or "00:00-24:00"),
                working_days=str(org.get("working_days") or "daily"), defaults=FALLBACK_DEFAULTS)
    cfg.projects = [_project_dict(p) for p in doc.get("projects") or []]
    cfg.hub_repo = str((doc.get("hub") or {}).get("repo") or "")
    _set_budgets(cfg, doc.get("budgets"))
    cfg.errors = errors + cfg.errors
    return cfg


def _load_yaml(path: str) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return doc if isinstance(doc, dict) else {}


def _workflow_for(key: str) -> str | None:
    try:
        from squadhq.config.registry import SCHEDULES
    except ImportError:
        return None
    entry = SCHEDULES.get(key)
    return entry[0] if entry else None


def job_for(key: str) -> tuple[str, dict[str, Any]]:
    """Schedule key -> (job kind, payload)."""
    k = key.lower()
    if k in HUB_LOOP_KEYS:
        return "hub_loop", {"mode": HUB_LOOP_KEYS[k], "schedule": key}
    if k in SWEEP_KEYS:
        return "sweep", {"schedule": key}
    if k in JANITOR_KEYS:
        return "janitor", {"scope": "hub", "schedule": key}
    if k == "hub_janitor_projects":
        return "janitor", {"scope": "projects", "schedule": key}
    wf = _workflow_for(key)
    if wf:
        return "hub_workflow", {"workflow": wf, "role": key, "schedule": key}
    return "scheduled", {"schedule": key}
