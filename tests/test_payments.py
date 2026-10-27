# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Online payment hardening: order ownership, amount floor, per-attempt tx_ref,
Flutterwave MoMo, status polling and webhook amount check. Network is faked."""
from __future__ import annotations

from decimal import Decimal

import pytest

import app as app_mod
from services.ledger import PaymentPosting


class Resp:
    def __init__(self, j, ok=True):
        self._j, self.ok, self.content = j, ok, b"x"

    def json(self):
        return self._j


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(app_mod, "verify_supabase_user", lambda: ("user-1", None))
    monkeypatch.setattr(app_mod, "FLW_SECRET_KEY", "FLWSECK_TEST-x")


def _orders(monkeypatch, rows):
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: rows)


def test_initiate_requires_login(client):
    assert client.post("/api/pay/initiate", json={"provider": "flutterwave"}).status_code == 401


def test_cannot_pay_someone_elses_order(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "other", "total_price": 5000, "payment_status": "unpaid"}])
    r = client.post("/api/pay/initiate", json={"provider": "flutterwave", "order_ref": "AB-1", "amount": 5000})
    assert r.status_code == 403


def test_unknown_order_and_already_paid(client, as_user, monkeypatch):
    _orders(monkeypatch, [])
    body = {"provider": "flutterwave", "order_ref": "AB-1", "amount": 5000}
    assert client.post("/api/pay/initiate", json=body).status_code == 404
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 5000, "payment_status": "paid"}])
    assert client.post("/api/pay/initiate", json=body).status_code == 409


def test_amount_is_never_below_saved_order_total(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 40000, "payment_status": "unpaid"},
                          {"buyer_id": "user-1", "total_price": 10000, "payment_status": "unpaid"}])
    sent = {}
    monkeypatch.setattr(app_mod.requests, "post",
                        lambda url, **k: sent.update(url=url, json=k["json"]) or Resp({"data": {"link": "https://pay/x"}}))
    r = client.post("/api/pay/initiate", json={"provider": "flutterwave", "order_ref": "AB-1", "amount": 1})
    assert r.get_json()["link"] == "https://pay/x"
    assert sent["json"]["amount"] == "50000"                      # floor from DB, not the client's 1
    assert sent["json"]["tx_ref"].startswith("AB-1~") and sent["json"]["tx_ref"] != "AB-1"
    assert sent["json"]["payment_options"].startswith("card")


def test_each_attempt_gets_a_fresh_tx_ref():
    assert app_mod._pay_attempt_ref("AB-1") != app_mod._pay_attempt_ref("AB-1")
    assert app_mod._base_ref("AB-1~abc123") == "AB-1"


def test_flw_momo_normalises_phone_and_network(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 20000, "payment_status": "unpaid"}])
    sent = {}
    monkeypatch.setattr(app_mod.requests, "post",
                        lambda url, **k: sent.update(url=url, json=k["json"]) or Resp(
                            {"status": "success", "meta": {"authorization": {"mode": "redirect", "redirect": "https://auth"}}}))
    r = client.post("/api/pay/initiate", json={"provider": "flw_momo_airtel", "order_ref": "AB-1",
                                               "amount": 20000, "phone": "0701 234 567"})
    j = r.get_json()
    assert j["ok"] and j["link"] == "https://auth"
    assert "mobile_money_uganda" in sent["url"]
    assert sent["json"]["phone_number"] == "256701234567" and sent["json"]["network"] == "AIRTEL"


def test_flw_momo_rejects_bad_phone(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 20000, "payment_status": "unpaid"}])
    r = client.post("/api/pay/initiate", json={"provider": "flw_momo_mtn", "order_ref": "AB-1",
                                               "amount": 20000, "phone": "12"})
    assert r.status_code == 400


def test_status_only_for_own_orders(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "other", "payment_status": "paid"}])
    assert client.get("/api/pay/status?order_ref=AB-1").status_code == 404
    _orders(monkeypatch, [{"buyer_id": "user-1", "payment_status": "paid"}])
    assert client.get("/api/pay/status?order_ref=AB-1~zz").get_json()["paid"] is True
    _orders(monkeypatch, [{"buyer_id": "user-1", "payment_status": "unpaid"}])
    assert client.get("/api/pay/status?order_ref=AB-1").get_json()["paid"] is False


def _webhook(client, monkeypatch, verified, owed=50000):
    monkeypatch.setattr(app_mod, "FLW_WEBHOOK_HASH", "h")
    monkeypatch.setattr(app_mod, "FLW_SECRET_KEY", "k")
    updates, postings = [], []
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [
        {"id": "o1", "total_price": owed, "buyer_id": None, "status": "pending"}])
    monkeypatch.setattr(app_mod, "supa_update", lambda *a, **k: updates.append(a) or True)
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_send_payment_receipt", lambda *a, **k: None)

    def fake_post(db, **kwargs):
        postings.append(kwargs)
        return PaymentPosting(ok=True, reference=f"payment:{kwargs['tx_ref']}",
                              journal_id="journal-test")

    monkeypatch.setattr(app_mod.ledger, "post_verified_payment", fake_post)
    monkeypatch.setattr(app_mod.requests, "get", lambda *a, **k: Resp({"data": verified}))
    r = client.post("/api/pay/webhook/flutterwave", headers={"verif-hash": "h"},
                    json={"data": {"status": "successful", "tx_ref": "AB-1~abc", "id": 7}})
    assert r.status_code == 200
    return updates, postings


def test_webhook_marks_paid_on_full_amount_and_posts_the_ledger(client, monkeypatch):
    ups, postings = _webhook(
        client, monkeypatch,
        {"status": "successful", "tx_ref": "AB-1~abc", "amount": 50000, "currency": "UGX"})
    assert ups and ups[0][2:] == ("payment_ref", "AB-1")
    assert len(postings) == 1
    assert postings[0]["tx_ref"] == "AB-1"
    assert postings[0]["received"] == Decimal("50000.00")
    assert postings[0]["provider"] == "Flutterwave"
    assert postings[0]["flw_id"] == 7
    assert postings[0]["currency"] == "UGX"


def test_webhook_ignores_underpayment_and_wrong_ref(client, monkeypatch):
    cases = [
        {"status": "successful", "tx_ref": "AB-1~abc", "amount": 100, "currency": "UGX"},
        {"status": "successful", "tx_ref": "AB-9~abc", "amount": 50000, "currency": "UGX"},
        {"status": "successful", "tx_ref": "AB-1~abc", "amount": 50000, "currency": "USD"},
        {"status": "successful", "tx_ref": "AB-1~abc", "amount": 50000},
        {"status": "failed", "tx_ref": "AB-1~abc", "amount": 50000, "currency": "UGX"},
    ]
    for verified in cases:
        ups, postings = _webhook(client, monkeypatch, verified)
        assert not ups
        assert not postings


def _run_verify(monkeypatch, *, update=True, posting=None, rows=None):
    monkeypatch.setattr(app_mod, "FLW_SECRET_KEY", "k")
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: rows if rows is not None else [
        {"id": "o1", "total_price": 50000, "buyer_id": None, "status": "pending"}])
    monkeypatch.setattr(app_mod, "supa_update", lambda *a, **k: update)
    monkeypatch.setattr(app_mod.requests, "get", lambda *a, **k: Resp({"data": {
        "status": "successful", "tx_ref": "AB-1~abc", "amount": 50000, "currency": "UGX"}}))
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_send_payment_receipt", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.NOTIFY, "enqueue", lambda *a, **k: None)
    if posting is not None:
        monkeypatch.setattr(app_mod.ledger, "post_verified_payment",
                            lambda *a, **kw: posting)
    events = []
    monkeypatch.setattr(app_mod, "_log_system_event",
                        lambda et, d, **k: events.append((et, d)))
    return events


def test_payment_mark_failure_is_not_reported_as_success(monkeypatch):
    events = _run_verify(monkeypatch, update=False)
    assert app_mod._flw_verify_and_mark("AB-1~abc", 7) is False
    assert events and events[0][0] == "payment_mark_paid_failed"


def test_ledger_failure_alerts_but_does_not_turn_a_paid_order_into_a_failed_payment(monkeypatch):
    events = _run_verify(monkeypatch, posting=PaymentPosting(
        ok=False, reference="payment:AB-1", error="unknown account"))
    assert app_mod._flw_verify_and_mark("AB-1~abc", 7) is True
    assert events and events[0][0] == "payment_ledger_post_failed"
    assert "unknown account" in events[0][1]["error"]


def test_notification_failure_does_not_report_the_payment_as_failed(monkeypatch):
    _run_verify(monkeypatch, posting=PaymentPosting(
        ok=True, reference="payment:AB-1", journal_id="journal-test"))

    def boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(app_mod, "_discord_notify", boom)
    assert app_mod._flw_verify_and_mark("AB-1~abc", 7) is True


def test_no_fake_bank_account_left():
    from pathlib import Path
    repo = Path(app_mod.__file__).resolve().parent
    for f in ("app.py", "static/index.html"):
        assert "9030" not in (repo / f).read_text(encoding="utf-8")
