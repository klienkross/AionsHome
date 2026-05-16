// Aion Service Worker — 静态资源缓存 + 强制刷新
const CACHE_NAME = 'aion-v1';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // 不缓存：API、WebSocket、带 nocache 参数的请求
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws') || url.searchParams.has('nocache')) return;

  // 只缓存同源的 GET 请求中的静态资源
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  const cacheable = url.pathname.startsWith('/static/') ||
                    url.pathname.startsWith('/public/') ||
                    url.pathname === '/manifest.json';

  if (!cacheable) return;

  e.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(response => {
          if (response.ok) cache.put(e.request, response.clone());
          return response;
        });
      })
    )
  );
});

self.addEventListener('message', e => {
  if (e.data?.type === 'FORCE_REFRESH') {
    caches.delete(CACHE_NAME).then(() =>
      self.clients.matchAll().then(clients =>
        clients.forEach(c => c.postMessage({ type: 'RELOAD' }))
      )
    );
  }
});
