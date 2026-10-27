# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Financial Control Center read models and delivery-flow schema fixes.

The ledger is not applied to every environment, so these tests pin two things:
real arithmetic over fetched rows, and an explicit "not initialized" response
instead of a dashboard full of invented zeros.
"""
from __future__ import annotations

import pytest

import app as app_mod


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


def _admin_headers():
    token = app_mod.make_token({'sub': 'admin', 'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def _fake_reads(payloads, errors=None):
    errors = errors or {}

    def read(table, filters=None, limit=100):
        if table in errors:
            return None, errors[table]
        return payloads.get(table, []), None

    return read


def _order(order_id, total, status='pending', payment='paid', ref='', created=None):
    return {
        'id': order_id,
        'total_amount': total,
        'total_price': total,
        'payment_status': payment,
        'payment_ref': ref,
        'status': status,
        'created_at': created or '2026-10-20T10:00:00+00:00',
    }


def _payout(payout_id, net, status, commission=1000, order_id='o1',
            farmer_id='f1', created='2026-10-20T11:00:00+00:00'):
    return {
        'id': payout_id,
        'order_id': order_id,
        'farmer_id': farmer_id,
        'gross_amount': net + commission,
        'commission_pct': 5,
        'commission_amount': commission,
        'net_amount': net,
        'status': status,
        'created_at': created,
    }


# ── Auth ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('path', [
    '/api/admin/finance/overview',
    '/api/admin/finance/journals',
    '/api/admin/finance/farmer-settlements',
    '/api/admin/finance/transporter-settlements',
    '/api/admin/finance/reconciliation',
    '/api/admin/finance/reports',
])
def test_finance_endpoints_require_admin(client, path):
    assert client.get(path).status_code == 401


# ── Overview ─────────────────────────────────────────────────────────────────
def test_overview_reports_unavailable_ledger_instead_of_zero(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'orders': [
            _order('o1', 100000, status='delivered', payment='paid'),
            _order('o2', 50000, payment='refunded'),
        ],
        'payouts': [
            _payout('p1', 90000, 'pending', commission=10000),
            _payout('p2', 45000, 'paid', commission=5000, order_id='o2'),
        ],
    }, errors={'ledger_account_balances': 'PGRST205: table not found'}))

    r = client.get('/api/admin/finance/overview', headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()

    assert body['customer_payments']['received'] == 100000
    assert body['customer_payments']['count'] == 1
    assert body['farmer_payables']['outstanding'] == 90000
    assert body['commission_revenue']['recognized'] == 5000
    assert body['commission_revenue']['pending'] == 10000
    assert body['refunds']['amount'] == 50000
    assert body['transporter_payables']['available'] is False
    assert body['transporter_payables']['outstanding'] is None
    assert body['delivery_revenue']['available'] is False
    assert body['ledger']['ready'] is False
    assert body['ledger']['balanced'] is None
    assert body['ledger']['error'].startswith('PGRST205')


def test_overview_marks_balanced_ledger(client, monkeypatch):
    balances = [
        {'code': 'PROVIDER_CLEARING', 'name': 'Clearing', 'account_type': 'asset',
         'normal_side': 'debit', 'total_debits': 100, 'total_credits': 0, 'balance': 100},
        {'code': 'FARMER_PAYABLE', 'name': 'Farmer payable', 'account_type': 'liability',
         'normal_side': 'credit', 'total_debits': 0, 'total_credits': 100, 'balance': 100},
    ]
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'orders': [], 'payouts': [], 'ledger_account_balances': balances,
    }))
    r = client.get('/api/admin/finance/overview', headers=_admin_headers())
    assert r.status_code == 200
    ledger = r.get_json()['ledger']
    assert ledger['ready'] is True
    assert ledger['balanced'] is True
    assert ledger['total_debits'] == ledger['total_credits'] == 100


# ── Journals ─────────────────────────────────────────────────────────────────
def test_journals_returns_actionable_error_when_ledger_missing(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads(
        {}, errors={'ledger_journals': 'PGRST205: Could not find the table'}))
    r = client.get('/api/admin/finance/journals', headers=_admin_headers())
    assert r.status_code == 503
    body = r.get_json()
    assert body['code'] == 'ledger_not_initialized'
    assert '0013_ledger_core.up.sql' in body['fix']


def test_journals_groups_lines_and_totals(client, monkeypatch):
    journals = [{
        'id': 'j1', 'reference': 'payment:P1', 'kind': 'customer_payment',
        'currency': 'UGX', 'payment_ref': 'P1', 'description': 'Payment',
        'status': 'posted', 'posted_at': '2026-10-20T10:00:00+00:00',
    }]
    lines = [
        {'journal_id': 'j1', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit',
         'amount': 100000, 'memo': None},
        {'journal_id': 'j1', 'account_code': 'FARMER_PAYABLE', 'side': 'credit',
         'amount': 90000, 'memo': None},
        {'journal_id': 'j1', 'account_code': 'COMMISSION_REVENUE', 'side': 'credit',
         'amount': 10000, 'memo': None},
    ]
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'ledger_journals': journals, 'ledger_lines': lines,
    }))
    r = client.get('/api/admin/finance/journals', headers=_admin_headers())
    assert r.status_code == 200
    row = r.get_json()['rows'][0]
    assert row['amount'] == 100000
    assert len(row['lines']) == 3


# ── Settlements ──────────────────────────────────────────────────────────────
def test_farmer_settlements_join_names_and_net(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'payouts': [_payout('p1', 90000, 'pending')],
        'orders': [_order('o1', 100000, status='delivered')],
        'farmers': [{'id': 'f1', 'full_name': 'Nabuufu Susan'}],
    }))
    r = client.get('/api/admin/finance/farmer-settlements', headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body['rows'][0]['farmer_name'] == 'Nabuufu Susan'
    assert body['rows'][0]['net_amount'] == 90000
    assert body['summary']['outstanding'] == 90000
    assert body['summary']['completed_sales_count'] == 1


def test_transporter_settlements_never_invents_a_charge(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'deliveries': [{
            'id': 'd1', 'order_id': 'o1', 'driver_name': 'Driver One',
            'driver_phone': '+256700000000', 'status': 'delivered',
            'actual_arrival': '2026-10-20T12:00:00+00:00', 'tracking_code': 'ABC',
        }],
    }))
    r = client.get('/api/admin/finance/transporter-settlements',
                   headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body['available'] is False
    assert body['summary']['payable_total'] is None
    assert body['summary']['delivered_count'] == 1
    assert body['rows'][0]['agreed_charge'] is None
    assert body['rows'][0]['payable_status'] == 'not_captured'


# ── Reconciliation ───────────────────────────────────────────────────────────
def test_reconciliation_finds_missing_mismatch_and_orphans(client, monkeypatch):
    orders = [
        _order('o-ok', 100000, ref='P-OK'),
        _order('o-missing', 50000, ref='P-MISSING'),
        _order('o-mismatch', 40000, ref='P-MISMATCH'),
        _order('o-dupe', 30000, ref='P-DUPE'),
        _order('o-refund', 20000, payment='refunded', ref='P-REFUND'),
    ]
    journals = [
        {'id': 'j-ok', 'kind': 'customer_payment', 'payment_ref': 'P-OK',
         'status': 'posted', 'reference': 'payment:P-OK'},
        {'id': 'j-mm', 'kind': 'customer_payment', 'payment_ref': 'P-MISMATCH',
         'status': 'posted', 'reference': 'payment:P-MISMATCH'},
        {'id': 'j-d1', 'kind': 'customer_payment', 'payment_ref': 'P-DUPE',
         'status': 'posted', 'reference': 'payment:P-DUPE:1'},
        {'id': 'j-d2', 'kind': 'customer_payment', 'payment_ref': 'P-DUPE',
         'status': 'posted', 'reference': 'payment:P-DUPE:2'},
        {'id': 'j-orphan', 'kind': 'customer_payment', 'payment_ref': 'P-ORPHAN',
         'status': 'posted', 'reference': 'payment:P-ORPHAN'},
    ]
    lines = [
        {'journal_id': 'j-ok', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit', 'amount': 100000},
        {'journal_id': 'j-ok', 'account_code': 'FARMER_PAYABLE', 'side': 'credit', 'amount': 100000},
        {'journal_id': 'j-mm', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit', 'amount': 35000},
        {'journal_id': 'j-mm', 'account_code': 'FARMER_PAYABLE', 'side': 'credit', 'amount': 35000},
        {'journal_id': 'j-d1', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit', 'amount': 15000},
        {'journal_id': 'j-d1', 'account_code': 'FARMER_PAYABLE', 'side': 'credit', 'amount': 15000},
        {'journal_id': 'j-d2', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit', 'amount': 15000},
        {'journal_id': 'j-d2', 'account_code': 'FARMER_PAYABLE', 'side': 'credit', 'amount': 15000},
        {'journal_id': 'j-orphan', 'account_code': 'PROVIDER_CLEARING', 'side': 'debit', 'amount': 9999},
        {'journal_id': 'j-orphan', 'account_code': 'FARMER_PAYABLE', 'side': 'credit', 'amount': 9999},
    ]
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'orders': orders, 'ledger_journals': journals, 'ledger_lines': lines,
    }))
    r = client.get('/api/admin/finance/reconciliation', headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    types = {issue['type'] for issue in body['issues']}
    assert 'missing_ledger_entry' in types
    assert 'amount_mismatch' in types
    assert 'duplicate_ledger_posting' in types
    assert 'ledger_payment_without_paid_order' in types
    assert 'refund_without_reversal' in types
    assert body['summary']['errors'] == 3


# ── Reports ──────────────────────────────────────────────────────────────────
def test_reports_filter_by_range_and_degrade_without_ledger(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'orders': [
            _order('o-in', 100000, status='delivered', created='2026-10-20T09:00:00Z'),
            _order('o-out', 70000, created='2026-09-01T09:00:00Z'),
        ],
        'payouts': [_payout('p1', 90000, 'paid', created='2026-10-20T10:00:00Z')],
    }, errors={'ledger_journals': 'PGRST205: table not found'}))
    r = client.get('/api/admin/finance/reports?from=2026-10-01&to=2026-10-31',
                   headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body['orders']['count'] == 1
    assert body['orders']['paid_value'] == 100000
    assert body['settlements']['commission'] == 1000
    assert body['ledger']['ready'] is False
    assert body['warnings']


def test_reports_reject_inverted_range(client):
    r = client.get('/api/admin/finance/reports?from=2026-10-31&to=2026-10-01',
                   headers=_admin_headers())
    assert r.status_code == 400


# ── Delivery route schema fixes ──────────────────────────────────────────────
def _stub_notifications(monkeypatch):
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **kw: None)
    monkeypatch.setattr(app_mod, '_discord_notify', lambda *a, **kw: None)
    monkeypatch.setattr(app_mod.NOTIFY, 'enqueue', lambda **kw: None)


def test_assign_transporter_writes_real_delivery_columns(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = {'insert': [], 'update': []}
    transporter = {'id': 't1', 'full_name': 'Driver One', 'phone': '+256700000000'}

    def fake_get(table, filters=None, limit=100):
        if table == 'transporters':
            return [transporter]
        if table == 'deliveries':
            return []
        return []

    monkeypatch.setattr(app_mod, 'supa_get', fake_get)
    monkeypatch.setattr(app_mod, 'supa_insert',
                        lambda table, data: calls['insert'].append((table, data)) or [data])
    monkeypatch.setattr(app_mod, 'supa_update',
                        lambda table, data, eq_col, eq_val: calls['update'].append(
                            (table, data, eq_col, eq_val)) or True)

    r = client.post('/api/admin/orders/o1/assign-transporter',
                    json={'transporter_id': 't1'}, headers=_admin_headers())
    assert r.status_code == 200

    delivery = calls['insert'][0][1]
    assert delivery['driver_name'] == 'Driver One'
    assert delivery['driver_phone'] == '+256700000000'
    assert delivery['status'] == 'scheduled'
    assert 'transporter_id' not in delivery
    assert any(c[0] == 'orders' and c[1].get('status') == 'in_transit'
               for c in calls['update'])


def test_assign_transporter_failure_is_not_reported_as_success(client, monkeypatch):
    _stub_notifications(monkeypatch)
    updates = []
    monkeypatch.setattr(app_mod, 'supa_get', lambda table, filters=None, limit=100: (
        [{'id': 't1', 'full_name': 'Driver One'}] if table == 'transporters'
        else [] if table == 'deliveries' else []))
    monkeypatch.setattr(app_mod, 'supa_insert', lambda table, data: None)
    monkeypatch.setattr(app_mod, 'supa_update',
                        lambda *a, **kw: updates.append(a) or True)

    r = client.post('/api/admin/orders/o1/assign-transporter',
                    json={'transporter_id': 't1'}, headers=_admin_headers())
    assert r.status_code == 503
    assert updates == []


def _delivery_order(payment='paid', method='cash', total=50000, ref='AB-1'):
    return {'id': 'o1', 'payment_ref': ref, 'payment_method': method,
            'payment_status': payment, 'status': 'in_transit',
            'total_amount': total, 'total_price': total,
            'quantity_kg': 1, 'price_per_kg': total}


def _delivery_stubs(monkeypatch, order, rpc_ok=True):
    from services.supabase_client import RpcResult
    calls = {'update': [], 'rpc': [], 'ledger': []}

    def fake_get(table, filters=None, limit=100):
        return [order] if table == 'orders' else []

    def fake_update(table, data, eq_col, eq_val):
        calls['update'].append((table, data, eq_col, eq_val))
        return True

    def fake_rpc(fn, args):
        calls['rpc'].append((fn, args))
        return RpcResult(ok=rpc_ok, data=True, status=200 if rpc_ok else 400,
                         error='' if rpc_ok else 'illegal order transition')

    monkeypatch.setattr(app_mod, 'supa_get', fake_get)
    monkeypatch.setattr(app_mod, 'supa_update', fake_update)
    monkeypatch.setattr(app_mod.SUPA, 'rpc', fake_rpc)
    return calls


def test_confirm_delivery_uses_actual_arrival_column(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = _delivery_stubs(monkeypatch, _delivery_order())

    r = client.post('/api/admin/orders/o1/confirm-delivery',
                    json={'note': 'handed over'}, headers=_admin_headers())
    assert r.status_code == 200

    delivery_update = [c for c in calls['update'] if c[0] == 'deliveries'][0]
    assert 'actual_arrival' in delivery_update[1]
    assert 'delivered_at' not in delivery_update[1]
    assert delivery_update[1]['status'] == 'delivered'
    assert calls['rpc'] == [('set_order_status',
                             {'p_order_id': 'o1', 'p_new_status': 'delivered'})]
    assert [c for c in calls['update'] if c[0] == 'orders'] == []


def test_confirm_delivery_failure_is_reported(client, monkeypatch):
    _stub_notifications(monkeypatch)
    _delivery_stubs(monkeypatch, _delivery_order())

    def fake_update(table, data, eq_col, eq_val):
        return table == 'orders'

    monkeypatch.setattr(app_mod, 'supa_update', fake_update)
    r = client.post('/api/admin/orders/o1/confirm-delivery',
                    json={}, headers=_admin_headers())
    assert r.status_code == 503
    assert r.get_json()['code'] == 'delivery_confirmation_failed'


def test_confirm_delivery_rejects_an_illegal_state_transition(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = _delivery_stubs(monkeypatch, _delivery_order(), rpc_ok=False)
    r = client.post('/api/admin/orders/o1/confirm-delivery',
                    json={}, headers=_admin_headers())
    assert r.status_code == 409
    assert calls['update'] == []


def test_cod_delivery_posts_the_ledger_and_marks_paid(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = _delivery_stubs(monkeypatch, _delivery_order(payment='unpaid'))
    from services.ledger import PaymentPosting

    def fake_post(db, **kwargs):
        calls['ledger'].append(kwargs)
        return PaymentPosting(ok=True, reference='cod:AB-1', journal_id='j-cod')

    monkeypatch.setattr(app_mod.ledger, 'post_verified_payment', fake_post)
    r = client.post('/api/admin/orders/o1/confirm-delivery', json={},
                    headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body['payment_status'] == 'paid' and body['ledger_journal_id'] == 'j-cod'
    post = calls['ledger'][0]
    assert post['tx_ref'] == 'AB-1' and post['received'] == 50000
    assert post['reference_prefix'] == 'cod' and post['kind'] == 'cod_collection'
    assert post['debit_account'] == 'COD_CASH_HELD'
    assert post.get('actor') is None
    paid_update = [c for c in calls['update'] if c[0] == 'orders'][0]
    assert paid_update[1] == {'payment_status': 'paid'}
    assert paid_update[2:] == ('payment_ref', 'AB-1')


def test_cod_ledger_failure_does_not_mark_paid(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = _delivery_stubs(monkeypatch, _delivery_order(payment='unpaid'))
    from services.ledger import PaymentPosting
    monkeypatch.setattr(app_mod.ledger, 'post_verified_payment', lambda db, **kw:
                        PaymentPosting(ok=False, reference='cod:AB-1', error='db down'))
    r = client.post('/api/admin/orders/o1/confirm-delivery', json={},
                    headers=_admin_headers())
    assert r.status_code == 503 and r.get_json()['code'] == 'ledger_post_failed'
    assert [c for c in calls['update'] if c[0] == 'orders'] == []


def test_unpaid_online_delivery_is_not_marked_paid(client, monkeypatch):
    _stub_notifications(monkeypatch)
    calls = _delivery_stubs(monkeypatch, _delivery_order(payment='unpaid', method='momo'))
    monkeypatch.setattr(app_mod.ledger, 'post_verified_payment',
                        lambda *a, **k: pytest.fail('no ledger post for unverified payment'))
    r = client.post('/api/admin/orders/o1/confirm-delivery', json={},
                    headers=_admin_headers())
    assert r.status_code == 200
    assert r.get_json()['payment_status'] == 'unpaid'
    assert [c for c in calls['update'] if c[0] == 'orders'] == []
    assert calls['ledger'] == []


# ── Recurring (Pesapal) finance view ───────────────────────────────────────
def test_recurring_view_explains_an_unapplied_migration(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads(
        {}, {'payment_subscriptions': 'PGRST205: relation "payment_subscriptions" does not exist'}))
    r = client.get('/api/admin/finance/recurring', headers=_admin_headers())
    assert r.status_code == 503
    j = r.get_json()
    assert j['code'] == 'recurring_not_initialized'
    assert '0017_pesapal_payments' in j['fix']


def test_recurring_view_counts_only_paid_charges_and_flags_missing_ledger(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _fake_reads({
        'payment_subscriptions': [{'status': 'active'}, {'status': 'cancelled'}],
        'subscription_charges': [
            {'status': 'paid', 'amount': 25000, 'ledger_reference': 'recurring:t1'},
            {'status': 'paid', 'amount': 15000, 'ledger_reference': None},
            {'status': 'failed', 'amount': -1, 'ledger_reference': None},
            {'status': 'pending', 'amount': 30000, 'ledger_reference': None},
        ],
    }))
    r = client.get('/api/admin/finance/recurring', headers=_admin_headers())
    assert r.status_code == 200
    s = r.get_json()['summary']
    assert s == {
        'subscriptions': 2,
        'active_subscriptions': 1,
        'charges': 4,
        'paid_charges': 2,
        'total_charged': 40000.0,
        'paid_amount_without_ledger': 15000.0,
    }
