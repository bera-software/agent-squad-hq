"""Microsoft Teams notifier (Workflows / incoming-webhook Adaptive Card).

Env: ``TEAMS_WEBHOOK_URL`` (a Power Automate "Post to a channel when a
webhook request is received" URL, or a legacy Office 365 connector URL —
both accept the ``message`` + adaptive-card attachment envelope).
"""
from __future__ import annotations

import os
from typing import Any

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification
from squadhq.notify._common import SEVERITY_EMOJI, truncate

_COLOR = {"info": "Accent", "warning": "Warning", "critical": "Attention"}


def build_payload(n: Notification) -> dict[str, Any]:
    facts = [{"title": "Severity", "value": n.severity}, {"title": "Kind", "value": n.kind}]
    if n.project:
        facts.append({"title": "Project", "value": n.project})
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder", "wrap": True,
             "color": _COLOR.get(n.severity, "Default"),
             "text": truncate(f"{SEVERITY_EMOJI.get(n.severity, '')} {n.title}", 300)},
            {"type": "TextBlock", "wrap": True, "text": truncate(n.body_markdown, 20000)},
            {"type": "FactSet", "facts": facts},
        ],
    }
    if n.url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": n.url}]
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card},
    ]}


class TeamsNotifier:
    name = "teams"

    def __init__(self, *, webhook_url: str | None = None, transport: Transport | None = None,
                 timeout: float = 15.0) -> None:
        self.webhook_url = webhook_url or os.environ.get("TEAMS_WEBHOOK_URL", "")
        self.transport = transport
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        if not self.webhook_url:
            raise FatalError("teams: TEAMS_WEBHOOK_URL is not set")
        request("POST", self.webhook_url, json_body=build_payload(notification), timeout=self.timeout,
                transport=self.transport)
