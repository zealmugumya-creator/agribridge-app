# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Customer-payment posting: exact arithmetic, balanced lines, idempotent RPC."""
from __future__ import annotations

from decimal import Decimal

import pytest

from services import ledger


class Result:
    def __init__(self, ok, data=None, error="", status=200):
        self.ok, self.data, self.error, self.status = ok, data, error, status


class Db:
    def __init__(self, result=None, exc=None):
        self.result = result or Result(True, data="journal-1")
        self.exc = exc
        self.calls = []

    def rpc(self, fn, args):
        self.calls.append((fn, args))
        if self.exc:
            raise self.exc
        return self.result


def _order(order_id, total, status="pending", **extra):
    row = {"id": order_id, "total_amount": total, "total_price": total, "status": status}
    row.update(extra)
    return row


def _accounts(lines):
    return [(line["account"], line["side"], line["amount"]) for line in lines]


def test_full_payment_is_a_transfer_from_clearing_to_farmer_payable():
    lines, summary = ledger.build_payment_lines([_order("o1", 50000)], 50000)
    assert _accounts(lines) == [
        ("PROVIDER_CLEARING", "debit", "50000.00"),
        ("FARMER_PAYABLE", "credit", "50000.00"),
    ]
    assert summary["orders_total"] == Decimal("50000.00")
    assert summary["suspense"] == Decimal("0.00")
    assert summary["order_ids"] == ["o1"]


def test_cancelled_order_money_is_a_customer_refund_not_a_farmer_payable():
    lines, summary = ledger.build_payment_lines(
        [_order("o1", 60000), _order("o2", 20000, status="cancelled")], 80000)
    assert _accounts(lines) == [
        ("PROVIDER_CLEARING", "debit", "80000.00"),
        ("FARMER_PAYABLE", "credit", "60000.00"),
        ("CUSTOMER_REFUND_PAYABLE", "credit", "20000.00"),
    ]
    assert summary["cancelled_order_ids"] == ["o2"]
    assert summary["refund_payable"] == Decimal("20000.00")


def test_overpayment_is_held_in_suspense_and_the_journal_still_balances():
    lines, summary = ledger.build_payment_lines([_order("o1", 50000)], 55000)
    assert _accounts(lines) == [
        ("PROVIDER_CLEARING", "debit", "55000.00"),
        ("FARMER_PAYABLE", "credit", "50000.00"),
        ("SUSPENSE_OVERPAYMENT", "credit", "5000.00"),
    ]
    assert summary["suspense"] == Decimal("5000.00")


def test_cod_collection_debits_cash_held_instead_of_provider_clearing():
    lines, _ = ledger.build_payment_lines(
        [_order("o1", 50000)], 50000,
        debit_account=ledger.ACCOUNT_COD_CASH_HELD,
        debit_memo="cash collected on delivery")
    assert _accounts(lines)[0] == ("COD_CASH_HELD", "debit", "50000.00")
    assert lines[0]["memo"] == "cash collected on delivery"


def test_cod_post_uses_its_own_reference_and_kind():
    db = Db()
    posting = ledger.post_verified_payment(
        db, tx_ref="AB-1", orders=[_order("o1", 50000)], received=50000,
        provider="cash", reference_prefix="cod", kind="cod_collection",
        debit_account=ledger.ACCOUNT_COD_CASH_HELD,
        description="Cash on delivery AB-1")
    assert posting.ok
    args = db.calls[0][1]
    assert args["p_reference"] == "cod:AB-1" and args["p_kind"] == "cod_collection"
    assert args["p_metadata"]["debit_account"] == "COD_CASH_HELD"
    assert args["p_description"] == "Cash on delivery AB-1"


def test_decimal_arithmetic_has_no_float_drift():
    lines, summary = ledger.build_payment_lines(
        [_order("o1", 10000.10), _order("o2", 20000.20)], 30000.30)
    assert summary["orders_total"] == Decimal("30000.30")
    assert summary["suspense"] == Decimal("0.00")
    assert sum(Decimal(line["amount"]) for line in lines if line["side"] == "debit") == \
        sum(Decimal(line["amount"]) for line in lines if line["side"] == "credit")


def test_generated_total_beats_a_legacy_client_total():
    row = {"id": "o1", "total_amount": 50000, "total_price": 99999, "status": "pending"}
    lines, summary = ledger.build_payment_lines([row], 50000)
    assert summary["orders_total"] == Decimal("50000.00")
    assert ("FARMER_PAYABLE", "credit", "50000.00") in _accounts(lines)


def test_missing_totals_fall_back_to_quantity_times_price():
    row = {"id": "o1", "quantity_kg": 2, "price_per_kg": 1500, "status": "pending"}
    _, summary = ledger.build_payment_lines([row], 3000)
    assert summary["orders_total"] == Decimal("3000.00")


def test_disputed_order_remains_a_farmer_payable_until_reversed():
    lines, _ = ledger.build_payment_lines([_order("o1", 1000, status="disputed")], 1000)
    assert ("FARMER_PAYABLE", "credit", "1000.00") in _accounts(lines)


@pytest.mark.parametrize("received,message", [
    (49999.99, "is less than order total"),
    (0, "received amount must be positive"),
    (-5, "received amount must be positive"),
    ("abc", "invalid amount"),
    (None, "amount is required"),
])
def test_unusable_receipts_are_rejected(received, message):
    with pytest.raises(ledger.LedgerError, match=message):
        ledger.build_payment_lines([_order("o1", 50000)], received)


def test_checkout_without_a_positive_total_is_rejected():
    with pytest.raises(ledger.LedgerError, match="no positive order total"):
        ledger.build_payment_lines([_order("o1", 0)], 100)


def test_non_ugx_currency_is_rejected():
    with pytest.raises(ledger.LedgerError, match="unsupported ledger currency"):
        ledger.build_payment_lines([_order("o1", 100)], 100, currency="USD")


def test_successful_post_uses_the_stable_reference_and_audit_metadata():
    db = Db()
    posting = ledger.post_verified_payment(
        db, tx_ref="AB-1", orders=[_order("o1", 50000)],
        received=Decimal("50000.00"), currency="UGX", provider="Flutterwave card",
        flw_id=55)

    assert posting.ok and posting.journal_id == "journal-1"
    fn, args = db.calls[0]
    assert fn == "post_journal"
    assert args["p_reference"] == "payment:AB-1"
    assert args["p_kind"] == "customer_payment"
    assert args["p_payment_ref"] == "AB-1"
    assert args["p_actor"] is None
    assert args["p_currency"] == "UGX"
    assert args["p_lines"] == [
        {"account": "PROVIDER_CLEARING", "side": "debit",
         "amount": "50000.00", "memo": "customer payment received"},
        {"account": "FARMER_PAYABLE", "side": "credit",
         "amount": "50000.00", "memo": "product value owed to farmers"},
    ]
    metadata = args["p_metadata"]
    assert metadata["provider"] == "Flutterwave card"
    assert metadata["flw_id"] == "55"
    assert metadata["received"] == "50000.00"
    assert metadata["order_ids"] == ["o1"]
    assert metadata["commission_policy"] == "recognized_at_settlement"


def test_rpc_failure_is_returned_not_raised():
    db = Db(result=Result(False, error="unknown account", status=400))
    posting = ledger.post_verified_payment(
        db, tx_ref="AB-1", orders=[_order("o1", 100)], received=100)
    assert posting.ok is False
    assert posting.error == "unknown account"
    assert posting.lines


def test_rpc_success_without_a_journal_id_is_a_failure():
    db = Db(result=Result(True, data=None))
    posting = ledger.post_verified_payment(
        db, tx_ref="AB-1", orders=[_order("o1", 100)], received=100)
    assert posting.ok is False
    assert "no journal id" in posting.error


def test_transport_exception_is_returned_not_raised():
    db = Db(exc=RuntimeError("boom"))
    posting = ledger.post_verified_payment(
        db, tx_ref="AB-1", orders=[_order("o1", 100)], received=100)
    assert posting.ok is False
    assert posting.error == "RuntimeError"


@pytest.mark.parametrize("tx_ref,orders,received", [
    ("", [_order("o1", 100)], 100),
    ("AB-1", [_order("o1", 100)], 99),
    ("AB-1", [], 100),
])
def test_invalid_inputs_never_reach_the_database(tx_ref, orders, received):
    db = Db()
    posting = ledger.post_verified_payment(
        db, tx_ref=tx_ref, orders=orders, received=received)
    assert posting.ok is False
    assert db.calls == []
