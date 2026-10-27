# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Farmer settlements: exact journal arithmetic and the settle endpoint's
refusal to pay before the receipt exists or without proof of transfer."""
from __future__ import annotations

from decimal import Decimal

import pytest

import app as app_mod
from services import ledger

PAYOUT_ID = '11111111-1111-4111-8111-111111111111'
ORDER_ID = '22222222-2222-4222-8222-222222222222'


class Result:
    def __init__(self, ok, data=None, error='', status=200):
        self.ok, self.data, self.error, self.status = ok, data, error, status


class Db:
    def __init__(self, result=None):
        self.result = result or Result(True, data='journal-1')
        self.calls = []

    def rpc(self, fn, args):
        self.calls.append((fn, args))
        return self.result


class Rpc:
    def __init__(self, journal_ok=True, mark_result=True):
        self.journal_ok = journal_ok
        self.mark_result = mark_result
        self.calls = []

    def __call__(self, fn, args):
        self.calls.append((fn, args))
        if fn == 'post_journal':
            return (Result(True, data='j-settle') if self.journal_ok
                    else Result(False, error='ledger down'))
        if fn == 'mark_payout_paid':
            return Result(True, data=self.mark_result)
        return Result(False, error=f'unexpected rpc {fn}')


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


def _admin_headers():
    token = app_mod.make_token({'sub': 'admin', 'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def _payout(**extra):
    row = {
        'id': PAYOUT_ID,
        'order_id': ORDER_ID,
        'farmer_id': '33333333-3333-4333-8333-333333333333',
        'gross_amount': 100000,
        'commission_pct': 5,
        'commission_amount': 5000,
        'net_amount': 95000,
        'status': 'pending',
        'provider': None,
        'provider_ref': None,
        'created_at': '2026-10-20T10:00:00+00:00',
        'paid_at': None,
    }
    row.update(extra)
    return row


def _order(**extra):
    row = {
        'id': ORDER_ID,
        'status': 'delivered',
        'payment_status': 'paid',
        'payment_ref': 'AB-1',
        'total_amount': 100000,
        'total_price': 100000,
    }
    row.update(extra)
    return row


def _receipt(journal_id='j-rec', kind='customer_payment', ref='AB-1'):
    return {'id': journal_id, 'reference': f'payment:{ref}', 'kind': kind,
            'status': 'posted', 'payment_ref': ref}


def _receipt_lines(account='PROVIDER_CLEARING', journal_id='j-rec'):
    return [{'journal_id': journal_id, 'account_code': account, 'side': 'debit',
             'amount': 100000, 'memo': 'customer payment received'}]


def _reads(payouts=None, orders=None, journals=None, lines=None):
    table_rows = {
        'payouts': payouts if payouts is not None else [_payout()],
        'orders': orders if orders is not None else [_order()],
        'ledger_journals': journals if journals is not None else [_receipt()],
        'ledger_lines': lines if lines is not None else _receipt_lines(),
    }
    return lambda table, filters=None, limit=100: (table_rows.get(table, []), None)


# ── Journal arithmetic ───────────────────────────────────────────────────────
def _accounts(lines):
    return [(line['account'], line['side'], line['amount']) for line in lines]


def test_settlement_moves_payable_into_commission_and_clearing():
    lines, summary = ledger.build_settlement_lines(
        _payout(), channel_account=ledger.ACCOUNT_PROVIDER_CLEARING)
    assert _accounts(lines) == [
        ('FARMER_PAYABLE', 'debit', '100000.00'),
        ('COMMISSION_REVENUE', 'credit', '5000.00'),
        ('PROVIDER_CLEARING', 'credit', '95000.00'),
    ]
    assert summary['commission'] == Decimal('5000.00')
    assert summary['net'] == Decimal('95000.00')


def test_cod_settlement_clears_cash_held_on_delivery():
    lines, summary = ledger.build_settlement_lines(
        _payout(commission_pct=0, commission_amount=0, net_amount=100000),
        channel_account=ledger.ACCOUNT_COD_CASH_HELD)
    assert _accounts(lines) == [
        ('FARMER_PAYABLE', 'debit', '100000.00'),
        ('COD_CASH_HELD', 'credit', '100000.00'),
    ]
    assert summary['commission'] == Decimal('0.00')


def test_full_commission_is_a_two_line_balanced_journal():
    lines, _ = ledger.build_settlement_lines(
        _payout(commission_pct=100, commission_amount=100000, net_amount=0),
        channel_account=ledger.ACCOUNT_PROVIDER_CLEARING)
    assert _accounts(lines) == [
        ('FARMER_PAYABLE', 'debit', '100000.00'),
        ('COMMISSION_REVENUE', 'credit', '100000.00'),
    ]


def test_settlement_rejects_rows_that_do_not_add_up():
    with pytest.raises(ledger.LedgerError):
        ledger.build_settlement_lines(
            _payout(net_amount=90000), channel_account=ledger.ACCOUNT_PROVIDER_CLEARING)


def test_settlement_rejects_an_unknown_asset_account():
    with pytest.raises(ledger.LedgerError):
        ledger.build_settlement_lines(_payout(), channel_account='SUSPENSE_OVERPAYMENT')


def test_settlement_posts_without_the_checkout_payment_ref():
    db = Db()
    posting = ledger.post_settlement(
        db, payout=_payout(), channel_account=ledger.ACCOUNT_PROVIDER_CLEARING,
        provider='MTN MoMo', provider_ref='MP240517.1234')
    assert posting.ok and posting.journal_id == 'journal-1'
    args = db.calls[0][1]
    assert args['p_reference'] == f'settlement:{PAYOUT_ID}'
    assert args['p_kind'] == 'farmer_settlement'
    assert args['p_payment_ref'] is None
    assert args['p_metadata']['provider_ref'] == 'MP240517.1234'
    assert args['p_metadata']['channel_account'] == 'PROVIDER_CLEARING'
    assert args['p_metadata']['commission_policy'] == 'recognized_at_settlement'


def test_settlement_requires_a_provider_reference():
    db = Db()
    posting = ledger.post_settlement(
        db, payout=_payout(), channel_account=ledger.ACCOUNT_PROVIDER_CLEARING,
        provider='MTN MoMo', provider_ref='   ')
    assert not posting.ok and 'provider reference' in posting.error
    assert db.calls == []


# ── Endpoint guards ─────────────────────────────────────────────────────────
def test_settle_requires_admin(client):
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle', json={'provider_ref': 'X'})
    assert r.status_code == 401


def test_settle_rejects_a_non_uuid_payout(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())
    r = client.post('/api/admin/payouts/not-a-uuid/settle',
                    json={'provider_ref': 'X'}, headers=_admin_headers())
    assert r.status_code == 400


def test_settle_requires_the_provider_reference(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': '  '}, headers=_admin_headers())
    assert r.status_code == 400
    assert r.get_json()['code'] == 'provider_ref_required'


def test_settle_returns_404_for_an_unknown_payout(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(payouts=[]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 404


def test_settle_is_idempotent_for_an_already_paid_payout(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        payouts=[_payout(status='paid', provider_ref='MP-1', paid_at='2026-10-21T09:00:00Z')]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    body = r.get_json()
    assert r.status_code == 200 and body['already_settled'] is True
    assert body['provider_ref'] == 'MP-1'


def test_settle_refuses_a_non_pending_payout(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        payouts=[_payout(status='disputed')]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'payout_not_pending'


def test_settle_refuses_an_order_that_is_not_delivered_and_paid(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        orders=[_order(status='in_transit')]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'order_not_settleable'


def test_settle_refuses_a_payout_that_no_longer_matches_the_order(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        orders=[_order(total_amount=90000, total_price=90000)]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'payout_amount_mismatch'


def test_settle_refuses_when_the_customer_receipt_is_missing(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(journals=[]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'receipt_not_posted'


def test_settle_refuses_an_ambiguous_receipt(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        journals=[_receipt(), _receipt(journal_id='j-rec-2')]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'receipt_ambiguous'


def test_settle_refuses_an_unknown_receipt_channel(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        lines=[{'journal_id': 'j-rec', 'account_code': 'FARMER_PAYABLE',
                'side': 'credit', 'amount': 100000, 'memo': 'owed'}]))
    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 409
    assert r.get_json()['code'] == 'receipt_channel_unknown'


def test_settle_posts_the_journal_then_marks_the_payout_paid(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())
    rpc = Rpc()
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    audits = []
    monkeypatch.setattr(app_mod, 'supa_insert',
                        lambda table, data: audits.append((table, data)))
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)

    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1', 'provider': 'MTN MoMo'},
                    headers=_admin_headers())
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and body['journal_id'] == 'j-settle'
    assert body['net'] == '95000.00' and body['channel_account'] == 'PROVIDER_CLEARING'
    assert [fn for fn, _ in rpc.calls] == ['post_journal', 'mark_payout_paid']
    assert rpc.calls[1][1] == {'p_payout_id': PAYOUT_ID, 'p_provider': 'MTN MoMo',
                               'p_provider_ref': 'MP-1'}
    assert audits[0][0] == 'admin_audit_log'
    assert audits[0][1]['action'] == 'payout.settled'


def test_settle_uses_cod_cash_held_when_the_receipt_debited_it(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads(
        journals=[_receipt(kind='cod_collection')],
        lines=_receipt_lines(account='COD_CASH_HELD')))
    rpc = Rpc()
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    monkeypatch.setattr(app_mod, 'supa_insert', lambda *a, **k: None)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)

    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'CASH-1'}, headers=_admin_headers())
    assert r.status_code == 200
    assert r.get_json()['channel_account'] == 'COD_CASH_HELD'
    lines = rpc.calls[0][1]['p_lines']
    assert [line['account'] for line in lines] == [
        'FARMER_PAYABLE', 'COMMISSION_REVENUE', 'COD_CASH_HELD']


def test_settle_does_not_mark_paid_when_the_journal_fails(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())
    rpc = Rpc(journal_ok=False)
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    monkeypatch.setattr(app_mod, 'supa_insert', lambda *a, **k: None)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)

    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 503
    assert r.get_json()['code'] == 'settlement_posting_failed'
    assert [fn for fn, _ in rpc.calls] == ['post_journal']


def test_settle_reports_a_status_update_failure_as_retryable(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())

    def broken_mark(fn, args):
        if fn == 'post_journal':
            return Result(True, data='j-settle')
        return Result(False, error='rpc missing')

    monkeypatch.setattr(app_mod.SUPA, 'rpc', broken_mark)
    monkeypatch.setattr(app_mod, 'supa_insert', lambda *a, **k: None)
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)

    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 503
    body = r.get_json()
    assert body['code'] == 'payout_status_update_failed' and body['retryable'] is True


def test_settle_treats_a_concurrent_settlement_as_done(client, monkeypatch):
    monkeypatch.setattr(app_mod, 'supa_get_checked', _reads())
    rpc = Rpc(mark_result=False)
    monkeypatch.setattr(app_mod.SUPA, 'rpc', rpc)
    audits = []
    monkeypatch.setattr(app_mod, 'supa_insert',
                        lambda table, data: audits.append((table, data)))
    monkeypatch.setattr(app_mod, '_log_system_event', lambda *a, **k: None)

    r = client.post(f'/api/admin/payouts/{PAYOUT_ID}/settle',
                    json={'provider_ref': 'MP-1'}, headers=_admin_headers())
    assert r.status_code == 200
    assert r.get_json()['already_settled'] is True
    assert audits == []
