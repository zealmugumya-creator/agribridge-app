# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Durable notification worker (mandate Phase 5: "persistent job queue or
equivalent durable worker mechanism").

Runs independently of the web process — the queue lives in Postgres
(`notification_outbox`), so this worker can crash, restart, or run on a separate
Render background service without losing events. It repeatedly claims a batch
(`FOR UPDATE SKIP LOCKED`), dispatches each item, and resolves it (sent /
backoff-retry / dead-letter).

Run:
    python -m services.worker            # foreground loop
    WIDGET=... on Render as a background worker with the same env as the API.

The worker is intentionally dependency-light: it constructs the same service
objects the API uses and shares configuration.
"""
from __future__ import annotations

import os
import signal
import time

from services.config import load_settings
from services.discord import DiscordNotifier
from services.logging import get_logger, new_correlation_id, set_correlation_id
from services.matching import MatchingService
from services.notifications import NotificationDispatcher
from services.push import build_push
from services.supabase_client import SupabaseClient

log = get_logger(__name__)

_running = True


def _stop(signum, frame):  # graceful shutdown on SIGTERM/SIGINT
    global _running
    _running = False
    log.info("worker.shutdown_requested", signal=signum)


def build_dispatcher(settings=None):
    s = settings or load_settings()
    db = SupabaseClient(s.supabase_url, s.supabase_key)
    # Discord dead-letter: if Discord fails, persist a critical fallback alert so
    # the incident is not lost (Phase 7 "alert-health").
    def _dead_letter(msg, err):
        db.rpc("enqueue_notification", {
            "p_channel": "push", "p_audience": "admin",
            "p_recipient": None, "p_event_type": "discord.dead_letter",
            "p_severity": msg.severity if msg.severity == "critical" else "warning",
            "p_title": f"Discord alert failed: {msg.event_type}",
            "p_body": (msg.title or msg.event_type)[:120],
            "p_payload": {"original_event": msg.event_type, "discord_error": err,
                          **(msg.fields or {})},
            "p_dedupe_key": f"dl:{msg.event_type}:{msg.correlation_id or ''}",
            "p_correlation_id": msg.correlation_id,
        })
    discord = DiscordNotifier(s.discord_webhook_url, dead_letter=_dead_letter)
    fcm_sender, admin_tokens, _registry = build_push(s, db)
    return NotificationDispatcher(supabase=db, discord=discord,
                                  fcm_sender=fcm_sender, admin_token_lookup=admin_tokens)


def build_matching(settings=None, dispatcher=None):
    s = settings or load_settings()
    db = (dispatcher._db if dispatcher is not None
          else SupabaseClient(s.supabase_url, s.supabase_key))
    return MatchingService(supabase=db, dispatcher=dispatcher)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    settings = load_settings()
    dispatcher = build_dispatcher(settings)
    matching = build_matching(settings, dispatcher)
    poll_seconds = max(2, int(os.environ.get("WORKER_POLL_SECONDS", "5")))
    batch_size = max(1, int(os.environ.get("WORKER_BATCH_SIZE", "10")))
    # Matching runs on a slower cadence than notification draining.
    match_every = max(1, int(os.environ.get("WORKER_MATCH_EVERY_N_POLLS", "3")))

    log.info("worker.started", poll_seconds=poll_seconds, batch_size=batch_size,
             discord_enabled=dispatcher._discord.enabled if dispatcher._discord else False)

    idle = 0
    poll = 0
    while _running:
        set_correlation_id(new_correlation_id())
        poll += 1
        claimed = 0
        try:
            stats = dispatcher.drain(batch_size=batch_size)
            claimed = stats.get("claimed", 0)
        except Exception:
            log.exception("worker.drain_error")

        # Periodic matching pass: expire overdue offers, then match/offer the rest.
        if poll % match_every == 0:
            try:
                matching.run_matching_pass(batch_size=batch_size)
            except Exception:
                log.exception("worker.match_error")

        if claimed:
            idle = 0
        else:
            idle += 1
        time.sleep(min(poll_seconds + idle, poll_seconds * 4))

    log.info("worker.stopped")


if __name__ == "__main__":
    main()
