"""Scheduler loop: leader-elected, idempotent enqueue of scheduled jobs.

Every replica runs the loop; only the holder of the ``scheduler`` store lock
enqueues. The lock has a TTL and is renewed every tick, so a crashed leader is
replaced within ``lock_ttl_s``. Even during a split-brain window the job
idempotency key ``sched:<key>:<fire-instant>`` makes double enqueues no-ops.

Missed fires (scheduler down) are *not* replayed one by one: on start-up only
the most recent missed fire is enqueued, so a long outage cannot flood the
queue.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from squadhq.scheduler.schedules import Schedule, ScheduleConfig, job_for
from squadhq.store.base import ConflictError, StateStore, parse_ts, ts, utcnow

log = logging.getLogger("squadhq.scheduler")

LOCK_NAME = "scheduler"
KV_NS = "scheduler"


@dataclass
class TickResult:
    leader: bool
    enqueued: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(self, store: StateStore, config: ScheduleConfig, *, holder: str | None = None,
                 lock_ttl_s: float = 90.0, max_catchup: timedelta = timedelta(hours=24),
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.config = config
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lock_ttl_s = lock_ttl_s
        self.max_catchup = max_catchup
        self.clock = clock
        self._stop = threading.Event()

    def _last(self, key: str) -> tuple[datetime | None, int | None]:
        got = self.store.kv_get(KV_NS, f"last:{key}")
        if not got:
            return None, None
        value, version = got
        return (parse_ts(value) if isinstance(value, str) else None), version

    def due(self, sched: Schedule, now: datetime) -> datetime | None:
        """Latest fire instant in ``(last, now]``; ``None`` when nothing is due.

        On first sight of a schedule the cursor starts at ``now`` (no back-fill).
        """
        last, _ = self._last(sched.key)
        if last is None:
            return None
        floor = max(last, now - self.max_catchup)
        latest: datetime | None = None
        cur = floor
        for _ in range(10_000):
            nxt = sched.cron.next_after(cur, self.config.tz_for(sched))
            if nxt > now:
                break
            latest = nxt
            cur = nxt
        return latest

    def tick(self) -> TickResult:
        now = self.clock().astimezone(timezone.utc)
        if not self.store.acquire_lock(LOCK_NAME, self.holder, self.lock_ttl_s):
            return TickResult(leader=False)
        res = TickResult(leader=True)
        for sched in self.config.schedules:
            last, version = self._last(sched.key)
            if last is None:
                self._advance(sched.key, now, version)
                continue
            fire = self.due(sched, now)
            if fire is None:
                continue
            kind, payload = job_for(sched.key)
            payload = {**payload, "fire_at": ts(fire)}
            r = self.store.enqueue(kind, payload, idempotency_key=f"sched:{sched.key}:{ts(fire)}")
            if self._advance(sched.key, fire, version):
                if r.created:
                    res.enqueued.append(sched.key)
                    log.info("scheduled job enqueued", extra={"schedule": sched.key, "job_id": r.job_id,
                                                              "fire_at": ts(fire)})
        return res

    def _advance(self, key: str, at: datetime, version: int | None) -> bool:
        try:
            self.store.kv_cas(KV_NS, f"last:{key}", ts(at), version)
            return True
        except ConflictError:
            log.warning("schedule cursor moved concurrently", extra={"schedule": key})
            return False

    def next_fires(self) -> dict[str, str]:
        now = self.clock()
        return {s.key: ts(s.cron.next_after(now, self.config.tz_for(s))) for s in self.config.schedules}

    def run_forever(self, interval_s: float = 20.0) -> None:  # pragma: no cover - loop wrapper
        log.info("scheduler started", extra={"holder": self.holder, "schedules": len(self.config.schedules),
                                             "timezone": self.config.timezone})
        for err in self.config.errors:
            log.error("invalid schedule", extra={"error": err})
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
                self._stop.wait(interval_s)
        finally:
            self.store.release_lock(LOCK_NAME, self.holder)

    def stop(self) -> None:
        self._stop.set()


def _utc_offset_minutes(tz: str, at: datetime) -> int:
    off = at.astimezone(ZoneInfo(tz)).utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def utc_crons(s: Schedule, tz: str, at: datetime) -> list[str]:
    """UTC cron line(s) equivalent to the schedule at the offset in effect at ``at``.

    The server itself evaluates local wall time (so DST never shifts a job);
    these lines are what an Actions-style UTC cron would need *today*.
    """
    c = s.cron
    if tz == "UTC" and not c.pairs and not c.source.startswith("@"):
        return [c.source]
    from types import SimpleNamespace

    local = SimpleNamespace(
        raw_utc=None, times=tuple(c.times()),
        dows=c.weekdays if c.dow_restricted else None,
        doms=c.days if c.dom_restricted else None,
        months=None if len(c.months) == 12 else c.months)
    try:
        from squadhq.config.schedules import to_utc_crons
    except ImportError:  # pragma: no cover - config ships with the core
        return []
    try:
        return list(to_utc_crons(local, _utc_offset_minutes(tz, at)))  # type: ignore[arg-type]
    except ValueError:  # e.g. day-of-month that crosses a UTC month boundary
        return []


def describe(config: ScheduleConfig, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or utcnow()
    out = []
    for s in config.schedules:
        tz = config.tz_for(s)
        nxt = s.cron.next_after(now, tz)
        crons = utc_crons(s, tz, nxt)
        off = _utc_offset_minutes(tz, nxt)
        out.append({
            "key": s.key, "source": s.source, "tz": tz,
            "cron": crons[0] if len(crons) == 1 else " | ".join(crons),  # resolved UTC cron(s)
            "utc_crons": crons,
            "utc_offset": f"{'+' if off >= 0 else '-'}{abs(off) // 60:02d}:{abs(off) % 60:02d}",
            "next": ts(nxt),
            "next_local": nxt.astimezone(ZoneInfo(tz)).isoformat(),
        })
    return out
