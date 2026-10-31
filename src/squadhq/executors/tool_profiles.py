"""Tool profiles, defined once and translated per coding-agent executor.

Profiles (each includes everything in the one before it):

``read-only``
    Read/search the checkout; ``git status|diff|log|show``; ``gh issue|pr
    view|list``, ``gh pr diff``. No web access (add ``WebSearch``/``WebFetch``
    via ``extra`` only for roles that need research).
``comment``
    + ``gh issue comment``, ``gh pr comment``, ``gh pr review`` (review
    comments; branch protection/merge policy decides whether a bot approval
    counts — the agent never merges).
``edit-test``
    + ``Edit``/``Write``/``MultiEdit``, ``git add``, ``git commit``, creating
    ``agent/*`` branches. Test/build commands come from the selected packs'
    ``tools`` lists, passed as ``extra`` (e.g. ``Bash(./gradlew *)``).
``edit-test-push``
    + ``git push origin HEAD:refs/heads/agent/*`` (agent branches only) and
    ``gh pr create``.

Edit profiles also get ``git fetch origin [<branch>]`` and ``git merge origin/*`` so
agents bring the base branch in by merging; ``git rebase`` is denied and
force pushes are never allowed, for any role.

Role adjustments (``role=`` / ``--role``; aliases ``designer``, ``reviewer``):

``design``
    pushes ``HEAD:refs/heads/design/*`` (and creates ``design/*`` branches)
    instead of ``agent/*``.
``review-fix``, ``mention``
    push to the PR head branch given as ``push_branch`` / ``--push-branch``,
    which must match ``agent/*`` or ``design/*`` (no wildcards, no ``..``);
    without it they keep the default ``agent/*`` grant.
``review``, ``review-fix``
    + ``Bash(gh api repos/*/pulls/*/comments*)``,
    ``Bash(gh api --method POST repos/*/pulls/*/comments*)``,
    ``Bash(gh api repos/*/pulls/*/reviews*)`` (inline review comments). For
    these roles the blanket ``Bash(gh api *)`` deny is replaced by targeted
    denies (``-X``, other ``--method`` verbs, ``graphql``, ``merge``), because
    deny rules beat allow rules in Claude Code. Every other ``gh api`` call
    stays outside the allowlist.

Deny rules (Claude Code ``--disallowedTools``; every profile and role) also
close the ways an allowlisted ``git`` prefix could write files or run code:
``--output``/``-o`` on diff/log/show, ``format-patch``, ``--upload-pack``,
``--receive-pack``, ``--exec``, ``--ext-diff``, ``--textconv``, global
``git -c``/``--config-env``/``-C``/``--git-dir``/``--work-tree``/``--exec-path``,
``git config``/``submodule``/``filter-branch``/``hook``, options on ``git fetch``
(so the grant only ever reaches the ``origin`` remote), and Edit/Write into
``.git/`` (hooks). Codex, gemini-cli and aider have no command deny list; for
them only the sandbox flags and workflow isolation apply.

**Trust boundary.** Pack build/test tools passed as ``extra`` (``npm run``,
``pytest``, ``./gradlew`` ...) execute repository code: they are arbitrary code
execution *by design*, as is any git hook a commit runs. The tool profile is
defence in depth and a guard against accidents, not a sandbox. The real
boundary is the workflow isolation: agent jobs hold no privileged tokens (only
a short-lived, repo-scoped, least-permission token), and bookkeeping (labels,
ledger, merges) runs in a separate job the agent cannot influence.

``gh`` bodies are always inline (``--body``/``-b``/``-f body=...``):
``--body-file``, ``--template``, ``gh ... -F`` (review roles keep typed
``-F key=value`` on ``gh api`` for integer fields such as ``line``) and ``gh api``
``key=@file`` / ``--field ...@...`` are denied because they read local files
(``/proc/self/environ``) into a comment. ``Read``/``Grep``/``Glob`` are denied
on ``/proc``, ``/sys``, ``/etc``, ``/root``, ``/tmp``, ``/github``, the
runner's ``_temp`` and home dotfiles (``/home/**`` itself is not denied: the
checkout lives there on GitHub-hosted runners), Bash commands may not mention
those paths, and ``$``/backticks are denied outright so no allowed command can
expand ``$ANTHROPIC_API_KEY``. Edit/Write never touch ``.git/**`` or
``.github/**``. ``gh ... -R/--repo <other>`` cannot be pinned to the current repo
with globs; the job token is scoped to that one repository, which is the
control there.

Never granted, whatever the profile: ``Bash(*)``, ``gh pr merge``, any other
``gh api`` call, force pushes, pushes to other refs, ``curl``/``wget``,
``rm -rf``. :func:`validate_extra` rejects extras that try. Prefix wildcards
cannot express every bad suffix (e.g. a second refspec), so branch protection
on the default branch remains the real backstop.

CLI::

    python -m squadhq.executors.tool_profiles --executor claude-code \\
        --profile edit-test-push --extra "Bash(./gradlew *)" [--extra ...]
    python -m squadhq.executors.tool_profiles --executor claude-code --profile edit-test --disallowed
    python -m squadhq.executors.tool_profiles --executor codex --profile edit-test-push
    python -m squadhq.executors.tool_profiles --executor claude-code --profile edit-test-push \\
        --role review-fix --push-branch agent/42-fix-login

Claude Code output is the comma-separated value for ``--allowedTools``
(``--disallowedTools`` with ``--disallowed``); other executors print
shell-quoted CLI flags. ``--format json`` prints a JSON list instead.
Exit status 2 on an unknown profile or a rejected ``--extra``.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Iterable, Sequence
from typing import get_args

from squadhq.interfaces import ToolProfile

PROFILES: tuple[str, ...] = tuple(get_args(ToolProfile))

# Hand-off files the run's own steps read back as size-capped data (findings to
# issues, PR title and body): the only paths outside the checkout an agent may
# write. Claude Code matches every file-writing tool against Edit(path) rules.
HANDOFF_FILES = ("/tmp/agent-findings.json", "/tmp/pr-title.txt", "/tmp/pr-body.md")
_READ_ONLY = [
    "Read", "Glob", "Grep", "LS",
    "Bash(git status)", "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
    "Bash(gh issue view *)", "Bash(gh issue list *)",
    "Bash(gh pr view *)", "Bash(gh pr list *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)",
    *(f"Edit(/{p})" for p in HANDOFF_FILES),
]
_COMMENT = [
    "Bash(gh issue comment *)", "Bash(gh pr comment *)", "Bash(gh pr review *)",
    # Labels added via edit/create pass through the label guard shim
    # (squadhq.label_guard), which refuses labels the role does not own.
    "Bash(gh issue edit *)", "Bash(gh pr edit *)",
    "Bash(gh issue create *)", "Bash(gh issue close *)",
]
_EDIT_TEST = [
    "Edit", "Write", "MultiEdit",
    "Bash(git add *)", "Bash(git commit *)",
    "Bash(git checkout -b agent/*)", "Bash(git switch -c agent/*)",
    # Bring the base branch in by merging, never rebase + force-push.
    "Bash(git fetch origin)", "Bash(git fetch origin *)", "Bash(git merge origin/*)",
    # The project's code graph (installed by run-agent when graphify-out/graph.json exists): read it
    # instead of grepping, and refresh it in the same PR (AST only: no model, no key, seconds).
    "Bash(graphify query *)", "Bash(graphify path *)", "Bash(graphify explain *)", "Bash(graphify update .)",
]
# The spellings agents actually use for one target ({r}: a prefix glob or one
# branch). Which branches may be pushed is enforced by the run's pre-push hook
# (allowed branches only, never the default branch, fast-forward, secret scan),
# so a refspec slipped into the glob is still refused there.
_PUSH_FORMS = ("git push origin HEAD:refs/heads/{r}", "git push origin {r}", "git push -u origin {r}",
               "git push -q origin {r}", "git push --set-upstream origin {r}")


def _push_rules(target: str) -> list[str]:
    return [f"Bash({f.format(r=target)})" for f in _PUSH_FORMS]


_PUSH = [
    *_push_rules("agent/*"),
    "Bash(gh pr create *)",
]
_REVIEW_API = [
    "Bash(gh api repos/*/pulls/*/comments*)",
    "Bash(gh api --method POST repos/*/pulls/*/comments*)",
    "Bash(gh api repos/*/pulls/*/reviews*)",
]
_ROLE_ALIASES = {"designer": "design", "reviewer": "review"}
REVIEW_API_ROLES = frozenset({"review", "review-fix"})
PR_BRANCH_ROLES = frozenset({"review-fix", "mention"})
_PUSH_BRANCH_RE = re.compile(r"^(agent|design)/[A-Za-z0-9][A-Za-z0-9._/-]*$")

_CLAUDE_ALLOWED: dict[str, list[str]] = {
    "read-only": _READ_ONLY,
    "comment": _READ_ONLY + _COMMENT,
    "edit-test": _READ_ONLY + _COMMENT + _EDIT_TEST,
    "edit-test-push": _READ_ONLY + _COMMENT + _EDIT_TEST + _PUSH,
}

_NON_POST_VERBS: tuple[str, ...] = tuple(
    v for base in ("DELETE", "PATCH", "PUT", "GET", "HEAD", "OPTIONS") for v in (base, base.lower(), base.title())
)
# Secret-bearing locations outside the checkout. Claude Code path rules use
# gitignore syntax: "//abs/path" is filesystem-absolute, "~/" is home; the
# single-slash form is emitted too (harmless if it means project-relative).
# "/home/**" and "~/**" are deliberately NOT denied wholesale: GitHub-hosted
# runners keep the checkout under /home/runner/work/<repo> (inside $HOME), and
# deny beats allow, so either rule would deny every file the agent must read.
# Only the home directory's hidden entries (~/.ssh, ~/.config, ...) are denied.
_SECRET_PATHS: tuple[str, ...] = (
    "/proc/**", "/sys/**", "/etc/**", "/root/**", "/tmp/**", "/github/**",  # noqa: S108 - deny rules, not temp-file use
    "/home/runner/work/_temp/**", "/home/*/.*/**", "/home/*/.*",
)
_READ_DENY: tuple[str, ...] = tuple(
    f"{tool}({prefix}{path})"
    for tool in ("Read", "Grep", "Glob")
    for path in _SECRET_PATHS
    for prefix in ("/", "")
) + tuple(f"{tool}({path})" for tool in ("Read", "Grep", "Glob") for path in ("~/.*/**", "~/.*"))
_DENY_COMMON: tuple[str, ...] = (
    "Bash(gh pr merge *)",
    "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git push --force-with-lease *)",
    "Bash(git push * --force*)", "Bash(git push * -f*)", "Bash(git push *+*)",
    "Bash(git push * --delete*)", "Bash(git push * -d *)", "Bash(git push * --mirror*)",
    "Bash(git push * --all*)", "Bash(git push *:* *:*)",
    "Bash(git rebase *)",
    # W3: git options/subcommands that write arbitrary files or run programs.
    "Bash(git * --output*)", "Bash(git * --output=*)",
    "Bash(git diff -o*)", "Bash(git diff * -o*)", "Bash(git log -o*)", "Bash(git log * -o*)",
    "Bash(git show -o*)", "Bash(git show * -o*)", "Bash(git format-patch*)",
    "Bash(git * --upload-pack*)", "Bash(git * --receive-pack*)", "Bash(git * --exec*)",
    "Bash(git * --ext-diff*)", "Bash(git * --textconv*)",
    "Bash(git -c*)", "Bash(git --config-env*)", "Bash(git -C *)", "Bash(git --exec-path*)",
    "Bash(git --git-dir*)", "Bash(git * --git-dir*)", "Bash(git --work-tree*)", "Bash(git * --work-tree*)",
    "Bash(git config*)", "Bash(git submodule*)", "Bash(git filter-branch*)", "Bash(git hook*)",
    "Bash(git fetch * -*)",
    # Never edit git internals (hooks) or workflows/actions.
    *(f"{tool}({path})" for tool in ("Edit", "Write", "MultiEdit")
      for path in (".git/**", "**/.git/**", ".github/**", "**/.github/**")),
    # Secret-bearing files and the runner's temp dirs: no reading, no shell paths.
    *_READ_DENY,
    "Bash(*/proc/*)", "Bash(*/etc/*)", "Bash(*/github/*)", "Bash(*_temp/*)",
    # P3: no absolute, home-relative or parent-relative paths in shell
    # commands, so glob tricks (/pro[c]/..., ~/.., ../../..) cannot reach
    # secret-bearing files; agents work with checkout-relative paths.
    "Bash(* /*)", "Bash(* ~*)", "Bash(*../*)", "Bash(*environ*)",
    "Bash(*--no-index*)", "Bash(git diff --no-index*)",
    # An APPROVE review counts toward the ruleset's required approval: agents
    # never submit one (only humans, or the ops identity after merge_policy).
    "Bash(gh pr review *--approve*)", "Bash(gh pr review * -a*)", "Bash(gh pr review -a*)",
    "Bash(gh api *pprove*)", "Bash(gh api *PPROVE*)",
    # No shell expansion: blocks $ANTHROPIC_API_KEY, $(...) and `...` in any
    # allowed command. Bodies with a literal "$" must be rephrased.
    "Bash(*$*)", "Bash(*`*)",
    # gh comment/review/create commands can read the body from a local file
    # (--body-file / -F), which would exfiltrate e.g. /proc/self/environ.
    "Bash(*--body-file*)", "Bash(gh issue * -F*)", "Bash(gh pr * -F*)", "Bash(gh release * -F*)",
    "Bash(gh *--template*)",
    "Bash(rm -rf *)", "Bash(curl *)", "Bash(wget *)",
)
# Roles without gh api also get a blanket "gh * -F" deny. Review roles keep
# typed "-F key=value" on gh api (line/position/start_line must be integers);
# their =@ / @ file-read denies stay.
CLAUDE_DISALLOWED: tuple[str, ...] = (*_DENY_COMMON[:1], "Bash(gh api *)", "Bash(gh * -F *)", *_DENY_COMMON[1:])
# For review roles the blanket gh api deny would cancel the review-comment
# allows (deny beats allow); deny the dangerous shapes instead.
_DENY_REVIEW_API: tuple[str, ...] = (
    *_DENY_COMMON,
    # W8: every -X / --method form except the exact "--method POST" grant.
    "Bash(gh api * -X*)", "Bash(gh api -X*)",
    "Bash(gh api *--method=*)",
    *(f"Bash(gh api *--method {verb}*)" for verb in _NON_POST_VERBS),
    "Bash(gh api *--method *--method*)",  # a later --method would override the POST
    "Bash(gh api *--method post*)", "Bash(gh api *--method Post*)",  # only the exact grant spelling
    # Method-override headers/fields, raw bodies, path tricks, other endpoints.
    "Bash(gh api * -H*)", "Bash(gh api * --header*)", "Bash(gh api *_method*)",
    "Bash(gh api *X-HTTP-Method*)", "Bash(gh api * --input*)", "Bash(gh api */..*)",
    "Bash(gh api *../*)",
    # -F key=@file / --field key=@file read a local file (or stdin) into the
    # request: an injected reviewer could post @/proc/self/environ. Bodies go
    # inline with -f body=...
    "Bash(gh api *=@*)", "Bash(gh api *--field*@*)", "Bash(gh api * -F @*)", "Bash(gh api * -F@*)",
    "Bash(gh api *graphql*)", "Bash(gh api */merge*)", "Bash(gh api * --hostname*)",
)

# Patterns an ``extra`` entry must never match (checked case-insensitively on
# the whitespace-normalised entry).
_FORBIDDEN_EXTRA = [
    (re.compile(r"^bash$"), "unrestricted Bash"),
    (re.compile(r"^bash\(\s*\*?\s*\)$"), "unrestricted Bash(*)"),
    (re.compile(r"^bash\(\s*\*"), "wildcard command prefix"),
    (re.compile(r"\bgh\s+pr\s+merge\b"), "gh pr merge (agents never merge)"),
    (re.compile(r"\bgh\s+api\b"), "gh api (arbitrary REST calls)"),
    (re.compile(r"\bgit\s+push\b"), "git push (only the edit-test-push profile pushes, to agent/* only)"),
    (re.compile(r"\b(curl|wget)\b"), "network download tools"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"), "rm -rf"),
    (re.compile(r"\bsudo\b"), "sudo"),
]


class InvalidToolError(ValueError):
    """An ``extra`` tool entry would widen the profile beyond policy."""


def _check_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"unknown tool profile {profile!r}; expected one of {list(PROFILES)}")
    return profile


def validate_extra(extra: Iterable[str]) -> list[str]:
    """Return the cleaned extras; raise :class:`InvalidToolError` on a forbidden one."""
    out: list[str] = []
    for item in extra:
        entry = " ".join(item.split())
        if not entry:
            continue
        low = entry.lower()
        for pat, why in _FORBIDDEN_EXTRA:
            if pat.search(low):
                raise InvalidToolError(f"tool entry {item!r} is not allowed: {why}")
        out.append(entry)
    return out


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def canonical_role(role: str | None) -> str | None:
    if not role:
        return None
    r = role.strip().lower()
    return _ROLE_ALIASES.get(r, r)


def validate_push_branch(branch: str) -> str:
    """A concrete PR head branch under ``agent/`` or ``design/``; raise :class:`InvalidToolError` otherwise."""
    b = branch.strip()
    if b.startswith("refs/heads/"):
        b = b[len("refs/heads/"):]
    if not _PUSH_BRANCH_RE.match(b) or ".." in b or b.endswith((".lock", "/", ".")) or "//" in b:
        raise InvalidToolError(f"push branch {branch!r} must be a concrete agent/* or design/* branch name")
    return b


def _role_entries(profile: str, role: str | None, push_branch: str | None) -> list[str]:
    base = list(_CLAUDE_ALLOWED[profile])
    r = canonical_role(role)
    branch = validate_push_branch(push_branch) if push_branch else None
    if branch and r not in PR_BRANCH_ROLES:
        raise InvalidToolError(f"--push-branch is only for roles {sorted(PR_BRANCH_ROLES)}, not {role!r}")
    edits = PROFILES.index(profile) >= PROFILES.index("edit-test")
    if r == "design" and edits:
        base = [t.replace(" agent/*)", " design/*)") for t in base]
        base = [t.replace("refs/heads/agent/*)", "refs/heads/design/*)") for t in base]
    if branch and edits:
        base += [f"Bash(git checkout {branch})", f"Bash(git switch {branch})"]
        if profile == "edit-test-push":
            base = [t for t in base if not t.startswith("Bash(git push ")] + _push_rules(branch) \
                + ["Bash(git push origin HEAD)", "Bash(git push)"]
    if r in REVIEW_API_ROLES:
        base += _REVIEW_API
    return base


def claude_code_tools(profile: str, extra: Iterable[str] = (), *, role: str | None = None,
                      push_branch: str | None = None) -> list[str]:
    """Allowed tool entries for Claude Code, profile (+ role) entries first then extras."""
    return _dedupe(_role_entries(_check_profile(profile), role, push_branch) + validate_extra(extra))


def claude_code_allowed_tools(profile: str, extra: Iterable[str] = (), *, role: str | None = None,
                              push_branch: str | None = None) -> str:
    """Comma-separated ``--allowedTools`` value for Claude Code."""
    return ",".join(claude_code_tools(profile, extra, role=role, push_branch=push_branch))


def claude_code_deny_list(profile: str, role: str | None = None) -> list[str]:
    _check_profile(profile)
    return list(_DENY_REVIEW_API if canonical_role(role) in REVIEW_API_ROLES else CLAUDE_DISALLOWED)


def claude_code_disallowed_tools(profile: str, role: str | None = None) -> str:
    """Comma-separated ``--disallowedTools`` value (role-aware ``gh api`` handling)."""
    return ",".join(claude_code_deny_list(profile, role))


def codex_flags(profile: str) -> list[str]:
    """Codex CLI sandbox/approval flags.

    ``edit-test-push`` enables network inside the workspace-write sandbox
    because ``git push`` needs it; push *scope* (agent/* only) must then be
    enforced by the token and branch protection, since Codex has no
    per-command allowlist.
    """
    p = _check_profile(profile)
    if p in ("read-only", "comment"):
        return ["--sandbox", "read-only", "--ask-for-approval", "never"]
    flags = ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
    if p == "edit-test-push":
        flags += ["-c", "sandbox_workspace_write.network_access=true"]
    return flags


_GEMINI_TOOLS = {
    "read-only": ["read_file", "read_many_files", "glob", "search_file_content", "list_directory",
                  "run_shell_command(git status)", "run_shell_command(git diff)", "run_shell_command(git log)",
                  "run_shell_command(gh issue view)", "run_shell_command(gh pr view)",
                  "run_shell_command(gh pr diff)"],
    "comment": ["run_shell_command(gh issue comment)", "run_shell_command(gh pr comment)"],
    "edit-test": ["write_file", "replace", "run_shell_command(git add)", "run_shell_command(git commit)"],
    "edit-test-push": ["run_shell_command(git push origin HEAD:refs/heads/agent/)",
                       "run_shell_command(gh pr create)"],
}


def gemini_cli_flags(profile: str, extra: Iterable[str] = ()) -> list[str]:
    """Gemini CLI flags (best effort: ``--approval-mode`` + ``--allowed-tools``).

    ``extra`` entries are Gemini tool names, e.g. ``run_shell_command(./gradlew)``.
    """
    p = _check_profile(profile)
    order = PROFILES[: PROFILES.index(p) + 1]
    tools = [t for name in order for t in _GEMINI_TOOLS[name]] + validate_extra(extra)
    mode = "default" if p in ("read-only", "comment") else "auto_edit"
    return ["--approval-mode", mode, "--allowed-tools", ",".join(_dedupe(tools))]


def aider_flags(profile: str) -> list[str]:
    """aider flags. aider has no tool allowlist: read-only profiles run ``--dry-run``."""
    p = _check_profile(profile)
    if p in ("read-only", "comment"):
        return ["--dry-run", "--no-auto-commits", "--yes-always"]
    return ["--yes-always", "--auto-commits", "--no-suggest-shell-commands"]


EXECUTORS = ("claude-code", "codex", "gemini-cli", "aider")


def render(executor: str, profile: str, extra: Iterable[str] = (), *, disallowed: bool = False,
           fmt: str = "text", role: str | None = None, push_branch: str | None = None) -> str:
    """The CLI's output for ``executor``/``profile`` as a string.

    ``role``/``push_branch`` refine Claude Code grants; other executors have no
    per-command allowlist (push scope there rests on token + branch protection),
    but ``push_branch`` is still validated.
    """
    extra = list(extra)
    items: list[str]
    if push_branch:
        validate_push_branch(push_branch)
    if executor == "claude-code":
        items = (claude_code_deny_list(profile, role) if disallowed
                 else claude_code_tools(profile, extra, role=role, push_branch=push_branch))
        return json.dumps(items) if fmt == "json" else ",".join(items)
    if executor == "codex":
        validate_extra(extra)
        items = codex_flags(profile)
    elif executor == "gemini-cli":
        items = gemini_cli_flags(profile, extra)
    elif executor == "aider":
        validate_extra(extra)
        items = aider_flags(profile)
    else:
        raise ValueError(f"unknown executor {executor!r}; expected one of {list(EXECUTORS)}")
    return json.dumps(items) if fmt == "json" else shlex.join(items)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m squadhq.executors.tool_profiles",
                                description="Print an executor's tool grant for a profile")
    p.add_argument("--executor", required=True, choices=EXECUTORS)
    p.add_argument("--profile", required=True)
    p.add_argument("--extra", action="append", default=[],
                   help='extra allowlist entry, repeatable (e.g. "Bash(./gradlew *)")')
    p.add_argument("--disallowed", action="store_true", help="claude-code: print the deny list instead")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--role", default=None,
                   help="job role (design|designer, review|reviewer, review-fix, mention, implement ...)")
    p.add_argument("--push-branch", default=None,
                   help="review-fix/mention: PR head branch to push to (must match agent/* or design/*)")
    args = p.parse_args(argv)
    try:
        print(render(args.executor, args.profile, args.extra, disallowed=args.disallowed, fmt=args.format,
                     role=args.role, push_branch=args.push_branch))
    except ValueError as e:
        print(f"tool_profiles: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
