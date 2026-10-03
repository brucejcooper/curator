# Design

The curator is a personal news reader with an editor in it. It gathers more
than anyone wants to read, works out which pieces are the same story, and
picks what each reader would want, learning from what they actually read.

## Two loops

**The fast loop** runs every 15 minutes:

1. **Collect** from every reader's sources at once: RSS/Atom feeds, Mastodon
   and Bluesky hashtags (each fetched once, however many readers follow it),
   and each connected reader's own Mastodon home timeline. A reply stands for
   its conversation: the conversation's first post is what gets judged.
2. **Resolve the story.** Identical links (tracking parameters stripped) and
   near-identical titles become one item, with every place it was seen
   recorded. Items get a local embedding (`bge-small`, on the CPU); items from
   different sources within 72 hours that are close enough are one story.
   Pairs in the grey zone are put to Jev ("the same story?"). Each story's
   lead is its most informative version: a fuller summary, first to report,
   a source the readers trust, fresh.
3. **Judge**, cheapest first. Rules for everyone (not English, hashtag soup).
   Then the item's **traits**, asked once for everyone: how significant it
   is as news, whether it's a news report, rage bait, an advert, hype,
   personal chatter or a template how-to. Then, for each reader who follows
   one of its sources: their own rules, their own questions (relevance,
   topic) with their profile as context, and their local model.
4. **Publish** each reader's feed: the deciding classifier's stories that
   clear their bar, up to a daily ceiling (a slow day gives a short feed,
   not filler), plus a "maybe" lane.

**The slow loop** runs once a day: Claude revises each reader's profile from
their recent behaviour, saving a new version; each reader's local model is
retrained; history the agents' never-collect lists forbid is purged; the
database is backed up.

## Two classifiers

- **Jev** (hosted, TypeSafe AI) answers typed questions with calibrated
  probabilities. An item's score is the better of its relevance to the
  reader and its importance as news, plus a small bonus for news reports,
  discounted for rage bait, adverts, hype, chatter and template how-tos.
- **The local model** uses embedding similarity to the reader's profile and
  to items they liked or didn't, source trust and story features; a plain
  heuristic until the reader has enough signals, then logistic regression.
  Every verdict records which features drove it.

Both score everything. One decides (a per-reader setting); the other fills
the **maybe lane** with what it would have picked, plus a small random
sample, so votes reach each model's blind spots, not just what it chose.

**Signals**, strongest first: bookmarking, opening the article, 👍. A 👎
always wins. Only a reader's own signals train their model.

## Profiles

Each reader's profile is three documents: who they are and what they care
about (Markdown), what to collect for them and their rules (YAML), and their
own questions for Jev (YAML). It starts from a short interview, is revised
daily ("revise, don't rewrite": most days change little), and the reader can
edit it. Every version is kept, and the app shows what changed in each.

## The reader app

One timeline, oldest to newest from where you left off, merging your Mastodon
home timeline (filtered) with the curated articles. Mastodon ids are
milliseconds << 16 and curated stories get an id in the same space from when
they were first seen, so both sort together and one reading position covers
both. The position is kept on the server and only moves forward, so devices
can't undo each other's progress. The app never talks to Mastodon itself;
replying and boosting happen on your server ("See original").

## Privacy

- The Mac agent sends titles and URLs, never page contents, and drops
  anything on your never-collect list before sending. The list stays on your
  Mac; only its rules go to the curator, which purges matching history.
  Agent history is kept 90 days.
- Jev and Claude see item text and your profile, never raw history.
- Mastodon access is read-only, and tokens are encrypted at rest.
- Each reader's feed is at an address only they know.
