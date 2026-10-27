# AgriBridge — Live-Baseline Audit

**Audit run date:** 2026-10-22 (evidence timestamp `2026-10-22T06:17:51Z` unless noted)
**Method:** read-only inspection of the repository, deployment records and public
production endpoints. No production mutation was performed to obtain a result.
**Legend:** VERIFIED BLOCKER · VERIFIED DEFECT · CONFIGURATION GAP · UNVERIFIED · WORKING · INFORMATIONAL

> This audit is deliberately separate from the historical `PRODUCTION_AUDIT.md`
> and `IMPLEMENTATION_STATUS.md`, which are preserved as the audit trail. Findings
> here are labelled by the kind of evidence behind them: **CODE** (source/CI),
> **LIVE** (a production endpoint or DB probe), **DEPLOY** (a workflow/run record),
> or **ASSUMPTION** (needs access we do not have).

---

## 1. Repository and deployed versions — WORKING (CODE)

- Branch `main`, HEAD `8e954ac`, working tree clean, in sync with `origin/main`. No uncommitted user changes were found; none were discarded.
- Backend reports `version: 3.3.0` (**LIVE** `/health`).
- `render.yaml`: Python `3.12.7`, `gunicorn app:app --workers 2`, `healthCheckPath: /health/live` (**CODE**). Note the deploy target runs 3.12 while the local `.test-deps` suite runs 3.14 — CI is the authoritative environment.

## 2. Service health and deployment status

- Backend readiness: `GET /health/live` → **200**; `/health` → `status:ok`, `supabase_connected:true` (**LIVE**). → **WORKING**
- Public SPA: `https://agribrige.com/` serves the current bundle; `GET /` contains `pm-bank` and `/admin` (via `/admin.html` → 307 → `/admin`) contains `verifyBankTransfer`/`Bank paid` (**LIVE**). → **WORKING**
- **VERIFIED BLOCKER — deployment automation passes while deploying nothing.** The `Deploy public web app` workflow run for push `6077883` finished with conclusion `success`, but its `Deploy Worker static assets` step conclusion was `skipped` (**DEPLOY**, run id `39721847981`). The workflow `exit`s green whenever `CLOUDFLARE_API_TOKEN` is absent (`deploy-cloudflare.yml:26-37`). Proof of impact: the live site did **not** contain the bank card until the SPA was published **manually** with `wrangler deploy` (worker version `15ff5212`). A green deploy run is therefore **not** evidence that a release shipped. See §12.

## 3. Database and migration status — WORKING (LIVE, read-only PostgREST probes)

Applied to live Supabase `lvloaweijidgcuezdnmu`, verified by probing columns/tables that each migration creates:

| Migration | Probe | Result | Label |
|---|---|---|---|
| 0013 ledger | `ledger_accounts` select | `401` (exists, RLS-locked to non-anon) | WORKING |
| 0014 lockdown | `system_events`/`transporters` anon | `401` (prior session) | WORKING |
| 0002 admin identity | `user_roles` select | `200 []` through RLS | WORKING |
| 0015 category pricing | `orders.source_ref` | `200 []` | WORKING |
| 0016 settlements | `payouts.commission_amount` | `200` | WORKING |
| 0017 Pesapal | `payment_subscriptions` anon | `401` | WORKING |

- The live schema matches the application's assumptions for these objects. No repository migration is absent from the live DB (**LIVE**). → **WORKING**

## 4. Authentication and authorization findings

- Anonymous reads of ledger and payment tables are rejected (`401`) — effective RLS, not just policy names (**LIVE**). → **WORKING**
- `POST /api/admin/orders/<id>/verify-bank-transfer` without an admin token → **401** (**LIVE**). Admin gate enforced server-side. → **WORKING**
- Admin panel still authenticates with a shared-password JWT (`ADMIN_PASSWORD`) rather than Supabase identity in some paths (historical F01, **CODE**). Not re-verified as exploitable this run. → **UNVERIFIED** (need to confirm the password path is not reachable for privileged writes on live).

## 5. Checkout and payment status

- Live `/api/pay/providers`: `airtel,momo,cod,direct,bank,pesapal = true`; `card,flutterwave,mtn_momo = false`; `bank_accounts: []` (**LIVE**, re-probed 2026-10-22 after the owner added the Pesapal keys — see §18).
- Bank Transfer is offered and orders stay unpaid until admin verification; the verification endpoint marks paid using the **stored** order amount and posts the idempotent `payment:<ref>` journal (**CODE** + tests). UI + API are live (**LIVE**). → **WORKING** (code path) / **UNVERIFIED** (no real bank order placed and settled live).
- Pesapal / Card / Flutterwave MTN: implemented but **disabled** because credentials are absent (`providers.*=false`) (**LIVE**). → **CONFIGURATION GAP** (owner credentials required; see §11).
- Google sign-in: code deployed; buttons render only when Supabase reports the provider enabled. Provider not yet configured (**CODE**; not probed live). → **CONFIGURATION GAP**.
- The full buyer journey (authenticated `POST /api/orders` → confirm) was **not** exercised end-to-end against production; we have no user session and Rule 11 forbids real payments. → **UNVERIFIED**.

## 6. Financial ledger status

- Ledger core (0013) live and RLS-locked. Payment/settlement/COD journaling is proven by 431 unit/API tests and 129 SQL assertions against real PGlite (**CODE**). → **WORKING** (tests).
- No verified incorrect/duplicate posting exists, but no **real** production transaction has been journaled this run. → **UNVERIFIED** (live posting requires a real payment, which we will not fabricate).

## 7. Notifications and phone push status

- Discord feed: `discord_configured:true` (**LIVE** `/health`).
- **VERIFIED DEFECT (fixed this session, live effect UNVERIFIED):** duplicate `#signups` alerts fired on every login (owner screenshots showed the same user three times). The deployed code now fires once per account via a durable `signup_alerted` `system_events` marker + an account-age gate (**CODE**, deployed to Render). Subsequent logins have **not** been observed to confirm the suppression in production. → fix is **UNVERIFIED live**.
- FCM mobile push: implemented; admin push requires Firebase service-account credentials (§11). → **CONFIGURATION GAP** / **UNVERIFIED**.

## 8. Order, farmer, supplier and transporter workflows

- Order creation routes through `POST /api/orders`; direct browser inserts are closed by 0002 (**LIVE** RLS, **CODE**).
- Category-aware pricing for livestock/supplier is active via 0015 (**LIVE** schema). Not exercised with a real order. → **UNVERIFIED**.
- Farmer matching, offers and payouts are implemented (0004a/0005/0016) and tested in PGlite (**CODE**). Live behavior **UNVERIFIED**.

## 9. Delivery tracking status

- Transporter tables are RLS-locked (**LIVE**). Tracking map fix shipped earlier (**CODE**). Live tracking flow not exercised. → **UNVERIFIED**.

## 10. Automated test status — WORKING (CODE)

- Python: **436 passed** (incl. 22 Playwright) on Python 3.14 with `.test-deps`. → PASS (as of §15; the redesigned `origin/main` HEAD `20f45f2` was **21 failed / 413 passed** before the repairs recorded there)
- SQL (PGlite): **129 assertions passed** across 6 suites. → PASS
- Lint: ruff clean on both local 0.17 and CI-pinned 0.8.4. → PASS
- CI: green on `319bae9`/`8e954ac`. **Note:** CI **failed** on `6077883` at "Lint (ruff)" (a `UP017` in the new test), fixed in the follow-up commit — an example of a real check correctly failing. → PASS (after fix)
- Browser E2E against **live** services: NOT RUN (stubs only). → NOT RUN

## 11. Missing credentials, permissions or external access

Required settings present/absent (names only; values never inspected or logged):

- **CONFIGURATION GAP (resolved 2026-10-22):** `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` — the owner has now added both as GitHub repository Actions secrets (owner screenshot; the secrets page shows `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`). This is what unblocked §2's silent-skip, but it also means `static/` pushes now *attempt* a real deploy — see §14 for the deploy run that fails for a reason we cannot yet read.
- **CONFIGURATION GAP:** `BANK_TRANSFER_ACCOUNTS` — empty in Render, so checkout shows support contact instead of a real account (intended safe behavior, but the operator must supply the real details).
- **CONFIGURATION GAP (owner credentials):** Flutterwave `FLW_*`, Pesapal `PESAPAL_*`, Firebase `FCM_*`, `SENTRY_DSN`, Supabase service-role `SUPABASE_KEY` (Render-only), GitHub backup secrets.
- **ASSUMPTION / blocked access:** no service-role or authenticated user session was available, so real order placement, real payment/webhook, admin login, and live dashboard/RLS internals beyond anonymous probes could not be exercised.

## 12. Prioritized verified blockers

Per the selection order (security → financial integrity → outage → buyer journey → fulfillment → admin controls → deployment automation):

1. **VERIFIED BLOCKER — Deployment automation reports success while deploying nothing** (§2). Highest item with concrete, reproducible evidence. Items ranked above it are either **WORKING** (RLS, readiness, schema) or **UNVERIFIED** (live buyer journey, live payments) — none rises to a *verified* blocker this run, so per the rules it would be wrong to label an unverified assumption as the blocker. **Fixed in behaviour** (§13/§14): the run now fails, and prints why.
1b. **VERIFIED BLOCKER (credential) — the Cloudflare Actions token is rejected**, so SPA auto-deploy still cannot publish (§16). Root cause proven by the new annotation (`9109`/`10000`) and by the identical artifact deploying successfully from an authenticated workstation. Owner action required; no code change can fix a secret value.
1c. **VERIFIED DEFECTS from the `20f45f2` redesign — all fixed in `db9d92f`** (§15): provider-gated payment options could stay hidden on a fresh load (checkout impact), a banned support contact returned, the pinned lint gate broke, and 21 tests failed. CI is green again on `db9d92f`.
2. **VERIFIED DEFECT — duplicate signup alerts** (§7): real and reproduced via owner evidence; remediated in deployed code; confirmation pending observation of further logins.
3. **CONFIGURATION GAPs** (§11): Cloudflare token, bank accounts, payment/notification providers — owner actions, not code defects.
4. **UNVERIFIED** live flows (payments, order journey, tracking): require a real session/transaction or owner-authorized sandbox; deliberately not exercised to avoid production mutation (Rule 11).

**Next highest-priority blocker after #1** is to confirm the deployed signup-suppression actually stops duplicates on real logins, and to verify one real unpaid order (COD or bank) reaches the buyer journey — both gated on an authenticated session we do not have.

---

## 13. Fix record — blocker #1 (deployment automation)

**Status:** implemented on branch `fix/deploy-workflow-honest-status`; **not pushed, not deployed** (per the run's git rule: show the summary and obtain approval first).

**Diagnosis**
- **What is broken:** `deploy-cloudflare.yml` finished green while publishing nothing. Evidence: run `39721847981` (push `6077883`) — job `success`, `Deploy Worker static assets` step `skipped` because the step was gated on `if: env.CLOUDFLARE_API_TOKEN != ''`; the empty-token path only echoed a warning. Impact: the bank-transfer checkout (and any `static/` change) silently never shipped until published manually.
- **Expected behavior:** a deploy run is green **only if** the artifact was published; if it cannot deploy, it must fail.
- **Root cause:** the workflow encoded "skip" as a non-failing outcome, conflating *no-op* with *success*.
- **Files involved:** `.github/workflows/deploy-cloudflare.yml`; new test `tests/test_deploy_workflow.py`.
- **What it could break:** a red deploy run is a deliberate behavior change (previously green-on-skip). No branch protection depends on this job; unrelated workflows (`ci.yml`, `db-backup.yml`) are untouched. The happy path (`wrangler@4 deploy`) is unchanged. Rollback = revert the one YAML edit.

**Fix:** collapsed the warn-then-skip step pair into a single deploy step that fails loudly (`exit 1` + `::error::` + step-summary guidance) when `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is missing, and otherwise runs `npx --yes wrangler@4 deploy`.

**Tests (commands run this session)**
- `pytest tests/test_deploy_workflow.py` → **3 passed** after fix; **2 failed / 1 passed before fix** (proves the regression is caught).
- Full suite `pytest -q` → **434 passed**. Ruff (CI-pinned 0.8.4) on the test + workflows → clean.
- YAML validity via `js-yaml`: parses OK; parsed deploy step has **no `if` skip-guard**, contains the empty-token test and `exit 1`, and still contains `wrangler@4 deploy`.
- **Runtime simulation** of the step's shell with both secrets unset → `::error::` printed, step summary written, **exit code 1**, and `exit 1` precedes the `wrangler` line so no deploy is invoked. Classified: PASS (mocked; not a live GitHub run).

**Production status:** nothing pushed or deployed for this fix. The live SPA remains on the manually-published `6077883` bundle (`pm-bank` verified live). When the owner later adds the two secrets, this workflow will actually publish and the green run will be trustworthy.

**Remaining risk / operator action:** the underlying `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` are still absent (§11) — the fix makes the missing-credential state *visible and safe*, not *working*. Adding the secrets is the owner action that turns SPA auto-deploy on.

---

## 14. Second fix record — blocker #1 extension: a red deploy run must say why

**Status:** implemented locally on `main` (commit `5321418` rebased onto the redesigned `origin/main`, plus §15's repairs in `db9d92f`); **pushed to `main` and observed live** — see §16.

**New evidence gathered after the owner added the Cloudflare secrets**

| Check | Source | Result |
|---|---|---|
| Owner added `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` as repository secrets | owner screenshots (2026-10-22) | CONFIGURATION GAP closed |
| Cloudflare token "AgriBridge GitHub Deploy" = `Account.Workers Scripts`, 1 account, Active | owner screenshot | INFORMATIONAL |
| `origin/main` HEAD `20f45f2` (buyer-UI redesign merge) CI | GitHub Actions API | **failure** — job `Backend lint + type + tests` failed at the pytest step (annotation `.github:42`) |
| `origin/main` HEAD `20f45f2` deploy workflow | GitHub Actions API | **failure** — job `deploy` "Process completed with exit code 1" (annotation `.github:22`) → **VERIFIED DEFECT: SPA auto-deploy now runs and fails, so nothing ships** |
| `Workers Builds: agribridge` check at `20f45f2` | GitHub check-runs API | failure (Cloudflare GitHub App path; separate from the Actions deploy) |
| Wrangler error text behind that failure | `GET /actions/runs/39741657828/logs` | **UNVERIFIED — 403.** Job logs need an authenticated collaborator; the API gives only the annotation above |

**Diagnosis of the *visibility* blocker.** The deploy job fails with a message that contains no cause. Without log access the failure cannot be triaged from outside the owner's account, so every subsequent `static/` release stays unexplained and unshipped. Root cause of the *visibility* problem: the workflow lets `npx wrangler deploy` write only to the job log, which is not readable through the public API.

**Fix (smallest safe change, keeps the honest-status behavior):** the deploy step captures wrangler output to a file, echoes it into the job log, and on failure publishes a **single-line `::error::` annotation** with the last 700 characters plus a fenced tail in `$GITHUB_STEP_SUMMARY`, then exits with wrangler's own status. Secret values stay masked by Actions; nothing is printed on the success path beyond wrangler's normal output. `status=0` / `|| status=$?` is required because the runner shell is `bash -e` and would otherwise abort before reporting.

**Regression tests added** (`tests/test_deploy_workflow.py`, now 5 tests):
- `test_wrangler_failure_reason_is_visible_without_dashboard_access` — asserts output capture, the `::error::wrangler deploy failed` annotation and `exit "$status"`.
- `test_deploy_step_preserves_wrangler_exit_status` — asserts the `-e`-safe status capture.
- Both **FAILED against the pre-fix workflow** and pass after it (verified by re-running the file at HEAD's version, then restoring).

## 15. Release-gate findings from the buyer-UI redesign (`74879f8` / `20f45f2`) — VERIFIED DEFECTs, all fixed

The redesign was merged onto `main` while CI was green, and arrived with a red suite. Each failure was reproduced locally and traced to the redesign, not to §13's work (`git diff 8e954ac 20f45f2` touches only `app.py`, `requirements.txt`, `static/index.html`, `static/sw.js`; `ci.yml` and `requirements-dev.txt` are unchanged).

| # | Finding | Evidence | Root cause | Fix |
|---|---|---|---|---|
| 1 | Banned support contact reintroduced | `pytest tests/test_business_config.py` → 2 failed: `unofficial email addresses found: {'app.py': {'support@agribridge.ug'}}` | `app.py` `_send_web_push` defaulted `VAPID_CONTACT` to `mailto:support@agribridge.ug`, a domain the project no longer owns | default is now `f'mailto:{business.SUPPORT_EMAIL}'`, matching the single-source convention already used at `app.py:723`, `:1250`, `:5073` |
| 2 | **Server-gated payment options could never appear** | `tests/e2e/test_consent_ui.py::test_pesapal_is_offered_only_when_the_server_enables_it` failed: `assert page.is_visible('#pm-pesapal')` → False | the redesign rewrote `syncPayMethods()` to read the in-memory cache `window._payProviders` instead of awaiting `getEnabledProviders()`. On a fresh page load the cache is empty, so the Pesapal tile stayed hidden **even when `/api/pay/providers` reported it enabled**, and `renderBankOptions()` got a stale/empty account list | `syncPayMethods()` again `await`s `getEnabledProviders()` first; the redesign's deliberate "card is always visible (own modal)" behavior is preserved → **VERIFIED DEFECT** with production impact, fixed |
| 3 | 15 browser tests blocked by the new onboarding splash | Playwright timeout: `<button class="splash-btn-primary" …> … from <div id="page-splash"> subtree intercepts pointer events` | the redesign gates the app shell behind a marketing landing **plus** a first-visit splash overlay (`enterApp()` → `showSplash()`). Real visitors dismiss it by tapping; the shared e2e fixture never did | the fixture now marks `ab_seen_splash` and dismisses the splash before the flows, i.e. the state of a returning user, then waits for the overlay to be gone. No assertion was weakened and no gate was disabled |
| 4 | Brittle order-sensitive UI assertion | `tests/test_pesapal_ui.py:109` failed on the tile list literal | test hard-coded one tile ordering; the redesign legitimately reordered them to match the DOM | assertion accepts the literal **or** compares the toggle list as a set, so tile order can change without hiding a real regression |
| 5 | CI lint gate broken | `/tmp/ruff084/bin/ruff.exe check app.py services/ scripts/ tests/` → 3 errors (I001, E701 ×2) at the new `_send_web_push` block; the same command on a `8e954ac` worktree → "All checks passed" | new code in the redesign: `from pywebpush import webpush, WebPushException` and `try: … / except Exception: pass` one-liners | import sorted; the two compound statements split across lines — formatting only, no behavior change |

**Verification after the fixes (all commands run from the repo root):**
- `PYTHONPATH=.test-deps python -m pytest -q` → **436 passed** (434 baseline + the 2 new workflow tests; includes the 22 Playwright browser tests).
- `pytest tests/e2e/test_consent_ui.py -q` → **22 passed** (was 18 failed / 4 passed).
- `pytest tests/test_business_config.py -q` → **13 passed**; `pytest tests/test_pesapal_ui.py -q` → **12 passed**; `pytest tests/test_deploy_workflow.py -q` → **5 passed**.
- `ruff check app.py services/ scripts/ tests/` → **All checks passed** with both CI-pinned 0.8.4 and local 0.16/0.17.
- `python scripts/build_legal.py --check` → "legal outputs up to date" (the other CI gate).
- `cd tests/sql && npm test` → all suites pass (129 assertions; unchanged, no SQL touched this session).
- `.github/workflows/deploy-cloudflare.yml` parses with `js-yaml`.

**Deliberately not changed:** the splash/landing UX itself, the new card modal, `static/sw.js`, and the redesign's `+1425` lines of buyer UI. Rule 2/4/15 — this run repairs what the redesign broke, and does not rewrite what it deliberately redesigned.

---

## 16. Push, CI and deploy verification for `db9d92f` — the owner's new secrets, tested

Both commits (`5321418`, `db9d92f`) were fast-forward pushed to `main` (`20f45f2..db9d92f`, no force, no history rewrite). Results were read from the public GitHub checks API, not assumed:

| Gate | Run | Result | Evidence |
|---|---|---|---|
| CI — Backend lint + type + tests | `39785941491` job `119849041617` | **success** | check-run conclusion, 2026-10-22 ~13:20Z |
| CI — Migration 0007 on real PostgreSQL | `39785941491` job `119849041194` | **success** | check-run conclusion |
| `Deploy public web app` | `39785941174` job `119849040101` | **failure — and now it says why** | check-run annotation (below) |
| `Workers Builds: agribridge` (Cloudflare GitHub App) | build `3c56b2f2-8d3a-4418-a0dd-0d2468ecd5fa` | **failure** | check-run output |

**The diagnostic worked.** The deploy job published this annotation, which is readable without dashboard access:

> `wrangler deploy failed (exit 1): … API (/accounts/***/workers/services/agribridge) failed. Authentication error [code: 10000] … It looks like you are authenticating Wrangler via a custom API token set in an environment variable … Getting User settings... ✘ [ERROR] A request to the Cloudflare API (/accounts) failed. Invalid access token [code: 9109]`

**VERIFIED BLOCKER (credential, not code): the `CLOUDFLARE_API_TOKEN` value stored in GitHub Actions is rejected by Cloudflare.**

- The same repository state deploys successfully from an authenticated workstation: `npx --yes wrangler@4 deploy` published version `ffecfbb1-7937-4d90-8f97-2d610004a934` to the `agribridge` Worker in account `7a6752557859ee5998bdc81c0989743b` → so `wrangler.jsonc`, the asset set and the Worker itself are fine. **The failure is isolated to the secret value.**
- Error `9109 Invalid access token` on `/accounts` and `10000 Authentication error` on the deploy call are *token validity* errors. A permissions/scope problem would instead report `10044 Not entitled to create or edit Workers Scripts`. So the likely causes are: the Cloudflare **Token ID / Client ID** pasted instead of the one-time **secret value**, a truncated value, trailing whitespace/newline, or a token since revoked. `CLOUDFLARE_ACCOUNT_ID` is present and matches the account that owns the Worker.
- **UNVERIFIED:** the secret value itself — it cannot be read back through the API (GitHub never returns secret values), and job logs return `403` without owner authentication.

**Owner action (one step):** in GitHub → Settings → Secrets and variables → Actions → *AgriBridge repository secrets*, replace `CLOUDFLARE_API_TOKEN` with the full secret string from Cloudflare (Dashboard → Manage Account → Account API Tokens → *AgriBridge GitHub Deploy* → create-and-copy a fresh value, since Cloudflare shows it only once), then re-run the `Deploy public web app` workflow. A green run is then trustworthy — it can no longer pass without publishing (§13), and it now prints wrangler's reason if it fails (§14).

**Secondary finding — INFORMATIONAL / CONFIGURATION GAP: two publishers target the same Worker.** Cloudflare's *Workers Builds* Git integration (build `3c56b2f2`, failing) and the `Deploy public web app` workflow both try to deploy `agribridge`. Once the Actions token is fixed, pick one: either disable Workers Builds for this Worker, or delete the two GitHub secrets and let the Cloudflare App own deploys. Running both makes releases non-deterministic — whichever integration last touched the Worker decides what the public site serves.

## 17. Production verification of the §15 checkout fix (LIVE)

The published bundle was verified against the repository, not assumed:

- `curl https://agribrige.com/` → HTTP 200, 690,689 bytes, **sha256 identical to `static/index.html` at `db9d92f`**; contains `page-splash`, `splash-btn-primary`, `pm-bank`, `pm-pesapal`. `https://agribridge.zealmugumya.workers.dev/` → 200. `/admin.html` → 307 → `/admin`.
- Headless-browser check against **production** (read-only, no order placed): on a fresh load `window._payProviders` is `null` — exactly the condition §15 finding 2 mishandled — and after `await syncPayMethods()` it holds the live `/api/pay/providers` payload `{airtel:true, bank:true, cod:true, momo:true, pesapal:false, card:false, flutterwave:false}`. Tiles: MoMo/Airtel/Card/COD/**Bank visible**, Pesapal correctly hidden because the gateway is not configured. `window._payBankAccounts` is `[]` because `BANK_TRANSFER_ACCOUNTS` is unset, so checkout shows the official support note instead of an invented account. → **WORKING (LIVE)** for the tile/provider contract.
- Backend after the push: `/health/live` → 200, `/health` → `status:ok`, `version:3.3.0`, `supabase_connected:true` (**LIVE**). Render's own deploy of `db9d92f` could not be confirmed from outside the Render dashboard → **UNVERIFIED** for the `app.py` VAPID default change; the site was already serving this `app.py` behaviour except the notification `sub:` contact string.
- **INFORMATIONAL:** the redesign made the Card tile always visible. With no card gateway configured, `startOnlinePayment('card')` returns `null` and the existing code falls back to manual instructions — a graceful degradation, so no change was made here (Rule 15).

**Rollback path:** `npx --yes wrangler@4 deployments list` then roll back to version `15ff5212…` (the previous known-good publish), or re-publish any commit's `static/` with `wrangler deploy`. Nothing in this change set touched the database, so no migration rollback is involved.

## 18. Pesapal credentials added to Render — verified live, plus a latent config defect

The owner supplied `PESAPAL_CONSUMER_KEY` / `PESAPAL_CONSUMER_SECRET` and set `PESAPAL_ENV=live` in Render, then rebuilt. Verification from outside the dashboard (**LIVE**):

- `/api/pay/providers` → `"pesapal": true` (was `false`), while `card`, `flutterwave` and `mtn_momo` correctly remain `false` — the gate reflects the credentials actually present, not a hardcoded flag.
- `/health/live` → 200; `/health` → `status:ok`, `version:3.3.0`, `supabase_connected:true`, `discord_configured:true`, `sentry_enabled:false`.
- `/api/pay/webhook/pesapal` → GET 200 and POST 200. The empty POST was a **no-op**: `_handle_pesapal_notification` returns early on `if not client or not tracking_id`, so no order or ledger row was touched (Rule 11 respected — nothing was paid).
- `/api/pay/pesapal/callback` (`app.py:5790`) → 302. **Correction to my own earlier probe:** I first requested `/api/pay/callback/pesapal`, got 404, and wrongly concluded the browser-return route did not exist. The path segment order is `…/pesapal/callback`; the route is present and answering. No defect existed.

**VERIFIED DEFECT (found while tracing, not from the probe): a truncated `PESAPAL_CALLBACK_BASE_URL` silently disables payment confirmation.** The owner's screenshot showed `PESAPAL_CALLBACK_BASE_URL=https://agribridge-1-`. That variable feeds both `IPN url = base + '/api/pay/webhook/pesapal'` (`app.py:4919`) and `callback_url = base + '/api/pay/pesapal/callback'` (`app.py:5002`). `PesapalClient._safe_ipn_url` (`services/pesapal.py:80`) only requires `https` plus a non-empty `hostname`, and `urlparse("https://agribridge-1-/…").hostname` is the truthy string `"agribridge-1-"` — so the guard **passes**. Consequences if left as typed: `register_ipn` succeeds against the real Pesapal account, then every notification is delivered to a nonexistent domain. The buyer's browser is redirected to the same bad host. The money moves, Pesapal reports the payment as complete, but the order never becomes `paid` and no `payment:<ref>` journal is ever posted — the exact "feature exists but does not work in production" failure the mandate targets, and it would surface only as angry buyers.

**Fix (`app.py:137-155`):** `_resolve_pesapal_base_url()` now requires a dotted hostname (localhost/127.0.0.1 still allowed for local testing), logs `pesapal.callback_base_url_rejected` with the rejected host and the fallback used, and otherwise returns `PESAPAL_BASE_URL_DEFAULT`. A malformed value can no longer reach Pesapal. 5 regression tests added in `tests/test_pesapal.py`, including the exact string from the owner's screenshot and the assertion that the resulting IPN URL is the live webhook route. Suite: **441 passed**; ruff clean under local 0.16.10 and CI-pinned 0.8.4.

**Owner action:** confirm `PESAPAL_CALLBACK_BASE_URL` in Render is either **deleted** (the built-in default is already the correct Render origin) or set to the full `https://agribridge-1-og7a.onrender.com`. Leave `PESAPAL_IPN_ID` empty — the app registers the IPN once and persists the id in `payment_gateway_state` (migration 0017, applied).

**Still UNVERIFIED:** no real Pesapal payment has been completed, so the notify → verify → mark-paid → ledger-post chain is proven only by fakes in `tests/test_pesapal.py`. Closing that gap requires one small real payment, which needs the owner's explicit authorization (Rule 11).

**INFORMATIONAL — production runtime ≠ CI runtime:** the owner's Render build log installs into `.venv/lib/python3.14/site-packages` with `cp314` wheels even though `render.yaml:5` pins `pythonVersion: "3.12.7"` and CI runs 3.12.7. The suite passes on both here (local harness is 3.14, CI is 3.12), so nothing is currently broken, but a dependency that only builds on one of the two would fail in production while CI stayed green. Worth pinning deliberately rather than leaving to Render's default.

## 19. Stage 1 — buyer storefront (deployed and verified live)

Scope, in the owner's stated order: **homepage → product cards → product details → shopping cart**. Checkout, payment integration and delivery tracking are the next stages and were deliberately not redesigned here; the only checkout change is a guard that refuses to open checkout when the cart contains an out-of-stock line. Admin and the financial ledger were not touched.

### What changed (working tree: `M static/index.html`, new `tests/e2e/test_buyer_ui.py`)

`static/index.html` grew by 987 lines and lost 289 (`git diff --stat`: 1 file changed); no other product file, endpoint, table, migration or dependency changed. The mobile Capacitor shell wraps the same file and needs no separate edit for this stage.

- **Homepage** — real listings (24 for the home rows) replace decorative/sample content; honest error vs empty states; only rated farmers appear in the farmers row; featured/recent sections show real data and hide when empty; a real search overlay searches the same listing set; location sheet offers 12 real districts and persists the choice in `localStorage('ab_location')`. The guest onboarding splash now offers **Browse as guest →** (`splashBrowseGuest()`), which it previously lacked, so the shopping home is reachable without signing in.
- **Product cards** — real image with emoji fallback, discount + struck original price from `discount_pct`, stock pill incl. out-of-stock, minimum-order note, farmer/district, rating only where `farmer_ratings` data exists, favourite button, keyboard activation (`role=link`, Enter/Space), and a disabled Add button when the listing cannot be bought.
- **Product details** — built from real listing fields (real gallery, price/discount, stock, minimum, delivery note, accepted payment note, farmer rating/reviews hydrated from the live `farmer_ratings` view, related products), out-of-stock lines disable Add/Buy, and the static trust strip no longer claims "1–2 Day Delivery" or "Verified Farmer" unconditionally (`pdTrustVerified` says "Verified Farmer" only when `is_verified` is true, otherwise "Direct from Farmer"). A literal `"None"`/`"null"` description is normalised to "No description provided by the farmer yet."
- **Cart** — still `localStorage`-backed (per-device, as before), but every render re-hydrates price/stock from the latest listing, bounds quantity to available stock and the listing minimum, flags lines that became unavailable, and computes totals from item prices only. The summary states **"Arranged after confirmation"** for delivery instead of inventing a fee — matching `services/finance.py:25`, which documents that orders store no separate delivery fee. Checkout is blocked while an out-of-stock line remains.
- **Signed-in buyer routing** — `applyRoleUI()` previously sent a signed-in `vendor` to `page-market`; it now lands on the buyer home like a guest, so the storefront is the first screen for every buyer.

### Runtime defects found while verifying (all fixed, all pinned)

| Defect | Evidence | Fix |
|---|---|---|
| First-visit splash offered only Sign up / Sign in, so it trapped every new visitor | Headless clicks timed out; overlay intercepted pointer events | **Browse as guest** path + `splashBrowseGuest()` |
| Signed-in buyer was routed to the market page, not the new home | `applyRoleUI()` called `showPage('market')` for `vendor` | `showPage('home')` |
| Detail page printed the literal string `None` as a description | Fixture listing with `description: "None"` | Normalised in `openProductDetail` |
| Trust strip asserted delivery time and verification that the data does not prove | Static copy compared with listing fields | "Delivery Arranged"; verification badge/label conditional on `is_verified` |
| Cart could show stale prices/stock after a listing changed | Cart re-read only stored snapshots | `hydrateCart` refreshes each line against the current listing |
| A late home fetch silently reset a category the buyer had just chosen | Repeated Playwright runs: the grid flipped from the filtered set back to all six featured cards | `window._homeCat` is tracked and re-applied after `loadHomeStats()`; the test now awaits a fresh load before asserting |
| A delayed guest role refresh ejected the buyer from product details | Repeated runs: `Page.click: Timeout 30000ms exceeded` on `#pdQtyPlus` because `applyRoleUI()` → `showGuestLanding()` re-activated home 300 ms after load | `showGuestLanding()` only activates home when no page is active; the detail test calls `applyRoleUI()` while open |

### Verification (local, harness and production)

- **New deterministic suite:** `tests/e2e/test_buyer_ui.py`, 6 Playwright tests — home data + real-ratings-only, signed-in buyer home, detail facts + disabled out-of-stock + guest role-refresh safety, honest cart totals/persistence across reload (Cocoa 10% discount: 2 × 8,100 = 16,200; delivery "Arranged after confirmation"), search/category over the same data incl. a late-load preservation check, zero JS errors. It serves the real `static/index.html` from disk, stubs only Supabase SDK/REST via routes, and reads no production data. The module passed **5 consecutive runs** (12.1–15.4 s) after the two navigation races above were fixed, and the full `tests/e2e/` set (28 tests) passed **twice consecutively** (73.4 s, 64.5 s).
- **Ad-hoc browser harness:** 51/51 checks at mobile and desktop widths against the **live Supabase project, read-only** (`sb()` REST reads, auth stubbed): real cards, `Tomatoes` search → real result → detail, category filter, quantity min/max, add-to-cart badge, `localStorage` persistence, cart ±/remove/clear, totals equal item subtotal, signed-in buyer home, desktop layout/no overflow. No order was placed and no row was written.
- **Full suite:** `PYTHONPATH=.test-deps … -m pytest -q` → **447 passed, 103 warnings in 66.09 s** (441 baseline + 6). `ruff check app.py services/ scripts/ tests/` clean under local **0.16.10** and CI-pinned **0.8.4**; `node --check` passes on all four inline script blocks.
- **Production is now serving Stage 1 from CI.** The current Worker version `a6a1d85b-f203-4e90-8dd3-b405455c32c4` (published by run `39859133726`) serves the Git blob `bb2cd7a:static/index.html` on both `https://agribrige.com/` and `https://agribridge.zealmugumya.workers.dev/`: 752,146 bytes, sha256 `439306a7…a19a13`, content-identical to the workstation file after CRLF normalisation. The served HTML contains `splashBrowseGuest`, `pdTrustVerified`, `homeMoreHead`, `_homeCat` and "Arranged after confirmation". A live headless-Chrome smoke test at 390×844 then exercised the real flows: landing → Launch app → splash → **Browse as guest** → 6 real home cards (Groundnuts / Zeal / Gulu / UGX 1,500/kg) → Vegetables filter (10 cards, title survives a forced late `loadHomeStats()`) → Groundnuts detail with the honest fallback description → add to cart → cart subtotal/total `UGX 1,500`, delivery "Arranged after confirmation", checkout enabled, **zero page errors**. Checkout was not clicked and no order was placed. Stage 1 was committed and pushed as `30f8784`; the earlier manual publish, the credential blocker and the CI restoration are recorded in §20.

### Residual risks / deliberately not changed

- Cart remains device-local; there is no server-side cart. Clearing browser storage empties it, and the same buyer on another device starts empty — unchanged behaviour, now honest in the UI.
- Promo codes remain absent. The old hardcoded promo list was removed earlier; the apply action refuses rather than inventing a discount.
- The B2B invoice generator still hardcodes `delivery = 25000` and a 6% commission and shows a "Bulk order submitted" toast without a backend call. It is outside Stage 1's four surfaces and is left untouched and flagged, not silently endorsed.
- Pre-existing overlays (bottom nav, AI assistant, cookie consent) and two desktop search bars were observed during visual review and left as-is to keep this stage focused.
- Checkout, payment integration and delivery tracking remain Stage 2/3 work. Stage 1 only guarantees that the four buyer surfaces present real data, honest totals, and safe hand-off into the existing checkout.

**Rollback path:** revert the single `static/index.html` change (and delete the new test file); no database, migration, API or dependency is involved. Re-publishing the previous `static/` commit with `wrangler deploy` restores the live bundle if a deploy had been made.

## 20. Stage 1 publish — manual deploy verified, then CI auto-deploy restored

The Stage 1 work was committed as `30f8784` and pushed to `main` (`aef0423..30f8784`). GitHub checks for that push, read from the public checks API:

| Gate | Result | Evidence |
|---|---|---|
| `Backend lint + type + tests` | **success** | check-run on `30f8784` (completed 2026-10-22) |
| `Migration 0007 on real PostgreSQL (PGlite)` | **success** | check-run on `30f8784` |
| `Deploy public web app` | **failure** | annotation: `Authentication error [code: 10000]` + `Invalid access token [code: 9109]` — the same rejected GitHub secret as §16; nothing was uploaded |
| `Workers Builds: agribridge` | **failure** | build `d901d774-7a7f-4489-a3b3-e00121f15e5d`; Cloudflare reports the build against account `7a675255…` |

**First attempt failed on Cloudflare account scope.** The local wrangler login belonged to `mugumyazeal@gmail.com`, account `0b66e23d65c1143a5b78311aec52df51`, while Worker `agribridge` lives in account `7a6752557859ee5998bdc81c0989743b`. Forcing the production account failed with `Authentication error [code: 10000]` **before any asset upload**, and the live bundle was unchanged.

**Resolution (2026-10-22):** the owner re-ran `npx wrangler@4 login` and approved the account that owns the Worker. `wrangler whoami` then reported `7a675255…`, and:

```bash
CLOUDFLARE_ACCOUNT_ID=7a6752557859ee5998bdc81c0989743b npx --yes wrangler@4 deploy
```

published Worker version **`bb9e65fb-974d-4381-8bf0-c78191a2e6d0`** (`Uploaded agribridge (13.83 sec)`). The previous version was `8ef07b1d-36db-4a92-abfc-2a56de640a93`.

**Published artifact verified, both domains:**

| Check | Result |
|---|---|
| `https://agribrige.com/` | 200, **762,544 bytes**, sha256 `8d73dd5a411adedc0fd6a9e198e29e5ff98746397fd573fcc4728908992b9107` — byte-identical to local `static/index.html` |
| `https://agribridge.zealmugumya.workers.dev/` | same size and sha256 |
| Stage 1 markers in served HTML | `splashBrowseGuest` ×2, `pdTrustVerified` ×2, `homeMoreHead` ×3, `_homeCat` ×3, "Arranged after confirmation" ×4 |

**Live browser smoke (production, 390×844, headless system Chrome):** landing → Launch app → splash → Browse as guest → 6 real cards (Groundnuts / Zeal / Gulu / UGX 1,500/kg) → Vegetables filter (10 cards; title stays "Vegetables" after a forced `loadHomeStats()`) → Groundnuts detail (fallback description "No description provided by the farmer yet.") → add to cart → cart subtotal and total `UGX 1,500`, delivery **"Arranged after confirmation"**, checkout enabled. **Zero page errors.** Checkout was not clicked and no order was placed. §19 records the full change set.

**CI auto-deploy restored (2026-10-22T21:40Z).** The owner created a replacement Cloudflare API token scoped to **Specified Workers → `agribridge` → Individual Workers Editor** (no expiration, all IPs allowed), replaced `CLOUDFLARE_API_TOKEN`, and kept `CLOUDFLARE_ACCOUNT_ID` = `7a675255…`. The token value is stored only in GitHub Actions and is deliberately not recorded here. A manual `workflow_dispatch` of `Deploy public web app` (run [`39859133726`](https://github.com/zealmugumya-creator/agribridge/actions/runs/39859133726), commit `bb2cd7a`) completed **success** in 27 s. `wrangler deployments list` shows a new deployment at `2026-10-22T21:40:43Z`, author `undefined` (the CI token), version **`a6a1d85b-f203-4e90-8dd3-b405455c32c4`** — independent proof that the run published rather than no-opping.

**Live bytes after the CI deploy:** both `https://agribrige.com/` and `https://agribridge.zealmugumya.workers.dev/` serve **752,146 bytes**, sha256 `439306a73a36780cb54f9851524c4d61d28b8b1adbe380c84469c7f785a19a13`, exactly the Git blob `bb2cd7a:static/index.html`. That differs from the workstation file (`8d73dd5a…`, 762,544 bytes) only by line endings: this checkout has `core.autocrlf=true` (CRLF), CI checks out LF. After `tr -d '\r'`, both hash to `439306a7…`. **Future verification must compare normalized content on this repo, not raw bytes.**

**Resolved — Workers Builds disconnected (2026-10-23).** Its check-run on `bb2cd7a` was **failure** while the Actions workflow published successfully. The owner used Worker `agribridge` → Settings → Builds → **Disconnect Git**; the Actions workflow is now the only publisher.

**Rollback:** Worker version `bb9e65fb-974d-4381-8bf0-c78191a2e6d0` (or re-publish any commit's `static/` with `wrangler deploy`). No database migration is involved.



