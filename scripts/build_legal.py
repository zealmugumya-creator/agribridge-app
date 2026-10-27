#!/usr/bin/env python3
# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Build the published legal documents from `legal/source/*.md`.

Outputs (committed, so the static site and API serve identical text):
  static/legal/<doc>.html            current version (full page)
  static/legal/<doc>.fragment.html   same body, for in-app display
  static/legal/<doc>-<version>.html  immutable page for that exact version
  static/legal/manifest.json         versions, hashes, role mapping
  legal/archive/<doc>/<version>.md   frozen copy of each published version

Rules enforced here (mandate sections 9-10):
  * A published version is NEVER silently replaced. If `legal/archive/<doc>/<v>.md`
    exists and the rebuilt text differs, the build fails: bump the version.
  * Unresolved `{{TOKENS}}` or `[PLACEHOLDER]` text fails the build.
  * Contact details come only from services/business.py.
  * The renderer escapes all text and only allows http(s)/mailto/tel links, so
    document content can never inject script.

Usage:  python scripts/build_legal.py          # write outputs
        python scripts/build_legal.py --check  # fail if outputs are stale
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import business  # noqa: E402

SRC = ROOT / "legal" / "source"
ARCHIVE = ROOT / "legal" / "archive"
OUT = ROOT / "static" / "legal"

ROLES = ("farmer", "vendor", "hotel", "supplier")
_TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
_PLACEHOLDER = re.compile(r"\[(?:DATE|EMAIL|PHONE|ADDRESS|CITY|YOUR [A-Z ]+|REGISTRATION[^\]]*)\]")


class LegalBuildError(Exception):
    pass


def parse_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise LegalBuildError("missing front matter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip() + "\n"


def substitute(body: str) -> str:
    def repl(mo):
        key = mo.group(1)
        if key not in business.LEGAL_TOKENS:
            raise LegalBuildError(f"unknown token {{{{{key}}}}}")
        return business.LEGAL_TOKENS[key]
    out = _TOKEN.sub(repl, body)
    if _PLACEHOLDER.search(out):
        raise LegalBuildError(f"placeholder text left in document: {_PLACEHOLDER.search(out).group(0)}")
    return out


# ── Minimal, safe markdown renderer ──────────────────────────────────────────
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(s: str) -> str:
    links: list[str] = []

    def stash(mo):
        url = mo.group(2)
        if not re.match(r"^(https?://|mailto:|tel:)", url):
            raise LegalBuildError(f"disallowed link scheme: {url}")
        links.append(f'<a href="{html.escape(url, quote=True)}" rel="noopener">{html.escape(mo.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"
    s = _LINK.sub(stash, s)
    s = html.escape(s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)


def render(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    in_list: str | None = None

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            close_list()
            continue
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        if h:
            flush_para()
            close_list()
            n = len(h.group(1))
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
            continue
        if line.startswith(">"):
            flush_para()
            close_list()
            out.append("<blockquote>" + _inline(line.lstrip("> ").strip()) + "</blockquote>")
            continue
        b = re.match(r"^[-*]\s+(.*)$", line)
        n_ = re.match(r"^\d+\.\s+(.*)$", line)
        if b or n_:
            flush_para()
            want = "ul" if b else "ol"
            if in_list != want:
                close_list()
                out.append(f"<{want}>")
                in_list = want
            out.append("<li>" + _inline((b or n_).group(1)) + "</li>")
            continue
        close_list()
        para.append(line.strip())
    flush_para()
    close_list()
    return "\n".join(out)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — AgriBridge</title>
<meta name="robots" content="index,follow">
<style>
body{{font:16px/1.65 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;color:#1b2a1f;background:#f7faf5}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 56px}}
h1{{font-size:1.6rem}} h2{{font-size:1.15rem;margin-top:1.8em}}
blockquote{{background:#fff7e0;border-left:4px solid #e0a800;margin:1em 0;padding:8px 14px}}
a{{color:#1f7a3d}} nav a{{font-weight:600}}
@media (prefers-color-scheme:dark){{body{{background:#101712;color:#e6efe8}}blockquote{{background:#2a2410}}a{{color:#6fd68f}}}}
</style></head><body><main>
<nav><a href="../index.html">&larr; Back to AgriBridge</a> ·
<a href="privacy-policy.html">Privacy Policy</a> · <a href="terms-of-use.html">Terms of Use</a></nav>
{body}
<hr><p>Questions? <a href="{mailto}">{email}</a> · <a href="{tel}">{phone}</a> · <a href="{wa}">WhatsApp</a></p>
<p><small>Document: {doc_type} · version {version} · effective {effective} · SHA-256 {sha}</small></p>
</main></body></html>
"""


def load_documents() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for path in sorted(SRC.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        for req in ("doc_type", "title", "version", "effective_date", "material",
                    "requires_acceptance", "applies_to_roles", "summary"):
            if req not in meta:
                raise LegalBuildError(f"{path.name}: missing '{req}'")
        if meta["doc_type"] != path.stem:
            raise LegalBuildError(f"{path.name}: doc_type must equal file name")
        if not re.match(r"^\d+\.\d+\.\d+$", meta["version"]):
            raise LegalBuildError(f"{path.name}: version must be x.y.z")
        if meta["material"] not in ("true", "false"):
            raise LegalBuildError(f"{path.name}: material must be true or false")
        if meta["material"] == "false" and not re.match(r"^\d+\.\d+\.\d+$", meta.get("last_material_version", "")):
            raise LegalBuildError(f"{path.name}: non-material versions need last_material_version")
        if meta["material"] == "true":
            meta["last_material_version"] = meta["version"]
        text = substitute(body)
        roles = ["all"] if meta["applies_to_roles"] == "all" else \
            [r.strip() for r in meta["applies_to_roles"].split(",")]
        for r in roles:
            if r != "all" and r not in ROLES:
                raise LegalBuildError(f"{path.name}: unknown role {r}")
        docs[meta["doc_type"]] = {
            **meta, "roles": roles, "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "html": render(text),
        }
    if "privacy-policy" not in docs or "terms-of-use" not in docs:
        raise LegalBuildError("privacy-policy and terms-of-use are mandatory")
    return docs


def build_manifest(docs: dict[str, dict]) -> dict:
    documents = {}
    for k, d in docs.items():
        documents[k] = {
            "doc_type": k, "title": d["title"], "version": d["version"],
            "effective_date": d["effective_date"], "sha256": d["sha256"],
            "material": d["material"] == "true",
            "last_material_version": d["last_material_version"],
            "requires_acceptance": d["requires_acceptance"],  # accept | acknowledge
            "applies_to_roles": d["roles"], "summary": d["summary"],
            "url": f"legal/{k}.html", "versioned_url": f"legal/{k}-{d['version']}.html",
        }
    role_documents = {}
    for role in ROLES:
        role_documents[role] = [k for k, d in docs.items()
                                if "all" in d["roles"] or role in d["roles"]]
    return {"schema": 1, "documents": documents, "role_documents": role_documents}


def _page(d: dict) -> str:
    return _PAGE.format(
        title=html.escape(d["title"]), body=d["html"], doc_type=d["doc_type"],
        version=d["version"], effective=d["effective_date"], sha=d["sha256"],
        mailto=business.MAILTO, email=html.escape(business.SUPPORT_EMAIL),
        tel=business.TEL, phone=html.escape(business.PHONE_DISPLAY), wa=business.WHATSAPP_URL)


def outputs(docs: dict[str, dict]) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for k, d in docs.items():
        page = _page(d)
        files[OUT / f"{k}.html"] = page
        files[OUT / f"{k}-{d['version']}.html"] = page
        files[OUT / f"{k}.fragment.html"] = d["html"] + "\n"
    files[OUT / "manifest.json"] = json.dumps(build_manifest(docs), indent=2, sort_keys=True) + "\n"
    return files


def check_archive(docs: dict[str, dict], write: bool) -> None:
    for k, d in docs.items():
        arch = ARCHIVE / k / f"{d['version']}.md"
        if arch.exists():
            if arch.read_text(encoding="utf-8") != d["text"]:
                raise LegalBuildError(
                    f"{k} {d['version']} was already published with different text. "
                    "Never edit a published version: bump the version number.")
        elif write:
            arch.parent.mkdir(parents=True, exist_ok=True)
            arch.write_text(d["text"], encoding="utf-8")


def main(argv: list[str]) -> int:
    check = "--check" in argv
    try:
        docs = load_documents()
        check_archive(docs, write=not check)
        files = outputs(docs)
    except LegalBuildError as exc:
        print(f"legal build failed: {exc}", file=sys.stderr)
        return 1
    if check:
        stale = [str(p.relative_to(ROOT)) for p, c in files.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != c]
        missing_arch = [k for k, d in docs.items() if not (ARCHIVE / k / f"{d['version']}.md").exists()]
        if stale or missing_arch:
            print("legal outputs are stale; run scripts/build_legal.py:", stale, missing_arch, file=sys.stderr)
            return 1
        print("legal outputs up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for p, c in files.items():
        p.write_text(c, encoding="utf-8")
    print(f"built {len(docs)} documents -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
