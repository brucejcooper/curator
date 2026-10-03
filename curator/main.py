"""The curator service: the web server, the 15-minute fast loop, and the daily
slow loop, in one process."""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn

from curator import pipeline, store
from curator.server import app

log = logging.getLogger("curator")
EVERY = timedelta(minutes=int(os.environ.get("CURATOR_CYCLE_MINUTES", "15")))
DAILY_AT = os.environ.get("CURATOR_DAILY_AT", "04:30")  # local time (TZ)


def next_daily(now: datetime) -> datetime:
    hour, minute = map(int, DAILY_AT.split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def rescore(users: set[str]) -> None:
    """After readers' votes, profile edits or decider switches: retrain their
    local models and re-publish their feeds. A new reader's first profile
    also needs their own questions asked about what's in the window."""
    conn = store.connect()
    try:
        for user in users:
            p = pipeline.people.profile(conn, user)
            if p:
                from curator.judge import judge_jev
                from curator.stories import WINDOW

                cutoff = (datetime.now(ZoneInfo("UTC")) - WINDOW).isoformat(timespec="seconds")
                recent = [r[0] for r in conn.execute("SELECT id FROM items WHERE rule IS NULL AND fetched >= ?", (cutoff,))]
                judge_jev(conn, p, recent)  # only asks about items not yet judged for them
                pipeline.rescore(conn, user)
    finally:
        conn.close()


async def loop() -> None:
    tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    await asyncio.to_thread(pipeline.startup)
    daily_at = next_daily(datetime.now(tz))
    next_cycle = datetime.now(tz)
    while True:
        now = datetime.now(tz)
        try:
            if now >= daily_at:
                daily_at = next_daily(now)
                await asyncio.to_thread(pipeline.daily)
            if now >= next_cycle:
                next_cycle = now + EVERY
                await asyncio.to_thread(pipeline.cycle)
            elif app.state.rescore:
                users, app.state.rescore = set(app.state.rescore), set()
                await asyncio.to_thread(rescore, users)
        except Exception:
            log.exception("loop")
        await asyncio.sleep(30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning")
    server = uvicorn.Server(config)

    async def run():
        task = asyncio.create_task(loop())
        await server.serve()
        task.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
