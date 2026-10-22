"""A small, dependency-free, timezone-aware cron parser.

Supports the classic five fields ``minute hour day-of-month month day-of-week``
with ``*``, lists (``1,15``), ranges (``1-5``), steps (``*/15``, ``10-50/10``),
month and weekday names (``jan``, ``mon-fri``), ``7`` as Sunday, and the macros
``@hourly @daily @midnight @weekly @monthly @yearly @annually``.

Day-of-month and day-of-week follow Vixie-cron semantics: when both are
restricted, a day matches if *either* matches.

Schedules are evaluated in local wall time of a ``zoneinfo`` zone:

* **Spring forward** (a wall time that does not exist): the fire happens at
  the first instant after the gap, i.e. the local time is shifted forward by
  the DST offset (01:30 Europe/London on the change day fires at 02:30 BST).
* **Fall back** (a wall time that happens twice): the fire happens once, at
  the first occurrence (``fold=0``).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = ["CronError", "CronExpr", "from_parts", "parse"]

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MAX_YEARS_AHEAD = 8  # Feb 29 on a Monday can be years away; bound the search


class CronError(ValueError):
    pass


def _value(tok: str, names: dict[str, int], lo: int, hi: int) -> int:
    t = tok.lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"bad value {tok!r}")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{v} out of range {lo}-{hi}")
    return v


def _field(spec: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    names = names or {}
    out: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty list item in {spec!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"bad step {step_s!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names, lo, hi), _value(b, names, lo, hi)
            if start > end:
                raise CronError(f"descending range {part!r}")
        else:
            start = _value(part, names, lo, hi)
            end = hi if step > 1 else start
        out.update(range(start, end + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class CronExpr:
    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool
    pairs: tuple[tuple[int, int], ...] = ()  # explicit (hour, minute) fire times; () = hours x minutes

    def times(self) -> list[tuple[int, int]]:
        if self.pairs:
            return sorted(self.pairs)
        return [(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.days
        dow_ok = ((d.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def matches_local(self, wall: datetime) -> bool:
        """Does the naive/aware *wall-clock* time match (seconds ignored)?"""
        return (wall.hour, wall.minute) in set(self.times()) and self._day_matches(wall.date())

    def next_after(self, after: datetime, tz: str | ZoneInfo = "UTC") -> datetime:
        """The first fire time strictly after ``after``, returned as an aware UTC datetime."""
        zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        after_utc = after.astimezone(timezone.utc)
        # start from the local wall date one day earlier: a gap-shifted candidate from the
        # previous wall minute can land after `after`.
        local = after_utc.astimezone(zone)
        day = local.date() - timedelta(days=1)
        times = self.times()
        limit = day + timedelta(days=366 * _MAX_YEARS_AHEAD)
        best: datetime | None = None
        while day <= limit:
            if self._day_matches(day):
                for h, m in times:
                    cand = _resolve(datetime.combine(day, time(h, m)), zone)
                    if cand > after_utc and (best is None or cand < best):
                        best = cand
                if best is not None:
                    # later wall days only produce later instants (gap shifts are < 1 day)
                    return best
            day += timedelta(days=1)
        raise CronError(f"{self.source!r} never fires within {_MAX_YEARS_AHEAD} years")

    def fires_between(self, start: datetime, end: datetime, tz: str | ZoneInfo = "UTC") -> list[datetime]:
        """All fire instants in ``(start, end]`` (UTC). Bounded to 10k results."""
        out: list[datetime] = []
        cur = start
        while len(out) < 10_000:
            nxt = self.next_after(cur, tz)
            if nxt > end.astimezone(timezone.utc):
                break
            out.append(nxt)
            cur = nxt
        return out


def _resolve(wall: datetime, zone: ZoneInfo) -> datetime:
    """Wall time -> UTC instant using the module's DST policy (see docstring)."""
    first = wall.replace(tzinfo=zone, fold=0)
    utc = first.astimezone(timezone.utc)
    # Non-existent wall time: round-tripping does not reproduce it. fold=0 already
    # applies the pre-transition offset, which lands just after the gap.
    return utc


def from_parts(source: str, *, times: tuple[tuple[int, int], ...], weekdays: frozenset[int] | None = None,
               days: frozenset[int] | None = None, months: frozenset[int] | None = None) -> CronExpr:
    """Build an expression from explicit local fire times (e.g. a ``squadhq.config`` LocalSchedule)."""
    if not times:
        raise CronError(f"{source!r} has no fire times")
    return CronExpr(
        source=source,
        minutes=frozenset(m for _, m in times),
        hours=frozenset(h for h, _ in times),
        days=frozenset(days) if days else frozenset(range(1, 32)),
        months=frozenset(months) if months else frozenset(range(1, 13)),
        weekdays=frozenset(weekdays) if weekdays is not None else frozenset(range(7)),
        dom_restricted=bool(days),
        dow_restricted=weekdays is not None,
        pairs=tuple(sorted(set(times))),
    )


def parse(expr: str) -> CronExpr:
    src = expr.strip()
    spec = _MACROS.get(src.lower(), src)
    parts = spec.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields, got {len(parts)} in {expr!r}")
    mi, hr, dom, mon, dow = parts
    weekdays = _field(dow, 0, 7, _DAYS)
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    return CronExpr(
        source=src,
        minutes=_field(mi, 0, 59),
        hours=_field(hr, 0, 23),
        days=_field(dom, 1, 31),
        months=_field(mon, 1, 12, _MONTHS),
        weekdays=frozenset(weekdays),
        dom_restricted=not dom.startswith("*"),
        dow_restricted=not dow.startswith("*"),
    )
