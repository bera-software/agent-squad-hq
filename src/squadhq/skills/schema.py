"""Skill manifest schema (``skills/<id>/skill.yml``).

The schema is plain Python so it has no dependency beyond PyYAML. The
authoritative prose description lives in ``docs/skills/authoring-skills.md``;
keep the two in step.
"""
from __future__ import annotations

import re
from typing import Any

PHASES: tuple[str, ...] = ("plan", "design", "build", "verify", "release", "operate", "business", "platform")
TIERS: tuple[str, ...] = ("core", "pro", "enterprise")
TOOL_PROFILES: tuple[str, ...] = ("read-only", "comment", "edit-test", "edit-test-push")
MODEL_TIERS: tuple[str, ...] = ("heavy", "light", "none")
TRIGGER_KINDS: tuple[str, ...] = ("label", "event", "schedule", "command", "manual")
OUTPUT_TYPES: tuple[str, ...] = ("artifact", "comment", "label", "issue", "pr", "check", "report", "json")

INPUTS: frozenset[str] = frozenset({
    "issue", "issue_comments", "pr", "pr_comments", "diff", "review", "commits",
    "repo", "adr", "design_doc", "spec", "threat_model", "preview_url",
    "ci_results", "sarif", "sbom", "manifest", "lockfile", "openapi", "schema",
    "migrations", "iac", "deploy_log", "release_history", "changelog", "alert",
    "incident", "metrics", "logs", "traces", "cost_ledger", "cloud_bill", "flows",
    "config", "okrs", "lessons", "web", "tracker", "support_tickets", "feedback",
    "analytics", "eval_results", "docs", "policy", "scope", "state",
})

BADGE_RE = re.compile(r"^\[[^\]]+\] $")
ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
UNTRUSTED_FIRST_LINE = "Read and apply prompts/_untrusted-input.md first."

#: Profiles that can produce a pull request.
PR_PROFILES = frozenset({"edit-test", "edit-test-push"})
#: Output types a read-only skill may declare (code posts them, not the agent).
READ_ONLY_OUTPUTS = frozenset({"report", "json", "check"})

MIN_EVALS = 2
MAX_EVALS = 5

KNOWN_KEYS = frozenset({
    "id", "name", "summary", "phase", "tier", "default_enabled", "legacy", "role", "badge",
    "trigger", "inputs", "tool_profile", "model_tier", "outputs", "gate", "requires",
    "untrusted_inputs", "prompt", "deterministic", "entrypoint", "config", "notes",
})


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def normalise_triggers(raw: Any) -> list[dict[str, Any]]:
    """Return the trigger(s) as a list of mappings (no validation)."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, dict)]
    return []


def trigger_summary(trigger: dict[str, Any]) -> str:
    """Human-readable one-liner, e.g. ``label needs-design``."""
    for kind in TRIGGER_KINDS:
        if kind in trigger:
            value = trigger[kind]
            if kind == "manual":
                return "manual"
            if kind == "command":
                return f"command @squad {value}"
            text = f"{kind} {value}"
            if trigger.get("filter"):
                text += f" ({trigger['filter']})"
            return text
    return "?"


def _validate_trigger(t: Any, where: str, errors: list[str]) -> None:
    if not isinstance(t, dict):
        errors.append(f"{where}: must be a mapping")
        return
    kinds = [k for k in TRIGGER_KINDS if k in t]
    if len(kinds) != 1:
        errors.append(f"{where}: needs exactly one of {', '.join(TRIGGER_KINDS)} (got {kinds or 'none'})")
        return
    kind = kinds[0]
    extra = set(t) - {kind, "filter", "on"}
    if extra:
        errors.append(f"{where}: unknown keys {sorted(extra)}")
    if "filter" in t and kind not in ("event", "label"):
        errors.append(f"{where}: 'filter' is only valid with an event or label trigger")
    value = t[kind]
    if kind == "manual":
        if value is not True:
            errors.append(f"{where}: manual must be true")
    elif not _is_str(value):
        errors.append(f"{where}: {kind} needs a non-empty string")
    elif kind == "event" and not re.match(r"^[a-z_]+(\.[a-z_]+)*$", value):
        errors.append(f"{where}: event must look like 'pull_request' or 'pull_request.opened' (got {value!r})")


def _validate_output(o: Any, where: str, errors: list[str]) -> None:
    if not isinstance(o, dict):
        errors.append(f"{where}: must be a mapping")
        return
    typ = o.get("type")
    if typ not in OUTPUT_TYPES:
        errors.append(f"{where}: type must be one of {', '.join(OUTPUT_TYPES)} (got {typ!r})")
        return
    if typ == "artifact" and not _is_str(o.get("path")):
        errors.append(f"{where}: artifact output needs a 'path' pattern")
    if typ == "label":
        labels = o.get("labels")
        if not isinstance(labels, list) or not labels or not all(_is_str(x) for x in labels):
            errors.append(f"{where}: label output needs a non-empty 'labels' list")
    extra = set(o) - {"type", "path", "labels", "description", "name"}
    if extra:
        errors.append(f"{where}: unknown keys {sorted(extra)}")


def validate_manifest(data: Any) -> list[str]:
    """Validate the manifest structure. Returns a list of error strings.

    File-system checks (prompt exists, evals parse, requires resolve) are done
    by :func:`squadhq.skills.loader.validate_catalogue`.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a mapping"]

    unknown = set(data) - KNOWN_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}")

    sid = data.get("id")
    if not _is_str(sid) or not ID_RE.match(str(sid)):
        errors.append(f"id must be lowercase kebab-case (got {sid!r})")
    for key in ("name", "summary"):
        if not _is_str(data.get(key)):
            errors.append(f"{key} is required")
    if data.get("phase") not in PHASES:
        errors.append(f"phase must be one of {', '.join(PHASES)} (got {data.get('phase')!r})")
    if data.get("tier") not in TIERS:
        errors.append(f"tier must be one of {', '.join(TIERS)} (got {data.get('tier')!r})")
    for key in ("default_enabled", "untrusted_inputs"):
        if not isinstance(data.get(key), bool):
            errors.append(f"{key} must be true or false")
    for key in ("legacy", "deterministic"):
        if key in data and not isinstance(data[key], bool):
            errors.append(f"{key} must be true or false")
    if data.get("tool_profile") not in TOOL_PROFILES:
        errors.append(f"tool_profile must be one of {', '.join(TOOL_PROFILES)} (got {data.get('tool_profile')!r})")
    if data.get("model_tier") not in MODEL_TIERS:
        errors.append(f"model_tier must be one of {', '.join(MODEL_TIERS)} (got {data.get('model_tier')!r})")
    for key in ("role", "badge", "entrypoint"):
        if key in data and data[key] is not None and not _is_str(data[key]):
            errors.append(f"{key} must be a non-empty string")
    badge = data.get("badge")
    if _is_str(badge) and not BADGE_RE.match(str(badge)):
        errors.append(f"badge must look like '[Name] ' with one trailing space (got {badge!r})")

    raw_trigger = data.get("trigger")
    if isinstance(raw_trigger, list):
        if not raw_trigger:
            errors.append("trigger list is empty")
        for i, t in enumerate(raw_trigger):
            _validate_trigger(t, f"trigger[{i}]", errors)
    elif isinstance(raw_trigger, dict):
        _validate_trigger(raw_trigger, "trigger", errors)
    else:
        errors.append("trigger is required (mapping or list of mappings)")

    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        errors.append("inputs must be a non-empty list")
    else:
        for item in inputs:
            if item not in INPUTS:
                errors.append(f"unknown input {item!r} (add it to squadhq.skills.schema.INPUTS)")

    outputs = data.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        errors.append("outputs must be a non-empty list")
        outputs = []
    else:
        for i, o in enumerate(outputs):
            _validate_output(o, f"outputs[{i}]", errors)
    out_types = {o.get("type") for o in outputs if isinstance(o, dict)}
    profile = data.get("tool_profile")
    if "pr" in out_types and profile not in PR_PROFILES:
        errors.append("a 'pr' output needs tool_profile edit-test or edit-test-push")
    if profile == "read-only" and out_types - READ_ONLY_OUTPUTS:
        errors.append(f"read-only skills may only declare {sorted(READ_ONLY_OUTPUTS)} outputs "
                      f"(got {sorted(t for t in out_types - READ_ONLY_OUTPUTS if t)})")

    gate = data.get("gate")
    if gate is not None:
        if not isinstance(gate, dict):
            errors.append("gate must be a mapping")
        else:
            if not any(_is_str(gate.get(k)) for k in ("check", "blocking_label", "verdict")):
                errors.append("gate needs 'check', 'blocking_label' and/or 'verdict'")
            if not _is_str(gate.get("condition")):
                errors.append("gate.condition is required")
            extra = set(gate) - {"check", "blocking_label", "verdict", "condition"}
            if extra:
                errors.append(f"gate: unknown keys {sorted(extra)}")

    requires = data.get("requires", [])
    if not isinstance(requires, list) or not all(_is_str(r) for r in requires):
        errors.append("requires must be a list of skill ids")
    elif sid in requires:
        errors.append("a skill cannot require itself")

    deterministic = data.get("deterministic", False) is True
    prompt = data.get("prompt")
    if deterministic:
        if data.get("model_tier") != "none":
            errors.append("deterministic skills set model_tier: none")
        if not _is_str(data.get("entrypoint")):
            errors.append("deterministic skills need an entrypoint")
        if prompt is not None and not _is_str(prompt):
            errors.append("prompt must be a path or null")
    else:
        if data.get("model_tier") == "none":
            errors.append("model_tier none is only valid for deterministic skills")
        if not _is_str(prompt):
            errors.append("prompt is required for LLM skills")

    if "config" in data and not isinstance(data["config"], dict):
        errors.append("config must be a mapping")
    return errors
