"""SQLite on the data volume. One connection per thread of work.

Shared by everyone: what was collected (items, sightings, stories) and what
is true of an item whoever reads it (its traits: importance, news, rage bait,
language...). Per reader: their profile (versioned), how the curator judged
each item for them, their votes, bookmarks, opens, position and feed."""

import json
import sqlite3

import numpy as np

from curator.config import DATA, DB, OWNER

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,          -- hash of the canonical URL
  url TEXT, canonical TEXT, title TEXT, summary TEXT,
  source TEXT,                  -- where first seen: "rss:ABC News Top Stories", "mastodon:#homelab", "mastodon:home:alex"
  author TEXT,
  published TEXT, fetched TEXT, -- ISO 8601 UTC
  story_id TEXT,
  embedding BLOB,
  always_show INTEGER DEFAULT 0,
  rule TEXT,                    -- a rule that drops it for everyone (language, hashtag soup)
  traits TEXT,                  -- Jev's answers to the reader-independent questions (traits.py)
  sample INTEGER DEFAULT 0,     -- picked for the maybe lane's random sample
  image TEXT                    -- picture URL; '' once looked for and none found
);
CREATE INDEX IF NOT EXISTS items_fetched ON items(fetched);
CREATE INDEX IF NOT EXISTS items_story ON items(story_id);
CREATE TABLE IF NOT EXISTS sightings (  -- every place an item was seen
  item_id TEXT, source TEXT, url TEXT, seen TEXT,
  PRIMARY KEY (item_id, source)
);
CREATE INDEX IF NOT EXISTS sightings_source ON sightings(source);
CREATE TABLE IF NOT EXISTS stories (id TEXT PRIMARY KEY, lead_id TEXT, first_seen TEXT, updated TEXT);

-- Readers: authentik usernames.
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT, created TEXT,
  admin INTEGER DEFAULT 0,
  feed_path TEXT,               -- the obscure part of their public feed's address
  agent_token TEXT,             -- sha256 of their Mac agent's token
  mastodon_server TEXT, mastodon_token TEXT, mastodon_account TEXT,  -- token encrypted (people.py)
  settings TEXT                 -- JSON: decider, ...
);
CREATE TABLE IF NOT EXISTS profile_versions (  -- the curator's model of each reader, every version kept
  user TEXT, version INTEGER, created TEXT,
  author TEXT,                  -- curator (daily refresh), onboarding, user (edited by hand), import
  summary TEXT,                 -- one line: what changed
  profile_md TEXT, sources_yaml TEXT, questions_yaml TEXT,
  PRIMARY KEY (user, version)
);
CREATE TABLE IF NOT EXISTS judgments (  -- how an item was judged for one reader
  user TEXT, item_id TEXT,
  rule TEXT,                    -- one of their own rules dropped it
  jev TEXT, jev_score REAL,     -- their questions' answers; 0..1 after traits
  local TEXT, local_score REAL, -- their local model's features and verdict
  PRIMARY KEY (user, item_id)
);
CREATE INDEX IF NOT EXISTS judgments_item ON judgments(item_id);
CREATE TABLE IF NOT EXISTS user_votes (user TEXT, item_id TEXT, vote INTEGER, time TEXT, PRIMARY KEY (user, item_id));
CREATE TABLE IF NOT EXISTS user_bookmarks (user TEXT, item_id TEXT, time TEXT, PRIMARY KEY (user, item_id));
CREATE TABLE IF NOT EXISTS positions (user TEXT PRIMARY KEY, position TEXT, updated TEXT);  -- read up to (a timeline id)
CREATE TABLE IF NOT EXISTS events (item_id TEXT, kind TEXT, time TEXT, user TEXT);
CREATE TABLE IF NOT EXISTS home_posts (  -- a post in someone's Mastodon home timeline → its item
  user TEXT, status_id TEXT, item_id TEXT, seen TEXT,
  PRIMARY KEY (user, status_id)
);
CREATE TABLE IF NOT EXISTS agent_events (id INTEGER PRIMARY KEY, source TEXT, time TEXT, host TEXT, body TEXT, user TEXT);
CREATE INDEX IF NOT EXISTS agent_events_time ON agent_events(time);
CREATE TABLE IF NOT EXISTS never_collect (user TEXT, kind TEXT, value TEXT, PRIMARY KEY (user, kind, value));  -- from each agent, for purging
CREATE TABLE IF NOT EXISTS mastodon_apps (server TEXT PRIMARY KEY, client_id TEXT, client_secret TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database from an earlier, single-reader version up to date.
    Everything that was per reader then belonged to the owner."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not tables:
        return
    if "items" in tables:
        cols = columns(conn, "items")
        if "image" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN image TEXT")
        if "traits" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN traits TEXT")
            if "jev" in cols:
                # Judgments moved to a per-reader table; the answers that
                # don't depend on the reader become the item's traits.
                conn.execute("""CREATE TABLE IF NOT EXISTS judgments (user TEXT, item_id TEXT, rule TEXT, jev TEXT,
                                jev_score REAL, local TEXT, local_score REAL, PRIMARY KEY (user, item_id))""")
                conn.execute("""INSERT OR IGNORE INTO judgments (user, item_id, jev, jev_score, local, local_score)
                                SELECT ?, id, jev, jev_score, local, local_score FROM items
                                WHERE jev IS NOT NULL OR local IS NOT NULL""", (OWNER,))
                conn.execute("UPDATE items SET traits = json_extract(jev, '$.answers') WHERE jev IS NOT NULL")
        # Your own home timeline is a per-reader source now.
        conn.execute("UPDATE items SET source = ? WHERE source = 'mastodon:home'", (f"mastodon:home:{OWNER}",))
    if "sightings" in tables:
        conn.execute("UPDATE OR IGNORE sightings SET source = ? WHERE source = 'mastodon:home'", (f"mastodon:home:{OWNER}",))
    if "events" in tables and "user" not in columns(conn, "events"):
        conn.execute("ALTER TABLE events ADD COLUMN user TEXT")
        conn.execute("UPDATE events SET user = ?", (OWNER,))
    if "agent_events" in tables and "user" not in columns(conn, "agent_events"):
        conn.execute("ALTER TABLE agent_events ADD COLUMN user TEXT")
        conn.execute("UPDATE agent_events SET user = ?", (OWNER,))
    if "home_posts" in tables and "user" not in columns(conn, "home_posts"):
        conn.execute("ALTER TABLE home_posts RENAME TO home_posts_old")
        conn.execute("CREATE TABLE home_posts (user TEXT, status_id TEXT, item_id TEXT, seen TEXT, PRIMARY KEY (user, status_id))")
        conn.execute("INSERT INTO home_posts SELECT ?, status_id, item_id, seen FROM home_posts_old", (OWNER,))
        conn.execute("DROP TABLE home_posts_old")
    # Before sign-in, votes and bookmarks were the owner's alone.
    if "votes" in tables:
        conn.execute("CREATE TABLE IF NOT EXISTS user_votes (user TEXT, item_id TEXT, vote INTEGER, time TEXT, PRIMARY KEY (user, item_id))")
        conn.execute("INSERT OR IGNORE INTO user_votes SELECT ?, item_id, vote, time FROM votes", (OWNER,))
        conn.execute("DROP TABLE votes")
    if "bookmarks" in tables:
        conn.execute("CREATE TABLE IF NOT EXISTS user_bookmarks (user TEXT, item_id TEXT, time TEXT, PRIMARY KEY (user, item_id))")
        conn.execute("INSERT OR IGNORE INTO user_bookmarks SELECT ?, item_id, time FROM bookmarks", (OWNER,))
        conn.execute("DROP TABLE bookmarks")
    conn.commit()


def get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


# How much each signal counts as a training label. An explicit vote is the
# baseline; opening the article is stronger (actually reading beats a button
# press); bookmarking stronger still. An explicit 👎 always wins.
SIGNAL_WEIGHTS = {"vote": 1.0, "open": 1.3, "bookmark": 1.5}


def labels(conn, user: str | None = None) -> dict[str, tuple[int, float]]:
    """item id → (+1/-1, weight) from a reader's votes, bookmarks and opens
    (everyone's when `user` is None, as for choosing a story's lead)."""
    who, args = ("AND user = ?", (user,)) if user else ("", ())
    out: dict[str, tuple[int, float]] = {}
    for (item,) in conn.execute(f"SELECT DISTINCT item_id FROM events WHERE kind = 'open' {who}", args):
        out[item] = (1, SIGNAL_WEIGHTS["open"])
    for (item,) in conn.execute(f"SELECT item_id FROM user_bookmarks WHERE 1 {who}", args):
        out[item] = (1, SIGNAL_WEIGHTS["bookmark"])
    for item, vote in conn.execute(f"SELECT item_id, vote FROM user_votes WHERE vote != 0 {who}", args):
        if vote < 0 or out.get(item, (0, 0))[1] < SIGNAL_WEIGHTS["vote"]:
            out[item] = (1 if vote > 0 else -1, SIGNAL_WEIGHTS["vote"])
    return out


def vec(blob: bytes | None) -> np.ndarray | None:
    return None if blob is None else np.frombuffer(blob, dtype=np.float32)
