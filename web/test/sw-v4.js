// Caches the app shell only. API responses always go to the network so the
// timeline and read position are never stale.
// One cache per copy of the app: prod and test live on the same origin and
// share Cache Storage, so each only manages caches for its own scope.
const CACHE = `mastorss-v4 ${self.registration.scope}`;
// What this worker noticed about its fetches, for the page's diagnostics log
// (the page may not exist yet when they happen). Kept across cache versions.
const LOG = `mastorss-swlog ${self.registration.scope}`;
const LOG_MAX = 50;
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './js/app.js',
  './js/api.js',
  './js/render.js',
  './js/profile.js',
  './js/suggestions.js',
  './manifest.webmanifest',
  './icons/icon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];
// How long the shell waits on the network before using the cached copy.
const TIMEOUT_MS = 1500;
const SLOW_MS = 1000; // network fetches slower than this are logged

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((k) => k !== CACHE && k !== LOG && (k === 'mastorss-v1' || k.endsWith(` ${self.registration.scope}`)))
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// One entry per fetch worth knowing about: { t, path, ms, from }. Paths only,
// never the query string (a sign-in return carries its code there). The page
// reads and removes them at start-up (js/app.js).
async function note(entry) {
  try {
    const c = await caches.open(LOG);
    const key = new URL(`sw-log/${Date.now()}-${Math.random().toString(36).slice(2)}`, self.registration.scope);
    await c.put(key, new Response(JSON.stringify({ t: Date.now(), ...entry }), { headers: { 'Content-Type': 'application/json' } }));
    const keys = await c.keys();
    await Promise.all(keys.slice(0, Math.max(0, keys.length - LOG_MAX)).map((k) => c.delete(k)));
  } catch {}
}

// Network first for the shell so deploys show up straight away, but only for
// TIMEOUT_MS: after a cold start on a slow or hung connection the cached copy
// is shown instead, and the network fetch finishes in the background to
// refresh the cache. The shell loads in three rounds (the page, app.js, its
// modules), so once one fetch has timed out the rest of that start comes
// from the cache straight away. With nothing cached yet, wait for the network.
const SKIP_MS = 30_000; // how long after a timeout to go straight to the cache
let skipUntil = 0;

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  // The curator's own endpoints (API, feed, status page) always go to the network.
  if (/^\/(api|f|agent|status|healthz|version\.json)(\/|$)/.test(url.pathname)) return;
  const path = url.pathname;
  const started = Date.now();
  const skipping = started < skipUntil;
  const network = fetch(event.request).then((res) => {
    const ms = Date.now() - started;
    if (ms < SLOW_MS && !skipping) skipUntil = 0; // the network is back
    // Cloned now, before the page starts reading the body.
    const copy = res.ok ? res.clone() : null;
    const saved = copy && caches.open(CACHE).then((c) => c.put(event.request, copy));
    return { res, ms, done: Promise.all([saved, ms > SLOW_MS && note({ path, ms, from: 'network', status: res.status })]) };
  });
  // Keeps the worker alive until the fetch, its cache copy and its log entry are done.
  event.waitUntil(network.then((r) => r.done, () => note({ path, ms: Date.now() - started, from: 'failed' })).catch(() => {}));
  event.respondWith((async () => {
    const first = skipping ? 'skipped' : await Promise.race([
      network.then(() => 'network', () => 'cache'),
      new Promise((resolve) => setTimeout(resolve, TIMEOUT_MS, 'timeout')),
    ]);
    if (first !== 'network') {
      const cached = await caches.match(event.request, { ignoreSearch: true });
      if (cached) {
        if (first === 'timeout') skipUntil = Date.now() + SKIP_MS;
        event.waitUntil(note({ path, ms: Date.now() - started, from: first }));
        return cached;
      }
    }
    return (await network).res; // rejects if the network failed and nothing is cached
  })());
});
