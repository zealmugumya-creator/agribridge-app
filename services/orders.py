# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Order service — the server-side authoritative order path (Phase 5 steps 1–4).

This is the module the Flask API calls to create an order. It:
  1. validates the buyer + items,
  2. calls the `create_order_atomic` RPC (price recomputed server-side, stock
     reserved atomically, no oversell — fixes C1/C2/C4),
  3. enqueues the administrator notification and the Discord ops event inside the
     same logical flow, using idempotency/dedupe keys so a retried request cannot
     double-create or double-alert (Phase 5 steps 2–4, Phase 7).

Farmer matching / offer routing (Phase 5 steps 5–15) is a separate concern owned
by the worker; this module guarantees the order is durably persisted and the
admin is alerted first, which is the mandate's hard requirement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.logging import get_logger

log = get_logger(__name__)


@dataclass
class OrderResult:
    ok: bool
    order_ref: str = ""
    tracking_code: str = ""
    total_price: float = 0.0
    items: list = field(default_factory=list)
    error: str = ""
    status_code: int = 200


class OrderService:
    def __init__(self, supabase, dispatcher):
        self._db = supabase
        self._notify = dispatcher

    def create_order(self, *, buyer_id: str, items: list[dict],
                     delivery_address: str, payment_method: str,
                     idempotency_key: str | None, correlation_id: str | None) -> OrderResult:
        # ── Step 1: validate input (server-side, never trust the client) ──────
        if not buyer_id:
            return OrderResult(ok=False, error="buyer_id required", status_code=400)
        if not isinstance(items, list) or not items:
            return OrderResult(ok=False, error="at least one item required", status_code=400)
        UUID_RE = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE,
        )
        normalized = []
        for it in items:
            listing_id_raw = str(it.get("listing_id") or "").strip()
            # Accept a real UUID listing_id; for non-UUID items (equipment, animals
            # listed outside the main listings table) fall back to None so the
            # atomic RPC treats them as a manual / free-form line-item.
            listing_id = listing_id_raw if UUID_RE.match(listing_id_raw) else None
            product    = str(it.get("product") or it.get("name") or "").strip()[:200]
            farmer_id  = str(it.get("farmer_id") or "").strip() or None
            try:
                qty = float(it.get("quantity") or 1)
            except (TypeError, ValueError):
                qty = 1.0
            try:
                price = float(it.get("price_per_unit") or it.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if qty <= 0:
                qty = 1.0
            # Need at least a listing_id OR a product name to proceed.
            if not listing_id and not product:
                log.warning("order.item_skipped", item=it)
                continue
            normalized.append({
                "listing_id":    listing_id,
                "farmer_id":     farmer_id,
                "product":       product,
                "quantity":      qty,
                "price_per_unit": price,
                "unit":          str(it.get("unit") or "kg")[:20],
                "currency":      "UGX",
                "item_category": str(it.get("item_category") or "produce")[:40],
            })
        if not normalized:
            return OrderResult(ok=False, error="no valid items in order", status_code=400)

        # ── Step 2: atomic create + reserve (Postgres RPC) ────────────────────
        res = self._db.rpc("create_order_atomic", {
            "p_buyer_id": buyer_id,
            "p_items": normalized,
            "p_delivery_address": (delivery_address or "")[:500],
            "p_payment_method": (payment_method or "")[:40],
            "p_idempotency_key": idempotency_key,
        })
        if not res.ok:
            # Map common Postgres error conditions to honest HTTP statuses.
            err = (res.error or "").lower()
            code = 409 if ("insufficient" in err or "race" in err or "not available" in err) else 400
            log.warning("order.create_failed", error=res.error, correlation_id=correlation_id)
            return OrderResult(ok=False, error=res.error or "order creation failed", status_code=code)

        data = res.data if isinstance(res.data, dict) else {}
        order_ref = data.get("order_ref", "")
        result = OrderResult(
            ok=True, order_ref=order_ref,
            tracking_code=data.get("tracking_code", ""),
            total_price=float(data.get("total_price") or 0),
            items=data.get("items", []),
        )

        # ── Steps 3–4: immediately enqueue admin notification + Discord event ─
        # dedupe on order_ref so a replayed request cannot double-alert.
        dedupe = f"order.created:{order_ref}" if order_ref else None
        try:
            self._notify.enqueue(
                channel="push", audience="admin", event_type="order.created",
                severity="info", title="New order", body=f"Order {order_ref}",
                payload={"order_ref": order_ref, "total": result.total_price,
                         "items": len(result.items)},
                dedupe_key=dedupe, correlation_id=correlation_id,
            )
            self._notify.enqueue(
                channel="discord", audience="admin", event_type="order.created",
                severity="info", title="New order received",
                payload={"order_ref": order_ref, "total_ugx": result.total_price,
                         "items": len(result.items), "payment_method": payment_method},
                dedupe_key=f"discord:{dedupe}", correlation_id=correlation_id,
            )
        except Exception:
            # The order is already committed; a notification failure must not
            # fail the order. It is observable via logs + the outbox retry path.
            log.exception("order.notify_enqueue_failed", order_ref=order_ref)

        log.info("order.created", order_ref=order_ref, total=result.total_price,
                 correlation_id=correlation_id)
        return result
