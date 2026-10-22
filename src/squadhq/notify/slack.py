"""Slack incoming-webhook notifier with Block Kit layout.

Env: ``SLACK_WEBHOOK_URL``. Target ``slack:#channel`` sets a ``channel``
override in the payload (honoured only by legacy webhooks; app-scoped
webhooks always post to their configured channel).
"""
from __future__ import annotations

import os
from typing import Any

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification
from squadhq.notify._common import SEVERITY_EMOJI, markdown_to_mrkdwn, truncate


def build_payload(n: Notification, channel: str | None = None) -> dict[str, Any]:
    emoji = SEVERITY_EMOJI.get(n.severity, "")
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": truncate(f"{emoji} {n.title}", 150), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": truncate(markdown_to_mrkdwn(n.body_markdown) or " ", 3000)}},
    ]
    ctx = [f"*{n.severity}*", n.kind]
    if n.project:
        ctx.append(f"`{n.project}`")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(ctx)}]})
    if n.url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open"}, "url": n.url},
        ]})
    payload: dict[str, Any] = {"text": truncate(f"{emoji} {n.title}", 300), "blocks": blocks}
    if channel:
        payload["channel"] = channel
    return payload


class SlackNotifier:
    name = "slack"

    def __init__(self, channel: str | None = None, *, webhook_url: str | None = None,
                 transport: Transport | None = None, timeout: float = 15.0) -> None:
        self.channel = channel or None
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        self.transport = transport
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        if not self.webhook_url:
            raise FatalError("slack: SLACK_WEBHOOK_URL is not set")
        request("POST", self.webhook_url, json_body=build_payload(notification, self.channel),
                timeout=self.timeout, transport=self.transport)
