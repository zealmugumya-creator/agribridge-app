# AgriBridge — Operations & Setup Guide

How to activate every integration that is **implemented but gated on credentials**, plus how to run the backend, worker, and migrations safely. All secrets are read from the environment only (Rule 4); variable **names** are in `.env.example` with no values.

---

## 0. Prerequisites
- Python 3.12.7 (matches Render). Install runtime deps: `pip install -r requirements.txt`; dev deps: `pip install -r requirements-dev.txt`.
- The Supabase project (`lvloaweijidgcuezdnmu`) service-role key, and the DB connection string for migrations.
- A Render service for the API (`agribridge-1`) and one for the worker (§5).

---

## 1. Environment variables (Render → Environment)
Confirm these exist. See `.env.example` for the full list and semantics.

**Core (required in production — the app fails closed without them):**
- `JWT_SECRET` — long random string. **Must be identical across all API workers** (a per-boot random value breaks sessions across the 2 gunicorn workers — audit H1).
- `SUPABASE_URL`, `SUPABASE_KEY` (service role).

**Admin:**
- `ADMIN_ALLOWED_UUIDS` — comma-separated verified admin UUIDs (bootstrap allowlist used before the `user_roles` row exists).
- `ADMIN_PASSWORD` — legacy shared-password console only. Rotate off the documented default (`agribridge2026`) and migrate to Supabase-Auth admin login (§3).

**Integrations (each stays a safe no-op until set):**
- `DISCORD_WEBHOOK_URL` (§4) · `SENTRY_DSN` (§6) · `FCM_CREDENTIALS_JSON`/`FCM_SERVICE_ACCOUNT_PATH`/`FCM_PROJECT_ID` (§7) · `FLW_SECRET_KEY`/`FLW_WEBHOOK_HASH` (§8) · Pesapal `PESAPAL_CONSUMER_KEY`/`PESAPAL_CONSUMER_SECRET`/`PESAPAL_ENV`/`PESAPAL_CALLBACK_BASE_URL` (§8b) · Bank Transfer `BANK_TRANSFER_ACCOUNTS` (§8d) · Africa's Talking `AT_*` · `GROQ_API_KEY`/`GEMINI_API_KEY`.
- Google sign-in needs **no API environment variable**; it is configured in Supabase and Google Cloud (§8c).

**Policy:**
- `STOCK_FRESHNESS_MINUTES` (default 4320), `OFFER_RESPONSE_SECONDS` (900), `OFFER_BATCH_SIZE` (1), `ACCOUNT_HARD_DELETE_ENABLED` (false — §9).

---

## 2. Database migrations (Phases 3–5, 10)
Migrations live in `supabase/migrations/` as numbered `*.up.sql` / `*.down.sql` pairs. They are **additive, reversible, and schema-defensive** (guarded by `IF NOT EXISTS`, `to_regclass`, and column-existence checks) so they will not touch unrelated apps in the shared project (Rule 5).

**Before applying (Rule 7):** take a backup — GitHub → Actions → **Database backup** → *Run workflow* (see `.github/workflows/db-backup.yml`).

Apply in order via the Supabase SQL editor or CLI:
```bash
supabase db push            # if the project is linked with the Supabase CLI
# or paste each 000N_*.up.sql into Supabase → SQL Editor, in numeric order.
```

> **LIVE STATUS (verified 2026-10-22 after pushes `9123b38` and `6077883`):**
> 0013, 0014, 0002, 0015, 0016 and 0017 are applied. **0018 and 0019 are not** —
> committed, tested against a real in-process Postgres, and waiting on the paste
> in "APPLY NOW" below. The new API and SPA bundle
> deploy automatically by Render and the `Deploy public web app` workflow
> immediately after each push. **0015 is now applied** — `orders.source_ref`
> returns HTTP 200 (read-only probe, 2026-10-22) — so livestock and supplier
> checkout price from each item's own listing via the category-aware
> `create_order_atomic`. 0002 has closed direct browser inserts; all checkout
> goes through `POST /api/orders`.
>
> **What `6077883` adds (no migration required):** Bank Transfer is offered in
> checkout again, safely. Account numbers are never hardcoded — they render only
> from the `BANK_TRANSFER_ACCOUNTS` env var (`bank|account name|account
> number[|branch]`, semicolon-separated); with no accounts configured the buyer
> sees official support contact instead. A bank order stays **unpaid** until an
> admin calls `POST /api/admin/orders/<id>/verify-bank-transfer`, which takes the
> amount from stored rows (never the request) and posts the idempotent
> `payment:<ref>` ledger journal via `_mark_checkout_paid`. The same push fixes
> Discord `#signups` spam: the new-user alert now fires once per account, gated
> by a durable `signup_alerted` system_event marker and an account-age window.
>
> **APPLY ORDER (historical).** 0015 was meant to run before 0002 and before the
> SPA cutover; both are now live and 0015 has caught up. It makes
> `create_order_atomic` price livestock from `animal_listings` and supplier
> inputs from `supplier_products`. The shared `payment_ref` reconciliation used
> by the payment webhook is unaffected. See the header of
> `0002_admin_identity_rls.up.sql`.
>
> **0016 (settlements)** is applied live. It creates `admin_audit_log` if 0002
> has not run yet; 0002 later treats it as an existing table. Applying 0016
> backfilled a **pending payout** for every order already delivered and paid. It
> does not pay anyone. Settling a payout requires the customer receipt to be
> posted in the ledger; historical paid orders from before 0013 will be refused
> until their receipts are backfilled from verified provider records.
>
> **0017 (Pesapal)** is applied live. It adds service-role-only
> gateway/subscription tables and widens the payment-method constraint. The
> tables reject `anon` with `42501`; no Pesapal credentials are configured yet.
> It does not create orders or payouts.

| Migration | Adds | Fixes |
|---|---|---|
| 0001 | order-integrity columns, `order_events`, `stock_changes`, `create_order_atomic`, `set_order_status` | C1, C2, C4 |
| 0002 | `user_roles`, `admin_audit_log`, `is_admin()`, `assign_role()`, RLS closing public order/payout inserts | C3, C5 |
| 0003 | `notification_outbox`, `admin_devices`, `enqueue_notification`, `claim_notifications`, `resolve_notification` | H3, H4, H5 |
| 0004 | in-app notification channel and user read access | Notifications |
| 0004a | `purchase_requests`, `order_offers`, `matching_config`, `match_eligible_farmers` | Phase 5 |
| 0005 | `create_purchase_request`, `match_and_offer`, `accept_offer`, `reject_offer`, `expire_overdue_offers`, `claim_unmatched_requests` | Phase 5 |
| 0006 | `account_deletion_requests`, `_acct_anonymize`, `request_account_deletion`, `mark_account_purged`, `farmers.deleted_at` | H6 |
| 0007 | versioned legal documents and consent records | Legal compliance |
| 0007a | free-form order items in `create_order_atomic` | Marketplace orders |
| 0008–0011 | profile validation, push subscriptions, service requests and market-price history | Product operations |
| 0012 | secure public `uploads` bucket; backend-only media writes | Listing uploads |
| 0013 | double-entry ledger: accounts, journals, lines, `post_journal`, `reverse_journal`, admin read policies | Financial correctness |
| 0014 | locks down `system_events` and transporter rows to service role / owner | Internal-data leak |
| 0015 | category-aware `create_order_atomic`: livestock and supplier prices/owners resolved server-side, stock decremented | Checkout regression after the SPA cutover |
| 0016 | pending farmer payouts on delivered+paid, `mark_payout_paid` guarded by a posted settlement journal, `admin_audit_log` dependency | Financial settlement correctness |
| 0017 | Pesapal gateway state, subscriptions, recurring charges, wider `orders.payment_method` constraint | Recurring payments / hosted checkout |
| 0018 | `public.farmer_ratings` view over `reviews` (`security_invoker`) | Buyer-home 404: the relation the SPA reads did not exist |
| 0019 | drops `orders_farmer_update` | A farmer PATCH could mark an order delivered+paid and mint a payout |

> **APPLY NOW — 0018 and 0019 (2026-10-23, written and tested but NOT live yet).**
> These are the only two changes from this stage that cannot be deployed from
> CI: the frontend and the API published themselves, but a schema change is the
> owner's to run (Rule 6). Neither rewrites a row, neither touches money, and
> both are safe to paste twice.
>
> 1. Supabase → your project → **SQL Editor** (left sidebar) → **New query**.
> 2. Open `supabase/migrations/0018_farmer_ratings_view.up.sql` in a text
>    editor, copy everything **below the comment block** (from `begin;` to
>    `commit;`), paste, **Run**. Wait for `Success. No rows returned`.
> 3. Repeat with `supabase/migrations/0019_lock_farmer_order_writes.up.sql`
>    (its whole body is one `drop policy if exists` inside `begin;`/`commit;`).
>    Run 0019 second so the ratings read is never missing while writes are
>    already locked; the order does not actually matter, they are independent.
> 4. Verify in the same editor — one query, no keys involved:
>    ```sql
>    select 'ratings_view'  as what, count(*) as n from public.farmer_ratings
>    union all
>    select 'farmer_update_policy', count(*) from pg_policies
>      where schemaname = 'public' and tablename = 'orders'
>        and policyname = 'orders_farmer_update';
>    ```
>    Expected: `ratings_view` `0` (live `reviews` is empty — that is honest, not
>    a failure) and `farmer_update_policy` **`0`**. If the second row says `1`,
>    0019 did not run.
> 5. Confirm the SPA's own read stops 404-ing — from **cmd**, where
>    `PASTE_ANON_KEY` is Supabase → Project Settings → API → `anon public`
>    (the same publishable key the site already ships; never the service-role
>    key):
>    ```cmd
>    curl -s -o ratings.json -w "HTTP %%{http_code}\n" "https://lvloaweijidgcuezdnmu.supabase.co/rest/v1/farmer_ratings?select=farmer_id,avg_rating,review_count&limit=5" -H "apikey: PASTE_ANON_KEY" -H "Authorization: Bearer PASTE_ANON_KEY"
>    ```
>    `200 []` before 0018 was `404`. At the interactive prompt (not inside a
>    `.bat`) drop one `%`: `-w "HTTP %{http_code}\n"`.
>
> **What 0018 does and does not do.** It gives the buyer home the aggregate it
> already asks for, so a real star rating can appear as soon as reviews exist.
> It does **not** invent ratings: `reviews` has zero rows live, so the strip
> stays hidden until review capture is actually exercised, and `renderHomeFarmers()`
> still hides it unless a farmer has `avg_rating > 0`. It also does not repair
> `match_eligible_farmers()` in 0004a, which expects one row per review — no
> code calls it.
>
> **What 0019 costs.** Farmers lose the ability to update their own order rows
> directly. Nothing in the app does that today: the SPA has no write to
> `/rest/v1/orders`, and admin status changes go through
> `/api/admin/order/<id>/status`, which runs as service role and still fires the
> payout trigger. Reads are untouched. Rollback is
> `0019_lock_farmer_order_writes.down.sql`, which re-opens the hole exactly as
> base-schema declared it.


> **Listing-photo fix:** deploy the API and web app together, then apply
> `0012_uploads_bucket.up.sql`. It creates the `uploads` bucket and removes any
> browser-write storage policies. Do not use the older `storage-uploads.sql`.

> **Verify after applying:** Supabase → Advisors → **Security** should show no new RLS-disabled or policy warnings on AgriBridge tables. Rollback with the matching `.down.sql` if needed.

---

## 3. Super-admin assignment (Phase 3 — Rule 9)
Admin authority is server-controlled (the `user_roles` table), **never** a client-side email check or hidden button. Assign the owner by **verified UUID**, not by email string:

1. Supabase → Authentication → Users → confirm `zealmugumya@gmail.com` exists and **email is confirmed**; copy its UUID.
2. In the SQL editor (service role):
   ```sql
   select public.assign_role('<VERIFIED-UUID>', 'superadmin', null);
   ```
   (See the footer of `supabase/migrations/0002_admin_identity_rls.up.sql`.)
3. Enable **MFA** for that account (Auth → user → require MFA).
4. Optionally add the UUID to `ADMIN_ALLOWED_UUIDS` as a bootstrap fallback, then remove it once the role row exists.

The backend authorizes admins via `require_admin_user()` → `is_admin_uuid()`, which checks `ADMIN_ALLOWED_UUIDS` **and** the `user_roles` table.

---

## 4. Discord operations feed (Phase 7)
1. Discord → Server Settings → Integrations → Webhooks → **New Webhook** → copy the URL.
2. Set `DISCORD_WEBHOOK_URL` in Render. Restart.
3. Verify: place a test order → an `order.created` embed should appear. Delivery has bounded retries, 429 handling, and a **dead-letter fallback** (a failed critical Discord alert is re-enqueued as an admin push so it is never silently lost).
- The URL is never logged or returned to clients.

---

## 5. Notification / matching worker (Phase 5)
The durable queue lives in Postgres (`notification_outbox`), so the worker runs as a **separate process** from the API.
- **Command:** `python -m services.worker`
- **Render:** create a **Background Worker** service with the **same env** as the API. It handles `SIGTERM`/`SIGINT` for graceful shutdown, drains notifications, and runs a periodic matching pass.
- **Tunables:** `WORKER_POLL_SECONDS` (5), `WORKER_BATCH_SIZE` (10), `WORKER_MATCH_EVERY_N_POLLS` (3).
- Without a running worker, enqueued notifications and matching simply wait in the DB (durable) — nothing is lost.

---

## 6. Sentry error monitoring (Phase 8)
1. Sentry → create project (Flask) → copy the DSN.
2. Set `SENTRY_DSN`, and optionally `SENTRY_ENVIRONMENT` / `SENTRY_TRACES_SAMPLE_RATE`.
3. `services/monitoring.py` scrubs request data/headers/cookies before send and drops the event if scrubbing fails. Without a DSN, Sentry is a no-op.
- Correlation IDs (`X-Correlation-ID`) tie a Sentry event to the request's structured logs.

---

## 7. Firebase Cloud Messaging — admin push (Phase 6)
1. Firebase console → create/select project → **Project settings → Service accounts → Generate new private key** (JSON).
2. Provide it to the backend **one** of two ways:
   - `FCM_CREDENTIALS_JSON` = the file's contents (inline), **or**
   - `FCM_SERVICE_ACCOUNT_PATH` = a path on the server to the JSON file.
   - `FCM_PROJECT_ID` optional (defaults to `project_id` in the JSON).
3. Add `cryptography` (already in `requirements.txt`) — PyJWT needs it to sign the RS256 assertion.
4. **Client side:** the admin app obtains an FCM registration token and calls `POST /api/admin/devices {token, platform}` with the admin's Supabase session. The API only registers a token **after** verifying the caller is an admin (Rule 9).
5. Push payloads are **lock-screen safe**: the visible notification is a short generic summary; detail rides in `data` and renders in-app after auth. Dead tokens (UNREGISTERED) are pruned automatically.
- Until credentials are set, push is a safe no-op and critical alerts fall back to Discord/dead-letter.

---

## 8. Payments — Flutterwave (Phase 9)
The existing verified webhook (`/api/pay/webhook/flutterwave`) is preserved: it checks the shared `verif-hash` **and** re-verifies the transaction with Flutterwave server-side before marking an order paid.
1. Flutterwave → Settings → API Keys → Secret Key → `FLW_SECRET_KEY`.
2. Invent a webhook secret → set `FLW_WEBHOOK_HASH` **and** paste the same value in Flutterwave → Settings → Webhooks.
3. Webhook URL: `https://agribridge-1-og7a.onrender.com/api/pay/webhook/flutterwave`.
4. Test in **sandbox** first. With keys empty, online payment stays OFF and checkout uses cash-on-delivery.
- Order totals are recomputed **server-side** by `create_order_atomic`; a client-supplied price is ignored (fixes C1/C4).

### 8b. Payments — Pesapal API 3.0

The hosted-checkout provider is off until both consumer credentials are set.
See [`docs/PAYMENTS_PESAPAL.md`](PAYMENTS_PESAPAL.md) for the full flow, amount
rules, recurring accounting and verification queries.

1. `0017_pesapal_payments.up.sql` is applied live. Verify the three tables
   still reject `anon`, and re-run the migration only if needed (it is safe to
   re-run).
2. Pesapal dashboard → API credentials → copy the consumer key and secret into
   Render as `PESAPAL_CONSUMER_KEY` and `PESAPAL_CONSUMER_SECRET`.
3. Set `PESAPAL_ENV=sandbox` for testing, then `PESAPAL_ENV=live` with live
   credentials. Sandbox and live credentials are different; clear or replace
   `PESAPAL_IPN_ID` when switching.
4. Set `PESAPAL_CALLBACK_BASE_URL` to the public **API** origin
   (`https://agribridge-1-og7a.onrender.com`). The static site does not proxy
   `/api`, so it cannot be the callback base. **Deleting the variable is also
   correct** — that origin is the built-in default. A host-less value (a pasted
   URL truncated at a hyphen, for example) used to be sent to Pesapal as-is, so
   it registered a notification endpoint that could not be reached: the buyer
   paid, Pesapal reported success, yet the order stayed unpaid. The app now
   rejects any base URL whose host has no dot, logs
   `pesapal.callback_base_url_rejected` with the fallback it used, and keeps the
   known-good origin instead. Check that log line after changing this variable.
5. Leave `PESAPAL_IPN_ID` empty to auto-register
   `<base>/api/pay/webhook/pesapal`; the id is stored in
   `payment_gateway_state`. Pin the variable only if the endpoint was registered
   in the Pesapal dashboard first.
6. Deploy. `GET /api/pay/providers` must report `"pesapal": true`; the SPA
   reveals the option. Buy a low-value sandbox order and confirm the order,
   `payment:<ref>` journal and browser return before going live.

The callback/IPN body is never trusted: every notification is verified with
`GetTransactionStatus` before state changes. Recurring charges post to
`SUSPENSE_OVERPAYMENT`, not to a farmer.

### 8c. Google sign-in

No backend environment variable is needed. The provider is configured in
Supabase and Google Cloud; the SPA hides the buttons until Supabase reports the
provider enabled. Full steps, redirect URLs and troubleshooting are in
[`docs/GOOGLE_SIGNIN.md`](GOOGLE_SIGNIN.md).

**Do not** put the Google client secret or the Supabase service-role key in the
Worker, `static/`, or any browser configuration.

### 8d. Bank transfer (offline, admin-verified)

Bank Transfer is **always offered** in checkout and needs no gateway. Orders
placed with it stay **unpaid** until an operator confirms the money.

- **Account details (optional):** to show real account numbers to buyers, set
  `BANK_TRANSFER_ACCOUNTS` in Render. Format is `bank|account name|account
  number[|branch]`, semicolon-separated for multiple accounts, e.g.
  `Stanbic|AgriBridge Ltd|9030xxxx|Kampala;DFCU|AgriBridge Ltd|1234xxxx`.
  With the variable **empty**, buyers see only the official support contact and
  are told to confirm the transfer with the team — the app never invents an
  account number (a fabricated Stanbic number was removed on purpose earlier).
- **Never** hardcode an account number in `static/` or commit a real one; the
  value is only ever served to the SPA from this env var via `/api/pay/providers`.
- **Receiving the money:** an admin opens the order in the panel and clicks
  **Bank paid**, entering the bank/transfer reference. This calls
  `POST /api/admin/orders/<id>/verify-bank-transfer` (admin token required). The
  amount is taken from the stored order rows, **not** the request, and the
  idempotent `payment:<ref>` ledger journal is posted through
  `_mark_checkout_paid`, so a replay cannot double-post.
- The action writes an `admin_audit_log` row (`order.bank_transfer_verified`)
  and an `order_bank_transfer_verified` system event. A ledger failure returns
  `503` and logs `bank_verify_failed` — the order is **not** marked paid.

---

## 9. Account deletion (Phase 10 / H6)
- Endpoint: `POST /api/account/delete` (identity from the verified session; a body-supplied id is ignored).
- **Default (reversible):** anonymizes the caller's own personal data, drops their push tokens/role, preserves the financial ledger, and records an auditable request. Reversible only from a backup.
- **Irreversible purge:** set `ACCOUNT_HARD_DELETE_ENABLED=true` **only after** (a) a fresh backup, (b) reviewing FK `ON DELETE` behaviour on `orders`/`payouts`/`reviews` so purging `auth.users` cannot cascade-destroy the ledger, and (c) explicit approval (Rule 8). Then deletion also calls the GoTrue admin endpoint.
- **Play Store:** wire a settings-screen "Delete account" button to this endpoint and publish the web deletion URL (see `docs/PLAY_STORE_RELEASE.md` §8).

---

## 10. Health & observability
- `GET /health` → 200 with `supabase_connected: true`, or **503** `degraded` when the DB round-trip fails (M6 — no lying health checks).
- `GET /health/live` → process liveness, independent of dependencies.
- Logs are JSON with correlation IDs; secrets are redacted by `services/logging.py`.
- `GET /api/pay/providers` → which payment methods are live.

---

## 11. Public web deployment (Cloudflare Worker)

The public site is the `agribridge` Cloudflare Worker defined in `wrangler.jsonc`.
To make every approved `main` push publish `static/` automatically, add these
repository secrets in GitHub Settings > Secrets and variables > Actions:

- `CLOUDFLARE_API_TOKEN` - a Cloudflare API token limited to editing this
  Worker. Keep it out of Render and out of source code.
- `CLOUDFLARE_ACCOUNT_ID` - the Cloudflare account that owns the Worker.

The `Deploy public web app` workflow now **fails (red)** when
`CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is missing — it no longer
reports a misleading green run while publishing nothing (an earlier version did
exactly that and silently stranded SPA releases; fixed in `5321418` + `db9d92f`,
both on `main` — see `docs/LIVE_BASELINE_AUDIT.md` §13).
**A red `Deploy public web app` run means the deploy did not happen.** Since
2026-10-22 the step also captures wrangler's own output and republishes its last
lines as a check annotation and in the run summary, so a failure is diagnosable
without opening the job log (`docs/LIVE_BASELINE_AUDIT.md` §14). Do not read a
green deploy as proof unless you also confirm the served HTML (e.g.
`curl -s https://agribrige.com/ | grep pm-bank`).

**Current state (verified 2026-10-22T21:40Z): auto-deploy is working.** The
owner replaced the rejected token with one scoped to **Specified Workers →
`agribridge` → Individual Workers Editor** (no expiration, all IPs allowed) and
kept `CLOUDFLARE_ACCOUNT_ID` = `7a6752557859ee5998bdc81c0989743b`. Manual
`workflow_dispatch` run `39859133726` on `bb2cd7a` completed green, and
`wrangler deployments list` shows the new deployment at `21:40:43Z` with version
`a6a1d85b-f203-4e90-8dd3-b405455c32c4` — proof that CI published rather than
no-opped. Earlier failures were `Authentication error [10000]` /
`Invalid access token [9109]` from an invalid stored value; that is history.

**Verification must normalise line endings on this repo.** The workstation
checkout is CRLF (`core.autocrlf=true`) while CI checks out LF, so raw sha256
values legitimately differ. Compare content instead:

```bash
git show HEAD:static/index.html | sha256sum
curl -s https://agribrige.com/ | tr -d '\r' | sha256sum   # must match
curl -s https://agribrige.com/ | grep -c splashBrowseGuest # must be > 0
```

**Workstation login check (verified 2026-10-22):** do not assume the local
wrangler login owns the Worker. A login can silently belong to a different
Cloudflare account (`0b66e23d…`) and then fail with `Authentication error
[code: 10000]` before upload. Always run `npx wrangler@4 whoami` first; for this
project it must report the account owning `agribridge`,
`7a6752557859ee5998bdc81c0989743b`. If it does not, sign in with the owning
account (`npx wrangler@4 login`, Cloudflare user `Zealmugumya@gmail.com`), then:

```bash
export PATH="/c/Program Files/nodejs:$PATH"
export NODE_EXTRA_CA_CERTS="C:\\Users\\HP\\win-root-ca.pem"
CLOUDFLARE_ACCOUNT_ID=7a6752557859ee5998bdc81c0989743b npx --yes wrangler@4 deploy
curl -s https://agribrige.com/ | grep -c splashBrowseGuest   # must be > 0
```

The manual path remains the fallback if CI is unavailable. It published Worker
version `bb9e65fb-974d-4381-8bf0-c78191a2e6d0` on 2026-10-22 before CI was
restored; a production browser smoke test passed with zero page errors.

Never run `wrangler deploy` without the account override while logged in to
`0b66…`: it targets the wrong account and leaves the public site untouched.

Cloudflare's **Workers Builds** Git integration targeted the *same* `agribridge`
Worker and failed on every push. On 2026-10-23 the owner disconnected it
(Worker `agribridge` → Settings → Builds → Disconnect Git), leaving the GitHub
Actions workflow as the only publisher. Do not reconnect it while the Actions
workflow deploys — two publishers means whichever integration last succeeded
decides what the public site serves.

### 11b. Response headers (`static/_headers`)

`static/_headers` is the only place the site's response headers are set, and it
is published with the assets — a change to it needs a `Deploy public web app`
run, not a Render redeploy. Verified on the live response for `/`,
`/admin.html` and `agribridge.zealmugumya.workers.dev`:

```bash
curl -sSI https://agribrige.com/ | grep -iE "x-frame|x-content|referrer|strict-transport"
```

Two constraints were learned by measurement, not from documentation, and are
recorded in `docs/IMPLEMENTATION_STATUS.md`:

- **A path-scoped block in `_headers` is ignored by this assets runtime.** A CSP
  declared for `/` and `/index.html` deployed green and arrived as *no CSP*.
  Only a `/*` block lands, which is why every header lives in one block.
- **Do not add `Content-Security-Policy` to `/*`.** It would bind
  `admin.html`, `logistics.html` and `transporter.html`, which declare no policy
  today and have never been measured against one. `index.html` keeps its meta
  CSP, which browsers honour; `X-Frame-Options` and `frame-ancestors` are the
  parts a meta cannot deliver, and the header now supplies `X-Frame-Options`.

A clickjacking change is not cosmetic: `X-Frame-Options` blocks embedding the
site in an iframe on another origin. Nothing in AgriBridge embeds the SPA that
way. `tests/test_spa_headers.py` fails if the file's syntax degrades (an
unindented or unwrapped line silently swallows a block) or if the four headers
disappear.


---

## 12. Running tests / lint locally
```bash
pip install -r requirements-dev.txt
ruff check app.py services/ scripts/ tests/  # lint gate (must be clean)
pytest tests/ -q                       # 447 passed with Playwright installed
cd tests/sql && npm install && npm test # PGlite migration suites (129 assertions)
```
Playwright lives outside `requirements-dev.txt` in this environment; the E2E
modules skip cleanly when it is absent, and fall back to the system Chrome
channel when Playwright's bundled Chromium is unavailable.

The buyer storefront is covered by `tests/e2e/test_buyer_ui.py` (6 tests:
home/cards, signed-in landing, detail, cart totals and reload persistence,
search/filter, JS errors). It serves the real `static/index.html` from disk via
Playwright routes and stubs only Supabase auth/REST, so it is deterministic and
writes nothing. `tests/e2e/test_consent_ui.py` covers consent, Google sign-in
and the server-gated payment tiles.

CI (`.github/workflows/ci.yml`) runs lint + (non-blocking) mypy + pytest on
push/PR. **SQL-migration integration tests and Android builds are not part of
CI yet** — they need a live DB / Android SDK and are not faked. Static-site
publishing is still manual until the rejected `CLOUDFLARE_API_TOKEN` is replaced
(§11); a green local suite says nothing about what the Cloudflare Worker serves.
