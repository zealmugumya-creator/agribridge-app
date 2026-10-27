# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Supabase table webhook -> Discord/system_events, admin inbox, logistics gate."""
from __future__ import annotations

from pathlib import Path

import pytest

import app as app_mod

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(app_mod, "SUPABASE_WEBHOOK_SECRET", "s3cret")
    sent, logged = [], []
    monkeypatch.setattr(app_mod, "_discord_notify", lambda **k: sent.append(k))
    monkeypatch.setattr(app_mod, "_log_system_event", lambda *a, **k: logged.append((a, k)))
    return sent, logged


def _hook(client, body, key="s3cret"):
    return client.post("/api/hooks/supabase", json=body, headers={"X-Webhook-Secret": key})


def test_hook_rejects_missing_or_wrong_secret(client, secret):
    assert client.post("/api/hooks/supabase", json={}).status_code == 401
    assert _hook(client, {}, key="nope").status_code == 401


def test_hook_disabled_until_secret_is_configured(client, monkeypatch):
    monkeypatch.setattr(app_mod, "SUPABASE_WEBHOOK_SECRET", "")
    assert _hook(client, {}, key="").status_code == 401


def test_insert_goes_to_discord_and_activity_feed_without_pii(client, secret):
    sent, logged = secret
    r = _hook(client, {"type": "INSERT", "table": "contact_messages", "record": {
        "id": "1", "subject": "Help", "message": "x" * 500, "email": "a@b.com", "phone": "0771",
        "password": "pw"}})
    assert r.status_code == 200
    assert sent[0]["event_type"] == "support.contact_message"
    body = sent[0]["description"]
    assert "Help" in body and "a@b.com" not in body and "0771" not in body and "pw" not in body
    assert len(logged) == 1


def test_fraud_flag_is_critical(client, secret):
    sent, _ = secret
    _hook(client, {"type": "INSERT", "table": "fraud_flags", "record": {"id": "9", "reason": "dup"}})
    assert sent[0]["severity"] == "critical"


def test_unwatched_table_and_non_insert_are_ignored(client, secret):
    sent, _ = secret
    assert _hook(client, {"type": "INSERT", "table": "password_reset_tokens", "record": {"token": "t"}}).get_json()["ignored"]
    assert _hook(client, {"type": "UPDATE", "table": "payouts", "record": {}}).get_json()["ignored"]
    assert sent == []


def test_inbox_requires_admin_and_whitelists_tables(client, monkeypatch):
    assert client.get("/api/admin/inbox").status_code == 401
    h = {"Authorization": "Bearer " + app_mod.make_token({"role": "admin"})}
    assert client.get("/api/admin/inbox?table=password_reset_tokens", headers=h).status_code == 400
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [{"id": "1", "token": "x", "subject": "hi"}])
    rows = client.get("/api/admin/inbox?table=contact_messages", headers=h).get_json()
    assert rows == [{"id": "1", "subject": "hi"}]          # secrets stripped


def test_logistics_is_gated_and_links_back_to_real_admin_page():
    html = (REPO / "static/logistics.html").read_text(encoding="utf-8")
    assert 'href="/admin.html"' in html and "/static/admin.html" not in html
    assert 'id="gate"' in html and "/api/admin/verify" in html
    assert "localStorage.getItem('ab_admin_token')" not in html   # shares admin's session instead
