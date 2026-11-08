"""Following wrappers down to the source (follow.py): a post sharing an
article that only advertises someone's Mastodon post becomes that post, with
the wrappers kept as `via`. No network: pages and APIs are served here, and
Jev and Haiku are stand-ins."""

import json
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest

from curator import articles, follow, store
from curator.canon import item_id
from curator.collect import from_bsky, item
from curator.stories import ingest

HACKADAY = "https://hackaday.com/2026/11/07/a-pen-plotter-from-a-dead-printer/"
TOOT = "https://chaos.social/@maker/113456789012345678"
SKEET = "https://bsky.app/profile/fan.bsky.social/post/3lqx7abc"
PARA = "A paragraph long enough to count as the page's own text and not as a caption, byline or share line. "
NOW = datetime.now(timezone.utc).isoformat()


def page(*body: str, head: str = "", article: bool = True) -> str:
    inner = "".join(body)
    return (f"<html><head><title>A page</title>{head}</head><body><nav><a href='https://twitter.com/share'>Share</a></nav>"
            + (f"<article>{inner}</article>" if article else inner) + "<footer><p><a href='/tag/plotters'>plotters</a></p></footer></body></html>")


# Hackaday's advert for a toot: a line of its own, the toot embedded, share and tag links.
HACKADAY_PAGE = page(
    "<p>Our friend [maker] has built a pen plotter out of a dead inkjet printer, and it works rather well.</p>",
    f"<figure class='wp-block-embed is-provider-mastodon'><iframe src='{TOOT}/embed' class='mastodon-embed'></iframe></figure>",
    "<p><a href='https://www.facebook.com/sharer.php?u=x'>Share on Facebook</a> <a href='/tag/plotter/'>plotter</a></p>",
    head="<meta property='og:site_name' content='Hackaday'><meta property='og:title' content='A Pen Plotter From A Dead Printer'>")

TOOT_TEXT = ("I finally finished the pen plotter I built from a dead inkjet: the carriage rail, the paper feed "
             "stepper and a servo for the pen lift. Firmware is GRBL with a tweaked config; the build log and "
             "the STL files for the pen holder are in the repo. Biggest lesson: inkjet steppers are weak, so "
             "keep the acceleration low.")


def status(text=TOOT_TEXT, **extra):
    return {"id": "113456789012345678", "created_at": "2026-11-07T08:00:00Z", "content": f"<p>{text}</p>",
            "language": "en", "visibility": "public", "url": TOOT, "uri": TOOT, "card": None, "reblog": None,
            "account": {"acct": "maker", "display_name": "Maker", "avatar_static": "https://chaos.social/a.png"},
            "media_attachments": [{"type": "image", "preview_url": "https://chaos.social/plot.png"}], **extra}


def skeet():
    """Someone on Bluesky sharing the Hackaday article (a link card)."""
    return {"uri": "at://did:plc:fan/app.bsky.feed.post/3lqx7abc", "labels": [],
            "author": {"handle": "fan.bsky.social", "displayName": "A Fan", "avatar": "https://cdn.bsky.app/fan.jpg"},
            "record": {"text": "This is great", "createdAt": "2026-11-08T01:00:00Z", "langs": ["en"]},
            "embed": {"external": {"uri": HACKADAY, "title": "A Pen Plotter From A Dead Printer", "description": "Our friend…"}}}


class Web:
    """Serves pages and API answers by URL, counting requests."""

    def __init__(self, routes: dict):
        self.routes, self.asked = routes, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.asked.append(url)
        found = self.routes.get(url)
        if isinstance(found, Exception):
            raise found
        if found is None:
            return httpx.Response(404)
        if isinstance(found, httpx.Response):
            return found
        if isinstance(found, str):
            return httpx.Response(200, text=found, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(200, json=found)


@pytest.fixture
def web(monkeypatch):
    w = Web({HACKADAY: HACKADAY_PAGE, f"https://chaos.social/api/v1/statuses/113456789012345678": status()})
    monkeypatch.setattr(articles, "client", lambda: httpx.Client(transport=httpx.MockTransport(w)))
    return w


@pytest.fixture
def models(monkeypatch):
    """Jev and Haiku stand-ins: Jev answers `noul` (None: an error); every call is recorded."""
    calls = {"jev": [], "haiku": [], "noul": None, "adds": False}

    def jev(pairs, timeout):
        calls["jev"] += pairs
        return [(calls["noul"], 7)] * len(pairs)

    def haiku(w, t, timeout):
        calls["haiku"].append((w, t))
        return calls["adds"], 11

    monkeypatch.setattr(follow, "jev", jev)
    monkeypatch.setattr(follow, "haiku", haiku)
    return calls


@pytest.fixture
def similarity(monkeypatch):
    """bge-small stand-in: every (wrapper, target) pair is `sim` apart."""
    sim = {"value": 0.5}

    def embed(texts):
        a, b = np.eye(384, dtype=np.float32)[:2]
        pair = [a, sim["value"] * a + np.sqrt(1 - sim["value"] ** 2) * b]
        return np.array([pair[i % 2] for i in range(len(texts))], dtype=np.float32)

    monkeypatch.setattr("curator.stories.embed", embed)
    return sim


def article(url, title="An article", summary="", source="rss:Feed"):
    return item(url, title, summary, source, "", datetime.now(timezone.utc))


# ── Bruce's chain ────────────────────────────────────────────────────────────

def test_a_bluesky_post_of_a_hackaday_advert_becomes_the_mastodon_post(db, web, models, similarity):
    it = from_bsky(skeet(), "bluesky:#plotters")
    assert it["url"] == HACKADAY  # the link card, as today
    new = ingest(db, [it])
    assert new == [item_id(TOOT)]
    row = db.execute("SELECT * FROM items").fetchone()
    assert row["url"] == TOOT and row["author"] == "@maker@chaos.social" and "GRBL" in row["summary"]
    assert json.loads(row["poster"])["name"] == "Maker" and row["image"] == "https://chaos.social/plot.png"
    assert json.loads(row["via"]) == [
        {"url": SKEET, "name": "A Fan", "kind": "bluesky", "author": "@fan.bsky.social"},
        {"url": HACKADAY, "name": "Hackaday", "kind": "article", "author": ""}]
    # Seen in the Bluesky tag, at the address it was shared under.
    assert [tuple(r) for r in db.execute("SELECT source, url FROM sightings")] == [("bluesky:#plotters", HACKADAY)]
    # A few words and an embedded post: a pointer, with no model asked.
    assert models["jev"] == models["haiku"] == []
    assert [tuple(r) for r in db.execute("SELECT next_url, verdict, method FROM hops")] == [(TOOT, "pointer", "heuristic")]
    counts = follow.counts(db)
    assert counts["followed"] == 1 and counts["hops"] == 1 and counts["heuristic"] == 1

    # Hackaday's own feed brings the same article: a sighting of the same
    # item, decided from the cache with nothing fetched and no model asked.
    web.asked.clear()
    again = article(HACKADAY, "A Pen Plotter From A Dead Printer", "Our friend [maker] has built…", "rss:Hackaday")
    assert ingest(db, [again]) == []
    assert web.asked == [] and models["jev"] == models["haiku"] == []
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert ("rss:Hackaday", HACKADAY) in [tuple(r) for r in db.execute("SELECT source, url FROM sightings WHERE item_id = ?", (item_id(TOOT),))]
    assert len(json.loads(db.execute("SELECT via FROM items").fetchone()[0])) == 2  # Hackaday was already there
    assert follow.counts(db)["cached"] == 1


def test_another_way_to_a_stored_original_adds_its_wrappers(db, web, models, similarity):
    ingest(db, [article(HACKADAY, source="rss:Hackaday")])
    other = "https://blog.example/plotters"
    web.routes[other] = page(f"<p>Look at <a href='{TOOT}'>this toot</a>.</p>", head="<meta property='og:site_name' content='A Blog'>")
    web.asked.clear()
    assert ingest(db, [article(other, source="rss:A Blog")]) == []
    assert not any("api/v1/statuses" in u for u in web.asked)  # the stored item is used, not fetched again
    via = json.loads(db.execute("SELECT via FROM items WHERE id = ?", (item_id(TOOT),)).fetchone()[0])
    assert [v["name"] for v in via] == ["Hackaday", "A Blog"]


def test_nothing_stripped_leaves_the_item_as_today(db, web, models, similarity):
    plain = "https://news.example/story"
    web.routes[plain] = page("<p>" + PARA * 3 + "</p>")  # no link anywhere
    it = from_bsky({**skeet(), "embed": {"external": {"uri": plain, "title": "A story", "description": "What happened"}}}, "bluesky:#x")
    ingest(db, [it])
    row = db.execute("SELECT * FROM items").fetchone()
    assert row["url"] == plain and row["via"] is None and row["summary"] == "What happened\n\n@fan.bsky.social: This is great"
    assert follow.counts(db)["followed"] == 0


# ── Deciding ─────────────────────────────────────────────────────────────────

def long_page(link, paragraphs=20):
    """An article with plenty to say of its own, linking what it discusses."""
    return page(*[f"<p>{PARA}Point {i} of the analysis, with figures and quotes of its own.</p>" for i in range(paragraphs)],
                f"<p>The post it discusses is <a href='{link}'>here</a>, and worth reading too.</p>")


def test_an_article_with_its_own_analysis_stays(db, web, models, similarity):
    url = "https://analysis.example/why"
    web.routes[url] = long_page(TOOT)
    similarity["value"] = 0.4
    it = article(url)
    follow.follow(db, [it])
    assert it["url"] == url and "via" not in it
    assert models["jev"] == [] and [tuple(r) for r in db.execute("SELECT verdict, method FROM hops")] == [("value", "heuristic")]


def grey_case(web, similarity):
    url = "https://grey.example/post"
    web.routes[url] = long_page(TOOT, paragraphs=3)  # some words of its own; Jev decides
    similarity["value"] = 0.7
    return article(url)


def test_a_confident_jev_needs_no_haiku(db, web, models, similarity):
    for noul, moved in ((0.9, False), (0.1, True)):
        db.execute("DELETE FROM hops")
        models["noul"] = noul
        it = grey_case(web, similarity)
        follow.follow(db, [it])
        assert (it["url"] == TOOT) is moved
    assert len(models["jev"]) == 2 and models["haiku"] == []
    assert db.execute("SELECT method FROM hops").fetchone()[0] == "jev"
    assert follow.counts(db)["jev_tokens"] == 14 and store.get(db, f"jev_tokens:{datetime.now().date().isoformat()}") == 14


@pytest.mark.parametrize("noul", [0.5, None])  # unsure, or an error
def test_haiku_decides_where_jev_cant(db, web, models, similarity, noul):
    models["noul"] = noul
    it = grey_case(web, similarity)
    follow.follow(db, [it])
    assert len(models["haiku"]) == 1 and it["url"] == TOOT
    assert db.execute("SELECT method FROM hops").fetchone()[0] == "haiku"
    c = follow.counts(db)
    assert c["haiku"] == 1 and c["haiku_tokens"] == 11


def test_past_haikus_daily_cap_the_wrapper_stays(db, web, models, similarity, monkeypatch):
    monkeypatch.setattr(follow, "HAIKU_DAILY", 2)
    store.put(db, follow.day_key(), {"haiku": 2})
    models["noul"] = 0.5
    it = grey_case(web, similarity)
    follow.follow(db, [it])
    assert models["haiku"] == [] and it["url"] == "https://grey.example/post"
    assert db.execute("SELECT COUNT(*) FROM hops").fetchone()[0] == 0  # undecided: asked again another day
    assert follow.counts(db)["haiku_capped"] == 1


def test_a_syndicated_copy_points_at_its_canonical(db, web, models, similarity):
    copy, original = "https://aggregator.example/x", "https://origin.example/x"
    web.routes[copy] = page("<p>" + PARA * 4 + "</p>", head=f"<link rel='canonical' href='{original}'>")
    web.routes[original] = page("<p>" + PARA * 4 + "</p>", head="<meta property='og:site_name' content='Origin'>")
    it = article(copy)
    follow.follow(db, [it])
    assert it["url"] == original and it["via"][0]["url"] == copy and models["jev"] == []


# ── Bounds ───────────────────────────────────────────────────────────────────

def pointer_page(link):
    return page(f"<p>Via <a href='{link}'>elsewhere</a>.</p>")


def test_at_most_three_hops_down(db, web, models, similarity):
    chain = [f"https://site{i}.example/p" for i in range(6)]
    for here, there in zip(chain, chain[1:]):
        web.routes[here] = pointer_page(there)
    web.routes[chain[-1]] = page("<p>" + PARA + "</p>")
    it = article(chain[0])
    follow.follow(db, [it])
    assert it["url"] == chain[3] and [v["url"] for v in it["via"]] == chain[:3]


def test_a_loop_keeps_the_last_hop(db, web, models, similarity):
    a, b = "https://a.example/p", "https://b.example/p"
    web.routes[a], web.routes[b] = pointer_page(b), pointer_page(a)
    it = article(a)
    follow.follow(db, [it])
    assert it["url"] == b and [v["url"] for v in it["via"]] == [a]


@pytest.mark.parametrize("failure", [httpx.Response(500), httpx.ReadTimeout("slow"), httpx.Response(200, text="%PDF", headers={"content-type": "application/pdf"})])
def test_an_unreadable_next_hop_keeps_the_last_good_one(db, web, models, similarity, failure):
    a, b, c = "https://a.example/p", "https://b.example/p", "https://c.example/p"
    web.routes.update({a: pointer_page(b), b: pointer_page(c), c: failure})
    it = article(a)
    follow.follow(db, [it])
    assert it["url"] == b and follow.counts(db)["failed"] == 1


def test_out_of_time_keeps_what_was_reached(db, web, models, similarity, monkeypatch):
    monkeypatch.setattr(follow, "BUDGET", 0)
    a, b = "https://a.example/p", "https://b.example/p"
    web.routes.update({a: pointer_page(b), b: page("<p>" + PARA + "</p>")})
    it = article(a)
    follow.follow(db, [it])
    assert it["url"] == a and follow.counts(db)["timeouts"] == 1


def test_sites_behind_a_login_and_paywalls_are_not_followed(db, web, models, similarity):
    tweet, reel = "https://x.com/someone/status/1", "https://www.instagram.com/p/abc/"
    web.routes["https://a.example/p"] = pointer_page(tweet)
    web.routes["https://b.example/p"] = page("<blockquote class='twitter-tweet'><p>A tweet</p><a href='https://twitter.com/x/status/2'>x</a></blockquote><p>Look.</p>")
    web.routes["https://c.example/p"] = pointer_page("https://paper.example/p")
    web.routes["https://paper.example/p"] = page("<p>" + PARA + "</p>", head='<script type="application/ld+json">{"isAccessibleForFree": false}</script>')
    items = [article("https://a.example/p"), article("https://b.example/p"), article("https://c.example/p"), article(reel, source="instagram:@x")]
    follow.follow(db, items)
    assert [it["url"] for it in items] == ["https://a.example/p", "https://b.example/p", "https://c.example/p", reel]
    assert not any(u.startswith(("https://x.com", "https://twitter.com", "https://www.instagram.com")) for u in web.asked)


def quote_post(comment):
    from curator.collect import from_status

    quoted = {"id": "9", "created_at": "2026-11-07T08:00:00Z", "visibility": "public", "language": "en", "url": TOOT,
              "account": {"acct": "maker@chaos.social", "display_name": "Maker"}, "content": f"<p>{TOOT_TEXT}</p>"}
    post = {"id": "1", "created_at": "2026-11-08T00:00:00Z", "visibility": "public", "language": "en",
            "url": "https://m.example/@pal/1", "account": {"acct": "pal@m.example", "display_name": "Pal"},
            "content": f'<p>{comment}</p><p class="quote-inline">RE: {TOOT}</p>', "quote": {"state": "accepted", "quoted_status": quoted}}
    return from_status(post, "mastodon:#plotters")


def test_a_quote_post_that_only_points_becomes_what_it_quotes(db, web, models, similarity):
    it = quote_post("Wow, look at this.")
    follow.follow(db, [it])
    assert it["url"] == TOOT and it["via"] == [{"url": "https://m.example/@pal/1", "name": "Pal", "kind": "mastodon", "author": "@pal@m.example"}]
    assert it["quote"] is None  # the original quotes nothing
    assert models["jev"] == []  # near-empty: a pointer with no model asked


def test_a_quote_post_with_a_comment_of_its_own_is_put_to_jev(db, web, models, similarity):
    # Short, as posts are, but it says something: Jev decides, and here it's value.
    models["noul"] = 0.9
    it = quote_post("I disagree with this strongly: the real problem is the rail flex, not the steppers. I measured half a "
                    "millimetre of deflection at the middle of the carriage, which is what blurs the lines, and a second "
                    "rod fixed it for me in an afternoon.")
    follow.follow(db, [it])
    assert it["url"] == "https://m.example/@pal/1" and "via" not in it and "rail flex" in it["summary"]
    assert len(models["jev"]) == 1 and models["haiku"] == []
    # Only its own words are compared: with what it quotes, a quote post would read like what it quotes.
    assert "Quoting" not in models["jev"][0][0]["own_text"]


def test_a_post_reached_on_the_way_keeps_its_comment(db, web, models, similarity):
    # The toot in Bruce's chain, with a link card: its comment goes to Jev, not
    # stripped for being under 60 words.
    web.routes["https://chaos.social/api/v1/statuses/113456789012345678"] = status(
        card={"type": "link", "url": "https://github.com/maker/plotter", "title": "maker/plotter"})
    web.routes["https://github.com/maker/plotter"] = page("<p>" + PARA * 3 + "</p>")
    models["noul"] = 0.9
    it = from_bsky(skeet(), "bluesky:#plotters")
    follow.follow(db, [it])
    assert it["url"] == TOOT and len(models["jev"]) == 1


def test_a_readers_tap_on_a_post_isnt_kept_waiting(db, web, models, similarity):
    # home.learn: a reader is waiting, so nothing is followed there.
    it = from_bsky(skeet(), "mastodon:home:alex")
    assert ingest(db, [it], follow_wrappers=False) == [item_id(HACKADAY)]
    assert web.asked == []


# ── Garbage in ───────────────────────────────────────────────────────────────

def test_garbage_pages_and_answers_change_nothing(db, web, models, similarity):
    junk = {
        "https://huge.example/p": "<p>" + "<a href='https://[::1'>x</a>" * 5000 + "<div>" * 100_000,
        "https://bad-og.example/p": page("<p>Hi</p>", head="<meta property='og:url' content='javascript:alert(1)'><link rel='canonical' href='not a url'>"),
        "https://broken.example/p": "<p><a href='https://chaos.social/@x/notdigits'>x</a><blockquote class='mastodon-embed' data-embed-url='https://[oops/embed'>",
        "https://masto-garbage.example/p": pointer_page("https://chaos.social/@x/1"),
        "https://bsky-garbage.example/p": pointer_page("https://bsky.app/profile/someone.example/post/3abc"),
        "https://chaos.social/api/v1/statuses/1": {"id": "1", "content": "no account, no date"},
        "https://api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle=someone.example": {"did": ["not", "a", "did"]},
    }
    web.routes.update(junk)
    items = [article(u) for u in junk if "api" not in u] + [article("not a url at all"), article("https://empty.example/")]
    before = [dict(it) for it in items]
    follow.follow(db, items)
    assert items == before
