"""What counts as the same story (#41): only pieces that report the same
thing. Similarity proposes and Jev confirms; a preview and the event it
previews are two stories at any time apart; a story stops taking items 72
hours after its first; the lead weighs significance."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from curator import reader
from curator.canon import item_id
from curator.publish import publish, sort_id
from curator.stories import best_version, ingest

SKY = "rss:Sky News Feed"
OTHER = "rss:Other"


def unit(*xs):
    v = np.zeros(384, dtype=np.float32)
    v[: len(xs)] = xs
    return v / np.linalg.norm(v)


# By an item's first word. The preview and the event read alike (0.94), as
# they did live (up to 0.90): past SAME_STORY, short of NEAR_COPY. "again"
# is a near-copy of the event (0.999); "grey" is in the grey zone of the
# preview (0.75). "alpha" and "beta" are two stories "both" is close to.
VECTORS = {"preview": unit(1, 0.35), "event": unit(1, 0), "again": unit(1, 0.05), "grey": unit(1, -0.4),
           "alpha": unit(1, 0.4), "beta": unit(1, -0.5), "both": unit(1, 0), "other": unit(0, 1, 1)}
PREVIEW = "preview: Trump, Xi and Putin to meet in Beijing"
EVENT = "event: Trump, Xi and Putin meet in Beijing"


class Jev:
    """Jev, stubbed: `same(earlier, new)` answers for a pair of titles
    (None: Jev couldn't say). Records what it was asked."""

    def __init__(self):
        self.asked = []
        self.same = lambda earlier, new: True

    def __call__(self, pairs):
        self.asked += [(a["title"], b["title"]) for a, b in pairs]
        return [self.same(a["title"], b["title"]) for a, b in pairs]


@pytest.fixture
def jev(monkeypatch):
    j = Jev()
    monkeypatch.setattr("curator.stories.same_stories", j)
    monkeypatch.setattr("curator.stories.embed", lambda texts: np.stack([VECTORS[t.split()[0].rstrip(":")] for t in texts]))
    return j


def item(title, source=SKY, **more):
    return {"url": f"https://news.example/{title.split(':')[0]}-{abs(hash(title))}", "title": title,
            "summary": f"{title}: what happened.", "source": source, "author": "Staff",
            "published": datetime.now(timezone.utc).isoformat(timespec="seconds"), "always_show": 0, **more}


def add(db, *items):
    ingest(db, list(items), follow_wrappers=False)


def age(db, title, hours):
    """Makes an item `hours` older."""
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    db.execute("UPDATE items SET fetched = ?, published = ? WHERE title = ?", (when, when, title))
    db.commit()


def story(db, title):
    return db.execute("SELECT story_id FROM items WHERE title = ?", (title,)).fetchone()[0]


def kv_count(db, key):
    row = db.execute("SELECT value FROM kv WHERE key LIKE 'follow:%'").fetchone()
    return json.loads(row[0]).get(key, 0) if row else 0


@pytest.mark.parametrize("hours", [20, 2])
def test_preview_and_event_are_separate_stories(alex, db, jev, hours):
    # Bruce on the Trump–Xi–Putin case: "One was speculation, the other the actual thing."
    jev.same = lambda earlier, new: False
    add(db, item(PREVIEW))
    age(db, PREVIEW, hours)
    add(db, item(EVENT))
    assert jev.asked == [(PREVIEW, EVENT)]  # close, but not joined on similarity alone
    assert story(db, EVENT) != story(db, PREVIEW)
    assert kv_count(db, "kept_apart") == 1

    db.executemany("INSERT INTO judgments (user, item_id, jev_score, local_score) VALUES ('alex', ?, 0.95, 0.9)",
                   [(r[0],) for r in db.execute("SELECT id FROM items")])
    db.commit()
    publish(db, alex)
    arts = {a["title"]: a for a in reader.articles("alex")}
    assert arts[EVENT]["id"] != arts[PREVIEW]["id"]  # a sort_id of its own, from when it came
    assert int(arts[EVENT]["id"]) > int(arts[PREVIEW]["id"])
    # The reader read past the preview; the event comes after it, as news.
    reader.set_position(db, "alex", arts[PREVIEW]["id"])
    assert [a["title"] for a in reader.after(db, alex, reader.position(db, "alex"))["items"]] == [EVENT]


def test_same_hour_duplicates_from_two_outlets_are_one_story(db, jev):
    add(db, item(EVENT))
    add(db, item("again: Trump, Xi and Putin meet", source=OTHER))  # a near-copy: not asked
    add(db, item("grey: leaders gather in Beijing", source=OTHER))  # Jev: the same
    assert jev.asked == [(EVENT, "grey: leaders gather in Beijing")]
    assert len({story(db, t) for t in (EVENT, "again: Trump, Xi and Putin meet", "grey: leaders gather in Beijing")}) == 1
    assert db.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1


def test_a_late_duplicate_of_the_same_news_still_joins(db, jev):
    add(db, item(EVENT))
    age(db, EVENT, 20)
    add(db, item("again: Trump, Xi and Putin meet", source=OTHER))  # past NEAR_COPY, but not within hours: asked
    assert jev.asked == [(EVENT, "again: Trump, Xi and Putin meet")]
    assert story(db, "again: Trump, Xi and Putin meet") == story(db, EVENT)


def test_an_item_joins_only_the_closest_story_jev_confirms(db, jev):
    jev.same = lambda earlier, new: new.startswith("both")
    add(db, item("alpha: one story"), item("beta: another story", source=OTHER))
    alpha, beta = story(db, "alpha: one story"), story(db, "beta: another story")
    assert alpha != beta
    add(db, item("both: close to each", source=OTHER))
    assert [a for a, _ in jev.asked] == ["alpha: one story", "beta: another story"]  # closest first
    assert story(db, "both: close to each") == alpha
    assert story(db, "beta: another story") == beta and db.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2


def test_a_link_joins_however_late_and_asks_nothing(db, jev):
    add(db, item(PREVIEW))
    age(db, PREVIEW, 20)
    original = db.execute("SELECT url FROM items").fetchone()[0]
    add(db, item("other: someone shares it", points_to={"url": original, "verdict": "value"}))
    assert jev.asked == []
    assert story(db, "other: someone shares it") == story(db, PREVIEW) == item_id(original)


def test_a_story_takes_no_items_past_its_window(db, jev):
    # The first item is 80 h old, a later one is still in the window: before,
    # anything close to that later one joined, and stories chained for days.
    add(db, item(PREVIEW))
    age(db, PREVIEW, 70)
    add(db, item("again: the preview, again", source=OTHER))  # Jev: the same
    old = story(db, PREVIEW)
    assert story(db, "again: the preview, again") == old
    age(db, PREVIEW, 80)
    age(db, "again: the preview, again", 30)
    jev.asked.clear()
    add(db, item(EVENT))
    assert jev.asked == []
    assert story(db, EVENT) != old


def test_when_jev_cant_say_similarity_decides_as_before(db, jev):
    jev.same = lambda earlier, new: None
    add(db, item(PREVIEW))
    age(db, PREVIEW, 20)
    add(db, item(EVENT, source=OTHER), item("grey: leaders gather in Beijing", source=OTHER))
    assert story(db, EVENT) == story(db, PREVIEW)  # past SAME_STORY: joins
    assert story(db, "grey: leaders gather in Beijing") != story(db, PREVIEW)  # grey: doesn't
    assert kv_count(db, "kept_apart") == 0


def test_the_lead_weighs_significance():
    def row(iid, summary, published, importance=None, news=None):
        traits = {"importance": {"score": importance}, "news": {"noul": news}} if importance is not None else None
        return {"id": iid, "summary": summary, "source": SKY, "published": published,
                "traits": json.dumps(traits) if traits else None}

    explainer = row("explainer", "x" * 600, "2026-11-17T19:50:00+00:00", importance=2, news=0.1)
    event = row("event", "x" * 200, "2026-11-18T16:00:00+00:00", importance=4, news=0.95)
    assert best_version([explainer, event], {})["id"] == "event"
    # Without traits, as before: the fuller, earlier version.
    plain = [{**explainer, "traits": None}, {**event, "traits": None}]
    assert best_version(plain, {})["id"] == "explainer"
    assert best_version([{k: v for k, v in r.items() if k != "traits"} for r in plain], {})["id"] == "explainer"


def test_the_lead_is_chosen_again_once_traits_are_in(db, jev):
    from curator.stories import choose_leads

    add(db, item(EVENT, summary="A long look at the meeting. " * 40))
    add(db, item("again: Trump, Xi and Putin meet", source=OTHER))
    lead = lambda: db.execute("SELECT title FROM items WHERE id = (SELECT lead_id FROM stories)").fetchone()[0]
    assert lead() == EVENT
    for title, importance, news in ((EVENT, 2, 0.1), ("again: Trump, Xi and Putin meet", 4, 0.95)):
        db.execute("UPDATE items SET traits = ? WHERE title = ?",
                   (json.dumps({"importance": {"score": importance}, "news": {"noul": news}}), title))
    choose_leads(db, [r[0] for r in db.execute("SELECT id FROM items WHERE source = ?", (OTHER,))])
    assert lead() == "again: Trump, Xi and Putin meet"
