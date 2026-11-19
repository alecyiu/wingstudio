/* Wing Studio - the workspace chrome and the per-stage views:
   top bar, stage rail, viewer, stage report, status bar and the Compare tab. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, state = AS.state;
  const V = AS.views;
  const SVG = 'http://www.w3.org/2000/svg';

  const ICON = { awaiting_approval: 'alert', passed: 'check', failed: 'x', stale: 'redo', done: 'check', stopped: 'stop', paused: 'pause' };
  const icon = (status) => h('span', { class: 'node-icon st-' + status, 'aria-hidden': 'true' }, ICON[status] ? AS.icon(ICON[status], 13) : null);

  AS.lightbox = function (url, label) {
    const box = AS.$('#lightbox');
    AS.clear(box);
    box.appendChild(h('figure', {}, h('img', { src: url, alt: label || '' }), h('figcaption', {}, label || '', ' ',
      h('a', { href: url, target: '_blank', rel: 'noopener' }, 'open original'), ' ',
      h('button', { type: 'button', class: 'btn small', onclick: AS.closeLightbox }, 'Close'))));
    lightboxOpener = document.activeElement;
    box.hidden = false;
    box.onclick = (e) => { if (e.target === box) AS.closeLightbox(); };
    AS.$('button', box).focus();
  };
  /* Closing returns the keyboard to the thumbnail or link that opened the snapshot. */
  let lightboxOpener = null;
  AS.closeLightbox = function () {
    AS.$('#lightbox').hidden = true;
    // the thumbnail may have been rebuilt while the snapshot was open: fall back to a control that is always there
    const onScreen = (n) => (AS.laidOut(n) ? n : null);
    const back = onScreen(lightboxOpener) || onScreen(AS.$('#viewer-toggle')) || AS.$('#doctabs .tab.active');
    if (back) back.focus();
    lightboxOpener = null;
  };

  /* Hand-off metrics of a stage, but only while they describe the current run: a stage that is
     stale, failed or running again still carries the metrics of its previous run. */
  AS.passedMetrics = function (c, key) {
    const st = AS.stageOf(c, key);
    return st && st.status === 'passed' ? st.metrics || {} : {};
  };
  /* Numbers for one case from whatever is available: the final case result, or
     the hand-offs of the solve, post and validate stages once each has passed. After a
     re-run (results cleared, stages stale) the numbers of the earlier run are not shown. */
  AS.caseNumbers = function (c) {
    const post = AS.passedMetrics(c, 'post'), solve = AS.passedMetrics(c, 'solve'), val = AS.passedMetrics(c, 'validate');
    const pick = (...values) => values.find((v) => AS.isNum(v));
    const r = c.results || {}, ref = c.reference || {};
    const cfd = { cl: pick(r.cl, post.cl, solve.cl), cd: pick(r.cd, post.cd, solve.cd), cm: pick(r.cm, post.cm) };
    cfd.ld = pick(r.ld, post.ld, cfd.cl != null && cfd.cd ? cfd.cl / cfd.cd : undefined);
    const xf = { cl: pick(ref.cl, val.xfoil_cl), cd: pick(ref.cd, val.xfoil_cd), cm: pick(ref.cm, val.xfoil_cm) };
    xf.ld = pick(ref.ld, xf.cl != null && xf.cd ? xf.cl / xf.cd : undefined);
    const d = c.deltas || {};
    const pct = (a, b) => (AS.isNum(a) && AS.isNum(b) && b !== 0 ? (a - b) / b * 100 : undefined);
    const deltas = { cl_pct: pick(d.cl_pct, val.cl_pct, pct(cfd.cl, xf.cl)), cd_pct: pick(d.cd_pct, val.cd_pct, pct(cfd.cd, xf.cd)),
      // absolute lift difference: what is shown when the reference lift is zero and there is no percentage
      cl_abs: pick(d.cl_abs, val.cl_abs, AS.isNum(cfd.cl) && AS.isNum(xf.cl) ? cfd.cl - xf.cl : undefined),
      cm_abs: pick(d.cm_abs, val.cm_abs, AS.isNum(cfd.cm) && AS.isNum(xf.cm) ? Math.abs(cfd.cm - xf.cm) : undefined),
      ld_pct: pick(d.ld_pct, pct(cfd.ld, xf.ld)), farfield_cd_estimate: d.farfield_cd_estimate };
    return { cfd, xf, deltas, verdict: c.verdict || val.verdict || null };
  };
  /* The lift difference as text: a percentage, or the absolute difference for a zero reference lift. */
  AS.fmtClDelta = (deltas) => (AS.isNum(deltas.cl_pct) ? AS.fmtPct(deltas.cl_pct)
    : AS.isNum(deltas.cl_abs) ? (deltas.cl_abs >= 0 ? '+' : '−') + Math.abs(deltas.cl_abs).toFixed(4) + ' abs' : AS.fmtPct(deltas.cl_pct));
  /* XFoil screening numbers of the candidate this case was built from (available before validation). */
  function screening(c) {
    const cand = ((state.study && state.study.candidates) || []).find((cd) => cd.naca === c.naca);
    return (cand && cand.xfoil) || {};
  }
  function needsAttention(c) { return c.status === 'awaiting_approval' || c.status === 'failed'; }
  /* Surface pressure curves of the current run: OpenFOAM once Post-process has passed,
     XFoil once Validate has passed (the loaded series may still hold an earlier run). */
  AS.cpData = function (c) {
    const sr = state.series[c.id] || {};
    const passed = (key) => (AS.stageOf(c, key) || {}).status === 'passed';
    return { cp: passed('post') ? sr.cp || null : null, xfoilCp: passed('validate') ? sr.xfoilCp || null : null };
  };
  /* The per-case fallback: the study is in auto-run, but after a failed stage this case
     asks before every further stage. POST /mode {auto} clears it. */
  AS.fallbackGated = function (c) {
    const s = state.study, st = c && AS.stageOf(c, c.current_stage);
    return !!(s && s.mode === 'auto' && c && c.status === 'awaiting_approval' && st && st.status !== 'failed');
  };
  /* Did this case meet the mission's target L/D in the RANS result? The backend judges it
     (report.table[].meets_target_ld); undefined while it has not, or for studies without a target. */
  AS.targetLdFlag = function (c) {
    const s = state.study, rows = (s && s.report && Array.isArray(s.report.table) && s.report.table) || [];
    const row = rows.find((r) => r && (r.case_id === c.id || (r.case_id == null && String(r.naca) === String(c.naca))));
    for (const src of [row, c.results, c]) if (src && typeof src.meets_target_ld === 'boolean') return src.meets_target_ld;
    return undefined;
  };

  // =====================================================================
  // Top bar
  // =====================================================================
  V.topbar = (function () {
    let el, project, mission, tabs, mode, totals, controls, stopArmed = false, stopTimer = null;

    function build(root) {
      el = root;
      project = h('div', { class: 'tb-project' });
      mission = h('div', { class: 'tb-mission' });
      tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Views' });
      mode = h('div', { class: 'tb-mode' });
      totals = h('div', { class: 'tb-totals', 'aria-label': 'Running totals' });
      controls = h('div', { class: 'tb-controls' });
      // Last cell, built once: the way into the guided tutorial, and inside it the way to start over
      // or leave. AS.tutorial (js/tutorial.js) loads after this file, so it is looked up on the click.
      const exitBtn = () => h('button', { type: 'button', class: 'btn small', id: 'tutorial-exit', title: 'Leave the tutorial and go back to the page you came from',
        onclick: () => (AS.tutorial && AS.tutorial.exit ? AS.tutorial.exit() : location.assign(location.pathname)) }, 'Exit tutorial');
      // The reference library (ref.html, js/ref.js) is a page of its own: a new tab, in every mode,
      // so the study on screen stays as it is.
      const refs = h('a', { class: 'btn small', id: 'refs-link', href: 'ref.html', target: '_blank', rel: 'noopener',
        title: 'The reference library: the sources behind the plan values, with the documents. Opens in a new tab' }, 'References');
      // The live tutorial (?tutorial=live) offers only the way out beside it: starting over would be a
      // second paid run, and the recorded tutorial is not entered from inside a live one.
      const help = h('div', { class: 'tb-help' }, refs, AS.mock.live ? exitBtn()
        : AS.mock.tutorial
          ? [h('a', { class: 'btn small', id: 'tutorial-restart', href: location.pathname + '?tutorial=reset', title: 'Start the tutorial again from the beginning' }, 'Restart tutorial'), exitBtn()]
          : AS.ui.tutorialLink({ class: 'btn small', id: 'tutorial-btn', title: 'Guided tutorial on recorded data, at no cost' }, 'Tutorial'));
      AS.append(el, [h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }, AS.icon('wing', 16)), h('span', { class: 'brand-name' }, 'Wing Studio')),
        project, mission, h('div', { class: 'spacer' }), mode, totals, controls, help]);
      // the page tabs sit on top of the document area (index.html #doctabs), not in the application bar
      AS.$('#doctabs').appendChild(tabs);
    }

    /* The study-level page that belongs to the study status: the Report once the study is done,
       otherwise the setup step it is at. */
    const studyStep = (s) => (s.status === 'done' ? 'report' : AS.stepForStatus(s));
    /* The Study tab stands for the study-level pages. From a stage page it has to change the
       selection as well, or the pane would stay on the stage while the tab reads as open. */
    function openStudyTab() {
      const s = state.study;
      if (s && state.ui.sel.kind === 'stage') AS.select({ kind: 'step', key: studyStep(s) });
      else AS.setTab('main');
    }

    function tabButton(key, label, extra, active, disabled) {
      return h('button', { type: 'button', role: 'tab', class: 'tab' + (active ? ' active' : '') + (['compare', 'dashboard', 'study'].includes(key) ? '' : ' case-tab'), 'aria-selected': active ? 'true' : 'false', disabled: !!disabled, dataset: { tab: key },
        onclick: () => (key === 'compare' || key === 'dashboard' ? AS.setTab(key) : key === 'study' ? openStudyTab() : AS.selectCase(key)) }, extra || null, label);
    }

    function render() {
      const s = state.study, ui = state.ui, cases = AS.caseList(), p = AS.project();
      AS.region(project, [p && p.name], () => h('button', { type: 'button', class: 'link-btn', title: 'Project (open the mission screen to switch)',
        onclick: () => AS.select({ kind: 'step', key: 'mission' }) }, p ? p.name : 'No project'));
      AS.region(mission, [s && s.requirements, s && s.title], () => (s
        ? h('button', { type: 'button', class: 'mission-chip', title: AS.studyTitle(s) + ' – open the mission', onclick: () => AS.select({ kind: 'step', key: 'mission' }) }, AS.ui.missionChip(s.requirements) || AS.studyTitle(s) || s.id)
        : h('span', { class: 'muted' }, 'No study open')));
      tabs.classList.toggle('many', cases.length > 2);
      // ui.sel.kind is part of the signature: the active tab follows the page shown, not only the current case
      AS.region(tabs, [cases.map((c) => [c.id, c.naca, c.status, AS.passedCount(c), c.verdict]), ui.tab, ui.caseId, ui.sel.kind, s && s.status, !!s], () => {
        const out = [];
        // The Study tab is always first: Mission, Airfoil suggestion, Plan and Report belong to no single case.
        out.push(tabButton('study', s ? 'Study' : 'New study', null, ui.tab === 'main' && (ui.sel.kind === 'step' || !cases.length)));
        cases.forEach((c) => {
          const active = ui.tab === 'main' && ui.sel.kind === 'stage' && ui.caseId === c.id;
          out.push(tabButton(c.id, [h('span', { class: 'tab-name' }, h('span', { class: 'tab-prefix' }, 'NACA '), c.naca),
            needsAttention(c) ? h('span', { class: 'tab-attn', title: c.status === 'failed' ? 'failed' : 'needs your approval' },
              h('span', { class: 'attn-long' }, c.status === 'failed' ? 'failed' : 'needs you'), h('span', { class: 'attn-short', 'aria-hidden': 'true' }, '!')) : null,
            needsAttention(c) ? null : h('span', { class: 'tab-count' }, `${AS.passedCount(c)}/${c.stages.length}`)],
          h('i', { class: 'dot st-' + c.status + (needsAttention(c) ? ' attention' : ''), title: AS.ui.statusText(c.status) }), active));
          // number keys follow the order of the tabs on screen, and the Study tab comes first
          out[out.length - 1].title = `${AS.ui.statusText(c.status)} · key ${out.length}`;
        });
        out.push(tabButton('compare', 'Compare', null, ui.tab === 'compare', !cases.length));
        out.push(tabButton('dashboard', 'Dashboard', null, ui.tab === 'dashboard'));
        return out;
      });
      AS.region(mode, [s && s.mode, s && s.status, cases.length > 0], () => {
        if (!s) return null;
        // Before the plan is approved nothing runs and the mode is chosen with the approve buttons;
        // afterwards the backend accepts gate and auto only.
        if (!cases.length) {
          return h('span', { class: 'mode-select mode-static', id: 'mode-static', title: 'Nothing runs before the plan is approved. Auto-run or gate-each-stage is chosen when you approve the plan.' },
            h('em', {}, 'mode'), 'Plan only');
        }
        const choices = AS.MODES.filter((m) => m.key !== 'plan');
        return h('label', { class: 'mode-select', title: 'Run mode. A change takes effect at the next stage boundary.' },
          h('select', { id: 'mode-select', 'aria-label': 'Run mode', onchange: async (e) => {
            const el = e.target;
            await AS.actions.setMode(el.value);
            // a refused change must not stay on screen
            if (state.study && el.isConnected && el.value !== state.study.mode) el.value = state.study.mode;
          } },
          choices.some((m) => m.key === s.mode) ? null : h('option', { value: s.mode, selected: true, disabled: true }, s.mode),
          choices.map((m) => h('option', { value: m.key, selected: s.mode === m.key, title: m.hint }, m.label))));
      });
      const u = (s && s.usage) || {};
      // share of the input tokens that were read from the prompt cache; nothing to state before the first model call
      const cached = AS.cachedShare(u.tokens_cached, u.tokens_in);
      AS.region(totals, [!!s, u.tokens_in, u.tokens_out, u.tokens_cached, u.cost_usd, state.timing.start, state.timing.end], () => (s ? [
        h('span', { class: 'total', title: 'Elapsed since the plan was approved' }, h('em', {}, 'time'), h('b', { dataset: { clock: 'run' } }, AS.fmtDur(AS.runSeconds()))),
        h('span', { class: 'total', title: `Tokens in ${AS.fmtInt(u.tokens_in || 0)} (cached ${AS.fmtInt(u.tokens_cached || 0)}), out ${AS.fmtInt(u.tokens_out || 0)}` }, h('em', {}, 'tok'), h('b', {}, AS.fmtTokens((u.tokens_in || 0) + (u.tokens_out || 0))),
          cached == null ? null : h('span', { class: 'total-cached' }, Math.round(cached * 100) + '% cached')),
        h('span', { class: 'total', title: `${u.llm_calls || 0} model calls` }, h('em', {}, 'cost'), h('b', {}, AS.fmtCost(u.cost_usd || 0))),
      ] : null));
      // with no study the group is empty: hidden, or its rule would stand as a second one beside the Tutorial cell
      controls.hidden = !s;
      AS.region(controls, [s && s.status, stopArmed, cases.map((c) => c.status)], () => {
        const out = [];
        if (s) {
          const paused = s.status === 'paused' || (cases.length && cases.every((c) => c.status !== 'running') && cases.some((c) => c.status === 'paused'));
          const live = ['running', 'paused', 'suggesting', 'planning'].includes(s.status);
          out.push(paused
            ? h('button', { type: 'button', class: 'btn small', id: 'resume-btn', dataset: { key: 'pause-resume' }, 'aria-label': 'Resume', title: 'Resume every paused case', onclick: () => AS.actions.control('resume') }, AS.icon('play', 12), h('span', { class: 'lbl' }, 'Resume'))
            : h('button', { type: 'button', class: 'btn small', id: 'pause-btn', dataset: { key: 'pause-resume' }, disabled: s.status !== 'running', 'aria-label': 'Pause', title: 'Pause every case at its next tool boundary', onclick: () => AS.actions.control('pause') }, AS.icon('pause', 12), h('span', { class: 'lbl' }, 'Pause')));
          out.push(h('button', { type: 'button', class: 'btn small danger' + (stopArmed ? ' armed' : ''), id: 'stop-btn', disabled: !live, 'aria-label': 'Stop',
            title: 'Stop every case and kill running solvers. Finished stages and files are kept.', onclick: () => {
              if (!stopArmed) { stopArmed = true; clearTimeout(stopTimer); stopTimer = setTimeout(() => { stopArmed = false; AS.invalidate(); }, 3500); AS.invalidate(); return; }
              stopArmed = false; clearTimeout(stopTimer);
              AS.actions.control('stop');
              AS.invalidate();
            } }, stopArmed ? 'Click again to stop' : [AS.icon('stop', 12), h('span', { class: 'lbl' }, 'Stop')]));
          out.push(h('button', { type: 'button', class: 'btn small', id: 'new-study-btn', title: 'Close this study and describe a new mission', onclick: () => AS.newStudy() }, 'New'));
        }
        return out;
      });
    }
    return { build, render };
  })();

  // =====================================================================
  // Stage rail
  // =====================================================================
  V.rail = (function () {
    let el, head, list, props, lastSel = '';

    function stepInfo(key) {
      const s = state.study;
      if (!s) return { status: key === 'mission' ? 'running' : 'pending', headline: key === 'mission' ? 'describe the flight condition' : '' };
      const st = s.status, cands = s.candidates || [], plan = s.plan;
      if (key === 'mission') return { status: st === 'draft' ? 'running' : 'passed', headline: AS.ui.missionChip(s.requirements).split(' · ').slice(0, 3).join(' · ') };
      if (key === 'suggest') {
        const status = st === 'suggesting' ? 'running' : st === 'awaiting_selection' ? 'awaiting_approval' : cands.length ? 'passed' : st === 'failed' || st === 'stopped' ? 'failed' : 'pending';
        const chosen = cands.filter((cd) => cd.selected).length;
        return { status, headline: cands.length ? `${cands.length} candidates` + (status === 'passed' ? ` · ${chosen} selected` : '') : '', working: st === 'suggesting' };
      }
      if (key === 'plan') {
        const status = st === 'planning' ? 'running' : st === 'awaiting_approval' ? 'awaiting_approval' : plan && plan.approved_at ? 'passed' : plan ? 'stale' : 'pending';
        return { status, headline: plan ? `v${plan.version || 1} · ${(plan.cases || []).length} cases` + (plan.approved_at ? ` · ${s.mode}` : '') : '', working: st === 'planning' };
      }
      // report
      const cases = AS.caseList();
      const finished = cases.length && cases.every((c) => ['done', 'failed', 'stopped'].includes(c.status));
      const status = s.report ? 'passed' : finished && st === 'running' ? 'running' : 'pending';
      const rec = AS.recommendationText(s.report);
      const naca = /NACA\s*\d{4}/i.exec(rec);
      return { status, headline: s.report ? (naca ? 'recommends ' + naca[0] : 'ready') : '', working: status === 'running' };
    }

    function phaseChip(phase) {
      return h('span', { class: 'phase', title: 'gather → act → verify', 'aria-label': 'phase ' + (phase || 'starting') },
        ['gather', 'act', 'verify'].map((p) => h('i', { class: p === phase ? 'on' : '' }, p)));
    }

    function node(opts) {
      const selected = opts.selected;
      return h('li', {}, h('button', { type: 'button', class: 'rail-node st-' + opts.status + (selected ? ' selected' : '') + (opts.status === 'running' ? ' active' : ''),
        'aria-current': selected ? 'step' : null, title: opts.title + (opts.headline ? ' – ' + opts.headline : ''), dataset: { key: opts.key }, onclick: opts.onclick },
      icon(opts.status),
      h('span', { class: 'node-body' },
        h('span', { class: 'node-title' }, opts.title, opts.attempts > 1 ? h('span', { class: 'attempt', title: `attempt ${opts.attempts}` }, '×' + opts.attempts) : null),
        opts.headline ? h('span', { class: 'node-headline' }, opts.headline) : null,
        opts.status === 'running' && opts.phase !== undefined ? phaseChip(opts.phase) : null),
      h('span', { class: 'node-time', dataset: opts.since ? { since: opts.since } : {} }, opts.since ? AS.fmtDur((Date.now() - Date.parse(opts.since)) / 1000) : opts.seconds != null ? AS.fmtSeconds(opts.seconds) : '')));
    }

    function build(root) {
      el = root;
      head = h('div', { class: 'rail-head' });
      list = h('ol', { class: 'rail-list' });
      props = h('section', { class: 'props', 'aria-label': 'Properties of the selected step' });
      AS.append(el, [head, list, props]);
    }

    /* Properties of whatever is selected in the tree: a read-only name / value grid built from the
       study state (nothing here is editable; plan values are edited on the Plan page). */
    function propsFor() {
      const s = state.study, ui = state.ui, c = AS.currentCase();
      if (!s) return { title: 'no study open', groups: [] };
      const req = s.requirements || {}, dash = '–';
      const mission = [['Mach number', AS.fmt(req.mach)], ['Reynolds number', req.reynolds != null ? AS.fmtSci(req.reynolds) : dash],
        ['Air density', req.density != null ? AS.fmt(req.density) + ' kg/m³' : dash], ['Design Cl', AS.fmt(req.design_cl)],
        ['Objective', AS.OBJECTIVES[req.objective] || req.objective || dash],
        AS.isNum(req.target_ld) && req.target_ld > 0 ? ['Target L/D', '≥ ' + AS.fmt(req.target_ld)] : null].filter(Boolean);
      const st = ui.tab === 'main' && ui.sel.kind === 'stage' && c ? AS.stageOf(c, ui.sel.key) : null;
      if (st) {
        const shownAs = AS.stageStatus(c, st);
        const general = [['Status', AS.ui.statusText(shownAs)], ['Case', 'NACA ' + c.naca],
          ['Angle of attack', AS.isNum(c.alpha_deg) ? AS.fmt(c.alpha_deg, 3) + '°' : dash], ['Attempts', String(st.attempts || 0)],
          ['Wall time', shownAs === 'pending' ? dash : shownAs === 'running' ? 'running' : AS.fmtSeconds(AS.stageClock(c, st))]];
        const metrics = Object.entries(st.metrics || {}).filter(([, v]) => v != null && typeof v !== 'object').map(([k, v]) => [AS.humanKey(k), AS.fmtValue(v)]);
        return { title: st.title || AS.stageTitle(st.key), groups: [['Stage', general], metrics.length ? ['Metrics', metrics] : null, ['Mission', mission]] };
      }
      const cases = AS.caseList();
      const study = [['Status', AS.ui.statusText(s.status)], ['Model', AS.modelLabel(s.model)],
        ['Run mode', (AS.MODES.find((m) => m.key === s.mode) || {}).label || s.mode || dash],
        ['Cases', cases.length ? cases.map((x) => 'NACA ' + x.naca).join(', ') : 'none yet']];
      return { title: 'Study', groups: [['Study', study], ['Mission', mission]] };
    }

    function render() {
      const ui = state.ui, s = state.study, c = AS.currentCase();
      el.closest('.app').classList.toggle('rail-collapsed', !!ui.railCollapsed);
      // "Follow live" is offered only while something is in flight or waiting for the engineer: on a
      // study that has ended (or not started) it would land on a page that is not live
      const live = !!s && ['suggesting', 'awaiting_selection', 'planning', 'awaiting_approval', 'running', 'paused'].includes(s.status);
      AS.region(head, [ui.railCollapsed, ui.follow, !!s, live], () => [
        h('button', { type: 'button', class: 'btn small icon', id: 'rail-toggle', title: 'Collapse or expand the stage rail ( [ )', 'aria-label': 'Toggle stage rail',
          'aria-expanded': ui.railCollapsed ? 'false' : 'true', onclick: () => { ui.railCollapsed = !ui.railCollapsed; AS.store.set('rail', ui.railCollapsed); AS.invalidate(); } }, AS.icon(ui.railCollapsed ? 'chevrons-right' : 'chevrons-left')),
        h('span', { class: 'rail-title' }, 'Stages'),
        live && !ui.follow ? h('button', { type: 'button', class: 'btn small', id: 'follow-btn', title: 'Jump to the active stage and keep following it', onclick: AS.followLive }, 'Follow live') : null,
      ]);
      const sig = [ui.sel, ui.caseId, ui.tab, s && [s.status, s.mode, (s.candidates || []).map((cd) => cd.selected), s.plan && [s.plan.version, s.plan.approved_at], s.requirements, !!s.report, AS.recommendationText(s.report)],
        c && c.stages.map((st) => [st.status, st.headline, st.attempts, st.phase, st.started_at, st.seconds]), c && c.status, AS.caseList().map((x) => x.status)];
      // With no study open the pane can only show the Mission form: no other step may read as
      // current, and a click on one says why instead of selecting a page that is not shown.
      const stepSelected = (key) => ui.tab === 'main' && (s ? ui.sel.kind === 'step' && ui.sel.key === key : key === 'mission');
      const openStep = (key) => (s || key === 'mission' ? AS.select({ kind: 'step', key }) : AS.toast('Start a study first.', 'info'));
      AS.region(list, sig, () => {
        const out = [h('li', { class: 'rail-sep' }, 'Study')];
        for (const step of AS.STEPS) {
          const info = stepInfo(step.key);
          out.push(node({ key: 'step:' + step.key, title: step.title, status: info.status, headline: info.headline, phase: undefined,
            selected: stepSelected(step.key), onclick: () => openStep(step.key) }));
        }
        out.push(h('li', { class: 'rail-sep' }, c ? 'NACA ' + c.naca : 'Case stages', c ? h('span', { class: 'chip st-' + c.status }, AS.ui.statusText(c.status)) : null));
        const defs = AS.stageDefs();
        defs.forEach((def) => {
          const st = c ? AS.stageOf(c, def.key) : null;
          // a paused case holds its stage in flight: the tree says so and its clock stands still
          const status = st ? AS.stageStatus(c, st) : 'pending';
          out.push(node({ key: 'stage:' + def.key, title: (st && st.title) || def.title, status, headline: st ? st.headline : '', attempts: st ? st.attempts : 0, phase: st ? st.phase : undefined,
            since: status === 'running' && st.started_at ? st.started_at : null, seconds: st && status !== 'pending' ? AS.stageClock(c, st) : null,
            selected: ui.tab === 'main' && ui.sel.kind === 'stage' && ui.sel.key === def.key,
            onclick: c ? () => AS.select({ kind: 'stage', key: def.key }) : () => AS.toast('Stages start when the plan is approved.', 'info') }));
        });
        out.push(h('li', { class: 'rail-sep' }, 'Result'));
        const rep = stepInfo('report');
        out.push(node({ key: 'step:report', title: 'Report', status: rep.status, headline: rep.headline, phase: undefined,
          selected: stepSelected('report'), onclick: () => openStep('report') }));
        return out;
      });
      const sel = ui.tab === 'main' && ui.sel.kind === 'stage' && c ? AS.stageOf(c, ui.sel.key) : null;
      AS.region(props, [ui.tab, ui.sel, ui.caseId, s && [s.status, s.mode, s.model, s.requirements], AS.caseList().map((x) => x.naca), c && c.alpha_deg,
        sel && [sel.status, sel.attempts, sel.seconds, sel.metrics, c.status]], () => {
        const p = propsFor();
        return [h('div', { class: 'props-head' }, h('span', { class: 'props-title' }, 'Properties'), h('span', { class: 'props-of' }, p.title)),
          h('div', { class: 'props-body' }, h('table', { class: 'props-grid' }, h('colgroup', {}, h('col', { style: { width: '46%' } }), h('col')), p.groups.filter(Boolean).map(([group, rows]) => h('tbody', {},
            h('tr', { class: 'props-group' }, h('th', { colspan: 2, scope: 'rowgroup' }, group)),
            rows.map(([k, v]) => h('tr', {}, h('th', { scope: 'row' }, k), h('td', {}, v)))))))];
      });
      // Follow, a banner or the status bar can select a row below the fold of the tree. Bring it into
      // view when the selection changed, never on other rebuilds: the tree is rebuilt on every stage
      // update and must not jump back while it is being scrolled. A collapsed rail waits until it opens.
      if (!ui.railCollapsed && ui.tab === 'main') {
        const selKey = JSON.stringify([ui.sel, ui.caseId]);
        if (selKey !== lastSel) {
          lastSel = selKey;
          const row = list.querySelector('.rail-node.selected');
          if (row) row.scrollIntoView({ block: 'nearest' });
        }
      }
    }
    return { build, render };
  })();

  // =====================================================================
  // Stage view: viewer (top) + stage report (bottom)
  // =====================================================================
  V.stage = (function () {
    let el, vHead, vBody, panes = {}, divider, rHead, rLive, rBody, rActions;
    let charts = {}, pv = {}, postTab = 'field', validateTab = 'compare', instruction = {}, busy = {}, askedGeometry = {};
    let lastShown = '', lastPanes = '';   // case | stage of the last render (and of the last pane reset), to tell a new page from an update of the same one
    let approveFocus = '';                // the case whose Approve button had the keyboard
    const PRESETS = [
      { key: 'full', label: 'Full domain', zoom: 'full' },
      { key: 'airfoil', label: 'Airfoil', zoom: 'auto' },
      { key: 'le', label: 'Leading edge', zoom: '-0.1,0.3,-0.15,0.15' },
      { key: 'te', label: 'Trailing edge', zoom: '0.8,1.2,-0.15,0.15' },
      { key: 'wake', label: 'Wake', zoom: '0.8,4,-0.8,0.8' },
    ];
    const DEFAULT_VIEW = { mesh: { mode: 'mesh', field: 'U', zoom: 'auto' }, physics: { mode: 'mesh', field: 'U', zoom: 'full' }, post: { mode: 'field', field: 'U', zoom: 'auto' },
      validate: { mode: 'field', field: 'p', zoom: 'auto' } };
    const paneFor = (key) => (key === 'geometry' ? 'geometry' : key === 'mesh' || key === 'physics' ? 'paraview' : key === 'solve' ? 'solve'
      : key === 'post' ? (postTab === 'cp' ? 'cp' : 'paraview') : key === 'validate' ? (validateTab === 'field' ? 'paraview' : 'validate') : 'none');

    // ---------- ParaView iframe, one per case, kept alive across stage and tab switches ----------
    function viewerOf(c) { return (pv[c.id] = pv[c.id] || { status: 'idle', choice: {}, state: null, frame: null, applied: '' }); }

    function startViewer(c) {
      const v = viewerOf(c), study = state.study;
      v.status = 'loading'; v.error = null;
      AS.api.get(`/api/studies/${study.id}/cases/${c.id}/viewer`).then((resp) => {
        // the study was closed while the viewer started (reset() replaced pv): an iframe added now would never be removed
        if (pv[c.id] !== v) return;
        const problem = resp && (resp.error || (resp.ok === false && 'The viewer did not start'));
        if (!resp || !resp.url || problem) throw new Error(problem || 'The server returned no viewer address');
        v.url = resp.url; v.state = resp.state && typeof resp.state === 'object' ? resp.state : {};
        v.frame = h('iframe', { class: 'pv-frame', src: resp.url, title: `Interactive view of NACA ${c.naca}`, dataset: { case: c.id },
          onload: () => { v.loaded = true; pushToMock(v, c); AS.invalidate(); } });
        panes.frames.appendChild(v.frame);
        v.status = 'ready';
      }).catch((e) => { v.status = 'error'; v.error = e.message; }).finally(AS.invalidate);
    }
    function pushToMock(v, c) {
      // the mock viewer page is same-origin and listens for the state; the real ParaView page ignores this
      if (AS.mock.enabled && v.frame && v.frame.contentWindow && v.state) v.frame.contentWindow.postMessage({ case: c.id, state: v.state }, location.origin);
    }
    function applyView(c, stageKey) {
      const v = viewerOf(c), st = AS.stageOf(c, stageKey);
      if (v.status !== 'ready') return;
      const want = v.choice[stageKey] || DEFAULT_VIEW[stageKey] || DEFAULT_VIEW.mesh;
      // re-sent when the stage finishes again, so new result times are picked up
      const key = stageKey + JSON.stringify(want) + ((st && st.ended_at) || '');
      if (v.applied === key) return;
      v.applied = key;
      AS.api.post(`/api/studies/${state.study.id}/cases/${c.id}/viewer`, { mode: want.mode, field: want.field, zoom: want.zoom, time: 'latest' })
        .then((resp) => {
          if (resp && resp.state && typeof resp.state === 'object') { v.state = resp.state; pushToMock(v, c); }
          if (v.applied === key) v.viewError = null;   // the warning of an earlier failure no longer applies
          AS.invalidate();
        })
        // A failed request stays the applied one: every render calls applyView, so forgetting it here would
        // re-send it for as long as it fails. It is sent again by a click (retryView) or when the key changes.
        // A newer request may have replaced this one; its outcome is the one that counts.
        .catch((e) => { if (v.applied !== key) return; v.viewError = e.message; AS.invalidate(); });
    }
    /* Send the view of this stage again after a failed change. */
    function retryView(c, stageKey) {
      const v = viewerOf(c);
      v.applied = ''; v.viewError = null;
      applyView(c, stageKey);
      AS.invalidate();
    }
    function choose(c, stageKey, patch) {
      const v = viewerOf(c);
      v.choice[stageKey] = Object.assign({}, v.choice[stageKey] || DEFAULT_VIEW[stageKey] || DEFAULT_VIEW.mesh, patch);
      // after a failure the choice may be the one that failed: an explicit click must send it again
      if (v.viewError) v.applied = '';
      v.viewError = null;
      applyView(c, stageKey);
      AS.invalidate();
    }

    function snapshots(st) {
      const images = ((st && st.artifacts) || []).filter((a) => a.kind === 'image');
      return images.map((a) => h('button', { type: 'button', class: 'snap', title: a.label, onclick: () => AS.lightbox(a.url, a.label) },
        h('img', { src: a.url, alt: a.label, loading: 'lazy' }), h('span', {}, a.label)));
    }

    function renderParaview(c, st) {
      const mesh = AS.stageOf(c, 'mesh'), v = viewerOf(c);
      const meshReady = mesh && mesh.status === 'passed';
      if (meshReady && v.status === 'idle') startViewer(c);
      if (meshReady) applyView(c, st.key);
      for (const frame of AS.$$('.pv-frame', panes.frames)) frame.hidden = !(meshReady && v.status === 'ready' && frame.dataset.case === c.id);
      const images = snapshots(st), firstImage = (st.artifacts || []).find((x) => x.kind === 'image');
      const caseLive = ['pending', 'running', 'paused', 'awaiting_approval'].includes(c.status);
      AS.region(panes.pvMsg, [c.id, st.key, meshReady, v.status, v.error, v.viewError, v.state && v.state.error, mesh && mesh.status, images.length, caseLive], () => {
        if (meshReady && v.status === 'ready') {
          // problems reported by the viewer itself are shown on top of the view, which stays usable
          const stateError = v.state && typeof v.state.error === 'string' ? v.state.error : null;
          // the note sits in a layer that lets the pointer through to the viewer, so the button opts back in
          return v.viewError ? h('p', { class: 'note warn overlay-note' }, 'The view could not be changed: ' + v.viewError + ' ',
            h('button', { type: 'button', class: 'btn small', id: 'view-retry', style: { pointerEvents: 'auto' }, onclick: () => retryView(c, st.key) }, 'Try again'))
            : stateError ? h('p', { class: 'note warn overlay-note' }, 'Viewer: ' + stateError) : null;
        }
        let text, working = false, retry = false;
        if (!meshReady) {
          text = mesh && mesh.status === 'running' ? 'The mesh is being built. The interactive view opens when the Mesh stage has passed.'
            : mesh && mesh.status === 'stale' ? (caseLive ? 'The mesh is being rebuilt. The interactive view returns when the Mesh stage has passed again.'
              : 'There is no current mesh and nothing is running: this case was stopped before the Mesh stage had passed again. Re-run the Mesh stage to build it; the interactive view returns when it has passed.')
              : mesh && mesh.status === 'failed' ? 'The Mesh stage failed. The interactive view opens when it has passed.'
                : 'There is no mesh yet. The interactive view opens when the Mesh stage has passed.';
          working = mesh && mesh.status === 'running';
        } else if (v.status === 'loading') { text = 'Starting the ParaView viewer for this case (about 5 seconds)'; working = true; }
        else { text = 'The interactive ParaView view is not available: ' + String(v.error || 'unknown reason').replace(/[.\s]+$/, '') + '. Snapshots rendered during the run are still shown below.'; retry = true; }
        return h('div', { class: 'pv-message' }, working ? AS.ui.spinner(text) : h('p', {}, text),
          retry ? h('button', { type: 'button', class: 'btn small', id: 'viewer-retry', onclick: () => { v.status = 'idle'; AS.invalidate(); } }, 'Try again') : null,
          firstImage ? h('div', { class: 'snap-large' }, h('img', { src: firstImage.url, alt: firstImage.label || 'Snapshot' })) : null);
      });
      AS.region(panes.snaps, [(st.artifacts || []).map((a) => a.url)], () => images);
      panes.snaps.hidden = images.length === 0;
    }

    function renderGeometry(c, st) {
      const sr = state.series[c.id] || {}, g = sr.geometry;
      AS.region(panes.geometry, [c.id, st.status, g && [g.upper.length, g.upper[Math.floor(g.upper.length / 3)], g.lower[Math.floor(g.lower.length / 2)]], st.metrics], () => {
        if (!g || !g.upper.length) {
          return h('div', { class: 'placeholder' }, AS.stageStatus(c, st) === 'running' ? AS.ui.spinner('Generating the profile') : 'The outline appears when the Geometry stage has generated the profile.');
        }
        const pts = g.upper.concat(g.lower.slice().reverse());
        const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
        const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        const pad = 0.06 * (x1 - x0), w = (x1 - x0) + 2 * pad, hgt = Math.max(y1 - y0, 0.18 * (x1 - x0)) + 2 * pad, cy = (y0 + y1) / 2;
        const svg = document.createElementNS(SVG, 'svg');
        svg.setAttribute('viewBox', `${x0 - pad} ${-(cy + hgt / 2)} ${w} ${hgt}`);
        svg.setAttribute('class', 'outline'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `Outline of NACA ${c.naca}`);
        const add = (tag, attrs) => { const n = document.createElementNS(SVG, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); svg.appendChild(n); return n; };
        add('line', { x1: x0, y1: 0, x2: x1, y2: 0, class: 'chord' });
        add('polygon', { points: pts.map((p) => `${p[0]},${-p[1]}`).join(' '), class: 'profile' });
        // camber line: mid-points of upper and lower surface where both have the same index
        if (g.upper.length === g.lower.length) add('polyline', { points: g.upper.map((p, i) => `${(p[0] + g.lower[i][0]) / 2},${-(p[1] + g.lower[i][1]) / 2}`).join(' '), class: 'camber' });
        const m = st.metrics || {};
        return h('div', { class: 'geom' }, svg, h('div', { class: 'geom-legend' },
          h('span', {}, h('i', { class: 'key profile' }), 'profile'), h('span', {}, h('i', { class: 'key camber' }), 'camber line'), h('span', {}, h('i', { class: 'key chord' }), 'chord line'),
          AS.isNum(m.thickness) ? h('span', {}, `thickness ${(m.thickness * 100).toFixed(1)} %`) : null,
          AS.isNum(m.max_camber) ? h('span', {}, `camber ${(m.max_camber * 100).toFixed(1)} %` + (AS.isNum(m.camber_position) ? ` at ${(m.camber_position * 100).toFixed(0)} % chord` : '')) : null,
          h('span', {}, `${g.upper.length + g.lower.length} points`)));
      });
    }

    function renderSolve(c, st) {
      const sr = state.series[c.id] || { res: [], forces: [], fields: [] }, a = state.activity[c.id] || {};
      const sig = JSON.stringify([c.id, sr.res.length, sr.res.length && sr.res[sr.res.length - 1].iter, sr.forces.length, (c.reference || {}).cl, sr.fields]);
      const info = AS.$('.solve-info', panes.solve);
      AS.region(info, [c.id, AS.stageStatus(c, st), a.iteration, a.total, a.cl, a.cd], () => {
        const running = AS.stageStatus(c, st) === 'running';   // a paused case does not show a running solver
        if (st.status === 'stale') return h('span', { class: 'muted' }, 'This stage is stale: the charts below show the earlier solver run until it runs again.');
        if (AS.isNum(a.iteration)) {
          const pct = a.total ? Math.min(100, a.iteration / a.total * 100) : 0;
          return [h('span', { class: 'progress', role: 'progressbar', 'aria-valuenow': Math.round(pct), 'aria-valuemin': 0, 'aria-valuemax': 100 }, h('i', { style: { width: pct + '%' } })),
            h('b', {}, `iteration ${AS.fmtInt(a.iteration)}${a.total ? ' / ' + AS.fmtInt(a.total) : ''}`),
            AS.isNum(a.cl) ? h('span', {}, `Cl ${a.cl.toFixed(4)}`) : null, AS.isNum(a.cd) ? h('span', {}, `Cd ${a.cd.toFixed(5)}`) : null,
            running ? AS.ui.spinner('solver running') : null];
        }
        return running ? AS.ui.spinner('Waiting for the solver to start') : h('span', { class: 'muted' }, sr.res.length ? '' : 'Residuals and force coefficients appear when the solver runs.');
      });
      if (panes.solve.__sig === sig) return;
      panes.solve.__sig = sig;
      const total = a.total || (sr.res.length ? sr.res[sr.res.length - 1].iter : null);
      charts.res.update({ yLog: true, xLabel: 'iteration', yLabel: 'initial residual', xMin: 0, xMax: total, fmtX: AS.fmtInt, fmtY: (v) => AS.fmtSci(v),
        series: sr.fields.map((f, i) => ({ name: f, color: AS.chart.fieldColor(f, i), points: sr.res.map((r) => [r.iter, r[f]]) })),
        emptyText: 'Residuals appear when the solver runs' });
      const ref = c.reference || {}, scr = screening(c);
      const refCl = AS.isNum(ref.cl) ? ref.cl : scr.cl, refCd = AS.isNum(ref.cd) ? ref.cd : scr.cd;
      // The first iterations swing by orders of magnitude; drawn in full they hide the
      // convergence, so the force charts start after the first 12 % of the history.
      const settled = sr.forces.length > 12 ? sr.forces.slice(Math.floor(sr.forces.length * 0.12)) : sr.forces;
      const range = (key, refValue) => {
        const vals = settled.map((f) => f[key]).filter(AS.isNum).concat(AS.isNum(refValue) ? [refValue] : []);
        if (!vals.length) return {};
        const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.15 || Math.abs(hi) * 0.05 || 0.01;
        return { yMin: lo - pad, yMax: hi + pad };
      };
      charts.cl.update(Object.assign({ xLabel: 'iteration', yLabel: 'Cl', xMin: 0, xMax: total, fmtX: AS.fmtInt, fmtY: (v) => v.toFixed(4),
        series: [{ name: 'Cl (OpenFOAM)', color: 'var(--s1)', points: settled.map((f) => [f.iter, f.cl]) }],
        refs: AS.isNum(refCl) ? [{ y: refCl, label: 'XFoil ' + refCl.toFixed(3) }] : [], emptyText: 'Cl history appears when the solver runs' }, range('cl', refCl)));
      charts.cd.update(Object.assign({ xLabel: 'iteration', yLabel: 'Cd', xMin: 0, xMax: total, fmtX: AS.fmtInt, fmtY: (v) => v.toFixed(5),
        series: [{ name: 'Cd (OpenFOAM)', color: 'var(--s2)', points: settled.map((f) => [f.iter, f.cd]) }],
        refs: AS.isNum(refCd) ? [{ y: refCd, label: 'XFoil ' + refCd.toFixed(4) }] : [], emptyText: 'Cd history appears when the solver runs' }, range('cd', refCd)));
    }

    AS.cpSeries = function (sr, colorCfd, colorXf, prefix) {
      const out = [];
      prefix = prefix || '';
      if (sr && sr.cp) {
        out.push({ name: prefix + 'OpenFOAM upper', color: colorCfd, points: sr.cp.upper, noLegend: true });
        out.push({ name: prefix + 'OpenFOAM lower', color: colorCfd, points: sr.cp.lower, noLegend: true });
      }
      if (sr && sr.xfoilCp) {
        out.push({ name: prefix + 'XFoil upper', color: colorXf, dash: true, points: sr.xfoilCp.upper, noLegend: true });
        out.push({ name: prefix + 'XFoil lower', color: colorXf, dash: true, points: sr.xfoilCp.lower, noLegend: true });
      }
      return out;
    };

    /* Draws the OpenFOAM and XFoil pressure curves of the current run into `chart`. */
    function drawCp(chart, host, c, st, noteEl) {
      const sr = state.series[c.id], post = AS.stageOf(c, 'post') || {};
      if (post.status === 'passed' && sr && sr.loaded && !sr.cp && !sr.cpAsked) { sr.cpAsked = true; AS.loadSeries(c.id); }
      const data = AS.cpData(c);
      const sig = JSON.stringify([c.id, data.cp && data.cp.upper.length, data.cp && data.cp.upper[data.cp.upper.length >> 1], data.xfoilCp && data.xfoilCp.upper.length, post.status, st.status]);
      if (host.__sig === sig) return;
      host.__sig = sig;
      const legend = [];
      if (data.cp) legend.push({ name: 'OpenFOAM', color: 'var(--s1)' });
      if (data.xfoilCp) legend.push({ name: 'XFoil (transition forced at 5 % chord)', color: 'var(--s2)', dash: true });
      chart.update({ yInvert: true, xLabel: 'x / c', yLabel: 'Cp (negative up)', xMin: 0, xMax: 1, legend, fmtX: (v) => v.toFixed(3), fmtY: (v) => v.toFixed(3),
        series: AS.cpSeries(data, 'var(--s1)', 'var(--s2)'),
        emptyText: post.status === 'running' ? 'Cp appears when the surface pressure has been extracted'
          : post.status === 'stale' || post.status === 'failed' ? 'The pressure distribution of the earlier run no longer applies: Post-process has to pass again'
            : 'No pressure distribution yet: it is extracted by the Post-process stage' });
      if (noteEl) noteEl.textContent = data.cp && !data.xfoilCp ? 'The XFoil curve is added when the Validate stage has run the reference.' : '';
    }
    function renderCp(c, st) { drawCp(charts.cp, panes.cp, c, st, AS.$('.cp-note', panes.cpWrap)); }

    function comparisonTable(c) {
      const n = AS.caseNumbers(c);
      const row = (label, cfd, xf, delta, limit, digits) => h('tr', {}, h('th', { scope: 'row' }, label),
        h('td', { class: 'num' }, AS.isNum(cfd) ? cfd.toFixed(digits) : '–'), h('td', { class: 'num' }, AS.isNum(xf) ? xf.toFixed(digits) : '–'),
        h('td', { class: 'num' }, delta), h('td', { class: 'muted' }, limit));
      return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl compare-tbl' },
        h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', { class: 'num' }, 'OpenFOAM'), h('th', { class: 'num' }, 'XFoil'), h('th', { class: 'num' }, 'Difference'),
          h('th', { title: 'Tolerance on the difference: within the first value the verdict is pass, within the second warn, beyond it fail' }, 'Pass / warn'))),
        h('tbody', {},
          row('Cl', n.cfd.cl, n.xf.cl, AS.fmtClDelta(n.deltas), '≤ 5 % / ≤ 10 %', 4),
          row('Cd', n.cfd.cd, n.xf.cd, AS.fmtPct(n.deltas.cd_pct), '≤ 25 % / ≤ 50 %', 5),
          row('Cm', n.cfd.cm, n.xf.cm, AS.isNum(n.deltas.cm_abs) ? '±' + Math.abs(n.deltas.cm_abs).toFixed(4) : '–', 'reported', 4),
          row('L/D', n.cfd.ld, n.xf.ld, AS.fmtPct(n.deltas.ld_pct), 'reported', 1))));
    }

    function renderValidate(c, st) {
      const n = AS.caseNumbers(c), m = AS.passedMetrics(c, 'validate'), ref = c.reference || {};
      // The pressure overlay is the evidence behind the table: shown beside it as soon as the run has a Cp curve.
      const hasCp = !!AS.cpData(c).cp;
      panes.validate.classList.toggle('has-cp', hasCp);
      panes.valChart.hidden = !hasCp;
      if (hasCp) drawCp(charts.vcp, panes.valCp, c, st, AS.$('.cp-note', panes.valChart));
      else panes.valCp.__sig = '';
      AS.region(panes.valInfo, [c.id, st.status, n, m, ref, c.alpha_deg], () => {
        if (!AS.isNum(n.xf.cl) && !AS.isNum(n.cfd.cl)) {
          return h('div', { class: 'placeholder' }, AS.stageStatus(c, st) === 'running' ? AS.ui.spinner('Running the XFoil reference')
            : st.status === 'stale' || st.status === 'failed' ? 'The comparison of the earlier run no longer applies: an earlier stage was redone or the case was stopped. It returns when the Validate stage has passed again.'
              : 'The comparison appears when the Validate stage has run XFoil under the same conditions.');
        }
        // the angle the comparison was made at (the case angle can be edited in the plan afterwards)
        const alpha = AS.isNum(ref.alpha) ? ref.alpha : c.alpha_deg;
        return h('div', { class: 'validate' },
          h('div', { class: 'validate-head' }, h('h3', {}, `OpenFOAM against XFoil · NACA ${c.naca} at ${AS.fmt(alpha, 2)}°`), 'Verdict ', AS.ui.verdict(n.verdict)),
          comparisonTable(c));
      });
      AS.region(panes.valNotes, [c.id, st.status, n, m, ref, c.alpha_deg], () => {
        if (!AS.isNum(n.xf.cl) && !AS.isNum(n.cfd.cl)) return null;
        const extra = Object.entries(m).filter(([k]) => /mach/i.test(k));
        const changed = AS.isNum(ref.alpha) && AS.isNum(c.alpha_deg) && Math.abs(ref.alpha - c.alpha_deg) > 1e-6;
        return [
          changed ? h('p', { class: 'note warn small' }, `The plan now sets ${AS.fmt(c.alpha_deg, 2)}° for this case; these numbers are for ${AS.fmt(ref.alpha, 2)}°. Re-run from the physics stage to apply the new angle.`) : null,
          h('p', { class: 'muted small' }, `Reference: XFoil at the same Reynolds number and angle, ${ref.transition ? 'transition ' + ref.transition : 'transition forced at 5 % chord'}, Mach ${AS.isNum(ref.mach) ? ref.mach : 0} (the RANS run is fully turbulent and incompressible).`,
            AS.isNum(n.deltas.farfield_cd_estimate) ? ` Expected spurious drag from the truncated far field: ${n.deltas.farfield_cd_estimate.toFixed(5)}.` : ''),
          extra.length ? h('p', { class: 'muted small' }, 'Compressibility effect, not used for the verdict: ', extra.map(([k, v]) => `${AS.humanKey(k)} ${AS.fmtValue(v)}`).join(' · ')) : null];
      });
    }

    // ---------- stage report ----------
    /* A decision that carries citations gets a reference mark (js/cite.js) in a last, narrow column.
       The column exists only when a row has one: stages of older studies keep the three columns.
       The mark is handed the value as the Value cell shows it, and the study and the case, so the
       pop-up reads as the row does and can list previous runs of similar cases. */
    function decisionsTable(st, c) {
      const rows = st.decisions || [];
      if (!rows.length) return null;
      const cited = AS.cite.hasMarks(rows);
      const where = 'NACA ' + c.naca + ' · ' + AS.stageTitle(st.key) + ' · decision';
      return [h('h4', {}, 'Decisions'), h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Setting'), h('th', {}, 'Value'), h('th', {}, 'Reason'), cited ? h('th', { class: 'cite-col', title: 'Reference' }, 'Ref') : null)),
        h('tbody', {}, rows.map((d, i) => {
          const text = AS.fmtValue(d.value);
          return h('tr', {}, h('td', { class: 'mono' }, d.param), h('td', { class: 'mono' }, text), h('td', {}, d.reason || ''),
            cited ? h('td', { class: 'cite-col' }, AS.cite.decisionMark(d, { key: 'dec:' + c.id + ':' + st.key + ':' + i, stage: st.key, where,
              text, study: c.study_id || (state.study && state.study.id) || '', case: c.id })) : null);
        }))))];
    }
    function gateTable(st) {
      const g = st.gate;
      if (!g) return null;
      return [h('h4', {}, 'Gate checks ', h('span', { class: 'verdict ' + (g.passed ? 'v-pass' : 'v-fail') }, AS.icon(g.passed ? 'check' : 'x', 12), g.passed ? 'passed' : 'failed')),
        h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Check'), h('th', {}, 'Value'), h('th', {}, 'Limit'), h('th', {}, 'Result'))),
          h('tbody', {}, (g.checks || []).map((ch) => h('tr', { class: ch.ok ? '' : 'row-fail' }, h('td', {}, ch.name), h('td', { class: 'mono' }, AS.fmtValue(ch.value)),
            h('td', { class: 'mono' }, AS.fmtValue(ch.limit)), h('td', {}, h('span', { class: 'verdict ' + (ch.ok ? 'v-pass' : 'v-fail') }, AS.icon(ch.ok ? 'check' : 'x', 12), ch.ok ? 'pass' : 'fail')))))))];
    }
    function metricsGrid(st) {
      const entries = Object.entries(st.metrics || {}).filter(([, v]) => v != null && typeof v !== 'object');
      if (!entries.length) return null;
      return [h('h4', {}, 'Metrics'), AS.ui.kv(entries.map(([k, v]) => [AS.humanKey(k), AS.fmtValue(v)]))];
    }
    function artifactList(st) {
      const list = st.artifacts || [];
      if (!list.length) return null;
      return [h('h4', {}, 'Artifacts'), h('ul', { class: 'artifacts' }, list.map((a) => h('li', {}, a.kind === 'image'
        ? h('button', { type: 'button', class: 'link-btn', onclick: () => AS.lightbox(a.url, a.label) }, AS.icon('image', 13), a.label || a.url)
        : h('a', { href: a.url, target: '_blank', rel: 'noopener' }, AS.icon('download', 13), a.label || a.url))))];
    }

    function renderReport(c, st) {
      const s = state.study, a = state.activity[c.id] || {};
      const waiting = st.status === 'awaiting_approval' || (c.status === 'awaiting_approval' && c.current_stage === st.key);
      rHead.parentNode.dataset.stage = st.key; rHead.parentNode.dataset.case = c.id;
      const held = c.status === 'paused' && st.status === 'running';   // paused at a tool boundary
      AS.region(rHead, [c.id, st.key, st.status, st.attempts, st.phase, st.started_at, st.seconds, st.agent, waiting, held], () => [
        h('h2', {}, st.title || AS.stageTitle(st.key), ' ', h('span', { class: 'muted' }, '· NACA ' + c.naca)),
        h('div', { class: 'report-meta' }, AS.ui.chip(waiting ? 'awaiting_approval' : held ? 'paused' : st.status, waiting ? 'waiting for your approval' : null),
          st.attempts > 1 ? h('span', { class: 'attempt' }, 'attempt ' + st.attempts) : null,
          st.status === 'running' ? h('span', { class: 'phase' }, ['gather', 'act', 'verify'].map((p) => h('i', { class: p === st.phase ? 'on' : '' }, p))) : null,
          h('span', { class: 'muted', dataset: st.status === 'running' && !held && st.started_at ? { since: st.started_at } : {} }, AS.fmtSeconds(AS.stageClock(c, st))),
          st.agent ? h('span', { class: 'muted mono', title: 'Subagent definition: wingstudio/agents/' + st.agent }, st.agent) : null),
      ]);
      AS.region(rLive, [c.id, st.key, st.status, a.tool, a.lastTool, a.lastSummary, a.thinking, c.status], () => {
        if (st.status !== 'running') return null;
        return h('div', { class: 'live-line' }, c.status === 'paused' ? h('span', { class: 'muted' }, AS.icon('pause', 12), 'paused at the next tool boundary')
          : AS.ui.spinner(a.tool ? `running ${a.tool}` : 'the agent is deciding the next step'),
          a.lastTool ? h('span', { class: 'muted' }, `last: ${a.lastTool} → ${a.lastSummary || ''}`) : null);
      });
      let failure = st.failure || null;
      if (st.status === 'failed' && !failure) {
        for (let i = state.events.length - 1; i >= 0 && !failure; i--) {
          const ev = state.events[i];
          if (ev.type === 'stage.failed' && ev.case_id === c.id && ev.stage === st.key) failure = (ev.data && ev.data.reason) || null;
        }
      }
      const caseLive = ['pending', 'running', 'paused', 'awaiting_approval'].includes(c.status);
      const stoppedHere = !caseLive && st.status === 'pending' && c.current_stage === st.key;   // stopped before this stage started
      AS.region(rBody, [c.id, st.key, st.status, st.summary, st.decisions, st.gate, st.metrics, st.artifacts, failure, caseLive, stoppedHere], () => {
        const out = [];
        if (st.status === 'stale') {
          // a stage is stale because an earlier one is redone, or because a stop interrupted it (then it may have no hand-off at all)
          const had = st.summary ? 'The hand-off below is from an earlier run and is stale' : 'This stage was interrupted and has no result';
          out.push(h('p', { class: 'note warn' }, caseLive
            ? `${had}: it runs again when the pipeline reaches it.`
            : `${had}: the case was stopped before this stage ran${st.summary ? ' again' : ' to the end'}. Nothing is running now. Use "Re-run this stage" below (or re-run an earlier stage) to continue.`));
        }
        if (st.status === 'failed') {
          out.push(h('p', { class: 'note error' }, 'Stage failed' + (failure ? ': ' + String(failure).replace(/[.\s]+$/, '') + '.' : '.') + ' Re-run it with an instruction, or change the plan values it uses: ',
            h('button', { type: 'button', class: 'link-btn', id: 'open-plan', onclick: () => AS.select({ kind: 'step', key: 'plan' }) }, 'open the plan'), '.'));
        }
        if (st.summary) out.push(h('h4', {}, 'Summary'), h('p', { class: 'summary' }, st.summary));
        else if (stoppedHere) out.push(h('p', { class: 'note warn' }, 'The case was stopped before this stage started. Nothing is running now. Use "Run this stage" below to continue here.'));
        else if (st.status === 'pending') out.push(h('p', { class: 'muted' }, 'This stage has not started yet.'));
        else if (st.status === 'running') out.push(h('p', { class: 'muted' }, 'The hand-off summary, decisions and gate result appear when the agent completes the stage. Follow the activity pane for each step.'));
        out.push(decisionsTable(st, c), gateTable(st), metricsGrid(st), artifactList(st));
        return out;
      });
      const key = c.id + '|' + st.key;
      // Any stage that has run can be re-run, also after the study was stopped or has ended:
      // the backend then starts a new pipeline at that stage (and answers 409 for one that never ran).
      const canRerun = st.status !== 'pending' || c.current_stage === st.key;
      const fallback = waiting && AS.fallbackGated(c);
      // Approve is rebuilt disabled and then removed, so the keyboard would be left on <body>. If it was
      // pressed from the keyboard, the Approve of the next gate of this case gets the focus (see after the
      // region); with Follow on the page has moved to the next stage by then, so the case is what must match.
      AS.region(rActions, [key, waiting, canRerun, busy[key], s.mode, st.status, fallback, s.status, caseLive, !!s.report], () => {
        const out = [];
        if (waiting) {
          out.push(h('div', { class: 'approve-box' }, h('p', {}, h('strong', {}, 'Waiting for you. '), st.status === 'failed'
            ? `The ${AS.stageTitle(st.key)} stage of NACA ${c.naca} failed. Approve to let the agent try it again (correct the plan first if a value caused the failure), or re-run it below with an instruction.`
            : `Approve to let the ${AS.stageTitle(st.key)} agent run this stage for NACA ${c.naca}.`),
            h('button', { type: 'button', class: 'btn primary', id: 'approve-stage', disabled: busy[key] === 'approve', onclick: async (e) => {
              approveFocus = e.detail === 0 ? c.id : '';   // detail 0: pressed with Enter or Space, not the mouse
              busy[key] = 'approve'; AS.invalidate();
              await AS.actions.approveStage(c.id);
              busy[key] = ''; AS.invalidate();
            } }, st.status === 'failed' ? 'Approve retry' : 'Approve stage'),
            fallback ? h('p', { class: 'fallback-note', id: 'fallback-note' }, 'The study is in auto-run, but this case asks before every stage because one of its stages failed earlier. ',
              h('button', { type: 'button', class: 'btn small', id: 'return-auto', disabled: busy[key] === 'auto', title: 'Clears the fallback for every case and lets this one carry on. Recorded as a human intervention.', onclick: async () => {
                busy[key] = 'auto'; AS.invalidate();
                await AS.actions.setMode('auto');
                busy[key] = ''; AS.invalidate();
              } }, 'Return to auto-run')) : null));
        }
        if (canRerun) {
          const box = h('textarea', { rows: 2, id: 'rerun-instruction', placeholder: 'Optional instruction for the agent, for example: use a first cell half as high.', 'aria-label': 'Instruction for the re-run',
            oninput: (e) => { instruction[key] = e.target.value; } });
          box.value = instruction[key] || '';
          const ended = !caseLive, verb = st.status === 'pending' ? 'Run' : 'Re-run';
          out.push(h('details', { class: 'rerun', open: !!instruction[key] || st.status === 'failed' || (ended && (st.status === 'stale' || stoppedHere)) }, h('summary', {}, `${verb} this stage`),
            h('p', { class: 'muted small' }, 'Marks this stage and every later stage as stale and restarts the pipeline here. Previous hand-offs stay in the trace.' +
              (ended ? ' This case is not running: the re-run starts it again' + (s.report ? ', and the report is rewritten when it ends.' : '.') : '')),
            box, h('button', { type: 'button', class: 'btn', id: 'rerun-btn', disabled: busy[key] === 'rerun', onclick: async () => {
              busy[key] = 'rerun'; AS.invalidate();
              const ok = await AS.actions.rerun(c.id, st.key, (instruction[key] || '').trim());
              if (ok !== null) { instruction[key] = ''; state.ui.follow = true; }
              busy[key] = ''; AS.invalidate();
            } }, `${verb} this stage`)));
        }
        return out;
      });
      if (approveFocus) {
        const at = document.activeElement, lost = !at || at === document.body;
        const next = AS.$('#approve-stage', rActions);
        if (approveFocus !== c.id || (!lost && at !== next)) approveFocus = '';   // another case, or the engineer moved on
        // stays armed while the keyboard is on Approve: the button is rebuilt once more before the gate closes
        else if (lost && next && !next.disabled) next.focus({ preventScroll: true });
      }
    }

    function build() {
      vHead = h('div', { class: 'viewer-head' });
      panes.geometry = h('div', { class: 'vpane vp-geometry' });
      panes.frames = h('div', { class: 'pv-frames' });
      panes.pvMsg = h('div', { class: 'pv-msg' });
      panes.snaps = h('div', { class: 'snaps', hidden: true });
      panes.paraview = h('div', { class: 'vpane vp-paraview' }, h('div', { class: 'pv-stage' }, panes.frames, panes.pvMsg), panes.snaps);
      const resHost = h('div', { class: 'chart-host' }), clHost = h('div', { class: 'chart-host' }), cdHost = h('div', { class: 'chart-host' });
      panes.solve = h('div', { class: 'vpane vp-solve' }, h('div', { class: 'solve-info' }),
        h('div', { class: 'solve-grid' }, h('div', { class: 'solve-res' }, h('h4', {}, 'Residuals (log scale)'), resHost),
          h('div', { class: 'solve-forces' }, h('div', {}, h('h4', { title: 'The start-up transient (first 12 % of the iterations) is not drawn' }, 'Lift coefficient ', h('span', { class: 'muted' }, '(after start-up)')), clHost),
            h('div', {}, h('h4', { title: 'The start-up transient (first 12 % of the iterations) is not drawn' }, 'Drag coefficient ', h('span', { class: 'muted' }, '(after start-up)')), cdHost))));
      const cpHost = h('div', { class: 'chart-host' });
      panes.cp = cpHost;
      panes.cpWrap = h('div', { class: 'vpane vp-cp' }, h('h4', {}, 'Pressure coefficient along the chord'), cpHost, h('p', { class: 'muted small cp-note' }));
      const valCpHost = h('div', { class: 'chart-host' });
      panes.valCp = valCpHost;
      panes.valInfo = h('div', { class: 'validate-info' });
      panes.valNotes = h('div', { class: 'validate-notes' });
      panes.valChart = h('div', { class: 'validate-chart', hidden: true }, h('h4', {}, 'Pressure coefficient along the chord: OpenFOAM against XFoil'), valCpHost, h('p', { class: 'muted small cp-note' }));
      panes.validate = h('div', { class: 'vpane vp-validate' }, panes.valInfo, panes.valChart, panes.valNotes);
      panes.none = h('div', { class: 'vpane vp-none' }, h('p', { class: 'muted' }, 'This stage writes solver dictionaries; there is nothing to draw. The settings and their reasons are in the report below.'));
      vBody = h('div', { class: 'viewer-body' }, panes.geometry, panes.paraview, panes.solve, panes.cpWrap, panes.validate, panes.none);
      divider = h('div', { class: 'divider', role: 'separator', 'aria-orientation': 'horizontal', 'aria-label': 'Resize viewer', tabindex: 0 });
      rHead = h('div', { class: 'report-head' }); rLive = h('div', {}); rBody = h('div', { class: 'report-content' }); rActions = h('div', { class: 'report-actions' });
      const report = h('div', { class: 'stage-report' }, rHead, rLive, rBody, rActions);
      el = h('div', { class: 'view view-stage' }, h('div', { class: 'viewer' }, vHead, vBody), divider, report);
      charts.res = AS.chart.line(resHost); charts.cl = AS.chart.line(clHost); charts.cd = AS.chart.line(cdHost); charts.cp = AS.chart.line(cpHost);
      charts.vcp = AS.chart.line(valCpHost);

      // draggable split between viewer and report (also arrow keys on the focused divider)
      // the stored share is applied here without being written back: only a drag or an arrow key stores one
      const applySplit = (pct) => { pct = Math.max(22, Math.min(78, pct)); el.style.setProperty('--viewer-share', pct + '%'); return pct; };
      const setSplit = (pct) => { AS.store.set('split', applySplit(pct)); };
      applySplit(AS.store.get('split', 56));
      divider.addEventListener('pointerdown', (ev) => {
        divider.setPointerCapture(ev.pointerId);
        el.classList.add('dragging');   // stops the iframe from swallowing pointer events
        const move = (e) => { const r = el.getBoundingClientRect(); setSplit((e.clientY - r.top) / r.height * 100); };
        const up = () => { divider.removeEventListener('pointermove', move); divider.removeEventListener('pointerup', up); el.classList.remove('dragging'); };
        divider.addEventListener('pointermove', move); divider.addEventListener('pointerup', up);
      });
      divider.addEventListener('keydown', (e) => {
        const now = parseFloat(el.style.getPropertyValue('--viewer-share')) || 56;
        // a render follows, so anything placed over the page (the quote button) is put back in step
        if (e.key === 'ArrowUp') { setSplit(now - 4); e.preventDefault(); AS.invalidate(); }
        if (e.key === 'ArrowDown') { setSplit(now + 4); e.preventDefault(); AS.invalidate(); }
      });
      return el;
    }

    function render() {
      const ui = state.ui, c = AS.currentCase();
      if (!c || ui.sel.kind !== 'stage') return;
      const st = AS.stageOf(c, ui.sel.key) || c.stages[0];
      const kind = paneFor(st.key);
      const collapsed = ui.viewerCollapsed[st.key] != null ? ui.viewerCollapsed[st.key] : kind === 'none';
      el.classList.toggle('viewer-collapsed', collapsed);
      const v = viewerOf(c);
      const want = v.choice[st.key] || DEFAULT_VIEW[st.key] || DEFAULT_VIEW.mesh;
      const fields = (v.state && Array.isArray(v.state.fields) && v.state.fields.length ? v.state.fields : ['U', 'p']).filter((f) => typeof f === 'string');
      AS.region(vHead, [c.id, st.key, kind, collapsed, postTab, validateTab, kind === 'paraview' && [want, fields, v.status]], () => {
        const tools = [];
        if (st.key === 'post') {
          tools.push(h('div', { class: 'segmented small', role: 'tablist', 'aria-label': 'Post-processing view' },
            [['field', 'Field view'], ['cp', 'Cp chart']].map(([k, label]) => h('button', { type: 'button', role: 'tab', class: 'seg-btn' + (postTab === k ? ' on' : ''), 'aria-selected': postTab === k ? 'true' : 'false',
              dataset: { key: 'posttab:' + k }, onclick: () => { postTab = k; AS.invalidate(); } }, label))));
        }
        if (st.key === 'validate') {
          tools.push(h('div', { class: 'segmented small', role: 'tablist', 'aria-label': 'Validation view' },
            [['compare', 'Comparison and Cp'], ['field', 'Field view']].map(([k, label]) => h('button', { type: 'button', role: 'tab', class: 'seg-btn' + (validateTab === k ? ' on' : ''), 'aria-selected': validateTab === k ? 'true' : 'false',
              dataset: { vtab: k, key: 'validatetab:' + k }, onclick: () => { validateTab = k; AS.invalidate(); } }, label))));
        }
        if (kind === 'paraview' && !collapsed) {
          const ready = v.status === 'ready';
          tools.push(h('div', { class: 'presets', role: 'group', 'aria-label': 'View presets' }, PRESETS.map((p) => h('button', { type: 'button', class: 'seg-btn' + (want.zoom === p.zoom ? ' on' : ''),
            disabled: !ready, 'aria-pressed': want.zoom === p.zoom ? 'true' : 'false', dataset: { preset: p.key }, onclick: () => choose(c, st.key, { zoom: p.zoom }) }, p.label))));
          tools.push(h('label', { class: 'field-select' }, h('span', { class: 'muted' }, 'Show'), h('select', { id: 'field-select', 'aria-label': 'Field shown in the viewer', disabled: !ready,
            onchange: (e) => choose(c, st.key, e.target.value === '__mesh' ? { mode: 'mesh' } : { mode: 'field', field: e.target.value }) },
          h('option', { value: '__mesh', selected: want.mode === 'mesh' }, 'Mesh'),
          fields.map((f) => h('option', { value: f, selected: want.mode === 'field' && want.field === f }, f)))));
        }
        return [h('span', { class: 'viewer-title' }, { geometry: 'Profile', paraview: st.key === 'post' || st.key === 'validate' ? 'Flow field' : st.key === 'physics' ? 'Domain and boundaries' : 'Mesh', solve: 'Convergence', cp: 'Pressure distribution', validate: 'Validation', none: 'Viewer' }[kind],
          h('span', { class: 'muted' }, ' · NACA ' + c.naca)),
        h('div', { class: 'viewer-tools' }, tools),
        h('button', { type: 'button', class: 'btn small icon', id: 'viewer-toggle', title: 'Collapse or expand the viewer ( v )', 'aria-label': 'Toggle viewer', 'aria-expanded': collapsed ? 'false' : 'true',
          onclick: () => { ui.viewerCollapsed[st.key] = !collapsed; AS.invalidate(); } }, AS.icon(collapsed ? 'chevron-down' : 'chevron-up'))];
      });
      const shown = { geometry: panes.geometry, paraview: panes.paraview, solve: panes.solve, cp: panes.cpWrap, validate: panes.validate, none: panes.none };
      for (const k in shown) shown[k].hidden = collapsed || k !== kind;
      vBody.dataset.stage = st.key; vBody.dataset.case = c.id;
      if (!collapsed) {
        if (kind === 'geometry') renderGeometry(c, st);
        else if (kind === 'paraview') renderParaview(c, st);
        else if (kind === 'solve') renderSolve(c, st);
        else if (kind === 'cp') renderCp(c, st);
        else if (kind === 'validate') renderValidate(c, st);
      }
      if (st.key === 'geometry' && st.status === 'passed' && !(state.series[c.id] && state.series[c.id].geometry) && !askedGeometry[c.id]) { askedGeometry[c.id] = true; AS.loadSeries(c.id); }
      renderReport(c, st);
      // One report scroller and one set of viewer panes serve every stage and case. Another stage or
      // case is a new page and starts at its top; an update of the page being read leaves the scroll alone.
      const page = c.id + '|' + st.key;
      if (page !== lastShown) {
        lastShown = page;
        rHead.parentNode.scrollTop = 0;
      }
      // the panes can only be scrolled back while they are laid out: a page opened with the viewer
      // collapsed is reset when the viewer is expanded
      const paneKey = page + '|' + kind;
      if (paneKey !== lastPanes) {
        if (collapsed) lastPanes = '';     // nothing is laid out: the pane is reset when the viewer is expanded
        else {
          lastPanes = paneKey;
          shown[kind].scrollTop = 0;
          for (const inner of AS.$$('.validate-notes, .snaps', shown[kind])) { inner.scrollTop = 0; inner.scrollLeft = 0; }
        }
      }
    }

    /* Viewer iframes belong to one study: drop them when another study is opened. */
    function reset() {
      for (const id in pv) if (pv[id].frame) pv[id].frame.remove();
      if (panes.frames) AS.clear(panes.frames);
      pv = {}; instruction = {}; busy = {}; askedGeometry = {}; lastShown = ''; lastPanes = ''; approveFocus = '';
      if (panes.solve) { panes.solve.__sig = ''; panes.cp.__sig = ''; panes.valCp.__sig = ''; }
    }
    return { build, render, reset, toggleViewer() {
      const ui = state.ui;
      if (ui.sel.kind !== 'stage') return;
      const kind = paneFor(ui.sel.key);
      const now = ui.viewerCollapsed[ui.sel.key] != null ? ui.viewerCollapsed[ui.sel.key] : kind === 'none';
      ui.viewerCollapsed[ui.sel.key] = !now;
      AS.invalidate();
    } };
  })();

  // =====================================================================
  // Status bar
  // =====================================================================
  V.statusbar = (function () {
    let el, left, right;
    function build(root) {
      el = root;
      left = h('div', { class: 'sb-left' }); right = h('div', { class: 'sb-right' });
      AS.append(el, [left, right]);
    }
    function caseLine(c) {
      const a = state.activity[c.id] || {}, st = AS.stageOf(c, c.current_stage);
      const parts = [h('i', { class: 'dot st-' + c.status }), h('b', {}, 'NACA ' + c.naca)];
      if (c.status === 'running' && st) {
        parts.push(h('span', {}, st.title || AS.stageTitle(st.key)));
        if (st.phase) parts.push(h('span', { class: 'sb-phase' }, st.phase));
        parts.push(h('span', { class: 'mono' }, a.tool || (a.thinking ? 'thinking' : 'idle')));
        if (st.key === 'solve' && AS.isNum(a.iteration)) parts.push(h('span', { class: 'mono' }, `iter ${AS.fmtInt(a.iteration)}${a.total ? ' / ' + AS.fmtInt(a.total) : ''}`));
        if (st.started_at) parts.push(h('span', { class: 'mono', dataset: { since: st.started_at } }, AS.fmtDur((Date.now() - Date.parse(st.started_at)) / 1000)));
      } else if (c.status === 'awaiting_approval') {
        parts.push(h('span', { class: 'sb-attn' }, `waiting for approval: ${AS.stageTitle(c.current_stage)}`));
      } else {
        parts.push(h('span', {}, AS.ui.statusText(c.status) + (c.status === 'done' && c.verdict ? ' · ' + c.verdict : c.status === 'paused' && st ? ' at ' + (st.title || st.key) : '')));
      }
      // Shows this case at the stage it is at. Not AS.followLive(): on a finished study that opens the Report instead of the case.
      return h('button', { type: 'button', class: 'sb-case', title: 'Show this case', onclick: () => {
        AS.selectCase(c.id);
        AS.select({ kind: 'stage', key: c.current_stage || (c.stages[0] && c.stages[0].key) || 'geometry' });
      } }, parts);
    }
    function render() {
      const s = state.study, cases = AS.caseList();
      const acts = cases.map((c) => { const a = state.activity[c.id] || {}, st = AS.stageOf(c, c.current_stage); return [c.id, c.status, c.current_stage, c.verdict, st && st.phase, st && st.started_at, a.tool, a.thinking, a.iteration, a.total]; });
      const sa = state.activity._study || {};
      AS.region(left, [s && s.status, acts, sa.tool, sa.thinking, sa.agent], () => {
        if (!s) return h('span', { class: 'muted' }, 'No study open. Describe a mission to begin.');
        if (!cases.length || (s.status === 'running' && cases.every((c) => ['done', 'failed', 'stopped'].includes(c.status)))) {
          const working = AS.isActiveStatus(s.status);
          const line = h('span', { class: 'sb-case' }, h('i', { class: 'dot st-' + (working ? 'running' : s.status === 'awaiting_selection' || s.status === 'awaiting_approval' ? 'awaiting_approval' : 'pending') }),
            h('b', {}, working ? (sa.agent || 'agent') : 'Study'), h('span', {}, AS.ui.statusText(s.status)), working ? h('span', { class: 'mono' }, sa.tool || 'thinking') : null);
          return cases.length ? cases.map(caseLine).concat(line) : line;
        }
        return cases.map(caseLine);
      });
      const gatedCases = cases.filter(AS.fallbackGated).map((c) => 'NACA ' + c.naca);
      AS.region(right, [state.conn, state.lastSeq, s && s.mode, AS.mock.enabled, !!AS.mock.live, gatedCases], () => [
        s ? h('span', { title: 'Run mode' }, 'mode: ', h('b', {}, (AS.MODES.find((m) => m.key === s.mode) || {}).label || s.mode),
          gatedCases.length ? h('span', { class: 'sb-attn', id: 'sb-fallback', title: 'After a failed stage a case asks before every further stage. Setting the mode to Auto-run again clears this.' },
            ` · ${gatedCases.join(', ')} gated after a failed stage`) : null) : null,
        s ? h('span', { class: 'conn conn-' + state.conn, title: 'Event stream' }, h('i', { class: 'dot' }),
          state.conn === 'live' ? 'live' : state.conn === 'reconnecting' ? 'reconnecting…' : state.conn === 'connecting' ? 'connecting…' : 'offline') : null,
        s ? h('span', { class: 'mono muted', title: 'Last event sequence number' }, '#' + state.lastSeq) : null,
        AS.mock.enabled ? h('span', { class: 'badge mock' }, (AS.mock.tutorial ? 'tutorial' : 'mock') + (AS.mock.speed !== 1 ? ' ×' + AS.mock.speed : '')) : null,
        // the live tutorial runs on the real backend: the mock is off, so it gets a badge of its own
        AS.mock.live ? h('span', { class: 'badge live' }, 'live tutorial') : null,
      ]);
    }
    return { build, render };
  })();

  // =====================================================================
  // Compare tab
  // =====================================================================
  V.compare = (function () {
    let el, head, table, chartsWrap, report, cpChart, clChart, built = false, sigCharts = '';
    const colorOf = (i) => AS.chart.SERIES[i % AS.chart.SERIES.length];

    function build(root) {
      el = root;
      head = h('div', { class: 'cmp-cases' });
      table = h('div', {});
      const cpHost = h('div', { class: 'chart-host tall' }), clHost = h('div', { class: 'chart-host tall' });
      chartsWrap = h('div', { class: 'cmp-charts' }, h('div', {}, h('h3', {}, 'Pressure distribution'), h('p', { class: 'muted small' }, 'Solid: OpenFOAM. Dashed: XFoil. Negative Cp is up.'), cpHost),
        h('div', {}, h('h3', {}, 'Lift convergence'), h('p', { class: 'muted small' }, 'Cl against iteration for every case.'), clHost));
      report = h('div', {});
      AS.append(el, [h('div', { class: 'view view-compare' }, h('h2', {}, 'Compare'), head, table, chartsWrap, report)]);
      cpChart = AS.chart.line(cpHost); clChart = AS.chart.line(clHost);
      built = true;
    }

    function caseCard(c, i) {
      const a = state.activity[c.id] || {}, cur = AS.stageOf(c, c.current_stage);
      return h('section', { class: 'cmp-case', dataset: { case: c.id } },
        h('header', {}, h('i', { class: 'key', style: { background: colorOf(i) } }), h('h3', {}, 'NACA ' + c.naca), h('span', { class: 'muted' }, `α ${AS.fmt(c.alpha_deg, 2)}°`),
          AS.ui.chip(c.status), c.verdict ? AS.ui.verdict(c.verdict) : null,
          h('button', { type: 'button', class: 'btn small', onclick: () => AS.selectCase(c.id) }, 'Open')),
        h('ol', { class: 'mini-rail' }, c.stages.map((st) => h('li', { class: 'st-' + AS.stageStatus(c, st), title: `${st.title}: ${AS.ui.statusText(AS.stageStatus(c, st))}${st.headline ? ' – ' + st.headline : ''}` },
          icon(AS.stageStatus(c, st)), h('span', { class: 'mini-title' }, st.title), h('span', { class: 'mini-head' }, st.headline || (st.status === 'running' && st.key === 'solve' && AS.isNum(a.iteration) ? `iter ${a.iteration} / ${a.total}` : '')),
          h('span', { class: 'mini-time' }, st.status === 'pending' ? '' : AS.fmtSeconds(AS.stageClock(c, st))),
          st.attempts > 1 ? h('span', { class: 'attempt' }, '×' + st.attempts) : null))),
        c.status === 'running' && cur ? h('p', { class: 'live-line' }, AS.ui.spinner(`${cur.title}: ${a.tool || 'deciding the next step'}`)) : null);
    }

    function resultsTable(cases) {
      const nums = cases.map(AS.caseNumbers);
      const target = AS.targetLd(state.study);
      const fmt = (v, d) => (AS.isNum(v) ? v.toFixed(d) : '–');
      const group = (label, get, d) => h('tr', {}, h('th', { scope: 'row' }, label), cases.map((c, i) => {
        const [cfd, xf, delta] = get(nums[i], c);
        return [h('td', { class: 'num' }, fmt(cfd, d)), h('td', { class: 'num muted' }, fmt(xf, d)), h('td', { class: 'num' }, delta)];
      }));
      const single = (label, get) => h('tr', {}, h('th', { scope: 'row' }, label), cases.map((c, i) => h('td', { class: 'num', colspan: 3 }, get(c, nums[i]))));
      return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl cmp-tbl' },
        h('thead', {}, h('tr', {}, h('th', {}, ''), cases.map((c, i) => h('th', { colspan: 3, class: 'cmp-col' }, h('i', { class: 'key', style: { background: colorOf(i) } }), 'NACA ' + c.naca))),
          h('tr', { class: 'sub' }, h('th', {}, ''), cases.map(() => [h('th', { class: 'num' }, 'OpenFOAM'), h('th', { class: 'num' }, 'XFoil'), h('th', { class: 'num' }, 'Δ')]))),
        h('tbody', {},
          group('Cl', (n) => [n.cfd.cl, n.xf.cl, AS.fmtClDelta(n.deltas)], 4),
          group('Cd', (n) => [n.cfd.cd, n.xf.cd, AS.fmtPct(n.deltas.cd_pct)], 5),
          group('L/D', (n) => [n.cfd.ld, n.xf.ld, AS.fmtPct(n.deltas.ld_pct)], 1),
          group('Cm', (n) => [n.cfd.cm, n.xf.cm, AS.isNum(n.deltas.cm_abs) ? '±' + Math.abs(n.deltas.cm_abs).toFixed(4) : '–'], 4),
          single('Verdict', (c, n) => AS.ui.verdict(n.verdict)),
          target == null ? null : single(`Target L/D ≥ ${AS.fmt(target)} (RANS)`, (c) => AS.ui.target(AS.targetLdFlag(c))),
          single('Cells', (c) => AS.fmtInt((c.results || {}).cells || AS.passedMetrics(c, 'mesh').cells)),
          single('Average y+', (c) => AS.fmt((c.results || {}).yplus_avg != null ? c.results.yplus_avg : AS.passedMetrics(c, 'post').yplus_avg)),
          single('Iterations', (c) => AS.fmtInt((c.results || {}).iterations || AS.passedMetrics(c, 'solve').iterations)),
          single('Stage time', (c) => AS.fmtSeconds(AS.sum(c.stages.map((st) => (st.status === 'pending' ? 0 : AS.stageSeconds(st)))))),
          single('Tokens (in + out)', (c) => AS.fmtTokens(((c.usage || {}).tokens_in || 0) + ((c.usage || {}).tokens_out || 0))),
          single('Cost', (c) => AS.fmtCost((c.usage || {}).cost_usd || 0)),
          single('Rework / gate failures / human', (c) => { const k = c.counters || {}; return `${k.rework_loops || 0} / ${k.gate_failures || 0} / ${k.human_interventions || 0}`; }))));
    }

    function render() {
      if (!built) return;
      const s = state.study, cases = AS.caseList();
      if (!s || !cases.length) {
        AS.region(head, 'none', () => h('div', { class: 'placeholder' }, 'The comparison appears when the plan is approved and the cases exist.'));
        AS.region(table, 'none', () => null); AS.region(report, 'none', () => null);
        chartsWrap.hidden = true;
        return;
      }
      chartsWrap.hidden = false;
      for (const c of cases) { const sr = state.series[c.id]; if (!sr || !sr.loaded) AS.loadSeries(c.id); }
      AS.region(head, cases.map((c) => [c.id, c.status, c.verdict, c.current_stage, c.stages.map((st) => [st.status, st.headline, st.attempts, st.seconds]),
        (state.activity[c.id] || {}).tool, AS.stageOf(c, 'solve').status === 'running' && (state.activity[c.id] || {}).iteration]), () => cases.map(caseCard));
      const redone = cases.filter((c) => c.stages.some((st) => st.status === 'stale'));
      AS.region(table, [AS.targetLd(s), cases.map((c) => [c.id, c.naca, AS.caseNumbers(c), c.results, c.usage, c.counters, AS.targetLdFlag(c), c.stages.map((st) => [st.status, st.seconds])])], () => [h('h3', {}, 'Results'),
        redone.length ? h('p', { class: 'note warn', id: 'cmp-stale-note' }, `${redone.map((c) => 'NACA ' + c.naca).join(' and ')}: some stages are stale (a stage is being redone, or the case was stopped before they ran again), ` +
          'so the numbers of the earlier run are not shown. They return as the stages pass again.') : null,
        resultsTable(cases)]);
      const sig = JSON.stringify(cases.map((c) => { const sr = state.series[c.id] || {}, d = AS.cpData(c); return [c.id, d.cp && d.cp.upper.length, d.cp && d.cp.upper[d.cp.upper.length >> 1], d.xfoilCp && d.xfoilCp.upper.length, sr.forces && sr.forces.length]; }));
      if (sig !== sigCharts) {
        sigCharts = sig;
        let cp = [];
        const legend = [];
        cases.forEach((c, i) => {
          cp = cp.concat(AS.cpSeries(AS.cpData(c), colorOf(i), colorOf(i), `NACA ${c.naca} `));
          legend.push({ name: 'NACA ' + c.naca, color: colorOf(i) });
        });
        legend.push({ name: 'XFoil (dashed)', color: 'var(--text-2)', dash: true });
        cpChart.update({ yInvert: true, xLabel: 'x / c', yLabel: 'Cp (negative up)', xMin: 0, xMax: 1, legend, series: cp, fmtY: (v) => v.toFixed(3), fmtX: (v) => v.toFixed(3),
          emptyText: 'Cp appears when the Post-process stage has extracted it' });
        clChart.update({ xLabel: 'iteration', yLabel: 'Cl', fmtX: AS.fmtInt, fmtY: (v) => v.toFixed(4), legend: cases.map((c, i) => ({ name: 'NACA ' + c.naca, color: colorOf(i) })),
          series: cases.map((c, i) => { const f = (state.series[c.id] || {}).forces || []; return { name: 'NACA ' + c.naca, color: colorOf(i), points: f.slice(Math.floor(f.length * 0.1)).map((r) => [r.iter, r.cl]) }; }),
          emptyText: 'Force history appears when the solvers run' });
      }
      AS.region(report, [s.status, s.report], () => (s.report
        ? [h('h3', {}, 'Final report'), AS.reportBlock(s), h('div', { class: 'row actions' },
          h('button', { type: 'button', class: 'btn primary', onclick: () => AS.setTab('dashboard') }, 'Open the dashboard'),
          h('button', { type: 'button', class: 'btn', onclick: () => AS.runWithAnotherModel() }, 'Run this mission with another model'))]
        : h('p', { class: 'muted' }, 'The final report and the recommendation appear here when every case has finished.')));
    }
    function reset() { sigCharts = ''; }
    return { build, render, reset };
  })();
})();
