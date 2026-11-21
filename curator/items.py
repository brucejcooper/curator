"""What every collected item is made with: the item itself, the time in UTC
and the User-Agent the curator fetches with. Shared by feeds.py, posts.py,
instagram.py and collect.py."""

from datetime import datetime, timezone

from curator.canon import plain
from curator.config import USER_AGENT

UA = {"User-Agent": USER_AGENT}


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
