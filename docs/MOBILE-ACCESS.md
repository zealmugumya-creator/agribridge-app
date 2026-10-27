# AgriBridge Mobile App — Access & Build Guide

## How to access the React app right now (before building the APK)

### Option 1 — Run locally on your PC and open in a mobile browser (fastest, zero setup)

```bash
cd C:\Users\HP\Documents\agribridge\react-app
npm install
npm run dev
```

Then on your Android phone:
1. Make sure your phone and PC are on the same Wi-Fi
2. Find your PC's local IP: run `ipconfig` in Command Prompt → look for "IPv4 Address" (e.g. 192.168.1.5)
3. Open Chrome on your phone and go to: `http://192.168.1.5:5173`

That's the full app — login, marketplace, cart, everything. Not a native APK yet but it works.

---

### Option 2 — Deploy to Vercel/Netlify for a public URL (share with anyone)

```bash
cd react-app
npm run build        # creates dist/ folder
```

Then drag-and-drop the `dist/` folder to https://app.netlify.com/drop  
You get a live URL like `https://agribridge-xyz.netlify.app` immediately — no account needed.

To make it permanent with your own domain, sign up at netlify.com and connect your GitHub repo.

**Environment variables to set in Netlify dashboard:**
```
VITE_SUPABASE_URL    = https://vyrctsiyaihsysgpozdm.supabase.co
VITE_SUPABASE_ANON_KEY = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
VITE_API_URL         = https://agribridge-1-og7a.onrender.com
```

---

### Option 3 — Native Android APK (for Google Play)

Requirements: Android Studio installed on your PC.

```bash
cd react-app

# First time only — add the Android platform:
npm run cap:add-android

# Every time you change code:
npm run android
```

This opens Android Studio. Then:
1. Wait for Gradle sync to finish
2. Click **Build → Generate Signed Bundle/APK**
3. Choose **Android App Bundle (.aab)**
4. Use your keystore (create one if you don't have it: Build → Generate Signed Bundle → Create new)
5. Upload the `.aab` to Google Play Console → Internal Testing

---

### Option 4 — iOS (requires a Mac + Xcode + Apple Developer account $99/yr)

The app **does** support iOS — `@capacitor/ios` is installed.  
You need a Mac to build iOS apps. On your Mac:

```bash
git clone https://github.com/zealmugumya-creator/agribridge
cd agribridge/react-app
npm install
npm run cap:add-ios    # adds ios/ platform folder
npm run ios            # opens Xcode
```

In Xcode:
1. Select your development team (Apple ID)
2. Set Bundle ID: `com.agribridge.app`
3. Connect iPhone via USB → select it as the build target
4. Press Play ▶ — app installs directly on your phone

For App Store: Archive → Distribute → App Store Connect.

---

## Push notifications

- **Android**: Works automatically via Firebase Cloud Messaging (FCM)
  - You need a `google-services.json` from Firebase Console → place it in `android/app/`
  - In Firebase: Project Settings → Cloud Messaging → get your Server Key
  - Set environment variable on Render: `FCM_CREDENTIALS_JSON` = your service account JSON

- **iOS**: Works via APNs (Apple Push Notification service)
  - Requires Apple Developer account
  - In Firebase, upload your APNs Auth Key
  - Add `NSRemoteNotificationUsageDescription` to `ios/App/App/Info.plist`
