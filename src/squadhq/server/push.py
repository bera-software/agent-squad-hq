"""Queue mobile push notifications (sent by the worker through ``notify.expo_push``).

Pushes are jobs so a slow or failing Expo API never blocks the API or a
webhook handler, and so they get retries. Nothing is queued when no device
subscribes to the topic.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from squadhq.interfaces import Notification
from squadhq.store.base import StateStore


def enqueue_push(store: StateStore, n: Notification, key: str) -> str | None:
    from squadhq.notify.expo_push import topic_for

    topic = topic_for(n)
    if topic is None or not store.list_devices(topic):
        return None
    r = store.enqueue("push_notify", {"notification": dataclasses.asdict(n)}, idempotency_key=f"push:{key}",
                      max_attempts=3)
    return r.job_id


def notification_from(payload: dict[str, Any]) -> Notification:
    fields = {f.name for f in dataclasses.fields(Notification)}
    return Notification(**{k: v for k, v in payload.items() if k in fields})
