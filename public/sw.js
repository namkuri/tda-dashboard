// TDA Dashboard Service Worker — v40 chunk2
// HTML/SW/Manifest: network-first (always latest)
// Icons/CDN libs: cache-first (rarely change)

const CACHE_NAME = 'tda-v40-002';
const STATIC_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache: Supabase API, OAuth callbacks, Realtime
  if (
    url.hostname.includes('supabase.co') ||
    url.hostname.includes('googleapis.com') ||
    url.hostname.includes('googleusercontent.com') ||
    url.hostname.includes('github.com') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/rest/') ||
    url.pathname.startsWith('/realtime/')
  ) {
    return;
  }

  // CDN libraries: cache-first (heavy, rarely change)
  if (
    url.hostname.includes('cdn.jsdelivr.net') ||
    url.hostname.includes('cdnjs.cloudflare.com') ||
    url.hostname.includes('unpkg.com')
  ) {
    event.respondWith(
      caches.match(event.request).then(
        (cached) => cached || fetch(event.request).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
          return res;
        }).catch(() => caches.match(event.request))
      )
    );
    return;
  }

  // Same origin
  if (url.origin === self.location.origin) {
    // HTML, manifest, sw → network-first (always latest code)
    const isHTML = url.pathname === '/' ||
                   url.pathname.endsWith('/') ||
                   url.pathname.endsWith('.html') ||
                   url.pathname.endsWith('.json') ||
                   url.pathname.endsWith('.js');
    if (isHTML) {
      event.respondWith(
        fetch(event.request)
          .then((res) => {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
            return res;
          })
          .catch(() => caches.match(event.request))  // offline fallback
      );
      return;
    }
    // Other (icons, images): cache-first
    event.respondWith(
      caches.match(event.request).then(
        (cached) => cached || fetch(event.request)
      )
    );
  }
});
