"""Account pool: pure selection logic, the capacity gate's per-account breaker
(resolve / check-trip / select) and the failure handler's pool rerun. No
network: the hub is the FakeHub of test_capacity_gate."""
import datetime
import json

import pytest

from squadhq import account_pool as AP
from squadhq import capacity_gate as cg
from squadhq import failure_handler as fh
from squadhq import github
from tests.test_capacity_gate import FakeHub, env, iso

NOW = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
POOL = {"pool": [{"id": "main", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_1"},
                 {"id": "second", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_2"},
                 {"id": "keys", "auth": "api", "secret": "ANTHROPIC_API_KEY_1"}]}


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    github.reset_identity_cache()
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)
    yield fake
    github.set_client(None)
    github.reset_identity_cache()


def accounts():
    return AP.parse_pool(POOL)[0]


def tripped(*ids, minutes=30, now=NOW):
    return {"accounts": {i: {"tripped_until": (now + datetime.timedelta(minutes=minutes)).isoformat()} for i in ids}}


def outputs(path):
    out = {}
    for line in path.read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


# ------------------------------------------------------------------ pure logic

def test_parse_pool_defaults_and_drops_bad_entries():
    accts, strategy = AP.parse_pool({"pool": [
        {"id": "a"},  # auth defaults to subscription, secret to CLAUDE_CODE_OAUTH_TOKEN_1
        {"id": "b", "auth": "api"},  # secret defaults by position: ANTHROPIC_API_KEY_2
        {"id": "a", "auth": "api"},  # duplicate id dropped
        {"id": "Bad Id"}, {"id": "c", "auth": "iam"}, {"id": "d", "secret": "lower"}, "junk",
    ], "pool_strategy": "bogus"})
    assert [a.public() for a in accts] == [
        {"id": "a", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_1"},
        {"id": "b", "auth": "api", "secret": "ANTHROPIC_API_KEY_2"}]
    assert strategy == "ordered"
    assert AP.parse_pool({}) == ([], "ordered")
    assert AP.parse_pool({"pool": "x"}) == ([], "ordered")
    assert len(AP.parse_pool({"pool": [{"id": f"a{i}"} for i in range(9)]})[0]) == AP.POOL_MAX


def test_select_ordered_skips_tripped_and_expired_trips_recover():
    a = accounts()
    assert AP.select(a, {}, NOW).id == "main"
    assert AP.select(a, tripped("main"), NOW).id == "second"
    assert AP.select(a, tripped("main", "second"), NOW).id == "keys"
    assert AP.select(a, tripped("main", "second", "keys"), NOW) is None
    # an expired window is available again, no write needed
    assert AP.select(a, tripped("main", minutes=-1), NOW).id == "main"
    assert AP.select([], {}, NOW) is None
    # garbage state never blocks
    assert AP.select(a, {"accounts": {"main": {"tripped_until": "soon"}}}, NOW).id == "main"
    assert AP.select(a, {"accounts": "x"}, NOW).id == "main"


def test_select_round_robin_rotates_and_skips_tripped():
    a = accounts()
    picks = [AP.select(a, {}, NOW, "round_robin", seed).id for seed in range(3)]
    assert picks == ["main", "second", "keys"]
    assert AP.select(a, tripped("second"), NOW, "round_robin", 1).id == "keys"
    assert AP.select(a, tripped("keys"), NOW, "round_robin", 2).id == "main"
    assert AP.select(a, tripped("main", "second", "keys"), NOW, "round_robin", 7) is None
    assert AP.rotation_seed("1", "2", "job") == AP.rotation_seed("1", "2", "job")


def test_trip_never_shortens_and_keeps_history_bounded():
    s = AP.trip({}, "main", NOW, "run/1")
    assert AP.tripped_until(s, "main", NOW) == NOW + datetime.timedelta(minutes=AP.TRIP_MINUTES)
    later = tripped("main", minutes=600)
    assert AP.tripped_until(AP.trip(later, "main", NOW), "main", NOW) == NOW + datetime.timedelta(minutes=600)
    s = {"history": [{"i": i} for i in range(30)]}
    s = AP.trip(s, "x", NOW, "r")
    assert len(s["history"]) == 20 and s["history"][-1]["account"] == "x"


def test_earliest_recovery_and_status():
    a = accounts()
    st = tripped("main", minutes=10)
    st["accounts"].update(tripped("second", minutes=5)["accounts"])
    assert AP.earliest_recovery(a, st, NOW) is None  # keys is still available
    st["accounts"].update(tripped("keys", minutes=50)["accounts"])
    assert AP.earliest_recovery(a, st, NOW) == NOW + datetime.timedelta(minutes=5)
    rows = AP.status(a, tripped("main"), NOW)
    assert [r["state"] for r in rows] == ["tripped", "available", "available"]
    assert set(rows[0]) == {"id", "auth", "secret", "state", "tripped_until", "tripped_at"}


def test_env_pool_and_credentials():
    env_ = {"ANTHROPIC_API_KEY_2": "k2", "CLAUDE_CODE_OAUTH_TOKEN_1": "t1", "ANTHROPIC_API_KEY": "base"}
    pool = AP.env_pool(env_)
    assert [(a.id, a.secret) for a in pool] == [("api-2", "ANTHROPIC_API_KEY_2"), ("sub-1", "CLAUDE_CODE_OAUTH_TOKEN_1")]
    assert AP.credential_from_env(pool[0], env_) == {"ANTHROPIC_API_KEY": "k2"}
    assert AP.credential_from_env(pool[1], env_) == {"CLAUDE_CODE_OAUTH_TOKEN": "t1"}
    assert AP.credential_from_env(AP.Account("x", "api", "MISSING"), env_) == {}


def test_actions_slots():
    assert AP.actions_slot_ok(AP.Account("a", "api", "ANTHROPIC_API_KEY_5"))
    assert AP.actions_slot_ok(AP.Account("a", "subscription", "CLAUDE_CODE_OAUTH_TOKEN"))
    assert not AP.actions_slot_ok(AP.Account("a", "api", "CLAUDE_CODE_OAUTH_TOKEN_1"))  # wrong family
    assert not AP.actions_slot_ok(AP.Account("a", "api", "ANTHROPIC_API_KEY_6"))
    assert not AP.actions_slot_ok(AP.Account("a", "api", "MY_KEY"))


# ------------------------------------------------------- capacity gate: resolve

def test_resolve_pool_selects_first_account(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    out, summ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert outputs(out) == {"auth_mode": "subscription", "account": "main", "auth_secret": "CLAUDE_CODE_OAUTH_TOKEN_1"}
    assert "runs on account main" in summ.read_text()


def test_resolve_pool_next_account_and_api_account(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    hub.files[cg.CAPACITY_PATH] = (tripped("main", now=cg.utcnow()), "c")
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert outputs(out)["account"] == "second"
    hub.files[cg.CAPACITY_PATH] = (tripped("main", "second", now=cg.utcnow()), "c2")
    out.unlink()
    cg.cmd_resolve()
    assert outputs(out) == {"auth_mode": "api", "account": "keys", "auth_secret": "ANTHROPIC_API_KEY_1"}


def test_resolve_pool_ignores_fleet_paused_until(hub, monkeypatch, tmp_path):
    # With a pool the per-account breakers decide; a stale fleet-level window does not pause.
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    hub.files[cg.CAPACITY_PATH] = ({"paused_until": iso(30)}, "c")
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert outputs(out)["account"] == "main"


def test_resolve_pool_all_tripped_skips_with_comment(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    hub.files[cg.CAPACITY_PATH] = (tripped("main", "second", "keys", now=cg.utcnow()), "c")
    out, summ = env(monkeypatch, tmp_path, ISSUE_OR_PR="12", COMMENT_ON_SKIP="true")
    cg.cmd_resolve()
    assert outputs(out) == {"auth_mode": "skip", "account": "", "auth_secret": ""}
    assert "every pool account" in summ.read_text()
    comment = hub.find("issue", "comment")
    assert comment and "every pool account" in comment[0][-1]


def test_resolve_pool_all_tripped_falls_back_to_metered(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (dict(POOL, fallback_on_capacity_trip=True), "a")
    hub.files[cg.CAPACITY_PATH] = (tripped("main", "second", "keys", now=cg.utcnow()), "c")
    out, summ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    # account "" + auth_secret "" = the single ANTHROPIC_API_KEY
    assert outputs(out) == {"auth_mode": "api", "account": "", "auth_secret": ""}
    assert "failing over to metered API" in summ.read_text()


def test_resolve_pool_skips_accounts_without_actions_slot(hub, monkeypatch, tmp_path, capsys):
    hub.files[cg.AUTH_PATH] = ({"pool": [{"id": "custom", "auth": "api", "secret": "MY_KEY"},
                                         {"id": "ok", "auth": "api", "secret": "ANTHROPIC_API_KEY_2"}]}, "a")
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert outputs(out)["account"] == "ok"
    assert "do not declare" in capsys.readouterr().out


def test_resolve_round_robin_uses_run_seed(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (dict(POOL, pool_strategy="round_robin"), "a")
    seen = set()
    for run_id in range(12):
        out, _ = env(monkeypatch, tmp_path, GITHUB_RUN_ID=str(run_id), GITHUB_RUN_ATTEMPT="1")
        if out.exists():
            out.unlink()
        cg.cmd_resolve()
        seen.add(outputs(out)["account"])
    assert seen == {"main", "second", "keys"}


def test_single_credential_output_unchanged(hub, monkeypatch, tmp_path):
    out, _ = env(monkeypatch, tmp_path)
    cg.cmd_resolve()
    assert out.read_text() == "auth_mode=subscription\n"


# --------------------------------------------------- capacity gate: check-trip

def write_result(tmp_path, **kw):
    data = {"schema_version": "1", "executor": "claude-code", "role": "implement", "model": "m",
            "outcome": "failure", "error_class": "capacity", "capacity_tripped": True}
    data.update(kw)
    p = tmp_path / "execution-result.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_check_trip_trips_only_the_account_that_ran(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    _, summ = env(monkeypatch, tmp_path, AUTH_MODE="subscription", EXECUTION_RESULT=write_result(tmp_path),
                  ACCOUNT="main", RUN_URL="http://run/1")
    cg.cmd_check_trip()
    state = hub.files[cg.CAPACITY_PATH][0]
    assert set(state["accounts"]) == {"main"}
    assert state["accounts"]["main"]["tripped_by"] == "http://run/1"
    assert not state.get("paused_until")  # the fleet keeps running on the other accounts
    assert not hub.find("issue", "create")  # no escalation while accounts remain
    assert "2 account(s) left: second, keys" in summ.read_text()


def test_check_trip_result_account_wins_and_api_accounts_trip(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    env(monkeypatch, tmp_path, AUTH_MODE="api", EXECUTION_RESULT=write_result(tmp_path, account="keys"),
        ACCOUNT="")
    cg.cmd_check_trip()
    assert set(hub.files[cg.CAPACITY_PATH][0]["accounts"]) == {"keys"}


def test_check_trip_last_account_escalates(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    hub.files[cg.CAPACITY_PATH] = (tripped("main", "second", now=cg.utcnow()), "c")
    env(monkeypatch, tmp_path, AUTH_MODE="api", EXECUTION_RESULT=write_result(tmp_path), ACCOUNT="keys")
    cg.cmd_check_trip()
    create = hub.find("issue", "create")
    assert create
    body = create[0][create[0].index("--body") + 1] if "--body" in create[0] else " ".join(create[0])
    assert "every pool account is exhausted" in body and "paused" in body


def test_check_trip_no_trip_no_write_and_untrusted_text_ignored(hub, monkeypatch, tmp_path):
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    # the normalized verdict says no trip: transcript text quoting a limit must not trip the account
    exe = tmp_path / "exec.json"
    exe.write_text(json.dumps([{"type": "assistant", "text": "hit your session limit"},
                               {"type": "result", "is_error": False, "result": "ok"}]))
    env(monkeypatch, tmp_path, AUTH_MODE="subscription", ACCOUNT="main", EXECUTION_FILE=str(exe),
        EXECUTION_RESULT=write_result(tmp_path, capacity_tripped=False, error_class="none", outcome="success"))
    cg.cmd_check_trip()
    assert cg.CAPACITY_PATH not in hub.files


def test_check_trip_malformed_account_falls_back_to_fleet(hub, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, AUTH_MODE="api", EXECUTION_RESULT=write_result(tmp_path), ACCOUNT="../x")
    cg.cmd_check_trip()  # api without a (valid) account: single-credential rules, nothing trips
    assert cg.CAPACITY_PATH not in hub.files


def test_write_account_trip_compare_before_write_and_retry(hub):
    now = cg.utcnow()
    assert cg.write_account_trip("o/hub", "main", "r", now)
    puts = len(hub.find("api", "-X", "PUT"))
    assert cg.write_account_trip("o/hub", "main", "r", now)  # covered by the existing window
    assert len(hub.find("api", "-X", "PUT")) == puts
    hub.put_fail = 1
    assert cg.write_account_trip("o/hub", "second", "r", now)
    assert set(hub.files[cg.CAPACITY_PATH][0]["accounts"]) == {"main", "second"}


def test_select_command_local_files(tmp_path, capsys, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(POOL))
    state = tmp_path / "capacity.json"
    state.write_text(json.dumps(tripped("main", now=cg.utcnow())))
    monkeypatch.setenv("JOB", "implement")
    cg.main(["select", "--auth", str(auth), "--state", str(state)])
    assert capsys.readouterr().out.strip() == "second"
    state.write_text(json.dumps(tripped("main", "second", "keys", now=cg.utcnow())))
    cg.main(["select", "--auth", str(auth), "--state", str(state)])
    assert capsys.readouterr().out.strip() == "none"


def test_pool_has_available(hub):
    assert cg.pool_has_available("o/hub") == (False, False, 0)
    hub.files[cg.AUTH_PATH] = (POOL, "a")
    assert cg.pool_has_available("o/hub") == (True, True, 3)
    hub.files[cg.CAPACITY_PATH] = (tripped("main", "second", "keys", now=cg.utcnow()), "c")
    assert cg.pool_has_available("o/hub") == (True, False, 3)


# ------------------------------------------------------ failure handler rerun

class _GH:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        import subprocess
        self.calls.append(cmd[1:])
        out = "https://github.com/o/hub/issues/1" if cmd[1:3] == ["issue", "create"] else "[]"
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def fgh():
    fake = _GH()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    yield fake
    github.set_client(None)


def _fh_env(monkeypatch, tmp_path, **kw):
    base = {"HUB_REPO": "o/hub", "RUN_REPO": "o/p", "JOB": "implement", "EVENT": "issues", "RUN_ID": "9",
            "RUN_ATTEMPT": "1", "RUN_URL": "http://run", "GITHUB_STEP_SUMMARY": str(tmp_path / "s.md")}
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return tmp_path / "s.md"


@pytest.mark.parametrize("available,attempt,rerun", [(True, "1", True), (False, "1", False), (True, "4", False)])
def test_failure_handler_reruns_on_next_pool_account(fgh, monkeypatch, tmp_path, available, attempt, rerun):
    summary = _fh_env(monkeypatch, tmp_path, RUN_ATTEMPT=attempt,
                      EXECUTION_RESULT=write_result(tmp_path, account="main", error_message="hit your session limit"))
    monkeypatch.setattr(cg, "pool_has_available", lambda _hub: (True, available, 3))
    fh.main(["ci"])
    reran = any(" ".join(c).endswith("/rerun") for c in fgh.calls)
    assert reran is rerun
    assert (not reran) == bool([c for c in fgh.calls if c[:2] == ["issue", "create"]])
    if rerun:
        assert "next available account" in summary.read_text()


def test_failure_handler_single_credential_capacity_still_escalates(fgh, monkeypatch, tmp_path):
    _fh_env(monkeypatch, tmp_path, EXECUTION_RESULT=write_result(tmp_path))
    monkeypatch.delenv("ACCOUNT", raising=False)
    monkeypatch.setattr(cg, "pool_has_available", lambda _hub: (_ for _ in ()).throw(AssertionError("no pool")))
    fh.main(["ci"])
    assert not any(" ".join(c).endswith("/rerun") for c in fgh.calls)


# ------------------------------------------------- executor result carries it

def test_normalize_records_account_and_result_roundtrip(tmp_path):
    from squadhq.executors import normalize
    from squadhq.executors.result import read_result

    exe = tmp_path / "exec.json"
    exe.write_text(json.dumps([{"type": "result", "is_error": True, "result": "You've hit your session limit"}]))
    out = tmp_path / "r.json"
    assert normalize.main(["--executor", "claude-code", "--input", str(exe), "--output", str(out),
                           "--account", "second"]) == 0
    res = read_result(out)
    assert res.account == "second" and res.capacity_tripped
    normalize.main(["--executor", "claude-code", "--input", str(exe), "--output", str(out), "--account", "Bad/Id"])
    assert read_result(out).account == ""


def test_api_key_exhaustion_counts_as_capacity():
    from squadhq.executors.normalize import detect_capacity

    assert detect_capacity("Your credit balance is too low to access the Anthropic API")
    assert detect_capacity("You have reached your specified API usage limits.")
    assert not detect_capacity("HTTP 429 rate limit")  # transient: retried, never trips an account
