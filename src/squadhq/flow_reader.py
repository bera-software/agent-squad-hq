#!/usr/bin/env python3
"""
Load and validate config/flows.yml — the single source of truth for the
label registry, flow state machines, role authority, idempotency rules, and
numeric limits. Every other script/prompt that needs one of these should go
through this module rather than re-parsing the YAML itself.

Public functions:
  load(path="config/flows.yml")            -> the full parsed dict
  get_labels(category=None, path=...)       -> dict of labels (one category,
                                                or the whole labels.* tree
                                                if category is None)
  get_transition(flow, from_state, to_state, path=...) -> transition dict
  get_role(name, path=...)                  -> role dict
  get_flood_guard(name, path=...)           -> flood-guard entry
  validate(path=...)                        -> list[str] of problems found
                                                (empty list == valid)

CLI: `python -m squadhq.flow_reader validate [--strict] [path]` prints
problems (if any) and exits 1, or prints "OK" and exits 0. Warnings (today:
drift between merge_policy.trivial_paths and router.yml's mirrored
`trivial:` filter) are printed but only fail the run with --strict. No
network, no LLM — pure Python + PyYAML, safe to run in CI on every push.

Parsing is cached per (path, mtime), so the many callers in one process
(label guard, sweeper, merge policy) read the file once; every call returns
its own deep copy, so a caller mutating the result cannot poison the cache.
"""
import copy
import os
import sys
from functools import lru_cache

try:
    import yaml
except ImportError:
    print("flow_reader: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    raise

from squadhq.paths import hub_path

# Resolved against the hub checkout (SQUADHQ_HOME), never the cwd: router jobs
# run with the PROJECT checkout as cwd.
DEFAULT_PATH = hub_path("config", "flows.yml")


def default_path():
    """config/flows.yml of the hub, resolved at CALL time (SQUADHQ_HOME may
    be set after import)."""
    return hub_path("config", "flows.yml")

# labels.report_tags_not_real_labels is intentionally excluded from the set
# of "real" registry labels a transition may reference — see the comment in
# flows.yml itself. Every other labels.* category is a real GitHub label.
_REAL_LABEL_CATEGORIES_EXCLUDED = {"report_tags_not_real_labels"}


@lru_cache(maxsize=16)
def _load_cached(path, mtime_ns, size):
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: did not parse to a mapping")
    return data


def load(path=None):
    path = os.path.abspath(path or default_path())
    st = os.stat(path)
    return copy.deepcopy(_load_cached(path, st.st_mtime_ns, st.st_size))


def _all_real_labels(data):
    """Flat set of every label name that is a real GitHub label (i.e. every
    labels.* category except report_tags_not_real_labels)."""
    names = set()
    for category, entries in data.get("labels", {}).items():
        if category in _REAL_LABEL_CATEGORIES_EXCLUDED:
            continue
        if isinstance(entries, dict):
            names.update(entries.keys())
    return names


def get_labels(category=None, path=None):
    """Return the labels dict for one category (e.g. "dept", "priority"),
    or the entire labels.* tree (all categories) if category is None."""
    data = load(path)
    labels = data.get("labels", {})
    if category is None:
        return labels
    if category not in labels:
        raise KeyError(f"no such label category {category!r}; have: {sorted(labels.keys())}")
    return labels[category]


def get_transition(flow, from_state, to_state, path=None):
    """Return the transition dict for flow's from_state -> to_state edge.
    `from` in flows.yml may be a single state name or a list of them (a
    transition reachable from several prior states) — both match."""
    data = load(path)
    flows = data.get("flows", {})
    if flow not in flows:
        raise KeyError(f"no such flow {flow!r}; have: {sorted(flows.keys())}")
    for t in flows[flow].get("transitions", []):
        froms = t.get("from")
        froms = froms if isinstance(froms, list) else [froms]
        if from_state in froms and t.get("to") == to_state:
            return t
    raise KeyError(f"no transition {from_state!r} -> {to_state!r} in flow {flow!r}")


def get_role(name, path=None):
    data = load(path)
    roles = data.get("roles", {})
    if name not in roles:
        raise KeyError(f"no such role {name!r}; have: {sorted(roles.keys())}")
    return roles[name]


def get_flood_guard(name, path=None):
    data = load(path)
    guards = data.get("flood-guards", {})
    if name not in guards:
        raise KeyError(f"no such flood-guard {name!r}; have: {sorted(guards.keys())}")
    return guards[name]


# Categories excluded from "the labels a project repo gets created with":
# `dynamic` (project:<name> is created per-project in the HUB repo, not per
# static label in a project repo), `hub_only` (real labels, but they belong on
# the hub and nowhere else — created by onboard.py's apply() on the HUB, same
# as project:<name>), and `report_tags_not_real_labels` (never real GitHub
# labels at all — see the comment in flows.yml).
_STATIC_LABEL_EXCLUDED_CATEGORIES = {"dynamic", "hub_only", "report_tags_not_real_labels"}


def static_label_names(path=None):
    """Every (name, color) pair scripts/onboard.py should `gh label create`
    on a project repo, in registry order. onboard.py imports and calls this
    directly — it has no label-flattening logic of its own, this function
    IS the one source (task requirement: "the registry IS the label list
    now"). Colors default to "ededed" (GitHub's own neutral gray) if a
    registry entry omits one."""
    data = load(path)
    out = []
    for category, entries in data.get("labels", {}).items():
        if category in _STATIC_LABEL_EXCLUDED_CATEGORIES:
            continue
        for name, meta in (entries or {}).items():
            out.append((name, (meta or {}).get("color", "ededed")))
    return out


def get_label_states(kind="issue", path=None):
    """label_states.<kind> ("issue" or "pr") from config/flows.yml
    (state_labels, exclusive, modifiers, combos, repairs)."""
    return load(path)["label_states"][kind]


def match_repair(labels, cfg):
    """First repair in cfg["repairs"] whose every group has a label present
    and none of whose `unless` labels is present; None otherwise."""
    labels = set(labels)
    for r in cfg.get("repairs") or []:
        if all(labels & set(g) for g in r["groups"]) and not labels & set(r.get("unless") or []):
            return r
    return None


def apply_repair(labels, repair):
    """Label set after a non-escalating repair."""
    return (set(labels) - set(repair.get("remove") or [])) | set(repair.get("add") or [])


def exclusive_violations(labels, cfg):
    """Exclusive groups with more than one label present -> [sorted labels]."""
    labels = set(labels)
    return [sorted(labels & set(g)) for g in cfg.get("exclusive") or [] if len(labels & set(g)) > 1]


def resolve_state(labels, cfg):
    """-> (combo, modifiers_stripped) for an open issue's labels, or
    (None, stripped) when the state-label set is illegal. Only labels in
    cfg["state_labels"] count; modifiers are stripped one at a time, in
    order, until a combo matches (see the LABEL STATES banner). A violated
    `exclusive` group makes the set illegal whatever the state labels say."""
    if exclusive_violations(labels, cfg):
        return None, []
    present = set(labels) & set(cfg["state_labels"])
    combos = {frozenset(c["labels"]): c for c in cfg["combos"]}
    stripped = []
    if frozenset(present) in combos:
        return combos[frozenset(present)], stripped
    for m in cfg.get("modifiers") or []:
        if m["label"] in present:
            present.discard(m["label"])
            stripped.append(m["label"])
            if frozenset(present) in combos:
                return combos[frozenset(present)], stripped
    return None, stripped


def _validate_label_states(data):
    problems = []
    ls = data.get("label_states")
    if not isinstance(ls, dict) or "issue" not in ls:
        return ["missing label_states.issue (see the LABEL STATES banner)"]
    real = _all_real_labels(data)
    flows = data.get("flows", {})
    for kind, cfg in ls.items():
        loc = f"label_states.{kind}"
        state_labels = set(cfg.get("state_labels") or [])
        for l in state_labels - real:
            problems.append(f"{loc}.state_labels: unknown label {l!r}")
        for g in cfg.get("exclusive") or []:
            if len(g) < 2:
                problems.append(f"{loc}.exclusive: group {g!r} needs at least two labels")
            for l in set(g) - real:
                problems.append(f"{loc}.exclusive: unknown label {l!r}")
            if set(g) & state_labels:
                problems.append(f"{loc}.exclusive: {sorted(set(g) & state_labels)} are state labels — "
                                "combos already make state labels exclusive")
        for m in cfg.get("modifiers") or []:
            if m.get("label") not in state_labels:
                problems.append(f"{loc}.modifiers: {m.get('label')!r} is not in state_labels")
        seen = {}
        for i, c in enumerate(cfg.get("combos") or []):
            cl = f"{loc}.combos[{i}]"
            for l in set(c.get("labels") or []) - state_labels:
                problems.append(f"{cl}: {l!r} is not in state_labels")
            key = frozenset(c.get("labels") or [])
            if key in seen:
                problems.append(f"{cl}: same label set as combos[{seen[key]}]")
            seen[key] = i
            flow, _, state = str(c.get("state", "")).partition(".")
            if flow not in flows or state not in (flows[flow].get("states") or []):
                problems.append(f"{cl}: state {c.get('state')!r} is not a declared flows.<flow>.<state>")
        for i, r in enumerate(cfg.get("repairs") or []):
            rl = f"{loc}.repairs[{i}]"
            mentioned = {l for g in r.get("groups") or [] for l in g}
            mentioned |= set(r.get("unless") or []) | set(r.get("remove") or []) | set(r.get("add") or [])
            for l in mentioned - real:
                problems.append(f"{rl}: unknown label {l!r}")
            if not r.get("groups") or not r.get("reason"):
                problems.append(f"{rl}: needs groups and reason")
            if r.get("escalate"):
                continue
            if not (r.get("remove") or r.get("add")):
                problems.append(f"{rl}: neither escalates nor changes any label")
                continue
            # The minimal set that triggers it (first label of each group)
            # must land on a legal combo once repaired.
            trigger = {g[0] for g in r["groups"]}
            combo, _ = resolve_state(apply_repair(trigger, r), cfg)
            if combo is None:
                problems.append(f"{rl}: repairing {sorted(trigger)} gives "
                                f"{sorted(apply_repair(trigger, r) & state_labels)}, which is not a legal combo")
    return problems


def validate(path=None, strict=False):
    """Returns a list of problem strings. Empty list means valid. Never
    raises for a structurally-sound-but-semantically-wrong file — every
    check below degrades to an appended problem string instead. With
    strict=True, warnings() count as problems too."""
    path = path or default_path()
    problems = _validate(path)
    if strict:
        problems += warnings(path)
    return problems


def warnings(path=None):
    """Non-fatal findings. Mirror drift between merge_policy.trivial_paths
    and router.yml is fail-SAFE (merge_policy.py's all_files_match re-checks
    the authoritative list before any trivial merge, so a wider router filter
    can only strand a PR unreviewed-and-unmerged, never merge it), so it is a
    warning by default and an error under --strict."""
    try:
        data = load(path)
    except Exception:
        return []
    return _validate_trivial_paths_mirror(data, path)


def _validate(path):
    problems = []

    try:
        data = load(path)
    except Exception as e:
        return [f"{path}: failed to load/parse: {e!r}"]

    for top in ("meta", "labels", "flows", "roles", "idempotency", "flood-guards",
                "merge_policy"):
        if top not in data:
            problems.append(f"missing top-level key: {top!r}")
    if problems:
        return problems  # nothing else is safe to check without these

    real_labels = _all_real_labels(data)
    idempotency_names = set(data["idempotency"].keys())

    # Every label referenced in a transition's labels_add/labels_remove
    # exists in the registry (excluding report_tags_not_real_labels, which
    # are deliberately not real labels and must never appear here).
    for flow_name, flow in data["flows"].items():
        declared_states = set(flow.get("states", []))
        transitions = flow.get("transitions", [])
        seen_states = set()

        for i, t in enumerate(transitions):
            loc = f"flows.{flow_name}.transitions[{i}]"

            froms = t.get("from")
            froms = froms if isinstance(froms, list) else ([froms] if froms is not None else [])
            to = t.get("to")

            if not froms:
                problems.append(f"{loc}: missing 'from'")
            if not to:
                problems.append(f"{loc}: missing 'to'")

            for s in froms:
                seen_states.add(s)
                if s not in declared_states:
                    problems.append(
                        f"{loc}: from-state {s!r} not declared in {flow_name}.states"
                    )
            if to is not None:
                seen_states.add(to)
                if to not in declared_states:
                    problems.append(
                        f"{loc}: to-state {to!r} not declared in {flow_name}.states"
                    )

            for field in ("labels_add", "labels_remove"):
                for label in t.get(field, []) or []:
                    if label not in real_labels:
                        problems.append(
                            f"{loc}: {field} references unknown label {label!r}"
                        )

            actor = t.get("actor")
            if actor and isinstance(actor, str):
                # actor is sometimes a job name (e.g. "po-continue",
                # "review-fix-job") rather than a roles.* persona — only
                # flag it if it looks like it was meant to be a role
                # (single lowercase-hyphen token matching an existing
                # near-miss) and isn't found anywhere. Job-name actors are
                # legitimate and not required to be in roles.*.
                pass  # intentionally not validated — see docstring; roles
                       # are validated on their own below, not via actor refs

        # Orphan-state check: every declared state should appear in at
        # least one transition (as a from or a to) — a state nobody ever
        # transitions into or out of is dead documentation.
        for s in declared_states:
            if s not in seen_states:
                problems.append(f"flows.{flow_name}.states: {s!r} is declared but never used in a transition")

    # Every role referenced by name from idempotency lists on roles exists;
    # every idempotency name a role lists exists in idempotency.*.
    for role_name, role in data["roles"].items():
        for idem_name in role.get("idempotency", []) or []:
            if idem_name not in idempotency_names:
                problems.append(
                    f"roles.{role_name}.idempotency references unknown rule {idem_name!r}"
                )

    # scripts/onboard.py's static_label_names() output (what it actually
    # creates on every project repo) must be exactly "every real label except
    # the non-project-scoped categories" — catches a bug in the flattening
    # logic itself, not just a hand-maintained-list drift (onboard.py has no
    # label list of its own any more, see static_label_names()'s docstring).
    # Derived from _STATIC_LABEL_EXCLUDED_CATEGORIES rather than naming
    # `dynamic` directly, so adding a new non-project category (e.g. hub_only)
    # can't leave this check asserting the old expectation.
    excluded_names = set()
    for category in _STATIC_LABEL_EXCLUDED_CATEGORIES:
        excluded_names.update((data["labels"].get(category) or {}).keys())
    expected_static = real_labels - excluded_names
    actual_static = {name for name, _color in static_label_names(path)}
    if actual_static != expected_static:
        missing = expected_static - actual_static
        extra = actual_static - expected_static
        problems.append(
            "static_label_names() (what onboard.py creates) does not match the "
            f"registry minus {sorted(_STATIC_LABEL_EXCLUDED_CATEGORIES)}: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )

    problems.extend(_validate_trivial_lanes(data))
    problems.extend(_validate_label_ownership(data))
    problems.extend(_validate_label_states(data))

    return problems


# applied_by may also name these actors, which have no roles.* entry: the
# sweeper is code, architect-quarterly shares the architect role's prompt
# family but runs as its own workflow with its own guard role.
_LABEL_EXTRA_ACTORS = {"any", "stuck-sweeper", "architect-quarterly"}


def _validate_label_ownership(data):
    """Every real label must declare applied_by, naming only known actors —
    a label with no owner entry would be silently unguardable."""
    problems = []
    groups = data.get("label_role_groups") or {}
    known = set(data.get("roles", {})) | set(groups) | _LABEL_EXTRA_ACTORS
    for g, members in groups.items():
        for m in members or []:
            if m not in known:
                problems.append(f"label_role_groups.{g}: unknown role {m!r}")
    for category, entries in data.get("labels", {}).items():
        if category in _REAL_LABEL_CATEGORIES_EXCLUDED or not isinstance(entries, dict):
            continue
        for name, meta in entries.items():
            owners = (meta or {}).get("applied_by")
            if owners is None:
                problems.append(f"labels.{category}.{name!r}: missing applied_by (label ownership)")
                continue
            for o in owners:
                if o not in known:
                    problems.append(f"labels.{category}.{name!r}: applied_by names unknown role {o!r}")
    return problems


def label_owners(path=None):
    """{label: set of roles allowed to add it}, groups expanded; "any" kept
    as a literal member. Dynamic "project:<name>" is keyed as "project:*"."""
    data = load(path)
    groups = data.get("label_role_groups") or {}
    out = {}
    for category, entries in data.get("labels", {}).items():
        if category in _REAL_LABEL_CATEGORIES_EXCLUDED or not isinstance(entries, dict):
            continue
        for name, meta in entries.items():
            roles = set()
            for o in (meta or {}).get("applied_by") or []:
                roles |= set(groups.get(o, [o]))
            out["project:*" if name == "project:<name>" else name] = roles
    return out


ROUTER_PATH = hub_path(".github", "workflows", "router.yml")


def _router_trivial_filter(router_path=None):
    """The pattern list from detect-changes' dorny/paths-filter `trivial:`
    filter in router.yml, or None if it can't be located.

    paths-filter's `filters:` is a YAML document embedded as a STRING inside
    router.yml, so this parses router.yml, pulls that string out, and parses it
    again. Returns None (not []) when anything is missing, so the caller can
    tell "no trivial filter found" apart from "the filter is empty".

    Resolves ROUTER_PATH at CALL time, not as a default argument: a default
    would bind at import and make the module-level constant unoverridable
    (which is how the drift test points this at a mutated copy)."""
    router_path = router_path or ROUTER_PATH
    try:
        router = yaml.safe_load(open(router_path))
        steps = ((router.get("jobs") or {}).get("detect-changes") or {}).get("steps") or []
        for st in steps:
            filters = ((st or {}).get("with") or {}).get("filters")
            if not filters:
                continue
            parsed = yaml.safe_load(filters)
            if isinstance(parsed, dict) and "trivial" in parsed:
                return list(parsed["trivial"] or [])
    except Exception:
        return None
    return None


def _validate_trivial_lanes(data):
    """merge_policy.trivial_lanes (branch-scoped trivial paths) must be
    well-formed, and trivial_paths must be exactly the union of their paths —
    otherwise the mirrored router filter and the evaluator disagree."""
    mp = data.get("merge_policy") or {}
    lanes = mp.get("trivial_lanes")
    if lanes is None:
        return []
    problems, union = [], []
    for i, ln in enumerate(lanes if isinstance(lanes, list) else []):
        if not isinstance(ln, dict) or not ln.get("head_branch") or not ln.get("paths"):
            problems.append(f"merge_policy.trivial_lanes[{i}]: needs head_branch and paths")
            continue
        for p in ln["paths"]:
            if "*" in p:
                problems.append(f"merge_policy.trivial_lanes[{i}]: {p!r} — trivial paths are exact files, no globs")
            if p not in union:
                union.append(p)
    if not isinstance(lanes, list):
        problems.append("merge_policy.trivial_lanes must be a list")
    declared = mp.get("trivial_paths") or []
    if set(declared) != set(union):
        problems.append(f"merge_policy.trivial_paths {sorted(declared)} is not the union of "
                        f"trivial_lanes paths {sorted(union)}")
    return problems


def _validate_trivial_paths_mirror(data, path):
    """merge_policy.trivial_paths is the source of truth for what skips review.
    router.yml's detect-changes `trivial:` filter is a MIRROR of it — it has to
    be, because dorny/paths-filter patterns must be literals in the workflow
    file and that job has no hub checkout.

    Drift is reported by warnings() (and fails `validate --strict`): it is
    fail-safe because merge_policy.py's all_files_match re-checks the
    authoritative list before any trivial merge, so a wider router filter can
    strand a PR but never merge an unreviewed non-trivial file."""
    problems = []
    declared = (data.get("merge_policy") or {}).get("trivial_paths")
    mirrored = _router_trivial_filter()

    if declared is None:
        problems.append("merge_policy.trivial_paths is missing — router.yml's "
                        "detect-changes `trivial:` filter has nothing to be checked against")
        return problems
    if mirrored is None:
        # Not a failure on its own: flow_reader is importable from contexts
        # with no router.yml on disk (a project repo checkout, a packaged
        # copy). Only report when the file exists but the filter doesn't.
        if os.path.exists(ROUTER_PATH):
            problems.append("router.yml has no detect-changes `trivial:` paths-filter entry, "
                            "but merge_policy.trivial_paths is declared — the trivial-merge "
                            "gate would never fire")
        return problems

    if list(declared) != list(mirrored):
        only_flows = [p for p in declared if p not in mirrored]
        only_router = [p for p in mirrored if p not in declared]
        detail = []
        if only_flows:
            detail.append(f"in flows.yml but not router.yml: {only_flows}")
        if only_router:
            detail.append(f"in router.yml but not flows.yml: {only_router}")
        if not detail:
            detail.append(f"same entries, different order: flows.yml={list(declared)} "
                          f"router.yml={list(mirrored)}")
        problems.append(
            "merge_policy.trivial_paths and router.yml's detect-changes `trivial:` "
            "filter have drifted — " + "; ".join(detail))
    return problems


def context_slice_for_role(role_name, path=None):
    """A compact, JSON-serializable slice of flows.yml for injecting into an
    agent_runner.py hub-loop agent's per-run context.

    Those agents (cos/brief/growth/seo/legal/finance/rollup) get ONLY a
    system prompt string + a JSON state blob per run — no Read/Bash tool
    access, unlike the claude-code-action agents (product-owner, design,
    implement, review, etc.) that can open config/flows.yml directly. A
    bare "see config/flows.yml" instruction in one of these prompts would
    be inert text to a model that cannot read files — scripts/agent_runner.py
    calls this function and injects the result as ctx["flows"] instead, so
    the actual label/authority/limit data reaches the model as data on
    every run, not just as a maintainer-facing doc pointer.

    Drops label colors/sources (irrelevant to which string the model should
    type) and the `deprecated` label category (nothing should reference
    those). Keeps everything else the role needs to act correctly.
    """
    data = load(path)
    role = get_role(role_name, path)
    idem_names = role.get("idempotency", []) or []
    idem_rules = {
        n: (data["idempotency"][n].get("rule", "") or "").strip()
        for n in idem_names if n in data["idempotency"]
    }

    labels = {}
    for category, entries in data.get("labels", {}).items():
        if category == "deprecated":
            continue
        labels[category] = {
            name: ((meta or {}).get("description", "") or "").strip()
            for name, meta in (entries or {}).items()
        }

    return {
        "role": {
            "badge": role.get("badge"),
            "authority": role.get("authority"),
            "hard_limits": role.get("hard_limits"),
        },
        "idempotency_rules": idem_rules,
        "labels": labels,
    }


def _parser():
    import argparse

    ap = argparse.ArgumentParser(prog="python -m squadhq.flow_reader",
                                 description="Validate config/flows.yml (labels, flows, roles, merge policy).")
    sub = ap.add_subparsers(dest="cmd", metavar="validate")
    v = sub.add_parser("validate", help="check flows.yml; exit 1 on problems")
    v.add_argument("--strict", action="store_true", help="treat warnings (router trivial-filter drift) as errors")
    v.add_argument("path", nargs="?", help="flows.yml to check (default: the hub's config/flows.yml)")
    return ap


def _cli(argv=None):
    ap = _parser()
    try:
        args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:  # -h -> 0, bad usage -> 2
        return int(e.code or 0)
    if args.cmd != "validate":
        ap.print_usage(sys.stderr)
        return 2
    path = args.path or default_path()
    problems = validate(path, strict=args.strict)
    warns = [] if args.strict else warnings(path)
    for w in warns:
        print(f"flow_reader validate: warning: {w}")
    if problems:
        print(f"flow_reader validate: {len(problems)} problem(s) in {path}:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"flow_reader validate: OK ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
