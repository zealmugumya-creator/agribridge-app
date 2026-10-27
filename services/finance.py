# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Read-only projections for the admin Financial Control Center.

These functions take rows already fetched from Supabase and return plain dicts.
Keeping the arithmetic out of the Flask routes makes every figure testable
offline. Nothing here writes to the ledger and nothing here guesses missing
business data: a metric with no source column is returned as unavailable.
"""
from __future__ import annotations

import datetime as _dt
import math

PAID_PAYOUT_STATUSES = {"paid", "completed", "settled"}
PENDING_PAYOUT_STATUSES = {"pending", "processing", "approved", "scheduled", "in_transit"}
FAILED_PAYOUT_STATUSES = {"failed", "cancelled", "rejected"}
DISPUTED_PAYOUT_STATUSES = {"disputed", "on_hold", "held"}

TRANSPORTER_FEE_REASON = (
    "Deliveries do not store an agreed transporter charge, so no transporter "
    "payable can be calculated yet. Add the agreed charge when assigning a "
    "transporter before this section can report real money."
)
DELIVERY_REVENUE_REASON = (
    "Orders do not store a separate delivery fee, so AgriBridge delivery "
    "revenue cannot be separated from product value yet."
)


def num(value) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else 0.0
    except (TypeError, ValueError):
        return 0.0


def money(value) -> float:
    return round(num(value), 2)


def status_of(row: dict) -> str:
    return str((row or {}).get("status") or "").strip().lower()


def order_total(row: dict) -> float:
    """Order value in UGX.

    `total_amount` is the generated qty*price column; `total_price` is the
    legacy client-supplied value. Prefer the generated column when populated.
    """
    row = row or {}
    total = num(row.get("total_amount"))
    if total <= 0:
        total = num(row.get("total_price"))
    if total <= 0:
        total = num(row.get("quantity_kg")) * num(row.get("price_per_kg"))
    return money(total)


def payout_bucket(row: dict) -> str:
    status = status_of(row)
    if status in PAID_PAYOUT_STATUSES:
        return "paid"
    if status in PENDING_PAYOUT_STATUSES:
        return "pending"
    if status in FAILED_PAYOUT_STATUSES:
        return "failed"
    if status in DISPUTED_PAYOUT_STATUSES:
        return "disputed"
    return "other"


def _bucket_totals(rows: list[dict]) -> dict:
    out = {
        name: {"amount": 0.0, "count": 0}
        for name in ("paid", "pending", "failed", "disputed", "other")
    }
    for row in rows:
        bucket = payout_bucket(row)
        out[bucket]["amount"] += num(row.get("net_amount"))
        out[bucket]["count"] += 1
    for bucket in out.values():
        bucket["amount"] = money(bucket["amount"])
    return out


def build_overview(orders: list[dict], payouts: list[dict],
                   balances: list[dict] | None,
                   ledger_ready: bool,
                   ledger_error: str | None = None) -> dict:
    orders = orders or []
    payouts = payouts or []
    paid_orders = [o for o in orders if str(o.get("payment_status") or "").lower() == "paid"]
    refunded_orders = [o for o in orders if str(o.get("payment_status") or "").lower() == "refunded"]
    buckets = _bucket_totals(payouts)

    unpaid_net = sum(
        num(p.get("net_amount")) for p in payouts if payout_bucket(p) != "paid"
    )
    unpaid_count = sum(1 for p in payouts if payout_bucket(p) != "paid")

    recognized_commission = sum(
        num(p.get("commission_amount")) for p in payouts if payout_bucket(p) == "paid"
    )
    pending_commission = sum(
        num(p.get("commission_amount")) for p in payouts if payout_bucket(p) == "pending"
    )

    accounts = []
    total_debits = 0.0
    total_credits = 0.0
    for row in balances or []:
        account = {
            "code": row.get("code"),
            "name": row.get("name"),
            "type": row.get("account_type"),
            "normal_side": row.get("normal_side"),
            "total_debits": money(row.get("total_debits")),
            "total_credits": money(row.get("total_credits")),
            "balance": money(row.get("balance")),
        }
        accounts.append(account)
        total_debits += account["total_debits"]
        total_credits += account["total_credits"]

    balanced = None
    if ledger_ready:
        balanced = abs(total_debits - total_credits) < 0.005

    return {
        "currency": "UGX",
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "customer_payments": {
            "received": money(sum(order_total(o) for o in paid_orders)),
            "count": len(paid_orders),
            "source": "orders.payment_status = paid",
        },
        "farmer_payables": {
            "outstanding": money(unpaid_net),
            "count": unpaid_count,
            "paid_to_date": buckets["paid"]["amount"],
            "pending": buckets["pending"],
            "failed": buckets["failed"],
            "disputed": buckets["disputed"],
            "other": buckets["other"],
            "source": "payouts.net_amount",
        },
        "transporter_payables": {
            "available": False,
            "outstanding": None,
            "reason": TRANSPORTER_FEE_REASON,
        },
        "commission_revenue": {
            "recognized": money(recognized_commission),
            "pending": money(pending_commission),
            "source": "payouts.commission_amount (paid vs pending payouts)",
        },
        "delivery_revenue": {
            "available": False,
            "recognized": None,
            "reason": DELIVERY_REVENUE_REASON,
        },
        "refunds": {
            "amount": money(sum(order_total(o) for o in refunded_orders)),
            "count": len(refunded_orders),
            "source": "orders.payment_status = refunded",
        },
        "outstanding_settlements": {
            "amount": money(unpaid_net),
            "count": unpaid_count,
            "source": "all payouts not in a paid status",
        },
        "ledger": {
            "ready": ledger_ready,
            "error": ledger_error,
            "accounts": accounts,
            "total_debits": money(total_debits),
            "total_credits": money(total_credits),
            "balanced": balanced,
        },
    }


def build_farmer_settlements(payouts: list[dict], orders: list[dict],
                             farmers: list[dict]) -> dict:
    orders_by_id = {str(o.get("id")): o for o in orders or [] if o.get("id")}
    farmers_by_id = {str(f.get("id")): f for f in farmers or [] if f.get("id")}
    rows = []
    for payout in payouts or []:
        order = orders_by_id.get(str(payout.get("order_id")), {})
        farmer = farmers_by_id.get(str(payout.get("farmer_id")), {})
        rows.append({
            "id": payout.get("id"),
            "order_id": payout.get("order_id"),
            "farmer_id": payout.get("farmer_id"),
            "farmer_name": farmer.get("full_name") or "Unknown farmer",
            "gross_amount": money(payout.get("gross_amount")),
            "commission_pct": num(payout.get("commission_pct")),
            "commission_amount": money(payout.get("commission_amount")),
            "net_amount": money(payout.get("net_amount")),
            "status": status_of(payout) or "unknown",
            "provider": payout.get("provider"),
            "provider_ref": payout.get("provider_ref"),
            "created_at": payout.get("created_at"),
            "paid_at": payout.get("paid_at"),
            "order_status": status_of(order) or None,
            "order_payment_status": (order.get("payment_status") or None),
            "order_total": order_total(order) if order else None,
        })
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    buckets = _bucket_totals(payouts or [])
    delivered = [o for o in orders or [] if status_of(o) == "delivered"]
    return {
        "currency": "UGX",
        "summary": {
            "completed_sales_count": len(delivered),
            "completed_sales_value": money(sum(order_total(o) for o in delivered)),
            "paid": buckets["paid"],
            "pending": buckets["pending"],
            "failed": buckets["failed"],
            "disputed": buckets["disputed"],
            "other": buckets["other"],
            "outstanding": money(sum(
                num(p.get("net_amount")) for p in payouts or []
                if payout_bucket(p) != "paid"
            )),
            "unmatched_payouts": sum(1 for r in rows if not r.get("order_id")),
        },
        "rows": rows,
    }


def build_transporter_settlements(deliveries: list[dict],
                                  transporters: list[dict] | None = None) -> dict:
    by_id = {str(t.get("id")): t for t in transporters or [] if t.get("id")}
    rows = []
    for delivery in deliveries or []:
        transporter = by_id.get(str(delivery.get("transporter_id")), {})
        rows.append({
            "id": delivery.get("id"),
            "order_id": delivery.get("order_id"),
            "driver_name": delivery.get("driver_name") or transporter.get("full_name") or "Unassigned",
            "driver_phone": delivery.get("driver_phone") or transporter.get("phone"),
            "tracking_code": delivery.get("tracking_code"),
            "status": status_of(delivery) or "unknown",
            "delivered_at": delivery.get("actual_arrival"),
            "agreed_charge": None,
            "payable_status": "not_captured",
            "payment_ref": None,
        })
    rows.sort(key=lambda r: str(r.get("delivered_at") or ""), reverse=True)
    delivered = [d for d in deliveries or [] if status_of(d) == "delivered"]
    return {
        "currency": "UGX",
        "available": False,
        "reason": TRANSPORTER_FEE_REASON,
        "summary": {
            "deliveries_total": len(deliveries or []),
            "delivered_count": len(delivered),
            "failed_count": sum(1 for d in deliveries or [] if status_of(d) == "failed"),
            "agreed_charges_total": None,
            "payable_total": None,
            "paid_total": None,
        },
        "rows": rows,
    }


def _journal_total(journal: dict, lines_by_journal: dict[str, list[dict]]) -> float:
    lines = lines_by_journal.get(str(journal.get("id")), [])
    debits = sum(num(line.get("amount")) for line in lines
                 if str(line.get("side") or "").lower() == "debit")
    credits = sum(num(line.get("amount")) for line in lines
                  if str(line.get("side") or "").lower() == "credit")
    return money(debits if debits else credits)


def build_journal_rows(journals: list[dict], lines: list[dict]) -> list[dict]:
    lines_by_journal: dict[str, list[dict]] = {}
    for line in lines or []:
        lines_by_journal.setdefault(str(line.get("journal_id")), []).append(line)
    rows = []
    for journal in journals or []:
        journal_id = str(journal.get("id"))
        rows.append({
            "id": journal.get("id"),
            "reference": journal.get("reference"),
            "kind": journal.get("kind"),
            "currency": journal.get("currency"),
            "payment_ref": journal.get("payment_ref"),
            "description": journal.get("description"),
            "status": journal.get("status"),
            "posted_by": journal.get("posted_by"),
            "posted_at": journal.get("posted_at"),
            "reverses_journal_id": journal.get("reverses_journal_id"),
            "amount": _journal_total(journal, lines_by_journal),
            "lines": sorted(
                lines_by_journal.get(journal_id, []),
                key=lambda line: (str(line.get("account_code") or ""), str(line.get("side") or "")),
            ),
        })
    return rows


def build_reconciliation(orders: list[dict], journals: list[dict],
                         lines: list[dict]) -> dict:
    orders = orders or []
    journals = journals or []
    lines_by_journal: dict[str, list[dict]] = {}
    for line in lines or []:
        lines_by_journal.setdefault(str(line.get("journal_id")), []).append(line)

    paid_refs: dict[str, dict] = {}
    paid_without_ref = []
    for order in orders:
        if str(order.get("payment_status") or "").lower() != "paid":
            continue
        ref = str(order.get("payment_ref") or "").strip()
        if not ref:
            paid_without_ref.append(order)
            continue
        bucket = paid_refs.setdefault(ref, {"total": 0.0, "order_ids": []})
        bucket["total"] += order_total(order)
        bucket["order_ids"].append(order.get("id"))

    active: dict[str, list[dict]] = {}
    reversed_refs = set()
    for journal in journals:
        status = status_of(journal)
        kind = str(journal.get("kind") or "").lower()
        ref = str(journal.get("payment_ref") or "").strip()
        if status == "reversed" and ref:
            reversed_refs.add(ref)
        if status != "posted" or kind == "reversal" or not ref:
            continue
        active.setdefault(ref, []).append(journal)

    issues = []
    for ref, group in paid_refs.items():
        group["total"] = money(group["total"])
        matches = active.get(ref, [])
        if not matches:
            if ref in reversed_refs:
                issues.append({
                    "type": "payment_reversed_but_order_paid",
                    "severity": "warning",
                    "payment_ref": ref,
                    "order_ids": group["order_ids"],
                    "amount": group["total"],
                    "detail": "The ledger payment was reversed but the order is still marked paid.",
                })
            else:
                issues.append({
                    "type": "missing_ledger_entry",
                    "severity": "error",
                    "payment_ref": ref,
                    "order_ids": group["order_ids"],
                    "amount": group["total"],
                    "detail": "A paid order has no active ledger journal for its payment reference.",
                })
            continue
        if len(matches) > 1:
            issues.append({
                "type": "duplicate_ledger_posting",
                "severity": "error",
                "payment_ref": ref,
                "order_ids": group["order_ids"],
                "amount": group["total"],
                "detail": f"{len(matches)} active journals share this payment reference.",
            })
            continue
        journal_total = _journal_total(matches[0], lines_by_journal)
        if abs(journal_total - group["total"]) > 0.01:
            issues.append({
                "type": "amount_mismatch",
                "severity": "error",
                "payment_ref": ref,
                "order_ids": group["order_ids"],
                "amount": money(group["total"] - journal_total),
                "detail": f"Orders total {group['total']:.2f} but the ledger journal totals {journal_total:.2f}.",
            })

    for ref, group in active.items():
        if ref in paid_refs:
            continue
        for journal in group:
            if str(journal.get("kind") or "").lower() != "customer_payment":
                continue
            issues.append({
                "type": "ledger_payment_without_paid_order",
                "severity": "warning",
                "payment_ref": ref,
                "order_ids": [],
                "amount": _journal_total(journal, lines_by_journal),
                "detail": "A customer payment is posted to the ledger but no paid order has this reference.",
            })

    for order in paid_without_ref:
        issues.append({
            "type": "paid_order_without_payment_ref",
            "severity": "warning",
            "payment_ref": "",
            "order_ids": [order.get("id")],
            "amount": order_total(order),
            "detail": "A paid order has no payment reference, so it cannot be reconciled.",
        })

    refunded_refs: dict[str, list] = {}
    for order in orders:
        if str(order.get("payment_status") or "").lower() != "refunded":
            continue
        ref = str(order.get("payment_ref") or "").strip()
        if ref:
            refunded_refs.setdefault(ref, []).append(order.get("id"))
    for ref, order_ids in refunded_refs.items():
        if ref not in reversed_refs:
            issues.append({
                "type": "refund_without_reversal",
                "severity": "warning",
                "payment_ref": ref,
                "order_ids": order_ids,
                "amount": None,
                "detail": "An order is marked refunded but the ledger has no reversal for its payment.",
            })

    order_severity = {"error": 0, "warning": 1}
    issues.sort(key=lambda i: (order_severity.get(i["severity"], 2), i["type"], i["payment_ref"]))
    by_type: dict[str, int] = {}
    for issue in issues:
        by_type[issue["type"]] = by_type.get(issue["type"], 0) + 1

    return {
        "currency": "UGX",
        "checked": {
            "paid_orders": sum(len(v["order_ids"]) for v in paid_refs.values()),
            "payment_refs": len(paid_refs),
            "active_journals": sum(len(v) for v in active.values()),
        },
        "summary": {
            "issues_total": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "by_type": by_type,
        },
        "issues": issues,
    }


def parse_dt(value) -> _dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(_dt.UTC)


def in_date_range(value, start: _dt.date, end: _dt.date) -> bool:
    parsed = parse_dt(value)
    if parsed is None:
        return False
    return start <= parsed.date() <= end


def build_reports(orders: list[dict], payouts: list[dict], journals: list[dict],
                  lines: list[dict], start: _dt.date, end: _dt.date) -> dict:
    orders_in = [o for o in orders or [] if in_date_range(o.get("created_at"), start, end)]
    payouts_in = [p for p in payouts or [] if in_date_range(p.get("created_at"), start, end)]
    journal_ids = {str(j.get("id")) for j in journals or [] if j.get("id")}
    lines_in = [ln for ln in lines or [] if str(ln.get("journal_id")) in journal_ids]

    account_activity: dict[str, dict] = {}
    for line in lines_in:
        code = str(line.get("account_code") or "")
        row = account_activity.setdefault(code, {"account_code": code, "debits": 0.0, "credits": 0.0})
        side = str(line.get("side") or "").lower()
        if side == "debit":
            row["debits"] += num(line.get("amount"))
        elif side == "credit":
            row["credits"] += num(line.get("amount"))
    activity_rows = []
    for row in account_activity.values():
        row["debits"] = money(row["debits"])
        row["credits"] = money(row["credits"])
        row["net"] = money(row["debits"] - row["credits"])
        activity_rows.append(row)
    activity_rows.sort(key=lambda r: r["account_code"])

    by_kind: dict[str, int] = {}
    for journal in journals or []:
        kind = str(journal.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    paid_in = [o for o in orders_in if str(o.get("payment_status") or "").lower() == "paid"]
    refunded_in = [o for o in orders_in if str(o.get("payment_status") or "").lower() == "refunded"]
    buckets = _bucket_totals(payouts_in)
    unpaid_all_time = [p for p in payouts or [] if payout_bucket(p) != "paid"]

    return {
        "currency": "UGX",
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "orders": {
            "count": len(orders_in),
            "value": money(sum(order_total(o) for o in orders_in)),
            "paid_count": len(paid_in),
            "paid_value": money(sum(order_total(o) for o in paid_in)),
            "refunded_count": len(refunded_in),
            "refunded_value": money(sum(order_total(o) for o in refunded_in)),
            "delivered_count": sum(1 for o in orders_in if status_of(o) == "delivered"),
        },
        "settlements": {
            "payouts_count": len(payouts_in),
            "gross": money(sum(num(p.get("gross_amount")) for p in payouts_in)),
            "commission": money(sum(num(p.get("commission_amount")) for p in payouts_in)),
            "net": money(sum(num(p.get("net_amount")) for p in payouts_in)),
            "paid": buckets["paid"],
            "pending": buckets["pending"],
            "failed": buckets["failed"],
            "disputed": buckets["disputed"],
        },
        "liabilities": {
            "unpaid_net": money(sum(num(p.get("net_amount")) for p in unpaid_all_time)),
            "unpaid_count": len(unpaid_all_time),
        },
        "ledger": {
            "journals_count": len(journals or []),
            "by_kind": by_kind,
            "account_activity": activity_rows,
            "total_debits": money(sum(r["debits"] for r in activity_rows)),
            "total_credits": money(sum(r["credits"] for r in activity_rows)),
        },
    }
