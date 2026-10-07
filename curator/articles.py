"""Text for feed items that came without any: the article's first few
paragraphs (or its description) and its preview image, fetched once when the
item is new, so the reader, the story grouping and Jev all have something to
go on. Hacker News' feed is the usual case: its summary is only the links and
points."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser

import httpx

from curator.images import POST_URL, preview_image

log = logging.getLogger(__name__)
HN = re.compile(r"^Article URL: \S+\s+Comments URL: (\S+)\s+Points: \d+\s+# Comments: \d+$")
THIN = 60  # a summary shorter than this (after the boilerplate) says nothing
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
    out, size = [], 0
    for p in parser.inside or parser.all:
        if size >= WANT or len(out) == MAX_PARAGRAPHS:
            break
        out.append(p)
        size += len(p)
    return "\n\n".join(out) or unescape(parser.description)


def needs_text(it: dict) -> bool:
    if not it["source"].startswith("rss:") or POST_URL.match(it["url"]):
        return False
    return len(HN.sub("", it["summary"]).strip()) < THIN


def fetch(http: httpx.Client, url: str) -> tuple[str, str]:
    """(text, image) from the article page; empty strings if unreadable."""
    try:
        with http.stream("GET", url, follow_redirects=True) as resp:
            if not resp.is_success or "html" not in resp.headers.get("content-type", ""):
                return "", ""
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
        html = body.decode(resp.encoding or "utf-8", "replace")
        return extract(html), preview_image(html, str(resp.url)) or ""
    except (httpx.HTTPError, LookupError):
        return "", ""


def fill_text(items: list[dict]) -> None:
    """Fills in `summary` (and `image`, if missing) for the items whose feed
    gave no text. Keeps a Hacker News item's discussion link."""
    todo = [it for it in items if needs_text(it)]
    if not todo:
        return
    from curator.config import USER_AGENT

    headers = {"User-Agent": f"Mozilla/5.0 (compatible; {USER_AGENT})"}
    with httpx.Client(headers=headers, timeout=8) as http, ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda it: fetch(http, it["url"]), todo))
    for it, (text, image) in zip(todo, found):
        hn = HN.match(it["summary"].strip())
        discussion = f"Discussion: {hn.group(1)}" if hn else ""
        if text or hn:
            it["summary"] = "\n\n".join(x for x in (text, discussion) if x)
        if image and not it.get("image"):
            it["image"] = image
    log.info("fetched text for %d items without any, found %d", len(todo), sum(bool(t) for t, _ in found))
