# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Tests for services.notifications.NotificationDispatcher (Phases 5–7).

Fakes the Supabase outbox RPCs so we can assert: dedupe on enqueue, atomic claim
+ resolve during drain, per-channel routing, and the alert-health rule that a
`critical` item is NOT silently dropped when its channel is unconfigured.
"""
from __future__ import annotations

from conftest import FakeSupabase, RpcResult

from services.discord import DiscordNotifier
from services.notifications import NotificationDispatcher


def test_enqueue_passes_dedupe_key_and_payload():
    db = FakeSupabase(rpc_results={"enqueue_notification": RpcResult(ok=True, data="nid-1")})
    d = NotificationDispatcher(supabase=db)
    out = d.enqueue(channel="discord", audience="admin", event_type="order.created",
                    severity="info", title="t", body="b", payload={"order_ref": "AB-1"},
                    dedupe_key="order.created:AB-1", correlation_id="c1")
    assert out == "nid-1"
    fn, args = db.rpc_calls[0]
    assert fn == "enqueue_notification"
    assert args["p_dedupe_key"] == "order.created:AB-1"
    assert args["p_payload"]["order_ref"] == "AB-1"


def test_drain_marks_sent_on_success():
    item = {"id": "n1", "channel": "discord", "severity": "info", "event_type": "e",
            "title": "t", "body": "b", "payload": {}, "attempts": 1, "max_attempts": 5}
    db = FakeSupabase(rpc_results={
        "claim_notifications": RpcResult(ok=True, data=[item]),
        "resolve_notification": RpcResult(ok=True),
    })
    sent = []

    class FakeDiscord:
        enabled = True
        def send(self, msg):
            sent.append(msg.event_type)
            return True

    d = NotificationDispatcher(supabase=db, discord=FakeDiscord())
    stats = d.drain(batch_size=5)
    assert stats["claimed"] == 1 and stats["sent"] == 1
    assert sent == ["e"]
    resolved = [a for (f, a) in db.rpc_calls if f == "resolve_notification"]
    assert resolved and resolved[0]["p_ok"] is True


def test_critical_not_dropped_when_discord_unconfigured():
    """Alert-health: a critical incident with no Discord must fail (-> retry /
    dead-letter), never be resolved as a silent success."""
    item = {"id": "n2", "channel": "discord", "severity": "critical", "event_type": "db.down",
            "title": "t", "body": "b", "payload": {}, "attempts": 1, "max_attempts": 5}
    db = FakeSupabase(rpc_results={
        "claim_notifications": RpcResult(ok=True, data=[item]),
        "resolve_notification": RpcResult(ok=True),
    })
    d = NotificationDispatcher(supabase=db, discord=DiscordNotifier(""))  # disabled
    d.drain()
    resolved = [a for (f, a) in db.rpc_calls if f == "resolve_notification"]
    assert resolved[0]["p_ok"] is False
    assert "critical" in resolved[0]["p_error"]


def test_noncritical_discord_skips_cleanly_when_unconfigured():
    item = {"id": "n3", "channel": "discord", "severity": "info", "event_type": "e",
            "title": "t", "body": "b", "payload": {}, "attempts": 1, "max_attempts": 5}
    db = FakeSupabase(rpc_results={
        "claim_notifications": RpcResult(ok=True, data=[item]),
        "resolve_notification": RpcResult(ok=True),
    })
    d = NotificationDispatcher(supabase=db, discord=DiscordNotifier(""))
    d.drain()
    resolved = [a for (f, a) in db.rpc_calls if f == "resolve_notification"]
    assert resolved[0]["p_ok"] is True     # drains without wedging the queue


def test_push_routes_to_fcm_tokens():
    item = {"id": "n4", "channel": "push", "severity": "info", "event_type": "order.created",
            "title": "New order", "body": "AB-1", "payload": {"order_ref": "AB-1"},
            "recipient": None, "attempts": 1, "max_attempts": 5}
    db = FakeSupabase(rpc_results={
        "claim_notifications": RpcResult(ok=True, data=[item]),
        "resolve_notification": RpcResult(ok=True),
    })
    pushed = []
    d = NotificationDispatcher(
        supabase=db,
        fcm_sender=lambda tok, title, body, data: pushed.append((tok, data.get("event"))) or True,
        admin_token_lookup=lambda recip: ["tok-a", "tok-b"],
    )
    d.drain()
    assert pushed == [("tok-a", "order.created"), ("tok-b", "order.created")]


def test_bad_item_does_not_wedge_queue():
    """An item that raises during dispatch is resolved as failed, not stuck."""
    item = {"id": "n5", "channel": "discord", "severity": "info", "event_type": "e",
            "title": "t", "body": "b", "payload": {}, "attempts": 1, "max_attempts": 5}
    db = FakeSupabase(rpc_results={
        "claim_notifications": RpcResult(ok=True, data=[item]),
        "resolve_notification": RpcResult(ok=True),
    })

    class ExplodingDiscord:
        enabled = True
        def send(self, msg):
            raise RuntimeError("boom")

    d = NotificationDispatcher(supabase=db, discord=ExplodingDiscord())
    stats = d.drain()
    assert stats["claimed"] == 1
    resolved = [a for (f, a) in db.rpc_calls if f == "resolve_notification"]
    assert resolved[0]["p_ok"] is False
