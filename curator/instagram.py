"""Instagram: every post from a handful of people, in order (no ads, no
algorithm). Instagram shows nothing to the logged out, so this reads as a
throwaway account (INSTAGRAM_USER, INSTAGRAM_PASSWORD), through the Android
app's private API (instagrapi: the website's endpoints, which instaloader
uses, refuse scripted logins outright), and gently, since
Instagram flags accounts that look automated: one person per cycle, each at
most every few hours. Its picture links expire, so pictures are saved here and
served from /media.

When it can't read (a failed login, Instagram asking "was this you?"), it says
so in the app (`problem`) rather than going quiet."""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from curator import store
from curator.canon import short_title
from curator.config import DATA, PUBLIC_URL
from curator.items import item

log = logging.getLogger(__name__)
MEDIA = DATA / "media"
PICTURES = MEDIA / "instagram"
SESSION = DATA / "instagram-settings.json"  # the app "device" and its login, kept between runs
FETCH_EVERY = timedelta(hours=3)
RETRY_LOGIN = timedelta(hours=6)  # repeated failed logins get an account locked
# After Instagram refuses ("Please wait a few minutes"): an hour, doubling with
# each refusal in a row up to a day, since every retry stretches the block.
BACK_OFF = timedelta(hours=1)
MAX_BACK_OFF = timedelta(days=1)
FIRST_LOOK = timedelta(days=14)  # how far back a newly added person is read
PER_FETCH = 12
PROBLEM = "instagram_problem"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def problem(conn, text: str | None) -> None:
    if text:
        log.warning("instagram: %s", text)
    if store.get(conn, PROBLEM) != text:
        store.put(conn, PROBLEM, text)
        conn.commit()


def due(conn, handles: set[str]) -> str | None:
    """The person most overdue for a look, if anyone is."""
    last = {h: store.get(conn, f"instagram_fetched:@{h}", "") for h in handles}
    cutoff = iso(now() - FETCH_EVERY)
    waiting = sorted((when, h) for h, when in last.items() if when < cutoff)
    return waiting[0][1] if waiting else None


def client(conn):
    """A logged-in client: the saved login, checked (and renewed if Instagram
    dropped it), else a fresh one. None if it can't log in."""
    from instagrapi import Client
    from instagrapi.exceptions import ClientError

    user = os.environ.get("INSTAGRAM_USER")
    if not user:
        problem(conn, "INSTAGRAM_USER isn't set, so Instagram can't be read")
        return None
    failed = store.get(conn, "instagram_login_failed", "")
    if failed and failed > iso(now() - RETRY_LOGIN):
        return None
    from curator.secrets import get_secret

    cl = Client()
    cl.delay_range = [2, 5]  # seconds between requests, like a person
    try:
        if SESSION.exists():
            cl.load_settings(SESSION)
        cl.login(user, get_secret("INSTAGRAM_PASSWORD"))
    except (ClientError, RuntimeError) as e:
        store.put(conn, "instagram_login_failed", iso(now()))
        problem(conn, f"Couldn't log in to Instagram as {user} (trying again in a few hours): {e}")
        return None
    store.put(conn, "instagram_login_failed", "")
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(SESSION)
    return cl


def save(http: httpx.Client, url: str | None, name: str, refresh: bool = False) -> str | None:
    """Saves a picture (once, unless `refresh`); its address here."""
    path = PICTURES / f"{name}.jpg"
    if url and (refresh or not path.exists()):
        try:
            resp = http.get(url)
            resp.raise_for_status()
            PICTURES.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        except httpx.HTTPError as e:
            log.warning("instagram picture %s: %s", name, e)
    return f"{PUBLIC_URL}/media/instagram/{name}.jpg" if path.exists() else None


def picture(media) -> str | None:
    """A post's picture: its own, or an album's first, or a video's still."""
    url = media.thumbnail_url or next((r.thumbnail_url for r in media.resources if r.thumbnail_url), None)
    return str(url) if url else None


def from_post(media, handle: str, poster: dict, http: httpx.Client) -> dict:
    caption = (media.caption_text or "").strip()
    it = item(f"https://www.instagram.com/p/{media.code}/", short_title(caption) if caption else f"{poster['name'] or handle} on Instagram",
              caption, f"instagram:@{handle}", f"@{handle}", media.taken_at, always_show=True)
    return {**it, "image": save(http, picture(media), media.code), "quote": None, "poster": poster}


class Private(Exception):
    """A private account the throwaway account doesn't follow (yet)."""


def person(conn, cl, http: httpx.Client, handle: str) -> list[dict]:
    """A person's posts since the last look (pinned ones too, if they're new)."""
    from instagrapi.exceptions import ClientError

    key = f"instagram:@{handle}"
    since = store.get(conn, key, "") or iso(now() - FIRST_LOOK)
    user = cl.user_info_by_username_v1(handle)
    try:
        medias = cl.user_medias_v1(user.pk, PER_FETCH)
    except ClientError:
        if user.is_private:  # Instagram says only "something went wrong"
            raise Private(handle)
        raise
    avatar = str(user.profile_pic_url) if user.profile_pic_url else None
    poster = {"name": user.full_name or "", "avatar": save(http, avatar, f"@{handle}", refresh=True) or ""}
    posts = [m for m in medias if iso(m.taken_at) > since]
    found = [from_post(m, handle, poster, http) for m in posts]
    if found:
        store.put(conn, key, max(it["published"] for it in found))
    return found


def collect(conn, handles: set[str]) -> list[dict]:
    """New posts from whoever is most overdue for a look."""
    from instagrapi.exceptions import ClientError, LoginRequired, UserNotFound

    handle = due(conn, handles) if handles else None
    if not handle or store.get(conn, "instagram_paused_until", "") > iso(now()):
        return []
    cl = client(conn)
    if not cl:
        return []
    try:
        with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as http:
            found = person(conn, cl, http, handle)
    except UserNotFound:
        log.warning("instagram @%s: no such account", handle)
        found = []
    except Private:
        # Not Instagram refusing: the others carry on. Looked at again in a few hours.
        store.put(conn, f"instagram_fetched:@{handle}", iso(now()))
        problem(conn, f"@{handle} is private: follow it from {os.environ.get('INSTAGRAM_USER')} to see its posts")
        return []
    except LoginRequired as e:
        problem(conn, f"Instagram logged us out (logging in again next time): {e}")
        return []
    except ClientError as e:
        refusals = store.get(conn, "instagram_refusals", 0) + 1
        wait = min(BACK_OFF * 2 ** (refusals - 1), MAX_BACK_OFF)
        store.put(conn, "instagram_refusals", refusals)
        store.put(conn, "instagram_paused_until", iso(now() + wait))
        problem(conn, f"Couldn't read @{handle} (trying again in {wait.total_seconds() / 3600:g} h): {e}")
        return []
    store.put(conn, f"instagram_fetched:@{handle}", iso(now()))
    store.put(conn, "instagram_refusals", 0)
    problem(conn, None)
    conn.commit()
    log.info("instagram @%s: %d new", handle, len(found))
    return found


def tidy(conn) -> int:
    """Deletes saved pictures no item uses any more (avatars stay)."""
    if not PICTURES.exists():
        return 0
    used = {r[0].rsplit("/", 1)[-1] for r in conn.execute("SELECT image FROM items WHERE image LIKE '%/media/instagram/%'")}
    gone = [p for p in PICTURES.glob("*.jpg") if not p.name.startswith("@") and p.name not in used]
    for p in gone:
        p.unlink()
    return len(gone)
