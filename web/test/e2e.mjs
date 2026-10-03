// End-to-end check of the reader against a fake curator API and a fake
// authentik (sign-in). Serve web/ first, then run this:
//   npx http-server -p 8124 -c-1 -s .     (in web/)
//   APP_URL=http://localhost:8124/ node test/e2e.mjs
import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const APP = process.env.APP_URL || 'http://localhost:8124/';
const AUTH = 'https://sso.example';
const ISSUER = `${AUTH}/application/o/mastorss/`;
const PIXEL = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==', 'base64');

// ---------------------------------------------------------------- fake data
// Mastodon posts have even ids 3000..3058; the curator's articles odd ids, so
// placement can be checked exactly. 3020 misses the curator's bar (it never
// arrives); 3014 is hidden by one of your Mastodon filters.
const acct = (i) => ({ id: `a${i}`, username: `user${i}`, acct: `user${i}@example.social`, display_name: `User ${i} :blob:`, avatar: 'https://cdn.example/avatar.png', avatar_static: 'https://cdn.example/avatar.png', emojis: [{ shortcode: 'blob', url: 'https://cdn.example/blob.png' }] });
function status(id) {
  return {
    id: String(id),
    created_at: new Date(Date.now() - (4000 - id) * 60000).toISOString(),
    account: acct(id % 7),
    content: `<p>Post number ${id} <a href="https://mastodon.au/tags/test" class="mention hashtag">#<span>test</span></a> <img src=x onerror="window.pwned=1"><script>window.pwned=1</script></p>`,
    spoiler_text: id === 3016 ? 'spoilers' : '',
    sensitive: false, visibility: 'public', mentions: [], emojis: [], media_attachments: [],
    replies_count: 0, reblogs_count: 1, favourites_count: 2,
    url: `https://example.social/@x/${id}`, reblog: null,
    filtered: id === 3014 ? [{ filter: { title: 'hidden', filter_action: 'hide' } }] : [],
    card: id === 3018 ? { url: 'https://example.com/article', title: 'An article', description: 'Something to read', provider_name: 'Example' } : null,
  };
}
const article = (id, item, extra = {}) => ({
  kind: 'article', id: String(id), item, url: `https://news.example/${item}`, title: `Article ${item}`,
  summary: `Summary of ${item}`, image: null, published: new Date().toISOString(), author: 'A. Writer',
  source: 'rss:ABC News', lane: 'main', reason: 'Jev 0.90: Must read', also: [], ...extra,
});
const longPost = 'A post with a lot to say. '.repeat(40).trim();

const state = {
  statuses: Array.from({ length: 30 }, (_, n) => status(3000 + 2 * n)).filter((s) => s.id !== '3020'),
  articles: [
    article(3007, 'read-one'),
    article(3013, 'between', { image: 'https://news.example/pic.jpg', summary: 'Summary of between. More at https://example.org/more.', also: [{ url: 'https://other.example/x', source: 'rss:The Verge' }] }),
    article(3201, 'newest', { lane: 'maybe', title: 'A post with a lot to say. A post with a lot…', summary: longPost }),
  ],
  verdicts: { 3022: { lane: 'maybe', also: [] }, 3024: { lane: 'main', also: [{ url: 'https://other.example/y', source: 'rss:9to5Mac' }] } },
  position: '3010', // the reader's position on the server
  positionPuts: [],
  votes: {}, // key → vote
  bookmarks: new Set(),
  bookmarkCalls: [],
  events: [],
  tokens: new Set(), // valid access tokens
  refresh: null, // the current (rotating) refresh token
  refreshes: 0,
  tokenLife: 600,
  failTimeline: false,
  onboarded: true,
  mastodon: 'mastodon.au',
  onboardingAnswers: null,
  profileVersions: [
    { version: 1, created: '2026-10-01T00:00:00Z', author: 'onboarding', summary: 'First profile, from the interview', profile_md: '# Profile of Alex\n\n## Live interests (this month)\n- **Homelab**: Proxmox\n', sources_yaml: 'learned: []\n', questions_yaml: 'relevant:\n  type: score\n' },
    { version: 2, created: '2026-10-02T00:00:00Z', author: 'curator', summary: 'Added KiCad', profile_md: '# Profile of Alex\n\n## Live interests (this month)\n- **Homelab**: Proxmox\n- **Electronics**: KiCad\n', sources_yaml: 'learned: []\n', questions_yaml: 'relevant:\n  type: score\n' },
  ],
  agentTokens: 0,
};
let tokenN = 0;
const num = (i) => BigInt(i);

const voteKey = (b) => (b.status_id ? `status:${b.status_id}` : b.item_id);
function annotate(e) {
  const key = e.kind === 'status' ? `status:${e.id}` : e.item;
  return { ...e, vote: state.votes[key] || 0, bookmarked: state.bookmarks.has(key) };
}
const asEntry = (s) => ({ kind: 'status', id: s.id, status: s, item: `item-${s.id}`, verdict: state.verdicts[s.id] || { lane: 'main', also: [] } });
const byId = (a, b) => (num(a.id) < num(b.id) ? -1 : 1);

function timelineAfter(after, limit) {
  // Never a full page (the curator drops posts that miss the bar after the limit).
  const page = state.statuses.filter((s) => !after || num(s.id) > num(after)).slice(0, limit - 3);
  const lo = after ? num(after) : (page.length ? num(page[0].id) - 1n : 0n);
  const hi = page.length ? num(page.at(-1).id) : null;
  const arts = state.articles.filter((a) => num(a.id) > lo && (hi == null || num(a.id) <= hi));
  const items = [...page.map(asEntry), ...arts].sort(byId).map(annotate);
  // Like the curator: the cursor covers the articles returned too.
  const covered = [...page.map((s) => s.id), ...arts.map((a) => a.id), ...(after ? [after] : [])];
  const cursor = covered.length ? covered.sort((a, b) => (num(a) < num(b) ? -1 : 1)).at(-1) : after;
  return { items, cursor, end: !page.length };
}

function timelineBefore(before, limit) {
  const page = state.statuses.filter((s) => num(s.id) < num(before)).slice(-limit);
  if (!page.length) return { items: [], cursor: before, end: true };
  const arts = state.articles.filter((a) => num(a.id) >= num(page[0].id) && num(a.id) < num(before));
  const items = [...page.map(asEntry), ...arts].sort(byId).map(annotate);
  return { items, cursor: page[0].id, end: false };
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(body) });

function issue() {
  const access = `acc${++tokenN}`;
  state.tokens.add(access);
  state.refresh = `ref${tokenN}`;
  return { access_token: access, refresh_token: state.refresh, expires_in: state.tokenLife, token_type: 'Bearer' };
}

async function fakes(ctx) {
  // authentik
  await ctx.route(`${AUTH}/**`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === '/application/o/mastorss/.well-known/openid-configuration') {
      return json(route, { issuer: ISSUER, authorization_endpoint: `${AUTH}/application/o/authorize/`, token_endpoint: `${AUTH}/application/o/token/` });
    }
    if (url.pathname === '/application/o/authorize/') {
      assert.equal(url.searchParams.get('client_id'), 'mastorss');
      assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
      assert.match(url.searchParams.get('scope'), /offline_access/);
      const back = new URL(url.searchParams.get('redirect_uri'));
      back.searchParams.set('code', 'the-code');
      back.searchParams.set('state', url.searchParams.get('state'));
      return route.fulfill({ status: 302, headers: { Location: back.toString() } });
    }
    if (url.pathname === '/application/o/token/') {
      const form = new URLSearchParams(req.postData());
      assert.equal(form.get('client_id'), 'mastorss');
      if (form.get('grant_type') === 'authorization_code') {
        assert.equal(form.get('code'), 'the-code');
        assert.ok(form.get('code_verifier'));
        return json(route, issue());
      }
      if (form.get('grant_type') === 'refresh_token') {
        if (state.tokenDelay) await new Promise((r) => setTimeout(r, state.tokenDelay));
        if (state.tokenDown) return route.abort();
        // Rotating: a refresh token works once.
        if (form.get('refresh_token') !== state.refresh) return json(route, { error: 'invalid_grant' }, 400);
        state.refreshes++;
        return json(route, issue());
      }
    }
    return route.fulfill({ status: 404 });
  });
  await ctx.route(/\/config\.json$/, (r) => json(r, { authority: ISSUER, client_id: 'mastorss', sign_in: 'Sign in with Example SSO' }));
  // the curator's reader API (same origin as the app)
  await ctx.route(/\/api\//, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const token = (req.headers().authorization || '').replace('Bearer ', '');
    if (!state.tokens.has(token)) return json(route, { error: 'sign in' }, 401);
    const body = req.postData() ? JSON.parse(req.postData()) : {};
    const q = url.searchParams;
    if (url.pathname === '/api/me') {
      return json(route, { user: 'alex', name: 'Alex', admin: true, onboarded: state.onboarded, mastodon: state.mastodon, position: state.position,
        feed_url: 'https://curator.example/f/secret/feed.json', agent: state.agentTokens > 0, decider: 'jev',
        suggestions: (state.suggestions || []).filter((x) => ['new', 'review'].includes(x.status)).length });
    }
    if (url.pathname === '/api/onboarding') {
      state.onboardingAnswers = body;
      state.onboarded = true;
      return json(route, { version: 1 });
    }
    if (url.pathname === '/api/profile' && req.method() === 'GET') {
      return json(route, { current: state.profileVersions.at(-1), versions: [...state.profileVersions].reverse().map(({ version, created, author, summary }) => ({ version, created, author, summary })) });
    }
    if (url.pathname === '/api/profile' && req.method() === 'PUT') {
      state.profileVersions.push({ ...body, version: state.profileVersions.length + 1, created: new Date().toISOString(), author: 'user', summary: body.summary || 'Edited by hand' });
      return json(route, { version: state.profileVersions.length });
    }
    const pv = url.pathname.match(/^\/api\/profile\/(\d+)$/);
    if (pv) return json(route, state.profileVersions[Number(pv[1]) - 1]);
    if (url.pathname === '/api/mastodon' && req.method() === 'POST') return json(route, { url: `https://${body.server}/oauth/authorize?client_id=x` });
    if (url.pathname === '/api/agent-token') {
      state.agentTokens++;
      return json(route, { token: `agent-token-${state.agentTokens}` });
    }
    if (url.pathname === '/api/admin/users') return json(route, { users: [{ user: 'alex', name: 'Alex', admin: true, profile_version: 2, votes: 3, mastodon: 'mastodon.au', agent: false }] });
    if (url.pathname === '/api/settings') return json(route, { decider: body.decider });
    if (url.pathname === '/api/timeline') {
      if (state.failTimeline) return json(route, { error: 'boom' }, 503);
      state.timelineCalls = (state.timelineCalls || 0) + 1;
      const limit = Number(q.get('limit'));
      return json(route, q.get('before') ? timelineBefore(q.get('before'), limit) : timelineAfter(q.get('after'), limit));
    }
    if (url.pathname === '/api/position') {
      state.positionPuts.push(body);
      if (body.latest) state.position = [...state.statuses, ...state.articles].sort(byId).at(-1).id;
      else if (num(body.position) > num(state.position || '0')) state.position = body.position;
      return json(route, { position: state.position });
    }
    if (url.pathname === '/api/vote') {
      state.votes[voteKey(body)] = body.vote;
      state.lastVote = body;
      return json(route, { ok: true });
    }
    if (url.pathname === '/api/bookmark') {
      state.bookmarkCalls.push(body);
      if (body.on) state.bookmarks.add(voteKey(body));
      else state.bookmarks.delete(voteKey(body));
      return json(route, { ok: true });
    }
    if (url.pathname === '/api/bookmarks') {
      const arts = state.articles.filter((a) => state.bookmarks.has(a.item)).map((a) => ({ ...a, id: `bookmark-${a.item}`, bookmarked: true }));
      return json(route, { articles: arts, posts: [{ kind: 'status', id: '2999', status: status(2999), verdict: {} }] });
    }
    if (url.pathname === '/api/suggestions') {
      const all = state.suggestions || [];
      return json(route, { open: all.filter((x) => ['new', 'review'].includes(x.status)), trying: all.filter((x) => x.status === 'trial'),
        history: all.filter((x) => ['accepted', 'dismissed'].includes(x.status)) });
    }
    const sug = url.pathname.match(/^\/api\/suggestions\/(\d+)$/);
    if (sug) {
      const x = state.suggestions.find((y) => y.id === Number(sug[1]));
      x.status = { add: 'accepted', try: 'trial', dismiss: 'dismissed', keep: 'accepted', drop: 'dismissed' }[body.action];
      x.decided = '2026-10-04 08:00:00';
      return json(route, { ok: true });
    }
    if (url.pathname === '/api/event') {
      state.events.push(body);
      return json(route, { ok: true });
    }
    const thread = url.pathname.match(/^\/api\/thread\/(\d+)$/);
    if (thread) {
      return json(route, { status: status(Number(thread[1])), ancestors: [status(2001)], descendants: [{ ...status(5000), in_reply_to_id: thread[1] }, { ...status(5001), in_reply_to_id: '5000' }] });
    }
    return json(route, { error: 'not found' }, 404);
  });
  // pictures and article pages
  await ctx.route(/^https:\/\/(cdn|news)\.example\//, (r) => (/\.(jpg|png)$/.test(r.request().url())
    ? r.fulfill({ contentType: 'image/png', body: PIXEL })
    : r.fulfill({ contentType: 'text/html', body: '<p>article</p>' })));
}

// A session as if signed in already (for the contexts after the first).
const signedIn = () => {
  const access = `acc${++tokenN}`;
  state.tokens.add(access);
  state.refresh = `ref${tokenN}`;
  return { access, refresh: state.refresh, expires: Date.now() + 3600e3 };
};

const browser = await chromium.launch();
const newContext = async () => {
  const c = await browser.newContext({ viewport: { width: 390, height: 800 }, serviceWorkers: 'block', permissions: ['clipboard-read', 'clipboard-write'] });
  await fakes(c);
  return c;
};

// ---------------------------------------------------------------- sign in
const ctx = await newContext();
const page = await ctx.newPage();
page.on('pageerror', (e) => console.error('pageerror', e));
await page.clock.install();
await page.goto(APP);
await page.waitForSelector('#login:not([hidden])');
await page.waitForFunction(() => document.querySelector('#login-button').textContent === 'Sign in with Example SSO');
await page.click('#login-button');
await page.waitForSelector('#reader:not([hidden])');
await page.waitForSelector('#timeline article');
assert.equal(new URL(page.url()).search, '', 'the code is cleared from the address');
assert.equal(await page.textContent('#who'), 'Alex');

// ---------------------------------------------------------------- the merged timeline
const ids = () => page.$$eval('#timeline article', (a) => a.map((n) => n.dataset.id));
const unreadIds = () => page.$$eval('#timeline article:not(.read)', (a) => a.map((n) => n.dataset.id));
const firstUnreadOffset = () => page.evaluate(() => {
  const first = document.querySelector('#timeline article:not(.read)');
  return first.getBoundingClientRect().top - document.querySelector('#bar').getBoundingClientRect().height;
});

// Starts right after the server's position, read entries above, scrolled to the first unread.
assert.deepEqual((await unreadIds()).slice(0, 3), ['3012', '3013', '3016'], 'starts after the position; 3014 hidden by a Mastodon filter');
assert.ok(Math.abs(await firstUnreadOffset()) < 2, 'scrolled to the first unread entry');
let order = await ids();
assert.ok(order.indexOf('3007') === order.indexOf('3006') + 1, 'a read article sits among the read posts');
assert.ok(await page.$('article[data-id="3007"].curated.read'));
assert.ok(order.indexOf('3013') === order.indexOf('3012') + 1, 'articles slot in between posts by id');
assert.equal(await page.$('article[data-id="3020"]'), null, 'a post that missed the curator\'s bar never arrives');
assert.equal(await page.evaluate(() => window.pwned), undefined, 'no script execution');
assert.equal(await page.$('article img[src="x"]'), null, 'inline img stripped');
assert.ok(await page.$('article .who img.emoji'), 'custom emoji rendered');
assert.ok(await page.$('article[data-id="3016"] details.cw'), 'content warning rendered');

// Mastodon posts: votes, bookmark and See original only.
const post = 'article[data-id="3012"]';
assert.equal(await page.$(`${post} button[aria-label="Reply"]`), null, 'no reply button: that happens on the server');
assert.equal(await page.$eval(`${post} a.see-original`, (a) => a.href), 'https://mastodon.au/@user2@example.social/3012');
await page.click(`${post} button[aria-label="More like this"]`);
await page.waitForSelector(`${post} button[aria-label="More like this"][aria-pressed="true"]`);
assert.deepEqual(state.lastVote, { status_id: '3012', vote: 1 }, 'votes on posts name the post');

// Articles: label, picture, linked URLs, votes, bookmark, tap-through.
const art = 'article[data-id="3013"]';
assert.match(await page.textContent(`${art} .curated-label`), /Picked for you/);
assert.equal(await page.$eval(`${art} .curated-image img`, (i) => i.src), 'https://news.example/pic.jpg');
assert.equal(await page.$eval(`${art} .curated-summary a`, (a) => a.href), 'https://example.org/more');
assert.match(await page.textContent(`${art} .also`), /Also covered by The Verge/);
await page.click(`${art} button[aria-label="Less like this"]`);
await page.waitForSelector(`${art} button[aria-label="Less like this"][aria-pressed="true"]`);
assert.deepEqual(state.lastVote, { item_id: 'between', vote: -1 });
await page.click(`${art} button[aria-label="Less like this"]`);
await page.waitForSelector(`${art} button[aria-label="Less like this"][aria-pressed="false"]`);
assert.deepEqual(state.lastVote, { item_id: 'between', vote: 0 }, 'pressing again clears the vote');
await page.click(`${art} button[aria-label="Bookmark"]`);
await page.waitForSelector(`${art} button[aria-label="Bookmark"][aria-pressed="true"]`);
assert.deepEqual(state.bookmarkCalls.at(-1), { item_id: 'between', on: true });
const [tapped] = await Promise.all([page.waitForEvent('popup'), page.click(`${art} .source-icon`)]);
assert.equal(tapped.url(), 'https://news.example/between', 'tapping the card opens the source');
await tapped.close();
for (let i = 0; i < 20 && !state.events.length; i++) await page.waitForTimeout(50);
assert.deepEqual(state.events.at(-1), { item_id: 'between', kind: 'open' }, 'and the open is reported');

// Bookmarks: articles kept here, and the Mastodon ones.
await page.click('#open-bookmarks');
await page.waitForSelector('#bookmarks-body article.curated');
assert.match(await page.textContent('#bookmarks-body'), /Bookmarked here.*Article between.*Mastodon bookmarks.*Post number 2999/s);
await page.goBack();
await page.waitForSelector('#bookmarks', { state: 'hidden' });

// Threads come from the curator.
await page.click(`${post} .content`, { position: { x: 4, y: 4 } }); // the text, not the hashtag link in it
await page.waitForSelector('#thread:not([hidden]) article.focus');
assert.deepEqual(await page.$$eval('#thread-body article', (a) => a.map((n) => n.dataset.id)), ['2001', '3012', '5000', '5001']);
assert.ok(await page.$('#thread-body article[data-id="5001"].d2'), 'replies indented');
await page.goBack();
await page.waitForSelector('#thread', { state: 'hidden' });

// ---------------------------------------------------------------- reading
for (let i = 0; i < 60; i++) {
  await page.mouse.wheel(0, 700);
  await page.waitForTimeout(40);
}
await page.waitForSelector('#end.caught-up');
// The curator's notes on posts: maybe, other coverage, link cards.
assert.match(await page.textContent('article[data-id="3022"] .curated-label'), /Maybe/);
assert.match(await page.textContent('article[data-id="3024"] .also'), /Also covered by 9to5Mac/);
assert.ok(await page.$('article[data-id="3018"] a.card'), 'link card rendered');
order = await ids();
assert.equal(order.at(-1), '3201', 'articles newer than every post come last');
assert.ok(await page.$('article[data-id="3201"].curated.maybe'));
// A long post shows once (no title repeating it), clipped, with Show more.
assert.equal(await page.$('article[data-id="3201"] .curated-title'), null);
await page.waitForSelector('article[data-id="3201"] .show-more:not([hidden])');
const clipped = await page.$eval('article[data-id="3201"] .curated-summary', (p) => p.clientHeight);
await page.click('article[data-id="3201"] .show-more');
assert.ok(await page.$eval('article[data-id="3201"] .curated-summary', (p) => p.clientHeight) > clipped, 'Show more opens it in place');
for (let i = 0; i < 8; i++) {
  await page.mouse.wheel(0, 700);
  await page.waitForTimeout(40);
}
await page.waitForTimeout(200);
assert.equal(await page.textContent('#count'), '0 unread');
// The position goes to the server a few seconds later, in one batch.
await page.clock.fastForward(5000);
for (let i = 0; i < 40 && state.position !== '3201'; i++) await page.waitForTimeout(50);
assert.equal(state.position, '3201', 'the position is saved on the server');
assert.ok(state.positionPuts.length <= 4, `position saves are batched (${state.positionPuts.length})`);

// New entries are picked up by the background check.
state.statuses.push(status(3300));
state.articles.push(article(3301, 'later'));
await page.clock.fastForward(2 * 60_000 + 1000);
await page.waitForSelector('#timeline article[data-id="3301"]');
assert.ok(await page.$('#timeline article[data-id="3300"]'));

// More checks with nothing new add nothing: no entry ever shows twice.
await page.click('#check-new');
await page.clock.fastForward(2 * 60_000 + 1000);
await page.click('#check-new');
await page.waitForTimeout(300);
const all = await ids();
assert.equal(new Set(all).size, all.length, `no duplicates on the page (${all.length} entries)`);

// A failing check shows Retry, and retries cleanly.
state.failTimeline = true;
await page.click('#check-new');
await page.waitForSelector('#retry:not([hidden])');
state.failTimeline = false;
await page.click('#retry');
await page.waitForSelector('#retry', { state: 'hidden' });

// The access token is refreshed before it runs out (the refresh token
// rotates), and requests carry the new one.
const refreshesBefore = state.refreshes;
await page.clock.fastForward(600_000);
await page.click('#check-new');
for (let i = 0; i < 40 && state.refreshes === refreshesBefore; i++) await page.waitForTimeout(50);
assert.equal(state.refreshes, refreshesBefore + 1, 'one refresh');

// Diagnostics: no tokens or post text.
await page.click('#menu');
assert.match(await page.textContent('#signed-in-as'), /Alex/);
await page.click('#copy-diagnostics');
const report = await page.evaluate(() => navigator.clipboard.readText());
assert.match(report, /^Reader diagnostics/);
assert.ok(!/acc\d|ref\d/.test(report), 'no tokens in the report');
assert.ok(!report.includes('Post number'), 'no post text in the report');
await page.click('#settings-close');

// Reload: picks up where we were, read entries above; the two that arrived
// in the background check (not scrolled past yet) are still unread.
await page.reload();
await page.waitForSelector('#end.caught-up');
assert.deepEqual(await unreadIds(), ['3300', '3301']);
assert.ok(await page.$('article[data-id="3201"].read'), 'what was read stays read');
await ctx.close();

// ---------------------------------------------------------------- how the curator sees you
{
  const session = signedIn();
  const c = await newContext();
  await c.addInitScript(([s]) => localStorage.setItem('reader.session', JSON.stringify(s)), [session]);
  const p = await c.newPage();
  await p.goto(APP);
  await p.waitForSelector('#timeline article');
  await p.click('#menu');
  // Settings: the feed address, Mastodon, the agent token, the readers list.
  assert.equal(await p.inputValue('#feed-url'), 'https://curator.example/f/secret/feed.json');
  assert.match(await p.textContent('#mastodon-info'), /Connected to mastodon\.au/);
  assert.equal(await p.isVisible('#mastodon-disconnect'), true);
  await p.click('#agent-token');
  await p.waitForSelector('#agent-token-value:not([hidden])');
  assert.equal(await p.inputValue('#agent-token-value'), 'agent-token-1', 'the token is shown once');
  await p.waitForSelector('#admin-readers li');
  assert.match(await p.textContent('#admin-readers'), /Alex \(alex\), admin: profile v2, 3 votes/);
  // The profile, its versions and what changed.
  await p.click('#open-profile');
  await p.waitForSelector('#profile-body .md');
  assert.match(await p.textContent('#profile-body .md'), /Electronics: KiCad/);
  assert.equal(await p.$eval('#profile-body .md strong', (n) => n.textContent), 'Homelab', 'bold rendered, as text');
  await p.click('#profile-body .versions li:first-child button');
  await p.waitForSelector('#profile-body .versions pre.diff .add');
  assert.match(await p.textContent('#profile-body .versions pre.diff .add'), /\+ - \*\*Electronics\*\*: KiCad/);
  // Editing saves a new version.
  await p.click('#profile-body button:text("Edit")');
  await p.fill('#edit-profile_md', '# Profile of Alex\n\n- **Birds**\n');
  await p.fill('#edit-summary', 'Added birds');
  await p.click('#profile-body button:text("Save as a new version")');
  await p.waitForFunction(() => /Birds/.test(document.querySelector('#profile-body .md')?.textContent || ''));
  assert.equal(state.profileVersions.at(-1).summary, 'Added birds');
  assert.match(await p.textContent('#profile-body .versions li:first-child'), /Edited by you/);
  await p.goBack();
  await p.waitForSelector('#profile', { state: 'hidden' });
  // Connecting Mastodon goes to the server to approve.
  state.mastodon = null;
  await p.reload();
  await p.waitForSelector('#reader:not([hidden])');
  await p.click('#menu');
  assert.equal(await p.isVisible('#mastodon-connect'), true);
  await p.route('https://m.example/**', (r) => r.fulfill({ contentType: 'text/html', body: '<p>approve?</p>' }));
  await p.fill('#mastodon-server', 'm.example');
  await Promise.all([p.waitForURL(/m\.example\/oauth\/authorize/), p.click('#mastodon-go')]);
  // Without Mastodon the timeline is articles only.
  await p.goto(APP);
  await p.waitForSelector('#timeline article');
  assert.equal(await p.$('#timeline article:not(.curated)'), null, 'no Mastodon posts without a connection');
  state.mastodon = 'mastodon.au';
  await c.close();
}

// ---------------------------------------------------------------- a new reader
// First visit: the interview, then the feed.
{
  state.onboarded = false;
  const session = signedIn();
  const c = await newContext();
  await c.addInitScript(([s]) => localStorage.setItem('reader.session', JSON.stringify(s)), [session]);
  const p = await c.newPage();
  await p.goto(APP);
  await p.waitForSelector('#onboarding:not([hidden])');
  assert.equal(await p.isVisible('#reader'), false);
  await p.fill('#ob-about', 'A gardener');
  await p.fill('#ob-interests', 'Native plants, astronomy');
  await p.fill('#ob-dislikes', 'Sport');
  await p.selectOption('#ob-volume', 'About 20');
  await Promise.all([p.waitForEvent('load'), p.click('#onboarding-form button[type="submit"]')]);
  await p.waitForSelector('#reader:not([hidden])');
  assert.equal(state.onboardingAnswers.interests, 'Native plants, astronomy');
  assert.equal(state.onboardingAnswers.volume, 'About 20');
  await c.close();
}

// ---------------------------------------------------------------- another device
// A second device behind the server's position picks up the server's.
{
  const session = signedIn();
  const c = await newContext();
  await c.addInitScript(([s]) => {
    if (sessionStorage.getItem('seeded')) return;
    sessionStorage.setItem('seeded', '1');
    localStorage.setItem('reader.session', JSON.stringify(s));
    localStorage.setItem('reader.pos.alex', JSON.stringify('3002'));
  }, [session]);
  const serverPos = state.position;
  const p = await c.newPage();
  await p.goto(APP);
  await p.waitForSelector('#end.caught-up');
  const unread = await p.$$eval('#timeline article:not(.read)', (a) => a.map((n) => n.dataset.id));
  assert.ok(unread.every((id) => BigInt(id) > BigInt(serverPos)), `starts from the furthest position, the server's (${serverPos}): ${unread}`);
  // Mark everything read: the server jumps to the newest entry.
  state.statuses.push(status(3400));
  await p.click('#menu');
  p.once('dialog', (d) => d.accept());
  await Promise.all([p.waitForEvent('load'), p.click('#mark-all')]);
  assert.deepEqual(state.positionPuts.at(-1), { latest: true });
  assert.equal(state.position, '3400');
  await p.waitForSelector('#end.caught-up');
  assert.equal(await p.textContent('#count'), '0 unread');
  // Signing out returns to the sign-in page.
  await p.click('#menu');
  p.once('dialog', (d) => d.accept());
  await Promise.all([p.waitForEvent('load'), p.click('#logout')]);
  await p.waitForSelector('#login:not([hidden])');
  await c.close();
}

// A revoked session (the refresh token no longer works) goes back to sign-in.
{
  const c = await newContext();
  await c.addInitScript(() => localStorage.setItem('reader.session', JSON.stringify({ access: 'stale', refresh: 'dead', expires: 0 })));
  const p = await c.newPage();
  await p.goto(APP);
  await p.waitForSelector('#login:not([hidden])');
  await c.close();
}

// ---------------------------------------------------------------- iOS
// Regression: on iOS, scroll corrections made while a flick is gliding can be
// dropped. The app used to remove old read entries from the top while you
// read and correct the scroll; when iOS dropped the correction, dozens of
// unseen entries were marked read in one go. Simulate that by dropping every
// scrollBy: reading must still advance only a few entries per small scroll.
{
  state.statuses = Array.from({ length: 120 }, (_, n) => status(1000 + n));
  state.articles = [];
  state.position = '1000';
  const session = signedIn();
  const c = await newContext();
  await c.addInitScript(([s]) => {
    if (!sessionStorage.getItem('seeded')) {
      sessionStorage.setItem('seeded', '1');
      localStorage.setItem('reader.session', JSON.stringify(s));
    }
    window.scrollBy = () => {};
  }, [session]);
  const p = await c.newPage();
  await p.goto(APP);
  await p.waitForSelector('#timeline article:not(.read)');
  const readPos = () => p.evaluate(() => Number(JSON.parse(localStorage.getItem('reader.pos.alex')) || 1000));
  let prev = await readPos();
  for (let i = 0; i < 60; i++) {
    await p.mouse.wheel(0, 300);
    await p.waitForTimeout(80);
    const now = await readPos();
    // Reaching the "caught up" card marks the last few entries above it read, by design.
    const atEnd = now === 1119;
    // A slow machine can merge two scroll steps (~4 short posts); the bug
    // this guards against marked 20-44 at once.
    assert.ok(now - prev <= 8 || atEnd, `read position jumped ${now - prev} entries in one small scroll (${prev} -> ${now})`);
    prev = now;
  }
  assert.ok(prev > 1040, `reading advanced normally (to ${prev})`);
  await c.close();
}

// iOS home-screen app: by default links open normally (the in-app viewer).
// With "Open links in Safari" on, they go through x-safari-https://, falling
// back to a normal open if nothing takes over (as here: Chromium doesn't
// know the scheme).
{
  const session = signedIn();
  const ios = await newContext();
  await ios.addInitScript(([s]) => {
    Object.defineProperty(Navigator.prototype, 'standalone', { get: () => true });
    localStorage.setItem('reader.session', JSON.stringify(s));
    window.opened = [];
    window.open = (url) => window.opened.push(url);
  }, [session]);
  const ip = await ios.newPage();
  await ip.goto(APP);
  await ip.waitForSelector('#timeline article .content a.hashtag');
  assert.equal(await ip.evaluate(async () => (await import('./js/render.js')).safariUrl('https://example.com/a?b=1')), 'x-safari-https://example.com/a?b=1');
  await ip.click('#menu');
  assert.equal(await ip.isVisible('#safari-row'), true, 'setting shown on iOS');
  assert.equal(await ip.isChecked('#links-in-safari'), false, 'off by default');
  await ip.click('#settings-close');
  const defaultOpen = ip.waitForEvent('popup');
  await ip.click('#timeline article .content a.hashtag');
  await (await defaultOpen).close(); // a plain new-window open, not intercepted
  assert.deepEqual(await ip.evaluate(() => window.opened), []);
  await ip.click('#menu');
  await ip.check('#links-in-safari');
  await ip.click('#settings-close');
  let popup = false;
  ip.on('popup', () => (popup = true));
  await ip.click('#timeline article .content a.hashtag');
  await ip.waitForTimeout(300);
  assert.deepEqual(await ip.evaluate(() => window.opened), [], 'not opened in the in-app viewer straight away');
  await ip.waitForFunction(() => window.opened.length === 1, null, { timeout: 3000 });
  assert.equal(await ip.evaluate(() => window.opened[0]), 'https://mastodon.au/tags/test', 'falls back when Safari does not take over');
  assert.equal(popup, false);
  await ios.close();
}

// ---------------------------------------------------------------- suggested sources
{
  const ev = { per_day: 4.2, hit_rate: 0.4, novelty: 0.8, samples: [{ title: 'A sample story', url: 'https://news.example/s' }] };
  state.suggestions = [
    { id: 1, kind: 'rss', value: 'https://good.example/feed', name: 'Good News', why: 'covered 3 stories you liked', evidence: ev, status: 'new' },
    { id: 2, kind: 'mastodon_account', value: 'sam@social.example', name: 'Sam', why: 'you liked 2 of their posts', evidence: ev, status: 'new' },
    { id: 3, kind: 'rss', value: 'https://trial.example/rss', name: 'Trial Times', why: 'web search', status: 'review',
      evidence: { items: 9, liked: 3, opened: 2, disliked: 1 } },
  ];
  const sc = await newContext();
  await sc.addInitScript(([s]) => localStorage.setItem('reader.session', JSON.stringify(s)), [signedIn()]);
  const sp = await sc.newPage();
  sp.on('pageerror', (e) => console.error('pageerror', e));
  await sp.goto(APP);
  await sp.waitForSelector('#suggest-bar:not([hidden])');
  assert.equal(await sp.textContent('#suggest-bar .badge'), '3');
  assert.match(await sp.getAttribute('#suggest-bar', 'title'), /3 new source suggestions/);
  // In the header, so it's in view wherever the timeline opens.
  assert.ok(await sp.evaluate(() => { const r = document.querySelector('#suggest-bar').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }));
  await sp.click('#suggest-bar');
  await sp.waitForSelector('#suggestions .suggestion');
  const cards = () => sp.$$eval('#suggestions .suggestion', (a) => a.map((n) => n.textContent));
  let text = await cards();
  assert.match(text[0], /Good News.*covered 3 stories.*About 4\.2 a day · 40% would make your feed/s);
  assert.match(text[1], /@sam@social\.example on Mastodon/);
  assert.match(text[2], /Trial over.*you liked 3, opened 2, disliked 1.*Keep it.*Drop it/s);
  await sp.click('.suggestion[data-id="2"] button:has-text("Try for a week")');
  await sp.waitForFunction(() => document.querySelectorAll('#suggestions .suggestion').length === 2);
  assert.match(await sp.textContent('#suggestions'), /Trying now.*@sam@social\.example on Mastodon|Trying now.*Sam/s);
  await sp.click('.suggestion[data-id="1"] button:has-text("Not interested")');
  await sp.waitForFunction(() => document.querySelectorAll('#suggestions .suggestion').length === 1);
  await sp.click('.suggestion[data-id="3"] button:has-text("Keep it")');
  await sp.waitForFunction(() => !document.querySelector('#suggestions .suggestion'));
  assert.match(await sp.textContent('#suggestions'), /Nothing new this week/);
  assert.deepEqual(state.suggestions.map((x) => x.status), ['dismissed', 'trial', 'accepted']);
  await sp.goBack();
  await sp.waitForFunction(() => document.querySelector('#suggest-bar').hidden);
  // A story from a source on trial says so.
  assert.match(await sp.evaluate(async () => (await import('./js/render.js')).renderCurated(
    { id: '1', item: 'i', url: 'https://news.example/t', title: 'T', summary: '', source: 'rss:Trial Times', lane: 'maybe', trial: 'Trial Times', also: [] }, {}).textContent),
  /Trying out Trial Times/);
  await sc.close();
  state.suggestions = [];
}

// ---------------------------------------------------------------- updates
// A new deploy is noticed when the app comes back to the foreground: a
// Reload bar while something is open, a straight reload otherwise.
{
  const session = signedIn();
  const upd = await newContext();
  let deployed = 'v1';
  await upd.route('**/version.json', (r) => r.fulfill({ contentType: 'application/json', body: JSON.stringify({ version: deployed, built: '2026-10-02T10:00:00Z' }) }));
  await upd.addInitScript(([s]) => localStorage.setItem('reader.session', JSON.stringify(s)), [session]);
  const up = await upd.newPage();
  await up.goto(APP);
  await up.waitForSelector('#timeline article');
  await up.click('#open-bookmarks');
  deployed = 'v2';
  await up.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await up.waitForSelector('#update-bar:not([hidden])');
  await up.goBack();
  await up.waitForSelector('#bookmarks', { state: 'hidden' });
  await up.evaluate(() => {
    window.stillOldPage = true;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await up.waitForFunction(() => !window.stillOldPage && document.querySelector('#timeline article'));
  await up.waitForTimeout(500);
  assert.equal(await up.isVisible('#update-bar'), false, 'no reload loop after updating');
  await up.click('#menu');
  assert.match(await up.textContent('#app-version'), /^Version v2 · .*2026/);
  await up.click('#check-update');
  await up.waitForFunction(() => /up to date/.test(document.querySelector('#app-version').textContent));
  await upd.close();
}

// ---------------------------------------------------------------- staying signed in
// A deploy noticed while the access token is being refreshed: the reload
// waits for the refresh, so the new refresh token isn't lost.
{
  const stale = { ...signedIn(), expires: Date.now() - 1000 }; // access token expired
  const rc = await newContext();
  let deployed = 'v1';
  await rc.route('**/version.json', (r) => r.fulfill({ contentType: 'application/json', body: JSON.stringify({ version: deployed }) }));
  await rc.addInitScript(([s]) => { if (!sessionStorage.getItem('seeded')) { localStorage.setItem('reader.session', JSON.stringify(s)); sessionStorage.setItem('seeded', '1'); } }, [stale]);
  const rp = await rc.newPage();
  await rp.goto(APP);
  await rp.waitForSelector('#timeline article');
  await rp.evaluate(() => {
    const s = JSON.parse(localStorage.getItem('reader.session'));
    localStorage.setItem('reader.session', JSON.stringify({ ...s, expires: Date.now() - 1000 }));
  });
  state.tokenDelay = 800;
  deployed = 'v2';
  const before = state.refreshes;
  await rp.evaluate(async () => {
    (await import('./js/api.js')).accessToken().catch(() => {}); // a refresh in flight, as when coming back to the app
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await rp.waitForTimeout(2500);
  state.tokenDelay = 0;
  await rp.waitForSelector('#reader:not([hidden])');
  assert.equal(await rp.isVisible('#login'), false, 'still signed in after reloading for a deploy mid-refresh');
  assert.ok(state.refreshes > before);
  // Sign-in unreachable at start-up (a restart, a blip): try again, don't ask to sign in.
  await rp.evaluate(() => {
    const s = JSON.parse(localStorage.getItem('reader.session'));
    localStorage.setItem('reader.session', JSON.stringify({ ...s, expires: Date.now() - 1000 }));
  });
  state.tokenDown = true;
  await rp.reload();
  await rp.waitForTimeout(1000);
  assert.equal(await rp.isVisible('#login'), false, 'no sign-in screen for a network blip');
  state.tokenDown = false;
  await rp.waitForSelector('#timeline article', { timeout: 8000 });
  await rc.close();
}

await browser.close();
console.log('e2e ok');
