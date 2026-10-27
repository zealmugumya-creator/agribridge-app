# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Tests for services.orders.OrderService — the server-side authoritative path.

Covers audit findings C1 (client price not trusted), C2 (oversell surfaced), and
Phase 5 steps 1–4 (validate, persist, idempotency, enqueue admin + Discord).
The Postgres atomicity itself is proven by the SQL integration tests (live DB);
here we assert the Python contract around the RPC.
"""
from __future__ import annotations

from conftest import FakeSupabase, RpcResult

from services.notifications import NotificationDispatcher
from services.orders import OrderService

LISTING_ID = "11111111-1111-4111-8111-111111111111"


def _svc(rpc_result):
    db = FakeSupabase(rpc_results={"create_order_atomic": rpc_result,
                                   "enqueue_notification": RpcResult(ok=True, data="nid")})
    notify = NotificationDispatcher(supabase=db, discord=None)
    return OrderService(supabase=db, dispatcher=notify), db, notify


def test_rejects_missing_buyer():
    svc, db, _ = _svc(RpcResult(ok=True))
    res = svc.create_order(buyer_id="", items=[{"listing_id": "x", "quantity": 1}],
                           delivery_address="", payment_method="cod",
                           idempotency_key=None, correlation_id=None)
    assert not res.ok and res.status_code == 400
    assert db.rpc_calls == []          # never hits the DB without a buyer


def test_rejects_empty_items():
    svc, db, _ = _svc(RpcResult(ok=True))
    res = svc.create_order(buyer_id="u1", items=[], delivery_address="",
                           payment_method="cod", idempotency_key=None, correlation_id=None)
    assert not res.ok and res.status_code == 400


def test_rejects_nonpositive_or_invalid_quantity():
    svc, _, _ = _svc(RpcResult(ok=True))
    for bad in (0, -5, "abc", None):
        res = svc.create_order(buyer_id="u1", items=[{"listing_id": "L", "quantity": bad}],
                               delivery_address="", payment_method="cod",
                               idempotency_key=None, correlation_id=None)
        assert not res.ok and res.status_code == 400, bad


def test_client_supplied_price_is_ignored():
    """The item dict carries a bogus 'price'; client-supplied price fields must
    NOT be forwarded to the RPC as authoritative price — only unit_price derived
    from normalisation (for free-form items) is passed, and the DB recomputes
    the price for catalogue items from their listing row (C1).
    The RPC receives listing_id, quantity, and contextual fields but never the
    raw 'price' or 'total_price' keys the client submitted."""
    svc, db, _ = _svc(RpcResult(ok=True, data={"order_ref": "AB-1", "tracking_code": "T",
                                               "total_price": 5000, "items": []}))
    svc.create_order(buyer_id="u1",
                     items=[{"listing_id": LISTING_ID, "quantity": 5, "price": 1, "total_price": 1}],
                     delivery_address="Kampala", payment_method="cod",
                     idempotency_key="k1", correlation_id="c1")
    fn, args = db.rpc_calls[0]
    assert fn == "create_order_atomic"
    # Core invariant (C1): raw client price keys are never forwarded verbatim.
    sent_item = args["p_items"][0]
    assert "price" not in sent_item, "raw 'price' must not be forwarded to RPC"
    assert "total_price" not in sent_item, "raw 'total_price' must not be forwarded to RPC"
    assert sent_item["listing_id"] == LISTING_ID
    assert sent_item["quantity"] == 5.0
    assert args["p_buyer_id"] == "u1"
    assert args["p_idempotency_key"] == "k1"


def test_successful_order_enqueues_admin_push_and_discord():
    svc, db, _ = _svc(RpcResult(ok=True, data={"order_ref": "AB-9", "tracking_code": "TRK",
                                               "total_price": 12000, "items": [{"listing_id": LISTING_ID}]}))
    res = svc.create_order(buyer_id="u1", items=[{"listing_id": LISTING_ID, "quantity": 2}],
                           delivery_address="A", payment_method="momo",
                           idempotency_key="k", correlation_id="cid")
    assert res.ok and res.order_ref == "AB-9" and res.total_price == 12000
    enq = [a for (f, a) in db.rpc_calls if f == "enqueue_notification"]
    channels = {e["p_channel"] for e in enq}
    assert channels == {"push", "discord"}
    # dedupe keys are order-scoped so a replay cannot double-alert
    assert all("AB-9" in e["p_dedupe_key"] for e in enq)
    assert all(e["p_correlation_id"] == "cid" for e in enq)


def test_insufficient_stock_maps_to_409():
    svc, _, _ = _svc(RpcResult(ok=False, status=400,
                               error="insufficient stock for listing L1"))
    res = svc.create_order(buyer_id="u1", items=[{"listing_id": LISTING_ID, "quantity": 999}],
                           delivery_address="", payment_method="cod",
                           idempotency_key=None, correlation_id=None)
    assert not res.ok and res.status_code == 409   # honest conflict, not a fake success


def test_reservation_race_maps_to_409():
    svc, _, _ = _svc(RpcResult(ok=False, status=400, error="stock reservation race lost"))
    res = svc.create_order(buyer_id="u1", items=[{"listing_id": LISTING_ID, "quantity": 1}],
                           delivery_address="", payment_method="cod",
                           idempotency_key=None, correlation_id=None)
    assert not res.ok and res.status_code == 409


def test_notification_failure_does_not_fail_the_order():
    """The order is committed by the RPC; if enqueueing alerts throws, the order
    must still succeed (alerting is observable/retryable, not order-blocking)."""
    class Boom(FakeSupabase):
        def rpc(self, fn, args):
            if fn == "enqueue_notification":
                raise RuntimeError("outbox down")
            return super().rpc(fn, args)
    db = Boom(rpc_results={"create_order_atomic": RpcResult(
        ok=True, data={"order_ref": "AB-2", "tracking_code": "T", "total_price": 1, "items": []})})
    svc = OrderService(supabase=db, dispatcher=NotificationDispatcher(supabase=db))
    res = svc.create_order(buyer_id="u1", items=[{"listing_id": LISTING_ID, "quantity": 1}],
                           delivery_address="", payment_method="cod",
                           idempotency_key=None, correlation_id=None)
    assert res.ok and res.order_ref == "AB-2"
