# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Browser regression tests for the AgriBridge farmer dashboard.

The real static/index.html runs in headless Chromium against a fixture set:
the Supabase SDK is stubbed for auth only, PostgREST returns the farmer's own
rows, and the Render API answers order-status and profile writes. So every
number on screen is produced by the real farmer code from real row shapes —
including the rule that money comes from payout records, not from orders.

No production data is read or written.
"""
from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
API = "https://agribridge-1-og7a.onrender.com"
NOW = "2026-10-20T09:00:00Z"
FID = "f1"

SUPABASE_STUB = r"""
(function(){
  var listeners = [];
  window.__sb = { session: null };
  window.__sb.emit = function(event, session){
    window.__sb.session = session;
    listeners.forEach(function(cb){ cb(event, session); });
  };
  function chain(){ return new Proxy(function(){}, { get(_, k){
    if(k==='then') return function(res){ res({data:[],error:null}); };
    return chain();
  }, apply(){ return chain(); } }); }
  window.supabase = { createClient: function(){
    return { from: chain, storage: { from: chain }, channel: chain, removeChannel: function(){},
      auth: {
        onAuthStateChange: function(cb){ listeners.push(cb); return {data:{subscription:{unsubscribe:function(){}}}}; },
        getSession: async function(){ return {data:{session: window.__sb.session}}; },
        refreshSession: async function(){ return {data:{session: window.__sb.session}}; },
        signInWithPassword: async function(){ return {data:{},error:{message:'stubbed'}}; },
        signUp: async function(){ return {data:{},error:{message:'stubbed'}}; },
        signInWithOAuth: async function(){ return {data:{},error:{message:'stubbed'}}; },
        updateUser: async function(){ return {data:{},error:null}; },
        signOut: async function(){ window.__sb.emit('SIGNED_OUT', null); return {error:null}; },
        resetPasswordForEmail: async function(){ return {data:{},error:null}; },
        resend: async function(){ return {data:{},error:null}; }
      } };
  } };
})();
"""


def _own(i, name, qty, reserved, price, status, **extra):
    row = {
        "id": i, "farmer_id": FID, "crop_name": name, "category": "staples",
        "quantity_kg": qty, "reserved_qty": reserved, "price_per_kg": price,
        "unit": "kg", "sale_unit": "per kg", "min_order_kg": 1, "discount_pct": 0,
        "district": "Mukono", "description": "From my farm", "is_available": True,
        "is_verified": False, "is_organic": False, "delivery_available": False,
        "payment_terms": None, "stock_status": status, "image_url": None,
        "photo_url": None, "views": 3, "created_at": NOW,
    }
    row.update(extra)
    return row


def _order(i, status, amount, paid, name, **extra):
    row = {
        "id": f"o{i}", "listing_id": None, "farmer_id": FID, "buyer_id": "b1",
        "quantity_kg": amount, "price_per_kg": 3000, "total_amount": amount * 3000,
        "total_price": None, "status": status, "payment_status": paid,
        "payment_method": "mtn_momo", "item_category": "produce",
        "notes": f"Item: {name}", "tracking_code": None, "buyer_phone": "+256700111222",
        "created_at": NOW, "updated_at": NOW,
    }
    row.update(extra)
    return row


def _payout(i, order, gross, pct, net, status, paid_at=None):
    return {
        "id": f"p{i}", "order_id": order, "farmer_id": FID, "gross_amount": gross,
        "commission_pct": pct, "commission_amount": round(gross * pct / 100), "net_amount": net,
        "status": status, "provider": "mtn_momo", "provider_ref": f"RF{i}",
        "created_at": NOW, "paid_at": paid_at,
    }


FARMER = {
    "full_name": "Grace Nakato", "phone": "+256700111222", "district": "Mukono",
    "sub_county": "Seyuka", "village": "Ntenderi", "farm_size_acres": 4,
    "crops_grown": ["Maize", "Beans"], "is_verified": True, "is_premium": False,
    "profile_photo": None, "created_at": "2026-03-01T08:00:00Z",
}

# 201 live · 202 fully reserved · 203 low stock · 204 hidden · 205 live with a photo.
OWN = [
    _own(201, "Tomatoes", 50, 10, 3000, "available"),
    _own(202, "Maize", 100, 100, 1800, "unavailable"),
    _own(203, "Beans", 10, 9, 5200, "low_stock"),
    _own(204, "Matooke", 40, 0, 2500, "available", is_available=False),
    _own(205, "Coffee", 60, 5, 9000, "available", is_organic=True, is_verified=True,
         image_url=API + "/media/listings/coffee.jpg"),
]
OTHER = [_own(301, "Avocado of another farmer", 20, 0, 2000, "available")]
OTHER[0]["farmer_id"] = "f9"

ORDERS = [
    _order(901, "pending", 5, "unpaid", "Tomatoes"),
    _order(902, "confirmed", 12, "paid", "Beans"),
    _order(903, "in_transit", 8, "paid", "Coffee"),
    _order(904, "delivered", 133, "paid", "Coffee", tracking_code="TRK904"),
    _order(905, "cancelled", 4, "unpaid", "Maize"),
    _order(906, "delivered", 12, "paid", "Beans"),
]
PAYOUTS = [
    # 400,000 gross at 5% commission → 380,000 net, already settled.
    _payout(1, "o904", 400000, 5, 380000, "paid", paid_at=NOW),
    # 40,000 gross at 5% → 38,000 net, created but not yet settled.
    _payout(2, "o906", 40000, 5, 38000, "pending"),
]

SC: dict = {}
CALLS: list = []

# A real (tiny) PNG, so the on-device downscale in the listing flow has
# something it can actually decode.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _reset_scenario(**over):
    SC.clear()
    SC["listings"] = json.loads(json.dumps(OWN + OTHER))
    SC["orders"] = json.loads(json.dumps(ORDERS))
    SC["payouts"] = json.loads(json.dumps(PAYOUTS))
    SC["farmer"] = dict(FARMER)
    SC["fail"] = set()
    SC["patch_status"] = None
    SC["patch_body"] = None
    SC.update(over)
    del CALLS[:]


@pytest.fixture(scope="module")
def site():
    import functools
    import http.server

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "static"))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as chromium_error:
            try:
                b = p.chromium.launch(channel="chrome")
            except Exception as chrome_error:
                pytest.skip(f"chromium unavailable: {chromium_error}; Chrome fallback: {chrome_error}")
        yield b
        b.close()


def _handle(route, request):
    import mimetypes

    cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*",
            "access-control-allow-methods": "GET,POST,PATCH,OPTIONS",
            "access-control-expose-headers": "content-range"}
    url = urlparse(request.url)
    host, path, method = url.netloc, url.path, request.method

    if host.startswith("127.0.0.1"):
        rel = unquote(path).lstrip("/") or "index.html"
        target = (ROOT / "static" / rel).resolve()
        if not str(target).startswith(str((ROOT / "static").resolve())) or not target.is_file():
            return route.fulfill(status=404, body="not found")
        return route.fulfill(status=200, path=str(target),
                             content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    if method == "OPTIONS":
        return route.fulfill(status=204, headers=cors, body="")

    if "jsdelivr.net" in host and "supabase" in request.url:
        return route.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB)

    if host.endswith("supabase.co"):
        q = parse_qs(url.query)
        if path.endswith("/auth/v1/settings"):
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body='{"external":{"google":false,"email":true}}')
        table = path.rsplit("/", 1)[-1]
        if table in SC["fail"]:
            return route.fulfill(status=500, headers=cors, content_type="application/json",
                                 body=json.dumps({"error": "fixture outage"}))
        rows: list = []
        if table == "listings":
            if "is_available" in q:
                rows = [row for row in SC["listings"] if row["is_available"]]
            else:
                rows = [row for row in SC["listings"] if row["farmer_id"] == _farm_id(q)]
        elif table == "orders":
            rows = [o for o in SC["orders"] if o.get("farmer_id") == _farm_id(q)]
        elif table == "payouts":
            rows = [p for p in SC["payouts"] if p.get("farmer_id") == _farm_id(q)]
        elif table == "farmers":
            if method in ("PATCH", "POST"):
                CALLS.append((f"farmers-{method.lower()}", request.post_data))
            rows = [SC["farmer"]]
        if q.get("select") == ["count"]:
            return route.fulfill(status=200, headers={**cors, "content-range": f"0-{len(rows) - 1}/{len(rows)}"},
                                 content_type="application/json", body="[]")
        return route.fulfill(status=200, headers=cors, content_type="application/json",
                             body=json.dumps(rows))

    if host.endswith("onrender.com"):
        if path.startswith("/media/"):
            return route.fulfill(status=200, headers=cors, content_type="image/png", body=PNG_1X1)
        if path == "/api/listings" and method == "POST":
            body = json.loads(request.post_data or "{}")
            CALLS.append(("new-listing", body))
            row = _own(501, body.get("crop_name", "New listing"),
                        body.get("quantity_kg", 0), 0, body.get("price_per_kg", 0), "available")
            row["sale_unit"] = body.get("sale_unit")
            row["min_order_kg"] = body.get("min_order_kg")
            row["description"] = body.get("description")
            row["district"] = body.get("district")
            SC["listings"].append(row)
            return route.fulfill(status=201, headers=cors, content_type="application/json",
                                 body=json.dumps({"ok": True, "listing": row}))
        if path.startswith("/api/listings/") and path.endswith("/media"):
            body = json.loads(request.post_data or "{}")
            lid = int(path.split("/")[3])
            CALLS.append(("attach-media", lid, body))
            for row in SC["listings"]:
                if row["id"] == lid:
                    row["image_url"] = body.get("image_url")
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"ok": True}))
        if path.startswith("/api/listings/") and method == "PATCH":
            # The guarded edit endpoint: the fixture keeps the row, so what the
            # farmer sees afterwards is what a successful write actually changed.
            lid = int(path.split("/")[3])
            body = json.loads(request.post_data or "{}")
            CALLS.append(("listing-patch", lid, body))
            if SC.get("patch_status"):
                return route.fulfill(status=SC["patch_status"], headers=cors,
                                     content_type="application/json",
                                     body=json.dumps(SC.get("patch_body") or {"error": "rejected"}))
            saved = None
            for row in SC["listings"]:
                if row["id"] == lid:
                    row.update(body)
                    saved = row
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"ok": True, "listing": saved}))
        if path == "/api/uploads":
            raw = (request.post_data_buffer or b"")[:4000].decode("utf-8", "replace")
            CALLS.append(("upload", raw))
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"url": API + "/media/listings/just-picked.jpg",
                                                  "kind": "listings"}))
        if "/api/orders/" in path and path.endswith("/status"):
            body = json.loads(request.post_data or "{}")
            oid = path.split("/api/orders/")[1].split("/")[0]
            CALLS.append(("order-status", oid, body.get("status")))
            for o in SC["orders"]:
                if o["id"] == oid:
                    o["status"] = body.get("status")
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"ok": True, "status": body.get("status")}))
        if path.endswith("/api/user/profile"):
            CALLS.append(("profile", json.loads(request.post_data or "{}")))
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"ok": True}))
        if path.endswith("/api/user/notifications"):
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body=json.dumps({"notifications": [], "unread": 0}))
        return route.fulfill(status=500, headers=cors, content_type="application/json", body="{}")

    return route.abort()


def _farm_id(query):
    return (query.get("farmer_id") or [""])[0].replace("eq.", "")


def _signin(page, site):
    page.goto(site + "/index.html", wait_until="domcontentloaded")
    page.wait_for_function("typeof sbAuth !== 'undefined' && sbAuth", timeout=10000)
    page.evaluate("try{localStorage.setItem('ab_seen_splash','1')}catch(e){}")
    page.evaluate("enterApp(); hideSplash()")
    page.evaluate("""() => {
      window.__sb.emit('SIGNED_IN', {access_token:'tok', user:{
        id:'f1', email:'grace@example.ug',
        user_metadata:{name:'Grace Nakato', role:'farmer', phone:'+256700111222', district:'Mukono'}}});
    }""")
    page.wait_for_function("typeof getCurrentRole==='function' && getCurrentRole()==='farmer'", timeout=8000)
    page.wait_for_function("!!document.querySelector('#roleHomePanel .fh-sell')", timeout=8000)


@pytest.fixture
def farm(browser, site):
    _reset_scenario()
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              service_workers="block", bypass_csp=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    ctx.route("**/*", _handle)
    _signin(page, site)
    yield page, errors
    ctx.close()


def _tiles(page, selector):
    """Label → value, from the rendered stat tiles (labels are CSS-uppercased)."""
    out = {}
    loc = page.locator(selector + " .fh-stat")
    for i in range(loc.count()):
        parts = [p.strip() for p in loc.nth(i).inner_text().split("\n") if p.strip()]
        if parts:
            out[parts[0].lower()] = parts[1] if len(parts) > 1 else ""
    return out


def _wait_call(kind, timeout_ms=8000, page=None):
    deadline = timeout_ms / 100
    while deadline:
        if any(c[0] == kind for c in CALLS):
            return True
        deadline -= 1
        if page is not None:
            page.wait_for_timeout(100)
    return any(c[0] == kind for c in CALLS)


def test_farmer_nav_is_five_named_tabs_and_sell_is_the_lead_action(farm):
    page, _ = farm
    assert page.locator(".bottom-nav .nav-item span:last-child").all_inner_texts() == \
        ["Home", "Produce", "Orders", "Earnings", "Profile"]
    ids = [el.get_attribute("id") for el in page.locator(".bottom-nav .nav-item").element_handles()]
    assert ids == ["nav-home", "nav-farmer-produce", "nav-farmer-orders", "nav-farmer-earnings", "nav-farmer-profile"]
    # The centre AI orb is replaced by the Home AI card and the AI entry in ☰.
    assert page.locator(".bottom-nav .nav-ai").count() == 0

    sell = page.locator("#roleHomePanel .fh-sell")
    assert sell.count() == 1 and "Sell produce" in sell.inner_text()
    assert sell.bounding_box()["y"] < page.locator("#roleHomePanel .fh-stats").bounding_box()["y"]
    assert sell.bounding_box()["width"] > page.locator("#roleHomePanel .fh-stat").first.bounding_box()["width"]
    assert sell.bounding_box()["height"] >= 44

    # The greeting carries the farmer's own verified given name, never a generic label.
    assert page.locator("#roleHomePanel .fh-name").first.inner_text() == "Grace"
    assert page.inner_text("#roleHomePanel .fh-hi") in ("Good morning", "Good afternoon", "Good evening")
    assert page.locator("#roleHomePanel .fh-av-btn").bounding_box()["height"] >= 44


def test_farmer_home_reports_real_counts_not_estimates(farm):
    page, _ = farm
    tiles = _tiles(page, "#roleHomePanel .fh-stats")
    assert list(tiles) == ["active produce", "new orders", "needs action", "awaiting payout"]
    assert tiles["active produce"] == "3"      # 201 + 205 live, 203 low stock
    assert tiles["new orders"] == "1"
    assert tiles["needs action"] == "3"        # pending + confirmed + in_transit
    # Money is the pending payout net (40,000 − 5%), never an order total.
    assert tiles["awaiting payout"] == "UGX 38,000"

    body = page.inner_text("#roleHomePanel")
    assert "of 5 listings" in body
    assert "1 paid out" in body
    assert "24°C" not in body and "Weather" not in body     # fabricated tile removed
    assert "Income" not in body
    assert "payout records" in body.lower()

    # Home is the farmer's dashboard, not the buyer storefront underneath it.
    hidden = page.evaluate(
        "() => ['buyerHomeTop','bhCatsRow','homeFeaturedHead','homeProdGrid','homeRecentHead']"
        ".filter(id => getComputedStyle(document.getElementById(id)).display !== 'none')"
    )
    assert hidden == []


def test_farmer_home_lists_only_own_orders_and_produce(farm):
    page, _ = farm
    body = page.inner_text("#roleHomePanel")
    assert "Tomatoes" in body and "Coffee" in body
    assert "Avocado of another farmer" not in body
    assert "TRK904" not in body                              # delivered is not "needs action"
    assert page.locator("#roleHomePanel .fh-card-wrap").count() == 3
    assert page.locator("#roleHomePanel .fh-list .fh-card").count() == 3


def test_my_produce_uses_the_real_stock_model_and_the_farmers_own_photos(farm):
    page, _ = farm
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_function("!!document.querySelector('#fp-body .fh-filters')", timeout=8000)
    body = page.inner_text("#fp-body")
    assert "Fully reserved" in body and "Hidden from buyers" in body and "Low stock" in body
    assert page.locator("#fp-body .fh-card").count() == 5

    tabs = page.locator("#fp-body .fh-tab").all_inner_texts()
    assert [t.split(" (")[0] for t in tabs] == ["All", "Live", "Low stock", "Fully reserved", "Hidden"]
    assert [t.split("(")[1].rstrip(")") for t in tabs] == ["5", "2", "1", "1", "1"]

    # Only the farmer's own uploaded photograph is ever shown as a picture.
    imgs = page.locator("#fp-body .fh-thumb img")
    assert imgs.count() == 1
    assert imgs.first.get_attribute("src") == API + "/media/listings/coffee.jpg"
    assert "images.unsplash.com" not in page.inner_html("#fp-body")
    assert "Avocado of another farmer" not in body

    # The stock rule is quantity − reserved, and it is never negative.
    page.evaluate("fhProdFilter('live')")
    assert "40 kg free of 50 kg" in page.inner_text("#fp-body")
    page.evaluate("fhProdFilter('sold')")
    assert "0 kg free of 100 kg" in page.inner_text("#fp-body")


def test_orders_expose_only_legal_transitions(farm):
    page, _ = farm
    page.evaluate("showPage('farmer-orders')")
    page.wait_for_function("!!document.querySelector('#fo-body .fh-tab')", timeout=8000)
    page.evaluate("fhOrderFilter('all')")
    rows = page.locator("#fo-body .fh-card-wrap")
    assert rows.count() == 6

    pending = rows.filter(has_text="New order").first
    assert pending.locator(".fh-acts button").all_inner_texts() == ["Confirm order", "Reject"]
    assert "Awaiting payment" in pending.inner_text()
    assert "MTN MoMo" in pending.inner_text()
    assert "UGX 15,000" in pending.inner_text()
    # The accepted privacy policy keeps the two sides' numbers private until an
    # order actually requires contact, so a request the farmer has not accepted
    # must not hand out the buyer's phone number.
    assert "+256700111222" not in pending.inner_text()

    accepted = rows.filter(has_text="#o902").first
    assert "+256700111222" in accepted.inner_text()
    assert accepted.locator(".fh-acts button").all_inner_texts() == ["Mark in transit", "Cancel order"]

    moving = rows.filter(has_text="On the way").first
    assert moving.locator(".fh-acts button").all_inner_texts() == ["Mark delivered"]
    # Selected by its server tracking code: "Delivered" also appears on the
    # in-transit row's "Mark delivered" button.
    delivered = rows.filter(has_text="TRK904").first
    assert delivered.locator("button").count() == 0
    assert "A payout record appears" in delivered.inner_text()
    cancelled = rows.filter(has_text="Cancelled").first
    assert cancelled.locator("button").count() == 0
    assert "returned to your listing" in cancelled.inner_text()

    # Thumb-sized targets on a phone: the filter row and the contact link are the
    # two controls a farmer uses constantly, so they must not be 32px links.
    # 0.1px slack because Chromium lays these out at 43.99998 CSS px; the rule is
    # min-height:44px and that noise is not a smaller target.
    tabs = page.locator("#fo-body .fh-tab")
    assert tabs.count() >= 3
    for i in range(tabs.count()):
        assert tabs.nth(i).bounding_box()["height"] >= 43.9
    contact = accepted.locator(".fh-more.fh-open")
    assert contact.count() == 1
    assert contact.first.bounding_box()["height"] >= 43.9


def test_confirming_an_order_goes_through_the_api_and_reloads(farm):
    page, errors = farm
    page.evaluate("showPage('farmer-orders')")
    page.wait_for_function("!!document.querySelector('#fo-body .fh-tab')", timeout=8000)
    assert ("order-status", "o901", "confirmed") not in CALLS
    # The default view is "Needs action": pending, confirmed and in-transit.
    assert page.locator("#fo-body .fh-card-wrap").count() == 3

    page.evaluate("fhAct('o901','confirmed',null,'Confirm order')")
    assert _wait_call("order-status", page=page)
    page.wait_for_function(
        "[...document.querySelectorAll('#fo-body .fh-card-wrap')]"
        ".filter(r => /New order/.test(r.textContent)).length === 0", timeout=8000)
    page.evaluate("fhOrderFilter('all')")
    page.wait_for_function(
        "document.querySelectorAll('#fo-body .fh-card-wrap').length === 6", timeout=8000)
    row901 = page.locator("#fo-body .fh-card-wrap").filter(has_text="#o901").first
    assert "Confirmed" in row901.inner_text()
    # The actions shown are the ones the server state now allows.
    assert row901.locator(".fh-acts button").all_inner_texts() == ["Mark in transit", "Cancel order"]
    # Accepting the order is what releases the buyer's contact number.
    assert "+256700111222" in row901.inner_text()
    # pending → confirmed keeps one action available, so "Needs action" stays at 3.
    page.evaluate("showPage('home')")
    page.wait_for_function("!!document.querySelector('#roleHomePanel .fh-stats')", timeout=8000)
    assert _tiles(page, "#roleHomePanel .fh-stats")["needs action"] == "3"
    assert _tiles(page, "#roleHomePanel .fh-stats")["new orders"] == "0"
    assert errors == [], errors


def test_cancelling_needs_an_explicit_confirmation(farm):
    page, _ = farm
    page.evaluate("showPage('farmer-orders')")
    page.wait_for_function("!!document.querySelector('#fo-body .fh-tab')", timeout=8000)
    page.evaluate("fhOrderFilter('pending')")
    assert page.locator("#fo-body .fh-card-wrap").count() == 1
    # Without a confirming dialog the order must not move.
    del CALLS[:]
    page.evaluate("() => { window.confirm = () => false; fhAct('o901','cancelled',null,'Reject'); }")
    page.wait_for_timeout(400)
    assert not any(c[0] == "order-status" for c in CALLS)
    page.evaluate("() => { window.confirm = () => true; fhAct('o901','cancelled',null,'Reject'); }")
    assert _wait_call("order-status", page=page)


def test_earnings_come_from_payout_records_only(farm):
    page, _ = farm
    page.evaluate("showPage('farmer-earnings')")
    page.wait_for_function("!!document.querySelector('#fe-body .fh-sum')", timeout=8000)
    tiles = _tiles(page, "#fe-body .fh-sum")
    assert tiles["awaiting payout"] == "UGX 38,000"
    assert tiles["paid out"] == "UGX 380,000"
    assert tiles["completed sales"] == "2"
    assert tiles["order value"] == "UGX 440,000"

    body = page.inner_text("#fe-body")
    assert "1 record" in body and "UGX 22,000" in body      # 20,000 + 2,000 commission
    assert "5%" in body
    # Transport is never invented: no delivery fee exists on an order or payout.
    low = body.lower()
    assert "transport charges are not included" in low
    assert "does not store a delivery fee" in low
    history = page.locator("#fe-body .fh-list .fh-card")
    assert history.count() == 2
    assert "Coffee" in history.first.inner_text()
    assert "mtn_momo · RF1" in history.first.inner_text()


def test_profile_splits_editable_from_verified_details(farm):
    page, _ = farm
    page.evaluate("showPage('farmer-profile')")
    page.wait_for_function("!!document.getElementById('fprName')", timeout=8000)
    assert page.input_value("#fprName") == "Grace Nakato"
    assert page.input_value("#fprPhone") == "+256700111222"
    assert page.input_value("#fprDistrict") == "Mukono"
    assert page.locator("#fprName").bounding_box()["height"] >= 44

    ro = page.locator("#fpr-body input[readonly]").evaluate_all("els => els.map(e => e.value)")
    assert "Verified" in ro
    assert "Seyuka · Ntenderi" in ro
    assert "4 acres" in ro
    assert "Maize, Beans" in ro

    del CALLS[:]
    page.fill("#fprName", "Grace A. Nakato")
    page.click("#fprSave")
    assert _wait_call("profile", page=page)
    sent = [c[1] for c in CALLS if c[0] == "profile"][-1]
    assert sent == {"full_name": "Grace A. Nakato", "phone": "+256700111222", "district": "Mukono"}
    page.wait_for_function("document.getElementById('toast').classList.contains('show')", timeout=6000)
    assert "Profile saved" in page.inner_text("#toast")


def test_empty_and_error_states_offer_a_next_action(farm):
    page, _ = farm
    _reset_scenario(listings=[], orders=[], payouts=[])
    page.evaluate("farmerDataClear(); loadFarmerHome()")
    page.wait_for_function("!!document.querySelector('#roleHomePanel .fh-empty')", timeout=8000)
    body = page.inner_text("#roleHomePanel")
    assert "You have not listed anything yet" in body
    assert "No orders waiting" in body
    assert "No payout record yet" in body
    assert page.locator("#roleHomePanel .fh-sell").count() == 1

    _reset_scenario(fail={"listings", "orders", "payouts"})
    page.evaluate("farmerDataClear(); loadFarmerHome()")
    page.wait_for_function("!!document.querySelector('#roleHomePanel .fh-err')", timeout=8000)
    assert page.locator("#roleHomePanel .fh-err button").first.inner_text() == "Try again"
    # A failed read must never be reported as "you have nothing".
    assert "You have not listed anything yet" not in page.inner_text("#roleHomePanel")

    page.evaluate("showPage('farmer-produce')")
    page.wait_for_function("!!document.querySelector('#fp-body .fh-err')", timeout=8000)
    assert page.locator("#fp-body .fh-err button").count() == 1


def test_publishing_produce_saves_the_listing_then_attaches_the_photo(farm):
    page, errors = farm
    del CALLS[:]
    page.evaluate("openListProduceModal()")
    page.wait_for_function("document.getElementById('listProduceModal').classList.contains('open')", timeout=8000)
    page.select_option("#lpCrop", "Tomatoes")
    page.select_option("#lpCat", "vegetables")
    page.fill("#lpQty", "30")
    page.fill("#lpPrice", "6500")
    page.set_input_files("#lpPhoto", {"name": "harvest.png", "mimeType": "image/png", "buffer": PNG_1X1})
    # Two taps while the first POST is still in flight must post one listing, not two.
    page.evaluate("submitListProduce(); submitListProduce()")

    assert _wait_call("new-listing", page=page)
    assert len([c for c in CALLS if c[0] == "new-listing"]) == 1
    sent = [c[1] for c in CALLS if c[0] == "new-listing"][-1]
    assert sent["crop_name"] == "Tomatoes" and sent["category"] == "vegetables"
    assert sent["quantity_kg"] == 30 and sent["price_per_kg"] == 6500
    assert sent["district"] == "Mukono"            # reused from the verified profile
    # The listing is posted without waiting for the upload, so a slow photo can
    # never leave the farmer with nothing.
    assert sent["image_url"] is None

    assert _wait_call("upload", page=page)
    up = [c for c in CALLS if c[0] == "upload"][-1][1]
    assert 'filename="listing.jpg"' in up        # downscaled on the device first
    assert "listings" in up                       # uploaded into the listing bucket
    assert _wait_call("attach-media", page=page)
    patch = [c for c in CALLS if c[0] == "attach-media"][-1]
    assert patch[1] == 501 and patch[2]["image_url"] == API + "/media/listings/just-picked.jpg"

    # The new listing lands in My Produce without a manual refresh.
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_function("!!document.querySelector('#fp-body .fh-tab')", timeout=8000)
    page.evaluate("fhProdFilter('all')")
    body = page.inner_text("#fp-body")
    assert "30 kg free of 30 kg" in body
    assert page.locator("#fp-body .fh-card img").count() == 2   # own photos only
    # A posted listing must not come back as an unsent draft on the next open.
    assert not page.evaluate("!!localStorage.getItem('ab_listing_draft')")
    assert errors == [], errors


def test_every_farmer_screen_stays_reachable_and_error_free(farm):
    page, errors = farm
    for name in ("farmer-produce", "farmer-orders", "farmer-earnings", "farmer-profile", "home"):
        page.evaluate(f"showPage('{name}')")
        page.wait_for_timeout(300)
        assert page.evaluate("(document.querySelector('.page.active')||{}).id") == f"page-{name}"
        assert page.locator(".bottom-nav .nav-item").count() == 5

    # A nav refresh (avatar update, profile save) must not eject the farmer.
    page.evaluate("showPage('farmer-orders')")
    page.evaluate("applyRoleUI()")
    assert page.evaluate("(document.querySelector('.page.active')||{}).id") == "page-farmer-orders"

    page.evaluate("openMore()")
    page.wait_for_selector("#moreSheet .more-grid button", timeout=4000)
    more = page.locator("#moreSheet .more-grid button").all_inner_texts()
    assert any("Market" in m for m in more) and any("Prices" in m for m in more)
    assert any("AI Assistant" in m for m in more)
    assert any("Finance" in m for m in more)

    # Section links are real buttons, so a keyboard user can reach every screen.
    page.evaluate("showPage('home')")
    page.wait_for_function("!!document.querySelector('#roleHomePanel .fh-sec-top')", timeout=8000)
    assert page.locator("#roleHomePanel a[onclick]").count() == 0
    shortcuts = page.locator("#roleHomePanel .fh-sec-top button")
    assert shortcuts.count() == 3
    for i in range(3):
        assert shortcuts.nth(i).bounding_box()["height"] >= 44

    assert errors == [], errors


# ══ Stage F2: the listing form, the photo, and managing a live listing ═══════


def test_a_chosen_photo_is_previewed_then_replaced_or_removed_before_posting(farm):
    page, errors = farm
    page.evaluate("openListProduceModal()")
    page.wait_for_selector("#listProduceModal.open", timeout=8000)
    assert "Take or choose a photo" in page.inner_text("#lpPhotoBox")

    page.set_input_files("#lpPhoto", {"name": "harvest.png", "mimeType": "image/png", "buffer": PNG_1X1})
    page.wait_for_selector("#lpPhotoBox .lp-shot img", timeout=4000)
    assert page.locator("#lpPhotoBox .lp-shot").count() == 1
    assert "harvest.png" in page.inner_text("#lpPhotoBox")
    # The farmer is told what the picture will do, not just that a file was picked.
    assert "This is the photo buyers will see" in page.inner_text("#lpPhotoHint")

    # Replacing swaps the one preview instead of stacking a second one.
    page.set_input_files("#lpPhoto", {"name": "second.png", "mimeType": "image/png", "buffer": PNG_1X1})
    page.wait_for_function(
        "document.querySelector('#lpPhotoBox .lp-name').textContent === 'second.png'", timeout=4000)
    assert page.locator("#lpPhotoBox .lp-shot").count() == 1

    # Removing puts the picker back and clears the file input, so a removed
    # photo can never be posted by accident.
    page.click("#lpPhotoBox .lp-acts button:nth-child(2)")
    page.wait_for_selector("#lpPhotoBox .lp-pick-btn", timeout=4000)
    assert page.evaluate("document.getElementById('lpPhoto').value") == ""
    assert "A real photo of what you are actually selling" in page.inner_text("#lpPhotoHint")
    assert errors == [], errors


def test_an_unusable_photo_is_refused_with_a_reason_and_never_uploaded(farm):
    page, errors = farm
    del CALLS[:]
    page.evaluate("openListProduceModal()")
    page.wait_for_selector("#listProduceModal.open", timeout=8000)

    page.set_input_files("#lpPhoto", {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"not an image"})
    page.wait_for_function(
        "document.getElementById('lpPhotoHint').textContent.indexOf('not a photo') >= 0", timeout=4000)
    assert "not a photo" in page.inner_text("#lpPhotoHint")
    assert page.locator("#lpPhotoBox .lp-pick-btn").count() == 1   # nothing was accepted

    # Oversize is refused on the phone, before a 5MB upload eats a data bundle.
    page.set_input_files("#lpPhoto", {"name": "huge.png", "mimeType": "image/png",
                                     "buffer": b"0" * (5 * 1024 * 1024 + 4096)})
    page.wait_for_function("document.getElementById('lpPhotoHint').textContent.indexOf('under 5MB') >= 0", timeout=4000)
    hint = page.inner_text("#lpPhotoHint")
    assert "5.0 MB" in hint and "under 5MB" in hint
    assert not any(c[0] == "upload" for c in CALLS)
    assert errors == [], errors


def test_an_unsent_listing_form_is_put_back_when_the_farmer_returns(farm):
    page, errors = farm
    page.evaluate("openListProduceModal()")
    page.wait_for_selector("#listProduceModal.open", timeout=8000)
    page.fill("#lpQty", "45")
    page.fill("#lpPrice", "2100")
    page.fill("#lpDesc", "Grade A, picked Tuesday")
    page.select_option("#lpCat", "fruits")

    # Interrupted before posting (dropped network, a phone call, the app closing).
    page.evaluate("closeModal('listProduceModal')")
    assert page.evaluate("!!localStorage.getItem('ab_listing_draft')")

    page.evaluate("openListProduceModal()")
    page.wait_for_selector("#listProduceModal.open", timeout=8000)
    assert page.input_value("#lpQty") == "45"
    assert page.input_value("#lpPrice") == "2100"
    assert page.input_value("#lpDesc") == "Grade A, picked Tuesday"
    assert page.evaluate("document.getElementById('lpCat').value") == "fruits"
    assert "put back the details" in page.inner_text("#lpPhotoHint")
    # Only the farmer's own typed text is kept — never a session or payment data.
    draft = page.evaluate("JSON.parse(localStorage.getItem('ab_listing_draft'))")
    assert set(draft) <= {"lpCrop", "lpCat", "lpQty", "lpPrice", "lpDistrict", "lpDesc",
                          "lpSaleUnit", "lpMinOrder", "lpDelivery", "lpPayMomo", "lpPayCash", "lpPayBank"}
    assert errors == [], errors


def test_photo_progress_and_failure_are_reported_on_the_listing_row(farm):
    page, errors = farm
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_selector("#fp-body .fh-tab", timeout=8000)

    # The listing is already saved and the form closed, so progress belongs on
    # the row the farmer is looking at.
    page.evaluate("lpMediaStart(201); lpMediaProgress(30/70); renderFarmerProduce()")
    pill = page.locator("#fp-body [data-lp-media='201']")
    assert "Adding photo" in pill.inner_text() and "43%" in pill.inner_text()

    # A failure must stop claiming a photo is still on its way.
    page.evaluate("lpMediaEnd(true); lpMediaProgress(0.6); renderFarmerProduce()")
    txt = page.locator("#fp-body [data-lp-media='201']").inner_text()
    assert "Photo not added" in txt and "Adding photo" not in txt

    page.evaluate("lpMediaEnd(false)")
    assert page.locator("#fp-body [data-lp-media]").count() == 0
    assert errors == [], errors


def test_the_edit_sheet_shows_real_numbers_and_blocks_a_quantity_below_reserved(farm):
    page, errors = farm
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_selector("#fp-body .fh-tab", timeout=8000)
    del CALLS[:]

    page.evaluate("fhOpenEdit(201)")   # Tomatoes: 50 kg listed, 10 kg already reserved
    page.wait_for_selector("#listingEditModal.open", timeout=8000)
    assert page.evaluate("document.getElementById('leCrop').value") == "Tomatoes"
    assert page.input_value("#leQty") == "50"
    assert page.input_value("#lePrice") == "3000"
    assert page.evaluate("document.getElementById('leUnitLabel').textContent") == "kg"
    assert "10 kg of this listing is already reserved" in page.inner_text("#leReservedLine")
    assert "Buyers can order 40 kg of the 50 kg" in page.inner_text("#leAvail")
    # Per-unit and total are spelled out, so a price is never a surprise.
    total = page.inner_text("#leTotal")
    assert "UGX 3,000 each kg" in total and "UGX 150,000" in total

    for fid in ("leCrop", "leQty", "lePrice", "lePhoto"):
        assert page.locator(f"label[for='{fid}']").count() == 1
    # Thumb-sized on a phone: 0.1px slack because Chromium lays these out at
    # 43.99998 CSS px against a min-height:44px rule.
    assert page.locator("#leSave").bounding_box()["height"] >= 43.9
    assert page.locator("#lePhotoBox .lp-pick-btn").bounding_box()["height"] >= 43.9

    # Under the reserved floor the sheet refuses locally — no wasted request,
    # and the sheet stays open so the farmer can correct the number.
    page.fill("#leQty", "5")
    assert "Buyers can order 0 kg" in page.inner_text("#leAvail")
    page.click("#leSave")
    page.wait_for_function("document.getElementById('leErr').textContent.length > 0", timeout=4000)
    err = page.inner_text("#leErr")
    assert "reserved by orders" in err and "cannot go below" in err
    assert not any(c[0] == "listing-patch" for c in CALLS)
    assert page.evaluate("document.getElementById('listingEditModal').classList.contains('open')")
    assert errors == [], errors


def test_saving_an_edit_writes_once_through_the_guarded_api(farm):
    page, errors = farm
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_selector("#fp-body .fh-tab", timeout=8000)
    page.evaluate("fhOpenEdit(201)")
    page.wait_for_selector("#listingEditModal.open", timeout=8000)
    page.fill("#leQty", "60")
    page.fill("#lePrice", "2900")
    del CALLS[:]

    # Two taps while the PATCH is in flight must write once, not twice.
    page.evaluate("fhSaveEdit(); fhSaveEdit()")
    assert _wait_call("listing-patch", page=page)
    patches = [c for c in CALLS if c[0] == "listing-patch"]
    assert len(patches) == 1
    assert patches[0][1] == 201
    # Only the three editable fields travel; reserved_qty and stock_status are
    # the server's to decide, and is_available is untouched by a price edit.
    assert patches[0][2] == {"crop_name": "Tomatoes", "quantity_kg": 60, "price_per_kg": 2900}

    page.wait_for_function("!document.getElementById('listingEditModal').classList.contains('open')", timeout=8000)
    assert "Saving" not in page.inner_text("#leSave")      # the button is usable again
    row = page.locator("#fp-body .fh-card", has_text="Tomatoes").filter(has_text="50 kg free of 60 kg")
    row.wait_for(timeout=8000)
    assert "UGX 2,900" in row.inner_text()
    assert errors == [], errors


def test_an_order_that_arrived_mid_edit_is_explained_and_the_floor_updates(farm):
    page, errors = farm
    # The server kept the old quantity because a reservation landed first; it
    # returns the real reserved figure rather than leaving the farmer to guess.
    SC["patch_status"] = 409
    SC["patch_body"] = {"error": "An order came in while you were editing, so the stock figure changed. "
                                 "Reload the listing and try again.", "reserved_qty": 35}
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_selector("#fp-body .fh-tab", timeout=8000)
    page.evaluate("fhOpenEdit(201)")
    page.wait_for_selector("#listingEditModal.open", timeout=8000)
    page.fill("#leQty", "40")
    del CALLS[:]
    page.click("#leSave")

    page.wait_for_function("document.getElementById('leErr').textContent.length > 0", timeout=8000)
    assert "An order came in while you were editing" in page.inner_text("#leErr")
    assert "35 kg of this listing is now reserved" in page.inner_text("#leReservedLine")
    assert "Buyers can order 5 kg of the 40 kg" in page.inner_text("#leAvail")
    # Nothing looks saved: the sheet stays open and the row still shows the old stock.
    assert page.evaluate("document.getElementById('listingEditModal').classList.contains('open')")
    assert "40 kg free of 50 kg" in page.locator("#fp-body .fh-card", has_text="Tomatoes").inner_text()
    assert len([c for c in CALLS if c[0] == "listing-patch"]) == 1
    assert errors == [], errors


def test_hiding_and_showing_a_listing_is_a_server_write_the_row_reflects(farm):
    page, errors = farm
    page.evaluate("showPage('farmer-produce')")
    page.wait_for_selector("#fp-body .fh-tab", timeout=8000)
    page.evaluate("fhProdFilter('all')")
    row = page.locator("#fp-body .fh-card", has_text="Tomatoes")
    row.wait_for(timeout=8000)
    del CALLS[:]

    page.evaluate("fhSetVisible(201, false)")
    assert _wait_call("listing-patch", page=page)
    # Pausing touches visibility only: no stock, price or status rewrite.
    assert [c for c in CALLS if c[0] == "listing-patch"][-1][2] == {"is_available": False}
    page.locator("#fp-body .fh-card", has_text="Tomatoes").filter(has_text="Hidden from buyers").wait_for(timeout=8000)
    assert "Show to buyers" in row.inner_text()
    assert "The marketplace does not show this listing" in row.inner_text()

    del CALLS[:]
    page.evaluate("fhSetVisible(201, true)")
    assert _wait_call("listing-patch", page=page)
    assert [c for c in CALLS if c[0] == "listing-patch"][-1][2] == {"is_available": True}
    page.locator("#fp-body .fh-card", has_text="Tomatoes").filter(has_text="Live").wait_for(timeout=8000)
    assert "Hide from buyers" in row.inner_text()
    assert errors == [], errors


def test_the_edit_sheet_prices_the_way_buyers_see_them(farm):
    page, errors = farm
    # A running discount changes what a buyer pays per unit, so the total the
    # farmer is shown must be built from the discounted figure, not the list price.
    for row in SC["listings"]:
        if row["id"] == 201:
            row["discount_pct"] = 20
    page.evaluate("farmerDataClear(); showPage('farmer-produce')")
    page.locator("#fp-body .fh-card", has_text="Tomatoes").filter(has_text="UGX 2,400").wait_for(timeout=8000)

    page.evaluate("fhOpenEdit(201)")
    page.wait_for_selector("#listingEditModal.open", timeout=8000)
    assert page.input_value("#lePrice") == "3000"      # the list price is what they edit
    total = page.inner_text("#leTotal")
    assert "UGX 2,400 each kg after your 20% discount" in total
    assert "UGX 120,000" in total                      # 2,400 × 50 kg
    assert errors == [], errors

