from datetime import date

import pytest

from squadhq.config import schedules as S

REF = date(2026, 1, 1)


def r(spec, tz="Europe/London", policy="daylight", wh="07:00-20:00", wd="daily"):
    return S.render(spec, tz, policy, wh, wd, REF)[0]


@pytest.mark.parametrize("spec,expected", [
    ("weekdays 07:30", ["30 6 * * 1-5"]),
    ("sun,wed 04:00", ["0 3 * * 0,3"]),
    ("hourly", ["0 * * * *"]),
    ("hourly :15", ["15 * * * *"]),
    ("every 30m during working_hours", ["0,30 6-18 * * *"]),
    ("hourly :15 during working_hours", ["15 6-18 * * *"]),
    ("quarterly 1st 05:00", ["0 4 1 1,4,7,10 *"]),
    ("monthly 15th 09:00", ["0 8 15 * *"]),
    ("daily 03:30", ["30 2 * * *"]),
    ("weekends 10:00", ["0 9 * * 0,6"]),
    ("mon-fri 09:00,15:00", ["0 8,14 * * 1-5"]),
    ("every 2h", ["0 1-23/2 * * *"]),
    ("every 15m", ["*/15 * * * *"]),
    ("30 6 * * 1-5", ["30 6 * * 1-5"]),
    ("07:30", ["30 6 * * *"]),
    ("weekdays every 30m during 09:00-10:00", ["0,30 8 * * 1-5"]),
])
def test_london_daylight(spec, expected):
    assert r(spec) == expected


def test_every_2h_utc_zone():
    assert r("every 2h", tz="UTC") == ["0 */2 * * *"]


def test_standard_policy_uses_winter_offset():
    assert r("weekdays 07:30", policy="standard") == ["30 7 * * 1-5"]


def test_day_shift_backwards_east_of_utc():
    # 01:00 Monday in Tokyo (UTC+9, no DST) is 16:00 Sunday UTC
    assert r("mon 01:00", tz="Asia/Tokyo") == ["0 16 * * 0"]


def test_day_shift_forward_west_of_utc():
    # 22:00 Saturday New York (summer UTC-4) is 02:00 Sunday UTC
    assert r("sat 22:00", tz="America/New_York") == ["0 2 * * 0"]


def test_monthly_shift_forward_ok_and_backward_error():
    assert r("monthly 1st 22:00", tz="America/New_York") == ["0 2 2 * *"]
    with pytest.raises(S.ScheduleError) as e:
        r("monthly 1st 01:00", tz="Asia/Tokyo")
    assert "previous month" in e.value.message


def test_window_straddling_midnight_utc_splits_lines():
    out = r("weekdays every 30m during 20:00-24:00", tz="Asia/Kolkata")
    assert out == ["30 14 * * 1-5", "0,30 15-17 * * 1-5", "0 18 * * 1-5"]


def test_half_hour_zone_every_day_merges():
    assert r("every 30m during 20:00-24:00", tz="Asia/Kolkata") == ["30 14 * * *", "0,30 15-17 * * *", "0 18 * * *"]


def test_working_days_restricts():
    assert r("every 30m during working_hours", wd="mon-fri") == ["0,30 6-18 * * 1-5"]


def test_comment_and_caveat():
    crons, comment = S.render("weekdays 07:30", "Europe/London", "daylight", ref=REF)
    assert "UTC+01:00" in comment and "earlier" in comment
    _c, comment2 = S.render("weekdays 07:30", "Europe/London", "standard", ref=REF)
    assert "later" in comment2
    _c, comment3 = S.render("weekdays 07:30", "Asia/Tokyo", ref=REF)
    assert "DST" not in comment3
    _c, comment4 = S.render("0 1 * * *", "Asia/Tokyo", ref=REF)
    assert "raw UTC" in comment4


def test_offset_label_negative():
    off = S.zone_offset("America/St_Johns", "standard", REF)
    assert off.label() == "UTC-03:30"
    assert "later" in off.caveat()


@pytest.mark.parametrize("spec,needle", [
    ("", "empty"),
    ("every 7m", "divide an hour"),
    ("every 5h", "divide a day"),
    ("every x", "invalid interval"),
    ("every 5m 3", "cannot parse"),
    ("hourly :99", "invalid minute"),
    ("hourly :15 extra", "cannot parse"),
    ("weekdays 25:00", "invalid time"),
    ("funday 07:00", "unknown day"),
    ("fri-mon 07:00", "backwards"),
    ("mon-xyz 07:00", "unknown day range"),
    ("monthly 31st 07:00", "day of month"),
    ("monthly 1st", "cannot parse"),
    ("weekdays 07:30 during working_hours", "only applies"),
    ("every 30m during 20:00-07:00", "ends before"),
    ("every 30m during 20:00", "invalid time window"),
    ("hourly :05 during 07:10-07:20", "no run times"),
    ("whenever", "cannot parse"),
])
def test_errors(spec, needle):
    with pytest.raises(S.ScheduleError) as e:
        r(spec)
    assert needle in e.value.message


def test_unknown_zone():
    with pytest.raises(S.ScheduleError):
        S.zone_offset("Mars/Olympus")


def test_parse_days_variants():
    assert S.parse_days("daily") is None
    assert S.parse_days("sun-sat") is None
    assert S.parse_days("weekends") == frozenset({0, 6})
    assert S.parse_days("Monday,friday") == frozenset({1, 5})
