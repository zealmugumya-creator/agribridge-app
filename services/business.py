# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Single source of truth for AgriBridge's official public business details.

The browser reads the same values from `static/business-config.js`; a test
(`tests/test_business_config.py`) fails if the two drift apart or if a stale
contact detail reappears anywhere user-facing.

Only PUBLIC contact details live here. Never put credentials in this module.
Details the owner has not supplied (legal entity name, registration number,
physical address, data protection officer) are deliberately NOT invented: they
are listed in `PENDING_LEGAL_DETAILS` and tracked in docs/LEGAL_PENDING.md.
"""
from __future__ import annotations

BUSINESS_NAME = "AgriBridge"
WEBSITE = "https://agribrige.com"
SUPPORT_EMAIL = "agribrige@gmail.com"
# Digits only, international format without '+': used for wa.me links.
PHONE_DIGITS = "256755966690"
PHONE_E164 = "+" + PHONE_DIGITS
PHONE_DISPLAY = "+256 755 966 690"
WHATSAPP_DIGITS = PHONE_DIGITS
COUNTRY = "Uganda"
CURRENCY = "UGX"

MAILTO = f"mailto:{SUPPORT_EMAIL}"
TEL = f"tel:{PHONE_E164}"
WHATSAPP_URL = f"https://wa.me/{WHATSAPP_DIGITS}"

# Promotional channels AgriBridge can actually send on today. SMS goes through
# Africa's Talking. There is no marketing email sender or WhatsApp Business API
# configured, so those channels are NOT offered in the UI or accepted by the API.
MARKETING_CHANNELS = ("sms",)

# Not supplied by the owner — never invent these (see docs/LEGAL_PENDING.md).
PENDING_LEGAL_DETAILS = (
    "registered legal name",
    "company registration number",
    "physical address",
    "data protection officer / privacy officer name",
    "PDPO registration number (if registered)",
)

# Tokens substituted into legal documents by scripts/build_legal.py.
LEGAL_TOKENS = {
    "BUSINESS_NAME": BUSINESS_NAME,
    "WEBSITE": WEBSITE,
    "EMAIL": SUPPORT_EMAIL,
    "PHONE": PHONE_DISPLAY,
    "WHATSAPP_URL": WHATSAPP_URL,
    "COUNTRY": COUNTRY,
    "CURRENCY": CURRENCY,
}
