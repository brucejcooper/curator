"""Instagram: every post from a handful of people, in order (no ads, no
algorithm). Instagram shows nothing to the logged out, so this reads as a
throwaway account (INSTAGRAM_USER, INSTAGRAM_PASSWORD), and gently, since
Instagram flags accounts that look automated: one person per cycle, each at
most every few hours. Its picture links expire, so pictures are saved here and
served from /media.

When it can't read (a failed login, Instagram asking "was this you?"), it says
so in the app (`problem`) rather than going quiet."""

import logging
import os
from datetime import datetime, timedelta, timezone
from itertools import islice

import httpx

from curator import store
from curator.canon import short_title
from curator.config import DATA, PUBLIC_URL

log = logging.getLogger(__name__)
MEDIA = DATA / "media"
PICTURES = MEDIA / "instagram"
SESSION = DATA / "instagram-session"
FETCH_EVERY = timedelta(hours=3)
RETRY_LOGIN = timedelta(hours=6)  # repeated failed logins get an account locked
BACK_OFF = timedelta(hours=1)  # after Instagram refuses ("Please wait a few minutes")
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


def loader(conn):
    import instaloader

    user = os.environ.get("INSTAGRAM_USER")
    if not user:
        problem(conn, "INSTAGRAM_USER isn't set, so Instagram can't be read")
        return None
    L = instaloader.Instaloader(quiet=True, download_pictures=False, download_videos=False,
                                download_video_thumbnails=False, save_metadata=False,
                                max_connection_attempts=1, request_timeout=30)
    if SESSION.exists():
        L.load_session_from_file(user, str(SESSION))
        return L
    failed = store.get(conn, "instagram_login_failed", "")
    if failed and failed > iso(now() - RETRY_LOGIN):
        return None
    from curator.secrets import get_secret

    try:
        L.login(user, get_secret("INSTAGRAM_PASSWORD"))
    except (instaloader.exceptions.InstaloaderException, RuntimeError) as e:
        store.put(conn, "instagram_login_failed", iso(now()))
        problem(conn, f"Couldn't log in to Instagram as {user} (trying again in a few hours): {e}")
        return None
    store.put(conn, "instagram_login_failed", "")
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    L.save_session_to_file(str(SESSION))
    log.info("instagram: logged in as %s", user)
    return L


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


def from_post(post, handle: str, poster: dict, http: httpx.Client) -> dict:
    from curator.collect import item

    caption = (post.caption or "").strip()
    it = item(f"https://www.instagram.com/p/{post.shortcode}/", short_title(caption) if caption else f"{poster['name'] or handle} on Instagram",
              caption, f"instagram:@{handle}", f"@{handle}", post.date_utc.replace(tzinfo=timezone.utc), always_show=True)
    return {**it, "image": save(http, post.url, post.shortcode), "quote": None, "poster": poster}


def person(conn, L, http: httpx.Client, handle: str) -> list[dict]:
    """A person's posts since the last look (pinned ones too, if they're new)."""
    import instaloader

    key = f"instagram:@{handle}"
    since = store.get(conn, key, "") or iso(now() - FIRST_LOOK)
    profile = instaloader.Profile.from_username(L.context, handle)
    poster = {"name": profile.full_name or "", "avatar": save(http, profile.profile_pic_url, f"@{handle}", refresh=True) or ""}
    posts = [p for p in islice(profile.get_posts(), PER_FETCH) if iso(p.date_utc.replace(tzinfo=timezone.utc)) > since]
    found = [from_post(p, handle, poster, http) for p in posts]
    if found:
        store.put(conn, key, max(it["published"] for it in found))
    return found


def collect(conn, handles: set[str]) -> list[dict]:
    """New posts from whoever is most overdue for a look."""
    import instaloader

    handle = due(conn, handles) if handles else None
    if not handle or store.get(conn, "instagram_paused_until", "") > iso(now()):
        return []
    L = loader(conn)
    if not L:
        return []
    try:
        with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as http:
            found = person(conn, L, http, handle)
    except instaloader.exceptions.ProfileNotExistsException:
        log.warning("instagram @%s: no such account", handle)
        found = []
    except (instaloader.exceptions.LoginRequiredException, instaloader.exceptions.LoginException) as e:
        SESSION.unlink(missing_ok=True)  # log in afresh next time
        problem(conn, f"Instagram logged us out: {e}")
        return []
    except instaloader.exceptions.InstaloaderException as e:
        store.put(conn, "instagram_paused_until", iso(now() + BACK_OFF))
        problem(conn, f"Couldn't read @{handle} (trying again in an hour): {e}")
        return []
    store.put(conn, f"instagram_fetched:@{handle}", iso(now()))
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
