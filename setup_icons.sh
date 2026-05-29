#!/bin/bash
# Run after generating icons to place them in all Android mipmap folders
# Usage: chmod +x setup_icons.sh && ./setup_icons.sh

ANDROID_RES="android/app/src/main/res"

for DIR in mipmap-mdpi mipmap-hdpi mipmap-xhdpi mipmap-xxhdpi mipmap-xxxhdpi; do
  mkdir -p "$ANDROID_RES/$DIR"
  cp icon-192.png "$ANDROID_RES/$DIR/ic_launcher.png"
  cp icon-192.png "$ANDROID_RES/$DIR/ic_launcher_round.png"
  cp icon-192.png "$ANDROID_RES/$DIR/ic_launcher_foreground.png"
  echo "Copied to $DIR"
done
echo "Icons placed in all mipmap folders"
