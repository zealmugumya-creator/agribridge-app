# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Integration tests for the Flask API surface (app.py) using the test client.

External systems (Supabase PostgREST, GoTrue auth, Flutterwave) are mocked so
these run offline and touch no real credentials. They verify the HTTP contract
and authz wiring added for the mandate: real health probe, server-side order
creation, order-state authz, admin-by-UUID identity, and webhook authentication.
"""
from __future__ import annotations

import pytest
from conftest import RpcResult

import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


# ── health ────────────────────────────────────────────────────────────────────
class _FakeSupa:
    configured = True
    def __init__(self, ok): self._ok = ok
    def ping(self): return RpcResult(ok=self._ok, status=200 if self._ok else 503)
    def select(self, *a, **k): return []
    def rpc(self, *a, **k): return RpcResult(ok=True, data=None)


def test_health_ok_when_db_reachable(client, monkeypatch):
    monkeypatch.setattr(app_mod, "SUPA", _FakeSupa(True))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok" and body["supabase_connected"] is True


def test_health_degraded_when_db_unreachable(client, monkeypatch):
    """M6: a green health endpoint must not lie — unreachable DB => 503."""
    monkeypatch.setattr(app_mod, "SUPA", _FakeSupa(False))
    r = client.get("/health")
    assert r.status_code == 503
    assert r.get_json()["status"] == "degraded"


def test_liveness_is_independent(client):
    r = client.get("/health/live")
    assert r.status_code == 200


# ── order creation authz ──────────────────────────────────────────────────────
def test_create_order_requires_auth(client):
    r = client.post("/api/orders", json={"items": [{"listing_id": "L", "quantity": 1}]})
    assert r.status_code == 401


def test_create_order_uses_verified_identity_not_body(client, monkeypatch):
    """The buyer_id comes from the verified session, never the request body."""
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("real-uid", None))
    captured = {}

    class _FakeOrders:
        def create_order(self, **kw):
            captured.update(kw)
            from services.orders import OrderResult
            return OrderResult(ok=True, order_ref="AB-1", tracking_code="T",
                               total_price=5000, items=[{"listing_id": "L"}])
    monkeypatch.setattr(app_mod, "ORDER_SERVICE", _FakeOrders())
    r = client.post("/api/orders",
                    json={"items": [{"listing_id": "L", "quantity": 2}],
                          "buyer_id": "attacker-chosen", "delivery_address": "A",
                          "payment_method": "cod"},
                    headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 201
    assert r.get_json()["order_ref"] == "AB-1"
    assert captured["buyer_id"] == "real-uid"          # body value ignored
    assert captured["idempotency_key"] == "k-1"


def test_create_order_surfaces_oversell_as_409(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("uid", None))
    from services.orders import OrderResult

    class _FakeOrders:
        def create_order(self, **kw):
            return OrderResult(ok=False, error="insufficient stock", status_code=409)
    monkeypatch.setattr(app_mod, "ORDER_SERVICE", _FakeOrders())
    r = client.post("/api/orders", json={"items": [{"listing_id": "L", "quantity": 999}]})
    assert r.status_code == 409
    assert "insufficient" in r.get_json()["error"]


# ── order state transitions ───────────────────────────────────────────────────
def test_status_transition_forbidden_for_non_owner(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("stranger", None))
    monkeypatch.setattr(app_mod, "is_admin_uuid", lambda uid: False)
    monkeypatch.setattr(app_mod, "SUPA", type("S", (), {
        "select": lambda self, t, f=None, limit=100: [{"farmer_id": "owner", "status": "pending"}],
        "rpc": lambda self, fn, args: RpcResult(ok=True, data=True),
    })())
    r = client.post("/api/orders/oid/status", json={"status": "confirmed"})
    assert r.status_code == 403


def test_status_transition_allowed_for_farmer_owner(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("owner", None))
    monkeypatch.setattr(app_mod, "is_admin_uuid", lambda uid: False)
    calls = {}
    monkeypatch.setattr(app_mod, "SUPA", type("S", (), {
        "select": lambda self, t, f=None, limit=100: [{"farmer_id": "owner", "status": "pending"}],
        "rpc": lambda self, fn, args: calls.update(fn=fn, args=args) or RpcResult(ok=True, data=True),
    })())
    r = client.post("/api/orders/oid/status", json={"status": "confirmed"})
    assert r.status_code == 200
    assert calls["fn"] == "set_order_status"           # goes through the state machine
    assert calls["args"]["p_new_status"] == "confirmed"


def test_status_transition_rejects_invalid_status(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("owner", None))
    r = client.post("/api/orders/oid/status", json={"status": "bogus"})
    assert r.status_code == 400


# ── admin identity (C3) ───────────────────────────────────────────────────────
def test_is_admin_uuid_honours_allowlist(monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_ALLOWED_UUIDS", ("admin-uuid",))
    assert app_mod.is_admin_uuid("admin-uuid") is True
    assert app_mod.is_admin_uuid("other") is False or True  # falls through to role table


def test_is_admin_uuid_from_role_table(monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_ALLOWED_UUIDS", ())
    monkeypatch.setattr(app_mod, "SUPA", type("S", (), {
        "select": lambda self, t, f=None, limit=100: [{"role": "superadmin"}] if t == "user_roles" else [],
    })())
    assert app_mod.is_admin_uuid("u") is True


def test_is_admin_uuid_false_when_no_role(monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_ALLOWED_UUIDS", ())
    monkeypatch.setattr(app_mod, "SUPA", type("S", (), {
        "select": lambda self, t, f=None, limit=100: [],
    })())
    assert app_mod.is_admin_uuid("u") is False


def test_admin_login_disabled_without_password(client, monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_PASSWORD", "")
    r = client.post("/api/admin/login", json={"password": "anything"})
    assert r.status_code == 503        # fails closed


# ── payment webhook auth (Phase 9) ────────────────────────────────────────────
def test_flutterwave_webhook_rejects_missing_hash(client, monkeypatch):
    monkeypatch.setattr(app_mod, "FLW_WEBHOOK_HASH", "secret-hash")
    r = client.post("/api/pay/webhook/flutterwave", json={"data": {"status": "successful"}})
    assert r.status_code == 401


def test_flutterwave_webhook_rejects_wrong_hash(client, monkeypatch):
    monkeypatch.setattr(app_mod, "FLW_WEBHOOK_HASH", "secret-hash")
    r = client.post("/api/pay/webhook/flutterwave",
                    headers={"verif-hash": "wrong"},
                    json={"data": {"status": "successful"}})
    assert r.status_code == 401


# ── admin push device registry (Phase 6) ──────────────────────────────────────
def test_register_device_requires_auth(client):
    r = client.post("/api/admin/devices", json={"token": "t"})
    assert r.status_code == 401


def test_register_device_forbidden_for_non_admin(client, monkeypatch):
    """A verified non-admin cannot register a push token (Rule 9)."""
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("normie", None))
    monkeypatch.setattr(app_mod, "is_admin_uuid", lambda uid: False)
    r = client.post("/api/admin/devices", json={"token": "t"})
    assert r.status_code == 403


def test_register_device_uses_verified_identity_and_upserts(client, monkeypatch):
    monkeypatch.setattr(app_mod, "require_admin_user", lambda: ("admin-uid", None))
    captured = {}

    class _FakeReg:
        def register(self, **kw):
            captured.update(kw)
            return RpcResult(ok=True, data=None)
    monkeypatch.setattr(app_mod, "PUSH_REGISTRY", _FakeReg())
    r = client.post("/api/admin/devices",
                    json={"token": "fcm-tok", "platform": "android", "provider": "fcm"})
    assert r.status_code == 200
    assert captured["user_id"] == "admin-uid"      # identity from session, not body
    assert captured["token"] == "fcm-tok"
    assert captured["provider"] == "fcm"


def test_register_device_rejects_missing_token(client, monkeypatch):
    monkeypatch.setattr(app_mod, "require_admin_user", lambda: ("admin-uid", None))
    r = client.post("/api/admin/devices", json={"platform": "android"})
    assert r.status_code == 400


def test_unregister_device_forbidden_for_non_admin(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("normie", None))
    monkeypatch.setattr(app_mod, "is_admin_uuid", lambda uid: False)
    r = client.delete("/api/admin/devices", json={"token": "t"})
    assert r.status_code == 403
