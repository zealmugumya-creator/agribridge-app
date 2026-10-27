# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Identity-scoped SMS and admin write boundaries.

The SMS receipt endpoint must not be usable as an open relay, and the generic
admin editor must not be able to rewrite order/payment status outside the
state machine and ledger.
"""
from __future__ import annotations

import pytest

import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('user-1', None))


class FakeSms:
    def __init__(self):
        self.sent = []

    def send(self, message, recipients, sender_id):
        self.sent.append({'message': message, 'recipients': recipients})


@pytest.fixture
def sms(monkeypatch):
    fake = FakeSms()
    monkeypatch.setattr(app_mod, 'at_sms', fake)
    return fake


def _orders(monkeypatch, rows):
    monkeypatch.setattr(app_mod, 'supa_get', lambda *a, **k: rows)


def test_sms_receipt_requires_a_session(client, sms):
    r = client.post('/api/sms-receipt', json={'ref': 'AB-1'})
    assert r.status_code == 401
    assert sms.sent == []


def test_sms_receipt_rejects_a_foreign_order(client, as_user, sms, monkeypatch):
    _orders(monkeypatch, [{'buyer_id': 'someone-else', 'buyer_phone': '0770000000',
                           'total_amount': 50000}])
    r = client.post('/api/sms-receipt', json={'ref': 'AB-1', 'phone': '0700000000',
                                              'total': 1})
    assert r.status_code == 404
    assert sms.sent == []


def test_sms_receipt_requires_a_reference(client, as_user, sms):
    r = client.post('/api/sms-receipt', json={'phone': '0700000000', 'total': 1})
    assert r.status_code == 400
    assert sms.sent == []


def test_sms_receipt_uses_the_order_phone_and_total(client, as_user, sms, monkeypatch):
    _orders(monkeypatch, [{'buyer_id': 'user-1', 'buyer_phone': '0770000000',
                           'total_amount': 50000, 'total_price': 1}])
    r = client.post('/api/sms-receipt', json={'ref': 'AB-1', 'phone': '0700000000',
                                              'total': 1})
    assert r.status_code == 200 and r.get_json()['sent'] is True
    assert len(sms.sent) == 1
    assert sms.sent[0]['recipients'] == ['0770000000']
    assert '50,000' in sms.sent[0]['message']
    assert '0700000000' not in sms.sent[0]['message']


def test_removed_notify_order_endpoint_is_gone(client, as_user, sms):
    r = client.post('/api/notify-order',
                    json={'farmer_id': 'f1', 'item': 'maize', 'qty': '1'})
    assert r.status_code == 404
    assert sms.sent == []


def _admin_headers():
    token = app_mod.make_token({'sub': 'admin', 'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def test_admin_editor_cannot_change_order_status(client, monkeypatch):
    updates = []
    monkeypatch.setattr(app_mod, 'supa_update',
                        lambda *a, **k: updates.append(a) or True)
    for field in ('status', 'payment_status'):
        r = client.patch('/api/admin/row', json={
            'table': 'orders', 'id': 'o1', 'fields': {field: 'delivered'}},
            headers=_admin_headers())
        assert r.status_code == 400
        assert 'workflow' in r.get_json()['error']
    assert updates == []


def test_admin_editor_still_updates_plain_order_fields(client, monkeypatch):
    updates = []
    monkeypatch.setattr(app_mod, 'supa_update',
                        lambda *a, **k: updates.append(a) or True)
    r = client.patch('/api/admin/row', json={
        'table': 'orders', 'id': 'o1', 'fields': {'delivery_address': 'Kampala'}},
        headers=_admin_headers())
    assert r.status_code == 200
    assert updates and updates[0][1] == {'delivery_address': 'Kampala'}


class FakeRpc:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, fn, args):
        self.calls.append((fn, args))
        return self.result


def test_admin_status_goes_through_the_state_machine(client, monkeypatch):
    from services.supabase_client import RpcResult
    rpc = FakeRpc(RpcResult(ok=True, data=True))
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)
    r = client.patch('/api/admin/order/o1/status', json={'status': 'confirmed'},
                     headers=_admin_headers())
    assert r.status_code == 200
    assert rpc.calls == [('set_order_status',
                          {'p_order_id': 'o1', 'p_new_status': 'confirmed'})]


def test_admin_status_rejects_illegal_transitions(client, monkeypatch):
    from services.supabase_client import RpcResult
    rpc = FakeRpc(RpcResult(ok=False, status=400,
                            error='illegal order transition pending -> delivered'))
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    r = client.patch('/api/admin/order/o1/status', json={'status': 'delivered'},
                     headers=_admin_headers())
    assert r.status_code == 409
    assert 'illegal' in r.get_json()['error']


def test_admin_status_reports_a_missing_order(client, monkeypatch):
    from services.supabase_client import RpcResult
    rpc = FakeRpc(RpcResult(ok=False, status=400, error='order not found'))
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    r = client.patch('/api/admin/order/o1/status', json={'status': 'confirmed'},
                     headers=_admin_headers())
    assert r.status_code == 404


def _transporter_location_stubs(monkeypatch, deliveries, delivery_ok=True):
    calls = {'get': [], 'update': []}

    def fake_get(table, filters=None, limit=100):
        calls['get'].append((table, filters))
        if table == 'transporters':
            return [{'id': 't1'}] if (filters or {}).get('supabase_uid') else []
        if table == 'deliveries':
            return deliveries
        return []

    def fake_update(table, data, eq_col, eq_val):
        calls['update'].append((table, data, eq_col, eq_val))
        return delivery_ok if table == 'deliveries' else True

    monkeypatch.setattr(app_mod, 'supa_get', fake_get)
    monkeypatch.setattr(app_mod, 'supa_update', fake_update)
    return calls


def test_transporter_location_updates_only_active_deliveries(client, as_user, monkeypatch):
    calls = _transporter_location_stubs(monkeypatch, [{'id': 'd1'}, {'id': 'd2'}])
    r = client.post('/api/transporter/location', json={'lat': 0.31, 'lng': 32.58})
    assert r.status_code == 200 and r.get_json()['deliveries_updated'] == 2
    filter_ = [c for c in calls['get'] if c[0] == 'deliveries'][0][1]
    assert filter_['status'] == 'in.(picked_up,in_transit,at_checkpoint)'
    delivery_updates = [c for c in calls['update'] if c[0] == 'deliveries']
    assert [c[2:] for c in delivery_updates] == [('id', 'd1'), ('id', 'd2')]
    assert all(set(c[1]) == {'transporter_lat', 'transporter_lng', 'location_updated_at'}
               for c in delivery_updates)


def test_transporter_location_does_not_touch_completed_deliveries(client, as_user, monkeypatch):
    calls = _transporter_location_stubs(monkeypatch, [])
    r = client.post('/api/transporter/location', json={'lat': 0.31, 'lng': 32.58})
    assert r.status_code == 200 and r.get_json()['deliveries_updated'] == 0
    assert [c for c in calls['update'] if c[0] == 'deliveries'] == []


def test_transporter_location_reports_a_failed_write(client, as_user, monkeypatch):
    _transporter_location_stubs(monkeypatch, [{'id': 'd1'}], delivery_ok=False)
    r = client.post('/api/transporter/location', json={'lat': 0.31, 'lng': 32.58})
    assert r.status_code == 503


def test_transporter_location_rejects_bad_coordinates(client, as_user):
    assert client.post('/api/transporter/location', json={'lat': 91, 'lng': 32}).status_code == 400


def test_farmer_order_actions_use_the_server_state_machine():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    assert "sbUpdate('orders'" not in html
    assert "/api/orders/'+encodeURIComponent(id)+'/status" in html
