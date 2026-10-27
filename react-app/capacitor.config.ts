/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.agribridge.app',
  appName: 'AgriBridge',
  webDir: 'dist',
  server: {
    androidScheme: 'https',
  },
  android: {
    minWebViewVersion: 80,
    backgroundColor: '#0f1310',
  },
  plugins: {
    PushNotifications: {
      presentationOptions: ['badge', 'sound', 'alert'],
    },
    SplashScreen: {
      launchShowDuration: 2000,
      backgroundColor: '#0f1310',
      androidSplashResourceName: 'splash',
      showSpinner: false,
    },
  },
};

export default config;
