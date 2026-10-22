"""GitHub side effects used by ``squadhq init``/``doctor``: labels, rulesets,
secrets and variables. Every function takes a :class:`~squadhq.config.gh.Gh`
and either *plans* (returns the argv it would run) or *applies*.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from squadhq.paths import hub_root

from . import registry as R
from .gh import Gh, Result

RULESET_NAME = "squadhq"


@dataclass
class Step:
    """One GitHub side effect: ``gh <args>`` (stdin from ``input_text``)."""

    description: str
    args: list[str]
    input_text: str | None = None
    redact: bool = False  # never print input_text
    kind: str = ""  # "ruleset": a failure may only mean the plan lacks rulesets (repo-settings warns)

    def display(self) -> str:
        import shlex
        cmd = "gh " + " ".join(shlex.quote(a) for a in self.args)
        if self.input_text is not None:
            cmd += " < (redacted)" if self.redact else " < (stdin)"
        return cmd


@dataclass
class Outcome:
    step: Step
    result: Result


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    def add(self, description: str, args: list[str], input_text: str | None = None, redact: bool = False,
            kind: str = "") -> None:
        self.steps.append(Step(description, args, input_text, redact, kind))

    def apply(self, gh: Gh) -> list[Outcome]:
        return [Outcome(s, gh.run(s.args, input_text=s.input_text)) for s in self.steps]


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------

def label_registry(flows_path: str | None = None) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """``(project_labels, hub_only_labels)`` as ``(name, color, description)``
    from config/flows.yml (same categories squadhq.onboard uses)."""
    import yaml
    path = flows_path or os.path.join(hub_root(), "config", "flows.yml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    project: list[tuple[str, str, str]] = []
    hub: list[tuple[str, str, str]] = []
    for category, entries in (data.get("labels") or {}).items():
        if category in ("dynamic", "report_tags_not_real_labels"):
            continue
        for name, meta in (entries or {}).items():
            meta = meta or {}
            desc = truncate_words(" ".join(str(meta.get("description", "")).split()))
            item = (str(name), str(meta.get("color", "ededed")), desc)
            (hub if category == "hub_only" else project).append(item)
    return project, hub


def truncate_words(text: str, limit: int = 100) -> str:
    """GitHub caps label descriptions at 100 chars: cut at a word boundary and
    end with an ellipsis instead of mid-word."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def plan_labels(plan: Plan, repo: str, labels: list[tuple[str, str, str]]) -> None:
    for name, color, desc in labels:
        args = ["label", "create", name, "-R", repo, "--force", "--color", color]
        if desc:
            args += ["--description", desc]
        plan.add(f"label {name} on {repo}", args)


# --------------------------------------------------------------------------
# rulesets
# --------------------------------------------------------------------------

def ruleset_body(check: str = R.REQUIRED_CHECK) -> dict[str, Any]:
    """Default-branch ruleset. One approval from someone other than the last
    pusher: the agent App pushes, so its own approval never counts; a human
    (or, in risk_based/auto modes, the ops App after squadhq.merge_policy
    passes) approves. No bypass actors at all, so no App can skip it.
    Fork PRs additionally need "Require approval for all outside collaborators"
    (repo Settings > Actions), which no API here can set for you."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": False,
                "require_last_push_approval": True,
                "required_review_thread_resolution": False,
            }},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": check}],
            }},
        ],
    }


def existing_ruleset_id(gh: Gh, repo: str) -> int | None:
    r = gh.run(["api", f"repos/{repo}/rulesets", "--paginate"])
    if not r.ok:
        return None
    for rs in r.json() or []:
        if isinstance(rs, dict) and rs.get("name") == RULESET_NAME:
            return int(rs["id"])
    return None


def plan_ruleset(plan: Plan, gh: Gh | None, repo: str) -> None:
    rid = existing_ruleset_id(gh, repo) if gh else None
    body = json.dumps(ruleset_body())
    if rid:
        plan.add(f"update ruleset '{RULESET_NAME}' on {repo} (requires `{R.REQUIRED_CHECK}`)",
                 ["api", "-X", "PUT", f"repos/{repo}/rulesets/{rid}", "--input", "-"], body, kind="ruleset")
    else:
        plan.add(f"create ruleset '{RULESET_NAME}' on {repo} (requires `{R.REQUIRED_CHECK}`)",
                 ["api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-"], body, kind="ruleset")


# --------------------------------------------------------------------------
# repo-settings: admin settings only (hosted / token-broker installs)
# --------------------------------------------------------------------------

RULESET_PLAN_HINT = ("rulesets on private repositories need GitHub Pro, Team or Enterprise: upgrade the plan or make "
                     "the repository public; until then a merge needs no `hub / ci-gate` and no approval")


def _repo_info(gh: Gh | None, repo: str) -> dict[str, Any]:
    if gh is None:
        return {}
    r = gh.run(["api", f"repos/{repo}"])
    data = r.json() if r.ok else None
    return data if isinstance(data, dict) else {}


def plan_hub_access(plan: Plan, gh: Gh | None, hub: str, notes: list[str]) -> None:
    """Let repositories of the hub's owner call its reusable workflows
    (PUT /repos/{hub}/actions/permissions/access)."""
    info = _repo_info(gh, hub)
    if info.get("visibility") == "public" or info.get("private") is False:
        notes.append(f"{hub} is public: every repository can already use its reusable workflows")
        return
    owner_type = str((info.get("owner") or {}).get("type") or "")
    if not owner_type and gh is not None:
        r = gh.run(["api", f"users/{hub.split('/')[0]}", "--jq", ".type"])
        owner_type = r.stdout.strip() if r.ok else ""
    level = "user" if owner_type == "User" else "organization"
    if gh is not None:
        cur = gh.run(["api", f"repos/{hub}/actions/permissions/access", "--jq", ".access_level"])
        if cur.ok and cur.stdout.strip() == level:
            notes.append(f"{hub}: Actions access already `{level}`")
            return
    plan.add(f"let repositories owned by {hub.split('/')[0]} use {hub}'s reusable workflows "
             f"(Actions access level `{level}`)",
             ["api", "-X", "PUT", f"repos/{hub}/actions/permissions/access", "-f", f"access_level={level}"])


def plan_merge_settings(plan: Plan, info: dict[str, Any], repo: str, notes: list[str]) -> None:
    """allow_auto_merge and allow_squash_merge on (auto-merge arms a squash merge)."""
    if info.get("allow_auto_merge") is True and info.get("allow_squash_merge") is True:
        notes.append(f"{repo}: auto-merge and squash merging already allowed")
        return
    plan.add(f"allow auto-merge and squash merging on {repo}",
             ["api", "-X", "PATCH", f"repos/{repo}", "-F", "allow_auto_merge=true", "-F", "allow_squash_merge=true"])


def repo_settings_plan(gh: Gh | None, hub: str, projects: list[str]) -> tuple[Plan, list[str]]:
    """The admin settings a hosted or token-broker install needs, and nothing
    else: hub Actions access, and per project merge settings plus the
    ``squadhq`` ruleset that ``squadhq init --apply`` creates."""
    plan = Plan()
    notes: list[str] = []
    plan_hub_access(plan, gh, hub, notes)
    for repo in projects:
        info = _repo_info(gh, repo)
        plan_merge_settings(plan, info, repo, notes)
        plan_ruleset(plan, gh, repo)
        if info.get("private") is True:
            notes.append(f"{repo} is private: {RULESET_PLAN_HINT.split(':')[0]}")
    return plan, notes


def is_plan_limit(result: Result) -> bool:
    """A ruleset call refused because the account's plan has no rulesets for private repos."""
    text = (result.stderr + result.stdout).lower()
    return "upgrade to github pro" in text or "make this repository public" in text or (
        "http 403" in text and "ruleset" in text)


def ruleset_requires_check(gh: Gh, repo: str, check: str = R.REQUIRED_CHECK) -> bool | None:
    """True/False, or None when rulesets cannot be read with this login."""
    r = gh.run(["api", f"repos/{repo}/rules/branches/{_default_branch(gh, repo)}"])
    if not r.ok:
        return None
    for rule in r.json() or []:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            checks = (rule.get("parameters") or {}).get("required_status_checks") or []
            if any(c.get("context") == check for c in checks if isinstance(c, dict)):
                return True
    return False


def ruleset_review(gh: Gh, repo: str) -> dict[str, Any] | None:
    """The effective pull_request rule on the default branch:
    ``{"approvals": int, "last_push": bool, "dismiss_stale": bool}``
    (approvals 0 when no PR rule), or None when the rules cannot be read."""
    r = gh.run(["api", f"repos/{repo}/rules/branches/{_default_branch(gh, repo)}"])
    if not r.ok:
        return None
    out: dict[str, Any] = {"approvals": 0, "last_push": False, "dismiss_stale": False}
    for rule in r.json() or []:
        if isinstance(rule, dict) and rule.get("type") == "pull_request":
            p = rule.get("parameters") or {}
            out["approvals"] = max(out["approvals"], int(p.get("required_approving_review_count") or 0))
            out["last_push"] = out["last_push"] or bool(p.get("require_last_push_approval"))
            out["dismiss_stale"] = out["dismiss_stale"] or bool(p.get("dismiss_stale_reviews_on_push"))
    return out


def _default_branch(gh: Gh, repo: str) -> str:
    r = gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"])
    return r.stdout.strip() if r.ok and r.stdout.strip() else "main"


# --------------------------------------------------------------------------
# variables / secrets
# --------------------------------------------------------------------------

def scope_args(owner: str, scope: str, hub_repo: str) -> list[str]:
    return ["--org", owner, "--visibility", "all"] if scope == "org" else ["-R", hub_repo]


def plan_variables(plan: Plan, variables: dict[str, str], owner: str, scope: str, hub_repo: str) -> None:
    for k, v in variables.items():
        if v == "":
            continue  # gh refuses empty bodies; unset == empty for the workflows
        plan.add(f"variable {k}", ["variable", "set", k, "--body", v, *scope_args(owner, scope, hub_repo)])


def plan_secret_from_file(plan: Plan, name: str, path: str, owner: str, scope: str, hub_repo: str) -> None:
    with open(path, encoding="utf-8") as f:
        value = f.read()
    plan.add(f"secret {name} (from {path})", ["secret", "set", name, *scope_args(owner, scope, hub_repo)],
             value, redact=True)


def list_names(gh: Gh, kind: str, owner: str, hub_repo: str) -> dict[str, dict[str, Any]]:
    """Names of secrets/variables visible to the hub (repo-level plus org-level)."""
    out: dict[str, dict[str, Any]] = {}
    fields = "name,updatedAt" if kind == "secret" else "name,value,updatedAt"
    for scope in (["--org", owner], ["-R", hub_repo]):
        r = gh.run([kind, "list", *scope, "--json", fields])
        if r.ok:
            for item in r.json() or []:
                if isinstance(item, dict) and "name" in item:
                    out[str(item["name"])] = item
    return out
