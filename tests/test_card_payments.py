# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Direct card charging: opt-in gating, validation, encryption, amount floor,
OTP completion and verification. Flutterwave network calls are faked."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

import app as app_mod
from services.ledger import PaymentPosting

KEY = "0123456789abcdefghijklmn"  # exactly 24 bytes for 3DES


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
    monkeypatch.setattr(app_mod, "FLW_PUBLIC_KEY", "FLWPUBK_TEST-x")
    monkeypatch.setattr(app_mod, "FLW_ENCRYPTION_KEY", KEY)
    monkeypatch.setattr(app_mod, "FLW_DIRECT_CARD", True)
    monkeypatch.setattr(app_mod, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_send_payment_receipt", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.NOTIFY, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(app_mod.ledger, "post_verified_payment", lambda *a, **k: PaymentPosting(
        ok=True, reference="payment:test", journal_id="journal-test"))


def _orders(monkeypatch, rows):
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: rows)


def _decrypt(client_b64):
    decryptor = Cipher(TripleDES(KEY.encode()), modes.ECB()).decryptor()
    raw = decryptor.update(base64.b64decode(client_b64)) + decryptor.finalize()
    unpadder = padding.PKCS7(64).unpadder()
    plain = unpadder.update(raw) + unpadder.finalize()
    return json.loads(plain.decode())


CARD = {
    "card_number": "4242 4242 4242 4242",
    "expiry": "12/34",
    "cvv": "123",
    "cardholder": "Test Buyer",
}


def test_card_endpoint_requires_login(client):
    assert client.post("/api/pay/card", json={}).status_code == 401


def test_card_disabled_unless_explicitly_enabled(client, as_user, monkeypatch):
    monkeypatch.setattr(app_mod, "FLW_DIRECT_CARD", False)
    assert app_mod._enabled_providers()["card"] is False
    r = client.post("/api/pay/card", json={"order_ref": "AB-1", "amount": 5000, **CARD})
    assert r.status_code == 200 and r.get_json()["enabled"] is False


def test_card_flag_requires_a_24_byte_key_and_public_key(client, as_user, monkeypatch):
    assert app_mod._direct_card_ready() is True
    monkeypatch.setattr(app_mod, "FLW_ENCRYPTION_KEY", "too-short")
    assert app_mod._direct_card_ready() is False
    monkeypatch.setattr(app_mod, "FLW_ENCRYPTION_KEY", KEY)
    monkeypatch.setattr(app_mod, "FLW_PUBLIC_KEY", "")
    assert app_mod._direct_card_ready() is False


def test_card_cannot_pay_someone_elses_order(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "other", "total_price": 5000, "payment_status": "unpaid"}])
    r = client.post("/api/pay/card", json={"order_ref": "AB-1", "amount": 5000, **CARD})
    assert r.status_code == 403


@pytest.mark.parametrize("field,value", [
    ("card_number", "4242 4242 4242 4243"),
    ("expiry", "12/20"),
    ("expiry", "13/34"),
    ("cvv", "12"),
    ("cardholder", " "),
])
def test_invalid_card_details_never_reach_flutterwave(client, as_user, monkeypatch, field, value):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 5000, "payment_status": "unpaid"}])
    calls = []
    monkeypatch.setattr(app_mod.requests, "post", lambda *a, **k: calls.append(a) or Resp({}))
    body = {"order_ref": "AB-1", "amount": 5000, **CARD, field: value}
    r = client.post("/api/pay/card", json=body)
    assert r.status_code == 400
    assert calls == []
    assert "4242" not in r.get_data(as_text=True)


def test_card_payload_is_encrypted_and_amount_uses_order_floor(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 40000, "payment_status": "unpaid"},
                          {"buyer_id": "user-1", "total_price": 10000, "payment_status": "unpaid"}])
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(url=url, **kwargs)
        inner = _decrypt(kwargs["json"]["client"])
        return Resp({"status": "success", "data": {
            "status": "pending", "flw_ref": "FLW-1", "tx_ref": inner["tx_ref"], "id": 1}})

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    r = client.post("/api/pay/card", json={"order_ref": "AB-1", "amount": 1, **CARD,
                                           "email": "buyer@example.com"})
    assert r.status_code == 200
    assert sent["url"].endswith("/v3/charges?type=card")
    assert sent["json"]["public_key"] == "FLWPUBK_TEST-x"
    assert "4242" not in json.dumps(sent["json"])          # never plaintext on the wire to FLW
    inner = _decrypt(sent["json"]["client"])
    assert inner["amount"] == 50000
    assert inner["card_number"] == "4242424242424242" and inner["cvv"] == "123"
    assert inner["currency"] == "UGX" and inner["enckey"] == KEY
    assert inner["tx_ref"].startswith("AB-1~")
    assert inner["meta"] == {"order_ref": "AB-1"}


def test_successful_card_charge_is_verified_and_marks_paid(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 50000, "payment_status": "unpaid"}])
    updates, tx_ref = [], {}

    def fake_post(url, **kwargs):
        inner = _decrypt(kwargs["json"]["client"])
        tx_ref["v"] = inner["tx_ref"]
        return Resp({"status": "success", "data": {
            "status": "successful", "id": 55, "tx_ref": inner["tx_ref"], "flw_ref": "FLW-55"}})

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    monkeypatch.setattr(app_mod.requests, "get", lambda *a, **k: Resp({"data": {
        "status": "successful", "tx_ref": tx_ref["v"], "amount": 50000, "currency": "UGX"}}))
    monkeypatch.setattr(app_mod, "supa_update", lambda *a, **k: updates.append(a) or True)
    r = client.post("/api/pay/card", json={"order_ref": "AB-1", "amount": 50000, **CARD})
    j = r.get_json()
    assert j["ok"] is True and j["mode"] == "paid"
    assert updates and updates[0][2:] == ("payment_ref", "AB-1")


def test_otp_charge_returns_flw_ref_then_validate_marks_paid(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "user-1", "total_price": 50000, "payment_status": "unpaid"}])
    posts, state = [], {}

    def fake_post(url, **kwargs):
        posts.append(url)
        if url.endswith("/v3/charges?type=card"):
            inner = _decrypt(kwargs["json"]["client"])
            state["tx_ref"] = inner["tx_ref"]
            return Resp({"status": "success", "data": {
                "status": "pending", "auth_mode": "otp", "flw_ref": "FLW-OTP-1",
                "tx_ref": inner["tx_ref"], "id": 56}})
        assert url.endswith("/v3/validate-charge")
        assert kwargs["json"] == {"flw_ref": "FLW-OTP-1", "otp": "123456"}
        return Resp({"status": "success", "data": {
            "status": "successful", "id": 56, "tx_ref": state["tx_ref"]}})

    monkeypatch.setattr(app_mod.requests, "post", fake_post)
    monkeypatch.setattr(app_mod.requests, "get", lambda *a, **k: Resp({"data": {
        "status": "successful", "tx_ref": state["tx_ref"], "amount": 50000, "currency": "UGX"}}))
    updates = []
    monkeypatch.setattr(app_mod, "supa_update", lambda *a, **k: updates.append(a) or True)

    first = client.post("/api/pay/card", json={"order_ref": "AB-1", "amount": 50000, **CARD})
    assert first.get_json()["mode"] == "otp"
    assert first.get_json()["flw_ref"] == "FLW-OTP-1"

    second = client.post("/api/pay/card/validate",
                         json={"order_ref": "AB-1", "flw_ref": "FLW-OTP-1", "otp": "123456"})
    assert second.get_json()["mode"] == "paid"
    assert updates and updates[0][2:] == ("payment_ref", "AB-1")


def test_validate_rejects_bad_otp_and_foreign_order(client, as_user, monkeypatch):
    _orders(monkeypatch, [{"buyer_id": "other", "total_price": 5000, "payment_status": "unpaid"}])
    r = client.post("/api/pay/card/validate",
                    json={"order_ref": "AB-1", "flw_ref": "FLW-OTP-1", "otp": "abc"})
    assert r.status_code == 400
    r = client.post("/api/pay/card/validate",
                    json={"order_ref": "AB-1", "flw_ref": "FLW-OTP-1", "otp": "123456"})
    assert r.status_code == 403
