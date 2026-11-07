"""Mastodon streams, the Bluesky poller and the social cycle (social.py),
with every server mocked."""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from curator import collect, pipeline, social, store
from curator.social import SSE, Social, Stream, TagPoll

LONG = "a long enough post about home automation and ESPHome sensors " * 2


def toot(sid: str, text: str = LONG) -> dict:
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"id": sid, "created_at": created, "content": f"<p>{text}</p>", "language": "en", "visibility": "public",
            "account": {"acct": "someone@x.social"}, "url": f"https://x.social/@someone/{sid}",
            "in_reply_to_id": None, "card": None, "reblog": None}


def tag_stream() -> Stream:
    return Stream("mastodon:#homelab", "m.example", "sekrit", "/api/v1/streaming/hashtag", {"tag": "homelab"},
                  "/api/v1/timelines/tag/homelab")


def sse(*events: tuple[str, str]) -> bytes:
    return "".join(f":thump\n\nevent: {e}\ndata: {d}\n\n" for e, d in events).encode()


@pytest.fixture
def mocked(monkeypatch):
    """Route every client, sync and async, to `handler` (set by the test)."""
    box = {}
    sync, async_ = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda **kw: sync(transport=httpx.MockTransport(lambda r: box["h"](r)), **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_(transport=httpx.MockTransport(lambda r: box["h"](r)), **kw))
    return box


# ── SSE ──────────────────────────────────────────────────────────────────────

def test_sse_events_and_heartbeats():
    p = SSE()
    got = [e for line in ["event: update", 'data: {"a":', "data: 1}", "", ":thump", "data: x", "", "event: delete",
                          "data:99", ""] if (e := p.line(line))]
    assert got == [("update", '{"a":\n1}'), (":", "thump"), ("message", "x"), ("delete", "99")]
    assert p.line("") is None  # a blank line with nothing before it


def test_sse_drops_an_oversized_event(monkeypatch):
    monkeypatch.setattr(social, "MAX_EVENT", 10)
    p = SSE()
    assert [p.line(x) for x in ("event: update", "data: " + "x" * 50, "")] == [None, None, None]
    assert p.line("data: ok") is None and p.line("") == ("message", "ok")  # the next one is fine


def test_only_updates_are_taken(monkeypatch):
    taken = []
    monkeypatch.setattr(social, "take", lambda s, status: taken.append(status["id"]) or [{"url": status["id"]}])
    s, live = tag_stream(), Social()

    async def lines():
        for line in sse(("update", json.dumps(toot("7"))), ("notification", "{}"), ("delete", "7"),
                        ("status.update", json.dumps(toot("7"))), ("update", "not json"),
                        ("update", json.dumps({**toot("8"), "id": "../8"}))).decode().split("\n"):
            yield line

    asyncio.run(live.events(s, lines()))
    assert taken == ["7"] and live.queue == [{"url": "7"}] and s.events == 1
    assert s.last_heartbeat is not None and s.last_event is not None


# ── Reconnects and the gap ───────────────────────────────────────────────────

def test_reconnects_fill_the_gap_first_and_move_the_cursor(db, mocked, monkeypatch):
    store.put(db, "since:mastodon:#homelab", "100")
    db.commit()
    seen, waits = [], []
    connects = iter([httpx.Response(502), httpx.Response(200, content=sse(("update", json.dumps(toot("110"))))),
                     httpx.Response(200, content=sse(("update", json.dumps(toot("120")))))])

    def handler(req: httpx.Request):
        assert "sekrit" not in str(req.url) and req.headers["authorization"] == "Bearer sekrit"
        if req.url.host == "stream.m.example":
            seen.append("stream")
            try:
                return next(connects)
            except StopIteration:
                raise asyncio.CancelledError  # the test is over
        assert req.url.path == "/api/v1/timelines/tag/homelab"
        since = req.url.params["since_id"]
        seen.append(f"gap since {since}")
        return httpx.Response(200, json=[toot("105")] if since == "100" else [])

    mocked["h"] = handler
    monkeypatch.setattr(social, "backoff", lambda attempt, later=None: waits.append(attempt) or 0)
    s, live = tag_stream(), Social()
    live.bases["m.example"] = "https://stream.m.example"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(live.run(s))
    # The gap is filled from the stored cursor before any event is taken; the
    # next connect's gap fill starts where the stream left off.
    assert seen == ["stream", "stream", "gap since 100", "stream", "gap since 110", "stream"]
    assert [i["url"].rsplit("/", 1)[1] for i in live.queue] == ["105", "110", "120"]
    assert store.get(store.connect(), "since:mastodon:#homelab") == "120"
    assert waits == [0, 1, 2] and s.view()["reconnects"] == 1 and not s.connected
    assert "sekrit" not in json.dumps(s.view()) and "sekrit" not in repr(s)


def test_backoff_doubles_with_jitter_and_honours_retry_after():
    assert 1 <= social.backoff(0) <= 2 and 4 <= social.backoff(2) <= 8
    assert social.backoff(10_000) <= social.BACKOFF_MAX  # no overflow
    assert social.backoff(0, timedelta(seconds=60)) == 60
    assert social.backoff(0, timedelta(days=9)) == social.BACKOFF_MAX
    assert social.backoff(0, timedelta(seconds=-5)) <= 2


def test_cursor_never_moves_back(db):
    collect.advance(db, "mastodon:#x", "200")
    for sid in ("199", "99", "1e9", "", None, "-5", "300 ", "9" * 40, 12345):
        collect.advance(db, "mastodon:#x", sid)
    assert store.get(db, "since:mastodon:#x") == "200"
    collect.advance(db, "mastodon:#x", "1000")
    assert store.get(db, "since:mastodon:#x") == "1000"


def test_streaming_url_from_the_instance():
    assert social.streaming_url("wss://stream.mastodon.au") == "https://stream.mastodon.au"
    assert social.streaming_url("https://m.example:4443/") == "https://m.example:4443"
    for bad in ("ws://m.example", "http://m.example", "javascript:alert(1)", "", None, 7, "wss://", "x" * 400):
        assert social.streaming_url(bad) is None


def test_the_streaming_base_falls_back_to_the_server(mocked):
    mocked["h"] = lambda r: httpx.Response(200, json={"configuration": {"urls": {"streaming": "ws://plain"}}})
    live = Social()
    assert asyncio.run(live.base("m.example")) == "https://m.example" and "m.example" not in live.bases
    mocked["h"] = lambda r: httpx.Response(200, json={"configuration": {"urls": {"streaming": "wss://s.m.example"}}})
    assert asyncio.run(live.base("m.example")) == "https://s.m.example" and live.bases["m.example"]


# ── Bluesky ──────────────────────────────────────────────────────────────────

def test_a_failing_tag_backs_off_and_the_others_still_run(db, mocked):
    asked = []

    def handler(req: httpx.Request):
        asked.append(req.url.params["q"])
        assert req.url.host == "api.bsky.app" and "since" not in req.url.params
        if req.url.params["q"] == "#broken":
            return httpx.Response(500)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return httpx.Response(200, json={"posts": [{"uri": "at://did:a/app.bsky.feed.post/1", "labels": [],
                                                    "author": {"handle": "a.bsky.social"},
                                                    "record": {"text": LONG, "createdAt": now, "langs": ["en"]}}]})

    mocked["h"] = handler
    live = Social()
    live.tags = {t: TagPoll(t) for t in ("broken", "fine")}
    asyncio.run(live.poll_tags())
    assert asked == ["#broken", "#fine"] and len(live.queue) == 1
    broken, fine = live.tags["broken"], live.tags["fine"]
    assert broken.errors == 1 and broken.backoff == 180 and "500" in broken.last_error and fine.last_success
    asyncio.run(live.poll_tags())  # neither is due yet
    assert asked == ["#broken", "#fine"]
    for _ in range(10):
        broken.failed(datetime.now(timezone.utc), "again")
    assert broken.backoff == social.BLUESKY_MAX_BACKOFF


@pytest.mark.parametrize("value", ["inf", "nan", "1e999", "-1", "99999999999", "soon", "Mon, 99 Foo 2026"])
def test_retry_after_from_a_server_stays_bounded(value):
    t = TagPoll("x")
    t.failed(datetime.now(timezone.utc), "429", collect.retry_after(httpx.Headers({"retry-after": value}), collect.now()))
    assert math.isfinite(t.backoff) and social.BLUESKY_EVERY * 2 <= t.backoff <= social.BLUESKY_MAX_BACKOFF


def test_bluesky_dates_from_posts_are_checked(db):
    stamp = lambda **ago: (datetime.now(timezone.utc) - timedelta(**ago)).isoformat().replace("+00:00", "Z")
    assert collect.bsky_time(stamp(hours=1)) and collect.bsky_time(stamp(minutes=-1))  # a clock a minute fast
    for bad in ("2999-01-01T00:00:00Z", stamp(hours=-1), "yesterday", "", None, 5, "9" * 100, "nan"):
        assert collect.bsky_time(bad) is None
    post = lambda when: {"record": {"createdAt": when}}
    hour_ago = stamp(hours=1)
    kept = collect.bsky_since(db, "bluesky:x", [post("2999-01-01T00:00:00Z"), post(hour_ago), "junk", post(None)])
    assert kept == [post(hour_ago)]
    assert store.get(db, "bluesky:x") == hour_ago  # not held in 2999
    assert collect.bsky_since(db, "bluesky:x", [post(hour_ago.replace("Z", "+00:00"))]) == []


# ── The social cycle ─────────────────────────────────────────────────────────

def test_the_social_cycle_waits_then_takes_everything_queued(monkeypatch):
    runs = []
    monkeypatch.setattr(pipeline, "social_cycle", lambda items: runs.append([i["url"] for i in items]))

    async def go():
        live = Social(debounce=0.2)
        task = asyncio.create_task(live.cycles())
        await asyncio.sleep(0.05)
        assert runs == []  # nothing queued: nothing runs
        live.add([{"url": "a"}])
        await asyncio.sleep(0.05)
        live.add([{"url": "b"}])
        live.add([])
        assert runs == []
        await asyncio.sleep(0.3)
        assert runs == [["a", "b"]] and live.queue == []
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(go())
    assert runs == [["a", "b"]]


def test_the_social_cycle_publishes_only_the_readers_it_reaches(db, monkeypatch):
    class Reader:
        def __init__(self, user, keys):
            self.user, self.sources = user, {"keys": keys}

    def ingest(conn, found):
        for it in found:
            conn.execute("INSERT INTO sightings VALUES (?, ?, ?, ?)", (it["url"], it["source"], it["url"], collect.iso(collect.now())))
        return [it["url"] for it in found]

    judged, published = [], []
    monkeypatch.setattr(pipeline, "ingest", ingest)
    monkeypatch.setattr(pipeline, "apply_shared_rules", lambda conn, ids: None)
    monkeypatch.setattr(pipeline, "judge_traits", lambda conn, ids: None)
    monkeypatch.setattr(pipeline, "judge_for", lambda conn, p, ids: judged.append(p.user))
    monkeypatch.setattr(pipeline, "publish", lambda conn, p: published.append(p.user) or 3)
    monkeypatch.setattr(pipeline, "profiles", lambda conn: [Reader("alex", {"mastodon:#homelab"}), Reader("sam", {"rss:x"})])
    result = pipeline.social_cycle([{"url": "u1", "source": "mastodon:#homelab"}])
    assert judged == published == ["alex"] and result["published"] == {"alex": 3} and result["new"] == 1
    assert store.get(store.connect(), "last_social_cycle")["found"] == 1


# ── The fallback ─────────────────────────────────────────────────────────────

def test_the_cycle_polls_only_what_isnt_live(db, mocked, monkeypatch):
    class Reader:
        sources = {"rss": [], "mastodon_tags": {"up", "down", "new"}, "bluesky_tags": {"sky"}, "mastodon_accounts": set(),
                   "bluesky_people": set(), "instagram_people": set(), "mastodon_home": True}
        user = "alex"

    asked = []
    mocked["h"] = lambda req: asked.append(req.url.path) or httpx.Response(200, json={"posts": []} if "bsky" in req.url.host else [])
    monkeypatch.setattr(collect.instagram, "collect", lambda conn, handles: [])
    monkeypatch.setattr(collect.people, "mastodon", lambda conn, user: ("m.example", "sekrit", "1"))
    live = Social()
    long_ago = collect.now() - timedelta(minutes=6)
    for tag, connected, since in (("up", True, long_ago), ("down", False, long_ago), ("starting", False, collect.now())):
        s = Stream(f"mastodon:#{tag}", "m.example", "sekrit", "", {}, "")
        s.connected, s.since = connected, since
        live.streams[s.source] = s
    home = Stream("mastodon:home:alex", "m.example", "sekrit", "", {}, "", "alex")
    home.since = collect.now() - timedelta(minutes=4)  # down, but not for long
    live.streams[home.source] = home
    live.tags = {"sky": TagPoll("sky")}
    collect.collect_all(db, [Reader()], {}, live)
    # Down more than 5 minutes, or not streamed at all: polled as before.
    assert sorted(asked) == ["/api/v1/timelines/tag/down", "/api/v1/timelines/tag/new"]
    asked.clear()
    collect.collect_all(db, [Reader()], {})  # without streams, everything is polled
    assert "/api/v1/timelines/home" in asked and "/xrpc/app.bsky.feed.searchPosts" in asked and len(asked) == 5


def test_reconcile_follows_the_sources(monkeypatch):
    wanted = {"streams": {"mastodon:#a": tag_stream()}, "tags": {"x"}}
    monkeypatch.setattr(Social, "_wanted", staticmethod(lambda: ({k: Stream(**{**v.__dict__, "task": None, "connected": False})
                                                                   for k, v in wanted["streams"].items()}, wanted["tags"])))
    started = []

    async def run(self, s):
        started.append(s.token)
        await asyncio.sleep(3600)

    monkeypatch.setattr(Social, "run", run)

    async def go():
        live = Social()
        await live.reconcile()
        await asyncio.sleep(0)
        first = live.streams["mastodon:#a"]
        await live.reconcile()  # unchanged: left running
        assert live.streams["mastodon:#a"] is first and set(live.tags) == {"x"}
        wanted["streams"] = {"mastodon:#a": Stream(**{**tag_stream().__dict__, "token": "new"})}
        wanted["tags"] = set()
        await live.reconcile()  # a new token: restarted with it
        await asyncio.sleep(0)
        assert first.task.cancelled() and live.tags == {}
        wanted["streams"] = {}
        await live.reconcile()
        assert live.streams == {}

    asyncio.run(go())
    assert started == ["sekrit", "new"]
