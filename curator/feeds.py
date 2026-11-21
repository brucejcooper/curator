"""RSS, Atom and JSON Feed: fetching each feed as often as it changes,
reading it into items, and the upkeep of its address (moved and dead
feeds)."""

import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from curator import store
from curator.canon import plain
from curator.config import CYCLE
from curator.items import item, iso, now
from curator.sponsors import same_name, sponsor_line, sponsored_label, without_sponsor_line

MAX_AGE = timedelta(days=3)


IMG_SRC = re.compile(r'<img[^>]+src="(https://[^"]+)"', re.I)
IMG_TITLE = re.compile(r'<img\b[^>]*?\btitle\s*=\s*("[^"]*"|\'[^\']*\')[^>]*>', re.I)


def hover_text(html: str) -> str:
    """A picture's hover text kept as a paragraph: for a comic (xkcd) it's the
    punchline. Other tags go when the HTML becomes text."""
    return IMG_TITLE.sub(lambda m: f"<p>{m.group(1)[1:-1]}</p>", html or "")


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
# Each feed is fetched as often as it changes, within the fast loop: about
# half the usual gap between its entries, between one cycle and FEED_CEILING.
# The publisher's hints (<ttl>, Cache-Control: max-age, Expires, Retry-After)
# are a lower bound: never fetched sooner than they ask, up to HINT_CAP.
# Errors back off, doubling with each in a row. The feed's state is its
# rss:{url} kv record, along with its ETag / Last-Modified.
FEED_CEILING = timedelta(hours=4)
HINT_CAP = timedelta(days=1)
DATED_ENTRIES = 10  # the most recent entries whose gaps set the interval
# The ones the publisher sets aren't moved earlier for being nearly due.
HINTED = ("ttl", "max-age", "expires", "retry-after")


def parse_seconds(value) -> float | None:
    """A number of seconds (or <ttl>'s minutes), at most HINT_CAP's worth;
    None for what isn't one (nan and inf included)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(n, 0), HINT_CAP.total_seconds()) if math.isfinite(n) else None


def http_date(value: str | None) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cache_hint(headers, now_: datetime) -> tuple[timedelta, str] | None:
    """How long the response says it stays fresh: max-age, else Expires.
    no-cache and no-store say nothing about when to come back."""
    control = {k.strip().lower(): v.strip() for k, _, v in
               (d.partition("=") for d in (headers.get("cache-control") or "").split(",")) if k.strip()}
    if "no-cache" in control or "no-store" in control:
        return None
    if (age := parse_seconds(control.get("max-age", "").strip('"'))) is not None:
        return timedelta(seconds=age), "max-age"
    if expires := http_date(headers.get("expires")):
        return expires - now_, "expires"
    return None


def retry_after(headers, now_: datetime) -> timedelta | None:
    """Retry-After, in seconds or as an HTTP date."""
    value = (headers.get("retry-after") or "").strip()
    if (secs := parse_seconds(value)) is not None:
        return timedelta(seconds=secs)
    when = http_date(value)
    return when - now_ if when else None


def clamp(interval: timedelta) -> timedelta:
    return min(max(interval, CYCLE), FEED_CEILING)


def from_dates(dates: list[datetime]) -> timedelta | None:
    """Half the median gap between the most recent entries, if they're dated."""
    recent = sorted(dates, reverse=True)[:DATED_ENTRIES]
    gaps = sorted(a - b for a, b in zip(recent, recent[1:]))
    return clamp(gaps[len(gaps) // 2] / 2) if gaps else None


def schedule(state: dict, status: int | None, headers, dates: list[datetime] | None, marker: str | None,
             ttl: str | None, now_: datetime) -> dict:
    """A feed's state after a fetch: when to fetch it next, and why.
    `status` is the HTTP status (None: it couldn't be fetched or read);
    `dates` the entries' dates and `marker` what identifies the newest entry
    (on a 200); `ttl` the feed's <ttl>, in minutes."""
    state = dict(state)
    interval = timedelta(seconds=state.get("interval") or CYCLE.total_seconds())
    if status in (200, 304):
        state["errors"] = 0
        if status == 200:
            new = marker != state.get("marker")
            state.update(marker=marker, dated=len(dates or []) >= 2, ttl=parse_seconds(ttl))
            state["last"] = "200-new" if new else "200"
            if (dated := from_dates(dates or [])) is not None:
                interval = dated
            else:
                interval = clamp(interval / 2 if new else interval * 1.5)
        else:
            state["last"] = "304"
            if not state.get("dated"):
                interval = clamp(interval * 1.5)
        wait, reason = interval, "adaptive"
        hints = [(timedelta(minutes=state["ttl"]), "ttl")] if state.get("ttl") else []
        if hint := cache_hint(headers, now_):
            hints.append(hint)
        for hint, why in hints:
            hint = min(hint, HINT_CAP)
            if hint > wait:
                wait, reason = hint, why
    else:
        state["errors"] = state.get("errors", 0) + 1
        state["last"] = str(status or "error")
        wait, reason = min(interval * 2 ** state["errors"], HINT_CAP), "error"
        if status in (429, 503) and (later := retry_after(headers, now_)) is not None:
            wait, reason = min(max(later, timedelta(0)), HINT_CAP), "retry-after"
    state.update(interval=interval.total_seconds(), next=iso(now_ + wait), reason=reason, fetched=iso(now_))
    return state


def due(state: dict, now_: datetime) -> bool:
    """Whether a feed is to be fetched in the cycle starting `now_`. Its own
    schedule is met by the cycle nearest to it (cycles drift by seconds, so
    "at most now" would skip one); a publisher's, by the first one after."""
    if not state.get("next"):
        return True
    slack = timedelta(0) if state.get("reason") in HINTED else CYCLE / 2
    return datetime.fromisoformat(state["next"]) <= now_ + slack


def rss(conn, http: httpx.Client, feed: dict, now_: datetime | None = None) -> list[dict]:
    """Conditional GET with the feed's last ETag / Last-Modified; schedules
    its next fetch, whatever the answer, and notes whether it has moved or
    gone (upkeep)."""
    key = f"rss:{feed['url']}"
    state = store.get(conn, key, {})
    now_ = now_ or now()
    headers = {k: v for k, v in (("If-None-Match", state.get("etag")), ("If-Modified-Since", state.get("modified"))) if v}
    items, status, resp_headers, dates, marker, ttl, gone = [], None, {}, None, None, None, None
    try:
        resp = http.get(feed["url"], headers=headers, follow_redirects=True)
        status, resp_headers = resp.status_code, resp.headers
        if resp.is_success:  # a 203 from a proxy is a feed too
            status = 200
            parsed = parse_any(resp.content, resp.headers.get("content-type"))
            if (parsed.bozo or not parsed.get("version")) and not parsed.entries:  # an HTML page has none
                status, gone = None, "not a feed"
                raise ValueError(f"not a feed: {parsed.get('bozo_exception')}")
            state.update(etag=resp.headers.get("etag"), modified=resp.headers.get("last-modified"))
            dates = [d for e in parsed.entries if (d := entry_date(e)) and d <= now_ + timedelta(days=1)]
            first = parsed.entries[0] if parsed.entries else {}
            marker = iso(max(dates)) if dates else first.get("id") or first.get("link")
            ttl = parsed.feed.get("ttl")
            items = feed_items(parsed, feed["name"], feed.get("always_show", False))
        elif status != 304:
            gone = str(status) if status in (404, 410) else None
            resp.raise_for_status()  # 1xx, 3xx and errors
        state = upkeep(state, resp, gone, now_)
    finally:
        # The error still goes up, to be logged; the feed waits longer.
        if gone:
            state = upkeep(state, None, gone, now_)
        store.put(conn, key, {**schedule(state, status, resp_headers, dates, marker, ttl, now_), "name": feed["name"]})
        conn.commit()  # don't hold the write lock across the next fetch
    return items


# Upkeep of a feed's address. A feed that answers only through permanent
# redirects (301/308) has moved: `moved_to` is where, since `moved_since`
# (/status shows it; the daily revision rewrites a learned source once it
# has stood for MOVE_SETTLED). It is still fetched at the profile's address,
# so its state stays under that key until then. A feed that answers 404, 410
# or something that isn't a feed is `failing` since `failing_since`; /status
# flags it after DEAD_AFTER. Other errors (5xx, timeouts) pass, and don't
# start the clock; any answer clears it.
PERMANENT = (301, 308)
MOVE_SETTLED = timedelta(days=7)
DEAD_AFTER = timedelta(days=7)
UPKEEP = ("moved_to", "moved_since", "failing", "failing_since")


def upkeep(state: dict, resp: httpx.Response | None, gone: str | None, now_: datetime) -> dict:
    """The feed's state after an answer (`resp`, a 200 or 304) or a sign it
    has gone (`gone`: "404", "410" or "not a feed")."""
    state = dict(state)
    if gone:
        state.setdefault("failing_since", iso(now_))
        state["failing"] = gone
        return state
    state.pop("failing_since", None)
    state.pop("failing", None)
    hops = [r.status_code for r in resp.history]
    if hops and all(h in PERMANENT for h in hops):
        if state.get("moved_to") != str(resp.url):
            state.update(moved_to=str(resp.url), moved_since=iso(now_))
    else:
        state.pop("moved_to", None)
        state.pop("moved_since", None)
    return state


def entry_date(e) -> datetime | None:
    stamp = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None


def parse_feed(content: bytes, name: str, always_show: bool = False, max_age: timedelta = MAX_AGE,
               content_type: str | None = None) -> list[dict]:
    return feed_items(parse_any(content, content_type), name, always_show, max_age)


# ── JSON Feed ────────────────────────────────────────────────────────────────
# feedparser reads no JSON Feed (https://jsonfeed.org, 1.0 and 1.1), so it's
# read here, into the shape feedparser gives the rest of this module.
JSON_TYPES = ("application/feed+json", "application/json")
JSON_FEED = "https://jsonfeed.org/version/"


def parse_any(content: bytes, content_type: str | None = None):
    """A feed, RSS, Atom or JSON Feed, as feedparser shapes it. JSON Feed by
    its content type or, served as anything, by its opening brace."""
    as_json = (content_type or "").split(";")[0].strip().lower() in JSON_TYPES
    if as_json or content.lstrip()[:1] == b"{":
        if (parsed := json_feed(content)) is not None:
            return parsed
        if as_json:
            return feedparser.FeedParserDict(feed={}, entries=[], bozo=True, bozo_exception="not a JSON Feed")
    return feedparser.parse(content)


def json_date(value) -> tuple | None:
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).timetuple()


def json_feed(content: bytes):
    """A JSON Feed document, or None if it isn't one."""
    try:
        doc = json.loads(content)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not str(doc.get("version", "")).startswith(JSON_FEED):
        return None
    entries = []
    for i in doc.get("items") or []:
        if not isinstance(i, dict):
            continue
        authors = i.get("authors") if isinstance(i.get("authors"), list) else []
        author = (authors or [i.get("author")])[0]  # 1.1, else 1.0
        text = i.get("content_text")
        image = i.get("image") or i.get("banner_image")
        entry = dict(
            id=str(i.get("id") or ""), link=i.get("url") or i.get("external_url"), title=i.get("title"),
            summary=i.get("content_html") or i.get("summary") or (html.escape(text) if text else None),
            published_parsed=json_date(i.get("date_published")), updated_parsed=json_date(i.get("date_modified")),
            author=author.get("name") if isinstance(author, dict) else None,
            tags=[{"term": str(t)} for t in i.get("tags") or []],
            media_thumbnail=[{"url": image}] if isinstance(image, str) else None,
        )
        entries.append(feedparser.FeedParserDict({k: v for k, v in entry.items() if v}))
    return feedparser.FeedParserDict(feed=feedparser.FeedParserDict(title=doc.get("title") or "", ttl=None),
                                     entries=entries, bozo=False, version="json")


def feed_items(parsed, name: str, always_show: bool = False, max_age: timedelta = MAX_AGE) -> list[dict]:
    items, site = [], parsed.feed.get("title") or ""
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        published = entry_date(e) or now()
        if now() - published > max_age:
            continue
        summary = e.get("summary") or (e.get("content") or [{}])[0].get("value", "")
        reason = sponsored_label(e)
        if series := sponsor_line(plain(summary)):
            if same_name(series, site) or same_name(series, name):
                summary = without_sponsor_line(summary)
            else:
                reason = reason or f'column "{series}"'
        it = item(link, e.get("title", link), hover_text(summary), f"rss:{name}", e.get("author"),
                  published, always_show, rss_image(e))
        # Dropped even from an always-show feed: the reader chose the feed,
        # nobody chose its paid posts.
        if reason:
            it["rule"] = f"sponsored ({reason})"
        items.append(it)
    return items
