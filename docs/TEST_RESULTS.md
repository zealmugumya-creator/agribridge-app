# AgriBridge — Test Results

**Date:** 2026-10-23
**Stage:** Bank Transfer (admin-verified), duplicate-signup-alert fix, Pesapal API 3.0 payments, recurring charges, Google sign-in, and Stage 1 of the buyer storefront (home / cards / details / cart)
**Environment:** Windows, repository `C:\Users\HP\Documents\agribridge`

Unless a row names an external log, every result below was produced by a
command run in this session. No live
payment transaction was made and no real Google OAuth consent screen was
exercised. Migration 0017, 0016, 0002 and **0015** are applied live (verified by
read-only PostgREST probes on 2026-10-22; `orders.source_ref` returns HTTP 200).
Push `9123b38` (Pesapal + Google) and push `6077883` (Bank Transfer + signup
fix) both deployed the **API on Render** — live `/api/pay/providers` now returns
`bank:true`. The **Cloudflare SPA auto-deploy is restored**: the owner replaced
the rejected secret with a token scoped to **Specified Workers → agribridge →
Individual Workers Editor**, and manual run `39859133726` (commit `bb2cd7a`)
succeeded, publishing Worker version `a6a1d85b-f203-4e90-8dd3-b405455c32c4`.
Both `agribrige.com` and the workers.dev host now serve the Git blob of
`static/index.html` from `bb2cd7a` (752,146 bytes, sha256 `439306a7…a19a13`;
content-identical to the workstation file after CRLF normalisation). The
`pm-bank` checkout card and the Stage 1 buyer surface are live, and
`agribrige.com/admin` serves the `Bank paid` / `verifyBankTransfer` button.
**Workers Builds was disconnected on 2026-10-23** (Cloudflare dashboard →
Worker `agribridge` → Settings → Builds → Disconnect Git), so the GitHub
`Deploy public web app` workflow is the only publisher; `npx wrangler@4 deploy`
remains the manual fallback. A Render request log from 2026-10-22T22:15Z also
shows the live SPA fetching the VAPID key (200), registering a push subscription
(200, `{"ok":true}`) and polling notifications (200); push *delivery* remains
unverified.
See §6 for exact evidence. Livestock/supplier checkout runs on the category-aware
`create_order_atomic`.

---

## 1. Python unit and API tests

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest -q
```

```
447 passed, 103 warnings in 66.09s
```

This includes the 28 Playwright browser tests (22 consent/checkout + 6 buyer
storefront), the 24 Pesapal tests (`tests/test_pesapal.py`, now covering the
callback base-URL resolver), the 14 Bank Transfer / signup-alert tests
(`tests/test_bank_transfer.py`) and the 5 deploy-workflow contract tests
(`tests/test_deploy_workflow.py`). One Playwright
consent case failed on the first full run of this change set and passed in
isolation and on a clean re-run — treated as a load-related flake, not a
regression; see LIVE_BASELINE_AUDIT §18. Run with the system `python`
(3.12) the Playwright E2E module skips because `.test-deps` is built for Python
3.14 and its native `greenlet` extension cannot load there. The skip is a local
interpreter mismatch, not a product failure; the 3.14 run is the full result.

Targeted payment/Pesapal run:

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest \
  tests/test_ledger.py tests/test_payments.py tests/test_card_payments.py \
  tests/test_admin_finance.py tests/test_settlements.py tests/test_pesapal.py -q
```

```
117 passed, 69 warnings in 2.96s
```

Targeted browser + Pesapal slice:

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest \
  tests/e2e/test_consent_ui.py tests/test_pesapal.py tests/test_pesapal_ui.py -q
```

```
53 passed in 59.23s
```

(22 browser tests + 19 Pesapal unit tests + 12 static UI tests.)

Targeted Bank Transfer + signup-alert slice:

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest \
  tests/test_bank_transfer.py -q
```

```
14 passed, 12 warnings in 0.67s
```

Covered: bank is always offered offline; `/api/pay/providers` returns
`bank_accounts` only from `BANK_TRANSFER_ACCOUNTS` and never a hardcoded number;
a source scan asserts no fabricated account (e.g. `9030`/Stanbic) appears in
`app.py`/`index.html`; `initiate` returns offline `mode:'bank'`; the verify
endpoint requires an admin token (401 without one), rejects a short reference
(400), a non-bank order (409), and a cancelled order (409); on success it marks
paid using the stored order total (asserts `provider='bank'`, the reference, and
that the posted `received` equals the DB amount, not a client value), reports
`already_paid` on replay, and returns 503 + a `bank_verify_failed` system_event
when the ledger post fails. The signup block asserts the `#signups` alert fires
once for a genuinely new account, is suppressed when the `signup_alerted`
system_event marker already exists, and is suppressed for an old account.

## 2. Lint

```bash
PYTHONPATH=.test-deps python -m ruff check app.py services/ scripts/ tests/
/tmp/ruff084/bin/ruff.exe check app.py services/ scripts/ tests/
```

Both the local ruff 0.17.1 and the CI-pinned 0.8.4 report:

```
All checks passed!
```

`node --check` parses all inline script blocks in `static/index.html` (4) and
`static/admin.html` (1).

## 3. SQL migration suites (real Postgres via PGlite)

```bash
cd tests/sql && npm test
```

| Suite | Assertions | Result |
|---|---|---|
| `legal_consent.test.mjs` | 31 | all passed |
| `ledger.test.mjs` | 36 | all passed |
| `security_lockdown.test.mjs` | 17 | all passed |
| `category_orders.test.mjs` | 10 | all passed |
| `settlements.test.mjs` | 11 | all passed |
| `pesapal.test.mjs` | 24 | all passed |
| `farmer_ratings.test.mjs` | 9 | all passed (new — migration 0018) |
| `order_write_lockdown.test.mjs` | 12 | all passed (new — migration 0019) |

Eight suites, 142 assertions in total, run with
`cd tests/sql && npm test` (Node 20.19.0, `@electric-sql/pglite` 0.5.8).

`farmer_ratings.test.mjs` starts by reproducing the production failure — the
exact select string from `static/index.html:3479` rejects against the pre-0018
schema, which is the 404 seen in the live console — then applies 0018 twice for
idempotence, reads real aggregates (A = 4.5 over 2 reviews, B = 3 over 1), proves
`insert into public.farmer_ratings` cannot write, swaps `reviews_public_select`
for a buyer-scoped policy to show anon gets 0 rows and a buyer sees only their
own reviews (so `security_invoker` is not a bypass), shows a deleted review moves
the average and a farmer with no reviews left disappears rather than reading as
zero stars, and down-migrates to confirm only the view goes.

`order_write_lockdown.test.mjs` builds `orders`, `payouts` and a stand-in for
0016's `create_payout_for_order()` with the same trigger event and the same
`security definer` context, copies both order policies verbatim from
`base-schema.sql`, and first demonstrates the hole: a farmer PATCH of
`{"status":"delivered","payment_status":"paid"}` succeeds and mints a pending
payout, and `payment_ref`/`notes` are writable. After 0019 every farmer write
returns 0 rows, existing rows are untouched, reads behave exactly as before for
farmer/buyer/anon, and `service_role` still completes an order and creates its
payout — the path `/api/admin/order/<id>/status` actually uses. The down
migration restores the policy text and re-applying closes it again.

The Pesapal suite applies migration 0017 twice, verifies every payment method
the checkout offers is accepted (including `pesapal`), NULL stays accepted and
unknown methods stay rejected, and the constraint is added `NOT VALID` so
legacy rows are not rescanned. It proves `anon` and `authenticated` cannot read
`payment_gateway_state`, `payment_subscriptions` or `subscription_charges`;
gateway state is one row per provider; correlation ids and tracking ids are
unique; statuses are constrained; replayed IPN cycles cannot double-charge; and
deleting a subscription keeps its charge history. The down migration removes
the tables, restores the legacy constraint, keeps existing Pesapal orders but
refuses new ones, and the migration can be re-applied.

The existing suites continue to cover ledger integrity, the original
anonymous-read vulnerability in 0014, category-aware pricing in 0015, and
settlement guards in 0016.

## 4. New coverage in this stage

`tests/test_pesapal.py` (19 tests):

* Sandbox is the default; `live`/`production` selects the live host.
* `RequestToken` is cached and sent as a Bearer token; secrets are not in the
  test's captured headers.
* Unsafe payment redirects, invalid tracking ids and non-HTTPS IPN URLs are
  rejected.
* The provider flag requires **both** consumer credentials.
* `POST /api/pay/initiate` uses the server-computed order total, sends a stable
  `account_number`, a unique attempt id, the registered notification id and the
  callback URL; a foreign order is refused and an unknown order is a 404.
* A callback is paid only when `GetTransactionStatus` reports completed and
  echoes the checkout reference; underpayment, failure, missing currency and a
  mismatched reference change nothing.
* The webhook acknowledges with the JSON shape Pesapal expects and returns an
  unsuccessful status when verification is unavailable.
* Recurring charges post `recurring:<tracking_id>` to
  `PROVIDER_CLEARING` / `SUSPENSE_OVERPAYMENT`, never reopen the original order,
  and are idempotent on replay.
* Zero/negative/non-numeric recurring amounts and non-UGX currencies are
  rejected.

`tests/test_pesapal_ui.py` (12 static checks, shared by the Pesapal and Google
slices):

* Exactly one login submit id and no stale duplicate.
* Google buttons exist, start hidden, and are gated by
  `/auth/v1/settings`; OAuth uses `provider: google`, the current origin and
  `prompt=select_account`.
* The OAuth onboarding modal collects name/phone/role/district/terms, cannot be
  dismissed with Escape or a backdrop click, and syncs profile + legal data.
* Pesapal appears in the payment selector only when enabled, shows its panel,
  starts the hosted payment, and handles paid/pending/failed/recurring returns.
* Admin Finance wires the Recurring view to `/api/admin/finance/recurring`.
* Browser assets contain no `service_role`, `sb_secret_`, Supabase service key,
  Pesapal/Flutterwave secret, `PESAPAL_IPN_ID` or OAuth `client_secret`;
  `.env.example` documents variable names only.

`tests/test_admin_finance.py` (2 new tests):

* An unapplied 0017 returns `503 recurring_not_initialized` and names
  `0017_pesapal_payments` in `fix`.
* The recurring summary counts only paid charges, totals only paid amounts, and
  reports paid charges without a ledger reference as
  `paid_amount_without_ledger`.

`tests/e2e/test_consent_ui.py` (22 Playwright tests, 3 new):

* `test_google_option_is_offered_on_login_and_registration` — provider settings,
  button visibility, exact OAuth arguments, redirect URL and zero page errors.
* `test_google_onboarding_blocks_until_terms_and_saves_the_profile` — the modal
  survives Escape; phone validation; terms required; saving writes role, a
  `+256...` phone, `legal_signup` versions and three legal acceptances; the
  legal review modal does not appear; zero page errors.
* `test_pesapal_is_offered_only_when_the_server_enables_it` — hidden while
  disabled; after enabling credentials the API reports `pesapal: true`, the
  option appears, selecting it shows the hosted-checkout panel and hides card,
  the explanation mentions Visa and recurring, and cash hides it again.

The 19 pre-existing browser tests still pass (legal consent, signup validation,
order flows and checkout UI).

### Buyer-UI redesign repair set (push `db9d92f`, 2026-10-22)

The redesign at `74879f8`/`20f45f2` arrived with **21 failures and a broken lint
gate**. Reproduced locally, fixed, and each fixed item is pinned by a test:

| Failure | Before | After |
|---|---|---|
| `tests/test_business_config.py` (2) — banned `support@agribridge.ug` back in `app.py` | FAIL | PASS (`VAPID_CONTACT` now defaults to `business.SUPPORT_EMAIL`) |
| `tests/e2e/test_consent_ui.py::test_pesapal_is_offered_only_when_the_server_enables_it` — `syncPayMethods()` read an unfetched cache | FAIL (tile stayed hidden although the API enabled it) | PASS (awaits `getEnabledProviders()`), verified against **live** production in the browser (audit §17) |
| `tests/e2e/test_consent_ui.py` (15) — first-visit splash overlay intercepted every click | FAIL | PASS (fixture enters the app as a returning user; no assertion weakened) |
| `tests/test_pesapal_ui.py:109` — tile-order literal | FAIL | PASS (assertion now order-independent, still requires all six methods) |
| `ruff check app.py services/ scripts/ tests/` (pinned 0.8.4) — I001 + 2x E701 in the new `_send_web_push` | FAIL | **All checks passed** |
| `tests/test_deploy_workflow.py` (2 new) — deploy failure reason invisible without log access | FAIL against pre-fix workflow | PASS (`::error::wrangler deploy failed`, output capture, `exit "$status"`) |

Full-suite result after the repairs: **436 passed**; SQL suites **129 assertions**
pass unchanged (no SQL touched); `python scripts/build_legal.py --check` →
"legal outputs up to date". CI on the pushed commit: `Backend lint + type + tests`
**success** and `Migration 0007 on real PostgreSQL` **success** (run `39785941491`).

### Stage 1 — buyer storefront (`tests/e2e/test_buyer_ui.py`, 6 tests)

New suite for the home → card → detail → cart slice. It serves the real
`static/index.html` from disk (Playwright routes, no localhost dependency) and
stubs only the Supabase SDK/REST and auth, so behaviour is deterministic and no
production row is read or written. Fixtures include eight listings with a
discount (10% on Cocoa at UGX 9,000), a minimum-order listing, an out-of-stock
listing, and a literal `"None"` description; ratings exist for two farmers only.

* `test_home_renders_listing_data_and_only_real_ratings` — cards show real
  title/price/farmer/stock; a farmer without rating data shows no stars.
* `test_signed_in_buyer_lands_on_the_shopping_home` — a signed-in `vendor`
  reaches the buyer home, not the market page.
* `test_detail_shows_real_listing_facts_and_disables_out_of_stock` — real facts,
  normalised description, verification label conditional, out-of-stock Add/Buy
  disabled, and a guest role refresh does not eject the buyer from the page.
* `test_cart_totals_are_honest_and_survive_a_reload` — 2 × discounted Cocoa =
  UGX 16,200, delivery "Arranged after confirmation", no invented fee, cart
  persists across reload.
* `test_search_and_category_filter_use_the_same_listing_data` — search and the
  category chips filter the real listing set and open the matching detail; a
  late `loadHomeStats()` must not reset the chosen category.
* `test_no_javascript_errors_in_the_buyer_flows` — zero page errors across the
  flows.

After the two navigation races were fixed (a late home fetch resetting the
chosen category; a delayed guest role refresh closing the detail page), the
module passed **five consecutive runs** (12.1–15.4 s) and the full browser set
(28 tests) passed **twice consecutively** (73.4 s / 64.5 s).

An additional **ad-hoc browser harness** ran 51 assertions at mobile and desktop
widths against the **live Supabase project in read-only mode** (real `sb()` REST
reads, auth stubbed): real listings, `Tomatoes` search → result → detail,
category filter, quantity min/max, add-to-cart badge, `localStorage`
persistence, cart ±/remove/clear, totals equal the item subtotal, signed-in
buyer home, and no desktop overflow. It placed no order and wrote nothing. This
is runtime evidence for the implementation, not a repeatable CI test.

Neither the 6-test suite nor the 51-check harness proves the stage in
production: the live bundle does not contain it (see §6).

### Stage F1 (2026-10-23) — farmer dashboard

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest tests/e2e/test_farmer_ui.py -q
```

```
12 passed in 23.13s
```

The harness is the buyer module's pattern: real `static/index.html` in headless
Chromium at 390 × 844, Supabase SDK stubbed for auth only, PostgREST and the
Render API answered from deterministic fixtures, unrelated hosts aborted. It
holds its own farmer's rows *and* another farmer's rows so ownership is genuinely
tested, and can fail any single table read to exercise the error path.

* `test_farmer_nav_is_five_named_tabs_and_sell_is_the_lead_action` — tab order
  and ids, no centre orb, Sell produce above the stats and wider than a tile,
  ≥ 44 px targets, greeting from the farmer's own name.
* `test_farmer_home_reports_real_counts_not_estimates` — 3 active produce
  (live + low stock), 1 new order, 3 needing action, Awaiting payout
  **UGX 38,000** = the pending payout's stored net, no `24°C`, "Weather" or
  "Income" anywhere, and the buyer storefront (`buyerHomeTop`, category chips,
  both product grids) computed `display:none` so a seller's Home is only a
  dashboard.
* `test_farmer_home_lists_only_own_orders_and_produce` — another farmer's
  listing never appears.
* `test_my_produce_uses_the_real_stock_model_and_the_farmers_own_photos` — all
  five stock states from `stock_status`/`reserved_qty`, tab counts from the same
  rows, exactly one `<img>` (the farmer's own upload), no Unsplash, `40 kg free
  of 50 kg` and `0 kg free of 100 kg`.
* `test_orders_expose_only_legal_transitions` — button sets match
  `set_order_status`, delivered/cancelled offer nothing, the buyer's phone
  appears only after the farmer accepts, and the filter chips plus the
  "Open delivery & tracking" button are ≥ 44 px touch targets.
* `test_confirming_an_order_goes_through_the_api_and_reloads` — real
  `POST /api/orders/<id>/status`, the row re-renders from the changed fixture
  state, the actions follow, and the home tiles agree.
* `test_cancelling_needs_an_explicit_confirmation` — no API call until the
  confirm dialog is accepted.
* `test_earnings_come_from_payout_records_only` — 38,000 / 380,000 / 2 completed
  / 440,000 order value / 22,000 commission, all from `payouts`, plus the
  statement that transport is not included because no delivery fee is stored.
* `test_profile_splits_editable_from_verified_details` — the exact
  `POST /api/user/profile` body, verified/sub-county/village/farm-size/crops
  read-only.
* `test_publishing_produce_saves_the_listing_then_attaches_the_photo` — the
  listing POSTs with `image_url: null` first, then `/api/uploads`, then
  `PATCH /api/listings/<id>/media`; the upload carries the on-device-compressed
  `listing.jpg`; two taps produce **one** listing.
* `test_empty_and_error_states_offer_a_next_action` — empty versus failed reads
  are never confused.
* `test_every_farmer_screen_stays_reachable_and_error_free` — nav, `applyRoleUI()`
  not ejecting the farmer, the ☰ sheet, keyboard-reachable section links, zero
  page errors.

Both defect tests were run against deliberately broken code and confirmed to
fail: disabling `attachListingMedia()` reproduces the production
`ReferenceError: attachListingMedia is not defined`, and disabling the save lock
produces two identical `POST /api/listings` bodies. The two new touch-target
assertions were likewise checked against reverted CSS — `.fh-tab` back to 40 px
and the contact button back to a 32 px inline link — and both failed before the
rules were restored.

Full runs after the stage: `tests/e2e` **40 passed** (79.4 s) and the whole
suite **459 passed** (85.2 s; 419 unit + 40 browser), `ruff check` clean.

### Stage F2 (2026-10-23) — managing a listing

```bash
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest tests/test_listing_edit.py -q
PYTHONPATH=.test-deps /c/Users/HP/OneDrive/Python314/python.exe -m pytest tests/e2e/test_farmer_ui.py -q
```

```
14 passed in 4.44s     (tests/test_listing_edit.py, new)
21 passed in 63.83s    (tests/e2e/test_farmer_ui.py — 12 F1 + 9 new F2)
```

Full runs after the stage: `tests/e2e` **49 passed** (146.4 s), whole suite
**482 passed** (246.3 s; 433 unit + 49 browser), `ruff check app.py services/
scripts/ tests/` clean, all 7 inline `<script>` blocks parse.

`tests/test_listing_edit.py` pins the endpoint's business rules: 401 without a
verified session, 404 for a listing the caller does not own, the ownership filter
present on *both* the read and the write, an empty body refused, a quantity below
`reserved_qty` rejected with 409 **and no write at all**, `stock_status`
re-derived at the trigger thresholds (`unavailable` at 0 remaining, `low_stock`
at 92/100 reserved, `available` at 80/100), the compare-and-swap guard carrying
the observed value, an in-flight reservation **losing the write** rather than
overwriting it, bounds on price and quantity, a blank crop name, `is_available`
touching no stock column, `updated_at` sent as an ISO stamp rather than the
literal `now()`, and a failed transport returning 502 instead of pretending.
`FakeSupa` enforces the guard and returns copies of rows, so the race test fails
if the guard is removed rather than passing for the wrong reason.

Browser coverage added in F2:

* `test_a_chosen_photo_is_previewed_then_replaced_or_removed_before_posting` —
  one preview, not two; replacing swaps it; removing returns the picker and
  clears the file input so a removed photo cannot be posted by accident.
* `test_an_unusable_photo_is_refused_with_a_reason_and_never_uploaded` — a text
  file is refused as "not a photo" and a 5 MB+ image is refused with its actual
  size, **before any upload request leaves the phone**.
* `test_an_unsent_listing_form_is_put_back_when_the_farmer_returns` — the draft
  survives a close and reopen, and its keys are asserted to be only the farmer's
  own typed fields: no session token, no payment data.
* `test_publishing_produce_saves_the_listing_then_attaches_the_photo` (extended) —
  after a successful post the draft is gone, so a published listing cannot
  resurrect itself as "unsent".
* `test_photo_progress_and_failure_are_reported_on_the_listing_row` — 43 % mid
  upload on the My Produce row, and after a failure the row stops claiming a
  photo is still coming.
* `test_the_edit_sheet_shows_real_numbers_and_blocks_a_quantity_below_reserved` —
  the row's own quantity, price, unit and reserved figure; "buyers can order
  40 kg of the 50 kg"; per-unit and total; a label for every field; 44 px save and
  photo buttons; and a below-floor value refused locally with **no request sent**
  while the sheet stays open.
* `test_the_edit_sheet_prices_the_way_buyers_see_them` — with `discount_pct: 20`
  the line reads UGX 2,400 each kg after the discount and UGX 120,000 for the
  whole listing, built from the discounted unit price rather than the list price.
* `test_saving_an_edit_writes_once_through_the_guarded_api` — two taps send **one**
  PATCH whose body is exactly `{crop_name, quantity_kg, price_per_kg}`, the sheet
  closes, the button is usable again and the row shows 50 kg free of 60 kg at the
  new price.
* `test_an_order_that_arrived_mid_edit_is_explained_and_the_floor_updates` — the
  server's 409 reason is shown verbatim, its `reserved_qty: 35` is adopted so the
  maths follows (5 kg of the 40 orderable), nothing looks saved and exactly one
  attempt was made.
* `test_hiding_and_showing_a_listing_is_a_server_write_the_row_reflects` — the
  pause body is exactly `{is_available: false}` (no stock, price or status
  rewrite), the row flips to "Hidden from buyers" with its next-step sentence and
  the button becomes "Show to buyers", then back to Live.

Seven deliberate mutations of the shipped code — drop the local reserved-floor
check, drop the one-save lock, drop the 409 reserved-figure adoption, make
hide/show write an extra column, revert the failed-upload pill, ignore the
discount in the total, remove the draft-open guard — were each run against the
matching test and **all seven were detected**; the file was restored byte-for-byte
after each (`cmp` clean). The draft bug is the one that mattered most: clearing an
`<input type=file>` programmatically fires a real `change` event, so the reset
after a successful post re-saved the published details as an unsent draft.

### Response headers (`tests/test_spa_headers.py`, 6 tests, 2026-10-23)

These guard `static/_headers`, the file that makes the site's declared
protection actually arrive on the response. Four deliberate mutations were run
against the suite and **all four were detected**, after which the file was
restored byte-for-byte (`git status` clean, and the LF-normalised checkout
`cmp`-identical to `git show HEAD:static/_headers`):

| Mutation | Result |
|---|---|
| a prose line wrapped without its leading `#` (reads as a path pattern, swallows the block) | 2 failed |
| `X-Frame-Options: SAMEORIGIN` deleted | 1 failed |
| a CSP added to the `/*` block (would bind admin/logistics/transporter) | 1 failed |
| `SAMEORIGIN` widened to `DENY` (a new policy nobody signed off) | 1 failed |

The suite itself came out of a measurement, not a preference. The first version
of the file put a CSP under `/` and `/index.html`; CI published it green,
wrangler reported `Uploaded 2 worker bundle files`, and the live document
returned the four universal headers with **no CSP** on both domains, both
`HIT`. The design was then reduced to what the platform applies, which is why
`test_no_csp_header_is_shipped_and_the_meta_policy_survives` pins the absence of
a header CSP and the presence of the meta one.

### Flake in the consent browser file (recorded, not papered over)

`tests/e2e/test_consent_ui.py` failed twice in four full-suite runs locally, on
**two different tests**, and was green every time it ran alone (22 passed) or as
part of `tests/e2e` (49 passed). One race was fixed in `61b0da7` — the test read
`#legalReviewErr` immediately after a click whose listener is attached while the
modal opens, so under load it can read an empty string; it now waits for the text
with the same 5 s ceiling and keeps the original assertion. The second failure
(`test_settings_privacy_section_marketing_toggle_and_history`) was not
reproduced after three attempts and is left alone rather than "fixed" blind. CI
was green on `6e9ace0` and `c34fa1d`, which run the same suite on Linux.

## 5. What these tests do not prove

* Pesapal API calls are faked. Token caching, redirect validation, verification
  and recurring posting are server-logic tests, not a gateway integration test.
* Google OAuth is stubbed in the browser. No Google client, consent screen or
  real identity provider was contacted.
* The admin Recurring view was exercised with fake Supabase reads, not against
  a database with 0017 applied.
* No money moved, no provider webhook arrived from the internet, and no
  production database row was written.
* Stage 1's browser tests use fixture listings; the 51-check harness read real
  Supabase listings but stubbed authentication. Neither places an order, so
  cart → checkout hand-off with a real backend is not covered by this stage.
* **The Stage F1 farmer dashboard is deployed and is the HTML production serves**
  (see §6): `agribrige.com`, the workers.dev copy and `git show HEAD` hash
  identically, so the 21 tests in `tests/e2e/test_farmer_ui.py` (F1's five
  screens plus F2's listing management) now describe what a farmer's phone
  downloads rather than a private working tree. Its 61 column names had already
  been confirmed against the live project read-only, but the **authenticated**
  read path still has not: no real farmer sign-in was performed, so every farmer
  figure is proven only against fixture rows of verified shape, not against the
  owner's real listings, orders or payouts.
* **Stage F2 is proven against fixtures, not against the owner's database.** The
  409 path, the reserved floor and the compare-and-swap loss are all exercised by
  fakes that implement the documented column behaviour; `PATCH /api/listings/<id>`
  has never run against real rows, and `reserved_qty` is still only protected by
  this endpoint's logic — no `CHECK` constraint exists in the schema. The route is
  deployed (see §6), but every live request to it so far has been refused at the
  auth check, which proves the code is running and proves nothing about the write.
* **Migrations 0018 and 0019 are tested, not applied.** The two new PGlite suites
  prove what the SQL does to a real Postgres with the Supabase roles stubbed, which
  is the strongest evidence available without touching the owner's project. Until
  the owner pastes them (steps in `docs/OPERATIONS_SETUP.md` §2), production still
  404s on `farmer_ratings` — re-confirmed on 2026-10-23 — and `orders_farmer_update`
  is still the policy in force, so the farmer-PATCH-mints-a-payout hole is still
  open live. Nothing in this document claims either migration is working in
  production.

## 6. Live verification status

| Item | Evidence | Status |
|---|---|---|
| Migration 0013 (ledger core) | Supabase SQL Editor success; SQL suite passes | Applied by owner, verified |
| Migration 0014 (internal-table lockdown) | Supabase SQL Editor success; anon `GET` on `system_events`/`transporters` returns `401` | Applied, verified |
| Migration 0015 (category pricing) | Read-only probe 2026-10-22: `orders.source_ref` select returns `200 []` (column exists) | **Applied live**; category checkout not exercised with a real order |
| Migration 0002 (admin identity RLS) | Read-only probe: `user_roles` exists (anon gets `200 []` through RLS) | Applied live; policy behavior tested in PGlite |
| Migration 0016 (settlements) | Read-only probe: `payouts.commission_amount`/`net_amount` select succeeds | Applied live; settle flow not exercised live |
| Migration 0017 (Pesapal) | Live tables exist; anon reads return `401` / `42501` | Applied live, RLS verified by probe |
| Admin Finance read endpoints | Live admin panel displays the Finance tab | Verified earlier |
| Payment → ledger posting | Unit/API tests with faked providers only | **Not yet exercised live** |
| COD → ledger posting | Unit/API tests with faked RPC only | **Not yet exercised live** |
| Payout → settlement journal | Unit/API tests and PGlite only | **Not yet exercised live** |
| Pesapal credentials recognised | `GET /api/pay/providers` → `"pesapal": true` after the owner added the keys with `PESAPAL_ENV=live` (2026-10-22); `card`/`flutterwave`/`mtn_momo` still `false` | **Verified live** |
| Pesapal endpoints reachable | `/api/pay/webhook/pesapal` → GET 200, POST 200 (empty POST is a guarded no-op: `_handle_pesapal_notification` returns early without a tracking id, so nothing was written); `/api/pay/pesapal/callback` → 302 | **Verified live** |
| Pesapal base-URL guard | `pytest tests/test_pesapal.py` → 24 passed, incl. 5 resolver tests covering the truncated `https://agribridge-1-` value the owner had typed | Verified locally; **owner must confirm the Render value** |
| Pesapal checkout / callback / IPN | Faked transport only; no real payment attempted (Rule 11) | **Not yet exercised live** |
| Pesapal recurring charge | Faked transport + PGlite | **Not yet exercised live** |
| Google sign-in | Stubbed OAuth in Playwright | **Not yet exercised live** |
| Browser E2E / Playwright | 22 passed locally (Chromium) | Verified in test harness |
| Pesapal UI / Google UI in a real browser against live services | Served locally with stubs | **Not yet exercised live** |
| API deployment of this stage | Push `9123b38`; live `/health` returns `200` with `supabase_connected:true`; `/api/pay/providers` includes `pesapal`; Pesapal callback route returns `302`; admin recurring route returns `401` | **Deployed and reachable**; business flows unverified |
| Public SPA bundle (Pesapal/Google) | Cloudflare run `39710797408` succeeded; live HTML contains `pm-pesapal` and `googleLoginBtn` | **Deployed**; live flows unverified |
| Bank Transfer method (API — Render) | Push `6077883`; live `GET /api/pay/providers` returns `bank:true` and `bank_accounts:[]` (empty until `BANK_TRANSFER_ACCOUNTS` is set in Render); `/health` `200 supabase_connected:true` | **DEPLOYED and verified live** |
| Bank Transfer checkout card + admin button (Cloudflare SPA) | Auto-deploy workflow for `6077883` **skipped** (missing `CLOUDFLARE_API_TOKEN`), so the SPA was published manually with `wrangler deploy` (version `15ff5212-5542-41f5-9682-01cf94903f8b`, account `Zealmugumya@gmail.com`). Live `GET https://agribrige.com/` now contains `pm-bank`/`renderBankOptions`/`bankInfoPanel`; `GET https://agribrige.com/admin` contains `verifyBankTransfer`/`Bank paid` | **DEPLOYED and verified live** on both the custom domain and `agribridge.zealmugumya.workers.dev`; checkout not exercised with a real bank order |
| CI on the repaired `main` | Push `db9d92f`: `Backend lint + type + tests` **success** and `Migration 0007 on real PostgreSQL` **success** (run `39785941491`); previously **failure** on `20f45f2` | **Green in GitHub**, matching the local 436-test result |
| `Deploy public web app` with the owner's new secrets | Run `39785941174` job `119849040101` **failure**, annotation: `Authentication error [code: 10000]` + `Invalid access token [code: 9109]` | **VERIFIED BLOCKER — stored token value rejected by Cloudflare**; nothing published by CI |
| Redesigned SPA + checkout fix | Published from an authenticated workstation: version `ffecfbb1-7937-4d90-8f97-2d610004a934`. Live HTML is sha256-identical to `static/index.html` at `db9d92f`; production browser check shows `window._payProviders` empty on load, then populated by `syncPayMethods()` → Bank tile visible, Pesapal correctly hidden | **DEPLOYED and verified live**; provider-gated tile behavior confirmed in production |
| Duplicate signup alert fix (API) | Once-per-account gate on the `signup_alerted` system_event + account-age window, carried by the deployed `6077883` API | **Deployed on Render**; live `#signups` behavior not yet observed over subsequent logins |
| Stage 1 buyer storefront (home / cards / details / cart) | Committed and pushed as `30f8784`: `static/index.html` sha256 `8d73dd5a…2b9107` (762,544 bytes, CRLF checkout); 6 new Playwright tests (5 consecutive green runs; all 28 browser tests green twice); 447-test suite green; 51/51 ad-hoc checks against live Supabase read-only | **DEPLOYED and verified live** |
| CI for `30f8784` and `bb2cd7a` | `Backend lint + type + tests` **success**, `Migration 0007 on real PostgreSQL (PGlite)` **success** on both commits | **Green in GitHub** |
| Stage 1 publish attempt (first) | `Deploy public web app` **failure**: `Authentication error [code: 10000]` / `Invalid access token [code: 9109]`; Workers Builds build `d901d774-…` **failure**; manual `wrangler deploy` against account `7a675255…` failed before upload because local OAuth was then logged in as `mugumyazeal@gmail.com` (account `0b66e23d…`) | **Resolved** — owner re-ran `wrangler login`, then later replaced the GitHub token |
| CI auto-deploy restored (2026-10-22T21:40Z) | Token scoped to **Specified Workers → agribridge → Individual Workers Editor** (no expiration, all IPs), `CLOUDFLARE_ACCOUNT_ID` `7a675255…`; manual `workflow_dispatch` run `39859133726` on `bb2cd7a` **success** (27 s). `wrangler deployments list` shows new deployment at `21:40:43Z`, author `undefined` (CI token), version `a6a1d85b-f203-4e90-8dd3-b405455c32c4` | **DEPLOYED BY CI and verified live** on both domains |
| Stage 1 live browser verification | Production headless Chrome at 390×844: landing → Launch app → splash → Browse as guest → 6 real home cards (Groundnuts / Zeal / Gulu / UGX 1,500/kg) → Vegetables filter (10 cards; survives a forced late `loadHomeStats()`) → Groundnuts detail (real fallback description) → add to cart → cart subtotal/total `UGX 1,500`, delivery "Arranged after confirmation", checkout enabled | **Verified in production, zero page errors**; checkout not clicked, no order placed |
| Stage 1 live HTML probe (post-CI) | `curl https://agribrige.com/` → 752,146 bytes, sha256 `439306a7…a19a13`, exactly `git show bb2cd7a:static/index.html`; after `tr -d '\r'` it matches the workstation file (`8d73dd5a…` raw, CRLF). Markers `splashBrowseGuest` ×2, `pdTrustVerified` ×2, `homeMoreHead` ×3, `_homeCat` ×3, "Arranged after confirmation" ×4; workers.dev host identical | **In production** on both domains; raw-byte hash differs only by line endings |
| Workers Builds on `bb2cd7a` | Check-run **failure** while the Actions deploy succeeds | **Resolved** — Git integration disconnected on 2026-10-23; the Actions workflow is the sole publisher |
| Stage F1+F2 farmer bundle (Cloudflare SPA) | Pushed `a02829a` + `d5bd6c0`; on `d5bd6c0` CI run `39992212374` gives `Backend lint + type + tests` **success** and `Migration 0007 on real PostgreSQL (PGlite)` **success**, deploy run `39992212165` **success**. `curl https://agribrige.com/` → 858,366 bytes, sha256 `137949de…931ce1`, byte-for-byte equal to `git show HEAD:static/index.html`; `agribridge.zealmugumya.workers.dev` returns the same hash. Markers `listingEditModal` ×4, `fhProduceRow` ×3, `leReservedLine` ×3, `lpSaveDraft` ×3 all present in the served bytes | **DEPLOYED and verified live** on both domains |
| Guarded listing-edit API (Render) | `PATCH /api/listings/999999` → `401 {"error":"Missing token"}`; with a forged bearer → `401 {"error":"Invalid or expired session"}`. Controls that make the 401 mean something: an unknown path → `404 {"error":"Not found"}`, `GET` on the same path → `405`, and the only `before_request` hook just mints a correlation ID. The rule does not exist in `365acec:app.py`, so `401` where the previous build answered `404` is the deployment evidence | **Deployed and reachable**; the write itself has never run with a real farmer token, so nothing was written |
| Farmer bundle booted in a real browser against production | Headless Chrome at 390×844 on `https://agribrige.com/` (guest, no sign-in): all 20 farmer/edit functions (`fhProduceRow`, `fhOpenEdit`, `fhSaveEdit`, `leCalc`, `lePickPhoto`, `lpMediaStart/End`, `lpSaveDraft`, …) are `typeof 'function'` and all 23 shipped ids exist, including `listingEditModal` and the `le*` form; `openModal('listingEditModal')` flips the `open` class and the sheet shows 4 labels. The page's own smoke test reports `11/11 passed`; the home grid rendered 6 real listing cards. Forced to a guest, all four farmer screens render their lock state instead of data — `farmer-produce` "Log in to see your produce", `farmer-orders` "Orders are only shown to the farmer who owns them", `farmer-earnings` "Payout records belong to your AgriBridge account", `farmer-profile` "Log in to manage your profile" — with no request for another farmer's rows | **Deployed and rendering live**, and the auth boundary holds in production |
| Farmer ratings strip (buyer home) | Live console: `404` on `GET …/rest/v1/farmer_ratings?select=farmer_id,avg_rating,review_count`. Read-only probe: `farmer_ratings` → `404` while `listings`, `farmers`, `orders`, `payouts`, `animal_listings`, `reviews`, `purchase_requests`, `order_offers`, `matching_config` → `200`. No migration in the repo creates `farmer_ratings` and nothing writes to it. Live DOM: `window.farmerRatings` undefined, `#homeFarmerStrip` and its heading `display:none` | **Pre-existing live gap, degrades honestly** — the strip can never appear in production, and no rating is fabricated to force it. Fixing it needs a migration plus a product decision, so it is recorded rather than half-patched |
| Authenticated farmer path in production | No real farmer sign-in performed; schema read-only probe only | **Not yet exercised live** — needs an owner-supplied farmer account |
| Web Push registration (production) | Render log 2026-10-22T22:15Z from `https://agribrige.com/`: `GET /api/push/vapid-public-key` 200, five `POST /api/user/push-subscriptions` 200 (`{"ok":true}`, upsert on `endpoint`), `GET /api/user/notifications` 200 every ~60 s | **Registration and polling verified in production**; delivery of a push is not tested |
| Stage F1 farmer column lists (live, read-only) | The four select strings scraped from `static/index.html` (`F_LISTING_SEL` 23 cols, `F_ORDER_SEL` 16, `F_PAYOUT_SEL` 11, the `farmers` profile read 11) sent as `GET …/rest/v1/<table>?<select>&limit=1` with only the publishable key already shipped in the SPA: `listings` **200**, `orders` **200**, `payouts` **200**, `farmers` **200**. No 42703 "column does not exist" on any of the 61 columns. `orders` and `payouts` returned **0 rows** to the anonymous role while `listings`/`farmers` returned rows | **Schema verified live** — every column the farmer screens name exists in the real project, and RLS still hides orders and payouts from anon. **Not** a verification of the authenticated read path: no real farmer sign-in was performed, so no farmer has yet seen their own rows |

| Security response headers (Cloudflare SPA) | `static/_headers` published by CI on `c34fa1d` (deploy **success**). Live response on `/`: `x-content-type-options: nosniff`, `x-frame-options: SAMEORIGIN`, `referrer-policy: strict-origin-when-cross-origin`, `Strict-Transport-Security: max-age=15552000; includeSubDomains`. `/admin.html` and `/logistics.html` (307) and `agribridge.zealmugumya.workers.dev/` carry the same four. Reloaded in a real browser: `[AgriBridge Smoke Test] 11/11 passed`, `window.AGRIBRIDGE` present, no new violation besides the pre-existing analytics-beacon block | **DEPLOYED and verified live**. `X-Frame-Options` was inert in meta before this; it is now a header |
| CSP as a response header | Shipped under `/` and `/index.html` in `e8b512f`, deploy job green, wrangler `Uploaded 2 worker bundle files` — and the served document returned **no** `Content-Security-Policy` on either domain (both HIT). Path-scoped `_headers` blocks are ignored by this assets runtime | **NOT SHIPPED, deliberately.** A `/*` CSP would bind the three operator pages, which declare none and have never been measured against a policy. `index.html` keeps its meta CSP, which browsers honour |
| Migrations 0018 / 0019 | Written, applied twice each and covered by 21 PGlite assertions. Live `GET …/rest/v1/farmer_ratings?select=…` still returns `404` on 2026-10-23, i.e. the schema has not changed | **NOT APPLIED LIVE — owner paste required** (steps in `docs/OPERATIONS_SETUP.md` §2 "APPLY NOW"). Nothing about them is claimed as working in production |
| Buyer cards against real data | Read-only probe of live `listings`: 63 rows, **every one with an empty `photo_url`**, so `listingImage()` (`static/index.html:3639`) falls through to `cropPhotoUrl()` and renders an Unsplash crop photo over each real listing; six such requests appear in the console on a live reload | **Verified production condition, not fixed** — presenting a stock photograph as a farmer's harvest is forbidden by the mandate, but removing the fallback would blank the owner's redesigned grid, so it is an owner decision (`docs/IMPLEMENTATION_STATUS.md`) |
| CI after this stage | `6e9ace0`: backend **success**, sql **success**. `c34fa1d`: backend **success**, sql **success**, deploy **success** | **Green in GitHub** |
| `npm test` intermittency on the runner | `61b0da7` sql job **failure** with no readable detail; the same eight files pass locally, in a fresh clone of the pushed commit, and with LF endings; green on `6e9ace0` and `c34fa1d`. `6e9ace0` adds an `if: failure()` step that re-runs each file and publishes the culprit as a check-run annotation, which the public API serves | **Runner-side flake, unresolved by design** — instrumented so the next occurrence names its file instead of being guessed at |

A live payment can only be claimed after a real verified transaction produces a
`payment:<tx_ref>` journal. Pesapal additionally requires live credentials and
an IPN endpoint reachable at the public API origin (the 0017 schema is already
applied). The read-only queries in
[`docs/FINANCIAL_LEDGER.md`](FINANCIAL_LEDGER.md) §8 and
[`docs/PAYMENTS_PESAPAL.md`](PAYMENTS_PESAPAL.md) §9 are the acceptance checks.

---

## 7. Reproduction prerequisites

* `.test-deps/` contains the Python test dependencies (pytest, requests, Flask,
  cryptography, PyJWT, Playwright 1.64 and greenlet 3.5.6), built for Python
  **3.14**. Use `/c/Users/HP/OneDrive/Python314/python.exe`; a 3.12 interpreter
  cannot load the native `greenlet` module and the E2E module skips.
* Playwright's Chromium is installed; the harness can fall back to the system
  Chrome channel when needed.
* `tests/sql/node_modules/` contains `@electric-sql/pglite 0.5.8`.
* Tests use `FLASK_ENV=development`, a fixed test `JWT_SECRET`, a placeholder
  Supabase URL/key, and faked network clients. No real secret is read or written.
* The SQL tests stub Supabase roles and `auth.uid()`; they do not connect to the
  live project.
* The E2E harness serves `static/` from disk, stubs Supabase REST/Auth and the
  OAuth settings endpoint, and aborts unrelated external requests, so no real
  Google or Pesapal call is made.
