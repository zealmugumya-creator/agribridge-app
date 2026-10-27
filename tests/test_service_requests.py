# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Cold storage / refrigerated transport / loan requests.

These requests used to be a toast in the web app and were never saved, so the admin
console could not list them. They now go through POST /api/service-requests and the
admin console reads them via /api/admin/service-requests. Supabase is faked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app as app_mod

REPO = Path(__file__).resolve().parent.parent
GOOD_ID = "0b7e7d0e-8f0e-4c64-9c41-3f4d2a1b5c6d"


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer " + app_mod.make_token({"role": "admin"})}


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("user-1", None))


def _storage_body(**over):
    body = {
        "kind": "cold_storage",
        "provider_name": "Namanve Cold Rooms",
        "requester_name": "Amina",
        "requester_phone": "0771 234 567",
        "details": {"product": "Tomatoes", "pallets": 10},
    }
    body.update(over)
    return body


# ── submitting a request ──────────────────────────────────────────────────────
def test_submit_requires_login(client):
    assert client.post("/api/service-requests", json=_storage_body()).status_code == 401


def test_submit_rejects_unknown_kind(client, as_user):
    r = client.post("/api/service-requests", json=_storage_body(kind="helicopter"))
    assert r.status_code == 400


def test_submit_requires_name_and_valid_phone(client, as_user):
    assert client.post("/api/service-requests",
                       json=_storage_body(requester_name="  ")).status_code == 400
    assert client.post("/api/service-requests",
                       json=_storage_body(requester_phone="12")).status_code == 400


def test_loan_requires_a_positive_amount(client, as_user):
    base = _storage_body(kind="loan", provider_name="Harvest Advance")
    assert client.post("/api/service-requests", json=base).status_code == 400
    assert client.post("/api/service-requests",
                       json={**base, "amount_ugx": -5}).status_code == 400
    assert client.post("/api/service-requests",
                       json={**base, "amount_ugx": "lots"}).status_code == 400


def test_submit_saves_with_verified_identity_and_alerts_ops(client, as_user, monkeypatch):
    saved, alerts = {}, []

    def fake_insert(table, data):
        saved["table"], saved["row"] = table, data
        return [{"id": GOOD_ID}]

    monkeypatch.setattr(app_mod, "supa_insert", fake_insert)
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: alerts.append(a))

    body = _storage_body(user_id="attacker", status="completed")
    body["details"] = {"product": "Tomatoes", "pallets": 10, "nested": {"x": 1},
                       "flag": True, "long": "x" * 999}
    r = client.post("/api/service-requests", json=body)

    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "id": GOOD_ID, "ref": GOOD_ID[:8].upper()}
    row = saved["row"]
    assert saved["table"] == "service_requests"
    assert row["user_id"] == "user-1"            # from the session, not the body
    assert row["status"] == "pending"            # client cannot pre-approve itself
    assert row["requester_phone"] == "0771234567"
    assert row["details"]["pallets"] == 10
    assert "nested" not in row["details"] and "flag" not in row["details"]
    assert len(row["details"]["long"]) == 300
    assert len(alerts) == 1


def test_submit_never_claims_success_when_save_fails(client, as_user, monkeypatch):
    monkeypatch.setattr(app_mod, "supa_insert", lambda *a, **k: None)
    r = client.post("/api/service-requests", json=_storage_body())
    assert r.status_code == 500
    assert "ok" not in r.get_json()


# ── admin console ─────────────────────────────────────────────────────────────
def test_admin_list_requires_admin_token(client):
    assert client.get("/api/admin/service-requests").status_code == 401
    user_token = {"Authorization": "Bearer " + app_mod.make_token({"role": "user"})}
    assert client.get("/api/admin/service-requests", headers=user_token).status_code == 403


def test_admin_list_filters_by_kind(client, admin_headers, monkeypatch):
    seen = {}

    def fake_get(table, filters=None, limit=100):
        seen["table"], seen["filters"] = table, filters
        return [{"id": GOOD_ID, "kind": "loan"}]

    monkeypatch.setattr(app_mod, "supa_get", fake_get)
    r = client.get("/api/admin/service-requests?kind=loan", headers=admin_headers)
    assert r.status_code == 200 and r.get_json() == [{"id": GOOD_ID, "kind": "loan"}]
    assert seen["table"] == "service_requests"
    assert seen["filters"]["kind"] == "eq.loan"
    assert seen["filters"]["order"] == "created_at.desc"


def test_admin_list_rejects_unknown_kind(client, admin_headers):
    r = client.get("/api/admin/service-requests?kind=drop_table", headers=admin_headers)
    assert r.status_code == 400


def test_admin_update_status_and_notes(client, admin_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "supa_update",
                        lambda table, data, col, val: calls.append((table, data, col, val)) or True)
    r = client.patch(f"/api/admin/service-requests/{GOOD_ID}", headers=admin_headers,
                     json={"status": "confirmed", "admin_notes": " Called the farmer ",
                           "kind": "loan", "user_id": "x"})
    assert r.status_code == 200
    table, data, col, val = calls[0]
    assert (table, col, val) == ("service_requests", "id", GOOD_ID)
    assert data["status"] == "confirmed" and data["admin_notes"] == "Called the farmer"
    assert "updated_at" in data
    assert "kind" not in data and "user_id" not in data     # only whitelisted fields


def test_admin_update_validates_input(client, admin_headers):
    url = f"/api/admin/service-requests/{GOOD_ID}"
    assert client.patch(url, json={"status": "confirmed"}).status_code == 401
    assert client.patch(url, headers=admin_headers, json={"status": "bogus"}).status_code == 400
    assert client.patch(url, headers=admin_headers, json={}).status_code == 400
    bad = client.patch("/api/admin/service-requests/not-a-uuid", headers=admin_headers,
                       json={"status": "confirmed"})
    assert bad.status_code == 400


# ── schema ────────────────────────────────────────────────────────────────────
def test_migration_is_reversible_and_locked_down():
    up = (REPO / "supabase/migrations/0010_service_requests.up.sql").read_text().lower()
    down = REPO / "supabase/migrations/0010_service_requests.down.sql"
    assert down.exists()
    assert "enable row level security" in up
    assert "for insert" not in up          # users never write directly; backend does


def test_date_inputs_open_their_picker_on_tap():
    html = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "el.showPicker" in html and "el.type === 'date'" in html
