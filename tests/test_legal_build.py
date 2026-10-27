# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""The legal build: outputs are in sync, versions are frozen, content is safe."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_legal  # noqa: E402


def test_committed_outputs_are_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_legal.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_manifest_hash_matches_the_archived_text():
    m = json.loads((ROOT / "static/legal/manifest.json").read_text())
    for k, d in m["documents"].items():
        archived = (ROOT / "legal" / "archive" / k / f"{d['version']}.md").read_text(encoding="utf-8")
        assert hashlib.sha256(archived.encode()).hexdigest() == d["sha256"]


def test_every_manifest_url_exists():
    m = json.loads((ROOT / "static/legal/manifest.json").read_text())
    for d in m["documents"].values():
        assert (ROOT / "static" / d["url"]).exists() and (ROOT / "static" / d["versioned_url"]).exists()


def test_published_text_cannot_be_silently_changed(tmp_path, monkeypatch):
    src, arch = tmp_path / "source", tmp_path / "archive"
    src.mkdir()
    for f in (ROOT / "legal" / "source").glob("*.md"):
        (src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    shutil_tree = ROOT / "legal" / "archive"
    import shutil
    shutil.copytree(shutil_tree, arch)
    monkeypatch.setattr(build_legal, "SRC", src)
    monkeypatch.setattr(build_legal, "ARCHIVE", arch)
    tou = src / "terms-of-use.md"
    tou.write_text(tou.read_text(encoding="utf-8").replace("You must be 18", "You must be 21"), encoding="utf-8")
    docs = build_legal.load_documents()
    with pytest.raises(build_legal.LegalBuildError, match="bump the version"):
        build_legal.check_archive(docs, write=True)


def test_placeholders_and_unknown_tokens_fail_the_build():
    with pytest.raises(build_legal.LegalBuildError):
        build_legal.substitute("Call [PHONE] now")
    with pytest.raises(build_legal.LegalBuildError):
        build_legal.substitute("{{NOT_A_TOKEN}}")


def test_renderer_escapes_html_and_blocks_script_links():
    out = build_legal.render("# Hi <script>alert(1)</script>\n\nA **bold** [x](https://a.b)")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<strong>bold</strong>" in out
    with pytest.raises(build_legal.LegalBuildError):
        build_legal.render("[x](javascript:alert(1))")


def test_non_material_versions_must_name_the_last_material_version():
    text = (ROOT / "legal/source/privacy-policy.md").read_text(encoding="utf-8").replace("material: true", "material: false")
    meta, _ = build_legal.parse_front_matter(text)
    assert meta["material"] == "false" and "last_material_version" not in meta


def test_published_pages_contain_no_scripts():
    for p in (ROOT / "static" / "legal").glob("*.html"):
        assert not re.search(r"<script|onerror=|javascript:", p.read_text(encoding="utf-8"), re.I)


def test_policy_does_not_make_unverified_security_or_deletion_claims():
    t = (ROOT / "legal/source/privacy-policy.md").read_text(encoding="utf-8").lower()
    for claim in ("row-level security", "encrypted at rest", "never reach", "deleted automatically",
                  "within 30 days of account closure", "gdpr compliant", "fully compliant"):
        assert claim not in t, claim


def test_terms_do_not_contain_unreviewed_hard_clauses():
    t = (ROOT / "legal/source/terms-of-use.md").read_text(encoding="utf-8").lower()
    for clause in ("escrow provider, or a guarantor", ):
        assert clause in t                      # the honest disclaimer is present
    for bad in ("cader", "binding arbitration", "no refunds", "liability shall not exceed",
                "guarantees product quality", "three-strikes"):
        assert bad not in t, bad
