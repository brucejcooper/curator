"""Sponsored items: what a feed labels as paid, and the sponsor line that
opens a sponsored column. Used by feeds.py, and by store.migrate to drop
earlier ones."""

import re

from curator.canon import plain

# Paid posts their source labels as such (9to5Mac's author "Sponsored Post"):
# the whole author or a category is one of these, or the title starts with
# one in brackets or followed by punctuation. Whole names only, so a person
# called "… Partner" isn't one. store.migrate drops earlier ones by author.
SPONSORED = {"sponsored", "sponsored post", "sponsored content", "paid post", "paid content",
             "partner content", "promoted", "advertorial", "brand content"}
SPONSORED_WORD = re.compile(r"\bsponsored\b", re.I)
SPONSORED_TITLE = re.compile(r"^\s*(?:[\[(]\s*(sponsored|advertorial|partner content)\s*[\])]|"
                             r"(sponsored|advertorial|partner content)\s*[:|])", re.I)


def sponsored_label(e) -> str | None:
    """Why an entry is labelled sponsored by its source, or None."""
    author = (e.get("author") or "").strip()
    if author.lower() in SPONSORED:
        return f'author "{author}"'
    for tag in e.get("tags") or []:
        term = (tag.get("term") or "").strip()
        if term.lower() in SPONSORED or SPONSORED_WORD.search(term):
            return f'category "{term}"'
    if m := SPONSORED_TITLE.match(e.get("title") or ""):
        return f'title "{m.group(0).strip()}"'
    return None


# Paid promotion its source doesn't label: an opening paragraph that says who
# pays for it. "Apple @ Work is exclusively brought to you by Mosyle" is a
# sponsored column (dropped); "9to5Mac is brought to you by Tello Mobile" is
# a sponsor of the whole site, on an ordinary article (kept, without that
# paragraph). The feed's own name tells them apart. Only the first paragraph
# counts: "brought to you by" further down, or quoted, is the article's own.
SPONSOR_LINE = re.compile(r"(?P<series>[^\s.!?:;][^\n.!?:;]{0,60}?)\s+is\s+(?:exclusively\s+)?"
                          r"(?:brought\s+to\s+you|sponsored|presented)\s+by\b", re.I)
PARAGRAPH = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)


def sponsor_line(text: str) -> str | None:
    """The series whose sponsor the text's first paragraph names, or None."""
    m = SPONSOR_LINE.match((text or "").strip().split("\n\n", 1)[0])
    return m.group("series").strip() if m and len(m.group("series").split()) <= 6 else None


def same_name(a: str, b: str) -> bool:
    key = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())
    return bool(key(a)) and key(a) == key(b)


def without_sponsor_line(html: str) -> str:
    """The HTML without its opening paragraph, if that's a sponsor line."""
    for m in PARAGRAPH.finditer(html):
        if plain(m.group(0)):
            if not plain(html[: m.start()]) and sponsor_line(plain(m.group(0))):
                return html[: m.start()] + html[m.end():]
            break
    return html
