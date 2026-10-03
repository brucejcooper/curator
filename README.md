# curator

A self-hosted, AI-curated news reader. It collects articles and posts from
RSS feeds, Mastodon and Bluesky hashtags, and your own Mastodon home timeline,
groups coverage of the same story, and picks what's worth your time using two
classifiers side by side: [Jev](https://typesafe.ai) (hosted, calibrated
judgments) and a local model trained on your votes. Claude keeps a readable
profile of each reader's interests, revised daily from what they read, vote
on and bookmark, and once a week it suggests new sources (sites, people,
hashtags) it has tried against your interests. You read it all in one timeline, oldest to newest, in the
included web app.

Several people can use one curator, each with their own profile, sources,
feed and (optionally) Mastodon account. Collection and the item-level
judgments (how significant is this as news, is it rage bait, an advert...)
are shared; each reader's own relevance questions and models are theirs.

See [`docs/design.md`](docs/design.md) for how it works.

- `curator/`: the service: collect, group stories, judge, publish, the API.
- `web/`: the reader app (static files, no build step), served at `/`.
- `agent/`: an optional Mac agent that sends your browser and Claude Code
  history (titles and URLs only) to sharpen your profile.
- `tests/`: Python tests; `web/test/` has the app's end-to-end test.

## Running it

    uv sync --extra server
    uv run pytest
    uv run curator                      # web server + 15-minute loop + daily jobs
    uv run curator-refresh-profile ALEX # one reader's daily profile revision

It needs an OIDC provider for sign-in (authentik, Keycloak, ...) with a
**public** client for the reader app (authorization code + PKCE, refresh
tokens; redirect URI `<CURATOR_PUBLIC_URL>/`), whose access tokens carry
`preferred_username`. Anyone it lets in can use the curator; their first
visit is a short interview that writes their first profile.

### Settings (environment)

| Variable | |
|---|---|
| `CURATOR_PUBLIC_URL` | Where it's served, e.g. `https://curator.example.org` |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID` | Your sign-in provider and the reader app's client |
| `CURATOR_SIGN_IN_LABEL` | The sign-in button's text |
| `CURATOR_OWNER` | The admin's username |
| `CURATOR_SECRET_KEY` | A Fernet key, for Mastodon tokens at rest (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |
| `ANTHROPIC_API_KEY`, `JEV_API_KEY` | Claude (profiles) and Jev (judging) |
| `MASTODON_SERVER`, `MASTODON_TOKEN` | The owner's Mastodon account, used to read the hashtags anyone follows (read-only) |
| `CURATOR_REGION` | Where "significant news" is judged from, e.g. `Perth, Australia` |
| `CURATOR_DAILY_AT`, `TZ` | When the daily jobs run (default 04:30) |
| `CURATOR_SCOUT_WEEKDAY` | Which day's daily run looks for new sources (0 = Monday; default 6, Sunday) |
| `CURATOR_DATA` | The data folder (SQLite, feeds, backups); `/data` in the container |
| `CURATOR_EXPORT_GIT_URL`, `CURATOR_EXPORT_USERS`, `FORGEJO_TOKEN` | Optional: a private git repo the chosen readers' profiles are exported to each day |

On the Mac, secrets can come from the keychain instead (service `curator`,
account = the variable name).

### Endpoints

- `/`: the reader app; `/api/...` its API (each call needs the reader's
  access token); `/config.json` tells the app how to sign in.
- `/f/<feed path>/feed.json` and `feed.xml`: each reader's feed for other
  readers, at an address only they know (Settings shows it).
- `/agent/events`, `/agent/rules`: the Mac agent, with the reader's agent token.
- `/status`: an overview for the admin. Put it behind single sign-on at your
  reverse proxy.
- `/version.json`: the deployed version (`CURATOR_VERSION`), which the app
  watches to reload itself after a deploy.

## Data

Everything lives in one SQLite database in `CURATOR_DATA`, with a consistent
copy made every night in `backups/` (two weeks kept). Readers' profiles are
versioned there: every daily revision, onboarding and hand edit is a new
version, and the app shows the history and what changed.

## Deploying

The Dockerfile builds the whole thing; mount a volume at `/data`.
