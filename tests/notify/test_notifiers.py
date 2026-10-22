import json
import smtplib
from email import message_from_bytes
from pathlib import Path

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError, Notification, Notifier, TransientError
from squadhq.notify import get_notifier
from squadhq.notify._common import markdown_to_mrkdwn, notification_dict, truncate
from squadhq.notify.email import EmailNotifier
from squadhq.notify.github_issue import GitHubIssueNotifier
from squadhq.notify.pagerduty import PagerDutyNotifier
from squadhq.notify.slack import SlackNotifier, build_payload
from squadhq.notify.teams import TeamsNotifier
from squadhq.notify.webhook import SIGNATURE_HEADER, WebhookNotifier, sign, verify_signature

FIX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FIX / name).read_text())


N = Notification(title="Capacity breaker tripped", body_markdown="**Paused** until 10:00. See [run](https://x.test/r/1)",
                 severity="critical", kind="incident", url="https://x.test/r/1", dedupe_key="cap-2026-10",
                 project="acme/app")
PLAIN = Notification(title="Daily brief", body_markdown="# Brief\nall quiet")


# ---------------------------------------------------------------- common
def test_common_helpers():
    assert truncate("abcdef", 4) == "abc…" and truncate("ab", 4) == "ab"
    assert markdown_to_mrkdwn("# H\n**b** [t](https://u.test)") == "*H*\n*b* <https://u.test|t>"
    d = notification_dict(N)
    assert d["title"] == N.title and d["source"] == "agent-squad-hq" and d["timestamp"].endswith("+00:00")


# ---------------------------------------------------------------- github_issue
def gh(t, **kw):
    return GitHubIssueNotifier("acme/hub", token="tok", transport=t, **kw)


def test_github_issue_comments_on_existing_dedupe_match():
    t = FakeTransport()
    t.add("GET /repos/acme/hub/issues", (200, fx("github_open_issues.json")))
    t.add("POST /repos/acme/hub/issues/11/comments", (201, {"id": 1}))
    n = gh(t)
    assert isinstance(n, Notifier)
    n.send(N)
    assert "labels=notification" in t.calls[0]["url"] and "state=open" in t.calls[0]["url"]
    assert t.calls[1]["url"].endswith("/issues/11/comments")  # #5 is a PR, skipped
    assert "**critical**" in t.calls[1]["body"]["body"] and "acme/app" in t.calls[1]["body"]["body"]


def test_github_issue_creates_when_absent_with_paging():
    t = FakeTransport()
    t.add("GET /repos/acme/hub/issues", (200, [{"number": 1, "title": "nope"}],
                                          {"Link": '<https://api.github.com/repos/acme/hub/issues?page=2>; rel="next"'}))
    t.add("GET /repos/acme/hub/issues?page=2", (200, []))
    t.add("POST /repos/acme/hub/issues", (201, fx("github_created_issue.json")))
    gh(t, label="alerts").send(N)
    created = t.calls[-1]["body"]
    assert created["title"] == "Capacity breaker tripped [cap-2026-10]"
    assert created["labels"] == ["alerts"]
    assert len(t.calls) == 3


def test_github_issue_no_dedupe_creates_directly():
    t = FakeTransport().add("POST /issues", (201, fx("github_created_issue.json")))
    gh(t).send(PLAIN)
    assert len(t.calls) == 1 and t.calls[0]["body"]["title"] == "Daily brief"
    assert GitHubIssueNotifier.title_for(Notification(title="x [k]", body_markdown="", dedupe_key="k")) == "x [k]"


def test_github_issue_scan_cap(monkeypatch):
    import squadhq.notify.github_issue as mod

    monkeypatch.setattr(mod, "_SCAN_LIMIT", 1)
    t = FakeTransport()
    t.add("GET /repos/acme/hub/issues", (200, [{"number": 1, "title": "nope"}],
                                          {"Link": '<https://api.github.com/repos/acme/hub/issues?page=2>; rel="next"'}))
    assert gh(t).find_open("[x]") is None


def test_github_issue_env(monkeypatch):
    monkeypatch.setenv("SQUADHQ_NOTIFY_REPO", "o/hub")
    monkeypatch.setenv("SQUADHQ_NOTIFY_LABEL", "lbl")
    monkeypatch.setenv("GH_TOKEN", "envtok")
    n = GitHubIssueNotifier()
    assert (n.repo, n.label, n.token) == ("o/hub", "lbl", "envtok")
    monkeypatch.delenv("SQUADHQ_NOTIFY_REPO")
    monkeypatch.setenv("SQUADHQ_HUB_REPO", "o/agent-squad-hq")
    assert GitHubIssueNotifier().repo == "o/agent-squad-hq"
    assert "Authorization" not in GitHubIssueNotifier(token="")._headers()


# ---------------------------------------------------------------- slack
def test_slack_block_kit_payload():
    p = build_payload(N, "#eng-alerts")
    assert p["channel"] == "#eng-alerts"
    types = [b["type"] for b in p["blocks"]]
    assert types == ["header", "section", "context", "actions"]
    assert p["blocks"][0]["text"]["text"].startswith("🚨")
    assert "<https://x.test/r/1|run>" in p["blocks"][1]["text"]["text"]
    assert p["blocks"][3]["elements"][0]["url"] == N.url
    p2 = build_payload(Notification(title="t" * 300, body_markdown=""))
    assert "channel" not in p2 and len(p2["blocks"][0]["text"]["text"]) <= 150
    assert [b["type"] for b in p2["blocks"]] == ["header", "section", "context"]


def test_slack_send(monkeypatch):
    t = FakeTransport().add("POST hooks.slack.test", (200, "ok"))
    SlackNotifier("#x", webhook_url="https://hooks.slack.test/services/T/B/X", transport=t).send(N)
    assert t.calls[0]["body"]["channel"] == "#x"
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    with pytest.raises(FatalError):
        SlackNotifier().send(N)


# ---------------------------------------------------------------- teams
def test_teams_adaptive_card(monkeypatch):
    t = FakeTransport().add("POST workflows.test", (202, ""))
    TeamsNotifier(webhook_url="https://workflows.test/hook", transport=t).send(N)
    body = t.calls[0]["body"]
    assert body["type"] == "message"
    att = body["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"][0]["color"] == "Attention"
    assert card["actions"][0]["url"] == N.url
    assert {"title": "Project", "value": "acme/app"} in card["body"][2]["facts"]
    t2 = FakeTransport().add("POST workflows.test", (200, ""))
    TeamsNotifier(webhook_url="https://workflows.test/hook", transport=t2).send(PLAIN)
    assert "actions" not in t2.calls[0]["body"]["attachments"][0]["content"]
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    with pytest.raises(FatalError):
        TeamsNotifier().send(N)


# ---------------------------------------------------------------- email
class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, fail=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.sent = []
        self.fail = fail
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        if self.fail == "refused":
            raise smtplib.SMTPRecipientsRefused({"a@b.com": (550, b"no")})
        if self.fail == "net":
            raise smtplib.SMTPServerDisconnected("gone")
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")


def _email(fail=None, **kw):
    FakeSMTP.instances = []
    defaults = dict(host="smtp.test", user="bot@x.test", password="pw",
                    smtp_factory=lambda h, p, timeout=None: FakeSMTP(h, p, timeout, fail))
    defaults.update(kw)
    return EmailNotifier("a@b.com, c@d.com", **defaults)


def test_email_starttls_multipart():
    e = _email()
    e.send(N)
    s = FakeSMTP.instances[0]
    assert (s.host, s.port) == ("smtp.test", 587)
    assert s.calls == ["starttls", ("login", "bot@x.test"), "quit"]
    msg = message_from_bytes(s.sent[0].as_bytes())
    assert msg["Subject"] == "[CRITICAL] Capacity breaker tripped"
    assert msg["To"] == "a@b.com, c@d.com" and msg["From"] == "bot@x.test"
    assert msg["X-SquadHQ-Dedupe-Key"] == "cap-2026-10"
    kinds = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in kinds and "text/html" in kinds


def test_email_no_tls_no_login_and_env(monkeypatch):
    monkeypatch.setenv("SQUADHQ_SMTP_HOST", "mail.test")
    monkeypatch.setenv("SQUADHQ_SMTP_TLS", "none")
    monkeypatch.setenv("SQUADHQ_SMTP_FROM", "hq@x.test")
    monkeypatch.setenv("SQUADHQ_SMTP_PORT", "2525")
    monkeypatch.setenv("SQUADHQ_NOTIFY_EMAIL_TO", "ops@x.test;dev@x.test")
    monkeypatch.delenv("SQUADHQ_SMTP_USER", raising=False)
    FakeSMTP.instances = []
    e = EmailNotifier(smtp_factory=lambda h, p, timeout=None: FakeSMTP(h, p, timeout))
    assert e.to == ["ops@x.test", "dev@x.test"] and e.port == 2525
    e.send(PLAIN)
    assert FakeSMTP.instances[0].calls == ["quit"]
    monkeypatch.delenv("SQUADHQ_SMTP_PORT")
    assert EmailNotifier(["a@b.com"], host="h", tls="ssl").port == 465


@pytest.mark.parametrize("fail,exc", [("auth", FatalError), ("refused", FatalError), ("net", TransientError)])
def test_email_errors(fail, exc):
    with pytest.raises(exc):
        _email(fail=fail).send(N)
    assert FakeSMTP.instances[0].calls[-1] == "quit"


def test_email_config_errors(monkeypatch):
    for k in ("SQUADHQ_SMTP_HOST", "SQUADHQ_SMTP_FROM", "SQUADHQ_SMTP_USER", "SQUADHQ_NOTIFY_EMAIL_TO", "SQUADHQ_SMTP_TLS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(FatalError):
        EmailNotifier("a@b.com", tls="bogus")
    with pytest.raises(FatalError, match="HOST"):
        EmailNotifier("a@b.com").send(N)
    with pytest.raises(FatalError, match="recipients"):
        EmailNotifier(host="h").send(N)
    with pytest.raises(FatalError, match="FROM"):
        EmailNotifier("a@b.com", host="h").send(N)


def test_email_real_smtp_classes_selected(monkeypatch):
    made = []
    monkeypatch.setattr(smtplib, "SMTP", lambda h, p, timeout=None: made.append(("smtp", h, p)) or FakeSMTP(h, p))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda h, p, timeout=None, context=None: made.append(("ssl", h, p)) or FakeSMTP(h, p))
    EmailNotifier("a@b.com", host="h", sender="s@x.test").send(PLAIN)
    EmailNotifier("a@b.com", host="h", sender="s@x.test", tls="ssl").send(PLAIN)
    assert made == [("smtp", "h", 587), ("ssl", "h", 465)]


# ---------------------------------------------------------------- webhook
def test_webhook_signature_known_vector():
    # GitHub's documented X-Hub-Signature-256 example (same scheme).
    expected = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert sign("It's a Secret to Everybody", b"Hello, World!") == expected
    assert verify_signature("It's a Secret to Everybody", b"Hello, World!", expected)
    assert not verify_signature("It's a Secret to Everybody", b"Hello, World?", expected)
    assert not verify_signature("secret", b"x", None)


def test_webhook_send_signed(monkeypatch):
    t = FakeTransport().add("POST hooks.example.test", (204, ""))
    WebhookNotifier("https://hooks.example.test/in", secret="s3", transport=t).send(N)
    call = t.calls[0]
    assert call["body"]["dedupe_key"] == "cap-2026-10" and "timestamp" in call["body"]
    sig = call["headers"]["X-squadhq-signature-256"]
    assert verify_signature("s3", call["raw"], sig)
    assert SIGNATURE_HEADER.lower() == "x-squadhq-signature-256"
    t2 = FakeTransport().add("POST hooks.example.test", (200, ""))
    WebhookNotifier("https://hooks.example.test/in", secret="", transport=t2).send(N)
    assert "X-squadhq-signature-256" not in t2.calls[0]["headers"]
    monkeypatch.delenv("SQUADHQ_WEBHOOK_URL", raising=False)
    with pytest.raises(FatalError):
        WebhookNotifier().send(N)


# ---------------------------------------------------------------- pagerduty
def test_pagerduty_event(monkeypatch):
    t = FakeTransport().add("POST events.pagerduty.com/v2/enqueue", (202, fx("pagerduty_accepted.json")))
    PagerDutyNotifier("rk", transport=t).send(N)
    b = t.calls[0]["body"]
    assert b["routing_key"] == "rk" and b["event_action"] == "trigger" and b["dedup_key"] == "cap-2026-10"
    assert b["payload"]["severity"] == "critical" and b["payload"]["source"] == "acme/app"
    assert b["links"] == [{"href": N.url, "text": "Details"}]
    t2 = FakeTransport().add("POST enqueue", (202, {}))
    PagerDutyNotifier("rk", transport=t2).send(PLAIN)
    b2 = t2.calls[0]["body"]
    assert "dedup_key" not in b2 and "links" not in b2 and b2["payload"]["severity"] == "info"
    monkeypatch.delenv("PAGERDUTY_ROUTING_KEY", raising=False)
    with pytest.raises(FatalError):
        PagerDutyNotifier().send(N)
    with pytest.raises(FatalError):
        PagerDutyNotifier("bad", transport=FakeTransport().add("POST enqueue", (400, {"status": "invalid event"}))).send(N)


def test_registry_builds_each():
    t = FakeTransport()
    assert get_notifier("github_issue:o/r", transport=t).repo == "o/r"  # type: ignore[attr-defined]
    assert get_notifier("slack:#a", transport=t).channel == "#a"  # type: ignore[attr-defined]
    assert get_notifier("teams:https://w.test/x", transport=t).webhook_url == "https://w.test/x"  # type: ignore[attr-defined]
    assert get_notifier("email:a@b.com,c@d.com").to == ["a@b.com", "c@d.com"]  # type: ignore[attr-defined]
    assert get_notifier("webhook:https://h.test/x", transport=t).url == "https://h.test/x"  # type: ignore[attr-defined]
    assert get_notifier("pagerduty:rk", transport=t).routing_key == "rk"  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        get_notifier("carrier-pigeon")
