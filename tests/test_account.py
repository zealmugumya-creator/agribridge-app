# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Phase 10 / H6 — account deletion: service behaviour + API authz.

Verifies the safe-by-default contract: identity from the verified session (never
the body), anonymize-only unless the operator enables the irreversible purge,
honest failure reporting, and that a purge is only attempted against GoTrue when
explicitly enabled.
"""
from __future__ import annotations

import pytest
from conftest import FakeSupabase, RpcResult

import app as app_mod
from services.account import AccountService


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


# ── AccountService ────────────────────────────────────────────────────────────
def test_requires_user_id():
    svc = AccountService(FakeSupabase(), supabase_url="u", service_key="k")
    res = svc.delete_account(user_id="")
    assert not res.ok and res.status_code == 400


def test_anonymize_only_by_default():
    db = FakeSupabase(rpc_results={"request_account_deletion": RpcResult(ok=True, data={"status": "anonymized"})})
    svc = AccountService(db, supabase_url="u", service_key="k", hard_delete_enabled=False)
    res = svc.delete_account(user_id="u1")
    assert res.ok and res.status == "anonymized"
    # No purge RPC and no HTTP call were made.
    assert all(fn == "request_account_deletion" for fn, _ in db.rpc_calls)


def test_anonymize_failure_maps_forbidden():
    db = FakeSupabase(rpc_results={"request_account_deletion": RpcResult(ok=False, error="forbidden")})
    svc = AccountService(db, supabase_url="u", service_key="k")
    res = svc.delete_account(user_id="u1")
    assert not res.ok and res.status_code == 403


def test_hard_delete_purges_via_gotrue(monkeypatch):
    calls = {}

    def _fake_delete(url, headers=None, timeout=None):
        calls["url"] = url
        return _Resp(200)
    monkeypatch.setattr("services.account.requests.delete", _fake_delete)
    db = FakeSupabase(rpc_results={"request_account_deletion": RpcResult(ok=True, data={})})
    svc = AccountService(db, supabase_url="https://x.supabase.co", service_key="sk",
                         hard_delete_enabled=True)
    res = svc.delete_account(user_id="u1")
    assert res.ok and res.status == "purged"
    assert calls["url"].endswith("/auth/v1/admin/users/u1")
    assert any(fn == "mark_account_purged" for fn, _ in db.rpc_calls)


def test_hard_delete_reports_purge_failure_honestly(monkeypatch):
    monkeypatch.setattr("services.account.requests.delete", lambda *a, **k: _Resp(500))
    db = FakeSupabase(rpc_results={"request_account_deletion": RpcResult(ok=True, data={})})
    svc = AccountService(db, supabase_url="https://x.supabase.co", service_key="sk",
                         hard_delete_enabled=True)
    res = svc.delete_account(user_id="u1")
    # Anonymization succeeded but the purge did not — do not claim 'purged'.
    assert not res.ok and res.status == "anonymized" and res.status_code == 502


def test_hard_delete_unconfigured_auth(monkeypatch):
    db = FakeSupabase(rpc_results={"request_account_deletion": RpcResult(ok=True, data={})})
    svc = AccountService(db, supabase_url="", service_key="", hard_delete_enabled=True)
    res = svc.delete_account(user_id="u1")
    assert not res.ok and res.status_code == 500


# ── API endpoint ──────────────────────────────────────────────────────────────
def test_endpoint_requires_auth(client):
    r = client.post("/api/account/delete", json={})
    assert r.status_code == 401


def test_endpoint_uses_verified_identity_not_body(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("real-uid", None))
    captured = {}

    class _FakeAcct:
        def delete_account(self, **kw):
            captured.update(kw)
            from services.account import DeletionResult
            return DeletionResult(ok=True, status="anonymized")
    monkeypatch.setattr(app_mod, "ACCOUNT_SERVICE", _FakeAcct())
    r = client.post("/api/account/delete", json={"user_id": "attacker-chosen"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "anonymized"
    assert captured["user_id"] == "real-uid"        # body id ignored


def test_endpoint_surfaces_failure(client, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("uid", None))
    from services.account import DeletionResult

    class _FakeAcct:
        def delete_account(self, **kw):
            return DeletionResult(ok=False, status="failed", error="forbidden", status_code=403)
    monkeypatch.setattr(app_mod, "ACCOUNT_SERVICE", _FakeAcct())
    r = client.post("/api/account/delete", json={})
    assert r.status_code == 403


# ── open orders need manual review (privacy-by-design, brief s.11) ────────────
def test_open_orders_block_automatic_deletion():
    db = FakeSupabase(select_results={"orders": [{"id": "o1"}]})
    svc = AccountService(db, supabase_url="u", service_key="k")
    res = svc.delete_account(user_id="u1")
    assert not res.ok and res.status == "needs_review" and res.status_code == 409
    assert db.rpc_calls == []                      # nothing was anonymised


def test_open_order_check_covers_buyer_and_farmer_and_unfinished_states():
    db = FakeSupabase()
    AccountService(db, supabase_url="u", service_key="k").delete_account(user_id="u1")
    table, filters = db.select_calls[0]
    assert table == "orders"
    assert "buyer_id.eq.u1" in filters["or"] and "farmer_id.eq.u1" in filters["or"]
    assert filters["status"] == "in.(pending,confirmed,in_transit)"


def test_endpoint_files_a_review_request_when_orders_are_open(client, monkeypatch):
    from services.account import DeletionResult
    from services.legal import LegalService
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("uid", None))

    class _Acct:
        def delete_account(self, **kw):
            return DeletionResult(ok=False, status="needs_review", error="open orders", status_code=409)
    db = FakeSupabase()
    monkeypatch.setattr(app_mod, "ACCOUNT_SERVICE", _Acct())
    monkeypatch.setattr(app_mod, "LEGAL_SERVICE", LegalService(db))
    r = client.post("/api/account/delete", json={})
    assert r.status_code == 409 and r.get_json()["manual_review"] is True
    table, row = db.insert_calls[0]
    assert table == "privacy_requests" and row["request_type"] == "deletion" and row["user_id"] == "uid"
