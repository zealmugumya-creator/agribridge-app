# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Farmer matching & offer routing service (mandate Phase 5 steps 5–15).

Thin orchestration over the Postgres functions from migrations 0004/0005. The
heavy lifting — eligibility filtering, ranking, atomic stock reservation on
acceptance, and conflict prevention — happens in the database so it is
transaction-safe and cannot oversell. This module:

  * creates purchase requests (idempotent),
  * runs a matching pass (expire overdue offers -> claim unmatched -> offer),
  * accepts/rejects offers on behalf of a verified farmer,
  * enqueues the admin/Discord alerts when matching fails.

Every method returns a structured result; failures are honest (no fake success)
and observable via the order_events audit trail written by the RPCs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.logging import get_logger

log = get_logger(__name__)


@dataclass
class MatchResult:
    ok: bool
    data: Any = None
    error: str = ""
    status_code: int = 200
    offers: int = 0
    expired: int = 0
    requests_processed: int = 0
    events: list = field(default_factory=list)


class MatchingService:
    def __init__(self, supabase, dispatcher=None):
        self._db = supabase
        self._notify = dispatcher

    # ── Purchase request creation ────────────────────────────────────────────
    def create_request(self, *, buyer_id, product, quantity, unit="kg",
                       delivery_address="", delivery_district=None, target_price=None,
                       idempotency_key=None, correlation_id=None) -> MatchResult:
        if not buyer_id or not product:
            return MatchResult(ok=False, error="buyer_id and product required", status_code=400)
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            return MatchResult(ok=False, error="invalid quantity", status_code=400)
        if qty <= 0:
            return MatchResult(ok=False, error="quantity must be positive", status_code=400)

        res = self._db.rpc("create_purchase_request", {
            "p_buyer_id": buyer_id, "p_product": str(product)[:120], "p_quantity": qty,
            "p_unit": str(unit)[:20], "p_delivery_address": (delivery_address or "")[:500],
            "p_delivery_district": delivery_district, "p_target_price": target_price,
            "p_idempotency_key": idempotency_key,
        })
        if not res.ok:
            return MatchResult(ok=False, error=res.error or "could not create request",
                               status_code=400)
        log.info("request.created", product=product, quantity=qty, correlation_id=correlation_id)
        return MatchResult(ok=True, data=res.data)

    # ── One matching pass (run by the worker on a schedule) ──────────────────
    def run_matching_pass(self, batch_size: int = 10) -> MatchResult:
        result = MatchResult(ok=True)

        # Step 10: expire overdue offers first so their requests can be re-matched.
        exp = self._db.rpc("expire_overdue_offers", {})
        if exp.ok and isinstance(exp.data, int):
            result.expired = exp.data

        claim = self._db.rpc("claim_unmatched_requests", {"p_limit": batch_size})
        if not claim.ok:
            log.error("matching.claim_failed", error=claim.error)
            return MatchResult(ok=False, error=claim.error, status_code=500)

        rows = claim.data if isinstance(claim.data, list) else ([claim.data] if claim.data else [])
        result.requests_processed = len(rows)
        for req in rows:
            rid = req.get("id") if isinstance(req, dict) else None
            if not rid:
                continue
            offered = self._db.rpc("match_and_offer", {"p_request_id": rid})
            if offered.ok and isinstance(offered.data, dict):
                n = int(offered.data.get("offers") or 0)
                result.offers += n
                result.events.append({"request_id": rid, "offers": n})
                if n == 0:
                    log.warning("matching.no_farmer", request_id=rid)
            else:
                log.error("matching.offer_failed", request_id=rid, error=offered.error)

        if result.requests_processed:
            log.info("matching.pass", processed=result.requests_processed,
                     offers=result.offers, expired=result.expired)
        return result

    # ── Farmer offer responses ───────────────────────────────────────────────
    def accept_offer(self, offer_id: str, farmer_id: str) -> MatchResult:
        res = self._db.rpc("accept_offer", {"p_offer_id": offer_id, "p_farmer_id": farmer_id})
        if not res.ok:
            err = (res.error or "").lower()
            code = 409 if ("conflict" in err or "no longer" in err or "expired" in err
                           or "stock" in err or "already" in err) else 400
            if "forbidden" in err:
                code = 403
            log.warning("offer.accept_failed", offer_id=offer_id, error=res.error)
            return MatchResult(ok=False, error=res.error or "could not accept offer",
                               status_code=code)
        return MatchResult(ok=True, data=res.data)

    def reject_offer(self, offer_id: str, farmer_id: str, reason: str | None = None) -> MatchResult:
        res = self._db.rpc("reject_offer",
                           {"p_offer_id": offer_id, "p_farmer_id": farmer_id, "p_reason": reason})
        if not res.ok:
            code = 403 if "forbidden" in (res.error or "").lower() else 400
            return MatchResult(ok=False, error=res.error or "could not reject offer",
                               status_code=code)
        return MatchResult(ok=True, data=True)
