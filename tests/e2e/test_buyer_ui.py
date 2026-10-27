# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Browser regression tests for the buyer home, product detail and cart.

The real static/index.html runs in headless Chromium. Only the outside world is
faked: the Supabase JS SDK (auth only), PostgREST responses (a small fixture
set), and unrelated API hosts. Prices, stock, minimums, ratings and totals are
therefore rendered by the real buyer code, not by mocks of it.

No production data is read or written; every request is answered locally.
"""
from __future__ import annotations

import functools
import http.server
import json
import mimetypes
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]

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

NOW = "2026-10-20T09:00:00Z"


def _listing(i, name, category, price, qty, farmer_id, farmer, **extra):
    row = {
        "id": i, "farmer_id": farmer_id, "crop_name": name, "category": category,
        "quantity_kg": qty, "reserved_qty": 0, "price_per_kg": price, "unit": "kg",
        "district": "Kampala", "is_organic": False, "is_verified": False,
        "description": "Fixture produce", "discount_pct": 0, "sale_unit": "per kg",
        "min_order_kg": 1, "delivery_available": True, "payment_terms": None,
        "image_url": None, "created_at": NOW,
        "farmers": {"full_name": farmer, "is_verified": extra.pop("farmer_verified", False)},
    }
    row.update(extra)
    return row


LISTINGS = [
    _listing(101, "Tomatoes", "vegetables", 3000, 50, "f1", "Grace Nakato",
             is_organic=True, is_verified=True, farmer_verified=True, description="Fresh tomatoes"),
    _listing(102, "Matooke", "staples", 2500, 80, "f2", "John Okello"),
    _listing(103, "Mangoes", "fruits", 4000, 6, "f3", "Sarah Auma", min_order_kg=2),
    _listing(104, "Maize", "grains", 1800, 300, "f2", "John Okello", description="None"),
    _listing(105, "Beans", "grains", 5000, 4, "f4", "Peter Wanyama", min_order_kg=5),
    _listing(106, "Pumpkin", "vegetables", 1500, 40, "f3", "Sarah Auma"),
    _listing(107, "Cocoa", "cash crops", 9000, 25, "f4", "Peter Wanyama", discount_pct=10),
    _listing(108, "Sold Out Maize", "grains", 1700, 0, "f2", "John Okello"),
]

RATINGS = [
    {"farmer_id": "f1", "avg_rating": 4.5, "review_count": 2},
    {"farmer_id": "f3", "avg_rating": 5.0, "review_count": 1},
]


@pytest.fixture(scope="module")
def site():
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


def _supabase_rest(route, request):
    cors = {
        "access-control-allow-origin": "*",
        "access-control-allow-headers": "*",
        "access-control-expose-headers": "content-range",
    }
    path = urlparse(request.url).path
    if path.endswith("/auth/v1/settings"):
        return route.fulfill(status=200, headers=cors, content_type="application/json",
                             body='{"external":{"google":false,"email":true}}')
    if path.endswith("/rest/v1/listings"):
        query = parse_qs(urlparse(request.url).query)
        if query.get("select") == ["count"]:
            return route.fulfill(status=200, headers={**cors, "content-range": "0-7/8"},
                                 content_type="application/json", body="[]")
        return route.fulfill(status=200, headers=cors, content_type="application/json",
                             body=json.dumps(LISTINGS))
    if path.endswith("/rest/v1/farmer_ratings"):
        return route.fulfill(status=200, headers=cors, content_type="application/json",
                             body=json.dumps(RATINGS))
    if path.endswith("/rest/v1/farmers") or path.endswith("/rest/v1/animal_listings"):
        return route.fulfill(status=200, headers={**cors, "content-range": "0-0/1"},
                             content_type="application/json", body="[]")
    return route.fulfill(status=200, headers=cors, content_type="application/json", body="[]")


@pytest.fixture
def env(browser, site):
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              service_workers="block", bypass_csp=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())

    ctx.route("**/npm/@supabase/**",
              lambda r, q: r.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB))
    ctx.route("**/supabase-js*",
              lambda r, q: r.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB))
    ctx.route("https://*.supabase.co/**", _supabase_rest)

    def other(route, request):
        host = urlparse(request.url).netloc
        if host.startswith("127.0.0.1"):
            rel = unquote(urlparse(request.url).path).lstrip("/") or "index.html"
            target = (ROOT / "static" / rel).resolve()
            if not str(target).startswith(str((ROOT / "static").resolve())):
                return route.fulfill(status=404, body="not found")
            if target.is_file():
                return route.fulfill(status=200, path=str(target),
                                     content_type=mimetypes.guess_type(target.name)[0]
                                     or "application/octet-stream")
            return route.fulfill(status=404, body="not found")
        if host.endswith("supabase.co"):
            return route.fallback()
        if "jsdelivr" in host and "supabase" in request.url:
            return route.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB)
        route.abort()

    ctx.route("**/*", other)
    _enter(page, site)
    yield page, errors
    ctx.close()


def _enter(page, site):
    page.goto(site + "/index.html", wait_until="domcontentloaded")
    page.wait_for_function("typeof sbAuth !== 'undefined' && sbAuth", timeout=10000)
    page.evaluate("try{localStorage.setItem('ab_seen_splash','1')}catch(e){}")
    page.evaluate("enterApp(); hideSplash()")
    page.wait_for_function(
        "document.querySelectorAll('#homeProdGrid .prod-card').length > 0", timeout=10000)


def test_home_renders_listing_data_and_only_real_ratings(env):
    page, _ = env
    cards = page.locator("#homeProdGrid .prod-card")
    assert cards.count() == 6
    names = cards.locator(".prod-card-name").all_inner_texts()
    assert "Tomatoes" in names
    assert "Sold Out Maize" not in names           # out-of-stock listings leave the buyer feed

    tomatoes = cards.filter(has_text="Tomatoes").first
    assert "Grace Nakato" in tomatoes.inner_text()
    assert "★ 4.5" in tomatoes.inner_text()        # from farmer_ratings, not a constant
    # A farmer with no rating must not show stars.
    maize = cards.filter(has_text="Maize").first
    assert "★" not in maize.inner_text()
    assert tomatoes.get_attribute("role") == "link" and tomatoes.get_attribute("tabindex") == "0"


def test_signed_in_buyer_lands_on_the_shopping_home(env):
    page, _ = env
    page.evaluate("""() => {
      const user = {id:'buyer-1', email:'buyer@example.com',
        user_metadata:{name:'Buyer', role:'vendor', phone:'', district:'Kampala'}};
      window.__sb.emit('SIGNED_IN', {access_token:'tok', user:user});
    }""")
    page.wait_for_function("(document.querySelector('.page.active')||{}).id === 'page-home'", timeout=5000)
    assert page.evaluate("getCurrentRole()") == "vendor"
    assert page.locator("#homeProdGrid .prod-card").count() == 6


def test_detail_shows_real_listing_facts_and_disables_out_of_stock(env):
    page, _ = env
    page.evaluate("openProductDetail(101)")
    page.wait_for_function("document.getElementById('pdName').textContent === 'Tomatoes'")
    assert page.inner_text("#pdDesc") == "Fresh tomatoes"
    assert page.inner_text("#pdDeliveryInfo") == "Arranged after the order is confirmed"
    assert page.inner_text("#pdPayInfo") == "Chosen at checkout"
    assert page.inner_text("#pdRatingCt") == "(2 reviews)"
    assert page.is_enabled("#pdAddBtn") and page.is_enabled("#pdBuyBtn")

    # A late guest role refresh must not eject the buyer from the detail page.
    page.evaluate("applyRoleUI()")
    assert page.evaluate("(document.querySelector('.page.active')||{}).id") == "page-product-detail"

    page.evaluate("openProductDetail(108)")
    page.wait_for_function("document.getElementById('pdName').textContent === 'Sold Out Maize'")
    assert page.get_attribute("#pdAddBtn", "disabled") is not None
    assert page.get_attribute("#pdBuyBtn", "disabled") is not None
    assert page.is_visible("#pdOutNote")

    page.evaluate("openProductDetail(104)")       # stored description is the literal "None"
    page.wait_for_function("document.getElementById('pdName').textContent === 'Maize'")
    assert page.inner_text("#pdDesc") == "No description provided by the farmer yet."


def test_cart_totals_are_honest_and_survive_a_reload(env, site):
    page, _ = env
    page.evaluate("openProductDetail(107)")       # 10% discount: 8,100/kg
    page.wait_for_function("document.getElementById('pdName').textContent === 'Cocoa'")
    page.click("#pdQtyPlus")
    page.click("#pdAddBtn")
    page.wait_for_function("typeof basketItems !== 'undefined' && basketItems.length === 1")

    page.evaluate("showPage('cart')")
    page.wait_for_function("document.getElementById('cartPageSummary').style.display === 'block'")
    assert page.inner_text("#cartDelivery") == "Arranged after confirmation"
    assert page.inner_text("#cartTotal") == page.inner_text("#cartSubtotal") == "UGX 16,200"
    assert "server" in page.inner_text("#cartNotices").lower()
    assert page.is_enabled("#cartCheckoutBtn")
    assert page.locator("#cartPageList .cart-item").count() == 1

    _enter(page, site)
    page.wait_for_timeout(500)
    assert page.evaluate("JSON.parse(localStorage.getItem('ab_basket')||'[]').length") == 1
    page.evaluate("showPage('cart')")
    assert page.locator("#cartPageList .cart-item").count() == 1
    assert page.inner_text("#cartPageCount").startswith("2 ")


def test_search_and_category_filter_use_the_same_listing_data(env):
    page, _ = env
    page.fill("#bhSearchInput", "Tomatoes")
    page.wait_for_selector("#bhSearchResults .bh-sr-row")
    assert "Grace Nakato" in page.inner_text("#bhSearchResults .bh-sr-row")
    page.evaluate("document.querySelector('#bhSearchResults .bh-sr-row').click()")
    page.wait_for_function("document.getElementById('pdName').textContent === 'Tomatoes'")

    page.evaluate("showPage('home')")
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('#bhCatsRow .bh-cat')].find(x => x.textContent.includes('Veggies'));
      b.click();
    }""")
    # A late home fetch must not silently reset a category the buyer already picked.
    page.evaluate("() => loadHomeStats()")
    names = page.locator("#homeProdGrid .prod-card-name").all_inner_texts()
    assert names and set(names).issubset({"Tomatoes", "Pumpkin"})
    assert page.inner_text("#homeFeaturedTitle") == "Vegetables"


def test_no_javascript_errors_in_the_buyer_flows(env):
    page, errors = env
    page.evaluate("openProductDetail(101)")
    page.evaluate("showPage('cart')")
    page.evaluate("showPage('home')")
    page.wait_for_timeout(300)
    assert errors == [], errors
