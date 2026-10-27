# AgriBridge — Google Sign-In

**Last updated:** 2026-10-22
**Status:** implemented in the production SPA source (`static/index.html`) and
verified in a browser test against a stubbed Supabase OAuth endpoint. The bundle
was deployed on 2026-10-22 (Cloudflare run `39710797408`). The real Google flow
has **not** been run: it requires the project owner to enable the provider in
Supabase and Google Cloud. No migration is required.

---

## 1. What the user sees

* The **Log In** and **Create Account** panels show a white
  “Continue with Google” button.
* The buttons stay hidden unless `GET /auth/v1/settings` on the Supabase project
  reports `external.google = true`, so an unconfigured project never shows a
  dead button.
* Clicking either button starts Supabase OAuth with:
  * `provider: "google"`
  * `redirectTo: window.location.origin + "/"`
  * `queryParams: { prompt: "select_account" }`
* After Google returns, Supabase creates/restores the session and the SPA's auth
  listener takes over.
* A first-time Google user must complete a **locked onboarding modal** before
  using the app. It collects:
  * full name (prefilled from the Google profile when available);
  * a Ugandan phone number (`+2567XXXXXXXX` after normalisation);
  * account type — Farmer, Vendor/Buyer, Hotel/Restaurant, or
    Supplier/Input Seller;
  * district;
  * acceptance of the Terms of Use plus the privacy/role terms for that account
    type.

The modal has no close button and no backdrop-click handler, and it is marked
`modal-locked`, so Escape does not dismiss it. The only way out is
**Use a different account**, which signs the user out and reopens login.

---

## 2. Where the data goes

On **Finish setup** the browser:

1. Calls `sbAuth.auth.updateUser({ data: {...} })` to store
   `name`, `full_name`, `phone`, `role`, `district` and a `legal_signup`
   snapshot (`accepted_at` + document versions) in the user's Supabase metadata.
2. Calls `saveProfileWithRetry()` → `POST /api/user/profile`, which upserts the
   caller's row in `farmers` using the verified session. There is **no
   `handle_new_user` trigger**, so this call is what creates the profile row.
3. Calls `postAuthLegalCheck('oauth_onboarding')` →
   `POST /api/legal/sync-signup`, which records the signup acceptance in the
   legal consent tables and runs the normal outstanding-document check.

If the server profile write fails but auth metadata succeeds, the user is told
the delivery details will be saved when the connection returns; the app does not
pretend the profile row exists.

### Authority boundaries

* Google identity is verified by Supabase; the SPA never trusts an email string
  or a client-provided user id.
* API calls use the Supabase access token; the backend re-verifies it with
  GoTrue and derives the user id from the token.
* The onboarding role list contains only farmer/vendor/hotel/supplier. Choosing
  one does **not** grant admin. Admin authority remains the server-side
  `user_roles` table plus the `ADMIN_ALLOWED_UUIDS` allowlist
  (`docs/OPERATIONS_SETUP.md` §3); no frontend metadata can promote an account.
* The browser bundle contains only the Supabase **publishable** key. The Google
  client secret and the Supabase service-role key are never in `static/`.

---

## 3. Owner setup (required once)

### 3.1 Google Cloud

1. Create or select a Google Cloud project.
2. Configure the OAuth consent screen:
   * User type: External (or Internal for a Workspace-only deployment);
   * app name, support email, developer contact;
   * scopes: `openid`, `email`, `profile` (Supabase requests these);
   * while the app is in **Testing**, only listed test users can sign in;
     publish it before opening sign-in to the public.
3. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**
   → **Web application**.
4. Add **Authorized JavaScript origins**:
   * `https://agribrige.com`
   * any preview/dev origin used for testing (for example
     `http://localhost:5000`).
5. Add **Authorized redirect URIs**:
   * `https://lvloaweijidgcuezdnmu.supabase.co/auth/v1/callback`

   This is the Supabase callback shown in the Supabase provider settings. It is
   **not** the AgriBridge site URL.
6. Copy the client ID and client secret.

### 3.2 Supabase

1. Dashboard → **Authentication → Providers → Google** → enable.
2. Paste the Google client ID and client secret. Save.
3. Dashboard → **Authentication → URL Configuration**:
   * **Site URL** — `https://agribrige.com`
   * **Redirect URLs** — add `https://agribrige.com/` and any development or
     preview origins used with the SPA (for example
     `http://localhost:5000/`). The SPA sends
     `redirectTo = window.location.origin + "/"`, so that exact origin must be
     allowed.
4. No Render environment variable is needed for Google. Do not put the Google
   client secret in `static/index.html`, Cloudflare variables, or client-side
   storage.

### 3.3 Verify

1. Open the live site in a private window.
2. Confirm the “Continue with Google” button appears on both login and
   registration. If it does not, see §5.
3. Sign in with a test Google account.
4. Complete the onboarding modal and confirm:
   * the app opens as the chosen account type;
   * `farmers` has a row for the new user with the phone/role/district;
   * `legal_acceptances` has a signup acceptance with the current document
     versions;
   * the account is **not** an admin.
5. Sign out and sign in again; onboarding must not reappear when phone, role,
   name and district are present.

---

## 4. Behaviour details

* **Provider gating** — `refreshOAuthButtons()` runs on page load. A failed
  settings request leaves both buttons hidden rather than showing a button that
  cannot work.
* **Return URL** — the SPA is served from the site root, so the OAuth redirect
  returns to `/`. Supabase parses the session from the URL fragment
  (`detectSessionInUrl` default). The normal `onAuthStateChange` path updates
  the nav and user state.
* **Profile completeness** — `_oauthNeedsProfile()` requires `phone`, `role`,
  `name`/`full_name` and `district` in metadata. Google supplies the name and
  email; the modal supplies the rest.
* **Legal re-acceptance** — after onboarding, the ordinary legal check runs and
  can still show a review modal if a document version is outstanding or has
  changed. That is intentional; onboarding is not a substitute for later
  re-acceptance.
* **Email/password accounts** are unchanged. Google is an additional option,
  not a replacement.
* **Existing account with the same email** — linking behaviour is controlled by
  Supabase's identity settings. If Supabase refuses the link, the user can sign
  in with the original method or choose a different Google account.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Buttons never appear | Google provider disabled, or the page is pointed at another Supabase project | Enable Google in Supabase; confirm `SUPA_URL`/publishable key in `static/index.html` match `lvloaweijidgcuezdnmu`; reload. |
| `redirect_uri_mismatch` from Google | Google redirect URI is the site, not Supabase | Set it to `https://lvloaweijidgcuezdnmu.supabase.co/auth/v1/callback`. |
| OAuth returns to `localhost` or the wrong host | Supabase Site URL / Redirect URLs do not include the origin | Add the exact origin (with trailing `/`) to Redirect URLs and set Site URL for production. |
| “provider is not enabled” | Provider saved but not enabled, or wrong project | Re-check Authentication → Providers → Google; allow a minute for settings to propagate. |
| Onboarding modal reappears | One of phone/role/name/district is missing from metadata, or `updateUser` failed | Check the browser console/network for the Supabase error; complete all fields. |
| “We could not load the latest terms” | Legal manifest fetch failed | Restore connectivity and retry; the modal does not close or save without versions. |
| Google sign-in works but orders/profile are missing | `/api/user/profile` failed during onboarding | Check the API logs and `farmers`; the auth metadata is saved, so the user can retry from the app or support can re-run the profile upsert. |
| Sign-in blocked in the Android app | Google blocks some embedded webviews; the Capacitor shell is not verified for OAuth | Use the system browser/deep-link pattern or a native Google plugin; not implemented in this build. |

---

## 6. Test evidence (repository only)

`tests/test_pesapal_ui.py` (12 static checks, shared with the Pesapal slice):

* exactly one login submit button id (`loginSubmitBtn`) and no stale `loginBtn`;
* both Google buttons exist and start hidden;
* provider gating reads `/auth/v1/settings`;
* OAuth call uses `provider: "google"`, the current origin and
  `prompt=select_account`;
* onboarding contains name, phone, role, district and terms fields and calls
  `saveProfileWithRetry` and the legal signup sync;
* the modal is `modal-locked` and has no overlay dismissal;
* the onboarding check suppresses the generic legal modal while it is open;
* browser assets contain no `service_role`, `sb_secret_`, Supabase service key,
  Pesapal secret, Flutterwave secret or Google client secret.

`tests/e2e/test_consent_ui.py` — 22 Playwright tests now run (Playwright and
Chromium are installed in `.test-deps`). The Google-specific tests:

* `test_google_option_is_offered_on_login_and_registration` — settings JSON,
  button visibility, exact OAuth arguments, redirect URL, no page errors.
* `test_google_onboarding_blocks_until_terms_and_saves_the_profile` — Escape
  does not close the modal; phone validation; terms required; save writes role,
  `+256...` phone, `legal_signup` versions and three legal acceptances; no legal
  review modal; no page errors.

The tests stub Supabase OAuth; **no request reached Google**. Live OAuth is
pending the owner setup in §3.
