from __future__ import annotations

from typing import Any

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError, Notification, TransientError
from squadhq.notify import NAMES, get_notifier, parse_targets
from squadhq.notify.expo_push import EXPO_PUSH_URL, ExpoPushNotifier, PushDevice, plain_excerpt, topic_for

A = "ExponentPushToken[aaaa]"
B = "ExpoPushToken[bbbb]"


def _ok(n: int) -> FakeTransport:
    return FakeTransport().add("POST exp.host", (200, {"data": [{"status": "ok"}] * n}))


def test_registered_in_registry() -> None:
    assert "expo_push" in NAMES
    assert parse_targets("slack,expo_push") == ["slack", "expo_push"]
    n = get_notifier("expo_push", devices=[], transport=_ok(0))
    assert isinstance(n, ExpoPushNotifier) and n.name == "expo_push"


def test_topic_mapping() -> None:
    assert topic_for(Notification("t", "b", kind="approval")) == "approvals"
    assert topic_for(Notification("t", "b", kind="escalation")) == "escalations"
    assert topic_for(Notification("t", "b", kind="incident")) == "escalations"
    assert topic_for(Notification("t", "b", kind="digest")) == "brief"
    assert topic_for(Notification("t", "b", kind="report")) is None


def test_sends_only_to_subscribed_devices() -> None:
    t = _ok(1)
    devices = [PushDevice(A, ("approvals",)), PushDevice(B, ("brief",)), PushDevice("garbage", ("approvals",))]
    n = ExpoPushNotifier(devices, transport=t, access_token="expo-secret")
    n.send(Notification("Merge approval needed", "**PR** [#12](https://x/12) <!-- m -->", kind="approval",
                        severity="warning", url="https://x/12", project="acme/a", dedupe_key="approval:1"))
    [call] = t.calls
    assert call["url"] == EXPO_PUSH_URL
    assert call["headers"]["Authorization"] == "Bearer expo-secret"
    [msg] = call["body"]
    assert msg["to"] == A and msg["priority"] == "high" and msg["channelId"] == "approvals"
    assert msg["body"] == "PR #12"
    assert msg["data"] == {"kind": "approval", "severity": "warning", "topic": "approvals", "url": "https://x/12",
                           "project": "acme/a", "dedupe_key": "approval:1"}
    assert n.last_result.sent == 1


def test_skips_without_subscribers_or_unpushed_kind() -> None:
    t = _ok(0)
    n = ExpoPushNotifier([PushDevice(A, ("brief",))], transport=t)
    n.send(Notification("x", "y", kind="approval"))
    assert t.calls == [] and "no devices" in n.last_result.skipped_reason
    n.send(Notification("x", "y", kind="report"))
    assert "not pushed" in n.last_result.skipped_reason


def test_batches_of_100_and_callable_source() -> None:
    t = _ok(100)
    devices = [PushDevice(f"ExponentPushToken[{i}]", ("brief",)) for i in range(150)]
    n = ExpoPushNotifier(lambda: devices, transport=t, access_token="")
    n.send(Notification("Brief", "x" * 500, kind="digest"))
    assert [len(c["body"]) for c in t.calls] == [100, 50]
    assert "Authorization" not in t.calls[0]["headers"]
    assert len(t.calls[0]["body"][0]["body"]) == 160


def test_invalid_tokens_reported_and_errors() -> None:
    pruned: list[list[str]] = []
    t = FakeTransport().add("POST exp.host", (200, {"data": [
        {"status": "error", "details": {"error": "DeviceNotRegistered"}}, {"status": "ok"}]}))
    n = ExpoPushNotifier([PushDevice(A, ("brief",)), PushDevice(B, ("brief",))], transport=t,
                         on_invalid=pruned.append)
    n.send(Notification("b", "b", kind="digest"))
    assert pruned == [[A]] and n.last_result.sent == 1

    bad = FakeTransport().add("POST exp.host", (200, {"data": [{"status": "error", "message": "InvalidCredentials"}]}))
    with pytest.raises(FatalError):
        ExpoPushNotifier([PushDevice(A, ("brief",))], transport=bad).send(Notification("b", "b", kind="digest"))
    rate = FakeTransport().add("POST exp.host", (200, {"data": [
        {"status": "error", "details": {"error": "MessageRateExceeded"}}]}))
    with pytest.raises(TransientError):
        ExpoPushNotifier([PushDevice(A, ("brief",))], transport=rate).send(Notification("b", "b", kind="digest"))


def test_reads_devices_from_store(tmp_path: Any, monkeypatch: Any) -> None:
    from squadhq.store import open_store

    url = f"sqlite:///{tmp_path}/hq.db"
    s = open_store(url)
    s.upsert_device(expo_push_token=A, platform="ios", name="", topics=["escalations"], owner="token:v")
    s.close()
    monkeypatch.setenv("SQUADHQ_DB_URL", url)
    t = _ok(1)
    ExpoPushNotifier(transport=t).send(Notification("Escalation", "help", kind="escalation"))
    assert t.calls[0]["body"][0]["to"] == A


def test_plain_excerpt() -> None:
    assert plain_excerpt("# Title\n\n**bold** `code`\n> quote") == "Title bold code quote"
