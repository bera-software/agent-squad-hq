"""Tests for squadhq.capacity_gate — auth resolution, breaker trip/clear,
compare-and-set writes. No network: the hub's Contents API is a fake."""
import base64
import datetime
import json
import subprocess

import pytest

from squadhq import capacity_gate as cg
from squadhq import github


class FakeHub:
    """Fake gh for one hub repo: files served via Contents API, issues listed."""

    def __init__(self, files=None):
        self.files = dict(files or {})  # path -> (dict, sha)
        self.calls = []
        self.put_fail = 0
        self.issues = []
        self.comments = []

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append((args, kw.get("input")))
        rc, out, err = self.handle(args, kw.get("input"))
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def handle(self, args, stdin):
        if args[0] == "api" and "/contents/" in args[-1] and len(args) == 2:
            path = args[1].split("/contents/", 1)[1]
            if path not in self.files:
                return 1, "", "HTTP 404 Not Found"
            data, sha = self.files[path]
            raw = data if isinstance(data, str) else json.dumps(data)
            return 0, json.dumps({"content": base64.b64encode(raw.encode()).decode(), "sha": sha}), ""
        if args[:3] == ["api", "-X", "PUT"]:
            if self.put_fail:
                self.put_fail -= 1
                return 1, "", "HTTP 409 conflict"
            body = json.loads(stdin)
            path = args[3].split("/contents/", 1)[1]
            cur = self.files.get(path)
            if cur and body.get("sha") != cur[1]:
                return 1, "", "HTTP 409 sha mismatch"
            self.files[path] = (json.loads(base64.b64decode(body["content"])), f"sha{len(self.calls)}")
            return 0, "{}", ""
        if args[:2] == ["api", "--paginate"]:
            return 0, "\n".join(json.dumps(b if isinstance(b, dict) else {"body": b, "user": {"login": "squadhq-ops[bot]"}})
                                  for b in self.comments), ""
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(self.issues), ""
        if args[:2] == ["issue", "create"]:
            return 0, "https://github.com/o/hub/issues/77", ""
        if args[:2] == ["issue", "comment"]:
            return 0, "", ""
        if args[0] == "api" and args[1].startswith("/users/"):
            return 0, "123\n", ""
        return 1, "", f"unexpected {args}"

    def find(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    github.reset_identity_cache()
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)
    yield fake
    github.set_client(None)
    github.reset_identity_cache()


def iso(minutes):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).isoformat()


def env(monkeypatch, tmp_path, **kw):
    out = tmp_path / "out"
    summ = tmp_path / "summary"
    base = {"HUB_REPO": "o/hub", "REPO": "o/p", "JOB": "implement", "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summ)}
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return out, summ


def modes(out):
    return [ln.split("=", 1)[1] for ln in out.read_text().splitlines()]


def test_parse_ts_variants():
    assert cg.parse_ts("2026-01-01T00:00:00Z").tzinfo is not None
    assert cg.parse_ts("2026-01-01T01:00:00+01:00") == cg.parse_ts("2026-01-01T00:00:00Z")
    assert cg.parse_ts("2026-01-01T00:00:00") == cg.parse_ts("2026-01-01T00:00:00Z")
    assert cg.parse_ts("garbage") is None
    assert cg.parse_ts("") is None and cg.parse_ts(None) is None


def test_fallback_defaults_false_when_key_missing():
    assert cg.fallback_enabled({}) is False
    assert cg.fallback_enabled(None) is False
    assert cg.fallback_enabled({"fallback_on_capacity_trip": True}) is True
    assert cg.fallback_enabled({"fallback_on_capacity_trip": "yes"}) is False


def test_read_hub_json(hub):
    assert cg.read_hub_json("o/hub", "x.json", {"a": 1}) == ({"a": 1}, None)
    hub.files["x.json"] = ("not json", "s1")
    assert cg.read_hub_json("o/hub", "x.json", {"a": 1}) == ({"a": 1}, "s1")
    hub.files["x.json"] = ("[1]", "s2")
    assert cg.read_hub_json("o/hub", "x.json", {"a": 1}) == ({"a": 1}, "s2")
    hub.files["x.json"] = ({"b": 2}, "s3")
    assert cg.read_hub_json("o/hub", "x.json", {}) == ({"b": 2}, "s3")


def test_read_hub_json_client_crash(monkeypatch):
    class Boom:
        def contents_get(self, *a):
            raise OSError("x")
    monkeypatch.setattr(github, "client", lambda: Boom())
    assert cg.read_hub_json("o/hub", "p", {"d": 1}) == ({"d": 1}, None)


def test_resolve_api_mode(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = ({"coding_agents_auth": "api"}, "a")
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert modes(out) == ["api"]


def test_resolve_subscription_untripped_and_missing_files(hub, monkeypatch, tmp_path):
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert modes(out) == ["subscription"]


def test_resolve_unparseable_paused_until_is_not_paused(hub, monkeypatch, tmp_path, capsys):
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": "soon-ish"}, "c")
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert modes(out) == ["subscription"]
    assert "unparseable" in capsys.readouterr().out


def test_resolve_skip_when_tripped_and_no_policy(hub, monkeypatch, tmp_path):
    # auth.json without the key must NOT fail over to metered API.
    hub.files[cg.AUTH_PATH] = ({"coding_agents_auth": "subscription"}, "a")
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": iso(30)}, "c")
    out, summ = env(monkeypatch, tmp_path, ISSUE_OR_PR="12", COMMENT_ON_SKIP="true")
    cg.cmd_resolve()
    assert modes(out) == ["skip"]
    assert "SKIP implement" in summ.read_text()
    comment = hub.find("issue", "comment")
    assert comment and comment[0][2] == "12"


def test_resolve_skip_comment_deduped(hub, monkeypatch, tmp_path):
    until = iso(30)
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": until}, "c")
    hub.comments = [f"Paused — capacity window exhausted, resuming after {until}"]
    out, _ = env(monkeypatch, tmp_path, ISSUE_OR_PR="12", COMMENT_ON_SKIP="true")
    cg.cmd_resolve()
    assert modes(out) == ["skip"]
    assert not hub.find("issue", "comment")


def test_resolve_failover_when_policy_allows(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = ({"fallback_on_capacity_trip": True}, "a")
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": iso(30).replace("+00:00", "Z")}, "c")
    out, summ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert modes(out) == ["api"]
    assert "failing over" in summ.read_text()


def test_resolve_probe_clear_when_window_passed(hub, monkeypatch, tmp_path):
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": iso(-5), "history": [{"a": 1}]}, "c")
    out, summ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert modes(out) == ["subscription"]
    assert hub.files[cg.CAPACITY_PATH][0] == dict(cg.DEFAULT_CAPACITY, history=[{"a": 1}])
    assert "probe" in summ.read_text()


def test_resolve_side_effect_failure_keeps_decision(hub, monkeypatch, tmp_path, capsys):
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": iso(-5)}, "c")
    out, _ = env(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "write_capacity", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    cg.cmd_resolve()
    assert modes(out) == ["subscription"]
    assert "best-effort step failed" in capsys.readouterr().out


def test_write_capacity_creates_missing_file(hub):
    assert cg.write_capacity("o/hub", {"paused_until": iso(60)}, "m", "trip")
    put = hub.find("api", "-X", "PUT")
    assert put
    assert cg.CAPACITY_PATH in hub.files


def test_write_capacity_noop_cases(hub):
    assert cg.write_capacity("o/hub", cg.DEFAULT_CAPACITY, "m", "clear")  # already clear (missing)
    later = iso(120)
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": later}, "c")
    assert cg.write_capacity("o/hub", {"paused_until": iso(60)}, "m", "trip")
    assert not hub.find("api", "-X", "PUT")
    # mixed formats still compare as instants
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": later.replace("+00:00", "Z")}, "c")
    assert cg.write_capacity("o/hub", {"paused_until": iso(60)}, "m", "trip")
    assert not hub.find("api", "-X", "PUT")


def test_write_capacity_retries_lost_race_then_gives_up(hub):
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": ""}, "c")
    hub.put_fail = 1
    assert cg.write_capacity("o/hub", {"paused_until": iso(60)}, "m", "trip")
    assert len(hub.find("api", "-X", "PUT")) == 2
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": ""}, "c")
    hub.put_fail = 10
    assert cg.write_capacity("o/hub", {"paused_until": iso(90)}, "m", "trip") is False


def test_check_trip_noop_paths(hub, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, AUTH_MODE="api")
    cg.cmd_check_trip()
    monkeypatch.setenv("AUTH_MODE", "subscription")
    monkeypatch.setenv("EXECUTION_FILE", str(tmp_path / "none"))
    cg.cmd_check_trip()
    p = tmp_path / "exec.txt"
    p.write_text("all fine")
    monkeypatch.setenv("EXECUTION_FILE", str(p))
    cg.cmd_check_trip()
    assert hub.calls == []


@pytest.mark.parametrize("fallback,phrase", [(False, "paused until"), (True, "failed over to metered API")])
def test_check_trip_trips_and_escalates(hub, monkeypatch, tmp_path, fallback, phrase):
    if fallback:
        hub.files[cg.AUTH_PATH] = ({"fallback_on_capacity_trip": True}, "a")
    p = tmp_path / "exec.txt"
    p.write_text('[{"type": "assistant", "message": "working"}, {"type": "result", "is_error": true, "result": "You\'ve hit your session limit"}]')
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_FILE=str(p), RUN_URL="http://r/1",
        WORKFLOW="router", JOB="implement")
    cg.cmd_check_trip()
    state = hub.files[cg.CAPACITY_PATH][0]
    assert state["tripped_by"] == "http://r/1" and cg.parse_ts(state["paused_until"])
    create = hub.find("issue", "create")[0]
    assert create[create.index("-t") + 1] == cg.CAPACITY_ESCALATION_TITLE
    assert phrase in create[create.index("-b") + 1]


def test_check_trip_comments_on_existing_escalation(hub, monkeypatch, tmp_path):
    hub.issues = [{"number": 4, "title": cg.CAPACITY_ESCALATION_TITLE, "author": {"login": "app/squadhq-ops"}}]
    p = tmp_path / "exec.txt"
    p.write_text('{"type": "result", "is_error": true, "result": "hit your weekly limit"}')
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_FILE=str(p))
    cg.cmd_check_trip()
    assert not hub.find("issue", "create")
    assert hub.find("issue", "comment", "4")


def test_main_and_run(hub, monkeypatch, tmp_path, capsys):
    out, _ = env(monkeypatch, tmp_path)
    cg.main(["bogus"])
    assert "unknown subcommand" in capsys.readouterr().out
    cg.main(["resolve"])
    assert modes(out) == ["subscription"]
    monkeypatch.setenv("AUTH_MODE", "api")
    cg.main(["check-trip"])


def test_run_fails_open_to_subscription(monkeypatch, tmp_path):
    out, _ = env(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "_EMITTED", False)
    monkeypatch.setattr(cg, "cmd_resolve", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert cg.run(["resolve"]) == 0
    assert modes(out) == ["subscription"]


def test_run_does_not_override_an_emitted_decision(monkeypatch, tmp_path):
    out, _ = env(monkeypatch, tmp_path)

    def boom():
        cg._append(str(out), "auth_mode=skip\n")
        cg._EMITTED = True
        raise RuntimeError("late")
    monkeypatch.setattr(cg, "_EMITTED", False)
    monkeypatch.setattr(cg, "cmd_resolve", boom)
    cg.run(["resolve"])
    assert modes(out) == ["skip"]


def test_run_output_unwritable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "missing-dir" / "out"))
    monkeypatch.setattr(cg, "_EMITTED", False)
    monkeypatch.setattr(cg, "cmd_resolve", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    cg.run(["resolve"])
    assert "could not write fallback" in capsys.readouterr().out


def test_bot_identity_and_sh(hub):
    assert cg.bot_identity()[1].startswith("123+")
    cg.sh(["gh", "issue", "comment", "1"])
    assert hub.calls[-1][0][:2] == ["issue", "comment"]


def test_check_trip_prefers_execution_result(hub, monkeypatch, tmp_path):
    r = tmp_path / "execution-result.json"
    r.write_text(json.dumps({"executor": "x", "role": "engineer", "model": "m", "outcome": "failure",
                             "capacity_tripped": True}))
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_RESULT=str(r),
        EXECUTION_FILE=str(tmp_path / "none"))
    cg.cmd_check_trip()
    assert hub.find("issue", "create")


def test_check_trip_untripped_result_is_trusted_alone(hub, monkeypatch, tmp_path):
    r = tmp_path / "execution-result.json"
    r.write_text(json.dumps({"executor": "x", "role": "e", "model": "m", "outcome": "success"}))
    p = tmp_path / "exec.txt"
    p.write_text("all fine")
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_RESULT=str(r), EXECUTION_FILE=str(p))
    cg.cmd_check_trip()
    assert hub.calls == []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert cg.load_execution_result(str(bad)) is None


def test_resolve_skip_spoofed_notice_does_not_dedupe(hub, monkeypatch, tmp_path):
    until = iso(30)
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": until}, "c")
    hub.comments = [{"body": f"Paused — capacity window exhausted, resuming after {until}",
                     "user": {"login": "mallory"}}]
    out, _ = env(monkeypatch, tmp_path, ISSUE_OR_PR="12", COMMENT_ON_SKIP="true")
    cg.cmd_resolve()
    assert hub.find("issue", "comment")



def test_normalized_result_not_tripped_beats_transcript_text(hub, monkeypatch, tmp_path):
    # C7: an untripped normalized result is trusted ALONE, even if the raw
    # transcript contains a trip phrase (e.g. quoted from an issue).
    r = tmp_path / "execution-result.json"
    r.write_text(json.dumps({"executor": "x", "role": "e", "model": "m", "outcome": "failure",
                             "capacity_tripped": False}))
    p = tmp_path / "exec.json"
    p.write_text(json.dumps([{"type": "result", "is_error": True, "result": "hit your session limit"}]))
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_RESULT=str(r), EXECUTION_FILE=str(p))
    cg.cmd_check_trip()
    assert hub.calls == []


@pytest.mark.parametrize("transcript", [
    # the phrase only in tool output / user content / assistant text
    [{"type": "user", "message": {"content": [{"type": "tool_result", "content": "hit your session limit"}]}},
     {"type": "result", "is_error": False, "result": "done"}],
    [{"type": "assistant", "message": "The issue says: hit your Opus limit"},
     {"type": "result", "is_error": False, "result": "quoted: hit your weekly limit"}],
    {"type": "result", "is_error": False, "result": "hit your session limit"},
    [{"type": "result", "is_error": True, "result": "hit your session limit"},
     {"type": "result", "is_error": False, "result": "recovered"}],
])
def test_injected_phrase_outside_final_error_never_trips(hub, monkeypatch, tmp_path, transcript):
    p = tmp_path / "exec.json"
    p.write_text(json.dumps(transcript))
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_FILE=str(p))
    cg.cmd_check_trip()
    assert hub.calls == []


def test_raw_text_and_unreadable_never_trip(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("You've hit your session limit")  # not a structured event
    assert not cg.trip_in_execution_file(str(p))
    assert not cg.trip_in_execution_file("")
    assert not cg.trip_in_execution_file(str(tmp_path))  # a directory: unreadable


def test_structured_error_fields_and_jsonl(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"type": "result", "error": {"message": "You've hit your Opus limit"}}))
    assert cg.trip_in_execution_file(str(p))
    p.write_text(json.dumps({"error_message": "hit your weekly limit"}))
    assert cg.trip_in_execution_file(str(p))
    p.write_text(json.dumps({"type": "assistant", "error": "hit your weekly limit"}))
    assert not cg.trip_in_execution_file(str(p))
    p.write_text("\n".join([json.dumps({"type": "user", "message": "hit your session limit"}), "garbage",
                            json.dumps({"type": "result", "is_error": True, "result": "hit your session limit"})]))
    assert cg.trip_in_execution_file(str(p))
    p.write_text(json.dumps([{"type": "user"}]))
    assert not cg.trip_in_execution_file(str(p))
