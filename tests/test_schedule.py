"""Polling each feed as often as it changes: the schedule, from its entries'
dates and the publisher's hints, and the collection that keeps to it."""

from datetime import datetime, timedelta, timezone

import httpx

from curator import collect, store
from curator.collect import due, schedule

NOW = datetime(2026, 11, 8, 6, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def every(gap: timedelta, n: int = 10) -> list[datetime]:
    return [NOW - gap * i for i in range(n)]


def wait(state: dict) -> timedelta:
    return datetime.fromisoformat(state["next"]) - NOW


def fetched(dates=None, headers=None, ttl=None, state=None, marker="m1"):
    return schedule(state or {}, 200, httpx.Headers(headers or {}), dates, marker, ttl, NOW)


def test_a_daily_feed_is_fetched_every_four_hours():
    s = fetched(every(timedelta(days=1)))
    assert wait(s) == 4 * H and s["reason"] == "adaptive" and s["last"] == "200-new"


def test_an_hourly_feed_every_half_hour():
    assert wait(fetched(every(H))) == H / 2


def test_a_busy_feed_every_cycle():
    assert wait(fetched(every(timedelta(minutes=5)))) == timedelta(minutes=15)


def test_the_median_gap_ignores_one_quiet_night():
    dates = every(H, 9) + [NOW - 20 * H]
    assert wait(fetched(dates)) == H / 2


def test_ttl_is_never_fetched_sooner():
    s = fetched(every(timedelta(minutes=5)), ttl="120")
    assert wait(s) == 2 * H and s["reason"] == "ttl"
    assert not due(s, NOW + 2 * H - timedelta(minutes=1)) and due(s, NOW + 2 * H)
    # A 304 brings no <ttl>: the one the feed gave still holds.
    again = schedule(s, 304, httpx.Headers(), None, None, None, NOW)
    assert wait(again) == 2 * H and again["last"] == "304"


def test_max_age_and_expires():
    s = fetched(every(timedelta(minutes=5)), {"Cache-Control": "public, max-age=3600"})
    assert wait(s) == H and s["reason"] == "max-age"
    assert not due(s, NOW + H - timedelta(seconds=1))
    s = fetched(every(timedelta(minutes=5)), {"Expires": "Sun, 08 Nov 2026 08:00:00 GMT"})
    assert wait(s) == 2 * H and s["reason"] == "expires"
    # max-age wins over Expires; no-cache isn't a hint; nor is a short one.
    assert wait(fetched(every(H), {"Cache-Control": "max-age=7200", "Expires": "Sun, 08 Nov 2026 07:00:00 GMT"})) == 2 * H
    assert fetched(every(H), {"Cache-Control": "no-cache, max-age=7200"})["reason"] == "adaptive"
    assert fetched(every(H), {"Cache-Control": "max-age=60"})["reason"] == "adaptive"


def test_hints_are_capped_at_a_day():
    assert wait(fetched(every(H), {"Cache-Control": "max-age=31536000"})) == 24 * H


def test_retry_after_in_seconds_and_as_a_date():
    s = schedule({"interval": 900}, 429, httpx.Headers({"Retry-After": "7200"}), None, None, None, NOW)
    assert wait(s) == 2 * H and s["reason"] == "retry-after" and s["last"] == "429"
    s = schedule({"interval": 900}, 503, httpx.Headers({"Retry-After": "Sun, 08 Nov 2026 09:00:00 GMT"}), None, None, None, NOW)
    assert wait(s) == 3 * H and not due(s, NOW + 3 * H - timedelta(minutes=1))
    # Without one, an ordinary error.
    assert schedule({"interval": 900}, 429, httpx.Headers(), None, None, None, NOW)["reason"] == "error"


def test_errors_back_off_doubling_up_to_a_day_and_reset_on_success():
    s = {"interval": 900}
    waits = []
    for _ in range(3):
        s = schedule(s, None, {}, None, None, None, NOW)
        waits.append(wait(s))
    assert waits == [H / 2, H, 2 * H] and s["errors"] == 3 and s["reason"] == "error"
    for _ in range(10):
        s = schedule(s, 500, {}, None, None, None, NOW)
    assert wait(s) == 24 * H and s["last"] == "500"
    s = fetched(every(H), state=s)
    assert s["errors"] == 0 and wait(s) == H / 2
    assert wait(schedule(s, None, {}, None, None, None, NOW)) == H  # doubling starts again from the interval


def test_a_feed_without_dates_learns_from_its_fetches():
    s = fetched(None, marker="a")  # a new feed: the first fetch brings something new
    assert wait(s) == timedelta(minutes=15)
    s = fetched(None, marker="a", state=s)  # nothing new
    assert wait(s) == timedelta(minutes=22.5) and s["last"] == "200"
    s = schedule(s, 304, httpx.Headers(), None, None, None, NOW)
    assert wait(s) == timedelta(minutes=33.75)
    for _ in range(10):
        s = schedule(s, 304, httpx.Headers(), None, None, None, NOW)
    assert wait(s) == 4 * H
    s = fetched(None, marker="b", state=s)  # something new: twice as often
    assert wait(s) == 2 * H and s["last"] == "200-new"


def test_its_own_schedule_is_met_by_the_nearest_cycle():
    s = fetched(every(timedelta(minutes=5)))
    assert due(s, NOW + timedelta(minutes=14, seconds=50))  # the next cycle started a little early
    assert not due(s, NOW + timedelta(minutes=5))
    assert due({}, NOW)


FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><ttl>60</ttl>
<item><title>One</title><link>https://blog.example/1</link><pubDate>Sun, 08 Nov 2026 05:00:00 GMT</pubDate></item>
<item><title>Two</title><link>https://blog.example/2</link><pubDate>Sat, 07 Nov 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_only_feeds_that_are_due_are_fetched(db, monkeypatch):
    class Reader:
        sources = {"rss": [{"url": "https://blog.example/rss", "name": "Blog"}, {"url": "https://new.example/rss", "name": "New"}],
                   "mastodon_tags": set(), "bluesky_tags": set(), "mastodon_accounts": set(),
                   "bluesky_people": set(), "instagram_people": set(), "mastodon_home": False}
        user = "alex"

    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, content=FEED, headers={"ETag": '"v1"'})

    real = httpx.Client
    monkeypatch.setattr(collect.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(collect, "now", lambda: NOW)
    monkeypatch.setattr(collect.instagram, "collect", lambda conn, handles: [])
    monkeypatch.setattr(collect.people, "mastodon", lambda conn, user: None)
    store.put(db, "rss:https://blog.example/rss", {"next": collect.iso(NOW + H), "reason": "adaptive", "etag": '"v0"'})
    tally: dict = {}
    found = collect.collect_all(db, [Reader()], tally)
    assert asked == ["https://new.example/rss"] and tally == {"feeds_fetched": 1, "feeds_skipped": 1}
    assert {i["url"] for i in found} == {"https://blog.example/1", "https://blog.example/2"}
    s = store.get(db, "rss:https://new.example/rss")
    assert s["etag"] == '"v1"' and s["name"] == "New" and s["reason"] == "adaptive" and wait(s) == 4 * H


def test_a_failed_fetch_is_scheduled_and_still_raised(db):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    try:
        collect.rss(db, http, {"url": "https://gone.example/rss", "name": "Gone"}, NOW)
        raise AssertionError("expected an error")
    except httpx.HTTPStatusError:
        pass
    s = store.get(db, "rss:https://gone.example/rss")
    assert s["last"] == "404" and s["errors"] == 1 and wait(s) == H / 2


def test_out_of_range_hints_are_capped_or_ignored():
    for value in ("inf", "-inf", "nan", "1e20"):
        s = fetched(every(H), {"Cache-Control": f"max-age={value}"}, ttl=value)
        assert timedelta(0) < wait(s) <= 24 * H
        s = schedule({"interval": 900}, 429, httpx.Headers({"Retry-After": value}), None, None, None, NOW)
        assert timedelta(0) <= wait(s) <= 24 * H
    assert wait(fetched(every(H), {"Cache-Control": "max-age=1e20"})) == 24 * H
    assert wait(fetched(every(H), ttl="1e15")) == 24 * H
    assert wait(fetched(every(H), ttl="inf")) == H / 2  # not a number: no hint
    s = schedule({"interval": 900}, 503, httpx.Headers({"Retry-After": "1e20"}), None, None, None, NOW)
    assert wait(s) == 24 * H and s["reason"] == "retry-after"


def test_any_success_is_read_as_a_feed(db, monkeypatch):
    monkeypatch.setattr(collect, "now", lambda: NOW)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(203, content=FEED)))
    items = collect.rss(db, http, {"url": "https://proxied.example/rss", "name": "Proxied"}, NOW)
    assert len(items) == 2 and store.get(db, "rss:https://proxied.example/rss")["last"] == "200-new"
