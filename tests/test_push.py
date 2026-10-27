# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Phase 6 — FCM admin push: device registry + HTTP v1 sender.

Covers the Python contract: registry CRUD/upsert-on-natural-key, sender
enable/disable, OAuth-cache reuse, and dead-token pruning. No Firebase
credentials are contacted — a fake session returns canned responses.
"""
from __future__ import annotations

import json
import time

from conftest import FakeSupabase, RpcResult

import services.push as push
from services.push import AdminDeviceRegistry, FcmPushSender, build_push

_VALID_SA = json.dumps({
    "client_email": "svc@proj.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "project_id": "proj",
})


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeSession:
    """Records posts; returns a scripted queue of responses."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._responses.pop(0) if self._responses else FakeResp(200)


def _enabled_sender(responses):
    session = FakeSession(responses)
    s = FcmPushSender(credentials_json=_VALID_SA, session=session)
    # Pre-seed the OAuth token so no real Google call is attempted.
    s._access_token = "cached-access-token"
    s._token_expiry = time.time() + 3600
    return s, session


# ── AdminDeviceRegistry ───────────────────────────────────────────────────────
def test_register_upserts_on_natural_key():
    db = FakeSupabase()
    reg = AdminDeviceRegistry(db)
    res = reg.register(user_id="u1", token="tok-abc", platform="android")
    assert res.ok
    table, row, on_conflict = db.upsert_calls[0]
    assert table == "admin_devices"
    assert on_conflict == "provider,token"
    assert row["user_id"] == "u1" and row["token"] == "tok-abc"
    assert row["provider"] == "fcm" and row["platform"] == "android"
    assert row["last_seen_at"]


def test_register_rejects_missing_fields_and_bad_provider():
    reg = AdminDeviceRegistry(FakeSupabase())
    assert not reg.register(user_id="", token="t").ok
    assert not reg.register(user_id="u", token="").ok
    assert not reg.register(user_id="u", token="t", provider="carrier-pigeon").ok


def test_register_propagates_db_failure():
    db = FakeSupabase()
    db.upsert_result = RpcResult(ok=False, error="boom")
    assert not AdminDeviceRegistry(db).register(user_id="u", token="t").ok


def test_tokens_for_all_vs_scoped():
    db = FakeSupabase(select_results={"admin_devices": [{"token": "a"}, {"token": "b"}]})
    reg = AdminDeviceRegistry(db)
    assert reg.tokens_for() == ["a", "b"]
    _, filters = db.select_calls[-1]
    assert filters["provider"] == "eq.fcm" and "user_id" not in filters

    assert reg.tokens_for("u1") == ["a", "b"]
    _, filters = db.select_calls[-1]
    assert filters["user_id"] == "eq.u1"


def test_tokens_for_skips_malformed_rows():
    db = FakeSupabase(select_results={"admin_devices": [{"token": "a"}, {}, {"nope": 1}]})
    assert AdminDeviceRegistry(db).tokens_for() == ["a"]


def test_unregister_and_prune_issue_deletes():
    db = FakeSupabase()
    reg = AdminDeviceRegistry(db)
    assert reg.unregister(user_id="u1", token="t")
    table, filters = db.delete_calls[-1]
    assert table == "admin_devices" and filters["token"] == "eq.t"
    assert reg.prune("dead-token")
    assert db.delete_calls[-1][1]["token"] == "eq.dead-token"
    assert not reg.unregister(user_id="", token="t")
    assert not reg.prune("")


# ── FcmPushSender enablement ──────────────────────────────────────────────────
def test_sender_disabled_without_credentials():
    assert not FcmPushSender().enabled
    assert not FcmPushSender(credentials_json="not-json").enabled
    # Missing required fields.
    assert not FcmPushSender(credentials_json=json.dumps({"foo": "bar"})).enabled


def test_sender_enabled_with_valid_service_account():
    assert FcmPushSender(credentials_json=_VALID_SA).enabled
    # project_id inferred from the service account JSON.
    assert FcmPushSender(credentials_json=_VALID_SA)._project_id == "proj"


def test_send_is_noop_when_disabled():
    # A disabled sender never raises and never delivers.
    assert FcmPushSender().send("tok", "t", "b", {}) is False


# ── FcmPushSender.send over a fake session ────────────────────────────────────
def test_send_success_builds_v1_message():
    s, session = _enabled_sender([FakeResp(200)])
    assert s.send("tok-1", "Order created", "New order #42", {"event": "order.created"})
    url, kwargs = session.posts[0]
    assert url.endswith("/v1/projects/proj/messages:send")
    assert kwargs["headers"]["Authorization"] == "Bearer cached-access-token"
    msg = kwargs["json"]["message"]
    assert msg["token"] == "tok-1"
    assert msg["notification"]["title"] == "Order created"
    # FCM v1 requires string data values.
    assert msg["data"] == {"event": "order.created"}
    assert msg["android"]["priority"] == "high"


def test_send_401_clears_cached_token():
    s, _ = _enabled_sender([FakeResp(401)])
    assert s.send("tok", "t", "b", {}) is False
    assert s._access_token is None and s._token_expiry == 0.0


def test_send_prunes_unregistered_token():
    pruned = []
    s, _ = _enabled_sender([FakeResp(404, {"error": {"status": "NOT_FOUND",
                                                     "details": [{"errorCode": "UNREGISTERED"}]}})])
    s._on_unregistered = pruned.append
    assert s.send("dead-tok", "t", "b", {}) is False
    assert pruned == ["dead-tok"]


def test_send_transient_failure_returns_false_without_pruning():
    pruned = []
    s, _ = _enabled_sender([FakeResp(503)])
    s._on_unregistered = pruned.append
    assert s.send("tok", "t", "b", {}) is False
    assert pruned == []


def test_send_truncates_long_title_and_body():
    s, session = _enabled_sender([FakeResp(200)])
    s.send("tok", "X" * 200, "Y" * 400, {})
    notif = session.posts[0][1]["json"]["message"]["notification"]
    assert len(notif["title"]) <= 65 and len(notif["body"]) <= 120


# ── build_push factory ────────────────────────────────────────────────────────
def test_build_push_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("FCM_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("FCM_SERVICE_ACCOUNT_PATH", raising=False)
    monkeypatch.delenv("FCM_PROJECT_ID", raising=False)
    from services.config import load_settings
    sender, lookup, registry = build_push(load_settings(), FakeSupabase())
    assert sender is None
    assert callable(lookup)
    assert isinstance(registry, AdminDeviceRegistry)


def test_build_push_enabled_with_credentials(monkeypatch):
    monkeypatch.setenv("FCM_CREDENTIALS_JSON", _VALID_SA)
    from services.config import load_settings
    sender, lookup, registry = build_push(load_settings(), FakeSupabase())
    assert callable(sender) and callable(lookup)
    assert isinstance(registry, AdminDeviceRegistry)


def test_module_marks_dead_token_codes():
    assert "UNREGISTERED" in push._DEAD_TOKEN_CODES
