# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Shared pytest fixtures and fakes.

These tests exercise the PYTHON logic (validation, idempotency handling, retry /
dead-letter, authz, HTTP wiring) with the Supabase RPC layer faked. The atomic
stock-reservation and state-machine guarantees themselves live in Postgres
functions (supabase/migrations/*.sql) and are covered by the SQL integration
tests noted in docs/IMPLEMENTATION_STATUS.md — they require a live DB, which is
an external blocker. Nothing here fabricates a passing integration test.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure the repo root is importable regardless of where pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set a safe dev environment at IMPORT time, before any test imports app.py
# (app.py builds its Settings at module load and fails closed in production).
os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-service-key")
os.environ.pop("DISCORD_WEBHOOK_URL", None)
os.environ.pop("SENTRY_DSN", None)


class RpcResult:
    """Mirrors services.supabase_client.RpcResult for fakes."""
    def __init__(self, ok, data=None, status=200, error=""):
        self.ok = ok
        self.data = data
        self.status = status
        self.error = error


class FakeSupabase:
    """Records RPC calls and returns scripted results.

    `rpc_results` maps fn-name -> either a single RpcResult, or a list consumed
    in order (for multi-call flows), or a callable(args)->RpcResult.
    """
    def __init__(self, rpc_results=None, select_results=None):
        self.rpc_results = rpc_results or {}
        self.select_results = select_results or {}
        self.rpc_calls: list[tuple[str, dict]] = []
        self.select_calls: list[tuple[str, dict]] = []
        self.upsert_calls: list[tuple[str, dict | list, str | None]] = []
        self.delete_calls: list[tuple[str, dict]] = []
        self.insert_calls: list[tuple[str, dict | list]] = []
        self.insert_result = RpcResult(ok=True, data=[{"id": "req-1"}])
        self.upsert_result = RpcResult(ok=True, data=None)
        self.delete_result = True
        self.configured = True

    def rpc(self, fn, args):
        self.rpc_calls.append((fn, args))
        r = self.rpc_results.get(fn)
        if callable(r):
            return r(args)
        if isinstance(r, list):
            return r.pop(0) if r else RpcResult(ok=True, data=None)
        return r if r is not None else RpcResult(ok=True, data=None)

    def select(self, table, filters=None, limit=100):
        self.select_calls.append((table, filters))
        return self.select_results.get(table, [])

    def update(self, table, data, eq_col, eq_val):
        return True

    def upsert(self, table, data, on_conflict=None):
        self.upsert_calls.append((table, data, on_conflict))
        return self.upsert_result

    def insert(self, table, rows):
        self.insert_calls.append((table, rows))
        return self.insert_result

    def delete(self, table, filters):
        self.delete_calls.append((table, filters))
        return self.delete_result

    def ping(self):
        return RpcResult(ok=True, status=200)


@pytest.fixture
def fake_supabase():
    return FakeSupabase()


@pytest.fixture(autouse=True)
def _dev_env(monkeypatch):
    """Run every test in development mode with a fixed secret so config never
    fails closed and no real credentials are touched."""
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("JWT_SECRET", "test-secret-not-for-production")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-service-key")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The API's limiter is in-process state; without a reset, tests that share a
    fake client IP would rate-limit each other and fail for the wrong reason."""
    import app as app_mod
    app_mod._LIMITER._fallback.clear()
    yield
    app_mod._LIMITER._fallback.clear()
