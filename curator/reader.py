"""A reader's view of the world: one timeline merging their Mastodon home
timeline (if they've connected one, filtered by the curator) with the
articles the curator picked for them, in timeline-id order. Mastodon ids are
milliseconds << 16, and curated stories carry a sort_id in the same space,
so "after X" and "before X" mean the same thing for both. The reader app
never talks to Mastodon; this module does, with the reader's own token."""

import json
import logging

import httpx

from curator import people
from curator.collect import mastodon_client
from curator.home import item_for_status, verdicts
from curator.people import Profile
from curator.publish import feed_folder

log = logging.getLogger(__name__)
_articles_cache: dict = {}


def num(i: str | None) -> int:
    return int(i) if i else 0


def account_id(conn, user: str, creds: tuple) -> str:
    """The reader's own Mastodon account id (looked up once)."""
    if creds[2]:
        return creds[2]
    with mastodon_client(creds[0], creds[1]) as http:
        account = http.get("/api/v1/accounts/verify_credentials").raise_for_status().json()["id"]
    conn.execute("UPDATE users SET mastodon_account = ? WHERE id = ?", (account, user))
    conn.commit()
    return account


# ── Curated articles (from the reader's published feed) ─────────────────────

def articles(user: str) -> list[dict]:
    """The curated stories in the reader's feed, oldest first. Stories that
    include a post from their own home timeline are left out: the timeline
    shows that post, with the other coverage under it."""
    path = feed_folder(user) / "feed.json"
    if not path.exists():
        return []
    home = people.home_source(user)
    mtime = path.stat().st_mtime
    cached = _articles_cache.get(user)
    if not cached or cached["mtime"] != mtime:
        feed = json.loads(path.read_text())
        out = []
        for it in feed.get("items", []):
            c = it.get("_curator") or {}
            if not c.get("sort_id") or c.get("source") == home or any(a.get("source") == home for a in c.get("also") or []):
                continue
            out.append({
                "kind": "article", "id": c["sort_id"], "item": it["id"], "url": it["url"], "title": it["title"],
                "summary": it.get("content_text") or "", "image": it.get("image"), "published": it.get("date_published"),
                "author": (it.get("authors") or [{}])[0].get("name") or "", "source": c.get("source", ""),
                "lane": c.get("lane", "main"), "reason": c.get("reason") or "", "also": c.get("also") or [],
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


# ── The merged timeline ──────────────────────────────────────────────────────

def statuses(conn, profile: Profile, creds: tuple, page: list[dict]) -> list[dict]:
    """Home posts that clear the bar, as timeline entries. Your own posts and
    posts mentioning you always show."""
    me = account_id(conn, profile.user, creds)
    inner = lambda s: s.get("reblog") or s
    mine = lambda s: inner(s)["account"]["id"] == me or any(m["id"] == me for m in inner(s).get("mentions") or [])
    v = verdicts(conn, profile, [s["id"] for s in page if not mine(s)]) if page else {}
    out = []
    for s in page:
        verdict = v.get(s["id"], {"show": True, "lane": "main"})
        if mine(s) or verdict.get("show", True):
            out.append({"kind": "status", "id": s["id"], "status": s, "item": verdict.get("item"),
                        "verdict": {k: verdict.get(k) for k in ("lane", "also", "reason")}})
    return out


def home_page(creds: tuple, params: dict) -> list[dict]:
    with mastodon_client(creds[0], creds[1]) as http:
        return list(reversed(http.get("/api/v1/timelines/home", params=params).raise_for_status().json()))


def after(conn, profile: Profile, since: str | None, limit: int = 40) -> dict:
    """Entries newer than `since`, oldest first. `cursor` is the newest
    timeline id covered (hidden posts and articles included), to ask from next
    time; `end` means there's nothing newer yet."""
    creds = people.mastodon(conn, profile.user)
    arts_all = articles(profile.user)
    if not creds:
        # Articles only: the whole list is here, so a page is the next `limit`.
        arts = [dict(a) for a in arts_all if num(a["id"]) > num(since)][:limit]
        cursor = arts[-1]["id"] if arts else since
        return {"items": annotate(conn, profile.user, arts), "cursor": cursor, "end": len(arts) < limit}
    page = home_page(creds, {"limit": limit, **({"min_id": since} if since else {})})
    lo = num(since) if since else (num(page[0]["id"]) - 1 if page else 0)
    hi = num(page[-1]["id"]) if page else None
    arts = [dict(a) for a in arts_all if num(a["id"]) > lo and (hi is None or num(a["id"]) <= hi)]
    entries = sorted(statuses(conn, profile, creds, page) + arts, key=lambda e: num(e["id"]))
    # The cursor covers everything returned, articles included: when there are
    # no new posts, articles newer than the last one must not come back on the
    # next check. (Safe: every article's id is a time already passed.)
    covered = ([num(page[-1]["id"])] if page else []) + [num(a["id"]) for a in arts] + ([num(since)] if since else [])
    cursor = str(max(covered)) if covered else since
    return {"items": annotate(conn, profile.user, entries), "cursor": cursor, "end": not page}


def before(conn, profile: Profile, until: str, limit: int = 20) -> dict:
    """Entries older than `until`, oldest first. `cursor` is the oldest id
    covered; `end` means you've reached the start of the timeline."""
    creds = people.mastodon(conn, profile.user)
    arts_all = articles(profile.user)
    if not creds:
        arts = [dict(a) for a in arts_all if num(a["id"]) < num(until)][-limit:]
        return {"items": annotate(conn, profile.user, arts), "cursor": arts[0]["id"] if arts else until, "end": len(arts) < limit}
    page = home_page(creds, {"limit": limit, "max_id": until})
    if not page:
        return {"items": [], "cursor": until, "end": True}
    lo, hi = num(page[0]["id"]), num(until)
    arts = [dict(a) for a in arts_all if lo <= num(a["id"]) < hi]
    entries = sorted(statuses(conn, profile, creds, page) + arts, key=lambda e: num(e["id"]))
    return {"items": annotate(conn, profile.user, entries), "cursor": page[0]["id"], "end": False}


def latest_id(conn, user: str) -> str | None:
    """The newest timeline id there is for this reader (for "mark everything read")."""
    ids = [num(a["id"]) for a in articles(user)[-1:]]
    creds = people.mastodon(conn, user)
    if creds:
        ids += [num(p["id"]) for p in home_page(creds, {"limit": 1})]
    return str(max(ids)) if ids else None


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
