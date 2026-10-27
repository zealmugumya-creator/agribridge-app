# AgriBridge — Production Audit Report

**Audited by:** Claude Code (baseline inspection)
**Audit date:** 2026-10-10
**Scope:** Full codebase at /home/claude/agribridge
**Auditor note:** This audit is based entirely on reading the source code. No live environment access was available. Findings about whether database migrations are actually applied, whether env vars are set in Render, or whether services are reachable are inferred from the code and the GO-LIVE.md checklist — not from live system probing.

> **2026-10-22 status update (read this before acting on F02):** The SPA sends
> produce, livestock and supplier checkout through `POST /api/orders`; migration
> 0015 makes that RPC price livestock/supplier items from their own catalogue
> tables. The client no longer inserts orders directly. **Both 0015 and 0002 are
> now applied live** (probes 2026-10-22: `orders.source_ref` returns `200`,
> `user_roles` exists and RLS closes public order/payout inserts). F02 is
> resolved. A real livestock/supply checkout order has not yet been placed
> against production. See `docs/OPERATIONS_SETUP.md` §2.

---

## Executive Summary

AgriBridge is a Flask-based agricultural marketplace for Uganda. The codebase is well-structured and has clearly had multiple rounds of security hardening (the internal comments reference audit findings by code — C1, C2, H1, etc. — showing prior review work). The core architecture is sound: server-side order creation with atomic stock reservation via a Postgres RPC, JWT + Supabase session auth, CORS restricted to known origins, and a pluggable payment provider system.

However, several meaningful issues remain. The most significant are:

1. **Migration 0002 (the RLS policy blocking direct order inserts) is deliberately held back** because the frontend still creates livestock and supplier orders via direct Supabase insert. Until that cutover is done, clients can bypass server-side price validation for those order categories.
2. **The admin dashboard (`/admin.html`) uses a shared-password JWT**, not the stronger Supabase-identity admin flow that the codebase already has built. GO-LIVE.md warns that the default password is `agribridge2026`.
3. **USSD session state is stored in-process memory (`USSD_SESSIONS` dict)** — this is lost on every Render restart and is not shared across the 2 gunicorn workers.
4. **The rate limiter is in-process memory** — same problem; not shared across workers and resets on restart.
5. **The CSP in index.html allows `unsafe-inline` scripts**, weakening XSS protection meaningfully for a financial app.
6. **Several env vars referenced in app.py are not in render.yaml**, relying entirely on the operator having set them manually in the Render dashboard — no infrastructure-as-code safety net.

Overall the backend is more hardened than most side-project-stage apps. The frontend is a single large HTML file with inline JS (~6,685 lines) which makes it hard to audit and harder to maintain.

---

## Severity-Rated Findings

| ID | Severity | Area | Finding | Evidence | Remediation |
|----|----------|------|---------|----------|-------------|
| F01 | HIGH | Auth | Admin dashboard uses shared-password JWT (`ADMIN_PASSWORD`), not Supabase-identity auth. GO-LIVE.md confirms default is `agribridge2026`. | `app.py:389-397`, `GO-LIVE.md §7` | Set a strong `ADMIN_PASSWORD` immediately. Fully migrate admin login to `require_admin_user()` (already built) and retire the password-based path. |
| F02 | HIGH | DB/RLS | **RESOLVED 2026-10-22.** Both migrations 0002 and 0015 are applied live (probes: `orders.source_ref` → `200`, `user_roles` exists; RLS closes public order/payout inserts). The frontend no longer inserts orders directly; `create_order_atomic` prices livestock/supplier server-side. | `0002_admin_identity_rls.up.sql` (header), `0015_category_order_pricing.up.sql` | Remaining: place one livestock and one supply order against production to confirm end-to-end pricing. |
| F03 | HIGH | Infra | `USSD_SESSIONS` dict is in-process. State is lost on Render restart and is NOT shared across 2 gunicorn workers, causing broken USSD flows mid-session. | `app.py:413` comment: "In-memory store (use Redis for production)" | Replace with Supabase `ussd_sessions` table or Redis. The table already exists in the schema — it just is not used for in-flight state. |
| F04 | MEDIUM | Security | CSP in `index.html` includes `'unsafe-inline'` in `script-src`. This permits inline script execution, which substantially weakens XSS protection for a financial application. | `index.html:5` | Move all inline JS to external files served from the same origin, then remove `unsafe-inline`. This is a significant refactor given the single-file architecture. |
| F05 | MEDIUM | Infra | Rate limiter (`_rl_buckets`) is in-process memory. Not shared across gunicorn workers; resets on restart. A determined attacker can bypass it by hitting different workers. | `app.py:113` | Replace with Redis-backed rate limiting (e.g. `flask-limiter` with Redis storage), or use Cloudflare rate limiting at the edge. |
| F06 | MEDIUM | Auth | `verify_token()` (used by admin panel routes) is a self-signed HS256 JWT. It is separate from `verify_supabase_user()` (used by user routes). This dual-auth system creates surface area: admin panel routes do not go through GoTrue identity verification. | `app.py:256-268` vs `app.py:275-305` | Migrate admin panel to `require_admin_user()` which uses GoTrue. The function already exists. |
| F07 | MEDIUM | Infra | `render.yaml` only defines `FLASK_ENV` and `SECRET_KEY`. All other required env vars (`JWT_SECRET`, `SUPABASE_KEY`, `ADMIN_PASSWORD`, `AT_*`, `FLW_*`, etc.) have no IaC representation. If the service is redeployed from scratch, these must be re-entered manually. | `render.yaml` (full file) | Add all required env vars to `render.yaml` as `sync: false` entries (value omitted; Render will prompt). This documents what is needed without storing secrets. |
| F08 | MEDIUM | Data | `ENFORCE_LEGAL_ACCEPTANCE` is `false` by default. The legal consent framework (migration 0007, `LegalService`) is built and wired but not enforced. Users can place orders without accepting terms. | `app.py:86`, `legal_gate()` | After migration 0007 is confirmed applied and the web app rollout is complete, set `ENFORCE_LEGAL_ACCEPTANCE=true` in Render per the GO-LIVE checklist. |
| F09 | MEDIUM | Typo/URL | The production domain used throughout the codebase is `agribrige.com` (one 'd' missing — "agribrige" not "agribridge"). This appears in USSD menus, SMS messages, CSP headers, `business-config.js`, and `services/config.py`. If the real domain is `agribridge.com`, every URL in user-facing content is wrong. | `app.py:479`, `business-config.js:9`, `config.py:51` | Confirm the intended domain. If `agribridge.com` is correct, do a global search-and-replace. If `agribrige.com` is intentional (possibly the registered domain), disregard. |
| F10 | MEDIUM | Backend | `supa_get`, `supa_insert`, `supa_update`, `supa_delete` in `app.py` are legacy raw HTTP helpers using `print()` for error logging and the service-role key. They bypass the `SupabaseClient` wrapper used elsewhere and will not feed structured logs or Sentry. | `app.py:176-244` | Migrate USSD and legacy routes to use `SUPA.*` (the structured client), or at minimum replace `print()` with `log.*` calls. |
| F11 | LOW | Security | `X-Correlation-ID` header from the client is trusted and set as `g.correlation_id` without sanitization. A client can inject arbitrary strings into structured log output. | `app.py:141-142` | Strip or validate the header value to alphanumeric + hyphens before logging it. |
| F12 | LOW | Auth | The `_auth_role()` function reads `role` from `user_metadata`, which in Supabase Auth is client-writable. A user could set their own role metadata to gain a different role assignment in the legal gate logic. | `app.py:1556-1558` | Use the server-controlled `user_roles` table (already built in migration 0002) rather than `user_metadata` for role determination. |
| F13 | LOW | Mobile | The Capacitor mobile app wraps `static/index.html` with no native bridge modules beyond SplashScreen and StatusBar. Push notifications, camera, biometrics, and deep links are not wired. No `targetSdkVersion` is set in `capacitor.config.json` — the default may not meet Play Store requirements (currently 34+). | `mobile/capacitor.config.json`, `mobile/package.json` | Set `android.targetSdkVersion: 34` in `capacitor.config.json`. |
| F14 | LOW | Testing | No end-to-end tests against the USSD flow. `tests/e2e/test_consent_ui.py` exists but there are no USSD handler tests. The USSD routing logic in `ussd_route()` (>650 lines) is entirely untested at the unit level. | `tests/` directory listing | Add unit tests for `ussd_route()` covering main paths and edge cases. |
| F15 | LOW | Data | `orders` table has both `total_amount` (a generated column = `quantity_kg * price_per_kg`) and `total_price` (a plain numeric column set by the RPC). These serve the same purpose and can drift. | `base-schema.sql:71,82` | Confirm which column is authoritative. If `total_price` (set by the RPC) is canonical, drop or document `total_amount`. |
| F16 | LOW | Data | `animal_listings` table has no `is_available`, `reserved_qty`, or stock lifecycle columns that `listings` has. Livestock orders via direct insert cannot benefit from the atomic stock reservation logic. | `base-schema.sql:192-200` | Add `is_available`, `reserved_qty`, and `stock_status` to `animal_listings` and extend `create_order_atomic` to cover it. |
| F17 | LOW | Infra | `match_eligible_farmers` SQL function references `public.farmer_ratings` table, which does not appear in `base-schema.sql` or any migration. If this table does not exist, the matching function will error at runtime. | `0004_farmer_matching.up.sql:107` | Create the `farmer_ratings` table in a migration, or add a `LEFT JOIN ... ON false` fallback. |
| F18 | LOW | Auth | `pay_initiate` endpoint (`POST /api/pay/initiate`) does not require authentication. An unauthenticated caller can initiate a Flutterwave payment session for any `order_ref` and `amount` they supply. | `app.py:1780-1802` | Add `verify_supabase_user()` guard. The caller should own the order being paid. |

---

## All API Routes Inventory

| Method | Path | Auth Required | Rate Limited | Notes |
|--------|------|---------------|--------------|-------|
| GET | `/` | No | No | Health check (alias for /health) |
| GET | `/health` | No | No | DB connectivity check; returns 503 if DB unreachable |
| GET | `/health/live` | No | No | Process liveness only |
| POST | `/api/admin/login` | No (password) | Yes (5/5min) | Shared-password JWT issue (F01/F06) |
| GET | `/api/admin/verify` | JWT (admin role) | No | Verifies admin JWT |
| GET | `/api/admin/stats` | JWT (admin role) | No | Farmer/listing/order counts |
| GET | `/api/admin/listings` | JWT (admin role) | No | Up to 200 listings |
| GET | `/api/admin/orders` | JWT (admin role) | No | Up to 200 orders |
| GET | `/api/admin/farmers` | JWT (admin role) | No | Up to 500 farmers |
| PATCH | `/api/admin/order/<id>/status` | JWT (admin role) | No | Order transition via `set_order_status` state machine |
| GET | `/api/admin/animals` | JWT (admin role) | No | Up to 300 animal listings |
| GET | `/api/admin/deliveries` | JWT (admin role) | No | Up to 300 deliveries |
| GET | `/api/admin/supplies` | JWT (admin role) | No | Supplier products |
| POST/PATCH/DELETE | `/api/admin/row` | JWT (admin role) | No | Generic table editor; column allowlist enforced (order/payment status rejected) |
| POST | `/api/admin/devices` | Supabase + admin UUID | Yes (30/min) | FCM device registration |
| DELETE | `/api/admin/devices` | Supabase + admin UUID | Yes (30/min) | FCM device unregistration |
| POST | `/api/worker/match` | Supabase + admin UUID | No | Manual matching trigger |
| POST | `/api/ussd` | No (AT platform) | No | Africa's Talking USSD callback |
| POST | `/api/sms/delivery` | No | No | AT SMS delivery webhook |
| GET | `/api/prices` | No | No | Public price data |
| POST | `/api/crop-doctor` | No | Yes (15/5min) | Gemini AI diagnosis |
| POST | `/api/ai` | No | Yes (20/5min) | Groq AI assistant |
| POST | `/api/orders` | Supabase session | Yes (20/min) | Server-authoritative order creation |
| POST | `/api/orders/<id>/status` | Supabase session | Yes (30/min) | Order state machine transition |
| POST | `/api/requests` | Supabase session | Yes (20/min) | Purchase request creation |
| POST | `/api/offers/<id>/accept` | Supabase session | Yes (30/min) | Accept a farmer offer |
| POST | `/api/offers/<id>/reject` | Supabase session | Yes (30/min) | Reject a farmer offer |
| POST | `/api/sms-receipt` | Supabase session | Yes (10/5min) | Order SMS to the buyer's number on file (no free-form recipient) |
| GET | `/api/pay/providers` | No | No | Lists enabled payment providers |
| POST | `/api/pay/initiate` | Supabase session | Yes (20/5min) | Initiate payment session (order ownership enforced) |
| POST | `/api/pay/webhook/flutterwave` | verif-hash header | No | Flutterwave payment webhook |
| GET | `/api/legal/manifest` | No | Yes (60/min) | Legal document manifest |
| GET | `/api/legal/status` | Supabase session | Yes (60/min) | User's acceptance status |
| POST | `/api/legal/accept` | Supabase session | Yes (20/min) | Record terms acceptance |
| POST | `/api/legal/sync-signup` | Supabase session | Yes (10/min) | Sync signup acceptance |
| GET | `/api/legal/history` | Supabase session | Yes (30/min) | Acceptance history |
| GET/POST | `/api/marketing/consent` | Supabase session | Yes (30/min) | Marketing consent read/write |
| GET/POST | `/api/privacy/requests` | Supabase session (GET), optional (POST) | Yes (5/5min) | Privacy requests |
| POST | `/api/account/delete` | Supabase session | Yes (5/5min) | Account deletion/anonymization |
| GET | `/legal/<name>` | No | No | Serves static legal HTML pages |

---

## Database Tables Inventory

### Base Schema Tables

| Table | Purpose | RLS | Notable Issues |
|-------|---------|-----|----------------|
| `farmers` | Farmer profiles | Enabled | OTP columns column-level revoked from anon/authed |
| `buyers` | Buyer profiles | Enabled | `buyer_id` in orders references this, but auth.uid() is a Supabase Auth UUID — FK linkage is unclear |
| `listings` | Crop listings | Enabled | Extended by migration 0001 with `reserved_qty`, `stock_status` |
| `orders` | Purchase orders | Enabled | Dual total columns (F15); direct insert policy from migration 0002 not yet applied |
| `deliveries` | Delivery records | Enabled | Publicly readable (all deliveries visible to all) |
| `reviews` | Buyer reviews | Enabled | Verified-purchase guard on insert |
| `payouts` | Farmer payouts | Enabled | No public write; service_role only |
| `platform_config` | Commission config | Enabled | Single-row table; public read |
| `market_prices` | Live price data | Enabled | Public read |
| `training_videos` | Training content | Enabled | Public read |
| `contact_messages` | Contact form submissions | Enabled | Public insert |
| `supply_orders` | Input supply orders | Enabled | Public insert; no auth guard |
| `disease_reports` | Disease reports | Enabled | Public insert; no auth guard |
| `price_alerts` | SMS price alerts | Enabled | Public insert; no auth guard |
| `ussd_sessions` | USSD session log | Enabled | No SELECT policy (correct — was deliberately excluded) |
| `animal_listings` | Livestock listings | Enabled | Missing stock lifecycle fields (F16) |
| `cart_sessions` | Browser cart state | Enabled | No RLS policy defined — likely all rows blocked or service-role only |
| `fraud_flags` | Fraud events | Enabled | `fraud_admin_only` policy blocks all public access |
| `supplier_products` | Agri-input products | Enabled | Owner insert/update/delete |
| `vet_bookings` | Vet booking requests | Enabled | Public insert |
| `community_posts` | Community forum | Enabled | Public insert, public read |

### Migration-Added Tables

| Table | Migration | Purpose |
|-------|-----------|---------|
| `order_events` | 0001 | Idempotency + order audit log |
| `stock_changes` | 0001 | Stock mutation audit trail |
| `user_roles` | 0002 | Server-controlled admin roles |
| `admin_audit_log` | 0002 | Admin action audit log |
| `notification_outbox` | 0003 | Durable async notification queue |
| `purchase_requests` | 0004 | Buyer purchase requests (pre-match) |
| `order_offers` | 0004 | Farmer offer lifecycle |
| `matching_config` | 0004 | Configurable matching weights |
| `legal_document_versions` | 0007 | Published legal document versions (immutable) |
| `legal_publication_log` | 0007 | Who published what |
| `legal_acceptances` | 0007 | User acceptance evidence (append-only) |
| `marketing_consent_events` | 0007 | Consent history (append-only) |
| `marketing_consent_current` | 0007 | View: latest consent per user/channel |
| `privacy_requests` | 0007 | GDPR/PDPA-style data requests |

### Missing or Assumed Tables

| Table | Where Referenced | Status |
|-------|-----------------|--------|
| `farmer_ratings` | `match_eligible_farmers` SQL function | **Not in any schema file** (F17) |
| `price_data` | `GET /api/prices` in app.py | Not in base-schema.sql (may be `market_prices` under a different alias or missing) |

---

## Security Findings Summary

| Category | Finding |
|----------|---------|
| Authentication | Dual auth systems (shared-password JWT for admin, GoTrue session for users) creates split surface. Admin panel should migrate fully to Supabase identity. |
| Authorization | Migrations 0002 and 0015 are both applied live (2026-10-22); direct PostgREST order writes are closed at the DB and livestock/supplier pricing runs server-side. |
| CSP | `unsafe-inline` in `script-src` weakens XSS protection. Single-file frontend architecture makes this hard to fix. |
| Rate limiting | In-process memory; not shared across workers; resets on restart. |
| Input handling | Correlation ID from request header is logged without sanitization. |
| Role trust | `_auth_role()` reads from client-writable `user_metadata` rather than server-controlled `user_roles`. |
| Payment | `pay_initiate` endpoint unauthenticated — any caller can initiate a payment session. |
| USSD | No validation of Africa's Talking request origin (no signature/secret check on USSD or SMS delivery webhooks). |
| Secrets in code | No secrets hardcoded. Default Supabase URL (`lvloaweijidgcuezdnmu.supabase.co`) is in `config.py` and `index.html` — this is the project ID, semi-public but worth noting. |
| Security headers | Backend sends `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`. Missing `Strict-Transport-Security` (HSTS) and `Content-Security-Policy` on API responses. |

---

## What Is Working (Evidence-Based)

- **Flask app boots** with fail-closed config validation: missing `JWT_SECRET` or `SUPABASE_KEY` in production raises `ConfigError` and refuses to start.
- **Health endpoint** does a real DB ping (not just a key presence check).
- **Server-side order creation** via `create_order_atomic` Postgres RPC: price recomputed from DB, stock reserved atomically, idempotency key supported.
- **Order state machine** enforced server-side via `set_order_status` RPC with legal transition checks.
- **Supabase GoTrue identity verification** used for all user-facing protected routes — buyer_id comes from the verified session, not the request body.
- **Flutterwave webhook** validates `verif-hash` with constant-time compare and re-verifies with Flutterwave before marking paid.
- **Admin login rate limited** to 5 attempts per 5 minutes.
- **RLS is enabled** on all tables in the schema.
- **Legal consent framework** (migration 0007) is fully built with append-only triggers and hash verification.
- **Account deletion** is implemented with reversible anonymization by default; hard delete opt-in.
- **CORS** restricted to known origins; no wildcard.
- **Structured logging** with correlation IDs on every request.
- **Sentry integration** for unhandled exceptions (when `SENTRY_DSN` is set).
- **FCM push** for admin notifications (when credentials are set).
- **Test suite** covers health, order creation authz, state machine, admin identity, webhook auth, and push device registration.

## What Is Confirmed Incomplete

- Migration 0002 (direct-insert block on orders) is deliberately not applied.
- `ENFORCE_LEGAL_ACCEPTANCE` is `false` — legal gate is built but inactive.
- Africa's Talking is in sandbox mode (SMS and USSD are no-ops until live keys are set).
- Flutterwave payment is off (no keys set in template).
- Pesapal and MTN MoMo are structurally wired but return "not configured yet".
- Email receipts (Resend / `send-receipt` Edge Function) require a key to activate.
- The `farmer_ratings` table referenced in the matching function does not exist in any schema file.
- The mobile app is a Capacitor wrapper of the web HTML — no native push, biometrics, or deep links.
- `targetSdkVersion` not set in `capacitor.config.json` (Play Store requires 34+).
- Payout to farmers on delivery is not implemented.
- Admin super-admin UUID assignment (the SQL block in migration 0002) is marked as a manual step that must be done after the migration is applied.

---

## Missing Features vs Spec

| Feature | Status |
|---------|--------|
| Crop marketplace (buy/list) | Built (frontend + backend) |
| Animal/livestock marketplace | Built; server-priced via `create_order_atomic` (migration 0015 applied live 2026-10-22) |
| USSD *789# | Built; in-memory session state (production risk) |
| SMS notifications | Wired; sandbox mode |
| AI Crop Doctor (Gemini) | Built; requires `GEMINI_API_KEY` |
| AI Assistant (Groq) | Built; requires `GROQ_API_KEY` |
| Farmer-buyer matching / offer routing | DB + service layer built; worker not running continuously |
| Mobile app (Android/iOS) | Capacitor shell built; no native features |
| Payments (Flutterwave) | Built; requires keys |
| Payments (MTN MoMo, Pesapal) | Stubs only |
| Order tracking | Frontend built; backend state machine built |
| Legal consent / GDPR requests | Built; not enforced |
| Admin dashboard | Built; shared-password auth (security concern) |
| Email receipts | Wired to Edge Function; requires Resend key |
| Farmer payouts | Built (0016): auto-created on delivered+paid, admin settle route posts the journal; migration not applied live |
| Database backups | Described in GO-LIVE.md; not automated in code |
| Cold chain / finance / SACCO | Frontend UI screens exist; no backend routes found |
