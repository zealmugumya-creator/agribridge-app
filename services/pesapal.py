# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Pesapal API 3.0 client (server-side only).

The backend is the only party holding the consumer secret. Nothing in this
module is safe to run in a browser, and no token, secret or card field is ever
logged.

Flow implemented here:

  1. RequestToken            -> short-lived bearer token (cached in process)
  2. RegisterIPN             -> one durable IPN id, reused on every order
  3. SubmitOrderRequest      -> redirect URL + order_tracking_id
  4. GetTransactionStatus    -> the only source of truth for a callback/IPN

`account_number` is always our stable checkout reference. Pesapal uses it for
recurring subscriptions, so a RECURRING IPN can be matched back to the order
even when the customer opted in on Pesapal's iframe.

Money is never trusted from a callback: callers must fetch the transaction
status with `get_transaction_status` and compare amount, currency and
merchant_reference before marking anything paid.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

from services.logging import get_logger

log = get_logger(__name__)

SANDBOX_BASE_URL = "https://cybqa.pesapal.com/pesapalv3"
LIVE_BASE_URL = "https://pay.pesapal.com/v3"

# Pesapal tokens are valid for five minutes. Refresh a little early so a slow
# request cannot start with a token that expires mid-flight.
_TOKEN_TTL_SECONDS = 240
_DEFAULT_TIMEOUT = 20


class PesapalError(RuntimeError):
    """A gateway call failed; the message is safe to log but not to expose raw."""


def base_url_for(env: str | None) -> str:
    return LIVE_BASE_URL if str(env or "").strip().lower() in (
        "live", "production", "prod") else SANDBOX_BASE_URL


def is_completed(status: dict | None) -> bool:
    """True when GetTransactionStatus says the payment succeeded."""
    status = status or {}
    code = str(status.get("status_code") or "").strip()
    description = str(status.get("payment_status_description") or "").strip().lower()
    return code == "1" or description == "completed"


def is_failed(status: dict | None) -> bool:
    status = status or {}
    code = str(status.get("status_code") or "").strip()
    description = str(status.get("payment_status_description") or "").strip().lower()
    return code in ("0", "2") or description in ("failed", "invalid", "reversed")


def _safe_gateway_redirect(url: str) -> bool:
    """Only follow redirects that belong to Pesapal over HTTPS."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https"
            and (host == "pesapal.com" or host.endswith(".pesapal.com")))


def _safe_ipn_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


def _error_text(data, status_code: int) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)[:160]
        if isinstance(error, str) and error.strip():
            return error.strip()[:160]
        for key in ("message", "error_description"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
    return f"gateway returned HTTP {status_code}"


class PesapalClient:
    """Small, testable wrapper around the Pesapal v3 JSON API."""

    def __init__(self, consumer_key: str, consumer_secret: str, *,
                 base_url: str | None = None, env: str | None = None,
                 transport=None, timeout: int = _DEFAULT_TIMEOUT,
                 clock=None):
        self._consumer_key = str(consumer_key or "").strip()
        self._consumer_secret = str(consumer_secret or "").strip()
        self.base_url = (base_url or base_url_for(env)).rstrip("/")
        self._transport = transport or requests
        self._timeout = timeout
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._consumer_key and self._consumer_secret)

    # ── HTTP plumbing ────────────────────────────────────────────────────────
    @staticmethod
    def _json(response) -> dict:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send(self, method: str, path: str, *, auth: bool = True, **kwargs):
        if not self.configured:
            raise PesapalError("Pesapal is not configured")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        if auth:
            headers["Authorization"] = f"Bearer {self.request_token()}"
        try:
            response = self._transport.request(
                method, self.base_url + path, headers=headers,
                timeout=self._timeout, **kwargs)
        except requests.RequestException as exc:
            log.warning("pesapal.transport_error", path=path,
                        error=type(exc).__name__)
            raise PesapalError("gateway connection failed") from None
        data = self._json(response)
        if not getattr(response, "ok", False):
            raise PesapalError(_error_text(data, getattr(response, "status_code", 0)))
        error = data.get("error")
        if isinstance(error, dict) and (error.get("message") or error.get("code")):
            raise PesapalError(_error_text(data, getattr(response, "status_code", 0)))
        if not data:
            raise PesapalError("gateway returned an empty response")
        return data

    def request_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._token_expires_at:
                return self._token
            if not self.configured:
                raise PesapalError("Pesapal is not configured")
            try:
                response = self._transport.request(
                    "POST", self.base_url + "/api/Auth/RequestToken",
                    json={
                        "consumer_key": self._consumer_key,
                        "consumer_secret": self._consumer_secret,
                    },
                    headers={"Accept": "application/json",
                             "Content-Type": "application/json"},
                    timeout=self._timeout)
            except requests.RequestException as exc:
                log.warning("pesapal.token_error", error=type(exc).__name__)
                raise PesapalError("gateway authentication failed") from None
            data = self._json(response)
            token = str(data.get("token") or "").strip()
            if not getattr(response, "ok", False) or not token:
                log.warning("pesapal.token_rejected",
                            status=getattr(response, "status_code", 0))
                raise PesapalError("gateway authentication failed")
            self._token = token
            self._token_expires_at = now + _TOKEN_TTL_SECONDS
            return token

    # ── API operations ───────────────────────────────────────────────────────
    def register_ipn(self, url: str, notification_type: str = "POST") -> str:
        if not _safe_ipn_url(url):
            raise PesapalError("invalid IPN URL")
        method = str(notification_type or "POST").strip().upper()
        if method not in ("GET", "POST"):
            raise PesapalError("invalid IPN method")
        data = self._send("POST", "/api/URLSetup/RegisterIPN", json={
            "url": url,
            "ipn_notification_type": method,
        })
        ipn_id = str(data.get("ipn_id") or data.get("ipnId") or "").strip()
        if not ipn_id:
            raise PesapalError("gateway did not return an IPN id")
        return ipn_id

    def submit_order(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise PesapalError("invalid order payload")
        data = self._send("POST", "/api/Transactions/SubmitOrderRequest",
                          json=payload)
        redirect = str(data.get("redirect_url") or "").strip()
        tracking = str(data.get("order_tracking_id") or "").strip()
        if not redirect or not tracking:
            raise PesapalError("gateway did not return a payment link")
        if not _safe_gateway_redirect(redirect):
            log.error("pesapal.unsafe_redirect", host=urlparse(redirect).hostname or "")
            raise PesapalError("gateway returned an unexpected payment link")
        return data

    def get_transaction_status(self, tracking_id: str) -> dict:
        tracking_id = str(tracking_id or "").strip()
        if not tracking_id or len(tracking_id) > 80 or not all(
                ch.isalnum() or ch in "-_" for ch in tracking_id):
            raise PesapalError("invalid tracking id")
        return self._send("GET", "/api/Transactions/GetTransactionStatus",
                          params={"orderTrackingId": tracking_id})
