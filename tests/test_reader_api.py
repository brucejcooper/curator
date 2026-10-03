"""The reader API: sign-in required, onboarding first, each reader's own
timeline (merged in id order), position, votes, profile and feed."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from conftest import docs
from curator import people, reader, server
from curator.auth import User
from curator.publish import feed_folder


def toot(sid, text="hello there", acct_id="other"):
    return {"id": sid, "created_at": "2026-10-02T00:00:00Z", "content": f"<p>{text}</p>", "account": {"id": acct_id, "acct": "x"},
            "mentions": [], "reblog": None, "visibility": "public", "language": "en", "url": f"https://x/{sid}"}


HOME = [toot("1000"), toot("1002"), toot("1004", acct_id="me"), toot("1006")]


def write_feed(user, items):
    folder = feed_folder(user)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "feed.json").write_text(json.dumps({"items": items}))
    reader._articles_cache.pop(user, None)


def art(item_id, sort_id, source="rss:Sky News Feed"):
    return {"id": item_id, "url": f"https://n/{item_id}", "title": item_id, "content_text": "", "_curator": {"sort_id": sort_id, "source": source}}


@pytest.fixture
def client(db, alex, monkeypatch):
    users = {"tok-alex": User("alex", "Alex"), "tok-sam": User("sam", "Sam")}
    monkeypatch.setattr(server, "user_from_token", lambda t: users.get(t))
    people.set_mastodon(db, "alex", "m.example", "masto-token", "me")
    pages = []

    def home_page(creds, params):
        pages.append(creds)
        posts = HOME
        if params.get("min_id"):
            posts = [s for s in posts if int(s["id"]) > int(params["min_id"])]
        if params.get("max_id"):
            posts = [s for s in posts if int(s["id"]) < int(params["max_id"])]
        return posts[: int(params.get("limit", 40))]

    monkeypatch.setattr(reader, "home_page", home_page)
    # 1002 misses the bar; the rest show. (Your own post, 1004, isn't even asked about.)
    monkeypatch.setattr(reader, "verdicts", lambda conn, p, ids: {i: {"show": i != "1002", "lane": "main", "item": f"item-{i}", "also": []} for i in ids})
    write_feed("alex", [art("art-a", "1001"), art("art-b", "1005"), art("art-home", "1003", "mastodon:home:alex"), art("art-new", "2000")])
    db.executemany("INSERT INTO items (id, url, canonical, title, summary, source, published, fetched) VALUES (?, '', '', '', '', '', '', '')",
                   [("art-a",), ("art-b",)])
    db.commit()
    c = TestClient(server.app)
    c.pages = pages
    return c


def auth(who="alex"):
    return {"Authorization": f"Bearer tok-{who}"}


def test_sign_in_required_and_onboarding_first(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer forged"}).status_code == 401
    me = client.get("/api/me", headers=auth()).json()
    assert me["user"] == "alex" and me["onboarded"] and me["mastodon"] == "m.example"
    # Sam has signed in but not done the interview: only `me` answers.
    assert client.get("/api/me", headers=auth("sam")).json()["onboarded"] is False
    assert client.get("/api/timeline", headers=auth("sam")).json() == {"error": "onboarding"}


def test_timeline_merges_by_id_and_filters(client):
    page = client.get("/api/timeline?after=999", headers=auth()).json()
    assert [e["id"] for e in page["items"]] == ["1000", "1001", "1004", "1005", "1006"]
    assert page["cursor"] == "1006" and page["end"] is False
    assert client.pages[0][1] == "masto-token", "Alex's own Mastodon token"
    # Caught up: articles newer than every post come at the end, once.
    tail = client.get("/api/timeline?after=1006", headers=auth()).json()
    assert [e["id"] for e in tail["items"]] == ["2000"] and tail["end"] is True and tail["cursor"] == "2000"
    assert client.get(f"/api/timeline?after={tail['cursor']}", headers=auth()).json()["items"] == []
    older = client.get("/api/timeline?before=1004", headers=auth()).json()
    assert [e["id"] for e in older["items"]] == ["1000", "1001"]


def test_without_mastodon_the_timeline_is_articles(client, db):
    people.set_mastodon(db, "alex", None, None, None)
    page = client.get("/api/timeline?after=0&limit=2", headers=auth()).json()
    assert [e["id"] for e in page["items"]] == ["1001", "1005"] and page["end"] is False
    rest = client.get(f"/api/timeline?after={page['cursor']}&limit=2", headers=auth()).json()
    assert [e["id"] for e in rest["items"]] == ["2000"] and rest["end"] is True


def test_position_only_moves_forward_and_is_per_reader(client, db):
    people.ensure_user(db, "sam", "Sam")
    people.add_version(db, "sam", "onboarding", "x", *docs())
    put = lambda body, who="alex": client.put("/api/position", json=body, headers=auth(who)).json()["position"]
    assert put({"position": "1004"}) == "1004"
    assert put({"position": "1002"}) == "1004"  # a device that read less can't move it back
    assert put({"position": "1001"}, "sam") == "1001"
    assert client.get("/api/me", headers=auth()).json()["position"] == "1004"


def test_votes_are_per_reader(client, db):
    people.ensure_user(db, "sam", "Sam")
    people.add_version(db, "sam", "onboarding", "x", *docs())
    write_feed("sam", [art("art-a", "1001")])
    assert client.post("/api/vote", json={"item_id": "art-a", "vote": 1}, headers=auth()).json() == {"ok": True}
    client.post("/api/vote", json={"item_id": "art-a", "vote": -1}, headers=auth("sam"))
    mine = {e["id"]: e.get("vote") for e in client.get("/api/timeline?after=999", headers=auth()).json()["items"]}
    assert mine["1001"] == 1
    assert client.post("/api/vote", json={"item_id": "nope", "vote": 1}, headers=auth()).status_code == 404


def test_profile_versions_and_hand_edits(client):
    p = client.get("/api/profile", headers=auth()).json()
    assert p["current"]["version"] == 1 and "Profile of Alex" in p["current"]["profile_md"]
    edited = {**p["current"], "profile_md": p["current"]["profile_md"] + "\n- Also: birds\n", "summary": "Added birds"}
    assert client.put("/api/profile", json=edited, headers=auth()).json() == {"version": 2}
    versions = client.get("/api/profile", headers=auth()).json()["versions"]
    assert [(v["version"], v["author"], v["summary"]) for v in versions] == [(2, "user", "Added birds"), (1, "import", "sample")]
    assert "birds" not in client.get("/api/profile/1", headers=auth()).json()["profile_md"]
    broken = {**edited, "questions_yaml": "relevant: [unclosed"}
    assert client.put("/api/profile", json=broken, headers=auth()).status_code == 422


def test_each_reader_has_their_own_feed_address(client, db):
    alex = people.user(db, "alex")
    assert client.get(f"/f/{alex['feed_path']}/feed.json").json()["items"][0]["id"] == "art-a"
    assert client.get("/f/guess/feed.json").status_code == 404


def test_agent_tokens_are_per_reader_and_rules_purge(client, db):
    token = client.post("/api/agent-token", headers=auth()).json()["token"]
    events = [{"source": "firefox", "time": "2026-10-02T00:00:00+00:00", "url": u, "title": "t"} for u in ("https://a.example/1", "https://bank.example/x")]
    assert client.post("/agent/events", json={"events": events}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/agent/events", json={"events": events, "rules": {"domains": [], "patterns": []}},
                       headers={"Authorization": f"Bearer {token}"}).json()["stored"] == 2
    assert db.execute("SELECT COUNT(*) FROM agent_events WHERE user = 'alex'").fetchone()[0] == 2
    purged = client.post("/agent/rules", json={"rules": {"domains": ["bank.example"], "patterns": []}},
                         headers={"Authorization": f"Bearer {token}"}).json()
    assert purged["purged_hosts"] == 1
    assert [r[0] for r in db.execute("SELECT host FROM agent_events")] == ["a.example"]


def test_admin_view_is_for_admins(client, db):
    assert client.get("/api/admin/users", headers=auth()).status_code == 403
    db.execute("UPDATE users SET admin = 1 WHERE id = 'alex'")
    db.commit()
    assert [u["user"] for u in client.get("/api/admin/users", headers=auth()).json()["users"]] == ["alex"]


def test_mastodon_connection_state_is_signed_and_expires(monkeypatch):
    from curator import mastodon_connect

    state = mastodon_connect.sign_state("alex", "m.example")
    assert mastodon_connect.check_state(state) == ("alex", "m.example")
    with pytest.raises(ValueError):
        mastodon_connect.check_state(state[:-1] + ("0" if state[-1] != "0" else "1"))
    monkeypatch.setattr(mastodon_connect, "STATE_TTL", -1)
    with pytest.raises(ValueError):
        mastodon_connect.check_state(mastodon_connect.sign_state("alex", "m.example"))
    assert mastodon_connect.normalise("@alex@Mastodon.Social") == "mastodon.social"
    assert mastodon_connect.normalise("https://m.example/home") == "m.example"


def test_the_app_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Sign in" in page.text
    assert client.get("/js/app.js").status_code == 200
    assert client.get("/status").status_code == 200  # Caddy puts this behind authentik
