"""Human-readable schedules -> UTC cron lines.

Grammar (case-insensitive)::

    hourly [:MM] [during WINDOW]
    every N(m|h) [during WINDOW]
    [DAYS] hourly ... / [DAYS] every ...          DAYS restricts the weekdays
    DAYS HH:MM[,HH:MM...]                           e.g. "weekdays 07:30", "sun,wed 04:00"
    monthly <Nth> HH:MM                             e.g. "monthly 1st 05:00"
    quarterly <Nth> HH:MM                           Jan/Apr/Jul/Oct
    <five cron fields>                              raw cron, taken as UTC verbatim

``DAYS`` is ``daily`` | ``weekdays`` | ``weekends`` | a list/range of
``sun mon tue wed thu fri sat`` (``mon-fri``, ``sun,wed``). ``WINDOW`` is
``working_hours`` (``org.working_hours`` on ``org.working_days``) or
``HH:MM-HH:MM`` (end exclusive).

DST: GitHub Actions cron is UTC-only and has no notion of a zone, so one
offset must be picked per render. ``org.dst`` chooses it:

* ``daylight`` (default) uses the zone's summer offset. Jobs run at the
  configured local time in summer and one hour *earlier* in winter, so a
  "07:30 brief" is never late.
* ``standard`` uses the winter offset (runs one hour *later* in summer).

Emitting two crons per schedule would double-fire jobs for half the year, so
we never do that. The chosen offset and the caveat are written into the
rendered comment, and ``squadhq doctor`` reports it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_DOW_NAMES = {v: k for k, v in DOW.items()}
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_RAW_FIELD = re.compile(r"^[0-9*,/\-A-Za-z]+$")
_CRON_NUM = re.compile(r"^[0-9*,/\-]+$")


class ScheduleError(ValueError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class LocalSchedule:
    """A schedule in local wall-clock time."""

    times: tuple[tuple[int, int], ...]
    dows: frozenset[int] | None = None  # None = every day
    doms: frozenset[int] | None = None
    months: frozenset[int] | None = None
    raw_utc: str | None = None  # set for raw cron pass-through


@dataclass(frozen=True)
class Offset:
    zone: str
    minutes: int  # minutes east of UTC actually used
    standard: int
    daylight: int
    policy: str

    @property
    def has_dst(self) -> bool:
        return self.standard != self.daylight

    def label(self) -> str:
        sign = "+" if self.minutes >= 0 else "-"
        m = abs(self.minutes)
        return f"UTC{sign}{m // 60:02d}:{m % 60:02d}"

    def caveat(self) -> str:
        if not self.has_dst:
            return ""
        delta = abs(self.daylight - self.standard)
        hrs = f"{delta // 60}h" if delta % 60 == 0 else f"{delta}m"
        if self.policy == "daylight":
            return f"run {hrs} earlier local time outside DST"
        return f"run {hrs} later local time during DST"


def parse_time(s: str) -> tuple[int, int]:
    m = _TIME.match(s.strip())
    if not m:
        raise ScheduleError(f"invalid time {s!r}", "use 24-hour HH:MM, e.g. 07:30")
    return int(m.group(1)), int(m.group(2))


def parse_window(s: str) -> tuple[tuple[int, int], tuple[int, int]]:
    parts = s.split("-")
    if len(parts) != 2:
        raise ScheduleError(f"invalid time window {s!r}", 'use "HH:MM-HH:MM", e.g. "07:00-20:00"')
    start = parse_time(parts[0])
    end = (24, 0) if parts[1].strip() == "24:00" else parse_time(parts[1])
    if end <= start:
        raise ScheduleError(f"time window {s!r} ends before it starts",
                            "overnight windows are not supported; split into two schedules or widen the window")
    return start, end


def parse_days(s: str) -> frozenset[int] | None:
    s = s.strip().lower()
    if s in ("daily", "everyday", "every day", "*"):
        return None
    if s == "weekdays":
        return frozenset({1, 2, 3, 4, 5})
    if s == "weekends":
        return frozenset({0, 6})
    out: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (p.strip()[:3] for p in part.split("-", 1))
            if a not in DOW or b not in DOW:
                raise ScheduleError(f"unknown day range {part!r}", "use sun mon tue wed thu fri sat")
            i, j = DOW[a], DOW[b]
            if j < i:
                raise ScheduleError(f"day range {part!r} runs backwards", "write it as e.g. mon-fri")
            out.update(range(i, j + 1))
        else:
            if part[:3] not in DOW:
                raise ScheduleError(f"unknown day {part!r}",
                                    "use daily, weekdays, weekends or sun,mon,tue,wed,thu,fri,sat")
            out.add(DOW[part[:3]])
    return frozenset(out) if len(out) < 7 else None


def _ordinal(s: str) -> int:
    m = re.match(r"^(\d{1,2})(st|nd|rd|th)?$", s)
    if not m or not 1 <= int(m.group(1)) <= 28:
        raise ScheduleError(f"invalid day of month {s!r}",
                            "use 1st..28th (29-31 do not exist in every month)")
    return int(m.group(1))


def _window_times(step_min: int, window: tuple[tuple[int, int], tuple[int, int]] | None,
                  align_minute: int | None = None) -> tuple[tuple[int, int], ...]:
    start, end = window or ((0, 0), (24, 0))
    s, e = start[0] * 60 + start[1], end[0] * 60 + end[1]
    out: list[tuple[int, int]] = []
    if align_minute is not None:  # hourly at :MM
        for h in range(24):
            t = h * 60 + align_minute
            if s <= t < e:
                out.append((h, align_minute))
    else:
        t = s
        while t < e:
            out.append((t // 60, t % 60))
            t += step_min
    if not out:
        raise ScheduleError("schedule window contains no run times", "widen the window or change the minute")
    return tuple(out)


def parse(spec: str, working_hours: str = "00:00-24:00", working_days: str = "daily") -> LocalSchedule:
    """Parse a schedule spec into a :class:`LocalSchedule`."""
    raw = spec.strip()
    if not raw:
        raise ScheduleError("empty schedule", 'e.g. "weekdays 07:30"')
    fields = raw.split()
    if len(fields) == 5 and all(_RAW_FIELD.match(f) for f in fields) and _CRON_NUM.match(fields[0]) \
            and _CRON_NUM.match(fields[1]):
        return LocalSchedule(times=(), raw_utc=raw)
    s = raw.lower()

    window: tuple[tuple[int, int], tuple[int, int]] | None = None
    window_days: frozenset[int] | None = None
    if " during " in s:
        s, w = s.split(" during ", 1)
        w = w.strip()
        if w == "working_hours":
            window = parse_window(working_hours)
            window_days = parse_days(working_days)
        else:
            window = parse_window(w)
    tokens = s.split()

    # optional DAYS prefix for hourly/every
    days: frozenset[int] | None = window_days
    if len(tokens) >= 2 and tokens[1] in ("hourly", "every"):
        days = parse_days(tokens[0])
        tokens = tokens[1:]

    if tokens and tokens[0] == "hourly":
        minute = 0
        if len(tokens) == 2:
            m = re.match(r"^:?([0-5]?\d)$", tokens[1])
            if not m:
                raise ScheduleError(f"invalid minute {tokens[1]!r} in {spec!r}", 'use e.g. "hourly :15"')
            minute = int(m.group(1))
        elif len(tokens) > 2:
            raise ScheduleError(f"cannot parse {spec!r}", 'use e.g. "hourly :15 during working_hours"')
        return LocalSchedule(times=_window_times(60, window, align_minute=minute), dows=days)

    if tokens and tokens[0] == "every":
        if len(tokens) != 2:
            raise ScheduleError(f"cannot parse {spec!r}", 'use e.g. "every 30m during working_hours"')
        m = re.match(r"^(\d+)\s*(m|min|h)$", tokens[1])
        if not m:
            raise ScheduleError(f"invalid interval {tokens[1]!r}", 'use minutes ("30m") or hours ("2h")')
        n, unit = int(m.group(1)), m.group(2)
        step = n if unit != "h" else n * 60
        if unit != "h" and (n <= 0 or 60 % n):
            raise ScheduleError(f"interval {n}m does not divide an hour",
                                "use 5m, 10m, 15m, 20m or 30m (cron cannot express other minute steps)")
        if unit == "h" and (n <= 0 or 24 % n):
            raise ScheduleError(f"interval {n}h does not divide a day", "use 1h, 2h, 3h, 4h, 6h, 8h or 12h")
        return LocalSchedule(times=_window_times(step, window), dows=days)

    if window is not None:
        raise ScheduleError(f"'during' only applies to hourly/every schedules: {spec!r}",
                            'e.g. "every 30m during working_hours"')

    if tokens and tokens[0] in ("monthly", "quarterly"):
        if len(tokens) != 3:
            raise ScheduleError(f"cannot parse {spec!r}", f'use e.g. "{tokens[0]} 1st 05:00"')
        dom = _ordinal(tokens[1])
        times = tuple(parse_time(t) for t in tokens[2].split(","))
        months = frozenset({1, 4, 7, 10}) if tokens[0] == "quarterly" else None
        return LocalSchedule(times=times, doms=frozenset({dom}), months=months)

    if len(tokens) == 2:
        days = parse_days(tokens[0])
        times = tuple(parse_time(t) for t in tokens[1].split(","))
        return LocalSchedule(times=times, dows=days)
    if len(tokens) == 1 and _TIME.match(tokens[0]):
        return LocalSchedule(times=(parse_time(tokens[0]),))

    raise ScheduleError(
        f"cannot parse schedule {spec!r}",
        'examples: "weekdays 07:30", "every 30m during working_hours", "sun,wed 04:00", '
        '"hourly", "quarterly 1st 05:00", or raw UTC cron "30 6 * * 1-5"',
    )


def zone_offset(tz: str, policy: str = "daylight", ref: date | None = None) -> Offset:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"unknown timezone {tz!r}", "use an IANA name such as Europe/London or America/New_York") from e
    year = (ref or date.today()).year
    offs = []
    for month in (1, 7):
        off = datetime(year, month, 15, 12, tzinfo=zone).utcoffset() or timedelta(0)
        offs.append(int(off.total_seconds() // 60))
    std, dst = min(offs), max(offs)
    return Offset(zone=tz, minutes=dst if policy == "daylight" else std, standard=std, daylight=dst, policy=policy)


def _compress(values: list[int], full: range, allow_step: bool = True) -> str:
    vals = sorted(set(values))
    if vals == list(full):
        return "*"
    if allow_step and len(vals) > 2:
        step = vals[1] - vals[0]
        if vals == list(range(vals[0], full.stop, step)) and vals[0] < step:
            return f"*/{step}" if vals[0] == full.start else f"{vals[0]}-{full.stop - 1}/{step}"
    parts: list[str] = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{vals[i]}-{vals[j]}")
        else:
            parts.extend(str(v) for v in vals[i:j + 1])
        i = j + 1
    return ",".join(parts)


def to_utc_crons(sched: LocalSchedule, offset_minutes: int) -> list[str]:
    """Convert to one or more UTC cron expressions (more than one only when
    the local times straddle UTC midnight differently or minute sets differ)."""
    if sched.raw_utc:
        return [sched.raw_utc]
    by_shift: dict[int, dict[int, set[int]]] = {}
    for h, m in sched.times:
        total = h * 60 + m - offset_minutes
        shift, t = divmod(total, 1440)
        by_shift.setdefault(shift, {}).setdefault(t // 60, set()).add(t % 60)
    every_day = sched.dows is None and sched.doms is None
    if every_day:  # day shift irrelevant: merge
        merged: dict[int, set[int]] = {}
        for hours in by_shift.values():
            for hr, mins in hours.items():
                merged.setdefault(hr, set()).update(mins)
        by_shift = {0: merged}
    out: list[str] = []
    for shift in sorted(by_shift):
        hours = by_shift[shift]
        groups: dict[tuple[int, ...], list[int]] = {}
        for hr in sorted(hours):
            groups.setdefault(tuple(sorted(hours[hr])), []).append(hr)
        dow_f = "*"
        if sched.dows is not None:
            dow_f = _compress([(d + shift) % 7 for d in sched.dows], range(0, 7), False)
        dom_f = "*"
        if sched.doms is not None:
            doms = [d + shift for d in sched.doms]
            if any(d < 1 or d > 28 for d in doms):
                raise ScheduleError(
                    "day-of-month schedule falls on the previous month in UTC",
                    "pick a later local time (so UTC stays on the same day) or a later day of month",
                )
            dom_f = _compress(doms, range(1, 32))
        mon_f = "*" if sched.months is None else _compress(list(sched.months), range(1, 13), False)
        for mkey, hrs in sorted(groups.items(), key=lambda kv: kv[1][0]):
            out.append(f"{_compress(list(mkey), range(0, 60))} {_compress(hrs, range(0, 24))} {dom_f} {mon_f} {dow_f}")
    return out


def render(spec: str, tz: str, policy: str = "daylight", working_hours: str = "00:00-24:00",
           working_days: str = "daily", ref: date | None = None) -> tuple[list[str], str]:
    """Return ``(crons, comment)`` for one schedule spec."""
    sched = parse(spec, working_hours, working_days)
    if sched.raw_utc:
        return [sched.raw_utc], f"{spec} (raw UTC cron)"
    off = zone_offset(tz, policy, ref)
    crons = to_utc_crons(sched, off.minutes)
    comment = f"{spec} {tz} -> {off.label()}"
    if off.caveat():
        comment += f"; {off.caveat()}"
    return crons, comment
