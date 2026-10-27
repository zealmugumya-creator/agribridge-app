# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Guards for static/_headers — the response headers the Worker actually sends.

The SPA announced nosniff, SAMEORIGIN and a Referrer-Policy as
<meta http-equiv=…> for as long as it has existed, and two of them cannot work
that way: browsers ignore X-Frame-Options delivered in a meta, and CSP forbids
frame-ancestors there. So the clickjacking protection the pages appeared to
declare was never in force. This file is what makes it real, and what extends
the same four headers to admin.html, logistics.html and transporter.html, which
declare nothing at all.

The tests below encode what was measured in production rather than what the
Cloudflare documentation implies: a per-path header block is ignored by the
assets runtime, so a CSP scoped to /index.html publishes nothing. That is why
only a `/*` block is shipped, and why no CSP header is shipped at all — a
blanket `/*` CSP would quietly re-policy the three screens the owner runs the
business from, none of which has ever been measured against a policy.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADERS = ROOT / "static" / "_headers"
INDEX = ROOT / "static" / "index.html"

UNIVERSAL = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=15552000; includeSubDomains",
}

PROTECTED_PAGES = ("/admin.html", "/logistics.html", "/transporter.html")


def _lines() -> list[str]:
    assert HEADERS.exists(), "static/_headers is missing — the Worker sends no custom headers"
    return HEADERS.read_text(encoding="utf-8").splitlines()


def _blocks() -> dict[str, dict[str, str]]:
    """Parse the file the way Cloudflare does: column 0 is a path pattern and
    only an indented line is a header. The syntax test below keeps that true."""
    blocks: dict[str, dict[str, str]] = {}
    pattern = None
    for raw in _lines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in (" ", "\t"):
            assert pattern is not None, f"header before any path pattern: {raw!r}"
            name, _, value = raw.strip().partition(":")
            assert name.strip(), f"header line with no name: {raw!r}"
            blocks[pattern][name.strip()] = " ".join(value.split())
        else:
            pattern = raw.strip()
            blocks.setdefault(pattern, {})
    return blocks


def test_only_comments_and_path_patterns_start_a_line() -> None:
    """A prose line that wraps without a leading # is read as a path pattern and
    silently swallows the block under it."""
    for raw in _lines():
        if raw.strip() and raw[0] not in (" ", "\t"):
            assert raw.strip().startswith(("/", "#")), f"bad line in _headers: {raw!r}"


def test_every_path_pattern_is_valid() -> None:
    for pattern in _blocks():
        assert pattern.startswith("/"), f"path pattern must start with /: {pattern!r}"
        assert " " not in pattern, f"path pattern contains a space: {pattern!r}"


def test_universal_headers_are_declared() -> None:
    got = _blocks().get("/*", {})
    for name, value in UNIVERSAL.items():
        assert got.get(name) == value, f"/* sends {got.get(name)!r}, expected {value!r}"


def test_no_csp_header_is_shipped_and_the_meta_policy_survives() -> None:
    """A path-scoped CSP publishes nothing on this platform, so the enforceable
    policy for index.html is its meta tag. Shipping a CSP header here would mean
    a blanket one, which would also bind the pages that have no policy today."""
    for pattern, headers in _blocks().items():
        assert "Content-Security-Policy" not in headers, f"{pattern} declares a CSP"
    head = INDEX.read_text(encoding="utf-8")[:4000]
    assert len(re.findall(r'<meta\s+http-equiv="Content-Security-Policy"', head, re.I)) == 1, (
        "index.html no longer carries exactly one CSP meta"
    )


def test_meta_only_headers_are_still_declared_in_the_page() -> None:
    """The nosniff/referrer metas are redundant now that the header is real, but
    the Capacitor shell loads the file without going through the Worker."""
    head = INDEX.read_text(encoding="utf-8")[:4000].lower()
    for name in ("x-content-type-options", "x-frame-options", "referrer-policy"):
        assert f'http-equiv="{name}"' in head, f"index.html no longer declares {name} in meta"


def test_protected_pages_are_covered_by_the_universal_block_only() -> None:
    """Nothing in the file overrides the universal headers for the operator
    screens; if a block ever appears for them it must be deliberate."""
    blocks = _blocks()
    for page in PROTECTED_PAGES:
        assert page not in blocks, f"{page} now has its own block — was that measured?"
