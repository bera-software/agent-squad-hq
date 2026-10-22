"""Load and validate the skill catalogue under ``skills/``."""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from squadhq import paths
from squadhq.skills import schema
from squadhq.skills.evals.cases import CaseError, EvalCase, case_files, load_case_file

#: Labels other owners are adding to ``config/flows.yml`` in the same release.
#: Treated as known so validation does not warn while that change lands.
PENDING_LABELS: frozenset[str] = frozenset({
    "needs-threat-model", "security:review-required", "security:approved",
    "gate:sast", "gate:sca", "gate:secrets",
})


def _uptime_validator(cfg: dict[str, Any]) -> list[str]:
    from squadhq.skills.uptime import validate_config

    return validate_config(cfg)


#: Skill-specific ``config:`` validators for deterministic skills.
CONFIG_VALIDATORS: dict[str, Any] = {"uptime-watcher": _uptime_validator}


class SkillError(ValueError):
    """A manifest could not be read."""



def _module_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False

@dataclass
class Skill:
    id: str
    name: str
    summary: str
    phase: str
    tier: str
    default_enabled: bool
    triggers: list[dict[str, Any]]
    inputs: list[str]
    tool_profile: str
    model_tier: str
    outputs: list[dict[str, Any]]
    untrusted_inputs: bool
    directory: str
    prompt: str | None = None
    gate: dict[str, Any] | None = None
    requires: list[str] = field(default_factory=list)
    legacy: bool = False
    deterministic: bool = False
    role: str | None = None
    badge: str | None = None
    entrypoint: str | None = None
    config: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_path(self) -> str | None:
        """Absolute path of the prompt file (may not exist)."""
        if not self.prompt:
            return None
        return os.path.normpath(os.path.join(self.directory, self.prompt))

    @property
    def evals_dir(self) -> str:
        return os.path.join(self.directory, "evals")

    def labels(self) -> set[str]:
        out: set[str] = set()
        for o in self.outputs:
            if o.get("type") == "label":
                out.update(o.get("labels") or [])
        if self.gate and self.gate.get("blocking_label"):
            out.add(str(self.gate["blocking_label"]))
        for t in self.triggers:
            if "label" in t:
                out.add(str(t["label"]))
        return out

    def load_evals(self) -> list[EvalCase]:
        cases: list[EvalCase] = []
        for path in case_files(self.evals_dir):
            cases.extend(load_case_file(path, default_skill=self.id))
        return cases

    def read_prompt(self) -> str:
        path = self.prompt_path
        if not path:
            raise SkillError(f"{self.id}: deterministic skill has no prompt")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "summary": self.summary, "phase": self.phase,
            "tier": self.tier, "default_enabled": self.default_enabled,
            "triggers": [schema.trigger_summary(t) for t in self.triggers],
            "inputs": self.inputs, "tool_profile": self.tool_profile, "model_tier": self.model_tier,
            "outputs": self.outputs, "gate": self.gate, "requires": self.requires,
            "untrusted_inputs": self.untrusted_inputs, "legacy": self.legacy,
            "deterministic": self.deterministic, "prompt": self.prompt, "role": self.role,
        }


def skills_root() -> str:
    return paths.hub_path("skills")


def _from_manifest(data: dict[str, Any], directory: str) -> Skill:
    return Skill(
        id=str(data["id"]), name=str(data["name"]), summary=" ".join(str(data["summary"]).split()),
        phase=str(data["phase"]), tier=str(data["tier"]), default_enabled=bool(data["default_enabled"]),
        triggers=schema.normalise_triggers(data.get("trigger")), inputs=list(data.get("inputs") or []),
        tool_profile=str(data["tool_profile"]), model_tier=str(data["model_tier"]),
        outputs=list(data.get("outputs") or []), untrusted_inputs=bool(data["untrusted_inputs"]),
        directory=directory, prompt=data.get("prompt"), gate=data.get("gate"),
        requires=list(data.get("requires") or []), legacy=bool(data.get("legacy", False)),
        deterministic=bool(data.get("deterministic", False)), role=data.get("role"),
        badge=data.get("badge"), entrypoint=data.get("entrypoint"), config=data.get("config"), raw=data,
    )


def read_manifest(directory: str) -> dict[str, Any]:
    path = os.path.join(directory, "skill.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise SkillError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillError(f"{path}: manifest must be a mapping")
    return data


def skill_dirs(root: str | None = None) -> list[str]:
    root = root or skills_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        os.path.join(root, name) for name in os.listdir(root)
        if os.path.isfile(os.path.join(root, name, "skill.yml"))
    )


def load_skill(directory: str) -> Skill:
    data = read_manifest(directory)
    errors = schema.validate_manifest(data)
    if errors:
        raise SkillError(f"{directory}: " + "; ".join(errors))
    return _from_manifest(data, os.path.abspath(directory))


def load_catalogue(root: str | None = None) -> dict[str, Skill]:
    """Load every valid skill. Raises :class:`SkillError` on the first invalid one."""
    out: dict[str, Skill] = {}
    for d in skill_dirs(root):
        skill = load_skill(d)
        if skill.id in out:
            raise SkillError(f"duplicate skill id {skill.id!r}")
        out[skill.id] = skill
    return out


def get_skill(skill_id: str, root: str | None = None) -> Skill:
    root = root or skills_root()
    directory = os.path.join(root, skill_id)
    if not os.path.isfile(os.path.join(directory, "skill.yml")):
        raise SkillError(f"unknown skill {skill_id!r} (no {directory}/skill.yml)")
    return load_skill(directory)


def flows_labels(flows_path: str | None = None) -> set[str] | None:
    """Label names registered in ``config/flows.yml``; None if unreadable."""
    flows_path = flows_path or paths.hub_path("config", "flows.yml")
    try:
        with open(flows_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    labels: set[str] = set()
    groups = data.get("labels") or {}
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, dict):
                labels.update(str(k) for k in group)
    return labels


def flows_roles(flows_path: str | None = None) -> dict[str, Any] | None:
    flows_path = flows_path or paths.hub_path("config", "flows.yml")
    try:
        with open(flows_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    roles = data.get("roles")
    return roles if isinstance(roles, dict) else {}


@dataclass
class ValidationReport:
    skills: int = 0
    eval_cases: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, strict: bool = False) -> bool:
        return not self.errors and not (strict and self.warnings)


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def validate_catalogue(root: str | None = None, *, hub: str | None = None,
                       flows_path: str | None = None) -> ValidationReport:
    """Full validation: schema, ids, prompts, evals, requires, labels."""
    root = root or skills_root()
    hub = hub or os.path.dirname(os.path.abspath(root))
    report = ValidationReport()
    seen: dict[str, str] = {}
    loaded: dict[str, Skill] = {}
    case_ids: dict[str, str] = {}

    if not os.path.isdir(root):
        report.errors.append(f"{root}: skills directory not found")
        return report

    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and not name.startswith((".", "_")) and not os.path.isfile(os.path.join(d, "skill.yml")):
            report.errors.append(f"{name}: directory has no skill.yml")

    for d in skill_dirs(root):
        name = os.path.basename(d)
        try:
            data = read_manifest(d)
        except SkillError as exc:
            report.errors.append(str(exc))
            continue
        errs = schema.validate_manifest(data)
        if data.get("id") != name:
            errs.append(f"id {data.get('id')!r} does not match directory name {name!r}")
        if errs:
            report.errors.extend(f"{name}: {e}" for e in errs)
            continue
        skill = _from_manifest(data, os.path.abspath(d))
        if skill.id in seen:
            report.errors.append(f"{name}: duplicate id {skill.id!r} (also {seen[skill.id]})")
            continue
        seen[skill.id] = name
        loaded[skill.id] = skill
        report.skills += 1

        # prompt
        ppath = skill.prompt_path
        if ppath:
            if not _inside(ppath, hub):
                report.errors.append(f"{name}: prompt {skill.prompt!r} resolves outside the hub")
            elif not os.path.isfile(ppath):
                report.errors.append(f"{name}: prompt file not found: {skill.prompt}")
            else:
                with open(ppath, encoding="utf-8") as fh:
                    first = fh.readline().strip()
                local = _inside(ppath, skill.directory)
                if skill.untrusted_inputs and first != schema.UNTRUSTED_FIRST_LINE:
                    msg = f"{name}: prompt does not start with {schema.UNTRUSTED_FIRST_LINE!r}"
                    (report.errors if local else report.warnings).append(
                        msg if local else msg + " (shared prompt; render-prompt still prepends the preamble)")

        # evals
        try:
            cases = skill.load_evals()
        except CaseError as exc:
            report.errors.append(f"{name}: {exc}")
            cases = []
        for case in cases:
            if case.id in case_ids:
                report.errors.append(f"{name}: duplicate eval id {case.id!r} (also in {case_ids[case.id]})")
            case_ids[case.id] = case.source
            if case.skill != skill.id:
                report.errors.append(f"{name}: eval {case.id!r} names skill {case.skill!r}")
        report.eval_cases += len(cases)
        if not skill.deterministic and not (schema.MIN_EVALS <= len(cases) <= schema.MAX_EVALS):
            report.errors.append(
                f"{name}: needs {schema.MIN_EVALS}-{schema.MAX_EVALS} eval cases (has {len(cases)})")
        if skill.untrusted_inputs and not skill.deterministic and cases and not any(
                c.kind == "injection" for c in cases):
            report.warnings.append(f"{name}: reads untrusted input but has no injection eval case")

        if skill.deterministic and skill.entrypoint and skill.entrypoint.startswith("python -m "):
            module = skill.entrypoint.split()[2]
            rel = module.replace(".", os.sep)
            src = os.path.join(hub, "src")
            in_tree = (os.path.isfile(os.path.join(src, rel + ".py"))
                       or os.path.isfile(os.path.join(src, rel, "__main__.py")))
            # A hub scaffolded from the wheel has no src/: the module ships installed.
            if not in_tree and not _module_installed(module):
                report.errors.append(f"{name}: entrypoint module {module} not found")

    for skill in loaded.values():
        for req in skill.requires:
            if req not in loaded:
                report.errors.append(f"{skill.id}: requires unknown skill {req!r}")

    known_labels = flows_labels(flows_path or os.path.join(hub, "config", "flows.yml"))
    if known_labels is not None:
        known = known_labels | PENDING_LABELS
        for skill in loaded.values():
            for label in sorted(skill.labels()):
                if label not in known and not label.startswith("project:"):
                    report.warnings.append(f"{skill.id}: label {label!r} is not in config/flows.yml")
    roles = flows_roles(flows_path or os.path.join(hub, "config", "flows.yml"))
    if roles is not None:
        for skill in loaded.values():
            if skill.role and skill.role in roles and skill.badge:
                badge = (roles[skill.role] or {}).get("badge")
                if isinstance(badge, str) and schema.BADGE_RE.match(badge) and badge != skill.badge:
                    report.warnings.append(
                        f"{skill.id}: badge {skill.badge!r} differs from flows.yml roles.{skill.role} {badge!r}")
            elif skill.role and skill.role not in roles:
                report.warnings.append(f"{skill.id}: role {skill.role!r} is not in config/flows.yml roles")

    # deterministic skill configs
    for skill in loaded.values():
        validator = CONFIG_VALIDATORS.get(skill.id)
        if validator is not None:
            if skill.config is None:
                report.errors.append(f"{skill.id}: config block is required")
            else:
                report.errors.extend(f"{skill.id}: config: {e}" for e in validator(skill.config))

    # cross-role golden set and red-team cases
    golden = os.path.join(hub, "evals", "golden")
    for path in case_files(golden):
        rel = os.path.relpath(path, hub)
        try:
            cases = load_case_file(path)
        except CaseError as exc:
            report.errors.append(f"{rel}: {exc}")
            continue
        for case in cases:
            target = loaded.get(case.skill)
            if target is None:
                report.errors.append(f"{rel}: case {case.id!r} targets unknown skill {case.skill!r}")
            elif target.deterministic:
                report.errors.append(f"{rel}: case {case.id!r} targets deterministic skill {case.skill!r}")
            if case.id in case_ids:
                report.errors.append(f"{rel}: duplicate eval id {case.id!r} (also in {case_ids[case.id]})")
            case_ids[case.id] = case.source
        report.eval_cases += len(cases)

    _check_cycles(loaded, report)
    return report


def _check_cycles(skills: dict[str, Skill], report: ValidationReport) -> None:
    state: dict[str, int] = {}

    def visit(sid: str, stack: list[str]) -> None:
        state[sid] = 1
        for req in skills[sid].requires:
            if req not in skills:
                continue
            if state.get(req) == 1:
                cycle = stack[stack.index(req):] + [req] if req in stack else [sid, req]
                report.errors.append("requires cycle: " + " -> ".join(cycle))
            elif state.get(req) is None:
                visit(req, stack + [req])
        state[sid] = 2

    for sid in sorted(skills):
        if state.get(sid) is None:
            visit(sid, [sid])
