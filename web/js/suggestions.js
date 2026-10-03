import { el, sourceLabel } from './render.js';

// The weekly scout's suggestions: new sources the curator found and tried out
// on paper, for you to add, try for a week, or turn down; and, when a trial's
// week is up, how it went.

const KIND = {
  rss: 'Site or feed', mastodon_account: 'Person on Mastodon', bluesky_account: 'Person on Bluesky',
  mastodon_hashtag: 'Hashtag on Mastodon', bluesky_hashtag: 'Hashtag on Bluesky',
};
const pct = (x) => `${Math.round(x * 100)}%`;
const when = (iso) => new Date(iso).toLocaleDateString([], { dateStyle: 'medium' });

function card(s, decide) {
  const e = s.evidence || {};
  const buttons = s.status === 'review'
    ? [['keep', 'Keep it', 'primary'], ['drop', 'Drop it', '']]
    : [['add', 'Add', 'primary'], ['try', 'Try for a week', ''], ['dismiss', 'Not interested', '']];
  const stats = s.status === 'review'
    ? `This week: ${e.items ?? 0} items; you liked ${e.liked ?? 0}, opened ${e.opened ?? 0}, disliked ${e.disliked ?? 0}.`
    : `About ${e.per_day} a day · ${pct(e.hit_rate)} would make your feed · ${pct(e.novelty)} of those new to you`;
  const row = el('div', { class: 'suggestion-actions' });
  for (const [action, label, cls] of buttons) {
    row.append(el('button', { type: 'button', class: cls, text: label, onclick: async (ev) => {
      row.querySelectorAll('button').forEach((b) => (b.disabled = true));
      try {
        await decide(s.id, action);
      } finally {
        row.querySelectorAll('button').forEach((b) => (b.disabled = false));
      }
    } }));
  }
  const href = s.kind === 'rss' ? s.value : null;
  return el('article', { class: 'suggestion', 'data-id': s.id },
    el('div', { class: 'curated-label', text: s.status === 'review' ? '🧪 Trial over' : `💡 ${KIND[s.kind] || s.kind}` }),
    el('strong', { class: 'suggestion-name', text: s.kind === 'rss' ? s.name : sourceLabel(`${s.kind.split('_')[0]}:${s.kind.endsWith('hashtag') ? '#' : '@'}${s.value}`) }),
    href ? el('small', { class: 'hint', text: href }) : null,
    el('p', { class: 'hint', text: `Why: ${s.why}` }),
    el('p', { text: stats }),
    (e.samples || []).length ? el('ul', { class: 'samples' }, ...e.samples.map((x) => el('li', {}, el('a', { href: x.url, target: '_blank', rel: 'noopener noreferrer', text: x.title })))) : null,
    row);
}

// Fills `body` with the suggestions page; `onChange` runs after a decision.
export async function showSuggestions(api, body, { showError, busyText, onChange }) {
  body.replaceChildren(el('p', { class: 'hint pad' }, ...busyText('Loading suggestions…')));
  const { open, trying, history } = await api.suggestions();
  const decide = async (id, action) => {
    try {
      await api.decide(id, action);
      onChange?.();
      await showSuggestions(api, body, { showError, busyText, onChange });
    } catch (err) {
      showError(err);
    }
  };
  const page = el('div', { class: 'pad suggestions' },
    el('p', { class: 'hint', text: 'Once a week the curator looks for sources you don’t have (outlets covering stories you liked, people whose posts you liked, and a web search around your interests), tries their recent items against your profile, and suggests the ones that would add something.' }));
  if (open.length) page.append(...open.map((s) => card(s, decide)));
  else page.append(el('p', { class: 'hint', text: 'Nothing new this week. Suggestions arrive on Monday mornings.' }));
  if (trying.length) {
    page.append(el('h2', { class: 'section-title', text: 'Trying now' }),
      el('ul', { class: 'hint' }, ...trying.map((s) => el('li', { text: `${s.name} (since ${when(`${(s.decided || '').slice(0, 10)}T12:00:00`)}; a week, then you decide)` }))));
  }
  if (history.length) {
    page.append(el('details', {}, el('summary', { text: 'Earlier suggestions' }),
      el('ul', { class: 'hint' }, ...history.map((s) => el('li', { text: `${s.status === 'accepted' ? '✓ Added' : '✗ Not added'}: ${s.name}` })))));
  }
  body.replaceChildren(page);
}
