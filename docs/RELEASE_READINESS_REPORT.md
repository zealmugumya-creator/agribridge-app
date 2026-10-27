# AgriBridge 2.0 — Release-Readiness Report

> **2026-10-22 note:** This is the 2026-10-09 baseline report. Its “85 tests”
> and migration-status statements are historical. Current verified evidence is
> in [`docs/TEST_RESULTS.md`](TEST_RESULTS.md) (434 Python tests including 22
> Playwright tests, 129 SQL assertions) and
> [`docs/IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md). Live probes show
> 0013, 0014, 0002, 0015, 0016 and 0017 applied (`orders.source_ref` returns
> `200`). The new SPA checkout bundle is deployed. Push `6077883` adds a
> safe admin-verified Bank Transfer method and fixes duplicate Discord signup
> alerts. Pesapal and Google sign-in are implemented but unconfigured and
> unverified live.

Date: 2026-10-09 · Track executed first: **Backend integrity & security** · Scope selection: **all** (frontend + order-routing/async included in plan; backend delivered this pass)

This is the mandate's final-report deliverable. It states plainly what was **built and verified**, what is **built but not yet verifiable here**, and what **remains** before AgriBridge can be called production- and Play-ready. Per Rule 12, the application is **not** declared production-ready: the acceptance tests that require live infrastructure have not been run.

---

## 1. Verification boundary (read first)

The live Supabase database, Render, Firebase, Discord, Sentry, and Flutterwave were **not reachable** from this environment. Therefore:

- ✅ What **is** verified here: the Python service/API layer — **85 pytest tests pass**, **ruff lint clean**, all modules parse. These cover validation, idempotency, retry/dead-letter, authz, and HTTP wiring with the DB and third parties faked.
- 🟡 What is **implemented but not live-verified**: all SQL migrations, RLS policies, the durable worker against real Postgres, FCM/Discord/Sentry/Flutterwave delivery, and the Android `.aab` build.
- ⛔ Nothing was **fabricated**: no migration, deployment, payment, push, or test result is claimed to have succeeded against real systems (Rule 3, Rule 11).

---

## 2. What was delivered

### Backend integrity (audit C1, C2, C4) — the core risk reduction
- `services/orders.py` + `create_order_atomic` RPC: **price is recomputed server-side** from the listing and **stock is reserved atomically** (`UPDATE … WHERE quantity >= n`, row locked `FOR UPDATE`). A tampered client cannot set its own total or oversell. Idempotency keys prevent double-create.
- `set_order_status` RPC enforces a legal state machine (pending → confirmed/cancelled → in_transit → delivered), releasing reservations on cancel.
- New API: `POST /api/orders`, `POST /api/orders/<id>/status`.

### Admin identity & RLS (audit C3, C5) — Phase 3
- `user_roles` + `is_admin()` + `assign_role()` (service-role only) + `admin_audit_log`.
- RLS closes the public INSERT path on `orders`/`payouts`; owner/admin read scoping.
- Backend resolves identity from the **verified Supabase session** (`verify_supabase_user`), never the request body; `require_admin_user()` gates admin routes.
- Super-admin assignment for `zealmugumya@gmail.com` is **manual by verified UUID** — deliberately not automated (Rule 9).

### Durable async + order routing (audit H3) — Phases 5
- `notification_outbox` (transactional outbox) + `claim_notifications` (`FOR UPDATE SKIP LOCKED`) + `resolve_notification` (exponential backoff, dead-letter).
- Farmer matching: `purchase_requests` (distinct from paid orders), `match_eligible_farmers` (filters unverified/suspended/stale/insufficient, ranks by weighted score), `order_offers` with response deadlines, `accept_offer` (atomic reservation + sibling expiry), `expire_overdue_offers`.
- `services/worker.py`: independent durable loop (SIGTERM-safe) draining notifications + periodic matching. Deployable as a Render Background Worker.
- New API: `POST /api/requests`, `/api/offers/<id>/accept|reject`, admin-only `/api/worker/match`.

### Admin push (audit H4) — Phase 6
- `services/push.py`: FCM **HTTP v1** sender (service-account RS256 → OAuth2 → send) + `AdminDeviceRegistry` over `admin_devices`. Dead tokens auto-pruned. Lock-screen-safe payloads.
- New API: `POST|DELETE /api/admin/devices` (verified admins only). Safe no-op until Firebase creds are set.

### Ops integrations (audit H5) — Phases 7, 8
- `services/discord.py`: env-only webhook, bounded retries, 429 handling, **dead-letter fallback** to admin push so a critical alert is never lost.
- `services/monitoring.py`: Sentry with request/PII scrubbing; no-op without a DSN.
- `services/logging.py`: JSON logs, correlation IDs (`X-Correlation-ID`), secret redaction.
- `services/config.py`: **fails closed** in production without `JWT_SECRET`/`SUPABASE_KEY` (H1); no localhost CORS in production (M6).
- Real `/health` (DB round-trip → 200/503) + `/health/live` (M6).

### Privacy & Play blocker (audit H6) — Phase 10
- Migration 0006 + `services/account.py` + `POST /api/account/delete`: self-scoped erasure. **Default is reversible anonymization** preserving the financial ledger; the irreversible GoTrue purge is gated behind `ACCOUNT_HARD_DELETE_ENABLED` and requires backup + FK review + approval (Rule 8). Schema-defensive (works without knowing exact columns; won't touch unrelated apps — Rule 5).

### Testing & CI (audit H7) — Phase 11
- 85 tests across orders, matching, notifications, discord, push, account, config/logging, and the API surface.
- `.github/workflows/ci.yml`: ruff + (non-blocking) mypy + pytest. `ruff.toml`, `pytest.ini`, `requirements-dev.txt`, `.env.example` (names only).

### Docs
- `docs/PRODUCTION_AUDIT.md`, `docs/IMPLEMENTATION_STATUS.md`, `docs/OPERATIONS_SETUP.md`, `docs/PLAY_STORE_RELEASE.md`, updated `README.md`, `.env.example`.

---

## 3. Preservation guarantees honoured (Rules 1, 2, 6, 7)

- The existing vanilla SPA, USSD/SMS, AI, and the **already-correct** Flutterwave webhook (hash check + server-side re-verification) were **left working**, not rewritten.
- All migrations are additive, reversible (`.up`/`.down`), and guarded (`IF NOT EXISTS`, `to_regclass`, column-existence checks).
- The weekly encrypted backup workflow is preserved and is the prerequisite before applying migrations.
- Legacy lint debt in `app.py` (E741/B005) was **not** rewritten (Rule 1); it is tracked in `ruff.toml` per-file-ignores for a dedicated cleanup.

---

## 4. What remains before "production-ready"

**Blocked on credentials/access (owner action):**
1. Apply migrations 0001–0006 to Supabase (after backup); verify with the Advisors security report.
2. Assign the super-admin UUID for `zealmugumya@gmail.com`; enable MFA.
3. Set `DISCORD_WEBHOOK_URL`, `SENTRY_DSN`, FCM creds, Flutterwave sandbox keys; verify each channel live.
4. Deploy `python -m services.worker` as a Render Background Worker.
5. Add **SQL integration tests** (pgTAP or ephemeral Postgres) — currently not possible without a DB; **not faked**.

**Blocked on build work (Track A, no creds needed):**
6. **Phase 2** — React + TypeScript app migrating the SPA screen-by-screen onto the new server-side APIs; Capacitor 7 + Android API 36; signed `.aab` (see `docs/PLAY_STORE_RELEASE.md`). Not started.
7. **Phase 12** — admin command center UI on the Phase 3 authz primitives.
8. ✅ Account-deletion **UI** wired in the web settings modal → `POST /api/account/delete` (double-confirm); publish the web deletion URL in the Play listing.
9. Repeatable CI `.aab` publish workflow (needs signing secrets + Android runner to validate).

---

## 5. Release-readiness verdict

| Area | Verdict |
|---|---|
| Backend order/stock integrity | 🟡 Implemented + unit-verified; **pending live DB migration + integration tests** |
| Admin authz / RLS | 🟡 Implemented; **pending migration apply + UUID assignment + MFA** |
| Async routing / worker | 🟡 Implemented; **pending worker deployment + live DB** |
| Push / Discord / Sentry | 🟡 Implemented; **pending credentials** |
| Payments integrity | 🟡 Server-side totals enforced; **pending Flutterwave sandbox E2E** |
| Account deletion (Play) | 🟡 Backend done; **pending UI + Play listing** |
| Testing / CI | ✅ 85 tests + lint green in CI config |
| React app / Android `.aab` | ⬜ Not started |

**Conclusion:** The highest-risk backend integrity and security gaps (C1–C5, H1–H7) are **addressed in code and verified offline**, with every external dependency implemented safely behind env-only configuration and documented for activation. The platform is **not yet production- or Play-ready**: that requires applying/verifying migrations against the live database, wiring the listed credentials, deploying the worker, and completing the Phase 2 React/Android build. No step has been claimed complete that was not actually verified.
