# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Browser tests of the real sign-up, login-review, settings and privacy UI.

The REAL static/index.html runs in headless Chromium. Only the outside world is
faked: the Supabase JS library (a stub), Supabase REST (empty lists), and the API
host, which is answered by the REAL Flask app with an in-memory database. So the
page and the backend are exercised together; nothing is asserted from mocks of
the code under test. Skipped automatically if Playwright/Chromium is missing.
"""
from __future__ import annotations

import functools
import http.server
import mimetypes
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from test_legal import LegalDb  # noqa: E402

import app as app_mod  # noqa: E402
from services.legal import LegalService  # noqa: E402

API_HOST = "agribridge-1-og7a.onrender.com"

SUPABASE_STUB = r"""
(function(){
  var listeners = [];
  var state = window.__sb = { session: null, signups: [], nextSignupSession: false, loginMeta: null,
                              oauth: null, profileUpdate: null };
  function chain(){ var p = new Proxy(function(){}, { get(_, k){ if(k==='then') return function(res){ res({data:[],error:null}); }; return chain(); }, apply(){ return chain(); } }); return p; }
  state.emit = function(event, session){ state.session = session; listeners.forEach(function(cb){ cb(event, session); }); };
  window.supabase = { createClient: function(){
    return { from: chain, storage: { from: chain }, channel: chain, removeChannel: function(){},
      auth: {
        onAuthStateChange: function(cb){ listeners.push(cb); return { data: { subscription: { unsubscribe: function(){} } } }; },
        getSession: async function(){ return { data: { session: state.session } }; },
        refreshSession: async function(){ return { data: { session: state.session } }; },
        signUp: async function(args){ state.signups.push(args);
          var user = { id: 'user-1', email: args.email, user_metadata: args.options.data, created_at: new Date().toISOString() };
          state.session = state.nextSignupSession ? { access_token: 'tok', user: user } : null;
          state.pendingUser = user;
          if (state.session) listeners.forEach(function(cb){ cb('SIGNED_IN', state.session); });
          return { data: { user: user, session: state.session }, error: null }; },
        signInWithPassword: async function(args){
          var user = state.pendingUser || { id: 'user-1', email: args.email, created_at: new Date().toISOString(),
            user_metadata: state.loginMeta || { role: 'farmer', name: 'Grace' } };
          state.session = { access_token: 'tok', user: user };
          listeners.forEach(function(cb){ cb('SIGNED_IN', state.session); });
          return { data: { user: user, session: state.session }, error: null }; },
        signOut: async function(){ state.session = null; listeners.forEach(function(cb){ cb('SIGNED_OUT', null); }); return { error: null }; },
        updateUser: async function(a){ state.profileUpdate = a.data; if(state.session) Object.assign(state.session.user.user_metadata, a.data); return { data: {}, error: null }; },
        signInWithOAuth: async function(args){ state.oauth = args; return { data: { provider: args.provider, url: 'https://accounts.google.com/o/oauth2/v2/auth' }, error: null }; },
        resend: async function(){ return { error: null }; }, resetPasswordForEmail: async function(){ return { error: null }; }
      } };
  } };
})();
"""


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
            # CI installs Playwright Chromium. Developers may only have the
            # normal Chrome browser installed, so use it as a test fallback
            # rather than silently skipping the entire browser suite.
            try:
                b = p.chromium.launch(channel="chrome")
            except Exception as chrome_error:  # no usable browser installed
                pytest.skip(
                    "chromium unavailable: "
                    f"{chromium_error}; Chrome fallback unavailable: {chrome_error}"
                )
        yield b
        b.close()


@pytest.fixture
def env(browser, site, monkeypatch):
    db = LegalDb()
    monkeypatch.setattr(app_mod, "LEGAL_SERVICE", LegalService(db))
    # The browser fake covers the Supabase SDK; the Flask app's own REST reads
    # must not reach the internet either, or a slow background request stalls
    # navigation. These endpoints are not what this suite is asserting.
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [])
    monkeypatch.setattr(app_mod, "supa_get_checked", lambda *a, **k: ([], None))
    monkeypatch.setattr(app_mod, "supa_insert", lambda *a, **k: True)
    monkeypatch.setattr(app_mod, "supa_update", lambda *a, **k: True)
    app_mod.app.config["TESTING"] = True
    flask = app_mod.app.test_client()
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, service_workers="block", bypass_csp=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def fake_verify():
        user = page.evaluate("window.__sb && window.__sb.session && window.__sb.session.user")
        if not user or "Bearer tok" not in app_mod.request.headers.get("Authorization", ""):
            return None, (app_mod.jsonify({"error": "Invalid or expired session"}), 401)
        app_mod.g.auth_user = user
        return user["id"], None
    monkeypatch.setattr(app_mod, "verify_supabase_user", fake_verify)

    cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*",
            "access-control-allow-methods": "GET,POST,OPTIONS"}

    def api(route, request):
        if request.method == "OPTIONS":
            return route.fulfill(status=204, headers=cors)
        path = urlparse(request.url).path
        body = request.post_data_json if request.post_data else None
        resp = flask.open(path, method=request.method, json=body,
                          headers={k: v for k, v in request.headers.items() if k.lower() == "authorization"})
        route.fulfill(status=resp.status_code, headers={**cors, "content-type": "application/json"},
                      body=resp.get_data(as_text=True))

    ctx.route(f"https://{API_HOST}/**", api)
    ctx.route("**/npm/@supabase/**", lambda r, q: r.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB))
    ctx.route("**/supabase-js*", lambda r, q: r.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB))
    def supabase_rest(route, request):
        # PostgREST reads return empty lists; Auth settings advertise Google so
        # the OAuth buttons can be exercised. Nothing else is faked.
        if urlparse(request.url).path.endswith("/auth/v1/settings"):
            return route.fulfill(status=200, headers=cors, content_type="application/json",
                                 body='{"external":{"google":true,"email":true}}')
        return route.fulfill(status=200, headers=cors, content_type="application/json", body="[]")

    ctx.route("https://*.supabase.co/**", supabase_rest)

    def other(route, request):
        host = urlparse(request.url).netloc
        if host.startswith("127.0.0.1"):
            # Serve static files through the route instead of the socket. Some
            # Windows antivirus web shields reset large localhost responses
            # (index.html is ~650 KB), which makes a real socket non-deterministic.
            rel = unquote(urlparse(request.url).path).lstrip("/") or "index.html"
            target = (ROOT / "static" / rel).resolve()
            if not str(target).startswith(str((ROOT / "static").resolve())):
                return route.fulfill(status=404, body="not found")
            if target.is_file():
                return route.fulfill(status=200, path=str(target),
                                     content_type=mimetypes.guess_type(target.name)[0]
                                     or "application/octet-stream")
            return route.fulfill(status=404, body="not found")
        if host == API_HOST or host.endswith("supabase.co"):
            return route.fallback()
        if "jsdelivr" in host and "supabase" in request.url:
            return route.fulfill(status=200, content_type="application/javascript", body=SUPABASE_STUB)
        route.abort()
    ctx.route("**/*", other)

    page.goto(site + "/index.html", wait_until="domcontentloaded")
    page.set_default_timeout(5000)
    page.wait_for_function("typeof sbAuth !== 'undefined' && sbAuth", timeout=10000)
    # First visit shows the onboarding splash, which overlays the app shell.
    # Mark it seen before entering, exactly as a returning user's browser has
    # already done, so the consent screens under test are reachable.
    page.evaluate("try{localStorage.setItem('ab_seen_splash','1')}catch(e){}")
    page.evaluate("enterApp()")      # leave the marketing landing overlay
    page.evaluate("hideSplash()")
    page.wait_for_function(
        "document.getElementById('page-splash').style.display === 'none'", timeout=5000)
    yield page, db, errors
    ctx.close()


def _fill_register(page, role="farmer"):
    page.evaluate("openModal('registerModal')")
    page.fill("#regName", "Grace Nakato")
    page.fill("#regPhone", "0771234567")
    page.fill("#regEmail", "grace@example.com")
    page.select_option("#regRole", role)
    page.fill("#regPwd", "Passw0rd!x")
    page.fill("#regPwdConfirm", "Passw0rd!x")


# ── sign-up form ─────────────────────────────────────────────────────────────
def test_signup_checkboxes_start_unchecked_and_marketing_is_separate(env):
    page, _, _ = env
    page.evaluate("openModal('registerModal')")
    assert page.is_checked("#regAcceptTerms") is False
    assert page.is_checked("#regMarketing") is False
    assert page.get_attribute("#regAcceptTerms", "required") is not None
    assert page.get_attribute("#regAcceptTerms", "aria-required") == "true"
    assert page.is_hidden("#regMarketingChannels")
    page.check("#regMarketing")
    assert page.is_visible("#regMarketingChannels")
    labels = page.locator("#regMarketingChannels label").all_inner_texts()
    assert len(labels) == 1 and "SMS" in labels[0]        # only the supported channel
    assert page.is_checked("#regMarketingSms") is False


def test_required_wording_and_links(env, site):
    page, _, _ = env
    page.evaluate("openModal('registerModal')")
    text = page.inner_text("label[for=regAcceptTerms]")
    assert text.startswith("I agree to the AgriBridge Terms of Use, including any terms applicable to my selected "
                           "account role, and acknowledge that I have read the AgriBridge Privacy Policy.")
    assert "optional AgriBridge promotions" in page.inner_text("label[for=regMarketing]")
    import urllib.request
    for sel in ("#regTermsLink", "#regPrivacyLink", "#regRoleTermsLink"):
        assert page.get_attribute(sel, "target") == "_blank"
        href = page.get_attribute(sel, "href")
        assert urllib.request.urlopen(f"{site}/{href}").status == 200, href


def test_role_specific_terms_link_follows_the_selected_role(env):
    page, _, _ = env
    page.evaluate("openModal('registerModal')")
    expect = {"farmer": "farmer-terms", "vendor": "buyer-terms", "hotel": "buyer-terms", "supplier": "supplier-terms"}
    for role, doc in expect.items():
        page.select_option("#regRole", role)
        page.wait_for_function(f"document.getElementById('regRoleTermsLink').getAttribute('href').includes('{doc}')")
        assert "version 1.0.0" in page.inner_text("#regRoleTermsVer")


def test_registration_blocked_without_acceptance_and_form_is_preserved(env):
    page, _, _ = env
    _fill_register(page)
    page.click("#regSubmitBtn")
    err = page.locator("#regTermsErr")
    assert err.is_visible() and "Terms of Use" in err.inner_text()
    assert page.evaluate("window.__sb.signups.length") == 0
    assert page.get_attribute("#regAcceptTerms", "aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == "regAcceptTerms"
    assert page.input_value("#regName") == "Grace Nakato" and page.input_value("#regEmail") == "grace@example.com"
    page.check("#regAcceptTerms")
    assert page.is_hidden("#regTermsErr")


def test_opening_a_legal_link_keeps_the_form(env):
    page, _, _ = env
    _fill_register(page)
    with page.expect_popup() as pop:
        page.click("#regTermsLink")
    pop.value.close()
    assert page.input_value("#regName") == "Grace Nakato"
    assert page.is_visible("#registerModal")


def test_checkbox_works_by_keyboard_and_has_a_touch_sized_row(env):
    page, _, _ = env
    page.evaluate("openModal('registerModal')")
    page.focus("#regAcceptTerms")
    page.keyboard.press("Space")
    assert page.is_checked("#regAcceptTerms")
    box = page.locator("label[for=regAcceptTerms]").bounding_box()
    assert box["height"] >= 44
    # The modal itself must fit a 390px phone (the page's price ticker already scrolls horizontally).
    modal = page.locator("#registerModal .modal-box").bounding_box()
    assert modal["x"] >= 0 and modal["x"] + modal["width"] <= 390


def test_marketing_without_a_channel_is_refused_but_terms_alone_are_enough(env):
    page, _, _ = env
    _fill_register(page)
    page.check("#regAcceptTerms")
    page.check("#regMarketing")
    page.click("#regSubmitBtn")
    assert "choose SMS" in page.inner_text("#regErrMsg").lower() or "Choose SMS" in page.inner_text("#regErrMsg")
    assert page.evaluate("window.__sb.signups.length") == 0
    page.uncheck("#regMarketing")
    page.click("#regSubmitBtn")
    page.wait_for_function("window.__sb.signups.length === 1")
    meta = page.evaluate("window.__sb.signups[0].options.data")
    assert meta["marketing_optin"] == {"sms": False}


def test_signup_records_exact_versions_for_the_role_and_no_marketing(env):
    page, db, _ = env
    page.evaluate("window.__sb.nextSignupSession = true")
    _fill_register(page, "supplier")
    page.check("#regAcceptTerms")
    page.click("#regSubmitBtn")
    page.wait_for_function("window.__sb.signups.length === 1")
    meta = page.evaluate("window.__sb.signups[0].options.data")
    assert meta["legal_signup"]["versions"] == {"terms-of-use": "1.0.0", "privacy-policy": "1.0.0", "supplier-terms": "1.0.0"}
    page.wait_for_function("true")
    for _ in range(50):
        if len(db.acceptances) == 3:
            break
        page.wait_for_timeout(100)
    assert {a["doc_type"] for a in db.acceptances} == {"terms-of-use", "privacy-policy", "supplier-terms"}
    assert {a["method"] for a in db.acceptances} == {"signup_checkbox_deferred"}
    assert db.marketing == []                      # promotions were never ticked
    assert page.is_hidden("#legalReviewModal")


def test_marketing_optin_at_signup_is_recorded_separately(env):
    page, db, _ = env
    page.evaluate("window.__sb.nextSignupSession = true")
    _fill_register(page)
    for sel in ("#regAcceptTerms", "#regMarketing", "#regMarketingSms"):
        page.check(sel)
    page.click("#regSubmitBtn")
    for _ in range(50):
        if db.marketing:
            break
        page.wait_for_timeout(100)
    assert [(m["channel"], m["granted"]) for m in db.marketing] == [("sms", True)]


# ── login review ─────────────────────────────────────────────────────────────
def _login(page):
    page.evaluate("openModal('loginModal')")
    page.fill("#loginPhone", "grace@example.com")
    page.fill("#loginPwd", "Passw0rd!x")
    page.evaluate("doLogin()")


def test_existing_user_without_acceptance_sees_review_and_must_tick(env):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    boxes = page.locator("#legalReviewDocs input[type=checkbox]")
    assert boxes.count() == 3 and all(not boxes.nth(i).is_checked() for i in range(3))
    page.click("#legalReviewBtn")
    # The click listener is attached while the modal opens, so under load this
    # click can land first; wait for the message instead of reading it once.
    page.wait_for_function(
        "document.getElementById('legalReviewErr').textContent.toLowerCase()"
        ".includes('tick each box')",
        timeout=5000,
    )
    assert "tick each box" in page.inner_text("#legalReviewErr").lower()
    assert db.acceptances == []
    page.keyboard.press("Escape")
    assert page.is_visible("#legalReviewModal")          # cannot be dismissed
    for i in range(3):
        boxes.nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    assert {a["method"] for a in db.acceptances} == {"login_review"}
    assert all(a["version"] == "1.0.0" and len(a["hash"]) == 64 for a in db.acceptances)


def test_failed_save_is_shown_and_retry_works(env):
    page, db, _ = env
    db.fail_accept = True
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    for i in range(3):
        page.locator("#legalReviewDocs input[type=checkbox]").nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_function("document.getElementById('legalReviewBtn').textContent.includes('Try again')")
    assert page.is_visible("#legalReviewErr") and db.acceptances == []
    assert page.is_visible("#legalReviewModal")           # never claims success
    db.fail_accept = False
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    assert len(db.acceptances) == 3


def test_user_who_already_accepted_is_not_asked_again(env):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    for i in range(3):
        page.locator("#legalReviewDocs input[type=checkbox]").nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    page.evaluate("logOut()")
    page.wait_for_function("!window.__sb.session")
    _login(page)
    page.wait_for_timeout(1200)
    assert page.is_hidden("#legalReviewModal") and len(db.acceptances) == 3


def test_material_update_asks_again_only_for_the_changed_document(env, monkeypatch):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    for i in range(3):
        page.locator("#legalReviewDocs input[type=checkbox]").nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    import copy
    m = copy.deepcopy(app_mod.LEGAL_SERVICE.manifest)
    m["documents"]["terms-of-use"].update(version="2.0.0", last_material_version="2.0.0", material=True,
                                           sha256="e" * 64, versioned_url="legal/terms-of-use-1.0.0.html",
                                           summary="We changed how refunds are reviewed.")
    monkeypatch.setattr(app_mod, "LEGAL_SERVICE", LegalService(db, manifest=m))
    page.evaluate("logOut()")
    page.wait_for_function("!window.__sb.session")
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    assert page.locator("#legalReviewDocs input[type=checkbox]").count() == 1
    assert "refunds" in page.inner_text("#legalReviewDocs")


def test_user_can_sign_out_or_file_a_privacy_request_from_the_review_screen(env):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    page.click("text=Make a privacy request")
    assert page.is_visible("#privacyRequestModal")
    page.select_option("#prType", "deletion")
    page.click("#prBtn")
    page.wait_for_selector("#prDone:not([hidden])")
    assert db.requests and db.requests[0]["request_type"] == "deletion" and db.requests[0]["user_id"] == "user-1"
    page.evaluate("closeModal('privacyRequestModal')")
    page.click("text=Sign out instead")
    page.wait_for_function("!window.__sb.session")
    assert page.is_hidden("#legalReviewModal")


def test_changing_account_type_asks_for_the_new_role_terms(env):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    for i in range(3):
        page.locator("#legalReviewDocs input[type=checkbox]").nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    page.evaluate("setAccountRole('supplier')")
    page.wait_for_selector("#legalReviewModal.open")
    assert page.locator("#legalReviewDocs input[type=checkbox]").count() == 1
    assert "Supplier Terms" in page.inner_text("#legalReviewDocs")


# ── privacy settings & requests ──────────────────────────────────────────────
def test_settings_privacy_section_marketing_toggle_and_history(env):
    page, db, _ = env
    _login(page)
    page.wait_for_selector("#legalReviewModal.open")
    for i in range(3):
        page.locator("#legalReviewDocs input[type=checkbox]").nth(i).check()
    page.click("#legalReviewBtn")
    page.wait_for_selector("#legalReviewModal:not(.open)", state="attached")
    page.evaluate("openSettings()")
    # Marketing consent lives behind the Privacy & legal row in the
    # Telegram-style settings home, rather than on the home screen itself.
    # Use the visible control a person uses so this test also guards that path.
    page.get_by_role("button", name="Privacy & legal").click()
    page.wait_for_selector("#setMktSms:not([disabled])")
    assert page.is_checked("#setMktSms") is False
    page.check("#setMktSms")
    page.wait_for_function("document.getElementById('toast').textContent.includes('turned on')")
    assert [m["granted"] for m in db.marketing] == [True]
    page.uncheck("#setMktSms")
    page.wait_for_function("document.getElementById('toast').textContent.includes('turned off')")
    assert [m["granted"] for m in db.marketing] == [True, False]
    page.click("text=View versions I accepted")
    page.wait_for_function("document.getElementById('acceptedHist').textContent.includes('terms-of-use v1.0.0')")
    body = page.inner_text("#privacySectionBody")
    assert "agribrige@gmail.com" in body and "Farmer and Producer Terms" in body
    assert page.get_attribute("#privacySectionBody a[href^='https://wa.me']", "href") == "https://wa.me/256755966690"
    assert page.get_attribute("#privacySectionBody a[href^='tel:']", "href") == "tel:+256755966690"


def test_privacy_request_works_when_signed_out(env):
    page, db, _ = env
    page.evaluate("openPrivacyRequest('access')")
    page.click("#prBtn")
    assert "valid email" in page.inner_text("#prErr")
    page.fill("#prEmail", "farmer@example.com")
    page.click("#prBtn")
    page.wait_for_selector("#prDone:not([hidden])")
    assert db.requests[0]["requester_email"] == "farmer@example.com" and db.requests[0]["user_id"] is None
    assert "agribrige@gmail.com" in page.inner_text("#prDone")


def test_terms_and_privacy_modals_show_the_published_documents(env):
    page, _, _ = env
    page.evaluate("openModal('termsModal')")
    page.wait_for_function("document.getElementById('termsModalBody').textContent.includes('Marketplace Agreement')")
    page.evaluate("openModal('privacyModal')")
    page.wait_for_function("document.getElementById('privacyModalBody').textContent.includes('Data Protection Notice')")
    assert "privacy@agribridge.ug" not in page.content()


def test_no_javascript_errors_during_the_flows(env):
    page, _, errors = env
    _fill_register(page)
    page.click("#regSubmitBtn")
    _login(page)
    page.wait_for_timeout(500)
    assert errors == [], errors

# ── Google sign-in and OAuth onboarding ─────────────────────────────────────
def _google_user(page, meta=None):
    page.evaluate(
        """meta => {
          const user = { id: 'user-1', email: 'grace@gmail.com',
            app_metadata: { provider: 'google', providers: ['google'] },
            user_metadata: meta || {}, created_at: new Date().toISOString() };
          window.__sb.emit('SIGNED_IN', { access_token: 'tok', user: user });
        }""",
        meta or {},
    )


def test_google_option_is_offered_on_login_and_registration(env):
    page, _, errors = env
    before = len(errors)
    settings = page.evaluate(
        "fetch(SUPA_URL + '/auth/v1/settings', {headers:{apikey:SUPA_KEY}}).then(function(r){return r.json();})")
    assert settings["external"]["google"] is True, settings
    page.wait_for_function("getComputedStyle(document.getElementById('googleLoginWrap')).display !== 'none'")
    page.evaluate("openModal('loginModal')")
    assert page.is_visible("#googleLoginBtn")
    page.click("#googleLoginBtn")
    page.wait_for_function("window.__sb.oauth && window.__sb.oauth.provider === 'google'")
    oauth = page.evaluate("window.__sb.oauth")
    assert oauth["provider"] == "google"
    assert oauth["options"]["redirectTo"].endswith("/")
    assert oauth["options"]["queryParams"]["prompt"] == "select_account"

    page.evaluate("closeModal('loginModal'); openModal('registerModal')")
    assert page.is_visible("#googleRegisterBtn")
    page.click("#googleRegisterBtn")
    page.wait_for_function("window.__sb.oauth && window.__sb.oauth.provider === 'google'")
    assert len(errors) == before, errors[before:]


def test_google_onboarding_blocks_until_terms_and_saves_the_profile(env, monkeypatch):
    page, db, errors = env
    before = len(errors)
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **k: [])

    class Resp:
        status_code = 201

    monkeypatch.setattr(app_mod.requests, "post", lambda *a, **k: Resp())

    _google_user(page)
    page.wait_for_selector("#oauthProfileModal.open")
    assert page.is_hidden("#legalReviewModal")

    # Escape must not let a Google user skip role, phone and legal acceptance.
    page.keyboard.press("Escape")
    assert page.is_visible("#oauthProfileModal")

    page.click("#oauthSaveBtn")
    # The name is prefilled from the Google account/email, so the first missing
    # requirement is the phone number.
    assert "phone" in page.inner_text("#oauthProfileErr").lower()
    page.fill("#oauthPhone", "12")
    page.click("#oauthSaveBtn")
    assert "phone" in page.inner_text("#oauthProfileErr").lower()
    page.fill("#oauthName", "Grace Nakato")
    page.fill("#oauthPhone", "0771 234567")
    page.select_option("#oauthRole", "supplier")
    page.select_option("#oauthDistrict", "Kampala")
    page.click("#oauthSaveBtn")
    assert "terms" in page.inner_text("#oauthProfileErr").lower()

    page.check("#oauthAcceptTerms")
    page.click("#oauthSaveBtn")
    page.wait_for_selector("#oauthProfileModal:not(.open)", state="attached")
    saved = page.evaluate("window.__sb.profileUpdate")
    assert saved["role"] == "supplier"
    assert saved["phone"] == "+256771234567"
    assert saved["legal_signup"]["versions"]["supplier-terms"] == "1.0.0"
    for _ in range(50):
        if len(db.acceptances) == 3:
            break
        page.wait_for_timeout(100)
    assert {a["doc_type"] for a in db.acceptances} == {
        "terms-of-use", "privacy-policy", "supplier-terms"}
    assert page.is_hidden("#legalReviewModal")
    assert len(errors) == before, errors[before:]


# ── Pesapal checkout option ────────────────────────────────────────────────
def test_pesapal_is_offered_only_when_the_server_enables_it(env, monkeypatch):
    page, _, errors = env
    before = len(errors)
    page.evaluate("openModal('checkoutModal')")
    page.evaluate("syncPayMethods()")
    assert page.is_hidden("#pm-pesapal")

    monkeypatch.setattr(app_mod, "PESAPAL_KEY", "test-key")
    monkeypatch.setattr(app_mod, "PESAPAL_SECRET", "test-secret")
    providers = page.evaluate(
        "fetch(USSD_API + '/api/pay/providers', {cache:'no-store'}).then(function(r){return r.json();})")
    assert providers["providers"]["pesapal"] is True, providers
    page.evaluate("window._payProviders = null; window._payProvidersAt = 0; syncPayMethods()")
    assert page.is_visible("#pm-pesapal")

    page.evaluate("selectPayMethod('pesapal')")
    assert page.is_visible("#pesapalInfoPanel")
    assert page.is_hidden("#cardInfoPanel")
    text = page.inner_text("#pesapalInfoPanel")
    assert "Visa" in text and "recurring" in text.lower()

    page.evaluate("selectPayMethod('cash')")
    assert page.is_hidden("#pesapalInfoPanel")
    assert len(errors) == before, errors[before:]
