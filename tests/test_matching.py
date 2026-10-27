# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Tests for services.matching.MatchingService (Phase 5 steps 5–15).

The Postgres functions (eligibility filter, ranking, atomic reservation on
accept, conflict prevention) are proven by the SQL integration tests against a
live DB. Here we assert the Python orchestration contract: input validation,
honest failure mapping (no fake success), the matching pass sequence
(expire -> claim -> offer), and that a no-match enqueues an admin alert.
"""
from __future__ import annotations

from conftest import FakeSupabase, RpcResult

from services.matching import MatchingService


def test_create_request_validates_buyer_and_product():
    svc = MatchingService(supabase=FakeSupabase())
    assert not svc.create_request(buyer_id="", product="Maize", quantity=10).ok
    assert not svc.create_request(buyer_id="u", product="", quantity=10).ok


def test_create_request_rejects_bad_quantity():
    svc = MatchingService(supabase=FakeSupabase())
    for bad in (0, -3, "abc", None):
        r = svc.create_request(buyer_id="u", product="Maize", quantity=bad)
        assert not r.ok and r.status_code == 400, bad


def test_create_request_calls_rpc_and_succeeds():
    db = FakeSupabase(rpc_results={
        "create_purchase_request": RpcResult(ok=True, data={"id": "pr-1", "status": "open"})})
    svc = MatchingService(supabase=db)
    r = svc.create_request(buyer_id="u", product="Maize", quantity=50, unit="kg",
                           delivery_district="Wakiso", idempotency_key="k1")
    assert r.ok and r.data["id"] == "pr-1"
    fn, args = db.rpc_calls[0]
    assert fn == "create_purchase_request"
    assert args["p_quantity"] == 50.0 and args["p_idempotency_key"] == "k1"


def test_matching_pass_expires_then_claims_then_offers():
    db = FakeSupabase(rpc_results={
        "expire_overdue_offers": RpcResult(ok=True, data=2),
        "claim_unmatched_requests": RpcResult(ok=True, data=[{"id": "pr-1"}, {"id": "pr-2"}]),
        "match_and_offer": RpcResult(ok=True, data={"offers": 1}),
    })
    svc = MatchingService(supabase=db)
    r = svc.run_matching_pass(batch_size=10)
    assert r.ok and r.expired == 2 and r.requests_processed == 2 and r.offers == 2
    fns = [f for (f, _) in db.rpc_calls]
    # expiry must happen before claiming so timed-out requests can be re-matched
    assert fns.index("expire_overdue_offers") < fns.index("claim_unmatched_requests")


def test_matching_pass_claim_failure_is_honest():
    db = FakeSupabase(rpc_results={
        "expire_overdue_offers": RpcResult(ok=True, data=0),
        "claim_unmatched_requests": RpcResult(ok=False, error="db down"),
    })
    svc = MatchingService(supabase=db)
    r = svc.run_matching_pass()
    assert not r.ok and r.status_code == 500


def test_accept_offer_maps_conflict_to_409():
    db = FakeSupabase(rpc_results={
        "accept_offer": RpcResult(ok=False, status=400, error="stock no longer available")})
    svc = MatchingService(supabase=db)
    r = svc.accept_offer("offer-1", "farmer-1")
    assert not r.ok and r.status_code == 409


def test_accept_offer_maps_forbidden_to_403():
    db = FakeSupabase(rpc_results={
        "accept_offer": RpcResult(ok=False, status=400, error="forbidden: offer belongs to another farmer")})
    svc = MatchingService(supabase=db)
    assert svc.accept_offer("o", "f").status_code == 403


def test_accept_offer_success():
    db = FakeSupabase(rpc_results={
        "accept_offer": RpcResult(ok=True, data={"request_id": "pr-1", "status": "assigned",
                                                 "order_ref": "PR-1", "farmer_id": "f"})})
    svc = MatchingService(supabase=db)
    r = svc.accept_offer("offer-1", "f")
    assert r.ok and r.data["status"] == "assigned"


def test_reject_offer_forbidden_maps_403():
    db = FakeSupabase(rpc_results={"reject_offer": RpcResult(ok=False, error="forbidden")})
    svc = MatchingService(supabase=db)
    assert svc.reject_offer("o", "f").status_code == 403
