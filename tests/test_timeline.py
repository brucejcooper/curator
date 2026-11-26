"""The timeline is read from feed_history (#46): the stories in the reader's
feed now, with the per-reader values publishing stored (lane, timeline id,
"Why this?", trial), in the same shape the app had from feed.json. feed.json
is written from the same rows, for other feed readers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import docs
from curator import people, reader, search
from curator.publish import FEED_DAYS, feed_folder, publish

SKY = "rss:Sky News Feed"
TRIAL = "rss:Trial Blog"


def ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def add(db, iid, story, source, hours, jev=None, local=None, **more):
    when = ago(hours)
    row = {"id": iid, "url": f"https://n.example/{iid}", "canonical": f"https://n.example/{iid}", "title": f"Title {iid}",
           "summary": f"What happened in {iid}.", "source": source, "author": "Staff", "published": when, "fetched": when,
           "story_id": story, "image": "", **more}
    db.execute(f"INSERT INTO items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    db.execute("INSERT INTO sightings VALUES (?, ?, ?, ?)", (iid, source, row["url"], when))
    db.execute("INSERT OR IGNORE INTO stories VALUES (?, ?, ?, ?)", (story, iid, when, when))
    if jev is not None or local is not None:
        verdict = json.dumps({"answers": {"relevant": {"score": 4, "legend": {"4": "very"}}}}) if jev is not None else None
        db.execute("INSERT INTO judgments (user, item_id, jev, jev_score, local_score) VALUES ('alex', ?, ?, ?, ?)",
                   (iid, verdict, jev, local))


@pytest.fixture
def fed(db, alex, monkeypatch):
    """Alex's feed, published the normal way: a main-lane story with other
    coverage, a quote post, the other classifier's pick (maybe), one from a
    source on trial, an always-show one, and one too old for the feed."""
    monkeypatch.setattr("curator.images.fill_images", lambda conn, stories: None)
    profile_md, sources_yaml, questions_yaml = docs()
    sources_yaml = sources_yaml.replace("learned: []", "learned:\n  - {kind: rss, value: 'https://trial.example/feed', "
                                                       "name: Trial Blog, trial_until: '2099-01-01'}")
    people.add_version(db, "alex", "user", "trial", profile_md, sources_yaml, questions_yaml)
    add(db, "a1", "a0a0", SKY, 5, jev=0.95, local=0.2, image="https://n.example/a1.jpg")
    add(db, "a2", "a0a0", "mastodon:#astronomy", 4, jev=0.9, local=0.2)
    quote = {"author": "@kim", "name": "Kim", "text": "Look up tonight", "url": "https://m.example/1"}
    add(db, "q1", "b1b1", "mastodon:#astronomy", 10, jev=0.92, local=0.3, quote=json.dumps(quote),
        summary="A comet!\n\nQuoting @kim: Look up tonight", poster=json.dumps({"name": "Sam", "avatar": ""}),
        via=json.dumps([{"url": "https://bsky.app/x", "name": "A fan", "kind": "bluesky", "author": "@fan"}]))
    add(db, "m1", "c2c2", SKY, 20, jev=0.1, local=0.95)
    add(db, "t1", "d3d3", TRIAL, 30, jev=0.2, local=0.1)
    add(db, "g1", "e4e4", "rss:Garden Blog", 40, jev=0.1, local=0.1)
    add(db, "old", "f5f5", SKY, FEED_DAYS * 24 + 5, jev=0.99, local=0.9)
    db.commit()
    profile = people.profile(db, "alex")
    publish(db, profile)
    return profile


def old_articles(user: str) -> list[dict]:
    """How reader.articles read the timeline before #46: from feed.json."""
    feed = json.loads((feed_folder(user) / "feed.json").read_text())
    out = []
    for it in feed.get("items", []):
        c = it.get("_curator") or {}
        if not c.get("sort_id"):
            continue
        out.append({
            "kind": "article", "id": c["sort_id"], "item": it["id"], "url": it["url"], "title": it["title"],
            "summary": reader.without_quote(it.get("content_text") or "", c.get("quote")), "quote": c.get("quote"), "poster": c.get("poster"),
            "image": it.get("image"), "images": c.get("images"), "published": it.get("date_published"),
            "author": (it.get("authors") or [{}])[0].get("name") or "", "source": c.get("source", ""),
            "lane": c.get("lane", "main"), "reason": c.get("reason") or "", "also": c.get("also") or [],
            "trial": c.get("trial"), "via": c.get("via") or [],
        })
    out.sort(key=lambda a: int(a["id"]))
    return out


def test_the_timeline_is_what_the_feed_file_said(fed, db):
    arts = reader.articles(db, "alex")
    assert arts == old_articles("alex")
    by_item = {a["item"]: a for a in arts}
    assert set(by_item) == {"a1", "q1", "m1", "t1", "g1"}  # one per story, and not the old one
    assert [by_item[i]["lane"] for i in ("a1", "q1", "m1", "t1", "g1")] == ["main", "main", "maybe", "maybe", "main"]
    assert by_item["t1"]["trial"] == "Trial Blog" and by_item["a1"]["trial"] is None
    assert by_item["g1"]["reason"].startswith("always shown") and "Jev 0.95" in by_item["a1"]["reason"]
    assert by_item["a1"]["also"] == [{"url": "https://n.example/a2", "title": "Title a2", "source": "mastodon:#astronomy"}]
    assert by_item["a1"]["image"] == "https://n.example/a1.jpg" and by_item["m1"]["image"] is None
    assert by_item["q1"]["summary"] == "A comet!" and by_item["q1"]["quote"]["author"] == "@kim"
    assert by_item["q1"]["via"][0]["kind"] == "bluesky" and by_item["q1"]["poster"]["name"] == "Sam"
    assert [a["item"] for a in arts] == ["g1", "t1", "m1", "q1", "a1"]  # by timeline id: when each was first seen


def test_the_timeline_needs_no_feed_file(fed, db):
    before = reader.articles(db, "alex")
    (feed_folder("alex") / "feed.json").unlink()
    reader._articles_cache.clear()
    assert reader.articles(db, "alex") == before and len(before) == 5


def test_a_story_that_leaves_the_feed_leaves_the_timeline(fed, db, monkeypatch):
    assert "m1" in {a["item"] for a in reader.articles(db, "alex")}
    db.execute("UPDATE judgments SET local_score = 0.1 WHERE item_id = 'm1'")
    db.commit()
    publish(db, fed)  # nobody picks it now
    assert "m1" not in {a["item"] for a in reader.articles(db, "alex")}
    assert "m1" not in {it["id"] for it in json.loads((feed_folder("alex") / "feed.json").read_text())["items"]}
    assert db.execute("SELECT lane FROM feed_history WHERE story_id = 'c2c2'").fetchone()[0] == "maybe"  # still searchable


def test_an_old_story_is_out_of_the_timeline_but_found_by_search(fed, db, monkeypatch):
    monkeypatch.setattr(search, "query_vector", lambda text: None)
    monkeypatch.setattr(search, "vectors", lambda conn, ids: {})
    # It was in the feed (published when it was new), and is still marked so.
    db.execute("""INSERT INTO feed_history (user, story_id, item_id, lane, added, feed_lane, sort_id, reason)
                  VALUES ('alex', 'f5f5', 'old', 'main', ?, 'main', '1', 'Jev 0.99')""", (ago(FEED_DAYS * 24 + 5),))
    db.commit()
    reader._articles_cache.clear()
    assert "old" not in {a["item"] for a in reader.articles(db, "alex")}
    found = search.search(db, "alex", alex_keys(db), "Title old")
    assert [r["item"] for r in found] == ["old"] and found[0]["picked"]


def alex_keys(db):
    return people.profile(db, "alex").sources["keys"]


def test_the_migration_brings_the_feed_file_into_feed_history(fed, db):
    """A database from before #46: feed_history had no per-reader values, and
    the timeline was the file. After migrating it reads the same."""
    from curator import store

    want = old_articles("alex")
    db.execute("ALTER TABLE feed_history RENAME TO fh")
    db.execute("CREATE TABLE feed_history (user TEXT, story_id TEXT, item_id TEXT, lane TEXT, added TEXT, PRIMARY KEY (user, story_id))")
    db.execute("INSERT INTO feed_history SELECT user, story_id, item_id, lane, added FROM fh")
    db.execute("DROP TABLE fh")
    db.execute("DELETE FROM kv WHERE key LIKE 'feed_edition:%'")
    db.commit()
    store.migrate(db)
    reader._articles_cache.clear()
    assert reader.articles(db, "alex") == want
