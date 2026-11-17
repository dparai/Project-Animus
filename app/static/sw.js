/* Animus service worker.

   It keeps a copy of the public design files (/static/*: styles, scripts, fonts, icons) on the
   device so the app opens fast, and shows a plain "offline" page when there is no connection.

   It never stores a journal page, a fragment or any other response from outside /static/, and it
   never answers such a request from a cache. Requests that are not GET are left untouched.

   It also shows the daily reminder. The push that wakes it carries no text at all: the line
   below is fixed, so nothing about the journal ever travels through a push service. */
const CACHE = 'animus-static-v1';
const OFFLINE = '/static/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('push', (event) => {
  event.waitUntil(self.registration.showNotification('Animus', {
    body: 'A quiet minute to write about today?',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    tag: 'animus-daily',
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const client of list) {
      if ('focus' in client) { return client.focus(); }
    }
    return self.clients.openWindow('/');
  }));
});

function isStatic(url) {
  return url.origin === self.location.origin && url.pathname.startsWith('/static/') && url.pathname !== '/static/sw.js';
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') { return; }
  const url = new URL(request.url);

  if (isStatic(url)) {
    // Design files: answer from the device at once, refresh the copy in the background.
    event.respondWith(caches.open(CACHE).then((cache) => cache.match(request).then((hit) => {
      const fresh = fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') { cache.put(request, response.clone()); }
        return response;
      });
      if (hit) { fresh.catch(() => {}); return hit; }
      return fresh;
    })));
    return;
  }

  if (request.mode === 'navigate') {
    // Pages always come from the server. Without a connection, show the offline page.
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
  }
});
