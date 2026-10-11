// Turns Mastodon status JSON into DOM. All remote HTML goes through an
// allowlist sanitiser before it is attached to the document.

const ALLOWED_TAGS = new Set([
  'P', 'BR', 'A', 'SPAN', 'STRONG', 'EM', 'B', 'I', 'U', 'S', 'DEL', 'CODE', 'PRE',
  'BLOCKQUOTE', 'UL', 'OL', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'SUB', 'SUP',
]);
const ALLOWED_CLASSES = new Set(['invisible', 'ellipsis', 'mention', 'hashtag', 'h-card', 'u-url', 'quote-inline']);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

function safeUrl(href) {
  // No address is no link (new URL(null) would quietly make one: "/null").
  if (!href || typeof href !== 'string') return null;
  try {
    const u = new URL(href, location.href);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.toString() : null;
  } catch {
    return null;
  }
}

function sanitise(html) {
  const doc = new DOMParser().parseFromString(`<body>${html || ''}</body>`, 'text/html');
  const out = document.createDocumentFragment();
  const walk = (src, dest) => {
    for (const child of src.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        dest.append(child.textContent);
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        if (!ALLOWED_TAGS.has(child.tagName)) {
          // Keep the text of unknown elements, drop scripts/styles entirely.
          if (!['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'TEMPLATE'].includes(child.tagName)) walk(child, dest);
          continue;
        }
        const copy = document.createElement(child.tagName);
        const cls = (child.getAttribute('class') || '').split(/\s+/).filter((c) => ALLOWED_CLASSES.has(c));
        if (cls.length) copy.className = cls.join(' ');
        if (child.tagName === 'A') {
          const href = safeUrl(child.getAttribute('href'));
          if (href) copy.href = href;
          copy.target = '_blank';
          copy.rel = 'noopener noreferrer';
        }
        walk(child, copy);
        dest.append(copy);
      }
    }
  };
  walk(doc.body, out);
  return out;
}

// Replace :shortcode: in text nodes with custom emoji images.
function emojify(root, emojis) {
  if (!emojis || !emojis.length) return root;
  const map = new Map(emojis.map((e) => [e.shortcode, e]));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const t of texts) {
    const parts = t.textContent.split(/:([a-zA-Z0-9_]+):/);
    if (parts.length === 1) continue;
    const frag = document.createDocumentFragment();
    parts.forEach((p, i) => {
      const e = i % 2 ? map.get(p) : null;
      if (e && safeUrl(e.url)) frag.append(el('img', { class: 'emoji', src: e.url, alt: `:${p}:`, title: `:${p}:`, loading: 'lazy' }));
      else frag.append(i % 2 ? `:${p}:` : p);
    });
    t.replaceWith(frag);
  }
  return root;
}

function richText(html, emojis) {
  const frag = sanitise(html);
  const wrap = document.createElement('div');
  wrap.append(frag);
  return emojify(wrap, emojis);
}

function plainWithEmoji(text, emojis) {
  const span = el('span', { text });
  return emojify(span, emojis);
}

function relTime(iso) {
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${Math.max(s, 0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d`;
  return new Date(iso).toLocaleDateString();
}

// Links that open on the user's home instance, so following/replying happens
// in the normal Mastodon UI.
function homeLinks(instance) {
  return {
    account: (acct) => `https://${instance}/@${acct}`,
    status: (s) => `https://${instance}/@${s.account.acct}/${s.id}`,
    tag: (name) => `https://${instance}/tags/${encodeURIComponent(name)}`,
  };
}

function rewriteLinks(content, status, links) {
  for (const a of content.querySelectorAll('a')) {
    if (a.classList.contains('mention')) {
      const m = status.mentions.find((m) => m.url === a.href);
      if (m) a.href = links.account(m.acct);
    } else if (a.classList.contains('hashtag')) {
      const name = a.textContent.replace(/^#/, '');
      if (name) a.href = links.tag(name);
    }
  }
}

function media(attachments, sensitive) {
  if (!attachments.length) return null;
  const grid = el('div', { class: `media n${Math.min(attachments.length, 4)}${sensitive ? ' sensitive' : ''}` });
  for (const m of attachments) {
    const alt = m.description || '';
    let item;
    // Reserve the space before the image loads, so posts don't jump around.
    const size = m.meta?.small || m.meta?.original || {};
    const dims = size.width && size.height ? { width: size.width, height: size.height } : {};
    if (m.type === 'image') {
      const full = safeUrl(m.url) || safeUrl(m.remote_url);
      item = el('a', { href: full, target: '_blank', rel: 'noopener noreferrer' },
        el('img', { src: safeUrl(m.preview_url) || full, alt, title: alt, loading: 'lazy', ...dims }));
    } else if (m.type === 'gifv') {
      item = el('video', { src: safeUrl(m.url), autoplay: true, loop: true, muted: true, playsinline: true, 'aria-label': alt, ...dims });
      item.muted = true;
    } else if (m.type === 'video') {
      item = el('video', { src: safeUrl(m.url), controls: true, preload: 'none', poster: safeUrl(m.preview_url), playsinline: true, 'aria-label': alt, ...dims });
    } else if (m.type === 'audio') {
      item = el('audio', { src: safeUrl(m.url), controls: true, preload: 'none' });
    } else {
      item = el('a', { href: safeUrl(m.remote_url || m.url), target: '_blank', rel: 'noopener noreferrer', class: 'unknown-media', text: 'Attachment' });
    }
    grid.append(el('div', { class: 'media-item' }, item, alt ? el('span', { class: 'alt-badge', title: alt, text: 'ALT' }) : null));
  }
  if (sensitive) {
    grid.append(el('button', { class: 'reveal', type: 'button', text: 'Sensitive media — tap to show', onclick: (e) => {
      grid.classList.remove('sensitive');
      e.currentTarget.remove();
    } }));
  }
  return grid;
}

function card(c) {
  const href = c && safeUrl(c.url);
  if (!href) return null;
  return el('a', { class: 'card', href, target: '_blank', rel: 'noopener noreferrer' },
    c.image ? el('img', { src: safeUrl(c.image), alt: '', loading: 'lazy' }) : null,
    el('div', { class: 'card-text' },
      el('strong', { text: c.title || href }),
      c.description ? el('p', { text: c.description }) : null,
      el('small', { text: c.provider_name || new URL(href).hostname })));
}

function poll(p, url) {
  if (!p) return null;
  const total = p.votes_count || 0;
  const list = el('ul', { class: 'poll' });
  for (const o of p.options) {
    const pct = total ? Math.round(((o.votes_count || 0) / total) * 100) : 0;
    list.append(el('li', {}, el('span', { class: 'bar', style: `width:${pct}%` }), el('span', { text: `${o.title}` }), el('b', { text: `${pct}%` })));
  }
  return el('div', {}, list, el('a', { class: 'poll-meta', href: url, target: '_blank', rel: 'noopener noreferrer', text: `${total} votes${p.expired ? ' · closed' : ' · vote on server'}` }));
}

function filterHit(status) {
  const hits = (status.filtered || []).map((f) => f.filter);
  if (hits.some((f) => f.filter_action === 'hide')) return { hide: true };
  const warn = hits.find((f) => f.filter_action === 'warn');
  return warn ? { warn: warn.title } : {};
}

function statusBody(s, links, { nested = false } = {}) {
  const body = el('div', { class: 'body' });
  const content = richText(s.content, s.emojis);
  content.className = 'content';
  rewriteLinks(content, s, links);
  const extras = [
    media(s.media_attachments || [], s.sensitive),
    s.poll ? poll(s.poll, links.status(s)) : null,
    !nested && s.quote && s.quote.quoted_status ? quote(s.quote.quoted_status, links) : null,
    !s.media_attachments?.length && !s.poll ? card(s.card) : null,
  ].filter(Boolean);
  if (s.spoiler_text) {
    body.append(el('details', { class: 'cw' },
      el('summary', {}, plainWithEmoji(s.spoiler_text, s.emojis)),
      content, ...extras));
  } else {
    body.append(content, ...extras);
  }
  return body;
}

function quote(q, links) {
  return el('a', { class: 'quote', href: links.status(q), target: '_blank', rel: 'noopener noreferrer' },
    el('div', { class: 'who' }, el('strong', {}, plainWithEmoji(q.account.display_name || q.account.username, q.account.emojis)), el('small', { text: ` @${q.account.acct}` })),
    statusBody(q, links, { nested: true }));
}

function actionButton(label, icon, count, active, onToggle) {
  const btn = el('button', { type: 'button', class: `act${active ? ' on' : ''}`, 'aria-pressed': String(!!active), 'aria-label': label, title: label },
    el('span', { class: 'icon', text: icon }), el('span', { class: 'count', text: count ? String(count) : '' }));
  btn.addEventListener('click', async () => {
    const next = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', String(next));
    btn.classList.toggle('on', next);
    btn.disabled = true;
    try {
      const updated = await onToggle(next);
      const c = updated && { favourite: updated.favourites_count, reblog: updated.reblogs_count }[btn.dataset.kind];
      if (c != null) btn.querySelector('.count').textContent = c ? String(c) : '';
    } catch (err) {
      btn.setAttribute('aria-pressed', String(!next));
      btn.classList.toggle('on', !next);
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

// Clicks on these open their own thing rather than the thread.
const INTERACTIVE = 'a, button, summary, details.cw:not([open]), video, audio, input, textarea';

// A Mastodon post, or null if one of your server-side filters hides it.
// `onThread(status)` opens the conversation. `curation` (for timeline posts): { verdict, vote,
// bookmarked, onVote(vote), onBookmark(on) }. Replying, boosting and
// favouriting happen on your server: ↗ opens the post there.
export function renderStatus(entry, { instance, onThread, focus = false, depth = 0, curation = null }) {
  const s = entry.reblog || entry;
  const f = filterHit(s);
  if (f.hide) return null;
  const links = homeLinks(instance);
  const a = s.account;
  const openThread = (e) => {
    // Let modified clicks open the link in a new tab as normal.
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1)) return;
    if (e) e.preventDefault();
    onThread?.(s);
  };

  const article = el('article', { class: `status${focus ? ' focus' : ''}${depth ? ` depth d${Math.min(depth, 5)}` : ''}`, 'data-id': entry.id });
  if (entry.reblog) {
    article.append(el('div', { class: 'boosted-by' }, '🔁 ',
      el('a', { href: links.account(entry.account.acct), target: '_blank', rel: 'noopener noreferrer' },
        plainWithEmoji(entry.account.display_name || entry.account.username, entry.account.emojis)),
      ' boosted'));
  }

  const header = el('header', {},
    el('a', { class: 'avatar', href: links.account(a.acct), target: '_blank', rel: 'noopener noreferrer' },
      el('img', { src: safeUrl(a.avatar_static || a.avatar), alt: '', loading: 'lazy' })),
    el('div', { class: 'who' },
      el('a', { href: links.account(a.acct), target: '_blank', rel: 'noopener noreferrer' },
        el('strong', {}, plainWithEmoji(a.display_name || a.username, a.emojis))),
      el('small', { text: `@${a.acct}` })),
    el('a', { class: 'time', href: links.status(s), title: new Date(s.created_at).toLocaleString(), text: focus ? new Date(s.created_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : relTime(s.created_at), onclick: openThread }));
  article.append(header);

  if (s.in_reply_to_id && !depth && !focus) {
    const target = s.mentions.find((m) => m.id === s.in_reply_to_account_id);
    article.append(el('a', { class: 'reply-to', href: links.status(s), onclick: openThread,
      text: s.in_reply_to_account_id === a.id ? '↩ Thread continues · show thread' : `↩ Replying to @${target ? target.acct : '…'} · show thread` }));
  }

  let body = statusBody(s, links);
  if (f.warn) {
    body = el('details', { class: 'cw filtered' }, el('summary', { text: `Filtered: ${f.warn}` }), body);
  }
  article.append(body);
  const verdict = curation?.verdict;
  if (verdict?.lane === 'maybe') {
    article.prepend(el('div', { class: 'curated-label' }, '🤔 Maybe: the curator isn\u2019t sure. Tell it with 👍 or 👎'));
    article.classList.add('maybe');
  }
  const also = alsoCovered(verdict?.also);
  if (also) body.append(also);

  const footer = el('footer', {});
  if (curation) {
    footer.append(...voteButtons(article, curation.vote || 0, curation.onVote),
      actionButton('Bookmark', '🔖', 0, curation.bookmarked, curation.onBookmark));
  }
  footer.append(el('a', { class: 'act see-original', href: links.status(s), target: '_blank', rel: 'noopener noreferrer',
    title: `See the original on ${instance} (reply, boost, favourite)`, 'aria-label': `See the original on ${instance}` },
  el('span', { class: 'icon', text: '↗' }), el('span', { class: 'label', text: 'See original' })));
  article.append(footer);

  // Tapping the post itself (not a link or button in it) opens the thread.
  if (!focus) {
    article.addEventListener('click', (e) => {
      if (e.target.closest(INTERACTIVE) || getSelection().toString()) return;
      openThread();
    });
  }
  return article;
}

// Plain text with any web addresses in it turned into links (curated text
// arrives as plain text, so a URL in a post would otherwise be dead).
const URL_RE = /\b(?:https?:\/\/|www\.)[^\s<>"]+/gi;
export function linkify(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    let url = m[0];
    const trail = url.match(/[.,;:!?)\]]+$/); // sentence punctuation isn't part of the link
    if (trail) url = url.slice(0, -trail[0].length);
    const href = safeUrl(url.startsWith('www.') ? `https://${url}` : url);
    if (!href) continue;
    frag.append(text.slice(last, m.index), el('a', { href, target: '_blank', rel: 'noopener noreferrer', text: url }));
    last = m.index + url.length;
  }
  frag.append(text.slice(last));
  return frag;
}

// 👍 and 👎 for any card. `current` is 1, -1 or 0; `onVote(vote)` returns a
// promise. Pressing the lit button again clears the vote.
function voteButtons(article, current, onVote) {
  return [[1, '👍', 'More like this'], [-1, '👎', 'Less like this']].map(([value, icon, label]) => {
    const btn = el('button', { type: 'button', class: `act vote${current === value ? ' on' : ''}`, 'aria-pressed': String(current === value), 'aria-label': label, title: label },
      el('span', { class: 'icon', text: icon }));
    btn.dataset.vote = String(value);
    btn.addEventListener('click', async () => {
      const next = btn.getAttribute('aria-pressed') === 'true' ? 0 : value;
      const buttons = article.querySelectorAll('button.vote');
      buttons.forEach((b) => (b.disabled = true));
      try {
        await onVote(next);
        for (const b of buttons) {
          const on = Number(b.dataset.vote) === next;
          b.setAttribute('aria-pressed', String(on));
          b.classList.toggle('on', on);
        }
      } catch (err) {
        alert(err.message);
      } finally {
        buttons.forEach((b) => (b.disabled = false));
      }
    });
    return btn;
  });
}

// Shown instead of the raw source id ("rss:ABC News Top Stories").
export function sourceLabel(source) {
  const [kind, rest = ''] = (source || '').split(/:(.*)/s);
  if (kind === 'rss') return rest;
  if (kind === 'mastodon' && rest.startsWith('home:')) return 'your Mastodon timeline';
  if (kind === 'mastodon') return /^[#@]/.test(rest) ? `${rest} on Mastodon` : 'Mastodon';
  if (kind === 'bluesky') return `${rest} on Bluesky`;
  return source;
}

// Pictures are cropped to a wide frame, which suits a photo but ruins a comic
// strip or a tall screenshot: those are shown whole.
function wholeIfOddShape(e) {
  const img = e.currentTarget;
  const ratio = img.naturalHeight / img.naturalWidth;
  if (ratio && (ratio < 0.45 || ratio > 0.7)) img.parentElement.classList.add('whole');
}

// The post a quote post quotes, in a box as Mastodon apps show it. Tapping
// it opens the quoted post.
function curatedQuote(q) {
  if (!q) return null;
  const href = safeUrl(q.url);
  const image = safeUrl(q.image);
  return el(href ? 'a' : 'div', { class: 'quote', ...(href ? { href, target: '_blank', rel: 'noopener noreferrer' } : {}) },
    el('div', { class: 'who' }, el('strong', { text: q.name || q.author }), q.name && q.name !== q.author ? el('small', { text: ` ${q.author}` }) : null),
    q.text ? el('p', { class: 'quote-text', text: q.text }) : null,
    image ? el('img', { src: image, alt: '', loading: 'lazy', onerror: (e) => e.currentTarget.remove() }) : null);
}

function alsoCovered(also) {
  const links = (also || []).filter((a) => safeUrl(a.url));
  if (!links.length) return null;
  return el('p', { class: 'also' }, 'Also covered by ',
    ...links.flatMap((a, i) => [i ? ', ' : '', el('a', { href: safeUrl(a.url), target: '_blank', rel: 'noopener noreferrer', text: sourceLabel(a.source) })]));
}

// A curated article. `data-id` is its place in the timeline (a Mastodon-style
// id), `data-item` the curator's id for votes. Tapping the card anywhere opens
// the source; `onOpen(item)` is told when it does.
export function renderCurated(item, { onVote, onBookmark, onOpen }) {
  const vote = item.vote || 0;
  const bookmarked = !!item.bookmarked;
  const href = safeUrl(item.url);
  const maybe = item.lane === 'maybe';
  const article = el('article', { class: `status curated${maybe ? ' maybe' : ''}`, 'data-id': item.id, 'data-item': item.item });
  const link = (attrs, ...children) => el('a', { href, target: '_blank', rel: 'noopener noreferrer', onclick: () => onOpen?.(item), ...attrs }, ...children);

  // Say in words when a card is unusual: the edge colour alone didn't explain
  // it. Everything here is picked by the curator, so a plain pick says nothing.
  if (item.trial || maybe) {
    article.append(el('div', { class: 'curated-label' }, item.trial
      ? `🧪 Trying out ${item.trial}: 👍 or 👎 helps you decide whether to keep it`
      : '🤔 Maybe: the curator isn’t sure. Tell it with 👍 or 👎'));
  } else if (item.picked === false) {
    article.append(el('div', { class: 'curated-label', text: '🙈 Not in your feed: the curator passed on this' }));
  }
  // A post (or a link someone shared): who posted it first, then where it
  // came from. An article: the source, then its writer.
  const poster = item.poster;
  const avatar = poster && safeUrl(poster.avatar);
  const icon = el('span', { class: 'avatar source-icon', 'aria-hidden': 'true', text: '📰' });
  article.append(el('header', {},
    avatar ? el('span', { class: 'avatar', 'aria-hidden': 'true' },
      el('img', { src: avatar, alt: '', loading: 'lazy', onerror: (e) => e.currentTarget.parentElement.replaceWith(icon) })) : icon,
    poster
      ? el('div', { class: 'who' },
        el('strong', { text: poster.name || item.author }),
        el('small', { text: [poster.name ? item.author : '', sourceLabel(item.source)].filter(Boolean).join(' · ') }))
      : el('div', { class: 'who' },
        el('strong', { text: sourceLabel(item.source) }),
        el('small', { text: item.author || 'Article' })),
    link({ class: 'time', title: new Date(item.published).toLocaleString(), text: relTime(item.published) })));

  const image = safeUrl(item.image);
  // A Mastodon or Bluesky post has no real title: the curator's "title" is
  // just its opening words. Show the post's text once, in full, instead.
  const opening = item.title.replace(/…$/, '').trim();
  const isPost = !!opening && item.summary.replace(/\s+/g, ' ').startsWith(opening.replace(/\s+/g, ' '));
  const text = item.summary ? el('p', { class: `curated-summary${isPost ? ' post-text' : ''}` }, linkify(item.summary)) : null;
  const titleLink = link({ class: 'curated-title' }, el('strong', { text: item.title }));
  const body = el('div', { class: 'body' },
    isPost ? null : titleLink,
    image ? link({ class: 'curated-image', tabindex: '-1', 'aria-hidden': 'true' },
      el('img', { src: image, alt: '', loading: 'lazy', onload: wholeIfOddShape, onerror: (e) => e.currentTarget.parentElement.remove() })) : null,
    text);
  if (text) {
    // Long text is clipped to a few lines; "Show more" opens it in place.
    const more = el('button', { type: 'button', class: 'linklike show-more', hidden: true, text: 'Show more' });
    more.addEventListener('click', () => {
      const open = text.classList.toggle('expanded');
      more.textContent = open ? 'Show less' : 'Show more';
    });
    body.append(more);
    // Only once it's on the page can we tell whether it overflows.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (text.scrollHeight > text.clientHeight + 2) more.hidden = false;
    }));
  }
  const quoted = curatedQuote(item.quote);
  if (quoted) body.append(quoted);
  const also = alsoCovered(item.also);
  if (also) body.append(also);
  if (item.reason) body.append(el('details', { class: 'why' }, el('summary', { text: 'Why this?' }), el('p', { text: item.reason })));
  article.append(body);

  const openLink = link({ class: 'act', title: 'Open the article', 'aria-label': 'Open the article' }, el('span', { class: 'icon', text: '↗' }));
  const footer = el('footer', {}, ...voteButtons(article, vote, (v) => onVote(item, v)));
  if (onBookmark) {
    footer.append(actionButton('Bookmark', '🔖', 0, bookmarked, (on) => onBookmark(item, on)));
  }
  footer.append(openLink);
  article.append(footer);

  // Tapping the card anywhere else opens the source, through the same link
  // (so "Open links in Safari" and the open signal both apply).
  article.addEventListener('click', (e) => {
    if (e.target.closest(INTERACTIVE) || getSelection().toString()) return;
    (isPost ? openLink : titleLink).click();
  });
  return article;
}

// A row for an account in search results; opens the profile on the home
// server, where following/unfollowing happens.
export function renderAccount(acc, { instance }) {
  const href = homeLinks(instance).account(acc.acct);
  return el('a', { class: 'account-row', href, target: '_blank', rel: 'noopener noreferrer' },
    el('img', { src: safeUrl(acc.avatar_static || acc.avatar), alt: '', loading: 'lazy' }),
    el('div', { class: 'who' },
      el('strong', {}, plainWithEmoji(acc.display_name || acc.username, acc.emojis)),
      el('small', { text: `@${acc.acct} · ${acc.followers_count ?? 0} followers` })));
}

export function renderTag(tag, { instance }) {
  const uses = (tag.history || []).slice(0, 7).reduce((n, d) => n + Number(d.uses || 0), 0);
  return el('a', { class: 'tag-row', href: homeLinks(instance).tag(tag.name), target: '_blank', rel: 'noopener noreferrer' },
    el('strong', { text: `#${tag.name}` }),
    el('small', { text: uses ? `${uses} posts this week` : '' }));
}

// iOS 17+ opens x-safari-https:// links in Safari itself rather than in the
// in-app viewer a home-screen app gets. Returns null for non-web links.
export function safariUrl(href) {
  try {
    const u = new URL(href);
    return u.protocol === 'https:' || u.protocol === 'http:' ? `x-safari-${u.href}` : null;
  } catch {
    return null;
  }
}

export { el };
