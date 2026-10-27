# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Bank transfer checkout: always offered, account details only when the owner
configured them, orders stay unpaid until an admin verifies against a bank
reference. Also covers the once-per-account signup alert. Network and DB faked."""
from __future__ import annotations

import datetime

import pytest

import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


def _admin_headers():
    token = app_mod.make_token({"sub": "admin", "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


# ── Providers / config ────────────────────────────────────────────────────────
def test_bank_is_always_offline_available(client, monkeypatch):
    monkeypatch.setattr(app_mod, "BANK_TRANSFER_ACCOUNTS", [])
    prov = client.get("/api/pay/providers").get_json()["providers"]
    assert prov["bank"] is True


def test_providers_expose_configured_accounts_only(client, monkeypatch):
    accounts = [{"bank": "DFCU", "account_name": "AgriBridge Ltd",
                 "account_number": "1234567890", "branch": "Main"}]
    monkeypatch.setattr(app_mod, "BANK_TRANSFER_ACCOUNTS", accounts)
    body = client.get("/api/pay/providers").get_json()
    assert body["bank_accounts"] == accounts


def test_no_hardcoded_bank_account_in_source():
    from pathlib import Path
    repo = Path(app_mod.__file__).resolve().parent
    html = (repo / "static" / "index.html").read_text(encoding="utf-8")
    assert "9030" not in html and "Stanbic" not in html


# ── Initiate is a no-op for bank ──────────────────────────────────────────────
def test_initiate_bank_returns_offline_mode(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("user-1", None))
    r = client.post("/api/pay/initiate",
                    json={"provider": "bank", "order_ref": "AB-1", "amount": 5000})
    assert r.status_code == 200
    assert r.get_json()["mode"] == "bank"


# ── Admin verification ────────────────────────────────────────────────────────
def _order_rows(monkeypatch, target, checkout):
    def fake_get(table, filters=None, limit=100):
        if table != "orders":
            return []
        if filters and "id" in filters:
            return target
        return checkout
    monkeypatch.setattr(app_mod, "supa_get", fake_get)


def test_verify_bank_requires_admin(client):
    assert client.post("/api/admin/orders/o1/verify-bank-transfer",
                       json={"bank_reference": "TRX-123"}).status_code == 401


def test_verify_bank_rejects_short_reference(client):
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "x"}, headers=_admin_headers())
    assert r.status_code == 400
    assert r.get_json()["code"] == "bank_reference_required"


def test_verify_bank_refuses_non_bank_order(client, monkeypatch):
    _order_rows(monkeypatch, [{"id": "o1", "payment_ref": "AB-1", "payment_method": "momo",
                               "payment_status": "unpaid", "status": "pending"}], [])
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "TRX-123"}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()["code"] == "not_a_bank_order"


def test_verify_bank_refuses_cancelled_order(client, monkeypatch):
    _order_rows(monkeypatch, [{"id": "o1", "payment_ref": "AB-1", "payment_method": "bank",
                               "payment_status": "unpaid", "status": "cancelled"}], [])
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "TRX-123"}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()["code"] == "order_cancelled"


def test_verify_bank_marks_paid_with_stored_amount(client, monkeypatch):
    target = [{"id": "o1", "payment_ref": "AB-1", "payment_method": "bank",
               "payment_status": "unpaid", "status": "pending"}]
    checkout = [{"id": "o1", "total_price": 40000, "total_amount": None,
                 "quantity_kg": None, "price_per_kg": None,
                 "status": "pending", "payment_status": "unpaid"}]
    _order_rows(monkeypatch, target, checkout)
    calls = {}

    def fake_mark(tx_ref, **kw):
        calls.update(tx_ref=tx_ref, **kw)
        return True

    monkeypatch.setattr(app_mod, "_mark_checkout_paid", fake_mark)
    monkeypatch.setattr(app_mod, "_log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "supa_insert", lambda *a, **k: None)
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "TRX-9001"}, headers=_admin_headers())
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert calls["tx_ref"] == "AB-1" and calls["provider"] == "bank"
    assert calls["provider_ref"] == "TRX-9001"
    assert int(calls["received"]) == 40000          # amount from the order, not the caller


def test_verify_bank_reports_already_paid(client, monkeypatch):
    target = [{"id": "o1", "payment_ref": "AB-1", "payment_method": "bank",
               "payment_status": "paid", "status": "delivered"}]
    checkout = [{"id": "o1", "total_price": 40000, "total_amount": None,
                 "quantity_kg": None, "price_per_kg": None,
                 "status": "delivered", "payment_status": "paid"}]
    _order_rows(monkeypatch, target, checkout)
    monkeypatch.setattr(app_mod, "_mark_checkout_paid", lambda *a, **k: True)
    monkeypatch.setattr(app_mod, "_log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "supa_insert", lambda *a, **k: None)
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "TRX-9001"}, headers=_admin_headers())
    assert r.status_code == 200 and r.get_json()["already_paid"] is True


def test_verify_bank_failure_is_not_success(client, monkeypatch):
    target = [{"id": "o1", "payment_ref": "AB-1", "payment_method": "bank",
               "payment_status": "unpaid", "status": "pending"}]
    checkout = [{"id": "o1", "total_price": 40000, "total_amount": None,
                 "quantity_kg": None, "price_per_kg": None,
                 "status": "pending", "payment_status": "unpaid"}]
    _order_rows(monkeypatch, target, checkout)
    monkeypatch.setattr(app_mod, "_mark_checkout_paid", lambda *a, **k: False)
    events = []
    monkeypatch.setattr(app_mod, "_log_system_event",
                        lambda et, d, **k: events.append(et))
    r = client.post("/api/admin/orders/o1/verify-bank-transfer",
                    json={"bank_reference": "TRX-9001"}, headers=_admin_headers())
    assert r.status_code == 503
    assert "bank_verify_failed" in events


# ── Signup alert fires once ───────────────────────────────────────────────────
class _Res:
    def __init__(self, ok=True, data=None):
        self.ok, self.data, self.error, self.status_code = ok, data or {}, None, 200


def _sync(monkeypatch, *, marker_present, created_at):
    auth_user = {"user_metadata": {"full_name": "Z"}, "created_at": created_at}

    # verify_supabase_user normally populates g.auth_user inside the request
    # context; mirror that here (patching g from outside a context raises).
    def _verify():
        app_mod.g.auth_user = auth_user
        return ("u1", None)

    monkeypatch.setattr(app_mod, "verify_supabase_user", _verify)
    monkeypatch.setattr(app_mod, "_auth_role", lambda: "farmer")
    monkeypatch.setattr(app_mod, "LEGAL_SERVICE",
                        type("L", (), {"sync_signup": lambda *a, **k: _Res(data={}),
                                       "status": lambda *a, **k: _Res(data={})})())

    def fake_get(table, filters=None, limit=100):
        if table == "system_events":
            return [{"id": "e1"}] if marker_present else []
        return []

    monkeypatch.setattr(app_mod, "supa_get", fake_get)
    logged, notified = [], []
    monkeypatch.setattr(app_mod, "_log_system_event",
                        lambda et, d, **k: logged.append(et))
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: notified.append(k))
    return logged, notified


def test_signup_alerts_once_for_a_new_account(monkeypatch):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    logged, notified = _sync(monkeypatch, marker_present=False, created_at=now)
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        assert c.post("/api/legal/sync-signup").status_code == 200
    assert "signup_alerted" in logged and len(notified) == 1


def test_signup_alert_suppressed_when_marker_exists(monkeypatch):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    logged, notified = _sync(monkeypatch, marker_present=True, created_at=now)
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        assert c.post("/api/legal/sync-signup").status_code == 200
    assert "signup_alerted" not in logged and notified == []


def test_signup_alert_suppressed_for_old_account(monkeypatch):
    old = (datetime.datetime.now(datetime.UTC)
           - datetime.timedelta(days=30)).isoformat()
    logged, notified = _sync(monkeypatch, marker_present=False, created_at=old)
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        assert c.post("/api/legal/sync-signup").status_code == 200
    assert notified == []
