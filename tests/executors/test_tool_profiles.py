import json
import re

import pytest

from squadhq.executors import tool_profiles as tp


def test_profiles_are_nested_and_never_widen_to_dangerous():
    prev: set[str] = set()
    for p in tp.PROFILES:
        tools = tp.claude_code_tools(p)
        assert prev <= set(tools)
        prev = set(tools)
        for t in tools:
            assert t != "Bash(*)" and t != "Bash"
            assert "gh pr merge" not in t and "gh api" not in t
            assert "--force" not in t
    assert tp.PROFILES == ("read-only", "comment", "edit-test", "edit-test-push")


def test_specific_grants():
    ro = tp.claude_code_tools("read-only")
    assert "Edit" not in ro and "Bash(gh pr view *)" in ro
    assert "Bash(gh pr comment *)" in tp.claude_code_tools("comment")
    et = tp.claude_code_tools("edit-test")
    assert "Bash(git commit *)" in et and not any("git push" in t for t in et)
    push = tp.claude_code_allowed_tools("edit-test-push", ["Bash(./gradlew *)", "Bash(./gradlew *)"])
    assert "Bash(git push origin HEAD:refs/heads/agent/*)" in push
    assert push.endswith(",Bash(./gradlew *)")
    assert push.count("./gradlew") == 1


@pytest.mark.parametrize("bad", [
    "Bash(*)", "Bash", "bash( * )", "Bash(gh pr merge 5)", "Bash(gh api repos/x -X DELETE)",
    "Bash(git push origin main)", "Bash(curl http://x)", "Bash(rm -rf /)", "Bash(sudo make)", "Bash(* --help)",
])
def test_validate_extra_rejects(bad):
    with pytest.raises(tp.InvalidToolError):
        tp.validate_extra([bad])


def test_validate_extra_cleans():
    assert tp.validate_extra(["  Bash(npm   test *) ", "", "Bash(mvn *)"]) == ["Bash(npm test *)", "Bash(mvn *)"]


def test_unknown_profile():
    with pytest.raises(ValueError):
        tp.claude_code_allowed_tools("admin")
    with pytest.raises(ValueError):
        tp.codex_flags("admin")


def test_disallowed():
    d = tp.claude_code_disallowed_tools("edit-test")
    assert "Bash(gh pr merge *)" in d and "Bash(gh api *)" in d


def test_codex_flags():
    assert tp.codex_flags("read-only") == ["--sandbox", "read-only", "--ask-for-approval", "never"]
    assert tp.codex_flags("comment")[1] == "read-only"
    assert tp.codex_flags("edit-test") == ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
    assert tp.codex_flags("edit-test-push")[-1] == "sandbox_workspace_write.network_access=true"


def test_gemini_and_aider_flags():
    g = tp.gemini_cli_flags("read-only")
    assert g[:2] == ["--approval-mode", "default"] and "write_file" not in g[3]
    g = tp.gemini_cli_flags("edit-test-push", ["run_shell_command(./gradlew)"])
    assert g[1] == "auto_edit" and "write_file" in g[3] and g[3].endswith("run_shell_command(./gradlew)")
    assert "--dry-run" in tp.aider_flags("comment")
    assert "--auto-commits" in tp.aider_flags("edit-test")


def test_render_and_cli(capsys):
    assert tp.render("codex", "edit-test", fmt="json") == json.dumps(tp.codex_flags("edit-test"))
    assert tp.render("aider", "read-only") == "--dry-run --no-auto-commits --yes-always"
    assert "--allowed-tools" in tp.render("gemini-cli", "comment")
    assert json.loads(tp.render("claude-code", "read-only", fmt="json"))[0] == "Read"
    with pytest.raises(ValueError):
        tp.render("cursor", "read-only")

    assert tp.main(["--executor", "claude-code", "--profile", "edit-test-push", "--extra", "Bash(./gradlew *)"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == tp.claude_code_allowed_tools("edit-test-push", ["Bash(./gradlew *)"])
    assert tp.main(["--executor", "claude-code", "--profile", "read-only", "--disallowed"]) == 0
    assert "Bash(gh api *)" in capsys.readouterr().out
    assert tp.main(["--executor", "codex", "--profile", "edit-test-push"]) == 0
    assert "workspace-write" in capsys.readouterr().out
    assert tp.main(["--executor", "claude-code", "--profile", "edit-test", "--extra", "Bash(*)"]) == 2
    assert tp.main(["--executor", "codex", "--profile", "nope"]) == 2
    assert tp.main(["--executor", "aider", "--profile", "edit-test", "--extra", "Bash(git push x)"]) == 2


def test_edit_profiles_merge_base_never_rebase():
    for p in ("edit-test", "edit-test-push"):
        tools = tp.claude_code_tools(p)
        assert {"Bash(git fetch origin)", "Bash(git fetch origin *)", "Bash(git merge origin/*)"} <= set(tools)
        assert "Bash(git fetch origin*)" not in tools  # would admit "originevil" / other remotes
    assert "Bash(git merge origin/*)" not in tp.claude_code_tools("comment")
    deny = tp.claude_code_deny_list("edit-test-push")
    assert "Bash(git rebase *)" in deny
    for role in (None, "review", "review-fix", "design", "mention"):
        d = tp.claude_code_deny_list("edit-test-push", role)
        assert {"Bash(git push --force *)", "Bash(git push * --force*)", "Bash(git push * -f*)",
                "Bash(git push *+*)", "Bash(gh pr merge *)"} <= set(d)


def test_designer_pushes_design_branches():
    for role in ("design", "designer", " Designer "):
        tools = tp.claude_code_tools("edit-test-push", role=role)
        assert "Bash(git push origin HEAD:refs/heads/design/*)" in tools
        assert "Bash(git checkout -b design/*)" in tools
        assert not any("agent/" in t for t in tools)
    # Below edit profiles the role changes nothing.
    assert tp.claude_code_tools("read-only", role="design") == tp.claude_code_tools("read-only")


def test_pr_branch_roles_push_to_head_branch():
    tools = tp.claude_code_tools("edit-test-push", role="review-fix", push_branch="refs/heads/agent/42-fix")
    pushes = [t for t in tools if t.startswith("Bash(git push")]
    assert pushes[0] == "Bash(git push origin HEAD:refs/heads/agent/42-fix)"
    # Only this branch, in the spellings agents use; the pre-push hook enforces it too.
    assert all("agent/42-fix" in t or t in ("Bash(git push origin HEAD)", "Bash(git push)") for t in pushes)
    assert "Bash(git push origin agent/42-fix)" in pushes and "Bash(git push -q origin agent/42-fix)" in pushes
    assert not any("agent/*" in t for t in pushes)
    assert "Bash(git switch agent/42-fix)" in tools
    m = tp.claude_code_tools("edit-test-push", role="mention", push_branch="design/landing")
    assert "Bash(git push origin HEAD:refs/heads/design/landing)" in m
    # edit-test: may check the branch out, still no push.
    et = tp.claude_code_tools("edit-test", role="mention", push_branch="agent/x")
    assert "Bash(git checkout agent/x)" in et and not any("git push" in t for t in et)
    # Without a branch they keep the default agent/* grant.
    assert "Bash(git push origin HEAD:refs/heads/agent/*)" in tp.claude_code_tools("edit-test-push", role="mention")


@pytest.mark.parametrize("branch", [
    "main", "feature/x", "agent/*", "agent/../main", "agent/", "agent/x.lock", "agent//x", "agent/x y",
    "agent/-f", "design/x;rm", "agent/x:main", "+agent/x",
])
def test_push_branch_validation(branch):
    with pytest.raises(tp.InvalidToolError):
        tp.claude_code_tools("edit-test-push", role="review-fix", push_branch=branch)


def test_push_branch_only_for_pr_roles():
    with pytest.raises(tp.InvalidToolError):
        tp.claude_code_tools("edit-test-push", role="implement", push_branch="agent/x")
    with pytest.raises(tp.InvalidToolError):
        tp.claude_code_tools("edit-test-push", push_branch="agent/x")


def test_review_roles_get_only_pr_comment_api():
    for role in ("review", "reviewer", "review-fix"):
        tools = tp.claude_code_tools("comment" if role != "review-fix" else "edit-test-push", role=role)
        api = [t for t in tools if "gh api" in t]
        assert api == ["Bash(gh api repos/*/pulls/*/comments*)",
                       "Bash(gh api --method POST repos/*/pulls/*/comments*)",
                       "Bash(gh api repos/*/pulls/*/reviews*)"]
        deny = tp.claude_code_deny_list("comment", role)
        # The blanket deny would cancel those allows (deny beats allow) ...
        assert "Bash(gh api *)" not in deny
        # ... so the dangerous shapes are denied individually.
        assert {"Bash(gh api * -X*)", "Bash(gh api *--method DELETE*)", "Bash(gh api *graphql*)",
                "Bash(gh api */merge*)", "Bash(gh api *--method=*)"} <= set(deny)
    for role in (None, "implement", "mention", "design"):
        assert not any("gh api" in t for t in tp.claude_code_tools("edit-test-push", role=role))
        assert "Bash(gh api *)" in tp.claude_code_deny_list("edit-test-push", role)
    # Extras still cannot add gh api.
    with pytest.raises(tp.InvalidToolError):
        tp.claude_code_tools("comment", ["Bash(gh api repos/*/issues*)"], role="review")


def test_cli_role_and_push_branch(capsys):
    argv = ["--executor", "claude-code", "--profile", "edit-test-push", "--role", "review-fix",
            "--push-branch", "agent/7-x"]
    assert tp.main(argv) == 0
    out = capsys.readouterr().out.strip()
    assert out == tp.claude_code_allowed_tools("edit-test-push", role="review-fix", push_branch="agent/7-x")
    assert tp.main(argv + ["--disallowed"]) == 0
    assert "Bash(gh api *)" not in capsys.readouterr().out
    assert tp.main(["--executor", "claude-code", "--profile", "edit-test-push", "--role", "review-fix",
                    "--push-branch", "main"]) == 2
    assert tp.main(["--executor", "codex", "--profile", "edit-test-push", "--push-branch", "main"]) == 2
    assert tp.claude_code_disallowed_tools("comment", "review").count("gh api") >= 5


# ---------------------------------------------------------------- W3 / W8


def _matches(rule: str, command: str) -> bool:
    """Claude Code style: ``Bash(<glob>)`` where ``*`` matches any characters."""
    m = re.fullmatch(r"Bash\((.*)\)", rule)
    if not m:
        return False
    return re.fullmatch(".*".join(re.escape(p) for p in m.group(1).split("*")), command, re.S) is not None


def _allowed(profile: str, command: str, role=None, push_branch=None) -> bool:
    allow = tp.claude_code_tools(profile, role=role, push_branch=push_branch)
    deny = tp.claude_code_deny_list(profile, role)
    return any(_matches(r, command) for r in allow) and not any(_matches(r, command) for r in deny)


@pytest.mark.parametrize("cmd", [
    "git diff --output=/tmp/x", "git diff HEAD --output /etc/x", "git log -p --output=evil.sh",
    "git diff -o out.patch", "git log HEAD -o x", "git show HEAD -o x", "git show -ox",
    "git diff --ext-diff HEAD", "git diff --textconv HEAD", "git log --ext-diff",
    "git fetch origin --upload-pack=touch_pwned", "git fetch origin main --upload-pack=x",
    "git fetch origin -q", "git fetch origin main --receive-pack=x",
    "git -c core.pager=sh status", "git -c diff.external=x diff", "git --config-env=core.pager=X log",
    "git -C /tmp status", "git --git-dir=/tmp/x log", "git status --work-tree=/tmp",
    "git log --git-dir=/x", "git --exec-path=/tmp diff",
    "git config core.hooksPath .", "git submodule update", "git filter-branch -f", "git hook run pre-commit",
    "git format-patch -1", "git push origin HEAD:refs/heads/agent/x --force", "git rebase origin/main",
    "git fetch originevil", "git fetch /tmp/other-repo", "git fetch https://evil.example/repo",
    "git commit --exec x",
])
def test_w3_git_file_write_and_exec_denied(cmd):
    for role in (None, "review-fix", "design"):
        assert not _allowed("edit-test-push", cmd, role=role), (cmd, role)


@pytest.mark.parametrize("cmd", [
    "git status", "git diff HEAD~1", "git log --oneline -5", "git show HEAD", "git fetch origin",
    "git fetch origin main", "git merge origin/main", "git add src/a.py", "git commit -m 'fix config'",
    "git switch -c agent/42-fix", "git push origin HEAD:refs/heads/agent/42-fix",
])
def test_w3_normal_git_still_allowed(cmd):
    assert _allowed("edit-test-push", cmd), cmd


def test_w3_git_dir_writes_denied_for_edit_tools():
    deny = tp.claude_code_deny_list("edit-test")
    assert {"Edit(.git/**)", "Write(.git/**)", "Write(**/.git/**)"} <= set(deny)


@pytest.mark.parametrize("cmd", [
    "gh api repos/o/r/pulls/1/comments --method=PATCH", "gh api repos/o/r/pulls/1/comments --method=DELETE",
    "gh api repos/o/r/pulls/1/comments/5 --method=put", "gh api repos/o/r/pulls/1/comments --method patch",
    "gh api repos/o/r/pulls/1/comments/5 --method Delete", "gh api repos/o/r/pulls/1/comments -X=PATCH",
    "gh api repos/o/r/pulls/1/comments -XDELETE", "gh api -X PUT repos/o/r/pulls/1/reviews/2/dismissals",
    "gh api repos/o/r/pulls/1/reviews/2 -X PUT", "gh api repos/o/r/pulls/1/comments --method PUT",
    "gh api --method POST repos/o/r/pulls/1/comments --method PATCH",
    "gh api --method POST repos/o/r/pulls/1/comments -X DELETE",
    "gh api --method post repos/o/r/pulls/1/comments",
    "gh api repos/o/r/pulls/1/comments -H X-HTTP-Method-Override:DELETE",
    "gh api repos/o/r/pulls/1/comments --header 'X-HTTP-Method-Override: PATCH'",
    "gh api repos/o/r/pulls/1/comments -f _method=DELETE",
    "gh api repos/o/r/pulls/1/comments --input payload.json",
    "gh api repos/o/r/pulls/1/comments/../../../../merge",
    "gh api repos/o/r/pulls/1/reviews/../../merge", "gh api repos/o/r/pulls/1/comments/../../../../../graphql",
    "gh api repos/o/r/pulls/1/comments --hostname evil.example",
    "gh api repos/o/r/issues/1/comments", "gh api user", "gh api graphql -f query=x",
])
def test_w8_review_api_method_overrides_denied(cmd):
    for role in ("review", "review-fix"):
        profile = "comment" if role == "review" else "edit-test-push"
        assert not _allowed(profile, cmd, role=role), (cmd, role)


@pytest.mark.parametrize("cmd", [
    "gh api repos/o/r/pulls/1/comments", "gh api repos/o/r/pulls/1/comments?per_page=100",
    "gh api --method POST repos/o/r/pulls/1/comments -f body=nit -f commit_id=abc -f path=a.py -f line=3",
    "gh api repos/o/r/pulls/1/reviews",
])
def test_w8_review_api_grants_still_work(cmd):
    assert _allowed("comment", cmd, role="review")
    assert not _allowed("comment", cmd, role=None)


def test_docstring_states_trust_boundary():
    doc = tp.__doc__ or ""
    assert "arbitrary code" in doc and "workflow isolation" in doc


@pytest.mark.parametrize("cmd", [
    "gh api --method POST repos/o/r/pulls/1/comments -F body=@/proc/self/environ",
    "gh api --method POST repos/o/r/pulls/1/comments -Fbody=@.env",
    "gh api --method POST repos/o/r/pulls/1/comments --field body=@/etc/passwd",
    "gh api --method POST repos/o/r/pulls/1/comments --field=body=@x",
    "gh api --method POST repos/o/r/pulls/1/comments -F body=@-",
    "gh api repos/o/r/pulls/1/comments -F @payload",
    "gh api repos/o/r/pulls/1/comments/1/../../merge",
    "gh api repos/o/r/pulls/1/comments -X PATCH", "gh api -XPATCH repos/o/r/pulls/1/comments",
])
def test_file_reads_and_traversal_denied(cmd):
    for role in ("review", "review-fix"):
        assert not _allowed("comment" if role == "review" else "edit-test-push", cmd, role=role), (cmd, role)


@pytest.mark.parametrize("cmd", [
    "gh pr comment 5 --body-file /proc/self/environ", "gh pr comment 5 -F /proc/self/environ",
    "gh issue comment 5 -F .env", "gh pr review 5 --body-file x", "gh pr create --body-file x",
])
def test_gh_body_file_denied_for_every_role(cmd):
    for role in (None, "review", "review-fix", "mention", "design"):
        assert not _allowed("edit-test-push", cmd, role=role), (cmd, role)


@pytest.mark.parametrize("cmd", [
    "gh api --method POST repos/o/r/pulls/1/comments -f body='Hmm... this looks off..' -f path=a.py -f line=3",
    "gh api --method POST repos/o/r/pulls/1/comments -f body='use foo-X instead' -f commit_id=abc",
    "gh api --method POST repos/o/r/pulls/1/comments -f body='see ../docs for context'",
])
def test_review_bodies_with_ellipsis_and_dash_x_allowed(cmd):
    if "../" in cmd:  # path-traversal guard is purely textual: such a body is refused
        assert not _allowed("comment", cmd, role="review")
    else:
        assert _allowed("comment", cmd, role="review"), cmd
    assert _allowed("comment", "gh pr comment 5 --body 'LGTM...'", role="review")


@pytest.mark.parametrize("cmd", [
    "git show HEAD:/proc/self/environ", "git diff --no-index /etc/passwd x", "git log -- /github/workspace",
    "git diff ../_temp/x", "gh pr comment 5 --body \"$ANTHROPIC_API_KEY\"", "gh pr comment 5 -b \"$(env)\"",
    "gh pr comment 5 --body `env`", "gh issue comment 5 --body-file x", "gh pr create --template t.md",
    "git commit -m \"$GH_TOKEN\"", "gh issue comment 5 -F x", "gh pr comment 5 -F x",
])
def test_env_and_secret_path_bash_denied(cmd):
    for role in (None, "review", "review-fix", "mention", "design"):
        assert not _allowed("edit-test-push", cmd, role=role), (cmd, role)


def test_read_grep_glob_and_edit_path_denies():
    for role in (None, "review"):
        deny = set(tp.claude_code_deny_list("read-only", role))
        for tool in ("Read", "Grep", "Glob"):
            for path in ("//proc/**", "//sys/**", "//etc/**", "//root/**", "//tmp/**", "//github/**",
                         "//home/runner/work/_temp/**", "~/.*/**", "~/.*"):
                assert f"{tool}({path})" in deny, (tool, path)
        # The checkout under /home/runner/work/<repo> (inside $HOME on hosted runners) must stay
        # readable: no rule may cover the whole home directory.
        for tool in ("Read", "Grep", "Glob"):
            assert not any(r.startswith((f"{tool}(//home/**", f"{tool}(/home/**", f"{tool}(~/**", f"{tool}(~)"))
                           for r in deny)
            assert all(r.startswith(f"{tool}(~/.") for r in deny if r.startswith(f"{tool}(~"))
        for tool in ("Edit", "Write", "MultiEdit"):
            for path in (".git/**", "**/.git/**", ".github/**", "**/.github/**"):
                assert f"{tool}({path})" in deny


def test_inline_bodies_still_allowed():
    assert _allowed("comment", "gh pr comment 5 --body 'Looks good... one nit'")
    assert _allowed("comment", "gh issue comment 5 -b 'ok'")
    assert _allowed("edit-test-push", "gh pr create --title t --body 'Fixes #4'")


def test_review_roles_typed_fields_allowed_file_reads_denied():
    ok = "gh api repos/o/r/pulls/1/comments --method POST -f body=x -f commit_id=abc -f path=a.py -F line=3"
    bad = "gh api repos/o/r/pulls/1/comments --method POST -f body=x -F body=@/proc/self/environ"
    for role, profile in (("review", "comment"), ("reviewer", "comment"), ("review-fix", "edit-test-push")):
        assert _allowed(profile, ok, role=role), role
        assert _allowed(profile, ok.replace("-F line=3", "-F line=3 -F start_line=1 -F position=2"), role=role)
        assert not _allowed(profile, bad, role=role), role
        assert not _allowed(profile, ok + " -F extra=@-", role=role)
    for role in (None, "mention", "implement"):
        assert "Bash(gh * -F *)" in tp.claude_code_deny_list("edit-test-push", role)
        assert not _allowed("edit-test-push", ok, role=role)


@pytest.mark.parametrize("role", [None, "reviewer", "review-fix", "engineer", "mention"])
def test_agents_can_never_approve(role):
    deny = tp.claude_code_deny_list("edit-test-push", role=role)
    for entry in ("Bash(gh pr review *--approve*)", "Bash(gh pr review * -a*)", "Bash(gh pr review -a*)",
                  "Bash(gh api *pprove*)", "Bash(gh api *PPROVE*)"):
        assert entry in deny, (role, entry)


def test_path_escape_denies_present():
    deny = tp.claude_code_deny_list("read-only")
    for entry in ("Bash(* /*)", "Bash(* ~*)", "Bash(*../*)", "Bash(*environ*)", "Bash(*--no-index*)"):
        assert entry in deny


def test_every_profile_may_write_the_hand_off_files():
    for profile in tp.PROFILES:
        tools = tp.claude_code_tools(profile)
        assert {f"Edit({p})" for p in tp.HANDOFF_FILES} <= set(tools), profile
        assert all(p.startswith(".squadhq-out/") for p in tp.HANDOFF_FILES)
        assert not [t for t in tools if t.startswith(("Edit(/", "Write(/", "Edit(~", "Write(~"))], profile
