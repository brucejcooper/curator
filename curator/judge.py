"""Judge each new item, cheapest first: rules, then the two classifiers.

For everyone: language and hashtag-soup rules, and the item's traits
(traits.py). For each reader: their own rules (blocked domains, title
patterns), Jev's answers to their own questions with their profile as
context, combined with the traits into a score, and their own local model.
Both classifiers score everything; which one decides is a per-reader
setting, and both verdicts are kept."""

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

from curator import store
from curator.people import Profile
from curator.traits import ask, count_tokens, item_state

log = logging.getLogger(__name__)
NEWS_BONUS = 0.15
IMPORTANCE_WEIGHT = 0.9  # "major news" alone scores 0.9; significant news 0.68 (+ the news bonus)
# How hard each yes/no pushes an item down: a certain "yes" cuts the score by
# this share. Chatter and how-tos are softer: Jev flags some wanted items
# (a maker's own build log, a how-to for something the reader does use).
PENALTIES = {"rage_bait": 0.7, "advert": 0.7, "hype": 0.7, "chatter": 0.6, "generic_howto": 0.4}
# Language: fastText drops confidently non-English items for free; Jev's
# `english` trait settles the uncertain rest (tested 2026-10-01: fastText
# alone misfires below ~0.75 on short posts, Jev got those right).
LID_CONFIDENT = 0.75
HASHTAG = re.compile(r"(?<![\w&])#\w+")


@lru_cache(maxsize=1)
def _detector():
    from fast_langdetect import LangDetectConfig, LangDetector

    # The default reads only the first 80 characters; 400 halved the
    # uncertain cases on real posts and caught several the default missed.
    return LangDetector(LangDetectConfig(model="lite", max_input_length=400))


def language(text: str) -> tuple[str, float]:
    d = _detector().detect(text.replace("\n", " "))[0]
    return d["lang"], d["score"]


def hashtag_soup(text: str) -> bool:
    """Mostly hashtags: at least six, and fewer than three ordinary words for
    each of them."""
    tags = len(HASHTAG.findall(text))
    words = len(re.findall(r"\b[^\W\d_]{2,}\b", HASHTAG.sub(" ", text)))
    return tags >= 6 and words < tags * 3


def marks(ids) -> str:
    return ",".join("?" * len(ids))


# ── Rules ────────────────────────────────────────────────────────────────────

def apply_shared_rules(conn, ids: list[str]) -> None:
    """Rules for everyone: hashtag soup, and items confidently not in English.
    Not for items from always-show sources: someone chose to see all of those."""
    if not ids:
        return
    for row in conn.execute(f"SELECT id, title, summary FROM items WHERE id IN ({marks(ids)}) AND rule IS NULL AND NOT always_show",
                            ids).fetchall():
        text = f"{row['title']}. {row['summary'][:300]}"
        lang, confidence = language(text)
        rule = None
        if hashtag_soup(f"{row['title']} {row['summary']}"):
            rule = "hashtag soup"
        elif lang != "en" and confidence >= LID_CONFIDENT:
            rule = f"language {lang} ({confidence:.2f})"
        if rule:
            conn.execute("UPDATE items SET rule = ? WHERE id = ?", (rule, row["id"]))
    conn.commit()


def apply_reader_rules(conn, user: str, ids: list[str], rules: dict) -> None:
    """A reader's own rules (sources.yaml `rules:`): blocked_domains and drop_title_patterns."""
    blocked = {d.lower() for d in rules.get("blocked_domains") or []}
    patterns = [re.compile(p) for p in rules.get("drop_title_patterns") or []]
    if not ids or not (blocked or patterns):
        return
    for row in conn.execute(f"SELECT id, canonical, title FROM items WHERE id IN ({marks(ids)})", ids).fetchall():
        host = row["canonical"].split("/")[2]
        parts = host.split(".")
        rule = None
        if any(".".join(parts[i:]) in blocked for i in range(len(parts))):
            rule = f"blocked domain {host}"
        elif hit := next((p.pattern for p in patterns if p.search(row["title"])), None):
            rule = f"title matches {hit}"
        if rule:
            conn.execute("INSERT INTO judgments (user, item_id, rule) VALUES (?, ?, ?) "
                         "ON CONFLICT (user, item_id) DO UPDATE SET rule = excluded.rule", (user, row["id"], rule))
    conn.commit()


def visible(conn, keys: set[str], ids: list[str]) -> list[str]:
    """The items among `ids` seen in one of a reader's sources."""
    if not ids or not keys:
        return []
    rows = conn.execute(f"SELECT DISTINCT item_id FROM sightings WHERE item_id IN ({marks(ids)}) AND source IN ({marks(keys)})",
                        [*ids, *keys]).fetchall()
    return [r[0] for r in rows]


# ── Jev ──────────────────────────────────────────────────────────────────────

def jev_score(answers: dict, questions: dict, traits: dict | None = None) -> float:
    """0..1: the better of relevance to the reader and importance as news,
    plus a bonus for news reports, discounted when the item looks like rage
    bait, an advert, hype, chatter or a template how-to. `answers` are the
    reader's questions; `traits` the item-level ones (traits.py)."""
    from curator.traits import QUESTIONS as TRAIT_QUESTIONS

    every = {**(traits or {}), **answers}
    asked = {**TRAIT_QUESTIONS, **questions}
    level = lambda k: every[k]["score"] / max(len(asked[k]["criteria"]) - 1, 1)
    # Interesting to the reader, or important as news: a significant story
    # counts even off their hobbies.
    score = level("relevant")
    if "importance" in every:
        score = max(score, IMPORTANCE_WEIGHT * level("importance"))
    if "news" in every:
        score += NEWS_BONUS * max(0.0, every["news"]["noul"] - 0.5) * 2
    # A noul at 0.5 or below costs nothing; at 1.0 it costs its full share.
    cut = max((s * max(0.0, every[k]["noul"] - 0.5) * 2 for k, s in PENALTIES.items() if k in every), default=0.0)
    return max(0.0, min(1.0, score * (1 - cut)))


def judge_jev(conn, profile: Profile, ids: list[str]) -> None:
    """Asks the reader's own questions about the given items (those they can
    see and haven't had judged), with their profile as context."""
    user = profile.user
    mine = visible(conn, profile.sources["keys"], ids)
    if not mine:
        return
    rows = conn.execute(
        f"""SELECT i.* FROM items i LEFT JOIN judgments j ON j.item_id = i.id AND j.user = ?
            WHERE i.id IN ({marks(mine)}) AND i.rule IS NULL AND j.rule IS NULL AND j.jev IS NULL""",
        [user, *mine]).fetchall()
    if not rows:
        return
    questions = profile.questions
    traits = {r["id"]: json.loads(r["traits"]) if r["traits"] else None for r in rows}
    results = asyncio.run(ask(rows, questions, lambda r: {"item": item_state(r), "reader": {"profile": profile.profile_md}}))
    for iid, resp in results:
        if resp is None:
            continue
        conn.execute("INSERT INTO judgments (user, item_id, jev, jev_score) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (user, item_id) DO UPDATE SET jev = excluded.jev, jev_score = excluded.jev_score",
                     (user, iid, json.dumps(resp), jev_score(resp["answers"], questions, traits[iid])))
    count_tokens(conn, results)
    conn.commit()
    log.info("jev judged %d items for %s", len(rows), user)


# ── Local model ──────────────────────────────────────────────────────────────

FEATURES = ("interest", "dislike", "liked", "disliked", "source", "outlets", "age")


def profile_topics(profile_md: str) -> tuple[list[str], list[str]]:
    """Bullets under the interest headings, and under "Push down or drop"."""
    likes, dislikes, section = [], [], ""
    for line in profile_md.splitlines():
        if line.startswith("## "):
            section = line[3:].lower()
        elif line.lstrip().startswith("- ") or (section.startswith("this week") and line.strip()):
            text = re.sub(r"[*_`]", "", line.strip().removeprefix("- "))
            if section.startswith("push down"):
                dislikes.append(text)
            elif section.startswith(("live", "standing", "this week")):
                likes.append(text)
    return likes, dislikes


class LocalModel:
    """Embedding similarity to the reader's profile and to items they liked
    or didn't, plus source and story features. Logistic regression once they
    have enough signals; a plain interest-minus-dislike heuristic until then.
    Every verdict records which features drove it."""

    def __init__(self, conn, profile: Profile):
        from curator.stories import embed, source_reputation

        likes, dislikes = profile_topics(profile.profile_md)
        self.likes, self.dislikes = embed(likes), embed(dislikes)
        self.reputation = source_reputation(conn, profile.user)
        signals = store.labels(conn, profile.user)  # votes, bookmarks, opens
        ids = list(signals)
        voted = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            voted += conn.execute(f"SELECT * FROM items WHERE embedding IS NOT NULL AND id IN ({marks(chunk)})", chunk).fetchall()
        label = {r["id"]: signals[r["id"]] for r in voted}
        self.up = np.stack([store.vec(r["embedding"]) for r in voted if label[r["id"]][0] > 0] or [np.zeros(384, np.float32)])
        self.down = np.stack([store.vec(r["embedding"]) for r in voted if label[r["id"]][0] < 0] or [np.zeros(384, np.float32)])
        self.conn = conn
        self.clf = None
        ups = sum(label[r["id"]][0] > 0 for r in voted)
        if ups >= 5 and len(voted) - ups >= 5 and len(voted) >= 20:
            from sklearn.linear_model import LogisticRegression

            # Leave-one-out for the voted item's own similarity to the voted set.
            X = np.array([self.features(r, exclude_self=True) for r in voted])
            y = np.array([label[r["id"]][0] > 0 for r in voted])
            w = np.array([label[r["id"]][1] for r in voted])
            self.clf = LogisticRegression(class_weight="balanced", max_iter=1000).fit(X, y, sample_weight=w)
        self.kind = "logistic" if self.clf is not None else "heuristic"

    def features(self, row, exclude_self: bool = False) -> list[float]:
        v = store.vec(row["embedding"])

        def best(m):
            sims = m @ v
            if exclude_self:
                sims = sims[sims < 0.999]
            return float(sims.max()) if sims.size else 0.0

        outlets = self.conn.execute("SELECT COUNT(*) FROM items WHERE story_id = ?", (row["story_id"],)).fetchone()[0]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["published"])).total_seconds() / 3600
        return [
            best(self.likes), best(self.dislikes), best(self.up), best(self.down),
            self.reputation.get(row["source"], 0.5), math.log1p(outlets), min(age / 72, 1.0),
        ]

    def judge(self, row) -> tuple[float, dict]:
        x = self.features(row)
        if self.clf is None:
            raw = (x[0] - x[1]) * 6 + (x[2] - x[3]) * 2
            score = 1 / (1 + math.exp(-raw))
            contrib = {"interest": x[0] * 6, "dislike": -x[1] * 6, "liked": x[2] * 2, "disliked": -x[3] * 2}
        else:
            score = float(self.clf.predict_proba([x])[0, 1])
            contrib = dict(zip(FEATURES, (self.clf.coef_[0] * np.array(x)).tolist()))
        top = dict(sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:3])
        return score, {"kind": self.kind, "features": dict(zip(FEATURES, x)), "drivers": top}


def judge_local(conn, profile: Profile, ids: list[str] | None = None) -> None:
    """The reader's local model on the given items, or (after a vote or a
    profile change) everything of theirs from the last 72 hours."""
    from curator.stories import WINDOW

    if ids is None:
        cutoff = (datetime.now(timezone.utc) - WINDOW).isoformat(timespec="seconds")
        ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE rule IS NULL AND fetched >= ?", (cutoff,))]
    mine = visible(conn, profile.sources["keys"], ids)
    if not mine:
        return
    model = LocalModel(conn, profile)
    rows = conn.execute(f"SELECT * FROM items WHERE id IN ({marks(mine)}) AND rule IS NULL AND embedding IS NOT NULL", mine).fetchall()
    for row in rows:
        score, detail = model.judge(row)
        conn.execute("INSERT INTO judgments (user, item_id, local, local_score) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (user, item_id) DO UPDATE SET local = excluded.local, local_score = excluded.local_score",
                     (profile.user, row["id"], json.dumps(detail), score))
    conn.commit()


def judge_for(conn, profile: Profile, ids: list[str]) -> None:
    """Everything one reader needs judged about some new items."""
    mine = visible(conn, profile.sources["keys"], ids)
    if not mine:
        return
    apply_reader_rules(conn, profile.user, mine, profile.sources["rules"])
    judge_jev(conn, profile, mine)
    judge_local(conn, profile, mine)
