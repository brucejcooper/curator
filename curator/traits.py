"""What's true of an item whoever reads it: how significant it is as news,
whether it's a news report, rage bait, an advert, paid-for (sponsored) content,
hype, personal chatter or a template how-to, and whether it's in English. Jev answers these once per item
(no reader's profile involved) and every reader's score uses them; each reader
then only needs their own relevance questions asked (judge.py)."""

import asyncio
import json
import logging
from datetime import date

import yaml

from curator import store
from curator.config import REGION
from curator.secrets import get_secret

log = logging.getLogger(__name__)

QUESTIONS = {
    "importance": {
        "type": "score",
        "instructions": (
            f"As news, how significant is `item` for a well-informed adult in {REGION} who follows current "
            "affairs: national and world politics, economy, science, health, environment and major events? "
            "Judge the story's significance, not whether it matches anyone's hobbies."
        ),
        "criteria": [
            "Not news, or trivial (gossip, sport results, a minor local incident)",
            "Minor news of little consequence beyond the people involved",
            "Notable news, worth a glance for someone keeping up with current affairs",
            "Significant news a well-informed person would want to know about",
            "Major news that people will be talking about (national or world events)",
        ],
    },
    "news": {
        "type": "noul",
        "instructions": ("Is `item` a news report: an account of a recent event or development (what happened, who "
                         "was involved, when), as a news outlet would publish it?"),
        "criteria": {"true": "Reports something that has just happened or been announced",
                     "false": "Opinion, tutorial, personal post, review, listicle or evergreen piece"},
    },
    "rage_bait": {"type": "noul", "instructions": "Is the headline of `item` written mainly to provoke outrage rather than inform?"},
    "advert": {"type": "noul", "instructions": "Is `item` mainly an advertisement, sponsored post, product listing, deal or shopping roundup?"},
    # Narrower than `advert` (which takes in deals and shopping roundups, which
    # are sometimes wanted): only paid-for pieces, and a confident yes drops them.
    "sponsored": {
        "type": "noul",
        "instructions": ("Is `item` paid or partner content: a sponsored post, advertorial or promotional piece paid "
                         "for by the company or product it features, including when it reads like a news article? "
                         "An author or byline such as 'Sponsored Post' or 'Partner content' means yes."),
        "criteria": {"true": "Written for, or paid for by, the company or product it promotes",
                     "false": ("Editorial coverage, even if positive, including reviews, deals posts and shopping "
                               "roundups by the outlet's own writers")},
    },
    "hype": {"type": "noul", "instructions": 'Is `item` mainly hype (crypto, "AI will replace X", funding-round news) with little substance?'},
    "chatter": {
        "type": "noul",
        "instructions": ("Is `item` mainly someone announcing what they are doing or did (a brew day, a harvest, "
                         "starting a project, a stream) rather than sharing something a reader could learn from or act on?"),
        "criteria": {"true": "A personal status update; the reader learns nothing new",
                     "false": "Shares a finding, result, technique, opinion with reasons, or news"},
    },
    "generic_howto": {
        "type": "noul",
        "instructions": ('Is `item` a generic, mass-produced how-to or tutorial (for example "How to install X on '
                         'Ubuntu VPS" or "Deploy Y in 5 minutes"), rather than someone\'s own write-up of a real problem they solved?'),
        "criteria": {"true": "Template tutorial or install guide, often one of a series",
                     "false": "An original write-up, or not a tutorial at all"},
    },
    "english": {
        "type": "noul",
        "instructions": "Is `item` written mainly in English? Ignore hashtags, URLs, product codes and proper names.",
        "criteria": {"true": "Mostly English prose or an English headline", "false": "Mostly another language"},
    },
}
JEV_NOT_ENGLISH = 0.3
JEV_SPONSORED = 0.9  # below this a `sponsored` yes only demotes (judge.PENALTIES)


def split_legacy_questions(text: str) -> str:
    """From a single-reader questions file, keep only the reader's own
    questions; the item-level ones now live here."""
    questions = yaml.safe_load(text) or {}
    own = {k: v for k, v in questions.items() if k not in QUESTIONS}
    return yaml.safe_dump(own, sort_keys=False, allow_unicode=True, width=100)


def item_state(row) -> dict:
    return {"title": row["title"], "summary": row["summary"][:800], "source": row["source"],
            "author": row["author"], "link": row["canonical"]}


async def ask(rows, questions: dict, state_for) -> list[tuple[str, dict | None]]:
    """Jev's answers to `questions` for each row, eight at a time."""
    from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

    sem = asyncio.Semaphore(8)
    async with AsyncTypeSafeClient(api_key=get_secret("JEV_API_KEY"), model="jev-latest") as client:
        async def one(row):
            async with sem:
                try:
                    resp = await client.system_one(state=state_for(row), questions=questions)
                except TypeSafeError as e:
                    log.warning("jev %s: %s", row["id"], e)
                    return row["id"], None
            return row["id"], resp.model_dump(mode="json")

        return await asyncio.gather(*(one(r) for r in rows))


def count_tokens(conn, results) -> None:
    tokens = sum(r["usage"]["input_tokens"] for _, r in results if r)
    day = f"jev_tokens:{date.today().isoformat()}"
    store.put(conn, day, store.get(conn, day, 0) + tokens)


def judge_traits(conn, ids: list[str]) -> None:
    """Asks the item-level questions for items that don't have answers yet.
    Items Jev says aren't in English, or are surely paid-for content, are
    dropped for everyone."""
    if not ids:
        return
    rows = conn.execute(
        f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))}) AND rule IS NULL AND traits IS NULL", ids).fetchall()
    if not rows:
        return
    results = asyncio.run(ask(rows, QUESTIONS, lambda r: {"item": item_state(r)}))
    for iid, resp in results:
        if resp is None:
            continue
        english = resp["answers"].get("english", {}).get("noul", 1.0)
        sponsored = resp["answers"].get("sponsored", {}).get("noul", 0.0)
        rule = (f"not English (Jev {english:.2f})" if english < JEV_NOT_ENGLISH else
                f"sponsored (Jev {sponsored:.2f})" if sponsored >= JEV_SPONSORED else None)
        conn.execute("UPDATE items SET traits = ?, rule = COALESCE(rule, ?) WHERE id = ?",
                     (json.dumps(resp["answers"]), rule, iid))
    count_tokens(conn, results)
    conn.commit()
    log.info("traits for %d items", len(rows))
