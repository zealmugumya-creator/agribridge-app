# AgriBridge — Native App (iOS + Android) via Capacitor

This folder turns the **existing AgriBridge web app** (`../static/index.html`) into a
real native app you can submit to the **Apple App Store** and **Google Play** — with
**no rewrite**. You keep editing the website as usual; this just wraps it.

> **You do NOT need this to put AgriBridge on a phone today.** The app already
> installs from the browser: on iPhone open **agribrige.com** in Safari →
> **Share** → **Add to Home Screen**. This folder is only for a real *App Store*
> listing.

---

## What you need first (one-time)

| Requirement | Why | Cost |
|---|---|---|
| A **Mac** (or a cloud-Mac service like MacStadium / a friend's Mac) | Apple only lets you build iOS apps on macOS | — |
| **Xcode** (free, from the Mac App Store) | Builds & uploads the iOS app | Free |
| An **Apple Developer account** | Required to publish to the App Store | **$99 / year** |
| **Node.js 18+** (nodejs.org) | Runs the build tooling | Free |

> Android does **not** need a Mac — you can build the Android app on your Windows
> PC with **Android Studio** (free). See the Android section below.

⚠️ **You cannot build the iOS app on Windows.** That's an Apple rule, not a
limitation of this setup. Everything here is ready so that the moment you're on a
Mac, it's just a few commands.

---

## iOS — step by step (on a Mac)

Open the **Terminal** app and run these one at a time:

```bash
# 1. Get the code (skip if you already cloned the repo)
git clone https://github.com/zealmugumya-creator/agribridge.git
cd agribridge/mobile

# 2. Install the tooling
npm install

# 3. Package the website + create the iOS project
npm run build          # copies ../static into ./www
npx cap add ios        # creates the native iOS project (needs macOS)

# 4. Generate app icons + splash screens from assets/icon.png
npx capacitor-assets generate --assetPath assets

# 5. Open it in Xcode
npx cap open ios
```

In **Xcode**:
1. Select the **App** target → **Signing & Capabilities** → pick your Apple
   Developer **Team** (this is where the $99 account is used).
2. Plug in an iPhone (or pick a Simulator) and press **▶ Run** to test.
3. When happy: **Product → Archive → Distribute App → App Store Connect** to
   upload, then finish the listing at **appstoreconnect.apple.com**.

### After you change the website later
Every time you update `static/index.html`, refresh the app with:
```bash
cd agribridge/mobile
npm run ios            # rebuilds www, syncs, reopens Xcode
```
Then Archive & upload a new version. (Tip: because the app loads live data from
Supabase/Render, content changes show up instantly; you only need to re-submit
when the HTML/JS itself changes.)

---

## Android — step by step (works on Windows too)

Install **Android Studio** (free), then:
```bash
cd agribridge/mobile
npm install
npm run build
npx cap add android

# Apply the correct SDK targets (targetSdkVersion = 36, required for Play Store)
cp scripts/android-variables.gradle android/variables.gradle

npx capacitor-assets generate --assetPath assets
npx cap open android
```

In Android Studio, press **▶ Run** to test on a device or emulator.

### Building the signed `.aab` for Play Store

1. **Build → Generate Signed Bundle / APK** → choose **Android App Bundle (.aab)**
2. Create a new keystore (save it somewhere safe — you can never change it!)
3. Set release build variant → **Finish**
4. Upload the `.aab` to **Google Play Console** (one-time **$25** developer fee)

### Play Store SDK requirements (2025+)
- `targetSdkVersion = 36` (Android 16) — required for new submissions after Aug 2025
- `minSdkVersion = 26` (Android 8.0) — covers ~99.5% of active devices
- These are set in `scripts/android-variables.gradle` which is copied to
  `android/variables.gradle` in the step above

### After you change the website later
```bash
cd agribridge/mobile
npm run android    # rebuilds www, syncs, reopens Android Studio
```

---

## What's in this folder

| File | Purpose |
|---|---|
| `capacitor.config.json` | App name (**AgriBridge**), id (**com.agribridge.app**), dark theme, splash |
| `package.json` | The Capacitor tooling + handy `npm run ios` / `android` scripts |
| `scripts/copy-web.mjs` | Copies `../static` → `./www` so there's one source of truth |
| `assets/icon.png` | Source image for app icons (replace with a **1024×1024** PNG for best quality) |
| `www/`, `ios/`, `android/`, `node_modules/` | Generated locally — not stored in git |

## Good to know / gotchas
- **Icon quality:** `assets/icon.png` is currently the 512×512 app icon. For a
  crisp App Store icon, drop in a **1024×1024** `assets/icon.png` before step 4.
- **Backend CORS:** the app calls Supabase (fine) and your Render backend
  (`agribridge-1-og7a.onrender.com`) for USSD/media. If those calls fail *only*
  inside the native app, add `capacitor://localhost` (iOS) and
  `http://localhost` (Android) to the allowed origins on the Render service.
- **The "Install AgriBridge" banner** in the web app is meant for browsers; it's
  harmless inside the native app but can be hidden later by checking
  `window.Capacitor` in `index.html` if you want.
- **App id** `com.agribridge.app` is permanent once published — change it now in
  `capacitor.config.json` if you'd prefer a different one.
