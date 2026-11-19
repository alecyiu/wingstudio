/* Wing Studio - citations: the reference mark beside a value an agent decided, and the one
   pop-up it opens (the reason, the sources with what their documents say, earlier runs of a similar
   case, links to ref.html).
   A citation is a list of ids into the studio's reference catalogue (GET /api/refs). Every title,
   address and quoted passage shown here comes from that catalogue, never from a model, a study
   record or an event. A quote is always the passage text as stored; the studio's own words (the
   summary, the note on a terse passage, how it differs) stand outside the quote. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h;

  const { ID, PASSAGE, KIND } = AS.REF;
  const NO_REASON = 'Plan default. No separate reason was recorded for this value; the references below support the default.';
  const NO_REASON_DECISION = 'No reason was recorded for this decision; the references below support the value.';
  const NO_RUNS = 'No earlier run of a similar case.';
  const QUOTES = 2, RUNS = 3;                     // the pop-up stays small: the rest is in the document and on the Dashboard
  const TERSE = 80;                               // a quote shorter than this rarely says alone what it is cited for
  const FORMULA = 'Formulas are in the notation of the source';
  const MARGIN = 8, GAP = 4, MIN_HEIGHT = 120;

  // ---------- the catalogue: one request, kept for the life of the page ----------
  let cat = null, loading = null, loadFailed = false;
  function load() {
    if (cat) return Promise.resolve(cat);
    if (loading) return loading;
    loadFailed = false;
    loading = AS.api.get('/api/refs').then((data) => {
      // the server answers 200 with no entries and an error text when its catalogue file is unusable
      if (!data || !Array.isArray(data.entries) || (data.error && !data.entries.length)) throw new Error((data && data.error) || 'no reference catalogue');
      // a Map, not an object: an id such as "constructor" matches the id pattern
      const byId = new Map();
      for (const e of data.entries) if (e && typeof e.id === 'string' && ID.test(e.id)) byId.set(e.id, e);
      cat = { byId, defaults: data.defaults && typeof data.defaults === 'object' ? data.defaults : {} };
      loading = null;
      return cat;
    }).catch((e) => { loading = null; loadFailed = true; throw e; });   // not kept: the next call asks again
    return loading;
  }
  /* The id is only ever a key of the catalogue, and only after it matched the id pattern. */
  function get(id) { return cat && typeof id === 'string' && ID.test(id) ? cat.byId.get(id) || null : null; }
  /* ref.html#<id> shows the entry, ref.html#<id>:<passage key> the entry with that passage in view. */
  const href = (id, passage) => 'ref.html#' + encodeURIComponent(id) + (typeof passage === 'string' && PASSAGE.test(passage) ? ':' + passage : '');
  function idList(v) {
    const list = typeof v === 'string' ? [v] : Array.isArray(v) ? v : [];
    return list.filter((x, i) => typeof x === 'string' && x && list.indexOf(x) === i);
  }
  /* The setting as the catalogue names it: 'mesh.first_cell_height', 'case.alpha_deg'. */
  const settingKey = (info) => (info.stage || 'case') + '.' + info.name;
  const about = (list, key) => (Array.isArray(list) ? list : []).filter((x) => x && typeof x.text === 'string' && x.text && Array.isArray(x.settings) && x.settings.includes(key));
  /* The value as the row shows it, so 0.000679 is not turned into 6.79e-4 beside its own field:
     the views pass the row's text with the mark (`text`); otherwise an editable plan field shows the
     number in full and a list with commas, and everything else shows AS.fmtValue. */
  function valueText(info, btn) {
    const v = info.value;
    if (info.text) return info.text;
    if (info.kind !== 'plan') return AS.fmtValue(v);
    const row = btn && btn.closest ? btn.closest('.plan-row') : null, fixed = row ? row.querySelector('.plan-value') : null;
    if (fixed) return fixed.textContent;
    if (typeof v === 'number') return String(v);
    return Array.isArray(v) ? v.join(', ') : AS.fmtValue(v);
  }

  // ---------- the mark ----------
  const asText = (v) => (typeof v === 'string' ? v : '');
  function mark(info) {
    const n = info.ids.length;
    const btn = h('button', { type: 'button', class: 'cite-btn', dataset: { cite: info.key, key: 'cite:' + info.key },
      'aria-haspopup': 'dialog', 'aria-expanded': cur && cur.key === info.key ? 'true' : 'false',
      'aria-label': `Why ${AS.humanKey(info.name)} is ${valueText(info)}: ${n} reference${n === 1 ? '' : 's'}`, title: 'Why this value',
      onclick: () => toggle(btn) }, AS.icon('ref', 13));
    btn.__cite = info;      // what the pop-up shows; a redraw builds a new button with new data
    return btn;
  }
  function planMark(o) {
    if (!o || !o.name || !Array.isArray(o.path)) return null;
    const ids = idList(o.section && o.section.cites && o.section.cites[o.name]);
    if (!ids.length || (typeof o.reason === 'string' && o.reason.startsWith(AS.ENGINEER_MARK))) return null;
    return mark({ key: 'plan:' + o.path.join('-'), kind: 'plan', stage: o.stage || '', name: String(o.name), path: o.path, value: o.value, reason: o.reason || '', ids, where: o.where || '',
      text: asText(o.text), study: asText(o.study), caseId: asText(o.case) });
  }
  function decisionMark(d, o) {
    const ids = idList(d && d.cites);
    if (!ids.length || !o || !o.key) return null;
    return mark({ key: String(o.key), kind: 'decision', stage: o.stage || '', name: String(d.param == null ? '' : d.param), path: null, value: d.value, reason: d.reason || '', ids, where: o.where || '',
      text: asText(o.text), study: asText(o.study), caseId: asText(o.case) });
  }
  const hasMarks = (decisions) => Array.isArray(decisions) && decisions.some((d) => idList(d && d.cites).length > 0);

  // ---------- the pop-up: one element on <body>, so no region rebuild can destroy it ----------
  let pop = null, parts = null;
  let cur = null;          // { key, info, sig } of the open citation, or null
  let anchor = null;       // the mark it belongs to; replaced when a redraw rebuilds the row
  let frame = 0, timer = 0, wired = false;

  function build() {
    if (pop) return;
    parts = {
      setting: h('span', { class: 'cite-setting' }), value: h('span', { class: 'cite-value' }),
      title: h('span', { id: 'cite-pop-title', class: 'cite-title' }), body: h('div', { class: 'cite-body' }),
      why: h('p', { class: 'cite-why' }),
      sources: h('section', { class: 'cite-sec cite-sources', 'aria-busy': 'false' }),
      runs: h('section', { class: 'cite-sec cite-runs', 'aria-busy': 'false' }),
      foot: h('div', { class: 'cite-foot' }),
    };
    pop = h('div', { id: 'cite-pop', class: 'cite-pop', role: 'dialog', 'aria-labelledby': 'cite-pop-title', tabindex: '-1', hidden: true, onkeydown: onPopKey },
      h('div', { class: 'cite-head' },
        AS.append(parts.title, [parts.setting, h('span', { class: 'cite-eq' }, '='), parts.value]),
        h('button', { type: 'button', class: 'btn icon small cite-close', 'aria-label': 'Close', title: 'Close (Esc)', onclick: () => close(true) }, AS.icon('x', 12))),
      AS.append(parts.body, [h('section', { class: 'cite-sec' }, h('h4', { class: 'cite-h' }, 'Why'), parts.why), parts.sources, parts.runs]),
      parts.foot);
    document.body.appendChild(pop);
  }

  /* Formulas are stored as the document writes them, in LaTeX: a whole passage that is one formula
     (a LaTeX command and no $), or $...$ and $$...$$ inside text. Nothing is typeset or dropped: a
     formula is set in the code face under a label, as in the document viewer. Only the $$ around a
     displayed formula is left out, since it already stands on a line of its own. */
  const TEX = /\\[A-Za-z]+|[\^_]\{/, MATH = /\$\$[^$]+?\$\$|\$(?!\s)[^$\n]+?(?<!\s)\$/g;
  /* One quoted passage: the text as stored, where it stands and, for a terse one, the catalogue's
     note on why it is cited. Terse: it says little without what stands around it in the document
     (a formula, a line that only leads in to the next one, a few words). The note is the studio's
     own text: it stands outside the quote, behind a label that says whose words it is. */
  function quoteItem(p) {
    const text = p.text, bare = !text.includes('$') && TEX.test(text), kids = [];
    let last = 0;
    for (const m of bare ? [] : text.matchAll(MATH)) {
      if (m.index > last) kids.push(text.slice(last, m.index));
      // a displayed formula ($$) keeps a line of its own, without its delimiters
      const shown = m[0].startsWith('$$');
      kids.push(h(shown ? 'div' : 'span', { class: 'cite-math mono' }, shown ? m[0].slice(2, -2).trim() : m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) kids.push(text.slice(last));
    const formula = bare || last > 0, heading = typeof p.heading === 'string' && p.heading ? 'Section: ' + p.heading : '';
    const why = typeof p.why === 'string' && (formula || text.length < TERSE || /:\s*$/.test(text)) ? p.why : '';
    return [h('blockquote', { class: 'cite-quote' + (bare ? ' cite-formula mono' : '') }, kids),
      formula || heading ? h('div', { class: 'cite-quote-at' }, formula ? FORMULA + (heading ? '. ' : '') : '', heading) : null,
      why ? h('div', { class: 'cite-quote-why' }, h('span', { class: 'cite-quote-who' }, 'Studio note:'), ' ', why) : null];
  }

  /* One source, top to bottom: what it is, the studio's one-line summary, what its document says
     about this setting (the stored passages, word for word), where the studio does something else,
     and the two ways on: the document in the reference viewer and the original. */
  function sourceItem(id, setting) {
    const e = get(id);
    if (!e) {
      return h('li', { class: 'cite-src' }, h('span', { class: 'cite-missing' }, id),
        h('p', { class: 'cite-src-says' }, 'This reference is no longer in the catalogue.'));
    }
    const url = AS.verifiedUrl(e), project = String(e.kind).startsWith('project-');
    const quotes = about(e.passages, setting), differs = about(e.differs, setting), more = quotes.length - QUOTES;
    return h('li', { class: 'cite-src' },
      h('div', { class: 'cite-src-title' }, e.title || e.id),
      // a project entry names its evidence as file and section: plain text in the file face, never a link
      h('div', { class: 'cite-src-meta' }, project ? h('span', { class: 'cite-file' }, e.source || '') : e.source || '', ' ', h('span', { class: 'cite-tag' }, KIND.get(e.kind) || String(e.kind || 'source'))),
      e.says ? h('p', { class: 'cite-src-says' }, e.says) : null,
      quotes.length ? h('div', { class: 'cite-from' }, h('h5', { class: 'cite-sub' }, 'From the document'),
        quotes.slice(0, QUOTES).map(quoteItem),
        more > 0 ? h('div', { class: 'cite-more' }, more + ' more in the document') : null) : null,
      differs.length ? h('div', { class: 'cite-differs' }, h('h5', { class: 'cite-sub' }, 'How the studio differs'), differs.map((d) => h('p', {}, d.text))) : null,
      h('div', { class: 'cite-src-links' },
        // the viewer opens at the first passage quoted here; an entry without a local document has its catalogue page only
        e.doc === true ? h('a', { class: 'btn small cite-open cite-doc', href: href(e.id, quotes.length ? quotes[0].key : ''), target: '_blank', rel: 'noopener', title: 'The document, in the reference viewer' }, 'Open document')
          : h('a', { class: 'btn small cite-open', href: href(e.id), target: '_blank', rel: 'noopener' }, 'Open reference'),
        url ? h('a', { class: 'btn small cite-ext', href: url, target: '_blank', rel: 'noopener noreferrer', title: AS.urlHost(url) + ', in a new tab' }, AS.icon('external', 12), 'Original') : null));
  }
  function sameIds(a, b) { return Array.isArray(b) && a.length === b.length && a.every((x, i) => x === b[i]); }
  function drawSources(info) {
    const n = info.ids.length, box = parts.sources;
    const heading = (tag) => h('h4', { class: 'cite-h' }, n === 1 ? 'Reference' : `References (${n})`, tag || null);
    const had = pop.contains(document.activeElement);
    AS.clear(box);
    box.setAttribute('aria-busy', cat || loadFailed ? 'false' : 'true');
    if (cat) {
      const isDefault = sameIds(info.ids, cat.defaults[info.stage + '.' + info.name]), setting = settingKey(info);
      AS.append(box, [heading(isDefault ? h('span', { class: 'cite-tag', title: 'The references of the plan default for this setting' }, 'default') : null),
        h('ol', { class: 'cite-list' }, info.ids.map((id) => sourceItem(id, setting)))]);
    } else if (loadFailed) {
      AS.append(box, [heading(), h('p', { class: 'cite-why' }, 'The reference catalogue could not be loaded. ',
        h('button', { type: 'button', class: 'link-btn', onclick: () => { request(); if (cur) { drawSources(cur.info); place(); } } }, 'Retry')),
        h('p', { class: 'cite-missing' }, info.ids.join(', '))]);
    } else {
      AS.append(box, [heading(), AS.ui.spinner('Loading the references')]);
    }
    // Retry was the focused control and is gone now: the keyboard stays in the pop-up
    if (had && !pop.contains(document.activeElement)) pop.focus({ preventScroll: true });
  }

  // ---------- previous runs: finished cases of other studies that are like this one (GET /api/refs/runs) ----------
  /* Shown, not used: the agents are not told about them. Asked for each time the pop-up opens, through
     AS.api so that the mock answers from its recorded runs. */
  let runs = { sig: '', state: 'idle', data: null, token: 0 };
  /* The request for this citation, or '' when there is no study to compare with. The mark carries
     its study and case (views_setup.js, views_workspace.js); before approval there is no case record
     and the case is named '<study>-<naca>', which the route accepts in place of the id
     (citations.previous_runs). A mark without them: a plan row names its case by position and then
     by its airfoil code, a decision takes the case of the page. */
  function runsUrl(info) {
    const s = AS.state.study;
    if (!s || !s.id || !info.name) return '';
    let caseId = '';
    if (info.caseId && (!info.study || info.study === s.id)) caseId = info.caseId;
    else if (info.kind === 'plan' && Array.isArray(info.path) && info.path[0] === 'cases') {
      const pc = s.plan && Array.isArray(s.plan.cases) ? s.plan.cases[info.path[1]] : null;
      const naca = pc && pc.naca != null ? String(pc.naca) : '';
      const c = naca ? AS.caseList().find((x) => String(x.naca) === naca) : null;
      caseId = c ? String(c.id) : naca;
    } else caseId = pop.dataset.case || '';
    return '/api/refs/runs?setting=' + encodeURIComponent(settingKey(info)) + '&study=' + encodeURIComponent(s.id) + '&case=' + encodeURIComponent(caseId);
  }
  /* '?#study=<id>&case=<id>&view=<stage>' on this page. On the mock (mock and recorded tutorial) the
     mode stays in the address: a recorded run exists only there. */
  const viewOf = (stage) => (stage === 'case' ? 'plan' : /^[\w-]+$/.test(stage || '') ? stage : '');   // alpha is a setting of the case, not of a stage: its page is the plan
  function studyHref(r, stage) {
    if (!r || typeof r.study_id !== 'string' || !/^[\w.-]+$/.test(r.study_id)) return '';
    const mode = AS.mock.enabled ? String(location.search || '').replace(/^\?/, '').split('&').filter((part) => /^(mock|tutorial)(=|$)/.test(part)).join('&') : '';
    const view = viewOf(stage);
    return '?' + mode + '#study=' + r.study_id + (typeof r.case_id === 'string' && r.case_id ? '&case=' + encodeURIComponent(r.case_id) : '') + (view ? '&view=' + view : '');
  }
  /* "Open study" of a previous run. On the live tutorial page it opens a new tab: in this one the
     study would replace the tutorial's, whose cost limit the page watches. Anywhere else the click
     opens the study in place (AS.openStudy, as the Dashboard does); followed as an address, the
     changed query would reload the page and lose the event stream, the viewers and an unsent chat
     text. While the guided tutorial is on, js/tutorial.js recognises the link and keeps it locked. */
  function openLink(r, stage) {
    const link = studyHref(r, stage);
    if (!link) return null;
    if (AS.mock.live) {
      return h('a', { class: 'cite-run-open', href: link, target: '_blank', rel: 'noopener',
        title: 'Opens in a new tab. This page keeps the tutorial study and goes on watching its cost limit.' }, AS.icon('external', 12), 'Open study');
    }
    return h('a', { class: 'cite-run-open', href: link, onclick: (e) => {
      // a new tab or window (modifier key, another button) is the browser's business
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      const view = viewOf(stage), want = {};
      if (typeof r.case_id === 'string' && r.case_id) want.case = r.case_id;
      if (view) want.view = view;
      AS.openStudy(r.study_id, want);        // the pop-up closes on 'reset', once the study has loaded
    } }, 'Open study');
  }
  function runLine(r, info, planValue) {
    const o = r && r.outcome && typeof r.outcome === 'object' ? r.outcome : {};
    const good = AS.truthy(o.success), verdict = typeof o.verdict === 'string' ? o.verdict : '';
    const word = verdict || (good ? 'pass' : 'not finished');
    // the server compares with the plan of the named case; when it could not find that case, the value on screen is compared here
    // (a run without a recorded value is never the same as anything, also not as a setting without one)
    const same = r.same_value === true || (planValue == null && r.value != null && JSON.stringify(r.value) === JSON.stringify(info.value));
    const summary = typeof r.summary === 'string' && r.summary ? r.summary
      : `NACA ${r.naca}, Re ${AS.fmtSci(r.reynolds)}, ${AS.modelLabel(r.model)}: ${AS.fmtValue(r.value)}, ${word}`;
    const facts = [AS.isNum(o.yplus_avg) ? 'y+ ' + AS.fmt(o.yplus_avg) : '', AS.isNum(o.cells) ? AS.fmtInt(o.cells) + ' cells' : '', AS.isNum(o.iterations) ? AS.fmtInt(o.iterations) + ' iterations' : '',
      AS.isNum(o.cl_err_pct) ? 'Cl ' + AS.fmtPct(o.cl_err_pct) : '', AS.isNum(o.cd_err_pct) ? 'Cd ' + AS.fmtPct(o.cd_err_pct) : '',
      AS.isNum(o.gate_failures) ? 'gate failures ' + o.gate_failures : '', AS.isNum(o.rework_loops) ? 'rework loops ' + o.rework_loops : ''].filter(Boolean).join(' · ');
    return h('li', { class: 'cite-run', title: facts || null },
      h('span', { class: 'verdict ' + (good ? (verdict === 'warn' ? 'v-warn' : 'v-pass') : 'v-fail'), role: 'img', 'aria-label': 'Verdict: ' + word }, AS.icon(good ? 'check' : 'x', 12)),
      h('span', { class: 'cite-run-text' }, summary, same ? [' ', h('span', { class: 'cite-tag' }, 'same value')] : null),
      // The mock marks a run it knows from the recorded metrics only (openable false): there is no study to open.
      r.openable === false ? h('span', { class: 'cite-run-open off', title: 'This run is known from the recorded metrics only. There is no study to open.' }, 'Recorded only')
        : openLink(r, info.stage));
  }
  function drawRuns(info) {
    const box = parts.runs, head = h('h4', { class: 'cite-h' }, 'Previous runs');
    const had = pop.contains(document.activeElement);
    AS.clear(box);
    box.setAttribute('aria-busy', runs.state === 'loading' ? 'true' : 'false');
    if (runs.state === 'loading') AS.append(box, [head, AS.ui.spinner('Looking for earlier runs')]);
    else if (runs.state === 'failed') AS.append(box, [head, h('p', { class: 'cite-none' }, 'Earlier runs could not be loaded.')]);
    else {
      const data = runs.data || {}, list = (Array.isArray(data.runs) ? data.runs : []).filter((r) => r && typeof r === 'object').slice(0, RUNS);
      AS.append(box, [head, list.length ? h('ul', { class: 'cite-run-list' }, list.map((r) => runLine(r, info, data.value))) : h('p', { class: 'cite-none' }, NO_RUNS)]);
    }
    if (had && !pop.contains(document.activeElement)) pop.focus({ preventScroll: true });
  }
  /* One request per opening and per value: a redraw of the same row does not ask again. */
  function askRuns(info) {
    const url = runsUrl(info), sig = url + ' ' + JSON.stringify(info.value);
    if (sig === runs.sig) { drawRuns(info); return; }
    const token = runs.token + 1;
    runs = { sig, state: url ? 'loading' : 'ok', data: null, token };
    drawRuns(info);
    if (!url) return;
    const mine = () => token === runs.token && !!cur;        // a late answer for another value, or after closing, is dropped
    AS.api.get(url).then((data) => { if (mine()) { runs.state = 'ok'; runs.data = data; } }, () => { if (mine()) runs.state = 'failed'; })
      .then(() => { if (mine()) { drawRuns(cur.info); place(); } });
  }

  function fill(info) {
    parts.setting.textContent = AS.humanKey(info.name);
    parts.value.textContent = valueText(info, anchor);
    parts.title.title = parts.setting.textContent + ' = ' + parts.value.textContent;    // the strip cuts a long name short
    parts.why.textContent = info.reason || (info.kind === 'decision' ? NO_REASON_DECISION : NO_REASON);
    parts.foot.textContent = info.where;
    parts.foot.hidden = !info.where;
    drawSources(info);
    askRuns(info);
  }
  /* Ask for the catalogue (again, after a failure) and draw the sources when the answer is in. */
  function request() {
    const again = () => { if (cur) { drawSources(cur.info); place(); } };
    load().then(again, again);
  }
  const sigOf = (info) => JSON.stringify([info.name, info.value, info.reason, info.ids, info.where, info.text, info.caseId]);

  /* Text selected in the pop-up can be quoted into the chat: chat.js reads the stage and the case
     from these attributes, which the mark's own surroundings give. */
  function copyOrigin(btn) {
    const stageEl = btn.closest('[data-stage]'), caseEl = btn.closest('[data-case]');
    let stage = stageEl ? stageEl.dataset.stage : '', caseId = caseEl ? caseEl.dataset.case : '';
    const ui = AS.state.ui;
    // as chat.js does for text in the centre pane without a marker of its own
    if (btn.closest('#pane-main')) {
      if (!stage) stage = ui.sel.key || '';
      if (!caseId && ui.sel.kind === 'stage') caseId = ui.caseId || '';
    }
    if (stage) pop.dataset.stage = stage; else delete pop.dataset.stage;
    if (caseId) pop.dataset.case = caseId; else delete pop.dataset.case;
  }

  function expand(btn, on) {
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (on) btn.setAttribute('aria-controls', 'cite-pop');
  }
  function open(btn) {
    const info = btn.__cite;
    if (!info) return;
    build();
    if (!wired) {
      // Registered at the first opening and not at load, so that it runs after the render of app.js:
      // the page has been redrawn when the mark is looked for again.
      wired = true;
      AS.on('render', sync);
    }
    if (anchor && anchor !== btn) expand(anchor, false);
    cur = { key: info.key, info, sig: sigOf(info) };
    anchor = btn;
    expand(btn, true);
    copyOrigin(btn);
    if (!cat) request();                   // the reason shows at once; the sources follow the one request
    runs.sig = '';                         // every opening asks for the earlier runs again
    fill(info);
    pop.hidden = false;
    parts.body.scrollTop = 0;
    place();
    if (!cur) return;                      // the mark was not on screen after all
    pop.focus({ preventScroll: true });
  }
  /* toMark: the keyboard goes back to the mark (Escape, the close button, Tab out of the pop-up). */
  function close(toMark) {
    if (!cur) return;
    const btn = anchor;
    cur = null; anchor = null;
    cancelAnimationFrame(frame); clearTimeout(timer); frame = 0; timer = 0;
    pop.hidden = true;
    if (btn) expand(btn, false);
    if (toMark && btn && btn.isConnected) btn.focus({ preventScroll: true });
  }
  function toggle(btn) {
    // by key, not by node: a redraw may have replaced the button since the pop-up opened
    if (cur && btn.__cite && cur.key === btn.__cite.key) { anchor = btn; close(true); } else open(btn);
  }
  const laidOut = AS.laidOut;
  function findMark(key) {
    // compared as text: a key is not safe to write into a selector
    for (const node of document.querySelectorAll('.cite-btn[data-cite]')) if (node.dataset.cite === key) return node;
    return null;
  }
  /* After a redraw the row may have been rebuilt (a new button), changed, or gone. */
  function sync() {
    if (!cur) return;
    const node = findMark(cur.key);
    if (!laidOut(node) || !node.__cite) {
      // the value is no longer cited (the engineer edited it) or its page is not shown: the keyboard goes to the row's field
      const inside = pop.contains(document.activeElement), path = cur.info.path;
      close(false);
      const fieldEl = inside && path ? document.getElementById('p-' + path.join('-')) : null;
      if (laidOut(fieldEl)) fieldEl.focus({ preventScroll: true });
      return;
    }
    if (node !== anchor) { anchor = node; expand(node, true); }
    const info = node.__cite, sig = sigOf(info);
    cur.info = info;
    if (sig !== cur.sig) { cur.sig = sig; fill(info); }
    later();
  }

  // ---------- placement (position: fixed) ----------
  /* The part of the window in which the mark can be seen: every scrolling ancestor clips it. */
  function clipOf(node) {
    const box = { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight };
    for (let p = node.parentElement; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const r = p.getBoundingClientRect();
      box.top = Math.max(box.top, r.top); box.left = Math.max(box.left, r.left);
      box.right = Math.min(box.right, r.right); box.bottom = Math.min(box.bottom, r.bottom);
    }
    return box;
  }
  function place() {
    if (!cur) return;
    if (!laidOut(anchor)) { sync(); return; }
    const a = anchor.getBoundingClientRect(), clip = clipOf(anchor);
    // the action bar docked at the foot of the page (Approve) is never covered
    const view = anchor.closest('.view'), foot = view && view.querySelector('.sticky-foot');
    let floor = window.innerHeight - MARGIN;
    if (laidOut(foot)) { const top = foot.getBoundingClientRect().top; floor = Math.min(floor, top - GAP); clip.bottom = Math.min(clip.bottom, top); }
    if (a.bottom <= clip.top || a.top >= clip.bottom || a.right <= clip.left || a.left >= clip.right) {
      // scrolled out of its pane: a pop-up that points at nothing is closed
      close(pop.contains(document.activeElement));
      return;
    }
    pop.style.maxHeight = '';
    const w = pop.offsetWidth, natural = pop.offsetHeight;
    const below = floor - (a.bottom + GAP), above = a.top - GAP - MARGIN;
    let height = natural, top;
    if (natural <= below || below >= above) { height = Math.min(natural, Math.max(MIN_HEIGHT, below)); top = a.bottom + GAP; }
    else { height = Math.min(natural, Math.max(MIN_HEIGHT, above)); top = a.top - GAP - height; }
    // inside the window on every side; the right edge sits a little beyond the mark, so the pop-up
    // covers later rows of the same case and not the other airfoil
    top = Math.max(MARGIN, Math.min(top, floor - height));
    const left = Math.max(MARGIN, Math.min(a.right + 6 - w, window.innerWidth - MARGIN - w));
    pop.style.maxHeight = height < natural ? Math.round(height) + 'px' : '';
    pop.style.left = Math.round(left) + 'px';
    pop.style.top = Math.round(top) + 'px';
  }
  /* At most once per frame. Frames stop while the window is covered, hence the timer beside it. */
  function later() {
    if (frame || !cur) return;
    const run = () => { cancelAnimationFrame(frame); clearTimeout(timer); frame = 0; timer = 0; place(); };
    frame = requestAnimationFrame(run);
    timer = setTimeout(run, 60);
  }

  // ---------- closing ----------
  function onPopKey(e) {
    if (e.key !== 'Tab') return;
    // Tab past the last control, or Shift+Tab before the first, leaves the pop-up and closes it
    const stops = AS.$$('a[href], button:not([disabled])', pop).filter(laidOut);
    const at = document.activeElement;
    const leaving = e.shiftKey ? at === pop || at === stops[0] : !stops.length || at === stops[stops.length - 1];
    if (leaving) { e.preventDefault(); close(true); }
  }
  /* Escape belongs to the pop-up while it is open. Capture phase on the window, registered when this
     file loads (before app.js and before the tutorial mounts), so neither the app's own Escape
     handling nor the tutorial's leave prompt reacts to the same key. */
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !cur) return;
    e.preventDefault(); e.stopImmediatePropagation();
    close(true);
  }, true);
  window.addEventListener('pointerdown', (e) => {
    if (!cur) return;
    const t = e.target && e.target.closest ? e.target : null;
    const own = t && t.closest('.cite-btn');
    // the mark closes it with its own click; the quote button and the toasts float over everything
    if (t && (pop.contains(t) || (own && own.dataset.cite === cur.key) || t.closest('#quote-btn, #toasts'))) return;
    close(false);                          // focus stays where the click puts it
  }, true);
  window.addEventListener('scroll', (e) => { if (cur && !(e.target && e.target.nodeType === 1 && pop.contains(e.target))) later(); }, true);
  window.addEventListener('resize', later);
  AS.on('reset', () => close(false));      // another study: nothing of the old one stays open

  if (document.body) build();              // the scripts sit at the end of <body>

  AS.cite = { planMark, decisionMark, hasMarks, close: () => close(false) };
})();
