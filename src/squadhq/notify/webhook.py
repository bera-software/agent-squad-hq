"""Generic JSON webhook notifier.

Env: ``SQUADHQ_WEBHOOK_URL`` (or target ``webhook:https://...``),
``SQUADHQ_WEBHOOK_SECRET`` (optional). With a secret, the request carries
``X-SquadHQ-Signature-256: sha256=<hex HMAC-SHA256(secret, raw body)>`` —
the same scheme as GitHub's ``X-Hub-Signature-256``; receivers verify with
:func:`verify_signature`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from squadhq.http import Transport, request
from squadhq.interfaces import FatalError, Notification
from squadhq.notify._common import notification_dict

SIGNATURE_HEADER = "X-SquadHQ-Signature-256"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    return hmac.compare_digest(sign(secret, body), header_value.strip())


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str | None = None, *, secret: str | None = None, transport: Transport | None = None,
                 timeout: float = 15.0) -> None:
        self.url = url or os.environ.get("SQUADHQ_WEBHOOK_URL", "")
        self.secret = secret if secret is not None else os.environ.get("SQUADHQ_WEBHOOK_SECRET", "")
        self.transport = transport
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        if not self.url:
            raise FatalError("webhook: SQUADHQ_WEBHOOK_URL is not set")
        body = json.dumps(notification_dict(notification), separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers[SIGNATURE_HEADER] = sign(self.secret, body)
        request("POST", self.url, data=body, headers=headers, timeout=self.timeout, transport=self.transport)
