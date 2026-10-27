# Legal, consent and contact upgrade: what was built

Status: implemented and tested locally. Not applied to the live database or deployed. See "Launch checklist".

## 1. Central business details

- `services/business.py` (backend) and `static/business-config.js` (browser) hold the official name, website, email, phone, WhatsApp, country and currency. `tests/test_business_config.py` fails if they differ.
- Official values: agribrige@gmail.com, +256 755 966 690 (`tel:+256755966690`), WhatsApp `https://wa.me/256755966690`, https://agribrige.com.
- **The phone number in the brief (+2567559566690) has one digit too many for a Ugandan mobile.** The number already used on the site, +256755966690, was kept (confirmed with the owner).
- Replaced: `invest@agribridge.ug`, `hello@agribridge.ug`, `privacy@agribridge.ug`, `orders@agribridge.ug`, a made-up payment fallback address `buyer@agribrige.com`, the invoice's invented bank account (`DFCU ... A/C 01234567`) and local MoMo number, the old named DPO, and contradictory opening hours.
- Not changed: the synthetic login emails `phone_<digits>@agribridge.ug` / `@agribridge.app`. They are internal sign-in identifiers for phone-number logins, never shown as contacts.

## 2. Documents

Source of truth: `legal/source/*.md` (Privacy Policy, Terms of Use, Farmer, Buyer and Supplier terms). `python scripts/build_legal.py` produces `static/legal/*.html`, `*.fragment.html`, immutable versioned pages, and `manifest.json` with SHA-256 hashes. Public URLs: `/legal/privacy-policy.html`, `/legal/terms-of-use.html`, `/legal/farmer-terms.html`, `/legal/buyer-terms.html`, `/legal/supplier-terms.html` (also served by the API under `/legal/...`).

- A published version can never be edited in place: the build fails and tells you to bump the version.
- Placeholder text or unknown tokens fail the build. The renderer escapes all text and only allows http(s)/mailto/tel links.
- Policy text avoids claims the code does not back (no "RLS enabled", no automatic 30-day deletion, no escrow, no quality guarantee, no CADER arbitration).

## 3. Database (migration 0007)

`supabase/migrations/0007_legal_consent.up.sql` / `.down.sql`: `legal_document_versions` (immutable), `legal_publication_log`, `legal_acceptances` (append-only, unique per user/document/version, hash and role recorded), `marketing_consent_events` (append-only, view `marketing_consent_current`), `privacy_requests`, and the RPC `record_legal_acceptance` (checks version and hash; service role only). RLS: users read only their own rows; client roles have no write privilege at all.

Verified on a real PostgreSQL engine (PGlite) by `tests/sql/legal_consent.test.mjs` (31 checks). That test applies 0007 to an empty database with stubbed Supabase roles; it does **not** prove it applies cleanly on top of your real schema.

## 4. API

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/legal/manifest` | none | current versions and hashes |
| `GET /api/legal/status` | session | which documents the user still needs to accept |
| `POST /api/legal/accept` | session | record explicit acceptance (stale version = 409) |
| `POST /api/legal/sync-signup` | session | persist what was ticked at sign-up (see below) |
| `GET /api/legal/history` | session | the user's accepted versions |
| `GET/POST /api/marketing/consent` | session | SMS promotions on/off |
| `GET/POST /api/privacy/requests` | POST works without a session | access, correction, deletion, withdrawal, complaint |

The role comes from the verified Supabase session, never from the request body. Role-to-documents mapping: farmer -> Farmer Terms; vendor and hotel -> Buyer Terms; supplier -> Supplier Terms; everyone -> Terms of Use and Privacy Policy. There is no public admin or staff registration or agreement.

`ENFORCE_LEGAL_ACCEPTANCE=true` makes `POST /api/orders`, `POST /api/requests` and offer acceptance return 403 until the user accepted the current terms. It is **off by default** (see checklist).

Account deletion now refuses to anonymise while the user has pending, confirmed or in-transit orders. It files a deletion privacy request for manual review and tells the user.

## 5. Web app

- **Sign-up:** required, unchecked, keyboard-operable checkbox with the exact wording from the brief, separate links (new tab, so the form is preserved) to Terms and Privacy, and a role-specific terms link with version that follows the selected account type. A separate unchecked optional promotions checkbox; the only channel offered is SMS, because no marketing email sender or WhatsApp Business API exists.
- **Recording:** sign-up may return no session (email confirmation), so the ticked versions travel in the account metadata and are written by `sync-signup` on the first signed-in call, only if the versions are still current and the account is under 30 days old. Method is stored as `signup_checkbox_deferred`.
- **Login / session restore / role change:** one check per user per page load. Missing documents open a review screen that cannot be dismissed with Esc or a click outside; it offers sign out, a privacy request and support contact. Acceptance shows as saved only after the API confirms; a failed save shows an error and a retry.
- **Settings:** a Privacy and legal section with current document versions, accepted history, an SMS promotions toggle, access / correction / complaint requests and contact links.
- **Footer and contact page:** links to Terms, Privacy and privacy requests (they did not exist before).
- Refund modal no longer promises "quality guaranteed, refund within 48 hours". Cookie banner no longer claims legal compliance.

## 6. Tests (run locally)

See `docs/IMPLEMENTATION_STATUS.md` for the commands and results.

## 7. Known limitations and risks

1. **Sign-up can be bypassed by calling Supabase directly.** The checkbox is enforced in the web app, but accounts are created in the browser through Supabase. Anyone calling the Supabase API directly gets an account with no acceptance record. The server-side gate (`ENFORCE_LEGAL_ACCEPTANCE`) is the real control; today the web app still writes orders and listings to Supabase tables from the browser, so the gate only covers the new server-side order routes. Closing that fully is part of the React/server-API migration.
2. Role is read from `user_metadata.role`, which the user can change (the app lets them in Settings; each change re-triggers the role's terms). It decides which terms apply; it is not an authorisation control.
3. Marketing consent is recorded and withdrawable, but **no promotional SMS is sent by AgriBridge today**, so nothing consumes the opt-in yet. Whoever builds that sender must read `marketing_consent_current`.
4. Privacy requests are stored for a person to handle. There is no admin screen for them yet; they are visible in the `privacy_requests` table. There is no automatic data export.
5. Emailed privacy requests are unverified until you confirm the sender's identity by reply.
6. Native apps: the same web build is packaged by Capacitor (`mobile/scripts/copy-web.mjs` copies `static/`, which now includes `legal/` and `business-config.js`). Not built or run on Android here.
7. The English documents are the only language. Luganda, Runyankole and Acholi versions are not provided.
8. No physical-device, screen-reader or Play Console checks were done. Checkbox labelling and keyboard operation were tested in headless Chromium only.
