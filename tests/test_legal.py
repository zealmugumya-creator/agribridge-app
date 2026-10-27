# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Legal documents, acceptance, marketing consent and privacy requests.

Service logic runs against an in-memory fake of the database; the SQL guarantees
(append-only, RLS, immutability) are proven separately against real PostgreSQL in
tests/sql/legal_consent.test.mjs.
"""
from __future__ import annotations

import copy
import datetime as dt

import pytest
from conftest import FakeSupabase, RpcResult

import app as app_mod
from services import business
from services.legal import LegalService, normalize_role


class LegalDb(FakeSupabase):
    """Stateful fake for the tables the legal service touches."""
    def __init__(self):
        super().__init__()
        self.acceptances: list[dict] = []
        self.marketing: list[dict] = []
        self.requests: list[dict] = []
        self.fail_accept = False
        self.fail_insert = False

    def rpc(self, fn, args):
        self.rpc_calls.append((fn, args))
        if fn != "record_legal_acceptance":
            return RpcResult(ok=True)
        if self.fail_accept:
            return RpcResult(ok=False, status=503, error="db down")
        key = (args["p_user_id"], args["p_doc_type"], args["p_version"])
        if any((a["user_id"], a["doc_type"], a["version"]) == key for a in self.acceptances):
            return RpcResult(ok=True, data={"recorded": False, "already_accepted": True})
        self.acceptances.append({"user_id": args["p_user_id"], "doc_type": args["p_doc_type"],
                                 "version": args["p_version"], "hash": args["p_content_sha256"],
                                 "role": args["p_account_role"], "method": args["p_method"],
                                 "evidence": args["p_evidence"]})
        return RpcResult(ok=True, data={"recorded": True})

    def select(self, table, filters=None, limit=100):
        self.select_calls.append((table, filters))
        uid = (filters or {}).get("user_id", "").removeprefix("eq.")
        if table == "legal_acceptances":
            return [{"doc_type": a["doc_type"], "version": a["version"], "accepted_at": "2026-10-09T00:00:00Z",
                     "method": a["method"], "account_role": a["role"]}
                    for a in self.acceptances if a["user_id"] == uid]
        if table == "marketing_consent_events":
            return [{"id": i} for i, m in enumerate(self.marketing) if m["user_id"] == uid][:limit]
        if table == "marketing_consent_current":
            latest = {}
            for m in self.marketing:
                if m["user_id"] == uid:
                    latest[m["channel"]] = m
            return [{"channel": c, "granted": m["granted"], "recorded_at": "x"} for c, m in latest.items()]
        return []

    def insert(self, table, rows):
        self.insert_calls.append((table, rows))
        if self.fail_insert:
            return RpcResult(ok=False, status=500, error="boom")
        rows = rows if isinstance(rows, list) else [rows]
        if table == "marketing_consent_events":
            self.marketing.extend(copy.deepcopy(rows))
        if table == "privacy_requests":
            self.requests.extend(copy.deepcopy(rows))
            return RpcResult(ok=True, data=[{"id": "req-1"}])
        return RpcResult(ok=True, data=rows)


@pytest.fixture
def db():
    return LegalDb()


@pytest.fixture
def svc(db):
    return LegalService(db)


def _docs(svc, role):
    return [{"doc_type": d["doc_type"], "version": d["version"]} for d in svc.required_documents(role)]


# ── which documents each role sees ───────────────────────────────────────────
@pytest.mark.parametrize("role,extra", [
    ("farmer", "farmer-terms"), ("vendor", "buyer-terms"), ("hotel", "buyer-terms"),
    ("supplier", "supplier-terms"),
])
def test_each_role_sees_only_its_own_terms(svc, role, extra):
    types = {d["doc_type"] for d in svc.required_documents(role)}
    assert types == {"terms-of-use", "privacy-policy", extra}


def test_unknown_or_missing_role_defaults_to_buyer_terms(svc):
    for raw in (None, "", "banana", "admin", "superadmin"):
        types = {d["doc_type"] for d in svc.required_documents(raw)}
        assert "buyer-terms" in types and "farmer-terms" not in types


def test_role_aliases_match_the_web_app():
    assert normalize_role("restaurant") == "hotel" and normalize_role("b2b") == "hotel"
    assert normalize_role("input_seller") == "supplier"


def test_no_public_admin_terms(svc):
    types = {d["doc_type"] for r in ("farmer", "vendor", "hotel", "supplier")
             for d in svc.required_documents(r)}
    assert not any("admin" in t or "staff" in t for t in types)


# ── status, acceptance, versions ─────────────────────────────────────────────
def test_new_user_has_everything_outstanding(svc):
    st = svc.status("u1", "farmer").data
    assert not st["complete"] and len(st["outstanding"]) == 3


def test_acceptance_records_version_hash_role_and_method(svc, db):
    res = svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"),
                     method="login_review", evidence={"surface": "review_screen"})
    assert res.ok and len(res.data["recorded"]) == 3
    by = {a["doc_type"]: a for a in db.acceptances}
    for d in svc.required_documents("farmer"):
        rec = by[d["doc_type"]]
        assert rec["version"] == d["version"] and rec["hash"] == d["sha256"]
        assert rec["role"] == "farmer" and rec["method"] == "login_review"
        assert rec["evidence"]["document_url"] == d["versioned_url"]
    assert svc.status("u1", "farmer").data["complete"]


def test_existing_user_is_not_asked_again_for_unchanged_terms(svc):
    svc.accept(user_id="u1", role="vendor", documents=_docs(svc, "vendor"), method="login_review")
    for _ in range(3):
        assert svc.status("u1", "vendor").data["outstanding"] == []


def test_publishes_versions_before_recording(svc, db):
    svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    assert db.upsert_calls and db.upsert_calls[0][0] == "legal_document_versions"


def test_stale_version_is_refused_with_409(svc):
    docs = _docs(svc, "farmer")
    docs[0]["version"] = "0.0.1"
    res = svc.accept(user_id="u1", role="farmer", documents=docs, method="login_review")
    assert not res.ok and res.status_code == 409 and "current_version" in res.data


def test_cannot_accept_another_roles_document(svc):
    res = svc.accept(user_id="u1", role="vendor", method="login_review",
                     documents=[{"doc_type": "farmer-terms", "version": "1.0.0"}])
    assert not res.ok and res.status_code == 400


def test_invalid_method_and_empty_documents_rejected(svc):
    assert svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="silent").status_code == 400
    assert svc.accept(user_id="u1", role="farmer", documents=[], method="login_review").status_code == 400
    assert svc.accept(user_id="", role="farmer", documents=_docs(svc, "farmer"), method="login_review").status_code == 401


def test_the_deferred_method_cannot_be_chosen_by_the_client(svc):
    res = svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"),
                     method="signup_checkbox_deferred")
    assert not res.ok and res.status_code == 400


def test_failed_write_is_reported_and_recoverable(svc, db):
    db.fail_accept = True
    res = svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    assert not res.ok and "try again" in res.error
    assert res.data["recorded"] == []                       # nothing claimed as recorded
    assert not svc.status("u1", "farmer").data["complete"]
    db.fail_accept = False                                  # retry succeeds
    assert svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review").ok
    assert svc.status("u1", "farmer").data["complete"]


def test_publication_failure_blocks_acceptance(svc, db):
    db.upsert_result = RpcResult(ok=False, status=500, error="x")
    res = svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    assert not res.ok and res.status_code == 503 and db.acceptances == []


def _bumped(svc, doc_type, version, material, last_material=None):
    m = copy.deepcopy(svc.manifest)
    d = m["documents"][doc_type]
    d["version"], d["material"] = version, material
    d["last_material_version"] = last_material or version
    d["sha256"] = "f" * 64
    d["versioned_url"] = f"legal/{doc_type}-{version}.html"
    return LegalService(svc._db, manifest=m)


def test_material_update_triggers_review_but_only_for_that_document(svc):
    svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    new = _bumped(svc, "terms-of-use", "2.0.0", True)
    out = new.status("u1", "farmer").data["outstanding"]
    assert [d["doc_type"] for d in out] == ["terms-of-use"]


def test_non_material_correction_does_not_interrupt_users(svc):
    svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    new = _bumped(svc, "privacy-policy", "1.0.1", False, last_material="1.0.0")
    assert new.status("u1", "farmer").data["complete"]
    # but a brand-new user is still asked for the current version
    assert not new.status("u2", "farmer").data["complete"]


def test_old_acceptance_records_survive_a_new_version(svc, db):
    svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    new = _bumped(svc, "terms-of-use", "2.0.0", True)
    new.accept(user_id="u1", role="farmer", method="login_review",
               documents=[{"doc_type": "terms-of-use", "version": "2.0.0"}])
    versions = sorted(a["version"] for a in db.acceptances if a["doc_type"] == "terms-of-use")
    assert versions == ["1.0.0", "2.0.0"]


# ── sign-up sync ─────────────────────────────────────────────────────────────
def _now():
    return dt.datetime.now(dt.UTC).isoformat()


def test_signup_sync_records_what_was_ticked(svc, db):
    versions = {d["doc_type"]: d["version"] for d in svc.required_documents("farmer")}
    meta = {"legal_signup": {"accepted_at": _now(), "versions": versions}}
    res = svc.sync_signup(user_id="u1", role="farmer", meta=meta, created_at=_now())
    assert res.ok and len(res.data["recorded"]) == 3
    assert {a["method"] for a in db.acceptances} == {"signup_checkbox_deferred"}


def test_signup_sync_without_a_tick_records_nothing(svc, db):
    res = svc.sync_signup(user_id="u1", role="farmer", meta={}, created_at=_now())
    assert res.ok and res.data["recorded"] == [] and db.acceptances == []
    assert not svc.status("u1", "farmer").data["complete"]       # still asked on the review screen


def test_signup_sync_ignores_old_accounts_and_stale_versions(svc, db):
    versions = {d["doc_type"]: d["version"] for d in svc.required_documents("farmer")}
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=90)).isoformat()
    svc.sync_signup(user_id="u1", role="farmer", created_at=old,
                    meta={"legal_signup": {"versions": versions}})
    assert db.acceptances == []
    svc.sync_signup(user_id="u1", role="farmer", created_at=_now(),
                    meta={"legal_signup": {"versions": {k: "0.0.1" for k in versions}}})
    assert db.acceptances == []


def test_signup_sync_ignores_documents_for_other_roles(svc, db):
    meta = {"legal_signup": {"versions": {"farmer-terms": "1.0.0", "terms-of-use": "1.0.0"}}}
    svc.sync_signup(user_id="u1", role="vendor", meta=meta, created_at=_now())
    assert [a["doc_type"] for a in db.acceptances] == ["terms-of-use"]


# ── marketing consent ────────────────────────────────────────────────────────
def test_accepting_terms_never_records_marketing_consent(svc, db):
    svc.accept(user_id="u1", role="farmer", documents=_docs(svc, "farmer"), method="login_review")
    assert db.marketing == []
    assert svc.get_marketing("u1").data["channels"] == {"sms": False}


def test_marketing_is_separate_optional_and_withdrawable(svc, db):
    assert svc.set_marketing(user_id="u1", channels={"sms": True}, method="settings").ok
    assert svc.get_marketing("u1").data["channels"]["sms"] is True
    assert svc.set_marketing(user_id="u1", channels={"sms": False}, method="settings").ok
    assert svc.get_marketing("u1").data["channels"]["sms"] is False
    assert [m["granted"] for m in db.marketing] == [True, False]        # history kept


def test_marketing_no_duplicate_events_when_unchanged(svc, db):
    svc.set_marketing(user_id="u1", channels={"sms": True})
    res = svc.set_marketing(user_id="u1", channels={"sms": True})
    assert res.ok and res.data["changed"] == [] and len(db.marketing) == 1


@pytest.mark.parametrize("channel", ["email", "whatsapp", "push", "fax"])
def test_only_supported_channels_are_accepted(svc, channel):
    res = svc.set_marketing(user_id="u1", channels={channel: True})
    assert not res.ok and res.status_code == 400


def test_marketing_values_must_be_booleans(svc):
    assert svc.set_marketing(user_id="u1", channels={"sms": "yes"}).status_code == 400


def test_marketing_save_failure_is_reported(svc, db):
    db.fail_insert = True
    res = svc.set_marketing(user_id="u1", channels={"sms": True})
    assert not res.ok and res.status_code == 502


def test_signup_marketing_optin_is_recorded_only_when_ticked(svc, db):
    svc.sync_signup(user_id="u1", role="farmer", created_at=_now(), meta={"marketing_optin": {"sms": False}})
    assert db.marketing == []
    svc.sync_signup(user_id="u2", role="farmer", created_at=_now(), meta={"marketing_optin": {"sms": True}})
    assert [(m["user_id"], m["granted"], m["method"]) for m in db.marketing] == [("u2", True, "signup_checkbox")]


def test_signup_optin_never_overrides_a_later_withdrawal(svc, db):
    meta = {"marketing_optin": {"sms": True}}
    svc.sync_signup(user_id="u1", role="farmer", created_at=_now(), meta=meta)
    svc.set_marketing(user_id="u1", channels={"sms": False})
    svc.sync_signup(user_id="u1", role="farmer", created_at=_now(), meta=meta)
    assert svc.get_marketing("u1").data["channels"]["sms"] is False


def test_the_only_supported_channel_is_sms():
    assert business.MARKETING_CHANNELS == ("sms",)


# ── privacy requests ─────────────────────────────────────────────────────────
def test_privacy_request_without_session_needs_a_valid_email(svc):
    assert svc.create_privacy_request(request_type="access").status_code == 400
    assert svc.create_privacy_request(request_type="access", email="not-an-email").status_code == 400
    res = svc.create_privacy_request(request_type="access", email="a@b.co")
    assert res.ok and res.status_code == 201 and res.data["status"] == "verifying"
    assert res.data["contact"]["email"] == business.SUPPORT_EMAIL


def test_signed_in_request_is_tied_to_the_account(svc, db):
    res = svc.create_privacy_request(request_type="correction", user_id="u1", details="fix my district")
    assert res.ok and db.requests[0]["user_id"] == "u1" and db.requests[0]["identity_verified"] is True


def test_privacy_request_validation(svc):
    assert svc.create_privacy_request(request_type="hack", email="a@b.co").status_code == 400
    assert svc.create_privacy_request(request_type="access", email="a@b.co", details="x" * 2001).status_code == 400


def test_privacy_request_failure_points_to_email(svc, db):
    db.fail_insert = True
    res = svc.create_privacy_request(request_type="access", email="a@b.co")
    assert not res.ok and res.data["email"] == business.SUPPORT_EMAIL


# ═══════════════ API surface ════════════════════════════════════════════════
@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def api(monkeypatch, db):
    svc = LegalService(db)
    monkeypatch.setattr(app_mod, "LEGAL_SERVICE", svc)
    return svc


def _as(monkeypatch, uid, role="farmer", created=None):
    def fake():
        app_mod.g.auth_user = {"id": uid, "user_metadata": {"role": role}, "created_at": created or _now()}
        return uid, None
    monkeypatch.setattr(app_mod, "verify_supabase_user", fake)


def test_manifest_and_documents_are_public(client, api):
    r = client.get("/api/legal/manifest")
    assert r.status_code == 200 and "terms-of-use" in r.get_json()["documents"]
    for name in ("privacy-policy.html", "terms-of-use.html", "farmer-terms.html", "manifest.json"):
        assert client.get(f"/legal/{name}").status_code == 200


def test_legal_route_cannot_read_other_files(client):
    for bad in ("../app.py", "..%2fapp.py", "x.py", "manifest.json/../../app.py", ".env"):
        assert client.get(f"/legal/{bad}").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/api/legal/status"), ("post", "/api/legal/accept"), ("post", "/api/legal/sync-signup"),
    ("get", "/api/legal/history"), ("get", "/api/marketing/consent"), ("post", "/api/marketing/consent"),
    ("get", "/api/privacy/requests"),
])
def test_protected_legal_routes_require_a_session(client, api, method, path):
    assert getattr(client, method)(path, json={}).status_code == 401


def test_accept_uses_verified_identity_and_role_not_the_body(client, api, monkeypatch, db):
    _as(monkeypatch, "real-uid", role="supplier")
    docs = [{"doc_type": d["doc_type"], "version": d["version"]} for d in api.required_documents("supplier")]
    r = client.post("/api/legal/accept", json={"documents": docs, "method": "login_review",
                                               "user_id": "attacker", "role": "farmer"})
    assert r.status_code == 200 and r.get_json()["complete"] is True
    assert {a["user_id"] for a in db.acceptances} == {"real-uid"}
    assert {a["role"] for a in db.acceptances} == {"supplier"}


def test_accept_stale_version_returns_409(client, api, monkeypatch):
    _as(monkeypatch, "u1")
    r = client.post("/api/legal/accept", json={"method": "login_review",
                    "documents": [{"doc_type": "terms-of-use", "version": "0.0.1"}]})
    assert r.status_code == 409


def test_accept_failure_is_an_error_not_a_success(client, api, monkeypatch, db):
    _as(monkeypatch, "u1")
    db.fail_accept = True
    docs = [{"doc_type": d["doc_type"], "version": d["version"]} for d in api.required_documents("farmer")]
    r = client.post("/api/legal/accept", json={"documents": docs, "method": "login_review"})
    assert r.status_code >= 400 and "complete" not in r.get_json()


def test_users_cannot_pick_the_deferred_method(client, api, monkeypatch):
    _as(monkeypatch, "u1")
    docs = [{"doc_type": d["doc_type"], "version": d["version"]} for d in api.required_documents("farmer")]
    r = client.post("/api/legal/accept", json={"documents": docs, "method": "signup_checkbox_deferred"})
    assert r.status_code == 400


def test_sync_signup_endpoint_records_ticked_versions(client, api, monkeypatch, db):
    versions = {d["doc_type"]: d["version"] for d in api.required_documents("farmer")}

    def fake():
        app_mod.g.auth_user = {"id": "u1", "created_at": _now(),
                               "user_metadata": {"role": "farmer", "legal_signup": {"versions": versions},
                                                 "marketing_optin": {"sms": True}}}
        return "u1", None
    monkeypatch.setattr(app_mod, "verify_supabase_user", fake)
    r = client.post("/api/legal/sync-signup", json={})
    body = r.get_json()
    assert r.status_code == 200 and len(body["recorded"]) == 3 and body["status"]["complete"] is True
    assert body["marketing"] == ["sms"]


def test_marketing_endpoint_roundtrip_and_unsupported_channel(client, api, monkeypatch):
    _as(monkeypatch, "u1")
    assert client.post("/api/marketing/consent", json={"channels": {"sms": True}}).status_code == 200
    assert client.get("/api/marketing/consent").get_json()["channels"]["sms"] is True
    assert client.post("/api/marketing/consent", json={"channels": {"whatsapp": True}}).status_code == 400
    assert client.post("/api/marketing/consent", json={"channels": {"sms": False}}).status_code == 200
    assert client.get("/api/marketing/consent").get_json()["channels"]["sms"] is False


def test_privacy_request_works_without_a_session(client, api):
    r = client.post("/api/privacy/requests", json={"request_type": "access", "email": "farmer@example.com"})
    assert r.status_code == 201 and r.get_json()["request_id"] == "req-1"


def test_privacy_request_with_bad_token_is_refused_not_silently_anonymous(client, api, monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: (None, (app_mod.jsonify({"error": "x"}), 401)))
    r = client.post("/api/privacy/requests", headers={"Authorization": "Bearer bad"},
                    json={"request_type": "access", "email": "a@b.co"})
    assert r.status_code == 401


def test_marketing_withdrawal_request_also_turns_sms_off(client, api, monkeypatch, db):
    _as(monkeypatch, "u1")
    api.set_marketing(user_id="u1", channels={"sms": True})
    r = client.post("/api/privacy/requests", json={"request_type": "marketing_withdrawal"},
                    headers={"Authorization": "Bearer t"})
    assert r.status_code == 201
    assert api.get_marketing("u1").data["channels"]["sms"] is False


def test_signed_in_request_ignores_body_user_id(client, api, monkeypatch, db):
    _as(monkeypatch, "real")
    client.post("/api/privacy/requests", headers={"Authorization": "Bearer t"},
                json={"request_type": "access", "user_id": "attacker"})
    assert db.requests[0]["user_id"] == "real"


# ── enforcement gate on protected actions ────────────────────────────────────
class _Orders:
    def create_order(self, **kw):
        from services.orders import OrderResult
        return OrderResult(ok=True, order_ref="R", tracking_code="T", total_price=1, items=[])


def test_gate_is_off_by_default_so_rollout_cannot_lock_everyone_out(client, api, monkeypatch):
    _as(monkeypatch, "u1")
    monkeypatch.setattr(app_mod, "LEGAL_ENFORCED", False)
    monkeypatch.setattr(app_mod, "ORDER_SERVICE", _Orders())
    assert client.post("/api/orders", json={"items": []}).status_code == 201


def test_gate_blocks_protected_actions_until_terms_accepted(client, api, monkeypatch, db):
    _as(monkeypatch, "u1")
    monkeypatch.setattr(app_mod, "LEGAL_ENFORCED", True)
    monkeypatch.setattr(app_mod, "ORDER_SERVICE", _Orders())
    r = client.post("/api/orders", json={"items": []})
    assert r.status_code == 403 and r.get_json()["code"] == "legal_acceptance_required"
    docs = [{"doc_type": d["doc_type"], "version": d["version"]} for d in api.required_documents("farmer")]
    api.accept(user_id="u1", role="farmer", documents=docs, method="login_review")
    assert client.post("/api/orders", json={"items": []}).status_code == 201


def test_gate_cannot_be_bypassed_by_claiming_a_different_role(client, api, monkeypatch):
    _as(monkeypatch, "u1", role="farmer")
    monkeypatch.setattr(app_mod, "LEGAL_ENFORCED", True)
    monkeypatch.setattr(app_mod, "ORDER_SERVICE", _Orders())
    r = client.post("/api/orders", json={"items": [], "role": "admin"})
    assert r.status_code == 403
