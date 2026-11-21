"""Collect candidate items for every reader at once: the RSS/Atom feeds,
Mastodon and Bluesky hashtags and people, and Instagram people in anyone's
sources (each fetched once), and each connected reader's own Mastodon home
timeline. The hashtags and home timelines also arrive in real time
(social.py), which shares the Mastodon and Bluesky code in posts.py."""

import logging

import httpx

from curator import instagram, people, store
from curator.config import OWNER
from curator.feeds import due, rss
from curator.items import UA, now
from curator.posts import bluesky, bluesky_person, mastodon, mastodon_account, mastodon_client

log = logging.getLogger(__name__)


def shared_sources(profiles: list) -> dict:
    """Everyone's sources, each once. A feed two readers both follow (maybe
    under different names) is fetched once and seen under each name."""
    feeds: dict[str, dict] = {}
    tags, bsky, accounts, people_, insta = set(), set(), set(), set(), set()
    for p in profiles:
        src = p.sources
        for f in src["rss"]:
            entry = feeds.setdefault(f["url"], {"url": f["url"], "name": f["name"], "names": set()})
            entry["names"].add(f["name"])
        tags |= src["mastodon_tags"]
        bsky |= src["bluesky_tags"]
        accounts |= src["mastodon_accounts"]
        people_ |= src["bluesky_people"]
        insta |= src["instagram_people"]
    return {"rss": list(feeds.values()), "mastodon_tags": tags, "bluesky_tags": bsky,
            "mastodon_accounts": accounts, "bluesky_people": people_, "instagram_people": insta}


def collect_all(conn, profiles: list, tally: dict | None = None, live=None) -> list[dict]:
    """New items from everyone's sources, and each connected reader's home
    timeline. `tally` collects how many feeds were fetched, and skipped (not due).
    `live` (social.Social) says which sources arrive in real time and so
    aren't polled here: a Mastodon stream that's up (or only just down), a
    Bluesky tag the poller has. Without it, everything is polled."""
    sources = shared_sources(profiles)
    polled = (lambda source: True) if live is None else (lambda source: not live.covers(source))
    found: list[dict] = []
    started = now()
    feeds = [f for f in sources["rss"] if due(store.get(conn, f"rss:{f['url']}", {}), started)]
    if tally is not None:
        tally.update(feeds_fetched=len(feeds), feeds_skipped=len(sources["rss"]) - len(feeds))
    with httpx.Client(headers=UA, timeout=30) as http:
        def feed_job(f):
            items = rss(conn, http, f)
            # Seen under each reader's name for this feed.
            return items + [{**it, "source": f"rss:{name}"} for name in sorted(f["names"] - {f["name"]}) for it in items]
        jobs = [(f"rss {f['name']}", lambda f=f: feed_job(f)) for f in feeds]
        jobs += [(f"bluesky #{t}", lambda t=t: bluesky(conn, http, t)) for t in sorted(sources["bluesky_tags"])
                 if polled(f"bluesky:#{t}")]
        jobs += [(f"bluesky @{h}", lambda h=h: bluesky_person(conn, http, h)) for h in sorted(sources["bluesky_people"])]
        for name, job in jobs:
            try:
                found += job()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("%s: %s", name, e)
    found += instagram.collect(conn, sources["instagram_people"])
    # Hashtags and people, read with the owner's account.
    owner = people.mastodon(conn, OWNER)
    tags = [t for t in sorted(sources["mastodon_tags"]) if polled(f"mastodon:#{t}")]
    if owner and (tags or sources["mastodon_accounts"]):
        with mastodon_client(owner[0], owner[1]) as http:
            roots: dict = {}
            for t in tags:
                if live is not None:
                    log.info("mastodon #%s: no stream up, polled instead", t)
                try:
                    found += mastodon(conn, http, f"/api/v1/timelines/tag/{t}", f"mastodon:#{t}", roots)
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("mastodon #%s: %s", t, e)
            for a in sorted(sources["mastodon_accounts"]):
                try:
                    found += mastodon_account(conn, http, a, roots)
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    log.warning("mastodon @%s: %s", a, e)
    # Each connected reader's own home timeline.
    for p in profiles:
        creds = people.mastodon(conn, p.user)
        source = people.home_source(p.user)
        if not creds or not p.sources["mastodon_home"] or not polled(source):
            continue
        if live is not None:
            log.info("home timeline for %s: no stream up, polled instead", p.user)
        with mastodon_client(creds[0], creds[1]) as http:
            try:
                found += mastodon(conn, http, "/api/v1/timelines/home", source, {}, home=True)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("home timeline for %s: %s", p.user, e)
    conn.commit()
    return found
