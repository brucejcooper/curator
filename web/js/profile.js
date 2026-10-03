import { el } from './render.js';

// "How the curator sees you": the reader's profile as the curator holds it,
// every version (daily refreshes, the onboarding interview, their own edits),
// what changed in each, and editing it by hand.

// A small Markdown subset (headings, bullets, **bold**), built as DOM with
// text only: profiles are plain prose and never carry HTML.
export function renderMarkdown(md) {
  const root = el('div', { class: 'md' });
  let list = null;
  const inline = (text) => text.split(/(\*\*[^*]+\*\*)/).map((part) =>
    part.startsWith('**') && part.endsWith('**') ? el('strong', { text: part.slice(2, -2) }) : part);
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*] (.*)/);
    if (bullet) {
      list ??= root.appendChild(el('ul'));
      list.append(el('li', {}, ...inline(bullet[1])));
      continue;
    }
    if (/^\s+\S/.test(line) && list?.lastChild) {
      list.lastChild.append(' ', ...inline(line.trim())); // a wrapped bullet
      continue;
    }
    list = null;
    const heading = line.match(/^(#{1,3}) (.*)/);
    if (heading) root.append(el(`h${heading[1].length + 1}`, { text: heading[2] }));
    else if (line.trim()) root.append(el('p', {}, ...inline(line)));
  }
  return root;
}

// Line diff (longest common subsequence): fine for documents this size.
export function lineDiff(before, after) {
  const a = before.split('\n');
  const b = after.split('\n');
  const n = a.length;
  const m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) {
      out.push([' ', a[i]]);
      i++;
      j++;
    } else if (j < m && (i >= n || lcs[i][j + 1] >= lcs[i + 1][j])) out.push(['+', b[j++]]);
    else out.push(['-', a[i++]]);
  }
  return out;
}

function diffView(before, after) {
  const lines = lineDiff(before, after);
  if (!lines.some(([k]) => k !== ' ')) return el('p', { class: 'hint', text: 'No change.' });
  // Only the changes, with a line of context either side.
  const keep = new Set();
  lines.forEach(([k], idx) => {
    if (k !== ' ') for (let d = -1; d <= 1; d++) keep.add(idx + d);
  });
  const pre = el('pre', { class: 'diff' });
  let skipped = false;
  lines.forEach(([k, text], idx) => {
    if (!keep.has(idx)) {
      if (!skipped) pre.append(el('span', { class: 'gap', text: '⋯\n' }));
      skipped = true;
      return;
    }
    skipped = false;
    pre.append(el('span', { class: k === '+' ? 'add' : k === '-' ? 'del' : '', text: `${k} ${text}\n` }));
  });
  return pre;
}

const AUTHORS = { curator: 'Daily refresh', onboarding: 'From your interview', user: 'Edited by you', import: 'Imported' };

const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });

// Fills `body` with the profile page. `onSaved` runs after an edit is saved.
export async function showProfile(api, body, { showError, busyText }) {
  body.replaceChildren(el('p', { class: 'hint pad' }, ...busyText('Loading your profile…')));
  const { current, versions } = await api.profile();
  const page = el('div', { class: 'profile pad' });

  const view = () => {
    const doc = el('div', {}, renderMarkdown(current.profile_md));
    const history = el('ul', { class: 'versions' });
    for (const v of versions) {
      const item = el('li', {},
        el('button', { type: 'button', class: 'linklike', text: `v${v.version} · ${AUTHORS[v.author] || v.author} · ${when(v.created)}` }),
        el('div', { class: 'hint', text: v.summary || '' }));
      const detail = el('div', { class: 'version-detail', hidden: true });
      item.append(detail);
      item.querySelector('button').onclick = async () => {
        if (!detail.hidden) {
          detail.hidden = true;
          return;
        }
        detail.hidden = false;
        detail.replaceChildren(el('p', { class: 'hint' }, ...busyText('Loading…')));
        try {
          const [it, prev] = await Promise.all([api.profileVersion(v.version), v.version > 1 ? api.profileVersion(v.version - 1) : null]);
          detail.replaceChildren(...['profile_md', 'sources_yaml', 'questions_yaml'].map((k) => el('details', { open: k === 'profile_md' },
            el('summary', { text: { profile_md: 'Profile', sources_yaml: 'Sources', questions_yaml: 'Questions' }[k] }),
            prev ? diffView(prev[k], it[k]) : el('pre', { class: 'diff', text: it[k] }))));
        } catch (err) {
          detail.replaceChildren(el('p', { class: 'hint', text: "Couldn't load this version." }));
          showError(err);
        }
      };
      history.append(item);
    }
    const edit = el('button', { type: 'button', text: 'Edit' });
    edit.onclick = editor;
    page.replaceChildren(
      el('p', { class: 'hint', text: `Version ${current.version}, ${AUTHORS[current.author] || current.author} on ${when(current.created)}. The curator revises this every morning from what you read and vote on; you can correct it any time.` }),
      doc, edit,
      el('h2', { class: 'section-title', text: 'How it has changed' }), history);
  };

  const editor = () => {
    const field = (key, label, rows) => [el('label', { for: `edit-${key}`, text: label }),
      el('textarea', { id: `edit-${key}`, rows, spellcheck: key === 'profile_md' ? 'true' : 'false' })];
    const summary = el('input', { id: 'edit-summary', placeholder: 'What you changed (optional)' });
    const save = el('button', { type: 'button', class: 'primary', text: 'Save as a new version' });
    const cancel = el('button', { type: 'button', text: 'Cancel', onclick: view });
    page.replaceChildren(
      el('p', { class: 'hint', text: 'Saved as a new version; nothing is overwritten. Sources and questions are YAML: keep the structure.' }),
      ...field('profile_md', 'Profile', 16), ...field('sources_yaml', 'Sources', 14), ...field('questions_yaml', 'Questions', 12),
      summary, el('div', { class: 'compose-row' }, cancel, save));
    for (const k of ['profile_md', 'sources_yaml', 'questions_yaml']) page.querySelector(`#edit-${k}`).value = current[k];
    save.onclick = async () => {
      save.disabled = true;
      try {
        const docs = Object.fromEntries(['profile_md', 'sources_yaml', 'questions_yaml'].map((k) => [k, page.querySelector(`#edit-${k}`).value]));
        await api.saveProfile({ ...docs, summary: summary.value.trim() });
        await showProfile(api, body, { showError, busyText });
      } catch (err) {
        showError(err);
        save.disabled = false;
      }
    };
  };

  view();
  body.replaceChildren(page);
}

// The first-visit interview. Resolves once the profile has been written.
export function runOnboarding(api, { showError }) {
  const form = document.querySelector('#onboarding-form');
  return new Promise((resolve) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      button.textContent = 'Writing your profile… (about half a minute)';
      try {
        await api.onboard(Object.fromEntries(new FormData(form)));
        resolve();
      } catch (err) {
        showError(err);
        button.disabled = false;
        button.textContent = 'Start my feed';
      }
    };
  });
}
