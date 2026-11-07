// The reader's only backend is the curator it's served from. You sign in with
// the curator's OIDC provider (authorization code + PKCE, public client: no
// secret in the app; /config.json says which), and every API call carries
// the access token. The
// refresh token rotates on every use, so refreshing is single-flight: two
// refreshes racing would spend the same token twice and end the session.
//
// No request waits indefinitely: a phone that slept can come back to a
// kept-alive connection that has silently died, and a fetch on it hangs
// until the OS gives up, minutes later. Every fetch here has a timeout; a
// read (and the position, which the server never moves back) is sent again
// once if it times out or fails on the way. Other writes are not: they
// might have reached the server, and a failed vote is easy to repeat. Nor
// is the refresh token: one that reached authentik is spent, whether or not
// its answer got back.

const SCOPE = 'openid profile offline_access';
const SESSION_KEY = 'reader.session';
const PENDING_KEY = 'reader.pending-login';
const SKEW_S = 30; // refresh this long before the access token expires
const READ_TIMEOUT_MS = 10_000; // reads, sign-in discovery, config.json
// Writes and the token refresh get longer: giving up on one that is merely
// slow can lose its effect (or spend the refresh token) for nothing.
const WRITE_TIMEOUT_MS = 20_000;
const SLOW_MS = 1000; // an API call slower than this is always logged

// `window.__readerTimeoutMs` shortens every timeout (for the tests).
const timeoutMs = (write) => globalThis.__readerTimeoutMs ?? (write ? WRITE_TIMEOUT_MS : READ_TIMEOUT_MS);

// Where api.js reports what it did (the app's diagnostics log); nothing until set.
let logger = () => {};
export function setLogger(fn) {
  logger = fn;
}

const timeoutError = (ms) => Object.assign(new Error(`Couldn't reach the server (no answer in ${Math.round(ms / 100) / 10} s)`), { timeout: true });

// fetch(), given up on after `ms`, reading the answer with `read(res)` inside
// the same time limit (a dead connection can stall the body too). A timeout
// throws an error with `timeout: true`; a failure on the way, `network: true`.
export async function timedFetch(url, opts, ms, read) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    let res;
    try {
      res = await fetch(url, { ...opts, signal: ctl.signal });
    } catch (err) {
      throw ctl.signal.aborted ? timeoutError(ms) : Object.assign(err, { network: true });
    }
    try {
      return await read(res);
    } catch (err) {
      if (ctl.signal.aborted) throw timeoutError(ms);
      if (err instanceof TypeError) err.network = true;
      throw err;
    }
  } finally {
    clearTimeout(timer);
  }
}

export const redirectUri = () => `${location.origin}/`;

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const randomString = (n = 32) => b64url(crypto.getRandomValues(new Uint8Array(n)));

async function pkceChallenge(verifier) {
  return b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
}

// { authority, client_id, sign_in } from the server.
let config = null;
export async function clientConfig() {
  config ??= timedFetch('config.json', { cache: 'no-store' }, timeoutMs(false), (r) => r.json()).catch((err) => {
    config = null;
    throw err;
  });
  return config;
}

let discovery = null;
async function endpoints() {
  discovery ??= clientConfig().then((c) => timedFetch(`${c.authority.replace(/\/?$/, '/')}.well-known/openid-configuration`, {}, timeoutMs(false), (r) => {
    if (!r.ok) throw new Error(`Sign-in service unavailable (${r.status})`);
    return r.json();
  })).catch((err) => {
    discovery = null;
    throw err;
  });
  return discovery;
}

const load = (key) => {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
};
const save = (key, value) => {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {}
};

export const signedIn = () => !!load(SESSION_KEY)?.refresh;

// Goes to authentik to sign in; comes back to `redirectUri()` with a code.
export async function beginLogin() {
  const { authorization_endpoint } = await endpoints();
  const verifier = randomString(48);
  const state = randomString(16);
  save(PENDING_KEY, { verifier, state });
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: (await clientConfig()).client_id,
    redirect_uri: redirectUri(),
    scope: SCOPE,
    state,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: 'S256',
  });
  location.assign(`${authorization_endpoint}?${params}`);
}

function keep(tokens) {
  save(SESSION_KEY, {
    access: tokens.access_token,
    refresh: tokens.refresh_token,
    expires: Date.now() + (tokens.expires_in || 300) * 1000,
  });
}

async function tokenRequest(form) {
  const { token_endpoint } = await endpoints();
  const client_id = (await clientConfig()).client_id;
  return timedFetch(token_endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id, ...form }),
  }, timeoutMs(true), (res) => {
    if (!res.ok) throw Object.assign(new Error(`Sign-in failed (${res.status})`), { status: res.status });
    return res.json();
  });
}

// Finishes the sign-in after authentik redirects back with ?code=…&state=….
export async function finishLogin(code, state) {
  const pending = load(PENDING_KEY);
  if (!pending) throw new Error('No sign-in in progress');
  if (state !== pending.state) throw new Error('Sign-in state mismatch, please try again');
  keep(await tokenRequest({ grant_type: 'authorization_code', code, redirect_uri: redirectUri(), code_verifier: pending.verifier }));
  save(PENDING_KEY, null);
}

let refreshing = null;

// Trades the refresh token for new tokens. Tabs share one session, so the
// trade is done under a lock across tabs, and re-reads the session inside it:
// if another tab has just done it, its tokens are used, not the spent one.
// A timeout counts as a blip: it is not tried again here (the next request
// does), since the token may have been spent on the way.
async function refreshSession(force) {
  const t0 = performance.now();
  const run = async () => {
    const s = load(SESSION_KEY);
    if (!s) return null;
    if (!force && s.access && s.expires - SKEW_S * 1000 > Date.now()) return s.access;
    const lockMs = Math.round(performance.now() - t0);
    const done = (outcome) => logger('token-refresh', { ms: Math.round(performance.now() - t0), lockMs, force, outcome });
    try {
      const t = await tokenRequest({ grant_type: 'refresh_token', refresh_token: s.refresh });
      keep({ ...t, refresh_token: t.refresh_token || s.refresh });
      done('ok');
      return t.access_token;
    } catch (err) {
      done(err.timeout ? 'timeout' : err.network ? 'network' : err.status ?? err.message);
      // A dead refresh token means signing in again, unless it was replaced
      // meanwhile; a network blip doesn't.
      const now = load(SESSION_KEY);
      if ((err.status === 400 || err.status === 401) && now?.refresh === s.refresh) save(SESSION_KEY, null);
      else if (now?.refresh && now.refresh !== s.refresh && now.expires - SKEW_S * 1000 > Date.now()) return now.access;
      throw err;
    }
  };
  return navigator.locks ? navigator.locks.request('reader.refresh', run) : run();
}

// A current access token, refreshing it if needed. Null means signed out.
export async function accessToken({ force = false } = {}) {
  const s = load(SESSION_KEY);
  if (!s) return null;
  if (!force && s.access && s.expires - SKEW_S * 1000 > Date.now()) return s.access;
  refreshing ??= refreshSession(force).finally(() => (refreshing = null));
  return refreshing;
}

// Resolves once no token refresh is in flight. Reloading mid-refresh would
// lose the new refresh token (the old one is already spent) and sign you out.
export function refreshSettled() {
  return refreshing ? refreshing.then(() => {}, () => {}) : Promise.resolve();
}

export function signOut() {
  save(SESSION_KEY, null);
}

export class Api {
  // `keepalive` lets a request finish while the page is being hidden.
  // `idempotent` requests are sent again once after a timeout or a network
  // failure (see the top of this file). Each call goes in the diagnostics
  // log with its time from here, and the part of it spent getting a token
  // (tokenMs); `quiet` ones (frequent, with a log line of their own) only
  // when slow or failed. No tokens, bodies or query strings.
  async request(path, { method = 'GET', body, keepalive = false, idempotent = method === 'GET', quiet = false } = {}) {
    const t0 = performance.now();
    const entry = { method, path: path.split('?')[0] };
    let tokenMs = 0;
    // Only a session that is gone means signing in again; a refresh that
    // timed out or failed on the way is that error, and the next call tries again.
    const token = async (force) => {
      const t = performance.now();
      let access = null;
      try {
        access = await accessToken({ force });
      } catch (err) {
        if (signedIn()) throw Object.assign(err, { during: 'token' });
      } finally {
        tokenMs += performance.now() - t;
      }
      if (!access) throw Object.assign(new Error(force ? 'Please sign in again' : 'Please sign in'), { unauthorised: true });
      return access;
    };
    const send = async (access) => {
      const attempt = () => timedFetch(path, {
        method,
        keepalive,
        headers: { Authorization: `Bearer ${access}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        cache: 'no-store',
      }, timeoutMs(method !== 'GET'), async (res) => {
        if (res.status === 401) return res;
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw Object.assign(new Error(detail.error || `The server said ${res.status}`), { status: res.status, detail });
        }
        return Object.assign(res, { data: await res.json() });
      });
      try {
        return await attempt();
      } catch (err) {
        if (!idempotent || !(err.timeout || err.network)) throw err;
        entry.retry = err.timeout ? 'timeout' : 'network';
        return attempt();
      }
    };
    try {
      let res = await send(await token(false));
      // A token the server no longer takes: refresh it and try once more.
      if (res.status === 401) res = await send(await token(true));
      entry.status = res.status;
      if (res.status === 401) throw Object.assign(new Error('Please sign in again'), { unauthorised: true });
      return res.data;
    } catch (err) {
      entry.status ??= err.timeout ? 'timeout' : err.network ? 'network' : err.unauthorised ? 'signed-out' : err.status ?? 'error';
      if (err.during) entry.during = err.during;
      throw err;
    } finally {
      const ms = Math.round(performance.now() - t0);
      if (!quiet || ms >= SLOW_MS || entry.retry || entry.status !== 200) logger('api', { ...entry, ms, tokenMs: Math.round(tokenMs) });
    }
  }

  me() {
    return this.request('/api/me');
  }

  // Timeline entries after `id` (or the latest when null), oldest first:
  // { items, cursor, end }.
  after(id, limit = 40) {
    return this.request(`/api/timeline?${new URLSearchParams({ limit, ...(id ? { after: id } : {}) })}`);
  }

  before(id, limit = 20) {
    return this.request(`/api/timeline?${new URLSearchParams({ limit, before: id })}`);
  }

  thread(statusId) {
    return this.request(`/api/thread/${encodeURIComponent(statusId)}`);
  }

  // Moves your position on (the server never moves it back). Returns where it is.
  async savePosition(position, { keepalive = false } = {}) {
    return (await this.request('/api/position', { method: 'PUT', body: { position }, keepalive, idempotent: true, quiet: true })).position;
  }

  async markAllRead() {
    return (await this.request('/api/position', { method: 'PUT', body: { latest: true }, idempotent: true })).position;
  }

  // `target` is { item_id } for an article or { status_id } for a post.
  vote(target, vote) {
    return this.request('/api/vote', { method: 'POST', body: { ...target, vote } });
  }

  bookmark(target, on) {
    return this.request('/api/bookmark', { method: 'POST', body: { ...target, on } });
  }

  bookmarks() {
    return this.request('/api/bookmarks');
  }

  // Articles matching `q`, best and most recent first; `everything` includes
  // what the curator passed on.
  search(q, everything = false) {
    return this.request(`/api/search?${new URLSearchParams({ q, ...(everything ? { everything: '1' } : {}) })}`);
  }

  onboard(answers) {
    return this.request('/api/onboarding', { method: 'POST', body: answers });
  }

  // How the curator sees you: { current, versions }.
  profile() {
    return this.request('/api/profile');
  }

  profileVersion(n) {
    return this.request(`/api/profile/${n}`);
  }

  saveProfile(docs) {
    return this.request('/api/profile', { method: 'PUT', body: docs });
  }

  setDecider(decider) {
    return this.request('/api/settings', { method: 'PUT', body: { decider } });
  }

  // Where to send you to approve the curator on your Mastodon server.
  async connectMastodon(server) {
    return (await this.request('/api/mastodon', { method: 'POST', body: { server } })).url;
  }

  disconnectMastodon() {
    return this.request('/api/mastodon', { method: 'DELETE' });
  }

  async newAgentToken() {
    return (await this.request('/api/agent-token', { method: 'POST' })).token;
  }

  // The weekly scout's suggestions: { open, trying, history }.
  suggestions() {
    return this.request('/api/suggestions');
  }

  // add | try | dismiss, or keep | drop after a trial.
  decide(id, action) {
    return this.request(`/api/suggestions/${id}`, { method: 'POST', body: { action } });
  }

  readers() {
    return this.request('/api/admin/users');
  }

  // The diagnostics text, into the server's journal for the majordomo: { id, time }.
  sendDiagnostics(text) {
    return this.request('/api/diagnostics', { method: 'POST', body: { text } });
  }

  event(target, kind) {
    return this.request('/api/event', { method: 'POST', body: { ...target, kind }, keepalive: true, quiet: true }).catch(() => {});
  }
}

// Timeline ids are numeric strings that may exceed 2^53, so compare as strings.
export function compareIds(a, b) {
  if (a === b) return 0;
  if (a == null) return -1;
  if (b == null) return 1;
  if (a.length !== b.length) return a.length < b.length ? -1 : 1;
  return a < b ? -1 : 1;
}

// The id just above `id`, so `before` (which is exclusive) includes `id` itself.
export function nextId(id) {
  return (BigInt(id) + 1n).toString();
}
