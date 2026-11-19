/* Wing Studio - the guided tutorial: ?tutorial=1 (recorded, on js/mock.js), ?tutorial=reset (the
   same from a clean slate) and ?tutorial=live (the real backend, real model calls and cost).

   30 steps in six chapters: a spotlight on the control to use, an arrow, and a card that says
   what to do. To stay in step with the app, no node is stored (a step's target is resolved
   again on every animation frame) and a step is complete when AS.state or the DOM says so, never
   because of a click or a timer. The recorded run holds the mock at named checkpoints while a
   card is read. Nothing is written to localStorage; progress is in sessionStorage
   (as.tutorial.v1, as.tutorial.live.v1).

   The live list cannot hold the run: it has its own order, reads the failure from the events
   and watches the run beside the steps. Nothing that spends is sent before its first card's
   Start button, and the tutorial itself never pauses, messages, re-runs or approves; the one
   request it sends alone is Stop at the cost limit, to its own study. ?tutorial=1&steps=live
   plays the live list on the mock, for tests. Outside tutorial mode this file only sets
   AS.tutorial = { enabled: false, active: false, mode: null }. */
(function () {
  'use strict';
  const AS = window.AS;
  const query = new URLSearchParams(location.search);
  const LIVE = query.get('tutorial') === 'live';
  if (!LIVE && !(AS.mock && AS.mock.tutorial)) { AS.tutorial = { enabled: false, active: false, mode: null }; return; }
  // a stale mock.js takes every tutorial value for the recorded one: recorded data must never pass as a live run
  const BLOCKED = LIVE && !!(AS.mock && AS.mock.enabled);
  const LIVE_STEPS = LIVE || query.get('steps') === 'live';   // the live step list; on the mock it is a test of that list
  const TEST = LIVE_STEPS && !LIVE;
  const h = AS.h, $ = AS.$, $$ = AS.$$, state = AS.state;
  const SVG = 'http://www.w3.org/2000/svg';
  const SONNET = 'anthropic/claude-sonnet-5-5', HAIKU = 'anthropic/claude-haiku-5-5';
  // the model of a live run is fixed by the address, never by the picker (it is under the shade)
  const MODEL_ARG = new Map([['haiku', HAIKU], ['sonnet', SONNET], ['scripted', 'scripted']]);
  const PAGE_MODEL = LIVE ? MODEL_ARG.get(query.get('model')) || HAIKU : SONNET;

  // ---------- preferences stay in memory ----------
  /* Model, project, rail, chat and split are remembered in localStorage by the app. In tutorial
     mode reads still see the real values, but every write stays in this object. */
  const mem = { model: PAGE_MODEL, rail: false, chat: false };
  const storedGet = AS.store.get;
  AS.store.get = (key, fallback) => (key in mem ? mem[key] : storedGet(key, fallback));
  AS.store.set = (key, value) => { mem[key] = value; };
  // store.js read the real values before this file loaded
  state.ui.railCollapsed = false; state.ui.chatCollapsed = false;

  // ---------- small helpers ----------
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function until(fn, ms) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(50); }
    return !!fn();
  }
  function safe(fn, fallback) { try { return fn(); } catch (e) { console.error('tutorial', e); return fallback; } }
  const laidOut = AS.laidOut;
  /* The first element matching the selector that is laid out (hidden views stay in the DOM). */
  const q = (selector) => $$(selector).find(laidOut) || null;
  const byId = (id) => document.getElementById(id);

  /* The mock's side of the contract (js/mock.js); a missing function is reported by selfTest. */
  const mock = {
    hold(name) { if (typeof AS.mock.hold === 'function') AS.mock.hold(name); },
    release(name) { if (typeof AS.mock.release === 'function') { if (name === undefined) AS.mock.release(); else AS.mock.release(name); } },
    holding() { return (typeof AS.mock.holding === 'function' && AS.mock.holding()) || []; },
    yplus(reynolds, height) { return typeof AS.mock.yplus === 'function' ? AS.mock.yplus(reynolds, height) : NaN; },
  };

  /* A click made by the tutorial itself (Do it for me, a set-up) must pass its own click guard. */
  let bypass = 0;
  function press(el) {
    if (!el || el.disabled) return false;
    bypass += 1;
    try { el.click(); } finally { bypass -= 1; }
    return true;
  }

  // ---------- what the steps read from the study ----------
  const FIELD = 'p-cases-0-stages-mesh-first_cell_height';
  const BAD_VALUE = 0.012;
  const study = () => state.study;
  const planCase = (i) => { const s = study(); return (s && s.plan && s.plan.cases && s.plan.cases[i]) || null; };
  const meshPlan = () => { const pc = planCase(0); return (pc && pc.stages && pc.stages.mesh) || null; };
  const typedHeight = () => { const m = meshPlan(); return m ? Number(m.first_cell_height) : NaN; };
  const heightReason = () => { const m = meshPlan(); return (m && m.reasons && m.reasons.first_cell_height) || ''; };
  const engineerSet = () => heightReason().startsWith(AS.ENGINEER_MARK);
  const predicted = (height) => mock.yplus(Number(((study() || {}).requirements || {}).reynolds), height);
  const nacaOf = (i, c) => (c ? c.naca : (planCase(i) || {}).naca) || '';
  const meshOf = (c) => AS.stageOf(c, 'mesh') || {};
  const failedCheck = (c) => ((meshOf(c).gate || {}).checks || []).find((ch) => ch.ok === false) || null;
  const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven'];

  const ENDED = ['done', 'failed', 'stopped'], ACTIVE = ['suggesting', 'planning', 'running', 'paused'];
  const ended = (c) => !!c && ENDED.includes(c.status);

  /* The candidate ticks of the Airfoil suggestion page. They take the reader's clicks on the
     candidates and confirm steps; R.picked records the first one, and from then on the tutorial
     ticks nothing itself. The label and the box both, so a click on the name and Tab both reach it. */
  const PICK = '#pane-main .view-suggest .cand-pick, #pane-main .view-suggest .cand-pick input';
  const pickBox = (code) => q('#pane-main .view-suggest input[type=checkbox][aria-label="Simulate NACA ' + code + '"]');
  function ticked() {
    return ((study() || {}).candidates || []).map((cd) => String(cd.naca)).filter((code) => { const box = pickBox(code); return !!box && box.checked; });
  }
  const ranked = () => ((study() || {}).candidates || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
  /* Ticks exactly `codes`. A few presses at most per step: a box that does not take the click must not be pressed on every frame. */
  function setTicks(c, codes) {
    for (const cd of (study() || {}).candidates || []) {
      const code = String(cd.naca), box = pickBox(code);
      if (box && box.checked !== codes.includes(code) && (c.local.presses = (c.local.presses || 0) + 1) <= 12) press(box);
    }
  }
  function notePick(c, e) {
    if (!R.picked && e.target && e.target.closest && e.target.closest(PICK)) { R.picked = true; save(); }
  }
  function tickedLine() {
    const codes = ticked();
    return (codes.length ? `${nacaList(codes)} ${codes.length === 1 ? 'is' : 'are'} ticked, and each` : 'Each') + ' ticked section becomes its own case. ';
  }

  // ---------- live mode: figures, record and what the events say ----------
  /* Shown on the consent card. Measured two-case studies cost 0.05 to 0.07 USD with Haiku; the
     estimate adds the retry, the steering answer and a margin. The limit is where the cost watch
     presses Stop; rerun is one more pass from Mesh to Validate (the optional step see-gate). */
  const LIVE_RUN = {
    project: { name: 'Tutorial', description: 'Runs started by the guided tutorial. Kept apart so they do not count in your own project.' },
    nacas: ['2412', '4412'],      // the pair of the recorded tutorial, ticked when the advisor shortlists both
    models: {
      [HAIKU]: { estimate: '0.10', limit: 0.30, rerun: '0.03' },
      [SONNET]: { estimate: '0.90', limit: 3.00, rerun: '0.40' },
      scripted: { estimate: '0', limit: Infinity, rerun: '0' },
    },
  };
  /* The recorded example with one more sentence. The form values win over the prompt on the server,
     so the sentence cannot change a requirement; the study title is the start of the prompt. */
  const LIVE_PRESET = { id: 'tutorial', label: 'Tutorial: glider cruise',
    prompt: 'Tutorial: wing section for a motor glider. Mach 0.09 at about 1,000 m (density 1.112 kg/m3), Reynolds number 1.5 million, design lift coefficient 0.5. ' +
      'Maximise L/D, at least 12% thick, |Cm| no more than 0.10, target L/D of at least 46. Include NACA 2412 and NACA 4412 in the shortlist.',
    requirements: { mach: 0.09, density: 1.112, reynolds: 1.5e6, design_cl: 0.5, objective: 'max_ld', min_thickness: 0.12, max_abs_cm: 0.1, target_ld: 46, notes: '' } };
  const figures = (model) => (Object.prototype.hasOwnProperty.call(LIVE_RUN.models, model) ? LIVE_RUN.models[model] : LIVE_RUN.models[SONNET]);   // an unknown model gets the larger figures
  function ceilingFor(model) {
    const limit = figures(model).limit, asked = query.has('ceiling') ? Number(query.get('ceiling')) : NaN;   // &ceiling= can only lower it (tests)
    return isFinite(asked) && asked >= 0 ? Math.min(limit, asked) : limit;
  }
  const money = (usd) => (AS.isNum(usd) ? usd.toFixed(2) : '');
  /* The live record, written to sessionStorage on every change: what a reload needs to go on.
     study: the id of the tutorial's own study, set when the reader's press on Suggest airfoils has
     created it, or on a resume; the cost watch acts on no other. stopSent: the limit was reached,
     so a Stop is owed until the study has left its active states (stopDone). mismatch: the model
     the study runs with when it is not the one of the consent card. picked: the reader has ticked
     a candidate, so the tutorial ticks none. */
  const freshRecord = (model) => ({ step: null, study: null, project: null, model, ceiling_usd: ceilingFor(model), consent: null, planned: null, picked: false,
    seqAtSend: null, sentText: null, sendCase: null, sawPaused: false, seqAtRerun: null, gateOffered: false, stopSent: false, stopDone: false, mismatch: null, ackSeq: 0, done: false });
  let R = freshRecord(PAGE_MODEL);
  let projectNote = '';      // shown once when the Tutorial project could not be created

  /* What the events of the open study say, worked out again only when an event has arrived. Every
     condition on the failure reads this and never stages[].gate: the reducer clears the gate when
     the next attempt starts, and the engine starts it before the failed tool result arrives. */
  let factsFor = '', factsNow = null;
  function facts() {
    const s = study(), key = (s ? s.id : '') + '|' + state.events.length + '|' + state.lastSeq;
    if (factsNow && key === factsFor) return factsNow;
    const f = { T0: 0, gateFail: {}, error: null, planningSeq: 0, suggesting: false, failed: [], completed: null, stopReason: '', acted: {}, actedCount: 0 };
    for (const ev of state.events) {
      const d = ev.data || {};
      if (ev.type === 'plan.approved') { if (!f.T0) f.T0 = ev.seq; }
      else if (ev.type === 'gate.result') { if (f.T0 && ev.case_id && (ev.stage || d.stage) === 'mesh' && d.passed === false) f.gateFail[ev.case_id] = ev; }
      else if (ev.type === 'error') f.error = ev;
      else if (ev.type === 'study.status') { if (d.status === 'planning') f.planningSeq = ev.seq; else if (d.status === 'suggesting') f.suggesting = true; }
      else if (ev.type === 'stage.failed') f.failed.push(ev);
      else if (ev.type === 'study.completed') f.completed = ev;
      else if (ev.type === 'case.status') { if (d.status === 'stopped' && d.reason) f.stopReason = String(d.reason); }
      // before approval there is no case to count an action on
      else if (ev.type === 'human.intervention') { if (f.T0 && d.kind) { f.acted[d.kind] = (f.acted[d.kind] || 0) + 1; f.actedCount += 1; } }
    }
    factsFor = key; factsNow = f;
    return f;
  }
  const gateFail = (c) => (c && facts().gateFail[c.id]) || null;
  /* The flat-plate estimate of planning.predicted_yplus (372 for 0.012 at Re 1.5e6), and the band the gate checks. */
  const yplus = (re, height) => (height / 2) * re * Math.sqrt(0.013 / Math.pow(re, 1 / 7));
  const band = (pc) => ((((pc || {}).stages || {}).physics || {}).wall_treatment === 'resolved' ? [0, 5] : [20, 200]);
  const liveYplus = (height) => yplus(Number(((study() || {}).requirements || {}).reynolds), Number(height));
  const tooTall = () => engineerSet() && liveYplus(typedHeight()) > band(planCase(0))[1];

  /* What every step function is handed. */
  const ctx = {
    planned: null, seqAtSend: 0, sawPaused: false, enteredAt: 0, local: {}, resume: null,
    /* The case objects are replaced by every snapshot, so they are looked up on every access. */
    get bad() { const naca = nacaOf(0); return AS.caseList().find((c) => String(c.naca) === String(naca)) || null; },
    get good() {
      const naca = nacaOf(0), others = AS.caseList().filter((c) => String(c.naca) !== String(naca));
      if (!LIVE_STEPS) return others[0] || null;
      // a real case can stop or fail: the first other case still worth showing, else the bad one itself
      return others.find((c) => c.status !== 'stopped' && c.status !== 'failed') || this.bad;
    },
    get model() { return (study() || {}).model || R.model || PAGE_MODEL; },
    get paid() { return LIVE && this.model !== 'scripted'; },
    cost() { return Number((((study() || {}).usage) || {}).cost_usd) || 0; },
    get branch() {
      const bad = this.bad, status = meshOf(bad).status;
      return gateFail(bad) ? 'rejected' : status === 'passed' ? 'fixed-first' : status === 'failed' ? 'failed' : 'pending';
    },
  };

  /* A look step of a live run pins its page: the case, the stage, and Follow off, or the page
     would move on with the run while the card is read. A call that changes nothing redraws nothing. */
  function pin(c, key) {
    if (!c) return;
    const ui = state.ui;
    if (ui.caseId !== c.id || ui.tab !== 'main') AS.selectCase(c.id);
    if (ui.tab !== 'main' || ui.sel.kind !== 'stage' || ui.sel.key !== key) AS.select({ kind: 'stage', key });
    if (ui.follow) { ui.follow = false; AS.invalidate(); }
  }
  function follow(c) {
    const ui = state.ui;
    if (c && (ui.caseId !== c.id || ui.tab !== 'main')) AS.selectCase(c.id);
    if (!ui.follow || ui.tab !== 'main') AS.followLive();
  }

  function openChat() {
    if (state.ui.chatCollapsed) { state.ui.chatCollapsed = false; AS.invalidate(); }
  }
  function showStep(key) {
    const ui = state.ui;
    if (ui.tab !== 'main' || ui.sel.kind !== 'step' || ui.sel.key !== key) AS.select({ kind: 'step', key });
  }
  /* Put one stage of one case on screen; a call that changes nothing causes no redraw. */
  function showStage(c, key, follow) {
    if (!c) return;
    const ui = state.ui;
    if (ui.caseId !== c.id || ui.tab !== 'main') AS.selectCase(c.id);
    if (ui.tab !== 'main' || ui.sel.kind !== 'stage' || ui.sel.key !== key) AS.select({ kind: 'stage', key }, follow ? { follow: true } : undefined);
    else if (follow && !ui.follow) ui.follow = true;
  }
  function lastAgentLine(c) {
    let any = '';
    for (let i = state.events.length - 1; i >= 0; i--) {
      const ev = state.events[i];
      if (ev.type !== 'agent.message' || ev.case_id !== c.id || !ev.data || !ev.data.text) continue;
      if (ev.stage === 'solve') return String(ev.data.text).slice(0, 200);
      any = any || String(ev.data.text).slice(0, 200);
    }
    return any;
  }

  // ---------- the steps ----------
  /* { id, chapter, kind: do | look | wait, title, body, note, target, frame, also, place, allow, interactive,
       ready, setup, leave, done, auto, hint, tick, timeout }
     target: a selector, or a function giving a selector or an element; the arrow points at it.
     frame: selector for target.closest(), the hole when it is larger than the target.
     also: a selector or a function giving a node beside the target that the hole takes in as well.
     allow: the selectors that accept input on a do step or an interactive look step (default: the target).
     ready: the card is docked with a waiting line until this is true.
     setup: idempotent; runs on entry, again when ready() turns true and when the target is missing.
     tick: called on every pass of the loop while the step is shown. */
  const PAUSE_RESUME = '#topbar [data-key="pause-resume"]';
  /* The plan step shows where a value comes from: the reference mark beside first cell height. */
  const meshGroup = () => '#pane-main .view-plan .plan-case[data-naca="' + nacaOf(0) + '"] .plan-stage[data-stage="mesh"]';
  const citeMark = () => q('#pane-main .view-plan [data-cite="plan:cases-0-stages-mesh-first_cell_height"]');
  const citeLine = () => (citeMark() ? ' The small book beside a value opens where it comes from: the reason, the source with a quote, and the document itself. Try the one next to first cell height.' : '');
  /* The last sentence of both Dashboard cards. It names no figure, so it holds for every criterion and filter of that panel. */
  const PASS_K = ' Further down, Reliability (pass^k) estimates the chance that k runs of a model all pass, whatever the airfoil.';
  const STEPS = [
    // ----- Mission -----
    { id: 'welcome', chapter: 'Mission', kind: 'look', place: 'centre', title: 'Tutorial', nextLabel: 'Start', noLeave: true,
      body: 'A recorded study replayed in this page: no simulation runs, no API key is used, and the costs shown are simulated. In about five minutes you plan two airfoils, set one bad value on purpose and watch the gate catch it.',
      note: () => (window.innerWidth <= 1180 ? 'Make the window wider for the tutorial.' : ''),   // at 1180 the stage counts on the tabs are hidden
      actions: [{ label: 'Not now', run: () => api.exit() }],
      setup() {
        const ui = state.ui, def = state.config && state.config.default_project_id;
        if (state.study) AS.newStudy();
        ui.railCollapsed = false; ui.chatCollapsed = false; ui.chatScope = 'case'; ui.chatVerbose = false;
        if (def && state.projectId !== def && state.projects.some((p) => p.id === def)) AS.setProject(def);   // the write is kept in memory
        mock.release();
        AS.invalidate();
      } },
    { id: 'example', chapter: 'Mission', kind: 'do', place: 'bottom', title: 'Start from an example',
      body: 'Click the example. It fills in the flight condition, the objective and a target L/D of 46.',
      target: '#pane-main .view-mission .chips .chip-btn[data-tour="preset:tutorial"]',
      setup() { showStep('mission'); },
      done() {
        const re = byId('f-reynolds'), ld = byId('f-target_ld'), prompt = byId('mission-prompt');
        return !!re && re.value === '1.5e6' && !!ld && ld.value !== '' && !!prompt && prompt.value.trim() !== '';
      },
      auto() { press(q(this.target)); } },
    { id: 'suggest', chapter: 'Mission', kind: 'do', place: 'top', title: 'Ask the advisor',
      body: 'The model chosen here plays every agent; the tutorial uses Sonnet 5.5. Press Suggest airfoils: the advisor screens candidates in XFoil and nothing is simulated yet.',
      target: '#suggest-btn', frame: '.row', allow: ['#suggest-btn'],
      setup() {
        showStep('mission');
        // the mock forces this model anyway; the picker must not show another one
        const radio = $('input[name=model][value="' + SONNET + '"]');
        if (radio) radio.checked = true;
      },
      done() { return !!study() && study().status !== 'draft'; },
      auto() { press(q('#suggest-btn')); } },
    { id: 'advisor', chapter: 'Mission', kind: 'wait', place: 'left', title: 'The advisor at work',
      body: 'The advisor reads the mission and screens five NACA sections in XFoil. Everything an agent says or does is listed in this log.',
      target: '#chat .chat-log',
      setup: openChat,
      done() { const s = study(); return !!s && s.status === 'awaiting_selection' && (s.candidates || []).length === 5; } },
    { id: 'candidates', chapter: 'Mission', kind: 'look', place: 'bottom', title: 'What the screen found',
      body: 'Five sections, ranked for your objective, each with its XFoil numbers at the design lift. A missed limit or target is written on the card and never removes a candidate. Tick the sections you want to simulate.',
      target: '#pane-main .view-suggest .cand-grid',
      interactive: true, allow: [PICK], onUse: notePick,
      setup() {
        showStep('suggest');
        const view = $('#pane-main .view-suggest');
        if (view) view.scrollTop = 0;
      } },
    // the recorded story follows two cases, so Confirm takes a click only while two are ticked
    { id: 'confirm', chapter: 'Mission', kind: 'do', place: 'top', title: 'Two airfoils, two cases',
      body: () => tickedLine() + (ticked().length === 2 ? 'Press Confirm and plan.' : 'This recorded tutorial follows two cases: tick two, then press Confirm and plan.'),
      target: '#confirm-btn', also: '#pane-main .view-suggest .cand-grid',
      allow: [PICK, () => (ticked().length === 2 ? '#confirm-btn' : null)], onUse: notePick,
      done() { const s = study(); return !!s && ['planning', 'awaiting_approval'].includes(s.status); },
      auto(c) {
        if (ticked().length !== 2) setTicks(c, ranked().slice(0, 2).map((cd) => String(cd.naca)));
        press(q('#confirm-btn'));
      } },

    // ----- Plan -----
    { id: 'plan', chapter: 'Plan', kind: 'look', place: 'right', title: 'Read the plan first', waitText: 'The planner is writing the plan',
      body: () => 'Each case lists every setting with its reason and, under Acceptance, what the gate will check. For the mesh that includes a predicted y+ between 20 and 200.' + citeLine(),
      // the arrow goes to the reference mark of first cell height (js/cite.js) and the hole stays on the
      // mesh group; a plan without citations has no mark, and the arrow points at the group as before
      target: () => citeMark() || meshGroup(),
      frame: '.plan-stage',
      // the mark may be opened while the card is read; everything else in the hole stays locked
      interactive: true, allow: [() => meshGroup() + ' .cite-btn', '#cite-pop', '#cite-pop a[href]', '#cite-pop button'],
      leave() { AS.cite.close(); },
      // the plan content arrives with the snapshot that follows plan.created
      ready() { const s = study(); return !!s && s.status === 'awaiting_approval' && !!byId(FIELD); },
      setup(c) {
        showStep('plan');
        if (c.planned == null && meshPlan() && !engineerSet()) { c.planned = typedHeight(); save(); }
      } },
    { id: 'bad-value', chapter: 'Plan', kind: 'do', place: 'right', title: 'Set a bad value on purpose', doLine: 'Type in the highlighted field', selectText: true,
      body(c) {
        const planned = plannedHeight(c);
        return `In first cell height for NACA ${nacaOf(0)}, type ${AS.fmtValue(BAD_VALUE)} and press Enter.` +
          (AS.isNum(planned) && planned > 0 ? ` That is about ${Math.round(BAD_VALUE / planned)} times the planned ${AS.fmtValue(planned)}.` : '');
      },
      target: '#' + FIELD,
      // the reference mark sits where the arrow from the card would end: the hole takes both, and only the field takes input
      also: () => citeMark(),
      setup() { showStep('plan'); },
      hint() {
        const field = byId(FIELD);
        if (field && field.classList.contains('invalid')) return 'The plan refused that value. Use 0.012.';
        if (!engineerSet()) return '';
        const yp = predicted(typedHeight());
        if (yp >= 20 && yp <= 200) return `That would pass the mesh gate (predicted y+ ${Math.round(yp)}). Type 0.012 to see a failure.`;
        if (yp < 20) return `That is too thin (predicted y+ ${Math.round(yp)}); the tutorial needs a cell that is too tall. Type 0.012.`;
        return '';
      },
      // only a value that is too tall keeps every later sentence true
      done() { return engineerSet() && predicted(typedHeight()) > 200; },
      auto() {
        const field = byId(FIELD);
        if (!field) return;
        field.value = String(BAD_VALUE);
        field.dispatchEvent(new Event('change', { bubbles: true }));
      } },
    { id: 'set-by-engineer', chapter: 'Plan', kind: 'look', place: 'right', title: 'The plan takes your word',
      body(c) {
        const plan = (study() || {}).plan || {}, planned = plannedHeight(c);
        const reason = heightReason().replace(/[.\s]+$/, '') || AS.ENGINEER_MARK + (AS.isNum(planned) ? ` (was ${AS.fmtValue(planned)})` : '');
        return `The plan is now version ${plan.version || 2} and the reason reads: ${reason}. Nothing stops you here; the check comes after the mesh is built.`;
      },
      target() { const field = byId(FIELD), row = field && field.closest('.plan-row'); return row ? row.querySelector('.plan-reason') || row : null; },
      frame: '.plan-row',
      setup() {
        showStep('plan');
        // a field left in focus could fire a late change event, and with it a second save
        const field = byId(FIELD);
        if (field && document.activeElement === field) field.blur();
      } },
    { id: 'approve', chapter: 'Plan', kind: 'do', place: 'top', title: 'Approve and choose a mode',
      body: 'Approve and auto-run goes through every stage and asks only if a gate keeps failing; the button next to it asks before every stage. Press Approve and auto-run.',
      target: '#approve-auto', frame: '.plan-foot', allow: ['#approve-auto'],
      setup() {
        showStep('plan');
        // every checkpoint of the run is armed before it starts, so no step has to race the mock
        const bad = nacaOf(0), good = nacaOf(1);
        mock.hold('geometry.passed'); mock.hold('mesh.gate_failed'); mock.hold('mesh.passed@' + bad);
        mock.hold('numerics.passed@' + good); mock.hold('solve.half@' + good); mock.hold('solve.half@' + bad);
      },
      done() { const s = study(); return !!s && !!s.plan && !!s.plan.approved_at && s.mode === 'auto' && AS.caseList().length === 2; },
      auto() { press(q('#approve-auto')); } },

    // ----- Parallel run -----
    { id: 'tabs', chapter: 'Parallel run', kind: 'look', place: 'bottom', title: 'Two cases at once', waitText: 'The two cases are starting',
      body: 'Each airfoil is a case with its own tab and its own seven agents, and both run at the same time. The count on a tab is the number of stages passed, out of seven.',
      target: '#doctabs .tabs',
      // both cases then stand at the geometry.passed checkpoint and the tabs read 1/7
      ready() { const cases = AS.caseList(); return cases.length === 2 && cases.every((c) => (AS.stageOf(c, 'geometry') || {}).status === 'passed'); },
      setup(c) { const bad = c.bad; if (bad) showStage(bad, bad.current_stage || 'geometry', true); } },
    { id: 'tree', chapter: 'Parallel run', kind: 'look', place: 'right', title: 'Stages and properties',
      body: 'The tree lists the seven stages of the open case: Geometry has passed and Mesh is next. Properties, below it, shows the facts of whatever is selected.',
      target: '#rail',
      setup() { if (state.ui.railCollapsed) { state.ui.railCollapsed = false; AS.invalidate(); } } },

    // ----- Failure and retry -----
    { id: 'mesh-agent', chapter: 'Failure and retry', kind: 'wait', place: 'left', title: 'Watch the mesh agent',
      body: () => `The mesh agent sees your ${AS.fmtValue(typedHeight())}, says it is too tall, and builds the mesh with it anyway because you set it. ` +
        'A real agent may correct it before the gate; this one submits it so you can see the gate at work.',
      target: '#chat .chat-log',
      setup(c) {
        const bad = c.bad;
        state.ui.chatScope = 'case';
        openChat();
        if (bad) showStage(bad, bad.current_stage || 'geometry', true);   // Follow then moves the page to Mesh
        mock.release('geometry.passed');
        AS.invalidate();
      },
      done(c) { return (meshOf(c.bad).gate || {}).passed === false; } },
    { id: 'gate-failed', chapter: 'Failure and retry', kind: 'look', place: 'top', title: 'The gate says no',
      body(c) {
        const check = failedCheck(c.bad);
        return (check ? `Predicted y+ is ${AS.fmtValue(check.value)} and the limit is ${AS.fmtValue(check.limit)}, so the stage did not pass.` : 'A check of the mesh gate failed, so the stage did not pass.') +
          ' The gate is fixed code, not the model, and the agent cannot overrule it.';
      },
      // the table is there while the case stands before attempt 2; the red card in the log is the fallback
      target() { return q('#pane-main .stage-report tr.row-fail') || $$('#chat .chat-log .msg.card.bad[data-stage="mesh"]').filter(laidOut).pop() || null; },
      frame: '.table-wrap',
      setup(c) { showStage(c.bad, 'mesh', true); } },
    { id: 'attempt-2', chapter: 'Failure and retry', kind: 'wait', place: 'right', title: 'Attempt 2, without asking',
      body: 'The failed check went back to the same agent and attempt 2 started without asking you. In auto-run you are asked only when a stage fails its gate three times in a row. It now sets the height that gives y+ 40, rebuilds and checks again.',
      target: '#rail .rail-node[data-key="stage:mesh"]',
      setup(c) { showStage(c.bad, 'mesh', true); mock.release('mesh.gate_failed'); },
      done(c) { const st = meshOf(c.bad); return st.status === 'passed' && st.attempts >= 2; } },
    { id: 'mesh-passed', chapter: 'Failure and retry', kind: 'look', place: 'top', title: 'Passed on the second attempt',
      body(c) {
        const m = meshOf(c.bad).metrics || {};
        return `The gate passes with a predicted y+ of ${AS.fmtValue(AS.isNum(m.predicted_yplus) ? m.predicted_yplus : 40)}. ` +
          `The hand-off records what was built, ${AS.fmtValue(AS.isNum(m.first_cell_height) ? m.first_cell_height : plannedHeight(c))}, and why it differs from your ${AS.fmtValue(typedHeight())}.`;
      },
      target: '#pane-main .stage-report .report-content',
      setup(c) {
        showStage(c.bad, 'mesh', true);
        if (!c.local.top) { c.local.top = true; const report = $('#pane-main .stage-report'); if (report) report.scrollTop = 0; }
      } },
    { id: 'viewer', chapter: 'Failure and retry', kind: 'look', place: 'bottom', title: 'Look at the mesh', waitText: 'The viewer is starting',
      body: 'In a real study this pane is a live ParaView view of the case. Here it is a schematic drawing, not a result: try the view presets.',
      target: '#pane-main .view-stage .viewer', interactive: true, allow: ['.viewer-head .presets .seg-btn', '#field-select'],
      ready() { return !!q('#pane-main .pv-frames .pv-frame:not([hidden])'); },
      setup(c) {
        showStage(c.bad, 'mesh', true);
        if (state.ui.viewerCollapsed.mesh !== false) { state.ui.viewerCollapsed.mesh = false; AS.invalidate(); }
      } },
    { id: 'three-failures', chapter: 'Failure and retry', kind: 'look', place: 'top', title: 'If it keeps failing',
      body: 'After three failed gates in a row the stage stops and the case waits for you: approve a retry, re-run here with an instruction, or stop. That case then asks before every later stage until you set the mode back to Auto-run.',
      note: 'Explained only, not played in this tutorial.',
      target: '#pane-main .stage-report .report-actions details.rerun',
      setup(c) { showStage(c.bad, 'mesh', true); } },

    // ----- Steering -----
    { id: 'other-case', chapter: 'Steering', kind: 'do', place: 'bottom', title: 'The other case never waited',
      body(c) {
        const good = c.good, n = AS.passedCount(good);
        return `While NACA ${nacaOf(0, c.bad)} repeated its mesh, NACA ${nacaOf(1, good)} passed ${WORDS[n] || n} stage${n === 1 ? '' : 's'}. Open its tab.`;
      },
      target: (c) => (c.good ? '#doctabs .tab[data-tab="' + c.good.id + '"]' : null),
      done(c) { const good = c.good; return !!good && state.ui.caseId === good.id && state.ui.tab === 'main' && state.ui.sel.kind === 'stage'; },
      // both cases go on from here: this one into Solve, the other through Physics and Solver setup
      leave() { mock.release('numerics.passed@' + nacaOf(1)); mock.release('mesh.passed@' + nacaOf(0)); },
      auto(c) { press(q(this.target(c))); } },
    { id: 'convergence', chapter: 'Steering', kind: 'look', place: 'bottom', title: 'Convergence, live', waitText: 'The solver is starting',
      body: 'Residuals on a log scale, and lift and drag with the XFoil value as a reference line. The agent judges the drift of the forces, not only the residuals.',
      target: '#pane-main .vp-solve .solve-grid',
      ready(c) {
        const good = c.good, ui = state.ui;
        if (!good || good.current_stage !== 'solve') return false;
        // the plots are what this card is about: wait (under "The solver is starting") until they have lines
        const sr = state.series && state.series[good.id];
        const drawn = !!sr && sr.res.length >= 3 && sr.forces.length >= 3;
        if (ui.caseId === good.id && ui.tab === 'main' && ui.sel.kind === 'stage' && ui.sel.key === 'solve') return drawn;
        // Follow normally brings the page here; after a second the tutorial goes there itself
        c.local.since = c.local.since || Date.now();
        if (Date.now() - c.local.since > 1000) showStage(good, 'solve', true);
        return false;
      } },
    { id: 'send', chapter: 'Steering', kind: 'do', place: 'left', title: 'Steer a running agent',
      body: 'A note is written for you with a line of the log quoted. Outside the tutorial, select any text on the page to quote it. Press Send.',
      target: '#send-btn', frame: '.composer', allow: ['#send-btn', '#composer'],
      setup(c) {
        const good = c.good;
        if (!good) return;
        openChat();
        showStage(good, 'solve', true);       // the message target then reads this case and Solve
        if (c.local.written) return;          // written once: a set-up repeated after Send must not refill the box
        c.local.written = true;
        c.seqAtSend = state.lastSeq;
        const box = byId('composer');
        if (box && !box.value.trim()) { box.value = 'Report the Cd drift over the last 200 iterations.'; box.dispatchEvent(new Event('input', { bubbles: true })); }
        const line = lastAgentLine(good);
        if (line && !$('#chat .composer .quote-chip')) AS.addQuote({ text: line, label: 'NACA ' + good.naca + ' · Solve', case_id: good.id, stage: 'solve' });
      },
      done(c) {
        const good = c.good;
        if (!good || !c.local.written) return false;
        return state.pending.some((p) => p.case_id === good.id) ||
          state.events.some((ev) => ev.type === 'human.message' && ev.case_id === good.id && ev.seq > c.seqAtSend);
      },
      auto() { press(q('#send-btn')); } },
    { id: 'reply', chapter: 'Steering', kind: 'wait', place: 'left', title: 'Queued, then answered',
      body: 'Your note waits until the agent reaches its next tool call. Then the agent answers and carries the note into its hand-off.',
      note: 'The reply is scripted in this tutorial.',
      target: '#chat',
      setup: openChat,
      // the greyed copy in the queue has to be seen before the answer
      tick(c, now) {
        if (c.local.released || now - c.enteredAt < 1500) return;
        c.local.released = true;
        mock.release('solve.half@' + nacaOf(1, c.good));
      },
      done(c) {
        const good = c.good;
        if (!good) return false;
        const sent = state.events.find((ev) => ev.type === 'human.message' && ev.case_id === good.id && ev.seq > c.seqAtSend);
        return !!sent && state.events.some((ev) => ev.type === 'agent.message' && ev.case_id === good.id && ev.seq > sent.seq);
      } },
    { id: 'pause-resume', chapter: 'Steering', kind: 'do', place: 'bottom', title: 'Pause and resume',
      body: 'Pause holds every case at its next tool call. Press Pause, then Resume.',
      // Pause and Resume carry the same data-key, so one selector follows the swap
      target: PAUSE_RESUME,
      tick(c) { if (AS.caseList().some((x) => x.status === 'paused')) c.sawPaused = true; },
      hint(c) { return c.sawPaused && AS.caseList().some((x) => x.status === 'paused') ? 'Paused. Now press Resume.' : ''; },
      done(c) { return c.sawPaused && AS.caseList().every((x) => x.status !== 'paused'); },
      async auto() {
        press(q(PAUSE_RESUME));
        await until(() => AS.caseList().some((x) => x.status === 'paused') && !!q('#resume-btn'), 5000);
        press(q(PAUSE_RESUME));
      } },

    // ----- Results -----
    { id: 'finish-run', chapter: 'Results', kind: 'wait', place: 'bottom', title: 'Let it finish', timeout: 90000,
      body: 'The cases now run to the end through Solve, Post-process and Validate, in under half a minute. Time, tokens and cost of the study add up in this bar; the cost is simulated.',
      target: '#topbar .tb-totals',
      setup(c) {
        mock.release();
        if (!c.local.followed) { c.local.followed = true; AS.followLive(); }   // the page moves through the stages
      },
      done() { const s = study(); return !!s && s.status === 'done' && !!s.report; } },
    { id: 'validation', chapter: 'Results', kind: 'look', place: 'bottom', title: 'Checked against XFoil',
      body(c) {
        const bad = c.bad, d = bad ? AS.caseNumbers(bad).deltas : {};
        return (AS.isNum(d.cl_pct) && AS.isNum(d.cd_pct)
          ? `Every case ends with the same condition run in XFoil: here lift differs by ${AS.fmtPct(d.cl_pct)} and drag by ${AS.fmtPct(d.cd_pct)}, inside the 5 % and 25 % limits.`
          : 'Every case ends with the same condition run in XFoil, and the differences in lift and drag are judged against limits of 5 % and 25 %.') +
          ' An agent in Solve, Post-process or Validate that distrusts a result can send the case back to an earlier stage, at most twice.';
      },
      target: '#pane-main .vp-validate',
      // the study is done, so nothing moves the page any more
      setup(c) {
        showStage(c.bad, 'validate', false);
        if (state.ui.viewerCollapsed.validate !== false) { state.ui.viewerCollapsed.validate = false; AS.invalidate(); }
        if (q('#pane-main .viewer-head .seg-btn.on[data-vtab="field"]')) press(q('#pane-main .viewer-head [data-vtab="compare"]'));
      } },
    { id: 'open-compare', chapter: 'Results', kind: 'do', place: 'bottom', title: 'Compare the cases',
      body: 'Compare puts the cases side by side. Open it.',
      target: '#doctabs .tab[data-tab="compare"]',
      done() { return state.ui.tab === 'compare'; },
      auto() { press(q(this.target)); } },
    { id: 'compare', chapter: 'Results', kind: 'look', place: 'top', title: 'Side by side',
      body: 'Each case next to its XFoil reference, with the target L/D row and, in the last row, rework, gate failures and your interventions. The reporter\'s recommendation and what was not verified are further down this page.',
      target: '#pane-compare table.cmp-tbl', frame: '.table-wrap',
      setup() { if (state.ui.tab !== 'compare') AS.setTab('compare'); } },
    { id: 'open-dashboard', chapter: 'Results', kind: 'do', place: 'bottom', title: 'Keep the history',
      body: 'Every finished case is kept on the Dashboard. Open it.',
      target: '#doctabs .tab[data-tab="dashboard"]',
      done() { return state.ui.tab === 'dashboard'; },
      auto() { press(q(this.target)); } },
    { id: 'dashboard', chapter: 'Results', kind: 'look', place: 'bottom', title: 'Model by geometry', waitText: 'The dashboard is loading',
      body(c) {
        return 'Each cell is the latest run of one model on one airfoil: result, difference to XFoil, rework and gate failures. ' +
          `Your NACA ${nacaOf(0, c.bad)} run is in the ${AS.modelLabel((study() || {}).model || SONNET)} row with its one gate failure; the other rows are sample data.` + PASS_K;
      },
      target: '#pane-dashboard table.matrix',
      ready() { return !!q('#pane-dashboard table.matrix'); },
      setup() { if (state.ui.tab !== 'dashboard') AS.setTab('dashboard'); } },
    { id: 'finish', chapter: 'Results', kind: 'look', place: 'centre', title: 'That is the whole loop', final: true, noNext: true, noLeave: true,
      body: 'Mission, plan, two cases in parallel, a failed gate fixed by its own agent, steering, validation and history. Start a real study with your own mission, or stay and explore this recorded one.',
      actions: [
        { label: 'Start a real study', primary: true, title: 'Needs the studio server and an API key; real runs cost money',
          run: () => { try { sessionStorage.removeItem('as.tutorial.return'); } catch (e) { /* private mode */ } api.exit(); } },
        { label: 'Explore this study', run: () => api.stop() },
        { label: 'Replay the tutorial', run: () => api.restart() }],
      setup() { mock.release(); finished = true; save(); } },
  ];
  /* Shown instead of a step after a reload in the middle of the run: the checkpoints lived in memory. */
  const RELOADED = { id: 'reloaded', chapter: 'Tutorial', kind: 'look', place: 'centre', title: 'Tutorial', final: true, noNext: true, noLeave: true,
    body: 'The page was reloaded during the run. The tutorial has to start again.',
    actions: [{ label: 'Restart', primary: true, run: () => api.restart() }, { label: 'Explore this study', run: () => api.stop() }] };

  /* The planned first cell height: noted when the plan is first read, else taken from the reason of the edit. */
  function plannedHeight(c) {
    if (AS.isNum(c.planned)) return c.planned;
    const was = /\(was ([^)]+)\)/.exec(heightReason());
    return was && isFinite(Number(was[1])) ? Number(was[1]) : NaN;
  }

  // ---------- the live step list ----------
  /* Step fields used on the live list only:
       progress(ctx): what the run is doing now, one line or several, under a wait or a waiting card.
       expect: how long it usually takes. softTimeout: after this many ms one more line (lateText)
       is shown and the step keeps waiting; a live step never restarts anything.
       skip(ctx): the step is passed over. variant(ctx): the key of one block of `variants`, merged
       over the step on every pass (another kind, target, text or buttons).
       own: the control is the reader's to press, because it spends or the engine counts it as an
       intervention, so the card never offers Do it for me.
       onUse(ctx, event): the allowed control is about to take a click or a key.
       quiet: the keyboard starts on the card, so Enter presses none of its buttons.
       interrupt with when(ctx): shown over the open step, which is entered again afterwards.
       after(ctx): on a live-only step, the index of the step that follows when it is done.
     title, waitText, nextLabel, doLine and actions may be functions of ctx, like body and note. A text may be
     a list of parts, where { cost: '...' } is set in the cost style; a note may be a list of lines. */
  const LIVE_STATES = ['running', 'paused', 'awaiting_approval'];
  const QUESTION = 'Question only, change nothing: in one sentence, how far have lift and drag settled?';
  /* The kinds of human.intervention events, as the Compare card names them. */
  const ACTED = [['steer', 'your message'], ['pause', 'Pause'], ['resume', 'Resume'], ['gate_decision', 'the retry you approved'], ['mode_change', 'the return to auto-run'],
    ['rerun', 'the re-run'], ['plan_edit', 'the plan edit'], ['stop', 'Stop']];
  const count = (n) => WORDS[n] || String(n);
  const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const joinAnd = (list) => (list.length > 1 ? list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1] : list[0] || '');
  const nacaList = (codes) => joinAnd(codes.map((x) => 'NACA ' + x));
  const clean = (s) => String(s == null ? '' : s).replace(/[.\s]+$/, '');
  const stepIndex = (id) => LIST.findIndex((s) => s.id === id);
  const toRecorded = () => location.assign(location.pathname + '?tutorial=reset');
  const noKey = (c) => c.paid && !!state.config && state.config.llm_ready === false;   // an older server has no such field
  const studyActive = () => { const s = study(); return !!s && ACTIVE.includes(s.status); };
  const runPaused = () => { const s = study(); return !!s && (s.status === 'paused' || AS.caseList().some((x) => x.status === 'paused')); };
  const canPause = () => { const s = study(); return !!s && s.status === 'running' && AS.caseList().some((x) => x.status === 'running'); };
  const viewerUp = () => !!q('#pane-main .pv-frames .pv-frame:not([hidden])');
  const approvedRetry = (c) => { const bad = c.bad; return !!bad && facts().failed.some((ev) => ev.case_id === bad.id && (ev.stage || (ev.data || {}).stage) === 'mesh'); };
  /* The case whose mesh the later look steps show: the bad one once its mesh has passed, else the other. */
  const meshCase = (c) => (meshOf(c.bad).status === 'passed' ? c.bad : c.good);
  const doneCase = (c) => { const bad = c.bad; return bad && bad.status === 'done' ? bad : AS.caseList().find((x) => x.status === 'done') || bad; };
  function sendCase(c) {
    const good = c.good, bad = c.bad;
    return good && LIVE_STATES.includes(good.status) ? good : bad && LIVE_STATES.includes(bad.status) ? bad : good;
  }
  /* The reader's message once it has been delivered: the event carries the text that was sent. */
  function sentMessage() {
    if (R.seqAtSend == null) return null;
    return state.events.find((ev) => ev.type === 'human.message' && ev.seq > R.seqAtSend && (R.sentText == null || (ev.data || {}).text === R.sentText)) || null;
  }
  function answered() {
    const sent = sentMessage();
    return !!sent && state.events.some((ev) => ev.seq > sent.seq && ((ev.type === 'agent.message' && ev.case_id === R.sendCase) || ev.type === 'steering.reply'));
  }

  // progress lines, from AS.state.activity
  const elapsed = (ts) => { const t = Date.parse(ts); return isFinite(t) ? Math.max(0, Math.round((Date.now() - t) / 1000)) : null; };
  function doing(a) {
    if (!a || (!a.tool && !a.lastTs)) return 'starting';
    const secs = elapsed(a.tool ? a.toolSince : a.lastTs);
    return (a.tool ? 'running ' + a.tool : 'thinking') + (secs == null ? '' : ' · ' + secs + ' s');
  }
  const studyLine = (who) => who + ' · ' + doing(state.activity._study);
  function stageLine(c) {
    if (!c) return '';
    const key = c.current_stage, stg = key ? AS.stageOf(c, key) : null;
    if (c.status !== 'running' || !stg) return { text: 'NACA ' + c.naca + ': ' + AS.ui.statusText(c.status), idle: true };
    return AS.stageTitle(key) + ' · attempt ' + (stg.attempts || 1) + ' · ' + doing(state.activity[c.id]);
  }
  function caseLine(c) {
    const a = state.activity[c.id] || {};
    if (c.status !== 'running') return { text: 'NACA ' + c.naca + ': ' + AS.ui.statusText(c.status) + (c.verdict ? ', ' + c.verdict : ''), idle: true };
    const solving = c.current_stage === 'solve' && AS.isNum(a.iteration);
    return 'NACA ' + c.naca + ': ' + AS.stageTitle(c.current_stage) + ' · ' + (solving ? 'iteration ' + AS.fmtInt(a.iteration) + (a.total ? ' of ' + AS.fmtInt(a.total) : '') : doing(a));
  }

  /* Tick the pair of the recorded tutorial when the advisor shortlisted both and XFoil converged for
     both (the test the server applies to a selection); otherwise its own choice stands. */
  const screened = (cd) => !!cd.xfoil && !!cd.xfoil.converged && AS.isNum(cd.xfoil.alpha_design);
  function wantedPair() {
    const cands = (study() || {}).candidates || [];
    const ok = (code) => cands.some((cd) => String(cd.naca) === code && screened(cd));
    const own = cands.filter((cd) => cd.selected).map((cd) => String(cd.naca));
    const pair = LIVE_RUN.nacas.every(ok);
    // a list that carries no selection: the best ranked sections that can be simulated, so Confirm is never left disabled
    const best = ranked().filter(screened).slice(0, 2).map((cd) => String(cd.naca));
    return { pair, own, want: pair ? LIVE_RUN.nacas : own.length ? own : best };
  }
  /* The tutorial ticks its pair once the cards are up, until the reader ticks a box: then the choice is theirs. */
  function tickPair(c) {
    if (R.picked) return;
    const w = wantedPair();
    if (w.want.length) setTicks(c, w.want);
  }
  /* Why these sections are ticked, under the candidates and confirm cards. */
  function pickNote() {
    if (R.picked) return ticked().length > 2 ? 'The estimate on the first card is for two cases. Each further case adds to the cost and the run time.' : '';
    const w = wantedPair(), same = w.own.length === w.want.length && w.own.every((x) => w.want.includes(x));
    if (!w.pair) {
      const n = w.want.length;
      if (!n) return '';       // nothing can be simulated: the watch shows the trouble card
      return 'The advisor did not shortlist both NACA 2412 and NACA 4412, so the tutorial ticked ' +
        (!w.own.length ? `the best ranked section${n === 1 ? '' : 's'} XFoil could screen.` : n === 2 ? 'its top two.' : n === 1 ? `its one choice, NACA ${w.want[0]}.` : 'its own choices.');
    }
    if (same) return '';
    return 'The tutorial ticked NACA 2412 and NACA 4412, the pair of the recorded tutorial.' +
      (w.own.length ? ` The advisor's own ${w.own.length === 2 ? 'top two were' : w.own.length === 1 ? 'choice was' : 'choices were'} ${nacaList(w.own)}.` : '');
  }
  /* The Mission form posts the model whose radio is checked. true when that is the model of the
     consent card; a server that does not offer it leaves the Suggest step locked. */
  function modelPicked() {
    const want = $$('input[name=model]').find((r) => r.value === R.model);
    if (want && !want.checked) want.checked = true;
    return !!want && want.checked;
  }
  /* The reader's click on Suggest airfoils at the Suggest step: the study that request creates is the
     tutorial's own, and it is known by the id the server returns for it. Nothing is guessed from the
     study that is open or from the time: a press that sends nothing, or a study the reader starts
     alone afterwards, binds nothing. The Mission form sends the request in the task of the click, so
     the mark is taken down as soon as that task is over. */
  let pressing = false;      // the click is being handled: the create request sent now is the tutorial's
  let flying = 0;            // such requests still without an answer
  let mine = null;           // the id one of them returned, until that study is open and its model was read (see adopt)
  function noteAsk() { pressing = true; setTimeout(() => { pressing = false; }, 0); }
  if (LIVE_STEPS) {
    const post = AS.api.post;
    AS.api.post = function (path, body) {
      const sent = post(path, body);
      if (!pressing || path !== '/api/studies') return sent;
      pressing = false;      // one request for one click
      flying += 1;
      // registered before the caller's own continuation: the record names the study before the page opens it
      sent.then(own, () => own(null));
      return sent;
    };
  }
  /* The Re-run box of the Mesh stage, opened and filled with the instruction that gave a failed gate in a real run. */
  function armRerun(c) {
    const bad = c.bad;
    if (!bad) return;
    pin(bad, 'mesh');
    const box = q('#pane-main .stage-report .report-actions details.rerun'), field = byId('rerun-instruction');
    if (box && !box.open) box.open = true;
    if (field && !field.value.trim() && !c.local.used) {
      field.value = `Test of the gate: keep first_cell_height at ${typedHeight()} chord exactly as the plan says, build the mesh and call complete_stage with it. ` +
        'Do not change the value before the gate has judged it. If the gate rejects it, fix the cause and submit again.';
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (!c.local.armed) { c.local.armed = true; R.seqAtRerun = state.lastSeq; save(); }
  }

  const LIVE_STEP = {
    // The consent card. Nothing that can spend is sent before its Start button.
    welcome: { title: 'Live tutorial', quiet: true, noNext: true, noLeave: true,
      body(c) {
        if (c.model === 'scripted') return 'Test run with the scripted model: no model calls and no cost. OpenFOAM, XFoil and ParaView are real.';
        return [`This is the same tutorial on a real run. The agents are ${AS.modelLabel(c.model)}, called with the API key of this studio server. OpenFOAM and XFoil run in Docker and the viewer is ParaView. `,
          { cost: `It costs money: about ${figures(c.model).estimate} USD, and the tutorial stops the run if it reaches ${money(ceilingFor(c.model))} USD while this page is open.` },
          ' It takes about 8 minutes, about 4 of them waiting for the agents and the solver. You plan two airfoils, set one bad value on purpose and watch the gate catch it.' +
            (TEST ? '' : ' The study is kept in a project named Tutorial, apart from your own.')];     // the test list on the mock changes no project
      },
      note() {
        const out = [];
        if (TEST) out.push('Test run: live steps on recorded data, no cost.');
        if (state.studies.some((s) => ACTIVE.includes(s.status))) out.push('Another study is running on this server, so this run will be slower.');
        if (window.innerWidth <= 1180) out.push('Make the window wider for the tutorial.');
        return out;
      },
      // the page never asks for the key
      hint: (c) => (noKey(c) ? 'No API key is set on this server. Put ANTHROPIC_API_KEY in .env, restart wing-studio and reload this page.' : ''),
      actions(c) {
        const busy = !!c.local.busy, free = TEST || c.model === 'scripted';
        return [{ label: free ? 'Start, no cost' : ['Start, ', { cost: `about ${figures(c.model).estimate} USD` }], key: 'start', primary: true, disabled: busy || noKey(c), run: startLive },
          { label: 'Recorded tutorial, free', disabled: busy, run: toRecorded },
          { label: 'Not now', disabled: busy, run: () => api.exit(true) }];
      },
      // a second tab, or a tab that lost its record: the run that is under way is offered, never a second paid one
      variant: (c) => (c.resume ? 'resume' : ''),
      variants: { resume: { title: 'A live tutorial run is in progress',
        body: (c) => `The study '${c.resume.title || c.resume.id}' was started from this tutorial and is ${AS.ui.statusText(c.resume.status)}. Resuming costs nothing new.`,
        note: (c) => (c.local.failed ? 'The study could not be opened.' : ''), hint: null,
        actions: (c) => [{ label: 'Resume it', key: 'resume', primary: true, disabled: !!c.local.busy, run: resumeRun }, { label: 'Not now', disabled: !!c.local.busy, run: () => api.exit(true) }] } },
      setup() {
        const ui = state.ui;
        if (state.study) AS.newStudy();        // a study that is running keeps running on the server
        ui.railCollapsed = false; ui.chatCollapsed = false; ui.chatScope = 'case'; ui.chatVerbose = false;
        AS.invalidate();                       // the project and the example are touched only after Start
      } },
    example: { note: () => projectNote,
      setup() { injectPreset(); showStep('mission'); } },
    suggest: { own: true,
      body: (c) => `The model chosen here plays every agent; this run uses ${AS.modelLabel(c.model)}. ` +
        (c.paid ? 'Press Suggest airfoils. This is the first paid call: the advisor screens candidates in XFoil and nothing is simulated yet.'
          : 'Press Suggest airfoils: the advisor screens candidates in XFoil and nothing is simulated yet.'),
      // handed over only while the form would post the model of the consent card
      allow: [() => (modelPicked() ? '#suggest-btn' : null)],
      doLine: () => (modelPicked() ? '' : 'Locked: this model is not offered'),
      hint: () => (modelPicked() ? '' : `This server does not offer ${AS.modelLabel(R.model)}, the model this tutorial was started with. Nothing was sent.`),
      actions: () => (modelPicked() ? [] : [{ label: 'Recorded tutorial, free', run: toRecorded }]),
      setup() { showStep('mission'); modelPicked(); },
      // the click alone: it is what submits the form, also for Enter and Space on the button
      onUse(c, e) { if (e.type === 'click') noteAsk(); },
      // the second clause: without an API key the study goes to suggesting and back to draft within milliseconds
      done() { const s = study(); return !!s && (s.status !== 'draft' || facts().suggesting); },
      auto() { if (modelPicked()) { noteAsk(); press(q('#suggest-btn')); } } },
    advisor: { expect: 'Usually about 15 seconds.', softTimeout: 60000,
      lateText: 'Still working. If Docker was not running it is starting now, which can take up to two minutes.',
      body: (c) => (c.paid ? 'The advisor is a real model call. It reads the mission and screens its shortlist in XFoil, inside Docker.' : 'The advisor reads the mission and screens its shortlist in XFoil.') +
        ' Everything an agent says or does is listed in this log.',
      progress: () => studyLine('Advisor'),
      done() { const s = study(); return !!s && s.status === 'awaiting_selection' && (s.candidates || []).length >= 2; } },
    candidates: {
      body() {
        const n = ((study() || {}).candidates || []).length;
        return `${capital(count(n))} sections, ranked for your objective, each with its XFoil numbers at the design lift. A missed limit or target is written on the card and never removes a candidate. ` +
          'Tick the sections you want to simulate.';
      },
      note: pickNote,
      tick: tickPair },
    // any selection the page accepts: the live list follows the cases there are
    confirm: { own: true,
      title() { const n = ticked().length; return n ? `${capital(count(n))} airfoil${n === 1 ? '' : 's'}, ${count(n)} case${n === 1 ? '' : 's'}` : 'One case per airfoil'; },
      body: (c) => tickedLine() + 'Press Confirm and plan.' + (c.paid ? ' The planner is the next paid call.' : ''),
      note: pickNote,
      allow: [PICK, '#confirm-btn'],
      setup(c) { showStep('suggest'); tickPair(c); },
      tick: tickPair,
      auto() { press(q('#confirm-btn')); } },
    plan: { waitText: 'The planner is writing the plan', softTimeout: 120000,
      expect: (c) => (st.waiting ? (c.paid ? 'This is a real model call and usually takes 25 to 50 seconds.' : 'Usually a few seconds.') : ''),
      progress: () => (st.waiting ? studyLine('Planner') : ''),
      body() {
        const b = band(planCase(0));
        return `Each case lists every setting with its reason and, under Acceptance, what the gate will check. For the mesh that includes a predicted y+ between ${b[0]} and ${b[1]}.` + citeLine();
      } },
    'bad-value': {
      hint() {
        const field = byId(FIELD);
        if (field && field.classList.contains('invalid')) return 'The plan refused that value. Use 0.012.';
        if (!engineerSet()) return '';
        const yp = liveYplus(typedHeight()), b = band(planCase(0));
        if (yp >= b[0] && yp <= b[1]) return `That would pass the mesh gate (predicted y+ ${Math.round(yp)}). Type 0.012 to see a failure.`;
        if (yp < b[0]) return `That is too thin (predicted y+ ${Math.round(yp)}); the tutorial needs a cell that is too tall. Type 0.012.`;
        return '';
      },
      done: tooTall },
    approve: { own: true,
      body: 'Approve and auto-run goes through every stage and asks only if a gate keeps failing; the button next to it asks before every stage. Press Approve and auto-run. ' +
        'From here the agents and the solver run on their own for about two and a half minutes. The tutorial follows the run and cannot hold it.',
      setup() { showStep('plan'); },
      done() { const s = study(); return !!s && !!s.plan && !!s.plan.approved_at && s.mode === 'auto' && AS.caseList().length === (s.plan.cases || []).length; } },
    tabs: {
      body() {
        const n = AS.caseList().length;
        return `Each airfoil is a case with its own tab and its own seven agents, and ${n === 2 ? 'both' : 'all'} run at the same time. The count on a tab is the number of stages passed, out of seven.`;
      },
      // not when both Geometry stages have passed: that takes a few seconds, and a real run does not wait
      ready() { const s = study(), n = AS.caseList().length; return !!s && !!s.plan && n > 0 && n === (s.plan.cases || []).length; },
      setup(c) { follow(c.bad); } },
    'mesh-agent': { expect: 'Usually about 20 seconds.', softTimeout: 150000,
      body: (c) => `The mesh agent of NACA ${nacaOf(0, c.bad)} has your ${AS.fmtValue(typedHeight())} as an instruction. ` +
        'Follow the log: it sets the value, builds the mesh and checks it, then complete_stage hands the mesh to the gate.',
      progress: (c) => stageLine(c.bad),
      setup(c) {
        if (state.ui.chatScope !== 'case') { state.ui.chatScope = 'case'; AS.invalidate(); }
        openChat();
        follow(c.bad);                         // the page moves from Geometry to Mesh by itself
      },
      done(c) { const bad = c.bad, status = meshOf(bad).status; return !!gateFail(bad) || status === 'passed' || status === 'failed' || ended(bad); } },
    'gate-failed': { waitText: 'The mesh agent is at work', nextLabel: 'Next',
      // the gate card in the log, found by the seq of its event; the gate table of the stage page is
      // gone as soon as the next attempt starts
      target(c) {
        const fail = gateFail(c.bad);
        return (fail && q('#chat .chat-log .msg.card.bad[data-seq="' + fail.seq + '"]')) || $$('#chat .chat-log .msg.card.bad[data-stage="mesh"]').filter(laidOut).pop() || q('#pane-main .stage-report .report-content');
      },
      frame: null,
      body(c) {
        const fail = gateFail(c.bad), checks = (fail && fail.data.checks) || [];
        const check = checks.find((ch) => ch.ok === false && /y\+/.test(String(ch.name))) || checks.find((ch) => ch.ok === false);
        const told = !check ? 'A check of the mesh gate failed, so the stage did not pass.'
          : /y\+/.test(String(check.name)) ? `Predicted y+ is ${AS.isNum(Number(check.value)) ? Math.round(Number(check.value)) : AS.fmtValue(check.value)} and the limit is ${AS.fmtValue(check.limit)}, so the stage did not pass.`
            : `The mesh gate failed on ${check.name} (${AS.fmtValue(check.value)}, limit ${AS.fmtValue(check.limit)}), so the stage did not pass.`;
        return told + ' The gate is fixed code, not the model, and the agent cannot overrule it.';
      },
      note: 'The run has gone on while you read.',
      variant: (c) => (c.branch === 'fixed-first' ? 'fixed' : ''),
      variants: { fixed: { title: 'The agent fixed it first', nextLabel: 'Go on',
        // the tool line that applied the typed value, when the log still shows it
        target(c) {
          const bad = c.bad, typed = typedHeight();
          const call = bad && state.events.find((ev) => ev.type === 'tool.call' && ev.case_id === bad.id && ev.stage === 'mesh' && (ev.data || {}).name === 'set_mesh_params' &&
            Number(((ev.data || {}).args || {}).first_cell_height) === typed);
          return (call && q('#chat .chat-log .msg.tool[data-seq="' + call.seq + '"]')) || q('#pane-main .stage-report .report-content');
        },
        body(c) {
          const typed = AS.fmtValue(typedHeight()), built = (meshOf(c.bad).metrics || {}).first_cell_height;
          return `This time the gate was never shown your ${typed}. The agent built with it, saw a predicted y+ of ${Math.round(liveYplus(typedHeight()))} and rebuilt` +
            (AS.isNum(built) ? ` with ${AS.fmtValue(built)}` : '') + ` before handing over. A model does not act the same way every time; the gate would have rejected ${typed} just the same.`;
        },
        note: (c) => (R.gateOffered ? '' : `See the gate reject it is optional. It re-runs Mesh and every later stage of NACA ${nacaOf(0, c.bad)}: about 2.5 more minutes` + (c.paid ? ` and about ${figures(c.model).rerun} USD.` : '.')),
        actions: () => (R.gateOffered ? [] : [{ label: 'See the gate reject it', run: () => detour(SEE_GATE) }]) } },
      ready(c) { const bad = c.bad; return !!gateFail(bad) || meshOf(bad).status === 'passed' || ended(bad); },
      progress: (c) => (st.waiting ? stageLine(c.bad) : ''),
      skip(c) { const bad = c.bad; return ended(bad) && !gateFail(bad) && meshOf(bad).status !== 'passed'; },
      setup(c) {
        pin(c.bad, 'mesh');
        // Stage scope: the log then holds only the Mesh events of this case and stops growing once Mesh has passed
        if (state.ui.chatScope !== 'stage') { state.ui.chatScope = 'stage'; AS.invalidate(); }
        openChat();
      },
      // the optional re-run is offered once; a step pushed aside by an interrupt comes back with it
      leave(c, why) { if (why === 'next' && c.branch === 'fixed-first' && !R.gateOffered) { R.gateOffered = true; save(); } } },
    'attempt-2': { expect: 'Usually about 10 seconds.', softTimeout: 90000,
      // after a failed stage the retry was the reader's own approval, and the scripted model cannot correct anything
      title: (c) => `Attempt ${Math.max(2, meshOf(c.bad).attempts || 0)}, ` + (approvedRetry(c) ? 'after your approval' : 'without asking'),
      body(c) {
        const n = Math.max(2, meshOf(c.bad).attempts || 0);
        return (approvedRetry(c) ? `The stage had stopped and you approved a retry, so attempt ${n} has started with the failed check in front of the agent. `
          : `The failed check went back to the same agent and attempt ${n} started without asking you: that is auto-run. `) +
          (c.model === 'scripted' ? 'The scripted model applies the plan as it is, so the same check fails again.' : 'The agent now changes the first cell height, rebuilds and checks again.');
      },
      progress: (c) => stageLine(c.bad),
      skip: (c) => !gateFail(c.bad),
      setup(c) { pin(c.bad, 'mesh'); },
      done(c) { const bad = c.bad, status = meshOf(bad).status; return status === 'passed' || status === 'failed' || ended(bad); } },
    'mesh-passed': { waitText: 'The mesh agent is at work',
      title(c) { const n = meshOf(c.bad).attempts || 1; return n > 1 ? `Passed on attempt ${n}` : 'Passed at the first attempt'; },
      body(c) {
        const m = meshOf(c.bad).metrics || {}, built = AS.isNum(m.first_cell_height) ? m.first_cell_height : plannedHeight(c), typed = typedHeight();
        // the reader may have put the plan right while the case waited: then nothing differs
        return (AS.isNum(m.predicted_yplus) ? `The gate passes with a predicted y+ of ${Math.round(m.predicted_yplus)}. ` : 'The gate passes. ') +
          (built === typed ? `The hand-off records what was built, ${AS.fmtValue(built)}.`
            : `The hand-off records what was built, ${AS.fmtValue(built)}, and why it differs from your ${AS.fmtValue(typed)}. The plan keeps your value: the agent corrects the mesh, not the plan.`);
      },
      ready: (c) => meshOf(c.bad).status === 'passed',
      progress: (c) => (st.waiting ? stageLine(c.bad) : ''),
      skip(c) { const bad = c.bad; return ended(bad) && meshOf(bad).status !== 'passed'; },
      setup(c) {
        pin(c.bad, 'mesh');
        if (state.ui.chatScope !== 'case') { state.ui.chatScope = 'case'; AS.invalidate(); }
        if (!st.waiting && !c.local.top) { c.local.top = true; const report = $('#pane-main .stage-report'); if (report) report.scrollTop = 0; }
      } },
    'other-case': { leave: null,
      body(c) {
        const good = c.good, bad = c.bad, k = AS.passedCount(good);
        return (ended(bad) && meshOf(bad).status !== 'passed' ? `NACA ${nacaOf(0, bad)} did not get past Mesh. NACA ${nacaOf(1, good)} went on: `
          : c.branch === 'rejected' ? `While NACA ${nacaOf(0, bad)} repeated its mesh, NACA ${nacaOf(1, good)} went on: ` : `NACA ${nacaOf(1, good)} ran alongside the whole time: `) +
          `${k} of 7 stages passed so far. Open its tab.`;
      },
      skip: (c) => !c.good || c.good === c.bad,     // only one case is left
      done(c) { const good = c.good; return !!good && state.ui.caseId === good.id && state.ui.tab === 'main'; } },
    convergence: {
      waitText: (c) => (c.good ? `NACA ${c.good.naca} is at ${AS.stageTitle(c.good.current_stage || 'geometry')}` : 'The solver is starting'),
      expect: (c) => (st.waiting ? (c.good && c.good.current_stage === 'numerics' ? 'The solver starts in a few seconds.' : 'The solver starts when Solver setup has passed.') : ''),
      note(c) {
        const good = c.good, solve = AS.stageOf(good, 'solve') || {}, a = (good && state.activity[good.id]) || {};
        if (solve.status === 'passed') return 'This solve has finished; the charts show the whole run.';
        if (solve.status === 'running' && AS.isNum(a.iteration)) return `Iteration ${AS.fmtInt(a.iteration)}${a.total ? ' of ' + AS.fmtInt(a.total) : ''}. A real solve of this mesh takes about a minute.`;
        return '';
      },
      ready(c) { const good = c.good; return !!good && (((AS.stageOf(good, 'solve') || {}).attempts || 0) > 0 || ended(good)); },
      setup(c) { if (st.waiting) follow(c.good); else pin(c.good, 'solve'); } },
    send: { own: true, note: '',
      body(c) {
        const to = state.cases[R.sendCase] || sendCase(c);
        return 'A question is written for you. ' + (c.paid ? 'It asks for no change, so the answer costs one or two short model calls. ' : 'It asks for no change. ') +
          'To quote something yourself, select any text on the page and press Quote in chat. Press Send.' +
          (to && !LIVE_STATES.includes(to.status) ? ` NACA ${to.naca} has finished, so the steering agent answers instead of a stage agent.` : '');
      },
      setup(c) {
        const to = sendCase(c);
        if (!to) return;
        openChat();
        if (state.ui.chatScope !== 'case') { state.ui.chatScope = 'case'; AS.invalidate(); }
        pin(to, to.current_stage || 'solve');    // the message target then reads this case and its stage
        if (R.sendCase !== to.id) { R.sendCase = to.id; save(); }
        if (c.local.written) return;             // written once: a set-up repeated after Send must not refill the box
        c.local.written = true;
        R.seqAtSend = state.lastSeq; R.sentText = null; save();
        // no quote is added: a stage agent takes a message as an instruction, and this one must change nothing
        const box = byId('composer');
        if (box && !box.value.trim()) { box.value = QUESTION; box.dispatchEvent(new Event('input', { bubbles: true })); }
      },
      tick(c) {
        const box = byId('composer'), to = byId('composer-target');
        if (box && box.value.trim()) c.local.draft = box.value.trim();
        if (to && to.value !== 'case' && $$('option', to).some((o) => o.value === 'case')) { to.value = 'case'; to.dispatchEvent(new Event('change', { bubbles: true })); }
      },
      // the text as it stands when Send is pressed: the delivered event is matched against it
      onUse(c, e) {
        const box = byId('composer'), t = e.target;
        const sends = (e.type === 'click' && !!t.closest('#send-btn')) || (e.type === 'keydown' && e.key === 'Enter' && !e.shiftKey && t === box);
        if (sends && box && box.value.trim()) { R.sentText = box.value.trim(); save(); }
      },
      done(c) {
        if (R.seqAtSend == null || !R.sendCase) return false;
        return state.pending.some((p) => p.case_id === R.sendCase) || !!sentMessage();
      } },
    reply: { expect: 'Usually 3 to 10 seconds.', softTimeout: 120000, tick: null,
      body: 'Your message waits until the agent reaches its next tool call. Then the agent reads it and answers.',
      note() {
        const to = ((sentMessage() || {}).data || {}).delivered_to;
        return !to ? '' : to === 'steering' ? 'Delivered to the steering agent. No stage agent was at work on this case, so the steering agent answers.' : `Delivered to ${to}.`;
      },
      lateText: () => (sentMessage() ? '' : 'The message is still queued. A model call can take a while.'),
      progress: () => (state.pending.some((p) => p.case_id === R.sendCase) ? 'Queued' : ''),
      // read, and no written answer after 45 s: the reader goes on with Next
      variant() { const sent = sentMessage(), t = sent ? Date.parse(sent.ts) : NaN; return sent && !answered() && isFinite(t) && Date.now() - t > 45000 ? 'silent' : ''; },
      variants: { silent: { kind: 'look', expect: '', body: 'The agent has read your message and went on without a written answer. It stays in the agent\'s context.' } },
      done: answered },
    'pause-resume': { own: true,
      body: 'Pause holds every agent at its next tool call. A solver that is already running keeps iterating in its container. Press Pause, wait for Resume to appear, then press it. Each press is recorded as an intervention.',
      // a do step only while an agent can be paused; decided on entry, and given up when the run ends before Pause was pressed
      variant(c) {
        if (!c.local.kind) c.local.kind = R.sawPaused || canPause() ? 'do' : 'look';
        if (c.local.kind === 'do' && !R.sawPaused && !AS.caseList().some((x) => x.status === 'running' || x.status === 'paused')) c.local.kind = 'look';
        return c.local.kind === 'look' ? 'look' : '';
      },
      variants: { look: { kind: 'look', hint: null, body: 'The run has no agent left to pause. During a run, Pause holds every agent at its next tool call and Resume lets them go on.' } },
      tick() { if (!R.sawPaused && AS.caseList().some((x) => x.status === 'paused')) { R.sawPaused = true; save(); } },
      hint() {
        const live = AS.caseList().filter((x) => x.status === 'paused' || x.status === 'running');
        if (!live.some((x) => x.status === 'paused')) return '';
        if (q('#resume-btn')) return 'Paused. Now press Resume.';
        return 'Pausing: ' + live.map((x) => `NACA ${x.naca} ${x.status === 'paused' ? 'paused' : 'finishing its call'}`).join(', ') + '.';
      },
      done() { return R.sawPaused && !AS.caseList().some((x) => x.status === 'paused') && (study() || {}).status !== 'paused'; } },
    tree: {
      body(c) {
        const p = meshCase(c), n = meshOf(p).attempts || 0;
        return 'The tree lists the seven stages of the open case with their state and time.' + (p && p === c.bad && n >= 2 ? ` Mesh of NACA ${p.naca} shows ${count(n)} attempts.` : '') +
          ' Properties, below it, shows the facts of whatever is selected.';
      },
      setup(c) { pin(meshCase(c), 'mesh'); if (state.ui.railCollapsed) { state.ui.railCollapsed = false; AS.invalidate(); } } },
    viewer: { waitText: 'Starting the ParaView viewer for this case', note: '',
      body(c) {
        if (!viewerUp()) return 'ParaView could not be started on this machine, so this pane stays empty. The agents work without it, and their snapshots are in the stage report.';
        const built = (meshOf(meshCase(c)).metrics || {}).first_cell_height;
        return 'This pane is ParaView, rendering the files of this case on this machine. It shows the mesh that passed' + (AS.isNum(built) ? `, with the ${AS.fmtValue(built)} first cell` : '') +
          '. Try the view presets, or drag and scroll in the view itself.';
      },
      allow: ['.viewer-head .presets .seg-btn', '#field-select', '#viewer-retry'],
      ready: (c) => viewerUp() || !!q('#viewer-retry') || Date.now() - c.enteredAt > 15000,
      setup(c) {
        pin(meshCase(c), 'mesh');
        if (state.ui.viewerCollapsed.mesh !== false) { state.ui.viewerCollapsed.mesh = false; AS.invalidate(); }
      } },
    'three-failures': {
      body: () => (facts().failed.length ? 'You saw this in your run: after three failed gates in a row, or an agent error, the stage stops and the case waits for you: approve a retry, re-run here with an instruction, or stop.'
        : 'After three failed gates in a row the stage stops and the case waits for you: approve a retry, re-run here with an instruction, or stop.') +
        ' That case then asks before every later stage until you return it to auto-run.',
      note: () => (facts().failed.length ? '' : 'Explained only. If it happens in your run, the tutorial takes you to the approval row.'),
      // the Re-run box stays under the plug: a re-run started here would be real
      setup(c) { pin(meshCase(c), 'mesh'); } },
    'finish-run': { softTimeout: 300000, lateText: 'This is taking longer than usual. The run is still going.',
      body(c) {
        const n = AS.caseList().filter((x) => x.status !== 'stopped' && x.status !== 'failed').length;
        return `${n === 2 ? 'Both cases run' : n === 1 ? 'The remaining case runs' : 'The cases run'} through Post-process and Validate, then the reporter writes the comparison. Time, tokens and cost of the study add up in this bar.` +
          (c.paid ? ` The cost is real: ${AS.fmtCost(c.cost())} so far. It is computed from the token counts and the price list; your invoice is the authority.` : '');
      },
      progress() {
        const cases = AS.caseList(), s = study() || {};
        if (cases.length && cases.every(ended) && s.status === 'running') return 'The reporter is writing the comparison, about 20 seconds.';
        return cases.map(caseLine);
      },
      setup(c) { if (!c.local.followed) { c.local.followed = true; AS.followLive(); } } },
    validation: {
      body(c) {
        const v = doneCase(c), n = v ? AS.caseNumbers(v) : null, d = n ? n.deltas : {};
        const told = !!n && AS.isNum(d.cd_pct) && (AS.isNum(d.cl_pct) || AS.isNum(d.cl_abs));
        const r = (v && v.counters && v.counters.rework_loops) || 0;
        return 'Every case ends with the same condition run in XFoil. ' +
          (told ? `Here lift differs by ${AS.fmtClDelta(d)} and drag by ${AS.fmtPct(d.cd_pct)}. The limits are 5 % and 25 % for pass and 10 % and 50 % for warn` + (n.verdict ? `, so the verdict is ${n.verdict}.` : '.')
            : 'The differences in lift and drag are judged against limits of 5 % and 25 % for pass and 10 % and 50 % for warn.') +
          ' An agent in Solve, Post-process or Validate that distrusts a result can send the case back to an earlier stage, at most twice.' +
          (r > 0 ? ` This case was sent back ${r === 1 ? 'once' : count(r) + ' times'} in this run.` : '');
      },
      setup(c) {
        pin(doneCase(c), 'validate');
        if (state.ui.viewerCollapsed.validate !== false) { state.ui.viewerCollapsed.validate = false; AS.invalidate(); }
        if (q('#pane-main .viewer-head .seg-btn.on[data-vtab="field"]')) press(q('#pane-main .viewer-head [data-vtab="compare"]'));
      } },
    compare: {
      body() {
        const cases = AS.caseList(), f = facts();
        const rows = cases.map((x) => { const k = x.counters || {}; return `${k.rework_loops || 0} / ${k.gate_failures || 0} / ${k.human_interventions || 0} for NACA ${x.naca}`; });
        // What counted is named from the events of this run, and only when the counters bear it out:
        // every action is in them at least once (the mock, for one, does not count Pause and Resume).
        const total = cases.reduce((n, x) => n + ((x.counters || {}).human_interventions || 0), 0);
        const names = ACTED.filter((a) => f.acted[a[0]]).map((a) => a[1]);
        const counted = names.length && total >= f.actedCount
          ? ` ${capital(joinAnd(names))} ${names.length === 1 ? 'counts' : 'each count'} as an intervention on every case ${names.length === 1 ? 'it' : 'they'} reached.` : '';
        return 'Each case next to its XFoil reference, with the target L/D row and, in the last row, rework, gate failures and your interventions' + (rows.length ? ': ' + joinAnd(rows) : '') +
          '.' + counted + ' The reporter\'s recommendation and what was not verified are further down this page.';
      } },
    dashboard: { softTimeout: 8000,
      body(c) {
        const done = AS.caseList().filter((x) => x.status === 'done'), bad = c.bad, g = (bad && bad.counters && bad.counters.gate_failures) || 0, p = AS.project();
        return 'Each cell is the latest run of one model on one airfoil: result, difference to XFoil, rework and gate failures. ' +
          `Your ${done.length === 1 ? 'run is' : count(done.length) + ' runs are'} in the ${AS.modelLabel(c.model)} row` +
          (bad && bad.status === 'done' ? `, NACA ${bad.naca} with ${g ? 'its ' + count(g) + ' gate failure' + (g === 1 ? '' : 's') : 'no gate failure'}` : '') + '.' +
          (p && p.name === LIVE_RUN.project.name ? ' The filter shows the project Tutorial, where these runs are kept apart from your own numbers.' : '') + PASS_K;
      },
      // the dashboard reloads after a case has finished: wait for a run of this study in the matrix
      ready() { const t = q('#pane-dashboard table.matrix'); return !!t && AS.caseList().some((x) => x.status === 'done' && String(t.textContent).includes(String(x.naca))); } },
    // No button on the end card starts another paid run.
    finish: {
      body(c) {
        const s = study() || {}, p = AS.project(), n = AS.caseList().length, branch = c.branch;
        const dirs = AS.caseList().map((x) => x.dir || `cases/st_${s.id}_${x.naca}`);
        return `Mission, plan, ${count(n)} case${n === 1 ? '' : 's'} in parallel, ` +
          (branch === 'rejected' ? 'a gate that caught the bad value' : branch === 'fixed-first' ? 'an agent that corrected the bad value before the gate' : 'a bad value set on purpose') +
          ', steering, validation and history' + (c.paid ? ', all with real agents and a real solver' : '') + `. This run cost ${AS.fmtCost(c.cost())}. ` +
          `The study is kept in project ${p ? p.name : LIVE_RUN.project.name}: open it again from the study list on the Mission page or from the run history on the Dashboard.` +
          (dirs.length ? ` Its case files are in ${joinAnd(dirs)}.` : '');
      },
      actions: () => [{ label: 'Explore this study', primary: true, run: () => api.stop() },
        { label: 'Start your own study', title: 'Leave the tutorial and return to the page you came from', run: () => api.exit(true) },
        { label: 'Replay on recorded data, free', run: toRecorded }],
      setup() { finished = true; R.done = true; save(); } },
  };

  // Live-only steps. They are not among the 30 and show no count.
  /* A stage failed (three failed gates in a row, a turn limit, a model error) and its case waits. */
  const NEEDS_YOU = { id: 'needs-you', interrupt: true, chapter: 'Failure and retry', kind: 'do', place: 'top', own: true, title: 'The case is waiting for you',
    target: '#approve-stage', frame: '.approve-box', allow: ['#approve-stage'],
    when: () => AS.caseList().find((x) => x.status === 'awaiting_approval' && (AS.stageOf(x, x.current_stage) || {}).status === 'failed') || null,
    fails(c) { const x = state.cases[c.local.caseId]; return x ? facts().failed.filter((ev) => ev.case_id === x.id && (ev.stage || (ev.data || {}).stage) === x.current_stage) : []; },
    body(c) {
      const x = state.cases[c.local.caseId];
      if (!x) return '';
      const fails = this.fails(c), stg = AS.stageOf(x, x.current_stage) || {};
      const reason = clean(String(stg.failure || ((fails[fails.length - 1] || {}).data || {}).reason || '').slice(0, 160));
      return (fails.length >= 2 ? 'It failed again' : `The ${AS.stageTitle(x.current_stage)} stage of NACA ${x.naca} failed`) + (reason ? ': ' + reason : '') +
        '. After three failed gates in a row, or an agent error, the stage stops and the case waits. Press Approve retry to let the agent try again.' + (c.paid ? ' A retry costs a few more model calls.' : '');
    },
    // after a second failure of the same stage the reader may give this case up and go on with the other
    actions(c) { return this.fails(c).length >= 2 ? [{ label: 'Stop this case and go on', run: () => AS.actions.control('stop', c.local.caseId) }] : []; },
    setup(c) { const x = state.cases[c.local.caseId]; if (x) pin(x, x.current_stage); },
    done(c) { const x = state.cases[c.local.caseId]; return !x || x.status !== 'awaiting_approval'; },
    after: () => resumeAt,
    auto(c) { if (this.fails(c).length >= 2) AS.actions.control('stop', c.local.caseId); else press(q('#approve-stage')); } };
  /* After the retry has passed, that case asks before every later stage until the mode is set again. */
  const RETURN_AUTO = { id: 'return-auto', interrupt: true, chapter: 'Failure and retry', kind: 'do', place: 'top', own: true, title: 'Back to auto-run',
    target: '#return-auto', frame: '#fallback-note', allow: ['#return-auto'],
    body: 'After a failed stage this case asks before every later stage, although the study is in auto-run. Press Return to auto-run so that it carries on by itself.',
    when: () => AS.caseList().find((x) => AS.fallbackGated(x)) || null,
    setup(c) { const x = state.cases[c.local.caseId]; if (x) pin(x, x.current_stage); },
    done: () => !AS.caseList().some((x) => AS.fallbackGated(x)),
    after: () => resumeAt,
    auto() { press(q('#return-auto')); } };
  const INTERRUPTS = [NEEDS_YOU, RETURN_AUTO];
  /* Optional, reached from the card "The agent fixed it first": one re-run of Mesh through the ordinary control. */
  const SEE_GATE = { id: 'see-gate', chapter: 'Failure and retry', kind: 'do', place: 'top', own: true, title: 'See the gate reject it',
    target: '#rerun-btn', frame: 'details.rerun', allow: ['#rerun-btn'],
    body: (c) => `An instruction for the mesh agent is written for you: keep your ${AS.fmtValue(typedHeight())}, submit it to the gate, and fix it only after the gate has said no. Press Re-run this stage. ` +
      `Mesh and every later stage of NACA ${nacaOf(0, c.bad)} run again: about 2.5 more minutes` + (c.paid ? ` and about ${figures(c.model).rerun} USD` : '') + '. It counts as one intervention.',
    actions: () => [{ label: 'Go on without it', run() { R.gateOffered = true; save(); go(stepIndex('gate-failed') + 1); } }],
    setup: armRerun,
    tick(c) { const field = byId('rerun-instruction'); if (!c.local.used && (!field || !field.value.trim())) armRerun(c); },
    onUse(c, e) { if (e.type === 'click') c.local.used = true; },      // the page empties the box after the click: it is not filled again
    done(c) { const bad = c.bad; return !!bad && R.seqAtRerun != null && state.events.some((ev) => ev.type === 'stage.rework' && ev.case_id === bad.id && (ev.data || {}).to === 'mesh' && ev.seq > R.seqAtRerun); },
    // Mesh is stale by then, so the step that watches the mesh agent waits for the new run
    after() { R.gateOffered = true; save(); return stepIndex('mesh-agent'); },
    auto() { press(q('#rerun-btn')); } };
  /* An end that cannot be continued. It replaces the open step and is left only by its buttons. */
  const TROUBLE = { id: 'trouble', chapter: 'Tutorial', kind: 'look', place: 'centre', title: 'The run needs you', final: true, noNext: true, noLeave: true,
    body(c) {
      const f = facts(), kind = c.local.kind, cost = AS.fmtCost(c.cost());
      const message = clean(String(((f.error || {}).data || {}).message || '').replace(/^(candidate suggestion failed|planning failed):\s*/, ''));
      if (kind === 'advisor') return `The advisor could not finish${message ? ': ' + message : ''}. ` + (/ANTHROPIC_API_KEY/.test(message) ? 'Nothing was spent.' : `Cost so far: ${cost}. If Docker is not running, start it with colima start and try again.`);
      if (kind === 'planner') return `The planner could not write the plan${message ? ': ' + message : ''}.`;
      if (kind === 'no_candidate') return `XFoil did not reach the design lift for any candidate, so there is no section to plan. Cost so far: ${cost}.`;
      if (kind === 'cost') {
        // worded from the status of the study, never from the request alone
        const head = `The cost reached ${cost}, the limit of this tutorial (${money(R.ceiling_usd)} USD)`, how = stopState();
        if (how === 'stopped') return head + ', so the run was stopped. Finished stages and files are kept.';
        if (how === 'ended') return head + '. The run ended before the stop took effect. Finished stages and files are kept.';
        if (how === 'idle') return head + '. Nothing is running now, so nothing more is spent.';
        if (how === 'again') return head + '. The study has been started again since then, and the tutorial does not stop it a second time.';
        if (how === 'stopping') return head + '. The run is being stopped.';
        if (how === 'retrying') return head + '. The stop request did not go through and is being sent again.';
        return head + (stopper.taken ? '. The server took the stop request, but the run is still going.' : '. The stop request did not go through, so the run is still going.') + ' Press Stop the run to try again.';
      }
      if (kind === 'model') {
        // without a limit reached, the only request is the reader's own press on Stop the run: a failed one is said too
        const how = R.stopSent ? stopState() : stopper.id === R.study && stopper.failed && !stopper.busy && studyActive() ? 'failed' : '';
        return `This study runs with ${AS.modelLabel(R.mismatch)}, not with ${AS.modelLabel(R.model)} as the first card said, so the tutorial does not go on with it. Cost so far: ${cost}.` +
          (how === 'stopped' ? ' The run was stopped.' : how === 'stopping' ? ' The run is being stopped.' : how === 'retrying' ? ' The stop request did not go through and is being sent again.'
            : how === 'failed' ? ' The stop request did not go through. Press Stop the run to try again.'
            : !how && studyActive() ? ' It is still running.' + (AS.isNum(R.ceiling_usd) ? ` The limit of ${money(R.ceiling_usd)} USD applies while this page is open.` : '') : '');
      }
      if (kind === 'no_report') return 'The study ended without a report: no case finished. Finished stages and files are kept.';
      const reason = clean(f.stopReason || ((f.error || {}).data || {}).message);
      return `The run was stopped before it finished${reason ? ': ' + reason : ''}. Finished stages and files are kept.`;
    },
    actions(c) {
      // a study that is still at work is not left without the Leave row
      const kind = c.local.kind, leave = { label: 'Leave', run: () => api.exit(!studyActive()) };
      // a new study: the one that failed stays in the project. Until the reader's next press on Suggest
      // has created it the record names no study, so the cost watch has nothing to act on.
      if (kind === 'advisor' || kind === 'no_candidate') {
        return [{ label: 'Try again', primary: true, run() { mine = null; AS.newStudy(); R.study = null; R.mismatch = null; go(stepIndex('suggest')); } }, { label: 'Recorded tutorial, free', run: toRecorded }, leave];
      }
      // the error is noted as seen, or the watch would raise this card again at once
      if (kind === 'planner') return [{ label: 'Try again', primary: true, run() { R.ackSeq = (facts().error || {}).seq || state.lastSeq; save(); go(stepIndex('confirm')); } }, leave];
      // the reader's own press: the tutorial's requests did not stop the run, or it runs with a model the reader did not choose
      const byHand = studyActive() && (R.stopSent ? stopState() === 'failed' : kind === 'model');
      return (byHand ? [{ label: 'Stop the run', key: 'stop-run', primary: true, disabled: stopper.busy, run: () => requestStop(true) }] : [])
        .concat([{ label: 'Explore this study', primary: !byHand, run: () => api.stop() }, leave]);
    } };
  /* Cards that are not steps of a run: shown in place of the tutorial, or over the page after the guide was put away. */
  const SYSTEM = { system: true, chapter: 'Live tutorial', kind: 'look', place: 'centre', title: 'Live tutorial', final: true, noNext: true, noLeave: true };
  const BLOCKED_CARD = Object.assign({ id: 'blocked', body: 'This page is running on recorded data, so the live tutorial cannot start here.',
    actions: () => [{ label: 'Recorded tutorial, free', primary: true, run: toRecorded }, { label: 'Leave', run: () => api.exit(true) }] }, SYSTEM);
  const NO_SERVER_CARD = Object.assign({ id: 'no-server', body: 'The live tutorial needs the studio server. Start it with wing-studio and reload this page, or use the recorded tutorial.',
    actions: () => [{ label: 'Recorded tutorial, free', primary: true, run: toRecorded }, { label: 'Leave', run: () => api.exit(true) }] }, SYSTEM);
  const LEAVING_CARD = Object.assign({ id: 'leaving', body: () => leaveText().replace(/^Leave the tutorial\?\s*/, ''), actions: () => leaveActions(true) }, SYSTEM, { title: 'Leave the tutorial?' });

  /* The Leave row of a live run. The study is on the server: leaving does not stop it. */
  function leaveText() {
    if (!studyActive()) return 'Leave the tutorial?';
    return 'Leave the tutorial? The study keeps running on the server' + (ctx.paid ? ', and keeps costing,' : '') + ' until it ends.' + (runPaused() ? ' The run is paused.' : '');
  }
  function leaveActions(standalone) {
    const out = standalone ? [{ label: 'Stay here', key: 'stay', primary: true, run: () => api.stop() }]
      : [{ label: 'Keep going', key: 'keep', primary: true, run: () => setLeave(false) },
        { label: 'Explore on my own', key: 'explore', title: 'Remove the guide and stay in this study', run: () => api.stop() }];
    if (!studyActive()) out.push({ label: 'Exit', key: 'exit', title: 'Leave the tutorial and return to the page you came from', run: () => api.exit(true) });
    else {
      out.push(runPaused() ? { label: 'Resume and leave', key: 'resume-leave', async run() { await AS.actions.control('resume'); api.exit(true); } }
        : { label: 'Leave, let it finish', key: 'let-finish', run: () => api.exit(true) });
      // a Stop that failed (the action shows why) must not be followed by leaving a run that still spends
      out.push({ label: 'Stop the study and leave', key: 'stop-leave', async run() { if ((await AS.actions.control('stop')) !== null) api.exit(true); } });
    }
    return out;
  }

  /* The live order: steering and Pause need a running agent, so the three steps that point at content
     that stays (the tree, the viewer, three failures) come after them, in a chapter of their own. */
  const LIVE_ORDER = ['welcome', 'example', 'suggest', 'advisor', 'candidates', 'confirm', 'plan', 'bad-value', 'set-by-engineer', 'approve', 'tabs',
    'mesh-agent', 'gate-failed', 'attempt-2', 'mesh-passed', 'other-case', 'convergence', 'send', 'reply', 'pause-resume', 'tree', 'viewer', 'three-failures',
    'finish-run', 'validation', 'open-compare', 'compare', 'open-dashboard', 'dashboard', 'finish'];
  const LIVE_CHAPTERS = [[6, 'Mission'], [10, 'Plan'], [11, 'Parallel run'], [15, 'Failure and retry'], [20, 'Steering'], [23, 'While it finishes'], [30, 'Results']];
  for (const step of STEPS) if (LIVE_STEP[step.id]) step.live = LIVE_STEP[step.id];
  /* The steps the page plays: the recorded list as it is, or the live order with each step's live fields merged over it. */
  const LIST = !LIVE_STEPS ? STEPS : LIVE_ORDER.map((id, i) => Object.assign({}, STEPS.find((s) => s.id === id), LIVE_STEP[id], { chapter: LIVE_CHAPTERS.find((ch) => i < ch[0])[1] }));
  const TOTAL = LIST.length;

  // ---------- geometry ----------
  const PAD = 4;           // the hole is this much larger than the element it shows
  const GAP = 30;          // between the hole and the card: room for the arrow
  const scrolls = (value) => value === 'auto' || value === 'scroll';
  function resolve(step) {
    return safe(() => {
      let t = step.target;
      if (typeof t === 'function') t = t.call(step, ctx);
      if (!t) return null;
      if (typeof t === 'string') return q(t);
      return t.isConnected && laidOut(t) ? t : null;
    }, null);
  }
  /* The part of an element that can be seen: its box cut by every ancestor that clips and by the window. */
  function visibleRect(el) {
    const b = el.getBoundingClientRect();
    const r = { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
    for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const pr = p.getBoundingClientRect(), left = pr.left + p.clientLeft, top = pr.top + p.clientTop;
      if (cs.overflowX !== 'visible') { r.left = Math.max(r.left, left); r.right = Math.min(r.right, left + p.clientWidth); }
      if (cs.overflowY !== 'visible') { r.top = Math.max(r.top, top); r.bottom = Math.min(r.bottom, top + p.clientHeight); }
    }
    r.left = Math.max(r.left, 0); r.top = Math.max(r.top, 0);
    r.right = Math.min(r.right, window.innerWidth); r.bottom = Math.min(r.bottom, window.innerHeight);
    return r.right - r.left >= 1 && r.bottom - r.top >= 1 ? r : null;
  }
  /* Scroll an element into its panel. Only ancestors that scroll are moved: the page is a fixed grid
     of overflow:hidden boxes, which Element.scrollIntoView can shift with no way back. The margins keep
     the element clear of the sticky report head and the sticky action bar. */
  function reveal(el) {
    if (!el || el.closest('.sticky-foot, .report-head')) return;     // stuck to its panel: always in view
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      const pr = p.getBoundingClientRect(), r = el.getBoundingClientRect();
      if (scrolls(cs.overflowY) && p.scrollHeight > p.clientHeight + 1) {
        const top = pr.top + p.clientTop + Math.min(36, p.clientHeight * 0.2), bottom = pr.top + p.clientTop + p.clientHeight - Math.min(56, p.clientHeight * 0.3);
        if (r.top < top) p.scrollTop -= top - r.top;
        else if (r.bottom > bottom) p.scrollTop += Math.min(r.bottom - bottom, r.top - top);
      }
      if (scrolls(cs.overflowX) && p.scrollWidth > p.clientWidth + 1) {
        const left = pr.left + p.clientLeft + 8, right = pr.left + p.clientLeft + p.clientWidth - 8;
        if (r.left < left) p.scrollLeft -= left - r.left;
        else if (r.right > right) p.scrollLeft += Math.min(r.right - right, r.left - left);
      }
    }
  }
  /* The box a wheel turn over the shade should scroll: the target itself or its nearest scrolling ancestor. */
  function scrollHost(el) {
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      if (scrolls(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight + 1) return p;
    }
    return null;
  }
  const OPPOSITE = { top: 'bottom', bottom: 'top', left: 'right', right: 'left' };
  /* Where the card goes: the side the step asks for, its opposite, the other two, else docked bottom right. */
  function place(step, hole, target, cw, ch, keep) {
    const W = window.innerWidth, H = window.innerHeight, M = 8;
    const cx = (target.left + target.right) / 2, cy = (target.top + target.bottom) / 2;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const at = (side) => {
      let x, y;
      if (side === 'bottom') { x = cx - cw / 2; y = hole.bottom + PAD + GAP; }
      else if (side === 'top') { x = cx - cw / 2; y = hole.top - PAD - GAP - ch; }
      else if (side === 'right') { x = hole.right + PAD + GAP; y = cy - ch / 2; }
      else { x = hole.left - PAD - GAP - cw; y = cy - ch / 2; }
      if (side === 'top' || side === 'bottom') x = clamp(x, M, W - cw - M); else y = clamp(y, M, H - ch - M);
      return x >= M && y >= M && x + cw <= W - M && y + ch <= H - M ? { x: Math.round(x), y: Math.round(y), side } : null;
    };
    const want = OPPOSITE[step.place] ? step.place : 'bottom';
    const order = [want, OPPOSITE[want]].concat(['bottom', 'top', 'right', 'left'].filter((s) => s !== want && s !== OPPOSITE[want]));
    if (keep && OPPOSITE[keep]) { order.splice(order.indexOf(keep), 1); order.unshift(keep); }   // a card keeps its side while it fits
    for (const side of order) { const pos = at(side); if (pos) return pos; }
    return { x: W - cw - 16, y: H - ch - 40, side: 'dock' };
  }
  /* A target inside a larger hole: the arrow ends on the edge of the hole that faces the card, in line
     with the target, so it does not run across the table or the fields shown in the hole. */
  function aimAt(card, t, box) {
    const a = { left: t.left, top: t.top, right: t.right, bottom: t.bottom };
    if (card.bottom <= box.top) a.top = a.bottom = box.top;
    else if (card.top >= box.bottom) a.top = a.bottom = box.bottom;
    else if (card.right <= box.left) a.left = a.right = box.left;
    else if (card.left >= box.right) a.left = a.right = box.right;
    return a;
  }
  /* The arrow: an orthogonal connector from the middle of the card edge that faces the target to the
     nearest point of the target's edge. Straight when the two spans overlap, one elbow otherwise.
     Returns the corner points and the unit direction of the last leg, or null when the card covers the target. */
  function route(card, target, endGap, side) {
    const above = card.bottom <= target.top, below = card.top >= target.bottom;
    const leftOf = card.right <= target.left, rightOf = card.left >= target.right;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const sideways = (leftOf || rightOf) && (side === 'left' || side === 'right' || !(above || below));
    if (!sideways && (above || below)) {
      const y0 = below ? card.top : card.bottom, yEnd = below ? target.bottom + endGap : target.top - endGap;
      const inset = Math.min(6, (target.right - target.left) / 2);
      const lo = Math.max(card.left + 14, target.left + inset), hi = Math.min(card.right - 14, target.right - inset);
      if (lo <= hi) { const x = clamp((card.left + card.right) / 2, lo, hi); return { pts: [[x, y0], [x, yEnd]], dir: [0, below ? -1 : 1] }; }
      const x = (card.left + card.right) / 2, y = (target.top + target.bottom) / 2, fromRight = x > target.right;
      return { pts: [[x, y0], [x, y], [fromRight ? target.right + endGap : target.left - endGap, y]], dir: [fromRight ? -1 : 1, 0] };
    }
    if (sideways) {
      const x0 = rightOf ? card.left : card.right, xEnd = rightOf ? target.right + endGap : target.left - endGap;
      const inset = Math.min(6, (target.bottom - target.top) / 2);
      const lo = Math.max(card.top + 14, target.top + inset), hi = Math.min(card.bottom - 14, target.bottom - inset);
      if (lo <= hi) { const y = clamp((card.top + card.bottom) / 2, lo, hi); return { pts: [[x0, y], [xEnd, y]], dir: [rightOf ? -1 : 1, 0] }; }
      const y = (card.top + card.bottom) / 2, x = (target.left + target.right) / 2, fromBelow = y > target.bottom;
      return { pts: [[x0, y], [x, y], [x, fromBelow ? target.bottom + endGap : target.top - endGap]], dir: [0, fromBelow ? -1 : 1] };
    }
    return null;
  }
  /* The node a step shows in the hole together with its target (also), as the part of it that can be seen. */
  function alsoRect(step) {
    const el = safe(() => (typeof step.also === 'function' ? step.also.call(step, ctx) : q(step.also)), null);
    return el && el.isConnected && laidOut(el) ? visibleRect(el) : null;
  }
  const union = (a, b) => ({ left: Math.min(a.left, b.left), top: Math.min(a.top, b.top), right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom) });

  // ---------- overlay DOM ----------
  let root = null, els = null;
  let active = false, idx = -1, special = null, finished = false, testing = false;
  let st = {};             // run-time facts of the step on screen, reset on every step change
  let raf = 0, timer = 0, busy = false, firstPaint = true;
  let closedAt = null;     // where the hole last was: a closed hole collapses to that point
  let resumeAt = -1;       // live: the step an interrupt was shown over, entered again when it is done
  let shown = null;        // the step on screen with its variant merged in, worked out once per pass
  /* A step with a variant is shown as that block merged over it. The prototype is the step itself,
     so its other fields and its own functions keep working. */
  function effective(step) {
    if (!step || !step.variant) return step;
    const key = safe(() => step.variant(ctx), '');
    const block = key && step.variants ? step.variants[key] : null;
    return block ? Object.assign(Object.create(step), block, { variantKey: key }) : step;
  }
  const cur = () => shown || (shown = effective(special || LIST[idx] || null));

  const half = (v) => Math.round(v * 2) / 2;
  function setBox(el, x, y, w, hgt) {
    const key = [half(x), half(y), w == null ? '' : half(Math.max(0, w)), hgt == null ? '' : half(Math.max(0, hgt))].join(',');
    if (el.__box === key) return;
    el.__box = key;
    el.style.left = half(x) + 'px'; el.style.top = half(y) + 'px';
    if (w != null) { el.style.width = half(Math.max(0, w)) + 'px'; el.style.height = half(Math.max(0, hgt)) + 'px'; }
  }
  function setAttr(el, name, value) { if (el.getAttribute(name) !== value) el.setAttribute(name, value); }

  const LISTEN = [['pointerdown', onPointer], ['mousedown', onPointer], ['mouseup', onPointer], ['click', onPointer], ['dblclick', onPointer], ['auxclick', onPointer],
    ['keydown', onKey], ['focusin', onFocusIn], ['input', soon], ['change', soon], ['keyup', soon]];
  function mount() {
    if (root) return;
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'tour-arrow'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    const path = (cls) => { const p = document.createElementNS(SVG, 'path'); p.setAttribute('class', cls); svg.appendChild(p); return p; };
    els = {
      shades: ['t', 'r', 'b', 'l'].map((k) => h('div', { class: 'tour-shade ' + k })),
      plug: h('div', { class: 'tour-plug', hidden: true }),
      ring: h('div', { class: 'tour-ring', hidden: true }),
      // the line is drawn over the head, so the head's own white outline never cuts into it
      arrow: svg, halo: path('halo'), head: path('head'), line: path('line'),
      top: h('div', { class: 'tour-head' }),
      content: h('div', { class: 'tour-content' }),
      live: h('div', { class: 'tour-live', role: 'status', 'aria-live': 'polite' }),
    };
    els.card = h('div', { class: 'tour-card', role: 'dialog', 'aria-labelledby': 'tour-title', 'aria-describedby': 'tour-body', tabindex: '-1' }, els.top, els.content);
    root = h('div', { id: 'tour', class: 'tour' }, els.shades, els.plug, els.ring, svg, els.card, els.live);
    document.body.appendChild(root);
    document.body.classList.add('tour-on');
    for (const [name, fn] of LISTEN) window.addEventListener(name, fn, true);
    window.addEventListener('wheel', onWheel, { capture: true, passive: true });
    window.addEventListener('blur', onBlur);
    firstPaint = true; closedAt = null;
    const loop = () => { raf = requestAnimationFrame(loop); tick(); };
    raf = requestAnimationFrame(loop);
    timer = setInterval(tick, 500);        // animation frames stop in a background tab; the run does not
  }
  function unmount() {
    if (!root) return;
    cancelAnimationFrame(raf); clearInterval(timer);
    for (const [name, fn] of LISTEN) window.removeEventListener(name, fn, true);
    window.removeEventListener('wheel', onWheel, { capture: true });
    window.removeEventListener('blur', onBlur);
    root.remove();
    document.body.classList.remove('tour-on');
    root = null; els = null;
  }

  // ---------- the card ----------
  const text = (value, step) => (typeof value === 'function' ? safe(() => value.call(step, ctx), '') : value) || '';
  /* A text of the live list may be a list of parts; { cost } is the part set in the cost style. */
  const rich = (value) => (Array.isArray(value) ? value.map((part) => (part && part.cost != null ? h('span', { class: 'tour-cost' }, part.cost) : part)) : value);
  const plain = (value) => (Array.isArray(value) ? value.map((part) => (part && part.cost != null ? part.cost : part)).join('') : String(value || ''));
  const linesOf = (value) => (Array.isArray(value) ? value : value ? [value] : []);
  const actionsOf = (step) => (typeof step.actions === 'function' ? safe(() => step.actions.call(step, ctx), []) : step.actions) || [];
  function button(label, run, opts) {
    opts = opts || {};
    return h('button', { type: 'button', class: 'btn small' + (opts.primary ? ' primary' : '') + (opts.cls ? ' ' + opts.cls : ''), title: opts.title || null, disabled: !!opts.disabled,
      dataset: { key: 'tour:' + (opts.key || plain(label)) }, onclick: run }, rich(label));
  }
  const canNext = (step) => step.kind === 'look' && !step.noNext && !st.waiting && !st.leaveOpen;
  /* The step Back goes to: the nearest earlier one that is not passed over, when it is a look step.
     A step with variants is never gone back to: its kind is only known once it is on screen. */
  function backIndex() {
    let i = idx - 1;
    while (i > 0 && LIST[i].skip && safe(() => LIST[i].skip(ctx), false)) i -= 1;
    return i >= 0 && LIST[i].kind === 'look' && !LIST[i].variant ? i : -1;
  }
  const canBack = (step) => step.kind === 'look' && !special && !step.actions && idx > 0 && backIndex() >= 0 && !st.leaveOpen;
  function userNext() { const step = cur(); if (step && canNext(step)) go(idx + 1); }
  function userBack() { const step = cur(); if (step && canBack(step)) go(backIndex()); }
  function setLeave(open) { st.leaveOpen = open; st.focus = true; tick(); }
  async function doAuto() {
    const step = cur();
    if (!step || !step.auto) return;
    try { await step.auto(ctx); } catch (e) { console.error('tutorial', e); }
    tick();
  }
  function tryAgain() {
    const step = cur();
    st.missingSince = 0; st.retries = 0; st.reveal = true;
    runSetup(step);
    tick();
  }
  function showIt() { st.away = false; st.reveal = true; st.lastWheel = 0; tick(); }

  function paintCard(step, now) {
    const n = special ? 0 : idx + 1;
    AS.region(els.top, [step.chapter, n], () => [h('span', { class: 'tour-chapter' }, step.chapter),
      n ? h('span', { class: 'tour-count', title: `Step ${n} of ${TOTAL}` }, `${n} / ${TOTAL}`) : null]);
    const missingFor = st.missing ? now - st.missingSince : 0;
    const acts = actionsOf(step);
    // live: what the run is doing, under a wait card or a waiting one. The texts are set after the
    // region, so a clock that counts seconds does not rebuild the card.
    const watching = LIVE_STEPS && !st.missing && (st.waiting || step.kind === 'wait');
    const lines = watching && step.progress ? linesOf(text(step.progress, step)).filter(Boolean).map((line) => (typeof line === 'string' ? { text: line } : line)) : [];
    const m = {
      id: step.id, title: text(step.title, step), kind: step.kind,
      mode: st.waiting ? 'waiting' : st.missing ? 'missing' : 'shown',
      body: text(step.body, step), note: text(step.note, step),
      hint: (step.hint ? text(step.hint, step) : '') || (st.linkOff ? 'Open study is off during the tutorial: it would replace the study on this page.' : ''),
      doLine: step.kind === 'do' ? text(step.doLine, step) || 'Press the highlighted control' : '',
      // a live run cannot be held, so the tag is never shown on the live list
      held: !LIVE_STEPS && step.kind === 'look' && !st.waiting && !step.final && mock.holding().length > 0,
      heldSlot: !LIVE_STEPS && step.kind === 'look' && !step.final && !!state.study && (state.study.status === 'running' || state.study.status === 'paused'),
      back: canBack(step), next: step.kind === 'look' && !step.noNext,
      auto: step.kind === 'do' && !!step.auto && !(LIVE && step.own) && (now - ctx.enteredAt > 8000 || missingFor > 4000),
      retry: missingFor > 4000, away: !!st.away, late: !!st.late, leave: !!st.leaveOpen,
      // a look step that waits for something that never comes (the viewer with noviewer=1), or whose
      // target stays away, must not be a dead end: it asks for nothing, so it can be passed over
      // (a real model call is slow, not stuck: on the live list only after the soft timeout)
      skip: step.kind === 'look' && !special && idx + 1 < TOTAL && ((st.waiting && now - ctx.enteredAt > (LIVE_STEPS ? step.softTimeout || SOFT_TIMEOUT : 8000)) || missingFor > 4000),
      variant: step.variantKey || '', wait: text(step.waitText, step), nextLabel: text(step.nextLabel, step),
      acts: acts.map((a) => [plain(a.label), !!a.disabled]),
      expect: watching ? text(step.expect, step) : '',
      lateText: watching && st.late ? (step.lateText !== undefined ? text(step.lateText, step) : 'This is taking longer than usual. The run is still going.') : '',
      lines: lines.map((line) => !!line.idle),
      leaveRow: LIVE_STEPS && st.leaveOpen ? [leaveText()].concat(leaveActions(false).map((a) => a.label)) : null,
    };
    AS.region(els.content, m, () => {
      const out = [h('div', { class: 'tour-title', id: 'tour-title', role: 'heading', 'aria-level': '2' }, m.title)];
      if (m.mode === 'waiting') out.push(h('div', { class: 'tour-body', id: 'tour-body' }, AS.ui.spinner(m.wait || 'Waiting for the run')));
      else if (m.mode === 'missing') out.push(h('div', { class: 'tour-body', id: 'tour-body' }, AS.ui.spinner('Looking for this control')));
      else {
        out.push(h('div', { class: 'tour-body', id: 'tour-body' }, rich(m.body)));
        for (const line of linesOf(m.note)) out.push(h('div', { class: 'tour-note' }, line));
        if (m.hint) out.push(h('div', { class: 'tour-hint', role: 'note' }, m.hint));
      }
      for (const idle of m.lines) out.push(h('div', { class: 'tour-progress' }, idle ? null : h('i', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { class: 'tour-progress-text' })));
      if (m.expect) out.push(h('div', { class: 'tour-expect' }, m.expect));
      if (m.held || m.heldSlot) out.push(h('div', { class: 'tour-held', style: m.held ? null : { visibility: 'hidden' } }, h('span', { class: 'tour-tag', title: 'The recorded run stands still at a checkpoint and goes on with the next step' }, 'Run held while you read')));
      // live: one more line, and the step keeps waiting
      if (LIVE_STEPS) { if (m.lateText) out.push(h('div', { class: 'tour-hint', role: 'note' }, m.lateText)); }
      else if (m.late) out.push(h('div', { class: 'tour-hint', role: 'note' }, 'This is taking longer than expected.'));
      if (m.leave && LIVE_STEPS) {
        out.push(h('div', { class: 'tour-foot tour-leave' }, h('span', { class: 'tour-ask' }, leaveText()), leaveActions(false).map((a) => button(a.label, a.run, a))));
        return out;
      }
      if (m.leave) {
        out.push(h('div', { class: 'tour-foot tour-leave' }, h('span', { class: 'tour-ask' }, 'Leave the tutorial?'),
          button('Keep going', () => setLeave(false), { primary: true, key: 'keep' }),
          button('Explore on my own', () => api.stop(), { key: 'explore', title: 'Remove the guide and stay in this recorded study' }),
          button('Exit', () => api.exit(), { key: 'exit', title: 'Leave the recorded study and return to the page you came from' })));
        return out;
      }
      const row = [];
      if (m.away) row.push(button('Show it', showIt, { key: 'show' }));
      if (m.mode === 'missing') {
        if (m.retry) row.push(button('Try again', tryAgain, { key: 'retry' }));
      } else if (m.mode === 'waiting' || m.kind === 'wait') {
        row.push(h('span', { class: 'tour-line' }, AS.ui.spinner('Waiting: controls are locked')));
      } else if (m.kind === 'do') {
        row.push(h('span', { class: 'tour-line' }, m.doLine));
      } else {
        if (m.back) row.push(button('Back', userBack, { key: 'back' }));
        if (m.next) row.push(button(m.nextLabel || 'Next', userNext, { primary: true, cls: 'tour-next', key: 'next' }));
      }
      if (m.skip) row.push(button('Skip', () => go(idx + 1), { key: 'skip' }));
      if (m.auto && m.mode !== 'waiting') row.push(button('Do it for me', doAuto, { key: 'auto' }));
      if (m.late && !LIVE_STEPS) row.push(button('Restart', () => api.restart(), { key: 'restart' }));
      for (const a of acts) row.push(button(a.label, a.run, { primary: a.primary, title: a.title, key: a.key, disabled: a.disabled }));
      row.push(h('span', { class: 'spacer' }));
      if (!step.noLeave) row.push(h('button', { type: 'button', class: 'link-btn tour-exit', dataset: { key: 'tour:leave' }, onclick: () => setLeave(true) }, 'Leave'));
      out.push(h('div', { class: 'tour-foot' }, row));
      return out;
    });
    if (lines.length) {
      const nodes = $$('.tour-progress-text', els.content);
      lines.forEach((line, i) => { if (nodes[i] && nodes[i].textContent !== line.text) nodes[i].textContent = line.text; });
    }
  }
  function focusCard() {
    if (!els) return;
    // on a card whose buttons leave the tutorial, on the consent card of a live run and on a step whose
    // control spends, the keyboard starts on the card, so Enter presses none of them
    const step = cur();
    const el = step && (step.final || step.quiet || (handsOff(step) && !st.leaveOpen)) ? els.card
      : $('.tour-next:not([disabled])', els.card) || $('.tour-foot .btn.primary:not([disabled])', els.card) || $('.tour-foot button:not([disabled])', els.card) || els.card;
    el.focus({ preventScroll: true });
  }
  function announce(step) {
    const n = special ? '' : `Step ${idx + 1} of ${TOTAL}. `;
    els.live.textContent = `${n}${step.chapter}. ${plain(text(step.title, step))}. ${plain(text(st.waiting ? step.waitText : step.body, step))}`;
  }

  // ---------- engine ----------
  const isReady = (step) => !step.ready || !!safe(() => step.ready(ctx), false);
  function runSetup(step) { if (step && step.setup) safe(() => step.setup(ctx)); }
  /* why: 'next' when the step is left for another one, 'push' when an interrupt or a detour takes its place. */
  function leaveCurrent(why) { const step = cur(); if (step && step.leave) safe(() => step.leave(ctx, why || 'next')); }
  function resetStep() {
    st = { waiting: false, missing: false, missingSince: 0, retries: 0, away: false, late: false, leaveOpen: false, reveal: true, focus: true, focusNode: null,
      lastWheel: 0, lastReveal: 0, revealAgain: 0, pos: null, anchor: null, painted: false, covered: false };
    ctx.enteredAt = Date.now(); ctx.sawPaused = false; ctx.local = {};
    shown = null;
  }
  function enter(i) {
    special = null;
    for (;;) {
      idx = i;
      resetStep();
      const step = cur();
      // live: a step whose subject is not there (a case that stopped, a branch not taken) is passed over
      if (step.skip && i + 1 < TOTAL && safe(() => step.skip(ctx), false)) { i += 1; continue; }
      // a do step whose state is already reached has nothing to ask for
      if (step.kind === 'do' && i + 1 < TOTAL && isReady(step) && safe(() => step.done(ctx), false)) { if (step.leave) safe(() => step.leave(ctx, 'next')); i += 1; continue; }
      break;
    }
    const step = cur();
    st.waiting = !isReady(step);
    runSetup(step);
    save();
    announce(step);
  }
  function go(i) {
    if (!active || i < 0 || i >= TOTAL) return;
    leaveCurrent();
    enter(i);
    tick();
  }
  function tick() {
    if (!active || busy || !root) return;
    busy = true;
    try { pass(); } catch (e) { console.error('tutorial', e); } finally { busy = false; }
  }
  function soon() { if (active) setTimeout(tick, 0); }

  function pass() {
    const now = Date.now();
    // the snapshot dialog is modal and sits under this layer: the guide steps aside while it is open
    const box = byId('lightbox');
    st.covered = !!(box && !box.hidden);
    root.hidden = st.covered;
    if (st.covered) return;
    shown = null;            // the variant of the step is worked out again on every pass
    if (LIVE_STEPS) { adopt(); costWatch(); watch(); }
    let step = cur();
    if (step.tick) safe(() => step.tick(ctx, now));
    if (st.waiting && isReady(step)) { st.waiting = false; st.late = false; st.reveal = true; st.focus = true; runSetup(step); announce(step); }   // focus: Next exists only now
    // a live-only step that is done hands over to the step its after() names; no other special ever advances
    if (!st.waiting && !st.leaveOpen && (!special || step.after) && step.kind !== 'look' && step.done && safe(() => step.done(ctx), false)) {
      const dwell = step.kind === 'wait' && !testing ? 4000 : 0;
      if (special) { const to = safe(() => step.after(ctx), -1); enter(to >= 0 && to < TOTAL ? to : Math.max(0, idx)); step = cur(); }
      else if (now - ctx.enteredAt >= dwell && idx + 1 < TOTAL) { leaveCurrent(); enter(idx + 1); step = cur(); }
    }
    // live: a soft timeout adds a line and the step keeps waiting
    if (LIVE_STEPS) { if (!st.late && (step.kind === 'wait' || st.waiting) && now - ctx.enteredAt > (step.softTimeout || SOFT_TIMEOUT)) st.late = true; }
    else if (step.kind === 'wait' && !st.late && now - ctx.enteredAt > (step.timeout || 60000)) st.late = true;

    // where the target is now
    const centre = step.place === 'centre' || !step.target;
    let target = null, hole = null, tRect = null, same = true;
    if (!centre && !st.waiting) {
      target = resolve(step);
      if (!target) {
        if (!st.missingSince) st.missingSince = now;
        const gone = now - st.missingSince;
        if (gone < 250) {
          // A region is being rebuilt, or the page of a new step is drawn on the next frame: the node is
          // there within a frame or two. The geometry stays as it is; a new step already shows its text.
          if (firstPaint) root.hidden = true;          // nothing has been placed yet
          else if (!st.painted) paintCard(step, now);
          return;
        }
        st.missing = true;
        if (st.retries < 3 && gone >= 1000 * (st.retries + 1)) { st.retries += 1; runSetup(step); }
      } else {
        st.missing = false; st.missingSince = 0; st.retries = 0;
        const holeEl = (step.frame && target.closest(step.frame)) || target;
        same = holeEl === target;
        if (st.reveal) { st.reveal = false; st.lastReveal = now; st.revealAgain = now + 300; reveal(holeEl); reveal(target); }
        // once more after the app has drawn the page of this step (a page shown anew starts at its top)
        else if (st.revealAgain && now >= st.revealAgain) { st.revealAgain = 0; if (!st.lastWheel) { st.lastReveal = now; reveal(holeEl); reveal(target); } }
        hole = visibleRect(holeEl);
        if (!hole && !st.away) {
          // out of its panel: put back when the page moved by itself, offered back when the reader scrolled away
          if (now - st.lastWheel <= 1500) st.away = true;
          else if (now - st.lastReveal > 300) { st.lastReveal = now; reveal(holeEl); reveal(target); hole = visibleRect(holeEl); }
        }
        // a node shown with the target widens the hole; the arrow then ends on the edge of the hole, as with a frame
        const extra = hole && step.also ? alsoRect(step) : null;
        if (extra) { hole = union(hole, extra); same = false; }
        if (hole) { st.away = false; tRect = visibleRect(target) || hole; }
      }
    } else st.missing = false;

    paintCard(step, now);

    // classes
    const light = step.kind === 'wait' || st.waiting || st.missing || st.away;
    const cls = 'tour ' + step.kind + (light ? ' light' : '') + (centre ? ' centre' : '');
    if (root.className !== cls) root.className = cls;
    const cardCls = 'tour-card' + (centre ? ' centre' : '');
    if (els.card.className !== cardCls) els.card.className = cardCls;

    // shade, ring and plug
    const W = window.innerWidth, H = window.innerHeight;
    const [sTop, sRight, sBottom, sLeft] = els.shades;
    if (hole) {
      // whole pixels, and every panel derived from the same four numbers: no seam and no overlap between them
      const x0 = Math.max(0, Math.floor(hole.left - PAD)), y0 = Math.max(0, Math.floor(hole.top - PAD));
      const x1 = Math.min(W, Math.ceil(hole.right + PAD)), y1 = Math.min(H, Math.ceil(hole.bottom + PAD));
      setBox(sTop, 0, 0, W, y0); setBox(sBottom, 0, y1, W, H - y1); setBox(sLeft, 0, y0, x0, y1 - y0); setBox(sRight, x1, y0, W - x1, y1 - y0);
      const rx0 = Math.max(0, x0 - 2), ry0 = Math.max(0, y0 - 2);   // a target at a window edge keeps all four sides of its ring
      setBox(els.ring, rx0, ry0, Math.min(W, x1 + 2) - rx0, Math.min(H, y1 + 2) - ry0);
      st.box = { left: x0, top: y0, right: x1, bottom: y1 };
      setBox(els.plug, x0, y0, x1 - x0, y1 - y0);
      closedAt = [Math.round((x0 + x1) / 2), Math.round((y0 + y1) / 2)];
    } else {
      // no hole: it closes on the point where the last one was, so the panels still tile the window while they glide
      const cx = Math.min(W, closedAt ? closedAt[0] : Math.round(W / 2)), cy = Math.min(H, closedAt ? closedAt[1] : Math.round(H / 2));
      setBox(sTop, 0, 0, W, cy); setBox(sBottom, 0, cy, W, H - cy); setBox(sLeft, 0, cy, cx, 0); setBox(sRight, cx, cy, W - cx, 0);
    }
    els.ring.hidden = !hole;
    // look and wait steps show the control without handing it over
    els.plug.hidden = !hole || step.kind === 'do' || !!step.interactive;

    // the card: placed again only when the hole moved by more than 2 px or a size changed
    const cw = els.card.offsetWidth, ch = els.card.offsetHeight;
    let pos;
    if (centre) pos = { x: Math.round((W - cw) / 2), y: Math.max(8, Math.round((H - ch) / 2 - 16)), side: 'centre' };
    else if (!hole) pos = st.away && st.pos && OPPOSITE[st.pos.side] ? st.pos : { x: Math.round((W - cw) / 2), y: Math.max(8, H - ch - 40), side: 'below' };
    else {
      const a = st.anchor;
      const moved = !a || !st.pos || !(OPPOSITE[st.pos.side] || st.pos.side === 'dock') || a.cw !== cw || a.ch !== ch || a.W !== W || a.H !== H ||
        ['left', 'top', 'right', 'bottom'].some((k) => Math.abs(a[k] - hole[k]) > 2);
      if (moved) { pos = place(step, hole, tRect, cw, ch, st.pos && st.pos.side); st.anchor = Object.assign({ cw, ch, W, H }, hole); }
      else pos = st.pos;
    }
    st.pos = pos;
    setBox(els.card, pos.x, pos.y);

    // the arrow, from the card to the target; it stops 3 px short of the ring (or of a target inside a larger hole)
    const cardBox = { left: pos.x, top: pos.y, right: pos.x + cw, bottom: pos.y + ch };
    const way = hole && tRect ? route(cardBox, same || !st.box ? tRect : aimAt(cardBox, tRect, st.box), same ? PAD + 6 : 5, pos.side) : null;
    if (!way) els.arrow.setAttribute('hidden', '');
    else {
      const pts = way.pts, tip = pts[pts.length - 1], dx = way.dir[0], dy = way.dir[1];
      const base = [tip[0] - dx * 8, tip[1] - dy * 8];
      const line = 'M' + pts.slice(0, -1).concat([[base[0] + dx, base[1] + dy]]).map((p) => half(p[0]) + ' ' + half(p[1])).join(' L');
      const head = `M${half(tip[0])} ${half(tip[1])} L${half(base[0] - dy * 5)} ${half(base[1] + dx * 5)} L${half(base[0] + dy * 5)} ${half(base[1] - dx * 5)} Z`;
      setAttr(els.line, 'd', line); setAttr(els.halo, 'd', line); setAttr(els.head, 'd', head);
      // the nudge of a do step moves the head back along the line, away from the control
      const nudge = (-dx * 4) + 'px,' + (-dy * 4) + 'px';
      if (els.arrow.__nudge !== nudge) { els.arrow.__nudge = nudge; els.arrow.style.setProperty('--nx', (-dx * 4) + 'px'); els.arrow.style.setProperty('--ny', (-dy * 4) + 'px'); }
      els.arrow.removeAttribute('hidden');
    }
    st.painted = true; firstPaint = false;
    keepFocus(step, target);
  }

  // ---------- focus ----------
  /* look and wait steps keep the keyboard on the card. A do step puts it on the control once, and
     again when the control was rebuilt and the focus fell to <body>; it is never taken from a field
     the reader is typing in. On the live list a control that spends or counts as an intervention
     (own) is never given the focus: a second Enter after Next would press it. Tab still reaches it. */
  const handsOff = (step) => LIVE_STEPS && !!step.own;
  function keepFocus(step, target) {
    const at = document.activeElement, lost = !at || at === document.body || at === document.documentElement;
    if (step.kind === 'do' && !st.leaveOpen && target && !handsOff(step)) {
      if (st.focus || (lost && target !== st.focusNode)) {
        st.focus = false; st.focusNode = target;
        if (!target.disabled && typeof target.focus === 'function') {
          target.focus({ preventScroll: true });
          if (step.selectText && typeof target.select === 'function') target.select();
        }
      }
      return;
    }
    if (st.focus || lost) { st.focus = false; focusCard(); }
  }
  function allowList(step) {
    if (!step || st.waiting || st.leaveOpen) return [];
    if (step.kind === 'do') return step.allow || [step.target];
    return step.interactive ? step.allow || [] : [];
  }
  /* A link of the reference pop-up that leads to this same page ("Open study" under Previous runs)
     would put another study in place of the tutorial's. It stays locked while the guide is on;
     links to the document viewer and to the original open in a tab of their own. */
  function samePage(node) {
    const a = node && node.closest ? node.closest('#cite-pop a[href]') : null;
    if (!a) return false;
    try { const u = new URL(a.getAttribute('href'), location.href); return u.origin === location.origin && u.pathname === location.pathname; } catch (e) { return false; }
  }
  // The bar's help cell (References, Restart tutorial, Exit tutorial) works on every step: it is the
  // way out of the guide, and styles.css lifts it above the shade for the same reason.
  const HELP = '.tb-help';
  function allowed(node) {
    if (!node || !node.closest) return false;
    if (safe(() => !!node.closest(HELP), false)) return true;
    if (samePage(node)) return false;
    const step = cur();
    for (let entry of allowList(step)) {
      if (typeof entry === 'function') entry = safe(() => entry.call(step, ctx), null);
      if (!entry) continue;
      if (typeof entry === 'string' ? safe(() => !!node.closest(entry), false) : entry.contains(node)) return true;
    }
    return false;
  }
  function tabStops(step) {
    const out = [];
    for (let entry of allowList(step)) {
      if (typeof entry === 'function') entry = safe(() => entry.call(step, ctx), null);
      if (!entry) continue;
      for (const node of typeof entry === 'string' ? safe(() => $$(entry), []) : [entry]) if (laidOut(node) && !node.disabled && node.tabIndex >= 0 && !samePage(node)) out.push(node);
    }
    return out.concat($$('button:not([disabled]), a[href]', els.card), $$(HELP + ' button:not([disabled]), ' + HELP + ' a[href]').filter(laidOut));
  }

  // ---------- guards (window, capture phase) ----------
  /* Only the card and the allowed control take the pointer. The shade panels stop the rest by
     geometry; this guard covers a hole that is larger than its control. A cancelled mousedown also
     leaves the focus and a text selection where they are. */
  function onPointer(e) {
    if (!active || bypass || st.covered) return;
    const t = e.target;
    if (!t || !t.closest) return;
    if (els.card.contains(t)) { if (e.type === 'click') soon(); return; }
    if (!root.contains(t) && allowed(t)) { used(e); if (e.type === 'click') soon(); return; }
    if (e.type === 'click' && samePage(t) && !st.linkOff) { st.linkOff = true; soon(); }     // the card says why nothing happened
    e.preventDefault(); e.stopPropagation();
  }
  function used(e) { const step = cur(); if (step && step.onUse) safe(() => step.onUse(ctx, e)); }
  const SHORTCUT = /^[\[\]v\/1-9]$/;      // the keys app.js listens for on the document
  function onKey(e) {
    if (!active || st.covered) return;
    const step = cur(), t = e.target && e.target.closest ? e.target : document.body;
    const inCard = els.card.contains(t), ok = !inCard && allowed(t);
    const editable = t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
    if (e.key === 'Escape') {
      if (ok && editable) return;                       // the plan field restores its value with Escape
      e.preventDefault(); e.stopPropagation();
      if (e.repeat) return;
      if (step.final) api.stop(); else setLeave(!st.leaveOpen);
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;     // browser shortcuts are not ours
    if (e.key === 'Tab') {
      // the keyboard moves between the allowed control and the card, nowhere else
      e.preventDefault(); e.stopPropagation();
      const stops = tabStops(step), i = stops.indexOf(document.activeElement);
      if (stops.length) stops[i < 0 ? (e.shiftKey ? stops.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + stops.length) % stops.length].focus({ preventScroll: true });
      return;
    }
    if (ok) { used(e); if (!editable && SHORTCUT.test(e.key)) e.stopPropagation(); return; }
    // the card, <body> or a control under the shade: the app must not see the key
    e.stopPropagation();
    const onButton = /^(BUTTON|A)$/.test(t.tagName);
    if (!inCard && (SHORTCUT.test(e.key) || e.key === 'Enter' || e.key === ' ')) e.preventDefault();
    if (e.repeat) { if (onButton && (e.key === 'Enter' || e.key === ' ')) e.preventDefault(); return; }   // a held key must not press a button again and again
    if (!inCard && t !== document.body) return;
    if (e.key === 'ArrowRight' || (e.key === 'Enter' && !(inCard && onButton))) { if (canNext(step)) { e.preventDefault(); userNext(); } }
    else if (e.key === 'ArrowLeft') { if (canBack(step)) { e.preventDefault(); userBack(); } }
  }
  function onFocusIn(e) {
    if (!active || bypass || st.covered) return;
    const t = e.target;
    if (!t || !t.closest || root.contains(t) || allowed(t)) return;
    focusCard();
  }
  /* A wheel turn over the shade or over a covered hole scrolls the panel of the target, so a long
     report can be read. The time is noted: a target the reader scrolled away is not pulled back. */
  function onWheel(e) {
    if (!active || st.covered) return;
    st.lastWheel = Date.now();
    const t = e.target;
    if (!t || !root.contains(t) || els.card.contains(t)) return;
    const target = resolve(cur()), host = target && scrollHost(target);
    if (!host) return;
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? host.clientHeight : 1;
    host.scrollTop += e.deltaY * unit; host.scrollLeft += e.deltaX * unit;
  }
  /* A click into the viewer iframe takes the keyboard out of this page; bring it back to the card. */
  function onBlur() {
    setTimeout(() => {
      if (!active || !document.activeElement || document.activeElement.tagName !== 'IFRAME') return;
      window.focus();
      focusCard();
    }, 0);
  }

  // ---------- persistence ----------
  const RECORD = LIVE ? 'as.tutorial.live.v1' : 'as.tutorial.v1';
  function readRecord() { try { return JSON.parse(sessionStorage.getItem(RECORD) || 'null'); } catch (e) { return null; } }
  function writeRecord(rec) { try { sessionStorage.setItem(RECORD, JSON.stringify(rec)); } catch (e) { /* private mode: a reload starts at the welcome card */ } }
  function save() {
    if (idx < 0) return;
    if (LIVE_STEPS) {
      // also while a live-only card is up: the step under it is the one a reload returns to.
      // The study is not taken from the page here: own() and a resume are the only ones to name it.
      Object.assign(R, { step: LIST[idx].id, planned: ctx.planned });
      if (state.study && state.study.id === R.study && state.study.project_id) R.project = state.study.project_id;
      writeRecord(R);
      return;
    }
    if (special) return;
    writeRecord({ step: STEPS[idx].id, study: state.study ? state.study.id : null, planned: ctx.planned, done: finished });
  }

  // ---------- live mode: the watch beside the steps ----------
  const SOFT_TIMEOUT = 90000;
  /* The answer to the tutorial's own create request (see noteAsk). The study it names becomes the
     tutorial's own: the record names it from then on, and the cost watch acts on that id alone. This
     is the only place a new study is taken, so one the reader opens or starts alone never is. */
  function own(made) {
    flying -= 1;
    const id = made && (made.id || (made.study && made.study.id));
    // nothing was created: with the guide put away meanwhile, the consent has nothing left to watch
    if (!id || !R.consent) { if (!active && !R.study && R.consent) { disarm(); writeRecord(R); } return; }
    mine = id;
    Object.assign(R, { study: id, stopSent: false, stopDone: false, mismatch: null });
    writeRecord(R);
  }
  /* That study is open now: its project and its model are read once. */
  function adopt() {
    const s = study();
    if (!mine || !s || s.id !== mine) return;
    mine = null;
    if (s.project_id) R.project = s.project_id;
    if (s.model && s.model !== R.model) {
      // Not the model of the consent card: the watch shows the trouble card. The limit can only come
      // down, and a card that promised no cost leaves nothing to spend, so that run is stopped at once.
      R.mismatch = s.model;
      R.ceiling_usd = R.model === 'scripted' ? 0 : Math.min(R.ceiling_usd == null ? Infinity : R.ceiling_usd, ceilingFor(s.model));
    }
    writeRecord(R);
  }
  /* The cost limit of the consent card, for the tutorial's own study and no other. It goes on after
     the guide was put away, for as long as this page is open: that is what the card promised. The
     Stop is owed until the study has left its active states, so it is sent again when a request
     fails, a few times, and again after a reload. */
  const STOP_TRIES = 5, STOP_EVERY = 4000, STOP_SETTLE = 30000;
  // failed: the last request did not go through. told: the reader has been told so in a toast.
  const stopper = { id: null, busy: false, tries: 0, next: 0, taken: false, failed: false, told: false, timer: 0 };
  /* The next look at an owed Stop, on a clock of its own. With the guide put away nothing else looks
     again until an event arrives, and a server that is down or a quiet solver sends none. */
  function stopLater(ms) {
    clearTimeout(stopper.timer);
    stopper.timer = setTimeout(() => { stopper.timer = 0; costWatch(); tick(); }, Math.max(0, ms));
  }
  function costWatch() {
    const s = study();
    if (!s || !R.consent || !R.study || R.study !== s.id) return;
    watched = ACTIVE.includes(s.status) && !R.stopDone && AS.isNum(R.ceiling_usd);
    if (R.stopDone) return;
    if (!ACTIVE.includes(s.status)) {
      // no agent is at work any more: the Stop is settled, and what the reader starts in this study later is theirs
      if (R.stopSent) { R.stopDone = true; writeRecord(R); }
      return;
    }
    if (!R.stopSent) {
      if (!AS.isNum(R.ceiling_usd) || ctx.cost() < R.ceiling_usd) return;
      R.stopSent = true;
      writeRecord(R);
      if (!active) AS.toast(`The cost reached ${AS.fmtCost(ctx.cost())}, the limit of this tutorial (${money(R.ceiling_usd)} USD). Stopping the run.`, 'warn');
    }
    requestStop(false);
  }
  /* The watch reads the cost from the open study. When the tutorial's study is closed or another one
     takes its place while it is at work, the limit can no longer be kept, and the page says so once. */
  let watched = false;       // the tutorial's study was open, at work and under a limit at the last look
  function noticeGone() {
    const s = study();
    if (!LIVE || !watched || (s && s.id === R.study)) return;
    watched = false;
    AS.toast('The tutorial study is not open any more, so this page no longer watches its cost limit. The study keeps running on the server.', 'warn');
  }
  /* One Stop for the tutorial's study. The request names the study, so it cannot reach another one
     that is open by then. byReader: the Stop the run button of the trouble card, which is not counted. */
  async function requestStop(byReader) {
    const id = R.study;
    if (!id) return;
    if (stopper.id !== id) Object.assign(stopper, { id, tries: 0, next: 0, taken: false, failed: false, told: false });
    if (stopper.busy || (!byReader && (stopper.tries >= STOP_TRIES || Date.now() < stopper.next))) return;
    stopper.busy = true;
    if (!byReader) stopper.tries += 1;
    let ok = true;
    try { await AS.api.post(`/api/studies/${id}/control`, { action: 'stop' }); } catch (e) { ok = false; }
    stopper.busy = false;
    stopper.failed = !ok;
    if (ok) { stopper.taken = true; stopper.told = false; }
    // a request the server took needs time to end the model call in flight; a failed one is sent again soon
    const wait = ok ? STOP_SETTLE : STOP_EVERY;
    stopper.next = Date.now() + wait;
    stopLater(wait + 50);      // a timer that fires a moment early would find the wait not over and end the chain
    // without the card a toast says it: at the first failure that another try follows, at the last one what to do
    if (!ok && !active) {
      if (stopper.tries >= STOP_TRIES) AS.toast('The stop request did not go through. Press Stop in the bar.', 'error');
      else if (!stopper.told) { stopper.told = true; AS.toast('The stop request did not go through. It is being sent again.', 'warn'); }
    }
    tick();
  }
  /* Where the Stop of the cost limit stands, read from the study and not from the request. */
  function stopState() {
    const s = study() || {};
    if (ACTIVE.includes(s.status)) {
      if (R.stopDone) return 'again';
      if (stopper.busy) return 'stopping';
      // no try is left: a request that failed is known at once, one the server took is given time to act
      if (stopper.tries >= STOP_TRIES) return stopper.failed || Date.now() >= stopper.next ? 'failed' : 'stopping';
      return stopper.failed ? 'retrying' : 'stopping';
    }
    if (s.status === 'stopped' || AS.caseList().some((x) => x.status === 'stopped')) return 'stopped';
    return ENDED.includes(s.status) ? 'ended' : 'idle';      // idle: it waits for a selection or an approval
  }
  /* The guide is put away or left before a study of its own exists: the consent has nothing to
     watch, and a study the reader starts alone afterwards is theirs. A create request of the
     tutorial that is still without its answer keeps the consent, so that study is watched. */
  function disarm() { if (!R.study && !flying) R.consent = null; }
  /* An end that cannot be continued, in the order the card kinds are judged. A failed stage is not one of them. */
  function troubleKind(s) {
    if (R.mismatch && R.study === s.id) return 'model';
    if (R.stopSent && R.study === s.id) return 'cost';
    const f = facts();
    if (s.status === 'draft' && f.error) return 'advisor';
    if (s.status === 'awaiting_selection' && f.error && f.planningSeq && f.error.seq > f.planningSeq && f.error.seq > (R.ackSeq || 0)) return 'planner';
    // nothing can be ticked on the Suggestions page: the server takes only sections XFoil converged for
    if (s.status === 'awaiting_selection' && (s.candidates || []).length > 0 && !s.candidates.some(screened)) return 'no_candidate';
    if (f.completed && (f.completed.data || {}).report == null && !ACTIVE.includes(s.status)) return 'no_report';
    // also before the plan was approved (a server restart while the advisor or the planner was at work)
    if (s.status === 'stopped' || s.status === 'failed') return 'stopped';
    return '';
  }
  /* Run on every pass. The trouble card replaces the open step; an interrupt is shown over it and the
     open step is entered again (its set-up run again) when the interrupt is done. */
  function watch() {
    const s = study(), open = special || LIST[idx];
    if (!s || !open || st.leaveOpen || open.system || open.id === 'trouble' || open.id === 'welcome') return;
    const kind = troubleKind(s);
    if (kind) { raise(TROUBLE, { kind }); return; }
    if (open.interrupt || open.final || !(s.plan && s.plan.approved_at)) return;
    for (const step of INTERRUPTS) {
      const c = safe(() => step.when(ctx), null);
      if (c) { raise(step, { caseId: c.id }); return; }
    }
  }
  function raise(step, data) {
    if (!special) { resumeAt = idx; leaveCurrent('push'); }
    special = step;
    resetStep();
    Object.assign(ctx.local, data);
    const now = cur();
    st.waiting = !isReady(now);
    runSetup(now);
    save();
    announce(now);
  }
  function detour(step) { raise(step, {}); tick(); }
  /* A card that is not a step of the run. */
  function showCard(card) {
    active = true;
    mount();
    special = card;
    resetStep();
    announce(cur());
    tick();
  }
  /* The study is on the server and goes on without this page: ask before leaving it behind. */
  function askLeave() {
    if (active && root && !(special && special.system)) setLeave(true);
    else showCard(LEAVING_CARD);
  }

  // ---------- live mode: start and resume ----------
  /* After a load the events are replayed from the first one, in one burst. The branch of a live step
     is read from them, so a step is entered only when the replay has arrived. */
  const replay = { since: Date.now(), seen: 0, last: 0, live: false };
  if (LIVE_STEPS) {
    // the check waits a moment: a study that is opened again is closed first and back at once
    AS.on('reset', () => { replay.since = Date.now(); replay.seen = 0; replay.last = 0; replay.live = false; setTimeout(noticeGone, 0); });
    AS.on('event', (ev, historical) => { if (historical) { replay.seen += 1; replay.last = Date.now(); } else replay.live = true; adopt(); costWatch(); });
  }
  const replayed = () => until(() => replay.live || (replay.seen > 0 && Date.now() - replay.last > 400) || Date.now() - replay.since > 2500, 6000);
  /* The example of the live run goes first on the Mission page (the chips are redrawn when the list changes). */
  function injectPreset() {
    const cfg = state.config;
    if (!cfg || (cfg.presets || [])[0] === LIVE_PRESET) return;
    cfg.presets = [LIVE_PRESET].concat((cfg.presets || []).filter((p) => p && p.id !== 'tutorial'));
    AS.invalidate();
  }
  /* The Start button of the consent card. The first request it may cause is the creation of the
     Tutorial project; the study itself is created by the reader's own click on Suggest airfoils. */
  async function startLive() {
    if (ctx.local.busy) return;
    ctx.local.busy = true;
    tick();
    R = freshRecord(PAGE_MODEL);
    R.consent = { ts: Date.now() };
    writeRecord(R);
    mem.model = R.model;
    if (!TEST) {
      const named = () => state.projects.find((p) => p.name === LIVE_RUN.project.name) || null;
      let p = named();
      if (!p) { const made = await AS.actions.createProject(LIVE_RUN.project.name, LIVE_RUN.project.description); p = (made && made.id && made) || named(); }
      if (!active || special || (LIST[idx] || {}).id !== 'welcome') return;      // left meanwhile
      if (p) AS.setProject(p.id);       // the write stays in memory: the owner's stored project is not changed
      else { const now = AS.project(); projectNote = now ? `The study is kept in project ${now.name}.` : ''; }
      R.project = state.projectId || null;
    }
    injectPreset();
    go(idx + 1);
  }
  /* A tutorial study that is still at work in the Tutorial project: it is offered instead of a new run. */
  async function findRunning() {
    if (TEST) return null;
    const p = state.projects.find((x) => x.name === LIVE_RUN.project.name);
    if (!p) return null;
    try {
      const resp = await AS.api.get(`/api/projects/${p.id}/studies`);
      return ((resp && resp.studies) || []).find((x) => /^Tutorial:/.test(x.title || '') && ACTIVE.includes(x.status)) || null;
    } catch (e) { return null; }
  }
  /* The step a study is taken up at. Every live step copes with a run that is ahead of it. */
  function resumeStep() {
    const s = study() || {};
    const at = new Map([['draft', 'suggest'], ['suggesting', 'advisor'], ['awaiting_selection', 'candidates'], ['planning', 'plan']]).get(s.status);
    if (at) return at;
    // only a value that fails the gate carries the story: anything else goes back to the plan
    if (s.status === 'awaiting_approval') return tooTall() ? 'set-by-engineer' : 'plan';
    // running, paused or done: the stored step when it lies in the run. Stopped or failed: the watch shows the trouble card
    return stepIndex(R.step) >= stepIndex('tabs') ? R.step : 'tabs';
  }
  async function resumeRun() {
    const want = ctx.resume;
    if (!want || ctx.local.busy) return;
    ctx.local.busy = true; ctx.local.failed = false;
    tick();
    await AS.openStudy(want.id);
    await until(() => !state.loading, 8000);
    if (!active || special || (LIST[idx] || {}).id !== 'welcome') return;
    if (!(state.study && state.study.id === want.id)) { ctx.local.busy = false; ctx.local.failed = true; tick(); return; }
    R = freshRecord(state.study.model || PAGE_MODEL);
    Object.assign(R, { consent: { ts: Date.now(), resumed: true }, study: want.id, project: state.study.project_id || null });
    mem.model = R.model;
    await replayed();
    ctx.resume = null;
    go(stepIndex(resumeStep()));
  }

  // ---------- public API ----------
  const api = (AS.tutorial = {
    enabled: true,
    mode: LIVE ? 'live' : 'recorded',
    /* true when the run on this page spends money: live mode with a model that is not the scripted one */
    get paid() { return ctx.paid; },
    get active() { return active; },
    steps: LIST,
    start(stepId) {
      const i = LIST.findIndex((s) => s.id === stepId);
      if (active) leaveCurrent();
      finished = false; active = true;
      mount();
      enter(i < 0 ? 0 : i);
      tick();
    },
    next() { if (active && !special) go(idx + 1); },
    back() { if (active && !special) go(idx - 1); },
    goto(id) { const i = LIST.findIndex((s) => s.id === id); if (active && i >= 0) go(i); },
    /* Remove the guide and stay in the recorded study ("Explore this study"). In a live run the study
       stays open, and running if it was. */
    stop() {
      if (active) leaveCurrent();
      active = false; special = null;
      unmount();
      // a reload must not bring the guide back; the application bar offers Restart tutorial
      finished = true;
      if (LIVE_STEPS) {
        disarm();
        // the watch goes on without the card, so it starts its count of Stop requests again
        stopper.tries = 0; stopper.told = false;
        R.done = true; writeRecord(R);
        // and looks at once: a Stop that is owed must not wait for the next event
        costWatch();
      } else {
        mock.release();
        writeRecord(Object.assign({ step: null, study: null, planned: null }, readRecord(), { done: true }));
      }
      AS.invalidate();
    },
    /* Leave tutorial mode for the page the reader came from (written by the entry links). A live run
       is on the server: while its study is at work the Leave row asks first, and exit(true) leaves
       without touching it. */
    exit(force) {
      if (LIVE_STEPS) {
        if (force !== true && studyActive()) { askLeave(); return; }
        disarm();
        R.done = true;
        if (LIVE) writeRecord(R);
      } else mock.release();
      let back = '';
      // the address is used once: a later visit that was typed by hand must not leave for a stale page
      try {
        if (!LIVE) { sessionStorage.removeItem(RECORD); sessionStorage.removeItem('as.mock.tutorial.v1'); }
        back = sessionStorage.getItem('as.tutorial.return') || ''; sessionStorage.removeItem('as.tutorial.return');
      } catch (e) { /* private mode */ }
      location.assign(location.pathname + (/^[?#]/.test(back) ? back : ''));
    },
    /* In a live run this only returns to the consent card (or to the offer to resume): it starts nothing by itself. */
    restart() {
      if (LIVE) {
        R.done = true; writeRecord(R);
        const keep = (name) => (query.has(name) ? '&' + name + '=' + encodeURIComponent(query.get(name)) : '');
        location.assign(location.pathname + '?tutorial=live' + keep('model') + keep('ceiling'));
        return;
      }
      location.assign(location.pathname + '?tutorial=reset' + (TEST ? '&steps=live' : ''));
    },
    selfTest,
  });

  // ---------- boot ----------
  function showReloaded() {
    active = true; idx = -1;
    mount();
    special = RELOADED;
    resetStep();
    announce(RELOADED);
    tick();
  }
  function begin() {
    const rec = readRecord(), s = state.study;
    // Finished, or left to explore the study: a reload does not bring the guide back (the bar offers Restart).
    // Coming in again through a Tutorial link is a new visit and starts it from the top.
    const nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    if (rec && rec.done && nav.type !== 'navigate') { finished = true; return; }
    if (rec && rec.done) { api.start('welcome'); return; }
    if (!rec || !s || rec.study !== s.id) { api.start('welcome'); return; }
    if (AS.isNum(rec.planned)) ctx.planned = rec.planned;
    const at = { suggesting: 'advisor', awaiting_selection: 'candidates', planning: 'plan', done: 'validation' }[s.status];
    if (at) api.start(at);
    else if (s.status === 'awaiting_approval') api.start(engineerSet() && predicted(typedHeight()) > 200 ? 'set-by-engineer' : 'plan');
    else if (s.status === 'running' || s.status === 'paused') showReloaded();   // the checkpoints cannot be rebuilt
    else api.start('welcome');
  }
  /* The boot of the live step list. A record with a study is taken up where the study stands, at no
     new cost; anything else starts at the consent card. */
  async function beginLive() {
    if (BLOCKED) { showCard(BLOCKED_CARD); return; }
    await until(() => !!state.config && !state.loading, 4000);
    if (!state.config) {
      showCard(NO_SERVER_CARD);
      // a slow server may still answer; a server that is down needs the reload the card asks for
      if (!(await until(() => !!state.config && !state.loading, 26000)) || special !== NO_SERVER_CARD) return;
      active = false; special = null;
      unmount();
    }
    // app.js opens the study named in the address after the projects have loaded: wait for it
    const named = (/(?:^#|&)study=([\w.-]+)/.exec(location.hash) || [])[1];
    await until(() => !state.loading && state.projects.length > 0 && (!named || (!!state.study && state.study.id === named)), 4000);
    const rec = readRecord(), nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    if (rec && rec.done) {
      // finished, or the guide was put away: a reload does not bring it back, but the cost watch goes on
      if (nav.type !== 'navigate') { R = Object.assign(freshRecord(rec.model || PAGE_MODEL), rec); finished = true; return; }
    } else if (rec && rec.study) {
      if (!(state.study && state.study.id === rec.study)) { await AS.openStudy(rec.study); await until(() => !state.loading, 8000); }
      if (state.study && state.study.id === rec.study) {
        R = Object.assign(freshRecord(rec.model || state.study.model || PAGE_MODEL), rec);
        mem.model = state.study.model || R.model;
        if (AS.isNum(R.planned)) ctx.planned = R.planned;
        await replayed();
        api.start(resumeStep());
        return;
      }
      // the study cannot be opened: the record is dropped
    }
    R = freshRecord(PAGE_MODEL);
    ctx.resume = await findRunning();
    api.start('welcome');
  }
  (function boot() {
    if (LIVE_STEPS) { beginLive(); return; }
    const t0 = Date.now();
    const poll = setInterval(() => {
      const late = Date.now() - t0 > 4000;
      if (!state.config || state.loading) { if (Date.now() - t0 > 30000) clearInterval(poll); return; }
      // app.js opens the study named in the address after the projects have loaded: wait for it
      const named = (/(?:^#|&)study=([\w.-]+)/.exec(location.hash) || [])[1];
      const rec = readRecord();
      const opening = !!named && !(state.study && state.study.id === named);   // with no record too: the welcome step closes it once it is open
      if ((opening || !state.projects.length) && !late) return;
      clearInterval(poll);
      begin();
    }, 60);
  })();
  AS.on('render', tick);       // registered after app.js: the page has been drawn when this runs
  AS.on('event', tick);
  AS.on('reset', soon);
  AS.invalidate();

  // ---------- self test ----------
  /* Plays every step with its auto() and reports selectors that did not resolve and conditions that
     were not reached. Run it from the console on ?tutorial=reset&speed=10 after any markup change
     (add &steps=live for the live step list on the mock). In live mode it presses the controls that
     spend, so it is refused unless the call says allowPaid or the run it would drive is scripted.
     That is judged by the model a press would spend with, not by the study that happens to be open:
     a test that starts at the consent card closes the open study and runs with the model of the
     address (or of the run it resumes). It is judged again before every press. */
  async function selfTest(opts) {
    const allowPaid = !!(opts && opts.allowPaid === true);
    const spends = (fresh) => {
      if (!LIVE || allowPaid) return false;
      const atStart = fresh || (!special && (LIST[idx] || {}).id === 'welcome');
      return (atStart ? (ctx.resume ? ctx.resume.model : PAGE_MODEL) : ctx.model) !== 'scripted';
    };
    if (spends(!active || !!special)) throw new Error('live mode: selfTest needs the scripted model');
    const report = { ok: false, missing: [], unresolved: [], timedOut: [], steps: [] };
    if (!LIVE) {
      for (const name of ['hold', 'release', 'holding', 'yplus']) if (typeof AS.mock[name] !== 'function') report.missing.push('AS.mock.' + name);
      if (!(((state.config || {}).presets) || []).some((p) => p && p.id === 'tutorial')) report.missing.push("preset 'tutorial' in /api/config");
    }
    // a real run is slow: the limits of the recorded list would report waits that are only long
    const readyMs = LIVE ? 300000 : 45000, doneMs = LIVE ? 600000 : 60000;
    testing = true;
    try {
      if (!active || special) api.start('welcome');
      for (let guard = 0; guard < TOTAL * 3 && active; guard++) {
        // the run turned out to be a paid one (a resumed study, a model the server chose): no press is made
        if (spends(false)) { report.refused = cur().id; break; }
        if (special) {
          // a live-only card: an interrupt is answered with its own control, anything else ends the test
          const card = special, now = cur(), row = { id: now.id, kind: now.kind, special: true, advanced: true };
          report.steps.push(row);
          if (!now.auto || !now.done) { report.ended = now.id + (ctx.local.kind ? ' (' + ctx.local.kind + ')' : ''); break; }
          await until(() => special !== card || !!resolve(cur()), 8000);
          if (special === card) { try { await now.auto(ctx); } catch (e) { row.error = String((e && e.message) || e); } }
          row.advanced = await until(() => special !== card, doneMs);
          if (!row.advanced) { report.timedOut.push(now.id + ' (done)'); break; }
          await sleep(30);
          continue;
        }
        const i = idx, step = cur(), row = { id: step.id, kind: step.kind, ready: true, resolved: true, advanced: true };
        const moved = () => idx !== i || !!special;
        report.steps.push(row);
        row.ready = await until(() => moved() || !st.waiting, readyMs);
        if (!row.ready) report.timedOut.push(step.id + ' (ready)');
        if (!moved() && step.target && step.place !== 'centre') {
          row.resolved = await until(() => moved() || !!resolve(cur()), 8000);
          if (!row.resolved) report.unresolved.push(step.id + ': ' + (typeof step.target === 'string' ? step.target : String(safe(() => step.target.call(step, ctx), '') || 'computed target')));
        }
        if (i === TOTAL - 1) break;
        if (moved()) continue;
        const now = cur();                     // the variant may have changed while the step was waiting
        if (now.kind === 'look') {
          // a card without Next is left by its first button (the consent card of the live list)
          const first = now.noNext ? actionsOf(now).find((a) => a.primary && !a.disabled) : null;
          if (first) { try { await first.run(); } catch (e) { row.error = String((e && e.message) || e); } if (!moved()) { row.advanced = await until(moved, 15000); if (!row.advanced) { report.timedOut.push(step.id + ' (done)'); go(i + 1); } } }
          else go(i + 1);
          await sleep(30);
          continue;
        }
        if (now.kind === 'do' && now.auto) { try { await now.auto(ctx); } catch (e) { row.error = String((e && e.message) || e); } }
        row.advanced = await until(moved, LIVE_STEPS ? doneMs : step.timeout || 60000);
        if (!row.advanced) { report.timedOut.push(step.id + ' (done)'); go(i + 1); }
        await sleep(30);
      }
    } finally { testing = false; }
    report.ok = !report.missing.length && !report.unresolved.length && !report.timedOut.length && !report.refused;
    return report;
  }
})();
