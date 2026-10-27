# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""Farmer listing-edit endpoint tests.

The `listings_owner_update` RLS policy lets an owner write any column of their
own listing, so the browser could set quantity_kg below reserved_qty and drive
the orderable amount negative. This endpoint is the guarded path: it re-reads
the row, refuses that, re-derives stock_status the way the reservation triggers
do, and writes back only while reserved_qty is unchanged.
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


class FakeSupa:
    """Records the guard filters, and enforces them the way PostgREST would.

    `update_checked` returns zero rows when a `reserved_qty` guard no longer
    matches the stored value, so a test that claims a lost race only passes if
    the endpoint actually sent the guard.
    """

    def __init__(self, row):
        self._row = row
        self.selects = []
        self.updates = []

    def select(self, table, filters=None, limit=100):
        self.selects.append((table, dict(filters or {})))
        # A copy, the way a JSON response is: the caller must not be able to
        # mutate the stored row through the object it was handed.
        return [dict(self._row)] if self._row else []

    def update_checked(self, table, data, filters):
        self.updates.append((table, dict(data), dict(filters)))
        guard = filters.get('reserved_qty')
        if guard is not None and guard != f"eq.{self._row.get('reserved_qty')}":
            return RpcResult(ok=True, data=[], status=200)
        return RpcResult(ok=True, data=[dict(self._row)], status=200)


def _prepare(monkeypatch, reserved='12', qty='50'):
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('farmer-1', None))
    monkeypatch.setattr(app_mod, 'legal_gate', lambda _uid: None)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **kw: None)
    row = {'id': 'listing-1', 'farmer_id': 'farmer-1', 'crop_name': 'Maize',
           'reserved_qty': reserved, 'quantity_kg': qty,
           'stock_status': 'available', 'is_available': True}
    fake = FakeSupa(row)
    monkeypatch.setattr(app_mod, 'SUPA', fake)
    return fake


def _patched(fake):
    return fake.updates[0][1]


def _guard(fake):
    return fake.updates[0][2]


def test_edit_requires_verified_session(client):
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 10})
    assert r.status_code == 401


def test_edit_rejects_a_listing_the_caller_does_not_own(client, monkeypatch):
    fake = _prepare(monkeypatch)
    fake.select = lambda table, filters=None, limit=100: []
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 10})
    assert r.status_code == 404
    assert fake.updates == []


def test_the_ownership_filter_is_part_of_the_read_and_the_write(client, monkeypatch):
    fake = _prepare(monkeypatch)
    client.patch('/api/listings/listing-1', json={'quantity_kg': 20})
    assert fake.selects[0][1]['farmer_id'] == 'eq.farmer-1'
    assert _guard(fake)['farmer_id'] == 'eq.farmer-1'
    assert _guard(fake)['id'] == 'eq.listing-1'


def test_empty_body_is_refused(client, monkeypatch):
    fake = _prepare(monkeypatch)
    r = client.patch('/api/listings/listing-1', json={})
    assert r.status_code == 400
    assert fake.updates == []


def test_quantity_below_reserved_stock_is_refused(client, monkeypatch):
    fake = _prepare(monkeypatch, reserved='12', qty='50')
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 11})
    assert r.status_code == 409
    body = r.get_json()
    assert body['reserved_qty'] == 12.0
    assert '12 kg reserved' in body['error']
    assert fake.updates == []


def test_quantity_may_equal_reserved_and_then_reads_unavailable(client, monkeypatch):
    fake = _prepare(monkeypatch, reserved='12', qty='50')
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 12})
    assert r.status_code == 200
    assert _patched(fake)['quantity_kg'] == 12.0
    assert _patched(fake)['stock_status'] == 'unavailable'


def test_stock_status_follows_the_trigger_thresholds(client, monkeypatch):
    # The trigger calls it low_stock at 10% or less remaining: 8 of 100 is,
    # 20 of 100 is not.
    fake = _prepare(monkeypatch, reserved='92', qty='100')
    client.patch('/api/listings/listing-1', json={'quantity_kg': 100})
    assert _patched(fake)['stock_status'] == 'low_stock'

    fake2 = _prepare(monkeypatch, reserved='80', qty='100')
    client.patch('/api/listings/listing-1', json={'quantity_kg': 100})
    assert _patched(fake2)['stock_status'] == 'available'


def test_the_write_is_guarded_on_the_reserved_value_just_read(client, monkeypatch):
    fake = _prepare(monkeypatch, reserved='12', qty='50')
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 40})
    assert r.status_code == 200
    assert _guard(fake)['reserved_qty'] == 'eq.12'


def test_a_reservation_in_flight_loses_the_write_instead_of_overwriting(client, monkeypatch):
    fake = _prepare(monkeypatch, reserved='12', qty='50')
    # An order reserves 3 kg after the endpoint read the row but before its
    # guarded write lands. Only a write that carries the value it read can
    # notice, so this asserts the race is lost rather than silently won.
    read = fake.select

    def racy_select(table, filters=None, limit=100):
        rows = read(table, filters, limit)
        fake._row['reserved_qty'] = '15'
        return rows

    fake.select = racy_select
    r = client.patch('/api/listings/listing-1', json={'quantity_kg': 40})
    assert r.status_code == 409
    assert 'came in while you were editing' in r.get_json()['error']
    assert _guard(fake)['reserved_qty'] == 'eq.12'


def test_price_and_quantity_bounds_match_the_form(client, monkeypatch):
    fake = _prepare(monkeypatch)
    assert client.patch('/api/listings/listing-1', json={'price_per_kg': 0}).status_code == 400
    assert client.patch('/api/listings/listing-1',
                        json={'price_per_kg': app_mod.LISTING_MAX_PRICE + 1}).status_code == 400
    assert client.patch('/api/listings/listing-1',
                        json={'quantity_kg': app_mod.LISTING_MAX_QTY + 1}).status_code == 400
    assert client.patch('/api/listings/listing-1', json={'quantity_kg': 'abc'}).status_code == 400
    assert fake.updates == []


def test_hiding_a_listing_does_not_touch_stock_or_its_guard(client, monkeypatch):
    fake = _prepare(monkeypatch, reserved='12', qty='50')
    r = client.patch('/api/listings/listing-1', json={'is_available': False})
    assert r.status_code == 200
    assert _patched(fake)['is_available'] is False
    assert 'stock_status' not in _patched(fake)
    assert 'quantity_kg' not in _patched(fake)
    # Nothing about the stock figure was asserted, so no reserved_qty guard.
    assert 'reserved_qty' not in _guard(fake)


def test_crop_name_cannot_be_blanked_out(client, monkeypatch):
    fake = _prepare(monkeypatch)
    assert client.patch('/api/listings/listing-1', json={'crop_name': '   '}).status_code == 400
    assert fake.updates == []


def test_updated_at_is_a_timestamp_not_a_sql_fragment(client, monkeypatch):
    fake = _prepare(monkeypatch)
    client.patch('/api/listings/listing-1', json={'price_per_kg': 1900})
    stamp = _patched(fake)['updated_at']
    assert 'now()' not in stamp
    assert stamp[:4].isdigit() and 'T' in stamp


def test_a_failed_write_reports_a_bad_gateway_not_success(client, monkeypatch):
    fake = _prepare(monkeypatch)
    fake.update_checked = lambda table, data, filters: RpcResult(
        ok=False, status=400, error='column does not exist')
    r = client.patch('/api/listings/listing-1', json={'price_per_kg': 1900})
    assert r.status_code == 502
    assert 'Could not save' in r.get_json()['error']
