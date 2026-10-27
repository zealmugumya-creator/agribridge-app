# AgriBridge — Google Play Release Guide

Status: **preparation guide**. This documents exactly what is in place and what remains before AgriBridge can be submitted to Google Play. Nothing here claims a build or submission has already happened.

> Target per mandate: **Capacitor 7**, **Android API 36 (compile/target SDK)**, keep **appId `com.agribridge.app`**, produce a signed **`.aab`** via a repeatable workflow.

---

## 1. Current state of the mobile shell

The repo already contains a Capacitor wrapper (`mobile/`) that packages the existing web app — it is **not** a rewrite:

| Item | Current value | Mandate target | Action |
|---|---|---|---|
| `appId` | `com.agribridge.app` | same | ✅ keep — do **not** change (changing it creates a new Play listing). |
| Capacitor | `^6.1.2` | 7.x | ⬜ upgrade (see §2). |
| `compileSdk`/`targetSdk` | Capacitor 6 defaults (API 34) | API 36 | ⬜ set after the Capacitor 7 upgrade (see §2). |
| Web source | copies `static/` → `www/` (`scripts/copy-web.mjs`) | same (until React migration) | ✅ works today. |
| Native project (`android/`) | **not committed** | generated | ⬜ `npx cap add android` (see §3). |

The shell currently wraps the vanilla SPA. When the Phase 2 React app lands, only `scripts/copy-web.mjs` needs to point at the React build output (`dist/`) instead of `static/` — the native project and signing stay the same.

---

## 2. Upgrade to Capacitor 7 + Android API 36

Run inside `mobile/`:

```bash
npm install @capacitor/cli@^7 @capacitor/core@^7 @capacitor/android@^7 @capacitor/ios@^7
npm install @capacitor/splash-screen@^7 @capacitor/status-bar@^7
npx cap sync
```

Then set the SDK levels in `mobile/android/variables.gradle` (created by §3):

```gradle
ext {
    minSdkVersion = 23
    targetSdkVersion = 36
    compileSdkVersion = 36
}
```

Capacitor 7 requires **Java 21** and **Android Studio** with the API 36 platform installed. Verify: `npx cap doctor android`.

> Do not fabricate the upgrade: run it on a machine with the Android SDK, then commit the resulting `android/` project (or keep it generated in CI — see §6).

---

## 3. Generate the Android project

```bash
cd mobile
npm run build          # copies web assets into www/
npx cap add android    # first time only; creates android/
npx cap sync android
```

---

## 4. Signing keys (do this once, keep safe)

Play App Signing is recommended: Google holds the app-signing key, you hold an **upload key**.

```bash
# Generate the upload keystore (store the password in a secrets manager, NOT in git).
keytool -genkeypair -v -storetype PKCS12 \
  -keystore agribridge-upload.p12 \
  -alias agribridge -keyalg RSA -keysize 2048 -validity 10000
```

- Enroll in **Play App Signing** in the Play Console (Release → Setup → App signing).
- Keep `agribridge-upload.p12` + its password offline / in CI secrets. Losing the upload key is recoverable via Play; losing the app-signing key (if you self-manage) is not.

---

## 5. Build the signed `.aab`

**Option A — Android Studio:** open `mobile/android/`, then **Build → Generate Signed Bundle / APK → Android App Bundle**, select the upload keystore, choose `release`.

**Option B — Gradle CLI:** create `mobile/android/keystore.properties` (git-ignored):

```
storeFile=/absolute/path/agribridge-upload.p12
storePassword=****
keyAlias=agribridge
keyPassword=****
```

Wire it into `mobile/android/app/build.gradle` and run:

```bash
cd mobile/android && ./gradlew bundleRelease
# output: app/build/outputs/bundle/release/app-release.aab
```

---

## 6. Repeatable release workflow (CI)

A GitHub Actions job should: install deps → `npm run build` → `cap sync` → `gradlew bundleRelease` using secrets (`KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD`) → upload the `.aab` as an artifact (and optionally publish to a Play track with the Google Play Android Publisher action using a service-account JSON secret).

> This workflow is **not yet committed** because it needs the signing secrets and a real Android SDK runner to validate. Adding an untested publish pipeline would violate Rule 3 (no fabricated deployments). Track it as a Track-A next step.

---

## 7. Play Console listing requirements

- **App content → Data safety:** declare data collection. AgriBridge collects account info (name, email, phone), order/transaction data, and device tokens for push. State encryption in transit (HTTPS/TLS) and the deletion path (see §8).
- **Privacy policy URL:** required. Host one (e.g. `https://agribrige.com/privacy.html`) covering collection, payments (Flutterwave), SMS (Africa's Talking), push (Firebase), and deletion.
- **Account deletion (REQUIRED):** Play requires an in-app + web account-deletion path. **Backend is implemented** — `POST /api/account/delete` (see `docs/IMPLEMENTATION_STATUS.md`, migration 0006). Remaining: add the UI button in the app/web settings that calls it, and link the web deletion URL in the Play listing.
- **Content rating:** complete the questionnaire (marketplace + user-generated content).
- **Target API level:** Play requires new apps to target a recent API level — API 36 per the mandate (§2).
- **App access:** provide a demo/test account for reviewers, plus instructions (the app has farmer + buyer roles).
- **Testing track:** upload to **Internal testing** first, then Closed → Open → Production.

---

## 8. Account deletion wiring (Play blocker — H6)

Backend done. To finish:
1. ✅ **Done:** a **"Delete account"** action in the web app settings modal (`static/index.html`,
   danger zone) calls `POST /api/account/delete` with the user's Supabase session token,
   with a double confirmation (confirm + type `DELETE`).
2. The default behaviour **anonymizes** the user's data and preserves the financial ledger (reversible). To make it an irreversible auth-user purge, the operator sets `ACCOUNT_HARD_DELETE_ENABLED=true` **after** a backup and an FK-cascade review (Rule 8).
3. Put the same web deletion URL (`https://agribrige.com` → Settings → Delete my account) in the Play listing's account-deletion field.

---

## 9. Pre-submission checklist

- [ ] Capacitor upgraded to 7; `compileSdk`/`targetSdk` = 36 (verified on a real SDK).
- [ ] `android/` project generated and committed (or reproducibly built in CI).
- [ ] Upload keystore created, backed up, enrolled in Play App Signing.
- [ ] Signed `.aab` builds via `gradlew bundleRelease`.
- [ ] App points at production API (`https://agribridge-1-og7a.onrender.com`) and site (`https://agribrige.com`).
- [x] Account-deletion UI wired to `POST /api/account/delete` (web settings modal); publish the web deletion URL in the Play listing.
- [ ] Privacy policy URL live.
- [ ] Data-safety form completed; content rating done.
- [ ] Smoke test on a physical Android device: signup, list, order, pay (sandbox), track, delete account.
- [ ] Internal-testing upload reviewed, then promote.

---

## 10. What is explicitly NOT done / NOT claimed

- No `.aab` has been built or uploaded from this environment (no Android SDK, no signing keys).
- No Play Console listing, data-safety form, or content rating has been submitted.
- The Capacitor 7 / API 36 upgrade has **not** been executed here — it requires a machine with the Android SDK and Java 21.
- React migration (Phase 2) is not started; the shell currently wraps the existing SPA.
