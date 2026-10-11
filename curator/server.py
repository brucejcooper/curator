"""HTTP. The reader app (web/) is served from here, and its API needs an
access token from your sign-in provider (OIDC): who you are decides whose timeline,
profile, votes, bookmarks and position you get. A reader's first visit
creates their record; until they've done the onboarding interview, the API
only answers `me` and `onboarding`. Separately: each reader's public feed at
an obscure path, the Mac agent's upload (each reader's own agent token), and
a status page that your reverse proxy should put behind single sign-on (admins)."""

import hmac
import html
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from curator import mastodon_connect, people, reader, store
from curator.auth import User, user_from_token
from curator.config import PUBLIC_URL
from curator.people import Profile
from curator.publish import feed_folder

WEB = Path(__file__).resolve().parent.parent / "web"
VERSION = os.environ.get("CURATOR_VERSION", "")  # the deployed commit; the app reloads when it changes


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


@dataclass
class Who:
    user: User
    record: dict
    profile: Profile | None

    @property
    def id(self) -> str:
        return self.user.id


def api(handler, needs_profile: bool = True):
    """A reader API endpoint: needs a valid SSO token; runs the (blocking)
    handler in a thread with a database connection and who's asking."""
    async def endpoint(request: Request) -> Response:
        user = await run_in_threadpool(user_from_token, bearer(request))
        if user is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = (await request.json() if await request.body() else {}) if request.method in ("POST", "PUT") else {}

        def work():
            conn = store.connect()
            try:
                record = people.ensure_user(conn, user.id, user.name)
                who = Who(user, record, people.profile(conn, user.id))
                if needs_profile and who.profile is None:
                    return JSONResponse({"error": "onboarding"}, status_code=409)
                return handler(conn, who, request, body)
            finally:
                conn.close()

        result = await run_in_threadpool(work)
        return result if isinstance(result, Response) else JSONResponse(result)
    return endpoint


def changed(request: Request, who: Who) -> None:
    """The reader's signals or profile changed: retrain and re-publish soon."""
    request.app.state.rescore.add(who.id)


# ── Reader API ───────────────────────────────────────────────────────────────

def me(conn, who: Who, request, body):
    r = who.record
    return {"user": who.id, "name": r["name"], "admin": bool(r["admin"]), "onboarded": who.profile is not None,
            "mastodon": r["mastodon_server"] if r["mastodon_token"] else None,
            "position": reader.position(conn, who.id),
            "feed_url": f"{PUBLIC_URL}/f/{r['feed_path']}/feed.json",
            "agent": bool(r["agent_token"]),
            "decider": people.decider(conn, who.id),
            "suggestions": conn.execute("SELECT COUNT(*) FROM suggestions WHERE user = ? AND status IN ('new', 'review')",
                                        (who.id,)).fetchone()[0]}


def timeline(conn, who: Who, request, body):
    q = request.query_params
    limit = max(1, min(int(q.get("limit", 40)), 80))
    if q.get("before"):
        return reader.before(conn, who.profile, q["before"], limit)
    return reader.after(conn, who.profile, q.get("after") or None, limit)


def thread(conn, who: Who, request, body):
    try:
        return reader.thread(conn, who.id, request.path_params["status_id"])
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


def position(conn, who: Who, request, body):
    """GET: where you're up to. PUT {position}: move it on (never back);
    PUT {latest: true}: everything read."""
    if request.method == "GET":
        return {"position": reader.position(conn, who.id)}
    if body.get("latest"):
        latest = reader.latest_id(conn, who.id)
        return {"position": reader.set_position(conn, who.id, latest) if latest else reader.position(conn, who.id)}
    return {"position": reader.set_position(conn, who.id, str(body.get("position", "")))}


def vote(conn, who: Who, request, body):
    """{item_id | status_id, vote: 1 | -1 | 0}. 0 clears a vote."""
    value = int(body.get("vote", 0))
    if value not in (-1, 0, 1):
        return JSONResponse({"error": "vote must be -1, 0 or 1"}, status_code=422)
    item = reader.resolve(conn, who.profile, body)
    if not item:
        return JSONResponse({"error": "unknown item"}, status_code=404)
    conn.execute("INSERT OR REPLACE INTO user_votes VALUES (?, ?, ?, ?)", (who.id, item, value, now()))
    conn.commit()
    changed(request, who)
    return {"ok": True}


def bookmark(conn, who: Who, request, body):
    """{item_id | status_id, on: true | false}. A bookmark is also a strong 👍."""
    item = reader.resolve(conn, who.profile, body)
    if not item:
        return JSONResponse({"error": "unknown item"}, status_code=404)
    if body.get("on", True):
        conn.execute("INSERT OR REPLACE INTO user_bookmarks VALUES (?, ?, ?)", (who.id, item, now()))
    else:
        conn.execute("DELETE FROM user_bookmarks WHERE user = ? AND item_id = ?", (who.id, item))
    conn.commit()
    changed(request, who)
    return {"ok": True}


def bookmarks(conn, who: Who, request, body):
    return reader.bookmarks(conn, who.id)


def search(conn, who: Who, request, body):
    """?q=words&everything=1: matching articles, best (and most recent) first.
    Without `everything`, only what the curator picked for you."""
    from curator.search import search as find

    q = request.query_params
    return {"items": find(conn, who.id, who.profile.sources["keys"], q.get("q", ""), everything=q.get("everything") == "1")}


def event(conn, who: Who, request, body):
    """{item_id | status_id, kind}. "open" (following the link) counts for more than a 👍."""
    item = str(body.get("item_id") or "") or (reader.resolve(conn, who.profile, body) or "")
    conn.execute("INSERT INTO events (item_id, kind, time, user) VALUES (?, ?, ?, ?)", (item, str(body.get("kind", ""))[:20], now(), who.id))
    conn.commit()
    if body.get("kind") == "open":
        changed(request, who)
    return {"ok": True}


# ── Profile: how the curator sees you ────────────────────────────────────────

def as_dict(p: Profile) -> dict:
    return {"version": p.version, "created": p.created, "author": p.author, "summary": p.summary,
            "profile_md": p.profile_md, "sources_yaml": p.sources_yaml, "questions_yaml": p.questions_yaml}


def profile(conn, who: Who, request, body):
    """GET: the current profile and the list of versions. PUT {profile_md,
    sources_yaml, questions_yaml, summary?}: save your own edit as a new version."""
    if request.method == "PUT":
        try:
            version = people.add_version(conn, who.id, "user", (body.get("summary") or "Edited by hand")[:200],
                                         str(body["profile_md"]), str(body["sources_yaml"]), str(body["questions_yaml"]))
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        changed(request, who)
        return {"version": version}
    return {"current": as_dict(who.profile), "versions": people.versions(conn, who.id)}


def profile_version(conn, who: Who, request, body):
    p = people.profile(conn, who.id, int(request.path_params["version"]))
    return as_dict(p) if p else JSONResponse({"error": "no such version"}, status_code=404)


def onboarding(conn, who: Who, request, body):
    """The interview's answers → the reader's first profile (Claude writes it)."""
    if who.profile is not None:
        return JSONResponse({"error": "already set up"}, status_code=409)
    from curator.profile_refresh import onboard

    answers = {k: str(v)[:2000] for k, v in body.items() if k in
               ("about", "where", "interests", "dislikes", "news", "sites", "hashtags", "volume")}
    version = onboard(conn, who.id, answers)
    changed(request, who)
    return {"version": version}


def settings(conn, who: Who, request, body):
    """PUT {decider: "jev" | "local"}: which classifier picks your feed."""
    if body.get("decider") in ("jev", "local"):
        people.set_setting(conn, who.id, "decider", body["decider"])
        changed(request, who)
    return {"decider": people.decider(conn, who.id)}


def agent_token(conn, who: Who, request, body):
    """A new token for your Mac agent (replacing any old one). Shown once."""
    return {"token": people.new_agent_token(conn, who.id)}


# ── Suggested sources ────────────────────────────────────────────────────────

def suggestions(conn, who: Who, request, body):
    from curator import scout

    return scout.listing(conn, who.id)


def decide_suggestion(conn, who: Who, request, body):
    """{action: add | try | dismiss (a suggestion) | keep | drop (after a trial)}"""
    from curator import scout

    try:
        result = scout.decide(conn, who.id, int(request.path_params["id"]), str(body.get("action", "")))
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    changed(request, who)
    return result


# ── Connecting Mastodon ──────────────────────────────────────────────────────

def mastodon_start(conn, who: Who, request, body):
    try:
        return {"url": mastodon_connect.authorize_url(conn, who.id, body.get("server", ""))}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:  # the server didn't answer like Mastodon
        return JSONResponse({"error": f"Couldn't reach that server ({e.__class__.__name__})"}, status_code=502)


def mastodon_stop(conn, who: Who, request, body):
    people.set_mastodon(conn, who.id, None, None, None)
    conn.execute("DELETE FROM home_posts WHERE user = ?", (who.id,))
    conn.commit()
    changed(request, who)
    return {"ok": True}


async def mastodon_callback(request: Request) -> Response:
    """Where the reader's Mastodon server sends them back after approving."""
    q = request.query_params

    def work():
        conn = store.connect()
        try:
            return mastodon_connect.finish(conn, q.get("code", ""), q.get("state", ""))
        finally:
            conn.close()

    if q.get("error"):
        return RedirectResponse("/?mastodon=declined", status_code=303)
    try:
        user = await run_in_threadpool(work)
    except Exception:
        return RedirectResponse("/?mastodon=failed", status_code=303)
    request.app.state.rescore.add(user)
    return RedirectResponse("/?mastodon=connected", status_code=303)


# ── Admin ────────────────────────────────────────────────────────────────────

def admin_users(conn, who: Who, request, body):
    if not who.record["admin"]:
        return JSONResponse({"error": "admins only"}, status_code=403)
    out = []
    for r in conn.execute("SELECT * FROM users ORDER BY created"):
        p = people.profile(conn, r["id"])
        out.append({
            "user": r["id"], "name": r["name"], "admin": bool(r["admin"]), "created": r["created"],
            "mastodon": r["mastodon_server"] if r["mastodon_token"] else None, "agent": bool(r["agent_token"]),
            "profile_version": p.version if p else None,
            "votes": conn.execute("SELECT COUNT(*) FROM user_votes WHERE user = ? AND vote != 0", (r["id"],)).fetchone()[0],
            "position": reader.position(conn, r["id"]),
        })
    return {"users": out}


# ── Feed, agent, status ──────────────────────────────────────────────────────

async def feed(request: Request) -> Response:
    conn = store.connect()
    try:
        user = people.user_for_feed_path(conn, request.path_params["secret"])
    finally:
        conn.close()
    name = request.path_params["name"]
    path = feed_folder(user) / name if user else None
    if not path or name not in ("feed.json", "feed.xml") or not path.exists():
        return Response(status_code=404)
    kind = "application/feed+json" if name == "feed.json" else "application/rss+xml"
    return FileResponse(path, media_type=kind, headers={"Cache-Control": "no-cache"})


def store_rules(conn, user: str, rules: dict) -> None:
    """The reader's never-collect domains and patterns, as their agent last sent them."""
    if not isinstance(rules, dict):
        return
    conn.execute("DELETE FROM never_collect WHERE user = ?", (user,))
    conn.executemany("INSERT OR IGNORE INTO never_collect VALUES (?, ?, ?)",
                     [(user, "domain", str(d)) for d in rules.get("domains", [])[:5000]]
                     + [(user, "pattern", str(p)) for p in rules.get("patterns", [])[:500]])


async def agent_events(request: Request) -> Response:
    """Batches from a reader's Mac agent, already filtered by their never-collect list."""
    body = await request.json()

    def work():
        conn = store.connect()
        try:
            user = people.user_for_agent_token(conn, bearer(request))
            if not user:
                return None
            events = body.get("events", [])
            conn.executemany(
                "INSERT INTO agent_events (source, time, host, body, user) VALUES (?, ?, ?, ?, ?)",
                [(e.get("source"), e.get("time"), urlsplit(e.get("url", "")).hostname, json.dumps(e), user) for e in events])
            if "rules" in body:
                store_rules(conn, user, body["rules"])
            conn.commit()
            return len(events)
        finally:
            conn.close()

    stored = await run_in_threadpool(work)
    return Response(status_code=401) if stored is None else JSONResponse({"ok": True, "stored": stored})


async def agent_rules(request: Request) -> Response:
    """The reader's agent sends its never-collect rules after `forget`; what
    they now forbid is purged straight away."""
    body = await request.json()

    def work():
        from curator_agent.never_collect import NeverCollect

        conn = store.connect()
        try:
            user = people.user_for_agent_token(conn, bearer(request))
            if not user:
                return None
            store_rules(conn, user, body.get("rules", {}))
            rules = NeverCollect.from_rules(body.get("rules", {}).get("domains", []), body.get("rules", {}).get("patterns", []))
            hosts = [h for (h,) in conn.execute("SELECT DISTINCT host FROM agent_events WHERE user = ? AND host IS NOT NULL", (user,))]
            doomed = [h for h in hosts if rules.blocks_host(h)]
            conn.executemany("DELETE FROM agent_events WHERE user = ? AND host = ?", [(user, h) for h in doomed])
            conn.commit()
            return len(doomed)
        finally:
            conn.close()

    purged = await run_in_threadpool(work)
    return Response(status_code=401) if purged is None else JSONResponse({"ok": True, "purged_hosts": purged})


async def status(request: Request) -> Response:
    conn = store.connect()
    try:
        last = store.get(conn, "last_cycle", {})
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        counts = conn.execute("SELECT COUNT(*), SUM(rule IS NOT NULL), SUM(traits IS NOT NULL) FROM items WHERE fetched >= ?",
                              (day_ago,)).fetchone()
        tokens = store.get(conn, f"jev_tokens:{date.today().isoformat()}", 0)
        per = []
        for r in conn.execute("SELECT id, name, mastodon_token IS NOT NULL AS masto FROM users ORDER BY created").fetchall():
            v = conn.execute("SELECT SUM(vote > 0), SUM(vote < 0) FROM user_votes WHERE user = ?", (r["id"],)).fetchone()
            j = conn.execute("""SELECT COUNT(*) FROM judgments j JOIN items i ON i.id = j.item_id
                                WHERE j.user = ? AND i.fetched >= ?""", (r["id"], day_ago)).fetchone()[0]
            d = people.decider(conn, r["id"])
            other = "local" if d == "jev" else "jev"
            per.append(f"""<tr><th>{html.escape(r['name'])}</th><td>{j} judged (24 h); votes {v[0] or 0} up, {v[1] or 0} down;
                Mastodon {'connected' if r['masto'] else 'not connected'}; deciding: <b>{d}</b>
                <form method="post" action="/status/decider" style="display:inline"><input type="hidden" name="user" value="{html.escape(r['id'])}">
                <input type="hidden" name="decider" value="{other}"><button>Switch to {other}</button></form></td></tr>""")
    finally:
        conn.close()
    rows = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in [
        ("Last cycle", json.dumps(last)),
        ("Items (24 h)", counts[0]), ("Dropped for everyone", counts[1] or 0), ("With traits", counts[2] or 0),
        ("Jev input tokens today", f"{tokens:,} (≈ ${tokens * 0.042 / 1e6:.3f})"),
    ])
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Curator status</title>
<style>body{{font:15px system-ui;margin:16px;max-width:900px}}th{{text-align:left;padding-right:1em;vertical-align:top}}td{{word-break:break-word}}</style>
<h1>Curator</h1><table>{rows}</table><h2>Readers</h2><table>{''.join(per)}</table>"""
    return HTMLResponse(page)


async def set_decider(request: Request) -> Response:
    form = await request.form()
    if form.get("decider") in ("jev", "local") and form.get("user"):
        conn = store.connect()
        try:
            people.set_setting(conn, form["user"], "decider", form["decider"])
        finally:
            conn.close()
        request.app.state.rescore.add(form["user"])
    return RedirectResponse("/status", status_code=303)


async def healthz(request: Request) -> Response:
    return Response("ok")


async def client_config(request: Request) -> Response:
    """What the reader app needs to sign in (public)."""
    from curator.config import OIDC_CLIENT_ID, OIDC_ISSUER, SIGN_IN_LABEL

    return JSONResponse({"authority": OIDC_ISSUER, "client_id": OIDC_CLIENT_ID, "sign_in": SIGN_IN_LABEL},
                        headers={"Cache-Control": "no-store"})


async def version(request: Request) -> Response:
    return JSONResponse({"version": VERSION or None, "built": os.environ.get("CURATOR_BUILT")} if VERSION else {},
                        status_code=200 if VERSION else 404, headers={"Cache-Control": "no-store"})


app = Starlette(
    routes=[
        Route("/api/me", api(me, needs_profile=False)),
        Route("/api/onboarding", api(onboarding, needs_profile=False), methods=["POST"]),
        Route("/api/timeline", api(timeline)),
        Route("/api/thread/{status_id:str}", api(thread)),
        Route("/api/position", api(position), methods=["GET", "PUT"]),
        Route("/api/vote", api(vote), methods=["POST"]),
        Route("/api/bookmark", api(bookmark), methods=["POST"]),
        Route("/api/bookmarks", api(bookmarks)),
        Route("/api/search", api(search)),
        Route("/api/event", api(event), methods=["POST"]),
        Route("/api/profile", api(profile), methods=["GET", "PUT"]),
        Route("/api/profile/{version:int}", api(profile_version)),
        Route("/api/settings", api(settings), methods=["PUT"]),
        Route("/api/suggestions", api(suggestions)),
        Route("/api/suggestions/{id:int}", api(decide_suggestion), methods=["POST"]),
        Route("/api/agent-token", api(agent_token, needs_profile=False), methods=["POST"]),
        Route("/api/mastodon", api(mastodon_start, needs_profile=False), methods=["POST"]),
        Route("/api/mastodon", api(mastodon_stop, needs_profile=False), methods=["DELETE"]),
        Route(mastodon_connect.CALLBACK, mastodon_callback),
        Route("/api/admin/users", api(admin_users, needs_profile=False)),
        Route("/f/{secret}/{name}", feed),
        Route("/agent/events", agent_events, methods=["POST"]),
        Route("/agent/rules", agent_rules, methods=["POST"]),
        Route("/status", status),
        Route("/status/decider", set_decider, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/version.json", version),
        Route("/config.json", client_config),
        Mount("/", StaticFiles(directory=WEB, html=True, check_dir=False)),
    ],
)
app.state.rescore = set()  # readers whose feed needs re-scoring and re-publishing
