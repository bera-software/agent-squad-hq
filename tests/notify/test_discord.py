import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError, Notification
from squadhq.notify import NAMES, get_notifier
from squadhq.notify.discord import DiscordNotifier, build_payload

N = Notification(title="Capacity breaker tripped", body_markdown="**Paused** @everyone", severity="critical",
                 kind="incident", url="https://x.test/r/1", project="acme/app")


def test_payload_limits_and_no_mentions():
    p = build_payload(Notification(title="t" * 400, body_markdown="b" * 5000))
    e = p["embeds"][0]
    assert len(e["title"]) <= 256 and len(e["description"]) <= 4096
    assert p["allowed_mentions"] == {"parse": []}
    full = build_payload(N)["embeds"][0]
    assert full["url"] == "https://x.test/r/1" and "acme/app" in full["footer"]["text"]


def test_send_and_errors(monkeypatch):
    t = FakeTransport().add("POST discord.test", (204, ""))
    DiscordNotifier("https://discord.test/api/webhooks/1/x", transport=t).send(N)
    assert t.calls[0]["body"]["embeds"][0]["color"] == 0xB60205
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(FatalError, match="not set"):
        DiscordNotifier().send(N)
    with pytest.raises(FatalError, match="https"):
        DiscordNotifier("http://discord.test/x").send(N)


def test_registry():
    assert "discord" in NAMES
    assert isinstance(get_notifier("discord:https://discord.test/x"), DiscordNotifier)
