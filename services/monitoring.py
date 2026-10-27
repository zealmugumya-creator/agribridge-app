# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Error monitoring wrapper (mandate Phase 8; audit finding H5).

Sentry is optional: when SENTRY_DSN is unset the module is a safe no-op, so the
app runs (and tests pass) without it. When enabled, secrets and sensitive PII
are scrubbed before any telemetry leaves the process — Sentry must never receive
passwords, tokens, keys, or full payment credentials (Phase 8).

Imported lazily so `sentry-sdk` is not a hard dependency for local runs/tests.
"""
from __future__ import annotations

from typing import Any

from services.logging import get_logger

log = get_logger(__name__)

_enabled = False

# Keys whose values are stripped from event data before sending.
_SCRUB_KEYS = {
    "password", "admin_password", "jwt_secret", "token", "access_token",
    "refresh_token", "authorization", "apikey", "api_key", "supabase_key",
    "secret", "secret_key", "flw_secret_key", "flw_webhook_hash",
    "discord_webhook_url", "sentry_dsn", "fcm_credentials_json",
    "consumer_secret", "subscription_key", "backup_passphrase", "verif-hash",
}
# Value patterns scrubbed regardless of key name.
_SCRUB_SUBSTRINGS = ("sb_secret", "sk_live", "sk_test", "flw_sk", "Bearer ", "atsk_")


def _scrub(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in _SCRUB_KEYS:
                out[k] = "[FILTERED]"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(data, list | tuple):
        return [_scrub(v) for v in data]
    if isinstance(data, str):
        s = data
        for token in _SCRUB_SUBSTRINGS:
            if token in s:
                return "[FILTERED]"
        return s
    return data


def init(dsn: str, environment: str = "production", traces_sample_rate: float = 0.1) -> bool:
    """Initialize Sentry if a DSN is provided. Returns True when active."""
    global _enabled
    if not dsn:
        log.info("sentry.disabled", reason="SENTRY_DSN not set")
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            traces_sample_rate=traces_sample_rate,
            integrations=[FlaskIntegration()],
            send_default_pii=False,          # do not auto-attach PII
            before_send=_before_send,
        )
        _enabled = True
        log.info("sentry.enabled", environment=environment)
        return True
    except Exception as exc:                 # sentry_sdk missing or bad DSN
        log.warning("sentry.init_failed", error=type(exc).__name__)
        return False


def _before_send(event: dict, hint: dict) -> dict | None:
    """Scrub every outgoing event. Returning None would drop it; we sanitize."""
    try:
        if "request" in event and "data" in event["request"]:
            event["request"]["data"] = _scrub(event["request"]["data"])
        if "extra" in event:
            event["extra"] = _scrub(event["extra"])
        # Scrub headers (Authorization, cookies, webhook hashes).
        req = event.get("request", {})
        if "headers" in req:
            req["headers"] = _scrub(req["headers"])
        if "cookies" in req:
            req["cookies"] = "[FILTERED]"
    except Exception:
        # If scrubbing itself fails, drop the event rather than risk leaking.
        return None
    return event


def capture_exception(exc: BaseException, **extra: Any) -> None:
    if not _enabled:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in _scrub(extra or {}).items():
                scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
    except Exception:
        log.warning("sentry.capture_failed")


def capture_message(message: str, level: str = "info", **extra: Any) -> None:
    if not _enabled:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in _scrub(extra or {}).items():
                scope.set_extra(k, v)
            sentry_sdk.capture_message(message, level=level)
    except Exception:
        log.warning("sentry.capture_failed")
