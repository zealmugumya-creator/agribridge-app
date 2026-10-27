# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Listing inserts must match the live public.listings schema.

Production rejected every web listing with 500 "Failed to create listing"
because the insert sent farmer_name/source (not columns in listings) and a
null min_order_kg (the column is NOT NULL). These tests pin the insert shape
so a schema drift fails in CI instead of losing a farmer's listing.
"""
from __future__ import annotations

import pytest

import app as app_mod

# public.listings as deployed: base-schema.sql + stock columns from 0001.
LISTING_COLUMNS = {
    'id', 'farmer_id', 'crop_name', 'category', 'quantity_kg', 'price_per_kg',
    'unit', 'description', 'district', 'sub_county', 'village', 'is_organic',
    'is_verified', 'is_available', 'harvest_date', 'expiry_date', 'photo_url',
    'views', 'created_at', 'updated_at', 'farmer_phone', 'discount_pct',
    'sale_unit', 'min_order_kg', 'delivery_available', 'payment_terms',
    'image_url', 'video_url', 'reserved_qty', 'stock_status',
    'stock_updated_at', 'availability_date',
}


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


def _fake_persistence(monkeypatch):
    inserted = []

    def fake_get(table, filters=None, limit=100):
        if table == 'farmers':
            return [{'id': 'farmer-1', 'full_name': 'Nabuufu Susan',
                     'phone': '+256700000001', 'district': 'Kampala'}]
        return []

    def fake_insert(table, data):
        inserted.append((table, dict(data)))
        return [{'id': 'listing-1', **data}]

    monkeypatch.setattr(app_mod, 'supa_get', fake_get)
    monkeypatch.setattr(app_mod, 'supa_insert', fake_insert)
    monkeypatch.setattr(app_mod, '_discord_notify', lambda **kw: None)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **kw: None)
    return inserted


def _assert_valid_listing_row(table, row):
    assert table == 'listings'
    assert set(row) <= LISTING_COLUMNS, f"unknown columns: {set(row) - LISTING_COLUMNS}"
    assert row['crop_name'] and row['district']
    assert row['quantity_kg'] > 0 and row['price_per_kg'] > 0
    # min_order_kg is NOT NULL DEFAULT 0: explicitly 0, or omitted to use the default.
    assert row.get('min_order_kg', 0) == 0
    assert 'farmer_name' not in row and 'source' not in row


def test_web_listing_insert_matches_live_schema(client, monkeypatch):
    inserted = _fake_persistence(monkeypatch)
    monkeypatch.setattr(app_mod, 'verify_supabase_user', lambda: ('farmer-1', None))
    monkeypatch.setattr(app_mod, 'legal_gate', lambda _uid: None)

    r = client.post('/api/listings', json={
        'crop_name': 'Tomatoes', 'category': 'vegetables',
        'quantity_kg': 1000, 'price_per_kg': 1500, 'district': 'Kampala',
        'description': 'Freshly uprooted', 'sale_unit': 'per kg',
        'delivery_available': True, 'payment_terms': 'MoMo',
    })

    assert r.status_code == 201, r.get_data(as_text=True)
    _assert_valid_listing_row(*inserted[0])


def test_ussd_listing_insert_matches_live_schema(client, monkeypatch):
    inserted = _fake_persistence(monkeypatch)
    monkeypatch.setattr(app_mod, '_ussd_get',
                        lambda _sid: {'crop': 'Maize', 'qty': '500'})
    monkeypatch.setattr(app_mod, '_ussd_set', lambda *a, **kw: None)
    monkeypatch.setattr(app_mod, '_ussd_del', lambda *a, **kw: None)
    monkeypatch.setattr(app_mod, 'at_sms', None)

    r = client.post('/api/ussd', data={
        'sessionId': 'AT_TEST_SCHEMA', 'phoneNumber': '+256700000001',
        'networkCode': '63902', 'serviceCode': '*789#', 'text': '4*1*1*500*800',
    })

    assert r.status_code == 200
    assert 'END' in r.get_data(as_text=True)
    _assert_valid_listing_row(*inserted[0])
