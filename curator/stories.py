"""Resolve the story: drop exact duplicates, group items about the same story
by embedding similarity, and pick the most informative version as the lead."""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np

from curator import store
from curator.articles import fill_text, needs_text
from curator.canon import canonical_url, item_id, title_key

log = logging.getLogger(__name__)
EMBED_MODEL = os.environ.get("CURATOR_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
# bge-small similarities sit high: same-story pairs from different outlets
# measured 0.87-0.99, while different stories from one outlet reached 0.86.
SAME_STORY = float(os.environ.get("CURATOR_SAME_STORY", "0.87"))
SAME_SOURCE = 0.93  # an outlet's different stories can read alike, so it takes more to merge outright
FIRST_ITEM_SLACK = 0.05  # also close to the story's first item, so stories don't chain
# Below SAME_STORY, wording can differ a lot for the same news ("Apple TV just
# added free classic movies" vs "Apple TV updated its 'Bonus' movies"
# measured 0.75). Pairs from different sources down to MAYBE_SAME get asked
# of Jev instead.
MAYBE_SAME = 0.72
# An outlet often runs a story again with a new headline or copy, or splits
# one event into several articles (one per OS on a beta day: 0.81-0.86).
# Pairs from one source down to MAYBE_SAME_SOURCE get asked of Jev too.
MAYBE_SAME_SOURCE = 0.78
GREY_ASKS = 3  # earlier stories to ask about, per new item
JEV_SAME = 0.6
WINDOW = timedelta(hours=72)
SAMPLE_RATE = 0.03  # share of items eligible for the maybe lane's random sample


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


def embed(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    vecs = np.array(list(_model().embed(texts)), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


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


def grey_matches(v: np.ndarray, source: str, story: str | None, ids, stories, sources, matrix) -> list[str]:
    """The closest earlier items in the grey zone (could be the same story,
    Jev decides), one per other story, up to GREY_ASKS. Returns their ids."""
    if not len(matrix):
        return []
    sims = matrix @ v
    found, seen = [], {story}
    for i in np.argsort(-sims):
        if sims[i] < min(MAYBE_SAME, MAYBE_SAME_SOURCE) or len(found) == GREY_ASKS:
            break
        if stories[i] in seen:
            continue
        if sources[i] == source:
            if sims[i] < MAYBE_SAME_SOURCE or sims[i] >= SAME_SOURCE:
                continue
        elif sims[i] < MAYBE_SAME or sims[i] >= SAME_STORY:
            continue
        seen.add(stories[i])
        found.append(ids[i])
    return found


def same_story(pairs: list[tuple[dict, dict]]) -> list[bool]:
    """Asks Jev whether each pair of items reports the same story."""
    if not pairs:
        return []
    from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

    from curator.secrets import get_secret

    question = {"same": {
        "type": "noul",
        "instructions": "Do `a` and `b` report the same news story or event (the same announcement, release, incident or "
                        "finding), even if worded differently, from different outlets, or one outlet posting it again? Count "
                        "separate articles about parts of one announcement or release as the same event (e.g. one article "
                        "per product in the same launch, or per operating system in the same set of updates), since a "
                        "reader of one would find the other redundant.",
        "criteria": {"true": "The same story or event", "false": "Different stories, even if on a similar topic"},
    }}

    async def run():
        sem = asyncio.Semaphore(8)
        async with AsyncTypeSafeClient(api_key=get_secret("JEV_API_KEY"), model="jev-latest") as client:
            async def one(a, b):
                state = {k: {"title": x["title"], "summary": x["summary"][:400], "source": x["source"]} for k, x in (("a", a), ("b", b))}
                async with sem:
                    try:
                        r = await client.system_one(state=state, questions=question)
                        return r.model_dump()["answers"]["same"]["noul"] >= JEV_SAME
                    except TypeSafeError:
                        return False
            return await asyncio.gather(*(one(a, b) for a, b in pairs))

    return asyncio.run(run())


def merge_story(conn, into: str, story: str) -> None:
    if into == story:
        return
    conn.execute("UPDATE items SET story_id = ? WHERE story_id = ?", (into, story))
    conn.execute("DELETE FROM stories WHERE id = ?", (story,))


def find_story(v: np.ndarray, source: str, ids, stories, sources, matrix) -> str | None:
    """The story of the closest earlier item that passes the thresholds."""
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
        if first is None or sims[first] >= SAME_STORY - FIRST_ITEM_SLACK:
            return stories[i]
    return None


def ingest(conn, found: list[dict]) -> list[str]:
    """Store new items; returns the ids that are new."""
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    ids, stories, sources, titles, matrix = recent(conn, now)
    fresh: list[dict] = []
    for it in found:
        iid = item_id(it["url"])
        existing = conn.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone()
        twin = titles.get(title_key(it["title"])) if len(it["title"]) > 20 else None
        target = iid if existing else twin
        if target is None and any(f["id"] == iid for f in fresh):
            target = iid
        if target:
            conn.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?)", (target, it["source"], it["url"], stamp))
            continue
        it = {**it, "id": iid, "canonical": canonical_url(it["url"]), "fetched": stamp}
        fresh.append(it)
        titles[title_key(it["title"])] = iid

    if any(needs_text(it) for it in fresh):
        conn.commit()  # don't hold the write lock while fetching articles
        fill_text(fresh)
    vectors = embed([text_of(it) for it in fresh])
    touched: set[str] = set()
    grey: list[tuple[str, str]] = []  # (new item, earlier item) for Jev to settle
    for it, v in zip(fresh, vectors):
        story = find_story(v, it["source"], ids, stories, sources, matrix)
        grey += [(it["id"], other) for other in grey_matches(v, it["source"], story, ids, stories, sources, matrix)]
        if story is None:
            story = it["id"]
            conn.execute("INSERT INTO stories VALUES (?, ?, ?, ?)", (story, it["id"], stamp, stamp))
        conn.execute(
            """INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched,
                                  story_id, embedding, always_show, rule, sample, image, quote, poster)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it["id"], it["url"], it["canonical"], it["title"], it["summary"], it["source"], it["author"],
             it["published"], stamp, story, v.tobytes(), it["always_show"], it.get("rule"), int(random.random() < SAMPLE_RATE),
             it.get("image"), json.dumps(it["quote"]) if it.get("quote") else None,
             json.dumps(it["poster"]) if it.get("poster") else None),
        )
        conn.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?)", (it["id"], it["source"], it["url"], stamp))
        ids.append(it["id"])
        stories.append(story)
        sources.append(it["source"])
        matrix = np.vstack([matrix, v[None, :]])
        touched.add(story)

    if grey:
        conn.commit()  # don't hold the write lock while Jev decides
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id, title, summary, source, story_id FROM items WHERE id IN ({','.join('?' * (2 * len(grey)))})",
            [x for pair in grey for x in pair])}
        pairs = [(a, b) for a, b in grey if a in rows and b in rows]
        verdicts = same_story([(rows[a], rows[b]) for a, b in pairs])
        for (a, b), same in zip(pairs, verdicts):
            if same:
                story = conn.execute("SELECT story_id FROM items WHERE id = ?", (a,)).fetchone()[0]
                into = conn.execute("SELECT story_id FROM items WHERE id = ?", (b,)).fetchone()[0]
                if story == into:
                    continue
                merge_story(conn, into, story)
                touched.discard(story)
                touched.add(into)
        log.info("jev settled %d possible duplicates: %d the same story", len(pairs), sum(verdicts))
    for story in touched:
        choose_lead(conn, story)
    conn.commit()
    return [it["id"] for it in fresh]


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


def best_version(rows: list, reputation: dict):
    """Of a story's items, the best version: a fuller summary, first to
    report, a trusted source, fresh."""
    earliest = min(r["published"] for r in rows)
    newest = max(r["published"] for r in rows)

    def merit(r) -> float:
        return (
            0.35 * min(len(r["summary"]) / 600, 1.0)
            + 0.25 * (r["published"] == earliest)
            + 0.25 * reputation.get(r["source"], 0.5)
            + 0.15 * (r["published"] == newest)
        )

    return max(rows, key=merit)


def choose_lead(conn, story: str, reputation: dict | None = None) -> None:
    """The story's lead, for everyone: its best version."""
    reputation = reputation if reputation is not None else source_reputation(conn)
    rows = conn.execute("SELECT id, summary, source, published FROM items WHERE story_id = ?", (story,)).fetchall()
    if not rows:
        return
    lead = best_version(rows, reputation)["id"]
    conn.execute("UPDATE stories SET lead_id = ?, updated = ? WHERE id = ?",
                 (lead, datetime.now(timezone.utc).isoformat(timespec="seconds"), story))
