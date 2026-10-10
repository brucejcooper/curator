"""Pick what goes in each reader's feed and write it as JSON Feed and RSS.

The decider's top stories fill the feed (up to `target_per_day` a day, only
those that clear the bar), plus anything from an "always show" source. The
"maybe" lane holds stories the other classifier would have picked, and a
small random sample of the rest, so votes reach each model's blind spots,
not just what it already chose."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from curator import people
from curator.config import FEEDS, PUBLIC_URL, SITE
from curator.people import Profile

FEED_DAYS = 3


def marks(n) -> str:
    return ",".join("?" * n)


def candidates(conn, user: str, keys: set[str], since: str) -> list[dict]:
    """One row per story the reader can see (it has an item from one of their
    sources): its lead, with each classifier's best score in the story."""
    keys = sorted(keys)
    rows = conn.execute(
        f"""SELECT s.id AS story, l.*,
                   MAX(j.jev_score) AS story_jev, MAX(j.local_score) AS story_local,
                   MAX(i.sample) AS story_sample, MIN(i.fetched) AS first_fetched
            FROM stories s
            JOIN items l ON l.id = s.lead_id
            JOIN items i ON i.story_id = s.id
            LEFT JOIN judgments j ON j.item_id = i.id AND j.user = ?
            WHERE i.rule IS NULL AND j.rule IS NULL
              AND i.id IN (SELECT item_id FROM sightings WHERE source IN ({marks(len(keys))}))
            GROUP BY s.id HAVING first_fetched >= ?""",
        (user, *keys, since)).fetchall()
    stories = [dict(r) for r in rows]
    own_leads(conn, stories, keys)
    # The lead's own verdicts, for "Why this?".
    leads = [s["id"] for s in stories]
    verdicts = {}
    for i in range(0, len(leads), 500):
        chunk = leads[i:i + 500]
        for r in conn.execute(f"SELECT item_id, jev, local FROM judgments WHERE user = ? AND item_id IN ({marks(len(chunk))})",
                              (user, *chunk)):
            verdicts[r["item_id"]] = (r["jev"], r["local"])
    for s in stories:
        s["jev"], s["local"] = verdicts.get(s["id"], (None, None))
    return stories


def own_leads(conn, stories: list[dict], keys: list[str]) -> None:
    """A story's lead is chosen for everyone. Where it isn't from one of this
    reader's sources (one they removed, or someone else's), they get the best
    version that is."""
    from curator.stories import best_version, source_reputation

    leads = [s["id"] for s in stories]
    theirs = set()
    for i in range(0, len(leads), 500):
        chunk = leads[i:i + 500]
        theirs |= {r[0] for r in conn.execute(
            f"SELECT item_id FROM sightings WHERE item_id IN ({marks(len(chunk))}) AND source IN ({marks(len(keys))})", (*chunk, *keys))}
    others = [s for s in stories if s["id"] not in theirs]
    if not others:
        return
    reputation = source_reputation(conn)
    for s in others:
        rows = conn.execute(
            f"""SELECT * FROM items WHERE story_id = ? AND rule IS NULL
                AND id IN (SELECT item_id FROM sightings WHERE source IN ({marks(len(keys))}))""", (s["story"], *keys)).fetchall()
        if rows:
            s.update(dict(best_version(rows, reputation)))


def margins(conn, user: str, stories: list[dict], floors: dict, source_floors: dict, always: set[str] = frozenset(),
            trial: dict | None = None) -> None:
    """Each story's best margin over the bar, per classifier: an item counts
    against its own source's bar where it has one (a news source with a lower
    bar, the reader's home timeline), else the classifier's. Sets
    story["margin_jev"/"margin_local"], and "story_always" when an item came
    from one of the reader's always-show sources, and "story_trial" (the
    source's name) when one came from a source they're trying out."""
    by_story: dict[str, list] = {}
    ids = [s["story"] for s in stories]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
            f"""SELECT i.story_id, sg.source, j.jev_score, j.local_score FROM items i
                JOIN sightings sg ON sg.item_id = i.id
                LEFT JOIN judgments j ON j.item_id = i.id AND j.user = ?
                WHERE i.rule IS NULL AND j.rule IS NULL AND i.story_id IN ({marks(len(chunk))})""",
            (user, *chunk)):
            by_story.setdefault(r["story_id"], []).append(r)
    for st in stories:
        rows = by_story.get(st["story"], [])
        st["story_always"] = int(any(r["source"] in always for r in rows))
        st["story_trial"] = next((trial[r["source"]] for r in rows if trial and r["source"] in trial), None)
        for kind in ("jev", "local"):
            best = None
            for r in rows:
                if r[f"{kind}_score"] is None:
                    continue
                m = r[f"{kind}_score"] - source_floors.get(r["source"], floors[kind])
                best = m if best is None else max(best, m)
            st[f"margin_{kind}"] = best


def select(conn, profile: Profile) -> list[dict]:
    """The reader's stories that clear the bar, best first, at most
    `target_per_day` a day; fewer when there isn't enough good material. The
    maybe lane (the other classifier's confident picks, plus the random
    sample) is capped at a third of the main lane's size."""
    src = profile.sources
    since = (datetime.now(timezone.utc) - timedelta(days=FEED_DAYS)).isoformat(timespec="seconds")
    stories = candidates(conn, profile.user, src["keys"], since)
    margins(conn, profile.user, stories, src["min_score"], src["source_floors"], src["always"], src["trial"])
    quota = src["target_per_day"] * FEED_DAYS
    main = people.decider(conn, profile.user)
    other = "local" if main == "jev" else "jev"

    def top(kind, limit):
        ranked = sorted((s for s in stories if s[f"margin_{kind}"] is not None and s[f"margin_{kind}"] >= 0),
                        key=lambda s: -(s[f"story_{kind}"] or 0))
        return [s["story"] for s in ranked[:limit]]

    chosen = set(top(main, quota))
    maybes = [sid for sid in top(other, quota) if sid not in chosen]
    maybes += [s["story"] for s in stories if s["story_sample"] and s["story"] not in chosen and s["story"] not in maybes]
    maybes = set(maybes[: max(3, len(chosen) // 3)])  # a third of what the main lane carries
    out = []
    for s in stories:
        if s["story_always"] or s["story"] in chosen:
            lane = "main"
        elif s["story"] in maybes or s["story_trial"]:  # a trial source's items all come to the maybe lane
            lane = "maybe"
        else:
            continue
        out.append({**s, "lane": lane, "decider": main})
    return sorted(out, key=lambda s: s["published"], reverse=True)


def reason(story: dict) -> str:
    parts = []
    if story.get("story_always"):
        parts.append("always shown")
    jev = json.loads(story["jev"]) if story.get("jev") else None
    if jev:
        rel = jev["answers"].get("relevant", {})
        topic = jev["answers"].get("topic", {})
        parts.append(f"Jev {story['story_jev'] or 0:.2f}: “{rel.get('legend', {}).get(str(round(rel.get('score', 0))), '?')}”")
        if topic.get("choice"):
            parts.append(f"topic {topic['choice']} ({topic['probabilities'][topic['choice']]:.0%})")
    local = json.loads(story["local"]) if story.get("local") else None
    if local:
        drivers = ", ".join(f"{k} {v:+.2f}" for k, v in local["drivers"].items())
        parts.append(f"local {story['story_local'] or 0:.2f} ({drivers})")
    return "; ".join(parts)


def also(conn, story: dict) -> list[dict]:
    """The story's other coverage: its other items, and everywhere else any
    of its items was seen (a hashtag post also in your home timeline)."""
    rows = conn.execute(
        """SELECT url, title, source FROM items WHERE story_id = ? AND id != ?
           UNION SELECT sg.url, NULL, sg.source FROM sightings sg JOIN items i ON i.id = sg.item_id
                 WHERE i.story_id = ? AND sg.source != i.source""",
        (story["story"], story["id"], story["story"]),
    ).fetchall()
    return [dict(r) for r in rows]


def sort_id(story: dict) -> str:
    """A Mastodon-style id (milliseconds << 16) from when the story was first
    seen, so readers that order by Mastodon id can slot it into a timeline.
    The low 16 bits come from the story id: stable, and unlikely to collide."""
    ms = int(datetime.fromisoformat(story["first_fetched"]).timestamp() * 1000)
    return str((ms << 16) | int(story["story"][:4], 16))


def json_feed(conn, stories: list[dict], title: str, feed_url: str) -> dict:
    items = []
    for s in stories:
        others = also(conn, s)
        items.append({
            "id": s["id"],
            "url": s["url"],
            "title": s["title"],
            "content_text": s["summary"],
            **({"image": s["image"]} if s.get("image") else {}),
            "date_published": s["published"],
            "authors": [{"name": s["author"] or s["source"]}],
            "tags": [s["lane"], s["source"]],
            "_curator": {
                "lane": s["lane"],
                "decider": s["decider"],
                "score": s["story_jev"] if s["decider"] == "jev" else s["story_local"],
                "jev": s["story_jev"],
                "local": s["story_local"],
                "reason": reason(s),
                "story": s["story"],
                "added": s["first_fetched"],
                "sort_id": sort_id(s),
                "source": s["source"],
                "trial": s.get("story_trial"),
                "also": others,
                **({"quote": json.loads(s["quote"])} if s.get("quote") else {}),
                **({"poster": json.loads(s["poster"])} if s.get("poster") else {}),
            },
        })
    return {"version": "https://jsonfeed.org/version/1.1", "title": title, "home_page_url": SITE,
            "feed_url": feed_url, "items": items}


def rss(feed: dict) -> bytes:
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = feed["title"]
    ET.SubElement(channel, "link").text = feed["home_page_url"]
    ET.SubElement(channel, "description").text = "AI-curated news and articles"
    for it in feed["items"]:
        e = ET.SubElement(channel, "item")
        prefix = "[maybe] " if it["_curator"]["lane"] == "maybe" else ""
        ET.SubElement(e, "title").text = prefix + it["title"]
        ET.SubElement(e, "link").text = it["url"]
        ET.SubElement(e, "guid", isPermaLink="false").text = it["id"]
        ET.SubElement(e, "pubDate").text = format_datetime(datetime.fromisoformat(it["date_published"]))
        body = it["content_text"]
        if it["_curator"]["also"]:
            body += "\n\nAlso covered by: " + ", ".join(a["source"] for a in it["_curator"]["also"])
        ET.SubElement(e, "description").text = f"{body}\n\nWhy: {it['_curator']['reason']}"
        for tag in it["tags"]:
            ET.SubElement(e, "category").text = tag
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def feed_folder(user: str):
    return FEEDS / user


def publish(conn, profile: Profile) -> int:
    """Writes the reader's feed; returns how many items it has."""
    from curator.images import fill_images

    reader = people.user(conn, profile.user) or {}
    stories = select(conn, profile)
    fill_images(conn, stories)
    feed_url = f"{PUBLIC_URL}/f/{reader.get('feed_path', '')}/feed.json"
    feed = json_feed(conn, stories, f"Curated for {reader.get('name') or profile.user}", feed_url)
    folder = feed_folder(profile.user)
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in (("feed.json", json.dumps(feed, ensure_ascii=False).encode()), ("feed.xml", rss(feed))):
        tmp = folder / f".{name}"
        tmp.write_bytes(body)
        tmp.replace(folder / name)
    return len(feed["items"])
