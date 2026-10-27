# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Notification dispatcher — drains the durable outbox (mandate Phases 5, 6, 7).

Design (transactional outbox):
  * Business code enqueues via the `enqueue_notification` Postgres RPC inside the
    same transaction as the state change — so an event is never lost if the
    process dies right after committing.
  * A worker calls `claim_notifications()` (atomic, `FOR UPDATE SKIP LOCKED`),
    dispatches each item to its channel, then `resolve_notification()` marks it
    sent / schedules a backoff retry / dead-letters it.
  * `dedupe_key` suppresses duplicate alerts (Phase 6/7).

Channels are pluggable and each fails independently. Discord and FCM are wired
here; SMS reuses the existing Africa's Talking helper passed in by the caller.
When a channel is unconfigured the item is resolved as sent-with-skip (not an
error) so the queue drains, but a `critical` item is dead-lettered for the
alert-health fallback rather than silently dropped.
"""
from __future__ import annotations

from collections.abc import Callable

from services.discord import DiscordMessage, DiscordNotifier
from services.logging import get_logger

log = get_logger(__name__)

# Signature: (token:str, title:str, body:str, data:dict) -> bool
FcmSender = Callable[[str, str, str, dict], bool]
# Signature: (phone:str, message:str) -> bool
SmsSender = Callable[[str, str], bool]


class NotificationDispatcher:
    def __init__(
        self,
        supabase,                              # services.supabase_client.SupabaseClient
        discord: DiscordNotifier | None = None,
        fcm_sender: FcmSender | None = None,
        sms_sender: SmsSender | None = None,
        admin_token_lookup: Callable[[str | None], list[str]] | None = None,
        web_push=None,                         # services.webpush.WebPushService | None
    ):
        self._db = supabase
        self._discord = discord
        self._fcm = fcm_sender
        self._sms = sms_sender
        # Given a recipient user_id (or None for "all admins"), return FCM tokens.
        self._admin_tokens = admin_token_lookup
        # Optional: also deliver every in-app notification as a Web Push (Android bar).
        self._web_push = web_push

    # ── Enqueue (called from business transactions) ──────────────────────────
    def enqueue(self, *, channel: str, audience: str, event_type: str,
                severity: str = "info", title: str = "", body: str = "",
                payload: dict | None = None, dedupe_key: str | None = None,
                recipient: str | None = None, correlation_id: str | None = None) -> str | None:
        res = self._db.rpc("enqueue_notification", {
            "p_channel": channel, "p_audience": audience,
            "p_recipient": recipient, "p_event_type": event_type,
            "p_severity": severity, "p_title": title, "p_body": body,
            "p_payload": payload or {}, "p_dedupe_key": dedupe_key,
            "p_correlation_id": correlation_id,
        })
        if not res.ok:
            log.error("notify.enqueue_failed", event_type=event_type, error=res.error)
            return None
        # Mirror in-app notifications to the user's phone. Best-effort and async:
        # a push failure must never affect the business transaction.
        if channel == "in_app" and recipient and self._web_push is not None:
            self._web_push.send_to_user_async(recipient, title, body)
        return res.data

    # ── Drain loop (run by the worker) ───────────────────────────────────────
    def drain(self, batch_size: int = 10) -> dict[str, int]:
        stats = {"claimed": 0, "sent": 0, "failed": 0, "dead": 0}
        res = self._db.rpc("claim_notifications", {"p_limit": batch_size})
        if not res.ok:
            log.error("notify.claim_failed", error=res.error)
            return stats
        items = res.data if isinstance(res.data, list) else ([res.data] if res.data else [])
        stats["claimed"] = len(items)

        for item in items:
            nid = item.get("id")
            ok, err = self._dispatch_one(item)
            self._db.rpc("resolve_notification", {"p_id": nid, "p_ok": ok, "p_error": err})
            if ok:
                stats["sent"] += 1
            elif item.get("attempts", 0) + 1 >= item.get("max_attempts", 5):
                stats["dead"] += 1
            else:
                stats["failed"] += 1
        if stats["claimed"]:
            log.info("notify.drain", **stats)
        return stats

    def _dispatch_one(self, item: dict) -> tuple[bool, str]:
        channel = item.get("channel")
        severity = item.get("severity", "info")
        event_type = item.get("event_type", "unknown")
        title = item.get("title") or event_type
        body = item.get("body") or ""
        payload = item.get("payload") or {}
        correlation_id = item.get("correlation_id")

        try:
            if channel == "discord":
                if not (self._discord and self._discord.enabled):
                    # Alert-health: never silently drop a critical incident.
                    if severity == "critical":
                        return False, "discord not configured (critical)"
                    return True, ""   # non-critical: skip-and-drain
                msg = DiscordMessage(
                    event_type=event_type, severity=severity, title=title,
                    fields={k: str(v) for k, v in payload.items()},
                    correlation_id=correlation_id,
                    order_ref=str(payload.get("order_ref") or item.get("order_ref") or "") or None,
                )
                delivered = self._discord.send(msg)
                return (True, "") if delivered else (False, "discord send failed")

            if channel == "push":
                if not self._fcm or not self._admin_tokens:
                    if severity == "critical":
                        return False, "push not configured (critical)"
                    return True, ""
                tokens = self._admin_tokens(item.get("recipient"))
                if not tokens:
                    return True, ""    # no devices registered yet — nothing to do
                sent_any = False
                for tok in tokens:
                    # Lock-screen safe: concise summary; detail loads after auth
                    # (Phase 6). Data payload carries the deep-link id only.
                    if self._fcm(tok, title[:65], body[:120], {"event": event_type, **payload}):
                        sent_any = True
                return (True, "") if sent_any else (False, "fcm rejected all tokens")

            if channel == "sms":
                if not self._sms:
                    return True, ""
                phone = str(payload.get("phone") or "")
                if not phone:
                    return True, ""
                return (True, "") if self._sms(phone, body[:300]) else (False, "sms failed")

            # Unknown channel: dead-letter rather than spin forever.
            return False, f"unknown channel {channel}"
        except Exception as exc:                     # a bad item must not wedge the queue
            log.exception("notify.dispatch_error", event_type=event_type)
            return False, type(exc).__name__
