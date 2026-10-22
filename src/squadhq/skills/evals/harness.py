"""Run eval cases against a model and score them.

The live path calls :mod:`squadhq.providers` (imported lazily so the harness
works without it). ``--dry-run`` and tests use :class:`FakeProvider`, which
replays each case's ``reference_response``; cases without one are rendered
but not scored.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import yaml

from squadhq import paths
from squadhq.interfaces import Message
from squadhq.skills.evals.cases import EvalCase, case_files, load_case_file
from squadhq.skills.evals.scoring import EVAL_MODE_INSTRUCTIONS, CaseScore, score
from squadhq.skills.loader import Skill, load_catalogue
from squadhq.skills.render import render_prompt

DEFAULT_PROVIDER_REF = "anthropic/claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 4096


class Completer(Protocol):
    """What the harness needs from a provider (a subset of ``LLMProvider``)."""

    name: str

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> Any: ...

    def price(self, model: str, usage: Any) -> Decimal: ...


@dataclass
class FakeReply:
    text: str
    usage: Any = None
    stop_reason: str = "end"
    model: str = "fake"
    provider: str = "fake"


class FakeProvider:
    """Offline provider. Replies from a ``{case_id: text}`` map or a callable."""

    name = "fake"

    def __init__(self, responses: dict[str, str] | None = None, default: str | None = None,
                 cost_usd: float = 0.0) -> None:
        self.responses = responses or {}
        self.default = default
        self.cost_usd = cost_usd
        self.calls: list[dict[str, Any]] = []
        self.current_case: str | None = None

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> FakeReply:
        self.calls.append({"model": model, "system": system, "messages": messages, "case": self.current_case})
        text = self.responses.get(self.current_case or "", self.default)
        if text is None:
            raise LookupError(f"no fake response for case {self.current_case!r}")
        return FakeReply(text=text, model=model)

    def price(self, model: str, usage: Any) -> Decimal:
        return Decimal(str(self.cost_usd))


def parse_provider_ref(ref: str) -> tuple[str, str]:
    name, sep, model = ref.partition("/")
    if not sep or not name or not model:
        raise ValueError(f"provider ref must look like 'anthropic/claude-sonnet-5-5' (got {ref!r})")
    return name, model


def load_provider(name: str) -> Completer:  # pragma: no cover - needs the providers package
    """Resolve a live provider from :mod:`squadhq.providers` (lazy import)."""
    try:
        from squadhq import providers
    except ImportError as exc:
        raise RuntimeError("squadhq.providers is not installed; use --dry-run or install the providers "
                           "package") from exc
    getter = getattr(providers, "get_provider", None)
    if getter is None:
        raise RuntimeError("squadhq.providers has no get_provider()")
    provider: Completer = getter(name)
    return provider


@dataclass
class CaseResult:
    case: EvalCase
    status: str  # "scored" | "skipped" | "error"
    score: CaseScore | None = None
    output: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case.id, "skill": self.case.skill, "kind": self.case.kind,
            "category": self.case.category, "source": self.case.source, "status": self.status,
            "passed": bool(self.score and self.score.passed), "model": self.model,
            "cost_usd": round(self.cost_usd, 6), "latency_s": round(self.latency_s, 3),
            "error": self.error, "score": self.score.to_dict() if self.score else None,
            "output": self.output[:6000],
        }


@dataclass
class RunSummary:
    started_at: str
    provider_ref: str
    dry_run: bool
    results: list[CaseResult] = field(default_factory=list)
    gate_failures: list[str] = field(default_factory=list)

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.results if r.status in ("scored", "error")]

    def pass_rate(self, rows: list[CaseResult] | None = None) -> float | None:
        rows = self.scored if rows is None else rows
        if not rows:
            return None
        return sum(1 for r in rows if r.score and r.score.passed) / len(rows)

    @property
    def policy_violations(self) -> int:
        return sum(len(r.score.policy_violations) for r in self.results if r.score)

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.results)

    def group(self, attr: str) -> dict[str, list[CaseResult]]:
        out: dict[str, list[CaseResult]] = {}
        for r in self.scored:
            out.setdefault(str(getattr(r.case, attr)), []).append(r)
        return out

    def to_dict(self) -> dict[str, Any]:
        def rates(attr: str) -> dict[str, Any]:
            return {k: {"cases": len(v), "pass_rate": self.pass_rate(v)} for k, v in sorted(self.group(attr).items())}
        return {
            "started_at": self.started_at, "provider_ref": self.provider_ref, "dry_run": self.dry_run,
            "totals": {
                "cases": len(self.results), "scored": len(self.scored),
                "skipped": sum(1 for r in self.results if r.status == "skipped"),
                "passed": sum(1 for r in self.scored if r.score and r.score.passed),
                "pass_rate": self.pass_rate(), "policy_violations": self.policy_violations,
                "cost_usd": round(self.total_cost, 6),
            },
            "by_kind": rates("kind"), "by_skill": rates("skill"), "by_category": rates("category"),
            "gate_failures": self.gate_failures,
            "results": [r.to_dict() for r in self.results],
        }


def collect_cases(*, hub: str | None = None, skills: dict[str, Skill] | None = None, suite: str = "all",
                  skill_id: str | None = None, kind: str | None = None) -> list[EvalCase]:
    hub = hub or paths.hub_root()
    skills = skills if skills is not None else load_catalogue(os.path.join(hub, "skills"))
    cases: list[EvalCase] = []
    if suite in ("all", "skills"):
        for _sid, skill in sorted(skills.items()):
            if skill.deterministic:
                continue
            cases.extend(skill.load_evals())
    if suite in ("all", "golden"):
        for path in case_files(os.path.join(hub, "evals", "golden")):
            cases.extend(load_case_file(path))
    if skill_id:
        cases = [c for c in cases if c.skill == skill_id]
    if kind:
        cases = [c for c in cases if c.kind == kind]
    return cases


def run_case(case: EvalCase, skill: Skill, provider: Completer, model: str, *, hub: str,
             max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = 180.0) -> CaseResult:
    if isinstance(provider, FakeProvider):
        provider.current_case = case.id
    rendered = render_prompt(skill, case.context, hub=hub, suffix=EVAL_MODE_INSTRUCTIONS)
    started = time.monotonic()
    try:
        reply = provider.complete(model=model, system=rendered.system,
                                  messages=[Message(role="user", content=rendered.user)],
                                  max_tokens=max_tokens, timeout=timeout)
    except LookupError:
        return CaseResult(case=case, status="skipped", model=model)
    except Exception as exc:  # provider failures are recorded, never crash the run
        text = f"{type(exc).__name__}: {exc}"
        return CaseResult(case=case, status="error", score=score(case, "", error=text), error=text,
                          latency_s=time.monotonic() - started, model=model)
    latency = time.monotonic() - started
    try:
        cost = float(provider.price(model, reply.usage)) if reply.usage is not None or isinstance(
            provider, FakeProvider) else 0.0
    except Exception:
        cost = 0.0
    text = str(getattr(reply, "text", "") or "")
    result = score(case, text, cost_usd=cost)
    return CaseResult(case=case, status="scored", score=result, output=text, cost_usd=cost,
                      latency_s=latency, model=str(getattr(reply, "model", model) or model))


def load_thresholds(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: thresholds must be a mapping")
    return data


def apply_thresholds(summary: RunSummary, thresholds: dict[str, Any],
                     baseline: dict[str, Any] | None = None) -> list[str]:
    failures: list[str] = []

    def check(label: str, rows: list[CaseResult], minimum: Any) -> None:
        rate = summary.pass_rate(rows)
        if rate is not None and minimum is not None and rate + 1e-9 < float(minimum):
            failures.append(f"{label}: pass rate {rate:.1%} < {float(minimum):.1%}")

    overall = (thresholds.get("overall") or {}).get("min_pass_rate")
    check("overall", summary.scored, overall)
    for group_name, attr in (("kinds", "kind"), ("categories", "category"), ("skills", "skill")):
        groups = summary.group(attr)
        for key, cfg in (thresholds.get(group_name) or {}).items():
            if key in groups and isinstance(cfg, dict):
                check(f"{attr} {key}", groups[key], cfg.get("min_pass_rate"))
    max_violations = thresholds.get("policy_violations_max")
    if max_violations is not None and summary.policy_violations > int(max_violations):
        failures.append(f"policy violations {summary.policy_violations} > {max_violations}")
    max_cost = thresholds.get("max_total_cost_usd")
    if max_cost is not None and summary.total_cost > float(max_cost):
        failures.append(f"total cost ${summary.total_cost:.2f} > ${float(max_cost):.2f}")
    if baseline:
        allowed = float((thresholds.get("baseline") or {}).get("max_regression", 0.02))
        prev = (baseline.get("totals") or {}).get("pass_rate")
        now = summary.pass_rate()
        if prev is not None and now is not None and now + 1e-9 < float(prev) - allowed:
            failures.append(f"pass rate {now:.1%} regressed more than {allowed:.0%} from baseline {float(prev):.1%}")
    return failures


def run(*, provider: Completer, provider_ref: str, hub: str | None = None, suite: str = "all",
        skill_id: str | None = None, kind: str | None = None, dry_run: bool = False,
        light_model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS,
        limit: int | None = None) -> RunSummary:
    hub = hub or paths.hub_root()
    skills = load_catalogue(os.path.join(hub, "skills"))
    cases = collect_cases(hub=hub, skills=skills, suite=suite, skill_id=skill_id, kind=kind)
    if limit:
        cases = cases[:limit]
    _, model = parse_provider_ref(provider_ref)
    summary = RunSummary(started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         provider_ref=provider_ref, dry_run=dry_run)
    for case in cases:
        skill = skills.get(case.skill)
        if skill is None or skill.deterministic:
            err = f"case targets unknown or deterministic skill {case.skill!r}"
            summary.results.append(CaseResult(case=case, status="error", error=err,
                                              score=score(case, "", error=err)))
            continue
        use_model = light_model if (light_model and skill.model_tier == "light") else model
        summary.results.append(run_case(case, skill, provider, use_model, hub=hub, max_tokens=max_tokens))
    return summary


def write_results(summary: RunSummary, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = summary.started_at.replace("-", "").replace(":", "")
    json_path = os.path.join(out_dir, f"{stamp}.json")
    md_path = os.path.join(out_dir, f"{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary.to_dict(), fh, indent=2)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(markdown_summary(summary))
    return json_path, md_path


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def markdown_summary(summary: RunSummary) -> str:
    d = summary.to_dict()
    t = d["totals"]
    lines = [
        f"# Eval run {summary.started_at}",
        "",
        f"- Provider: `{summary.provider_ref}`{' (dry run: reference responses only)' if summary.dry_run else ''}",
        f"- Cases: {t['cases']} ({t['scored']} scored, {t['skipped']} skipped)",
        f"- Pass rate: **{_pct(t['pass_rate'])}** ({t['passed']}/{t['scored']})",
        f"- Policy violations: **{t['policy_violations']}**",
        f"- Cost: ${t['cost_usd']:.4f}",
        f"- Gate: {'PASS' if not summary.gate_failures else 'FAIL'}",
        "",
    ]
    if summary.gate_failures:
        lines += ["## Gate failures", ""] + [f"- {f}" for f in summary.gate_failures] + [""]
    lines += ["## By kind", "", "| Kind | Cases | Pass rate |", "|---|---|---|"]
    lines += [f"| {k} | {v['cases']} | {_pct(v['pass_rate'])} |" for k, v in d["by_kind"].items()]
    lines += ["", "## By skill", "", "| Skill | Cases | Pass rate |", "|---|---|---|"]
    lines += [f"| {k} | {v['cases']} | {_pct(v['pass_rate'])} |" for k, v in d["by_skill"].items()]
    failed = [r for r in summary.scored if not (r.score and r.score.passed)]
    if failed:
        lines += ["", "## Failed cases", ""]
        for r in failed:
            reasons = [f"{c.name}: {c.detail}" if c.detail else c.name
                       for c in (r.score.checks if r.score else []) if not c.passed]
            lines.append(f"- `{r.case.id}` ({r.case.skill}): " + "; ".join(reasons or [r.error]))
    return "\n".join(lines) + "\n"
