# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Discord operations feed (mandate Phase 7; audit findings H5, and Phase 7).

The webhook URL is read ONLY from the environment (DISCORD_WEBHOOK_URL) and is
never logged, never returned to a client, and never committed. Messages are
concise, severity-colored, and carry correlation/order references. Delivery has
bounded timeouts, exponential backoff, and a caller-provided dead-letter path
(persist to the notification_outbox so a Discord outage cannot lose a critical
alert — Phase 7 "alert-health").

This module is transport-only. Business events decide *what* to send; this
decides *how* to send it reliably.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from services.logging import get_logger

log = get_logger(__name__)

# Discord embed color by severity.
_SEVERITY_COLOR = {"info": 0x2ECC71, "warning": 0xF0A020, "critical": 0xE05555}
_TIMEOUT = 8          # bounded per-attempt timeout (seconds)
_MAX_ATTEMPTS = 4
_BASE_BACKOFF = 0.5   # 0.5s, 1s, 2s ...


@dataclass
class DiscordMessage:
    event_type: str
    severity: str = "info"           # info | warning | critical
    title: str = ""
    fields: dict[str, str] | None = None
    correlation_id: str | None = None
    order_ref: str | None = None

    def to_payload(self) -> dict:
        embed_fields = [
            {"name": k[:256], "value": str(v)[:1024], "inline": True}
            for k, v in (self.fields or {}).items()
        ]
        if self.order_ref:
            embed_fields.insert(0, {"name": "Order", "value": self.order_ref, "inline": True})
        if self.correlation_id:
            embed_fields.append({"name": "Correlation", "value": self.correlation_id, "inline": True})
        return {
            "username": "AgriBridge Ops",
            "embeds": [{
                "title": (self.title or self.event_type)[:256],
                "description": f"`{self.event_type}`",
                "color": _SEVERITY_COLOR.get(self.severity, _SEVERITY_COLOR["info"]),
                "fields": embed_fields[:25],   # Discord caps at 25 fields
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "footer": {"text": f"severity: {self.severity}"},
            }],
        }


class DiscordNotifier:
    # Maps event_type prefix → env var name for the channel-specific webhook.
    # Falls back to the default webhook when a category-specific one isn't set.
    _CHANNEL_MAP: dict[str, str] = {
        "order":     "DISCORD_WEBHOOK_ORDERS",
        "payment":   "DISCORD_WEBHOOK_ORDERS",   # payment confirmed → orders channel
        "signup":    "DISCORD_WEBHOOK_SIGNUPS",
        "user":      "DISCORD_WEBHOOK_SIGNUPS",  # user.signup → signups channel
        "listing":   "DISCORD_WEBHOOK_LISTINGS",
        "scan":      "DISCORD_WEBHOOK_LISTINGS",  # crop doctor scans → listings channel
        "matching":  "DISCORD_WEBHOOK_MATCHING",
        "transport": "DISCORD_WEBHOOK_TRANSPORT",
        "server":    "DISCORD_WEBHOOK_ERRORS",
        "price":     "DISCORD_WEBHOOK_ERRORS",
        "ai":        "DISCORD_WEBHOOK_ERRORS",    # AI queries → errors/monitoring channel
        "service_request": "DISCORD_WEBHOOK_REQUESTS",  # cold storage / transport / loans
        "request":   "DISCORD_WEBHOOK_REQUESTS",
        "support":   "DISCORD_WEBHOOK_SUPPORT",   # contact, privacy, deletion, disease reports
        "safety":    "DISCORD_WEBHOOK_SUPPORT",   # fraud flags, role/device changes
    }

    def __init__(self, webhook_url: str, dead_letter: Callable[[DiscordMessage, str], None] | None = None):
        # webhook_url comes from env only. Empty => disabled (safe no-op).
        self._url = webhook_url or ""
        self._dead_letter = dead_letter
        self._session = requests.Session()
        # Pre-load channel-specific webhooks from environment (never log them).
        import os as _os
        self._channels: dict[str, str] = {}
        for prefix, env_var in self._CHANNEL_MAP.items():
            val = _os.environ.get(env_var, "").strip()
            if val:
                self._channels[prefix] = val

    def _url_for(self, event_type: str) -> str:
        """Return the best webhook URL for this event type."""
        prefix = event_type.split(".")[0]
        return self._channels.get(prefix, self._url)

    @property
    def enabled(self) -> bool:
        return bool(self._url) or bool(self._channels)

    def send(self, msg: DiscordMessage) -> bool:
        """Attempt delivery with bounded retries. Returns True on success.

        On total failure the message is handed to the dead-letter callback (if
        provided) so the alert can be persisted and retried / escalated by an
        independent channel. Never raises to the caller.
        """
        url = self._url_for(msg.event_type)
        if not url:
            log.info("discord.disabled", event_type=msg.event_type)
            return False

        last_error = "unknown"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._session.post(
                    url,
                    json=msg.to_payload(),
                    timeout=_TIMEOUT,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code in (200, 204):
                    log.info("discord.sent", event_type=msg.event_type,
                             severity=msg.severity, attempt=attempt,
                             correlation_id=msg.correlation_id)
                    return True
                if resp.status_code == 429:
                    # Respect Discord rate-limit retry_after when present.
                    try:
                        retry_after = float(resp.json().get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    last_error = "429 rate limited"
                    time.sleep(min(retry_after, 5.0))
                    continue
                # 4xx (other than 429) is not retryable — bad payload/webhook.
                last_error = f"http {resp.status_code}"
                if 400 <= resp.status_code < 500:
                    break
            except requests.RequestException as exc:
                last_error = type(exc).__name__
            # Exponential backoff before the next attempt.
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_BASE_BACKOFF * (2 ** (attempt - 1)))

        log.error("discord.failed", event_type=msg.event_type, severity=msg.severity,
                  error=last_error, attempts=_MAX_ATTEMPTS,
                  correlation_id=msg.correlation_id)
        if self._dead_letter is not None:
            try:
                self._dead_letter(msg, last_error)
            except Exception:
                log.exception("discord.dead_letter_failed", event_type=msg.event_type)
        return False
