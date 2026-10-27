# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Tests for services.config (fail-closed production config, H1/M6) and
services.logging (secret redaction)."""
from __future__ import annotations

import json

import pytest

from services.config import ConfigError, load_settings
from services.logging import JsonFormatter, _redact, set_correlation_id


# ── config ────────────────────────────────────────────────────────────────────
def test_production_requires_jwt_secret(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("SUPABASE_KEY", "k")
    with pytest.raises(ConfigError) as ei:
        load_settings()
    assert "JWT_SECRET" in str(ei.value)


def test_production_requires_supabase_key(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "s")
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    with pytest.raises(ConfigError) as ei:
        load_settings()
    assert "SUPABASE_KEY" in str(ei.value)


def test_production_ok_when_both_set(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "s")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    s = load_settings()
    assert s.is_production and s.jwt_secret == "s"


def test_dev_does_not_fail_closed(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    s = load_settings()      # must not raise
    assert not s.is_production


def test_no_localhost_cors_in_production(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "s")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    s = load_settings()
    assert not any("localhost" in o or "127.0.0.1" in o for o in s.cors_origins)


def test_localhost_cors_allowed_in_dev(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    s = load_settings()
    assert any("localhost" in o for o in s.cors_origins)


def test_admin_allowed_uuids_parsed(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("ADMIN_ALLOWED_UUIDS", "aaa, bbb ,,ccc")
    s = load_settings()
    assert s.admin_allowed_uuids == ("aaa", "bbb", "ccc")


# ── logging redaction ─────────────────────────────────────────────────────────
def test_redact_scrubs_sensitive_keys():
    out = _redact({"password": "hunter2", "jwt_secret": "x", "order_ref": "AB-1",
                   "nested": {"flw_secret_key": "sk", "ok": 1}})
    assert out["password"] == "[REDACTED]"
    assert out["jwt_secret"] == "[REDACTED]"
    assert out["order_ref"] == "AB-1"          # safe field preserved
    assert out["nested"]["flw_secret_key"] == "[REDACTED]"
    assert out["nested"]["ok"] == 1


def test_formatter_emits_json_with_correlation_id():
    import logging
    set_correlation_id("corr-123")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    rec.fields = {"order_ref": "AB-9", "token": "secret"}   # token must be scrubbed
    line = JsonFormatter().format(rec)
    obj = json.loads(line)
    assert obj["msg"] == "hello"
    assert obj["correlation_id"] == "corr-123"
    assert obj["order_ref"] == "AB-9"
    assert obj["token"] == "[REDACTED]"
    assert "secret" not in line
