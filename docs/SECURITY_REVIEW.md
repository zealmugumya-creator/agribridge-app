# Security Review — PostgREST exposure audit

Date: 2026-10-21
Project: `lvloaweijidgcuezdnmu` (`https://lvloaweijidgcuezdnmu.supabase.co`)
Scope: anonymous/authenticated access to internal tables reported by the administrator.
Method: read-only PostgREST probes using the public publishable key, plus a
non-mutating write probe (an insert with a NULL required column). No production
rows were created, changed, or deleted.

## Confirmed findings

### 1. CRITICAL — `system_events` readable and writable by anonymous users

Evidence collected before the fix:

- `GET /rest/v1/system_events?select=*&limit=1` returned `200` with an internal
  event (`order_failed`, buyer UUID, error details).
- `POST /rest/v1/system_events` with `{"event_type": null}` returned PostgreSQL
  error `23502` (NOT NULL violation), not a permission error. This proves the
  `anon` role held INSERT privilege and the row-level policy allowed the write.

Root cause: `migrations/0008_transporters_and_system_events.sql` created

```sql
create policy "service role full access" on public.system_events
  using (true) with check (true);
```

Without a `TO service_role` clause, a policy applies to `PUBLIC` — every role,
including `anon`. Supabase's default table grants then made the policy effective.

Impact: anyone holding the public key could read operational events (including
buyer identifiers and failure details), forge events, or delete the error log.

Fix:

- `supabase/migrations/0014_lockdown_internal_tables.up.sql` drops the unscoped
  policy, creates a `service_role`-only policy, and revokes all privileges from
  `public`, `anon`, and `authenticated`.
- The legacy `migrations/0008_transporters_and_system_events.sql` script was
  patched with `FOR ALL TO service_role` so re-running it cannot reopen the hole.
- Covered by `tests/sql/security_lockdown.test.mjs`.

### 2. HIGH — `transporters` fully exposed to anonymous users

Same unscoped policy on `public.transporters`. The table was empty during the
audit, so no data leaked in the sample, but `anon` held full table privileges
and the policy allowed every row. Authenticated owner policies also existed, but
they grant access rather than restrict the PUBLIC policy.

Fix in 0014:

- `service_role` keeps full access.
- `authenticated` keeps `select`/`update` with existing owner-only policies
  (`supabase_uid = auth.uid()`); `insert`/`delete` are revoked.
- `anon` loses all privileges.

No client code reads or writes these tables directly; transporter management is
server-side (`service_role`) and transporter self-service uses the authenticated
owner policies.

### 3. HIGH — SMS endpoints were an unauthenticated relay

`POST /api/notify-order` and `POST /api/sms-receipt` required no session. Anyone
could name a farmer id or a phone number and make AgriBridge send SMS to it on
AgriBridge's Africa's Talking account. That is an SMS-toll abuse vector and lets
an attacker text arbitrary numbers with AgriBridge branding (the receipt text
also echoed a client-supplied total).

Fix:

- `/api/notify-order` is removed. Farmer SMS is already sent server-side inside
  `POST /api/orders` after the order is committed, so no functionality is lost.
- `/api/sms-receipt` now requires a verified Supabase session. It accepts only an
  order reference; recipient phone and amount come from the caller's own order
  rows. Unknown and foreign orders both return `404` (no order-existence
  oracle). Payment receipts are sent only by the verified payment path.
- The browser no longer sends a phone number or total for receipts.

Evidence: `tests/test_identity_and_admin.py` (anonymous `401`, foreign order
`404`, body-supplied phone/total ignored, removed endpoint `404`).

### 4. HIGH — admin generic editor could rewrite order and payment state

`POST/PATCH /api/admin/row` allowed `orders.status` and `orders.payment_status`
in its column allowlist. An admin token could move an order to `delivered`
without `set_order_status` (skipping stock release and `order_events`) or mark
it `paid` without any gateway verification or ledger posting.

Fix:

- Both columns are removed from the generic allowlist; a request naming either
  is rejected with `400` instead of being silently dropped.
- `PATCH /api/admin/order/<id>/status` now calls the `set_order_status` Postgres
  state machine (`404` for a missing order, `409` for an illegal transition) and
  records a `order_status_changed` system event.
- The admin console edits only delivery address and notes, and advances status
  through a dedicated dropdown that calls the state-machine endpoint.
- The farmer dashboard had the same bypass from the other side: its Confirm /
  Reject / Deliver buttons called `sbUpdate('orders', {status})` directly from
  the browser. They now call `POST /api/orders/<id>/status`, which verifies the
  session, checks that the caller is the order's farmer (or an admin), and runs
  `set_order_status`; the UI follows the legal `confirmed → in_transit →
  delivered` path instead of jumping straight to delivered.
- Payment status is no longer writable from any generic editor. It changes only
  when a gateway payment is verified, or when a `cash` order is delivered and
  the COD journal posts first (`cod:<ref>`); an unpaid gateway order stays
  unpaid at delivery confirmation. See [`docs/FINANCIAL_LEDGER.md`](FINANCIAL_LEDGER.md) §3.

Evidence: `tests/test_identity_and_admin.py`.

### 5. MEDIUM — transporter GPS writes overwrote finished deliveries silently

`POST /api/transporter/location` updated every delivery row for the transporter,
including delivered and failed ones, and ignored the result of both writes. A
transporter could move the recorded end position of historical deliveries, and a
database outage still returned `{"ok": true}`.

Fix: only `picked_up`, `in_transit` and `at_checkpoint` deliveries are updated
(by row id), completed rows are untouched, and a failed write returns `503`.

Evidence: `tests/test_identity_and_admin.py`.

### 6. Settlement integrity controls (migration 0016)

New admin mutation, new invariants. A payout can only be marked paid when the
settlement journal exists; the endpoint refuses to create that journal without
a posted customer receipt and an operator-supplied provider reference.

* Admin token required (`verify_token(required_role='admin')`); payout id must
  be a UUID.
* The order must still be `delivered` and `paid`, and the payout gross must
  equal the server-computed order total.
* Exactly one posted receipt (`customer_payment` / `cod_collection`) must exist
  for the checkout. The settlement clears the same asset account the receipt
  debited (`PROVIDER_CLEARING` or `COD_CASH_HELD`), not a client-supplied one.
* `settlement:<payout_id>` is unique and idempotent; `payment_ref` is null so
  reconciliation cannot mistake it for a second receipt.
* `mark_payout_paid()` re-checks the posted journal in SQL and is executable
  only by `service_role`; anon/authenticated are revoked.
* `payouts` remain backend-only (RLS; 0002 closes direct inserts).
* A provider/bank reference is required as evidence of money movement. The
  system does not claim the transfer is verified by the provider.

Evidence: `tests/test_settlements.py` (23 tests) and
`tests/sql/settlements.test.mjs` (11 assertions).

### 7. Pesapal gateway surface (migration 0017)

Pesapal adds a hosted checkout, an IPN endpoint and recurring-charge recording.
The security boundary is the same as Flutterwave's: callback data is untrusted.

* Consumer key/secret exist only in the API environment. The browser learns
  only a boolean from `GET /api/pay/providers`; tokens and secrets are never
  logged or returned (`services/pesapal.py`).
* Every callback and IPN calls `GetTransactionStatus` server-side. Completion,
  amount, currency and merchant reference come from that response, not from the
  request. A forged callback cannot mark an order paid.
* The echoed reference must match the base checkout reference; currency must be
  explicitly UGX; the verified amount must be at least the server-computed order
  total. Duplicate notifications are idempotent through `payment:<ref>`.
* The gateway redirect URL is accepted only over HTTPS on `pesapal.com` or a
  subdomain; tracking ids are length/charset checked; registered IPN URLs must
  be HTTPS (or localhost in development).
* `payment_gateway_state`, `payment_subscriptions` and `subscription_charges`
  have RLS enabled, `service_role` policies, and no privileges for `anon` or
  `authenticated`. `tests/sql/pesapal.test.mjs` asserts the denial after
  applying the migration twice.
* A recurring notification never creates an order, a farmer payable or a
  payout. It posts provider cash to `SUSPENSE_OVERPAYMENT` and is unique on
  `(provider, provider_tracking_id)` and `recurring:<tracking_id>`.
* `POST /api/pay/initiate` remains session- and ownership-checked and keeps the
  server-side amount floor; the callback route is rate-limited.
* `GET /api/admin/finance/recurring` requires an admin token; without 0017 it
  returns a 503 that names the migration rather than exposing database detail
  broadly.

**Live schema verified 2026-10-22:** 0017 is applied. Read-only PostgREST
probes with the publishable key return `401` / `42501` for
`payment_gateway_state`, `payment_subscriptions` and `subscription_charges`,
so the service-role-only posture is effective. The payment flow itself is still
unverified because no live Pesapal credentials exist; see
`docs/PAYMENTS_PESAPAL.md` §8.

### 8. Google sign-in and onboarding (no migration)

* Provider availability is read from Supabase `/auth/v1/settings`; the buttons
  stay hidden when Google is not configured, so the UI cannot imply a working
  provider.
* `signInWithOAuth` is called with `provider: "google"` and the current origin
  as `redirectTo`. No access token, client secret or user identity is
  constructed client-side beyond what Supabase returns.
* A first-time Google user must complete a locked onboarding modal (no close
  button, no overlay dismissal, Escape excluded). It validates a Ugandan phone,
  account type, district and explicit terms acceptance before the app is usable.
* The onboarding role list is limited to farmer/vendor/hotel/supplier. Admin
  authority still comes only from `user_roles` / `ADMIN_ALLOWED_UUIDS`; choosing
  a role in metadata does not grant admin.
* Profile and legal writes go through the verified-session endpoints
  (`/api/user/profile`, `/api/legal/sync-signup`); the backend derives the user
  id from the token.
* `tests/test_pesapal_ui.py` scans the browser assets for `service_role`,
  `sb_secret_`, the Supabase service key, Pesapal/Flutterwave secrets and
  `client_secret`; the Playwright suite asserts the locked-modal and OAuth
  argument behaviour.

**Not yet verified live:** the real Google consent screen was not exercised;
the browser tests stub Supabase OAuth. Owner setup and acceptance checks are in
`docs/GOOGLE_SIGNIN.md` §3.

### 9. Bank transfer — offline, admin-verified (no migration)

* Bank Transfer was removed earlier because the UI displayed a **fabricated**
  Stanbic account number. It is restored without any hardcoded account: real
  details render only from the `BANK_TRANSFER_ACCOUNTS` env var served through
  `/api/pay/providers`. With the variable empty the SPA shows official support
  contact, never an invented number. `tests/test_bank_transfer.py` scans source
  for the old fabricated digits to prevent regression.
* Selecting bank does **not** mark the order paid and does not start an online
  charge; the order persists `payment_method='bank'`, `payment_status='unpaid'`.
* The only path that records bank money is
  `POST /api/admin/orders/<id>/verify-bank-transfer`. It requires an admin JWT
  (`verify_token(required_role='admin')`); role and identity are taken from the
  token, never the request.
* Defense-in-depth on that endpoint: rejects a short/blank reference (400), an
  order whose stored `payment_method` is not `bank` (409), and a cancelled order
  (409). The **amount** is re-read from the stored order rows sharing the
  `payment_ref` (split orders verified whole), never from the client body.
* Settlement reuses `_mark_checkout_paid`, which posts the idempotent
  `payment:<ref>` ledger journal, so a double-click or replay cannot double-post
  and cannot inflate revenue. A failed ledger post returns 503 and logs
  `bank_verify_failed` without marking paid; success writes an `admin_audit_log`
  row and an `order_bank_transfer_verified` system event.

**Not yet verified live:** no real bank order has been placed and settled against
production; behavior is proven by faked transport in the 14 tests.

## Verified as not exploitable

The following tables returned zero rows to `anon` and their policies are
correctly scoped (`TO service_role` / owner checks):

`admin_audit_log`, `admin_devices`, `notification_outbox`, `orders`,
`user_roles`, `web_push_subscriptions`, `payouts`, `contact_messages`,
`account_deletion_requests`, `privacy_requests`.

Note: a `has_table_privilege(...)` query can return `true` because Supabase
grants table privileges by default while RLS still filters rows. Grants alone do
not prove exploitability; the probes above test actual access. `system_events`
was the exception because its policy explicitly allowed every role.

## Open findings (not changed in this migration)

| Severity | Finding | Recommendation |
|---|---|---|
| Medium | `farmers` is intentionally public (`farmers_public_select`) and exposes `email`, `phone`, full name, village/sub-county. No web/mobile code reads it directly today. | Decide whether buyer-facing profiles need `email`. If not, revoke column `select (email)` from `anon, authenticated`, or serve profiles through a narrow public view. |
| Low | `platform_config` is public and exposes `commission_pct` and `payout_note`. No secrets present. | Keep public only if the app needs it; otherwise move to a safe view. |
| Info | `legal_document_versions`, `listings`, `animal_listings`, `market_prices`, `supplier_products`, `training_videos` are intentionally public catalog data. | No action; review before adding sensitive columns. |

## Live verification steps

Applied in the Supabase SQL Editor on 2026-10-21. Post-fix probes:

| Probe | Before | After |
|---|---|---|
| anon `GET /system_events` | `200` + internal rows | `401` permission denied |
| anon `POST /system_events` | `23502` (write privilege) | `401` permission denied |
| anon `GET /transporters` | `200` (empty) | `401` permission denied |
| anon `GET /ledger_account_balances` | `401` | `401` |

Reproduce with:

```bash
# expect 401 permission denied (was 200 with data)
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://lvloaweijidgcuezdnmu.supabase.co/rest/v1/system_events?select=*&limit=1" \
  -H "apikey: <publishable-key>" -H "Authorization: Bearer <publishable-key>"

# expect 401, not 23502
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "https://lvloaweijidgcuezdnmu.supabase.co/rest/v1/system_events" \
  -H "apikey: <publishable-key>" -H "Authorization: Bearer <publishable-key>" \
  -H "Content-Type: application/json" -d '{"event_type":null}'
```

Also confirm the admin ⚠ Errors tab still loads (it reads through the backend
service role) and that transporter self-service still works for a logged-in
transporter.

## Test evidence

`cd tests/sql && npm install && npm test`

- 17 security assertions pass (vulnerability reproduced before the fix, denied
  after; owner isolation; idempotent re-apply; down migration keeps the revokes).
- 24 Pesapal assertions pass in `tests/sql/pesapal.test.mjs`, including anon and
  authenticated denial on all three new tables and denial after a second
  migration apply.
- Existing legal-consent, ledger, category-order and settlement SQL suites
  continue to pass.
- `tests/test_identity_and_admin.py`: SMS auth/scoping and the admin status
  boundary still pass.
- Full pytest suite is **417 passed** with Playwright installed, including 22
  browser tests (Google sign-in gating/onboarding and Pesapal checkout gating).
  Without Playwright the E2E module skips; the rest still runs.
- Both ruff versions (local 0.17.1 and CI-pinned 0.8.4) report
  `All checks passed!`.
