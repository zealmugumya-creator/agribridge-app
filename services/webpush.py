# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Web Push (VAPID) for signed-in users — shows in the Android notification bar.

Separate from `services.push` (FCM, admin devices in the native app). This module
serves the website/PWA: a browser subscribes through the service worker, the
subscription is stored in `web_push_subscriptions`, and every in-app notification
for that user is also delivered as a system push.

Configuration comes ONLY from the environment and is never logged or returned:
  VAPID_PUBLIC_KEY   url-safe base64 public key (also served to the browser)
  VAPID_PRIVATE_KEY  url-safe base64 private key (server only)
  VAPID_SUBJECT      contact, e.g. mailto:you@example.com

When keys are absent, sending is a safe no-op and `/api/push/vapid-public-key`
reports "not configured", so nothing breaks before keys are set.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from services.logging import get_logger

log = get_logger(__name__)

_DEAD_STATUSES = {404, 410}   # push service says this subscription is gone forever
_TIMEOUT = 8
_RECORD_SIZE = 4096


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def encrypt_payload(p256dh: str, auth: str, plaintext: bytes) -> bytes:
    """RFC 8291 / RFC 8188 `aes128gcm` encryption of one Web Push message."""
    ua_public_raw = _b64u_decode(p256dh)
    auth_secret = _b64u_decode(auth)
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)

    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public_raw = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_private.exchange(ec.ECDH(), ua_public)

    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public_raw + as_public_raw).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    # 0x02 = padding delimiter marking the final (only) record.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + _RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public_raw)]) + as_public_raw
    return header + ciphertext


class WebPushService:
    def __init__(self, db, *, public_key: str = "", private_key: str = "",
                 subject: str = "", sender=None):
        self._db = db
        self.public_key = (public_key or "").strip()
        self._private_key = (private_key or "").strip()
        self._subject = (subject or "").strip() or "https://agribridge-1-og7a.onrender.com"
        self._sender = sender          # injectable for tests: (sub_info, payload, claims) -> status

    @classmethod
    def from_env(cls, db) -> WebPushService:
        return cls(
            db,
            public_key=os.environ.get("VAPID_PUBLIC_KEY", ""),
            private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            subject=os.environ.get("VAPID_SUBJECT", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self._private_key)

    # ── Subscription registry ────────────────────────────────────────────────
    def register(self, *, user_id: str, subscription: dict, user_agent: str = ""):
        """Upsert one device. Caller MUST have verified `user_id` from the session."""
        endpoint = str(subscription.get("endpoint", "")).strip()
        keys = subscription.get("keys") or {}
        p256dh = str(keys.get("p256dh", "")).strip()
        auth = str(keys.get("auth", "")).strip()
        if not user_id or not endpoint.startswith("https://") or not p256dh or not auth:
            return None
        row = {
            "user_id": user_id, "endpoint": endpoint[:1024],
            "p256dh": p256dh[:256], "auth": auth[:128],
            "user_agent": (user_agent or "")[:300] or None,
            "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return self._db.upsert("web_push_subscriptions", row, on_conflict="endpoint")

    def unregister(self, *, user_id: str, endpoint: str) -> bool:
        if not user_id or not endpoint:
            return False
        return self._db.delete("web_push_subscriptions",
                               {"user_id": f"eq.{user_id}", "endpoint": f"eq.{endpoint}"})

    def _subscriptions_for(self, user_id: str) -> list[dict]:
        rows = self._db.select("web_push_subscriptions",
                               {"select": "endpoint,p256dh,auth", "user_id": f"eq.{user_id}"},
                               limit=20)
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("endpoint")]

    # ── Sending ──────────────────────────────────────────────────────────────
    def _vapid_headers(self, endpoint: str) -> dict[str, str]:
        """RFC 8292 VAPID `Authorization` header (ES256 JWT, 12h expiry)."""
        import jwt  # PyJWT[crypto]

        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}",
                  "exp": int(time.time()) + 12 * 3600, "sub": self._subject}
        key = ec.derive_private_key(int.from_bytes(_b64u_decode(self._private_key), "big"),
                                    ec.SECP256R1())
        token = jwt.encode(claims, key, algorithm="ES256")
        return {"Authorization": f"vapid t={token}, k={self.public_key}"}

    def _send_one(self, sub: dict, payload: str) -> int:
        """Return the HTTP status from the push service (0 on local failure)."""
        info = {"endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        if self._sender:
            return self._sender(info, payload, {"sub": self._subject})
        try:
            body = encrypt_payload(sub["p256dh"], sub["auth"], payload.encode("utf-8"))
            headers = {**self._vapid_headers(sub["endpoint"]),
                       "Content-Encoding": "aes128gcm",
                       "Content-Type": "application/octet-stream",
                       "TTL": "86400", "Urgency": "normal"}
            resp = requests.post(sub["endpoint"], data=body, headers=headers, timeout=_TIMEOUT)
            if resp.status_code >= 300:
                log.error("webpush.send_failed", status=resp.status_code)
            return resp.status_code
        except Exception as exc:      # never let push take down a request
            log.error("webpush.send_error", error=type(exc).__name__)
            return 0

    def send_to_user(self, user_id: str, title: str, body: str, url: str = "/") -> int:
        """Deliver to every device of one user. Returns the number delivered."""
        if not self.enabled or not user_id:
            return 0
        payload = json.dumps({"title": (title or "AgriBridge")[:65],
                              "body": (body or "")[:160], "url": url or "/"})
        delivered = 0
        for sub in self._subscriptions_for(user_id):
            status = self._send_one(sub, payload)
            if 200 <= status < 300:
                delivered += 1
            elif status in _DEAD_STATUSES:
                self._db.delete("web_push_subscriptions", {"endpoint": f"eq.{sub['endpoint']}"})
        return delivered

    def send_to_user_async(self, user_id: str, title: str, body: str, url: str = "/") -> None:
        """Fire-and-forget so an order request never waits on a push service."""
        if not self.enabled or not user_id:
            return
        threading.Thread(target=self._safe_send, args=(user_id, title, body, url),
                         daemon=True).start()

    def _safe_send(self, user_id: str, title: str, body: str, url: str) -> None:
        try:
            self.send_to_user(user_id, title, body, url)
        except Exception as exc:
            log.error("webpush.async_error", error=type(exc).__name__)
