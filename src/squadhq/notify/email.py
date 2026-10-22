"""SMTP email notifier (``smtplib``; STARTTLS by default).

Env:
  ``SQUADHQ_SMTP_HOST`` (required), ``SQUADHQ_SMTP_PORT`` (default 587, or
  465 when TLS mode is ``ssl``), ``SQUADHQ_SMTP_USER``,
  ``SQUADHQ_SMTP_PASSWORD``, ``SQUADHQ_SMTP_FROM`` (default: the user),
  ``SQUADHQ_SMTP_TLS`` = ``starttls`` (default) | ``ssl`` | ``none``,
  ``SQUADHQ_NOTIFY_EMAIL_TO`` (default recipients for a bare ``email`` target).
"""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from squadhq.interfaces import FatalError, Notification, TransientError

SMTPFactory = Callable[..., Any]

_PREFIX = {"info": "", "warning": "[WARNING] ", "critical": "[CRITICAL] "}


def build_message(n: Notification, sender: str, to: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{_PREFIX.get(n.severity, '')}{n.title}"
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if n.dedupe_key:
        msg["X-SquadHQ-Dedupe-Key"] = n.dedupe_key
    text = n.body_markdown + (f"\n\n{n.url}" if n.url else "")
    msg.set_content(text)
    link = f'<p><a href="{html.escape(n.url, quote=True)}">Open</a></p>' if n.url else ""
    msg.add_alternative(
        f"<html><body><h2>{html.escape(n.title)}</h2>"
        f'<pre style="white-space:pre-wrap;font-family:inherit">{html.escape(n.body_markdown)}</pre>'
        f"{link}<p style=\"color:#888\">{html.escape(n.severity)} · {html.escape(n.kind)}</p></body></html>",
        subtype="html",
    )
    return msg


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        to: list[str] | str | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        tls: str | None = None,
        timeout: float = 30.0,
        smtp_factory: SMTPFactory | None = None,
    ) -> None:
        raw_to = to if to is not None else os.environ.get("SQUADHQ_NOTIFY_EMAIL_TO", "")
        if isinstance(raw_to, str):
            raw_to = [a for a in (p.strip() for p in raw_to.replace(";", ",").split(",")) if a]
        self.to = list(raw_to)
        self.host = host or os.environ.get("SQUADHQ_SMTP_HOST", "")
        self.tls = (tls or os.environ.get("SQUADHQ_SMTP_TLS") or "starttls").lower()
        if self.tls not in ("starttls", "ssl", "none"):
            raise FatalError(f"email: SQUADHQ_SMTP_TLS must be starttls|ssl|none, got {self.tls!r}")
        default_port = 465 if self.tls == "ssl" else 587
        self.port = port or int(os.environ.get("SQUADHQ_SMTP_PORT") or default_port)
        self.user = user if user is not None else os.environ.get("SQUADHQ_SMTP_USER", "")
        self.password = password if password is not None else os.environ.get("SQUADHQ_SMTP_PASSWORD", "")
        self.sender = sender or os.environ.get("SQUADHQ_SMTP_FROM") or self.user
        self.timeout = timeout
        self.smtp_factory = smtp_factory

    def _connect(self) -> Any:
        if self.smtp_factory is not None:
            return self.smtp_factory(self.host, self.port, timeout=self.timeout)
        if self.tls == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ssl.create_default_context())
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, notification: Notification) -> None:
        if not self.host:
            raise FatalError("email: SQUADHQ_SMTP_HOST is not set")
        if not self.to:
            raise FatalError("email: no recipients (use email:a@b.com or SQUADHQ_NOTIFY_EMAIL_TO)")
        if not self.sender:
            raise FatalError("email: SQUADHQ_SMTP_FROM (or SQUADHQ_SMTP_USER) is not set")
        msg = build_message(notification, self.sender, self.to)
        try:
            smtp = self._connect()
            try:
                if self.tls == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            finally:
                smtp.quit()
        except smtplib.SMTPAuthenticationError as e:
            raise FatalError(f"email: SMTP auth failed: {e}") from None
        except smtplib.SMTPRecipientsRefused as e:
            raise FatalError(f"email: recipients refused: {e}") from None
        except (smtplib.SMTPException, OSError) as e:
            raise TransientError(f"email: SMTP error: {e!r}") from None
