"""Pictures for feed items that came without one: the article's own preview
image (og:image / twitter:image), as link previews use. Only for items that
make the feed, each looked up once ('' records "none found")."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)
META = re.compile(r"<meta\b[^>]*>", re.I)
ATTR = re.compile(r'([\w:-]+)\s*=\s*("[^"]*"|\'[^\']*\')')
WANTED = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
POST_URL = re.compile(r"^https://(bsky\.app/profile/|[^/]+/(@[^/]+|users/[^/]+/statuses)/\d+)")
MAX_BYTES = 400_000  # the <head> is near the top


def preview_image(html: str, base: str) -> str | None:
    found = {}
    for tag in META.findall(html):
        attrs = {k.lower(): unescape(v[1:-1]) for k, v in ATTR.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in WANTED and attrs.get("content"):
            found.setdefault(key, attrs["content"])
    for key in WANTED:
        if key in found:
            url = urljoin(base, found[key].strip())
            return url if url.startswith("https://") else None
    return None


def lookup(http: httpx.Client, url: str) -> str:
    if POST_URL.match(url):
        return ""  # a social post's preview image is just the author's avatar
    try:
        with http.stream("GET", url, follow_redirects=True) as resp:
            if not resp.is_success or "html" not in resp.headers.get("content-type", ""):
                return ""
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES or b"</head>" in body:
                    break
        return preview_image(body.decode("utf-8", "replace"), str(resp.url)) or ""
    except httpx.HTTPError:
        return ""


def fill_images(conn, stories: list[dict], limit: int = 60) -> None:
    """Looks up a picture for the stories' leads that have never been checked,
    and sets `image` on the story dicts."""
    todo = [s for s in stories if s.get("image") is None][:limit]
    if not todo:
        return
    from curator.config import USER_AGENT

    headers = {"User-Agent": f"Mozilla/5.0 (compatible; {USER_AGENT})"}
    with httpx.Client(headers=headers, timeout=8) as http, ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda s: lookup(http, s["url"]), todo))
    for s, image in zip(todo, found):
        s["image"] = image
        conn.execute("UPDATE items SET image = ? WHERE id = ?", (image, s["id"]))
    conn.commit()
    log.info("looked up %d preview images, found %d", len(todo), sum(bool(i) for i in found))
