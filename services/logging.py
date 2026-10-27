# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Structured JSON logging with per-request correlation IDs (audit finding H5).

Every log line is a single JSON object so it can be parsed by Render/log
aggregators. A correlation_id is generated per request (or accepted from a
trusted upstream header) and attached to all logs emitted while handling it.

Secrets are never logged: use `services.logging.get_logger(__name__)` and pass
only safe fields. A redaction filter scrubs known-sensitive keys defensively.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Correlation ID lives in a ContextVar so it is safe across gunicorn workers and
# threads, and does not leak between concurrent requests.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

# Keys whose values must never appear in logs, even if a caller passes them.
_REDACT_KEYS = {
    "password", "admin_password", "jwt_secret", "token", "access_token",
    "refresh_token", "authorization", "apikey", "api_key", "supabase_key",
    "secret", "secret_key", "flw_secret_key", "flw_webhook_hash",
    "discord_webhook_url", "webhook", "sentry_dsn", "fcm_credentials_json",
    "consumer_secret", "subscription_key", "backup_passphrase",
}


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(cid: str | None) -> str:
    cid = (cid or "").strip() or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _REDACT_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [_redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "service": "agribridge-api",
        }
        # Structured extras attached via logger.info("msg", extra={"fields": {...}})
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(_redact(fields))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _LoggerAdapter(logging.LoggerAdapter):
    """Lets callers do log.info("event", correlation=..., order_ref=...)."""

    def process(self, msg, kwargs):
        fields = dict(self.extra or {})
        extra = kwargs.pop("extra", None)
        if isinstance(extra, dict):
            fields.update(extra)
        # Pull any top-level keyword fields into the structured payload.
        for key in list(kwargs.keys()):
            if key not in ("exc_info", "stack_info", "stacklevel"):
                fields[key] = kwargs.pop(key)
        if fields:
            kwargs["extra"] = {"fields": fields}
        return msg, kwargs


_configured = False


def configure_logging() -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    global _configured
    if _configured:
        return
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # Quiet noisy libs.
    for noisy in ("urllib3", "requests", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.LoggerAdapter:
    configure_logging()
    return _LoggerAdapter(logging.getLogger(name), {})
