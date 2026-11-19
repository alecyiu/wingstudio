/* Wing Studio - chat / activity pane: a readable transcript of the event
   stream, the message composer (always enabled; messages are queued for the
   agent's next tool boundary) and quote-to-chat for any selected text. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, state = AS.state;
  const V = AS.views;
  const STAGE_KEYS = AS.STAGE_DEFS.map((d) => d.key);

  V.chat = (function () {
    let el, head, log, jump, pendingBox, quotesBox, box, target, strip;
    let rendered = 0, filterKey = '', stick = true, unseen = 0, unread = 0;
    let quotes = [], chosenTarget = '', offMainTarget = '', lastTab = '', draftFor = null;
    let pendingTools = {}, progressRows = {}, approvalCards = {};
    let pos = {}, anchor = null;        // reading position per filter key; the one still to be put back
    let counted = 0, liveFrom = 0;      // unread: events already counted; index of the first live event
    let wasCollapsed = false;

    const agentName = (ev) => (ev.agent ? String(ev.agent).replace(/\.md$/, '') : 'agent');
    const caseTag = (ev) => {
      const c = ev.case_id && state.cases[ev.case_id];
      return c ? 'NACA ' + c.naca + (ev.stage ? ' · ' + AS.stageTitle(ev.stage) : '') : ev.stage ? AS.stageTitle(ev.stage) : '';
    };

    /* The chosen scope holds on every tab: tied to the tab, the transcript would be rebuilt and lose
       its reading position on every switch. */
    function scope() {
      return AS.caseList().length ? state.ui.chatScope : 'all';
    }
    function matches(ev) {
      const ui = state.ui, sc = scope();
      if (sc === 'all') return true;
      if (sc === 'case') return !ev.case_id || ev.case_id === ui.caseId;
      if (ui.sel.kind !== 'stage') return !ev.case_id;
      return ev.case_id === ui.caseId && (ev.stage === ui.sel.key || (ev.data && (ev.data.target_stage === ui.sel.key || ev.data.stage === ui.sel.key || ev.data.to === ui.sel.key)));
    }

    function row(cls, ev, ...content) {
      const tag = scope() === 'all' ? caseTag(ev) : '';   // within one case the stage dividers already say where we are
      return h('div', { class: 'msg ' + cls, dataset: { seq: ev.seq, case: ev.case_id || '', stage: ev.stage || '' } },
        h('div', { class: 'msg-meta' }, tag ? h('span', { class: 'msg-tag' }, tag) : null, h('time', {}, AS.fmtClock(ev.ts))), content);
    }
    const sys = (ev, text, cls) => row('sys ' + (cls || ''), ev, h('span', {}, text));

    function argsLine(args) {
      const parts = Object.entries(args || {}).filter(([, v]) => v !== undefined).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`);
      const line = parts.join(', ');
      return line.length > 70 ? line.slice(0, 67) + '…' : line;
    }

    function checksList(checks) {
      return h('ul', { class: 'checks' }, (checks || []).map((ch) => h('li', { class: ch.ok ? 'ok' : 'bad' },
        h('span', { class: 'verdict ' + (ch.ok ? 'v-pass' : 'v-fail'), role: 'img', 'aria-label': ch.ok ? 'pass' : 'fail' }, AS.icon(ch.ok ? 'check' : 'x', 12)), `${ch.name}: `,
        h('b', {}, AS.fmtValue(ch.value)), h('span', { class: 'muted' }, ` (limit ${AS.fmtValue(ch.limit)})`))));
    }

    /* One event -> one transcript element (or an update of an earlier element). */
    function add(ev) {
      const d = ev.data || {};
      switch (ev.type) {
        case 'agent.message':
          return row('agent', ev, h('div', { class: 'who' }, agentName(ev)), AS.md(d.text));
        case 'steering.reply':
          return row('agent steering', ev, h('div', { class: 'who' }, 'steering'), AS.md(d.text));
        case 'human.message':
          return row('human', ev, h('div', { class: 'who' }, 'You', d.target_stage ? h('span', { class: 'muted' }, ' → ' + AS.stageTitle(d.target_stage)) : null),
            d.quote ? h('blockquote', {}, typeof d.quote === 'string' ? d.quote : AS.fmtValue(d.quote)) : null, h('p', {}, d.text));
        case 'tool.call': {
          const status = h('span', { class: 'tool-result' }, h('i', { class: 'spinner' }), ' running');
          const node = row('tool pending', ev, h('span', { class: 'tool-name mono' }, d.name), h('span', { class: 'tool-args mono' }, '(' + argsLine(d.args) + ')'), status,
            h('pre', { class: 'only-verbose mono' }, JSON.stringify(d.args || {}, null, 1).slice(0, 1500)));
          const key = (ev.case_id || '') + '|' + d.name;
          (pendingTools[key] = pendingTools[key] || []).push({ node, status });
          return node;
        }
        case 'tool.result': {
          const key = (ev.case_id || '') + '|' + d.name;
          const open = (pendingTools[key] || []).shift();
          const text = [AS.icon(d.ok === false ? 'x' : 'arrow-right', 11), d.summary || (d.ok === false ? 'failed' : 'ok')];
          const ms = AS.isNum(d.ms) ? h('span', { class: 'muted' }, ' · ' + AS.fmtSeconds(d.ms / 1000)) : null;
          if (open) {
            open.node.classList.remove('pending');
            if (d.ok === false) open.node.classList.add('failed');
            AS.clear(open.status); AS.append(open.status, [text, ms]);
            return null;
          }
          // the call was outside the current filter or before the replay window
          return row('tool' + (d.ok === false ? ' failed' : ''), ev, h('span', { class: 'tool-name mono' }, d.name), h('span', { class: 'tool-result' }, text, ms));
        }
        case 'llm.call':
          return row('sys only-verbose', ev, h('span', { class: 'mono' }, `model call · ${AS.modelLabel(d.model)} · ${AS.fmtTokens(d.tokens_in)} in (${AS.fmtTokens(d.tokens_cached)} cached) / ${AS.fmtTokens(d.tokens_out)} out · ${AS.fmtSeconds((d.ms || 0) / 1000)} · ${AS.fmtCost(d.cost_usd)}`));
        case 'stage.phase':
          return row('sys only-verbose', ev, h('span', {}, `phase: ${d.phase}`));
        case 'stage.started':
          if (ev.stage === 'solve') delete progressRows[ev.case_id];
          return row('divider', ev, h('span', {}, `${AS.stageTitle(ev.stage)} started` + (d.attempt > 1 ? ` · attempt ${d.attempt}` : '')));
        case 'stage.completed':
          return row('card ok', ev, h('div', { class: 'card-title' }, AS.icon('check', 13), `${AS.stageTitle(ev.stage)} passed`, d.headline ? h('span', { class: 'muted' }, ' · ' + d.headline) : null,
            AS.isNum(d.seconds) ? h('span', { class: 'muted' }, ' · ' + AS.fmtSeconds(d.seconds)) : null), d.summary ? h('p', {}, d.summary) : null);
        case 'stage.failed':
          return row('card bad', ev, h('div', { class: 'card-title' }, AS.icon('x', 13), `${AS.stageTitle(ev.stage)} failed`), h('p', {}, d.reason || ''));
        case 'stage.rework':
          return row('card warn', ev, h('div', { class: 'card-title' }, AS.icon('redo', 13), `Rework: back to ${AS.stageTitle(d.to)}` + (d.from && d.from !== d.to ? ` from ${AS.stageTitle(d.from)}` : '')), h('p', {}, d.reason || ''));
        case 'gate.result':
          return row('card ' + (d.passed ? 'ok' : 'bad'), ev, h('div', { class: 'card-title' }, AS.icon(d.passed ? 'check' : 'x', 13), `Gate ${d.passed ? 'passed' : 'failed'} · ${AS.stageTitle(ev.stage)}`), checksList(d.checks));
        case 'approval.requested': {
          const stage = d.stage || ev.stage, c = state.cases[ev.case_id];
          const stillWaiting = c && c.status === 'awaiting_approval' && c.current_stage === stage;
          const action = h('div', { class: 'card-actions' }, stillWaiting
            ? h('button', { type: 'button', class: 'btn primary small', onclick: (e) => { e.target.disabled = true; AS.actions.approveStage(ev.case_id); } }, 'Approve stage')
            : h('span', { class: 'muted' }, 'answered'));
          approvalCards[ev.case_id + '|' + stage] = action;
          return row('card warn', ev, h('div', { class: 'card-title' }, AS.icon('alert', 13), `Approval needed · ${AS.stageTitle(stage)}`), h('p', {}, d.reason || ''), action,
            h('button', { type: 'button', class: 'link-btn', onclick: () => { AS.selectCase(ev.case_id); AS.select({ kind: 'stage', key: stage }); } }, 'Show the stage'));
        }
        case 'approval.granted': {
          const key = ev.case_id + '|' + (d.stage || ev.stage), action = approvalCards[key];
          if (action) { AS.clear(action); action.appendChild(h('span', { class: 'muted' }, 'approved')); delete approvalCards[key]; }
          return sys(ev, `Approved: ${AS.stageTitle(d.stage || ev.stage)}`);
        }
        case 'solver.progress': {
          const text = `simpleFoam · iteration ${AS.fmtInt(d.iteration)}${d.total ? ' / ' + AS.fmtInt(d.total) : ''}` +
            (AS.isNum(d.cl) ? ` · Cl ${d.cl.toFixed(4)}` : '') + (AS.isNum(d.cd) ? ` · Cd ${d.cd.toFixed(5)}` : '');
          const pct = d.total ? Math.min(100, d.iteration / d.total * 100) : 0;
          // no isConnected test: during a rebuild the row is still in the fragment, and the map is
          // cleared on every rebuild and when a solve starts, so an entry is always the current row
          const existing = progressRows[ev.case_id];
          if (existing) { existing.text.textContent = text; existing.bar.style.width = pct + '%'; return null; }
          const label = h('span', { class: 'mono' }, text), bar = h('i', { style: { width: pct + '%' } });
          const node = row('progress-row', ev, label, h('span', { class: 'progress' }, bar));
          progressRows[ev.case_id] = { node, text: label, bar };
          return node;
        }
        case 'artifact.created':
          return row('sys artifact', ev, d.kind === 'image'
            ? h('button', { type: 'button', class: 'snap small', onclick: () => AS.lightbox(d.url, d.label) }, h('img', { src: d.url, alt: d.label || 'snapshot', loading: 'lazy' }), h('span', {}, d.label || 'snapshot'))
            : h('a', { href: d.url, target: '_blank', rel: 'noopener' }, AS.icon('download', 12), d.label || d.url));
        case 'case.created':
          return sys(ev, `Case created: NACA ${d.case && d.case.naca} at ${AS.fmt(d.case && d.case.alpha_deg, 2)}°`);
        case 'case.status':
          return sys(ev, `Case ${AS.ui.statusText(d.status)}`, d.status === 'failed' ? 'bad' : '');
        case 'case.completed': {
          const r = d.results || {}, dl = d.deltas || {};
          return row('card ' + (d.verdict === 'fail' ? 'bad' : d.verdict === 'warn' ? 'warn' : 'ok'), ev,
            h('div', { class: 'card-title' }, `Case finished · ${caseTag({ case_id: ev.case_id })} `, AS.ui.verdict(d.verdict)),
            h('p', { class: 'mono' }, `Cl ${AS.fmt(r.cl, 4)} · Cd ${AS.isNum(r.cd) ? r.cd.toFixed(5) : '–'} · L/D ${AS.fmt(r.ld, 1)} · dCl ${AS.fmtPct(dl.cl_pct)} · dCd ${AS.fmtPct(dl.cd_pct)} vs XFoil`));
        }
        case 'study.status':
          return sys(ev, `Study: ${AS.ui.statusText(d.status)}` + (d.mode ? ` · mode ${d.mode}` : ''));
        case 'requirements.parsed':
          return sys(ev, 'Mission understood: ' + AS.ui.missionChip(d.requirements || {}));
        case 'candidates.proposed':
          return sys(ev, `${(d.candidates || []).length} candidates proposed: ` + (d.candidates || []).map((cd) => 'NACA ' + cd.naca + (cd.selected ? ' (top)' : '')).join(', '));
        case 'plan.created': return sys(ev, `Plan v${d.version} written. Review it and approve, edit or comment.`);
        case 'plan.updated': return sys(ev, `Plan updated to v${d.version}.`);
        case 'plan.approved': return sys(ev, `Plan v${d.version} approved` + (d.mode ? ` · ${d.mode === 'gate' ? 'gate each stage' : 'auto-run'}` : ''));
        case 'human.intervention':
          return sys(ev, 'Human intervention: ' + AS.humanKey(d.kind || ''), 'intervention');
        case 'study.completed':
          // always emitted when the pipelines end; without a finished case there is no report
          if (!d.report) {
            return row('card warn', ev, h('div', { class: 'card-title' }, 'Study ended without a report'),
              h('p', {}, 'No case finished, so no report or recommendation was written. Finished stages and files are kept: re-run a stage to continue.'),
              h('div', { class: 'card-actions' }, h('button', { type: 'button', class: 'btn small', onclick: () => AS.setTab('compare') }, 'Compare')));
          }
          return row('card ok', ev, h('div', { class: 'card-title' }, 'Study complete'), h('p', {}, AS.recommendationText(d.report) || 'The report is ready.'),
            h('div', { class: 'card-actions' }, h('button', { type: 'button', class: 'btn small primary', onclick: () => AS.select({ kind: 'step', key: 'report' }) }, 'Open report'),
              h('button', { type: 'button', class: 'btn small', onclick: () => AS.setTab('compare') }, 'Compare'), h('button', { type: 'button', class: 'btn small', onclick: () => AS.setTab('dashboard') }, 'Dashboard')));
        case 'error':
          return row('card bad', ev, h('div', { class: 'card-title' }, 'Error'), h('p', {}, d.message || ''));
        default:
          // unknown event types stay visible instead of being dropped
          return sys(ev, `${ev.type} ${JSON.stringify(d).slice(0, 160)}`);
      }
    }

    function scrollToEnd() { log.scrollTop = log.scrollHeight; stick = true; unseen = 0; jump.hidden = true; }

    /* Where the reader is in the current filter: at the end, or at a row (by seq,
       which survives a rebuild) and how far that row sits from the top of the log. */
    function readPos() {
      if (anchor) return anchor;        // restored while collapsed and never shown: still the position to return to
      const p = { stick, unseen, at: rendered, seq: null, off: 0 };
      if (stick) return p;
      const top = log.getBoundingClientRect().top;
      for (const node of log.children) {
        const r = node.getBoundingClientRect();
        if (r.height && r.bottom > top) { p.seq = node.dataset.seq; p.off = r.top - top; break; }
      }
      return p;
    }
    function restorePos(p) {
      const node = Array.prototype.find.call(log.children, (n) => n.dataset.seq === p.seq);
      const r = node && node.getBoundingClientRect();
      if (r && r.height) log.scrollTop += r.top - log.getBoundingClientRect().top - p.off;
      else scrollToEnd();               // the row is gone or hidden
    }

    function send() {
      const text = box.value.trim();
      if (!text && !quotes.length) return;
      if (!state.study) { AS.toast('Start a study first: there is no agent to talk to yet.', 'info'); return; }
      const ui = state.ui, message = { text: text || 'See the quoted text.' };
      if (target.value === 'case' && ui.caseId) {
        message.case_id = ui.caseId;
        if (ui.sel.kind === 'stage') message.stage = ui.sel.key;
      }
      if (quotes.length) message.quote = quotes.map((q) => (q.label ? `[${q.label}] ` : '') + q.text).join('\n');
      box.value = '';
      quotes = [];
      AS.actions.sendMessage(message);
      scrollToEnd();
      render();
    }

    AS.addQuote = function (quote) {
      quotes.push(quote);
      if (state.ui.chatCollapsed) { state.ui.chatCollapsed = false; AS.store.set('chat', false); }
      render();
      box.focus();
    };

    function build(root) {
      el = root;
      head = h('div', { class: 'chat-head' });
      log = h('div', { class: 'chat-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Activity transcript', tabindex: 0 });
      jump = h('button', { type: 'button', class: 'jump', hidden: true, onclick: scrollToEnd }, 'Jump to latest');
      pendingBox = h('div', { class: 'pending' });
      quotesBox = h('div', { class: 'quotes' });
      box = h('textarea', { id: 'composer', rows: 2, placeholder: 'Message the agents. Enter sends, Shift+Enter adds a line.', 'aria-label': 'Message to the agents',
        oninput: () => { draftFor = state.study ? state.study.id : null; },
        onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } } });
      target = h('select', { id: 'composer-target', 'aria-label': 'Send to', onchange: (e) => { if (state.ui.tab !== 'main') offMainTarget = e.target.value; else chosenTarget = e.target.value; } });
      strip = h('button', { type: 'button', class: 'chat-strip', title: 'Open the chat ( ] )', onclick: () => { state.ui.chatCollapsed = false; AS.store.set('chat', false); AS.invalidate(); } });
      AS.append(el, [strip, head, h('div', { class: 'chat-log-wrap' }, log, jump), pendingBox,
        h('div', { class: 'composer' }, quotesBox, box, h('div', { class: 'composer-row' }, h('label', { class: 'muted', for: 'composer-target' }, 'To'), target,
          h('button', { type: 'button', class: 'btn primary small', id: 'send-btn', onclick: send }, 'Send')))]);
      // a change of the panel's size is not a scroll: a log that was at its end stays there
      new ResizeObserver(() => { if (stick && log.clientHeight) log.scrollTop = log.scrollHeight; }).observe(log);
      log.addEventListener('scroll', () => {
        if (!log.clientHeight) return;  // collapsed: nothing to measure, and "at the end" must not be assumed
        stick = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
        if (stick) { unseen = 0; jump.hidden = true; }
      });
      // events replayed after a load are history: they must not count as unread
      AS.on('event', (ev, historical) => { if (historical) liveFrom = state.events.length; });
      AS.on('reset', () => {
        rendered = 0; filterKey = ''; quotes = []; unread = 0; chosenTarget = ''; offMainTarget = '';
        pos = {}; anchor = null; counted = 0; liveFrom = 0;
        // a draft belongs to the study it was typed in and must not be sent to the next one;
        // text typed before any study is kept for the study that opens next
        if (draftFor) { box.value = ''; draftFor = null; }
        else if (state.study && box.value) draftFor = state.study.id;
      });
    }

    function render() {
      const ui = state.ui, s = state.study, c = AS.currentCase();
      const opened = wasCollapsed && !ui.chatCollapsed, closed = !wasCollapsed && !!ui.chatCollapsed;
      const hadFocus = el.contains(document.activeElement);
      wasCollapsed = !!ui.chatCollapsed;
      el.closest('.app').classList.toggle('chat-collapsed', wasCollapsed);
      // collapsing hides the button that was pressed: the keyboard moves to the strip that reopens the panel
      if (closed && hadFocus) strip.focus({ preventScroll: true });
      const sc = scope();
      // data-key: lets AS.region put keyboard focus back on the same control after the rebuild its own click causes
      AS.region(head, [sc, ui.chatScope, ui.chatVerbose, AS.caseList().length > 0], () => [
        h('span', { class: 'chat-title' }, 'Activity'),
        AS.caseList().length ? h('div', { class: 'segmented small', role: 'group', 'aria-label': 'Which events to show' },
          [['stage', 'Stage'], ['case', 'Case'], ['all', 'All']].map(([k, label]) => h('button', { type: 'button', class: 'seg-btn' + (ui.chatScope === k ? ' on' : ''), dataset: { key: 'scope:' + k },
            'aria-pressed': ui.chatScope === k ? 'true' : 'false', onclick: () => { ui.chatScope = k; AS.invalidate(); } }, label))) : h('span', { class: 'muted small' }, 'all events'),
        h('label', { class: 'check small', title: 'Also show model calls, phases and full tool arguments' },
          h('input', { type: 'checkbox', checked: ui.chatVerbose, dataset: { key: 'verbose' }, onchange: (e) => { ui.chatVerbose = e.target.checked; AS.invalidate(); } }), 'verbose'),
        h('button', { type: 'button', class: 'btn small icon', id: 'chat-toggle', title: 'Collapse the chat ( ] )', 'aria-label': 'Collapse chat', onclick: () => { ui.chatCollapsed = true; AS.store.set('chat', true); AS.invalidate(); } }, AS.icon('chevrons-right')),
      ]);
      if (opened && hadFocus) { const t = AS.$('#chat-toggle', el); if (t) t.focus({ preventScroll: true }); }
      log.classList.toggle('verbose', !!ui.chatVerbose);

      // rebuild when the filter changes, otherwise only append what is new
      const key = JSON.stringify([s && s.id, sc, sc !== 'all' && ui.caseId, sc === 'stage' && ui.sel]);
      const rebuilt = key !== filterKey;
      if (rebuilt) {
        // each filter keeps its reading position, so another case or stage and back returns to the same row
        if (filterKey) pos[filterKey] = readPos();
        const saved = pos[key];
        filterKey = key; rendered = 0; pendingTools = {}; progressRows = {}; approvalCards = {};
        AS.clear(log);
        if (!s) log.appendChild(h('div', { class: 'chat-empty' }, h('p', {}, 'Everything the agents do appears here: what they say, each tool call with its result, gate checks, reworks and your own messages.'),
          h('p', {}, 'Select text anywhere on the page to quote it into a message.')));
        stick = saved ? saved.stick : true;
        unseen = stick ? 0 : saved.unseen;
        anchor = stick ? null : saved;
      }
      let added = 0, fresh = 0, live = 0;
      const fragment = document.createDocumentFragment();
      for (; rendered < state.events.length; rendered++) {
        const ev = state.events[rendered];
        if (!matches(ev)) continue;
        const node = add(ev);
        if (!node) continue;
        fragment.appendChild(node); added++;
        // new to the reader: everything when appending, after a rebuild only what came since this filter was left
        if (!rebuilt || (anchor && rendered >= anchor.at)) fresh++;
        // unread: a rebuild and the replay after a load repeat history, so only live events seen for the first time count
        if (rendered >= counted && rendered >= liveFrom) live++;
      }
      counted = rendered;
      if (added) log.appendChild(fragment);
      if (ui.chatCollapsed) unread += live;
      // a collapsed log has no box: the position is put back, or the end shown, when the pane reopens
      if (anchor && log.clientHeight) { restorePos(anchor); anchor = null; }
      if (stick) { if (added || rebuilt || opened) scrollToEnd(); }
      else if (fresh || rebuilt) { unseen += fresh; jump.hidden = !unseen; AS.clear(jump); AS.append(jump, [AS.icon('arrow-down', 12), `${unseen} new · jump to latest`]); }
      if (!ui.chatCollapsed) unread = 0;
      AS.region(strip, [unread], () => [h('span', { class: 'strip-label' }, 'Activity'), unread ? h('span', { class: 'badge' }, unread > 99 ? '99+' : unread) : null]);

      AS.region(pendingBox, [state.pending.map((p) => [p.localId, p.text])], () => state.pending.map((p) => h('div', { class: 'msg human queued', title: 'Delivered to the agent at its next tool boundary' },
        h('div', { class: 'who' }, 'You ', h('span', { class: 'muted' }, '· queued, waiting for the next tool boundary')),
        p.quote ? h('blockquote', {}, p.quote) : null, h('p', {}, p.text))));
      AS.region(quotesBox, [quotes.map((q) => q.text + q.label)], () => quotes.map((q, i) => h('span', { class: 'quote-chip' },
        h('span', { class: 'quote-src' }, q.label || 'page'), h('span', { class: 'quote-text' }, q.text.length > 80 ? q.text.slice(0, 77) + '…' : q.text),
        h('button', { type: 'button', 'aria-label': 'Remove quote', title: 'Remove quote', onclick: () => { quotes.splice(i, 1); render(); } }, AS.icon('x', 11)))));
      const stageLabel = c ? 'NACA ' + c.naca + (ui.sel.kind === 'stage' ? ' · ' + AS.stageTitle(ui.sel.key) : '') : 'the study';
      AS.region(target, [stageLabel, !!c, !!s, ui.tab], () => (c ? [h('option', { value: 'case' }, stageLabel), h('option', { value: 'all' }, 'All running cases')] : [h('option', { value: 'all' }, s ? 'The study agents' : 'No study yet')]));
      // default: the selected case and stage on a stage page; "all cases" on Compare, Dashboard and the
      // study-level pages, which belong to no single case. A choice made by hand wins until the tab
      // changes (it is kept apart for the two kinds of tab, so neither overwrites the other).
      if (ui.tab !== lastTab) { lastTab = ui.tab; offMainTarget = ''; }
      if (c) target.value = ui.tab !== 'main' ? offMainTarget || 'all' : chosenTarget || (ui.sel.kind === 'stage' ? 'case' : 'all');
    }
    return { build, render };
  })();

  // =====================================================================
  // Quote-to-chat: a small button near any text selection
  // =====================================================================
  (function quoteToChat() {
    const btn = AS.$('#quote-btn');
    let current = null;

    function origin(node) {
      const elem = node.nodeType === 1 ? node : node.parentElement;
      if (!elem) return {};
      const stageEl = elem.closest('[data-stage]'), caseEl = elem.closest('[data-case]');
      let stage = stageEl && stageEl.dataset.stage, caseId = caseEl && caseEl.dataset.case;
      const ui = state.ui;
      // text inside the centre pane without its own marker belongs to what is selected there
      if (!stage && elem.closest('#pane-main')) stage = ui.sel.key;
      if (!caseId && elem.closest('#pane-main') && ui.sel.kind === 'stage') caseId = ui.caseId;
      if (!stage && elem.closest('#pane-compare')) stage = 'compare';
      if (!stage && elem.closest('#pane-dashboard')) stage = 'dashboard';
      const c = caseId && state.cases[caseId];
      const names = { compare: 'Compare', dashboard: 'Dashboard', report: 'Report' };
      const label = [c ? 'NACA ' + c.naca : null, stage ? names[stage] || AS.stageTitle(stage) : null].filter(Boolean).join(' · ');
      return { case_id: caseId || null, stage: STAGE_KEYS.includes(stage) ? stage : null, label };
    }

    function update() {
      const sel = window.getSelection();
      const text = sel && !sel.isCollapsed ? sel.toString().trim() : '';
      const anchor = sel && sel.anchorNode && (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement);
      if (!text || text.length < 2 || !anchor || anchor.closest('.composer, input, textarea, select, #quote-btn, #toasts')) { btn.hidden = true; current = null; return; }
      const rect = sel.getRangeAt(sel.rangeCount - 1).getBoundingClientRect();
      if (!rect.width && !rect.height) { btn.hidden = true; return; }
      current = Object.assign({ text: text.replace(/\s+/g, ' ').slice(0, 600) }, origin(sel.anchorNode));
      btn.hidden = false;
      const bw = btn.offsetWidth || 110;
      btn.style.left = Math.max(4, Math.min(window.innerWidth - bw - 4, rect.right - bw / 2)) + 'px';
      btn.style.top = (rect.top > 40 ? rect.top - 32 : rect.bottom + 6) + 'px';
    }
    const later = AS.debounce(update, 180);
    document.addEventListener('selectionchange', later);
    document.addEventListener('mouseup', later);
    document.addEventListener('scroll', () => { if (!btn.hidden) later(); }, true);
    // a redraw or a resized window can move the selected text without any of the events above
    AS.on('render', () => { if (!btn.hidden) later(); });
    window.addEventListener('resize', () => { if (!btn.hidden) later(); });
    btn.addEventListener('mousedown', (e) => e.preventDefault());    // keep the selection alive while clicking
    btn.addEventListener('click', () => {
      if (!current) return;
      AS.addQuote(current);
      btn.hidden = true; current = null;
      const sel = window.getSelection();
      if (sel) sel.removeAllRanges();
    });
  })();
})();
