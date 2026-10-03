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
  token for the Mac agent, which classifier decides.
- **New posts**: checked every 2 minutes while caught up, when the app comes
  back to the foreground, at the end of the page, and by pulling up.
- **Updates**: the server's `version.json` is the deployed version; the app
  reloads itself when it changes.

Reading behaviour (what counts as read, the iOS scroll quirks, the faded
history above your position, "Open links in Safari", diagnostics) is
explained in the comments in `js/app.js`.

## Tests

    npm install && npx playwright install chromium
    npm start &           # serves this folder on :8124
    npm test

The end-to-end test fakes the sign-in provider and the curator API.
