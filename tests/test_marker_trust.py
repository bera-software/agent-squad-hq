"""Spoofed markers and look-alike titles are ignored.

Anyone who can comment or open an issue can TYPE a protocol string; the
control plane honours one only from our bot (or, where a human decision is
intended, a trusted association)."""
import datetime
import json
import subprocess

from squadhq import failure_handler, github, hub_janitor, untrusted
from squadhq import flow_reader

BOT = {"login": "app/squadhq-ops", "is_bot": True}
MALLORY = {"login": "mallory", "is_bot": False}
OTHER_APP = {"login": "app/other-app", "is_bot": True}


class Runner:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, input=None, timeout=None):
        self.calls.append(cmd[1:])
        rc, out, err = self.handler(cmd[1:])
        return subprocess.CompletedProcess(cmd, rc, out, err)


def with_client(handler):
    r = Runner(handler)
    github.set_client(github.GitHubClient(runner=r, sleep=lambda s: None))
    return r


def teardown_function(_):
    github.set_client(None)


def test_failure_escalation_not_swallowed_by_lookalike_issue():
    title_holder = {}

    def h(args):
        if args[:2] == ["issue", "list"]:
            t = args[args.index("--search") + 1].split('"')[1]
            title_holder["t"] = t
            return 0, json.dumps([{"number": 66, "title": t, "author": MALLORY}]), ""
        if args[:2] == ["issue", "create"]:
            return 0, "https://github.com/o/hub/issues/70\n", ""
        return 0, "", ""
    r = with_client(h)
    ok, _ = failure_handler.escalate("o/hub", "INFRA", "u", 1, "review", "o/p", "pr", "3", "sig")
    assert ok
    assert any(c[:2] == ["issue", "create"] for c in r.calls)
    assert not any(c[:2] == ["issue", "comment"] for c in r.calls)


def _issue(title, author, days=30, comments=()):
    created = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    return {"number": abs(hash((title, author["login"], days))) % 10000, "title": title, "body": "", "labels": [],
            "author": author, "createdAt": created, "comments": list(comments), "isPinned": False}


def test_janitor_never_closes_issues_not_opened_by_our_bot():
    cfg = flow_reader.get_flood_guard("hub_janitor")
    now = datetime.datetime.now(datetime.timezone.utc)
    ours_new = _issue("Daily Brief — 2099-01-02", BOT, days=1)
    spoof_old = _issue("Daily Brief — 2099-01-01", MALLORY, days=40)
    other_app_old = _issue("Daily Brief — 2098-12-01", OTHER_APP, days=60)
    actions, kept = hub_janitor.plan([ours_new, spoof_old, other_app_old], now, cfg)
    closed = [a["close"]["number"] if isinstance(a["close"], dict) else None for a in actions]
    assert spoof_old["number"] not in closed and other_app_old["number"] not in closed
    reasons = {it["number"]: why for it, why in kept}
    assert reasons[spoof_old["number"]] == "human-authored"
    assert reasons[other_app_old["number"]] == "not opened by our bot"


def test_janitor_human_engaged_needs_trusted_association():
    cfg = flow_reader.get_flood_guard("hub_janitor")
    outsider = {"author": {"login": "mallory"}, "authorAssociation": "NONE"}
    member = {"author": {"login": "alice"}, "authorAssociation": "MEMBER"}
    bot_c = {"author": BOT, "authorAssociation": "MEMBER"}
    assert hub_janitor.protected_reason(_issue("x", BOT, comments=[outsider]), cfg) is None
    assert hub_janitor.protected_reason(_issue("x", BOT, comments=[bot_c]), cfg) is None
    assert hub_janitor.protected_reason(_issue("x", BOT, comments=[member]), cfg) == "approver commented"


def test_helpers_reject_other_apps_and_humans():
    assert untrusted.by_bot({"author": BOT})
    assert not untrusted.by_bot({"author": OTHER_APP})
    assert not untrusted.by_bot({"user": {"login": "mallory"}})
