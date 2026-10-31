"""squadhq.label_sync: missing labels are created on projects and the hub, refusals are visible."""
import json
import subprocess
from unittest import mock

from squadhq import label_sync as ls


def _fake(have, refuse=()):
    calls = []

    def gh(args):
        calls.append(args)
        if args[0] == "api":
            repo = args[1].split("/")[1] + "/" + args[1].split("/")[2]
            return subprocess.CompletedProcess(args, 0, "\n".join(have.get(repo, [])), "")
        if args[:2] == ["label", "create"]:
            if args[2] in refuse:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 403: Resource not accessible")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)
    return gh, calls


def test_only_missing_labels_are_created_and_the_hub_gets_project_labels(tmp_path, monkeypatch):
    projects = [{"name": "AgentGuard", "repo": "acme/agentguard"}]
    plan = dict(ls.plan(projects, "acme/hub"))
    registry = [n for n, _ in plan["acme/agentguard"]]
    assert "needs-approver-merge" in registry and "status:in-review" in registry
    assert {"daily-brief", "weekly-rollup", "project:agentguard"} <= {n for n, _ in plan["acme/hub"]}

    gh, calls = _fake({"acme/agentguard": registry[1:]})
    with mock.patch.object(ls, "gh", gh):
        created, failed = ls.ensure("acme/agentguard", plan["acme/agentguard"])
    assert created == [registry[0]] and failed == []
    assert [c for c in calls if c[0] == "label"] == [["label", "create", registry[0], "-R", "acme/agentguard",
                                                      "--color", plan["acme/agentguard"][0][1]]]


def test_a_refused_label_fails_the_run(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "projects.json").write_text(json.dumps({"projects": [{"name": "web", "repo": "acme/web"}]}))
    monkeypatch.setattr(ls, "hub_path", lambda *p: str(tmp_path.joinpath(*p)))
    monkeypatch.setattr(ls.settings, "hub_repo", lambda: "acme/hub")
    first = ls.static_label_names()[0][0]
    gh, _ = _fake({}, refuse={first})
    with mock.patch.object(ls, "gh", gh):
        assert ls.main([]) == 1
    gh, _ = _fake({"acme/web": [n for n, _ in ls.static_label_names()],
                   "acme/hub": ["daily-brief", "weekly-rollup", "project:web"]})
    with mock.patch.object(ls, "gh", gh):
        assert ls.main([]) == 0
