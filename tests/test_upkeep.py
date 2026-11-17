"""Feed address upkeep and JSON Feed: moved feeds (permanent redirects), dead
ones (404, 410, not a feed) on /status, the daily revision following a learned
feed that has moved, and JSON Feed read like RSS and Atom."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from curator import collect, people, profile_refresh, scout, store
from curator.profile_refresh import JevQuestion, LearnedSource, Option, Revision
from curator.server import feed_rows

D = timedelta(days=1)
FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>One</title><link>https://blog.example/1</link></item></channel></rss>"""


def serve(routes: dict) -> httpx.Client:
    """A client answering each URL with (status, headers or a Location)."""
    def handler(request):
        status, extra = routes[str(request.url)]
        if status in (301, 302, 307, 308):
            return httpx.Response(status, headers={"Location": extra})
        return httpx.Response(status, content=FEED if status == 200 else b"", headers=extra or {})
    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch(db, http, url="https://old.example/rss", at=None):
    try:
        collect.rss(db, http, {"url": url, "name": "Blog"}, at or collect.now())
    except (httpx.HTTPError, ValueError):
        pass
    return store.get(db, f"rss:{url}")


# ── Moved ────────────────────────────────────────────────────────────────────

def test_a_permanent_redirect_is_kept_as_a_move(db):
    moved = serve({"https://old.example/rss": (301, "https://new.example/rss"), "https://new.example/rss": (200, None)})
    s = fetch(db, moved)
    assert s["moved_to"] == "https://new.example/rss" and s["moved_since"] and s["last"] == "200-new"
    since = s["moved_since"]
    s = fetch(db, moved, at=collect.now() + D)
    assert s["moved_since"] == since  # the same move, still since the first time
    s = fetch(db, serve({"https://old.example/rss": (200, None)}))
    assert "moved_to" not in s and "moved_since" not in s


def test_a_temporary_redirect_is_not_a_move(db):
    for code in (302, 307):
        s = fetch(db, serve({"https://old.example/rss": (code, "https://new.example/rss"), "https://new.example/rss": (200, None)}))
        assert "moved_to" not in s
    # Permanent, then temporary: not every hop says it has moved.
    s = fetch(db, serve({"https://old.example/rss": (301, "https://mid.example/rss"),
                         "https://mid.example/rss": (302, "https://new.example/rss"), "https://new.example/rss": (200, None)}))
    assert "moved_to" not in s


def test_a_moved_feed_says_where_on_status(alex, db):
    store.put(db, "rss:https://sky.example/rss", {"moved_to": "https://sky.example/new<rss>", "moved_since": collect.iso(collect.now())})
    assert "moved → https://sky.example/new&lt;rss&gt;" in feed_rows(db)


# ── Dead ─────────────────────────────────────────────────────────────────────

def test_a_feed_gone_for_a_week_is_flagged_and_any_answer_clears_it(alex, db):
    url, start = "https://sky.example/rss", collect.now() - 8 * D
    gone = serve({url: (404, None)})
    for day in range(9):
        s = fetch(db, gone, url, start + day * D)
    assert s["failing"] == "404" and s["failing_since"] == collect.iso(start) and s["errors"] == 9
    rows = feed_rows(db)
    assert "dead? 404 since" in rows and rows.index("Sky News Feed") < rows.index("Garden Blog")  # first
    s = fetch(db, serve({url: (200, None)}), url)
    assert "failing_since" not in s and "dead?" not in feed_rows(db)


def test_a_new_failure_is_not_yet_dead(alex, db):
    s = fetch(db, serve({"https://sky.example/rss": (410, None)}), "https://sky.example/rss", collect.now() - 3 * D)
    assert s["failing"] == "410" and "dead?" not in feed_rows(db)


def test_not_a_feed_starts_the_clock_but_a_503_does_not(db):
    s = fetch(db, serve({"https://old.example/rss": (503, None)}))
    assert "failing_since" not in s and s["last"] == "503"
    html = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html><p>Parked domain</p></html>")))
    s = fetch(db, html)
    assert s["failing"] == "not a feed" and s["failing_since"]
    since = s["failing_since"]
    s = fetch(db, serve({"https://old.example/rss": (503, None)}), at=collect.now() + D)
    assert s["failing_since"] == since  # a passing error neither starts nor stops it


# ── The daily revision follows a learned feed ────────────────────────────────

def test_the_revision_moves_a_learned_feed_after_a_week(alex, db, monkeypatch):
    now = collect.now()
    learned = [
        {"kind": "rss", "value": "https://settled.example/rss", "reason": "r", "added": "2026-10-01"},
        {"kind": "rss", "value": "https://recent.example/rss", "reason": "r", "added": "2026-10-01"},
    ]
    people.add_version(db, "alex", "test", "learned", alex.profile_md,
                       people.edit_learned(alex.sources_yaml, lambda _: learned), alex.questions_yaml)
    store.put(db, "rss:https://settled.example/rss", {"etag": '"e"', "next": "x", "moved_to": "https://settled.example/feed",
                                                      "moved_since": collect.iso(now - 8 * D)})
    store.put(db, "rss:https://recent.example/rss", {"moved_to": "https://recent.example/feed",
                                                     "moved_since": collect.iso(now - 2 * D)})
    # A fixed source that has moved: only flagged, never rewritten.
    store.put(db, "rss:https://sky.example/rss", {"moved_to": "https://sky.example/feed", "moved_since": collect.iso(now - 30 * D)})
    db.commit()

    def claude(system, prompt, output):  # keeps everything as it was
        current = people.profile(db, "alex")
        relevant = JevQuestion(id="relevant", type="score", instructions="How much?",
                               options=[Option(key="0", description="No"), Option(key="1", description="Yes")])
        return Revision(profile_md=current.profile_md, jev_questions=[relevant], summary="No change",
                        learned_sources=[LearnedSource(**s) for s in learned])

    monkeypatch.setattr(profile_refresh, "claude", claude)
    monkeypatch.setattr(profile_refresh, "gather", lambda *a: {})
    summary = profile_refresh.refresh(db, "alex")
    assert summary == "No change; feed moved: https://settled.example/rss → https://settled.example/feed"
    sources = people.parse_sources(people.profile(db, "alex").sources_yaml, "alex")
    urls = {f["url"] for f in sources["rss"]}
    assert {"https://settled.example/feed", "https://recent.example/rss", "https://sky.example/rss"} <= urls
    assert "https://settled.example/rss" not in urls
    assert store.get(db, "rss:https://settled.example/feed") == {"etag": '"e"', "next": "x"}
    assert people.versions(db, "alex")[0]["summary"] == summary


# ── JSON Feed ────────────────────────────────────────────────────────────────

def json_feed_11(published: datetime) -> bytes:
    return json.dumps({
        "version": "https://jsonfeed.org/version/1.1", "title": "Jason's Blog",
        "items": [
            {"id": "1", "url": "https://json.example/1", "title": "Hello", "content_html": "<p>Hi <b>there</b></p>",
             "date_published": published.isoformat().replace("+00:00", "Z"), "authors": [{"name": "Jason"}],
             "image": "https://json.example/1.png"},
            {"id": "2", "external_url": "https://elsewhere.example/a", "content_text": "A <short> note",
             "date_modified": published.isoformat(), "author": {"name": "Ada"}},
            {"id": "3", "title": "No link"},
        ]}).encode()


@pytest.mark.parametrize("ctype", ["application/feed+json", "text/plain"])
def test_a_json_feed_is_read_like_any_other(db, ctype):
    published = (collect.now() - timedelta(hours=2)).replace(microsecond=0)
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"\n " + json_feed_11(published), headers={"content-type": ctype})))
    items = collect.rss(db, http, {"url": "https://json.example/feed.json", "name": "Jason"})
    first, second = items
    assert (first["url"], first["title"], first["author"], first["summary"]) == (
        "https://json.example/1", "Hello", "Jason", "Hi there")
    assert first["published"] == collect.iso(published) and first["image"] == "https://json.example/1.png"
    assert second["url"] == "https://elsewhere.example/a" and second["summary"] == "A <short> note"
    assert second["author"] == "Ada" and second["published"] == collect.iso(published)
    assert store.get(db, "rss:https://json.example/feed.json")["last"] == "200-new"


def test_a_json_feed_1_0_works_too():
    published = collect.now() - timedelta(hours=1)
    doc = json.dumps({"version": "https://jsonfeed.org/version/1", "title": "Old",
                      "items": [{"id": "a", "url": "https://old.example/a", "title": "A", "summary": "Short",
                                 "date_published": published.isoformat(), "author": {"name": "Ada"}}]}).encode()
    [it] = collect.parse_feed(doc, "Old", content_type="application/json")
    assert (it["url"], it["title"], it["author"], it["summary"]) == ("https://old.example/a", "A", "Ada", "Short")


def test_json_that_is_no_json_feed_is_not_a_feed(db):
    for body in (b'{"hello": "world"}', b"{not json"):
        http = httpx.Client(transport=httpx.MockTransport(
            lambda r, body=body: httpx.Response(200, content=body, headers={"content-type": "application/json"})))
        with pytest.raises(ValueError, match="not a feed"):
            collect.rss(db, http, {"url": "https://api.example/x", "name": "X"})


def test_a_site_s_json_feed_is_found():
    page = '<html><head><link rel="alternate" type="application/feed+json" href="/feed.json"></head></html>'
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    assert scout.find_feed(http, "blog.example") == "https://blog.example/feed.json"
    # Not advertised: found in its usual place.
    doc = json_feed_11(collect.now())
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=doc) if r.url.path == "/feed.json"
        else httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}) if r.url.path == "/"
        else httpx.Response(404)))
    assert scout.find_feed(http, "blog.example") == "https://blog.example/feed.json"
