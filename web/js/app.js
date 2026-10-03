import { Api, accessToken, beginLogin, clientConfig, compareIds, finishLogin, nextId, signOut, signedIn } from './api.js';
import { runOnboarding, showProfile } from './profile.js';
import { el, renderCurated, renderStatus, safariUrl } from './render.js';
import { showSuggestions } from './suggestions.js';

// The reader: your Mastodon home timeline, filtered by the curator and merged
// with the articles it picked, read oldest to newest from where you left off.
// Everything comes from the curator this page is served by; the app never
// talks to Mastodon. Replying, boosting and favouriting happen on your
// server's own site (↗ See original).

const $ = (sel) => document.querySelector(sel);
const PAGE_SIZE = 40;
const POLL_MS = 2 * 60_000; // how often to look for new posts while caught up
const MIN_RECHECK_MS = 15_000; // don't re-check more often than this when scrolling to the end
// A fetch quicker than this shows "Fetched" for FETCHED_SHOW_MS afterwards so
// it doesn't flash by unnoticed; slower ones just clear when done.
const QUICK_FETCH_MS = 1000;
const FETCHED_SHOW_MS = 1000;
const STATUS_FADE_MS = 300; // matches the #end-status transition
const SAFARI_FALLBACK_MS = 1500;
const HISTORY_SIZE = 20; // already-read entries shown above the reading position on load
const POSITION_SAVE_MS = 4000; // batch position saves to the server

// iOS only: `navigator.standalone` exists in Safari on iPhone/iPad and is
// true when running from the home screen.
const IS_IOS = 'standalone' in navigator;
const IOS_HOME_SCREEN = navigator.standalone === true;

const store = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {}
  },
};

// ---------------------------------------------------------------- diagnostics
// A rolling log of what the app did and why, for working out odd behaviour
// on a phone where there are no developer tools. Settings → Copy diagnostics
// puts it on the clipboard. It records ids and positions only: no post text,
// names or tokens. It survives reloads, since iOS reloading the app is often
// part of the story.

const DIAG_KEY = 'reader.diag';
const DIAG_MAX = 400;

const diag = {
  entries: store.get(DIAG_KEY, []),
  log(event, data = {}) {
    this.entries.push({ t: Date.now(), e: event, ...data });
    if (this.entries.length > DIAG_MAX) this.entries.splice(0, this.entries.length - DIAG_MAX);
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.save(), 2000);
  },
  save() {
    clearTimeout(this.saveTimer);
    store.set(DIAG_KEY, this.entries);
  },
  lines() {
    const pad = (n, w = 2) => String(n).padStart(w, '0');
    return this.entries.map(({ t, e, ...data }) => {
      const d = new Date(t);
      const time = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
      const fields = Object.entries(data).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
      return `${time} ${e}${fields ? ` ${fields}` : ''}`;
    });
  },
};

window.addEventListener('error', (e) => diag.log('error', { msg: e.message, at: `${(e.filename || '').split('/').pop()}:${e.lineno}` }));
window.addEventListener('unhandledrejection', (e) => diag.log('error', { msg: String(e.reason?.message || e.reason) }));

function showToast(text) {
  const bar = $('#toast');
  bar.textContent = text;
  bar.hidden = false;
  clearTimeout(showToast.t);
  showToast.t = setTimeout(() => (bar.hidden = true), 5000);
}

function showError(err) {
  console.error(err);
  showToast(err.message || String(err));
}

const spinner = () => el('span', { class: 'spinner', 'aria-hidden': 'true' });
const busyText = (text) => [spinner(), text];

// ---------------------------------------------------------------- updates
// iOS keeps home-screen apps suspended in memory for days, so a deploy isn't
// picked up until the app is killed. The server says which version it runs
// (version.json); we compare it with the one we started with whenever the
// app comes back to the foreground (and every so often while open). If it
// changed, reload straight away when nothing is in progress (the reading
// position is saved, so we land in the same place), otherwise offer a reload.

const UPDATE_CHECK_MS = 30 * 60_000;
let loadedVersion = null;
let loadedBuilt = null;

async function fetchVersion() {
  try {
    const res = await fetch('version.json', { cache: 'no-store' });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

function versionLabel() {
  if (!loadedVersion) return 'Development build';
  const built = loadedBuilt ? ` · ${new Date(loadedBuilt).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}` : '';
  return `Version ${loadedVersion.slice(0, 7)}${built}`;
}

function busy() {
  return !!document.querySelector('dialog[open]') || !!document.querySelector('.overlay:not([hidden])');
}

async function checkForUpdate() {
  if (!loadedVersion || document.visibilityState !== 'visible') return;
  const latest = (await fetchVersion())?.version;
  if (!latest || latest === loadedVersion) return;
  if (!busy()) {
    diag.log('update-reload', { from: loadedVersion.slice(0, 7), to: latest.slice(0, 7) });
    diag.save();
    location.reload();
    return;
  }
  diag.log('update-available', { to: latest.slice(0, 7) });
  $('#update-bar').hidden = false;
}

async function watchForUpdates() {
  const v = await fetchVersion();
  loadedVersion = v?.version ?? null;
  loadedBuilt = v?.built ?? null;
  diag.log('version', { version: loadedVersion?.slice(0, 7) ?? 'dev' });
  if (!loadedVersion) return;
  $('#update-reload').onclick = () => location.reload();
  document.addEventListener('visibilitychange', checkForUpdate);
  setInterval(checkForUpdate, UPDATE_CHECK_MS);
}

// ---------------------------------------------------------------- sign in

function showLogin() {
  $('#boot').hidden = true;
  $('#login').hidden = false;
  $('#reader').hidden = true;
  $('#login-button').onclick = () => beginLogin().catch(showError);
  clientConfig().then((c) => c.sign_in && ($('#login-button').textContent = c.sign_in)).catch(() => {});
}

// First visit: the short interview that writes the reader's first profile.
async function onboard(api) {
  $('#boot').hidden = true;
  $('#onboarding').hidden = false;
  await runOnboarding(api, { showError });
  location.reload();
}

// ---------------------------------------------------------------- reader

class Reader {
  constructor(api, me) {
    this.api = api;
    this.me = me; // { user, name, owner, mastodon, position }
    this.posKey = `reader.pos.${me.user}`;
    this.settingsKey = `reader.settings.${me.user}`;
    this.settings = store.get(this.settingsKey, {});
    this.position = null; // id of the newest entry that has been read
    this.newest = null; // where to ask for newer entries from
    this.oldest = null; // where to ask for older entries from
    this.loadingOlder = false;
    this.noMoreOlder = false;
    this.overlays = []; // open thread/bookmarks panels, innermost last
    this.loading = false;
    this.caughtUp = false;
    this.lastChecked = 0;
    this.readCursor = 0; // index into the list of the first entry not yet marked read
    this.positionDirty = false;

    this.list = $('#timeline');
    this.end = $('#end');
  }

  async start() {
    $('#boot').hidden = true;
    $('#login').hidden = true;
    $('#reader').hidden = false;
    this.startBusy('Loading…');
    $('#who').textContent = this.me.name;
    this.bindUi();
    this.resolvePosition();
    await this.loadHistory();
    // Load until there's a screenful below the first unread entry (or nothing
    // more), so jumping to it isn't cut short by the bottom of the page.
    for (let i = 0; i < 10 && !this.caughtUp; i++) {
      const first = this.articles()[this.readCursor];
      if (first && this.end.getBoundingClientRect().top - first.getBoundingClientRect().top > window.innerHeight) break;
      if (!(await this.loadMore())) break;
    }
    this.jumpToFirstUnread();
    // Only start infinite loading once we know where the reader is up to.
    new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) this.loadOlder();
    }, { rootMargin: '1500px 0px 0px 0px' }).observe($('#older'));
    new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      if (!this.caughtUp) this.loadMore();
      else if (Date.now() - this.lastChecked > MIN_RECHECK_MS) this.checkForNew({ background: true });
    }, { rootMargin: '0px 0px 2000px 0px' }).observe(this.end);
  }

  // Your position is kept on the server, so every device picks up where any
  // other left off; this device's copy covers the moments it can't reach it.
  resolvePosition() {
    const local = store.get(this.posKey);
    const server = this.me.position;
    this.position = compareIds(server, local) >= 0 ? server : local;
    diag.log('start', { position: this.position, local, server });
    this.newest = this.position;
    if (compareIds(local, this.position) < 0) store.set(this.posKey, this.position);
    if (compareIds(local, server) > 0) {
      this.positionDirty = true; // this device read further while offline
      this.savePosition();
    }
  }

  // A few already-read entries above the reading position, so scrolling up
  // shows where you were.
  async loadHistory() {
    if (!this.position) return;
    try {
      const page = await this.api.before(nextId(this.position), HISTORY_SIZE);
      this.oldest = page.cursor;
      this.noMoreOlder = page.end;
      for (const entry of page.items) {
        const node = this.render(entry);
        if (!node) continue;
        node.classList.add('read');
        this.list.append(node);
        this.readCursor++;
      }
      diag.log('history', { got: page.items.length, oldest: this.oldest });
    } catch (err) {
      diag.log('history-failed', { status: err.status ?? err.message });
      if (err.unauthorised) return this.handleError(err);
      console.warn('Could not load read entries', err);
    }
  }

  // Put the first unread entry just under the header. With nothing unread,
  // show the last few read entries with the "caught up" message below them,
  // rather than the message alone.
  jumpToFirstUnread() {
    const first = this.articles()[this.readCursor];
    if (first) {
      const y = first.getBoundingClientRect().top + window.scrollY - this.headerHeight();
      window.scrollTo(0, y);
      diag.log('jump-to-unread', { id: first.dataset.id, y: Math.round(y), scrollY: Math.round(window.scrollY) });
    } else if (this.readCursor) {
      const endTop = this.end.getBoundingClientRect().top + window.scrollY;
      window.scrollTo(0, Math.max(0, endTop - window.innerHeight * 0.6));
    }
  }

  async loadOlder() {
    if (this.loadingOlder || this.noMoreOlder || !this.oldest || !this.overlaysClosed()) return;
    this.loadingOlder = true;
    const status = $('#older-status');
    status.replaceChildren(...busyText('Loading older posts…'));
    let ok = false;
    try {
      const page = await this.api.before(this.oldest, PAGE_SIZE);
      this.oldest = page.cursor;
      if (page.end) {
        this.noMoreOlder = true;
        status.textContent = 'Start of your timeline';
        return;
      }
      const frag = document.createDocumentFragment();
      let added = 0;
      for (const entry of page.items) {
        const node = this.render(entry);
        if (!node) continue;
        node.classList.add('read');
        frag.append(node);
        added++;
      }
      // Prepend without moving what the reader is looking at. Adding content
      // above the viewport needs a matching scroll correction, and iOS drops
      // or delays those while a flick is still gliding, so wait for scrolling
      // to stop first.
      const waitStart = performance.now();
      await this.scrollSettled();
      const anchor = this.list.firstElementChild || this.end;
      const before = anchor.getBoundingClientRect().top;
      this.list.prepend(frag);
      this.readCursor += added;
      const shift = anchor.getBoundingClientRect().top - before;
      window.scrollBy(0, shift);
      const drift = Math.round(anchor.getBoundingClientRect().top - before);
      diag.log('older', { got: page.items.length, added, shift: Math.round(shift), drift, waitedMs: Math.round(performance.now() - waitStart) });
      status.textContent = '';
      ok = true;
    } catch (err) {
      status.textContent = "Couldn't load older posts";
      showError(err);
    } finally {
      this.loadingOlder = false;
    }
    if (ok && window.scrollY < 1500) this.loadOlder();
  }

  // A timeline entry: a Mastodon post (filtered and annotated by the curator)
  // or an article it picked.
  render(entry) {
    if (entry.kind === 'status' && !this.me.mastodon) return null;
    if (entry.kind === 'article') {
      return renderCurated(entry, {
        onVote: (it, vote) => this.api.vote({ item_id: it.item }, vote),
        onBookmark: (it, on) => this.api.bookmark({ item_id: it.item }, on),
        onOpen: (it) => this.api.event({ item_id: it.item }, 'open'),
      });
    }
    return renderStatus(entry.status, {
      instance: this.me.mastodon,
      onThread: (s) => this.openThread(s),
      curation: {
        verdict: entry.verdict,
        vote: entry.vote,
        bookmarked: entry.bookmarked,
        onVote: (vote) => this.api.vote({ status_id: entry.id }, vote),
        onBookmark: (on) => this.api.bookmark({ status_id: entry.id }, on),
      },
    });
  }

  // On an iOS home-screen app, links to other sites open in an in-app Safari
  // viewer. If that page hands off to another app (YouTube, Mastodon, ...),
  // iOS leaves a blank viewer behind that we can neither detect nor close.
  // Optionally (off by default) send those links to Safari proper instead
  // (iOS 17+). The catch is that the other app's back button then returns to
  // Safari rather than here. If nothing happens (older iOS), fall back to the
  // normal behaviour.
  linksInSafari() {
    return IOS_HOME_SCREEN && this.settings.linksInSafari === true;
  }

  bindExternalLinks() {
    document.addEventListener('click', (e) => {
      if (!this.linksInSafari() || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = e.target.closest('a[href]');
      if (!a) return;
      const url = new URL(a.href, location.href);
      const target = url.origin === location.origin ? null : safariUrl(url.href);
      if (!target) return;
      e.preventDefault();
      location.href = target;
      setTimeout(() => {
        if (document.visibilityState === 'visible') window.open(url.href, '_blank', 'noopener');
      }, SAFARI_FALLBACK_MS);
    });
  }

  bindUi() {
    this.bindExternalLinks();
    // The bar is position: fixed; pad the page by its real height (which
    // includes the iOS safe area and changes with text size).
    const bar = $('#bar');
    new ResizeObserver(() => document.documentElement.style.setProperty('--bar-h', `${bar.offsetHeight}px`)).observe(bar);

    // We position the page ourselves (at the first unread entry); stop the
    // browser restoring the old scroll offset over the top of that.
    history.scrollRestoration = 'manual';
    if (history.state?.overlay) history.replaceState(null, '');
    window.addEventListener('popstate', () => this.closeTopOverlay());
    for (const back of document.querySelectorAll('.overlay .back')) back.onclick = () => history.back();
    $('#open-bookmarks').onclick = () => this.openBookmarks();
    $('#open-profile').onclick = () => {
      $('#settings').close();
      this.openProfile();
    };
    $('#open-suggestions').onclick = () => {
      $('#settings').close();
      this.openSuggestions();
    };
    $('#suggest-bar').onclick = () => this.openSuggestions();
    this.updateSuggestBar();

    // Browsers also fire scroll events when content is added or removed, so
    // note when the reader actually touched, wheeled, clicked or typed.
    this.lastInput = 0;
    for (const type of ['touchmove', 'wheel', 'pointerdown', 'keydown']) {
      window.addEventListener(type, () => (this.lastInput = performance.now()), { passive: true });
    }
    let lastY = window.scrollY;
    window.addEventListener('touchstart', () => (this.touching = true), { passive: true });
    window.addEventListener('touchend', () => (this.touching = false), { passive: true });
    window.addEventListener('touchcancel', () => (this.touching = false), { passive: true });
    let ticking = false;
    window.addEventListener('scroll', () => {
      this.lastScrollAt = performance.now();
      if (ticking || !this.overlaysClosed()) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        // Big moves with no recent touch are worth knowing about.
        const dy = window.scrollY - lastY;
        lastY = window.scrollY;
        if (Math.abs(dy) > 1200 && performance.now() - this.lastInput > 500) diag.log('jump', { dy: Math.round(dy), scrollY: Math.round(window.scrollY) });
        this.trackRead({ byScrolling: performance.now() - this.lastInput < 1000 });
      });
    }, { passive: true });
    this.bindPull();

    document.addEventListener('visibilitychange', () => {
      diag.log(document.visibilityState === 'hidden' ? 'hidden' : 'visible', { scrollY: Math.round(window.scrollY) });
      if (document.visibilityState === 'hidden') {
        diag.save();
        this.savePosition({ keepalive: true });
      } else if (this.caughtUp) {
        this.checkForNew({ background: true });
      }
    });
    window.addEventListener('pagehide', () => this.savePosition({ keepalive: true }));

    $('#check-new').onclick = () => this.checkForNew();
    $('#retry').onclick = () => this.loadMore({ force: true });
    $('#refresh').onclick = () => (this.caughtUp ? this.checkForNew() : this.nextPost());
    $('#menu').onclick = () => this.openSettings();

    document.addEventListener('keydown', (e) => {
      if (e.target.closest('input, textarea, select, dialog[open]')) return;
      if (e.key === 'Escape' && this.overlays.length) history.back();
      if (!this.overlaysClosed()) return;
      if (e.key === 'j') this.nextPost();
      if (e.key === 'k') window.scrollBy({ top: -window.innerHeight * 0.8, behavior: 'smooth' });
      if (e.key === 'r' && this.caughtUp) this.checkForNew();
    });
  }

  // Resolves once the page has stopped scrolling (no finger down, no scroll
  // events for a moment, which covers iOS momentum scrolling).
  scrollSettled() {
    return new Promise((resolve) => {
      const check = () => {
        if (!this.touching && performance.now() - (this.lastScrollAt || 0) > 250) resolve();
        else setTimeout(check, 100);
      };
      check();
    });
  }

  headerHeight() {
    return $('#bar').getBoundingClientRect().height;
  }

  articles() {
    return this.list.children;
  }

  // Scroll so the first unread entry sits just below the header.
  nextPost() {
    const next = this.articles()[this.readCursor];
    const target = next && next.getBoundingClientRect().top <= this.headerHeight() + 4 ? this.articles()[this.readCursor + 1] : next;
    if (target) window.scrollBy({ top: target.getBoundingClientRect().top - this.headerHeight(), behavior: 'smooth' });
    else this.end.scrollIntoView({ behavior: 'smooth' });
  }

  // Loads the entries after the newest one asked about. Only `end` means
  // we've caught up: the timeline has short pages mid-way (the curator drops
  // posts that miss the bar, Mastodon drops deleted or muted ones).
  async loadMore({ force = false, background = false } = {}) {
    if (this.loading || (this.caughtUp && !force)) return;
    this.loading = true;
    let ok = false;
    let failed = false;
    let result = 'Fetched';
    const checking = this.caughtUp;
    this.startBusy(checking ? 'Checking for new posts…' : 'Loading…');
    $('#retry').hidden = true;
    try {
      const after = this.newest;
      const page = await this.api.after(this.newest, PAGE_SIZE);
      this.lastChecked = Date.now();
      diag.log(checking ? 'check' : 'load', { after, got: page.items.length, cursor: page.cursor, end: page.end, background });
      // Never the same entry twice, whatever the server sends.
      const onPage = new Set([...this.articles()].map((n) => n.dataset.id));
      const fresh = page.items.filter((e) => compareIds(e.id, after) > 0 && !onPage.has(e.id));
      if (!this.oldest && fresh.length) this.oldest = fresh[0].id;
      for (const entry of fresh) {
        const node = this.render(entry);
        if (node) this.list.append(node);
      }
      if (page.cursor && compareIds(page.cursor, this.newest) > 0) this.newest = page.cursor;
      if (page.end) this.markCaughtUp();
      else this.leaveCaughtUp();
      if (checking) result = fresh.length ? `Fetched ${fresh.length} new post${fresh.length === 1 ? '' : 's'}` : 'Fetched · no new posts';
      this.updateCount();
      this.trackRead();
      ok = true;
    } catch (err) {
      diag.log('load-failed', { status: err.status ?? err.message, background });
      if (background && !err.unauthorised) {
        console.warn('Background check failed', err);
        $('#last-checked').textContent = "Couldn't check for new posts, will try again shortly.";
        this.schedulePoll();
      } else {
        failed = true;
        this.handleError(err);
      }
    } finally {
      this.loading = false;
      // Always settle the status line, whatever happened above.
      if (!failed) this.endBusy(result);
    }
    // Keep going while the end is close (but never retry in a loop).
    if (ok && !this.caughtUp && this.end.getBoundingClientRect().top < window.innerHeight + 2000) await this.loadMore();
    return ok;
  }

  // Pull up past the end of the timeline to check for new posts: the
  // bottom-of-the-page version of pull-to-refresh, since new posts arrive
  // at the bottom.
  bindPull() {
    const PULL_PX = 70;
    const hint = $('#pull-hint');
    let startY = null;
    let armed = false;
    const atBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    const reset = () => {
      startY = null;
      armed = false;
      hint.textContent = 'Pull up to check for new posts';
      this.end.classList.remove('pulling');
    };
    window.addEventListener('touchstart', (e) => {
      if (!this.caughtUp || !this.overlaysClosed() || e.touches.length !== 1 || !atBottom()) return;
      startY = e.touches[0].clientY;
    }, { passive: true });
    window.addEventListener('touchmove', (e) => {
      if (startY == null) return;
      const pulled = startY - e.touches[0].clientY;
      armed = pulled > PULL_PX && atBottom();
      this.end.classList.toggle('pulling', pulled > 10);
      hint.textContent = armed ? 'Release to check for new posts' : 'Pull up to check for new posts';
    }, { passive: true });
    window.addEventListener('touchend', () => {
      const go = armed;
      reset();
      if (go) this.checkForNew();
    });
    window.addEventListener('touchcancel', reset);
  }

  markCaughtUp() {
    this.caughtUp = true;
    this.end.classList.add('caught-up');
    const time = new Date(this.lastChecked || Date.now()).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    $('#last-checked').textContent = `New posts will appear here automatically. Last checked ${time}.`;
    this.updateCount();
    this.schedulePoll();
  }

  leaveCaughtUp() {
    this.caughtUp = false;
    this.end.classList.remove('caught-up');
    clearTimeout(this.pollTimer);
  }

  schedulePoll() {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => {
      // While hidden, visibilitychange triggers the next check instead.
      if (document.visibilityState === 'visible') this.checkForNew({ background: true });
    }, POLL_MS);
  }

  checkForNew({ background = false } = {}) {
    return this.loadMore({ force: true, background });
  }

  // The status line fades in and out (see #end-status in styles.css). Its
  // contents are only removed once the fade-out has finished, and any new
  // status cancels a pending fade.
  setStatus(text, isBusy = false) {
    clearTimeout(this.statusTimer);
    clearTimeout(this.fadeTimer);
    const line = $('#end-status');
    if (!text) {
      line.classList.remove('show');
      this.fadeTimer = setTimeout(() => line.replaceChildren(), STATUS_FADE_MS);
      return;
    }
    line.replaceChildren(...(isBusy ? busyText(text) : [text]));
    requestAnimationFrame(() => line.classList.add('show'));
  }

  startBusy(text) {
    this.busySince = performance.now();
    this.setStatus(text, true);
  }

  endBusy(done) {
    if (performance.now() - this.busySince >= QUICK_FETCH_MS) {
      this.setStatus('');
      return;
    }
    this.setStatus(`✓ ${done}`);
    this.statusTimer = setTimeout(() => this.setStatus(''), FETCHED_SHOW_MS);
  }

  // An entry counts as read once its bottom edge has scrolled up under the
  // header. Scrolling down to the "caught up" message also counts everything
  // above it as read, so the last few entries don't need a screen of empty
  // space below them. That only happens when the reader scrolls, never
  // because a new entry arrived while they were already at the bottom.
  trackRead({ byScrolling = false } = {}) {
    const limit = this.headerHeight();
    const items = this.articles();
    let advanced = false;
    const from = this.readCursor;
    let lowest = null; // bottom edge of the last entry marked, relative to the bar
    while (this.readCursor < items.length) {
      const node = items[this.readCursor];
      const bottom = node.getBoundingClientRect().bottom;
      if (bottom > limit) break;
      lowest = Math.round(bottom - limit);
      node.classList.add('read');
      this.setPosition(node.dataset.id);
      this.readCursor++;
      advanced = true;
    }
    if (this.readCursor > from) {
      diag.log('read', { why: 'scrolled-past', n: this.readCursor - from, to: items[this.readCursor - 1].dataset.id, lowest, scrollY: Math.round(window.scrollY), byScrolling, inputAgoMs: Math.round(performance.now() - (this.lastInput || 0)) });
    }
    const done = this.end.querySelector('.done');
    if (byScrolling && this.caughtUp && this.readCursor < items.length && done.getBoundingClientRect().bottom <= window.innerHeight) {
      const endFrom = this.readCursor;
      for (; this.readCursor < items.length; this.readCursor++) {
        const node = items[this.readCursor];
        node.classList.add('read');
        this.setPosition(node.dataset.id);
      }
      diag.log('read', { why: 'end-card', n: this.readCursor - endFrom, to: items[this.readCursor - 1].dataset.id, scrollY: Math.round(window.scrollY) });
      advanced = true;
    }
    if (advanced) this.updateCount();
  }

  updateCount() {
    const unread = this.articles().length - this.readCursor;
    $('#count').textContent = unread || this.caughtUp ? `${unread}${this.caughtUp ? '' : '+'} unread` : '';
  }

  setPosition(id) {
    if (compareIds(id, this.position) <= 0) return;
    this.position = id;
    store.set(this.posKey, id);
    this.positionDirty = true;
    clearTimeout(this.positionTimer);
    this.positionTimer = setTimeout(() => this.savePosition(), POSITION_SAVE_MS);
  }

  // Sends the position to the server (which never moves it back, so a device
  // that has read less can't undo another's progress).
  async savePosition({ keepalive = false } = {}) {
    if (!this.positionDirty || !this.position) return;
    this.positionDirty = false;
    clearTimeout(this.positionTimer);
    try {
      const saved = await this.api.savePosition(this.position, { keepalive });
      diag.log('position-saved', { position: this.position, server: saved, keepalive });
    } catch (err) {
      this.positionDirty = true;
      diag.log('position-save-failed', { status: err.status ?? err.message });
    }
  }

  handleError(err) {
    if (err.unauthorised) {
      showToast('Please sign in again');
      signOut();
      setTimeout(() => location.reload(), 1500);
      return;
    }
    this.setStatus('Could not load posts.');
    $('#retry').hidden = false;
    showError(err);
  }

  // ------------------------------------------------ thread + bookmarks panels

  overlaysClosed() {
    return this.overlays.length === 0;
  }

  // Panels are history entries, so the back button/swipe closes them.
  openOverlay(panel) {
    if (!panel.hidden) return;
    panel.hidden = false;
    panel.querySelector('.overlay-body').scrollTop = 0;
    this.overlays.push(panel);
    history.pushState({ overlay: panel.id }, '');
  }

  closeTopOverlay() {
    const panel = this.overlays.pop();
    if (panel) {
      panel.hidden = true;
      if (panel.id === 'thread') $('#thread-body').replaceChildren();
      if (panel.id === 'profile') $('#profile-body').replaceChildren();
      if (panel.id === 'suggestions') $('#suggestions-body').replaceChildren();
    }
  }

  renderPlain(status, extra = {}) {
    return renderStatus(status, { instance: this.me.mastodon, onThread: (s) => this.openThread(s), ...extra });
  }

  async openThread(status) {
    const panel = $('#thread');
    const body = $('#thread-body');
    this.openOverlay(panel);
    this.threadStatus = status;
    body.replaceChildren(el('p', { class: 'hint pad' }, ...busyText('Loading conversation…')));
    body.scrollTop = 0;
    try {
      const t = await this.api.thread(status.id);
      if (this.threadStatus !== status || panel.hidden) return;
      const frag = document.createDocumentFragment();
      for (const s of t.ancestors) {
        const node = this.renderPlain(s);
        if (node) frag.append(node);
      }
      const focus = this.renderPlain(t.status, { focus: true }) || this.renderPlain(status, { focus: true });
      if (focus) frag.append(focus);
      // Indent replies under the post they answer.
      const depth = new Map([[status.id, 0], [t.status.id, 0]]);
      for (const s of t.descendants) {
        const d = (depth.get(s.in_reply_to_id) ?? 0) + 1;
        depth.set(s.id, d);
        const node = this.renderPlain(s, { depth: d });
        if (node) frag.append(node);
      }
      body.replaceChildren(frag);
      if (focus && t.ancestors.length) body.scrollTop = focus.offsetTop;
    } catch (err) {
      body.replaceChildren(el('p', { class: 'hint pad', text: "Couldn't load this conversation." }));
      showError(err);
    }
  }

  // A quiet bar under the header when the scout has something to look at.
  updateSuggestBar() {
    const n = this.me.suggestions || 0;
    const bar = $('#suggest-bar');
    bar.hidden = !n;
    bar.textContent = `💡 ${n} new source suggestion${n === 1 ? '' : 's'} from the curator`;
  }

  async openSuggestions() {
    this.openOverlay($('#suggestions'));
    const onChange = async () => {
      this.me = { ...this.me, ...(await this.api.me()) };
      this.updateSuggestBar();
    };
    try {
      await showSuggestions(this.api, $('#suggestions-body'), { showError, busyText, onChange });
    } catch (err) {
      $('#suggestions-body').replaceChildren(el('p', { class: 'hint pad', text: "Couldn't load suggestions." }));
      showError(err);
    }
  }

  async openProfile() {
    this.openOverlay($('#profile'));
    try {
      await showProfile(this.api, $('#profile-body'), { showError, busyText });
    } catch (err) {
      $('#profile-body').replaceChildren(el('p', { class: 'hint pad', text: "Couldn't load your profile." }));
      showError(err);
    }
  }

  // Bookmarks: articles and posts you've bookmarked here, newest first, and
  // (for the owner) the Mastodon bookmarks kept on the server.
  async openBookmarks() {
    const body = $('#bookmarks-body');
    this.openOverlay($('#bookmarks'));
    body.replaceChildren(el('p', { class: 'hint pad' }, ...busyText('Loading bookmarks…')));
    try {
      const { articles, posts } = await this.api.bookmarks();
      const frag = document.createDocumentFragment();
      if (articles.length) frag.append(el('h2', { class: 'section-title', text: 'Bookmarked here' }), ...articles.map((a) => this.render(a)));
      const ps = posts.map((p) => this.renderPlain(p.status)).filter(Boolean);
      if (ps.length) frag.append(el('h2', { class: 'section-title', text: 'Mastodon bookmarks' }), ...ps);
      if (!frag.childNodes.length) frag.append(el('p', { class: 'hint pad', text: 'Nothing bookmarked yet. Tap 🔖 on a card to keep it here.' }));
      body.replaceChildren(frag);
    } catch (err) {
      body.replaceChildren(el('p', { class: 'hint pad', text: "Couldn't load your bookmarks." }));
      showError(err);
    }
  }

  diagnosticsText() {
    const state = {
      app: versionLabel(),
      copied: new Date().toString(),
      device: navigator.userAgent,
      homeScreen: navigator.standalone === true || matchMedia('(display-mode: standalone)').matches,
      screen: `${window.innerWidth}x${window.innerHeight} @${window.devicePixelRatio}`,
      reader: this.me.user,
      settings: JSON.stringify({ linksInSafari: !!this.settings.linksInSafari }),
      position: this.position,
      onPage: `${this.articles().length} entries, first unread index ${this.readCursor}, ${this.articles().length - this.readCursor} unread`,
      caughtUp: this.caughtUp,
      scrollY: `${Math.round(window.scrollY)} of ${document.documentElement.scrollHeight}`,
    };
    return [
      'Reader diagnostics',
      ...Object.entries(state).map(([k, v]) => `${k}: ${v}`),
      '',
      `Log (last ${diag.entries.length} events, oldest first):`,
      ...diag.lines(),
    ].join('\n');
  }

  // Clipboard first; if the browser refuses, the share sheet (Messages,
  // Mail, Notes...); failing both, select the text so it can be copied.
  async copyDiagnostics() {
    diag.log('diagnostics-copied');
    const text = this.diagnosticsText();
    try {
      await navigator.clipboard.writeText(text);
      showToast('Diagnostics copied. Paste them into a message.');
      return;
    } catch {}
    if (navigator.share) {
      try {
        await navigator.share({ title: 'Reader diagnostics', text });
        return;
      } catch {}
    }
    const area = el('textarea', { class: 'diag-text', readonly: true });
    area.value = text;
    $('#settings').append(area);
    area.select();
    showToast('Select all and copy the text below.');
  }

  openSettings() {
    const dlg = $('#settings');
    $('#signed-in-as').textContent = `Signed in as ${this.me.name}`;
    $('#safari-row').hidden = !IS_IOS;
    const safari = $('#links-in-safari');
    safari.checked = this.linksInSafari();
    safari.onchange = () => {
      this.settings.linksInSafari = safari.checked;
      store.set(this.settingsKey, this.settings);
    };
    this.settingsMastodon();
    $('#feed-url').value = this.me.feed_url;
    $('#feed-url').onfocus = (e) => e.target.select();
    $('#agent-info').textContent = this.me.agent
      ? 'Your Mac agent is set up. Making a new token replaces the old one.'
      : 'Sends your browser and Claude Code history (titles and addresses only) to make your profile better. See agent/README.md.';
    $('#agent-token').onclick = async () => {
      if (this.me.agent && !confirm('Replace your agent token? The old one stops working.')) return;
      try {
        const token = await this.api.newAgentToken();
        const out = $('#agent-token-value');
        out.value = token;
        out.hidden = false;
        out.select();
        this.me.agent = true;
        $('#agent-info').textContent = 'Copy this into your Mac\'s keychain now (service curator, account CURATOR_AGENT_TOKEN); it won\'t be shown again.';
      } catch (err) {
        showError(err);
      }
    };
    const decider = $('#decider');
    decider.value = this.me.decider || 'jev';
    decider.onchange = () => this.api.setDecider(decider.value).then(() => showToast('Saved: your feed updates in a minute')).catch(showError);
    $('#admin-group').hidden = !this.me.admin;
    if (this.me.admin) {
      this.api.readers().then(({ users }) => $('#admin-readers').replaceChildren(...users.map((u) => el('li', {
        text: `${u.name} (${u.user})${u.admin ? ', admin' : ''}: ${u.profile_version ? `profile v${u.profile_version}` : 'not set up yet'}, ${u.votes} votes, Mastodon ${u.mastodon || 'not connected'}${u.agent ? ', Mac agent' : ''}`,
      })))).catch(() => {});
    }
    $('#copy-diagnostics').onclick = () => this.copyDiagnostics();
    $('#app-version').textContent = versionLabel();
    $('#check-update').onclick = async () => {
      const latest = (await fetchVersion())?.version;
      if (!latest) $('#app-version').textContent = `${versionLabel()} · couldn't check`;
      else if (latest === loadedVersion) $('#app-version').textContent = `${versionLabel()} · up to date`;
      else location.reload(); // your place is saved, so this lands where you were
    };
    $('#position-info').textContent = this.position ? `Read up to ${this.position}` : 'No position saved yet';
    $('#mark-all').onclick = async () => {
      if (!confirm('Skip everything and start from now?')) return;
      try {
        await this.savePosition();
        const latest = await this.api.markAllRead();
        if (latest) store.set(this.posKey, latest);
        location.reload();
      } catch (err) {
        showError(err);
      }
    };
    $('#logout').onclick = async () => {
      if (!confirm('Sign out on this device?')) return;
      await this.savePosition();
      signOut();
      location.reload();
    };
    dlg.showModal();
  }

  settingsMastodon() {
    const on = !!this.me.mastodon;
    $('#mastodon-info').textContent = on
      ? `Connected to ${this.me.mastodon}: your home timeline is filtered into your feed. Replying and boosting happen there (↗ See original).`
      : 'Connect your Mastodon account to have your home timeline filtered into your feed. The curator only reads; it never posts.';
    $('#mastodon-connect').hidden = on;
    $('#mastodon-disconnect').hidden = !on;
    $('#mastodon-go').onclick = async () => {
      try {
        location.assign(await this.api.connectMastodon($('#mastodon-server').value));
      } catch (err) {
        showError(err);
      }
    };
    $('#mastodon-disconnect').onclick = async () => {
      if (!confirm('Disconnect Mastodon? Your feed goes back to curated articles only.')) return;
      try {
        await this.api.disconnectMastodon();
        location.reload();
      } catch (err) {
        showError(err);
      }
    };
  }
}

// ---------------------------------------------------------------- boot

async function boot() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
  watchForUpdates();

  const params = new URLSearchParams(location.search);
  if (params.has('code')) {
    history.replaceState(null, '', location.pathname);
    try {
      await finishLogin(params.get('code'), params.get('state'));
    } catch (err) {
      showError(err);
    }
  } else if (params.has('error')) {
    history.replaceState(null, '', location.pathname);
    showError(new Error(params.get('error_description') || params.get('error')));
  }

  // Back from connecting Mastodon.
  const mastodon = params.get('mastodon');
  if (mastodon) {
    history.replaceState(null, '', location.pathname);
    showToast({ connected: 'Mastodon connected: your home timeline will appear shortly', declined: 'Mastodon not connected', failed: "Couldn't connect Mastodon; try again" }[mastodon] || '');
  }

  if (!signedIn() || !(await accessToken().catch(() => null))) return showLogin();
  const api = new Api();
  try {
    const me = await api.me();
    if (!me.onboarded) return await onboard(api);
    await new Reader(api, me).start();
  } catch (err) {
    showError(err);
    if (err.unauthorised) {
      signOut();
      showLogin();
    }
  }
}

$('#settings-close').onclick = () => $('#settings').close();
boot();
