# AgriBridge Android App

Capacitor wrapper for AgriBridge Uganda — Uganda's #1 farm-to-table platform.

## Quick Start — GitHub Actions (Recommended)

1. Push this folder to a new GitHub repo: `agribridge-app`
2. Go to **Actions** tab → **Build AgriBridge APK** → **Run workflow**
3. Wait ~10 minutes
4. Download `agribridge-debug-apk` from the Artifacts section
5. Install on any Android 5.1+ phone

## What's inside

- Full Capacitor 6 Android project
- Points to: https://agribridge-1-og7a.onrender.com
- App ID: `ug.agribridge.app`
- www/index.html: Latest AgriBridge v7.1 (371KB, 19 pages, 32 features)

## Features in this build

- 19 pages: Marketplace, Animals, Finance Hub, B2B Portal, Cold Chain, Export Tools, USSD
- Role-based UI: Guest / Farmer / Buyer / Hotel views
- Matooke Green design palette (spec compliant)
- Offline fallback via Service Worker
- MTN MoMo + Airtel Money payments
- AI Crop Doctor + Animal Doctor
- Supabase authentication
- 16 modals, full B2B invoice system

## Permissions requested

- INTERNET, NETWORK_STATE — for platform access
- CAMERA — for product photos
- RECORD_AUDIO — for voice input to AI
- LOCATION — for delivery GPS
- RECEIVE_SMS — for MoMo confirmation codes
- VIBRATE — for payment notifications

## Push to GitHub (5 commands)

```bash
cd agribridge-app          # or wherever you extracted the ZIP
git init
git add .
git commit -m "AgriBridge v7.1 - role-based UI, B2B portal, full feature build"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/agribridge-app.git
git push -u origin main
```

Replace YOUR_USERNAME with your GitHub username (zealmugumya-creator).

## Contact

Zeal Mugumya — zealmugumya@gmail.com — +256 755 966 690
