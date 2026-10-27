# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Firebase Cloud Messaging admin push (mandate Phase 6; audit finding H4).

Two pieces:

  * `AdminDeviceRegistry` — server-side CRUD over the `admin_devices` token table.
    A device registers its FCM token *after* the API has already authorized the
    caller as an admin (identity from the verified session, never from the
    request body — Rule 9). Tokens are upserted on their natural key so a token
    refresh does not create duplicates, and permanently-dead tokens are pruned.

  * `FcmPushSender` — transport-only sender using the FCM HTTP **v1** API with a
    Google service account. Credentials come ONLY from the environment
    (`FCM_CREDENTIALS_JSON` / `FCM_SERVICE_ACCOUNT_PATH`) and are never logged,
    never returned to a client, never committed (Rule 4). When credentials are
    absent the sender is a safe no-op so nothing breaks before Firebase is set up
    (Rule 11 — live verification pending).

Lock-screen safety: the visible `notification` carries only a short, generic
summary; anything sensitive rides in the `data` payload and is rendered inside
the app after the admin is authenticated.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from services.logging import get_logger
from services.supabase_client import RpcResult

log = get_logger(__name__)

_TIMEOUT = 8
_OAUTH_URL = "https://oauth2.googleapis.com/token"
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
# FCM error codes that mean "this token will never work again" → prune, don't retry.
_DEAD_TOKEN_CODES = {"UNREGISTERED", "INVALID_ARGUMENT", "THIRD_PARTY_AUTH_ERROR"}


@dataclass
class PushResult:
    ok: bool
    error: str = ""


class AdminDeviceRegistry:
    """CRUD over `admin_devices`. Callers MUST authorize the admin first."""

    def __init__(self, supabase):
        self._db = supabase

    def register(self, *, user_id: str, token: str, provider: str = "fcm",
                 platform: str | None = None) -> RpcResult:
        if not user_id or not token:
            return RpcResult(ok=False, error="user_id and token are required")
        if provider not in ("fcm", "apns"):
            return RpcResult(ok=False, error=f"unsupported provider {provider}")
        row = {
            "user_id": user_id, "provider": provider, "token": token,
            "platform": platform,
            "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        res = self._db.upsert("admin_devices", row, on_conflict="provider,token")
        if not res.ok:
            log.error("push.register_failed", error=res.error)
        return res

    def tokens_for(self, user_id: str | None = None) -> list[str]:
        """FCM tokens for one admin (user_id) or all admins (None)."""
        filters: dict[str, str] = {"select": "token", "provider": "eq.fcm"}
        if user_id:
            filters["user_id"] = f"eq.{user_id}"
        rows = self._db.select("admin_devices", filters, limit=200)
        return [r["token"] for r in rows if isinstance(r, dict) and r.get("token")]

    def unregister(self, *, user_id: str, token: str) -> bool:
        if not user_id or not token:
            return False
        return self._db.delete("admin_devices", {"user_id": f"eq.{user_id}", "token": f"eq.{token}"})

    def prune(self, token: str) -> bool:
        """Remove a token FCM reported as permanently invalid (any owner)."""
        if not token:
            return False
        ok = self._db.delete("admin_devices", {"token": f"eq.{token}"})
        if ok:
            log.info("push.token_pruned")
        return ok


class FcmPushSender:
    """FCM HTTP v1 sender. Injectable `session` for tests."""

    def __init__(self, *, credentials_json: str = "", service_account_path: str = "",
                 project_id: str = "", session: requests.Session | None = None,
                 on_unregistered: Callable[[str], None] | None = None):
        self._session = session or requests.Session()
        self._on_unregistered = on_unregistered
        self._project_id = (project_id or "").strip()
        self._sa = self._load_service_account(credentials_json, service_account_path)
        if self._sa and not self._project_id:
            self._project_id = str(self._sa.get("project_id", "")).strip()
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    @staticmethod
    def _load_service_account(credentials_json: str, path: str) -> dict | None:
        raw = credentials_json.strip()
        try:
            if raw:
                sa = json.loads(raw)
            elif path.strip():
                with open(path.strip(), "r", encoding="utf-8") as fh:
                    sa = json.load(fh)
            else:
                return None
        except (ValueError, OSError) as exc:
            # Log the failure WITHOUT the credential contents (Rule 4).
            log.error("push.bad_credentials", error=type(exc).__name__)
            return None
        if not isinstance(sa, dict) or not sa.get("client_email") or not sa.get("private_key"):
            log.error("push.incomplete_credentials")
            return None
        return sa

    @property
    def enabled(self) -> bool:
        return bool(self._sa and self._project_id)

    # ── OAuth2: sign a JWT with the service account, exchange for an access token ──
    def _access(self) -> str | None:
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token
        try:
            import jwt  # PyJWT[crypto]; RS256 needs `cryptography`.
        except ImportError:
            log.error("push.missing_pyjwt", note="install PyJWT[crypto] to enable FCM")
            return None
        now = int(time.time())
        claims = {
            "iss": self._sa["client_email"], "sub": self._sa["client_email"],
            "aud": _OAUTH_URL, "scope": _FCM_SCOPE,
            "iat": now, "exp": now + 3600,
        }
        try:
            assertion = jwt.encode(claims, self._sa["private_key"], algorithm="RS256")
            resp = self._session.post(
                _OAUTH_URL,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": assertion},
                timeout=_TIMEOUT)
            if not resp.ok:
                log.error("push.oauth_failed", status=resp.status_code)
                return None
            tok = resp.json().get("access_token")
            if not tok:
                return None
            self._access_token = tok
            # Refresh 60s early to avoid using a just-expired token.
            self._token_expiry = now + max(60, int(resp.json().get("expires_in", 3600)) - 60)
            return tok
        except (ValueError, KeyError, requests.RequestException) as exc:
            log.error("push.oauth_error", error=type(exc).__name__)
            return None

    def _endpoint(self) -> str:
        return f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"

    def send(self, token: str, title: str, body: str, data: dict | None = None) -> bool:
        """Deliver one message. Returns True on success. Never raises."""
        if not self.enabled:
            log.info("push.disabled")
            return False
        if not token:
            return False
        access = self._access()
        if not access:
            return False
        # FCM v1 requires all data values to be strings.
        payload = {
            "message": {
                "token": token,
                "notification": {"title": (title or "")[:65], "body": (body or "")[:120]},
                "data": {k: str(v) for k, v in (data or {}).items()},
                "android": {"priority": "high",
                            "notification": {"channel_id": "ab_ops_alerts"}},
            }
        }
        try:
            resp = self._session.post(
                self._endpoint(), json=payload, timeout=_TIMEOUT,
                headers={"Authorization": f"Bearer {access}",
                         "Content-Type": "application/json"})
        except requests.RequestException as exc:
            log.error("push.send_error", error=type(exc).__name__)
            return False

        if resp.status_code == 200:
            return True
        # A force-refreshed/expired access token surfaces as 401 — drop the cache
        # so the next attempt re-authenticates.
        if resp.status_code == 401:
            self._access_token, self._token_expiry = None, 0.0
            log.warning("push.auth_rejected")
            return False
        code = self._fcm_error_code(resp)
        if code in _DEAD_TOKEN_CODES:
            # Permanent: prune so we stop targeting a dead device (self-healing).
            log.warning("push.token_dead", code=code)
            if self._on_unregistered is not None:
                try:
                    self._on_unregistered(token)
                except Exception:
                    log.exception("push.prune_failed")
            return False
        log.error("push.rejected", status=resp.status_code, code=code)
        return False

    @staticmethod
    def _fcm_error_code(resp) -> str:
        try:
            details = resp.json().get("error", {})
            code = details.get("status") or ""
            # The specific FCM code lives in details[].errorCode for some errors.
            for d in details.get("details", []) or []:
                if isinstance(d, dict) and d.get("errorCode"):
                    return str(d["errorCode"])
            return str(code)
        except (ValueError, AttributeError):
            return ""


def build_push(settings, db):
    """Construct the (fcm_sender, admin_token_lookup, registry) trio from config.

    Shared by the API process and the worker so both dispatch push identically.
    Dead tokens reported by FCM are pruned from the registry automatically.
    Returns `(fcm_sender_callable | None, lookup_callable | None, registry)`.
    """
    registry = AdminDeviceRegistry(db)
    sender = FcmPushSender(
        credentials_json=settings.fcm_credentials_json,
        service_account_path=settings.fcm_service_account_path,
        project_id=settings.fcm_project_id,
        on_unregistered=registry.prune,
    )
    if not sender.enabled:
        log.info("push.not_configured",
                 note="FCM credentials absent; admin push is a safe no-op")
        return None, registry.tokens_for, registry
    return sender.send, registry.tokens_for, registry

