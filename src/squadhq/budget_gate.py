"""Budget gate for agent jobs: monthly, per-project and per-run caps.

Hub loops check ``budgets.monthly_usd`` in ``squadhq.agent_runner``; coding
agents (implement, review, design, mention, skills...) are billed per run and
checked here, in ``agent-prep`` before the agent starts. Only API-billed runs
are checked: a subscription run adds no marginal spend.

Spend this month = hub-loop usage (``state/usage.json``) + API-billed rows of
the bot's "Cost ledger — <month>" hub issue. Per-project spend counts the
ledger rows whose ``repo`` is the project.

When a cap is reached, ``budgets.on_exceed`` decides:

* ``pause``     the agent does not start (auth_mode=skip); the issue or PR
                gets a notice and the hub an escalation issue.
* ``downgrade`` the agent runs on the cheapest configured model.
* ``notify``    the agent runs; the hub gets the escalation issue.

``budgets.per_run_usd`` (job name or ``default``) becomes Claude Code's
``--max-budget-usd`` for API-billed runs.

Config: ``config/projects.json`` ``budgets`` (rendered from squadhq.yml) and
each project's ``budget_usd``; ``SQUADHQ_MONTHLY_BUDGET_USD`` overrides the
monthly cap. Any read failure fails open (the run goes ahead) and says so in
the step summary.

    python -P -m squadhq.budget_gate check     # env: JOB REPO AUTH_MODE HUB_REPO ISSUE_OR_PR COMMENT_ON_SKIP
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from squadhq.paths import hub_path

ACTIONS = ("pause", "downgrade", "notify")
USAGE_PATH = "state/usage.json"
_ROW_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


@dataclass
class Budgets:
    monthly_usd: float | None = None
    per_run_usd: dict[str, float] = field(default_factory=dict)
    on_exceed: str = "pause"
    project_usd: dict[str, float] = field(default_factory=dict)  # repo -> cap


@dataclass
class Decision:
    action: str = "run"  # run | pause | downgrade | notify
    reason: str = ""
    max_run_usd: float | None = None
    model: str = ""


def month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_budgets(path: str | None = None, env: dict[str, str] | None = None) -> Budgets:
    env = dict(os.environ) if env is None else env
    try:
        with open(path or hub_path("config", "projects.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    raw = data.get("budgets") if isinstance(data.get("budgets"), dict) else {}
    b = Budgets()
    monthly = env.get("SQUADHQ_MONTHLY_BUDGET_USD") or raw.get("monthly_usd")
    try:
        b.monthly_usd = float(monthly) if monthly not in (None, "") else None
    except (TypeError, ValueError):
        b.monthly_usd = None
    b.per_run_usd = {str(k): float(v) for k, v in (raw.get("per_run_usd") or {}).items()
                     if isinstance(v, (int, float))}
    b.on_exceed = str(raw["on_exceed"]) if raw.get("on_exceed") in ACTIONS else "pause"
    for p in data.get("projects") or []:
        if isinstance(p, dict) and p.get("repo") and isinstance(p.get("budget_usd"), (int, float)):
            b.project_usd[str(p["repo"]).lower()] = float(p["budget_usd"])
    return b


def ledger_spend(comments: list[dict[str, Any]], by_bot) -> tuple[float, dict[str, float]]:
    """(API-billed total, per-repo totals) from ledger comments the bot wrote."""
    total = 0.0
    per_repo: dict[str, float] = {}
    for c in comments:
        if not by_bot(c):
            continue
        m = _ROW_RE.search(c.get("body") or "")
        if not m:
            continue
        try:
            row = json.loads(m.group(1))
        except ValueError:  # noqa: S112 - a malformed line is skipped, as in agent_runner
            continue
        cost = row.get("cost_usd")
        if row.get("skipped_capacity") or not isinstance(cost, (int, float)) or (row.get("billing") or "api") != "api":
            continue
        total += cost
        repo = str(row.get("repo") or "").lower()
        if repo:
            per_repo[repo] = per_repo.get(repo, 0.0) + cost
    return total, per_repo


def hub_loop_spend(usage: dict[str, Any], current: str) -> float:
    if not isinstance(usage, dict) or usage.get("month") != current:
        return 0.0
    v = usage.get("est_cost_usd")
    return float(v) if isinstance(v, (int, float)) else 0.0


def cheapest_model(models: dict[str, Any], rates) -> str:
    """The configured coding-agent model with the lowest input price."""
    best: tuple[float, str] | None = None
    for tier in (models.get("tiers") or {}).values():
        for key in ("primary", "fallback"):
            ref = tier.get(key) if isinstance(tier, dict) else None
            if not ref:
                continue
            r = rates(ref)
            if r is not None and (best is None or r < best[0]):
                best = (r, ref)
    return best[1] if best else ""


def decide(b: Budgets, job: str, repo: str, spent: float, project_spent: float,
           downgrade_model: str = "") -> Decision:
    d = Decision(max_run_usd=b.per_run_usd.get(job, b.per_run_usd.get("default")))
    cap = b.project_usd.get(repo.lower())
    if b.monthly_usd is not None and spent >= b.monthly_usd:
        d.reason = f"monthly budget ${b.monthly_usd:g} reached (${spent:.2f} spent in {month()})"
    elif cap is not None and project_spent >= cap:
        d.reason = f"{repo} budget ${cap:g} reached (${project_spent:.2f} spent in {month()})"
    else:
        return d
    d.action = b.on_exceed
    if d.action == "downgrade":
        if downgrade_model:
            d.model = downgrade_model
        else:
            d.action = "pause"
            d.reason += "; no cheaper model is configured, so the run is paused"
    return d


def _rates(ref: str) -> float | None:
    from squadhq.providers import prices, resolve
    try:
        provider, model = resolve(ref)
        r = prices.rates(provider, model)
    except Exception:
        return None
    return r[0] if r else None


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def check(env: dict[str, str] | None = None) -> Decision:
    from squadhq import capacity_gate, github, untrusted

    env = dict(os.environ) if env is None else env
    job, repo = env.get("JOB", ""), env.get("REPO", "")
    hub = env.get("HUB_REPO") or env.get("GITHUB_REPOSITORY", "")
    out, summary = env.get("GITHUB_OUTPUT"), env.get("GITHUB_STEP_SUMMARY")
    b = load_budgets(env=env)
    if env.get("AUTH_MODE") != "api":
        d = Decision()
    else:
        current = month()
        try:
            usage, _ = capacity_gate.read_hub_json(hub, USAGE_PATH, {})
            number = github.client().find_issue_by_title(hub, f"Cost ledger — {current}", state="all")
            comments = github.client().paginate(f"repos/{hub}/issues/{number}/comments") if number else []
            total, per_repo = ledger_spend(comments or [], untrusted.by_bot)
        except Exception as e:  # fail open: a missed check must not stop the squad
            _append(summary, f"budget-gate: could not read spend ({e!r}); not checked\n")
            d = Decision(max_run_usd=b.per_run_usd.get(job, b.per_run_usd.get("default")))
        else:
            try:
                with open(hub_path("config", "models.json"), encoding="utf-8") as f:
                    models = json.load(f)
            except (OSError, ValueError):
                models = {}
            d = decide(b, job, repo, total + hub_loop_spend(usage, current), per_repo.get(repo.lower(), 0.0),
                       cheapest_model(models, _rates))
            if d.action != "run":
                badge = f"Budget: {d.reason}."
                follow = {"pause": "Agent runs are paused until the budget is raised or the month ends.",
                          "downgrade": f"Agent runs continue on {d.model}.",
                          "notify": "Agent runs continue (budgets.on_exceed: notify)."}[d.action]
                try:
                    # Once per month and action: no comment on an existing issue, so runs do not pile up notices.
                    github.client().find_or_create_issue_by_title(
                        hub, f"BUDGET CAP HIT: {current} ({d.action})", f"{badge} {follow}", labels=["escalation"])
                    if d.action == "pause" and env.get("COMMENT_ON_SKIP") == "true" and env.get("ISSUE_OR_PR"):
                        capacity_gate.dedupe_comment(repo, env["ISSUE_OR_PR"],
                                                     f"Paused: {d.reason}. The agent will run once the budget "
                                                     "allows; re-apply the label or comment again then.")
                except Exception as e:
                    _append(summary, f"budget-gate: could not post the notice ({e!r})\n")
                _append(summary, f"budget-gate: {d.action} — {d.reason}\n")
    if d.max_run_usd is not None and env.get("AUTH_MODE") != "api":
        d.max_run_usd = None
    mode = "skip" if d.action == "pause" else env.get("AUTH_MODE", "")
    _append(out, f"auth_mode={mode}\nbudget_action={d.action}\nmodel={d.model}\n"
                 f"max_budget_usd={'' if d.max_run_usd is None else f'{d.max_run_usd:g}'}\n")
    return d


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["check"]:
        print("usage: python -P -m squadhq.budget_gate check", file=sys.stderr)
        return 2
    check()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
