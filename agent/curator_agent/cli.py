"""curator-agent: collects your browsing history and Claude Code prompts on
the Mac, filters them through the never-collect list, and keeps them in a
local outbox until they are sent to the curator.

  curator-agent collect          run once (launchd runs this every 15 min)
  curator-agent pause 2h         collect nothing from the next 2 hours
  curator-agent forget DOMAIN    add DOMAIN to the never-collect list and purge it
  curator-agent status           outbox counts, cursors and pauses
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from curator_agent import sources
from curator_agent.never_collect import NeverCollect, add_domain

HOME = Path(os.environ.get("CURATOR_AGENT_HOME", Path.home() / "Library/Application Support/curator-agent"))
STATE = HOME / "state.json"
OUTBOX = HOME / "outbox.sqlite"
LOG = Path.home() / "Library/Logs/curator-agent.log"
BACKFILL_DAYS = 365

log = logging.getLogger("curator-agent")


def load_state() -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state.setdefault("started", time.time())
    state.setdefault("cursors", {})
    state.setdefault("pauses", [])
    return state


def save_state(state: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def outbox() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OUTBOX)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY, source TEXT, time TEXT, body TEXT, host TEXT,
             sent INTEGER DEFAULT 0)"""
    )
    return conn


def paused(record: dict, pauses: list) -> bool:
    when = datetime.fromisoformat(record["time"].replace("Z", "+00:00")).timestamp()
    return any(start <= when < end for start, end in pauses)


def collect() -> None:
    state = load_state()
    rules = NeverCollect()
    since = state["started"] - BACKFILL_DAYS * 86400
    cursors = state["cursors"]
    gathered: list[dict] = []
    readers = {
        "firefox": lambda c: sources.firefox(c or {}, since),
        "safari": lambda c: sources.safari(c, since),
        "claude-code": lambda c: sources.claude_code(c or {}, since),
    }
    for name, read in readers.items():
        try:
            records, cursors[name] = read(cursors.get(name))
            gathered += records
        except PermissionError:
            log.warning("%s: no permission; grant Full Disk Access to %s", name, os.path.realpath(sys.executable))
        except FileNotFoundError:
            log.info("%s: not found, skipped", name)
        except sqlite3.Error as e:
            log.warning("%s: %s", name, e)

    kept, dropped = [], 0
    for r in gathered:
        if paused(r, state["pauses"]) or ("url" in r and rules.blocks(r["url"])):
            dropped += 1
        else:
            kept.append(r)

    with outbox() as conn:
        conn.executemany(
            "INSERT INTO events (source, time, body, host) VALUES (?, ?, ?, ?)",
            [(r["source"], r["time"], json.dumps(r), urlsplit(r.get("url", "")).hostname) for r in kept],
        )
    day = date.today().isoformat()
    state.setdefault("dropped", {})[day] = state.get("dropped", {}).get(day, 0) + dropped
    state["dropped"] = dict(sorted(state["dropped"].items())[-30:])
    state["pauses"] = [p for p in state["pauses"] if p[1] > time.time() - 7 * 86400]
    save_state(state)
    counts = {}
    for r in kept:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    log.info("kept %s; dropped %d", counts or "nothing", dropped)
    send()


def post(path: str, body: dict) -> httpx.Response | None:
    """To the curator, with your agent token (Settings → Mac agent in the
    reader app makes one). None when no curator is configured."""
    url = os.environ.get("CURATOR_URL")
    if not url:
        return None
    from curator.secrets import get_secret

    resp = httpx.post(f"{url.rstrip('/')}{path}", json=body,
                      headers={"Authorization": f"Bearer {get_secret('CURATOR_AGENT_TOKEN')}"}, timeout=60)
    resp.raise_for_status()
    return resp


def send() -> None:
    """Post unsent events to the curator, with your never-collect rules (so it
    can purge whatever they now forbid)."""
    if not os.environ.get("CURATOR_URL"):
        return
    rules = NeverCollect().rules()
    with outbox() as conn:
        while rows := conn.execute("SELECT id, body FROM events WHERE sent = 0 ORDER BY id LIMIT 500").fetchall():
            post("/agent/events", {"events": [json.loads(b) for _, b in rows], "rules": rules})
            conn.executemany("UPDATE events SET sent = 1 WHERE id = ?", [(i,) for i, _ in rows])
            conn.commit()
            log.info("sent %d events", len(rows))


def pause(duration: str) -> None:
    m = re.fullmatch(r"(\d+)\s*([mhd])", duration)
    if not m:
        raise SystemExit("duration like 30m, 2h or 1d")
    seconds = int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]
    state = load_state()
    now = time.time()
    state["pauses"].append([now, now + seconds])
    save_state(state)
    print(f"Paused for {duration}: nothing visited until {time.strftime('%H:%M %a', time.localtime(now + seconds))} will be collected.")


def forget(domain: str) -> None:
    domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    add_domain(domain)
    rules = NeverCollect()
    with outbox() as conn:
        hosts = [h for (h,) in conn.execute("SELECT DISTINCT host FROM events WHERE host IS NOT NULL")]
        doomed = [h for h in hosts if rules.blocks_host(h)]
        conn.executemany("DELETE FROM events WHERE host = ?", [(h,) for h in doomed])
    from curator_agent.never_collect import local_list

    print(f"Added {domain} to {local_list()} and purged it from the local outbox.")
    try:
        if post("/agent/rules", {"rules": rules.rules()}):
            print("The curator has purged it too.")
    except httpx.HTTPError as e:
        print(f"Couldn't reach the curator ({e}); it will purge it after the next upload.")


def status() -> None:
    state = load_state()
    with outbox() as conn:
        rows = conn.execute("SELECT source, sent, COUNT(*) FROM events GROUP BY source, sent").fetchall()
    for source, sent, n in rows:
        print(f"{source:14} {'sent' if sent else 'waiting':8} {n}")
    active = [p for p in state["pauses"] if p[1] > time.time()]
    if active:
        print("paused until", time.strftime("%H:%M %a", time.localtime(max(p[1] for p in active))))
    print("dropped per day:", state.get("dropped", {}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect")
    sub.add_parser("status")
    sub.add_parser("pause").add_argument("duration")
    sub.add_parser("forget").add_argument("domain")
    args = parser.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG if args.command == "collect" else None,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "collect":
        collect()
    elif args.command == "status":
        status()
    elif args.command == "pause":
        pause(args.duration)
    else:
        forget(args.domain)


if __name__ == "__main__":
    main()
