"""A reader's own Mastodon activity: favourites, bookmarks, boosts and posts,
follows and followed hashtags."""

import html
import re
from datetime import datetime, timedelta, timezone

import httpx

TAG_RE = re.compile(r"<[^>]+>")


def plain(content: str, limit: int = 400) -> str:
    text = html.unescape(TAG_RE.sub(" ", content.replace("</p>", "\n")))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:limit]


def describe(status: dict) -> dict:
    """The few fields of a status worth showing Claude."""
    if status.get("reblog"):
        status = status["reblog"]
    card = status.get("card") or {}
    return {
        "id": status["id"],
        "author": status["account"]["acct"],
        "text": plain(status.get("content", "")),
        "tags": [t["name"] for t in status.get("tags", [])],
        "link": card.get("url"),
        "link_title": card.get("title"),
    }


class Mastodon:
    def __init__(self, server: str, token: str):
        self.http = httpx.Client(
            base_url=f"https://{server}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def _pages(self, path: str, limit: int, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        url, query = path, {"limit": 40, **(params or {})}
        while url and len(items) < limit:
            resp = self.http.get(url, params=query)
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            items.extend(page)
            url, query = resp.links.get("next", {}).get("url"), None
        return items[:limit]

    def me(self) -> dict:
        resp = self.http.get("/api/v1/accounts/verify_credentials")
        resp.raise_for_status()
        return resp.json()

    def activity(self, days: int, seen: set[str], limit: int = 200) -> dict:
        """Activity new since the last run. Favourites and bookmarks carry no
        timestamp of when the reader acted, so they are filtered by `seen` ids;
        posts and boosts by date."""
        me = self.me()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        favourites = [describe(s) for s in self._pages("/api/v1/favourites", limit)]
        bookmarks = [describe(s) for s in self._pages("/api/v1/bookmarks", limit)]
        own = [
            s for s in self._pages(f"/api/v1/accounts/{me['id']}/statuses", limit)
            if datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) >= since
        ]
        following = self._pages(f"/api/v1/accounts/{me['id']}/following", 1000)
        tags = self._pages("/api/v1/followed_tags", 500)
        return {
            "favourites": [s for s in favourites if s["id"] not in seen],
            "bookmarks": [s for s in bookmarks if s["id"] not in seen],
            "boosts": [describe(s) for s in own if s.get("reblog")],
            "posts": [describe(s) for s in own if not s.get("reblog")],
            "following": sorted(a["acct"] for a in following),
            "followed_tags": sorted(t["name"] for t in tags),
        }
