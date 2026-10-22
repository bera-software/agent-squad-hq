"""Tests for squadhq.board_sync. No network: GraphQL goes through a fake
GitHubClient runner that answers by query name."""
import json
import os
import subprocess
from unittest import mock

import pytest

from squadhq import board_sync as bs
from squadhq import github

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "onboard")


def board(bid, title, status=True, more_fields=False):
    fields = [{"id": "F1", "name": "Status", "options": [{"id": "o1", "name": "Todo"},
                                                         {"id": "o2", "name": "In Progress"},
                                                         {"id": "o3", "name": "In review"}]}] if status else [{}]
    return {"id": bid, "title": title, "fields": {"pageInfo": {"hasNextPage": more_fields}, "nodes": fields}}


class Fake:
    def __init__(self, repo_boards=(), org_boards=(), repo_null=False, org_null=False, items=(),
                 issue=True, list_out=None, list_rc=0, more=False):
        self.repo_boards, self.org_boards = list(repo_boards), list(org_boards)
        self.repo_null, self.org_null = repo_null, org_null
        self.items, self.issue, self.more = list(items), issue, more
        self.list_out = list_out if list_out is not None else []
        self.list_rc = list_rc
        self.calls = []

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        if args[:2] == ["api", "graphql"]:
            q = args[3]
            if "repository(owner: $owner, name: $name) {\n    projectsV2" in q:
                data = {"repository": None if self.repo_null else
                        {"projectsV2": {"pageInfo": {"hasNextPage": self.more}, "nodes": self.repo_boards}}}
            elif "organization(login" in q:
                data = {"organization": None if self.org_null else
                        {"projectsV2": {"pageInfo": {"hasNextPage": False}, "nodes": self.org_boards}}}
            elif "issue(number" in q:
                data = {"repository": {"issue": {"id": "I1", "projectItems": {
                    "pageInfo": {"hasNextPage": False}, "nodes": self.items}} if self.issue else None}}
            elif "addProjectV2ItemById" in q:
                data = {"addProjectV2ItemById": {"item": {"id": "NEW"}}}
            else:
                data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": data}), "")
        if args[:2] == ["issue", "list"]:
            return subprocess.CompletedProcess(cmd, self.list_rc, json.dumps(self.list_out), "denied")
        raise AssertionError(args)


@pytest.fixture
def use(monkeypatch):
    monkeypatch.setenv("SQUADHQ_HOME", FIXTURE)
    monkeypatch.setenv("SQUADHQ_HUB_REPO", "acme/hub")
    monkeypatch.delenv("BOARD_ORG", raising=False)
    monkeypatch.delenv("BOARD_TITLE", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def install(fake):
        github.set_client(github.GitHubClient(runner=fake, retries=0))
        return fake
    yield install
    github.set_client(None)


def test_org_defaults_to_settings(use, monkeypatch):
    assert bs.board_org() == "acme"
    monkeypatch.setenv("BOARD_ORG", "other")
    assert bs.board_org() == "other"


def test_discover_single_board_merges_repo_and_org(use):
    use(Fake(repo_boards=[board("B1", "Main")], org_boards=[board("B1", "Main")]))
    info = bs.discover("acme/demo")
    assert info["project_id"] == "B1" and info["field_id"] == "F1"


def test_discover_multiple_needs_title(use, monkeypatch):
    use(Fake(org_boards=[board("B1", "A"), board("B2", "B")]))
    with pytest.raises(RuntimeError, match="Multiple boards"):
        bs.discover()
    monkeypatch.setenv("BOARD_TITLE", "B")
    assert bs.discover()["project_id"] == "B2"
    monkeypatch.setenv("BOARD_TITLE", "C")
    with pytest.raises(RuntimeError, match="No board titled"):
        bs.discover()


def test_discover_none(use):
    use(Fake())
    with pytest.raises(RuntimeError, match="genuinely isn't linked"):
        bs.discover("acme/demo")
    use(Fake(repo_null=True, org_null=True))
    with pytest.raises(RuntimeError, match="Likely cause"):
        bs.discover("acme/demo")


def test_discover_no_status_field_and_truncation_warning(use, tmp_path, monkeypatch, capsys):
    summary = tmp_path / "s"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    use(Fake(repo_boards=[board("B1", "Main", status=False, more_fields=True)], more=True))
    with pytest.raises(RuntimeError, match="No single-select 'Status'"):
        bs.discover("acme/demo")
    assert "more results" in summary.read_text()
    assert "warning" in capsys.readouterr().err


def test_graphql_errors_raise(use):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"errors": [{"message": "nope"}]}), "")
    use(runner)
    with pytest.raises(RuntimeError, match="GraphQL errors"):
        bs.run_graphql("query{x}")


def test_sync_existing_item_and_add(use, capsys):
    f = use(Fake(org_boards=[board("B1", "Main")], items=[{"id": "IT", "project": {"id": "B1"}}]))
    bs.sync("acme/demo", 5, "in_progress")
    assert not any("addProjectV2ItemById" in " ".join(c) for c in f.calls)
    assert "-> In Progress" in capsys.readouterr().out
    f = use(Fake(org_boards=[board("B1", "Main")]))
    bs.sync("acme/demo", 5, "in_review")
    assert any("addProjectV2ItemById" in " ".join(c) for c in f.calls)


def test_sync_errors(use):
    use(Fake(org_boards=[board("B1", "Main")]))
    with pytest.raises(RuntimeError, match="No Status option"):
        bs.sync("acme/demo", 5, "done")
    use(Fake(org_boards=[board("B1", "Main")], issue=False))
    with pytest.raises(RuntimeError, match="not found"):
        bs.sync("acme/demo", 5, "todo")


def test_label_to_state():
    assert bs.label_to_state({"status:in-progress", "agent:implement"}) == "in_progress"
    assert bs.label_to_state({"status:in-review"}) == "in_review"
    assert bs.label_to_state({"agent:implement"}) == "todo"
    assert bs.label_to_state({"P1"}) is None


def test_backfill_ok_failed_and_capped(use, monkeypatch, capsys):
    issues = [{"number": 1, "labels": [{"name": "agent:implement"}]}, {"number": 2, "labels": []},
              {"number": 3, "labels": [{"name": "status:in-review"}]}]
    use(Fake(list_out=issues))
    monkeypatch.setattr(bs, "BACKFILL_LIMIT", 3)
    calls = []

    def fake_sync(r, n, state):
        calls.append((r, n, state))
        if n == 3:
            raise RuntimeError("x")
    with mock.patch.object(bs, "sync", fake_sync):
        res = bs.backfill()
    assert ("acme/demo", 1, "todo", "ok") in res
    assert any(r[1] == 3 and r[3].startswith("FAILED") for r in res)
    assert "cap" in capsys.readouterr().err
    use(Fake(list_rc=1))
    assert bs.backfill("acme/x")[0][3].startswith("FAILED to list")


def test_main_exit_codes(use, capsys):
    use(Fake(org_boards=[board("B1", "Main")]))
    bs.main(["discover"])
    assert '"project_id": "B1"' in capsys.readouterr().out
    with mock.patch.object(bs, "sync") as s:
        bs.main(["sync", "--repo", "acme/demo", "--issue-number", "4", "--state", "todo"])
    s.assert_called_once_with("acme/demo", 4, "todo")
    with mock.patch.object(bs, "backfill", return_value=[("r", 1, "todo", "ok")]):
        bs.main(["backfill"])
    with mock.patch.object(bs, "backfill", return_value=[("r", 1, "todo", "FAILED: x")]), \
            pytest.raises(SystemExit) as e:
        bs.main(["backfill"])
    assert e.value.code == 1
    with mock.patch.object(bs, "discover", side_effect=RuntimeError("bad")), pytest.raises(SystemExit) as e:
        bs.main(["discover"])
    assert e.value.code == 1
