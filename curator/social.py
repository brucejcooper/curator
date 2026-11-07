"""Mastodon and Bluesky in real time, outside the 15-minute cycle.

Each followed Mastodon hashtag (read with the owner's token) and each
connected reader's home timeline (with their own) is a stream: Server-Sent
Events from the server's streaming API, outbound, the token in a header.
Every (re)connect first polls the timeline since its cursor, so a
disconnect leaves no gap; a stream that's down for more than DOWN_GRACE is
polled by the 15-minute cycle instead. Bluesky's hashtags are polled every
BLUESKY_EVERY (its firehose would cost far more bandwidth than it brings in).
What arrives is queued; DEBOUNCE after the first item, the social cycle
judges and publishes it (pipeline.social_cycle)."""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from curator import people, pipeline, store
from curator.collect import (UA, advance, bluesky, iso, mastodon, mastodon_client, now, record_home, retry_after,
                             shared_sources, status_id, statuses_items)
from curator.config import OWNER

log = logging.getLogger(__name__)
DOWN_GRACE = timedelta(minutes=5)  # down for longer: the 15-minute cycle polls it
BACKOFF_FIRST, BACKOFF_MAX = 2.0, 300.0  # seconds between reconnects, doubling
STAYED_UP = 60  # seconds connected before the backoff starts again from the first
HEARTBEAT = 90  # seconds without a byte (Mastodon sends one every 15 s or so): reconnect
TIMEOUT = 10  # seconds, for everything but waiting on the stream
MAX_EVENT = 1_000_000  # characters; a bigger event isn't a status
DEBOUNCE = 45  # seconds from the first queued item to the social cycle
BLUESKY_EVERY = 90  # seconds between polls of each Bluesky hashtag
BLUESKY_MAX_BACKOFF = 900.0  # a failing tag waits up to this long


# ── Server-Sent Events ───────────────────────────────────────────────────────

HEARTBEAT_EVENT = ":"


class SSE:
    """Server-Sent Events, a line at a time. `line` returns (event, data)
    when a blank line completes one, (HEARTBEAT_EVENT, text) for a comment
    (Mastodon's heartbeat), else None."""

    def __init__(self):
        self.event, self.data, self.size = "", [], 0

    def line(self, line: str) -> tuple[str, str] | None:
        if not line:
            event, data = self.event or "message", "\n".join(self.data)
            had = bool(self.data) and self.size <= MAX_EVENT
            self.event, self.data, self.size = "", [], 0
            return (event, data) if had else None
        if line.startswith(":"):
            return HEARTBEAT_EVENT, line[1:].strip()
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            self.event = value
        elif name == "data":
            self.size += len(value)
            if self.size <= MAX_EVENT:
                self.data.append(value)
        return None


def status_of(data: str) -> dict | None:
    """The status an `update` event carries, if it looks like one."""
    try:
        s = json.loads(data)
    except ValueError:
        return None
    if not isinstance(s, dict) or not status_id(s.get("id")) or not isinstance(s.get("created_at"), str):
        return None
    return s


def backoff(attempt: int, at_least: timedelta | None = None) -> float:
    """Seconds before reconnect number `attempt` (0 for the first): doubling
    up to BACKOFF_MAX, with jitter so streams don't reconnect in step; at
    least what Retry-After asked, up to BACKOFF_MAX."""
    wait = min(BACKOFF_FIRST * 2 ** min(attempt, 20), BACKOFF_MAX)
    wait *= random.uniform(0.5, 1.0)
    if at_least is not None:
        wait = max(wait, min(max(at_least.total_seconds(), 0), BACKOFF_MAX))
    return wait


def streaming_url(value) -> str | None:
    """The base of a server's streaming API from its `configuration.urls.
    streaming` (wss://…), as https; None for anything else."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError, TypeError):
        return None
    if url.scheme not in ("wss", "https") or not url.host:
        return None
    port = f":{url.port}" if url.port else ""
    return f"https://{url.host}{port}{url.path.rstrip('/')}"


# ── Mastodon streams ─────────────────────────────────────────────────────────

class Refused(Exception):
    def __init__(self, status: int, later: timedelta | None):
        super().__init__(f"HTTP {status}")
        self.later = later


@dataclass
class Stream:
    """One Mastodon stream: what it reads, and how it's doing."""
    source: str  # mastodon:#tag, or mastodon:home:<user>
    server: str
    token: str = field(repr=False)
    path: str  # the streaming endpoint
    params: dict
    poll: str  # the timeline that fills a gap
    home_user: str | None = None
    connected: bool = False
    since: datetime = field(default_factory=now)  # connected, or down, since
    last_event: datetime | None = None
    last_heartbeat: datetime | None = None
    events: int = 0
    connects: int = 0
    last_error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def same(self, other: "Stream") -> bool:
        return (self.server, self.token, self.path, self.params, self.poll, self.home_user) == \
            (other.server, other.token, other.path, other.params, other.poll, other.home_user)

    def up(self) -> None:
        self.connected, self.since, self.last_error = True, now(), ""
        self.connects += 1

    def down(self, error: str) -> None:
        if self.connected:
            self.since = now()
        self.connected, self.last_error = False, error

    def view(self) -> dict:
        return {"source": self.source, "connected": self.connected, "since": iso(self.since),
                "last_event": iso(self.last_event) if self.last_event else None,
                "last_heartbeat": iso(self.last_heartbeat) if self.last_heartbeat else None,
                "events": self.events, "reconnects": max(self.connects - 1, 0), "last_error": self.last_error}


def wanted(conn) -> tuple[dict[str, Stream], set[str]]:
    """The streams everyone's sources call for, and the Bluesky hashtags."""
    everyone = pipeline.profiles(conn)
    sources = shared_sources(everyone)
    streams = {}
    owner = people.mastodon(conn, OWNER)
    if owner:
        for t in sorted(sources["mastodon_tags"]):
            s = Stream(f"mastodon:#{t}", owner[0], owner[1], "/api/v1/streaming/hashtag", {"tag": t},
                       f"/api/v1/timelines/tag/{t}")
            streams[s.source] = s
    for p in everyone:
        creds = people.mastodon(conn, p.user)
        if creds and p.sources["mastodon_home"]:
            s = Stream(people.home_source(p.user), creds[0], creds[1], "/api/v1/streaming/user", {},
                       "/api/v1/timelines/home", p.user)
            streams[s.source] = s
    return streams, set(sources["bluesky_tags"])


def gap_fill(stream: Stream) -> list[dict]:
    """The stream's timeline since its cursor (what was missed while it
    was down)."""
    conn = store.connect()
    try:
        home_map: list | None = [] if stream.home_user else None
        with mastodon_client(stream.server, stream.token, TIMEOUT) as http:
            items = mastodon(conn, http, stream.poll, stream.source, {}, home_map)
        if stream.home_user:
            record_home(conn, stream.home_user, home_map)
        return items
    finally:
        conn.close()


def take(stream: Stream, status: dict) -> list[dict]:
    """The items a streamed status makes, as a poll would; moves the cursor."""
    conn = store.connect()
    try:
        home_map: list | None = [] if stream.home_user else None
        with mastodon_client(stream.server, stream.token, TIMEOUT) as http:
            items = statuses_items(http, [status], stream.source, {}, home_map)
        advance(conn, stream.source, status["id"])
        if stream.home_user:
            record_home(conn, stream.home_user, home_map)
        return items
    finally:
        conn.close()


# ── Bluesky ──────────────────────────────────────────────────────────────────

@dataclass
class TagPoll:
    """One Bluesky hashtag's polling: when, how it went."""
    tag: str
    last_poll: datetime | None = None
    last_success: datetime | None = None
    next: datetime | None = None
    errors: int = 0
    backoff: float = 0.0  # seconds, while failing
    last_error: str = ""

    def ok(self, at: datetime) -> None:
        self.last_success, self.errors, self.backoff, self.last_error = at, 0, 0.0, ""
        self.next = at + timedelta(seconds=BLUESKY_EVERY)

    def failed(self, at: datetime, error: str, later: timedelta | None = None) -> None:
        self.errors += 1
        self.backoff = min(BLUESKY_EVERY * 2.0 ** min(self.errors, 20), BLUESKY_MAX_BACKOFF)
        if later is not None:
            self.backoff = max(self.backoff, min(max(later.total_seconds(), 0), BLUESKY_MAX_BACKOFF))
        self.last_error = error
        self.next = at + timedelta(seconds=self.backoff)

    def view(self) -> dict:
        stamp = lambda d: iso(d) if d else None
        return {"tag": self.tag, "last_poll": stamp(self.last_poll), "last_success": stamp(self.last_success),
                "backoff": self.backoff, "errors": self.errors, "last_error": self.last_error}


# ── All of it ────────────────────────────────────────────────────────────────

class Social:
    """The streams, the Bluesky poller and the queue they feed, run as tasks
    on the server's event loop. `covers` tells the 15-minute cycle what it
    needn't poll."""

    def __init__(self, debounce: float = DEBOUNCE):
        self.streams: dict[str, Stream] = {}
        self.tags: dict[str, TagPoll] = {}
        self.queue: list[dict] = []
        self.arrived = asyncio.Event()
        self.debounce = debounce
        self.bases: dict[str, str] = {}  # server → its streaming API
        self.tasks: list[asyncio.Task] = []
        self.bsky: httpx.Client | None = None

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self.cycles()), asyncio.create_task(self.poll_bluesky())]

    async def close(self) -> None:
        for task in self.tasks + [s.task for s in self.streams.values() if s.task]:
            task.cancel()
        if self.bsky:
            self.bsky.close()

    def covers(self, source: str) -> bool:
        """Whether a source arrives here, so the 15-minute cycle needn't poll it."""
        if source.startswith("bluesky:#"):
            return source.removeprefix("bluesky:#") in self.tags
        s = self.streams.get(source)
        return s is not None and (s.connected or now() - s.since <= DOWN_GRACE)

    async def reconcile(self) -> None:
        """Start and stop streams and Bluesky tags to match everyone's
        sources (a hashtag followed or dropped, a reader connecting)."""
        try:
            streams, tags = await asyncio.to_thread(self._wanted)
        except Exception:
            log.exception("social sources")
            return
        for source in list(self.streams):
            if source not in streams or not streams[source].same(self.streams[source]):
                if task := self.streams.pop(source).task:
                    task.cancel()
                log.info("stream %s stopped", source)
        for source, s in streams.items():
            if source not in self.streams:
                self.streams[source] = s
                s.task = asyncio.create_task(self.run(s))
                log.info("stream %s started", source)
        self.tags = {t: self.tags.get(t) or TagPoll(t) for t in sorted(tags)}

    @staticmethod
    def _wanted():
        conn = store.connect()
        try:
            return wanted(conn)
        finally:
            conn.close()

    def add(self, items: list[dict]) -> None:
        if items:
            self.queue += items
            self.arrived.set()

    # ── Mastodon ──

    async def base(self, server: str) -> str:
        """Where a server's streaming API is (configuration.urls.streaming)."""
        if server not in self.bases:
            found = None
            try:
                async with httpx.AsyncClient(headers=UA, timeout=TIMEOUT) as http:
                    resp = await http.get(f"https://{server}/api/v2/instance")
                    if resp.is_success:
                        found = streaming_url(((resp.json().get("configuration") or {}).get("urls") or {}).get("streaming"))
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                log.warning("mastodon %s: no instance info (%s)", server, e)
            if not found:
                return f"https://{server}"  # the API itself streams too; looked up again next time
            self.bases[server] = found
        return self.bases[server]

    async def run(self, s: Stream) -> None:
        """Keep a stream connected: backoff between tries, the gap filled on
        every connect, its events queued."""
        attempt = 0
        while True:
            opened, later = None, None
            try:
                url = await self.base(s.server) + s.path
                headers = {**UA, "Authorization": f"Bearer {s.token}", "Accept": "text/event-stream"}
                async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(TIMEOUT, read=HEARTBEAT)) as http:
                    async with http.stream("GET", url, params=s.params) as resp:
                        if resp.status_code != 200:
                            raise Refused(resp.status_code, retry_after(resp.headers, now()))
                        opened = time.monotonic()
                        # Before taking its events: what came while it was down.
                        self.add(await asyncio.to_thread(gap_fill, s))
                        s.up()
                        log.info("stream %s connected", s.source)
                        await self.events(s, resp.aiter_lines())
                raise ConnectionError("the stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # anything: the stream must come back
                later = e.later if isinstance(e, Refused) else None
                s.down(f"{type(e).__name__}: {e}"[:300])
                log.warning("stream %s: %s", s.source, s.last_error)
            if opened is not None and time.monotonic() - opened >= STAYED_UP:
                attempt = 0
            await asyncio.sleep(backoff(attempt, later))
            attempt += 1

    async def events(self, s: Stream, lines) -> None:
        sse = SSE()
        async for line in lines:
            event = sse.line(line)
            if event is None:
                continue
            kind, data = event
            if kind == HEARTBEAT_EVENT:
                s.last_heartbeat = now()
                continue
            if kind != "update":  # notifications, deletes, edits, filters
                continue
            status = status_of(data)
            if status is None:
                log.warning("stream %s: an update that isn't a status", s.source)
                continue
            s.last_event, s.events = now(), s.events + 1
            try:
                self.add(await asyncio.to_thread(take, s, status))
            except Exception as e:
                log.warning("stream %s: status %s: %s", s.source, status["id"], e)

    # ── Bluesky ──

    async def poll_bluesky(self) -> None:
        while True:
            await self.poll_tags()
            await asyncio.sleep(BLUESKY_EVERY)

    async def poll_tags(self) -> None:
        """One round: every tag that's due, one after another; a failing
        one backs off on its own."""
        for t in list(self.tags.values()):
            at = now()
            if t.next and at < t.next:
                continue
            t.last_poll = at
            try:
                self.add(await asyncio.to_thread(self._bluesky, t.tag))
                t.ok(at)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                later = retry_after(e.response.headers, at) if isinstance(e, httpx.HTTPStatusError) else None
                t.failed(at, f"{type(e).__name__}: {e}"[:300], later)
                log.warning("bluesky #%s: %s (next in %.0f s)", t.tag, t.last_error, t.backoff)

    def _bluesky(self, tag: str) -> list[dict]:
        if self.bsky is None:
            self.bsky = httpx.Client(headers=UA, timeout=TIMEOUT)
        conn = store.connect()
        try:
            return bluesky(conn, self.bsky, tag)
        finally:
            conn.close()

    # ── The social cycle ──

    async def cycles(self) -> None:
        while True:
            await self.arrived.wait()
            await asyncio.sleep(self.debounce)
            await self.drain()

    async def drain(self) -> None:
        items, self.queue = self.queue, []
        self.arrived.clear()
        if items:
            try:
                await asyncio.to_thread(pipeline.social_cycle, items)
            except Exception:
                log.exception("social cycle")

    def view(self) -> dict:
        return {"streams": [s.view() for _, s in sorted(self.streams.items())],
                "bluesky": [t.view() for _, t in sorted(self.tags.items())],
                "queued": len(self.queue)}
