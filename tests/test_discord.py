# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Tests for services.discord.DiscordNotifier (Phase 7) with a mocked transport.

Uses monkeypatched requests.Session.post so no real network call is made and no
webhook URL is ever needed. Verifies: disabled no-op, success, retry on 5xx /
429, no-retry on 4xx, dead-letter on total failure, and that the webhook URL is
never logged.
"""
from __future__ import annotations

import services.discord as discord_mod
from services.discord import DiscordMessage, DiscordNotifier


class FakeResp:
    def __init__(self, status, json_data=None, text=""):
        self.status_code = status
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json})
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return FakeResp(204)


def _patch(monkeypatch, session):
    monkeypatch.setattr(discord_mod.requests, "Session", lambda: session)
    # No real sleeping in tests.
    monkeypatch.setattr(discord_mod.time, "sleep", lambda *_: None)


def test_disabled_when_no_webhook_is_safe_noop():
    n = DiscordNotifier("")
    assert n.enabled is False
    assert n.send(DiscordMessage(event_type="x")) is False   # no exception, no send


def test_success_returns_true(monkeypatch):
    s = FakeSession([FakeResp(204)])
    _patch(monkeypatch, s)
    n = DiscordNotifier("https://discord.test/webhook")
    assert n.send(DiscordMessage(event_type="order.created", title="New order",
                                 fields={"order_ref": "AB-1"})) is True
    assert len(s.calls) == 1
    assert s.calls[0]["json"]["embeds"][0]["fields"]  # payload built


def test_retries_on_5xx_then_succeeds(monkeypatch):
    s = FakeSession([FakeResp(500), FakeResp(502), FakeResp(204)])
    _patch(monkeypatch, s)
    n = DiscordNotifier("https://discord.test/webhook")
    assert n.send(DiscordMessage(event_type="e")) is True
    assert len(s.calls) == 3


def test_respects_429_retry_after(monkeypatch):
    s = FakeSession([FakeResp(429, {"retry_after": 0.01}), FakeResp(200)])
    _patch(monkeypatch, s)
    n = DiscordNotifier("https://discord.test/webhook")
    assert n.send(DiscordMessage(event_type="e")) is True


def test_does_not_retry_on_4xx(monkeypatch):
    s = FakeSession([FakeResp(400, text="bad request")])
    _patch(monkeypatch, s)
    n = DiscordNotifier("https://discord.test/webhook")
    assert n.send(DiscordMessage(event_type="e")) is False
    assert len(s.calls) == 1        # gave up immediately


def test_dead_letter_called_on_total_failure(monkeypatch):
    s = FakeSession([FakeResp(500)] * 6)   # exhausts all attempts
    _patch(monkeypatch, s)
    captured = []
    n = DiscordNotifier("https://discord.test/webhook",
                        dead_letter=lambda msg, err: captured.append((msg.event_type, err)))
    assert n.send(DiscordMessage(event_type="db.down", severity="critical")) is False
    assert captured and captured[0][0] == "db.down"


def test_webhook_url_never_logged(monkeypatch):
    logged = []
    s = FakeSession([FakeResp(500)] * 6)
    _patch(monkeypatch, s)
    monkeypatch.setattr(discord_mod.log, "error", lambda evt, **kw: logged.append((evt, kw)))
    n = DiscordNotifier("https://discord.test/SECRET-WEBHOOK-TOKEN")
    n.send(DiscordMessage(event_type="e"))
    blob = repr(logged)
    assert "SECRET-WEBHOOK-TOKEN" not in blob
