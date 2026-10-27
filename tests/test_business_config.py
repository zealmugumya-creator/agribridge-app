# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Official contact details: one source, no stale or placeholder values.

Fails if an old/placeholder contact reappears in anything a user can see, if the
browser config drifts from services/business.py, or if a legal document is
published with placeholder text.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from services import business

ROOT = Path(__file__).resolve().parent.parent

# User-facing sources scanned for stray contacts. docs/PRODUCTION_AUDIT.md is
# excluded on purpose: it quotes the OLD values as audit evidence.
SCANNED = [
    "static/index.html", "static/admin.html", "app.py", "README.md", "GO-LIVE.md",
    "mobile/README.md", "mobile/capacitor.config.json", "render.yaml",
    *[str(p.relative_to(ROOT)) for p in (ROOT / "static" / "legal").glob("*.html")],
    *[str(p.relative_to(ROOT)) for p in (ROOT / "legal" / "source").glob("*.md")],
    *[str(p.relative_to(ROOT)) for p in (ROOT / "services").glob("*.py")],
]
# Not support contacts: the owner's admin login (docs only) and the USSD simulator's dummy handset.
ALLOWED_EMAILS = {business.SUPPORT_EMAIL, "zealmugumya@gmail.com"}
ALLOWED_PHONES = {"256700000000",  # fake number the USSD simulator dials from
                  "256771234567"}  # example shown in the sign-up validation message

# Obvious example/placeholder addresses used as input hints or setup examples.
EXAMPLE_DOMAINS = ("@example.com", "@b.co", "@email.com", "@yourbusiness.com", "@yourdomain.com")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?:\+?256[\s-]?|\b0)(7\d{2})[\s-]?(\d{3})[\s-]?(\d{3})\b")


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_official_values():
    assert business.SUPPORT_EMAIL == "agribrige@gmail.com"
    assert business.WEBSITE == "https://agribrige.com"
    assert business.PHONE_E164 == "+256755966690"
    assert business.WHATSAPP_URL == "https://wa.me/256755966690"
    assert business.MAILTO == "mailto:agribrige@gmail.com" and business.TEL == "tel:+256755966690"


def test_official_phone_is_a_valid_ugandan_mobile():
    assert re.fullmatch(r"\+2567\d{8}", business.PHONE_E164)


def test_browser_config_matches_python_config():
    js = _read("static/business-config.js")
    def grab(key):
        return re.search(rf"{key}:\s*'([^']*)'", js).group(1)
    assert grab("email") == business.SUPPORT_EMAIL
    assert grab("website") == business.WEBSITE
    assert grab("phoneE164") == business.PHONE_E164
    assert grab("phoneDigits") == business.PHONE_DIGITS
    assert grab("phoneDisplay") == business.PHONE_DISPLAY
    assert grab("whatsappDigits") == business.WHATSAPP_DIGITS
    assert grab("whatsappUrl") == business.WHATSAPP_URL
    assert grab("mailto") == business.MAILTO and grab("tel") == business.TEL
    assert grab("currency") == business.CURRENCY and grab("country") == business.COUNTRY


def test_index_loads_the_central_config():
    assert 'src="business-config.js"' in _read("static/index.html")


def test_no_stale_or_unofficial_emails_anywhere_user_facing():
    bad = {}
    for rel in SCANNED:
        for m in _EMAIL.findall(_read(rel)):
            if m.lower() not in ALLOWED_EMAILS and not m.lower().endswith(EXAMPLE_DOMAINS):
                if re.search(r"\.(png|jpg|svg|js|css)$", m):
                    continue
                bad.setdefault(rel, set()).add(m)
    assert not bad, f"unofficial email addresses found: {bad}"


def test_no_other_phone_numbers_user_facing():
    bad = {}
    for rel in SCANNED:
        for m in _PHONE.finditer(_read(rel)):
            digits = "256" + m.group(1) + m.group(2) + m.group(3)
            if digits != business.PHONE_DIGITS and digits not in ALLOWED_PHONES:
                bad.setdefault(rel, set()).add(m.group(0))
    assert not bad, f"unofficial phone numbers found: {bad}"


def test_old_agribridge_ug_support_domain_is_gone():
    for rel in SCANNED:
        text = _read(rel)
        for m in re.finditer(r"[\w.+-]+@agribridge\.(ug|app|com)", text):
            raise AssertionError(f"{rel}: {m.group(0)}")


def test_no_made_up_payment_or_bank_details_in_the_web_app():
    html = _read("static/index.html")
    assert "A/C 01234567" not in html and "DFCU" not in html


def test_wa_me_links_all_use_the_official_number():
    for rel in ("static/index.html", *[str(p.relative_to(ROOT)) for p in (ROOT / "static" / "legal").glob("*.html")]):
        for m in re.finditer(r"https://wa\.me/(\d*)", _read(rel)):
            assert m.group(1) in ("", business.WHATSAPP_DIGITS), f"{rel}: {m.group(0)}"


_PLACEHOLDER = re.compile(r"\[(?:DATE|EMAIL|PHONE|ADDRESS|CITY|YOUR [A-Z ]+|REGISTRATION[^\]]*)\]|\{\{[A-Z_]+\}\}|lorem ipsum", re.I)


def test_published_legal_documents_have_no_placeholders():
    for p in list((ROOT / "static" / "legal").glob("*.html")) + list((ROOT / "legal" / "archive").rglob("*.md")):
        assert not _PLACEHOLDER.search(p.read_text(encoding="utf-8")), p.name


def test_legal_documents_use_the_official_contacts():
    for name in ("privacy-policy", "terms-of-use"):
        html = _read(f"static/legal/{name}.html")
        assert business.SUPPORT_EMAIL in html and business.PHONE_DISPLAY in html
        assert business.WHATSAPP_URL in html and business.WEBSITE in html


def test_unsupplied_legal_details_are_not_invented():
    text = _read("static/legal/privacy-policy.html") + _read("static/legal/terms-of-use.html")
    assert not re.search(r"Reg(?:istration)?\.? ?(?:No|Number)\.?[:\s]+[A-Z0-9/-]{5,}", text)
    assert "Data Protection Officer:" not in text
    assert "Zeal Mugumya" not in text


def test_manifest_is_valid_json_with_hashes():
    m = json.loads(_read("static/legal/manifest.json"))
    for d in m["documents"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", d["sha256"])
