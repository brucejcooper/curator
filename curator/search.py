"""Search a reader's articles by words and by meaning, recent ones first.

Two signals: SQLite full-text search (exact words, names, phrases) and the
items' embeddings (so "home battery" finds the Powerwall story). A match's
relevance is weighted by its age: a good match from yesterday beats a slightly
better one from three weeks ago, but a strong old match can still surface.

By default it searches what the curator picked for the reader (feed_history:
every story that has been in their feed); `everything` widens it to all the
items from their sources, picked or not. One result per story."""

import math
import re
from datetime import datetime, timezone

import numpy as np

from curator import store
from curator.reader import annotate, without_quote

HALF_LIFE_DAYS = 10  # recency weight halves every ten days...
RECENCY_FLOOR = 0.35  # ...down to this, so old matches still count for something
# Cosine similarity of a query to an item (bge-small): unrelated text sits
# around 0.4-0.55. Below SEM_MIN, meaning alone isn't a match. Short queries
# squeeze real matches into a narrow band above that (0.62-0.68 for "home
# battery"), so meaning is scaled against the query's best match.
SEM_MIN = 0.62
MEANING_WEIGHT = 0.6  # the rest is word matches
SHARPEN = 2  # relevance is raised to this, so a clearly better match outweighs a few days of age
LIMIT = 40
MIN_SCORE = 0.02  # below this (or TAIL of the best result's score), a result is noise
TAIL = 0.05
WORD = re.compile(r"\w+", re.UNICODE)

_vectors: dict[str, np.ndarray] = {}  # item id -> embedding, filled as searches need them


def fts_query(text: str) -> str | None:
    """Every word must appear (as a prefix, so "batter" finds "battery");
    words are quoted so FTS syntax in the query can't break it."""
    words = WORD.findall(text.lower())
    return " ".join(f'"{w}"*' for w in words) or None


def query_vector(text: str) -> np.ndarray:
    from curator.stories import _model

    model = _model()
    embed = getattr(model, "query_embed", model.embed)  # bge's query prefix, where fastembed has it
    v = np.array(next(iter(embed([text]))), dtype=np.float32)
    return v / np.linalg.norm(v)


def vectors(conn, ids: list[str]) -> dict[str, np.ndarray]:
    missing = [i for i in ids if i not in _vectors]
    for n in range(0, len(missing), 500):
        chunk = missing[n:n + 500]
        for r in conn.execute(f"SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND id IN ({','.join('?' * len(chunk))})", chunk):
            _vectors[r["id"]] = store.vec(r["embedding"])
    return {i: _vectors[i] for i in ids if i in _vectors}


def scope(conn, user: str, keys: set[str], everything: bool) -> dict[str, str]:
    """item id -> story id, for the items the search covers."""
    if everything:
        keys = sorted(keys)
        rows = conn.execute(
            f"""SELECT DISTINCT i.id, i.story_id FROM items i JOIN sightings sg ON sg.item_id = i.id
                WHERE sg.source IN ({','.join('?' * len(keys))})""", keys)
    else:
        rows = conn.execute(
            """SELECT i.id, i.story_id FROM items i JOIN feed_history f ON f.story_id = i.story_id AND f.user = ?
               UNION SELECT i.id, i.story_id FROM items i JOIN user_bookmarks b ON b.item_id = i.id AND b.user = ?""",
            (user, user))
    return {r[0]: r[1] for r in rows}


def recency(published: str | None, now: datetime) -> float:
    try:
        when = datetime.fromisoformat(published)
    except (TypeError, ValueError):
        return RECENCY_FLOOR
    age = max(0.0, (now - when).total_seconds() / 86400)
    return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (age / HALF_LIFE_DAYS)


def search(conn, user: str, keys: set[str], text: str, everything: bool = False, limit: int = LIMIT) -> list[dict]:
    text = " ".join(text.split())[:200]
    if not text:
        return []
    covered = scope(conn, user, keys, everything)
    if not covered:
        return []

    # Words: bm25 is negative, more negative is better; scaled to 0..1.
    words: dict[str, float] = {}
    q = fts_query(text)
    if q:
        hits = conn.execute("SELECT i.id, bm25(items_fts, 3.0, 1.0, 1.0) AS rank FROM items_fts JOIN items i ON i.rowid = items_fts.rowid "
                            "WHERE items_fts MATCH ? ORDER BY rank LIMIT 1000", (q,)).fetchall()
        hits = [(r["id"], -r["rank"]) for r in hits if r["id"] in covered]
        best = max((s for _, s in hits), default=0) or 1
        words = {i: s / best for i, s in hits}

    # Meaning.
    ids = list(covered)
    vecs = vectors(conn, ids)
    have = [i for i in ids if i in vecs]
    cos = dict(zip(have, (np.stack([vecs[i] for i in have]) @ query_vector(text)).tolist())) if have else {}

    top = max(cos.values(), default=0)
    now = datetime.now(timezone.utc)
    best_per_story: dict[str, tuple[float, str]] = {}
    matched = [i for i in ids if i in words or cos.get(i, 0) >= SEM_MIN]
    published = {}
    for n in range(0, len(matched), 500):
        chunk = matched[n:n + 500]
        for r in conn.execute(f"SELECT id, published, fetched FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            published[r["id"]] = r["published"] or r["fetched"]
    for i in matched:
        meaning = max(0.0, (cos.get(i, 0) - SEM_MIN) / (top - SEM_MIN)) if top > SEM_MIN else 0.0
        relevance = MEANING_WEIGHT * meaning + (1 - MEANING_WEIGHT) * words.get(i, 0)
        score = relevance ** SHARPEN * recency(published.get(i), now)
        story = covered[i] or i
        if score > best_per_story.get(story, (-math.inf, ""))[0]:
            best_per_story[story] = (score, i)
    ranked = sorted(best_per_story.items(), key=lambda kv: -kv[1][0])[:limit]
    floor = max(MIN_SCORE, ranked[0][1][0] * TAIL) if ranked else 0
    ranked = [r for r in ranked if r[1][0] >= floor]
    return entries(conn, user, [(story, item, score) for story, (score, item) in ranked])


def entries(conn, user: str, results: list[tuple[str, str, float]]) -> list[dict]:
    """Cards for the app: the version of the story the reader was shown, where
    it was in their feed; else the item that matched."""
    if not results:
        return []
    stories = [s for s, _, _ in results]
    marks = ",".join("?" * len(stories))
    shown = dict(conn.execute(f"SELECT story_id, item_id FROM feed_history WHERE user = ? AND story_id IN ({marks})", (user, *stories)).fetchall())
    show = [shown.get(story) or item for story, item, _ in results]
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT id, url, title, summary, image, source, published, author, quote, poster FROM items
            WHERE id IN ({','.join('?' * len(show))})""", show)}
    out = []
    for (story, _, score), item in zip(results, show):
        r = rows.get(item)
        if not r:
            continue
        quote = store.json_or_none(r["quote"])
        out.append({"kind": "article", "id": f"search-{item}", "item": item, "url": r["url"], "title": r["title"],
                    "summary": without_quote(r["summary"] or "", quote), "quote": quote, "poster": store.json_or_none(r["poster"]),
                    "image": r["image"] or None, "published": r["published"], "author": r["author"] or "", "source": r["source"],
                    "lane": "main", "reason": "", "also": [], "picked": story in shown, "score": round(score, 3)})
    return annotate(conn, user, out)


def record(conn, user: str, stories: list[dict]) -> None:
    """Notes the stories in a reader's feed, so search can find what they
    were shown long after it has left the feed."""
    conn.executemany(
        """INSERT INTO feed_history (user, story_id, item_id, lane, added) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (user, story_id) DO UPDATE SET item_id = excluded.item_id,
             lane = CASE WHEN feed_history.lane = 'main' THEN 'main' ELSE excluded.lane END""",
        [(user, s["story"], s["id"], s["lane"], s["first_fetched"]) for s in stories])


def backfill(conn, profile) -> int:
    """Before feed_history existed: every story that cleared the reader's bar
    (or came from an always-show source), as if it had been in their feed.
    Looser than the feed itself (no daily quota), which suits search."""
    from curator import people
    from curator.publish import candidates, margins

    src = profile.sources
    stories = candidates(conn, profile.user, src["keys"], "")
    margins(conn, profile.user, stories, src["min_score"], src["source_floors"], src["always"], src["trial"])
    main = people.decider(conn, profile.user)
    picked = [{**s, "lane": "main"} for s in stories
              if s["story_always"] or (s[f"margin_{main}"] is not None and s[f"margin_{main}"] >= 0)]
    conn.executemany("INSERT OR IGNORE INTO feed_history (user, story_id, item_id, lane, added) VALUES (?, ?, ?, ?, ?)",
                     [(profile.user, s["story"], s["id"], s["lane"], s["first_fetched"]) for s in picked])
    return len(picked)
