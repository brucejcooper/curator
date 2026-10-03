"""Collect candidate items for every reader at once: the RSS/Atom feeds,
Mastodon and Bluesky hashtags in anyone's sources (each fetched once), and
each connected reader's own Mastodon home timeline."""

import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import yaml

from curator import people, store
from curator.canon import item_id, plain, short_title
from curator.config import OWNER, USER_AGENT

log = logging.getLogger(__name__)
UA = {"User-Agent": USER_AGENT}
MAX_AGE = timedelta(days=3)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def item(url, title, summary, source, author, published, always_show=False, image=None) -> dict:
    return {
        "url": url, "title": " ".join(plain(title).split())[:300], "summary": plain(summary), "source": source,
        "author": author or "", "published": iso(published), "always_show": int(always_show),
        "image": image if image and image.startswith("https://") else None,
    }


IMG_SRC = re.compile(r'<img[^>]+src="(https://[^"]+)"', re.I)


def rss_image(e) -> str | None:
    """The entry's picture: media:content (largest), media:thumbnail, an image
    enclosure, or the first <img> in its HTML."""
    media = [m for m in e.get("media_content") or [] if m.get("url") and (m.get("medium") == "image" or "image" in m.get("type", "image"))]
    if media:
        return max(media, key=lambda m: int(m.get("width") or 0))["url"]
    if thumbs := e.get("media_thumbnail"):
        return thumbs[0].get("url")
    for enc in e.get("enclosures") or []:
        if enc.get("type", "").startswith("image/"):
            return enc.get("href")
    html = (e.get("content") or [{}])[0].get("value", "") + e.get("summary", "")
    m = IMG_SRC.search(html)
    return m.group(1) if m else None


# ── RSS / Atom ───────────────────────────────────────────────────────────────

def rss(conn, http: httpx.Client, feed: dict) -> list[dict]:
    """Conditional GET with the feed's last ETag / Last-Modified."""
    key = f"rss:{feed['url']}"
    cache = store.get(conn, key, {})
    headers = {k: v for k, v in (("If-None-Match", cache.get("etag")), ("If-Modified-Since", cache.get("modified"))) if v}
    resp = http.get(feed["url"], headers=headers, follow_redirects=True)
    if resp.status_code == 304:
        return []
    resp.raise_for_status()
    store.put(conn, key, {"etag": resp.headers.get("etag"), "modified": resp.headers.get("last-modified")})
    return parse_feed(resp.content, feed["name"], feed.get("always_show", False))


def parse_feed(content: bytes, name: str, always_show: bool = False, max_age: timedelta = MAX_AGE) -> list[dict]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else now()
        if now() - published > max_age:
            continue
        summary = e.get("summary") or (e.get("content") or [{}])[0].get("value", "")
        items.append(item(link, e.get("title", link), summary, f"rss:{name}", e.get("author"),
                          published, always_show, rss_image(e)))
    return items


# ── Mastodon ─────────────────────────────────────────────────────────────────
# A reply out of context makes no sense in a feed, so a reply stands for its
# conversation: the curator judges the conversation's first post instead (and
# several replies in one thread become sightings of that one post). Replies
# that share a link are kept as the article they link to, which stands alone.
MAX_ROOT_AGE = timedelta(days=7)  # older conversations aren't news


def has_link_card(s: dict) -> bool:
    card = s.get("card") or {}
    return bool(card.get("url")) and card.get("type") == "link"


def from_status(s: dict, source: str, home: bool = False) -> dict | None:
    """The linked article if the post has one, else the post itself. For your
    own home timeline (`home`), followers-only, content-warned and short posts
    are judged too: you follow these people."""
    s = s.get("reblog") or s
    if not home and (s.get("sensitive") or s.get("visibility") not in ("public", "unlisted")):
        return None
    if (s.get("language") or "en") != "en":
        return None
    published = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
    text = plain(s.get("content"))
    if home and not text:  # a photo post: judge it by its descriptions
        text = " ".join(m.get("description") or "" for m in s.get("media_attachments") or []).strip() or "(a photo)"
    author = "@" + s["account"]["acct"]
    card = s.get("card") or {}
    photo = next((m.get("preview_url") or m.get("url") for m in s.get("media_attachments") or [] if m.get("type") in ("image", "gifv")), None)
    if has_link_card(s):
        return item(card["url"], card.get("title") or short_title(text), f"{card.get('description', '')}\n\n{author}: {text}",
                    source, author, published, image=card.get("image") or photo)
    if len(text) < 80 and not home:
        return None
    return item(s.get("url") or s["uri"], short_title(text), text, source, author, published, image=photo)


def mastodon_client(server: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=f"https://{server}", headers={**UA, "Authorization": f"Bearer {token}"}, timeout=30)


def conversation_root(http: httpx.Client, status_id: str, cache: dict) -> dict | None:
    """The first post of a conversation, or None if the server can't see it.
    `http` is a client for the server the status id belongs to."""
    if status_id not in cache:
        resp = http.get(f"/api/v1/statuses/{status_id}/context")
        cache[status_id] = (resp.json().get("ancestors") or [None])[0] if resp.is_success else None
    return cache[status_id]


def mastodon(conn, http: httpx.Client, path: str, source: str, roots: dict, home_map: list | None = None) -> list[dict]:
    """A timeline's new posts. For a home timeline, `home_map` collects
    (status id, item id) so the reader app's questions about those posts are
    answered without another fetch."""
    key = f"since:{source}"  # per source, so per reader for home timelines
    since = store.get(conn, key)
    params = {"limit": 40, **({"since_id": since} if since else {})}
    resp = http.get(path, params=params)
    resp.raise_for_status()
    statuses = resp.json()
    if statuses:
        store.put(conn, key, statuses[0]["id"])
    items = []
    home = home_map is not None
    for status in statuses:
        s = status.get("reblog") or status
        if s.get("in_reply_to_id") and not has_link_card(s):
            s = conversation_root(http, s["id"], roots)
            if s is None or now() - datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) > MAX_ROOT_AGE:
                continue
        if it := from_status(s, source, home=home):
            items.append(it)
            if home:
                home_map.append((status["id"], item_id(it["url"])))
    return items


def mastodon_account_id(conn, http: httpx.Client, acct: str) -> str | None:
    key = f"mastodon-id:{acct}"
    if (known := store.get(conn, key)) is None:
        resp = http.get("/api/v1/accounts/lookup", params={"acct": acct})
        known = resp.json()["id"] if resp.is_success else ""
        store.put(conn, key, known)
    return known or None


def mastodon_account(conn, http: httpx.Client, acct: str, roots: dict) -> list[dict]:
    """A person's public posts, without following them."""
    account = mastodon_account_id(conn, http, acct)
    if not account:
        log.warning("mastodon @%s: no such account", acct)
        return []
    return mastodon(conn, http, f"/api/v1/accounts/{account}/statuses", f"mastodon:@{acct}", roots)


# ── Bluesky ──────────────────────────────────────────────────────────────────

def from_bsky(p: dict, source: str) -> dict | None:
    record = p.get("record", {})
    if any(lbl.get("val") in ("porn", "sexual", "nudity", "graphic-media") for lbl in p.get("labels", [])):
        return None
    if record.get("langs") and "en" not in record["langs"]:
        return None
    published = datetime.fromisoformat(record.get("createdAt", "").replace("Z", "+00:00"))
    author = "@" + p["author"]["handle"]
    text = record.get("text", "")
    embed = p.get("embed") or {}
    media = embed.get("media") or embed  # recordWithMedia wraps the media
    ext = media.get("external") or {}
    photo = next((i.get("thumb") or i.get("fullsize") for i in media.get("images") or []), None)
    if ext.get("uri"):
        return item(ext["uri"], ext.get("title") or short_title(text), f"{ext.get('description', '')}\n\n{author}: {text}",
                    source, author, published, image=ext.get("thumb") or photo)
    if len(text) < 80:
        return None
    rkey = p["uri"].rsplit("/", 1)[-1]
    return item(f"https://bsky.app/profile/{p['author']['handle']}/post/{rkey}", short_title(text), text, source, author, published,
                image=photo)


def bluesky_posts(http: httpx.Client, uris: list[str]) -> dict[str, dict]:
    """Posts by at:// uri (getPosts takes 25 at a time)."""
    found = {}
    for i in range(0, len(uris), 25):
        resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.getPosts", params=[("uris", u) for u in uris[i:i + 25]])
        if resp.is_success:
            found.update({p["uri"]: p for p in resp.json().get("posts", [])})
    return found


def bluesky(conn, http: httpx.Client, tag: str) -> list[dict]:
    key = f"bluesky:{tag}"
    last = store.get(conn, key, "")
    resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                    params={"q": f"#{tag}", "sort": "latest", "limit": 50})
    resp.raise_for_status()
    posts = [p for p in resp.json().get("posts", []) if p.get("record", {}).get("createdAt", "") > last]
    # Text replies stand for their thread's first post (see the Mastodon note).
    is_reply = lambda p: p.get("record", {}).get("reply") and not ((p.get("embed") or {}).get("external") or {}).get("uri")
    roots = bluesky_posts(http, sorted({p["record"]["reply"]["root"]["uri"] for p in posts if is_reply(p)}))
    items = []
    for p in posts:
        if is_reply(p):
            p = roots.get(p["record"]["reply"]["root"]["uri"])
            if p is None or now() - datetime.fromisoformat(p["record"]["createdAt"].replace("Z", "+00:00")) > MAX_ROOT_AGE:
                continue
        if it := from_bsky(p, f"bluesky:#{tag}"):
            items.append(it)
    if posts:
        store.put(conn, key, max(p["record"]["createdAt"] for p in posts))
    return items


def shared_sources(profiles: list) -> dict:
    """Everyone's sources, each once. A feed two readers both follow (maybe
    under different names) is fetched once and seen under each name."""
    feeds: dict[str, dict] = {}
    tags, bsky, accounts, people_ = set(), set(), set(), set()
    for p in profiles:
        src = p.sources
        for f in src["rss"]:
            entry = feeds.setdefault(f["url"], {"url": f["url"], "name": f["name"], "names": set()})
            entry["names"].add(f["name"])
        tags |= src["mastodon_tags"]
        bsky |= src["bluesky_tags"]
        accounts |= src["mastodon_accounts"]
        people_ |= src["bluesky_people"]
    return {"rss": list(feeds.values()), "mastodon_tags": tags, "bluesky_tags": bsky,
            "mastodon_accounts": accounts, "bluesky_people": people_}


def bluesky_person(conn, http: httpx.Client, handle: str) -> list[dict]:
    """A person's own Bluesky posts (not their replies)."""
    key = f"bluesky:@{handle}"
    last = store.get(conn, key, "")
    resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                    params={"actor": handle, "limit": 50, "filter": "posts_no_replies"})
    resp.raise_for_status()
    posts = [f["post"] for f in resp.json().get("feed", []) if not f.get("reason")]  # skip reposts
    posts = [p for p in posts if p.get("record", {}).get("createdAt", "") > last]
    if posts:
        store.put(conn, key, max(p["record"]["createdAt"] for p in posts))
    return [it for p in posts if (it := from_bsky(p, f"bluesky:@{handle}"))]


def collect_all(conn, profiles: list) -> list[dict]:
    """New items from everyone's sources, and each connected reader's home timeline."""
    sources = shared_sources(profiles)
    found: list[dict] = []
    with httpx.Client(headers=UA, timeout=30) as http:
        def feed_job(f):
            items = rss(conn, http, f)
            # Seen under each reader's name for this feed.
            return items + [{**it, "source": f"rss:{name}"} for name in sorted(f["names"] - {f["name"]}) for it in items]
        jobs = [(f"rss {f['name']}", lambda f=f: feed_job(f)) for f in sources["rss"]]
        jobs += [(f"bluesky #{t}", lambda t=t: bluesky(conn, http, t)) for t in sorted(sources["bluesky_tags"])]
        jobs += [(f"bluesky @{h}", lambda h=h: bluesky_person(conn, http, h)) for h in sorted(sources["bluesky_people"])]
        for name, job in jobs:
            try:
                found += job()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("%s: %s", name, e)
    # Hashtags and people, read with the owner's account.
    owner = people.mastodon(conn, OWNER)
    if owner and (sources["mastodon_tags"] or sources["mastodon_accounts"]):
        with mastodon_client(owner[0], owner[1]) as http:
            roots: dict = {}
            for t in sorted(sources["mastodon_tags"]):
                try:
                    found += mastodon(conn, http, f"/api/v1/timelines/tag/{t}", f"mastodon:#{t}", roots)
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("mastodon #%s: %s", t, e)
            for a in sorted(sources["mastodon_accounts"]):
                try:
                    found += mastodon_account(conn, http, a, roots)
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    log.warning("mastodon @%s: %s", a, e)
    # Each connected reader's own home timeline.
    stamp = iso(now())
    for p in profiles:
        creds = people.mastodon(conn, p.user)
        if not creds or not p.sources["mastodon_home"]:
            continue
        home_map: list = []
        with mastodon_client(creds[0], creds[1]) as http:
            try:
                found += mastodon(conn, http, "/api/v1/timelines/home", people.home_source(p.user), {}, home_map)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("home timeline for %s: %s", p.user, e)
        conn.executemany("INSERT OR IGNORE INTO home_posts VALUES (?, ?, ?, ?)", [(p.user, sid, iid, stamp) for sid, iid in home_map])
    conn.commit()
    return found
