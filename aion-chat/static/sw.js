// Aion Service Worker — 静态资源缓存 + 强制刷新
const CACHE_NAME = 'aion-v2';

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

  // stale-while-revalidate: 缓存秒开 + 后台静默更新
  e.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(e.request).then(cached => {
        const fetchPromise = fetch(e.request).then(response => {
          if (response.ok && response.status !== 206) cache.put(e.request, response.clone());
          return response;
        });
        // 有缓存就直接返回，同时后台更新
        if (cached) {
          fetchPromise.catch(() => {}); // 后台更新失败不报错
          return cached;
        }
        return fetchPromise;
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
