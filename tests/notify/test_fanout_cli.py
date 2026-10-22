import io

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import Notification
from squadhq.notify import fan_out, parse_targets
from squadhq.notify.__main__ import main


def test_parse_targets_keeps_email_commas():
    assert parse_targets("github_issue,email:a@b.com,c@d.com,slack:#eng") == [
        "github_issue", "email:a@b.com,c@d.com", "slack:#eng"]
    assert parse_targets(["teams; webhook:https://h.test/x", "pagerduty", " "]) == [
        "teams", "webhook:https://h.test/x", "pagerduty"]
    assert parse_targets("bogus,other") == ["bogus", "other"]


def test_fan_out_isolates_failures(monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    t = FakeTransport().add("POST hooks.slack.test", (200, "ok"))
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")
    res = fan_out(Notification(title="t", body_markdown="b"), ["slack:#a", "teams", "nope"], transport=t)
    assert res["slack:#a"] is None
    assert "TEAMS_WEBHOOK_URL" in res["teams"]
    assert res["nope"].startswith("ValueError")


def test_cli_send(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_fan_out(n, targets):
        calls.append((n, targets))
        return {t: (None if t.startswith("slack") else "FatalError: x") for t in targets}

    monkeypatch.setattr("squadhq.notify.__main__.fan_out", fake_fan_out)
    body = tmp_path / "b.md"
    body.write_text("hello")
    rc = main(["send", "--targets", "slack:#eng,email:a@b.com,c@d.com", "--targets", "pagerduty",
               "--title", "T", "--body-file", str(body), "--severity", "warning", "--kind", "incident",
               "--url", "https://u.test", "--dedupe-key", "k", "--project", "o/r"])
    assert rc == 0
    n, targets = calls[0]
    assert targets == ["slack:#eng", "email:a@b.com,c@d.com", "pagerduty"]
    assert (n.body_markdown, n.severity, n.kind, n.url, n.dedupe_key, n.project) == (
        "hello", "warning", "incident", "https://u.test", "k", "o/r")
    out = capsys.readouterr().out
    assert "slack:#eng: ok" in out and "pagerduty: FAILED" in out


def test_cli_stdin_all_failed_and_empty(monkeypatch, capsys):
    monkeypatch.setattr("squadhq.notify.__main__.fan_out", lambda n, t: {x: "err" for x in t})
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert main(["send", "--targets", "teams", "--title", "T", "--body-file", "-"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["send", "--targets", " ", "--title", "T", "--body-file", "-"]) == 2
    with pytest.raises(SystemExit):
        main(["send", "--title", "T", "--body-file", "-"])
