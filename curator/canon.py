"""Canonical URLs and titles, so the same article from two places is one item."""

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_[ce]id|ref|ref_src|cmpid|ocid|sr_share|share|s|at_.*|__twitter_impression)$", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:16]


BLOCK_END = re.compile(r"(?i)</(p|div|li|h[1-6]|blockquote|pre)>|<br\s*/?>")


def plain(text: str | None, limit: int = 4000) -> str:
    """Text from HTML, keeping paragraph breaks. Inline tags vanish without
    a space, so Mastodon's split-up links ("https://" + "example.com") read
    as one."""
    text = BLOCK_END.sub(lambda m: "\n" if m.group(0).lower().startswith("<br") else "\n\n", text or "")
    text = html.unescape(TAG_RE.sub("", text))
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:limit]


def short_title(text: str, limit: int = 120) -> str:
    """A post's first line, cut at a word with an ellipsis if it's long."""
    first = text.strip().split("\n", 1)[0]
    if len(first) <= limit:
        return first
    cut = first[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def title_key(title: str) -> str:
    """Near-identical titles: case, punctuation and outlet suffixes ignored."""
    title = re.split(r"\s+[|–—-]\s+[^|–—-]{2,40}$", title.strip())[0]
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
