"""Social posts: Mastodon and Bluesky timelines, hashtags and people, read
into items. Polled by collect.py, and shared with social.py (the same in
real time) and follow.py."""

import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

from curator import store
from curator.canon import plain, short_title
from curator.items import UA, item, now
from curator.links import host, one_source

log = logging.getLogger(__name__)


# ── Mastodon ─────────────────────────────────────────────────────────────────
# A reply out of context makes no sense in a feed, so a reply stands for its
# conversation: the curator judges the conversation's first post instead (and
# several replies in one thread become sightings of that one post). Replies
# that share a link are kept as the article they link to, which stands alone.
MAX_ROOT_AGE = timedelta(days=7)  # older conversations aren't news


def has_link_card(s: dict) -> bool:
    card = s.get("card") or {}
    return bool(card.get("url")) and card.get("type") == "link"


# A quote post makes no sense without what it quotes. Mastodon sends the
# quoted post along (`quote.quoted_status`) and puts a "RE: <link>" fallback
# paragraph in the text for apps that can't show it; Bluesky embeds the
# quoted record. The quoted text comes after the post's own, so the reader,
# the story grouping and Jev all see it; it's also kept apart (`quote`) for
# the reader app to show in a box, as Mastodon apps do.
QUOTE_INLINE = re.compile(r'<p class="quote-inline">.*?</p>', re.I | re.S)
LEADING_MENTIONS = re.compile(r"^(?:@[\w.@-]+\s+)+")


def quoted(author: str, name: str, text: str, url: str, image: str | None) -> dict | None:
    text = plain(LEADING_MENTIONS.sub("", text))  # without a reply's addressees
    if not text and not image:
        return None
    return {"author": author, "name": name or author, "text": text, "url": url,
            "image": image if image and image.startswith("https://") else None}


def quoting(q: dict | None) -> str:
    """The quoted post as a paragraph of the quoting post's text."""
    return f"Quoting {q['author']}: {q['text']}" if q and q["text"] else ""


def mastodon_quote(s: dict) -> dict | None:
    """The post a Mastodon quote post quotes, if it can be shown."""
    q = (s.get("quote") or {}).get("quoted_status")
    if not q:
        return None
    text = plain(QUOTE_INLINE.sub("", q.get("content") or ""))
    media = q.get("media_attachments") or []
    if not text:
        text = " ".join(m.get("description") or "" for m in media).strip()
    photo = next((m.get("preview_url") or m.get("url") for m in media if m.get("type") in ("image", "gifv")), None)
    acct = q["account"]
    return quoted("@" + acct["acct"], acct.get("display_name") or "", text, q.get("url") or q.get("uri") or "", photo)


# A post with no link card whose text links to one place that could be a
# source is collected as that article all the same, as if it had one: Mastodon
# fetches a card after the post arrives, and many Bluesky apps never make one.
def text_card(url: str | None) -> dict | None:
    return {"url": url, "title": None, "description": "", "image": None} if url else None


class Links(HTMLParser):
    """The links in a Mastodon post's HTML, but not its mentions and hashtags."""

    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "a" and a.get("href") and not {"mention", "hashtag", "u-url"} & set(classes):
            self.hrefs.append(a["href"].strip())


def mastodon_links(content: str) -> list[str]:
    """The links in a post's text, without a quote post's "RE: <link>"."""
    parser = Links()
    parser.feed(QUOTE_INLINE.sub("", content or ""))
    parser.close()
    return parser.hrefs


def mastodon_post(s: dict) -> dict:
    """A Mastodon post's parts, as items are made from it (here, and when a
    wrapper is followed to a post: follow.py): its own text, its text with
    what it quotes, who posted it, its picture and its link card (else the
    one source its text links to: text_card)."""
    quote = mastodon_quote(s)
    text = plain(QUOTE_INLINE.sub("", s.get("content") or "") if quote else s.get("content"))
    account = s["account"]
    url = s.get("url") or s["uri"]
    links = [u for u in mastodon_links(s.get("content")) if u != (quote or {}).get("url")]
    return {
        "url": url, "kind": "mastodon", "text": text, "quote": quote, "author": "@" + account["acct"],
        "published": datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")),
        "poster": {"name": account.get("display_name") or "", "avatar": account.get("avatar_static") or account.get("avatar") or ""},
        "photo": next((m.get("preview_url") or m.get("url") for m in s.get("media_attachments") or [] if m.get("type") in ("image", "gifv")), None),
        "card": s["card"] if has_link_card(s) else text_card(one_source(links, host(url))),
    }


def full_text(post: dict) -> str:
    """A post's text, then what it quotes."""
    return "\n\n".join(x for x in (post["text"], quoting(post["quote"])) if x)


def shared_by(post: dict) -> dict:
    """The post that shared a link, as a wrapper the reader can see ("via")
    if the item turns out to be something further down (follow.py)."""
    return {"url": post["url"], "kind": post["kind"], "name": post["poster"]["name"] or post["author"], "author": post["author"]}


def from_status(s: dict, source: str, home: bool = False) -> dict | None:
    """The linked article if the post has one, else the post itself. For your
    own home timeline (`home`), followers-only, content-warned and short posts
    are judged too: you follow these people."""
    s = s.get("reblog") or s
    if not home and (s.get("sensitive") or s.get("visibility") not in ("public", "unlisted")):
        return None
    if (s.get("language") or "en") != "en":
        return None
    post = mastodon_post(s)
    if home and not post["text"] and not post["quote"]:  # a photo post: judge it by its descriptions
        post["text"] = " ".join(m.get("description") or "" for m in s.get("media_attachments") or []).strip() or "(a photo)"
    return post_item(post, source, short_ok=home)


def post_item(post: dict, source: str, short_ok: bool = False) -> dict | None:
    """The item for a post (mastodon_post, bsky_post): the article it links
    to, with the post's text in its summary, or the post itself. A short post
    with no link says too little to judge (except where `short_ok`)."""
    full, author, card = full_text(post), post["author"], post["card"]
    if card:
        summary = "\n\n".join(x for x in (card.get("description") or "", f"{author}: {full}") if x)
        it = item(card["url"], card.get("title") or short_title(post["text"] or full), summary,
                  source, author, post["published"], image=card.get("image") or post["photo"])
        it["shared_by"] = shared_by(post)
        if not card.get("title"):
            it["untitled"] = True  # the post's words for now: the article's own title once follow reads it
    elif len(full) < 80 and not short_ok:
        return None
    else:
        it = item(post["url"], short_title(post["text"] or full), full, source, author, post["published"], image=post["photo"])
    return {**it, "quote": post["quote"], "poster": post["poster"]}


def mastodon_client(server: str, token: str, timeout: float = 30) -> httpx.Client:
    return httpx.Client(base_url=f"https://{server}", headers={**UA, "Authorization": f"Bearer {token}"}, timeout=timeout)


def conversation_root(http: httpx.Client, status_id: str, cache: dict) -> dict | None:
    """The first post of a conversation, or None if the server can't see it.
    `http` is a client for the server the status id belongs to."""
    if status_id not in cache:
        resp = http.get(f"/api/v1/statuses/{status_id}/context")
        cache[status_id] = (resp.json().get("ancestors") or [None])[0] if resp.is_success else None
    return cache[status_id]


STATUS_ID = re.compile(r"\d{1,20}")  # Mastodon's ids: snowflakes, as strings


def status_id(value) -> str | None:
    """A status id from server input, or None for anything that isn't one."""
    return value if isinstance(value, str) and STATUS_ID.fullmatch(value) else None


def advance(conn, source: str, sid) -> None:
    """Move a timeline's since_id cursor forward to `sid` (never back: a
    stream event and a gap-filling poll can arrive in either order)."""
    if not (sid := status_id(sid)):
        return
    key = f"since:{source}"  # per source, so per reader for home timelines
    since = status_id(store.get(conn, key))
    if since is None or (len(sid), sid) > (len(since), since):
        store.put(conn, key, sid)
        conn.commit()  # don't hold the write lock across the next fetch


def statuses_items(http: httpx.Client, statuses: list[dict], source: str, roots: dict,
                   home: bool = False) -> list[dict]:
    """Items from statuses, however they came (a timeline poll or a stream).
    `home`: they are from a reader's own home timeline (see from_status)."""
    items = []
    for status in statuses:
        s = status.get("reblog") or status
        if s.get("in_reply_to_id") and not has_link_card(s):
            s = conversation_root(http, s["id"], roots)
            if s is None or now() - datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) > MAX_ROOT_AGE:
                continue
        if it := from_status(s, source, home=home):
            items.append(it)
    return items


def mastodon(conn, http: httpx.Client, path: str, source: str, roots: dict, home: bool = False) -> list[dict]:
    """A timeline's new posts since its cursor (see statuses_items)."""
    since = status_id(store.get(conn, f"since:{source}"))
    params = {"limit": 40, **({"since_id": since} if since else {})}
    resp = http.get(path, params=params)
    resp.raise_for_status()
    statuses = resp.json()
    if not isinstance(statuses, list):
        raise ValueError(f"{path}: not a list of statuses")
    if statuses:
        advance(conn, source, statuses[0].get("id"))
    return statuses_items(http, statuses, source, roots, home)


def mastodon_account_id(conn, http: httpx.Client, acct: str) -> str | None:
    key = f"mastodon-id:{acct}"
    if (known := store.get(conn, key)) is None:
        resp = http.get("/api/v1/accounts/lookup", params={"acct": acct})
        known = resp.json()["id"] if resp.is_success else ""
        store.put(conn, key, known)
        conn.commit()  # don't hold the write lock across the next fetch
    return known or None


def mastodon_account(conn, http: httpx.Client, acct: str, roots: dict) -> list[dict]:
    """A person's public posts, without following them."""
    account = mastodon_account_id(conn, http, acct)
    if not account:
        log.warning("mastodon @%s: no such account", acct)
        return []
    return mastodon(conn, http, f"/api/v1/accounts/{account}/statuses", f"mastodon:@{acct}", roots)


# ── Bluesky ──────────────────────────────────────────────────────────────────

def bsky_post(p: dict) -> dict:
    """A Bluesky post's parts (see mastodon_post). Its link card is `card`
    with `url`, as Mastodon's; else the one source its text links to (from
    its link facets: apps shorten a long link in the text itself)."""
    record = p.get("record", {})
    embed = p.get("embed") or {}
    media = embed.get("media") or embed  # recordWithMedia wraps the media
    ext = media.get("external") or {}
    handle = p["author"]["handle"]
    url = f"https://bsky.app/profile/{handle}/post/{p['uri'].rsplit('/', 1)[-1]}"
    return {
        "url": url, "kind": "bluesky",
        "text": record.get("text", ""), "quote": bsky_quote(embed), "author": "@" + handle,
        "published": datetime.fromisoformat(record.get("createdAt", "").replace("Z", "+00:00")),
        "poster": {"name": p["author"].get("displayName") or "", "avatar": p["author"].get("avatar") or ""},
        "photo": next((i.get("thumb") or i.get("fullsize") for i in media.get("images") or []), None),
        "card": {"url": ext["uri"], "title": ext.get("title"), "description": ext.get("description", ""), "image": ext.get("thumb")}
                if ext.get("uri") else text_card(one_source(bsky_links(record), host(url))),
    }


def bsky_links(record: dict) -> list[str]:
    """The links in a post's text, from its facets."""
    facets = record.get("facets") if isinstance(record.get("facets"), list) else []
    return [f.get("uri") for facet in facets if isinstance(facet, dict) for f in facet.get("features") or []
            if isinstance(f, dict) and f.get("$type") == "app.bsky.richtext.facet#link"]


def bsky_unwanted(p: dict) -> bool:
    """Labelled adult, or not in English."""
    langs = (p.get("record") or {}).get("langs")
    return (any(lbl.get("val") in ("porn", "sexual", "nudity", "graphic-media") for lbl in p.get("labels", []))
            or bool(langs and "en" not in langs))


def from_bsky(p: dict, source: str) -> dict | None:
    if bsky_unwanted(p):
        return None
    return post_item(bsky_post(p), source)


def bsky_quote(embed: dict) -> dict | None:
    """The post a Bluesky quote post embeds, if any."""
    rec = embed.get("record") or {}
    rec = rec if "value" in rec else rec.get("record") or {}  # recordWithMedia wraps the record too
    if not isinstance(rec.get("value"), dict) or not rec.get("author"):
        return None
    handle = rec["author"]["handle"]
    images = [i for e in rec.get("embeds") or [] for i in (e.get("images") or (e.get("media") or {}).get("images") or [])]
    url = f"https://bsky.app/profile/{handle}/post/{rec['uri'].rsplit('/', 1)[-1]}" if rec.get("uri") else ""
    return quoted("@" + handle, rec["author"].get("displayName") or "", rec["value"].get("text", ""), url,
                  next((i.get("thumb") or i.get("fullsize") for i in images), None))


def bluesky_posts(http: httpx.Client, uris: list[str]) -> dict[str, dict]:
    """Posts by at:// uri (getPosts takes 25 at a time)."""
    found = {}
    for i in range(0, len(uris), 25):
        resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.getPosts", params=[("uris", u) for u in uris[i:i + 25]])
        if resp.is_success:
            found.update({p["uri"]: p for p in resp.json().get("posts", [])})
    return found


FUTURE_SLACK = timedelta(minutes=10)  # clocks differ; later than this, a post's date is made up


def bsky_time(value) -> datetime | None:
    """A post's createdAt, which its author's app sets: None if it isn't a
    date or is in the future (one such post would hold the tag's cursor
    there, and nothing newer would be seen until then)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt if dt <= now() + FUTURE_SLACK else None


def bsky_since(conn, key: str, posts: list[dict]) -> list[dict]:
    """The posts newer than the source's cursor, with a sane date; moves
    the cursor to the newest."""
    last = bsky_time(store.get(conn, key, ""))
    dated = [(d, p) for p in posts if isinstance(p, dict) and (d := bsky_time((p.get("record") or {}).get("createdAt")))]
    dated = [(d, p) for d, p in dated if last is None or d > last]
    if dated:
        store.put(conn, key, max(dated, key=lambda dp: dp[0])[1]["record"]["createdAt"])
        conn.commit()  # don't hold the write lock across the next fetch
    return [p for _, p in dated]


def bluesky(conn, http: httpx.Client, tag: str) -> list[dict]:
    resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                    params={"q": f"#{tag}", "sort": "latest", "limit": 50})
    resp.raise_for_status()
    if not isinstance(found := resp.json(), dict):
        raise ValueError("searchPosts: not an object")
    posts = bsky_since(conn, f"bluesky:{tag}", found.get("posts") or [])
    # Text replies stand for their thread's first post (see the Mastodon note).
    is_reply = lambda p: p.get("record", {}).get("reply") and not ((p.get("embed") or {}).get("external") or {}).get("uri")
    roots = bluesky_posts(http, sorted({p["record"]["reply"]["root"]["uri"] for p in posts if is_reply(p)}))
    items = []
    for p in posts:
        if is_reply(p):
            p = roots.get(p["record"]["reply"]["root"]["uri"])
            created = bsky_time(((p or {}).get("record") or {}).get("createdAt"))
            if created is None or now() - created > MAX_ROOT_AGE:
                continue
        if it := from_bsky(p, f"bluesky:#{tag}"):
            items.append(it)
    return items


def bluesky_person(conn, http: httpx.Client, handle: str) -> list[dict]:
    """A person's own Bluesky posts (not their replies)."""
    key = f"bluesky:@{handle}"
    resp = http.get("https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                    params={"actor": handle, "limit": 50, "filter": "posts_no_replies"})
    resp.raise_for_status()
    posts = [f["post"] for f in resp.json().get("feed", []) if not f.get("reason")]  # skip reposts
    posts = bsky_since(conn, key, posts)
    return [it for p in posts if (it := from_bsky(p, f"bluesky:@{handle}"))]
