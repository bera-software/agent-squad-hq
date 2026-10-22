from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from squadhq.scheduler import CronError, Scheduler, load_schedule_config, parse, translate
from squadhq.scheduler.cron import from_parts
from squadhq.scheduler.schedules import build, job_for, parse_spec

UTC = timezone.utc


def u(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


# ----------------------------------------------------------------- parser

def test_parse_fields() -> None:
    e = parse("*/15 9-17 * * mon-fri")
    assert e.minutes == frozenset({0, 15, 30, 45})
    assert e.hours == frozenset(range(9, 18))
    assert e.weekdays == frozenset({1, 2, 3, 4, 5})
    assert parse("0 0 * * 7").weekdays == frozenset({0})
    assert parse("0 0 1 jan,jul *").months == frozenset({1, 7})
    assert parse("5/20 * * * *").minutes == frozenset({5, 25, 45})
    assert parse("10-30/10 * * * *").minutes == frozenset({10, 20, 30})
    assert parse("@hourly").source == "@hourly"
    assert parse("@daily").hours == frozenset({0})


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *",
                                 "a * * * *", "1,,2 * * * *", "* * 0 * *", "* * * 13 *"])
def test_parse_errors(bad: str) -> None:
    with pytest.raises(CronError):
        parse(bad)


def test_next_after_utc() -> None:
    e = parse("30 6 * * 1-5")
    assert e.next_after(u(2026, 10, 21, 6, 29)) == u(2026, 10, 21, 6, 30)
    assert e.next_after(u(2026, 10, 21, 6, 30)) == u(2026, 10, 22, 6, 30)  # strictly after
    assert e.next_after(u(2026, 10, 23, 7, 0)) == u(2026, 10, 26, 6, 30)  # Fri -> Mon
    assert parse("0 0 29 2 *").next_after(u(2026, 3, 1)) == u(2028, 2, 29)
    naive = datetime(2026, 10, 21, 6, 29)
    assert e.next_after(naive) == u(2026, 10, 21, 6, 30)


def test_dom_dow_or_semantics() -> None:
    e = parse("0 12 1 * mon")  # the 1st OR any Monday
    fires = e.fires_between(u(2026, 7, 1), u(2026, 7, 31, 23, 59))  # July 1 2026 is a Wednesday
    assert [f.day for f in fires] == [1, 6, 13, 20, 27]
    assert e.next_after(u(2026, 5, 31, 23)) == u(2026, 6, 1, 12)
    assert parse("0 0 13 * *").matches_local(datetime(2026, 3, 13))


def test_timezone_conversion() -> None:
    e = parse("30 7 * * *")
    # London is UTC+1 in summer, UTC+0 in winter
    assert e.next_after(u(2026, 7, 1), "Europe/London") == u(2026, 7, 1, 6, 30)
    assert e.next_after(u(2026, 12, 1), "Europe/London") == u(2026, 12, 1, 7, 30)
    assert e.next_after(u(2026, 7, 1), "America/New_York") == u(2026, 7, 1, 11, 30)


def test_dst_spring_forward_shifts_into_existing_time() -> None:
    # 2026-03-29: London clocks go 01:00 GMT -> 02:00 BST; 01:30 local does not exist
    e = parse("30 1 * * *")
    fire = e.next_after(u(2026, 3, 28, 12), "Europe/London")
    assert fire == u(2026, 3, 29, 1, 30)  # == 02:30 BST, right after the gap
    # and the day after is normal again (01:30 BST = 00:30 UTC)
    assert e.next_after(fire, "Europe/London") == u(2026, 3, 30, 0, 30)


def test_dst_fall_back_fires_once() -> None:
    # 2026-10-25: London 02:00 BST -> 01:00 GMT; 01:30 happens twice
    e = parse("30 1 * * *")
    fires = e.fires_between(u(2026, 10, 24, 12), u(2026, 10, 26, 12), "Europe/London")
    assert fires == [u(2026, 10, 25, 0, 30), u(2026, 10, 26, 1, 30)]
    hourly = parse("0 * * * *").fires_between(u(2026, 10, 24, 23, 30), u(2026, 10, 25, 3, 30), "Europe/London")
    assert len(hourly) == len(set(hourly))


def test_new_york_dst() -> None:
    e = parse("0 9 * * *")
    assert e.next_after(u(2026, 3, 7, 20), "America/New_York") == u(2026, 3, 8, 13)  # EDT starts Mar 8
    assert e.next_after(u(2026, 3, 7, 0), "America/New_York") == u(2026, 3, 7, 14)  # still EST


def test_from_parts_and_never() -> None:
    e = from_parts("x", times=((7, 30), (12, 0)), weekdays=frozenset({1}))
    assert e.next_after(u(2026, 10, 19, 8), "UTC") == u(2026, 10, 19, 12)
    with pytest.raises(CronError):
        from_parts("none", times=())
    with pytest.raises(CronError):
        parse("0 0 31 2 *").next_after(u(2026, 1, 1))


# ------------------------------------------------------- schedule phrases

@pytest.mark.parametrize("phrase,expected", [
    ("weekdays 07:30", "30 7 * * 1-5"),
    ("daily 06:45", "45 6 * * *"),
    ("sun 04:00", "0 4 * * sun"),
    ("mon,thu 05:00", "0 5 * * mon,thu"),
    ("every 30m", "*/30 * * * *"),
    ("every 30m during working_hours", "*/30 7-19 * * *"),
    ("every 2h", "0 */2 * * *"),
    ("every 2h during working_hours", "0 7-19/2 * * *"),
    ("0 5 * * *", "0 5 * * *"),
    ("@daily", "@daily"),
    ("off", None),
])
def test_translate(phrase: str, expected: str | None) -> None:
    assert translate(phrase, "07:00-20:00") == expected


@pytest.mark.parametrize("bad", ["whenever", "every 0m", "every 99h", "weekdays 25:00", "someday 07:00"])
def test_translate_errors(bad: str) -> None:
    with pytest.raises(CronError):
        translate(bad, "07:00-20:00")
    with pytest.raises(CronError):
        translate("every 5m during working_hours", "nonsense")


def test_parse_spec_uses_config_grammar() -> None:
    expr, tz = parse_spec("weekdays 07:30", "07:00-20:00", "mon-fri")
    assert tz is None and expr.next_after(u(2026, 10, 24), "Europe/London") == u(2026, 10, 26, 7, 30)
    raw, raw_tz = parse_spec("15 5 * * *")
    assert raw_tz == "UTC"
    assert parse_spec("off") is None
    with pytest.raises(CronError):
        parse_spec("whenever")


def test_build_collects_errors() -> None:
    cfg = build({"daily_brief": "weekdays 07:30", "bad": "whenever", "disabled": False, "off": "off"})
    assert [s.key for s in cfg.schedules] == ["daily_brief"]
    assert cfg.errors and cfg.errors[0].startswith("bad:")


def test_load_from_yaml_file(tmp_path: Any) -> None:
    p = tmp_path / "squadhq.yml"
    p.write_text("version: 1\norg: {name: Acme, timezone: America/New_York}\nhub: {repo: acme/hq}\n"
                 "schedules: {daily_brief: 'weekdays 08:00'}\nprojects: [{name: A, repo: acme/a}]\n")
    cfg = load_schedule_config(str(p))
    assert cfg.timezone == "America/New_York"
    keys = {s.key for s in cfg.schedules}
    assert "daily_brief" in keys and "stuck_sweeper" in keys  # canonical defaults from squadhq.config
    assert cfg.projects[0]["repo"] == "acme/a"
    assert cfg.hub_repo == "acme/hq"


def test_load_invalid_yaml_falls_back(tmp_path: Any) -> None:
    p = tmp_path / "squadhq.yml"
    p.write_text("org: {timezone: UTC}\nschedules: {stuck_sweeper: 'every 10m'}\n")
    cfg = load_schedule_config(str(p))
    assert any("invalid" in e for e in cfg.errors)
    assert {s.key for s in cfg.schedules} >= {"stuck_sweeper", "hub_janitor"}
    missing = load_schedule_config(str(tmp_path / "nope.yml"))
    assert {s.key for s in missing.schedules} == {"stuck_sweeper", "hub_janitor"}


def test_job_for() -> None:
    assert job_for("daily_brief") == ("hub_loop", {"mode": "brief", "schedule": "daily_brief"})
    assert job_for("chief_of_staff")[1]["mode"] == "cos"
    assert job_for("stuck_sweeper")[0] == "sweep"
    assert job_for("hub_janitor") == ("janitor", {"scope": "hub", "schedule": "hub_janitor"})
    assert job_for("hub_janitor_projects")[1]["scope"] == "projects"
    assert job_for("debt_scan")[0] == "hub_workflow"
    assert job_for("mystery") == ("scheduled", {"schedule": "mystery"})


# -------------------------------------------------------------- the loop

class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _cfg() -> Any:
    return load_schedule_config(inline={"org": {"timezone": "Europe/London"},
                                        "schedules": {"daily_brief": "daily 07:30", "stuck_sweeper": "*/15 * * * *"}})


def test_tick_enqueues_due_jobs_once(store: Any) -> None:
    clock = Clock(u(2026, 12, 1, 7, 0))
    s = Scheduler(store, _cfg(), holder="a", clock=clock)
    assert s.tick().enqueued == []  # first sight: cursor starts now, no back-fill
    clock.t = u(2026, 12, 1, 7, 31)
    r = s.tick()
    assert r.leader and sorted(r.enqueued) == ["daily_brief", "stuck_sweeper"]
    assert s.tick().enqueued == []  # same minute: nothing new
    jobs = {j.kind: j for j in store.list_jobs("queued")}
    assert jobs["hub_loop"].payload == {"mode": "brief", "schedule": "daily_brief",
                                        "fire_at": "2026-12-01T07:30:00.000000Z"}
    assert jobs["sweep"].payload["fire_at"] == "2026-12-01T07:30:00.000000Z"  # only the latest missed sweep
    assert len(store.list_jobs("queued")) == 2
    assert set(s.next_fires()) == {"daily_brief", "stuck_sweeper"}


def test_only_leader_enqueues_and_failover(store: Any) -> None:
    clock = Clock(u(2026, 12, 1, 7, 0))
    a = Scheduler(store, _cfg(), holder="a", clock=clock, lock_ttl_s=60)
    b = Scheduler(store, _cfg(), holder="b", clock=clock, lock_ttl_s=60)
    assert a.tick().leader
    assert not b.tick().leader
    # a dies; once its lock expires b takes over
    store.acquire_lock("scheduler", "a", -1)  # simulate expiry
    clock.t = u(2026, 12, 1, 7, 16)
    rb = b.tick()
    assert rb.leader and rb.enqueued == ["stuck_sweeper"]


def test_split_brain_is_idempotent(store: Any) -> None:
    clock = Clock(u(2026, 12, 1, 7, 0))
    a = Scheduler(store, _cfg(), holder="a", clock=clock)
    a.tick()
    clock.t = u(2026, 12, 1, 7, 16)
    a.tick()
    # replay the same fire as if a second leader raced: the idempotency key dedupes
    r = store.enqueue("sweep", {}, idempotency_key="sched:stuck_sweeper:2026-12-01T07:15:00.000000Z")
    assert r.created is False


def test_catchup_window(store: Any) -> None:
    clock = Clock(u(2026, 12, 1, 7, 0))
    s = Scheduler(store, _cfg(), holder="a", clock=clock, max_catchup=timedelta(hours=1))
    s.tick()
    clock.t = u(2026, 12, 5, 9, 0)  # down for days
    r = s.tick()
    assert sorted(r.enqueued) == ["stuck_sweeper"]  # the brief at 07:30 is outside the 1h window


def test_load_from_text() -> None:
    cfg = load_schedule_config(text="version: 1\norg: {name: A, timezone: Asia/Tokyo, approvers: ['@a/leads']}\nhub: {repo: a/hq}\n"
                                    "schedules: {daily_brief: 'daily 09:00'}\n")
    assert cfg.timezone == "Asia/Tokyo" and cfg.hub_repo == "a/hq" and not cfg.errors
    bad = load_schedule_config(text="org: {timezone: UTC}\nschedules: {stuck_sweeper: 'every 10m'}\n")
    assert bad.errors and bad.errors[0].startswith("squadhq.yml invalid")
    assert {s.key for s in bad.schedules} >= {"stuck_sweeper"}
