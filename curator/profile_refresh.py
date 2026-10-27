"""The slow loop: once a day Claude revises each reader's model, and on their
first visit writes it from a short interview.

The daily revision reads the reader's latest profile and their new
behaviour (their Mastodon activity, if connected) and saves a revised
profile as a new version: profile.md, the `learned:` part of sources.yaml,
and their own Jev questions. Item-level questions (importance, rage bait...)
are shared and fixed (traits.py), so they aren't the reader's to change.
"""

import argparse
import json
import os
import sys
from datetime import date
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel, Field

from curator import people, store
from curator.catalogue import CATALOGUE
from curator.mastodon import Mastodon
from curator.secrets import get_secret

MODEL = os.environ.get("CURATOR_PROFILE_MODEL", "claude-haiku-4-5")

QUESTIONS_HEADER = """\
# The reader's own questions, sent to Jev with each item. The daily job
# rewrites these as the reader's tastes become clearer. Every choice question
# keeps an "other/unsure" option because Jev answers confidently when the
# right option is missing. Jev's state is {item: {...}, reader: {profile:
# <profile.md>}}. Score levels are listed lowest first (0 to n-1).

"""


class LearnedSource(BaseModel):
    kind: Literal["rss", "mastodon_hashtag", "bluesky_hashtag", "mastodon_account", "bluesky_account"]
    value: str = Field(description="Feed URL, hashtag without the #, or account (user@server, or a Bluesky handle)")
    name: str | None = Field(default=None, description="A display name, if it has one; keep it as it was")
    reason: str = Field(description="Why this source, in a few words")
    added: str = Field(description="Date first added, YYYY-MM-DD; keep the original date for existing entries")
    trial_until: str | None = Field(default=None, description="Keep exactly as it was (the reader is trying this source out)")


class Option(BaseModel):
    key: str = Field(description="Short snake_case id")
    description: str


class JevQuestion(BaseModel):
    id: str = Field(description="Short snake_case id")
    type: Literal["noul", "choice", "score"]
    instructions: str
    options: list[Option] = Field(
        default_factory=list,
        description="choice: the options (include 'other'); score: 2-10 levels, lowest first (key is ignored); "
        "noul: optionally two options keyed 'true' and 'false' describing what yes and no mean",
    )


class Revision(BaseModel):
    profile_md: str = Field(description="The full revised profile.md")
    learned_sources: list[LearnedSource] = Field(description="The full learned list: kept, added; retired ones left out")
    jev_questions: list[JevQuestion] = Field(description="The full set of the reader's Jev questions")
    summary: str = Field(description="What changed, in under 60 characters")


def system_prompt(name: str) -> str:
    return f"""\
You maintain a "slow-moving model" of a reader, {name}, used to curate a \
personal news feed for them. Once a day you revise yesterday's profile in the \
light of new behaviour. Refer to them by name or as they/them.

Rules:
- Revise, don't rewrite. Keep what still holds; change only what the new \
evidence supports. Most days the change should be small. If there is no new \
evidence, return the profile unchanged apart from the date line.
- Update the "This week" section from the new activity. Promote a topic to \
"Live interests" only when it recurs; move live interests that have gone quiet \
for weeks to "Standing interests".
- Never invent facts about them, and never copy private details (names of \
private people, addresses, anything personal in a post) into the profile. It \
is a summary of interests, not a log.
- Keep "Push down or drop" unless the evidence clearly contradicts it.
- Sources: the fixed sources are maintained by hand and not yours to change. \
Propose learned sources (Mastodon/Bluesky hashtags, RSS feeds) only when the \
activity shows a sustained interest the fixed sources miss; give a reason; \
retire learned sources the activity no longer supports. Only propose RSS URLs \
you have seen in the activity; never guess a URL. Learned sources with \
`trial_until` are on trial at the reader's request: keep them exactly as they \
are. (A weekly scout suggests new sources separately; you needn't.)
- Jev questions: Jev is a classifier that reads instructions literally and \
answers with probabilities; it can't do arithmetic or dates. Each request's \
state is {{"item": {{title, summary, source, ...}}, "reader": {{"profile": \
<profile.md>}}}}, so questions refer to `item` and `reader.profile` rather \
than repeating the profile. These are only the reader's own questions: how \
significant an item is as news, and whether it's rage bait, an advert, \
sponsored, hype, chatter, a template how-to or not in English, are asked separately for \
everyone. The curator relies on `relevant` (a score, levels lowest first, \
about fit with the reader's interests, not news importance). Keep question ids \
stable unless there's a reason to change them. Every choice question must \
include an "other" option for "something else, or unsure". Keep the set small.
- Keep profile.md under about 700 words, in the same Markdown structure, with \
the heading "# Profile of {name}" followed by the date line.
"""


def split_sources(text: str) -> tuple[str, list]:
    """sources.yaml keeps its comments and fixed part byte-for-byte; only the
    trailing `learned:` section is regenerated."""
    head, sep, _ = text.partition("\nlearned:")
    if not sep:
        raise ValueError("sources.yaml has no top-level 'learned:' section")
    return head + "\n", yaml.safe_load(text).get("learned") or []


def render_sources(head: str, learned: list[LearnedSource]) -> str:
    entries = [{k: v for k, v in s.model_dump().items() if v is not None} for s in learned]
    return head + yaml.safe_dump({"learned": entries}, sort_keys=False, allow_unicode=True, width=100)


class _Dumper(yaml.SafeDumper):
    """Long strings as folded blocks, lists indented, so diffs stay readable."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


_Dumper.add_representer(
    str,
    lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style=">" if len(s) > 80 else None),
)


def render_questions(questions: list[JevQuestion]) -> str:
    out = {}
    for q in questions:
        entry: dict = {"type": q.type, "instructions": q.instructions}
        if q.type == "choice":
            options = {o.key: o.description for o in q.options}
            options.setdefault("other", "Something else, or unsure")
            entry["criteria"] = options
        elif q.type == "score":
            entry["criteria"] = [o.description for o in q.options]
        elif q.type == "noul" and {o.key for o in q.options} == {"true", "false"}:
            entry["criteria"] = {o.key: o.description for o in sorted(q.options, key=lambda o: o.key != "true")}
        out[q.id] = entry
    blocks = [yaml.dump({k: v}, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=80) for k, v in out.items()]
    return QUESTIONS_HEADER + "\n".join(blocks)


def claude(system: str, prompt: str, output):
    client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    response = client.messages.parse(model=MODEL, max_tokens=16000, system=system,
                                     messages=[{"role": "user", "content": prompt}], output_format=output)
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"Claude stopped with {response.stop_reason} (request {response._request_id})")
    u = response.usage
    print(f"{MODEL}: {u.input_tokens} in, {u.output_tokens} out (request {response._request_id})", file=sys.stderr)
    return response.parsed_output


# ── The daily revision ───────────────────────────────────────────────────────

def gather(conn, user: str, days: int, state: dict) -> dict:
    """The reader's new Mastodon activity (empty if they haven't connected)."""
    creds = people.mastodon(conn, user)
    if not creds:
        return {}
    activity = Mastodon(creds[0], creds[1]).activity(days, set(state["seen"]))
    previous = state["following"]
    if previous is not None:
        activity["new_follows"] = sorted(set(activity["following"]) - set(previous))
        activity["unfollows"] = sorted(set(previous) - set(activity["following"]))
    return activity


def refresh(conn, user: str, days: int = 1, dry_run: bool = False) -> str:
    """Revises the reader's profile from their latest activity; saves it as a
    new version (unless `dry_run`). Returns the one-line summary."""
    current = people.profile(conn, user)
    if current is None:
        return "no profile yet"
    state_key = f"refresh:{user}"
    state = store.get(conn, state_key, {"seen": [], "following": None})
    activity = gather(conn, user, days, state)
    name = (people.user(conn, user) or {}).get("name") or user
    docs = "\n\n".join(f'<file name="{n}">\n{t}</file>' for n, t in (
        ("profile.md", current.profile_md), ("sources.yaml", current.sources_yaml), ("questions.yaml", current.questions_yaml)))
    prompt = (f"Today is {date.today().isoformat()}.\n\nYesterday's profile:\n\n{docs}\n\n"
              f"{name}'s new Mastodon activity since the last revision (JSON):\n\n"
              f"<activity>\n{json.dumps(activity, ensure_ascii=False, indent=1)}\n</activity>\n\nReturn the revised profile.")
    revision = claude(system_prompt(name), prompt, Revision)
    head, _ = split_sources(current.sources_yaml)
    new = (revision.profile_md.rstrip() + "\n", render_sources(head, revision.learned_sources), render_questions(revision.jev_questions))
    if dry_run:
        return revision.summary
    people.add_version(conn, user, "curator", revision.summary, *new)
    if activity:
        seen = [s["id"] for s in activity["favourites"] + activity["bookmarks"]] + state["seen"]
        store.put(conn, state_key, {"seen": seen[:5000], "following": activity["following"]})
        conn.commit()
    return revision.summary


# ── Onboarding ───────────────────────────────────────────────────────────────

class Onboarding(BaseModel):
    profile_md: str = Field(description="profile.md for the new reader")
    feeds: list[str] = Field(description="Names of catalogue feeds to follow (exactly as listed)")
    own_feeds: list[str] = Field(default_factory=list, description="RSS/Atom URLs the reader gave themselves")
    mastodon_hashtags: list[str] = Field(default_factory=list, description="Hashtags without the #")
    bluesky_hashtags: list[str] = Field(default_factory=list, description="Hashtags without the #")
    topics: list[Option] = Field(description="5-9 topic areas for the reader, snake_case keys, plus 'other'")
    target_per_day: int = Field(description="Most items a day they'd want: 20, 50 or 100")


ONBOARDING_SYSTEM = """\
A new reader has answered a short interview so a curator can build them a \
personal news feed. Write their first profile and choose their sources.

profile.md: Markdown, under 500 words, with the heading "# Profile of <name>" \
then a date line, and the sections "## Who", "## This week" (say "Nothing yet"), \
"## Live interests (this month)", "## Standing interests", "## Push down or \
drop" and "## Volume". Use only what they told you; don't invent facts. Refer \
to them by name or as they/them.

Sources: pick from the catalogue the feeds that fit what they said (general \
news for where they live if they want news), plus any feed URLs they gave. \
Suggest a few Mastodon and Bluesky hashtags for their hobbies. Topics: the \
areas their interests fall into, for sorting items.
"""


def onboard(conn, user: str, answers: dict) -> int:
    """Writes a new reader's first profile from their interview answers."""
    name = (people.user(conn, user) or {}).get("name") or user
    catalogue = "\n".join(f"- {c['name']}: {c['about']}" for c in CATALOGUE)
    prompt = (f"Today is {date.today().isoformat()}. The reader is {name}.\n\nCatalogue of feeds:\n{catalogue}\n\n"
              f"Their answers (JSON):\n<answers>\n{json.dumps(answers, ensure_ascii=False, indent=1)}\n</answers>")
    o = claude(ONBOARDING_SYSTEM, prompt, Onboarding)
    by_name = {c["name"]: c for c in CATALOGUE}
    rss = [{"name": n, "url": by_name[n]["url"]} for n in o.feeds if n in by_name]
    rss += [{"name": url, "url": url} for url in o.own_feeds if url.startswith(("http://", "https://"))]
    sources = {
        "feed": {"target_per_day": o.target_per_day, "min_score": {"jev": 0.7, "local": 0.6}, "home_min_score": 0.5},
        "fixed": {"rss": rss, "mastodon": {"home_timeline": True, "hashtags": o.mastodon_hashtags},
                  "bluesky": {"hashtags": o.bluesky_hashtags}},
        "rules": {"blocked_domains": [], "drop_title_patterns": []},
    }
    sources_yaml = ("# What the curator collects for you. Edit `fixed` by hand; the daily job adds and\n"
                    "# retires entries under `learned`, with a reason and date on each.\n\n"
                    + yaml.safe_dump(sources, sort_keys=False, allow_unicode=True, width=100) + "\nlearned: []\n")
    topics = [t for t in o.topics if t.key != "other"]
    questions = [
        JevQuestion(id="relevant", type="score", instructions=(
            "How much would the reader described in `reader.profile` want to read `item`, given their interests and "
            "dislikes? (How important the news is in general is a separate question.)"), options=[
            Option(key="0", description="Clearly unwanted, a topic the reader dislikes, or nothing in it for them"),
            Option(key="1", description="Probably skip, only a weak link to the reader's interests"),
            Option(key="2", description="Might read, a loose fit with the reader's interests"),
            Option(key="3", description="Likely read, a solid fit with a live or standing interest"),
            Option(key="4", description="Must read, squarely on a live interest, substantive and new")]),
        JevQuestion(id="topic", type="choice", instructions="Which of the reader's interest areas is `item` mainly about?",
                    options=topics),
    ]
    return people.add_version(conn, user, "onboarding", "First profile, from the interview",
                              o.profile_md.rstrip() + "\n", sources_yaml, render_questions(questions))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("user")
    parser.add_argument("--days", type=int, default=1, help="window for posts and boosts (use ~30 for a first run)")
    parser.add_argument("--dry-run", action="store_true", help="show the summary without saving a version")
    args = parser.parse_args()
    conn = store.connect()
    print(refresh(conn, args.user, args.days, args.dry_run))


if __name__ == "__main__":
    main()
