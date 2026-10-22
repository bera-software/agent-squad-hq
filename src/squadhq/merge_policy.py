#!/usr/bin/env python3
"""
Merge-policy evaluator: decides whether an open PR may merge without a human,
per the `merge_policy` section of config/flows.yml.

Two tiers, evaluated by name:

  auto_merge     — the Reviewer already APPROVED (that verdict is the
                   "ready-to-merge" label that fires router.yml's `self-merge`
                   job). This tier asks whether that approval is enough to skip
                   the approver's click: risk:low AND a deterministic path
                   floor of low (squadhq.policy.risk), no failing check,
                   bot-authored. Disabled by default in flows.yml.

  trivial_merge  — no review happened at all. The PR contains ONLY generated or
                   deterministic files, so the file-path classification IS the
                   gate. Used by router.yml's `trivial-merge` job, which also
                   applies the "ready-to-merge" label itself.

FAILS CLOSED, always. Every condition that cannot be EVALUATED counts as NOT
MET: a missing label, a failed API call, an unparseable flows.yml, or a
condition key present in flows.yml with no evaluator in this file. Adding a
condition to flows.yml without adding an evaluator here disables the tier
rather than silently widening it — that direction is deliberate.

Usage (from a workflow step):

    python -m squadhq.merge_policy <tier> >> "$GITHUB_OUTPUT"

Emits exactly two lines, in $GITHUB_OUTPUT's key=value form:

    auto_merge=true|false
    reason=<one line; empty when auto_merge=true>

`reason` names the FIRST failing condition in approver-readable terms, so a skip
is never silent. It is single-line by construction (whitespace collapsed) and
must be passed to a shell through `env:`, never spliced in with `${{ }}` — it
can quote a label name, a PR author login, or a check name, all of which are
writable by anyone with push access to the repo.

Required env: REPO (owner/name), PR (number), FLOWS (path to flows.yml;
defaults to the hub checkout's config/flows.yml). Optional: DC_CODE /
DC_BACKEND / DC_FRONTEND ("true"/"false" from the detect-changes job; DC_CODE
is the stack-neutral flag, the other two are accepted for compatibility — the authoritative
change-scope source; when both are empty the file list is fetched and
classified here instead), RISK_EXTRA_GLOBS (newline- or comma-separated
globs that force high risk — the selected packs' risk_paths). GH_TOKEN must
be set for the gh CLI.
"""
import json
import os
import re
import sys

from squadhq import github, settings, untrusted
from squadhq.paths import hub_path
from squadhq.policy import risk as risk_policy

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by the workflow's pip step
    print("merge_policy: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    yaml = None  # type: ignore[assignment]


DEFAULT_FLOWS = os.path.join("config", "flows.yml")

# squadhq.yml policies.merge.always_human area -> squadhq.policy.risk category.
ALWAYS_HUMAN_CATEGORIES = {
    "auth": "security-sensitive code",
    "payments": "security-sensitive code",
    "migrations": "schema migration",
    "workflows": "CI/CD definition",
    "infra": "infrastructure-as-code / container",
    "deps-major": "dependency manifest / lockfile",
    "secrets": "secrets / environment config",
}


def project_merge(repo, projects_path=None):
    """The ``merge`` block rendered into config/projects.json for ``repo``
    (``{}`` when absent: legacy projects keep flows.yml behaviour)."""
    try:
        with open(projects_path or hub_path("config", "projects.json")) as fh:
            projects = (json.load(fh) or {}).get("projects", []) or []
    except (OSError, ValueError, AttributeError):
        return {}
    p = next((p for p in projects if isinstance(p, dict) and str(p.get("repo", "")).lower() == repo.lower()), {})
    m = p.get("merge")
    return m if isinstance(m, dict) else {}


def apply_project_merge(root, merge):
    """Overlay squadhq.yml's merge policy (rendered per project) on flows.yml's
    ``merge_policy`` so the three modes behave differently:

    * ``approvers``  -> auto_merge disabled: an approver always merges.
    * ``risk_based`` -> auto_merge enabled with risk_tag/final_risk = max_risk
      and an ``always_human`` condition; every other flows.yml condition stays.
    * ``auto`` / no block -> flows.yml unchanged (router merges "auto" itself).
    """
    mode = (merge or {}).get("mode")
    if mode not in ("approvers", "risk_based"):
        return root
    root = dict(root or {})
    tier = dict(root.get("auto_merge") or {})
    if mode == "approvers":
        tier["enabled"] = False
        root["auto_merge"] = tier
        return root
    max_risk = ((merge.get("risk_based") or {}).get("max_risk")) or "low"
    reqs = []
    for entry in tier.get("requires_all") or [{"review_verdict": "approve"}, {"ci_checks": "passing"},
                                              {"pr_author_type": "bot"}]:
        entry = dict(entry or {})
        for key in ("risk_tag", "final_risk"):
            if key in entry:
                entry[key] = max_risk
        reqs.append(entry)
    for key in ("risk_tag", "final_risk"):
        if not any(key in e for e in reqs):
            reqs.append({key: max_risk})
    if not any("ci_checks" in e for e in reqs):
        reqs.append({"ci_checks": "passing"})
    reqs.append({"always_human": list(merge.get("always_human") or [])})
    tier.update(enabled=True, requires_all=reqs)
    root["auto_merge"] = tier
    return root

# Mirrors detect-changes' dorny/paths-filter `backend` list in router.yml. Only
# used on the fallback path (detect-changes outputs absent); deliberately coarse,
# every ambiguity resolves toward "production code".
BACKEND_PATTERNS = [
    "backend/**", "src/**", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gradlew", "gradlew.bat",
    "gradle/**", "**/*.java", "**/*.kt", "**/*.properties", "*.yml", "*.yaml",
]


def glob_to_regex(pattern):
    """picomatch-ish subset, enough for the path lists this file matches:
    `**/` spans zero or more directories, `**` spans anything, `*` stops at a
    separator. Anchored at both ends."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def matches_any(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


def gh_json(args, default=None):
    return github.client().json(args, default=default)


def gh_lines(args, default=None):
    """For `gh api --paginate --jq`: each page is emitted separately, so the
    output is NOT one JSON document. Ask for newline-delimited scalars and read
    them as lines instead of parsing."""
    return github.client().lines(args, default=default)


_UNSET = object()


VERDICT_MARKER_RE = re.compile(
    r"<!-- squadhq:verdict sha=([0-9a-f]{40}) verdict=(approve|request-changes) risk=(low|medium|high) -->")


def verdict_provenance(events, comments, head_sha, ops_logins, labels):
    """Was the approve verdict on the CURRENT head recorded by the ops identity?

    Pure function over GitHub data (unit-tested with fixtures):
      events    issue events (``repos/{r}/issues/{n}/events``): dicts with
                ``event``, ``label.name``, ``actor.login``, ``created_at``
      comments  issue comments: ``user.login``, ``body``, ``created_at``
      head_sha  the PR's current head commit
      ops_logins the ops identity's logins (settings.bot_logins())
      labels    the PR's current label names

    Holds when (1) the latest ``ready-to-merge`` labeled event and the latest
    labeled event of every current ``risk:*`` label were made by ops, (2) an
    ops-authored ``<!-- squadhq:verdict sha=<head_sha> verdict=approve -->``
    marker exists (review-post writes it for the head it evaluated, so it
    post-dates that head's push), its risk equals the current risk label, and
    (3) the ``ready-to-merge`` event is not older than that marker. An agent
    writing labels through the REST API therefore cannot satisfy it.
    Returns (ok, reason)."""
    ops = {str(x) for x in ops_logins if x}
    if not head_sha or not re.fullmatch(r"[0-9a-f]{40}", str(head_sha)):
        return False, "PR head commit unknown"

    def latest(name):
        hits = [e for e in events or [] if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == name]
        return max(hits, key=lambda e: e.get("created_at") or "") if hits else None

    def by_ops(who):
        # Exact [bot] login AND a Bot account: a human named like the App's slug never counts.
        who = who or {}
        return who.get("login") in ops and who.get("type", "Bot") == "Bot" and str(who.get("login", "")).endswith("[bot]")

    rtm = latest("ready-to-merge")
    if rtm is None or not by_ops(rtm.get("actor")):
        return False, "ready-to-merge was not added by the ops identity"
    risks = [n for n in labels or [] if n.startswith("risk:")]
    for name in risks:
        ev = latest(name)
        if ev is None or not by_ops(ev.get("actor")):
            return False, f"{name} was not added by the ops identity"
    markers = []
    for c in comments or []:
        if not by_ops(c.get("user")):
            continue
        for m in VERDICT_MARKER_RE.finditer(c.get("body") or ""):
            if m.group(1) == head_sha and m.group(2) == "approve":
                markers.append((c.get("created_at") or "", m.group(3)))
    if not markers:
        return False, f"no ops-recorded approve verdict for the current head {head_sha[:7]}"
    at, risk = max(markers)
    if len(risks) != 1 or risks[0] != f"risk:{risk}":
        return False, f"risk label {risks or 'missing'} does not match the recorded verdict risk:{risk}"
    if (rtm.get("created_at") or "") < at:
        return False, "ready-to-merge predates the recorded verdict for the current head"
    return True, ""


class Evaluator:
    """Holds the per-PR facts every condition draws on, fetched once."""

    def __init__(self, repo, pr, policy_root, dc_backend="", dc_frontend="",
                 pr_data=_UNSET, files=None, dc_code=""):
        self.repo = repo
        self.pr = pr
        self.root = policy_root or {}
        self.dc_backend = dc_backend
        self.dc_frontend = dc_frontend
        self.dc_code = dc_code
        # pr_data/files are injectable so tests can drive every branch without
        # a network round-trip. _UNSET (not None) is the sentinel: None is a
        # meaningful value here — it means "the API call failed", which
        # decide() turns into a fail-closed.
        self.pr_data = self._fetch_pr() if pr_data is _UNSET else pr_data
        self._files = None if files is None else list(files)

    def _fetch_pr(self):
        return gh_json(["pr", "view", self.pr, "-R", self.repo, "--json",
                        "labels,author,statusCheckRollup,isDraft,baseRefName,headRefName,headRefOid,title"])

    def provenance(self):
        if not hasattr(self, "_prov"):
            events = gh_lines(["api", f"repos/{self.repo}/issues/{self.pr}/events", "--paginate",
                               "--jq", ".[] | tojson"])
            comments = gh_lines(["api", f"repos/{self.repo}/issues/{self.pr}/comments", "--paginate",
                                 "--jq", ".[] | tojson"])
            if events is None or comments is None:
                self._prov = (False, "could not read the PR's label events or comments — failing closed")
            else:
                self._prov = verdict_provenance([json.loads(e) for e in events], [json.loads(c) for c in comments],
                                                (self.pr_data or {}).get("headRefOid"), settings.bot_logins(),
                                                self.label_names())
        return self._prov

    # ---- lazily-fetched facts -------------------------------------------

    def files(self):
        if self._files is None:
            self._files = gh_lines(
                ["api", f"repos/{self.repo}/pulls/{self.pr}/files", "--paginate",
                 "--jq", ".[].filename"])
        return self._files

    def label_names(self):
        return [l["name"] for l in (self.pr_data.get("labels") or [])]

    # ---- condition evaluators. Each returns (met, reason). `reason` is read
    # only when met is False, and must name the failure in approver-readable
    # terms rather than restating the condition.

    def c_review_verdict(self, expected):
        # The Reviewer's APPROVE verdict IS the label that fired the calling
        # job. Re-read it rather than trusting the event payload: a REQUEST
        # CHANGES round between the event and this evaluation removes the
        # label (prompts/review.md), and that removal must win.
        if expected != "approve":
            return False, f"policy asks for review_verdict '{expected}', only 'approve' is implemented"
        gate = (self.root.get("auto_merge") or {}).get("label", "ready-to-merge")
        names = self.label_names()
        if gate not in names:
            return False, f"'{gate}' is no longer on the PR — the verdict changed after the label fired"
        if "review:changes-requested" in names:
            return False, "PR carries review:changes-requested"
        # The labels must come from the ops identity, for this head — an agent
        # token can write labels through the REST API (label_guard is advisory).
        return self.provenance()

    def c_risk_tag(self, expected):
        # Exactly one risk:* label, at or below `expected` (risk_based max_risk
        # may be medium/high; flows.yml's default is low).
        risks = [n for n in self.label_names() if n.startswith("risk:")]
        if not risks:
            return False, "no risk:* label — the Reviewer did not classify risk"
        want = risk_policy.normalize_risk(expected)
        got = risk_policy.normalize_risk(risks[0][len("risk:"):]) if len(risks) == 1 else None
        if want is None or got is None or risk_policy.max_risk(got, want) != want:
            return False, f"risk is {', '.join(sorted(risks))}, policy needs risk:{expected} or lower"
        return True, ""

    def c_always_human(self, areas):
        # squadhq.yml policies.merge.always_human: any touched path in one of
        # these risk categories keeps the PR for a human. Unreadable files ->
        # fail closed.
        cats = {ALWAYS_HUMAN_CATEGORIES[a] for a in areas or [] if a in ALWAYS_HUMAN_CATEGORIES}
        if not cats:
            return True, ""
        files = self.files()
        if files is None:
            return False, "could not list the PR's changed files"
        hits = [h for h in risk_policy.explain(files, self.extra_risk_globs()) if h["category"] in cats]
        if hits:
            return False, (f"{hits[0]['file']} is {hits[0]['category']}, which policies.merge.always_human "
                           "keeps for an approver")
        return True, ""

    def extra_risk_globs(self):
        globs = list(self.root.get("risk_paths") or [])
        env = os.environ.get("RISK_EXTRA_GLOBS", "")
        globs += [g.strip() for g in re.split(r"[\n,]", env) if g.strip()]
        return globs

    def c_final_risk(self, expected):
        # max(the Reviewer's risk:* label, the deterministic path floor). A
        # model can raise risk, never lower it below what the touched paths
        # imply (squadhq.policy.risk). Unreadable files -> floor high.
        want = risk_policy.normalize_risk(expected)
        if want is None:
            return False, f"policy asks for final_risk '{expected}', which is not a risk level"
        risks = [n for n in self.label_names() if n.startswith("risk:")]
        llm = risks[0] if len(risks) == 1 else None
        files = self.files()
        extra = self.extra_risk_globs()
        final = risk_policy.final_risk(llm, files, extra)
        if risk_policy.max_risk(final, want) == want:
            return True, ""
        fl = risk_policy.floor(files, extra)
        if files is None:
            detail = "could not list the PR's changed files"
        else:
            hits = risk_policy.explain(files, extra)
            detail = (f"{hits[0]['file']} is {hits[0]['category']}" + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else "")
                      if hits else "no risk path touched")
        return False, (f"final risk is {final} (label {llm or 'missing/ambiguous'}, path floor {fl}: {detail}), "
                       f"policy needs {want}")

    def c_ci_checks(self, expected):
        # PENDING/QUEUED/IN_PROGRESS is NOT a failure here: the caller merges
        # with `gh pr merge --auto`, so GitHub's own auto-merge queue holds the
        # PR until every REQUIRED check passes and drops it if one fails. This
        # check refuses a PR whose checks have ALREADY failed; it does not
        # re-implement branch protection.
        if expected != "passing":
            return False, f"policy asks for ci_checks '{expected}', only 'passing' is implemented"
        rollup = self.pr_data.get("statusCheckRollup")
        if rollup is None:
            return False, "could not read the check rollup"
        bad = []
        for c in rollup:
            concl = (c.get("conclusion") or c.get("state") or "").upper()
            if concl in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT",
                         "ACTION_REQUIRED", "STARTUP_FAILURE"):
                bad.append(f"{c.get('name') or c.get('context')}={concl.lower()}")
        if bad:
            return False, "failing checks: " + ", ".join(sorted(bad))
        return True, ""

    def c_change_scope_excludes(self, excluded):
        # detect-changes is the source of truth. It fails OPEN on an
        # indeterminate diff (backend=frontend=true), which arrives here as
        # "touches production code" — exactly the closed-fail we want.
        if self.dc_backend or self.dc_frontend or self.dc_code:
            # Any value other than an explicit "false" counts as touched
            # (fail closed). DC_CODE is the stack-neutral single flag; it
            # stands in for whichever of backend/frontend is not given.
            def touched(v):
                return v != "false"
            code = touched(self.dc_code) if self.dc_code else None
            be = touched(self.dc_backend) if self.dc_backend else bool(code)
            fe = touched(self.dc_frontend) if self.dc_frontend else bool(code)
            scope = {"backend": be, "frontend": fe, "code": code if code is not None else (be or fe)}
        else:
            files = self.files()
            if files is None:
                return False, "could not list the PR's changed files"
            scope = {
                "backend": any(not f.startswith("frontend/")
                               and matches_any(f, BACKEND_PATTERNS) for f in files),
                "frontend": any(f.startswith("frontend/") for f in files),
            }
            scope["code"] = scope["backend"] or scope["frontend"]
        hit = [k for k in excluded if scope.get(k)]
        if hit:
            return False, "touches " + " and ".join(sorted(hit)) + " (production code)"
        return True, ""

    def c_pr_author_type(self, expected):
        author = self.pr_data.get("author") or {}
        if "is_bot" not in author:
            return False, "could not determine the PR author type"
        actual = "bot" if author["is_bot"] else "human"
        if actual != expected:
            return False, (f"PR is {actual}-authored (@{author.get('login', '?')}), "
                           f"policy needs {expected}-authored")
        # "bot" means OUR coding agent (settings.agent_bot_logins()), never just
        # any [bot] account: another installed app's PR is somebody else's code.
        if expected == "bot" and not untrusted.is_agent_login(author.get("login") or "",
                                                              is_bot=author.get("is_bot") is True):
            return False, (f"PR author @{author.get('login', '?')} is not this hub's agent "
                           f"({settings.agent_bot_login()})")
        return True, ""

    def c_pr_author_login(self, expected):
        # "bot" here means the OPS identity exactly (settings.bot_logins()):
        # trivial lanes (graph-refresh-*) are opened by deterministic ops jobs.
        # The agent App and any other [bot] account never merge unreviewed.
        if expected != "bot":
            return False, f"policy asks for pr_author_login '{expected}', only 'bot' is implemented"
        login = (self.pr_data.get("author") or {}).get("login") or ""
        if not login:
            return False, "could not determine the PR author login"
        if not untrusted.is_bot_login(login, is_bot=(self.pr_data.get("author") or {}).get("is_bot") is True):
            return False, f"PR author @{login} is not this hub's bot ({settings.bot_login()})"
        return True, ""

    def c_trivial_lane(self, list_name):
        # `list_name` names merge_policy.<list>: lanes of {head_branch, paths}.
        # The head branch picks the lane (graph-refresh-* may only touch the
        # graph files, flow-diagram-* only the diagram); every changed file
        # must match THAT lane's paths. No lane for the branch -> not trivial.
        lanes = self.root.get(list_name)
        if not lanes:
            return False, f"merge_policy.{list_name} is missing or empty"
        head = self.pr_data.get("headRefName") or ""
        if not head:
            return False, "could not read the PR's head branch"
        lane = next((ln for ln in lanes if isinstance(ln, dict) and ln.get("head_branch")
                     and glob_to_regex(ln["head_branch"]).match(head)), None)
        if lane is None:
            return False, f"head branch '{head}' matches no merge_policy.{list_name} entry"
        files = self.files()
        if files is None:
            return False, "could not list the PR's changed files"
        if not files:
            return False, "PR has no changed files"
        strays = [f for f in files if not matches_any(f, lane.get("paths") or [])]
        if strays:
            shown = ", ".join(sorted(strays)[:3])
            more = f" (+{len(strays) - 3} more)" if len(strays) > 3 else ""
            return False, f"files outside the '{lane['head_branch']}' lane: {shown}{more}"
        return True, ""

    def c_all_files_match(self, list_name):
        # `list_name` names a sibling array under merge_policy (today:
        # trivial_paths). Indirection rather than an inline list so the same
        # array is the single thing router.yml's detect-changes `trivial:`
        # filter is checked against by flow_reader.validate().
        patterns = self.root.get(list_name)
        if not patterns:
            return False, f"merge_policy.{list_name} is missing or empty"
        files = self.files()
        if files is None:
            return False, "could not list the PR's changed files"
        if not files:
            return False, "PR has no changed files"
        strays = [f for f in files if not matches_any(f, patterns)]
        if strays:
            shown = ", ".join(sorted(strays)[:3])
            more = f" (+{len(strays) - 3} more)" if len(strays) > 3 else ""
            return False, f"not all files are in merge_policy.{list_name}: {shown}{more}"
        return True, ""

    EVALUATORS = {
        "review_verdict": "c_review_verdict",
        "risk_tag": "c_risk_tag",
        "final_risk": "c_final_risk",
        "ci_checks": "c_ci_checks",
        "change_scope_excludes": "c_change_scope_excludes",
        "pr_author_type": "c_pr_author_type",
        "all_files_match": "c_all_files_match",
        "pr_author_login": "c_pr_author_login",
        "trivial_lane": "c_trivial_lane",
        "always_human": "c_always_human",
    }

    # Tiers that merge without a human click. In single-app mode the agents
    # share the ops identity, so authorship proves nothing: a human merges.
    UNATTENDED_TIERS = ("auto_merge", "trivial_merge")

    def decide(self, tier_name):
        if tier_name in self.UNATTENDED_TIERS and settings.single_app_mode():
            return False, ("single-app mode (no SQUADHQ_AGENT_APP_CLIENT_ID): agents share the ops identity, "
                           "so an approver merges; create the agent App to enable unattended merges")
        tier = self.root.get(tier_name) or {}
        if not tier:
            return False, f"config/flows.yml has no merge_policy.{tier_name} section"
        if tier.get("enabled") is not True:
            return False, f"merge_policy.{tier_name}.enabled is not true"
        if self.pr_data is None:
            return False, "could not read the PR — failing closed"
        if self.pr_data.get("isDraft"):
            return False, "PR is a draft"
        for entry in tier.get("requires_all") or []:
            for key, expected in (entry or {}).items():
                name = self.EVALUATORS.get(key)
                if name is None:
                    return False, (f"unsupported policy condition '{key}' — failing "
                                   f"closed until squadhq.merge_policy implements it")
                met, why = getattr(self, name)(expected)
                if not met:
                    return False, why
        return True, ""


def main(argv):
    tier = argv[1] if len(argv) > 1 else "auto_merge"
    flows_path = os.environ.get("FLOWS") or hub_path(DEFAULT_FLOWS)
    try:
        with open(flows_path) as fh:
            root = (yaml.safe_load(fh) or {}).get("merge_policy") or {}
    except Exception as e:
        root = {}
        print(f"merge_policy: could not read {flows_path}: {e!r}", file=sys.stderr)

    root = apply_project_merge(root, project_merge(os.environ.get("REPO", "")))
    ev = Evaluator(
        os.environ.get("REPO", ""),
        os.environ.get("PR", ""),
        root,
        os.environ.get("DC_BACKEND", ""),
        os.environ.get("DC_FRONTEND", ""),
        dc_code=os.environ.get("DC_CODE", ""),
    )
    ok, reason = ev.decide(tier)
    print(f"auto_merge={'true' if ok else 'false'}")
    print("reason=" + " ".join(reason.split()))
    # The router's own "auto" merge_mode path must also stand down in single-app mode.
    print(f"single_app={'true' if settings.single_app_mode() else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:
        # Never leave the caller without outputs: no outputs would be read as
        # "not true", which is the safe answer anyway, but an explicit false
        # with a reason is what makes the step summary useful.
        print("auto_merge=false")
        print(f"reason=merge_policy.py raised {e!r} — failing closed")
        sys.exit(0)
