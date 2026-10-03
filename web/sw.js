// Caches the app shell only. API responses always go to the network so the
// timeline and read position are never stale.
// One cache per copy of the app: prod and test live on the same origin and
// share Cache Storage, so each only manages caches for its own scope.
const CACHE = `mastorss-v3 ${self.registration.scope}`;
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './js/app.js',
  './js/api.js',
  './js/render.js',
  './js/profile.js',
  './manifest.webmanifest',
  './icons/icon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((k) => k !== CACHE && (k === 'mastorss-v1' || k.endsWith(` ${self.registration.scope}`)))
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Network first for the shell so deploys show up straight away, cache as the
// offline fallback.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  // The curator's own endpoints (API, feed, status page) always go to the network.
  if (/^\/(api|f|agent|status|healthz|version\.json)(\/|$)/.test(url.pathname)) return;
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request, { ignoreSearch: true })),
  );
});
