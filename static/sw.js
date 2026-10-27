/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// AgriBridge Uganda — Service Worker (push notifications only)
//
// Deliberately has NO fetch handler and caches NOTHING: earlier caching versions
// served stale code after deploys. This worker exists so Android can show
// notifications in the system notification bar, even when the site is closed.

const SW_VERSION = 'push-v2';

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  // Remove any caches left behind by older caching versions of this worker.
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── PUSH: show a system notification ──
self.addEventListener('push', event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || 'AgriBridge';
  const options = {
    body: payload.body || 'You have a new notification',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: payload.tag || 'agribridge',
    renotify: true,
    data: { url: payload.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// ── TAP: focus an open tab or open the app ──
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const client of list) {
        if (client.url.startsWith(self.location.origin) && 'focus' in client) {
          return client.focus().then(focused => {
            // The page is already open: bring it forward and open the bell panel
            // instead of doing a full navigation that would lose app state.
            (focused || client).postMessage({ type: 'OPEN_NOTIFICATIONS' });
            return focused;
          });
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'GET_VERSION' && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ version: SW_VERSION });
  }
});

// ── PUSH: Show OS notification when a Web Push arrives ──
self.addEventListener('push', event => {
  let data = { title: 'AgriBridge', body: 'You have a new notification', icon: '/icon-192.png', badge: '/icon-96.png' };
  try {
    if (event.data) {
      const parsed = event.data.json();
      data = { ...data, ...parsed };
    }
  } catch (e) {
    if (event.data) data.body = event.data.text() || data.body;
  }

  const options = {
    body: data.body,
    icon: data.icon || '/icon-192.png',
    badge: data.badge || '/icon-96.png',
    tag: data.tag || 'agribridge-notif',
    renotify: true,
    data: { url: data.url || '/' },
    vibrate: [200, 100, 200],
    requireInteraction: false,
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

// ── NOTIFICATIONCLICK: Open the app when the user taps the notification ──
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.postMessage({ type: 'PUSH_NAV', url: target });
          return client.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});
