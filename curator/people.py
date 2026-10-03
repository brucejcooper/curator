"""Readers and the curator's model of each of them.

A reader is an authentik username. Their profile is three documents, kept as
numbered versions so you can see how the curator's picture of them changed:
`profile_md` (who they are, in prose), `sources_yaml` (what to collect for
them and their rules) and `questions_yaml` (what Jev asks about each item on
their behalf). The daily refresh, onboarding and hand edits each add a
version; nothing is overwritten."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

from curator.config import LEGACY_PROFILE, OWNER


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Secrets at rest ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("CURATOR_SECRET_KEY")
    if not key:
        raise RuntimeError("CURATOR_SECRET_KEY is not set (Fernet key for tokens at rest)")
    return Fernet(key.encode())


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── Readers ──────────────────────────────────────────────────────────────────

def ensure_user(conn, user_id: str, name: str = "") -> dict:
    """The reader's record, created on first sign-in."""
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users (id, name, created, admin, feed_path, settings) VALUES (?, ?, ?, ?, ?, '{}')",
                     (user_id, name or user_id, now(), int(user_id == OWNER), secrets.token_hex(24)))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    elif name and row["name"] != name:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        conn.commit()
    return dict(row)


def user(conn, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def readers(conn) -> list[str]:
    """Everyone with a profile: the people the curator works for."""
    return [r[0] for r in conn.execute("SELECT DISTINCT user FROM profile_versions ORDER BY user")]


def settings(conn, user_id: str) -> dict:
    row = conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
    return json.loads(row[0] or "{}") if row else {}


def set_setting(conn, user_id: str, key: str, value) -> None:
    s = settings(conn, user_id)
    s[key] = value
    conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(s), user_id))
    conn.commit()


def decider(conn, user_id: str) -> str:
    return settings(conn, user_id).get("decider", "jev")


def user_for_feed_path(conn, path: str) -> str | None:
    row = conn.execute("SELECT id FROM users WHERE feed_path = ?", (path,)).fetchone()
    return row[0] if row else None


def new_agent_token(conn, user_id: str) -> str:
    """A fresh token for the reader's Mac agent; only its hash is kept."""
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE users SET agent_token = ? WHERE id = ?", (sha(token), user_id))
    conn.commit()
    return token


def user_for_agent_token(conn, token: str) -> str | None:
    if not token:
        return None
    row = conn.execute("SELECT id FROM users WHERE agent_token = ?", (sha(token),)).fetchone()
    return row[0] if row else None


# ── Mastodon connections ─────────────────────────────────────────────────────

def set_mastodon(conn, user_id: str, server: str | None, token: str | None, account: str | None) -> None:
    conn.execute("UPDATE users SET mastodon_server = ?, mastodon_token = ?, mastodon_account = ? WHERE id = ?",
                 (server, encrypt(token) if token else None, account, user_id))
    conn.commit()


def mastodon(conn, user_id: str) -> tuple[str, str, str] | None:
    """(server, token, account id) for a reader who has connected Mastodon."""
    row = conn.execute("SELECT mastodon_server, mastodon_token, mastodon_account FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    return row[0], decrypt(row[1]), row[2]


def connected(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM users WHERE mastodon_token IS NOT NULL")]


# ── Profiles ─────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    user: str
    version: int
    created: str
    author: str
    summary: str
    profile_md: str
    sources_yaml: str
    questions_yaml: str

    @property
    def questions(self) -> dict:
        return yaml.safe_load(self.questions_yaml) or {}

    @property
    def sources(self) -> dict:
        return parse_sources(self.sources_yaml, self.user)


def home_source(user_id: str) -> str:
    return f"mastodon:home:{user_id}"


def source_key(kind: str, value: str, name: str | None = None) -> str:
    """The key items from a source are recorded under."""
    return {
        "rss": f"rss:{name or value}",
        "mastodon_hashtag": f"mastodon:#{value}",
        "bluesky_hashtag": f"bluesky:#{value}",
        "mastodon_account": f"mastodon:@{value.lstrip('@')}",
        "bluesky_account": f"bluesky:@{value.lstrip('@')}",
    }[kind]


def parse_sources(text: str, user_id: str) -> dict:
    """A reader's sources.yaml, as the collector and selection use it.
    `learned` entries (added by the daily job or accepted suggestions) may be
    on trial (`trial_until`): their items go to the maybe lane, marked."""
    sources = yaml.safe_load(text) or {}
    fixed, learned = sources.get("fixed") or {}, sources.get("learned") or []
    feed = sources.get("feed") or {}
    masto, bsky = fixed.get("mastodon") or {}, fixed.get("bluesky") or {}
    out = {
        "rss": list(fixed.get("rss") or []),
        "mastodon_home": masto.get("home_timeline", True),
        "mastodon_tags": set(masto.get("hashtags") or []),
        "mastodon_accounts": {a.lstrip("@") for a in masto.get("accounts") or []},
        "bluesky_tags": set(bsky.get("hashtags") or []),
        "bluesky_people": {h.lstrip("@") for h in bsky.get("people") or []},
        "rules": sources.get("rules") or {},
        "target_per_day": feed.get("target_per_day", 100),
        "min_score": {"jev": 0.7, "local": 0.6, **(feed.get("min_score") or {})},
        # A lower (or higher) bar for particular sources, on either classifier's scale.
        "source_floors": {
            **{f"rss:{f['name']}": f["min_score"] for f in fixed.get("rss") or [] if "min_score" in f},
            home_source(user_id): feed.get("home_min_score", 0.5),
        },
        "always": {f"rss:{f['name']}" for f in fixed.get("rss") or [] if f.get("always_show")},
        "trial": {},  # source key → name, for sources on trial
    }
    for entry in learned:
        kind, value = entry.get("kind"), str(entry.get("value", ""))
        if kind == "rss":
            out["rss"].append({"name": entry.get("name") or value, "url": value})
        elif kind == "mastodon_hashtag":
            out["mastodon_tags"].add(value)
        elif kind == "bluesky_hashtag":
            out["bluesky_tags"].add(value)
        elif kind == "mastodon_account":
            out["mastodon_accounts"].add(value.lstrip("@"))
        elif kind == "bluesky_account":
            out["bluesky_people"].add(value.lstrip("@"))
        else:
            continue
        if entry.get("trial_until"):
            out["trial"][source_key(kind, value, entry.get("name"))] = entry.get("name") or value
    # Every source key this reader reads from: an item is theirs if it was seen in one.
    out["keys"] = ({f"rss:{f['name']}" for f in out["rss"]} | {f"mastodon:#{t}" for t in out["mastodon_tags"]}
                   | {f"bluesky:#{t}" for t in out["bluesky_tags"]} | {f"mastodon:@{a}" for a in out["mastodon_accounts"]}
                   | {f"bluesky:@{h}" for h in out["bluesky_people"]} | {home_source(user_id)})
    return out


def edit_learned(sources_yaml: str, change) -> str:
    """sources.yaml with its `learned:` list changed by `change(list)`; the
    hand-kept part above it stays byte for byte."""
    head, sep, _ = sources_yaml.partition("\nlearned:")
    if not sep:
        raise ValueError("sources.yaml has no top-level 'learned:' section")
    learned = list((yaml.safe_load(sources_yaml) or {}).get("learned") or [])
    learned = change(learned)
    return head + "\n" + yaml.safe_dump({"learned": learned}, sort_keys=False, allow_unicode=True, width=100)


def profile(conn, user_id: str, version: int | None = None) -> Profile | None:
    """The reader's latest profile (or a given version)."""
    q = "SELECT * FROM profile_versions WHERE user = ?" + (" AND version = ?" if version else " ORDER BY version DESC LIMIT 1")
    row = conn.execute(q, (user_id, version) if version else (user_id,)).fetchone()
    return Profile(**dict(row)) if row else None


def versions(conn, user_id: str) -> list[dict]:
    rows = conn.execute("SELECT version, created, author, summary FROM profile_versions WHERE user = ? ORDER BY version DESC",
                        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def validate(sources_yaml: str, questions_yaml: str) -> None:
    """Raises ValueError if the documents won't work."""
    try:
        sources = yaml.safe_load(sources_yaml) or {}
        questions = yaml.safe_load(questions_yaml) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Not valid YAML: {e}") from e
    if not isinstance(sources, dict) or not isinstance(questions, dict):
        raise ValueError("Each document must be a YAML mapping")
    if "learned" not in sources:
        raise ValueError("sources.yaml needs a top-level 'learned:' list (it can be empty)")
    if "relevant" not in questions or questions["relevant"].get("type") != "score":
        raise ValueError("questions.yaml needs a 'relevant' score question")
    for qid, q in questions.items():
        if q.get("type") not in ("score", "choice", "noul"):
            raise ValueError(f"Question '{qid}' has an unknown type")


def add_version(conn, user_id: str, author: str, summary: str, profile_md: str, sources_yaml: str,
                questions_yaml: str, created: str | None = None) -> int:
    """Saves a new version (unless nothing changed) and returns its number."""
    validate(sources_yaml, questions_yaml)
    latest = profile(conn, user_id)
    if latest and (latest.profile_md, latest.sources_yaml, latest.questions_yaml) == (profile_md, sources_yaml, questions_yaml):
        return latest.version
    version = (latest.version if latest else 0) + 1
    conn.execute("INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (user_id, version, created or now(), author, summary, profile_md, sources_yaml, questions_yaml))
    conn.commit()
    return version


# ── Moving the owner in from the single-reader days ─────────────────────────

LEGACY_FILES = ("profile.md", "sources.yaml", "jev-questions.yaml")


def import_legacy(conn, user_id: str, folder: Path) -> int:
    """Imports profile files from a git checkout, one version per commit that
    changed them (so the history comes along), or just the files as they are."""
    if not (folder / "profile.md").exists():
        return 0
    from curator.traits import split_legacy_questions

    repo = folder.parent
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    log = git("log", "--reverse", "--format=%H%x09%cI%x09%s", "--", "profile")
    commits = [line.split("\t", 2) for line in log.stdout.splitlines()] if log.returncode == 0 else []
    added = 0
    for sha_, when, subject in commits:
        files = [git("show", f"{sha_}:profile/{name}") for name in LEGACY_FILES]
        if any(f.returncode for f in files):
            continue
        md, src, questions = (f.stdout for f in files)
        try:
            add_version(conn, user_id, "import", subject[:200], md, src, split_legacy_questions(questions), created=when)
            added += 1
        except ValueError:
            continue
    if not added:
        md, src, questions = ((folder / name).read_text() for name in LEGACY_FILES)
        add_version(conn, user_id, "import", "Imported from the profile files", md, src, split_legacy_questions(questions))
        added = 1
    return added


def bootstrap(conn) -> None:
    """Makes sure the owner exists, with everything they had before there
    were multiple readers: profile (and its git history), Mastodon token,
    agent token, feed address and decider."""
    from curator import store

    ensure_user(conn, OWNER)
    if not profile(conn, OWNER):
        import_legacy(conn, OWNER, LEGACY_PROFILE)
    row = user(conn, OWNER)
    if not row["mastodon_token"] and os.environ.get("MASTODON_TOKEN") and os.environ.get("CURATOR_SECRET_KEY"):
        from curator.config import MASTODON_SERVER

        set_mastodon(conn, OWNER, MASTODON_SERVER, os.environ["MASTODON_TOKEN"], None)
    if not row["agent_token"] and os.environ.get("CURATOR_AGENT_TOKEN"):
        conn.execute("UPDATE users SET agent_token = ? WHERE id = ?", (sha(os.environ["CURATOR_AGENT_TOKEN"]), OWNER))
    if os.environ.get("CURATOR_FEED_PATH") and not store.get(conn, "owner_feed_path_set"):
        conn.execute("UPDATE users SET feed_path = ? WHERE id = ?", (os.environ["CURATOR_FEED_PATH"], OWNER))
        store.put(conn, "owner_feed_path_set", True)
    old_decider = store.get(conn, "decider")
    if old_decider:
        set_setting(conn, OWNER, "decider", old_decider)
        conn.execute("DELETE FROM kv WHERE key = 'decider'")
    conn.commit()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")
