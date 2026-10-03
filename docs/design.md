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

## Finding new sources

Once a week (Sunday's daily run; `CURATOR_SCOUT_WEEKDAY`) a scout looks for
sources each reader doesn't have yet, from three places:

- **Their signals**: outlets that covered stories they liked (another
  reader's feed for that outlet, or the site's own feed, found by
  autodiscovery), sites whose articles they opened from posts, and people
  whose posts they liked more than once.
- **Their browsing**: sites where the Mac agent saw them read articles on
  at least four days of the last month (article pages only, so dashboards,
  sign-in pages and their own services don't count).
- **The web**: Claude (Sonnet, with web search) looks for news sites,
  aggregators, blogs and people around the profile's interests and region.

Each candidate is tried before it's suggested: its recent items (up to 20,
from the last month) go through the same Jev questions as the feed, which
says how many a day it would add, what share would clear the reader's bar,
and how many of those are stories they don't already get. The bar is
deliberately high: something is suggested only when it's genuinely good.
At least eight recent items judged, at least half of them would have made
the feed, and comfortably, on their own merits (an item that's a how-to,
an advert, hype, rage bait or chatter never counts), and at least one or
two good, new stories a week. At most three a week; most weeks there may be
none. Everything tried is remembered so it isn't tried again for twelve
weeks. Web finds are tried first; candidates without a readable feed don't
use up the week's tries.

Nothing joins a reader's sources without them. Each suggestion offers
**Add**, **Try for a week** (its stories come to the maybe lane, marked as
on trial) and **Not interested**. When a trial's week is up the reader sees
how it went (items, likes, opens, dislikes) and keeps or drops it. Decisions
edit the `learned:` part of their sources as a new profile version, which
the daily revision keeps.

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
