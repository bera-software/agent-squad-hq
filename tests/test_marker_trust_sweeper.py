"""Spoofed markers, badges and protocol lines from non-bot / untrusted
authors are ignored by the sweeper and ticket transitions."""
import datetime
import subprocess
from unittest import mock

import pytest

from squadhq import stuck_sweeper as sw
from squadhq import ticket_transition as tt

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.timezone.utc)
BOT = {"login": "squadhq-ops[bot]", "type": "Bot"}
EVIL = {"login": "mallory", "type": "User"}
APP_BOT = {"login": "app/squadhq-ops", "is_bot": True}


def ago(m):
    return (NOW - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


def com(body, user, minutes=60, assoc="NONE"):
    return {"body": body, "user": user, "author_association": assoc, "created_at": ago(minutes)}


def issue(labels=(), body="## Why this matters\nx", assoc="MEMBER", author=None, n=1):
    return {"number": n, "title": "t", "body": body, "createdAt": ago(600),
            "labels": [{"name": l} for l in labels],
            "author": author or {"login": "alice", "is_bot": False}, "authorAssociation": assoc}


def labeled(label, minutes):
    return {"event": "labeled", "label": {"name": label}, "created_at": ago(minutes),
            "actor": {"login": "squadhq-ops[bot]"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SQUADHQ_BOT_LOGIN", raising=False)
    monkeypatch.delenv("SQUADHQ_TRUSTED_ASSOCIATIONS", raising=False)


def test_attempt_markers_only_from_bot():
    window = NOW - datetime.timedelta(hours=24)
    tag = "<!-- stuck-sweeper state=unshaped -->"
    esc = "<!-- stuck-sweeper state=unshaped escalated -->"
    assert sw.attempts([com(tag, EVIL, 30), com(esc, EVIL, 20)], "unshaped", window) == (0, False)
    assert sw.attempts([com(tag, BOT, 30), com(esc, BOT, 20)], "unshaped", window) == (0, True)


def test_spoofed_sweeper_badge_is_not_the_sweeper():
    assert not sw.is_sweeper(com(sw.BADGE + "hi", EVIL))
    assert sw.is_sweeper(com(sw.BADGE + "hi", BOT))


def test_is_human_requires_trusted_association(monkeypatch):
    assert sw.is_human(com("x", EVIL, assoc="MEMBER"))
    assert not sw.is_human(com("x", EVIL, assoc="CONTRIBUTOR"))
    assert not sw.is_human(com("x", {"login": "other[bot]"}, assoc="MEMBER"))
    monkeypatch.setenv("SQUADHQ_TRUSTED_ASSOCIATIONS", "OWNER")
    assert not sw.is_human(com("x", EVIL, assoc="MEMBER"))


def test_untrusted_mention_and_spoofed_answer():
    th = {"mention_unanswered": 10}
    ctx = {"comments": [com("@squad help", EVIL, 120, assoc="NONE")]}
    assert sw.mention_unanswered(ctx, NOW, th) is None
    ask = com("@squad help", {"login": "alice"}, 120, assoc="MEMBER")
    # a non-bot "answer" does not count as answered
    ctx = {"comments": [ask, com("[Engineer] done", EVIL, 60)]}
    assert sw.mention_unanswered(ctx, NOW, th)[0] == "mention_unanswered"
    ctx = {"comments": [ask, com(sw.BADGE + "x <!-- stuck-sweeper state=mention_unanswered -->", EVIL, 60)]}
    assert sw.mention_unanswered(ctx, NOW, th)[0] == "mention_unanswered"


def test_spoofed_po_answer_and_proposal():
    th = {"approver_reply_unanswered": 10, "unshaped": 10, "team_thread": 10}
    it = issue(["proposal:pending"])
    reply = com("go ahead", {"login": "alice"}, 120, assoc="OWNER")
    ctx = {"comments": [reply, com("[Product Owner] fine", EVIL, 60)], "events": []}
    assert sw.approver_reply_unanswered(it, ctx, NOW, th)[0] == "approver_reply_unanswered"
    # a spoofed PO comment does not make the proposal "posted"
    ctx = {"comments": [com("[Product Owner] ## Proposal", EVIL, 300)], "events": [labeled("proposal:pending", 300)],
           "open_pr_closes": set(), "gate_prs": set()}
    assert sw.classify_issue(it, ctx, NOW, th)[0] == "proposal_missing"


def test_spoofed_team_thread_done_and_trigger():
    th = {"approver_reply_unanswered": 10**6, "unshaped": 10, "team_thread": 10, "mention_unanswered": 10**6}
    it = issue(["proposal:pending"])
    po = com("[Product Owner] ## Proposal", BOT, 300)
    trig = com("@team-thread\nTeam-input-scope: architect", BOT, 200)
    ctx = {"comments": [po, trig, com(sw.TEAM_THREAD_DONE, EVIL, 100)], "events": [],
           "open_pr_closes": set(), "gate_prs": set()}
    assert sw.classify_issue(it, ctx, NOW, th)[0] == "team_thread"
    ctx["comments"] = [po, com("@team-thread", EVIL, 200)]
    assert sw.classify_issue(it, ctx, NOW, th) is None  # no BOT trigger -> waiting on approver


def test_spoofed_engineer_badges():
    it = issue(["agent:implement"])
    since = sw.parse_ts(ago(200))
    assert sw.engineer_block({"comments": [com("[Engineer] not doing it", EVIL, 100)]}, since, it) is None
    assert sw.engineer_block({"comments": [com("[Engineer] not doing it", BOT, 100)]}, since, it)
    ctx = {"events": [labeled("status:in-review", 300)],
           "comments": [com("[Engineer] " + sw.DELIVERED_MARK, EVIL, 100)]}
    assert not sw.delivered_report(ctx)
    ctx["comments"][0]["user"] = BOT
    assert sw.delivered_report(ctx)
    assert not sw.agent_spoke_after([com("[Architect] no", EVIL, 10)], "[Architect]", since)


def test_spoofed_dependency_block_and_untrusted_after():
    f = {"open_impl": {275: 9}, "merged_impl": {}, "closed_issues": set()}
    it = issue(["needs-human"], body="## Why this matters\nAfter: #275")
    ctx = {"facts": f, "comments": [com("[Engineer] Blocked — dependency #275", EVIL, 60)]}
    assert sw.dependency_ready(it, ctx) is None
    ctx["comments"][0]["user"] = BOT
    assert sw.dependency_ready(it, ctx)
    # After: from an untrusted author is not a dependency at all
    assert sw.after_dep(issue(body="After: #275", assoc="NONE")) is None
    assert sw.after_dep(issue(body="After: #275", assoc="NONE", author=APP_BOT)) == 275
    assert sw.dependency_ready(issue(["needs-human"], body="After: #275", assoc="CONTRIBUTOR"), ctx) is None


def test_why_this_matters_only_from_trusted_or_bot():
    assert sw.has_why(issue(assoc="MEMBER"))
    assert not sw.has_why(issue(assoc="NONE"))
    assert not sw.has_why(issue(assoc=""))
    assert sw.has_why(issue(assoc="", author=APP_BOT))
    facts = {"open_impl": {}, "merged_impl": {}}
    assert "Why this matters" in sw.block_reason(issue(["triaged:ready"], assoc="NONE"), facts)
    assert sw.block_reason(issue(["triaged:ready"]), facts) == ""


def test_umbrella_shaped_into_only_from_bot():
    it = issue(["proposal:pending"])
    c = com("[Product Owner] ok\nShaped into: #5, #6", EVIL)
    assert sw.umbrella_done(it, {"comments": [c], "facts": {"closed_issues": {5, 6}}}) is None
    c["user"] = BOT
    assert sw.umbrella_done(it, {"comments": [c], "facts": {"closed_issues": {5, 6}}}) == [5, 6]


def test_gate_for_only_on_bot_prs_and_closes_regex_on_trusted():
    human = {"number": 9, "body": "Gate-for: #2\nCloses #3", "author": {"login": "mallory"},
             "authorAssociation": "NONE", "closingIssuesReferences": [{"number": 4}]}
    assert sw.gates(human) == set()
    assert sw.closes(human) == {4}
    member = dict(human, authorAssociation="MEMBER")
    assert sw.gates(member) == set()          # Gate-for is bot-only
    assert sw.closes(member) == {3, 4}
    bot = dict(human, author={"login": "app/squadhq-ops"})
    assert sw.gates(bot) == {2}
    facts = sw.pr_facts([human], [], 5)
    assert facts["gate_open"] == {} and facts["open_impl"] == {4: 9}


def test_originated_by_only_on_our_bot_issues():
    th = {"unshaped": 10, "mention_unanswered": 10, "approver_reply_unanswered": 10}
    ctx = {"comments": [], "events": [], "open_pr_closes": set(), "gate_prs": set()}
    other = issue(body="Originated-by: x", author={"login": "app/other", "is_bot": True})
    assert sw.classify_issue(other, ctx, NOW, th) is None
    ours = issue(body="Originated-by: x", author=APP_BOT)
    assert sw.classify_issue(ours, ctx, NOW, th)[0] == "unshaped"


def test_park_expiry_ignores_untrusted_reply():
    it = issue(["verdict:decline"])
    events = [labeled("verdict:decline", 60 * 24 * 30)]
    assert sw.park_expired(it, events, [com("please!", EVIL, 60, assoc="NONE")], NOW, 14)
    assert sw.park_expired(it, events, [com("wait", {"login": "alice"}, 60, assoc="OWNER")], NOW, 14) is None


def test_lost_ready_only_counts_our_bot():
    it = issue([])
    ev = [{"event": "labeled", "label": {"name": "triaged:ready"}, "created_at": ago(100), "actor": {"login": "x"}},
          {"event": "unlabeled", "label": {"name": "triaged:ready"}, "created_at": ago(50),
           "actor": {"login": "other-app[bot]"}}]
    assert sw.lost_ready(it, ev) is None
    ev[1]["actor"] = {"login": "squadhq-ops[bot]"}
    assert sw.lost_ready(it, ev)


def test_enrich_trust_looks_up_association_and_fails_closed():
    items = [issue(body="After: #3", assoc=""), issue(body="plain", assoc="", n=2),
             issue(body="Why this matters", assoc="", author=APP_BOT, n=3),
             issue(body="Gate-for: #1", assoc="", n=4)]
    for it in items:
        it.pop("authorAssociation")
    calls = []

    def fake(args):
        calls.append(args)
        if args[1].endswith("/1"):
            return subprocess.CompletedProcess(args, 0, "member\n", "")
        return subprocess.CompletedProcess(args, 1, "", "boom")

    with mock.patch.object(sw, "gh", side_effect=fake):
        sw.enrich_trust("o/r", items)
    assert len(calls) == 2
    assert items[0]["authorAssociation"] == "MEMBER"
    assert "authorAssociation" not in items[1] and "authorAssociation" not in items[2]
    assert items[3]["authorAssociation"] == "NONE"
    assert sw.enrich_trust("o/r", None) is None


def test_transition_ignores_untrusted_after_dependents():
    pr = {"number": 80, "body": "Closes #275", "author": {"login": "app/squadhq-ops"},
          "closingIssuesReferences": [{"number": 275}]}
    issues = [issue(body="After: #275", assoc="NONE", n=276), issue(body="After: #275", assoc="MEMBER", n=277)]
    hit, impl = tt.affected([pr], issues)
    assert impl == {275}
    assert 276 not in hit and hit[277] == 80


def test_spoofed_title_issue_is_not_the_alarm():
    with mock.patch.object(sw, "gh", return_value=subprocess.CompletedProcess(
            [], 0, '[{"number": 5, "title": "T", "author": {"login": "mallory"}}]', "")):
        assert sw.open_issues_titled("o/r", "T") == []


def test_refire_team_thread_uses_bot_trigger_scope():
    ctx = {"comments": [com("@team-thread\nTeam-input-scope: legal", EVIL),
                        com("@team-thread\nTeam-input-scope: architect", BOT)]}
    seen = []
    with mock.patch.object(sw, "gh", side_effect=lambda a: seen.append(a) or subprocess.CompletedProcess(a, 0, "", "")):
        sw.refire("o/r", {"number": 1}, "team_thread", False, ctx, set())
    assert "Team-input-scope: architect" in seen[-1][-1]
    ctx = {"comments": [com("@team-thread\nTeam-input-scope: legal", EVIL)]}
    with mock.patch.object(sw, "gh", side_effect=lambda a: seen.append(a) or subprocess.CompletedProcess(a, 0, "", "")):
        sw.refire("o/r", {"number": 1}, "team_thread", False, ctx, set())
    assert "Team-input-scope: architect, engineer" in seen[-1][-1]
