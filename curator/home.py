"""A reader's own Mastodon home timeline is just another of their sources,
collected every cycle. This is for a post the reader acts on by its Mastodon
id (a vote on one of their Mastodon bookmarks): one the curator hasn't seen
is fetched with the reader's token, judged and grouped into a story on the
spot."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from curator import people
from curator.canon import item_id
from curator.collect import conversation_root, from_status, has_link_card, mastodon_client
from curator.judge import apply_shared_rules, judge_for
from curator.people import Profile
from curator.stories import ingest
from curator.traits import judge_traits

log = logging.getLogger(__name__)


def fetch_statuses(server: str, token: str, ids: list[str]) -> dict[str, dict]:
    """Statuses by id from the reader's server, replies replaced by their conversation's first post."""
    with mastodon_client(server, token) as http:
        def one(sid):
            resp = http.get(f"/api/v1/statuses/{sid}")
            if not resp.is_success:
                return sid, None
            s = resp.json()
            inner = s.get("reblog") or s
            if inner.get("in_reply_to_id") and not has_link_card(inner):
                inner = conversation_root(http, inner["id"], {}) or inner
            return sid, inner

        with ThreadPoolExecutor(8) as pool:
            return {sid: s for sid, s in pool.map(one, ids) if s}


def learn(conn, profile: Profile, ids: list[str]) -> None:
    """Fetch, judge and map the reader's posts the curator hasn't seen yet."""
    user = profile.user
    known = {r[0] for r in conn.execute(
        f"SELECT status_id FROM home_posts WHERE user = ? AND status_id IN ({','.join('?' * len(ids))})", (user, *ids))}
    todo = [i for i in ids if i not in known]
    creds = people.mastodon(conn, user)
    if not todo or not creds:
        return
    statuses = fetch_statuses(creds[0], creds[1], todo)
    source = people.home_source(user)
    items, mapping = [], []
    for sid, s in statuses.items():
        it = from_status(s, source, home=True)
        if it:
            items.append(it)
            mapping.append((sid, item_id(it["url"])))
        else:
            mapping.append((sid, ""))  # nothing to judge (e.g. not English): no item
    new = ingest(conn, items)
    if new:
        apply_shared_rules(conn, new)
        judge_traits(conn, new)
    # Judge for this reader everything newly mapped (new items, and items
    # already known from elsewhere that they hadn't had judged).
    judge_for(conn, profile, [iid for _, iid in mapping if iid])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany("INSERT OR REPLACE INTO home_posts VALUES (?, ?, ?, ?)", [(user, sid, iid, stamp) for sid, iid in mapping])
    conn.commit()
    log.info("home: judged %d new posts for %s (%d new items)", len(statuses), user, len(new))


def repoint(conn, user: str, ids: list[str]) -> None:
    """Posts whose item was folded into another at ingest (a near-identical
    title elsewhere) point at that item instead."""
    rows = conn.execute(
        f"""SELECT h.status_id, h.item_id FROM home_posts h LEFT JOIN items i ON i.id = h.item_id
            WHERE h.user = ? AND h.status_id IN ({','.join('?' * len(ids))}) AND h.item_id != '' AND i.id IS NULL""",
        (user, *ids)).fetchall()
    if not rows:
        return
    wanted = {r["item_id"]: r["status_id"] for r in rows}
    for target, url in conn.execute("SELECT item_id, url FROM sightings WHERE source = ? ORDER BY seen DESC LIMIT 5000",
                                    (people.home_source(user),)):
        if (iid := item_id(url)) in wanted:
            conn.execute("UPDATE home_posts SET item_id = ? WHERE user = ? AND status_id = ?", (target, user, wanted.pop(iid)))
    conn.commit()


def item_for_status(conn, profile: Profile, status_id: str) -> str | None:
    learn(conn, profile, [status_id])
    repoint(conn, profile.user, [status_id])
    row = conn.execute("SELECT item_id FROM home_posts WHERE user = ? AND status_id = ?", (profile.user, status_id)).fetchone()
    return row[0] if row and row[0] else None
