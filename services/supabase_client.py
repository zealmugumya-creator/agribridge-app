# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Supabase data access: PostgREST helpers, RPC calls, and a real health probe.

Audit findings addressed:
  * M6 — `/health` must verify actual DB connectivity, not just "key is set".
  * C1/C2 — order creation goes through the `create_order_atomic` Postgres RPC so
    price recomputation and stock reservation happen server-side, atomically.
  * H3 — the notification outbox is drained via the `claim_notifications` /
    `resolve_notification` RPCs (durable, idempotent).

All calls use the SERVICE-ROLE key and are backend-only. Bounded timeouts on
every request (Phase 10). This module never raises on transient failures where a
soft fallback is safer; it returns a structured result the caller can branch on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from services.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 8

# The web app serves listing media from this public bucket, but browsers never
# receive permission to write to it.  Uploads are authenticated and relayed by
# the API using the backend-only service-role key.
_UPLOADS_BUCKET = "uploads"


@dataclass
class RpcResult:
    ok: bool
    data: Any = None
    status: int = 0
    error: str = ""


class SupabaseClient:
    def __init__(self, url: str, service_key: str):
        self._url = (url or "").rstrip("/")
        self._key = service_key or ""

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        h = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    # ── Health probe: a real round-trip, not a key-presence check (M6) ────────
    def ping(self) -> RpcResult:
        """Execute a trivial query through PostgREST to prove connectivity."""
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        try:
            # HEAD on a system-visible table; 2xx/401/406 all prove we reached the
            # DB gateway. A connection error / timeout does not.
            resp = requests.get(
                f"{self._url}/rest/v1/platform_config",
                params={"select": "commission_pct", "limit": "1"},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            reachable = resp.status_code < 500
            return RpcResult(ok=reachable, status=resp.status_code,
                             error="" if reachable else f"http {resp.status_code}")
        except requests.RequestException as exc:
            return RpcResult(ok=False, error=type(exc).__name__)

    # ── Generic table access (read paths used by admin/public endpoints) ──────
    def select(self, table: str, filters: dict[str, Any] | None = None,
               limit: int = 100) -> list[dict]:
        if not self.configured:
            return []
        params: dict[str, Any] = {"limit": limit}
        if filters:
            params.update(filters)
        try:
            resp = requests.get(f"{self._url}/rest/v1/{table}",
                                params=params, headers=self._headers(), timeout=_TIMEOUT)
            return resp.json() if resp.ok else []
        except (requests.RequestException, ValueError) as exc:
            log.error("supabase.select_failed", table=table, error=type(exc).__name__)
            return []

    def update(self, table: str, data: dict, eq_col: str, eq_val: Any) -> bool:
        if not self.configured:
            return False
        try:
            resp = requests.patch(f"{self._url}/rest/v1/{table}", json=data,
                                  params={eq_col: f"eq.{eq_val}"},
                                  headers=self._headers("return=minimal"), timeout=_TIMEOUT)
            return resp.status_code < 300
        except requests.RequestException as exc:
            log.error("supabase.update_failed", table=table, error=type(exc).__name__)
            return False

    def update_checked(self, table: str, data: dict, filters: dict[str, Any]) -> RpcResult:
        """UPDATE ... WHERE <filters> RETURNING the affected rows.

        PostgREST cannot compare two columns in a filter, so a guard like
        "only write this quantity while reserved_qty is still what I read" is
        expressed as an exact-match condition on the value just observed. Zero
        rows back means somebody else changed the row in between, which is how
        the caller detects a lost race instead of silently overwriting it.
        """
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        try:
            resp = requests.patch(f"{self._url}/rest/v1/{table}", json=data,
                                  params=filters,
                                  headers=self._headers("return=representation"),
                                  timeout=_TIMEOUT)
            if resp.status_code < 300:
                try:
                    return RpcResult(ok=True, data=resp.json(), status=resp.status_code)
                except ValueError:
                    return RpcResult(ok=True, data=[], status=resp.status_code)
            return RpcResult(ok=False, status=resp.status_code, error=resp.text[:300])
        except requests.RequestException as exc:
            log.error("supabase.update_checked_failed", table=table, error=type(exc).__name__)
            return RpcResult(ok=False, error=type(exc).__name__)

    def upsert(self, table: str, data: dict | list[dict],
               on_conflict: str | None = None) -> RpcResult:
        """Insert-or-update by natural key (PostgREST merge resolution).

        Used for the admin device-token registry: a token refresh upserts on
        (provider, token) rather than creating duplicates.
        """
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        params: dict[str, Any] = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        try:
            resp = requests.post(
                f"{self._url}/rest/v1/{table}", json=data, params=params,
                headers=self._headers("return=representation,resolution=merge-duplicates"),
                timeout=_TIMEOUT)
            if resp.status_code < 300:
                try:
                    return RpcResult(ok=True, data=resp.json(), status=resp.status_code)
                except ValueError:
                    return RpcResult(ok=True, data=None, status=resp.status_code)
            return RpcResult(ok=False, status=resp.status_code, error=resp.text[:300])
        except requests.RequestException as exc:
            log.error("supabase.upsert_failed", table=table, error=type(exc).__name__)
            return RpcResult(ok=False, error=type(exc).__name__)

    def insert(self, table: str, rows: dict | list[dict]) -> RpcResult:
        """Plain INSERT (no merge). Used for append-only evidence tables."""
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        try:
            resp = requests.post(f"{self._url}/rest/v1/{table}", json=rows,
                                 headers=self._headers("return=representation"), timeout=_TIMEOUT)
            if resp.status_code < 300:
                try:
                    return RpcResult(ok=True, data=resp.json(), status=resp.status_code)
                except ValueError:
                    return RpcResult(ok=True, data=None, status=resp.status_code)
            return RpcResult(ok=False, status=resp.status_code, error=resp.text[:300])
        except requests.RequestException as exc:
            log.error("supabase.insert_failed", table=table, error=type(exc).__name__)
            return RpcResult(ok=False, error=type(exc).__name__)

    def delete(self, table: str, filters: dict[str, Any]) -> bool:
        if not self.configured:
            return False
        try:
            resp = requests.delete(f"{self._url}/rest/v1/{table}", params=filters,
                                   headers=self._headers("return=minimal"), timeout=_TIMEOUT)
            return resp.status_code < 300
        except requests.RequestException as exc:
            log.error("supabase.delete_failed", table=table, error=type(exc).__name__)
            return False

    # ── Storage ──────────────────────────────────────────────────────────
    def storage_bucket_exists(self, bucket: str = _UPLOADS_BUCKET) -> bool:
        """Return whether a bucket exists without changing project state."""
        if not self.configured:
            return False
        try:
            resp = requests.get(f"{self._url}/storage/v1/bucket/{bucket}",
                                headers=self._headers(), timeout=_TIMEOUT)
            return resp.ok
        except requests.RequestException as exc:
            log.error("supabase.storage_bucket_check_failed",
                      bucket=bucket, error=type(exc).__name__)
            return False

    def ensure_public_uploads_bucket(self) -> RpcResult:
        """Create the public listing-media bucket once when it is missing.

        This is deliberately idempotent so a newly deployed project cannot
        leave every listing upload hanging just because an operator forgot the
        separate dashboard bucket setup step.  The backend service-role is the
        only writer; public access is read-only through the public bucket URL.
        """
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        try:
            current = requests.get(
                f"{self._url}/storage/v1/bucket/{_UPLOADS_BUCKET}",
                headers=self._headers(), timeout=_TIMEOUT,
            )
            if current.ok:
                return RpcResult(ok=True, status=current.status_code)
            if current.status_code != 404:
                return RpcResult(ok=False, status=current.status_code,
                                 error="storage bucket check failed")

            created = requests.post(
                f"{self._url}/storage/v1/bucket",
                json={
                    "id": _UPLOADS_BUCKET,
                    "name": _UPLOADS_BUCKET,
                    "public": True,
                    "file_size_limit": 26 * 1024 * 1024,
                    "allowed_mime_types": [
                        "image/jpeg", "image/png", "image/webp",
                        "video/mp4", "video/webm", "video/quicktime",
                    ],
                },
                headers=self._headers(), timeout=_TIMEOUT,
            )
            # A concurrent Render worker may have created it between our GET
            # and POST.  That is success, not an upload failure.
            if created.ok or created.status_code == 409:
                return RpcResult(ok=True, status=created.status_code)
            return RpcResult(ok=False, status=created.status_code,
                             error="storage bucket creation failed")
        except requests.RequestException as exc:
            log.error("supabase.storage_bucket_ensure_failed",
                      error=type(exc).__name__)
            return RpcResult(ok=False, error=type(exc).__name__)

    def upload_public_media(self, object_path: str, content: bytes,
                            content_type: str) -> RpcResult:
        """Store a validated media object and return its public URL.

        `object_path` is generated by the API; callers must not be able to
        choose another user's key or overwrite an existing object.
        """
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        bucket = self.ensure_public_uploads_bucket()
        if not bucket.ok:
            return bucket
        safe_path = quote(object_path.lstrip("/"), safe="/")
        headers = self._headers()
        headers["Content-Type"] = content_type
        headers["x-upsert"] = "false"
        try:
            resp = requests.post(
                f"{self._url}/storage/v1/object/{_UPLOADS_BUCKET}/{safe_path}",
                data=content, headers=headers, timeout=30,
            )
            if not resp.ok:
                return RpcResult(ok=False, status=resp.status_code,
                                 error="storage upload failed")
            return RpcResult(
                ok=True,
                data={"url": f"{self._url}/storage/v1/object/public/"
                             f"{_UPLOADS_BUCKET}/{safe_path}"},
                status=resp.status_code,
            )
        except requests.RequestException as exc:
            log.error("supabase.storage_upload_failed", error=type(exc).__name__)
            return RpcResult(ok=False, error=type(exc).__name__)

    # ── RPC calls (server-side Postgres functions from the migrations) ────────
    def rpc(self, fn: str, args: dict[str, Any]) -> RpcResult:
        if not self.configured:
            return RpcResult(ok=False, error="supabase not configured")
        try:
            resp = requests.post(f"{self._url}/rest/v1/rpc/{fn}", json=args,
                                 headers=self._headers("return=representation"),
                                 timeout=_TIMEOUT)
            if resp.ok:
                try:
                    return RpcResult(ok=True, data=resp.json(), status=resp.status_code)
                except ValueError:
                    return RpcResult(ok=True, data=None, status=resp.status_code)
            # Surface the Postgres error message for logging (may contain the
            # raised exception text from our RPCs). Never contains secrets.
            detail = ""
            try:
                detail = str(resp.json().get("message", ""))[:300]
            except ValueError:
                detail = resp.text[:300]
            return RpcResult(ok=False, status=resp.status_code, error=detail or f"http {resp.status_code}")
        except requests.RequestException as exc:
            return RpcResult(ok=False, error=type(exc).__name__)
