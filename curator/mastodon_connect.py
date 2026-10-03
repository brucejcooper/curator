"""Connecting a reader's Mastodon account, so the curator can read their home
timeline (and favourites, for their profile). Read-only: the app never posts.

1. The app asks for an authorize URL for the reader's server. The curator
   registers itself there once (an OAuth app per server) and signs a `state`
   saying who's connecting, to which server, until when.
2. The reader approves on their server, which redirects to the callback here.
3. The callback checks the state, swaps the code for a token, and stores it
   encrypted against the reader."""

import base64
import hashlib
import hmac
import json
import os
import re
import time

import httpx

from curator import people
from curator.config import PUBLIC_URL

SCOPES = "read"
CALLBACK = "/api/mastodon/callback"
STATE_TTL = 15 * 60


def redirect_uri() -> str:
    return PUBLIC_URL + CALLBACK


def normalise(server: str) -> str:
    s = (server or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0].lstrip("@")
    if "@" in s:  # someone typed their handle
        s = s.rsplit("@", 1)[1]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(:\d+)?", s):
        raise ValueError("That doesn't look like a Mastodon server name")
    return s


def _key() -> bytes:
    key = os.environ.get("CURATOR_SECRET_KEY")
    if not key:
        raise RuntimeError("CURATOR_SECRET_KEY is not set")
    return hashlib.sha256(b"mastodon-state:" + key.encode()).digest()


def sign_state(user: str, server: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": user, "s": server, "e": int(time.time()) + STATE_TTL}).encode()).decode()
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def check_state(state: str) -> tuple[str, str]:
    """(user, server) from a state this curator signed and that hasn't expired."""
    body, _, mac = (state or "").partition(".")
    if not hmac.compare_digest(mac, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
        raise ValueError("bad state")
    data = json.loads(base64.urlsafe_b64decode(body.encode()))
    if data["e"] < time.time():
        raise ValueError("the connection took too long; try again")
    return data["u"], data["s"]


def app_for(conn, server: str) -> tuple[str, str]:
    """(client id, secret) of the curator's OAuth app on `server`."""
    row = conn.execute("SELECT client_id, client_secret FROM mastodon_apps WHERE server = ?", (server,)).fetchone()
    if row:
        return row[0], row[1]
    resp = httpx.post(f"https://{server}/api/v1/apps", timeout=20, json={
        "client_name": "Curator", "redirect_uris": redirect_uri(), "scopes": SCOPES, "website": PUBLIC_URL})
    resp.raise_for_status()
    app = resp.json()
    conn.execute("INSERT OR REPLACE INTO mastodon_apps VALUES (?, ?, ?)", (server, app["client_id"], people.encrypt(app["client_secret"])))
    conn.commit()
    return app["client_id"], people.encrypt(app["client_secret"])


def authorize_url(conn, user: str, server: str) -> str:
    server = normalise(server)
    client_id, _ = app_for(conn, server)
    q = httpx.QueryParams({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri(),
                           "scope": SCOPES, "state": sign_state(user, server)})
    return f"https://{server}/oauth/authorize?{q}"


def finish(conn, code: str, state: str) -> str:
    """Completes the connection; returns the reader's id."""
    user, server = check_state(state)
    client_id, secret = app_for(conn, server)
    resp = httpx.post(f"https://{server}/oauth/token", timeout=20, json={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": people.decrypt(secret), "redirect_uri": redirect_uri(), "scope": SCOPES})
    resp.raise_for_status()
    token = resp.json()["access_token"]
    account = httpx.get(f"https://{server}/api/v1/accounts/verify_credentials", timeout=20,
                        headers={"Authorization": f"Bearer {token}"}).raise_for_status().json()
    people.set_mastodon(conn, user, server, token, account["id"])
    return user
