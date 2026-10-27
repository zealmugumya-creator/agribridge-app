# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Pesapal API 3.0: client hardening, verified callbacks, recurring suspense.

Network and PostgREST are faked. These tests prove:
  * the token is cached and never exposed;
  * only Pesapal HTTPS redirects are accepted;
  * a callback is never trusted without GetTransactionStatus;
  * an underpayment or wrong reference cannot close an order;
  * a recurring IPN posts to SUSPENSE_OVERPAYMENT, never to a farmer and never
    re-marks the original checkout paid.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import app as app_mod
from services import ledger
from services.pesapal import PesapalClient, PesapalError, base_url_for


class Resp:
    def __init__(self, j, ok=True, status=200, content=b"x"):
        self._j, self.ok, self.status_code, self.content = j, ok, status, content

    def json(self):
        return self._j


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakePesapal:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error
        self.status_calls = []
        self.submitted = []

    def get_transaction_status(self, tracking_id):
        self.status_calls.append(tracking_id)
        if self.error:
            raise self.error
        return self.status

    def submit_order(self, payload):
        self.submitted.append(payload)
        return {"redirect_url": "https://cybqa.pesapal.com/pesapalv3/pay/abc",
                "order_tracking_id": "trk-1"}


class FakeDB:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def rpc(self, fn, args):
        self.calls.append((fn, args))
        return self.result


class Rpc:
    def __init__(self, ok=True, data=None, status=200, error=""):
        self.ok, self.data, self.status, self.error = ok, data, status, error


class FakeSupa:
    def __init__(self, upsert_data=None):
        self.upsert_calls = []
        self.insert_calls = []
        self.upsert_result = Rpc(ok=True, data=upsert_data or [{"id": "sub-1"}])
        self.insert_result = Rpc(ok=True, data=[{"id": "charge-1"}])

    def upsert(self, table, data, on_conflict=None):
        self.upsert_calls.append((table, data, on_conflict))
        return self.upsert_result

    def insert(self, table, rows):
        self.insert_calls.append((table, rows))
        return self.insert_result


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("user-1", None))
    monkeypatch.setattr(app_mod, "PESAPAL_KEY", "pk")
    monkeypatch.setattr(app_mod, "PESAPAL_SECRET", "sk")


def _status(**over):
    data = {
        "status_code": 1,
        "payment_status_description": "Completed",
        "amount": 50000,
        "currency": "UGX",
        "merchant_reference": "AB-1",
        "confirmation_code": "CONF-1",
    }
    data.update(over)
    return data


def _wire_mark(monkeypatch, rows=None, posting_ok=True):
    posts, updates = [], []
    rows = rows if rows is not None else [
        {"id": "o1", "total_price": 50000, "buyer_id": None,
         "status": "pending", "payment_status": "unpaid"}]
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: rows)
    monkeypatch.setattr(app_mod, "supa_update",
                        lambda *a, **k: updates.append(a) or True)
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_send_payment_receipt", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.NOTIFY, "enqueue", lambda *a, **k: None)

    def fake_post(db, **kwargs):
        posts.append(kwargs)
        return ledger.PaymentPosting(
            ok=posting_ok, reference=f"payment:{kwargs['tx_ref']}",
            journal_id="journal-1" if posting_ok else None,
            error="" if posting_ok else "ledger down")

    monkeypatch.setattr(app_mod.ledger, "post_verified_payment", fake_post)
    return posts, updates


# ── Client ──────────────────────────────────────────────────────────────────
def test_environment_defaults_to_sandbox():
    assert base_url_for(None) == "https://cybqa.pesapal.com/pesapalv3"
    assert base_url_for("sandbox") == "https://cybqa.pesapal.com/pesapalv3"
    assert base_url_for("live") == "https://pay.pesapal.com/v3"
    assert base_url_for("PRODUCTION") == "https://pay.pesapal.com/v3"


def test_token_is_cached_and_used_as_bearer():
    transport = FakeTransport([
        Resp({"token": "tok-1", "expiryDate": "ignored"}),
        Resp({"redirect_url": "https://cybqa.pesapal.com/pay/1",
              "order_tracking_id": "trk"}),
        Resp({"status_code": 1}),
    ])
    pesapal = PesapalClient("pk", "sk", env="sandbox", transport=transport,
                            clock=lambda: 1000.0)
    pesapal.submit_order({"id": "AB-1~abc"})
    pesapal.get_transaction_status("trk")
    token_calls = [c for c in transport.calls if c[1].endswith("/Auth/RequestToken")]
    assert len(token_calls) == 1
    for _method, _url, kwargs in transport.calls[1:]:
        assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
    assert "sk" not in str(transport.calls[1][2]["headers"])


def test_submit_rejects_a_redirect_outside_pesapal():
    transport = FakeTransport([
        Resp({"token": "tok"}),
        Resp({"redirect_url": "https://evil.example/pay", "order_tracking_id": "t"}),
    ])
    pesapal = PesapalClient("pk", "sk", transport=transport)
    with pytest.raises(PesapalError):
        pesapal.submit_order({"id": "AB-1~x"})


def test_status_rejects_an_unsafe_tracking_id():
    pesapal = PesapalClient("pk", "sk", transport=FakeTransport([]))
    with pytest.raises(PesapalError):
        pesapal.get_transaction_status("../../etc/passwd")
    with pytest.raises(PesapalError):
        pesapal.get_transaction_status("x" * 81)


def test_register_ipn_rejects_plain_http_remote_url():
    pesapal = PesapalClient("pk", "sk", transport=FakeTransport([]))
    with pytest.raises(PesapalError):
        pesapal.register_ipn("http://agribridge.example/ipn")


# ── Callback base URL ───────────────────────────────────────────────────────
def test_a_truncated_base_url_cannot_register_a_dead_ipn(monkeypatch):
    """Pesapal accepts any https URL, so a host-less value would silently route
    every payment notification to a domain that does not exist."""
    monkeypatch.setenv("PESAPAL_CALLBACK_BASE_URL", "https://agribridge-1-")
    assert app_mod._resolve_pesapal_base_url() == app_mod.PESAPAL_BASE_URL_DEFAULT


def test_blank_base_url_falls_back_to_the_render_origin(monkeypatch):
    monkeypatch.setenv("PESAPAL_CALLBACK_BASE_URL", "")
    assert app_mod._resolve_pesapal_base_url() == app_mod.PESAPAL_BASE_URL_DEFAULT


def test_a_complete_base_url_is_kept_and_trailing_slash_removed(monkeypatch):
    monkeypatch.setenv("PESAPAL_CALLBACK_BASE_URL", "https://api.example.com/")
    assert app_mod._resolve_pesapal_base_url() == "https://api.example.com"


def test_localhost_still_works_for_local_pesapal_testing(monkeypatch):
    monkeypatch.setenv("PESAPAL_CALLBACK_BASE_URL", "http://localhost:5000")
    assert app_mod._resolve_pesapal_base_url() == "http://localhost:5000"


def test_ipn_url_built_from_the_default_is_the_live_webhook_route(monkeypatch):
    monkeypatch.setenv("PESAPAL_CALLBACK_BASE_URL", "https://agribridge-1-")
    base = app_mod._resolve_pesapal_base_url()
    assert base + "/api/pay/webhook/pesapal" == (
        "https://agribridge-1-og7a.onrender.com/api/pay/webhook/pesapal")


# ── Providers / initiate ────────────────────────────────────────────────────
def test_provider_flag_requires_both_keys(client, monkeypatch):
    monkeypatch.setattr(app_mod, "PESAPAL_KEY", "")
    monkeypatch.setattr(app_mod, "PESAPAL_SECRET", "")
    assert client.get("/api/pay/providers").get_json()["providers"]["pesapal"] is False
    monkeypatch.setattr(app_mod, "PESAPAL_KEY", "pk")
    monkeypatch.setattr(app_mod, "PESAPAL_SECRET", "sk")
    assert client.get("/api/pay/providers").get_json()["providers"]["pesapal"] is True


def test_initiate_uses_server_total_and_stable_account_number(client, as_user, monkeypatch):
    orders = [{"buyer_id": "user-1", "total_price": 40000, "payment_status": "unpaid"},
              {"buyer_id": "user-1", "total_price": 10000, "payment_status": "unpaid"}]
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: orders)
    fake = FakePesapal()
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    monkeypatch.setattr(app_mod, "_ensure_pesapal_ipn", lambda c: "ipn-1")
    monkeypatch.setattr(app_mod, "PESAPAL_CALLBACK_BASE_URL", "https://api.example")
    r = client.post("/api/pay/initiate", json={
        "provider": "pesapal", "order_ref": "AB-1", "amount": 1, "email": "b@example.com"})
    body = r.get_json()
    assert body["ok"] is True and body["link"].startswith("https://")
    payload = fake.submitted[0]
    assert payload["amount"] == 50000
    assert payload["currency"] == "UGX"
    assert payload["account_number"] == "AB-1"
    assert payload["id"].startswith("AB-1~")
    assert payload["notification_id"] == "ipn-1"
    assert payload["callback_url"] == "https://api.example/api/pay/pesapal/callback"


def test_initiate_refuses_someone_elses_order(client, as_user, monkeypatch):
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [
        {"buyer_id": "other", "total_price": 5000, "payment_status": "unpaid"}])
    r = client.post("/api/pay/initiate", json={
        "provider": "pesapal", "order_ref": "AB-1", "amount": 5000})
    assert r.status_code == 403


# ── Callback ────────────────────────────────────────────────────────────────
def test_callback_marks_paid_only_from_a_verified_completed_status(client, monkeypatch):
    fake = FakePesapal(status=_status())
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    posts, updates = _wire_mark(monkeypatch)
    r = client.get("/api/pay/pesapal/callback?OrderTrackingId=trk-1"
                   "&OrderMerchantReference=AB-1&OrderNotificationType=IPNCHANGE")
    assert r.status_code == 302
    assert "status=paid" in r.headers["Location"]
    assert "tx_ref=AB-1" in r.headers["Location"]
    assert updates and updates[0][2:] == ("payment_ref", "AB-1")
    assert posts and posts[0]["tx_ref"] == "AB-1"
    assert posts[0]["received"] == Decimal("50000.00")
    assert posts[0]["provider"] == "Pesapal"
    assert posts[0]["flw_id"] == "trk-1"


def test_callback_ignores_an_underpayment(client, monkeypatch):
    fake = FakePesapal(status=_status(amount=100))
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    posts, updates = _wire_mark(monkeypatch)
    r = client.get("/api/pay/pesapal/callback?OrderTrackingId=trk-1"
                   "&OrderMerchantReference=AB-1")
    assert r.status_code == 302 and "status=pending" in r.headers["Location"]
    assert not posts and not updates


def test_callback_ignores_a_failed_status(client, monkeypatch):
    fake = FakePesapal(status=_status(status_code=2, payment_status_description="Failed"))
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    posts, updates = _wire_mark(monkeypatch)
    r = client.get("/api/pay/pesapal/callback?OrderTrackingId=trk-1"
                   "&OrderMerchantReference=AB-1")
    assert "status=failed" in r.headers["Location"]
    assert not posts and not updates


def test_callback_uses_the_status_reference_not_the_query_string(client, monkeypatch):
    fake = FakePesapal(status=_status(merchant_reference="AB-REAL"))
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    posts, updates = _wire_mark(monkeypatch, rows=[
        {"id": "o1", "total_price": 50000, "buyer_id": None,
         "status": "pending", "payment_status": "unpaid"}])
    client.get("/api/pay/pesapal/callback?OrderTrackingId=trk-1"
               "&OrderMerchantReference=AB-FORGED")
    assert posts[0]["tx_ref"] == "AB-REAL"
    assert updates[0][2:] == ("payment_ref", "AB-REAL")


def test_callback_without_a_tracking_id_does_nothing(client, monkeypatch):
    fake = FakePesapal(status=_status())
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    posts, updates = _wire_mark(monkeypatch)
    r = client.get("/api/pay/pesapal/callback?OrderMerchantReference=AB-1")
    assert r.status_code == 302
    assert not fake.status_calls and not posts and not updates


def test_verified_payment_is_idempotent_on_a_replayed_callback(client, monkeypatch):
    fake = FakePesapal(status=_status())
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    receipts = []
    posts, _ = _wire_mark(monkeypatch)
    monkeypatch.setattr(app_mod, "_send_payment_receipt", lambda ref: receipts.append(ref))
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [
        {"id": "o1", "total_price": 50000, "buyer_id": None,
         "status": "pending", "payment_status": "paid"}])
    client.get("/api/pay/pesapal/callback?OrderTrackingId=trk-1&OrderMerchantReference=AB-1")
    # The ledger is retried idempotently, but the buyer is not told twice.
    assert len(posts) == 1
    assert receipts == []


# ── IPN / recurring ─────────────────────────────────────────────────────────
def test_webhook_rejects_an_unverifiable_notification(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_pesapal_client",
                        lambda: FakePesapal(error=PesapalError("status unavailable")))
    r = client.post("/api/pay/webhook/pesapal", json={
        "OrderTrackingId": "trk-1", "OrderMerchantReference": "AB-1",
        "OrderNotificationType": "IPNCHANGE"})
    assert r.get_json()["status"] == 500


def _recurring_status(**over):
    data = _status(amount=25000)
    data["subscription_transaction_info"] = {
        "account_reference": "AB-1", "amount": 25000,
        "first_name": "A", "last_name": "B", "correlation_id": "corr-1",
    }
    data.update(over)
    return data


def test_recurring_ipn_posts_to_suspense_and_never_reopens_the_order(client, monkeypatch):
    fake = FakePesapal(status=_recurring_status())
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    fake_supa = FakeSupa()
    monkeypatch.setattr(app_mod, "SUPA", fake_supa)
    updates, recurring_posts = [], []
    monkeypatch.setattr(app_mod, "supa_update",
                        lambda *a, **k: updates.append(a) or True)
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.NOTIFY, "enqueue", lambda *a, **k: None)

    def fake_get(table, *a, **k):
        if table == "subscription_charges":
            return []
        if table == "orders":
            return [{"buyer_id": "buyer-1", "payment_ref": "AB-1",
                     "tracking_code": "TRK-1"}]
        return []

    monkeypatch.setattr(app_mod, "supa_get", fake_get)
    monkeypatch.setattr(app_mod.ledger, "post_verified_payment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-mark")))

    def fake_recurring(db, **kwargs):
        recurring_posts.append(kwargs)
        return ledger.PaymentPosting(ok=True, reference="recurring:trk-1",
                                     journal_id="journal-r1")

    monkeypatch.setattr(app_mod.ledger, "post_recurring_charge", fake_recurring)

    r = client.post("/api/pay/webhook/pesapal", json={
        "OrderTrackingId": "trk-1", "OrderMerchantReference": "AB-1",
        "OrderNotificationType": "RECURRING"})
    body = r.get_json()
    assert body == {"orderNotificationType": "RECURRING",
                    "orderTrackingId": "trk-1",
                    "orderMerchantReference": "AB-1", "status": 200}
    assert fake_supa.upsert_calls[0][0] == "payment_subscriptions"
    assert fake_supa.upsert_calls[0][1]["provider_correlation_id"] == "corr-1"
    assert fake_supa.insert_calls[0][0] == "subscription_charges"
    assert fake_supa.insert_calls[0][1]["amount"] == 25000.0
    assert recurring_posts[0]["tracking_id"] == "trk-1"
    assert recurring_posts[0]["amount"] == Decimal("25000.00")
    assert recurring_posts[0]["account_ref"] == "AB-1"
    # No order row is touched by a renewal.
    assert not any(u[0] == "orders" for u in updates)


def test_recurring_ipn_is_idempotent_when_already_posted(client, monkeypatch):
    fake = FakePesapal(status=_recurring_status())
    monkeypatch.setattr(app_mod, "_pesapal_client", lambda: fake)
    fake_supa = FakeSupa()
    monkeypatch.setattr(app_mod, "SUPA", fake_supa)
    monkeypatch.setattr(app_mod, "supa_get", lambda table, *a, **k: (
        [{"id": "charge-1", "ledger_reference": "recurring:trk-1"}]
        if table == "subscription_charges" else []))
    monkeypatch.setattr(app_mod.ledger, "post_recurring_charge",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not double-post")))
    r = client.post("/api/pay/webhook/pesapal", json={
        "OrderTrackingId": "trk-1", "OrderNotificationType": "RECURRING"})
    assert r.get_json()["status"] == 200
    assert not fake_supa.insert_calls


# ── Ledger ──────────────────────────────────────────────────────────────────
def test_recurring_ledger_lines_hold_money_in_suspense():
    db = FakeDB(Rpc(ok=True, data="journal-1"))
    posting = ledger.post_recurring_charge(
        db, tracking_id="trk-1", amount=25000, currency="UGX",
        provider="Pesapal", account_ref="AB-1", correlation_id="corr-1")
    assert posting.ok and posting.reference == "recurring:trk-1"
    fn, args = db.calls[0]
    assert fn == "post_journal"
    assert args["p_reference"] == "recurring:trk-1"
    assert args["p_kind"] == "recurring_payment"
    assert args["p_payment_ref"] is None
    lines = {ln["account"]: (ln["side"], ln["amount"]) for ln in args["p_lines"]}
    assert lines["PROVIDER_CLEARING"] == ("debit", "25000.00")
    assert lines["SUSPENSE_OVERPAYMENT"] == ("credit", "25000.00")
    assert "FARMER_PAYABLE" not in lines
    assert args["p_metadata"]["allocation_policy"] == "suspense_until_order_allocated"


def test_recurring_ledger_rejects_bad_money():
    db = FakeDB(Rpc(ok=True, data="journal-1"))
    assert not ledger.post_recurring_charge(
        db, tracking_id="trk-1", amount=0).ok
    assert not ledger.post_recurring_charge(
        db, tracking_id="trk-1", amount=100, currency="USD").ok
    assert not ledger.post_recurring_charge(
        db, tracking_id="", amount=100).ok
    assert not db.calls
