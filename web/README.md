# The reader app

The app you read the curator through (it began as Mastorss, a Mastodon reader
that read the home timeline oldest to newest, like an RSS reader). Your
Mastodon home timeline, filtered by the curator and merged with the articles
it picked for you, read oldest to newest from where you left off. Static
files with no build step, served by the curator at `/`; its only backend is
the curator's `/api/` (see `../README.md`).

- **Sign-in**: the curator's OIDC provider, authorization code + PKCE with a
  public client (no secret in the app; `/config.json` says which). Tokens
  live in `localStorage`; the refresh token rotates on every use, so
  refreshing is single-flight.
- **First visit**: a short interview; the curator writes your first profile
  from it.
- **Timeline**: `GET /api/timeline?after=<id>` and `?before=<id>`. Entries
  are Mastodon posts (`kind: "status"`, with the curator's verdict) and
  articles (`kind: "article"`), in timeline-id order: Mastodon ids are
  ms << 16, and each curated story carries a `sort_id` in the same space.
  Only `end: true` means caught up; pages are often short. Without a
  Mastodon connection the timeline is articles only.
- **Position**: kept on the server per reader (`PUT /api/position`, which
  never moves it back), plus a copy in `localStorage` for the moments it
  can't be reached. On start the further of the two wins.
- **Reading actions**: 👍/👎 and 🔖 on every card; threads via
  `GET /api/thread/<id>`. Replying, boosting and favouriting happen on your
  Mastodon server: ↗ See original.
- **How the curator sees you**: your profile, every version and what changed,
  and editing it by hand (Settings).
- **Settings**: connect Mastodon, your feed's address for other readers, a
  token for the Mac agent, which classifier decides, and the diagnostics:
  sent to the server's journal under an id (`POST /api/diagnostics`), saved
  as a file (the share sheet, or a download), or copied.
- **New posts**: checked every 2 minutes while caught up, when the app comes
  back to the foreground, at the end of the page, and by pulling up.
- **Updates**: the server's `version.json` is the deployed version; the app
  reloads itself when it changes.
- **One version at a time**: the curator serves every file but the page and
  `sw.js` under a name carrying its content hash (`js/app.<hash>.js`, the
  imports and `index.html` rewritten to match; `curator/shell.py`), so a page
  only ever loads files of its own version, whatever a cache holds. The files
  here keep their plain names. Each shell has a version id, written into
  `sw.js` and given as `shell` in `version.json`: a new one is a new worker,
  which caches all of that version's files or none, and drops the others.
- **Offline and slow starts**: `sw.js` serves the hashed files from its cache,
  and the page from the network, but from its cache if the network hasn't
  answered in 1.5 s. Each start's timings (the page, each file, the server's
  `Server-Timing`, what the service worker did) go in the diagnostics log;
  see `js/app.js`.
- **A start that fails**: if the app hasn't started within 8 s, or its files
  fail to load, `js/boot.js` (a plain script, before the modules) replaces
  the spinner with "Couldn't start the reader" and **Reset and reload**, which
  drops the service worker and its caches.
- **Dead connections**: every request has a timeout (10 s for reads, 20 s
  for writes and the token refresh). A read, or the position, is sent again
  once; other writes and the refresh token never are. A check that still
  fails says "Couldn't reach the server" with Retry. Each API call's time
  (and the part spent on the token) goes in the diagnostics log; see
  `js/api.js`.

Reading behaviour (what counts as read, the iOS scroll quirks, the faded
history above your position, "Open links in Safari", diagnostics) is
explained in the comments in `js/app.js`.

## Tests

    npm install && npx playwright install chromium
    npm start &           # serves this folder on :8124
    npm test

The service worker's part builds the shell with `python -m curator.shell`
(the repo's `.venv`, or `CURATOR_PYTHON`).

The end-to-end test fakes the sign-in provider and the curator API.
