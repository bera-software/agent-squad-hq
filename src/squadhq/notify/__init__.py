"""Notifiers (``Notifier`` protocol in :mod:`squadhq.interfaces`) and fan-out.

Targets are ``name`` or ``name:argument``:

=============================  ===============================================
``github_issue[:owner/repo]``  find-or-create hub issue (dedupe by title marker)
``slack[:#channel]``           Slack incoming webhook (``SLACK_WEBHOOK_URL``)
``teams``                      Teams Workflows webhook (``TEAMS_WEBHOOK_URL``)
``email[:a@b.com,c@d.com]``    SMTP (``SQUADHQ_SMTP_*``)
``webhook[:https://...]``      JSON POST, optional HMAC (``SQUADHQ_WEBHOOK_*``)
``pagerduty``                  Events API v2 (``PAGERDUTY_ROUTING_KEY``)
``expo_push``                  Expo push to devices registered with the server
``discord[:https://...]``      Discord webhook (``DISCORD_WEBHOOK_URL``)
=============================  ===============================================

:func:`parse_targets` splits target lists on ``;`` and ``,``; a comma
segment that does not start with a known notifier name is glued back onto the
previous target, so ``github_issue,email:a@b.com,c@d.com,slack`` yields three
targets.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from squadhq.http import Transport
from squadhq.interfaces import Notification, Notifier

__all__ = ["NAMES", "get_notifier", "parse_targets", "fan_out"]

log = logging.getLogger(__name__)

NAMES = ("github_issue", "slack", "teams", "email", "webhook", "pagerduty", "expo_push", "discord")


def _name_of(segment: str) -> str:
    return segment.split(":", 1)[0].strip().lower()


def parse_targets(values: str | Iterable[str]) -> list[str]:
    """Split target strings (``;`` or ``,`` separated, repeatable) into a list."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        for chunk in value.split(";"):
            for seg in chunk.split(","):
                seg = seg.strip()
                if not seg:
                    continue
                if out and _name_of(seg) not in NAMES and ":" in out[-1]:
                    out[-1] = f"{out[-1]},{seg}"
                else:
                    out.append(seg)
    return out


def get_notifier(target: str, *, transport: Transport | None = None, **kwargs: Any) -> Notifier:
    """Build the notifier for one target string (see module docstring)."""
    name, _, arg = target.strip().partition(":")
    name = name.strip().lower()
    arg = arg.strip()
    if name == "github_issue":
        from squadhq.notify.github_issue import GitHubIssueNotifier

        return GitHubIssueNotifier(arg or None, transport=transport, **kwargs)
    if name == "slack":
        from squadhq.notify.slack import SlackNotifier

        return SlackNotifier(arg or None, transport=transport, **kwargs)
    if name == "teams":
        from squadhq.notify.teams import TeamsNotifier

        return TeamsNotifier(webhook_url=arg or None, transport=transport, **kwargs)
    if name == "email":
        from squadhq.notify.email import EmailNotifier

        return EmailNotifier(arg or None, **kwargs)
    if name == "webhook":
        from squadhq.notify.webhook import WebhookNotifier

        return WebhookNotifier(arg or None, transport=transport, **kwargs)
    if name == "pagerduty":
        from squadhq.notify.pagerduty import PagerDutyNotifier

        return PagerDutyNotifier(arg or None, transport=transport, **kwargs)
    if name == "expo_push":
        from squadhq.notify.expo_push import ExpoPushNotifier

        return ExpoPushNotifier(transport=transport, **kwargs)
    if name == "discord":
        from squadhq.notify.discord import DiscordNotifier

        return DiscordNotifier(arg or None, transport=transport, **kwargs)
    raise ValueError(f"unknown notifier {name!r}; expected one of {', '.join(NAMES)}")


def fan_out(notification: Notification, targets: str | Iterable[str], *,
            transport: Transport | None = None) -> dict[str, str | None]:
    """Send to every target. Returns ``{target: None | error message}``; never raises per target."""
    results: dict[str, str | None] = {}
    for target in parse_targets(targets):
        try:
            get_notifier(target, transport=transport).send(notification)
            results[target] = None
        except Exception as e:  # one broken channel must not silence the others
            log.warning("notify: %s failed: %s", target, e)
            results[target] = f"{type(e).__name__}: {e}"
    return results
