"""Follow wrappers down to the source. A new item is often just a pointer: a
post sharing an article that is itself an advert for someone's Mastodon post,
which holds the real content. While an item's page only points at another
(an embedded post, a syndicated copy, one link it summarises), the item
becomes the page it points at, at most MAX_HOPS down, and the wrappers are
kept as `via` for the reader to see. This runs before an item's id is fixed
(stories.ingest), so dedupe, sightings and story grouping all work on the
original.

Cheapest first: heuristics settle the obvious cases with no model; Jev
decides the grey ones; Claude Haiku is the fallback where Jev is unsure or
fails, at most HAIKU_DAILY calls a day. Each verdict is kept by the
wrapper's URL (the `hops` table) for CACHE_DAYS, so a wrapper seen again is
not judged again. Anything that fails or runs out of time keeps the last hop
reached: never worse than not following."""

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from curator import articles, store
from curator.canon import canonical_url, item_id, short_title
from curator.collect import bluesky_posts, bsky_post, bsky_unwanted, full_text, mastodon_post, quoting
from curator.images import POST_URL, preview_image
from curator.secrets import get_secret

log = logging.getLogger(__name__)
MAX_HOPS = 3
BUDGET = 20  # seconds for a batch's following, after which each item keeps the hop it reached
OWN_WORDS = 60  # fewer words of its own than this: a page is just a pointer
# A post is short anyway (Bluesky's 300 characters are about 50 words), and
# a line of comment is value: only a near-empty one ("look at this", an
# emoji) is a pointer by heuristic; the rest go to Jev.
POST_WORDS = 8
SIMILAR = 0.9  # bge-small: a wrapper this close to its target, and not much longer, only retells it
LONGER = 1.5
VALUE_WORDS = 400  # this much of its own and not close to the target: it has something to say
DISTINCT = 0.8
JEV_VALUE, JEV_POINTER = 0.7, 0.3  # between: Haiku decides
EXTRACT = 1500  # characters of each hop the models compare
MODEL = os.environ.get("CURATOR_FOLLOW_MODEL", "claude-haiku-5-5")
HAIKU_DAILY = int(os.environ.get("CURATOR_FOLLOW_HAIKU_DAILY", "300"))
CACHE_DAYS = 30

# Sites that can't be read without signing in: never followed (an item there
# stays as it is, and a wrapper pointing there is the item).
NEVER = {"twitter.com", "x.com", "t.co", "instagram.com", "facebook.com", "fb.com", "fb.watch", "youtube.com",
         "youtu.be", "threads.net", "threads.com", "linkedin.com", "tiktok.com"}
# Links that aren't what a page points at: sharing, shops and affiliate redirects.
NOT_SOURCES = {"reddit.com", "pinterest.com", "whatsapp.com", "t.me", "telegram.me", "getpocket.com", "flipboard.com",
               "amazon.com", "amzn.to", "ebay.com", "aliexpress.com", "etsy.com", "walmart.com", "bestbuy.com",
               "newegg.com", "skimresources.com", "shareasale.com", "awin1.com", "viglink.com", "linksynergy.com",
               "anrdoezrs.net", "dpbolvw.net", "jdoqocy.com", "tkqlhce.com", "kqzyfj.com", "avantlink.com", "howl.me",
               "geni.us", "patreon.com", "ko-fi.com", "buymeacoffee.com", "paypal.com", "paypal.me", "gravatar.com"}
NOT_SOURCE_PATH = re.compile(r"/(tags?|categor(y|ies)|authors?|topics?|search|share|sharer|intent|login|signup|subscribe|feed)(/|\.|$)"
                             r"|^/@[^/]+/?$|^/users/[^/]+/?$|^/profile/[^/]+/?$", re.I)
SOURCE_MARK = re.compile(r"\b(via|source|h/t|hat tip|spotted (by|on)|originally|first (posted|seen))\b", re.I)
EMBED_CLASSES = ("mastodon-embed", "bluesky-embed", "twitter-tweet", "instagram-media", "tiktok-embed")

MASTODON_POST = re.compile(r"^https://([a-z0-9.-]{1,253})/(?:@[\w.@-]{1,100}|users/[\w.-]{1,100}/statuses)/(\d{1,20})/?$", re.I)
MASTODON_EMBED = re.compile(r"^(https://[a-z0-9.-]{1,253}/@[\w.@-]{1,100}/\d{1,20})/embed/?$", re.I)
BSKY_POST = re.compile(r"^https://bsky\.app/profile/([\w.:%-]{1,253})/post/([a-z0-9]{1,32})/?$", re.I)
BSKY_EMBED = re.compile(r"^https://embed\.bsky\.app/embed/(did:[a-z]+:[\w.:%-]{1,200})/app\.bsky\.feed\.post/([a-z0-9]{1,32})", re.I)
AT_POST = re.compile(r"^at://(did:[a-z]+:[\w.:%-]{1,200})/app\.bsky\.feed\.post/([a-z0-9]{1,32})$", re.I)
DID = re.compile(r"^did:[a-z]+:[\w.:%-]{1,200}$")
HANDLE = re.compile(r"^[a-z0-9.-]{1,253}$", re.I)
WORD = articles.WORD

QUESTION = {"adds_value": {
    "type": "noul",
    "instructions": ("Does `wrapper` add substantial value beyond `target`, the page or post it links to: its own "
                     "reporting, analysis, commentary or context? Or is it mainly a summary, teaser, embed, repost or "
                     "advert for `target`?"),
    "criteria": {"true": "Adds its own reporting, analysis, commentary or context that a reader of `target` alone would miss",
                 "false": "A summary, teaser, embed, repost or advert for `target`: reading `target` instead loses nothing"},
}}
HAIKU_SYSTEM = ("You decide whether a web page or social post (the wrapper) adds substantial value beyond the page or "
                "post it links to (the target): its own reporting, analysis, commentary or context. A summary, teaser, "
                "embed, repost or advert for the target adds none. You see the start of each.")


# ── Where a page points ──────────────────────────────────────────────────────

def host(url: str) -> str:
    try:
        name = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return name.lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")


def site(name: str) -> str:
    """The registrable part of a host name, near enough: example.co.uk, example.com."""
    labels = name.split(".")
    n = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "gov", "edu", "ac") else 2
    return ".".join(labels[-n:])


def followable(url) -> bool:
    if not isinstance(url, str) or not url.startswith(("https://", "http://")) or len(url) > 2000:
        return False
    name = host(url)
    return bool(name) and name not in NEVER and site(name) not in NEVER


def post_url(url: str) -> str | None:
    """A Mastodon or Bluesky post's address from one of its embeds' (or itself)."""
    if m := MASTODON_EMBED.match(url):
        return m.group(1)
    if m := (AT_POST.match(url) or BSKY_EMBED.match(url)):
        return f"https://bsky.app/profile/{m.group(1)}/post/{m.group(2)}"
    return url


class Page(articles.Paragraphs):
    """An article page: its paragraphs (outside embedded posts), the posts it
    embeds, its canonical address, its title and the links in its text."""

    def __init__(self):
        super().__init__()
        self.embed = 0  # blockquote depth inside an embedded post
        self.embeds: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.title: list[str] | None = None
        self.title_text = ""
        self.links: list[tuple[str, bool, bool]] = []  # (href, in the article body, marked as the source)
        self.link: tuple[str, str] | None = None  # the open <a>: href, the paragraph's text before it
        self.anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v or "" for k, v in attrs}
        if self.embed:
            self.embed += tag == "blockquote"
            return
        classes = a.get("class", "").split()
        if tag == "blockquote" and any(c in classes for c in EMBED_CLASSES):
            self.embed = 1
            self.embeds.append(a.get("data-embed-url") or a.get("data-bluesky-uri") or a.get("cite")
                               or f"{classes[0]}:{len(self.embeds)}")  # not followable, but counted
            return
        if tag == "iframe" and (MASTODON_EMBED.match(a.get("src", "")) or BSKY_EMBED.match(a.get("src", ""))):
            self.embeds.append(a["src"])
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:site_name", "og:url", "author") and a.get("content"):
                self.meta.setdefault(key, " ".join(a["content"].split()))
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split() and not self.canonical:
            self.canonical = a.get("href", "").strip()
        elif tag == "title" and not self.title_text:
            self.title = []
        elif tag == "a" and self.current is not None and not self.skip:
            self.link, self.anchor = (a.get("href", "").strip(), "".join(self.current)[-30:]), []
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.embed:
            self.embed -= tag == "blockquote"
            return
        if tag == "title" and self.title is not None:
            self.title_text, self.title = " ".join("".join(self.title).split()), None
        elif tag == "a" and self.link:
            href, before = self.link
            self.links.append((href, bool(self.body), bool(SOURCE_MARK.search(before + " " + "".join(self.anchor)))))
            self.link = None
        super().handle_endtag(tag)

    def handle_data(self, data):
        if self.embed:
            return
        if self.title is not None:
            self.title.append(data)
        if self.link:
            self.anchor.append(data)
        super().handle_data(data)


def is_source(url: str, home: str) -> bool:
    """Whether a link in a page's text could be what it points at: elsewhere,
    and not sharing, a shop, a profile or a tag page."""
    if not followable(url) or site(host(url)) == site(home):
        return False
    name = host(url)
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return name not in NOT_SOURCES and site(name) not in NOT_SOURCES and not NOT_SOURCE_PATH.search(path)


def next_hop(p: Page, base: str) -> tuple[str | None, bool]:
    """What a page points at, and whether that alone makes it a pointer (a
    syndicated copy). In order: a canonical address on another site; the one
    post it embeds; the one place its text links to, or the link it marks as
    its source."""
    home = host(base)
    for raw in (p.canonical, p.meta.get("og:url", "")):
        url = urljoin(base, raw) if raw else ""
        if followable(url) and site(host(url)) != site(home):
            return url, True
    embeds = {post_url(urljoin(base, e)) for e in p.embeds}
    if len(embeds) == 1:
        return embeds.pop(), False  # a tweet or a reel isn't followable: the page stays the item
    if embeds:
        return None, False  # a roundup of posts is its own thing
    links = [(urljoin(base, href), marked) for href, inside, marked in p.links if inside or not p.inside]
    links = [(canonical_url(url), url, marked) for url, marked in links if is_source(url, home)]
    for group in ([x for x in links if x[2]], links):
        if len({c for c, _, _ in group}) == 1:
            return group[0][1], False
    return None, False


# ── Hops ─────────────────────────────────────────────────────────────────────

def words(text: str) -> int:
    return len(WORD.findall(articles.URL.sub(" ", text or "")))


def entry(hop: dict) -> dict:
    """A hop as the reader sees it in `via`."""
    return {"url": hop["url"], "name": hop["name"], "kind": hop["kind"], "author": hop.get("author") or ""}


def article_hop(url: str, html: str, final: str, name: str = "") -> dict | None:
    p = articles.parse(html, Page())
    base = final or url
    text = articles.first_paragraphs(p)
    title = p.meta.get("og:title") or p.title_text
    if not text and not title:
        return None
    nxt, pointer = next_hop(p, base)
    return {"url": base, "kind": "article", "name": p.meta.get("og:site_name") or name or host(base),
            "author": p.meta.get("author", ""), "title": title or short_title(text), "summary": text,
            "image": preview_image(html, base), "poster": None, "quote": None, "text": text,
            "own": sum(words(x) for x in (p.inside or p.all)), "next": nxt, "pointer": pointer,
            "paywalled": articles.paywalled(html)}


def post_hop(post: dict) -> dict:
    full = full_text(post)
    card = post["card"]
    return {"url": post["url"], "kind": post["kind"], "name": post["poster"]["name"] or post["author"],
            "author": post["author"], "title": short_title(post["text"] or full), "summary": full,
            "image": post["photo"] or (card or {}).get("image"), "poster": post["poster"], "quote": post["quote"],
            "text": full, "own_text": post["text"], "own": words(post["text"]), "next": card["url"] if card else (post["quote"] or {}).get("url") or None,
            "pointer": False}


def item_hop(it: dict) -> dict:
    """A post the curator collected, as the first hop (no fetch: it has
    what's needed). Only what it quotes is a next hop: a link card was
    already followed when it was collected."""
    own = it["summary"].removesuffix(quoting(it.get("quote"))).rstrip()
    poster = it.get("poster") or {}
    return {"url": it["url"], "kind": "bluesky" if it["url"].startswith("https://bsky.app/") else "mastodon",
            "name": poster.get("name") or it["author"], "author": it["author"], "title": it["title"],
            "summary": it["summary"], "image": it.get("image"), "poster": it.get("poster"), "quote": it.get("quote"),
            "text": it["summary"], "own_text": own, "own": words(own), "next": (it.get("quote") or {}).get("url") or None, "pointer": False}


def stored_hop(conn, url: str) -> dict | None:
    """An item the curator already has: a wrapper pointing there becomes a
    sighting of it."""
    r = conn.execute("SELECT url, title, summary, author, poster, image, quote FROM items WHERE id = ?", (item_id(url),)).fetchone()
    if r is None:
        return None
    kind = "bluesky" if r["url"].startswith("https://bsky.app/") else "mastodon" if POST_URL.match(r["url"]) else "article"
    return {"url": r["url"], "kind": kind, "name": host(r["url"]), "author": r["author"] or "", "title": r["title"],
            "summary": r["summary"] or "", "image": r["image"], "poster": store.json_or_none(r["poster"]),
            "quote": store.json_or_none(r["quote"]), "text": r["summary"] or "", "own": words(r["summary"]), "next": None,
            "pointer": False, "stored": True}


def mastodon_hop(http: httpx.Client, url: str) -> dict | None:
    """A public Mastodon post, from its own server's API (no token)."""
    m = MASTODON_POST.match(url)
    resp = http.get(f"https://{m.group(1)}/api/v1/statuses/{m.group(2)}")
    if not resp.is_success or not isinstance(s := resp.json(), dict):
        return None
    s = s.get("reblog") or s
    if s.get("sensitive") or s.get("visibility") not in ("public", "unlisted") or (s.get("language") or "en") != "en":
        return None
    post = mastodon_post(s)
    if "@" not in post["author"][1:]:  # its own server names its own accounts without the server
        post["author"] += f"@{m.group(1).lower()}"
    return post_hop(post)


def bluesky_hop(http: httpx.Client, url: str) -> dict | None:
    """A Bluesky post, from api.bsky.app (the public AppView answers 403: #15)."""
    actor, rkey = BSKY_POST.match(url).groups()
    if not DID.match(actor):
        if not HANDLE.match(actor):
            return None
        resp = http.get("https://api.bsky.app/xrpc/com.atproto.identity.resolveHandle", params={"handle": actor})
        actor = resp.json().get("did") if resp.is_success else None
        if not isinstance(actor, str) or not DID.match(actor):
            return None
    p = next(iter(bluesky_posts(http, [f"at://{actor}/app.bsky.feed.post/{rkey}"]).values()), None)
    return None if p is None or bsky_unwanted(p) else post_hop(bsky_post(p))


def load(http: httpx.Client, url: str, pages: dict, name: str = "") -> dict | None:
    """The hop at `url`: a post through its API, else the page (kept in
    `pages`, so the article text isn't fetched again), called `name` if it
    doesn't say. None if unreadable."""
    try:
        if MASTODON_POST.match(url):
            return mastodon_hop(http, url)
        if BSKY_POST.match(url):
            return bluesky_hop(http, url)
        html, final = articles.page(http, url)
        pages[url] = (html, final)
        return article_hop(url, html, final, name) if html else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        log.info("follow %s: %s", url, e.__class__.__name__)
        return None


# ── Deciding ─────────────────────────────────────────────────────────────────

def is_post(hop: dict) -> bool:
    return hop["kind"] in ("mastodon", "bluesky")


def few_words(hop: dict) -> bool:
    return hop["own"] < (POST_WORDS if is_post(hop) else OWN_WORDS)


def own_text(hop: dict) -> str:
    """What the hop says itself (a post's, without what it quotes), for the
    similarity heuristic."""
    return hop.get("own_text", hop["text"])


def heuristic(wrapper: dict, target: dict, sim: float | None) -> str | None:
    """'pointer', 'value', or None for a grey case. A post has value by
    heuristic never: its few words may be the comment that matters."""
    if wrapper["pointer"] or few_words(wrapper):
        return "pointer"
    if sim is None:
        return None
    if sim >= SIMILAR and words(own_text(wrapper)) <= LONGER * max(words(target["text"]), 1):
        return "pointer"
    if wrapper["own"] >= VALUE_WORDS and sim < DISTINCT and not is_post(wrapper):
        return "value"
    return None


def state(hop: dict) -> dict:
    return {"title": hop["title"], "text": hop["text"][:EXTRACT], "link": hop["url"], "author": hop.get("author") or ""}


def jev(pairs: list[tuple[dict, dict]], timeout: float) -> list[tuple[float | None, int]]:
    """Jev's `adds_value` for each (wrapper, target), with its input tokens;
    None where it failed."""
    from typesafe_sdk import AsyncTypeSafeClient

    async def run():
        sem = asyncio.Semaphore(8)
        async with AsyncTypeSafeClient(api_key=get_secret("JEV_API_KEY"), model="jev-latest") as client:
            async def one(w, t):
                async with sem:
                    try:
                        r = (await client.system_one(state={"wrapper": state(w), "target": state(t)}, questions=QUESTION)).model_dump()
                        return float(r["answers"]["adds_value"]["noul"]), int(r["usage"]["input_tokens"])
                    except Exception as e:  # anything Jev can't answer goes to Haiku
                        log.info("jev adds_value: %s", e.__class__.__name__)
                        return None, 0
            return await asyncio.gather(*(one(w, t) for w, t in pairs))

    try:
        return asyncio.run(asyncio.wait_for(run(), timeout))
    except Exception as e:
        log.warning("jev adds_value: %s", e.__class__.__name__)
        return [(None, 0)] * len(pairs)


def haiku(wrapper: dict, target: dict, timeout: float = 15) -> tuple[bool | None, int]:
    """Haiku's verdict (does the wrapper add value?) and the tokens it took;
    None if it gave none."""
    import anthropic
    from pydantic import BaseModel

    class Verdict(BaseModel):
        adds_value: bool

    show = lambda tag, h: f'<{tag} url="{h["url"]}">\n{h["title"]}\n\n{h["text"][:EXTRACT]}\n</{tag}>'
    try:
        client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"), timeout=timeout, max_retries=0)
        r = client.messages.parse(model=MODEL, max_tokens=100, system=HAIKU_SYSTEM, output_format=Verdict, messages=[{
            "role": "user", "content": f"{show('wrapper', wrapper)}\n\n{show('target', target)}\n\nDoes the wrapper add value beyond the target?"}])
    except Exception as e:
        log.warning("haiku adds_value: %s", e.__class__.__name__)
        return None, 0
    tokens = r.usage.input_tokens + r.usage.output_tokens
    return (r.parsed_output.adds_value if r.parsed_output is not None else None), tokens


# ── The cache and the counts ─────────────────────────────────────────────────

def cached(conn, url: str) -> dict | None:
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
    r = conn.execute("SELECT next_url, verdict, via FROM hops WHERE url = ? AND checked >= ?", (canonical_url(url), since)).fetchone()
    return {"next": r["next_url"], "verdict": r["verdict"], "via": store.json_or_none(r["via"])} if r else None


def remember(conn, wrapper: dict, target: dict, verdict: str, method: str) -> None:
    conn.execute("INSERT OR REPLACE INTO hops (url, next_url, verdict, method, checked, via) VALUES (?, ?, ?, ?, ?, ?)",
                 (canonical_url(wrapper["url"]), target["url"], verdict, method,
                  datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(entry(wrapper))))


def day_key(day: date | None = None) -> str:
    return f"follow:{(day or date.today()).isoformat()}"


def counts(conn, day: date | None = None) -> dict:
    return store.get(conn, day_key(day), {})


def describe(c: dict) -> str:
    """A day's counts, for /status."""
    if not c.get("considered"):
        return "nothing new to follow"
    per = (c.get("jev", 0) + c.get("haiku", 0)) / c["considered"]
    return (f"{c.get('followed', 0)} of {c['considered']} new items followed, {c.get('hops', 0)} hops down; "
            f"verdicts: {c.get('heuristic', 0)} by heuristic, {c.get('cached', 0)} cached, {c.get('jev', 0)} Jev, "
            f"{c.get('haiku', 0)} Haiku ({c.get('haiku_tokens', 0):,} tokens; cap {HAIKU_DAILY}, "
            f"{c.get('haiku_capped', 0)} past it); {per:.2f} model calls per new item; "
            f"{c.get('failed', 0)} unreadable, {c.get('timeouts', 0)} out of time; "
            f"{c.get('linked', 0)} put in a story by a link, {c.get('original_leads', 0)} leads kept or given to an original")


def count(conn, stats: Counter) -> None:
    today = Counter(counts(conn))
    today.update(stats)
    store.put(conn, day_key(), dict(today))


# ── Following ────────────────────────────────────────────────────────────────

def points(chain: dict, url: str, verdict: str) -> None:
    """Where the chain's last hop points, and the verdict on it (pointer,
    value, undecided), kept with the item it becomes (`points_to`), so a
    wrapper kept as an item of its own gives way to its original as a
    story's lead (stories.choose_lead)."""
    chain["points_to"] = {"url": canonical_url(url), "verdict": verdict} if followable(url) else None


def follow(conn, items: list[dict], tally: dict | None = None) -> dict[str, tuple[str, str]]:
    """Moves each new item that is just a wrapper down to what it wraps (in
    place: its url, text, author, poster, picture; `via` lists the wrappers,
    outermost first; `seen_url` is where it was seen). Returns the pages it
    fetched ({url: (html, final url)}) for articles.fill_text."""
    started = time.monotonic()
    left = lambda: BUDGET - (time.monotonic() - started)
    late = {"late": True}  # a hop not read for want of time

    def read(url: str, name: str = "") -> dict | None:
        return load(http, url, pages, name) if left() > 0 else late

    pages: dict[str, tuple[str, str]] = {}
    groups: dict[str, list[dict]] = {}
    for it in items:
        if it.get("rule") or not followable(it["url"]) or conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id(it["url"]),)).fetchone():
            continue
        groups.setdefault(canonical_url(it["url"]), []).append(it)
    if not groups:
        return pages
    stats = Counter(considered=len(groups))
    chains = [{"items": group, "hops": [], "seen": {key}, "done": False} for key, group in groups.items()]
    with articles.client() as http, ThreadPoolExecutor(8) as pool:
        # The first hop: a collected post has what's needed; an article is fetched.
        for c in chains:
            it = c["items"][0]
            if memo := cached(conn, it["url"]):  # seen before as a wrapper: no need to read it again
                c["cached"] = memo
                c["hops"].append({"name": host(it["url"]), "kind": "article", **(memo["via"] or {}),
                                  "url": it["url"], "author": it["author"]})
            elif POST_URL.match(it["url"]) and "poster" in it:
                c["hops"].append(item_hop(it))
        todo = [c for c in chains if not c["hops"]]
        feed = lambda c: c["items"][0]["source"].removeprefix("rss:") if c["items"][0]["source"].startswith("rss:") else ""
        for c, hop in zip(todo, pool.map(lambda c: read(c["items"][0]["url"], feed(c)), todo)):
            if hop is None or hop is late:
                stats["timeouts" if hop else "failed"] += 1
                c["done"] = True
            else:
                hop["url"] = c["items"][0]["url"]  # the item stays where it was found, if it isn't moved
                c["hops"].append(hop)

        for _ in range(MAX_HOPS):
            active = [c for c in chains if not c["done"]]
            if not active:
                break
            if left() <= 0:
                stats["timeouts"] += len(active)
                break
            # Where each wrapper points: its cached verdict, else what its page says.
            loading = []
            for c in active:
                w = c["examined"] = c["hops"][-1]
                memo = c.pop("cached", None) or (cached(conn, w["url"]) if len(c["hops"]) > 1 else None)
                if memo:
                    stats["cached"] += 1
                    if memo["verdict"] != "pointer":
                        c["done"] = True
                        points(c, memo["next"], memo["verdict"])
                        continue
                    c["verdict"] = ("pointer", None)
                    nxt = memo["next"]
                else:
                    c["verdict"] = None
                    nxt = w.get("next")
                if not followable(nxt) or (key := canonical_url(nxt)) in c["seen"]:
                    c["done"] = True
                    continue
                c["seen"].add(key)
                points(c, nxt, "pointer" if memo else "undecided")  # until it's decided
                if target := stored_hop(conn, nxt):
                    c["target"] = target
                else:
                    loading.append((c, nxt))
            for (c, _), target in zip(loading, pool.map(lambda cn: read(cn[1]), loading)):
                c["target"] = target
                if target is None or target is late:
                    stats["timeouts" if target else "failed"] += 1
                    c["done"] = True
            ready = [c for c in active if not c["done"]]
            for c in ready:
                if c["target"].get("paywalled"):
                    c["done"] = True  # can't be read: the wrapper is what there is
                    c["points_to"] = None
            ready = [c for c in ready if not c["done"]]

            # Decide: the heuristics, then Jev for the grey cases, then Haiku.
            undecided = [c for c in ready if c["verdict"] is None]
            needs_sim = [c for c in undecided if not c["hops"][-1]["pointer"] and not few_words(c["hops"][-1])]
            sims = {}
            if needs_sim:
                from curator.stories import embed

                texts = []
                for c in needs_sim:
                    w, t = c["hops"][-1], c["target"]
                    texts += [f"{w['title']}. {own_text(w)[:EXTRACT]}", f"{t['title']}. {t['text'][:EXTRACT]}"]
                vecs = embed(texts)
                sims = {id(c): float(vecs[2 * i] @ vecs[2 * i + 1]) for i, c in enumerate(needs_sim)}
            grey = []
            for c in undecided:
                if verdict := heuristic(c["hops"][-1], c["target"], sims.get(id(c))):
                    c["verdict"] = (verdict, "heuristic")
                    stats["heuristic"] += 1
                else:
                    grey.append(c)
            if grey and left() <= 0:
                stats["timeouts"] += len(grey)
            elif grey:
                answers = jev([(c["hops"][-1], c["target"]) for c in grey], max(left(), 3))
                stats["jev"] += len(grey)
                stats["jev_tokens"] += sum(t for _, t in answers)
                unsure = []
                for c, (p, _) in zip(grey, answers):
                    if p is not None and p >= JEV_VALUE:
                        c["verdict"] = ("value", "jev")
                    elif p is not None and p <= JEV_POINTER:
                        c["verdict"] = ("pointer", "jev")
                    else:
                        unsure.append(c)
                room = max(HAIKU_DAILY - counts(conn).get("haiku", 0) - stats["haiku"], 0)
                asking = unsure[:room] if left() > 0 else []
                stats["haiku_capped"] += len(unsure) - len(asking)
                for c, (adds, tokens) in zip(asking, pool.map(lambda c: haiku(c["hops"][-1], c["target"], min(15, max(left(), 3))), asking)):
                    stats["haiku"] += 1
                    stats["haiku_tokens"] += tokens
                    if adds is not None:
                        c["verdict"] = ("value" if adds else "pointer", "haiku")
            # Step down where the wrapper is just a pointer.
            for c in ready:
                verdict, method = c["verdict"] or (None, None)
                if method:
                    remember(conn, c["hops"][-1], c["target"], verdict, method)
                if verdict != "pointer":
                    c["done"] = True  # it has value, or nobody could say: it stays
                    if verdict == "value":
                        c["points_to"]["verdict"] = "value"
                    continue
                c["points_to"] = None
                c["hops"].append(c.pop("target"))
                stats["hops"] += 1
                c["done"] = c["hops"][-1].get("stored", False)  # the original is already an item
            conn.commit()  # the verdicts: don't hold the write lock through the next round's fetches

    for c in chains:
        # A hop reached but not looked at (out of hops, or of time): where it points is undecided.
        last = c["hops"][-1] if c["hops"] else None
        if last and c.get("examined") is not last and not last.get("stored"):
            memo = c.get("cached")
            nxt = memo["next"] if memo else last.get("next")
            if followable(nxt) and canonical_url(nxt) not in c["seen"]:
                points(c, nxt, memo["verdict"] if memo else "undecided")
        for it in c["items"]:
            if c.get("points_to"):
                it["points_to"] = {**c["points_to"], "via": [it["shared_by"]] if it.get("shared_by") and len(c["hops"]) == 1 else []}

    moved = [c for c in chains if len(c["hops"]) > 1]
    for c in moved:
        final = c["hops"][-1]
        wrappers = [entry(h) for h in c["hops"][:-1]]
        for it in c["items"]:
            it.update(seen_url=it["url"], url=final["url"], title=" ".join(final["title"].split())[:300],
                      summary=final["summary"], author=final["author"], poster=final["poster"], quote=final["quote"],
                      image=final["image"] if (final["image"] or "").startswith("https://") else None,
                      via=([it["shared_by"]] if it.get("shared_by") else []) + wrappers)
    stats["followed"] = len(moved)
    if stats["jev_tokens"]:
        key = f"jev_tokens:{date.today().isoformat()}"
        store.put(conn, key, store.get(conn, key, 0) + stats["jev_tokens"])
    count(conn, stats)
    conn.commit()
    if tally is not None:
        tally["followed"] = tally.get("followed", 0) + len(moved)
    log.info("followed %d of %d new items down %d hops (%s) in %.1f s", len(moved), len(groups), stats["hops"],
             ", ".join(f"{k} {v}" for k, v in sorted(stats.items()) if k not in ("considered", "followed", "hops")) or "no calls",
             time.monotonic() - started)
    return pages
