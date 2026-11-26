"""Resolve the story: drop exact duplicates, group items about the same story
by embedding similarity, and pick the most informative version as the lead."""

import asyncio
import json
import logging
import os
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np

from curator import embeddings, store
from curator.articles import fill_text, needs_text
from curator.canon import canonical_url, item_id, title_key
from curator.follow import count as count_follow, follow, host
from curator.images import POST_URL
from curator.traits import QUESTIONS as TRAITS

log = logging.getLogger(__name__)
# bge-small similarities sit high: same-story pairs from different outlets
# measured 0.87-0.99, while different stories from one outlet reached 0.86.
# Past these an item joins when Jev can't be asked (find_story).
SAME_STORY = float(os.environ.get("CURATOR_SAME_STORY", "0.87"))
SAME_SOURCE = 0.93  # an outlet's different stories can read alike, so it takes more
FIRST_ITEM_SLACK = 0.05  # also close to the story's first item, so stories don't chain
# Similarity only proposes: a preview and the event it previews read alike
# (0.75-0.90 measured), so Jev confirms every join (QUESTIONS["same"]). The
# closest earlier items down to MAYBE_SAME are asked about, as wording can
# differ a lot for the same news ("Apple TV just added free classic movies"
# vs "Apple TV updated its 'Bonus' movies" measured 0.75).
MAYBE_SAME = 0.72
# An outlet often runs a story again with a new headline or copy, or splits
# one event into several articles (one per OS on a beta day: 0.81-0.86).
# Pairs from one source down to MAYBE_SAME_SOURCE get asked of Jev too.
MAYBE_SAME_SOURCE = 0.78
# A near-copy joins without asking: pairs this close within NEAR_COPY_HOURS
# were all the same story (Nov 2026: 11 of 11), and just below it were two
# listings of different CPUs (0.969); a preview and its event reached 0.90.
NEAR_COPY = float(os.environ.get("CURATOR_NEAR_COPY", "0.97"))
NEAR_COPY_HOURS = timedelta(hours=6)
GREY_ASKS = 3  # earlier stories to ask about, per new item
JEV_SAME = 0.6
# A story takes items for 72 hours from its first, so stories don't chain.
WINDOW = timedelta(hours=72)
SAMPLE_RATE = 0.03  # share of items eligible for the maybe lane's random sample


def text_of(item: dict) -> str:
    return f"{item['title']}. {item['summary'][:600]}"


def recent(conn, now: datetime):
    cutoff = (now - WINDOW).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title, source, story_id, embedding FROM items WHERE fetched >= ? AND embedding IS NOT NULL",
        (cutoff,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    stories = [r["story_id"] for r in rows]
    sources = [r["source"] for r in rows]
    titles = {title_key(r["title"]): r["id"] for r in rows}
    matrix = np.stack([store.vec(r["embedding"]) for r in rows]) if rows else np.zeros((0, 384), np.float32)
    return ids, stories, sources, titles, matrix


QUESTIONS = {"same": {
    "type": "noul",
    "instructions": "Do `a` and `b` report the same thing: the same event, announcement, release, incident or finding, in "
                    "the same state, so that a reader of one would learn nothing new from the other? The same news from "
                    "different outlets, in other words, or posted again, is the same; so are a live page and the report of "
                    "the same event, and separate articles about parts of one announcement or release (one article per "
                    "product in the same launch, or per operating system in the same set of updates). But these are "
                    "different, even on the same topic: anticipation of an event (a preview, speculation, something "
                    "'expected', 'to meet', 'braces for') and the event itself; an event and its outcome or result; a "
                    "report and a later reversal or a significant new fact; news and an explainer or analysis of it. If "
                    "`b` tells a reader of `a` something new that happened, they are different.",
    "criteria": {"true": "The same news: a reader of one learns nothing new from the other",
                 "false": "Different news, even on the same topic or about the same event: anticipation and the event, "
                          "an event and its outcome, news and analysis"},
}}


def same_stories(pairs: list[tuple[dict, dict]]) -> list[bool | None]:
    """Asks Jev whether each pair of items reports the same thing; None
    where Jev couldn't say."""
    if not pairs:
        return []
    from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

    from curator.secrets import get_secret

    async def run():
        sem = asyncio.Semaphore(8)
        async with AsyncTypeSafeClient(api_key=get_secret("JEV_API_KEY"), model="jev-latest") as client:
            async def one(a, b):
                state = {k: {"title": x["title"], "summary": x["summary"][:400], "source": x["source"]} for k, x in (("a", a), ("b", b))}
                async with sem:
                    try:
                        r = await client.system_one(state=state, questions=QUESTIONS)
                        return r.model_dump()["answers"]["same"]["noul"] >= JEV_SAME
                    except TypeSafeError:
                        return None
            return await asyncio.gather(*(one(a, b) for a, b in pairs))

    return asyncio.run(run())


def same_story(pairs: list[tuple[dict, dict]]) -> list[bool]:
    """Asks Jev whether each pair of items reports the same thing."""
    return [bool(v) for v in same_stories(pairs)]


def merge_story(conn, into: str, story: str) -> None:
    if into == story:
        return
    conn.execute("UPDATE items SET story_id = ? WHERE story_id = ?", (into, story))
    conn.execute("DELETE FROM stories WHERE id = ?", (story,))


def candidates(v: np.ndarray, source: str, ids, stories, sources, matrix, skip=frozenset()) -> list[tuple[str, float]]:
    """The closest earlier items that may report the same thing (Jev
    decides), one per story (none from `skip`), up to GREY_ASKS, closest
    first: (id, similarity)."""
    if not len(matrix):
        return []
    sims = matrix @ v
    found, seen = [], set(skip)
    for i in np.argsort(-sims):
        if sims[i] < min(MAYBE_SAME, MAYBE_SAME_SOURCE) or len(found) == GREY_ASKS:
            break
        if stories[i] in seen or sims[i] < (MAYBE_SAME_SOURCE if sources[i] == source else MAYBE_SAME):
            continue
        seen.add(stories[i])
        found.append((ids[i], float(sims[i])))
    return found


def find_story(v: np.ndarray, source: str, ids, stories, sources, matrix) -> str | None:
    """The story the item would join on similarity alone: that of the
    closest earlier item past SAME_STORY (SAME_SOURCE from the same source),
    if it's close to the story's first item too, so stories don't chain. A
    story whose first item has left the window takes no more. Only where
    Jev can't confirm it (ingest)."""
    if not len(matrix):
        return None
    sims = matrix @ v
    index = {iid: i for i, iid in enumerate(ids)}
    for i in np.argsort(-sims):
        if sims[i] < SAME_STORY:
            return None
        if sources[i] == source and sims[i] < SAME_SOURCE:
            continue
        first = index.get(stories[i])
        if first is not None and sims[first] >= SAME_STORY - FIRST_ITEM_SLACK:
            return stories[i]
    return None


def add_via(conn, iid: str, via: list[dict]) -> None:
    """Another way to the same item: its wrappers join the item's `via`."""
    row = conn.execute("SELECT via FROM items WHERE id = ?", (iid,)).fetchone() if via else None
    if row is None:
        return
    known = store.json_or_none(row[0]) or []
    new = [v for v in via if v["url"] not in {k["url"] for k in known}]
    if new:
        conn.execute("UPDATE items SET via = ? WHERE id = ?", (json.dumps(known + new), iid))


def link_story(conn, it: dict, stories: list, touched: set) -> str | None:
    """The story a new item belongs to by a link, whatever the embedding
    says: that of the stored item it points to (`points_to`), and that of
    any stored wrapper pointing to it. Several are merged into one. `stories`
    (the recent items' stories) and `touched` follow the merge."""
    found = []
    if p := it.get("points_to"):
        found += [r[0] for r in conn.execute("SELECT story_id FROM items WHERE id = ?", (item_id(p["url"]),))]
    found += [r[0] for r in conn.execute("SELECT i.story_id FROM points_to p JOIN items i ON i.id = p.item_id WHERE p.url = ?",
                                         (it["canonical"],))]
    found = list(dict.fromkeys(s for s in found if s))
    if not found:
        return None
    into = found[0]
    for other in found[1:]:
        merge_story(conn, into, other)
        stories[:] = [into if s == other else s for s in stories]
        touched.discard(other)
    if len(found) > 1:
        count_follow(conn, Counter(linked=len(found) - 1))
    return into


def ingest(conn, found: list[dict], tally: dict | None = None, follow_wrappers: bool = True) -> list[str]:
    """Store new items; returns the ids that are new. A new item that only
    wraps another (follow.py) is stored as the one it wraps, seen where it
    was, unless not `follow_wrappers`. `tally` gets how many were followed."""
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    pages = {}
    if follow_wrappers:
        conn.commit()  # don't hold the write lock while following links
        try:
            pages = follow(conn, found, tally)
        except Exception:  # never worse than not following
            log.exception("following wrappers")
    ids, stories, sources, titles, matrix = recent(conn, now)
    fresh: list[dict] = []
    for it in found:
        iid = item_id(it["url"])
        seen_at = it.get("seen_url") or it["url"]  # the wrapper's, if it was followed
        existing = conn.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone()
        twin = titles.get(title_key(it["title"])) if len(it["title"]) > 20 else None
        target = iid if existing else twin
        if target is None and (same := next((f for f in fresh if f["id"] == iid), None)):
            target = iid
            same["via"] = (same.get("via") or []) + [v for v in it.get("via") or [] if v not in (same.get("via") or [])]
        if target:
            conn.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?)", (target, it["source"], seen_at, stamp))
            if existing:
                add_via(conn, target, it.get("via"))
            continue
        it = {**it, "id": iid, "canonical": canonical_url(it["url"]), "fetched": stamp, "seen_at": seen_at}
        fresh.append(it)
        titles[title_key(it["title"])] = iid

    if any(needs_text(it) for it in fresh):
        conn.commit()  # don't hold the write lock while fetching articles
        fill_text(fresh, pages)
    vectors = embeddings.embed([text_of(it) for it in fresh])
    touched: set[str] = set()
    starts = story_starts(conn, stories)
    cutoff = (now - WINDOW).isoformat(timespec="seconds")
    aged = {s for s, t in starts.items() if t < cutoff}  # past their window: they take no more items
    asks: list[tuple[str, list, str | None]] = []  # (new item, its candidates, the story similarity alone would join)
    for it, v in zip(fresh, vectors):
        story = link_story(conn, it, stories, touched)
        if story:
            count_follow(conn, Counter(linked=1))
        elif near := [(c, sim, stories[ids.index(c)]) for c, sim in candidates(v, it["source"], ids, stories, sources, matrix, aged)]:
            if near_copy(conn, near[0], now):
                story = near[0][2]
            else:  # its own story until Jev says otherwise
                strong = find_story(v, it["source"], ids, stories, sources, matrix)
                asks.append((it["id"], near, strong if strong not in aged else None))
        if story is None:
            story = it["id"]
            conn.execute("INSERT INTO stories VALUES (?, ?, ?, ?)", (story, it["id"], stamp, stamp))
        conn.execute(
            """INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched,
                                  story_id, embedding, always_show, rule, sample, image, quote, poster, via, images)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it["id"], it["url"], it["canonical"], it["title"], it["summary"], it["source"], it["author"],
             it["published"], stamp, story, v.tobytes(), it["always_show"], it.get("rule"), int(random.random() < SAMPLE_RATE),
             it.get("image"), json.dumps(it["quote"]) if it.get("quote") else None,
             json.dumps(it["poster"]) if it.get("poster") else None, json.dumps(it["via"]) if it.get("via") else None,
             json.dumps(it["images"]) if it.get("images") else None),
        )
        conn.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?)", (it["id"], it["source"], it["seen_at"], stamp))
        if p := it.get("points_to"):
            conn.execute("INSERT OR REPLACE INTO points_to VALUES (?, ?, ?, ?)",
                         (it["id"], p["url"], p["verdict"], json.dumps(p.get("via") or [])))
        ids.append(it["id"])
        stories.append(story)
        sources.append(it["source"])
        matrix = np.vstack([matrix, v[None, :]])
        touched.add(story)

    if asks:
        conn.commit()  # don't hold the write lock while Jev decides
        settle(conn, asks, touched)
    for story in touched:
        choose_lead(conn, story)
    conn.commit()
    return [it["id"] for it in fresh]


def near_copy(conn, candidate: tuple, now: datetime) -> bool:
    """Whether the closest candidate is so close, and so recent, that it's
    surely the same news (NEAR_COPY): it's joined without asking Jev."""
    iid, sim, _ = candidate
    if sim < NEAR_COPY:
        return False
    fetched = conn.execute("SELECT fetched FROM items WHERE id = ?", (iid,)).fetchone()[0]
    return fetched >= (now - NEAR_COPY_HOURS).isoformat(timespec="seconds")


def choose_leads(conn, ids: list[str]) -> None:
    """Choose again the leads of these items' stories: once their traits
    are in, the more significant version may lead."""
    reputation = source_reputation(conn)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for (story,) in conn.execute(f"SELECT DISTINCT story_id FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            choose_lead(conn, story, reputation, count=False)
    conn.commit()


def story_starts(conn, stories: list) -> dict[str, str]:
    """When each of these stories' first item was fetched."""
    out, ids = {}, sorted(set(stories))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out |= dict(conn.execute(f"SELECT story_id, MIN(fetched) FROM items WHERE story_id IN ({','.join('?' * len(chunk))}) "
                                 "GROUP BY story_id", chunk).fetchall())
    return out


def settle(conn, asks: list[tuple[str, list, str | None]], touched: set) -> None:
    """Jev confirms what similarity proposes. Each new item in `asks` (with
    its candidates, closest first: (item, similarity, its story then), and
    the story similarity alone would join) joins the story of the closest
    candidate Jev calls the same thing, and only that one: the item's own
    new story moves (with anything of this round that joined it), never an
    existing one, so a wrong "same" can't fuse two stories. Where Jev can't
    say, it joins the story similarity alone would join, as before."""
    wanted = sorted({x for a, near, _ in asks for x in (a, *(c for c, _, _ in near))})
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, title, summary, source FROM items WHERE id IN ({','.join('?' * len(wanted))})", wanted)}
    pairs = [(a, c) for a, near, _ in asks for c, _, _ in near if a in rows and c in rows]
    verdicts = dict(zip(pairs, same_stories([(rows[c], rows[a]) for a, c in pairs])))
    story_of = lambda iid: conn.execute("SELECT story_id FROM items WHERE id = ?", (iid,)).fetchone()[0]
    joined = apart = 0
    for a, near, strong in asks:
        own = story_of(a)
        if own != a:  # no longer the story it began (a link merged it): leave it be
            continue
        for c, sim, then in near:
            same = verdicts.get((a, c))
            if same or (same is None and then == strong):
                if (into := story_of(c)) != own:
                    merge_story(conn, into, own)
                    touched.discard(own)
                    touched.add(into)
                    joined += 1
                break
            apart += same is False and sim >= SAME_STORY
    if apart:
        count_follow(conn, Counter(kept_apart=apart))
    log.info("jev settled %d new items against %d candidates: %d joined a story, %d close ones kept apart",
             len(asks), len(pairs), joined, apart)


def source_reputation(conn, user: str | None = None) -> dict[str, float]:
    """(weighted ups + 1) / (weighted signals + 2) per source, from a
    reader's votes, bookmarks and opens (everyone's when `user` is None, as
    when choosing which version leads a story)."""
    signals = store.labels(conn, user)
    if not signals:
        return {}
    tally: dict[str, list[float]] = {}
    ids = list(signals)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for item, source in conn.execute(f"SELECT id, source FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            label, weight = signals[item]
            t = tally.setdefault(source, [0.0, 0.0])
            t[0] += weight * (label > 0)
            t[1] += weight
    return {source: (up + 1) / (total + 2) for source, (up, total) in tally.items()}


def significance(r) -> tuple[float, float]:
    """(importance 0..1, news report 0..1) from the item's traits (traits.py);
    halfway on each when it has none yet."""
    traits = store.json_or_none(r["traits"]) if "traits" in r.keys() else None
    traits = traits or {}
    levels = len(TRAITS["importance"]["criteria"]) - 1
    importance = traits["importance"]["score"] / levels if "importance" in traits else 0.5
    return importance, traits["news"]["noul"] if "news" in traits else 0.5


def best_version(rows: list, reputation: dict):
    """Of a story's items, the best version: the most significant news
    report, a fuller summary, first to report, a trusted source, fresh. A
    clearly more important version leads over a fuller or earlier one, and
    a preview, explainer or analysis over the news itself only when there's
    little else between them."""
    earliest = min(r["published"] for r in rows)
    newest = max(r["published"] for r in rows)

    def merit(r) -> float:
        importance, news = significance(r)
        return (
            0.35 * min(len(r["summary"]) / 600, 1.0)
            + 0.25 * (r["published"] == earliest)
            + 0.25 * reputation.get(r["source"], 0.5)
            + 0.15 * (r["published"] == newest)
            + 0.8 * importance
            + 0.25 * news
        )

    return max(rows, key=merit)


def wrappers(conn, ids: list[str]) -> dict[str, tuple[str, list]]:
    """Of these items, those that point to another of them and don't add
    value to it (a pointer, or undecided): {wrapper: (what it points to,
    the post that shared the wrapper, as `via`)}."""
    marks = ",".join("?" * len(ids))
    points = {r[0]: (item_id(r[1]), store.json_or_none(r[2]) or []) for r in conn.execute(
        f"SELECT item_id, url, via FROM points_to WHERE verdict IN ('pointer', 'undecided') AND item_id IN ({marks})", ids)}
    return {w: t for w, t in points.items() if t[0] in ids and t[0] != w}


def via_entry(row) -> dict:
    """An item, as a `via` entry of the item it points to."""
    url, poster = row["url"], store.json_or_none(row["poster"]) or {}
    kind = "bluesky" if url.startswith("https://bsky.app/") else "mastodon" if POST_URL.match(url) else "article"
    name = (poster.get("name") or row["author"] if kind != "article" else
            row["source"].removeprefix("rss:") if row["source"].startswith("rss:") else host(url))
    return {"url": url, "name": name or url, "kind": kind, "author": row["author"] or ""}


def choose_lead(conn, story: str, reputation: dict | None = None, count: bool = True) -> None:
    """The story's lead, for everyone: its best version. An item that only
    points to another of the story (follow.py: a pointer, or undecided)
    never leads over it, whatever its summary, date or source; it's added
    to the `via` of the item it points to instead. A wrapper that adds
    value is a version like any other. `count`: whether to count, for
    /status, a lead the wrapper rule chose (not when choosing again)."""
    reputation = reputation if reputation is not None else source_reputation(conn)
    rows = conn.execute("SELECT id, url, summary, source, published, author, poster, via, traits FROM items WHERE story_id = ?",
                        (story,)).fetchall()
    if not rows:
        return
    pointing = wrappers(conn, [r["id"] for r in rows])
    lead = best_version([r for r in rows if r["id"] not in pointing] or rows, reputation)["id"]
    if pointing:
        by_id = {r["id"]: r for r in rows}

        def end(w):  # the item a chain of wrappers comes to, and how far
            seen, steps = {w}, 0
            while w in pointing and pointing[w][0] not in seen:
                w = pointing[w][0]
                seen.add(w)
                steps += 1
            return w, steps

        for w in sorted(pointing, key=lambda w: -end(w)[1]):  # outermost first
            r = by_id[w]
            add_via(conn, end(w)[0], pointing[w][1] + (store.json_or_none(r["via"]) or []) + [via_entry(r)])
        if count and lead != best_version(rows, reputation)["id"]:  # the rule, not the scores, chose it
            count_follow(conn, Counter(original_leads=1))
    conn.execute("UPDATE stories SET lead_id = ?, updated = ? WHERE id = ?",
                 (lead, datetime.now(timezone.utc).isoformat(timespec="seconds"), story))
