# Design

The curator is a personal news reader with an editor in it. It gathers more
than anyone wants to read, works out which pieces are the same story, and
picks what each reader would want, learning from what they actually read.

## Two loops

**The fast loop** runs every 15 minutes:

1. **Collect** from every reader's sources at once (each fetched once,
   however many readers follow it): RSS/Atom feeds, Instagram, and the
   Mastodon and Bluesky people readers follow. Mastodon and Bluesky
   hashtags and each connected reader's own Mastodon home timeline arrive
   in real time instead (see *Social, in real time* below); the loop polls a
   Mastodon timeline only while its stream has been down for more than
   5 minutes, so a broken stream is no worse than polling.
   A reply stands for
   its conversation: the conversation's first post is what gets judged.
   A quote post carries the text of the post it quotes.
   Each feed is fetched only when it's due, as often as it changes: about
   half the median gap between its last ten entries' dates, between one
   cycle and 4 hours (a feed without dates: twice as often after a new
   entry, half again as long after none). The publisher's `<ttl>`,
   `Cache-Control: max-age` and `Expires` are honoured as the soonest it's
   fetched again, as is `Retry-After` on a 429 or 503, up to a day. Errors
   back off, doubling with each in a row, up to a day. Requests stay
   conditional (ETag, Last-Modified). `/status` lists each feed's interval,
   next fetch, last answer and what set it; `last_cycle` counts the feeds
   fetched and skipped.
   A feed item that comes with no text of its own (Hacker News' is just
   links and points) or only a teaser ("… more…", "[…]") gets the
   article's first few paragraphs and picture, fetched once when it's new;
   a teaser's opening line is kept.
2. **Follow wrappers to the source.** A new item is often just a pointer:
   a post sharing a Hackaday article that is only an advert for someone's
   Mastodon post, which holds the real content. Each new item's page is
   read once (shared with the text fetch above) for what it points at: a
   canonical address on another site (a syndicated copy), the one post it
   embeds (Mastodon, Bluesky), a quoted post, or the one place its text
   links to, or marks as its source ("via", "h/t"). Then, cheapest first:
   a page with few words of its own (under 60, not counting what it
   embeds), a post with next to none (under 8: "look at this", an emoji;
   a post's line of comment is value), or one that `bge-small` finds
   retells its target (≥ 0.9) at no greater length, is a pointer; a page
   with plenty of its own (400+ words) that reads unlike its target has
   value; anything between is put to Jev
   ("does it add value beyond what it links to?"), and where Jev is unsure
   (0.3–0.7) or fails, to Claude Haiku, at most 300 calls a day. They
   compare short extracts (the title and about 1,500 characters of each).
   While it's a pointer, the item becomes what it points at, at most three
   hops down and within about 20 s for a batch; anything unreadable, out of
   time or past the cap keeps the hop reached, as it was before. Posts are
   read through their APIs (a Mastodon server's public status lookup,
   Bluesky's getPosts), never scraped; X, Instagram, Facebook, YouTube,
   Threads, LinkedIn, TikTok and paywalled pages are never followed. The
   reader sees the original, with the wrappers as "via Hackaday · via @fan
   on Bluesky"; every verdict is kept by the wrapper's address for 30 days,
   so a popular original reached through many wrappers is judged once.
   A post a reader taps that the curator hasn't seen (their Mastodon
   bookmarks) isn't followed: they're waiting.
   Following happens before an item's id is fixed, so the same original
   reached through different wrappers is one item, with a sighting for
   each. `/status` shows the day's counts: items followed, hops, verdicts
   by heuristic, cache, Jev and Haiku.
3. **Resolve the story.** Identical links (tracking parameters stripped) and
   near-identical titles become one item, with every place it was seen
   recorded. Items get a local embedding (`bge-small`, on the CPU); items from
   different sources within 72 hours that are close enough are one story.
   Pairs in the grey zone are put to Jev ("the same story?"), including an
   outlet's own reposts and one event split over several articles (a beta
   day's post per OS). A wrapper kept as an item of its own (not followed:
   past the Haiku cap, out of time, or nobody sure) and the item it points
   to are one story by definition, whatever the embedding says, in either
   order of arrival; each new item's next hop and the verdict on it are
   kept (`points_to`) for this. Each story's
   lead is its most informative version: a fuller summary, first to report,
   a source the readers trust, fresh. But an item that points to another of
   its story and doesn't add value to it (a pointer, or undecided) never
   leads over it: it's taken out of the running, and becomes a "via" of
   what it points to. A wrapper that adds value is a version like any
   other. `/status` counts the items put in a story by a link and the leads
   the rule decided.
4. **Judge**, cheapest first. Rules for everyone (not English, hashtag soup,
   labelled sponsored by its source).
   Then the item's **traits**, asked once for everyone: how significant it
   is as news, whether it's a news report, rage bait, an advert, hype,
   personal chatter or a template how-to, and whether it's paid-for
   content (sponsored, an advertorial; a confident yes drops it for
   everyone, a likely one only demotes it). Then, for each reader who follows
   one of its sources: their own rules, their own questions (relevance,
   topic) with their profile as context, and their local model.
5. **Publish** each reader's feed: the deciding classifier's stories that
   clear their bar, up to a daily ceiling (a slow day gives a short feed,
   not filler), plus a "maybe" lane.

**Social, in real time.** New Mastodon and Bluesky posts reach the feed
within a minute or two, over outbound connections only:

- Each followed Mastodon hashtag is a stream (Server-Sent Events from the
  server's streaming API, found in `/api/v2/instance`), read with the
  owner's token; each connected reader's home timeline is their `user`
  stream, with their own. Only new posts (`update`) are taken. On every
  (re)connect the timeline is first polled since its cursor (`since_id`),
  so a disconnect leaves no gap. Reconnects back off from 2 s, doubling to
  5 minutes, with jitter; 90 s without a heartbeat counts as a disconnect.
- Bluesky's hashtags are polled (`searchPosts`, latest first) every 90 s,
  each on its own: a failing tag backs off, doubling up to 15 minutes,
  and the others carry on. Its firehose (Jetstream) would cost about
  1.35 GB a day, even compressed, to keep a few hundred posts an hour;
  polling costs about 190 MB for about a minute's delay.
- What arrives is queued. 45 s after the first item, the **social cycle**
  takes the queue through the same steps 2 to 5 as the fast loop, and
  publishes only the readers who follow a source it was seen in. An empty
  queue costs nothing.
- Collection runs alongside; processing (ingest to publish, the daily jobs,
  re-scoring) runs one at a time, so the social cycle waits at most for the
  fast loop's few seconds of judging, never for its slow feeds.
- Sources followed or dropped, and readers connecting Mastodon, are picked
  up at the next fast loop. `/status` shows each stream (connected since,
  last event and heartbeat, reconnects, last error), each Bluesky tag
  (last poll and success, backoff, last error) and the last social cycle.

**The slow loop** runs once a day: Claude revises each reader's profile from
their recent behaviour, saving a new version; each reader's local model is
retrained; history the agents' never-collect lists forbid is purged; the
database is backed up.

## Two classifiers

- **Jev** (hosted, TypeSafe AI) answers typed questions with calibrated
  probabilities. An item's score is the better of its relevance to the
  reader and its importance as news, plus a small bonus for news reports,
  discounted for rage bait, adverts, sponsored content, hype, chatter and
  template how-tos.
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
At least four items from the last three months judged, at least half of
them would have made the feed, and comfortably, on their own merits (an
item that's a how-to, an advert, sponsored, hype, rage bait or chatter never counts),
and most of those are stories the reader doesn't already get. How often a
source posts doesn't count against it: rare posting is often a sign of care. At most three a week; most weeks there may be
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

One timeline, oldest to newest from where you left off: the stories the
curator picked. Your Mastodon home timeline is just another source (with a
lower bar), so a post in it that's also elsewhere is one story, shown once.
Stories get a Mastodon-style id (milliseconds << 16) from when they were
first seen. The position is kept on the server and only moves forward, so devices
can't undo each other's progress. The app never talks to Mastodon itself;
replying and boosting happen on your server ("See original").
Its diagnostics log (ids, paths and timings only) goes from Settings to the
server's journal under a short id (`POST /api/diagnostics`, a line per entry),
where the majordomo reads it in `/srv/logs`; or out as a file through the
share sheet.

## Privacy

- The Mac agent sends titles and URLs, never page contents, and drops
  anything on your never-collect list before sending. The list stays on your
  Mac; only its rules go to the curator, which purges matching history.
  Agent history is kept 90 days.
- Jev and Claude see item text and your profile, never raw history.
- Mastodon access is read-only, and tokens are encrypted at rest.
- Each reader's feed is at an address only they know.
