"""The fast loop (every 15 minutes) and the daily jobs, for every reader."""

import logging
import os
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

from curator import instagram, people, store
from curator.collect import collect_all
from curator.config import BACKUPS, DATA, DB
from curator.judge import apply_shared_rules, judge_for, judge_local
from curator.publish import publish
from curator.stories import ingest
from curator.traits import judge_traits

log = logging.getLogger(__name__)
KEEP_ITEMS = timedelta(days=365)  # a year of articles to search
KEEP_AGENT_EVENTS = timedelta(days=90)
KEEP_BACKUPS = 14
SCOUT_WEEKDAY = int(os.environ.get("CURATOR_SCOUT_WEEKDAY", "6"))  # Sunday: suggestions ready on Monday morning


def profiles(conn) -> list:
    return [p for u in people.readers(conn) if (p := people.profile(conn, u))]


def cycle() -> dict:
    """Collect once for everyone; judge what's new (shared questions once,
    each reader's own for them); publish each reader's feed."""
    started = time.monotonic()
    conn = store.connect()
    try:
        everyone = profiles(conn)
        found = collect_all(conn, everyone)
        new = ingest(conn, found)
        if new:
            apply_shared_rules(conn, new)
            judge_traits(conn, new)
        # Items seen anew in someone's source (new, or new to them) need judging for them.
        fresh = sorted({it_id for it_id in new} | recently_sighted(conn, started))
        published = {}
        for p in everyone:
            if fresh:
                judge_for(conn, p, fresh)
            published[p.user] = publish(conn, p)
        result = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "found": len(found), "new": len(new), "published": published,
            "seconds": round(time.monotonic() - started, 1),
        }
        store.put(conn, "last_cycle", result)
        conn.commit()
        log.info("cycle %s", result)
        return result
    finally:
        conn.close()


def recently_sighted(conn, since_monotonic: float) -> set[str]:
    """Items sighted during this cycle (an existing item can turn up in a new
    reader's source)."""
    started = datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - since_monotonic + 5)
    return {r[0] for r in conn.execute("SELECT DISTINCT item_id FROM sightings WHERE seen >= ?",
                                       (started.isoformat(timespec="seconds"),))}


def rescore(conn, user: str) -> None:
    """After a reader's votes or profile change: retrain their local model,
    re-publish their feed."""
    p = people.profile(conn, user)
    if p:
        judge_local(conn, p)
        publish(conn, p)


# ── Daily ────────────────────────────────────────────────────────────────────

def backup(conn) -> None:
    """A consistent copy of the database (VACUUM INTO), kept for two weeks,
    for PBS to back up as a plain file."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    target = BACKUPS / f"curator-{date.today().isoformat()}.sqlite"
    if not target.exists():
        conn.execute("VACUUM INTO ?", (str(target),))
    for old in sorted(BACKUPS.glob("curator-*.sqlite"))[:-KEEP_BACKUPS]:
        old.unlink()


def export_profiles(conn) -> None:
    """Optionally, readers' profiles as files in a private git repo (one folder
    each, a commit per day), for browsing their history as git. Off unless
    CURATOR_EXPORT_GIT_URL is set; only readers in CURATOR_EXPORT_USERS."""
    url = os.environ.get("CURATOR_EXPORT_GIT_URL")
    users = [u for u in os.environ.get("CURATOR_EXPORT_USERS", "").split(",") if u]
    if not url or not users:
        return
    repo = DATA / "export"
    token = os.environ.get("FORGEJO_TOKEN", "")
    authed = url.replace("https://", f"https://oauth2:{token}@") if token else url
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout
    if not (repo / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", authed, str(repo)], check=True)
        git("config", "user.name", "curator")
        git("config", "user.email", os.environ.get("CURATOR_GIT_EMAIL", "curator@localhost"))
    else:
        git("remote", "set-url", "origin", authed)
        git("pull", "--quiet", "--rebase")
    summaries = []
    for user in users:
        p = people.profile(conn, user)
        if not p:
            continue
        folder = repo / user
        folder.mkdir(exist_ok=True)
        (folder / "profile.md").write_text(p.profile_md)
        (folder / "sources.yaml").write_text(p.sources_yaml)
        (folder / "questions.yaml").write_text(p.questions_yaml)
        summaries.append(f"{user}: {p.summary}")
    if git("status", "--porcelain").strip():
        git("add", "-A")
        git("commit", "-m", f"Profiles {date.today().isoformat()}\n\n" + "\n".join(summaries))
        git("push", "--quiet")


def purge(conn) -> None:
    """Agent history each reader's never-collect list now forbids, and
    everything past its keeping time."""
    from curator_agent.never_collect import NeverCollect

    for (user,) in conn.execute("SELECT DISTINCT user FROM never_collect").fetchall():
        rows = conn.execute("SELECT kind, value FROM never_collect WHERE user = ?", (user,)).fetchall()
        rules = NeverCollect.from_rules([v for k, v in rows if k == "domain"], [v for k, v in rows if k == "pattern"])
        hosts = [h for (h,) in conn.execute("SELECT DISTINCT host FROM agent_events WHERE user = ? AND host IS NOT NULL", (user,))]
        conn.executemany("DELETE FROM agent_events WHERE user = ? AND host = ?", [(user, h) for h in hosts if rules.blocks_host(h)])
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM agent_events WHERE time < ?", ((now - KEEP_AGENT_EVENTS).isoformat(),))
    old = (now - KEEP_ITEMS).isoformat(timespec="seconds")
    conn.execute("""DELETE FROM items WHERE fetched < ? AND id NOT IN (SELECT item_id FROM user_votes)
                    AND id NOT IN (SELECT item_id FROM user_bookmarks)""", (old,))
    for table, column in (("sightings", "item_id"), ("judgments", "item_id")):
        conn.execute(f"DELETE FROM {table} WHERE {column} NOT IN (SELECT id FROM items)")
    conn.execute("DELETE FROM stories WHERE id NOT IN (SELECT story_id FROM items)")
    conn.execute("DELETE FROM feed_history WHERE story_id NOT IN (SELECT id FROM stories)")
    conn.commit()
    instagram.tidy(conn)


def daily() -> None:
    """Revise every reader's profile, re-score them with it, purge, back up,
    and export the profiles that are exported."""
    from curator.profile_refresh import refresh
    from curator.scout import review_trials, scout

    conn = store.connect()
    try:
        scouting = date.today().weekday() == SCOUT_WEEKDAY
        for user in people.readers(conn):
            try:
                log.info("profile %s: %s", user, refresh(conn, user))
            except Exception:
                log.exception("profile refresh for %s", user)
            rescore(conn, user)
            try:
                review_trials(conn, user)
                if scouting:
                    scout(conn, user)
            except Exception:
                log.exception("scout for %s", user)
        purge(conn)
        backup(conn)
        try:
            export_profiles(conn)
        except Exception:
            log.exception("profile export")
    finally:
        conn.close()


def startup() -> None:
    """Brings the database up to date and the owner in."""
    conn = store.connect()
    try:
        people.bootstrap(conn)
        if not store.get(conn, "feed_history_backfilled"):
            from curator.search import backfill

            for (user,) in conn.execute("SELECT id FROM users").fetchall():
                p = people.profile(conn, user)
                if p:
                    log.info("feed history for %s: %d stories", user, backfill(conn, p))
            store.put(conn, "feed_history_backfilled", True)
            conn.commit()
    finally:
        conn.close()
    log.info("database %s ready", DB)
