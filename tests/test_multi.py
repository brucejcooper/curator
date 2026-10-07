"""Several readers: shared collection, per-reader judging, and moving the
single-reader profile (and its git history) in."""

import subprocess

import yaml

from conftest import docs
from curator import judge, people


def test_a_feed_two_readers_follow_is_fetched_once(db):
    a, b = docs()[1], docs()[1].replace("{name: Sky News Feed,", "{name: Sky,")
    for user, src in (("alex", a), ("sam", b)):
        people.ensure_user(db, user)
        people.add_version(db, user, "import", "x", docs()[0], src, docs()[2])
    from curator.collect import shared_sources

    shared = shared_sources([people.profile(db, "alex"), people.profile(db, "sam")])
    sky = [f for f in shared["rss"] if f["url"] == "https://sky.example/rss"]
    assert len(sky) == 1 and sky[0]["names"] == {"Sky News Feed", "Sky"}
    assert shared["mastodon_tags"] == {"astronomy"}


def test_judging_is_per_reader_and_only_for_what_they_follow(alex, db, monkeypatch):
    now = "2026-10-02T00:00:00+00:00"
    for iid, source, title, url in [("i1", "mastodon:#astronomy", "A comet", "https://a.example/1"),
                                    ("i2", "rss:Not Alex's", "Something else", "https://b.example/2"),
                                    ("i3", "mastodon:#astronomy", "Your horoscope", "https://a.example/3"),
                                    ("i4", "mastodon:#astronomy", "Cheap pills", "https://spam.example/4")]:
        db.execute("INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched, traits) "
                   "VALUES (?, ?, ?, ?, '', ?, '', ?, ?, ?)", (iid, url, url, title, source, now, now, '{"importance": {"score": 4.0}}'))
        db.execute("INSERT INTO sightings VALUES (?, ?, ?, ?)", (iid, source, url, now))
    db.commit()
    asked = []

    async def fake_ask(rows, questions, state_for):
        asked.extend(r["id"] for r in rows)
        assert "reader" in state_for(rows[0]) and set(questions) == {"relevant", "topic"}
        return [(r["id"], {"answers": {"relevant": {"score": 1.0}}, "usage": {"input_tokens": 10}}) for r in rows]

    monkeypatch.setattr(judge, "ask", fake_ask)
    monkeypatch.setattr(judge, "judge_local", lambda conn, p, ids=None: None)
    judge.judge_for(db, alex, ["i1", "i2", "i3", "i4"])
    assert asked == ["i1"]  # not i2 (not Alex's source); not i3/i4 (Alex's own rules)
    rows = {r["item_id"]: (r["rule"], r["jev_score"]) for r in db.execute("SELECT * FROM judgments WHERE user = 'alex'")}
    assert rows["i3"][0].startswith("title matches") and rows["i4"][0].startswith("blocked domain")
    assert rows["i1"][1] >= 0.9 * 1.0 - 1e-9  # off-interest, but major news (a shared trait) carries it


def test_the_owner_moves_in_with_their_profile_history(db, tmp_path, monkeypatch):
    repo = tmp_path / "old"
    (repo / "profile").mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example")
    git("config", "user.name", "t")
    md, src, own_questions = docs()
    legacy_questions = own_questions + "\nrage_bait:\n  type: noul\n  instructions: Rage?\n"
    for n, text in enumerate([md, md + "\n- birds\n"]):
        (repo / "profile/profile.md").write_text(text)
        (repo / "profile/sources.yaml").write_text(src)
        (repo / "profile/jev-questions.yaml").write_text(legacy_questions)
        git("add", "-A")
        git("commit", "-qm", f"Profile refresh {n}")
    monkeypatch.setattr(people, "LEGACY_PROFILE", repo / "profile")
    monkeypatch.setattr(people, "OWNER", "owner")
    monkeypatch.setenv("CURATOR_FEED_PATH", "old-secret-path")
    people.bootstrap(db)
    assert [(v["version"], v["summary"]) for v in people.versions(db, "owner")] == [(2, "Profile refresh 1"), (1, "Profile refresh 0")]
    p = people.profile(db, "owner")
    assert "birds" in p.profile_md and "rage_bait" not in yaml.safe_load(p.questions_yaml), "item-level questions are shared now"
    assert people.user(db, "owner")["admin"] == 1
    assert people.user_for_feed_path(db, "old-secret-path") == "owner", "the existing feed address keeps working"


def test_connecting_doesnt_wait_for_a_writer(db):
    # The collection cycle may be mid-write; the API must still open and read.
    import time

    from curator import store

    writer = store.connect()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO kv VALUES ('busy', '1')")
    started = time.monotonic()
    reader = store.connect()
    reader.execute("SELECT count(*) FROM items").fetchone()
    assert time.monotonic() - started < 1
    writer.rollback()
