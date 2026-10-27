# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Post verified customer payments into the double-entry ledger.

Accounting policy for a receipt:

  debit  PROVIDER_CLEARING          money received from the provider
  credit FARMER_PAYABLE             product value of live orders
  credit CUSTOMER_REFUND_PAYABLE    product value of cancelled orders in the batch
  credit SUSPENSE_OVERPAYMENT       anything received above the order total

Cash collected on delivery posts the same credits, but debits COD_CASH_HELD
instead of PROVIDER_CLEARING and uses a `cod:` reference so it cannot collide
with a gateway receipt for the same checkout.

Commission is deliberately NOT recognised here. `platform_config.commission_pct`
applies to future payouts and no payout exists at receipt time. The settlement
entry (`settlement:<payout_id>`) moves the gross out of FARMER_PAYABLE, credits
COMMISSION_REVENUE and clears the same asset account the receipt debited.
Recognising it twice would overstate revenue.

Settlement journals intentionally carry no `payment_ref`: reconciliation groups
active journals by that column, so reusing the checkout ref would look like a
duplicate receipt.

Everything up to the RPC call is pure arithmetic so the money rules can be
tested offline. The database enforces balance, append-only and idempotency
(`payment:<tx_ref>`), so a retried webhook cannot post the same receipt twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from services import finance
from services.logging import get_logger

log = get_logger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
UGX = "UGX"

ACCOUNT_PROVIDER_CLEARING = "PROVIDER_CLEARING"
ACCOUNT_COD_CASH_HELD = "COD_CASH_HELD"
ACCOUNT_FARMER_PAYABLE = "FARMER_PAYABLE"
ACCOUNT_CUSTOMER_REFUND_PAYABLE = "CUSTOMER_REFUND_PAYABLE"
ACCOUNT_SUSPENSE_OVERPAYMENT = "SUSPENSE_OVERPAYMENT"
ACCOUNT_COMMISSION_REVENUE = "COMMISSION_REVENUE"

SETTLEMENT_KIND = "farmer_settlement"
SETTLEMENT_ASSET_ACCOUNTS = (ACCOUNT_PROVIDER_CLEARING, ACCOUNT_COD_CASH_HELD)

CANCELLED_STATUSES = {"cancelled"}


class LedgerError(ValueError):
    """Raised when a receipt cannot be expressed as a balanced journal."""


@dataclass
class PaymentPosting:
    ok: bool
    reference: str
    journal_id: str | None = None
    error: str = ""
    lines: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def money(value) -> Decimal:
    """Return an exact 2-decimal amount.

    Floats are converted through their string form so 0.1 + 0.2 style drift
    never reaches a journal. Non-finite and non-numeric input is rejected.
    """
    if value is None or value == "":
        raise LedgerError("amount is required")
    if isinstance(value, bool):
        raise LedgerError(f"invalid amount: {value!r}")
    try:
        if isinstance(value, float):
            if not math.isfinite(value):
                raise LedgerError(f"invalid amount: {value!r}")
            amount = Decimal(str(value))
        else:
            amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError(f"invalid amount: {value!r}") from None
    if not amount.is_finite():
        raise LedgerError(f"invalid amount: {value!r}")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def whole(value) -> int:
    """Round an amount to the nearest whole currency unit."""
    return int(money(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _line(account: str, side: str, amount: Decimal, memo: str) -> dict:
    return {"account": account, "side": side, "amount": _fmt(amount), "memo": memo}


def build_payment_lines(orders: list[dict], received, currency: str = UGX, *,
                        debit_account: str = ACCOUNT_PROVIDER_CLEARING,
                        debit_memo: str = "customer payment received"
                        ) -> tuple[list[dict], dict]:
    """Build the journal lines for one verified receipt.

    `orders` are the order rows sharing the checkout's payment_ref. Cancelled
    orders are still part of the money received, but that value is owed back to
    the customer rather than to the farmer.

    `debit_account` names where the money landed: provider clearing for gateway
    payments, COD_CASH_HELD for cash collected on delivery.
    """
    cur = str(currency or "").strip().upper()
    if cur != UGX:
        raise LedgerError(f"unsupported ledger currency: {currency!r}")
    if not str(debit_account or "").strip():
        raise LedgerError("a debit account is required")

    received_amount = money(received)
    if received_amount <= ZERO:
        raise LedgerError("received amount must be positive")

    farmer_total = ZERO
    refund_total = ZERO
    order_ids: list[str] = []
    cancelled_ids: list[str] = []

    for order in orders or []:
        if not isinstance(order, dict):
            raise LedgerError("order rows must be objects")
        total = money(finance.order_total(order))
        if total < ZERO:
            raise LedgerError("order total cannot be negative")
        if total == ZERO:
            continue
        order_id = str(order.get("id") or "")
        if order_id:
            order_ids.append(order_id)
        if str(order.get("status") or "").strip().lower() in CANCELLED_STATUSES:
            refund_total += total
            if order_id:
                cancelled_ids.append(order_id)
        else:
            farmer_total += total

    orders_total = farmer_total + refund_total
    if orders_total <= ZERO:
        raise LedgerError("checkout has no positive order total")
    if received_amount < orders_total:
        raise LedgerError(
            f"received {_fmt(received_amount)} is less than order total {_fmt(orders_total)}"
        )
    suspense = received_amount - orders_total

    lines = [_line(debit_account, "debit", received_amount, debit_memo)]
    if farmer_total > ZERO:
        lines.append(_line(ACCOUNT_FARMER_PAYABLE, "credit", farmer_total,
                           "product value owed to farmers"))
    if refund_total > ZERO:
        lines.append(_line(ACCOUNT_CUSTOMER_REFUND_PAYABLE, "credit", refund_total,
                           "cancelled order refund due"))
    if suspense > ZERO:
        lines.append(_line(ACCOUNT_SUSPENSE_OVERPAYMENT, "credit", suspense,
                           "received above order total"))

    debits = sum((money(line["amount"]) for line in lines if line["side"] == "debit"), ZERO)
    credits = sum((money(line["amount"]) for line in lines if line["side"] == "credit"), ZERO)
    if debits != credits or len(lines) < 2:
        raise LedgerError("journal does not balance")

    summary = {
        "currency": cur,
        "received": received_amount,
        "orders_total": orders_total,
        "farmer_payable": farmer_total,
        "refund_payable": refund_total,
        "suspense": suspense,
        "order_ids": order_ids,
        "cancelled_order_ids": cancelled_ids,
        "order_count": len(order_ids),
    }
    return lines, summary


def post_verified_payment(db, *, tx_ref: str, orders: list[dict], received,
                          currency: str = UGX, provider: str = "",
                          flw_id=None, actor=None, reference_prefix: str = "payment",
                          kind: str = "customer_payment", debit_account: str = ACCOUNT_PROVIDER_CLEARING,
                          debit_memo: str = "customer payment received",
                          description: str | None = None) -> PaymentPosting:
    """Post one idempotent receipt journal through `post_journal`.

    `db` is a SupabaseClient-like object with `.rpc(name, args)`. The reference
    is derived from the checkout ref plus a prefix, so retries return the
    original journal. COD collection uses prefix/reference `cod` so a gateway
    receipt and a cash collection for the same checkout cannot collide.
    """
    tx_ref = str(tx_ref or "").strip()
    prefix = str(reference_prefix or "").strip()
    reference = f"{prefix}:{tx_ref}"
    if not tx_ref or len(tx_ref) > 160 or not prefix:
        return PaymentPosting(ok=False, reference=reference,
                              error="a valid payment reference is required")

    try:
        lines, summary = build_payment_lines(
            orders, received, currency, debit_account=debit_account,
            debit_memo=debit_memo)
    except LedgerError as exc:
        return PaymentPosting(ok=False, reference=reference, error=str(exc))

    metadata = {
        "provider": str(provider or "")[:80],
        "flw_id": None if flw_id is None else str(flw_id)[:80],
        "currency": summary["currency"],
        "debit_account": debit_account,
        "received": _fmt(summary["received"]),
        "orders_total": _fmt(summary["orders_total"]),
        "farmer_payable": _fmt(summary["farmer_payable"]),
        "refund_payable": _fmt(summary["refund_payable"]),
        "suspense": _fmt(summary["suspense"]),
        "order_ids": summary["order_ids"],
        "cancelled_order_ids": summary["cancelled_order_ids"],
        "commission_policy": "recognized_at_settlement",
    }
    args = {
        "p_reference": reference,
        "p_kind": kind,
        "p_description": description or (
            f"Customer payment {tx_ref}" + (f" via {provider}" if provider else "")),
        "p_lines": lines,
        "p_payment_ref": tx_ref,
        "p_actor": actor,
        "p_metadata": metadata,
        "p_currency": summary["currency"],
    }

    try:
        result = db.rpc("post_journal", args)
    except Exception as exc:  # a transport failure must not crash the payment path
        log.error("ledger.post_exception", reference=reference, error=type(exc).__name__)
        return PaymentPosting(ok=False, reference=reference, lines=lines, summary=summary,
                              error=type(exc).__name__)

    if not getattr(result, "ok", False):
        error = str(getattr(result, "error", "") or "ledger post failed")[:300]
        log.error("ledger.post_failed", reference=reference, error=error)
        return PaymentPosting(ok=False, reference=reference, lines=lines, summary=summary,
                              error=error)

    data = getattr(result, "data", None)
    if isinstance(data, list) and data:
        data = data[0]
    journal_id = str(data) if isinstance(data, str) and data else None
    if not journal_id:
        log.error("ledger.post_without_id", reference=reference)
        return PaymentPosting(ok=False, reference=reference, lines=lines, summary=summary,
                              error="post_journal returned no journal id")

    log.info("ledger.posted", reference=reference, journal_id=journal_id,
             amount=_fmt(summary["received"]))
    return PaymentPosting(ok=True, reference=reference, journal_id=journal_id,
                          lines=lines, summary=summary)


@dataclass
class SettlementPosting:
    ok: bool
    reference: str
    journal_id: str | None = None
    error: str = ""
    lines: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def settlement_reference(payout_id) -> str:
    return f"settlement:{str(payout_id or '').strip()}"


def build_settlement_lines(payout: dict, *, channel_account: str
                           ) -> tuple[list[dict], dict]:
    """Build the journal that pays a farmer payout.

    The payout row is the snapshot: gross, commission and net must already add
    up. `channel_account` is the asset the customer receipt debited
    (PROVIDER_CLEARING or COD_CASH_HELD), never a caller-supplied choice.
    """
    if not isinstance(payout, dict):
        raise LedgerError("payout row must be an object")
    if channel_account not in SETTLEMENT_ASSET_ACCOUNTS:
        raise LedgerError(f"unsupported settlement channel: {channel_account!r}")

    gross = money(payout.get("gross_amount"))
    commission = money(payout.get("commission_amount"))
    net = money(payout.get("net_amount"))
    pct = money(payout.get("commission_pct") or 0)

    if gross <= ZERO:
        raise LedgerError("settlement gross must be positive")
    if commission < ZERO or net < ZERO:
        raise LedgerError("settlement amounts cannot be negative")
    if commission + net != gross:
        raise LedgerError("settlement commission and net do not add up to gross")
    if pct < ZERO or pct > Decimal("100"):
        raise LedgerError(f"invalid commission percentage: {pct}")

    lines = [_line(ACCOUNT_FARMER_PAYABLE, "debit", gross,
                   "farmer payable settled")]
    if commission > ZERO:
        lines.append(_line(ACCOUNT_COMMISSION_REVENUE, "credit", commission,
                           f"commission {pct}%"))
    if net > ZERO:
        lines.append(_line(channel_account, "credit", net,
                           "farmer payout paid"))

    debits = sum((money(line["amount"]) for line in lines if line["side"] == "debit"), ZERO)
    credits = sum((money(line["amount"]) for line in lines if line["side"] == "credit"), ZERO)
    if debits != credits or len(lines) < 2:
        raise LedgerError("settlement journal does not balance")

    summary = {
        "currency": UGX,
        "gross": gross,
        "commission_pct": pct,
        "commission": commission,
        "net": net,
        "channel_account": channel_account,
    }
    return lines, summary


def post_settlement(db, *, payout: dict, channel_account: str, provider: str,
                    provider_ref: str, actor=None) -> SettlementPosting:
    """Post one idempotent farmer-settlement journal (`settlement:<payout_id>`).

    The provider reference is the evidence that money actually moved. The
    caller must only invoke this after verifying the payout is pending, its
    order is delivered and paid, and the customer receipt is in the ledger.
    """
    payout_id = str((payout or {}).get("id") or "").strip()
    reference = settlement_reference(payout_id)
    provider_ref = str(provider_ref or "").strip()
    if not payout_id or len(payout_id) > 80:
        return SettlementPosting(ok=False, reference=reference,
                                 error="a valid payout id is required")
    if not provider_ref or len(provider_ref) > 120:
        return SettlementPosting(ok=False, reference=reference,
                                 error="a provider reference is required")
    if not all(32 <= ord(ch) < 127 for ch in provider_ref):
        return SettlementPosting(ok=False, reference=reference,
                                 error="provider reference must be printable text")

    try:
        lines, summary = build_settlement_lines(
            payout, channel_account=channel_account)
    except LedgerError as exc:
        return SettlementPosting(ok=False, reference=reference, error=str(exc))

    metadata = {
        "payout_id": payout_id,
        "order_id": (payout or {}).get("order_id"),
        "farmer_id": (payout or {}).get("farmer_id"),
        "gross": _fmt(summary["gross"]),
        "commission_pct": _fmt(summary["commission_pct"]),
        "commission": _fmt(summary["commission"]),
        "net": _fmt(summary["net"]),
        "channel_account": channel_account,
        "provider": str(provider or "")[:40],
        "provider_ref": provider_ref,
        "currency": summary["currency"],
        "commission_policy": "recognized_at_settlement",
    }
    args = {
        "p_reference": reference,
        "p_kind": SETTLEMENT_KIND,
        "p_description": (f"Farmer settlement for payout {payout_id} "
                          f"order {metadata.get('order_id') or '-'}"),
        "p_lines": lines,
        "p_payment_ref": None,
        "p_actor": actor,
        "p_metadata": metadata,
        "p_currency": summary["currency"],
    }

    try:
        result = db.rpc("post_journal", args)
    except Exception as exc:  # a transport failure must leave the payout pending
        log.error("ledger.settlement_exception", reference=reference,
                  error=type(exc).__name__)
        return SettlementPosting(ok=False, reference=reference, lines=lines,
                                 summary=summary, error=type(exc).__name__)

    if not getattr(result, "ok", False):
        error = str(getattr(result, "error", "") or "ledger post failed")[:300]
        log.error("ledger.settlement_failed", reference=reference, error=error)
        return SettlementPosting(ok=False, reference=reference, lines=lines,
                                 summary=summary, error=error)

    data = getattr(result, "data", None)
    if isinstance(data, list) and data:
        data = data[0]
    journal_id = str(data) if isinstance(data, str) and data else None
    if not journal_id:
        log.error("ledger.settlement_without_id", reference=reference)
        return SettlementPosting(ok=False, reference=reference, lines=lines,
                                 summary=summary, error="post_journal returned no journal id")

    log.info("ledger.settlement_posted", reference=reference, journal_id=journal_id,
             net=_fmt(summary["net"]))
    return SettlementPosting(ok=True, reference=reference, journal_id=journal_id,
                             lines=lines, summary=summary)


RECURRING_KIND = "recurring_payment"


def recurring_reference(tracking_id) -> str:
    return f"recurring:{str(tracking_id or '').strip()}"


def post_recurring_charge(db, *, tracking_id: str, amount, currency: str = UGX,
                          provider: str = "", account_ref: str = "",
                          correlation_id: str = "",
                          confirmation_code: str = "") -> PaymentPosting:
    """Post a gateway recurring charge that has no new order attached yet.

    The customer authorised Pesapal to charge the same amount on a schedule,
    but goods for this cycle have not been priced or dispatched. Crediting
    FARMER_PAYABLE would pay a farmer for an order that does not exist, so the
    receipt is held in SUSPENSE_OVERPAYMENT until operations allocate it.

    Reference `recurring:<tracking_id>` makes retried IPNs idempotent. The
    journal deliberately has no `payment_ref`: reconciliation groups active
    journals by that column and this money belongs to no checkout receipt.
    """
    tracking_id = str(tracking_id or "").strip()
    reference = recurring_reference(tracking_id)
    if not tracking_id or len(tracking_id) > 80:
        return PaymentPosting(ok=False, reference=reference,
                              error="a valid tracking id is required")
    cur = str(currency or "").strip().upper()
    if cur != UGX:
        return PaymentPosting(ok=False, reference=reference,
                              error=f"unsupported ledger currency: {currency!r}")
    try:
        received = money(amount)
    except LedgerError as exc:
        return PaymentPosting(ok=False, reference=reference, error=str(exc))
    if received <= ZERO:
        return PaymentPosting(ok=False, reference=reference,
                              error="recurring amount must be positive")

    lines = [
        _line(ACCOUNT_PROVIDER_CLEARING, "debit", received,
              "recurring customer payment received"),
        _line(ACCOUNT_SUSPENSE_OVERPAYMENT, "credit", received,
              "recurring payment awaiting order allocation"),
    ]
    summary = {"currency": cur, "received": received}
    metadata = {
        "provider": str(provider or "")[:80],
        "tracking_id": tracking_id,
        "account_ref": str(account_ref or "")[:160],
        "correlation_id": str(correlation_id or "")[:120],
        "confirmation_code": str(confirmation_code or "")[:80],
        "received": _fmt(received),
        "currency": cur,
        "allocation_policy": "suspense_until_order_allocated",
    }
    args = {
        "p_reference": reference,
        "p_kind": RECURRING_KIND,
        "p_description": (f"Recurring payment {tracking_id} for account "
                          f"{metadata['account_ref'] or '-'}"),
        "p_lines": lines,
        "p_payment_ref": None,
        "p_actor": None,
        "p_metadata": metadata,
        "p_currency": cur,
    }

    try:
        result = db.rpc("post_journal", args)
    except Exception as exc:
        log.error("ledger.recurring_exception", reference=reference,
                  error=type(exc).__name__)
        return PaymentPosting(ok=False, reference=reference, lines=lines,
                              summary=summary, error=type(exc).__name__)

    if not getattr(result, "ok", False):
        error = str(getattr(result, "error", "") or "ledger post failed")[:300]
        log.error("ledger.recurring_failed", reference=reference, error=error)
        return PaymentPosting(ok=False, reference=reference, lines=lines,
                              summary=summary, error=error)

    data = getattr(result, "data", None)
    if isinstance(data, list) and data:
        data = data[0]
    journal_id = str(data) if isinstance(data, str) and data else None
    if not journal_id:
        log.error("ledger.recurring_without_id", reference=reference)
        return PaymentPosting(ok=False, reference=reference, lines=lines,
                              summary=summary, error="post_journal returned no journal id")

    log.info("ledger.recurring_posted", reference=reference, journal_id=journal_id,
             amount=_fmt(received))
    return PaymentPosting(ok=True, reference=reference, journal_id=journal_id,
                          lines=lines, summary=summary)
