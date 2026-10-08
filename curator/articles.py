"""Text for feed items that came with little or none: the article's first
few paragraphs (or its description) and its preview image, fetched once when
the item is new, so the reader, the story grouping and Jev all have something
to go on. Two kinds: bare (Hacker News: only links and points; a summary that
repeats the title; "[link] [comments]") and teasers cut off with "more…" or
"[…]" (9to5Mac, The Verge, Hackaday), whose opening line is kept."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser

import httpx

from curator.images import POST_URL, preview_image

log = logging.getLogger(__name__)
HN = re.compile(r"^Article URL: \S+\s+Comments URL: (\S+)\s+Points: \d+\s+# Comments: \d+$")
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[^\W\d_]{3,}")
FURNITURE = set("article url comments comment points read more continue reading link submitted via".split())
BARE = 3  # fewer words than this beyond the title, links and furniture: says nothing
# A teaser: cut off with a marker, and short. (CNX Software's footer follows
# 1,000+ characters of real text; that isn't one.)
CUT = re.compile(r"\s*(\bmore…|\bmore\.\.\.|\[…\]|\[\.\.\.\]|…\s*read more|\bcontinue reading\b.*|\bread more\b.*)\s*$", re.I)
TEASER = 600
# Paywalled publishers mark their article pages so search engines can tell
# (schema.org): "isAccessibleForFree": false.
PAYWALL = re.compile(r'"isAccessibleForFree"\s*:\s*"?false\b', re.I)
WANT = 900  # enough text for a few paragraphs
MAX_PARAGRAPHS = 3
MIN_PARAGRAPH = 60  # shorter <p>s are bylines, captions, "Share this"
MAX_BYTES = 1_500_000
SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure", "figcaption", "button"}


class Paragraphs(HTMLParser):
    """The text of <p>s outside page furniture, and separately those inside
    <article>/<main> (preferred when there are any)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = self.body = 0
        self.current: list[str] | None = None
        self.all: list[str] = []
        self.inside: list[str] = []
        self.description = ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in ("article", "main"):
            self.body += 1
        elif tag == "p" and not self.skip:
            self.current = []
        elif tag == "meta" and not self.description:
            a = {k: v or "" for k, v in attrs}
            if (a.get("property") or a.get("name") or "").lower() in ("og:description", "description", "twitter:description"):
                self.description = " ".join(a.get("content", "").split())

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("article", "main"):
            self.body = max(0, self.body - 1)
        elif tag == "p" and self.current is not None:
            text = " ".join("".join(self.current).split())
            if len(text) >= MIN_PARAGRAPH and not text.endswith(":"):  # not a lead-in to a list or code
                self.all.append(text)
                if self.body:
                    self.inside.append(text)
            self.current = None

    def handle_data(self, data):
        if self.current is not None and not self.skip:
            self.current.append(data)


def extract(html: str) -> str:
    """The first few paragraphs, up to about WANT characters; the page's
    description if it has no usable paragraphs."""
    parser = Paragraphs()
    try:
        parser.feed(html)
    except Exception:  # malformed pages: keep what was parsed
        pass
    out, size, seen = [], 0, set()
    for p in parser.inside or parser.all:
        if size >= WANT or len(out) == MAX_PARAGRAPHS:
            break
        if key(p) in seen:  # a subtitle repeated in the page
            continue
        seen.add(key(p))
        out.append(p)
        size += len(p)
    return "\n\n".join(out) or unescape(parser.description)


def own_words(it: dict) -> list[str]:
    """The summary's words that aren't in the title, links or furniture."""
    title = {w.lower() for w in WORD.findall(it["title"])}
    return [w for w in WORD.findall(URL.sub(" ", it["summary"])) if w.lower() not in title | FURNITURE]


def bare(it: dict) -> bool:
    return len(own_words(it)) < BARE


def teaser(it: dict) -> bool:
    return bool(CUT.search(it["summary"])) and len(it["summary"]) < TEASER


def needs_text(it: dict) -> bool:
    if not it["source"].startswith("rss:") or POST_URL.match(it["url"]):
        return False
    return bare(it) or teaser(it)


def key(text: str) -> str:
    return " ".join(WORD.findall(text.lower()))


def with_teaser(lead: str, text: str) -> str:
    """The teaser's opening line, then the article's paragraphs; just the
    paragraphs if the first one is the teaser in full."""
    lead = CUT.sub("", lead).rstrip(" …")
    paragraphs = text.split("\n\n")
    start = key(lead)[:80]
    if start and any(key(p).startswith(start) for p in paragraphs):
        return text
    return "\n\n".join([lead] + paragraphs)


def page(http: httpx.Client, url: str) -> tuple[str, str]:
    """(html, final url) of an article page; empty strings if unreadable."""
    try:
        with http.stream("GET", url, follow_redirects=True) as resp:
            if not resp.is_success or "html" not in resp.headers.get("content-type", ""):
                return "", ""
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
        return body.decode(resp.encoding or "utf-8", "replace"), str(resp.url)
    except (httpx.HTTPError, LookupError):
        return "", ""


def fetch(http: httpx.Client, url: str) -> tuple[str, str]:
    """(text, image) from the article page; empty strings if unreadable."""
    html, final = page(http, url)
    return (extract(html), preview_image(html, final) or "") if html else ("", "")


def paywalled(html: str) -> bool:
    return bool(PAYWALL.search(html))


def fill_text(items: list[dict]) -> None:
    """Fills in `summary` (and `image`, if missing) for the items whose feed
    gave little or no text. Keeps a Hacker News item's discussion link; a
    teaser stays as it was if the article can't be read."""
    todo = [it for it in items if needs_text(it)]
    if not todo:
        return
    from curator.config import USER_AGENT

    headers = {"User-Agent": f"Mozilla/5.0 (compatible; {USER_AGENT})"}
    with httpx.Client(headers=headers, timeout=8) as http, ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda it: fetch(http, it["url"]), todo))
    for it, (text, image) in zip(todo, found):
        hn = HN.match(it["summary"].strip())
        if hn:
            it["summary"] = "\n\n".join(x for x in (text, f"Discussion: {hn.group(1)}") if x)
        elif text:  # the feed's own line, if it has one, comes first
            it["summary"] = with_teaser(it["summary"], text) if own_words(it) else text
        if image and not it.get("image"):
            it["image"] = image
    log.info("fetched text for %d items with little or none, found %d", len(todo), sum(bool(t) for t, _ in found))
