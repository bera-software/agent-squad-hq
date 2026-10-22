"""Head-SHA lookup and SHA-pinned merges (server merge gate, security review S1)."""
from __future__ import annotations

from squadhq.codehost.github import GitHubCodeHost
from squadhq.codehost.gitlab import GitLabCodeHost
from squadhq.http import FakeTransport

SHA = "a" * 40


def test_github_pull_request_head_and_fork() -> None:
    t = FakeTransport().add("GET /pulls/7", (200, {"head": {"sha": SHA, "ref": "main",
                                                            "repo": {"full_name": "evil/fork"}}}))
    head = GitHubCodeHost("tok", transport=t).pull_request_head("acme/a", "7")
    assert (head.sha, head.repo, head.fork) == (SHA, "evil/fork", True)
    same = FakeTransport().add("GET /pulls/8", (200, {"head": {"sha": SHA, "repo": {"full_name": "ACME/a"}}}))
    assert GitHubCodeHost("tok", transport=same).pull_request_head("acme/a", "8").fork is False
    deleted = FakeTransport().add("GET /pulls/9", (200, {"head": {"sha": SHA, "repo": None}}))
    assert GitHubCodeHost("tok", transport=deleted).pull_request_head("acme/a", "9").fork is True


def test_github_merge_passes_sha_and_checks_by_sha() -> None:
    t = FakeTransport().add("PUT /pulls/7/merge", (200, {"merged": True}))
    GitHubCodeHost("tok", transport=t).merge("acme/a", "7", method="squash", sha=SHA)
    assert t.calls[-1]["body"] == {"merge_method": "squash", "sha": SHA}
    GitHubCodeHost("tok", transport=t).merge("acme/a", "7")  # backward compatible: no sha
    assert t.calls[-1]["body"] == {"merge_method": "squash"}
    c = FakeTransport().add("GET /check-runs", (200, {"check_runs": [{"name": "hub / ci-gate", "status": "completed",
                                                                      "conclusion": "success"}]}))
    runs = GitHubCodeHost("tok", transport=c).list_checks("acme/a", SHA)
    assert runs[0].name == "hub / ci-gate" and f"/commits/{SHA}/check-runs" in c.calls[0]["url"]


def test_gitlab_head_and_sha_merge() -> None:
    t = FakeTransport().add("GET /merge_requests/3", (200, {"sha": SHA, "source_project_id": 1,
                                                            "target_project_id": 2}))
    head = GitLabCodeHost("tok", transport=t).pull_request_head("grp/app", "3")
    assert head.sha == SHA and head.fork is True
    m = FakeTransport().add("PUT /merge_requests/3/merge", (200, {"state": "merged"}))
    GitLabCodeHost("tok", transport=m).merge("grp/app", "3", sha=SHA)
    assert m.calls[-1]["body"] == {"squash": True, "sha": SHA}
