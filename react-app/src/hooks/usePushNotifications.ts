/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useEffect } from 'react';
import { Capacitor } from '@capacitor/core';
import { PushNotifications } from '@capacitor/push-notifications';
import { supabase } from '../lib/supabase';

export function usePushNotifications(userId: string | null) {
  useEffect(() => {
    if (!userId || !Capacitor.isNativePlatform()) return;

    const platform = Capacitor.getPlatform() as 'android' | 'ios';

    const register = async () => {
      let permResult = await PushNotifications.checkPermissions();
      if (permResult.receive === 'prompt') {
        permResult = await PushNotifications.requestPermissions();
      }
      if (permResult.receive !== 'granted') return;
      await PushNotifications.register();
    };

    // Save FCM/APNS token to Supabase push_tokens table
    const tokenHandlerPromise = PushNotifications.addListener('registration', async (token) => {
      await supabase
        .from('push_tokens')
        .upsert(
          { user_id: userId, token: token.value, platform },
          { onConflict: 'user_id,token' }
        );
    });

    const errorHandlerPromise = PushNotifications.addListener('registrationError', (err) => {
      console.error('Push registration error:', err);
    });

    register();

    return () => {
      tokenHandlerPromise.then(h => h.remove());
      errorHandlerPromise.then(h => h.remove());
    };
  }, [userId]);
}
