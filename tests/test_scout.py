"""The weekly scout: candidates, feed discovery, trying them on paper,
suggesting, and what the reader's decisions do."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import numpy as np
import yaml

from curator import people, scout


def now(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def add_item(db, iid, source, story, url=None, author="", title=None):
    url = url or f"https://{source.replace(':', '-').replace('#', '').replace('@', '')}.example/{iid}"
    db.execute("""INSERT INTO items (id, url, canonical, title, summary, source, author, published, fetched, story_id)
                  VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?)""", (iid, url, url, title or iid, source, author, now(), now(), story))
    db.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?, ?, ?)", (iid, source, url, now()))


def like(db, iid, user="alex"):
    db.execute("INSERT INTO user_votes VALUES (?, ?, 1, 't')", (user, iid))


def test_candidates_come_from_what_the_reader_liked(alex, db):
    # Two stories Alex liked were also covered by The Daily Planet, which Alex doesn't follow.
    for n in (1, 2):
        add_item(db, f"mine{n}", "mastodon:#astronomy", f"s{n}", url=f"https://news.example/a{n}")
        add_item(db, f"other{n}", "rss:Daily Planet", f"s{n}", url=f"https://planet.example/{n}")
        like(db, f"mine{n}")
    # Two liked posts by the same person, from a hashtag Alex follows.
    for n in (3, 4):
        add_item(db, f"post{n}", "mastodon:#astronomy", f"p{n}", author="@stargazer@space.example")
        like(db, f"post{n}")
    # Another reader follows the Daily Planet, so its feed address is known.
    people.ensure_user(db, "sam")
    from conftest import docs
    people.add_version(db, "sam", "import", "x", docs()[0],
                       docs()[1].replace("{name: Garden Blog,", "{name: Daily Planet, url: \"https://planet.example/rss\"}\n    - {name: Garden Blog,"), docs()[2])
    db.commit()
    found = {(c["kind"], c["value"]): c["why"] for c in scout.from_signals(db, alex)}
    assert ("rss", "https://planet.example/rss") in found and "covered 2 stories" in found[("rss", "https://planet.example/rss")]
    assert ("mastodon_account", "stargazer@space.example") in found
    assert ("site", "news.example") in found  # articles reached through posts


def test_a_site_s_feed_is_found_from_its_page():
    page = '<html><head><link rel="alternate" type="application/rss+xml" href="/feeds/all.xml"></head></html>'
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})))
    assert scout.find_feed(http, "blog.example") == "https://blog.example/feeds/all.xml"


def fake_ask(scores):
    async def ask(rows, questions, state_for):
        assert {"relevant", "importance"} <= set(questions)  # the reader's own questions, and the shared ones
        return [(r["id"], {"answers": {"relevant": {"score": scores[int(r["id"])]}}, "usage": {"input_tokens": 1}}) for r in rows]
    return ask


def sample(n, days=7):
    return [{"url": f"https://c.example/{i}", "title": f"c{i}", "summary": "", "source": "rss:C", "author": "",
             "published": now(days * i / max(n - 1, 1))} for i in range(n)]


def test_trying_a_candidate_on_paper(alex, db, monkeypatch):
    # 8 items over a week; 4 would make Alex's feed (relevant ≥ 3 of 4); one of those Alex already got elsewhere.
    monkeypatch.setattr(scout, "ask", fake_ask([4, 4, 3, 3, 1, 0, 1, 0]))
    vectors = np.eye(8, 384, dtype=np.float32)
    monkeypatch.setattr("curator.stories.embed", lambda texts: vectors[: len(texts)])
    known = vectors[[0]]  # the first item is a story Alex already has
    stats = scout.evaluate(db, alex, {"items": sample(8)}, known)
    assert stats["hit_rate"] == 0.5 and stats["novelty"] == 0.75
    assert stats["per_day"] == round(8 / 7, 1) and stats["samples"][0]["title"] in ("c1", "c2", "c3")
    assert scout.worth_suggesting(stats)
    assert not scout.worth_suggesting({**stats, "hit_rate": 0.4})  # some good bits isn't enough
    assert not scout.worth_suggesting({**stats, "judged": 3})
    # Posting rarely doesn't count against a source.
    assert scout.worth_suggesting({**stats, "per_day": 0.1, "useful_per_day": 0.04, "judged": 5})


def test_how_tos_and_adverts_never_count_as_hits(alex, db, monkeypatch):
    async def ask(rows, questions, state_for):
        # Every item is relevant, but six of eight are generic how-tos.
        return [(r["id"], {"answers": {"relevant": {"score": 4}, "generic_howto": {"noul": 0.9 if int(r["id"]) < 6 else 0.1}},
                           "usage": {"input_tokens": 1}}) for r in rows]
    monkeypatch.setattr(scout, "ask", ask)
    monkeypatch.setattr("curator.stories.embed", lambda texts: np.eye(len(texts), 384, dtype=np.float32))
    stats = scout.evaluate(db, alex, {"items": sample(8)}, np.zeros((0, 384), np.float32))
    assert stats["hit_rate"] == 0.25 and not scout.worth_suggesting(stats)


def test_only_real_articles_make_candidates(alex, db):
    assert scout.is_post("https://mastodon.social/@sam/113000000000000000")
    assert scout.is_post("https://bsky.app/profile/sam.bsky.social/post/3kabc")
    assert not scout.is_post("https://news.example/2026/10/a-real-story")
    day = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
    rows = []
    for d in range(6):
        rows.append(("browser", day(d), "blog.example", json.dumps({"url": f"https://blog.example/posts/a-long-article-title-{d}"}), "alex"))
        rows.append(("browser", day(d), "router.example", json.dumps({"url": "https://router.example/network/default/dashboard"}), "alex"))
    db.executemany("INSERT INTO agent_events (source, time, host, body, user) VALUES (?, ?, ?, ?, ?)", rows)
    assert [c["value"] for c in scout.from_browsing(db, alex)] == ["blog.example"]  # not the dashboard


def test_the_weekly_run_suggests_the_good_and_remembers_the_rest(alex, db, monkeypatch):
    cands = [{"kind": "site", "value": "good.example", "name": "good", "why": "w"},
             {"kind": "site", "value": "meh.example", "name": "meh", "why": "w"},
             {"kind": "site", "value": "sky.example", "name": "already followed", "why": "w"}]
    monkeypatch.setattr(scout, "from_signals", lambda conn, p: cands)
    monkeypatch.setattr(scout, "from_browsing", lambda conn, p: [])
    tried = []
    monkeypatch.setattr(scout, "resolve", lambda conn, http, oh, c: tried.append(c["value"]) or {"kind": "rss", "value": f"https://{c['value']}/rss", "name": c["name"], "items": []})
    good = {"per_day": 3.0, "hit_rate": 0.5, "novelty": 0.8, "useful_per_day": 1.2, "margin": 0.2, "judged": 10, "samples": [{"title": "t", "url": "u", "score": 0.9}]}
    monkeypatch.setattr(scout, "evaluate", lambda conn, p, found, known: good if "good" in found["value"] else {**good, "hit_rate": 0.05})
    ids = scout.scout(db, "alex", use_web=False)
    assert tried == ["good.example", "meh.example"]  # not one Alex already follows
    listing = scout.listing(db, "alex")
    assert [s["value"] for s in listing["open"]] == ["https://good.example/rss"] and len(ids) == 1
    # Next week neither is tried again (one is waiting on Alex, one tested badly).
    tried.clear()
    scout.scout(db, "alex", use_web=False)
    assert tried == []


def test_decisions_change_the_sources_and_trials_go_to_the_maybe_lane(alex, db):
    sid = lambda kind, value: db.execute("INSERT INTO suggestions (user, kind, value, name, why, evidence, score, status, created) "
                                         "VALUES ('alex', ?, ?, ?, 'why', '{}', 1, 'new', ?)", (kind, value, value, now())).lastrowid
    a, t, d = sid("rss", "https://good.example/rss"), sid("mastodon_account", "stargazer@space.example"), sid("bluesky_hashtag", "meh")
    assert scout.decide(db, "alex", a, "add") == {"status": "accepted"}
    assert scout.decide(db, "alex", t, "try") == {"status": "trial"}
    assert scout.decide(db, "alex", d, "dismiss") == {"status": "dismissed"}
    p = people.profile(db, "alex")
    learned = {e["value"]: e for e in yaml.safe_load(p.sources_yaml)["learned"]}
    assert set(learned) == {"https://good.example/rss", "stargazer@space.example"}
    assert learned["stargazer@space.example"]["trial_until"] == (date.today() + timedelta(days=7)).isoformat()
    assert "mastodon:@stargazer@space.example" in p.sources["keys"] and p.sources["trial"] == {"mastodon:@stargazer@space.example": "stargazer@space.example"}
    assert [v["summary"] for v in people.versions(db, "alex")][:2] == ["Trying stargazer@space.example", "Added https://good.example/rss"]
    # A trial source's stories come to the maybe lane, marked, whatever they score.
    from test_curator import add_story
    from curator.publish import select
    add_story(db, "trial-story", "mastodon:@stargazer@space.example", 0.2)
    lanes = {s["story"]: (s["lane"], s["story_trial"]) for s in select(db, people.profile(db, "alex"))}
    assert lanes["trial-story"] == ("maybe", "stargazer@space.example")
    # A week on: how it went, then keep it.
    db.execute("UPDATE suggestions SET status = 'trial' WHERE id = ?", (t,))
    db.commit()
    p = people.profile(db, "alex")
    people.add_version(db, "alex", "user", "trial over", p.profile_md,
                       people.edit_learned(p.sources_yaml, lambda l: [{**e, "trial_until": "2020-01-01"} if "trial_until" in e else e for e in l]),
                       p.questions_yaml)
    like(db, "trial-story")
    scout.review_trials(db, "alex")
    review = scout.listing(db, "alex")["open"][0]
    assert review["status"] == "review" and review["evidence"]["liked"] == 1
    assert scout.decide(db, "alex", review["id"], "keep") == {"status": "accepted"}
    kept = {e["value"]: e for e in yaml.safe_load(people.profile(db, "alex").sources_yaml)["learned"]}
    assert "trial_until" not in kept["stargazer@space.example"]
    assert people.profile(db, "alex").sources["trial"] == {}


def test_the_daily_revision_keeps_new_source_kinds_and_trials():
    from curator.profile_refresh import LearnedSource, render_sources

    entries = [LearnedSource(kind="mastodon_account", value="a@b.example", reason="r", added="2026-10-01", trial_until="2026-10-08"),
               LearnedSource(kind="rss", value="https://x/rss", name="X", reason="r", added="2026-10-01")]
    out = yaml.safe_load(render_sources("feed: {}\n", entries))["learned"]
    assert out[0]["trial_until"] == "2026-10-08" and out[1]["name"] == "X" and "trial_until" not in out[1]


def test_paywalled_sites_are_ruled_out_before_judging(alex, db, monkeypatch):
    from curator import articles

    assert articles.paywalled('<script type="application/ld+json">{"isAccessibleForFree": false}</script>')
    assert articles.paywalled('{"isAccessibleForFree":"False"}')
    assert not articles.paywalled('{"isAccessibleForFree": true}') and not articles.paywalled("<p>no markup</p>")
    cands = [{"kind": "site", "value": v, "name": v, "why": "w"} for v in ("walled.example", "open.example")]
    monkeypatch.setattr(scout, "from_signals", lambda conn, p: cands)
    monkeypatch.setattr(scout, "from_browsing", lambda conn, p: [])
    item = lambda site: {"url": f"https://{site}/2026/10/a-story", "title": "t", "summary": "", "published": now()}
    monkeypatch.setattr(scout, "resolve", lambda conn, http, oh, c: {"kind": "rss", "value": f"https://{c['value']}/rss",
                                                                     "name": c["name"], "items": [item(c["value"])] * 5})
    pages = []
    monkeypatch.setattr(articles, "page", lambda http, url: pages.append(url) or (
        '{"isAccessibleForFree": false}' if "walled" in url else '{"isAccessibleForFree": true}', url))
    judged = []
    good = {"per_day": 3.0, "hit_rate": 0.9, "novelty": 0.8, "useful_per_day": 2, "margin": 0.2, "judged": 10, "samples": [{"title": "t", "url": "u", "score": 0.9}]}
    monkeypatch.setattr(scout, "evaluate", lambda conn, p, found, known: judged.append(found["value"]) or good)
    scout.scout(db, "alex", use_web=False)
    assert judged == ["https://open.example/rss"], "the paywalled site isn't judged at all"
    assert len(pages) == 1 + scout.PAYWALL_CHECKS  # the walled site's first article rules it out; the open site's are all checked
    listing = scout.listing(db, "alex")
    assert [s["value"] for s in listing["open"]] == ["https://open.example/rss"]
    assert db.execute("SELECT why FROM suggestions WHERE value = 'https://walled.example/rss'").fetchone()[0] == "tested: paywalled"
    assert not listing["history"], "a site ruled out on paper isn't in the reader's history"
