# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Legal documents, acceptance records, marketing consent and privacy requests.

Rules this module enforces (mandate sections 7-11):
  * The user's identity always comes from the verified session (the API passes
    `user_id` in); nothing here trusts a client-supplied id.
  * Acceptance is only recorded when the user explicitly sends it (checkbox on
    sign-up or the review screen). It is never inferred from continued use.
  * The server decides which documents apply to the user's role and which
    versions are current. A request naming a stale version is refused (409).
  * "Recorded" is only reported after the database confirms the write.
  * Marketing consent is separate from acceptance, covers only channels that are
    really supported, and is an append-only log: the latest event wins.
  * A non-material update (correction) does not force re-acceptance when the
    user already accepted a version at or after `last_material_version`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services import business
from services.logging import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "static" / "legal" / "manifest.json"

ACCEPT_METHODS = ("signup_checkbox", "signup_checkbox_deferred", "login_review", "settings_review")
_CLIENT_METHODS = ("login_review", "settings_review", "signup_checkbox")
PRIVACY_REQUEST_TYPES = ("access", "correction", "deletion", "marketing_withdrawal", "complaint", "other")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def normalize_role(raw: str | None) -> str:
    """Same mapping as the web app's getCurrentRole(); unknown -> buyer."""
    r = (raw or "").strip().lower()
    if r in ("hotel", "restaurant", "b2b"):
        return "hotel"
    if r == "farmer":
        return "farmer"
    if r in ("supplier", "input_seller"):
        return "supplier"
    return "vendor"


def _semver(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


@dataclass
class LegalResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    status_code: int = 200


class LegalService:
    def __init__(self, supabase, manifest: dict | None = None):
        self._db = supabase
        self._manifest = manifest
        self._published = False

    # ── manifest ─────────────────────────────────────────────────────────────
    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return self._manifest

    def public_manifest(self) -> dict:
        return self.manifest

    def required_documents(self, role: str) -> list[dict]:
        role = normalize_role(role)
        docs = self.manifest["documents"]
        return [docs[k] for k in self.manifest["role_documents"][role] if k in docs]

    # ── publishing (idempotent; versions are immutable in the database) ──────
    def ensure_published(self) -> LegalResult:
        if self._published:
            return LegalResult(ok=True)
        rows = []
        for d in self.manifest["documents"].values():
            rows.append({
                "doc_type": d["doc_type"], "version": d["version"], "title": d["title"],
                "content_sha256": d["sha256"], "effective_at": f"{d['effective_date']}T00:00:00Z",
                "is_material": d["material"], "requires_acceptance": d["requires_acceptance"],
                "applies_to_roles": d["applies_to_roles"], "change_summary": d.get("summary"),
            })
        res = self._db.upsert("legal_document_versions", rows, on_conflict="doc_type,version")
        if not res.ok:
            log.error("legal.publish_failed", status=res.status)
            return LegalResult(ok=False, error="legal documents are not available right now",
                               status_code=503)
        self._published = True
        return LegalResult(ok=True)

    # ── status ───────────────────────────────────────────────────────────────
    def _accepted_versions(self, user_id: str) -> dict[str, list[str]]:
        rows = self._db.select("legal_acceptances",
                               {"user_id": f"eq.{user_id}", "select": "doc_type,version"},
                               limit=500)
        out: dict[str, list[str]] = {}
        for r in rows if isinstance(rows, list) else []:
            if _SEMVER.match(str(r.get("version", ""))):
                out.setdefault(r["doc_type"], []).append(r["version"])
        return out

    @staticmethod
    def _satisfied(doc: dict, accepted: list[str]) -> bool:
        """Accepted the current version, or (for a non-material correction) any
        version at or after the last material one."""
        if doc["version"] in accepted:
            return True
        floor = _semver(doc.get("last_material_version") or doc["version"])
        return any(_semver(v) >= floor for v in accepted)

    def status(self, user_id: str, role: str) -> LegalResult:
        if not user_id:
            return LegalResult(ok=False, error="user required", status_code=401)
        role = normalize_role(role)
        accepted = self._accepted_versions(user_id)
        items = []
        for d in self.required_documents(role):
            items.append({
                "doc_type": d["doc_type"], "title": d["title"], "version": d["version"],
                "effective_date": d["effective_date"], "url": d["url"],
                "versioned_url": d["versioned_url"], "sha256": d["sha256"],
                "requires_acceptance": d["requires_acceptance"], "summary": d["summary"],
                "material": d["material"],
                "accepted": self._satisfied(d, accepted.get(d["doc_type"], [])),
            })
        outstanding = [i for i in items if not i["accepted"]]
        return LegalResult(ok=True, data={
            "role": role, "required": items, "outstanding": outstanding,
            "complete": not outstanding,
        })

    # ── acceptance ───────────────────────────────────────────────────────────
    def accept(self, *, user_id: str, role: str, documents: list[dict], method: str,
               evidence: dict | None = None) -> LegalResult:
        if method not in _CLIENT_METHODS:
            return LegalResult(ok=False, error="invalid method", status_code=400)
        return self._record(user_id=user_id, role=role, documents=documents,
                            method=method, evidence=evidence)

    def _record(self, *, user_id: str, role: str, documents: list[dict], method: str,
                evidence: dict | None) -> LegalResult:
        if not user_id:
            return LegalResult(ok=False, error="user required", status_code=401)
        if not isinstance(documents, list) or not documents:
            return LegalResult(ok=False, error="documents required", status_code=400)
        role = normalize_role(role)
        required = {d["doc_type"]: d for d in self.required_documents(role)}
        todo = []
        for item in documents:
            if not isinstance(item, dict):
                return LegalResult(ok=False, error="invalid document entry", status_code=400)
            dt, ver = str(item.get("doc_type", "")), str(item.get("version", ""))
            doc = required.get(dt)
            if doc is None:
                return LegalResult(ok=False, error=f"{dt or 'document'} does not apply to this account type",
                                   status_code=400)
            if ver != doc["version"]:
                return LegalResult(ok=False, error="a newer version is available; please review it",
                                   data={"doc_type": dt, "current_version": doc["version"]},
                                   status_code=409)
            todo.append(doc)
        pub = self.ensure_published()
        if not pub.ok:
            return pub
        ev = dict(evidence or {})
        recorded = []
        for doc in todo:
            res = self._db.rpc("record_legal_acceptance", {
                "p_user_id": user_id, "p_doc_type": doc["doc_type"], "p_version": doc["version"],
                "p_content_sha256": doc["sha256"], "p_account_role": role, "p_method": method,
                "p_evidence": {**ev, "document_url": doc["versioned_url"]},
            })
            if not res.ok:
                log.error("legal.accept_failed", doc=doc["doc_type"], error=res.error)
                return LegalResult(
                    ok=False, error="we could not save your acceptance; please try again",
                    data={"recorded": recorded}, status_code=502 if res.status >= 500 or not res.status else 400)
            recorded.append({"doc_type": doc["doc_type"], "version": doc["version"]})
        return LegalResult(ok=True, data={"recorded": recorded})

    def sync_signup(self, *, user_id: str, role: str, meta: dict, created_at: str | None) -> LegalResult:
        """Persist what the user explicitly ticked on the sign-up form.

        Sign-up may not return a session (email confirmation), so the box state
        travels in the account's own metadata and is written here on the first
        signed-in call. Only versions that are still current are recorded; nothing
        is recorded for a user who did not tick the box.
        """
        sig = (meta or {}).get("legal_signup")
        recorded: list[dict] = []
        if isinstance(sig, dict) and isinstance(sig.get("versions"), dict) and _recent(created_at):
            required = {d["doc_type"]: d for d in self.required_documents(role)}
            accepted = self._accepted_versions(user_id)
            docs = []
            for dt, ver in sig["versions"].items():
                d = required.get(dt)
                if d and d["version"] == ver and not accepted.get(dt):
                    docs.append({"doc_type": dt, "version": ver})
            if docs:
                res = self._record(user_id=user_id, role=role, documents=docs,
                                   method="signup_checkbox_deferred",
                                   evidence={"signup_ticked_at": str(sig.get("accepted_at", ""))[:40]})
                if not res.ok:
                    return res
                recorded = res.data["recorded"]
        marketing = []
        if isinstance((meta or {}).get("marketing_optin"), dict):
            opt = meta["marketing_optin"]
            if opt.get("sms") is True and "sms" in business.MARKETING_CHANNELS \
                    and _recent(created_at) and not self._marketing_events_exist(user_id):
                res = self.set_marketing(user_id=user_id, channels={"sms": True}, method="signup_checkbox")
                if not res.ok:
                    return res
                marketing = res.data["changed"]
        return LegalResult(ok=True, data={"recorded": recorded, "marketing": marketing})

    def history(self, user_id: str) -> LegalResult:
        rows = self._db.select("legal_acceptances", {
            "user_id": f"eq.{user_id}",
            "select": "doc_type,version,accepted_at,method,account_role",
            "order": "accepted_at.desc"}, limit=200)
        return LegalResult(ok=True, data={"acceptances": rows if isinstance(rows, list) else []})

    # ── marketing consent ────────────────────────────────────────────────────
    def _marketing_events_exist(self, user_id: str) -> bool:
        rows = self._db.select("marketing_consent_events",
                               {"user_id": f"eq.{user_id}", "select": "id"}, limit=1)
        return bool(rows)

    def get_marketing(self, user_id: str) -> LegalResult:
        rows = self._db.select("marketing_consent_current",
                               {"user_id": f"eq.{user_id}", "select": "channel,granted,recorded_at"},
                               limit=10)
        state = {c: False for c in business.MARKETING_CHANNELS}
        for r in rows if isinstance(rows, list) else []:
            if r.get("channel") in state:
                state[r["channel"]] = bool(r.get("granted"))
        return LegalResult(ok=True, data={"channels": state,
                                          "supported_channels": list(business.MARKETING_CHANNELS)})

    def set_marketing(self, *, user_id: str, channels: dict, method: str = "settings") -> LegalResult:
        if not isinstance(channels, dict) or not channels:
            return LegalResult(ok=False, error="channels required", status_code=400)
        for ch, val in channels.items():
            if ch not in business.MARKETING_CHANNELS:
                return LegalResult(ok=False, error=f"{ch} promotions are not available",
                                   status_code=400)
            if not isinstance(val, bool):
                return LegalResult(ok=False, error="channel values must be true or false",
                                   status_code=400)
        current = self.get_marketing(user_id).data["channels"]
        rows = [{"user_id": user_id, "channel": ch, "granted": val, "method": method,
                 "evidence": {"wording": "optional promotions and marketplace updates"}}
                for ch, val in channels.items() if current.get(ch) != val]
        if rows:
            res = self._db.insert("marketing_consent_events", rows)
            if not res.ok:
                log.error("marketing.save_failed", status=res.status)
                return LegalResult(ok=False, error="we could not save your choice; please try again",
                                   status_code=502)
        return LegalResult(ok=True, data={"changed": [r["channel"] for r in rows],
                                          "channels": {**current, **channels}})

    # ── privacy requests ─────────────────────────────────────────────────────
    def create_privacy_request(self, *, request_type: str, details: str = "",
                               user_id: str | None = None, email: str | None = None) -> LegalResult:
        if request_type not in PRIVACY_REQUEST_TYPES:
            return LegalResult(ok=False, error="unknown request type", status_code=400)
        email = (email or "").strip()
        if not user_id and not _EMAIL.match(email):
            return LegalResult(ok=False, error="a valid email is required", status_code=400)
        if email and not _EMAIL.match(email):
            return LegalResult(ok=False, error="a valid email is required", status_code=400)
        details = (details or "").strip()
        if len(details) > 2000:
            return LegalResult(ok=False, error="details are too long (2000 characters max)",
                               status_code=400)
        row = {"request_type": request_type, "details": details or None,
               "user_id": user_id, "requester_email": email or None,
               # A signed-in request is tied to a verified session; an emailed one is not.
               "identity_verified": bool(user_id), "status": "received" if user_id else "verifying"}
        res = self._db.insert("privacy_requests", row)
        if not res.ok:
            log.error("privacy.request_failed", status=res.status)
            return LegalResult(ok=False, error="we could not record your request; please email us",
                               data={"email": business.SUPPORT_EMAIL}, status_code=502)
        rid = None
        if isinstance(res.data, list) and res.data:
            rid = res.data[0].get("id")
        return LegalResult(ok=True, data={
            "request_id": rid, "status": row["status"],
            "next_step": ("We will confirm your identity first, then reply by email."
                          if not user_id else "We have received your request and will reply."),
            "contact": {"email": business.SUPPORT_EMAIL, "phone": business.PHONE_DISPLAY,
                        "whatsapp": business.WHATSAPP_URL}}, status_code=201)

    def list_privacy_requests(self, user_id: str) -> LegalResult:
        rows = self._db.select("privacy_requests", {
            "user_id": f"eq.{user_id}", "select": "id,request_type,status,created_at,status_note",
            "order": "created_at.desc"}, limit=50)
        return LegalResult(ok=True, data={"requests": rows if isinstance(rows, list) else []})


def _recent(created_at: str | None, days: int = 30) -> bool:
    """True if the account was created within `days`. Unknown age => not recent."""
    import datetime as _dt
    if not created_at:
        return False
    try:
        ts = _dt.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.UTC)
    return (_dt.datetime.now(_dt.UTC) - ts) <= _dt.timedelta(days=days)
