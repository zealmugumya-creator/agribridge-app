#!/usr/bin/env bash
# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

# Build a signed Android App Bundle for AgriBridge.
# Run from react-app/ directory with Android Studio + JDK 17 installed.
# Usage: ./scripts/build-android.sh

set -euo pipefail

echo "==> Checking environment…"
command -v java >/dev/null 2>&1 || { echo "JDK 17+ required"; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "npx required"; exit 1; }

echo "==> Building React app…"
npm run build

echo "==> Syncing Capacitor…"
npx cap sync android

echo "==> Copying android-variables.gradle…"
cp scripts/android-variables.gradle android/variables.gradle

echo "==> Building signed AAB via Gradle…"
cd android
./gradlew bundleRelease

echo ""
echo "✅ AAB built at: android/app/build/outputs/bundle/release/app-release.aab"
echo ""
echo "Next steps:"
echo "  1. Sign with jarsigner / apksigner using your upload keystore"
echo "  2. Upload signed AAB to Google Play Console > Internal Testing"
