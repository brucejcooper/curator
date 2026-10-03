"""Read browsing history and Claude Code prompts since a cursor. Each reader
returns (records, new_cursor). Records carry titles and URLs, never page
contents."""

import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
FIREFOX = HOME / "Library/Application Support/Firefox/Profiles"
SAFARI = HOME / "Library/Safari/History.db"
CLAUDE_PROJECTS = HOME / ".claude/projects"
APPLE_EPOCH = 978307200  # 2001-01-01 in Unix seconds

# Firefox visit types worth keeping: link, typed, bookmark, framed link.
FIREFOX_TYPES = (1, 2, 3, 8)

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@contextmanager
def snapshot(db: Path):
    """Browsers lock their history while running: read a copy."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmp) / (db.name + suffix))
        conn = sqlite3.connect(Path(tmp) / db.name)
        try:
            yield conn
        finally:
            conn.close()


def firefox(cursor: dict, since: float) -> tuple[list[dict], dict]:
    """cursor: {profile name: last visit_date in microseconds}."""
    records, cursor = [], dict(cursor)
    for places in sorted(FIREFOX.glob("*/places.sqlite")):
        profile = places.parent.name
        last = cursor.get(profile, int(since * 1_000_000))
        with snapshot(places) as conn:
            rows = conn.execute(
                f"""SELECT v.visit_date, p.url, p.title FROM moz_historyvisits v
                    JOIN moz_places p ON p.id = v.place_id
                    WHERE v.visit_date > ? AND v.visit_type IN {FIREFOX_TYPES}
                    ORDER BY v.visit_date""",
                (last,),
            ).fetchall()
        for when, url, title in rows:
            records.append({"source": "firefox", "time": iso(when / 1_000_000), "url": url, "title": title or ""})
            last = when
        cursor[profile] = last
    return records, cursor


def safari(cursor: float | None, since: float) -> tuple[list[dict], float]:
    """Mac Safari history, which includes iPhone visits synced by iCloud.
    Needs Full Disk Access. cursor: last visit_time (seconds since 2001)."""
    last = cursor if cursor is not None else since - APPLE_EPOCH
    with snapshot(SAFARI) as conn:
        rows = conn.execute(
            """SELECT v.visit_time, i.url, v.title, v.origin FROM history_visits v
               JOIN history_items i ON i.id = v.history_item
               WHERE v.visit_time > ? AND v.load_successful = 1
                 AND v.redirect_destination IS NULL
               ORDER BY v.visit_time""",
            (last,),
        ).fetchall()
    records = []
    for when, url, title, origin in rows:
        device = "iphone" if origin == 1 else "mac"
        records.append({"source": f"safari-{device}", "time": iso(when + APPLE_EPOCH), "url": url, "title": title or ""})
        last = when
    return records, last


def prompt_text(message: dict) -> str | None:
    """Your own words from a Claude Code user message, or None for tool
    results, command output and injected context."""
    content = message.get("content")
    if isinstance(content, list):
        if any(b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("<"):
        return None
    return SECRET_RE.sub("[redacted]", text)[:2000]


def claude_code(cursor: dict, since: float) -> tuple[list[dict], dict]:
    """Your prompts from Claude Code sessions, not Claude's replies or tool
    output. cursor: {file path: byte offset read so far}."""
    records, cursor = [], dict(cursor)
    for log in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        key = str(log)
        offset = cursor.get(key, 0)
        size = log.stat().st_size
        if size <= offset:
            continue
        with log.open("rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a partly written last line for next time
        for line in data[:end].splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
                continue
            when = entry.get("timestamp", "")
            if not when or datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() < since:
                continue
            text = prompt_text(entry.get("message") or {})
            if text:
                project = Path(entry.get("cwd") or "").name
                records.append({"source": "claude-code", "time": when, "project": project, "text": text})
        cursor[key] = offset + end
    return records, cursor
