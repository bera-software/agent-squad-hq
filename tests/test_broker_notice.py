import datetime

from squadhq import broker_notice

DAY = datetime.date(2026, 10, 29)


def test_notice_names_the_job_the_reason_and_the_fix():
    body = broker_notice.notice("quota_exceeded", "review", "https://app.example/t/1/billing", DAY)
    assert body.startswith("The **review** agent did not start: this month's agent runs")
    assert "https://app.example/t/1/billing" in body and "2026-10" in body


def test_unknown_codes_and_bad_links():
    assert broker_notice.notice("rate_limited", "review", "", DAY) is None
    body = broker_notice.notice("plan_limit", "re`view", "javascript:alert(1)", DAY)
    assert "javascript" not in body and "**review**" in body


def test_main_comments_once(monkeypatch):
    posted = []
    monkeypatch.setattr(broker_notice.capacity_gate, "dedupe_comment", lambda *a: posted.append(a))
    for k, v in {"REPO": "acme/web", "ISSUE_OR_PR": "12", "ROLE": "implement", "REFUSED": "plan_limit",
                 "HELP_URL": "https://app.example/b"}.items():
        monkeypatch.setenv(k, v)
    assert broker_notice.main() == 0 and posted[0][:2] == ("acme/web", "12")
    monkeypatch.setenv("ISSUE_OR_PR", "")
    assert broker_notice.main() == 0 and len(posted) == 1
