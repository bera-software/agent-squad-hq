#!/usr/bin/env python3
"""
Label ownership guard. The registry is config/flows.yml labels.*.applied_by;
this module is the one place that decides "may role R add label L".

Two enforcement points, same decision:

  hub loops     squadhq.agent_runner calls blocked_labels() in execute()
                and drops forbidden labels from add_labels / create_issue.

  agent jobs    claude-code-action agents call `gh` themselves, so a check in
                any executor never sees them. Before the agent step, a job runs

                    python -m squadhq.label_guard install --role <role>

                which writes a `gh` wrapper into a fresh private directory
                (mkdtemp under $RUNNER_TEMP) and prepends it
                to PATH via $GITHUB_PATH. Every later `gh` call in the job goes
                through it: a label add the role does not own exits 1 with a
                message the agent can read and act on; anything else is passed
                to the real gh untouched. Blocking BEFORE the add (rather than
                reverting after) means no race with another job labeling the
                same item meanwhile.

Recognised add paths: `gh issue|pr edit --add-label`, `gh issue|pr create
--label/-l`, `gh api` POST/PUT to .../labels or .../issues with labels fields
or --input JSON, and GraphQL addLabelsToLabelable (label IDs cannot be mapped
to names, so it is refused outright for guarded roles — use --add-label).
Label names not in the registry pass (they drive no automation); removing a
label is never guarded.

Approvers, router.yml's own shell steps and squadhq.stuck_sweeper are
code or humans, not agents — they never install the wrapper. The guard is
advisory against a determined agent (an absolute gh path or curl bypasses
it); server-side enforcement on `labeled` events is the real control.
"""
import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile

from squadhq.paths import hub_path

HERE = os.path.dirname(os.path.abspath(__file__))


def load_owners():
    # PyYAML is a declared dependency of the package; never pip-install at
    # run time (an unpinned install inside an agent job is a supply-chain hole).
    from squadhq import flow_reader
    # Explicit path: in router jobs the cwd is the PROJECT checkout, and
    # flow_reader's default "config/flows.yml" is cwd-relative.
    flows = hub_path("config", "flows.yml")
    return {k: sorted(v) for k, v in flow_reader.label_owners(flows).items()}


def may_add(role, label, owners):
    if label.startswith("project:"):
        label = "project:*"
    roles = owners.get(label)
    if roles is None:
        return True  # not a registry label: drives no automation
    return "any" in roles or role in roles


def blocked_labels(role, labels, owners):
    return [l for l in labels if not may_add(role, l, owners)]


def _split(v):
    return [x.strip() for x in v.split(",") if x.strip()]


def _flag_values(args, names):
    """Values of every occurrence of the flags in ``names``. Accepts
    ``--flag v``, ``--flag=v`` and, for short flags, ``-fv`` attached."""
    out = []
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                out += _split(args[i + 1])
            elif n.startswith("--") and a.startswith(n + "="):
                out += _split(a[len(n) + 1:])
            elif not n.startswith("--") and len(n) == 2 and a.startswith(n) and len(a) > 2:
                v = a[2:]
                out += _split(v[1:] if v.startswith("=") else v)
    return out


# gh api flags that take a value — that value is never the endpoint path.
_API_VALUE_FLAGS = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input", "-H",
                    "--header", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}


def _api_path(rest):
    skip = False
    for a in rest:
        if skip:
            skip = False
            continue
        if a in _API_VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("-X") or a.startswith("--method="):
            continue
        if not a.startswith("-"):
            return a
    return ""


# Repository writes an agent must never make through `gh api`: pushes go via
# the deterministic push step, merges via the merge-policy job.
_FORBIDDEN_PATHS = (
    (re.compile(r"^/?repos/[^/]+/[^/]+/contents(/|$)"), "writing repository contents"),
    (re.compile(r"^/?repos/[^/]+/[^/]+/git/refs?(/|$)"), "creating/moving/deleting git refs"),
    (re.compile(r"^/?repos/[^/]+/[^/]+/merges/?$"), "merging branches"),
    (re.compile(r"^/?repos/[^/]+/[^/]+/pulls/\d+/merge/?$"), "merging pull requests"),
)
_FORBIDDEN_MUTATIONS = ("mergePullRequest", "enablePullRequestAutoMerge", "createCommitOnBranch",
                        "updateRef", "createRef", "deleteRef", "updateRefs", "mergeBranch")


def forbidden_write(argv, stdin_text=None):
    """Refuse reason when a `gh api` call would write contents, refs or merge;
    None otherwise. Any non-GET counts, including the implicit POST gh uses
    when -f/-F/--input is given."""
    if not argv or argv[0] != "api":
        return None
    rest = argv[1:]
    path = _api_path(rest)
    if path == "graphql":
        body = " ".join(rest) + (stdin_text or "")
        inp = _flag_values(rest, ["--input"])
        if inp and inp[0] != "-":
            try:
                with open(inp[0]) as fh:
                    body += fh.read()
            except OSError:
                pass
        for m in _FORBIDDEN_MUTATIONS:
            if re.search(r"\b" + m + r"\b", body):
                return f"GraphQL {m} is not allowed — agents never push or merge; the deterministic push/merge steps do"
        return None
    method = (_flag_values(rest, ["-X", "--method"]) or [""])[0].upper()
    implicit = bool(_flag_values(rest, ["-f", "--raw-field", "-F", "--field", "--input"]))
    if method in ("", "GET", "HEAD") and not (method == "" and implicit):
        return None
    clean = path.split("?")[0]
    for rx, what in _FORBIDDEN_PATHS:
        if rx.search(clean):
            return f"`gh api {method or 'POST'} {clean}` ({what}) is not allowed — agents push through the " \
                   "deterministic push step and never merge"
    return None


_BODY_FILE_FLAGS = ("--body-file", "-F")


def referenced_files(argv):
    """Paths whose CONTENT this gh invocation would send: ``--body-file`` /
    ``-F`` (issue/pr/release commands), ``gh api -F/-f key=@file`` and
    ``--input file``. ``-`` (stdin) is not a path."""
    out = []
    is_api = bool(argv) and argv[0] == "api"
    if is_api:
        for v in _flag_values(argv[1:], ["-F", "--field", "-f", "--raw-field"]):
            if "=" in v and v.split("=", 1)[1].startswith("@"):
                out.append(v.split("=", 1)[1][1:])
        out += _flag_values(argv[1:], ["--input"])
    else:
        for i, a in enumerate(argv):
            if a in _BODY_FILE_FLAGS and i + 1 < len(argv):
                out.append(argv[i + 1])
            elif a.startswith("--body-file="):
                out.append(a.split("=", 1)[1])
    return [p for p in out if p and p != "-"]


def outgoing_refusal(argv, stdin_text=None, env=None):
    """Why this gh call must not run, or None. An injected agent must not be
    able to post the LLM key or a token into a public comment, issue or PR:
    every argument, stdin, and every file the call would upload is scanned
    (squadhq.redact.find_secrets — token shapes plus the exact values of
    *KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*PRIVATE* env vars). Files must also
    live inside $GITHUB_WORKSPACE when that is set."""
    from squadhq import redact

    env = os.environ if env is None else env
    kinds = []
    for a in argv:
        kinds += redact.find_secrets(a, env)
    if stdin_text:
        kinds += redact.find_secrets(stdin_text, env)
    workspace = env.get("GITHUB_WORKSPACE")
    for path in referenced_files(argv):
        real = os.path.realpath(path)
        if workspace and os.path.commonpath([real, os.path.realpath(workspace)]) != os.path.realpath(workspace):
            return f"refusing to send {path!r}: files outside the workspace ($GITHUB_WORKSPACE) may not be uploaded"
        try:
            with open(real, errors="replace") as fh:
                kinds += redact.find_secrets(fh.read(), env)
        except OSError:
            pass  # gh itself reports the missing file
    if kinds:
        uniq = sorted(set(kinds))
        return ("refusing to send content that contains a secret (" + ", ".join(uniq) + "). Remove the "
                "credential from the text; never post tokens or keys.")
    return None


_APPROVE_RE = re.compile(r"\bevent\s*[=:]\s*\"?approve\b", re.I)


def approval_refusal(argv, stdin_text=None):
    """Refuse reason when this gh call would APPROVE a pull request, else None.

    An approving review counts toward the branch ruleset's required approval,
    so an agent identity must never submit one: `gh pr review --approve/-a`,
    and `gh api .../pulls/N/reviews` with event=APPROVE in a field, in stdin or
    in an --input file (any letter case). Approvals come from humans, or from
    the ops identity after merge_policy passed."""
    if len(argv) >= 2 and argv[0] == "pr" and argv[1] == "review":
        for a in argv[2:]:
            if a in ("--approve", "-a") or (a.startswith("-") and not a.startswith("--") and "a" in a[1:]):
                return "agents may not approve pull requests (`gh pr review --approve`)"
        return None
    if argv and argv[0] == "api":
        rest = argv[1:]
        path = _api_path(rest).split("?")[0]
        body = " ".join(rest) + (stdin_text or "")
        inp = _flag_values(rest, ["--input"])
        if inp and inp[0] != "-":
            try:
                with open(inp[0], errors="replace") as fh:
                    body += fh.read()
            except OSError:
                pass
        if (re.search(r"/pulls/\d+/reviews", path) or path == "graphql") and \
                (_APPROVE_RE.search(body) or re.search(r"\bAPPROVE\b", body, re.I) and "reviews" in path):
            return "agents may not submit APPROVE reviews"
        if path == "graphql" and re.search(r"\b(addPullRequestReview|submitPullRequestReview)\b", body) \
                and re.search(r"\bAPPROVE\b", body, re.I):
            return "agents may not submit APPROVE reviews"
    return None


def added_labels(argv, stdin_text=None):
    """Labels a gh invocation would ADD. Returns (labels, refuse_reason)."""
    why = approval_refusal(argv, stdin_text)
    if why:
        return [], why
    if len(argv) >= 2 and argv[0] in ("issue", "pr") and argv[1] in ("edit", "create"):
        names = ["--add-label"] if argv[1] == "edit" else ["--label", "-l"]
        return _flag_values(argv[2:], names), None
    if argv and argv[0] == "api":
        why = forbidden_write(argv, stdin_text)
        if why:
            return [], why
        rest = argv[1:]
        path = _api_path(rest)
        method = (_flag_values(rest, ["-X", "--method"]) or ["GET"])[0].upper()
        if path == "graphql":
            body = " ".join(rest) + (stdin_text or "")
            if "addLabelsToLabelable" in body:
                return [], "GraphQL addLabelsToLabelable is not allowed here — use `gh issue edit --add-label`"
            return [], None
        fields = _flag_values(rest, ["-f", "--raw-field", "-F", "--field"])
        has_fields = bool(fields)
        has_input = bool(_flag_values(rest, ["--input"]))
        if method == "GET" and not has_fields and not has_input:
            return [], None
        if method == "DELETE":
            return [], None
        path = path.split("?")[0].rstrip("/")
        if not (path.endswith("/labels") or path.endswith("/issues") or re.search(r"/issues/\d+$", path)
                or re.search(r"/pulls?/\d+$", path)):
            return [], None
        labels = [f.split("=", 1)[1] for f in fields
                  if "=" in f and f.split("=", 1)[0] in ("labels[]", "labels")]
        inp = _flag_values(rest, ["--input"])
        if inp:
            try:
                raw = stdin_text if inp[0] == "-" else open(inp[0]).read()
                data = json.loads(raw or "{}")
                got = data.get("labels", []) if isinstance(data, dict) else data
                labels += [g["name"] if isinstance(g, dict) else str(g) for g in got]
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                return labels, None  # unparseable --input: the real gh rejects it too
        return labels, None
    return [], None


SHIM = r'''#!/usr/bin/env python3
# Generated by `python -m squadhq.label_guard install` — see squadhq/label_guard.py.
import json, os, sys, subprocess
sys.path.insert(0, {pkg_parent!r})
from squadhq.label_guard import added_labels, blocked_labels, outgoing_refusal
REAL, ROLE = {real!r}, {role!r}
OWNERS = json.loads({owners!r})
argv = sys.argv[1:]
stdin_text = None
if any(f in argv and argv[argv.index(f) + 1:argv.index(f) + 2] == ["-"]
       for f in ("--input", "--body-file", "-F")) or "--body-file=-" in argv:
    stdin_text = sys.stdin.read()
refuse = outgoing_refusal(argv, stdin_text)
if refuse:
    print("label-guard: " + refuse, file=sys.stderr)
    sys.exit(1)
labels, refuse = added_labels(argv, stdin_text)
bad = blocked_labels(ROLE, labels, OWNERS)
if refuse or bad:
    why = refuse or ("role '%s' may not add %s — config/flows.yml labels.*.applied_by names who owns "
                     "each label. Leave it for its owner, or say in your comment what should happen." % (ROLE, bad))
    print("label-guard: " + why, file=sys.stderr)
    sys.exit(1)
if stdin_text is not None:
    sys.exit(subprocess.run([REAL] + argv, input=stdin_text, text=True).returncode)
os.execv(REAL, [REAL] + argv)
'''


def install(role):
    real = shutil.which("gh")
    if not real:
        print("label-guard: gh not found, guard NOT installed", file=sys.stderr)
        return 1
    owners = load_owners()
    # A fresh, private directory every time — never a predictable shared path
    # like /tmp/label-guard, which another user on a self-hosted runner could
    # pre-create and plant their own `gh` in.
    d = tempfile.mkdtemp(prefix="label-guard-", dir=os.environ.get("RUNNER_TEMP") or None)
    os.chmod(d, 0o700)
    shim = os.path.join(d, "gh")
    fd = os.open(shim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(SHIM.format(pkg_parent=os.path.dirname(HERE), real=real, role=role, owners=json.dumps(owners)))
    os.chmod(shim, stat.S_IRWXU)
    gp = os.environ.get("GITHUB_PATH")
    if gp:
        with open(gp, "a") as f:
            f.write(d + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    allowed = sorted(l for l in owners if may_add(role, l, owners))
    msg = f"label-guard: installed for role `{role}` at {d} — may add: {', '.join(allowed) or 'nothing'}"
    print(msg)
    if summary:
        with open(summary, "a") as f:
            f.write(msg + "\n")
    return 0


def main_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m squadhq.label_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--role", required=True)
    args = ap.parse_args(argv)
    return install(args.role)


def main():
    return main_args()


if __name__ == "__main__":
    sys.exit(main())
