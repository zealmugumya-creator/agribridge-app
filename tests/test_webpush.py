# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Web Push for signed-in users: registry, sending, dead-subscription pruning,
and the in-app → push hook on the dispatcher. No network, no database."""
from __future__ import annotations

import json

from services.notifications import NotificationDispatcher
from services.supabase_client import RpcResult
from services.webpush import WebPushService

SUB = {"endpoint": "https://push.example/abc",
       "keys": {"p256dh": "pk", "auth": "ak"}}


class FakeDb:
    def __init__(self):
        self.rows: list[dict] = []
        self.deleted: list[dict] = []

    def upsert(self, table, data, on_conflict=None):
        self.rows = [r for r in self.rows if r["endpoint"] != data["endpoint"]] + [data]
        return RpcResult(ok=True, data=[data], status=201)

    def select(self, table, filters=None, limit=None):
        uid = (filters or {}).get("user_id", "").removeprefix("eq.")
        return [r for r in self.rows if r["user_id"] == uid]

    def delete(self, table, filters):
        self.deleted.append(filters)
        ep = filters.get("endpoint", "").removeprefix("eq.")
        self.rows = [r for r in self.rows if r["endpoint"] != ep]
        return True

    def rpc(self, fn, args):
        return RpcResult(ok=True, data="notif-id")


def _svc(db, status=201, sent=None):
    def sender(info, payload, claims):
        if sent is not None:
            sent.append((info, json.loads(payload)))
        return status
    return WebPushService(db, public_key="pub", private_key="priv", sender=sender)


def test_disabled_without_keys_is_noop():
    svc = WebPushService(FakeDb())
    assert svc.enabled is False
    assert svc.send_to_user("u1", "t", "b") == 0


def test_register_rejects_bad_subscriptions():
    svc = _svc(FakeDb())
    assert svc.register(user_id="u1", subscription={"endpoint": "http://x", "keys": {}}) is None
    assert svc.register(user_id="", subscription=SUB) is None
    assert svc.register(user_id="u1", subscription={"endpoint": SUB["endpoint"]}) is None


def test_register_upserts_one_row_per_endpoint():
    db = FakeDb()
    svc = _svc(db)
    assert svc.register(user_id="u1", subscription=SUB).ok
    assert svc.register(user_id="u1", subscription=SUB).ok
    assert len(db.rows) == 1


def test_send_delivers_to_each_device_with_payload():
    db, sent = FakeDb(), []
    svc = _svc(db, sent=sent)
    svc.register(user_id="u1", subscription=SUB)
    svc.register(user_id="u1", subscription={**SUB, "endpoint": "https://push.example/def"})
    assert svc.send_to_user("u1", "Order shipped", "Your order is on the way") == 2
    assert sent[0][1]["title"] == "Order shipped"
    assert sent[0][1]["url"] == "/"


def test_dead_subscription_is_pruned():
    db = FakeDb()
    svc = _svc(db, status=410)
    svc.register(user_id="u1", subscription=SUB)
    assert svc.send_to_user("u1", "t", "b") == 0
    assert db.rows == []


def test_transient_failure_keeps_subscription():
    db = FakeDb()
    svc = _svc(db, status=503)
    svc.register(user_id="u1", subscription=SUB)
    assert svc.send_to_user("u1", "t", "b") == 0
    assert len(db.rows) == 1


def test_only_target_users_devices_are_used():
    db, sent = FakeDb(), []
    svc = _svc(db, sent=sent)
    svc.register(user_id="u1", subscription=SUB)
    svc.register(user_id="u2", subscription={**SUB, "endpoint": "https://push.example/zzz"})
    assert svc.send_to_user("u2", "t", "b") == 1
    assert sent[0][0]["endpoint"] == "https://push.example/zzz"


class SpyPush:
    def __init__(self):
        self.calls = []

    def send_to_user_async(self, user_id, title, body, url="/"):
        self.calls.append((user_id, title, body))


def test_in_app_enqueue_triggers_push():
    spy = SpyPush()
    d = NotificationDispatcher(supabase=FakeDb(), web_push=spy)
    d.enqueue(channel="in_app", audience="user", event_type="order", recipient="u1",
              title="Order approved", body="Pickup soon")
    assert spy.calls == [("u1", "Order approved", "Pickup soon")]


def test_non_in_app_enqueue_does_not_push():
    spy = SpyPush()
    d = NotificationDispatcher(supabase=FakeDb(), web_push=spy)
    d.enqueue(channel="discord", audience="ops", event_type="order", title="x")
    d.enqueue(channel="in_app", audience="user", event_type="order", recipient=None, title="x")
    assert spy.calls == []


# ── Real crypto: decrypt exactly the way a browser/push client would (RFC 8291) ──
def _client_decrypt(body: bytes, ua_private, auth_secret: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    salt, idlen = body[:16], body[20]
    as_public_raw = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    ua_public_raw = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_raw))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public_raw + as_public_raw).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_encrypt_payload_roundtrips():
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from services.webpush import _b64u_encode, encrypt_payload

    ua_private = ec.generate_private_key(ec.SECP256R1())
    auth_secret = os.urandom(16)
    p256dh = _b64u_encode(ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    msg = json.dumps({"title": "Order approved", "body": "Pickup in Mukono"}).encode()
    body = encrypt_payload(p256dh, _b64u_encode(auth_secret), msg)
    assert _client_decrypt(body, ua_private, auth_secret) == msg


def test_vapid_header_is_valid_es256_jwt():
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from services.webpush import _b64u_encode

    key = ec.generate_private_key(ec.SECP256R1())
    priv = _b64u_encode(key.private_numbers().private_value.to_bytes(32, "big"))
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    svc = WebPushService(FakeDb(), public_key=_b64u_encode(pub_raw), private_key=priv,
                         subject="mailto:test@example.com")
    header = svc._vapid_headers("https://fcm.googleapis.com/fcm/send/xyz")["Authorization"]
    assert header.startswith("vapid t=") and f"k={_b64u_encode(pub_raw)}" in header
    token = header.split("t=", 1)[1].split(",", 1)[0]
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"],
                        audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:test@example.com"
