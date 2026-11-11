// JAGUAR phone app service worker: every file the app needs is cached at install, so it starts
// and runs with no network. One cache per version (`build.mjs` fills in VERSION and FILES); a new
// version installs next to the old one and waits until the page says to switch (shim.js offers
// "Reload" and never while a run is going).

const VERSION = '__VERSION__';
const FILES = __FILES__;
const CACHE = `jaguar-${VERSION}`;

self.addEventListener('install', ev => {
  ev.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES.map(f => new Request(f, { cache: 'reload' })))));
});

self.addEventListener('activate', ev => {
  ev.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('jaguar-') && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener('message', ev => {
  if (ev.data === 'skipWaiting') self.skipWaiting();
});

self.addEventListener('fetch', ev => {
  const req = ev.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  ev.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = req.mode === 'navigate'
      ? await cache.match(new URL('./', self.registration.scope).href) ?? await cache.match('index.html')
      : await cache.match(req, { ignoreSearch: true });
    return hit ?? fetch(req);
  })());
});
