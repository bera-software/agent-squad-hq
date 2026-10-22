"""Discord webhook notifier (stdlib HTTP).

Env: ``DISCORD_WEBHOOK_URL``. Target ``discord:https://discord.com/api/webhooks/...``
overrides the URL. Posts one embed; Discord caps embed titles at 256 and
descriptions at 4096 characters.
"""
from __future__ import annotations

import os
from typing import Any

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification
from squadhq.notify._common import SEVERITY_EMOJI, truncate

# Embed side colours per severity (decimal RGB).
COLORS = {"info": 0x1D76DB, "warning": 0xFBCA04, "error": 0xD93F0B, "critical": 0xB60205}


def build_payload(n: Notification) -> dict[str, Any]:
    emoji = SEVERITY_EMOJI.get(n.severity, "")
    embed: dict[str, Any] = {
        "title": truncate(f"{emoji} {n.title}".strip(), 256),
        "description": truncate(n.body_markdown or " ", 4096),
        "color": COLORS.get(n.severity, COLORS["info"]),
        "footer": {"text": " · ".join(x for x in (n.severity, n.kind, n.project or "") if x)},
    }
    if n.url:
        embed["url"] = n.url
    # allowed_mentions: never let notification text ping @everyone or roles.
    return {"content": truncate(f"{emoji} {n.title}".strip(), 2000), "embeds": [embed],
            "allowed_mentions": {"parse": []}}


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str | None = None, *, transport: Transport | None = None,
                 timeout: float = 15.0) -> None:
        self.webhook_url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.transport = transport
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        if not self.webhook_url:
            raise FatalError("discord: DISCORD_WEBHOOK_URL is not set")
        if not self.webhook_url.startswith("https://"):
            raise FatalError("discord: webhook URL must be https")
        request("POST", self.webhook_url, json_body=build_payload(notification),
                timeout=self.timeout, transport=self.transport)
