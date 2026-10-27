# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""Web Push HTTP endpoints.

The SPA can only subscribe a device for the user who owns the verified session;
the public key endpoint is the only unauthenticated route.
"""
from __future__ import annotations

import pytest
from conftest import RpcResult

import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


class FakeWebPush:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.public_key = 'test-vapid-public-key'
        self.register_result = RpcResult(ok=True, data=[], status=201)
        self.registered: list[tuple[str, dict, str]] = []
        self.unregistered: list[tuple[str, str]] = []

    def register(self, *, user_id, subscription, user_agent=''):
        self.registered.append((user_id, subscription, user_agent))
        return self.register_result

    def unregister(self, *, user_id, endpoint):
        self.unregistered.append((user_id, endpoint))
        return True


SUBSCRIPTION = {
    'endpoint': 'https://push.example/abc',
    'keys': {'p256dh': 'client-key', 'auth': 'client-auth'},
}


def test_public_key_hidden_when_push_not_configured(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'WEB_PUSH', FakeWebPush(enabled=False))
    assert client.get('/api/push/vapid-public-key').status_code == 503


def test_public_key_is_served_when_configured(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'WEB_PUSH', FakeWebPush())
    r = client.get('/api/push/vapid-public-key')
    assert r.status_code == 200
    assert r.get_json() == {'key': 'test-vapid-public-key'}


def test_subscribe_requires_verified_session(client):
    r = client.post('/api/user/push-subscriptions', json=SUBSCRIPTION)
    assert r.status_code == 401


def test_subscribe_registers_the_session_user(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('user-1', None))
    fake = FakeWebPush()
    monkeypatch.setattr(app_mod, 'WEB_PUSH', fake)
    r = client.post('/api/user/push-subscriptions', json=SUBSCRIPTION)
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}
    assert fake.registered[0][0] == 'user-1'
    assert fake.registered[0][1] == SUBSCRIPTION


def test_subscribe_rejects_a_subscription_the_service_refuses(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('user-1', None))
    fake = FakeWebPush()
    fake.register_result = None
    monkeypatch.setattr(app_mod, 'WEB_PUSH', fake)
    r = client.post('/api/user/push-subscriptions', json=SUBSCRIPTION)
    assert r.status_code == 400


def test_unsubscribe_is_scoped_to_the_session_user(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('user-1', None))
    fake = FakeWebPush()
    monkeypatch.setattr(app_mod, 'WEB_PUSH', fake)
    r = client.delete('/api/user/push-subscriptions', json={'endpoint': SUBSCRIPTION['endpoint']})
    assert r.status_code == 200
    assert fake.unregistered == [('user-1', SUBSCRIPTION['endpoint'])]
