"""Judging a link: whether it can be read without signing in (`followable`)
and whether it could be what a page or post points at (`is_source`). Shared
by collect.py (a post's link in its text) and follow.py (a page's links)."""

import re
from urllib.parse import urlsplit

from curator.canon import canonical_url

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


def one_source(urls, home: str) -> str | None:
    """The one place a post's text links to, if it could be a source: the
    first of the links that are sources, if they're all the same page. Several
    make a roundup, which is its own thing."""
    links = [url for url in urls if isinstance(url, str) and is_source(url, home)]
    return links[0] if len({canonical_url(url) for url in links}) == 1 else None
