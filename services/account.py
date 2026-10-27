# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Account deletion / erasure service (Phase 10; fixes audit finding H6).

Google Play requires an in-app account-deletion path. This service performs a
two-stage, safe-by-default deletion:

  1. **Anonymize (always, reversible).** Calls the `request_account_deletion`
     Postgres RPC, which strips personal identifiers from the caller's own rows
     and records an auditable request. The financial ledger is preserved.

  2. **Purge (only when explicitly enabled, irreversible).** If the operator set
     `ACCOUNT_HARD_DELETE_ENABLED`, we then call the GoTrue admin endpoint to
     remove the `auth.users` row. This is gated behind an explicit toggle and is
     NOT the default, because purging an auth user can cascade through FKs and
     must only run after a backup + schema review + approval (mandate Rule 8).

The caller's identity is resolved by the API from the verified session and
passed in; this module never trusts a client-supplied id (Rule 9).
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from services.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 8


@dataclass
class DeletionResult:
    ok: bool
    status: str = ""            # 'anonymized' | 'purged' | 'failed'
    error: str = ""
    status_code: int = 200


class AccountService:
    def __init__(self, supabase, *, supabase_url: str, service_key: str,
                 hard_delete_enabled: bool = False):
        self._db = supabase
        self._url = (supabase_url or "").rstrip("/")
        self._key = service_key or ""
        self._hard_delete = hard_delete_enabled

    def delete_account(self, *, user_id: str, correlation_id: str | None = None) -> DeletionResult:
        if not user_id:
            return DeletionResult(ok=False, status="failed",
                                  error="user_id required", status_code=400)

        # ── Open orders need a person (money/fulfilment in flight) ─────────────
        open_orders = self._db.select(
            "orders",
            {"or": f"(buyer_id.eq.{user_id},farmer_id.eq.{user_id})",
             "status": "in.(pending,confirmed,in_transit)", "select": "id"},
            limit=1)
        if open_orders:
            return DeletionResult(
                ok=False, status="needs_review", status_code=409,
                error=("You have orders that are not finished yet, so we cannot delete your "
                       "account automatically. We have logged your request and will complete "
                       "it after those orders are settled."))

        # ── Stage 1: anonymize (reversible) ────────────────────────────────────
        res = self._db.rpc("request_account_deletion", {"p_user_id": user_id})
        if not res.ok:
            err = (res.error or "").lower()
            code = 403 if "forbidden" in err else 400
            log.warning("account.anonymize_failed", error=res.error,
                        correlation_id=correlation_id)
            return DeletionResult(ok=False, status="failed",
                                  error=res.error or "deletion failed", status_code=code)

        if not self._hard_delete:
            log.info("account.anonymized", correlation_id=correlation_id)
            return DeletionResult(ok=True, status="anonymized", status_code=200)

        # ── Stage 2: irreversible GoTrue purge (operator-enabled only) ─────────
        if not (self._url and self._key):
            log.error("account.purge_unconfigured", correlation_id=correlation_id)
            return DeletionResult(ok=False, status="anonymized",
                                  error="hard delete enabled but auth unconfigured",
                                  status_code=500)
        try:
            resp = requests.delete(
                f"{self._url}/auth/v1/admin/users/{user_id}",
                headers={"apikey": self._key, "Authorization": f"Bearer {self._key}"},
                timeout=_TIMEOUT)
        except requests.RequestException as exc:
            log.error("account.purge_error", error=type(exc).__name__,
                      correlation_id=correlation_id)
            # Data is already anonymized; report honestly that the purge failed.
            return DeletionResult(ok=False, status="anonymized",
                                  error="auth purge failed", status_code=502)

        if resp.status_code >= 300:
            log.error("account.purge_rejected", status=resp.status_code,
                      correlation_id=correlation_id)
            return DeletionResult(ok=False, status="anonymized",
                                  error="auth purge rejected", status_code=502)

        # Record the purge for audit (best-effort; service role).
        self._db.rpc("mark_account_purged", {"p_user_id": user_id})
        log.info("account.purged", correlation_id=correlation_id)
        return DeletionResult(ok=True, status="purged", status_code=200)
