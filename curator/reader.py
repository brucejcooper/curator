"""A reader's view of the world: one timeline of the stories the curator
picked for them, in timeline-id order. Their Mastodon home timeline is just
another source: its posts are collected, grouped and judged like any other,
so each story shows once. Story ids are Mastodon-style (milliseconds << 16)
from when the story was first seen. The reader app never talks to Mastodon;
this module does (threads, bookmarks), with the reader's own token."""

import json
import logging

import httpx

from curator import people
from curator.collect import mastodon_client
from curator.home import item_for_status
from curator.people import Profile
from curator.publish import feed_folder

log = logging.getLogger(__name__)
_articles_cache: dict = {}


def num(i: str | None) -> int:
    return int(i) if i else 0


# ── Curated articles (from the reader's published feed) ─────────────────────

def articles(user: str) -> list[dict]:
    """The curated stories in the reader's feed, oldest first."""
    path = feed_folder(user) / "feed.json"
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    cached = _articles_cache.get(user)
    if not cached or cached["mtime"] != mtime:
        feed = json.loads(path.read_text())
        out = []
        for it in feed.get("items", []):
            c = it.get("_curator") or {}
            if not c.get("sort_id"):
                continue
            out.append({
                "kind": "article", "id": c["sort_id"], "item": it["id"], "url": it["url"], "title": it["title"],
                "summary": it.get("content_text") or "", "image": it.get("image"), "published": it.get("date_published"),
                "author": (it.get("authors") or [{}])[0].get("name") or "", "source": c.get("source", ""),
                "lane": c.get("lane", "main"), "reason": c.get("reason") or "", "also": c.get("also") or [],
                "trial": c.get("trial"),
            })
        out.sort(key=lambda a: num(a["id"]))
        cached = _articles_cache[user] = {"mtime": mtime, "items": out}
    return cached["items"]


# ── Per-reader state on items ────────────────────────────────────────────────

def annotate(conn, user: str, entries: list[dict]) -> list[dict]:
    """Adds this reader's vote and bookmark to each entry."""
    items = {e["item"] for e in entries if e.get("item")}
    if not items:
        return entries
    marks = ",".join("?" * len(items))
    votes = dict(conn.execute(f"SELECT item_id, vote FROM user_votes WHERE user = ? AND item_id IN ({marks})", (user, *items)).fetchall())
    kept = {r[0] for r in conn.execute(f"SELECT item_id FROM user_bookmarks WHERE user = ? AND item_id IN ({marks})", (user, *items))}
    for e in entries:
        if e.get("item"):
            e["vote"] = votes.get(e["item"], 0)
            e["bookmarked"] = e["item"] in kept
    return entries


# ── The timeline ─────────────────────────────────────────────────────────────

def after(conn, profile: Profile, since: str | None, limit: int = 40) -> dict:
    """Entries newer than `since`, oldest first. `cursor` is the newest id
    covered, to ask from next time; `end` means there's nothing newer yet."""
    arts = [dict(a) for a in articles(profile.user) if num(a["id"]) > num(since)][:limit]
    cursor = arts[-1]["id"] if arts else since
    return {"items": annotate(conn, profile.user, arts), "cursor": cursor, "end": len(arts) < limit}


def before(conn, profile: Profile, until: str, limit: int = 20) -> dict:
    """Entries older than `until`, oldest first. `cursor` is the oldest id
    covered; `end` means you've reached the start of the timeline."""
    arts = [dict(a) for a in articles(profile.user) if num(a["id"]) < num(until)][-limit:]
    return {"items": annotate(conn, profile.user, arts), "cursor": arts[0]["id"] if arts else until, "end": len(arts) < limit}


def latest_id(conn, user: str) -> str | None:
    """The newest timeline id there is for this reader (for "mark everything read")."""
    last = articles(user)[-1:]
    return last[0]["id"] if last else None


def thread(conn, user: str, status_id: str) -> dict:
    """A conversation: the post, what it replies to, and the replies."""
    creds = people.mastodon(conn, user)
    if not creds:
        raise LookupError("Mastodon isn't connected")
    with mastodon_client(creds[0], creds[1]) as http:
        status = http.get(f"/api/v1/statuses/{status_id}").raise_for_status().json()
        ctx = http.get(f"/api/v1/statuses/{status_id}/context").raise_for_status().json()
    return {"status": status, "ancestors": ctx.get("ancestors", []), "descendants": ctx.get("descendants", [])}


# ── Position ─────────────────────────────────────────────────────────────────

def position(conn, user: str) -> str | None:
    row = conn.execute("SELECT position FROM positions WHERE user = ?", (user,)).fetchone()
    return row[0] if row else None


def set_position(conn, user: str, pos: str, force: bool = False) -> str:
    """Moves the reader's position forward (never back, unless `force`), so a
    device that has read less can't undo another's progress. Returns where
    it ends up."""
    current = position(conn, user)
    if force or num(pos) > num(current):
        conn.execute("INSERT OR REPLACE INTO positions VALUES (?, ?, datetime('now'))", (user, pos))
        conn.commit()
        return pos
    return current


# ── Bookmarks ────────────────────────────────────────────────────────────────

def bookmarks(conn, user: str) -> dict:
    """The reader's bookmarks, newest first, and their Mastodon bookmarks if
    they've connected Mastodon."""
    rows = conn.execute(
        """SELECT i.id, i.url, i.title, i.summary, i.image, i.source, i.published, i.author
           FROM user_bookmarks b JOIN items i ON i.id = b.item_id WHERE b.user = ? ORDER BY b.time DESC LIMIT 500""",
        (user,)).fetchall()
    kept = [{"kind": "article", "id": f"bookmark-{r['id']}", "item": r["id"], "url": r["url"], "title": r["title"],
             "summary": r["summary"] or "", "image": r["image"], "published": r["published"], "author": r["author"] or "",
             "source": r["source"], "lane": "main", "reason": "", "also": [], "bookmarked": True} for r in rows]
    posts = []
    creds = people.mastodon(conn, user)
    if creds:
        try:
            with mastodon_client(creds[0], creds[1]) as http:
                posts = [{"kind": "status", "id": s["id"], "status": s, "verdict": {}}
                         for s in http.get("/api/v1/bookmarks", params={"limit": 40}).raise_for_status().json()]
        except httpx.HTTPError as e:
            log.warning("mastodon bookmarks for %s: %s", user, e)
    return {"articles": kept, "posts": posts}


def resolve(conn, profile: Profile, body: dict) -> str | None:
    """The item a request is about: `item_id`, or `status_id` for a post in
    the reader's home timeline (judged on the spot if new)."""
    if body.get("status_id"):
        return item_for_status(conn, profile, str(body["status_id"]))
    item = str(body.get("item_id", ""))
    return item if conn.execute("SELECT 1 FROM items WHERE id = ?", (item,)).fetchone() else None
