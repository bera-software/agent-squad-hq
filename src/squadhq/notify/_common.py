"""Helpers shared by the notifier implementations."""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from typing import Any

from squadhq.interfaces import Notification

SEVERITY_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def notification_dict(n: Notification) -> dict[str, Any]:
    d = dataclasses.asdict(n)
    d["timestamp"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    d["source"] = "agent-squad-hq"
    return d


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def markdown_to_mrkdwn(md: str) -> str:
    """Basic GitHub-markdown -> Slack mrkdwn (links, bold, headings)."""
    out = _LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", md)
    out = _BOLD.sub(r"*\1*", out)
    out = _HEADING.sub(r"*\1*", out)
    return out
