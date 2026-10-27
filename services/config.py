# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Centralized configuration + startup validation (audit findings H1, M6).

Reads every env var in one place and exposes a typed `Settings` object. Fails
CLOSED for the security-critical values: in production, a missing JWT_SECRET or
SUPABASE_KEY is a hard startup error rather than a silently degraded app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from services.logging import get_logger

log = get_logger(__name__)


class ConfigError(RuntimeError):
    """Raised when a required production setting is missing or invalid."""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        log.warning("config.invalid_int", var=name)
        return default


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("FLASK_ENV", "production"))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET", ""))
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    admin_allowed_uuids: tuple[str, ...] = field(default_factory=lambda: tuple(
        u.strip() for u in os.environ.get("ADMIN_ALLOWED_UUIDS", "").split(",") if u.strip()
    ))
    supabase_url: str = field(default_factory=lambda: os.environ.get(
        "SUPABASE_URL", "https://lvloaweijidgcuezdnmu.supabase.co"))
    supabase_key: str = field(default_factory=lambda: os.environ.get("SUPABASE_KEY", ""))
    supabase_db_url: str = field(default_factory=lambda: os.environ.get("SUPABASE_DB_URL", ""))
    public_base_url: str = field(default_factory=lambda: os.environ.get(
        "PUBLIC_BASE_URL", "https://agribrige.com"))
    discord_webhook_url: str = field(default_factory=lambda: os.environ.get("DISCORD_WEBHOOK_URL", ""))
    sentry_dsn: str = field(default_factory=lambda: os.environ.get("SENTRY_DSN", ""))
    fcm_credentials_json: str = field(default_factory=lambda: os.environ.get("FCM_CREDENTIALS_JSON", ""))
    fcm_service_account_path: str = field(default_factory=lambda: os.environ.get("FCM_SERVICE_ACCOUNT_PATH", ""))
    fcm_project_id: str = field(default_factory=lambda: os.environ.get("FCM_PROJECT_ID", ""))
    stock_freshness_minutes: int = field(default_factory=lambda: _int("STOCK_FRESHNESS_MINUTES", 4320))
    offer_response_seconds: int = field(default_factory=lambda: _int("OFFER_RESPONSE_SECONDS", 900))
    offer_batch_size: int = field(default_factory=lambda: _int("OFFER_BATCH_SIZE", 1))
    # Irreversible GoTrue purge is OFF by default; account deletion stops at a
    # reversible anonymization unless an operator explicitly enables this (Rule 8).
    account_hard_delete_enabled: bool = field(default_factory=lambda: _bool("ACCOUNT_HARD_DELETE_ENABLED", False))

    @property
    def is_production(self) -> bool:
        return self.env.lower() != "development"

    @property
    def cors_origins(self) -> list[str]:
        # M6: no localhost origins in production.
        base = ["https://agribrige.com", "https://www.agribrige.com", self.public_base_url]
        base.append("https://agribridge-1-og7a.onrender.com")
        if not self.is_production:
            base += ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173"]
        # De-duplicate, preserve order.
        seen: dict[str, None] = {}
        for o in base:
            if o:
                seen.setdefault(o, None)
        return list(seen)


def load_settings() -> Settings:
    s = Settings()
    if s.is_production:
        missing = []
        if not s.jwt_secret:
            missing.append("JWT_SECRET")
        if not s.supabase_key:
            missing.append("SUPABASE_KEY")
        if missing:
            # Fail closed (H1): do not boot an admin-capable API without a stable
            # secret. A random per-boot secret silently breaks sessions across the
            # 2 gunicorn workers, so we refuse instead.
            raise ConfigError(
                "Missing required production env var(s): " + ", ".join(missing)
            )
    else:
        if not s.jwt_secret:
            log.warning("config.dev_random_jwt",
                        note="JWT_SECRET unset; using an ephemeral dev secret")
    return s
