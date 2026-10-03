"""A reader's own Mastodon home timeline, as one more of their sources. The
reader app asks about the posts it's about to show; posts the curator hasn't
seen are fetched with the reader's token, judged and grouped into stories on
the spot. A post shows when its story clears the bar (the home timeline has
a lower one), merging with any other coverage of the same story."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from curator import people
from curator.canon import item_id
from curator.collect import conversation_root, from_status, has_link_card, mastodon_client
from curator.judge import apply_shared_rules, judge_for
from curator.people import Profile
from curator.publish import margins, reason
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


def verdicts(conn, profile: Profile, ids: list[str]) -> dict[str, dict]:
    """status id → {show, lane, item, story, reason, also}. Unknown or
    unjudgeable posts get show=True: when in doubt, the timeline wins."""
    user = profile.user
    ids = [str(i) for i in ids][:80]
    learn(conn, profile, ids)
    repoint(conn, user, ids)
    src = profile.sources
    main = people.decider(conn, user)
    other = "local" if main == "jev" else "jev"
    out = {}
    rows = conn.execute(
        f"""SELECT h.status_id, i.id, i.story_id, i.rule, i.sample, j.rule AS my_rule, j.jev, j.local, j.jev_score, j.local_score
            FROM home_posts h LEFT JOIN items i ON i.id = h.item_id
            LEFT JOIN judgments j ON j.item_id = i.id AND j.user = h.user
            WHERE h.user = ? AND h.status_id IN ({','.join('?' * len(ids))})""", (user, *ids)).fetchall()
    for r in rows:
        if not r["id"]:
            out[r["status_id"]] = {"show": False, "lane": "drop", "why": "not judged (e.g. not in English)"}
            continue
        if r["rule"] or r["my_rule"]:
            out[r["status_id"]] = {"show": False, "lane": "drop", "why": r["rule"] or r["my_rule"], "item": r["id"]}
            continue
        story = {"story": r["story_id"]}
        margins(conn, user, [story], src["min_score"], src["source_floors"])
        passes = lambda kind: story["margin_" + kind] is not None and story["margin_" + kind] >= 0
        if passes(main):
            lane = "main"
        elif passes(other) or r["sample"]:
            lane = "maybe"
        else:
            lane = "drop"
        also = [dict(a) for a in conn.execute(
            "SELECT url, title, source FROM items WHERE story_id = ? AND id != ? AND source NOT LIKE 'mastodon:home:%' AND rule IS NULL",
            (r["story_id"], r["id"]))]
        scores = {"jev": r["jev_score"], "local": r["local_score"]}
        out[r["status_id"]] = {
            "show": lane != "drop", "lane": lane, "item": r["id"], "story": r["story_id"],
            "score": scores[main], "also": also,
            "reason": reason({"jev": r["jev"], "local": r["local"], "story_jev": r["jev_score"], "story_local": r["local_score"]}),
        }
    for sid in ids:
        out.setdefault(sid, {"show": True, "lane": "unknown"})
    return out


def item_for_status(conn, profile: Profile, status_id: str) -> str | None:
    learn(conn, profile, [status_id])
    repoint(conn, profile.user, [status_id])
    row = conn.execute("SELECT item_id FROM home_posts WHERE user = ? AND status_id = ?", (profile.user, status_id)).fetchone()
    return row[0] if row and row[0] else None
