// 네트워크 우선: 항상 최신 화면을 시도하고, 연결이 안 될 때만 마지막으로 본 화면을 보여준다.
// (화면 하단의 "화면 생성" 시각으로 오래된 화면인지 알 수 있다)
const CACHE = 'kms-v5';

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/static/icon-192.png'])));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // /health(온라인 판별)와 /watchlist/search(자동완성)는 캐시/가로채기 대상에서 제외한다
  if (url.origin !== self.location.origin || url.pathname === '/sw.js' || url.pathname === '/health' || url.pathname === '/watchlist/search' || url.pathname.startsWith('/push/')) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then(
          (hit) =>
            hit ||
            new Response('오프라인입니다. 저장된 화면이 없습니다.', {
              status: 503,
              headers: { 'Content-Type': 'text/plain; charset=utf-8' },
            })
        )
      )
  );
});

// 웹 푸시: 서버가 보낸 제목/본문을 그대로 보여주고, 누르면 해당 화면을 연다.
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: '내손안의주식', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || '내손안의주식', {
    body: d.body || '', tag: d.tag || 'kms', icon: '/static/icon-192.png', badge: '/static/icon-192.png', data: { url: d.url || '/' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) { if ('focus' in c) { c.navigate(url).catch(() => {}); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
