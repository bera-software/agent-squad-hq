"""verdict_provenance: verdict labels count only when the ops identity added
them for the current head (fixture data, no network)."""
from squadhq import merge_policy as mp

OPS = {"squadhq-ops[bot]"}
HEAD = "a" * 40
OLD = "b" * 40


def ev(label, login, at):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": login}, "created_at": at}


def marker(sha, risk="low", login="squadhq-ops[bot]", at="2026-10-22T10:00:00Z", verdict="approve"):
    return {"user": {"login": login}, "created_at": at,
            "body": f"<!-- squadhq:verdict sha={sha} verdict={verdict} risk={risk} -->\n[Reviewer] verdict recorded"}


def good():
    events = [ev("risk:low", "squadhq-ops[bot]", "2026-10-22T10:00:01Z"),
              ev("ready-to-merge", "squadhq-ops[bot]", "2026-10-22T10:00:02Z")]
    return events, [marker(HEAD)]


def test_ops_labels_with_head_marker_pass():
    events, comments = good()
    assert mp.verdict_provenance(events, comments, HEAD, OPS, ["ready-to-merge", "risk:low"]) == (True, "")


def test_agent_added_ready_to_merge_fails():
    events, comments = good()
    events.append(ev("ready-to-merge", "squadhq-agent[bot]", "2026-10-22T10:05:00Z"))
    ok, why = mp.verdict_provenance(events, comments, HEAD, OPS, ["ready-to-merge", "risk:low"])
    assert not ok and "ready-to-merge" in why


def test_agent_added_risk_label_fails():
    events, comments = good()
    events.append(ev("risk:low", "squadhq-agent[bot]", "2026-10-22T10:05:00Z"))
    ok, why = mp.verdict_provenance(events, comments, HEAD, OPS, ["ready-to-merge", "risk:low"])
    assert not ok and "risk:low" in why


def test_marker_for_an_older_head_fails():
    events, _ = good()
    ok, why = mp.verdict_provenance(events, [marker(OLD)], HEAD, OPS, ["ready-to-merge", "risk:low"])
    assert not ok and "current head" in why


def test_marker_from_agent_or_human_fails():
    events, _ = good()
    for login in ("squadhq-agent[bot]", "octocat"):
        ok, _why = mp.verdict_provenance(events, [marker(HEAD, login=login)], HEAD, OPS, ["ready-to-merge", "risk:low"])
        assert not ok


def test_request_changes_marker_is_not_an_approval():
    events, _ = good()
    ok, _why = mp.verdict_provenance(events, [marker(HEAD, verdict="request-changes")], HEAD, OPS,
                                     ["ready-to-merge", "risk:low"])
    assert not ok


def test_risk_label_must_match_recorded_risk():
    events, _ = good()
    ok, why = mp.verdict_provenance(events, [marker(HEAD, risk="medium")], HEAD, OPS, ["ready-to-merge", "risk:low"])
    assert not ok and "does not match" in why


def test_ready_to_merge_older_than_marker_fails():
    events = [ev("risk:low", "squadhq-ops[bot]", "2026-10-22T09:00:01Z"),
              ev("ready-to-merge", "squadhq-ops[bot]", "2026-10-22T09:00:02Z")]
    ok, why = mp.verdict_provenance(events, [marker(HEAD, at="2026-10-22T10:00:00Z")], HEAD, OPS,
                                    ["ready-to-merge", "risk:low"])
    assert not ok and "predates" in why


def test_unknown_head_fails_closed():
    events, comments = good()
    assert not mp.verdict_provenance(events, comments, None, OPS, ["ready-to-merge", "risk:low"])[0]


def test_evaluator_fails_closed_when_provenance_fails():
    root = {"auto_merge": {"enabled": True, "requires_all": [{"review_verdict": "approve"}]}}
    pr = {"labels": [{"name": "ready-to-merge"}, {"name": "risk:low"}], "isDraft": False, "headRefOid": HEAD,
          "author": {"login": "squadhq-agent[bot]", "is_bot": True}}
    e = mp.Evaluator("o/r", "1", root, pr_data=pr, files=[])
    e._prov = (False, "ready-to-merge was not added by the ops identity")
    ok, why = e.c_review_verdict("approve")
    assert not ok and "ops identity" in why


def test_human_named_like_the_app_never_counts():
    events = [ev("risk:low", "squadhq-ops", "2026-10-22T10:00:01Z"),
              ev("ready-to-merge", "squadhq-ops", "2026-10-22T10:00:02Z")]
    ok, _why = mp.verdict_provenance(events, [marker(HEAD, login="squadhq-ops")], HEAD,
                                     {"squadhq-ops[bot]", "squadhq-ops"}, ["ready-to-merge", "risk:low"])
    assert not ok


def test_user_type_account_with_bot_login_never_counts():
    events, comments = good()
    for e in events:
        e["actor"]["type"] = "User"
    assert not mp.verdict_provenance(events, comments, HEAD, OPS, ["ready-to-merge", "risk:low"])[0]
