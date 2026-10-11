"""Search: words and meaning, recent first, over what the reader was shown
(or everything from their sources)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from curator import search

DIM = 384


def unit(*hot: int) -> np.ndarray:
    v = np.zeros(DIM, np.float32)
    v[list(hot)] = 1
    return v / np.linalg.norm(v)


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def add(db, iid, title, *, source="mastodon:#astronomy", days=1.0, vec=None, summary="", story=None, shown=False):
    when = ago(days)
    db.execute("INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched, story_id, embedding) "
               "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)",
               (iid, f"https://x.example/{iid}", f"https://x.example/{iid}", title, summary, source, when, when,
                story or iid, (vec if vec is not None else unit(DIM - 1)).tobytes()))
    db.execute("INSERT INTO sightings VALUES (?, ?, ?, ?)", (iid, source, f"https://x.example/{iid}", when))
    if shown:
        db.execute("INSERT INTO feed_history VALUES ('alex', ?, ?, 'main', ?)", (story or iid, iid, when))
    db.commit()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    search._vectors.clear()
    monkeypatch.setattr(search, "query_vector", lambda text: unit(0))


def titles(results):
    return [r["title"] for r in results]


def test_finds_words_and_meaning_in_what_you_were_shown(alex, db):
    add(db, "a", "Comet Tsuchinshan brightens", shown=True)
    add(db, "b", "A new home for the telescope", vec=unit(0, 1), shown=True)  # about the query, no shared words
    add(db, "c", "Comet hunters gather", shown=False)  # never in the feed
    add(db, "d", "Gardening tips", shown=True)
    found = search.search(db, "alex", alex.sources["keys"], "comet")
    assert set(titles(found)) == {"Comet Tsuchinshan brightens", "A new home for the telescope"}
    assert all(r["picked"] for r in found)
    everything = search.search(db, "alex", alex.sources["keys"], "comet", everything=True)
    assert "Comet hunters gather" in titles(everything)
    assert next(r for r in everything if r["title"] == "Comet hunters gather")["picked"] is False


def test_recent_wins_between_similar_matches_but_a_much_better_old_one_still_shows(alex, db):
    add(db, "old", "Comet comet comet", days=40, vec=unit(0), shown=True)
    add(db, "new", "Comet seen", days=1, vec=unit(0, 1, 2), shown=True)
    add(db, "older", "Comet seen", days=20, vec=unit(0, 1, 2), shown=True)
    ids = [r["item"] for r in search.search(db, "alex", alex.sources["keys"], "comet")]
    assert ids.index("new") < ids.index("older")
    assert "old" in ids


def test_one_result_per_story_showing_the_version_you_saw(alex, db):
    add(db, "seen", "Comet over Perth", story="s1", shown=True)
    add(db, "other", "Comet over Perth: the full story", story="s1", vec=unit(0))
    found = search.search(db, "alex", alex.sources["keys"], "comet perth")
    assert [r["item"] for r in found] == ["seen"]


def test_other_readers_private_sources_stay_out(alex, db):
    add(db, "mine", "Comet news", source="mastodon:#astronomy")
    add(db, "theirs", "Comet news from Sam's timeline", source="mastodon:home:sam")
    found = search.search(db, "alex", alex.sources["keys"], "comet", everything=True)
    assert [r["item"] for r in found] == ["mine"]


def test_query_syntax_cant_break_it(alex, db):
    add(db, "a", "Comet", shown=True)
    for q in ['"', "comet AND", "NEAR(", "*", "-comet", "col:umn"]:
        search.search(db, "alex", alex.sources["keys"], q)  # no error
    assert search.search(db, "alex", alex.sources["keys"], "   ") == []


def test_the_word_index_follows_the_items(alex, db):
    add(db, "a", "Comet", shown=True)
    db.execute("UPDATE items SET title = 'Asteroid' WHERE id = 'a'")
    db.commit()
    count = lambda q: db.execute("SELECT count(*) FROM items_fts WHERE items_fts MATCH ?", (q,)).fetchone()[0]
    assert count("asteroid") == 1 and count("comet") == 0
    db.execute("DELETE FROM items WHERE id = 'a'")
    db.commit()
    assert count("asteroid") == 0


def test_publishing_records_what_was_in_the_feed(alex, db):
    stories = [{"story": "s1", "id": "a", "lane": "maybe", "first_fetched": ago(1)}]
    search.record(db, "alex", stories)
    search.record(db, "alex", [{**stories[0], "lane": "main"}])
    search.record(db, "alex", [{**stories[0], "id": "b", "lane": "maybe"}])
    assert tuple(db.execute("SELECT item_id, lane FROM feed_history WHERE story_id = 's1'").fetchone()) == ("b", "main")
