/* Wing Studio - client state, the event reducer and the actions that call
   the API. Views read AS.state and call AS.invalidate() to be redrawn. */
(function () {
  'use strict';
  const AS = window.AS;

  AS.STAGE_DEFS = [
    { key: 'geometry', title: 'Geometry', agent: 'geometry.md' },
    { key: 'mesh', title: 'Mesh', agent: 'mesher.md' },
    { key: 'physics', title: 'Boundary conditions and physics', agent: 'boundary-conditions.md' },
    { key: 'numerics', title: 'Solver setup', agent: 'solver-setup.md' },
    { key: 'solve', title: 'Solve', agent: 'run-monitor.md' },
    { key: 'post', title: 'Post-process', agent: 'post-processor.md' },
    { key: 'validate', title: 'Validate against XFoil', agent: 'xfoil-validator.md' },
  ];
  AS.STEPS = [
    { key: 'mission', title: 'Mission' },
    { key: 'suggest', title: 'Airfoil suggestion' },
    { key: 'plan', title: 'Plan' },
  ];
  /* 'plan' is the state before the plan is approved; POST /mode accepts only gate and auto. */
  AS.MODES = [
    { key: 'plan', label: 'Plan only', hint: 'Agents propose; nothing runs until the plan is approved' },
    { key: 'gate', label: 'Gate each stage', hint: 'Pause before every stage for approval' },
    { key: 'auto', label: 'Auto-run', hint: 'Run through; ask only when a gate keeps failing' },
  ];

  const state = (AS.state = {
    config: null,
    projects: [],
    projectId: AS.store.get('project', null),
    studies: [],                 // summaries for the selected project
    study: null,
    cases: {},                   // id -> Case
    events: [],                  // every event of the open study, in order
    activity: {},                // case id (or '_study') -> what is happening right now
    series: {},                  // case id -> chart data
    pending: [],                 // human messages posted but not yet delivered to an agent
    timing: { start: null, end: null },
    conn: 'idle',
    lastSeq: 0,
    loading: false,
    ui: {
      tab: 'main',               // main | compare | dashboard
      caseId: null,
      sel: { kind: 'step', key: 'mission' },
      follow: true,              // keep the selection on the live page: the active stage, or the setup step before cases exist
      railCollapsed: AS.store.get('rail', false),
      chatCollapsed: AS.store.get('chat', false),
      viewerCollapsed: {},       // manual choice per stage key
      chatScope: 'case',         // stage | case | all
      chatVerbose: false,
    },
  });

  // ---------- render scheduling ----------
  const listeners = { event: [], reset: [], render: [] };
  AS.on = (name, fn) => listeners[name].push(fn);
  function fire(name, ...args) { for (const fn of listeners[name]) fn(...args); }

  let frame = 0, fallback = 0;
  AS.invalidate = function () {
    if (frame) return;
    const run = () => { cancelAnimationFrame(frame); clearTimeout(fallback); frame = 0; fallback = 0; fire('render'); };
    frame = requestAnimationFrame(run);
    // Animation frames stop while the window is covered or in the background (and are throttled in an
    // embedded pane); a click on a tab or a stage must still change the page at once.
    fallback = setTimeout(run, 50);
  };

  // ---------- selectors ----------
  AS.stageDefs = () => (state.config && Array.isArray(state.config.stage_defs) && state.config.stage_defs.length ? state.config.stage_defs : AS.STAGE_DEFS);
  AS.stageTitle = function (key) {
    const def = AS.stageDefs().find((d) => d.key === key) || AS.STEPS.find((d) => d.key === key);
    return def ? def.title : key === 'report' ? 'Report' : AS.humanKey(key || '');
  };
  AS.caseList = function () {
    const ids = (state.study && state.study.case_ids) || [];
    const ordered = ids.map((id) => state.cases[id]).filter(Boolean);
    for (const id in state.cases) if (!ids.includes(id)) ordered.push(state.cases[id]);
    return ordered;
  };
  AS.currentCase = () => state.cases[state.ui.caseId] || null;
  AS.stageOf = (c, key) => (c && c.stages ? c.stages.find((st) => st.key === key) : null) || null;
  AS.caseLabel = (c) => (c ? 'NACA ' + c.naca : '');
  AS.passedCount = (c) => (c && c.stages ? c.stages.filter((st) => st.status === 'passed').length : 0);
  AS.isActiveStatus = (status) => ['suggesting', 'planning', 'running'].includes(status);
  AS.project = () => state.projects.find((p) => p.id === state.projectId) || null;

  /* Which setup step a study status belongs to. */
  function stepForStatus(study) {
    const s = study.status;
    if (s === 'draft') return 'mission';
    if (s === 'suggesting' || s === 'awaiting_selection') return 'suggest';
    if (s === 'planning' || s === 'awaiting_approval') return 'plan';
    // failed/stopped before any case existed: show the last step that has content
    if (study.plan) return 'plan';
    if (study.candidates && study.candidates.length) return 'suggest';
    return 'mission';
  }
  AS.stepForStatus = stepForStatus;   // the Study tab opens the same step (views_workspace.js)
  /* The stage workspace exists once the plan is approved and its cases are created; until then the
     study is on one of the setup steps. */
  function inWorkspace(study) {
    return AS.caseList().length > 0 && ['running', 'paused', 'done', 'failed', 'stopped'].includes(study.status);
  }
  const caseLive = (c) => !!c && (c.status === 'running' || c.status === 'awaiting_approval' || c.status === 'paused');

  // ---------- the reducer (also used by the mock server to keep its snapshot) ----------
  /* Engineer actions that count as help (store.HELP_KINDS on the server, which also lists
     retry_approval: that one arrives as a gate_decision with `retry` true). The reducer counts
     them for the page and for the mock alike; tests/test_metrics.py compares the two lists. */
  AS.HELP_KINDS = ['steer', 'rerun', 'plan_edit'];
  function ensureUsage(o) {
    o.usage = Object.assign({ llm_calls: 0, tool_calls: 0, tokens_in: 0, tokens_out: 0, tokens_cached: 0, cost_usd: 0 }, o.usage || {});
    return o.usage;
  }

  AS.applyEvent = function (bundle, ev) {
    const study = bundle.study, d = ev.data || {};
    if (!study) return;
    const c = ev.case_id ? bundle.cases[ev.case_id] : null;
    const stageKey = ev.stage || d.stage;
    const st = c && stageKey ? AS.stageOf(c, stageKey) : null;
    switch (ev.type) {
      case 'study.status': {
        // a study that had ended and runs again (a stage was re-run) has no report until it ends again
        if (d.status === 'running' && ['done', 'failed', 'stopped'].includes(study.status)) study.report = null;
        study.status = d.status;
        if (d.mode) study.mode = d.mode;
        break;
      }
      case 'requirements.parsed':
        if (d.requirements) study.requirements = d.requirements;
        if (d.conditions) study.conditions = d.conditions;
        break;
      case 'candidates.proposed':
        study.candidates = d.candidates || [];
        break;
      case 'plan.created':
      case 'plan.updated':
        if (study.plan && d.version != null) study.plan.version = d.version;   // content arrives with the next snapshot
        break;
      case 'plan.approved':
        if (study.plan) study.plan.approved_at = ev.ts;
        if (d.mode) study.mode = d.mode;
        break;
      case 'case.created':
        if (d.case && d.case.id) {
          bundle.cases[d.case.id] = d.case;
          study.case_ids = study.case_ids || [];
          if (!study.case_ids.includes(d.case.id)) study.case_ids.push(d.case.id);
        }
        break;
      case 'case.status':
        if (c) {
          c.status = d.status;
          // a stop closes the stage in flight without a stage event: what had started is stale,
          // what was only waiting for its first approval is pending again (same rule as the backend)
          if (d.status === 'stopped') for (const x of c.stages) if (x.status === 'running' || x.status === 'awaiting_approval') { x.status = x.attempts ? 'stale' : 'pending'; x.phase = null; }
        }
        break;
      case 'stage.started':
        if (st) {
          st.status = 'running'; st.attempts = d.attempt || (st.attempts || 0) + 1;
          st.started_at = ev.ts; st.ended_at = null; st.failure = null; st.gate = null;
          c.current_stage = st.key;
          if (c.status === 'pending' || c.status === 'awaiting_approval') c.status = 'running';
        }
        break;
      case 'stage.phase':
        if (st) st.phase = d.phase;
        break;
      case 'gate.result':
        if (st) st.gate = { passed: !!d.passed, checks: d.checks || [] };
        if (c && !d.passed) { c.counters = c.counters || {}; c.counters.gate_failures = (c.counters.gate_failures || 0) + 1; }
        break;
      case 'stage.completed':
        if (st) {
          st.status = 'passed'; st.phase = null; st.ended_at = ev.ts;
          for (const k of ['summary', 'headline', 'decisions', 'metrics', 'seconds']) if (d[k] !== undefined) st[k] = d[k];
        }
        break;
      case 'stage.failed':
        if (st) { st.status = 'failed'; st.phase = null; st.ended_at = ev.ts; st.failure = d.reason; }
        break;
      case 'stage.rework':
        if (c) {
          // everything from the target stage onwards has to be redone
          const from = c.stages.findIndex((x) => x.key === d.to);
          // (a stage that never started, because it was only waiting for approval, has nothing to redo and is pending again)
          const neverStarted = (x) => !x.attempts && !['running', 'passed', 'failed'].includes(x.status);
          if (from >= 0) for (let i = from; i < c.stages.length; i++) if (c.stages[i].status !== 'pending') { c.stages[i].status = neverStarted(c.stages[i]) ? 'pending' : 'stale'; c.stages[i].phase = null; }
          // the results of the earlier run no longer describe this case (same rule as the backend's rewind)
          c.verdict = null; c.deltas = null; c.reference = null;
          const index = (key) => c.stages.findIndex((x) => x.key === key);
          if (from >= 0 && from <= index('post')) { const cells = (c.results || {}).cells; c.results = cells && from > index('mesh') ? { cells } : null; }
          // the backend counts a rework loop only for an agent's request_rework; a re-run asked
          // by the engineer is a human intervention (counted from human.intervention)
          if (d.by !== 'human') { c.counters = c.counters || {}; c.counters.rework_loops = (c.counters.rework_loops || 0) + 1; }
        }
        break;
      case 'approval.requested':
        if (c) { c.status = 'awaiting_approval'; if (stageKey) c.current_stage = stageKey; }
        if (st && st.status !== 'failed') st.status = 'awaiting_approval';
        break;
      case 'approval.granted':
        if (c && c.status === 'awaiting_approval') c.status = 'running';
        if (st && st.status === 'awaiting_approval') st.status = 'pending';
        break;
      case 'artifact.created':
        if (st) {
          st.artifacts = st.artifacts || [];
          if (!st.artifacts.some((a) => a.url === d.url)) st.artifacts.push({ kind: d.kind, label: d.label, url: d.url });
        }
        break;
      case 'llm.call': {
        for (const owner of [study, c]) {
          if (!owner) continue;
          const u = ensureUsage(owner);
          u.llm_calls += 1;
          u.tokens_in += d.tokens_in || 0; u.tokens_out += d.tokens_out || 0;
          u.tokens_cached += d.tokens_cached || 0; u.cost_usd += d.cost_usd || 0;
        }
        break;
      }
      case 'tool.call':
        if (c) ensureUsage(c).tool_calls += 1;
        break;
      case 'human.intervention': {
        // The event lists every case the engine counted the action for (case_ids): a pause, a mode
        // change or a plan edit names several and has no case_id. An event from before that field
        // names at most one case.
        const ids = Array.isArray(d.case_ids) ? d.case_ids : (ev.case_id ? [ev.case_id] : []);
        const helped = AS.HELP_KINDS.includes(d.kind) || d.retry === true;
        for (const id of ids) {
          const k = bundle.cases[id];
          if (!k) continue;
          k.counters = k.counters || {};
          k.counters.human_interventions = (k.counters.human_interventions || 0) + 1;
          // a case record without the help count (older than it) stays without one, as on the server
          if (helped && AS.isNum(k.counters.help_interventions)) k.counters.help_interventions += 1;
        }
        break;
      }
      case 'case.completed':
        if (c) {
          for (const k of ['results', 'reference', 'deltas', 'verdict']) if (d[k] !== undefined) c[k] = d[k];
          if (c.status === 'running') c.status = 'done';
        }
        break;
      case 'study.completed':
        study.report = d.report || null;      // always emitted when the pipelines end; null when no case finished
        break;
      default:
        break;
    }
  };

  // ---------- activity: "what is each agent doing right now" ----------
  function trackActivity(ev) {
    const key = ev.case_id || '_study';
    const a = (state.activity[key] = state.activity[key] || { tool: null, toolSince: null, lastTool: null, iteration: null, total: null, lastTs: null, agent: null });
    const d = ev.data || {};
    a.lastTs = ev.ts;
    if (ev.agent) a.agent = ev.agent;
    switch (ev.type) {
      case 'tool.call': a.tool = d.name; a.toolSince = ev.ts; a.thinking = false; break;
      case 'tool.result': a.lastTool = d.name; a.lastSummary = d.summary; a.tool = null; a.thinking = true; break;
      case 'llm.call': a.thinking = false; break;
      case 'agent.message': a.thinking = true; break;
      case 'solver.progress': a.iteration = d.iteration; a.total = d.total; a.cl = d.cl; a.cd = d.cd; a.residuals = d.residuals; break;
      case 'stage.started': a.tool = null; a.thinking = true; if (ev.stage === 'solve') { a.iteration = null; a.total = null; } break;
      case 'stage.completed': case 'stage.failed': case 'approval.requested': a.tool = null; a.thinking = false; break;
      case 'case.completed': a.tool = null; a.thinking = false; break;
      case 'study.status': if (!AS.isActiveStatus(d.status)) { a.tool = null; a.thinking = false; } break;
      default: break;
    }
    // run clock: from plan approval to the end of the study
    if (ev.type === 'plan.approved' && !state.timing.start) state.timing.start = ev.ts;
    if (ev.type === 'study.status') {
      if (d.status === 'running' && !state.timing.start) state.timing.start = ev.ts;
      state.timing.end = ['done', 'failed', 'stopped'].includes(d.status) ? ev.ts : null;
    }
  }

  // ---------- chart series ----------
  function emptySeries() { return { fields: [], res: [], forces: [], cp: null, xfoilCp: null, geometry: null, loaded: false }; }

  function normaliseSeries(raw) {
    const out = emptySeries();
    out.loaded = true;
    const r = (raw && raw.residuals) || {};
    const rows = Array.isArray(r.series) ? r.series : [];
    if (rows.length && (Array.isArray(rows[0].values) || Array.isArray(rows[0].data))) {
      // columnar form: residuals.iterations [..] + series [{name, values [..]}]
      const iters = Array.isArray(r.iterations) ? r.iterations : rows[0].values.map((_, i) => i + 1);
      out.res = iters.map((it, i) => {
        const row = { iter: it };
        for (const col of rows) row[col.name || col.field] = (col.values || col.data)[i];
        return row;
      });
    } else {
      out.res = rows.map((row) => {
        const o = Object.assign({}, row);
        o.iter = row.iter != null ? row.iter : row.iteration != null ? row.iteration : row.time;
        delete o.iteration; delete o.time;
        return o;
      }).filter((o) => AS.isNum(o.iter));
    }
    const fields = new Set();
    for (const row of out.res) for (const k in row) if (k !== 'iter' && AS.isNum(row[k])) fields.add(k);
    out.fields = Array.from(fields);
    out.forces = (Array.isArray(raw && raw.forces) ? raw.forces : []).map((f) => ({
      iter: f.iter != null ? f.iter : f.iteration,
      cl: f.cl != null ? f.cl : f.Cl, cd: f.cd != null ? f.cd : f.Cd, cm: f.cm != null ? f.cm : f.Cm,
    })).filter((f) => AS.isNum(f.iter));
    const pair = (v) => (v && (v.upper || v.lower) ? { upper: AS.toXY(v.upper), lower: AS.toXY(v.lower) } : null);
    out.cp = pair(raw && raw.cp);
    out.xfoilCp = pair(raw && raw.xfoil_cp);
    out.geometry = pair(raw && raw.geometry);
    return out;
  }

  function thin(rows) {
    // keep live arrays bounded: drop every other row of the older half
    if (rows.length <= 2400) return rows;
    const half = rows.length >> 1;
    return rows.slice(0, half).filter((_, i) => i % 2 === 0).concat(rows.slice(half));
  }

  function trackSeries(ev) {
    if (ev.type !== 'solver.progress' || !ev.case_id) return;
    const d = ev.data || {};
    if (!AS.isNum(d.iteration)) return;
    const sr = (state.series[ev.case_id] = state.series[ev.case_id] || emptySeries());
    const lastRes = sr.res.length ? sr.res[sr.res.length - 1].iter : -Infinity;
    if (d.iteration < lastRes - 1 && d.iteration < lastRes * 0.5) { sr.res = []; sr.forces = []; }   // solver restarted from scratch
    if (!sr.res.length || d.iteration > sr.res[sr.res.length - 1].iter) {
      if (d.residuals && Object.keys(d.residuals).length) {
        sr.res.push(Object.assign({ iter: d.iteration }, d.residuals));
        for (const k in d.residuals) if (!sr.fields.includes(k)) sr.fields.push(k);
        sr.res = thin(sr.res);
      }
    }
    if (AS.isNum(d.cl) && AS.isNum(d.cd) && (!sr.forces.length || d.iteration > sr.forces[sr.forces.length - 1].iter)) {
      sr.forces.push({ iter: d.iteration, cl: d.cl, cd: d.cd });
      sr.forces = thin(sr.forces);
    }
  }

  const seriesInFlight = {};
  const seriesRefreshedAt = {};      // case id -> time of the last refresh during a solve
  AS.loadSeries = async function (caseId) {
    const study = state.study;
    if (!study || !caseId || seriesInFlight[caseId]) return;
    seriesInFlight[caseId] = true;
    try {
      const raw = await AS.api.get(`/api/studies/${study.id}/cases/${caseId}/series`);
      if (!state.study || state.study.id !== study.id) return;
      const fresh = normaliseSeries(raw);
      const old = state.series[caseId];
      // keep live points that are newer than what the server returned
      if (old) {
        const lastR = fresh.res.length ? fresh.res[fresh.res.length - 1].iter : -Infinity;
        const lastF = fresh.forces.length ? fresh.forces[fresh.forces.length - 1].iter : -Infinity;
        const oldLastR = old.res.length ? old.res[old.res.length - 1].iter : -Infinity;
        if (oldLastR >= lastR) {   // same solver run: append the live tail
          fresh.res = fresh.res.concat(old.res.filter((r) => r.iter > lastR));
          fresh.forces = fresh.forces.concat(old.forces.filter((f) => f.iter > lastF));
          for (const f of old.fields) if (!fresh.fields.includes(f)) fresh.fields.push(f);
        }
      }
      state.series[caseId] = fresh;
      AS.invalidate();
    } catch (e) {
      const sr = (state.series[caseId] = state.series[caseId] || emptySeries());
      sr.error = e.message;
    } finally {
      seriesInFlight[caseId] = false;
    }
  };

  // ---------- opening a study: snapshot first, then the event stream ----------
  let stream = null;
  let horizon = { seq: null, time: 0 };   // events at or before this are already inside the snapshot

  function setBundle(resp) {
    state.study = resp.study || resp;
    state.cases = {};
    for (const c of resp.cases || []) state.cases[c.id] = c;
    if (!state.ui.caseId || !state.cases[state.ui.caseId]) {
      const first = AS.caseList()[0];
      state.ui.caseId = first ? first.id : null;
    }
  }

  function isHistorical(ev) {
    if (horizon.seq != null) return ev.seq <= horizon.seq;
    const t = Date.parse(ev.ts);
    return isFinite(t) && t <= horizon.time;
  }

  /* Move the selection to where the study is, after a status change (`before` is the previous status).
     The selection moves only on the first load, while Follow is on, or when the page on screen is
     the one the study has just left (the step of the previous status; the plan that has just been
     approved). A status change, a pause, a resume or the end of the run never pulls the engineer
     off a page they chose to read. */
  function syncViewToStatus(initial, before) {
    const study = state.study, ui = state.ui;
    if (!study) return;
    const cases = AS.caseList();
    if (!inWorkspace(study)) {
      const onPrevious = ui.sel.kind === 'step' && ui.sel.key === stepForStatus(Object.assign({}, study, { status: before }));
      if (initial || ui.follow || onPrevious) ui.sel = { kind: 'step', key: stepForStatus(study) };
      if (ui.tab !== 'dashboard') ui.tab = 'main';
      return;
    }
    if (!ui.caseId || !state.cases[ui.caseId]) ui.caseId = cases[0].id;
    if (study.status === 'done' && (initial || ui.follow)) {
      ui.sel = { kind: 'step', key: 'report' };
    } else if (initial || ui.follow || (ui.sel.kind === 'step' && ui.sel.key === 'plan' && before === 'awaiting_approval')) {
      followActiveStage(true);
    }
    if (ui.tab !== 'dashboard' && ui.tab !== 'compare') ui.tab = 'main';
  }

  function followActiveStage(force) {
    const ui = state.ui;
    let c = AS.currentCase();
    if (!c || (!ui.follow && !force)) return;
    if (ui.sel.kind === 'step' && ui.sel.key === 'report' && state.study.status === 'done') return;
    // Following a case that has finished while another is still at work: move on to that one,
    // otherwise Follow would sit on a finished stage and the button to catch up is not offered.
    if (ui.follow && !caseLive(c)) {
      const next = AS.caseList().find(caseLive);
      if (next) {
        ui.caseId = next.id; c = next;
        if (!(state.series[next.id] && state.series[next.id].loaded)) AS.loadSeries(next.id);
      }
    }
    const key = c.current_stage || (c.stages && c.stages[0] && c.stages[0].key);
    if (key) ui.sel = { kind: 'stage', key };
  }

  const RECONCILE = new Set(['study.status', 'plan.created', 'plan.updated', 'plan.approved', 'case.created', 'case.status',
    'stage.completed', 'stage.failed', 'stage.rework', 'case.completed', 'study.completed', 'approval.requested', 'candidates.proposed']);
  const SERIES_AFTER = { geometry: 1, solve: 1, post: 1, validate: 1 };

  let reconcileTimer = null;
  function scheduleReconcile() {
    clearTimeout(reconcileTimer);
    reconcileTimer = setTimeout(reconcile, 250);
  }
  async function reconcile() {
    const study = state.study;
    if (!study) return;
    try {
      const resp = await AS.api.get(`/api/studies/${study.id}`);
      if (!state.study || state.study.id !== study.id) return;
      const before = state.study.status;
      setBundle(resp);
      if (state.study.status !== before) syncViewToStatus(false, before);
      followActiveStage(false);
      AS.invalidate();
    } catch (e) { /* the stream state already shows a lost connection */ }
  }

  const refreshStudyList = AS.debounce(() => AS.loadStudies(), 600);

  function onEvent(ev) {
    // an event of a study that is no longer the open one (a stream that was superseded) is not ours
    if (!state.study || (ev.study_id && ev.study_id !== state.study.id)) return;
    state.events.push(ev);
    state.lastSeq = ev.seq;
    trackActivity(ev);
    trackSeries(ev);
    if (ev.type === 'human.message') {
      // a queued message has been delivered: drop the greyed copy
      const text = (ev.data && ev.data.text) || '';
      const i = state.pending.findIndex((p) => p.text === text && (!p.case_id || !ev.case_id || p.case_id === ev.case_id));
      if (i >= 0) state.pending.splice(i, 1);
    }
    const historical = isHistorical(ev);
    if (!historical) {
      const before = state.study.status;
      AS.applyEvent(state, ev);
      if (RECONCILE.has(ev.type)) scheduleReconcile();
      if (ev.case_id && (ev.type === 'stage.completed' && SERIES_AFTER[ev.stage])) AS.loadSeries(ev.case_id);
      // solver.progress arrives every 2 s (about 60 iterations apart): while a
      // solver runs, refill the charts from the full history every few seconds.
      if (ev.type === 'solver.progress' && ev.case_id) {
        const now = Date.now();
        if (now - (seriesRefreshedAt[ev.case_id] || 0) > 5000) { seriesRefreshedAt[ev.case_id] = now; AS.loadSeries(ev.case_id); }
      }
      if (ev.type === 'study.status') refreshStudyList();     // keeps the project's study list current
      if (state.study.status !== before) syncViewToStatus(false, before);
      else if (ev.type === 'stage.started' || ev.type === 'approval.requested' || ev.type === 'case.created') {
        if (ev.type === 'case.created' && !state.ui.caseId) state.ui.caseId = ev.data.case.id;
        followActiveStage(false);
      }
      if (ev.type === 'approval.requested') {
        const c = state.cases[ev.case_id];
        // on the study pages the row above the page says this; the toast is for Compare and Dashboard, where that row is hidden
        const covered = !!(AS.$('#lightbox') && !AS.$('#lightbox').hidden);   // the snapshot dialog hides the row
        if (state.ui.tab !== 'main' || covered) AS.toast(`${AS.caseLabel(c)} is waiting for your approval: ${AS.stageTitle((ev.data && ev.data.stage) || ev.stage)}`, 'warn');
      }
      if (ev.type === 'study.completed') {
        // always emitted when the pipelines end; the report is null when no case finished
        if (ev.data && ev.data.report) AS.toast('Study complete. The report and the comparison are ready.', 'ok');
        else AS.toast('The study ended without a report: no case finished. Re-run a stage to continue.', 'warn');
      }
      if (ev.type === 'error') AS.toast((ev.data && ev.data.message) || 'Error', 'error');
      // an answer to the engineer must not go unnoticed while the chat is collapsed
      if (ev.type === 'steering.reply' && state.ui.chatCollapsed) AS.toast('Reply: ' + String((ev.data && ev.data.text) || '').slice(0, 180), 'info');
    }
    fire('event', ev, historical);
    AS.invalidate();
  }

  let openToken = 0;       // identifies the latest openStudy / closeStudy; older calls give up after their await
  let openingId = null;    // the study an openStudy is still fetching

  // ---------- the address bar ----------
  /* #study=<id>, then &tab=compare|dashboard when that tab is shown, or &case=<id>&view=<stage or
     step key> while Follow is off on the main tab. A followed page is not recorded: after a reload
     it would no longer be the live one, and restoring it would turn Follow off unasked. */
  let hashReady = false;   // the hash the page was loaded with is not rewritten before applyHash has read it

  function parseHash() {
    const out = {};
    for (const part of location.hash.slice(1).split('&')) {
      const m = /^(study|tab|case|view)=(.+)$/.exec(part);
      if (!m || m[1] in out) continue;
      try { out[m[1]] = decodeURIComponent(m[2]); } catch (e) { /* a malformed value is ignored */ }
    }
    if (out.study && !/^[\w.-]+$/.test(out.study)) delete out.study;
    return out;
  }
  function hashForState() {
    const s = state.study, ui = state.ui;
    if (!s) return '';
    let hash = '#study=' + s.id;
    // the case is named in every state, so a reload returns to the case that was on screen; the page
    // only while Follow is off (a followed page is wherever the run is by then)
    if (ui.caseId && state.cases[ui.caseId]) hash += '&case=' + encodeURIComponent(ui.caseId);
    if (!ui.follow) hash += '&view=' + encodeURIComponent(ui.sel.key);
    if (ui.tab === 'compare' || ui.tab === 'dashboard') hash += '&tab=' + ui.tab;
    return hash;
  }
  /* Bring the address bar in step with the state. `push` adds a history entry (a move to another
     study, or to none, made inside the app), so that Back returns to the study that was left. */
  function writeHash(push, mark) {
    const hash = hashForState();
    if (location.hash === hash) return;
    const url = location.pathname + location.search + hash;
    // browsers limit how often the history may be written; a refused write must not stop a render
    try { if (push) history.pushState(mark || null, '', url); else history.replaceState(null, '', url); } catch (e) { /* the next render tries again */ }
  }
  /* Put the page a hash names on screen. Values that do not fit the open study are ignored. */
  function applyView(want) {
    const ui = state.ui;
    if (want.case && state.cases[want.case] && ui.caseId !== want.case) {
      if (want.view) AS.selectCase(want.case);
      else {
        // The address names a case but no page: it was written while Follow was on. Put that case
        // on screen without touching a study page (the Report) or the Follow flag.
        const c = state.cases[want.case];
        ui.caseId = want.case;
        if (ui.sel.kind === 'stage') ui.sel = { kind: 'stage', key: c.current_stage || (c.stages[0] && c.stages[0].key) || 'geometry' };
        if (!(state.series[want.case] && state.series[want.case].loaded)) AS.loadSeries(want.case);
      }
    }
    if (want.view) {
      const isStep = want.view === 'report' || AS.STEPS.some((d) => d.key === want.view);
      if (isStep) AS.select({ kind: 'step', key: want.view });
      else if (AS.stageOf(AS.currentCase(), want.view)) AS.select({ kind: 'stage', key: want.view });
    }
    // the Compare tab is disabled until the study has cases
    if (want.tab === 'dashboard' || (want.tab === 'compare' && AS.caseList().length)) ui.tab = want.tab;
  }

  /* Called at the end of every render: the hash is derived from the state in this one place, because
     the selection also moves without a click (Follow, status changes). Never invalidates. */
  AS.syncHash = function () {
    // while a study is being fetched the hash may already name it (Back, an edited hash); openStudy settles it
    if (!hashReady || state.loading) return;
    writeHash(false);
  };

  /* The hash changed under the app (Back, Forward, an edit of the address bar) or the page has just loaded. */
  AS.applyHash = function () {
    hashReady = true;
    const want = parseHash();
    const back = steppingBack;
    steppingBack = null; clearTimeout(steppingTimer);
    if (back && back === want.study && (!state.study || state.study.id !== back)) return AS.openStudy(back, { case: want.case });
    if (!want.study) {
      if (state.study || state.loading) AS.newStudy();
      return Promise.resolve();
    }
    // Back fires popstate and hashchange for the same move: the open already under way is the answer to both
    if (state.loading && openingId === want.study) return Promise.resolve();
    if (!state.study || state.study.id !== want.study) return AS.openStudy(want.study, want);
    // the address is back on the open study while another one was being fetched (Back during a load):
    // that open is dropped, or its answer would replace the study the address names
    if (openingId) { openToken += 1; openingId = null; state.loading = false; AS.invalidate(); }
    if (hashForState() !== location.hash) {
      state.ui.tab = 'main';
      applyView(want);
      AS.invalidate();
    }
    return Promise.resolve();
  };

  function teardown() {
    state.loading = false;
    if (stream) { stream.close(); stream = null; }
    clearTimeout(reconcileTimer);
    Object.assign(state, { study: null, cases: {}, events: [], activity: {}, series: {}, pending: [], lastSeq: 0, timing: { start: null, end: null } });
    state.ui.caseId = null;
    state.ui.sel = { kind: 'step', key: 'mission' };
    state.ui.follow = true;
    // the Dashboard's "open" link stays on the Dashboard until the study has loaded
    if (state.ui.tab !== 'dashboard') state.ui.tab = 'main';
    fire('reset');
  }

  AS.closeStudy = function () {
    openToken += 1;
    openingId = null;
    teardown();
  };

  let steppingBack = null, steppingTimer = 0;   // the study a history.back() is returning to
  /* `view` is the page a hash asked for ({ tab, case, view }); it is applied once the study has loaded. */
  AS.openStudy = async function (studyId, view) {
    if (steppingBack) return;              // a step back to this study is under way (see below)
    // "New", then the same study again from the list or the Dashboard: its history entry is one step
    // back. The step arrives as a popstate; applyHash then opens the study on the main tab at its live
    // page, not on the tab and page that old entry happened to record.
    if (!view && !state.study && !parseHash().study && history.state && history.state.closed === studyId) {
      steppingBack = studyId;
      clearTimeout(steppingTimer);
      steppingTimer = setTimeout(() => { steppingBack = null; }, 1500);   // no popstate came: do not block later opens
      history.back();
      return;
    }
    const token = ++openToken;
    openingId = studyId;
    state.loading = true;
    AS.invalidate();
    const requestedAt = Date.now();
    let replaced = false;
    try {
      const resp = await AS.api.get(`/api/studies/${studyId}`);
      if (token !== openToken) return;     // another study was opened (or this one closed) meanwhile
      const named = parseHash().study;
      // The study that was open is closed only now: a snapshot that cannot be fetched leaves it,
      // its event stream and its viewers as they were.
      teardown();
      replaced = true;
      setBundle(resp);
      // Events up to here are already reflected in the snapshot; they are
      // replayed only to rebuild the transcript and the live charts.
      horizon = { seq: typeof resp.last_seq === 'number' ? resp.last_seq : null, time: requestedAt };
      // not quiet: the study list shown next to the mission has to be the one of this project
      if (state.study.project_id && state.projects.some((p) => p.id === state.study.project_id)) AS.setProject(state.study.project_id);
      syncViewToStatus(true);
      if (view) { state.ui.tab = 'main'; applyView(view); }
      // A new history entry only when the app itself moved from one study to another; when the hash
      // already names this study (Back, a reload, an edited hash) the entry exists.
      writeHash(!!named && named !== studyId);
      stream = new AS.EventStream(studyId, 0, onEvent, (conn) => {
        const was = state.conn;
        state.conn = conn;
        if (conn === 'live' && was === 'reconnecting') reconcile();   // catch up on anything missed
        AS.invalidate();
      });
      for (const c of AS.caseList()) AS.loadSeries(c.id);
    } catch (e) {
      if (token !== openToken) return;
      AS.toast(`Could not open study ${studyId}: ${e.message}`, 'error');
      writeHash(false);                    // back to the study that is open, or to no hash when none is
    } finally {
      if (token === openToken) {
        openingId = null;
        state.loading = false;
        if (replaced) fire('reset');       // never on a failed fetch: the open study keeps its viewers and transcript
        AS.invalidate();
      }
    }
  };

  AS.newStudy = function () {
    const named = parseHash().study;
    AS.closeStudy();
    state.ui.tab = 'main';                 // "New" on the Dashboard has to show the mission form
    // Back returns to the study; when the hash was emptied the entry exists. The new entry notes which
    // study it left, so that re-opening that study steps back instead of adding a second entry for it.
    writeHash(!!named, named ? { closed: named } : null);
    AS.invalidate();
  };

  // ---------- projects ----------
  AS.loadProjects = async function () {
    const resp = await AS.api.get('/api/projects');
    state.projects = (resp && resp.projects) || [];
    if (!state.projects.some((p) => p.id === state.projectId)) {
      const preferred = state.config && state.config.default_project_id;
      state.projectId = state.projects.some((p) => p.id === preferred) ? preferred : (state.projects[0] && state.projects[0].id) || null;
    }
    await AS.loadStudies();
    AS.invalidate();
  };
  AS.loadStudies = async function () {
    const pid = state.projectId;
    if (!pid) { state.studies = []; return; }
    let studies = [];
    try {
      const resp = await AS.api.get(`/api/projects/${pid}/studies`);
      studies = (resp && resp.studies) || [];
    } catch (e) { /* shown as an empty list */ }
    if (state.projectId !== pid) return;   // the project changed meanwhile: its own request fills the list
    state.studies = studies;
    AS.invalidate();
  };
  AS.setProject = function (projectId, quiet) {
    if (state.projectId === projectId) return;
    state.projectId = projectId;
    AS.store.set('project', projectId);
    if (!quiet) AS.loadStudies();
    AS.invalidate();
  };

  // ---------- actions ----------
  async function act(label, fn) {
    try { return await fn(); }
    catch (e) { AS.toast(`${label}: ${e.message}`, 'error'); return null; }
  }
  const base = () => `/api/studies/${state.study.id}`;

  AS.actions = {
    createProject: (name, description) => act('Create project', async () => {
      const p = await AS.api.post('/api/projects', { name, description });
      await AS.loadProjects();
      if (p && p.id) AS.setProject(p.id);
      return p;
    }),
    /* Create a study and start the advisor in one go. */
    startStudy: (payload) => act('Suggest airfoils', async () => {
      const study = await AS.api.post('/api/studies', payload);
      const id = study.id || (study.study && study.study.id);
      await AS.openStudy(id);
      await AS.api.post(`/api/studies/${id}/suggest`);
      AS.loadStudies();
      return id;
    }),
    suggest: () => act('Suggest airfoils', () => AS.api.post(base() + '/suggest')),
    select: (nacas) => act('Confirm selection', () => AS.api.post(base() + '/select', { nacas })),
    patchPlan: (plan) => act('Save plan edit', async () => {
      const study = await AS.api.patch(base() + '/plan', { plan });
      if (study && study.plan && state.study) { state.study.plan = study.plan; AS.invalidate(); }
      return study;
    }),
    revisePlan: (comment) => act('Revise plan', () => AS.api.post(base() + '/plan/revise', { comment })),
    approvePlan: (mode) => act('Approve plan', () => AS.api.post(base() + '/approve', { mode })),
    setMode: (mode) => act('Change mode', async () => {
      const study = await AS.api.post(base() + '/mode', { mode });
      if (state.study) { state.study.mode = (study && study.mode) || mode; AS.invalidate(); }
      return study || { mode };
    }),
    control: (action, caseId) => act(action.charAt(0).toUpperCase() + action.slice(1), () =>
      AS.api.post(base() + '/control', caseId ? { action, case_id: caseId } : { action })),
    approveStage: (caseId) => act('Approve stage', () => AS.api.post(`${base()}/cases/${caseId}/approve`)),
    rerun: (caseId, stage, instruction) => act('Re-run stage', () =>
      AS.api.post(`${base()}/cases/${caseId}/rerun`, instruction ? { stage, instruction } : { stage })),
    sendMessage: (message) => act('Send message', async () => {
      const local = Object.assign({ localId: Date.now() + Math.random(), ts: new Date().toISOString() }, message);
      state.pending.push(local);
      AS.invalidate();
      try {
        await AS.api.post(base() + '/message', message);
      } catch (e) {
        state.pending = state.pending.filter((p) => p !== local);
        AS.invalidate();
        throw e;
      }
    }),
  };

  // ---------- selection helpers used by several views ----------
  AS.select = function (sel, opts) {
    const ui = state.ui, study = state.study;
    ui.sel = sel;
    ui.tab = 'main';
    const c = AS.currentCase();
    // following stops when the user picks something other than the live page: the stage in flight
    // or, before the cases exist, the setup step the study is at
    const live = sel.kind === 'stage' ? caseLive(c) && c.current_stage === sel.key
      : !!study && !inWorkspace(study) && sel.key === stepForStatus(study);
    ui.follow = !!(opts && opts.follow) || live;
    AS.invalidate();
  };
  AS.selectCase = function (caseId) {
    const ui = state.ui, c = state.cases[caseId];
    const current = () => ({ kind: 'stage', key: (c && c.current_stage) || 'geometry' });
    // as in AS.select: Follow is on only on the stage in flight of a case that is running or waiting
    const following = () => caseLive(c) && ui.sel.kind === 'stage' && ui.sel.key === c.current_stage;
    ui.tab = 'main';
    if (ui.caseId !== caseId) {
      // A past stage picked on purpose is kept, to compare it across cases. Otherwise (a step, Follow,
      // or the stage the case left was at) the new case opens on its own current stage.
      const prev = state.cases[ui.caseId];
      const onCurrent = ui.sel.kind !== 'stage' || ui.follow || (prev && prev.current_stage === ui.sel.key);
      ui.caseId = caseId;
      if (onCurrent) ui.sel = current();
      ui.follow = following();
      if (c && !(state.series[caseId] && state.series[caseId].loaded)) AS.loadSeries(caseId);
    } else if (ui.sel.kind === 'step') {
      ui.sel = current();
      ui.follow = following();
    }
    AS.invalidate();
  };
  AS.setTab = function (tab) { state.ui.tab = tab; AS.invalidate(); };

  /* The case "Follow live" should show: the current one if it is at work, else the first that is,
     else one that still has work to do (paused or not started). */
  function liveCase() {
    const cur = AS.currentCase(), cases = AS.caseList();
    for (const group of [['running', 'awaiting_approval'], ['paused', 'pending']]) {
      if (cur && group.includes(cur.status)) return cur;
      const c = cases.find((x) => group.includes(x.status));
      if (c) return c;
    }
    return cur;
  }
  AS.followLive = function () {
    const ui = state.ui, study = state.study;
    if (study) {
      if (!inWorkspace(study)) ui.sel = { kind: 'step', key: stepForStatus(study) };   // no cases yet: the step the study is at
      else if (study.status === 'done') ui.sel = { kind: 'step', key: 'report' };
      else {
        const live = liveCase();
        if (live && live.id !== ui.caseId) AS.selectCase(live.id);   // also loads its charts
        followActiveStage(true);
      }
    }
    ui.follow = true;
    ui.tab = 'main';
    AS.invalidate();
  };

  /* A paused case holds its stage in flight at a tool boundary: that stage is shown as paused, and
     its clock stands at the value it had when the pause was first drawn (it runs on after Resume). */
  AS.stageStatus = (c, st) => (!st ? 'pending' : c && c.status === 'paused' && st.status === 'running' ? 'paused' : st.status);
  const heldClock = {};
  AS.stageClock = function (c, st) {
    const k = c.id + '|' + st.key;
    if (AS.stageStatus(c, st) === 'paused') { if (!(k in heldClock)) heldClock[k] = AS.stageSeconds(st); return heldClock[k]; }
    delete heldClock[k];
    return AS.stageSeconds(st);
  };
  /* Seconds a stage has been running (live) or took (finished). */
  AS.stageSeconds = function (st) {
    if (!st) return null;
    if (st.status === 'running' && st.started_at) return Math.max(0, (Date.now() - Date.parse(st.started_at)) / 1000);
    if (AS.isNum(st.seconds)) return st.seconds;
    if (st.started_at && st.ended_at) return (Date.parse(st.ended_at) - Date.parse(st.started_at)) / 1000;
    return null;
  };
  AS.runSeconds = function () {
    const t = state.timing;
    if (!t.start) return null;
    const end = t.end ? Date.parse(t.end) : Date.now();
    return Math.max(0, (end - Date.parse(t.start)) / 1000);
  };
})();
