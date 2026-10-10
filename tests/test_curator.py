import numpy as np

from curator.canon import canonical_url, item_id, title_key
from curator.collect import from_status
from curator.judge import jev_score
from curator.stories import find_story, grey_matches


def test_tracking_params_and_www_ignored():
    a = "https://www.abc.net.au/news/story/123?utm_source=x&fbclid=y"
    assert canonical_url(a) == "https://abc.net.au/news/story/123"
    assert item_id(a) == item_id("http://abc.net.au/news/story/123/")


def test_title_key_drops_outlet_suffix():
    assert title_key("Big news happens - ABC News") == title_key("Big news happens")


def test_jev_score_penalises_rage_bait():
    q = {"relevant": {"criteria": ["a", "b", "c", "d", "e"]}}
    calm = {"relevant": {"score": 4.0}, "rage_bait": {"noul": 0.1}}
    angry = {"relevant": {"score": 4.0}, "rage_bait": {"noul": 1.0}}
    assert jev_score(calm, q) == 1.0
    assert abs(jev_score(angry, q) - 0.3) < 1e-9


def unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_same_source_needs_a_closer_match():
    ids, stories, sources = ["a"], ["a"], ["rss:X"]
    matrix = np.stack([unit(1, 0, 0)])
    near = unit(1, 0.4, 0)  # cosine ≈ 0.93 boundary region
    sim = float(matrix[0] @ near)
    assert 0.87 < sim < 0.93
    assert find_story(near, "rss:X", ids, stories, sources, matrix) is None
    assert find_story(near, "rss:Y", ids, stories, sources, matrix) == "a"



def test_grey_zone_asks_about_reposts_and_other_stories_too():
    # One outlet's iOS and macOS beta posts (0.86): too far apart to merge
    # outright, close enough to ask. Asked even once the item has a story,
    # one earlier item per other story, never the story it's already in.
    ids, stories, sources = ["ios", "ios9", "far"], ["s1", "s1", "s2"], ["rss:MR", "rss:9", "rss:MR"]
    matrix = np.stack([unit(1, 0.59, 0), unit(1, 0.6, 0), unit(1, 2, 0)])
    mac = unit(1, 0, 0)
    assert 0.78 < float(matrix[0] @ mac) < 0.93 and float(matrix[2] @ mac) < 0.72
    assert grey_matches(mac, "rss:MR", None, ids, stories, sources, matrix) == ["ios"]
    assert grey_matches(mac, "rss:MR", "s1", ids, stories, sources, matrix) == []

def test_non_english_posts_skipped():
    status = {"created_at": "2026-10-01T00:00:00Z", "content": "<p>" + "Hallo " * 30 + "</p>", "language": "de",
              "visibility": "public", "account": {"acct": "x"}, "url": "https://m/1"}
    assert from_status(status, "mastodon:#x") is None
    assert from_status({**status, "language": "en"}, "mastodon:#x") is not None


def test_language_detector_bundled_and_confident():
    from curator.judge import LID_CONFIDENT, language

    assert language("Home Assistant adds a native llama.cpp integration for local voice")[0] == "en"
    lang, conf = language("So #homeassistant ist grundsätzlich eingerichtet. Backup auf die NAS läuft jetzt jede Nacht.")
    assert lang == "de" and conf >= LID_CONFIDENT


def test_sort_id_is_a_mastodon_style_snowflake():
    from curator.publish import sort_id

    sid = int(sort_id({"first_fetched": "2026-10-01T08:00:00+00:00", "story": "abcd1234"}))
    assert sid >> 16 == 1790841600000  # milliseconds
    assert sid & 0xFFFF == 0xABCD


# ── Replies stand for their conversation's first post ────────────────────────

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx


def kv_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def toot(sid, text, reply_to=None, card=None, age_days=0):
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    return {"id": sid, "created_at": created, "content": f"<p>{text}</p>", "language": "en", "visibility": "public",
            "account": {"acct": "someone@x.social"}, "url": f"https://x.social/@someone/{sid}",
            "in_reply_to_id": reply_to, "card": card, "reblog": None}


LONG = "a long enough post about home automation and ESPHome sensors " * 2


def mastodon_with(timeline, contexts):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/context"):
            sid = req.url.path.split("/")[-2]
            return httpx.Response(200, json={"ancestors": contexts.get(sid, []), "descendants": []})
        return httpx.Response(200, json=timeline)
    return httpx.Client(base_url="https://m.example", transport=httpx.MockTransport(handler))


def test_mastodon_reply_becomes_its_conversation_root():
    from curator.collect import mastodon

    root = toot("1", "Root: " + LONG)
    timeline = [toot("3", "reply two " + LONG, reply_to="2"), toot("2", "reply one " + LONG, reply_to="1")]
    items = mastodon(kv_conn(), mastodon_with(timeline, {"2": [root], "3": [root, toot("2", "x", "1")]}), "/t", "mastodon:#x", {})
    assert [i["url"] for i in items] == ["https://x.social/@someone/1"] * 2  # both replies → the root (deduped at ingest)
    assert items[0]["title"].startswith("Root:")


def test_mastodon_stale_or_unseen_roots_are_dropped():
    from curator.collect import mastodon

    timeline = [toot("5", "reply " + LONG, reply_to="4"), toot("7", "reply " + LONG, reply_to="6")]
    items = mastodon(kv_conn(), mastodon_with(timeline, {"5": [toot("4", "old " + LONG, age_days=30)]}), "/t", "m", {})
    assert items == []


def test_mastodon_reply_sharing_a_link_stays_an_article():
    from curator.collect import mastodon

    card = {"url": "https://news.example/a", "type": "link", "title": "An article", "description": "d"}
    items = mastodon(kv_conn(), mastodon_with([toot("9", "look", reply_to="8", card=card)], {}), "/t", "m", {})
    assert [i["url"] for i in items] == ["https://news.example/a"]


def test_bluesky_reply_becomes_thread_root():
    from curator.collect import bluesky

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    root = {"uri": "at://did:a/app.bsky.feed.post/root", "author": {"handle": "a.bsky.social"}, "labels": [],
            "record": {"text": "Root " + LONG, "createdAt": now, "langs": ["en"]}}
    reply = {"uri": "at://did:b/app.bsky.feed.post/r1", "author": {"handle": "b.bsky.social"}, "labels": [],
             "record": {"text": "reply " + LONG, "createdAt": now, "langs": ["en"],
                        "reply": {"root": {"uri": root["uri"]}, "parent": {"uri": root["uri"]}}}}

    def handler(req: httpx.Request):
        if req.url.path.endswith("searchPosts"):
            return httpx.Response(200, json={"posts": [reply]})
        assert req.url.params.get_list("uris") == [root["uri"]]
        return httpx.Response(200, json={"posts": [root]})

    items = bluesky(kv_conn(), httpx.Client(transport=httpx.MockTransport(handler)), "homelab")
    assert [i["url"] for i in items] == ["https://bsky.app/profile/a.bsky.social/post/root"]


def test_hashtag_soup():
    from curator.judge import hashtag_soup

    assert hashtag_soup("🤣🤣 #wii #wiihacks #wiiscene #wiiunderground #wiiretro #wiiverberate #bluesky")
    assert hashtag_soup("New video! #homelab #selfhosted #proxmox #docker #linux #server #rack")
    assert not hashtag_soup("I finally automated FreeBSD jail updates in my #homelab with a small ansible role "
                            "that snapshots first, upgrades, runs smoke tests and rolls back on failure #freebsd")
    assert not hashtag_soup("Release notes: C# 13 & F# 9 are out with better pattern matching")


def test_preview_image_from_meta_tags():
    from curator.images import POST_URL, preview_image

    html = '<head><meta name="twitter:image" content="https://x/t.jpg"><meta content="/og.jpg" property="og:image"></head>'
    assert preview_image(html, "https://news.example/a/b") == "https://news.example/og.jpg"
    assert preview_image("<head><title>none</title></head>", "https://x/") is None
    assert POST_URL.match("https://bsky.app/profile/a.bsky.social/post/xyz")
    assert POST_URL.match("https://mastodon.social/@someone/1234567")
    assert not POST_URL.match("https://www.abc.net.au/news/2026-10-01/story/1234567")


def test_rss_image_prefers_largest_media():
    from curator.collect import rss_image

    e = {"media_content": [{"url": "https://i/small.jpg", "width": "100", "medium": "image"},
                           {"url": "https://i/big.jpg", "width": "1200", "medium": "image"}]}
    assert rss_image(e) == "https://i/big.jpg"
    assert rss_image({"summary": '<p><img class="x" src="https://i/in-html.jpg"></p>'}) == "https://i/in-html.jpg"
    assert rss_image({"summary": "no pictures"}) is None


def test_plain_keeps_paragraphs_and_joins_split_links():
    from curator.canon import plain, short_title

    html = ('<p>First para with a <a href="x"><span class="invisible">https://</span><span>example.com/a</span></a> link.</p>'
            "<p>Second<br>line two</p>")
    assert plain(html) == "First para with a https://example.com/a link.\n\nSecond\nline two"
    assert short_title("short one\nmore") == "short one"
    long = "word " * 40
    t = short_title(long)
    assert t.endswith("…") and len(t) <= 120 and not t[:-1].endswith(" ")


def test_important_news_scores_even_off_the_readers_interests():
    from curator.judge import jev_score

    q = {"relevant": {"criteria": list("abcde")}, "importance": {"criteria": list("abcde")}}
    off_topic_major = {"relevant": {"score": 2.0}, "importance": {"score": 4.0}}
    on_topic_trivial = {"relevant": {"score": 4.0}, "importance": {"score": 0.0}}
    off_topic_trivial = {"relevant": {"score": 1.0}, "importance": {"score": 0.5}}
    assert jev_score(off_topic_major, q) >= 0.7  # major news makes the bar on its own
    # Traits (asked once for everyone) count the same as the reader's own answers.
    assert jev_score({"relevant": {"score": 2.0}}, {"relevant": q["relevant"]}, {"importance": {"score": 4.0}}) >= 0.7
    assert jev_score(on_topic_trivial, q) == 1.0  # a hobby story needn't be news
    assert jev_score(off_topic_trivial, q) < 0.5


# ── Per-reader selection ─────────────────────────────────────────────────────

def add_story(db, sid, source, jev, local=0.1, user="alex"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO stories VALUES (?, ?, ?, ?)", (sid, sid, now, now))
    db.execute("""INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched, story_id)
                  VALUES (?, ?, ?, ?, '', ?, '', ?, ?, ?)""", (sid, f"https://x/{sid}", f"https://x/{sid}", f"t {sid}", source, now, now, sid))
    db.execute("INSERT INTO sightings VALUES (?, ?, ?, ?)", (sid, source, f"https://x/{sid}", now))
    if user:
        db.execute("INSERT INTO judgments (user, item_id, jev_score, local_score) VALUES (?, ?, ?, ?)", (user, sid, jev, local))
    db.commit()


def test_feed_only_carries_what_clears_the_bar(alex, db):
    """A slow day gives a short feed, not filler; the maybe lane stays small."""
    from curator.publish import select

    for n, (jev, local) in enumerate([(0.95, 0.7), (0.8, 0.3), (0.72, 0.65), (0.5, 0.9), (0.3, 0.2), (0.1, 0.1)]):
        add_story(db, f"s{n}", "mastodon:#astronomy", jev, local)
    lanes = {s["story"]: s["lane"] for s in select(db, alex)}
    assert {k for k, v in lanes.items() if v == "main"} == {"s0", "s1", "s2"}  # only ≥ 0.7, though 150 were allowed
    assert {k for k, v in lanes.items() if v == "maybe"} == {"s3"}  # the local model liked it; Jev didn't
    assert "s4" not in lanes and "s5" not in lanes


def test_source_floor_always_show_and_what_readers_can_see(alex, db):
    from curator.publish import select

    add_story(db, "news", "rss:Sky News Feed", 0.45)  # its own lower bar (0.3)
    add_story(db, "other", "mastodon:#astronomy", 0.45)  # the same score elsewhere misses
    add_story(db, "blog", "rss:Garden Blog", 0.1)  # always shown
    add_story(db, "not-mine", "rss:Somebody Else's Feed", 0.99)  # not one of Alex's sources
    add_story(db, "unjudged", "mastodon:#astronomy", None, None, user=None)
    lanes = {s["story"]: s["lane"] for s in select(db, alex)}
    assert lanes == {"news": "main", "blog": "main"}


def test_signals_are_per_reader_with_explicit_down_vote_winning(db):
    from curator import store

    db.executemany("INSERT INTO events (item_id, kind, time, user) VALUES (?, 'open', 't', 'alex')",
                   [("opened",), ("opened-then-down",), ("opened-then-up",)])
    db.executemany("INSERT INTO user_bookmarks VALUES ('alex', ?, 't')", [("kept",)])
    db.executemany("INSERT INTO user_votes VALUES ('alex', ?, ?, 't')", [("opened-then-down", -1), ("opened-then-up", 1), ("kept", 1)])
    db.execute("INSERT INTO user_votes VALUES ('sam', 'opened', -1, 't')")  # someone else's vote isn't Alex's
    got = store.labels(db, "alex")
    assert got["opened"] == (1, 1.3)  # opening outweighs a 👍
    assert got["opened-then-down"] == (-1, 1.0)  # but an explicit 👎 wins
    assert got["opened-then-up"] == (1, 1.3)
    assert got["kept"] == (1, 1.5)
    assert store.labels(db)["opened"][0] == -1  # everyone's together (for choosing leads)


def test_single_reader_database_moves_to_the_owner(tmp_path, monkeypatch):
    from curator import store

    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "t.sqlite")
    monkeypatch.setattr(store, "OWNER", "owner")
    old = sqlite3.connect(tmp_path / "t.sqlite")
    old.executescript("""CREATE TABLE items (id TEXT PRIMARY KEY, url TEXT, canonical TEXT, title TEXT, summary TEXT, source TEXT,
                           author TEXT, published TEXT, fetched TEXT, story_id TEXT, embedding BLOB, always_show INTEGER, rule TEXT,
                           jev TEXT, jev_score REAL, local TEXT, local_score REAL, sample INTEGER);
                         INSERT INTO items (id, source, jev, jev_score, local_score) VALUES ('i1', 'mastodon:home', '{"answers": {"news": {"noul": 0.9}}}', 0.8, 0.6);
                         CREATE TABLE sightings (item_id TEXT, source TEXT, url TEXT, seen TEXT, PRIMARY KEY (item_id, source));
                         INSERT INTO sightings VALUES ('i1', 'mastodon:home', 'u', 't');
                         CREATE TABLE votes (item_id TEXT PRIMARY KEY, vote INTEGER, time TEXT);
                         CREATE TABLE bookmarks (item_id TEXT PRIMARY KEY, time TEXT);
                         CREATE TABLE events (item_id TEXT, kind TEXT, time TEXT);
                         CREATE TABLE home_posts (status_id TEXT PRIMARY KEY, item_id TEXT, seen TEXT);
                         INSERT INTO votes VALUES ('a', 1, 't'); INSERT INTO bookmarks VALUES ('b', 't');
                         INSERT INTO events VALUES ('c', 'open', 't'); INSERT INTO home_posts VALUES ('99', 'i1', 't');""")
    old.commit()
    old.close()
    conn = store.connect()
    rows = lambda sql: [tuple(r) for r in conn.execute(sql)]
    assert rows("SELECT user, item_id, vote FROM user_votes") == [("owner", "a", 1)]
    assert rows("SELECT user, item_id FROM user_bookmarks") == [("owner", "b")]
    assert rows("SELECT user FROM events") == [("owner",)]
    assert rows("SELECT user, status_id FROM home_posts") == [("owner", "99")]
    assert rows("SELECT source FROM items") == [("mastodon:home:owner",)]
    assert rows("SELECT source FROM sightings") == [("mastodon:home:owner",)]
    assert rows("SELECT user, item_id, jev_score, local_score FROM judgments") == [("owner", "i1", 0.8, 0.6)]
    assert json.loads(conn.execute("SELECT traits FROM items").fetchone()[0]) == {"news": {"noul": 0.9}}


def test_items_without_text_get_the_articles_first_paragraphs():
    from curator.articles import extract, needs_text

    para = "A paragraph long enough to count as the article's own text, not a caption. "
    html = (f"<html><head><meta property='og:description' content='The description'></head><body>"
            f"<nav><p>{para}Menu</p></nav><p>{para}Outside the article.</p>"
            f"<article><p>By someone</p><p>{para}One.</p><p>{para}Here's how:</p><p>{para}Two.</p>"
            f"<figure><p>{para}Caption.</p></figure><p>{para}Three.</p><p>{para}Four.</p></article></body></html>")
    assert extract(html).split("\n\n") == [para + "One.", para + "Two.", para + "Three."]
    assert extract("<html><head><meta name='description' content='Only this'></head><body><p>Short.</p></body></html>") == "Only this"
    hn = {"source": "rss:Hacker News Best", "url": "https://example.org/a", "title": "A story",
          "summary": "Article URL: https://example.org/a\n\nComments URL: https://news.ycombinator.com/item?id=1\n\nPoints: 9\n\n# Comments: 2"}
    assert needs_text(hn)
    assert not needs_text({**hn, "summary": "A real standfirst that says what the story is about, at some length."})
    assert not needs_text({**hn, "source": "mastodon:#homelab"})


def test_bare_summaries_and_teasers_are_told_apart():
    from curator.articles import needs_text, teaser, with_teaser

    it = lambda summary, title="Polars 2.0 ships": {"source": "rss:X", "url": "https://example.org/a", "title": title, "summary": summary}
    assert needs_text(it("Polars 2.0 ships"))  # repeats the title
    assert needs_text(it("submitted by /u/someone [link] [comments]"))
    assert not needs_text(it("Two cargo ships have sunk, one ship was afire, and sailors are dead or missing."))
    cut = it("Apple is rolling out the third beta of macOS 27.2. Here's what's coming. more…")
    assert needs_text(cut) and teaser(cut)
    assert not teaser(it("x " * 400 + "[…] The post Y appeared first on CNX Software."))  # long: real text already
    article = "The full first paragraph, about the third beta of macOS 27.2, and what's in it.\n\nThe second."
    # The teaser's full paragraph is in the article: just the article.
    assert with_teaser("The full first paragraph, about the third beta […]", article) == article
    # A standfirst that isn't in the article comes first.
    assert with_teaser("Apple ships another beta. more…", article) == "Apple ships another beta.\n\n" + article


def test_a_quote_post_carries_what_it_quotes():
    quoted = {"content": "<p>@lrosa @timbray This piece is obviously AI generated, and confirmed as such.</p>",
              "account": {"acct": "jn@c.im"}, "media_attachments": []}
    status = {"created_at": "2026-10-08T23:00:00Z", "visibility": "public", "language": "en", "account": {"acct": "tim@cosocial.ca"},
              "url": "https://cosocial.ca/@tim/1",
              "content": '<p class="quote-inline">RE: <a href="https://c.im/@jn/2">c.im/@jn/2</a></p><p>That’s twice today.</p>',
              "quote": {"state": "accepted", "quoted_status": quoted}}
    it = from_status(status, "mastodon:home:x", home=True)
    assert it["title"] == "That’s twice today."
    assert it["summary"] == "That’s twice today.\n\nQuoting @jn@c.im: This piece is obviously AI generated, and confirmed as such."
    assert it["quote"] == {"author": "@jn@c.im", "name": "@jn@c.im", "text": "This piece is obviously AI generated, and confirmed as such.",
                           "url": "", "image": None}
    # The app shows the quote in a box, so its paragraph leaves the text.
    from curator.reader import without_quote
    assert without_quote(it["summary"], it["quote"]) == "That’s twice today."
    assert without_quote("Unrelated text.", it["quote"]) == "Unrelated text."
    # A quote that can't be shown (pending, removed) leaves the post as it was.
    gone = from_status({**status, "quote": {"state": "deleted", "quoted_status": None}}, "mastodon:home:x", home=True)
    assert "RE: c.im/@jn/2" in gone["summary"] and gone["quote"] is None

    from curator.collect import from_bsky

    post = {"uri": "at://did/app.bsky.feed.post/abc", "author": {"handle": "sam.bsky.social"},
            "record": {"text": "Exactly this.", "createdAt": "2026-10-08T23:00:00Z", "langs": ["en"]},
            "embed": {"$type": "app.bsky.embed.record#view",
                      "record": {"author": {"handle": "ana.bsky.social"}, "value": {"text": "Our new paper on soil carbon is out today, after four years of fieldwork."}}}}
    it = from_bsky(post, "bluesky:#x")
    assert it["title"] == "Exactly this." and it["summary"].endswith("Quoting @ana.bsky.social: Our new paper on soil carbon is out today, after four years of fieldwork.")
    assert it["quote"]["author"] == "@ana.bsky.social" and it["quote"]["text"].startswith("Our new paper")
    wrapped = {**post, "embed": {"$type": "app.bsky.embed.recordWithMedia#view", "media": {"images": []}, "record": {"record": post["embed"]["record"]}}}
    assert from_bsky(wrapped, "bluesky:#x")["summary"] == it["summary"]


def test_a_reader_gets_a_version_from_their_own_sources(alex, db):
    # The story's lead is from a source Alex doesn't have (one they removed);
    # another of its items came through one they do. Alex gets that one.
    from curator.publish import select

    add_story(db, "st", "rss:Walled Garden", 0.9)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("""INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched, story_id)
                  VALUES ('mine', 'https://mine/a', 'https://mine/a', 'Mine', 'My version', 'mastodon:#astronomy', '', ?, ?, 'st')""", (now, now))
    db.execute("INSERT INTO sightings VALUES ('mine', 'mastodon:#astronomy', 'https://mine/a', ?)", (now,))
    db.execute("INSERT INTO judgments (user, item_id, jev_score, local_score) VALUES ('alex', 'mine', 0.9, 0.1)")
    db.commit()
    picked = {s["story"]: s for s in select(db, alex)}
    assert picked["st"]["id"] == "mine" and picked["st"]["url"] == "https://mine/a" and picked["st"]["source"] == "mastodon:#astronomy"


def test_a_comics_hover_text_is_kept_and_posts_say_who_posted():
    from curator.collect import parse_feed

    atom = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>xkcd.com</title>
      <entry><title>Juice</title><link href="https://xkcd.com/3308/"/><updated>2099-01-01T00:00:00Z</updated>
      <summary type="html">&lt;img src="https://imgs.xkcd.com/comics/juice.png" title="Definitely not do anything else." alt="x" /&gt;</summary></entry></feed>"""
    from datetime import timedelta
    (it,) = parse_feed(atom, "xkcd", max_age=timedelta(days=36500 * 2))
    assert it["summary"] == "Definitely not do anything else." and it["image"] == "https://imgs.xkcd.com/comics/juice.png"

    status = {"created_at": "2026-10-01T00:00:00Z", "content": "<p>" + "A post with enough words in it to count. " * 3 + "</p>",
              "language": "en", "visibility": "public", "url": "https://m/1",
              "account": {"acct": "ana@m.example", "display_name": "Ana", "avatar_static": "https://m/ana.png"}}
    assert from_status(status, "mastodon:#x")["poster"] == {"name": "Ana", "avatar": "https://m/ana.png"}
