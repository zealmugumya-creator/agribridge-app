# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Static regression guards for the Google sign-in and Pesapal checkout UI.

These read the shipped browser assets and the Flask URL map. They cannot prove
Google or Pesapal works end to end (that needs the providers' dashboards and
credentials); they prove the wiring cannot silently disappear, that the browser
never receives a server credential, and that the onboarding/review screens are
not skippable.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app as app_mod

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
ADMIN = ROOT / "static" / "admin.html"


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


def _ids(path: Path) -> list[str]:
    parser = _Ids()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.ids


@pytest.fixture(scope="module")
def index_html():
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def admin_html():
    return ADMIN.read_text(encoding="utf-8")


# ── login / Google sign-in ──────────────────────────────────────────────────
def test_login_has_exactly_one_submit_button(index_html):
    ids = _ids(INDEX)
    assert ids.count("loginSubmitBtn") == 1
    assert "loginBtn" not in ids
    # The lockout UI and the form must point at the same element.
    assert index_html.count("getElementById('loginSubmitBtn')") >= 2


def test_google_buttons_are_hidden_until_the_provider_is_enabled(index_html):
    ids = _ids(INDEX)
    for element in ("googleLoginWrap", "googleRegisterWrap", "googleLoginBtn", "googleRegisterBtn"):
        assert element in ids
    assert re.search(r'id="googleLoginWrap"[^>]*style="display:none', index_html)
    assert re.search(r'id="googleRegisterWrap"[^>]*style="display:none', index_html)
    assert "fetch(SUPA_URL + '/auth/v1/settings'" in index_html
    assert "j.external && j.external.google" in index_html
    assert "googleLoginWrap', 'googleRegisterWrap'" in index_html


def test_google_sign_in_uses_supabase_oauth_and_returns_to_the_site(index_html):
    assert "sbAuth.auth.signInWithOAuth({" in index_html
    assert re.search(r"provider:\s*'google'", index_html)
    assert "redirectTo: window.location.origin + '/'" in index_html
    # Login and registration both route through the same handler.
    assert index_html.count("doGoogleLogin(") >= 3
    assert "openModal('registerModal')" in index_html


def test_google_users_must_finish_role_phone_district_and_terms(index_html):
    for field in ("oauthName", "oauthPhone", "oauthRole", "oauthDistrict",
                  "oauthAcceptTerms", "oauthProfileModal", "oauthSaveBtn"):
        assert field in _ids(INDEX), field
    assert "function _oauthNeedsProfile(u)" in index_html
    assert "m.phone && m.role && (m.name || m.full_name) && m.district" in index_html
    assert "if (_oauthNeedsProfile(u)) _openOAuthProfile(u);" in index_html
    # The modal cannot be dismissed by clicking the backdrop or pressing Escape.
    assert re.search(r'class="modal-overlay modal-locked" id="oauthProfileModal"', index_html)
    assert not re.search(r'id="oauthProfileModal"[^>]*handleOverlay', index_html)
    assert "if (!terms) return fail(" in index_html
    assert "legal_signup: { accepted_at: new Date().toISOString(), versions: versions }" in index_html
    assert "await saveProfileWithRetry({" in index_html
    assert "postAuthLegalCheck('oauth_onboarding')" in index_html


def test_oauth_onboarding_suppresses_the_second_legal_prompt_until_saved(index_html):
    assert "if (!_oauthOnboardingOpen) postAuthLegalCheck();" in index_html
    assert "_oauthOnboardingOpen = false;" in index_html
    assert "function oauthSignOut()" in index_html


# ── Pesapal checkout ───────────────────────────────────────────────────────
def test_pesapal_checkout_is_hidden_until_the_api_enables_it(index_html):
    ids = _ids(INDEX)
    assert "pm-pesapal" in ids and "pesapalInfoPanel" in ids
    assert "selectPayMethod('pesapal')" in index_html
    assert "['card','momo','airtel','pesapal','cash','bank']" in index_html or \
        set(re.findall(r"'([a-z]+)'", re.search(r"\[([^\]]*)\]\.forEach\(m =>", index_html).group(1))) == {
            'momo', 'airtel', 'card', 'pesapal', 'cash', 'bank'}
    assert "const p = document.getElementById('pm-pesapal');" in index_html
    assert "const hasPesapal = !!prov.pesapal;" in index_html
    assert "if (!hasPesapal && selectedPayMethod === 'pesapal') selectPayMethod('momo');" in index_html


def test_pesapal_payment_starts_on_the_server_and_redirects(index_html):
    assert re.search(r"if\(payMethod==='pesapal'\)", index_html)
    assert "provider:'pesapal'" in index_html
    assert "return { redirect:j.link };" in index_html
    assert "Pesapal is unavailable right now" in index_html


def test_pesapal_return_handler_reports_every_outcome(index_html):
    assert "if (q.get('pay') !== 'done') return;" in index_html
    assert re.search(r"st\s*===\s*'recurring'", index_html)
    assert "Recurring payment received" in index_html
    assert "Payment was not completed" in index_html
    assert "history.replaceState(null, '', location.pathname);" in index_html


def test_admin_has_a_recurring_finance_view(admin_html):
    assert "['recurring','Recurring']" in admin_html
    assert "recurring:'/api/admin/finance/recurring'" in admin_html
    assert "function finRecurring(d)" in admin_html
    assert "SUSPENSE_OVERPAYMENT" in admin_html
    assert "paid_amount_without_ledger" in admin_html


def test_pesapal_routes_are_registered():
    rules = {str(r) for r in app_mod.app.url_map.iter_rules()}
    assert "/api/pay/pesapal/callback" in rules
    assert "/api/pay/webhook/pesapal" in rules
    assert "/api/admin/finance/recurring" in rules


# ── secrets never reach the browser ────────────────────────────────────────
def test_browser_assets_never_contain_server_credentials(monkeypatch):
    forbidden = re.compile(
        r"service_role|sb_secret_|SUPABASE_SERVICE|PESAPAL_CONSUMER_|"
        r"FLW_SECRET|FLW_ENCRYPTION|PESAPAL_IPN_ID|client_secret", re.IGNORECASE)
    for path in (INDEX, ADMIN):
        text = path.read_text(encoding="utf-8")
        match = forbidden.search(text)
        assert not match, f"{path.name} exposes {match.group(0)} to the browser"


def test_pesapal_secrets_are_documented_by_name_only():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("PESAPAL_CONSUMER_KEY", "PESAPAL_CONSUMER_SECRET",
                 "PESAPAL_ENV", "PESAPAL_CALLBACK_BASE_URL", "PESAPAL_IPN_ID"):
        assert re.search(rf"^{name}=.*$", example, re.MULTILINE), name
    assert not re.search(r"^PESAPAL_CONSUMER_SECRET=\S+", example, re.MULTILINE)
