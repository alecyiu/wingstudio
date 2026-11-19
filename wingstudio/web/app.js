/* Wing Studio - bootstrap: builds the page once, then redraws the parts
   whose inputs changed whenever AS.invalidate() is called. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, state = AS.state, V = AS.views;
  const $ = AS.$;

  V.topbar.build($('#topbar'));
  V.rail.build($('#rail'));
  V.chat.build($('#chat'));
  V.statusbar.build($('#statusbar'));
  const banner = h('div', { class: 'banner-slot' });
  const views = { mission: V.mission.build(), suggest: V.suggest.build(), plan: V.plan.build(), report: V.report.build(), stage: V.stage.build() };
  AS.append($('#pane-main'), [banner, Object.values(views)]);
  V.compare.build($('#pane-compare'));
  V.dashboard.build($('#pane-dashboard'));
  let fatal = null, lastShown = '';

  function activeView() {
    const ui = state.ui;
    if (!state.study) return 'mission';
    if (ui.sel.kind === 'stage') return AS.currentCase() ? 'stage' : 'plan';
    return views[ui.sel.key] ? ui.sel.key : 'mission';
  }

  function renderBanner() {
    const s = state.study, ui = state.ui;
    // Every waiting case is listed, whatever is selected: the row must not come and go with clicks
    // in the tree, or the viewer below it is resized each time.
    const waiting = AS.caseList().filter((c) => c.status === 'awaiting_approval');
    const failedAt = (c) => (AS.stageOf(c, c.current_stage) || {}).status === 'failed';
    const onScreen = (c) => ui.caseId === c.id && ui.sel.kind === 'stage' && ui.sel.key === c.current_stage;
    const holdRow = !!s && s.mode === 'gate' && (s.status === 'running' || s.status === 'paused') && AS.caseList().length > 0;
    AS.region(banner, [fatal, state.loading, state.conn, s && s.status, holdRow, waiting.map((c) => [c.id, c.naca, c.current_stage, failedAt(c), AS.fallbackGated(c), onScreen(c)])], () => {
      const out = [];
      if (fatal) {
        out.push(h('div', { class: 'note error banner' }, h('span', {}, fatal + ' '),
          h('button', { type: 'button', class: 'btn small', onclick: () => location.reload() }, 'Retry'),
          AS.mock.enabled ? null : h('a', { class: 'btn small', href: location.pathname + '?mock=1' }, 'Open with sample data'),
          // the tutorial runs on the mock as well, so it is offered when the API fails
          AS.mock.enabled ? null : AS.ui.tutorialLink({ class: 'btn small' }, 'Start the tutorial')));
      }
      if (state.loading) out.push(h('div', { class: 'note info banner' }, AS.ui.spinner('Opening the study and replaying its events')));
      if (s && state.conn === 'reconnecting') out.push(h('div', { class: 'note warn banner' }, AS.ui.spinner('The connection to the server was lost. Reconnecting; nothing is lost, the event log is replayed from the last event received.')));
      if (waiting.length) {
        // one row of fixed height for all of them; the full sentence is the tooltip of each entry
        out.push(h('div', { class: 'note warn banner banner-wait' }, h('strong', {}, 'Waiting for you:'), waiting.map((c) => {
          const stage = AS.stageTitle(c.current_stage);
          const full = `NACA ${c.naca} is waiting for you: ` + (failedAt(c) ? `"${stage}" failed; approve to retry it.` : `approve to run "${stage}".`)
            + (AS.fallbackGated(c) ? ' (Auto-run asks before every stage of this case after a failed stage.)' : '');
          return h('span', { class: 'wait-item', title: full },
            h('span', { class: 'wait-label' }, h('span', { class: 'wait-code' }, 'NACA ' + c.naca), h('span', { class: 'wait-stage' }, ': ' + stage + (failedAt(c) ? ' failed, approve to retry' : ''))),
            h('button', { type: 'button', class: 'btn small', disabled: onScreen(c), dataset: { key: 'wait-show:' + c.id }, onclick: () => { AS.selectCase(c.id); AS.select({ kind: 'stage', key: c.current_stage }); } }, 'Show'),
            h('button', { type: 'button', class: 'btn small primary', dataset: { key: 'wait-approve:' + c.id }, onclick: () => AS.actions.approveStage(c.id) }, 'Approve'));
        })));
      }
      else if (holdRow) {
        // Gate mode between two approvals: the row keeps its height, so the viewer and the report
        // below it do not move up and down with every approval.
        out.push(h('div', { class: 'note banner banner-wait banner-idle' }, h('span', { class: 'muted' }, 'Gate mode: no stage is waiting for approval right now.')));
      }
      if (s && s.status === 'stopped') out.push(h('div', { class: 'note info banner' }, 'This study was stopped. Finished stages and files are kept; re-run a stage to continue, or start a new study.'));
      if (s && s.status === 'failed') out.push(h('div', { class: 'note error banner' }, 'This study failed. Open the failed stage for the reason, then re-run it with an instruction.'));
      return out;
    });
  }

  function render() {
    const ui = state.ui, s = state.study;
    $('#pane-main').hidden = ui.tab !== 'main';
    $('#pane-compare').hidden = ui.tab !== 'compare';
    $('#pane-dashboard').hidden = ui.tab !== 'dashboard';
    const active = activeView();
    for (const k in views) views[k].hidden = k !== active;
    // A setup page shown anew (from the tree, for another study, after New) starts at its top, as the
    // stage reports do. Only a visible page can be scrolled, and a round trip to Compare or Dashboard
    // is not a new page, so the key is compared on the main tab only.
    const shownKey = (s ? s.id : '') + '|' + active;
    if (ui.tab === 'main' && shownKey !== lastShown) { lastShown = shownKey; if (active !== 'stage') views[active].scrollTop = 0; }
    V.topbar.render();
    V.rail.render();
    renderBanner();
    V.mission.render();                       // also keeps the form in step with the open study while hidden
    if (ui.tab === 'main' && active !== 'mission') V[active].render();
    if (ui.tab === 'compare') V.compare.render();
    if (ui.tab === 'dashboard') V.dashboard.render();
    V.chat.render();
    V.statusbar.render();
    const title = AS.studyTitle(s);           // never cut inside a number (js/util.js)
    document.title = (title ? title + ' – ' : '') + 'Wing Studio';
    AS.syncHash();                            // last: the address describes the page just drawn (store.js)
  }
  AS.on('render', render);
  AS.on('reset', () => { V.stage.reset(); V.compare.reset(); });

  // running clocks: updated in place once a second, without rebuilding anything
  setInterval(() => {
    const now = Date.now();
    for (const node of AS.$$('[data-since]')) node.textContent = AS.fmtDur((now - Date.parse(node.dataset.since)) / 1000);
    const clock = $('[data-clock=run]');
    if (clock) clock.textContent = AS.fmtDur(AS.runSeconds());
  }, 1000);

  // keyboard: [ rail, ] chat, v viewer, 1-9 tabs, / composer, Esc closes overlays
  document.addEventListener('keydown', (e) => {
    // A held Enter or Space on the focused control would press it again and again (and confirm the
    // two-step Stop by itself).
    if (e.repeat && (e.key === 'Enter' || e.key === ' ') && e.target && e.target.closest && e.target.closest('button, [role=tab], summary')) { e.preventDefault(); return; }
    if (e.key === 'Escape') {
      const box = $('#lightbox');
      if (!box.hidden) { AS.closeLightbox(); e.preventDefault(); }
      $('#quote-btn').hidden = true;
      return;
    }
    if (!$('#lightbox').hidden) {
      // The snapshot dialog is modal: no shortcut changes the page behind it, and Tab stays inside it.
      if (e.key === 'Tab') {
        const stops = AS.$$('a[href], button:not([disabled])', $('#lightbox'));
        if (stops.length) {
          const i = stops.indexOf(document.activeElement);
          e.preventDefault();
          stops[(i + (e.shiftKey ? -1 : 1) + stops.length) % stops.length].focus();
        }
      }
      return;
    }
    const t = e.target;
    if (e.metaKey || e.ctrlKey || e.altKey || (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)))) return;
    const ui = state.ui;
    // [ ] and v move the selected text without a selection or scroll event, which would leave the
    // "Quote in chat" button behind at its old place
    const moved = () => { $('#quote-btn').hidden = true; };
    if (e.key === '[') { ui.railCollapsed = !ui.railCollapsed; AS.store.set('rail', ui.railCollapsed); moved(); AS.invalidate(); }
    else if (e.key === ']') { ui.chatCollapsed = !ui.chatCollapsed; AS.store.set('chat', ui.chatCollapsed); moved(); AS.invalidate(); }
    else if (e.key === 'v') { if (ui.tab === 'main' && ui.sel.kind === 'stage') { V.stage.toggleViewer(); moved(); } }   // only the viewer that is on screen: Compare and Dashboard keep the stage selected
    else if (e.key === '/') { e.preventDefault(); if (ui.chatCollapsed) { ui.chatCollapsed = false; AS.store.set('chat', false); render(); } $('#composer').focus(); }
    else if (/^[1-9]$/.test(e.key)) {
      // the n-th tab on top of the document area; a disabled tab keeps its number and does nothing
      const tab = AS.$$('#doctabs .tab')[parseInt(e.key, 10) - 1];
      if (tab && !tab.disabled) tab.click();
    }
  });

  // Back, Forward or an edited address. One Back step fires both events; AS.applyHash answers the
  // second with the open that is already under way.
  window.addEventListener('hashchange', () => { AS.applyHash(); });
  window.addEventListener('popstate', () => { AS.applyHash(); });

  async function start() {
    render();
    try {
      state.config = await AS.api.get('/api/config');
      await AS.loadProjects();
    } catch (e) {
      fatal = `${e.message}. Start the server (wing-studio) and reload` + (AS.mock.enabled ? '.' : ', or open the page with sample data (?mock=1), which runs no simulation.');
      render();
      return;
    }
    await AS.applyHash();                // the study, tab, case and view the address names, if any
    AS.invalidate();
  }
  start();
})();
