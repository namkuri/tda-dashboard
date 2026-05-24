// TDA Dashboard Service Worker
// 정적 자산만 캐시, Supabase API는 캐시 X (실시간 데이터)

const CACHE_NAME = 'tda-v40-001';
const STATIC_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

// 설치 시 정적 자산 캐시
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())  // 새 SW 즉시 활성화
  );
});

// 활성화 시 옛 캐시 청소
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// 요청 처리: cache-first for static, network-only for APIs
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Supabase API, OAuth callback, Realtime → 절대 캐시 X
  if (
    url.hostname.includes('supabase.co') ||
    url.hostname.includes('googleapis.com') ||
    url.hostname.includes('googleusercontent.com') ||
    url.hostname.includes('github.com') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/rest/') ||
    url.pathname.startsWith('/realtime/')
  ) {
    return;  // 기본 네트워크 동작
  }

  // CDN 라이브러리도 캐시
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

  // 같은 origin 정적 자산: cache-first
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then(
        (cached) => cached || fetch(event.request)
      )
    );
  }
});
