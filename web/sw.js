// The curator serves this file with two lines in front of it
// (curator/shell.py): VERSION, the shell's version id, and FILES, every shell
// file under its hashed name (`js/app.<hash>.js`). So each new shell is a new
// worker, which caches that version's files as one, and a page only ever
// gets files of its own version (#20).
// API responses always go to the network so the timeline and read position
// are never stale.
// One cache per copy of the app: prod and test live on the same origin and
// share Cache Storage, so each only manages caches for its own scope.
const CACHE = `mastorss-${VERSION} ${self.registration.scope}`;
// What this worker noticed about its fetches, for the page's diagnostics log
// (the page may not exist yet when they happen). Kept across versions.
const LOG = `mastorss-swlog ${self.registration.scope}`;
const LOG_MAX = 50;
const PAGE = self.registration.scope; // index.html, as installed with this version
const HASHED = new Set(FILES.map((f) => new URL(f, self.registration.scope).href));
// How long a start waits on the network for the page before using the cached copy.
const TIMEOUT_MS = 1500;
const SLOW_MS = 1000; // network fetches slower than this are logged

// All of this version's files or none: addAll fails if any one does, and the
// worker before keeps serving its own version, whole. `reload` so no copy
// from the HTTP cache is taken for the server's.
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll([PAGE, ...HASHED].map((url) => new Request(url, { cache: 'reload' }))))
    .catch((err) => caches.delete(CACHE).then(() => { throw err; }))
    .then(() => self.skipWaiting()));
});

// Every other version of this scope goes, the caches from before hashing
// (mastorss-v1 to v4) among them.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((k) => k !== CACHE && k !== LOG && (k === 'mastorss-v1' || (k.startsWith('mastorss-') && k.endsWith(` ${self.registration.scope}`))))
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

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (event.request.mode === 'navigate') {
    const { res, done } = page(event);
    event.respondWith(res);
    event.waitUntil(done); // its log entries
  }
  else if (HASHED.has(url.origin + url.pathname)) event.respondWith(hashed(event));
  // Anything else (the API, config.json, a newer page's files) is the network's.
});

// A hashed file never changes, so this version's copy is always right. A miss
// (only if the cache was cleared under us) goes to the network.
async function hashed(event) {
  const cached = await caches.match(event.request, { cacheName: CACHE });
  if (cached) return cached;
  event.waitUntil(note({ path: new URL(event.request.url).pathname, ms: 0, from: 'missing' }));
  return fetch(event.request);
}

// The page names the files, so it comes from the network first (never from
// the HTTP cache), but only for TIMEOUT_MS: after that, on a slow or hung
// connection, this version's copy is shown instead. The cached copy is the
// one installed with this version's files, never a newer one from the
// network whose files this cache doesn't have.
function page(event) {
  const path = new URL(event.request.url).pathname;
  const started = Date.now();
  const network = fetch(event.request, { cache: 'no-cache' });
  const done = network.then(
    (r) => Date.now() - started > SLOW_MS && note({ path, ms: Date.now() - started, from: 'network', status: r.status }),
    () => note({ path, ms: Date.now() - started, from: 'failed' }),
  );
  const res = (async () => {
    const first = await Promise.race([
      network.then(() => 'network', () => 'cache'),
      new Promise((resolve) => setTimeout(resolve, TIMEOUT_MS, 'timeout')),
    ]);
    if (first !== 'network') {
      const cached = await caches.match(PAGE, { cacheName: CACHE });
      if (cached) {
        await note({ path, ms: Date.now() - started, from: first });
        return cached;
      }
    }
    return network; // rejects if the network failed and nothing is cached
  })();
  return { res, done: Promise.all([done, res.catch(() => {})]) };
}
