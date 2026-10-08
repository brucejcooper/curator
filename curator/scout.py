"""The weekly scout: looks for sources a reader would like and doesn't have,
tries them out on paper, and suggests the best few. The reader decides:
add it, try it for a week (its items come to their maybe lane, marked), or
not interested (never suggested again).

Candidates come from:
- outlets that keep covering stories the reader liked, but aren't theirs;
- sites the reader opens articles from (and, if they run the Mac agent,
  sites they visit often);
- people whose posts the reader liked, from hashtags they follow;
- Claude with web search, for aggregators, newsletters, blogs, people and
  hashtags around the reader's live interests.

Each candidate's feed is found and its recent items judged with the
reader's own questions: how many would have made their feed (hit rate), how
many a day, and how many of those were stories they didn't already get
(novelty). The bar is high: a source is suggested only if most of what it
publishes would have made the reader's feed on its own merits (no how-tos,
adverts, hype or chatter), and most of that is new to them. How often it
posts doesn't count against it. Most weeks
nothing clears it, and that's fine: nothing is suggested."""

import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np
import yaml

from curator import people, store
from curator.collect import UA, from_bsky, from_status, mastodon_client, parse_feed
from curator.config import OWNER
from curator.judge import jev_score
from curator.people import Profile, source_key
from curator.secrets import get_secret
from curator.traits import QUESTIONS as TRAIT_QUESTIONS
from curator.traits import ask, count_tokens, item_state

log = logging.getLogger(__name__)
MODEL = os.environ.get("CURATOR_SCOUT_MODEL", "claude-sonnet-5-5")
SUGGEST = 3  # at most this many a week
TEST_AT_MOST = 12  # candidates tried out per reader per week
RESOLVE_AT_MOST = 30  # candidates looked up (many have no readable feed)
# The bar for a suggestion: "genuinely good", not "has some good bits".
# How often a source posts doesn't count against it: rare posting is often
# a sign of care. It's judged on quality alone.
MIN_JUDGED = 4  # recent items judged, so the numbers mean something
MIN_HIT_RATE = 0.5  # at least half would have made the feed
MIN_MARGIN = 0.05  # and the typical hit clears the bar (hits cluster just above it, so this is modest)
MIN_NOVELTY = 0.5  # and most of those are stories the reader doesn't already get
FLAWS = ("rage_bait", "advert", "hype", "chatter", "generic_howto")  # an item with any of these isn't a hit
SAMPLE = 20  # recent items judged per candidate
PAYWALL_CHECKS = 3  # a site's recent articles looked at for a paywall; any one paywalled rules it out
SAMPLE_AGE = timedelta(days=90)  # long enough that someone who posts weekly has enough to judge
TRIAL = timedelta(days=7)
QUIET = timedelta(weeks=12)  # don't re-suggest a dismissed candidate (or one that tested badly) for this long
SAME_STORY = 0.87
KINDS = ("rss", "mastodon_account", "bluesky_account", "mastodon_hashtag", "bluesky_hashtag")
# Places everyone visits that aren't news sources.
NOT_SOURCES = re.compile(r"(^|\.)(google|gmail|youtube|facebook|instagram|x|twitter|linkedin|reddit|github|gitlab|"
                         r"amazon|ebay|apple|icloud|microsoft|office|live|bing|duckduckgo|wikipedia|wikimedia|zoom|"
                         r"slack|notion|figma|claude|anthropic|openai|chatgpt|localhost|awsapps|okta|auth0)\.", re.I)
# A post's own address on Mastodon or Bluesky, not an article it links to.
POST_URL = re.compile(r"^/(@[^/]+/\d+|users/[^/]+/statuses/\d+|notes/\w+|profile/[^/]+/post/\w+)/?$")
# An article's address: a slug of a few hyphenated words somewhere in the path.
ARTICLE_PATH = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+){2,}(?:\.html?)?/?(?:$|[?#])", re.I)


def is_post(url: str) -> bool:
    return bool(POST_URL.match(urlsplit(url).path or ""))


def own_domains() -> set[str]:
    """The curator's own domains (and its sign-in provider's): the reader's
    own services aren't news sources."""
    from curator.config import OIDC_ISSUER, PUBLIC_URL

    return {".".join(h.split(".")[-2:]) for h in (host(PUBLIC_URL), host(OIDC_ISSUER)) if h and "." in h}


def today() -> str:
    return date.today().isoformat()


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


# ── Candidates from what the curator already knows ──────────────────────────

def from_signals(conn, profile: Profile) -> list[dict]:
    """Outlets covering stories the reader liked; sites they open; people
    whose posts they liked."""
    user, keys = profile.user, profile.sources["keys"]
    liked = [i for i, (sign, _) in store.labels(conn, user).items() if sign > 0]
    if not liked:
        return []
    marks = ",".join("?" * len(liked))
    out: list[dict] = []
    # Other coverage of stories they liked, from sources they don't read.
    outlets: Counter = Counter()
    known_feeds: dict[str, tuple[str, str]] = {}
    for r in conn.execute(
        f"""SELECT i.canonical, sg.source FROM items l JOIN items i ON i.story_id = l.story_id AND i.id != l.id
            JOIN sightings sg ON sg.item_id = i.id WHERE l.id IN ({marks})""", liked):
        if r["source"] in keys or r["source"].startswith("mastodon:home:"):
            continue
        if r["source"].startswith("rss:"):
            outlets[r["source"]] += 1
        elif not is_post(r["canonical"]):
            outlets["site:" + host(r["canonical"])] += 1
    for p in people.readers(conn):  # feeds other readers follow have known addresses
        other = people.profile(conn, p)
        for f in other.sources["rss"] if other else []:
            known_feeds[f"rss:{f['name']}"] = (f["url"], f["name"])
    for key, n in outlets.items():
        if n < 2:
            continue
        if key in known_feeds:
            url, name = known_feeds[key]
            out.append({"kind": "rss", "value": url, "name": name, "why": f"covered {n} stories you liked; you don't follow it"})
        elif key.startswith("site:") and not NOT_SOURCES.search(key[5:] + "."):
            out.append({"kind": "site", "value": key[5:], "name": key[5:], "why": f"covered {n} stories you liked"})
    # Sites of articles they liked or opened that came via social posts.
    sites = Counter(host(r[0]) for r in conn.execute(
        f"SELECT canonical FROM items WHERE id IN ({marks}) AND (source LIKE 'mastodon:%' OR source LIKE 'bluesky:%')", liked)
        if not is_post(r[0]))
    for site, n in sites.items():
        if n >= 2 and site and not NOT_SOURCES.search(site + "."):
            out.append({"kind": "site", "value": site, "name": site, "why": f"you liked or opened {n} of its articles via posts"})
    # People whose posts they liked, from hashtags they follow.
    authors = Counter((r["source"].split(":")[0], r["author"]) for r in conn.execute(
        f"SELECT source, author FROM items WHERE id IN ({marks}) AND (source LIKE 'mastodon:#%' OR source LIKE 'bluesky:#%')", liked))
    for (network, author), n in authors.items():
        if n >= 2 and author:
            kind = "mastodon_account" if network == "mastodon" else "bluesky_account"
            out.append({"kind": kind, "value": author.lstrip("@"), "name": author, "why": f"you liked {n} of their posts"})
    return out


def from_browsing(conn, profile: Profile) -> list[dict]:
    """Sites where the reader reads articles on several different days (from
    their Mac agent, after their never-collect rules). Only article pages
    count, so dashboards, sign-in pages and their own services don't."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    mine = own_domains()
    days: dict[str, set] = {}
    articles: dict[str, set] = {}
    for h, t, body in conn.execute("SELECT host, substr(time, 1, 10), body FROM agent_events WHERE user = ? AND time >= ? AND host IS NOT NULL",
                                   (profile.user, since)):
        h = h.lower().removeprefix("www.")
        if ".".join(h.split(".")[-2:]) in mine or NOT_SOURCES.search(h + "."):
            continue
        try:
            url = json.loads(body or "{}").get("url") or ""
        except ValueError:
            continue
        if ARTICLE_PATH.search(urlsplit(url).path + "/") and not is_post(url):
            days.setdefault(h, set()).add(t)
            articles.setdefault(h, set()).add(urlsplit(url).path)
    ranked = sorted(((h, len(d)) for h, d in days.items() if len(d) >= 4 and len(articles[h]) >= 5), key=lambda x: -x[1])
    return [{"kind": "site", "value": h, "name": h, "why": f"you visited it on {n} days this month"} for h, n in ranked[:10]]


# ── Candidates from the web ──────────────────────────────────────────────────

SCOUT_SYSTEM = """\
You find new sources for a personal news feed. From the reader's profile and \
the sources they already have, search the web for places that would add \
something they'd value: news aggregators and curated link sites in their \
areas, active blogs and newsletters with RSS feeds, people worth reading on \
Mastodon or Bluesky, and hashtags with real discussion. Favour sources that \
are active (posting in the last month), substantive, and different from what \
they already follow. Avoid rage bait, SEO content farms, product-deal sites, \
tutorial mills, paywalled sites (they can't read them) and anything they \
said they dislike. Only suggest sources you \
would personally vouch for as outstanding for this reader; each will be \
tested against their profile and only the genuinely good ones shown. Only \
give addresses and handles you found in your searches; never guess one. \
Give up to 8, best first."""

SCOUT_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["site", "rss", "mastodon_account", "bluesky_account", "mastodon_hashtag", "bluesky_hashtag"]},
            "value": {"type": "string", "description": "site address, feed URL, user@server, Bluesky handle, or hashtag without #"},
            "name": {"type": "string"},
            "why": {"type": "string", "description": "why this reader would value it, in under 15 words"},
        },
        "required": ["kind", "value", "name", "why"],
        "additionalProperties": False,
    }}},
    "required": ["candidates"],
    "additionalProperties": False,
}


def from_web(profile: Profile, skip: list[str]) -> list[dict]:
    """Claude, with web search, on the reader's live interests."""
    import anthropic

    src = profile.sources
    have = sorted(src["keys"])
    prompt = (f"<profile>\n{profile.profile_md}\n</profile>\n\nThey already follow: {', '.join(have)}.\n"
              f"Don't suggest these again: {', '.join(skip) or 'nothing'}.\n\nFind new sources for them.")
    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    messages = [{"role": "user", "content": prompt}]
    for _ in range(5):  # resume if the server-side search loop pauses
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=SCOUT_SYSTEM, messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            output_config={"format": {"type": "json_schema", "schema": SCOUT_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    if response.stop_reason != "end_turn":
        log.warning("scout web search for %s stopped: %s (request %s)", profile.user, response.stop_reason, response._request_id)
        return []
    text = next((b.text for b in reversed(response.content) if b.type == "text"), "")
    try:
        found = json.loads(text)["candidates"]
    except (ValueError, KeyError):
        log.warning("scout web search for %s: unreadable answer", profile.user)
        return []
    return [c for c in found if c.get("kind") in ("site", *KINDS) and c.get("value")]


# ── Turning a candidate into a source, with a sample of its items ───────────

FEED_LINK = re.compile(r'<link\b[^>]*type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def find_feed(http: httpx.Client, site: str) -> str | None:
    """A site's feed: its advertised RSS/Atom link, else a few usual places."""
    base = site if site.startswith("http") else f"https://{site}/"
    try:
        page = http.get(base, follow_redirects=True)
        if page.is_success and "xml" in page.headers.get("content-type", "") and b"<item" in page.content[:20000] + b"<entry":
            return str(page.url)
        for tag in FEED_LINK.findall(page.text if page.is_success else ""):
            if m := HREF.search(tag):
                return urljoin(str(page.url), m.group(1))
    except httpx.HTTPError:
        return None
    for path in ("feed", "rss", "feed.xml", "rss.xml", "index.xml", "atom.xml"):
        try:
            r = http.get(urljoin(base, path), follow_redirects=True)
            if r.is_success and re.search(rb"<(rss|feed)\b", r.content[:2000]):
                return str(r.url)
        except httpx.HTTPError:
            continue
    return None


def resolve(conn, http: httpx.Client, owner_http: httpx.Client | None, c: dict) -> dict | None:
    """{kind, value, name, items} ready to judge, or None if it can't be read."""
    kind, value = c["kind"], c["value"].strip()
    cutoff = datetime.now(timezone.utc) - SAMPLE_AGE
    recent = lambda items: [i for i in items if datetime.fromisoformat(i["published"]) >= cutoff][:SAMPLE]
    try:
        if kind in ("site", "rss"):
            url = value if kind == "rss" else find_feed(http, value)
            if not url:
                return None
            resp = http.get(url, follow_redirects=True)
            resp.raise_for_status()
            import feedparser

            name = c.get("name") if kind == "rss" else (feedparser.parse(resp.content).feed.get("title") or host(url))
            items = recent(parse_feed(resp.content, name or url, max_age=SAMPLE_AGE))
            return {"kind": "rss", "value": url, "name": name or url, "items": items}
        if kind == "mastodon_account" and owner_http:
            acct = value.lstrip("@")
            r = owner_http.get("/api/v1/accounts/lookup", params={"acct": acct})
            if not r.is_success:
                return None
            statuses = owner_http.get(f"/api/v1/accounts/{r.json()['id']}/statuses",
                                      params={"limit": 40, "exclude_replies": "true"}).raise_for_status().json()
            items = recent([i for s in statuses if (i := from_status(s, f"mastodon:@{acct}"))])
            return {"kind": kind, "value": acct, "name": c.get("name") or f"@{acct}", "items": items}
        if kind == "mastodon_hashtag" and owner_http:
            tag = value.lstrip("#")
            statuses = owner_http.get(f"/api/v1/timelines/tag/{tag}", params={"limit": 40}).raise_for_status().json()
            items = recent([i for s in statuses if (i := from_status(s, f"mastodon:#{tag}"))])
            return {"kind": kind, "value": tag, "name": f"#{tag} on Mastodon", "items": items}
        if kind == "bluesky_account":
            handle = value.lstrip("@")
            feed = http.get("https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                            params={"actor": handle, "limit": 50, "filter": "posts_no_replies"}).raise_for_status().json()
            items = recent([i for f in feed.get("feed", []) if not f.get("reason") and (i := from_bsky(f["post"], f"bluesky:@{handle}"))])
            return {"kind": kind, "value": handle, "name": c.get("name") or f"@{handle}", "items": items}
        if kind == "bluesky_hashtag":
            tag = value.lstrip("#")
            posts = http.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                             params={"q": f"#{tag}", "sort": "latest", "limit": 50}).raise_for_status().json().get("posts", [])
            items = recent([i for p in posts if not p.get("record", {}).get("reply") and (i := from_bsky(p, f"bluesky:#{tag}"))])
            return {"kind": kind, "value": tag, "name": f"#{tag} on Bluesky", "items": items}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.info("scout: couldn't read %s %s: %s", kind, value, e)
    return None


# ── Trying a candidate out on paper ──────────────────────────────────────────

def recent_matrix(conn, profile: Profile) -> np.ndarray:
    """Embeddings of what the reader already gets (last 30 days)."""
    keys = sorted(profile.sources["keys"])
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    rows = conn.execute(
        f"""SELECT DISTINCT i.embedding FROM items i JOIN sightings s ON s.item_id = i.id
            WHERE s.source IN ({','.join('?' * len(keys))}) AND i.fetched >= ? AND i.embedding IS NOT NULL""",
        (*keys, since)).fetchall()
    return np.stack([store.vec(r[0]) for r in rows]) if rows else np.zeros((0, 384), np.float32)


def is_paywalled(http: httpx.Client, items: list[dict]) -> bool:
    """Whether any of a site's recent articles is marked as paywalled."""
    from curator.articles import page, paywalled

    urls = [it["url"] for it in items if not is_post(it["url"])][:PAYWALL_CHECKS]
    return any(paywalled(page(http, u)[0]) for u in urls)


def evaluate(conn, profile: Profile, found: dict, known: np.ndarray) -> dict | None:
    """Hit rate, volume and novelty of a candidate's recent items for this reader."""
    from curator.stories import embed, text_of

    items = found["items"]
    if len(items) < 3:
        return None
    rows = [{"id": str(n), **it, "canonical": it["url"]} for n, it in enumerate(items)]
    questions = {**TRAIT_QUESTIONS, **profile.questions}
    results = asyncio.run(ask(rows, questions, lambda r: {"item": item_state(r), "reader": {"profile": profile.profile_md}}))
    count_tokens(conn, results)
    floor = profile.sources["min_score"]["jev"]
    vectors = embed([text_of(it) for it in items])
    novel = (known @ vectors.T).max(axis=0) < SAME_STORY if len(known) else np.ones(len(items), bool)
    scored = []
    for (n, resp), it, is_new in zip(results, items, novel):
        if resp:
            a = resp["answers"]
            score = jev_score(a, profile.questions, a)
            flawed = any(a.get(f, {}).get("noul", 0.0) >= 0.5 for f in FLAWS)
            if a.get("english", {}).get("noul", 1.0) >= 0.3:
                scored.append((-1.0 if flawed else score, bool(is_new), it))
    if len(scored) < 3:
        return None
    dates = [datetime.fromisoformat(it["published"]) for it in items]
    span = max((max(dates) - min(dates)).total_seconds() / 86400, 1.0)
    per_day = min(len(items) / span, 50.0)
    hits = [(s, new, it) for s, new, it in scored if s >= floor]
    hit_rate = len(hits) / len(scored)
    novel_hits = [(s, it) for s, new, it in hits if new]
    novelty = len(novel_hits) / len(hits) if hits else 0.0
    useful_per_day = per_day * hit_rate * novelty
    margin = float(np.median([s - floor for s, _, _ in hits])) if hits else 0.0
    samples = [{"title": it["title"], "url": it["url"], "score": round(s, 2)} for s, it in sorted(novel_hits, key=lambda x: -x[0])[:3]]
    return {"per_day": round(per_day, 1), "hit_rate": round(hit_rate, 2), "novelty": round(novelty, 2),
            "useful_per_day": round(useful_per_day, 2), "margin": round(margin, 2), "judged": len(scored), "samples": samples}


def worth_suggesting(stats: dict | None) -> bool:
    """Only the genuinely good: most of what it publishes would make the
    reader's feed, comfortably, and most of that is new to them. However
    rarely it posts."""
    return (bool(stats) and stats.get("judged", 0) >= MIN_JUDGED and stats["hit_rate"] >= MIN_HIT_RATE
            and stats["novelty"] >= MIN_NOVELTY and stats.get("margin", 0.0) >= MIN_MARGIN
            and bool(stats["samples"]))


# ── The weekly run ───────────────────────────────────────────────────────────

def already(conn, user: str) -> set[tuple[str, str]]:
    """(kind, value) the reader has, or was suggested recently, or turned down."""
    quiet = (datetime.now(timezone.utc) - QUIET).isoformat()
    rows = conn.execute("SELECT kind, value FROM suggestions WHERE user = ? AND (status != 'dismissed' OR decided >= ? OR created >= ?)",
                        (user, quiet, quiet)).fetchall()
    return {(r[0], r[1].lower()) for r in rows}


def scout(conn, user: str, use_web: bool = True) -> list[int]:
    """Finds, tries and suggests new sources for one reader. Returns the new suggestions' ids."""
    profile = people.profile(conn, user)
    if not profile:
        return []
    src = profile.sources
    have_urls = {f["url"].lower() for f in src["rss"]}
    have_hosts = {host(u) for u in have_urls}
    seen = already(conn, user)
    # Sites are suggested as their feed's address: match candidates by host too.
    seen_hosts = {host(v if "://" in v else f"https://{v}") for k, v in seen if k == "rss"}
    candidates = []
    if use_web:
        try:
            candidates += from_web(profile, sorted(v for _, v in seen))
        except Exception:
            log.exception("scout web search for %s", user)
    # Web finds first (already vetted), then feeds and people, then bare sites.
    order = {"rss": 0, "mastodon_account": 1, "bluesky_account": 1, "mastodon_hashtag": 2, "bluesky_hashtag": 2, "site": 3}
    candidates += sorted(from_signals(conn, profile) + from_browsing(conn, profile), key=lambda c: order.get(c["kind"], 4))
    # One of each, nothing they have or were offered recently.
    unique, keys_seen = [], set()
    for c in candidates:
        k = (c["kind"], c["value"].lower().lstrip("@#"))
        if k in keys_seen or (c["kind"] in ("site", "rss") and (c["value"].lower() in have_urls or host(c["value"]) in have_hosts
                                                                  or host(f"https://{c['value']}") in have_hosts)):
            continue
        if c["kind"] in KINDS and source_key(c["kind"], c["value"].lstrip("@#"), c.get("name")) in src["keys"]:
            continue
        if any(v == k[1] for _, v in seen):
            continue
        if c["kind"] in ("site", "rss") and host(c["value"] if "://" in c["value"] else f"https://{c['value']}") in seen_hosts:
            continue
        keys_seen.add(k)
        unique.append(c)
    owner = people.mastodon(conn, OWNER)
    known = recent_matrix(conn, profile)
    tested = []
    with httpx.Client(headers=UA, timeout=20) as http:
        owner_http = mastodon_client(owner[0], owner[1]) if owner else None
        try:
            for c in unique[:RESOLVE_AT_MOST]:
                if len(tested) >= TEST_AT_MOST:
                    break
                found = resolve(conn, http, owner_http, c)
                if not found:
                    log.info("scout %s: no readable feed for %s %s", user, c["kind"], c["value"])
                    continue
                if (found["kind"], found["value"].lower()) in seen or found["value"].lower() in have_urls:
                    continue
                if found["kind"] == "rss" and is_paywalled(http, found["items"]):
                    tested.append((c, found, {"paywalled": True}))
                    log.info("scout %s: %s is paywalled", user, found["value"])
                    continue
                stats = evaluate(conn, profile, found, known)
                tested.append((c, found, stats))
                log.info("scout %s: tried %s %s: %s", user, found["kind"], found["value"],
                         {k: v for k, v in (stats or {}).items() if k != "samples"} or "too few recent items")
        finally:
            if owner_http:
                owner_http.close()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    good = sorted((t for t in tested if worth_suggesting(t[2])), key=lambda t: (-t[2]["hit_rate"], -t[2]["margin"]))[:SUGGEST]
    ids = []
    for c, found, stats in good:
        cur = conn.execute("INSERT INTO suggestions (user, kind, value, name, why, evidence, score, status, created) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
                           (user, found["kind"], found["value"], found["name"], c["why"], json.dumps(stats), stats["hit_rate"], now))
        ids.append(cur.lastrowid)
    # Remember the ones that tested badly, so they aren't tried again soon.
    for c, found, stats in tested:
        if (c, found, stats) not in good:
            conn.execute("INSERT INTO suggestions (user, kind, value, name, why, evidence, score, status, created, decided) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 'dismissed', ?, ?)",
                         (user, found["kind"], found["value"], found["name"],
                          "tested: paywalled" if (stats or {}).get("paywalled") else "tested: not enough for you",
                          json.dumps(stats), 0, now, now))
    conn.commit()
    log.info("scout %s: %d candidates, %d tested, %d suggested", user, len(unique), len(tested), len(ids))
    return ids


def review_trials(conn, user: str) -> None:
    """Trials that have run their week: how they went, for the reader to keep or drop."""
    profile = people.profile(conn, user)
    if not profile:
        return
    for entry in (yaml.safe_load(profile.sources_yaml) or {}).get("learned") or []:
        until = entry.get("trial_until")
        if not until or until > today():
            continue
        key = source_key(entry["kind"], str(entry["value"]), entry.get("name"))
        if conn.execute("SELECT 1 FROM suggestions WHERE user = ? AND status = 'review' AND value = ?", (user, str(entry["value"]))).fetchone():
            continue
        start = (date.fromisoformat(until) - TRIAL).isoformat()
        ids = [r[0] for r in conn.execute("SELECT DISTINCT item_id FROM sightings WHERE source = ? AND seen >= ?", (key, start))]
        marks = ",".join("?" * len(ids)) or "''"
        signals = store.labels(conn, user)
        ups = sum(1 for i in ids if signals.get(i, (0,))[0] > 0)
        downs = sum(1 for i in ids if signals.get(i, (0,))[0] < 0)
        opened = conn.execute(f"SELECT COUNT(DISTINCT item_id) FROM events WHERE user = ? AND kind = 'open' AND item_id IN ({marks})",
                              (user, *ids)).fetchone()[0] if ids else 0
        stats = {"items": len(ids), "liked": ups, "disliked": downs, "opened": opened}
        conn.execute("INSERT INTO suggestions (user, kind, value, name, why, evidence, score, status, created) VALUES (?, ?, ?, ?, ?, ?, 0, 'review', ?)",
                     (user, entry["kind"], str(entry["value"]), entry.get("name") or str(entry["value"]),
                      "your week's trial is over", json.dumps(stats), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()


# ── The reader's decision ────────────────────────────────────────────────────

def decide(conn, user: str, suggestion_id: int, action: str) -> dict:
    """add | try | dismiss for a suggestion; keep | drop after a trial."""
    row = conn.execute("SELECT * FROM suggestions WHERE id = ? AND user = ?", (suggestion_id, user)).fetchone()
    if not row:
        raise LookupError("no such suggestion")
    allowed = {"new": ("add", "try", "dismiss"), "review": ("keep", "drop")}.get(row["status"], ())
    if action not in allowed:
        raise ValueError(f"can't {action} a suggestion that is {row['status']}")
    profile = people.profile(conn, user)
    kind, value, name = row["kind"], row["value"], row["name"]
    entry = {"kind": kind, "value": value, **({"name": name} if kind == "rss" else {}),
             "reason": f"suggested: {row['why']}", "added": today()}
    label = name or value
    if action in ("add", "try"):
        if action == "try":
            entry["trial_until"] = (date.today() + TRIAL).isoformat()
        change = lambda learned: [e for e in learned if str(e.get("value")) != value] + [entry]
        summary = f"{'Trying' if action == 'try' else 'Added'} {label}"
    elif action == "keep":
        change = lambda learned: [{k: v for k, v in e.items() if k != "trial_until"} if str(e.get("value")) == value else e for e in learned]
        summary = f"Kept {label} after a trial"
    elif action == "drop":
        change = lambda learned: [e for e in learned if str(e.get("value")) != value]
        summary = f"Dropped {label} after a trial"
    else:
        change, summary = None, None
    if change:
        people.add_version(conn, user, "user", summary, profile.profile_md,
                           people.edit_learned(profile.sources_yaml, change), profile.questions_yaml)
    status = {"add": "accepted", "try": "trial", "dismiss": "dismissed", "keep": "accepted", "drop": "dismissed"}[action]
    conn.execute("UPDATE suggestions SET status = ?, decided = ? WHERE id = ?",
                 (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), suggestion_id))
    if action in ("keep", "drop"):  # the original 'trial' suggestion is settled too
        conn.execute("UPDATE suggestions SET status = ?, decided = ? WHERE user = ? AND value = ? AND status = 'trial'",
                     (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), user, value))
    conn.commit()
    return {"status": status}


def listing(conn, user: str) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM suggestions WHERE user = ? ORDER BY created DESC LIMIT 200", (user,))]
    for r in rows:
        r["evidence"] = json.loads(r["evidence"] or "{}")
    return {"open": [r for r in rows if r["status"] in ("new", "review")],
            "trying": [r for r in rows if r["status"] == "trial"],
            "history": [r for r in rows if r["status"] in ("accepted", "dismissed") and not (r["why"] or "").startswith("tested:")][:50]}
