"""PagerDuty Events API v2 notifier.

Env: ``PAGERDUTY_ROUTING_KEY`` (integration key of an Events API v2
service). ``dedupe_key`` becomes PagerDuty's ``dedup_key`` so repeats update
one incident instead of paging again.
"""
from __future__ import annotations

import os
from typing import Any

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification
from squadhq.notify._common import truncate

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
SEVERITY_MAP = {"info": "info", "warning": "warning", "critical": "critical"}


def build_payload(n: Notification, routing_key: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "payload": {
            "summary": truncate(n.title, 1024),
            "source": n.project or "agent-squad-hq",
            "severity": SEVERITY_MAP.get(n.severity, "info"),
            "component": "agent-squad-hq",
            "class": n.kind,
            "custom_details": {"body": truncate(n.body_markdown, 10000)},
        },
    }
    if n.dedupe_key:
        payload["dedup_key"] = n.dedupe_key
    if n.url:
        payload["links"] = [{"href": n.url, "text": "Details"}]
    return payload


class PagerDutyNotifier:
    name = "pagerduty"

    def __init__(self, routing_key: str | None = None, *, events_url: str = EVENTS_URL,
                 transport: Transport | None = None, timeout: float = 15.0) -> None:
        self.routing_key = routing_key or os.environ.get("PAGERDUTY_ROUTING_KEY", "")
        self.events_url = events_url
        self.transport = transport
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        if not self.routing_key:
            raise FatalError("pagerduty: PAGERDUTY_ROUTING_KEY is not set")
        request("POST", self.events_url, json_body=build_payload(notification, self.routing_key),
                timeout=self.timeout, transport=self.transport)
