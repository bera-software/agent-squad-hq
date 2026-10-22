"""Expo push notifier: sends to mobile devices registered with the server.

Devices register through ``POST /api/v1/devices`` (server mode). Each
device picks topics; a notification goes only to devices subscribed to its
topic:

=================================  ===============
``Notification.kind``              topic
=================================  ===============
``approval``                       ``approvals``
``escalation``, ``incident``       ``escalations``
``digest`` (the daily brief)       ``brief``
=================================  ===============

Other kinds are not pushed. Messages go to
``https://exp.host/--/api/v2/push/send`` in batches of 100. Tokens Expo
reports as ``DeviceNotRegistered`` are passed to ``on_invalid`` (the server
deletes them).

Env: ``EXPO_ACCESS_TOKEN`` (optional; required when "enhanced push security"
is on for the Expo project), ``SQUADHQ_DB_URL`` (device list when no
``devices`` are passed in).

Push text is shown on a lock screen. Only the title and a short plain-text
excerpt are sent; the full body stays behind the authenticated API.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification, TransientError
from squadhq.notify._common import truncate

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
BATCH = 100
TOPIC_FOR_KIND = {"approval": "approvals", "escalation": "escalations", "incident": "escalations",
                  "digest": "brief"}
TOKEN_RE = re.compile(r"^(ExponentPushToken|ExpoPushToken)\[[^\]]+\]$")
_MD = re.compile(r"[*_`\[\]]|<!--.*?-->|\(https?://[^)]*\)|^\s*(#{1,6}|>)\s*", re.S | re.M)


@dataclass(frozen=True)
class PushDevice:
    token: str
    topics: tuple[str, ...] = ("approvals", "escalations")


@dataclass
class PushResult:
    sent: int = 0
    skipped_reason: str = ""
    invalid_tokens: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def topic_for(n: Notification) -> str | None:
    return TOPIC_FOR_KIND.get(n.kind)


def plain_excerpt(markdown: str, limit: int = 160) -> str:
    text = " ".join(_MD.sub("", markdown).split())
    return truncate(text, limit)


def _store_devices() -> list[PushDevice]:
    from squadhq.store import open_store

    store = open_store(os.environ.get("SQUADHQ_DB_URL", "sqlite:///squadhq.db"))
    try:
        return [PushDevice(d.expo_push_token, tuple(d.topics)) for d in store.list_devices()]
    finally:
        store.close()


class ExpoPushNotifier:
    name = "expo_push"

    def __init__(self, devices: Iterable[PushDevice] | Callable[[], Iterable[PushDevice]] | None = None, *,
                 access_token: str | None = None, transport: Transport | None = None, timeout: float = 15.0,
                 on_invalid: Callable[[list[str]], Any] | None = None, url: str = EXPO_PUSH_URL) -> None:
        self._devices = devices
        self.access_token = access_token if access_token is not None else os.environ.get("EXPO_ACCESS_TOKEN", "")
        self.transport = transport
        self.timeout = timeout
        self.on_invalid = on_invalid
        self.url = url
        self.last_result = PushResult()

    def _resolve_devices(self) -> list[PushDevice]:
        src = self._devices
        if src is None:
            return _store_devices()
        if callable(src):
            return list(src())
        return list(src)

    def messages(self, n: Notification, devices: Sequence[PushDevice]) -> list[dict[str, Any]]:
        topic = topic_for(n)
        data: dict[str, Any] = {"kind": n.kind, "severity": n.severity, "topic": topic}
        if n.url:
            data["url"] = n.url
        if n.project:
            data["project"] = n.project
        if n.dedupe_key:
            data["dedupe_key"] = n.dedupe_key
        return [{"to": d.token, "title": truncate(n.title, 100), "body": plain_excerpt(n.body_markdown),
                 "data": data, "sound": "default", "priority": "high" if n.severity != "info" else "default",
                 "channelId": topic or "default"}
                for d in devices if TOKEN_RE.match(d.token)]

    def send(self, notification: Notification) -> None:
        res = PushResult()
        self.last_result = res
        topic = topic_for(notification)
        if topic is None:
            res.skipped_reason = f"kind {notification.kind!r} is not pushed"
            return
        devices = [d for d in self._resolve_devices() if topic in d.topics]
        if not devices:
            res.skipped_reason = f"no devices subscribed to {topic}"
            return
        msgs = self.messages(notification, devices)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        for i in range(0, len(msgs), BATCH):
            batch = msgs[i:i + BATCH]
            resp = request("POST", self.url, json_body=batch, headers=headers, timeout=self.timeout,
                           transport=self.transport)
            tickets = (resp.json() or {}).get("data") or []
            for msg, ticket in zip(batch, tickets, strict=False):
                if not isinstance(ticket, dict):
                    continue
                if ticket.get("status") == "ok":
                    res.sent += 1
                    continue
                err = str((ticket.get("details") or {}).get("error") or ticket.get("message") or "error")
                if err == "DeviceNotRegistered":
                    res.invalid_tokens.append(str(msg["to"]))
                else:
                    res.errors.append(err)
        if res.invalid_tokens and self.on_invalid:
            self.on_invalid(res.invalid_tokens)
        if res.sent == 0 and res.errors:
            if "MessageRateExceeded" in res.errors:
                raise TransientError("expo_push: rate limited by Expo")
            raise FatalError(f"expo_push: every message failed: {', '.join(sorted(set(res.errors)))}")
