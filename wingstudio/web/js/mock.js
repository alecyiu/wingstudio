/* Wing Studio - mock mode (?mock=1), also the engine of the recorded tutorial (?tutorial=1).

   An in-page stand-in for the backend: it answers the HTTP API of server.py and streams the
   same events, so the UI runs through its normal code paths. Only the read-only reference
   routes (/api/refs, /api/refs/doc) are the server's own. It plays a recorded study: the XFoil
   polars, Cp curves and solver history in fixtures/recorded.json come from a real run; agent
   messages and timings are scripted. State is kept in sessionStorage, so a reload restores it.

   URL options:  mock=1         enable          mock=reset   also forget stored mock studies
                 speed=10       play 10x faster noviewer=1   pretend ParaView is unavailable
                 tutorial=1     the recorded tutorial (js/tutorial.js); implies the mock
                 tutorial=reset the same, from a clean slate
                 steps=live     with tutorial=1: the live tutorial's step list, on the mock

   Where the UI depends on it, the mock follows the engine: stops, re-runs, gate failures and
   retry approvals, plan validation, the y+ gate on an engineer's first cell height (corrected
   on attempt 2), citations from the server's catalogue, metrics rows and pass^k (AS.passK).

   The tutorial adds checkpoints: while AS.mock.hold(name) is armed, a case that reaches that
   point stands still until AS.mock.release(name). Names: '<stage>.passed', 'mesh.gate_failed',
   'solve.half', optionally followed by '@<naca>'. A checkpoint emits no event, and the time spent
   there is left out of the stage and solver seconds. */
(function () {
  'use strict';
  const AS = window.AS;
  const query = new URLSearchParams(location.search);
  // the tutorial runs on the mock, so asking for it turns the mock on;
  // the live tutorial runs on the real backend, so it keeps the mock off whatever else the address says
  const LIVE = query.get('tutorial') === 'live';
  const TUTORIAL = !LIVE && query.has('tutorial') && query.get('tutorial') !== '0';
  const enabled = !LIVE && (TUTORIAL || (query.has('mock') && query.get('mock') !== '0'));
  AS.mock = { enabled, tutorial: TUTORIAL, live: LIVE };
  if (!enabled) return;

  // the live step list of js/tutorial.js played on the mock (its test switch)
  const LIVE_STEPS = TUTORIAL && query.get('steps') === 'live';
  const SPEED = Math.max(0.1, parseFloat(query.get('speed')) || 1);
  const NO_VIEWER = query.has('noviewer');
  // a database of its own: a ?mock=1 session is neither shown nor overwritten by the tutorial
  const DB_KEY = TUTORIAL ? 'as.mock.tutorial.v1' : 'as.mock.db.v1';
  if (query.get('mock') === 'reset') {
    try { sessionStorage.removeItem(DB_KEY); } catch (e) { /* ignore */ }
    // reset once: a later reload of the page must keep the mock study
    query.set('mock', '1');
    history.replaceState(null, '', location.pathname + '?' + query.toString() + location.hash);
  }
  if (query.get('tutorial') === 'reset') {
    // the study and the step record of js/tutorial.js go together: one without the other cannot be resumed
    try { sessionStorage.removeItem('as.mock.tutorial.v1'); sessionStorage.removeItem('as.tutorial.v1'); } catch (e) { /* ignore */ }
    query.set('tutorial', '1');
    history.replaceState(null, '', location.pathname + '?' + query.toString() + location.hash);
  }

  const KEYS = ['geometry', 'mesh', 'physics', 'numerics', 'solve', 'post', 'validate'];
  const CANCEL = { cancelled: true };
  let fx = null;            // fixtures
  let db = null;            // { projects, studies: {sid: S}, runs, counter }
  const runtime = {};       // sid -> { listeners, procs, queues, approvals, paused }

  const nowIso = () => new Date().toISOString();
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const R = (S) => (runtime[S.study.id] = runtime[S.study.id] || { listeners: new Set(), procs: {}, queues: {}, approvals: {}, paused: new Set(), forceGate: new Set() });
  const stageOf = (c, key) => c.stages.find((st) => st.key === key);
  const sig = (v, n) => parseFloat(Number(v).toPrecision(n || 3));

  // ---------- citations (ports of the rules in planning.py; ids come from the server's catalogue only) ----------
  const engineerSet = AS.engineerSet;
  // the default references of 'stage.setting' or 'case.alpha_deg': a copy, at most three, [] if none
  const defaultCites = (key) => ((fx.refs.defaults || {})[key] || []).slice(0, 3);
  // without a catalogue an id cannot be judged, so it is kept
  const knownRef = (id) => !fx.refIds.size || fx.refIds.has(id);
  const planSection = (S, c, stage) => ((((S.study.plan || {}).cases || []).find((pc) => pc.naca === c.naca) || {}).stages || {})[stage] || {};
  /* Equal as decision_cites compares a decision with the plan: numbers within 1 %, anything else as text. */
  function sameValue(a, b) {
    const number = (v) => (typeof v === 'number' || (typeof v === 'string' && v.trim() !== '' && isFinite(Number(v))) ? Number(v) : null);
    const x = number(a), y = number(b);
    if (x != null && y != null) return Math.abs(x - y) <= 0.01 * Math.max(Math.abs(x), Math.abs(y));
    return String(a).trim() === String(b).trim();
  }
  /* The references of one decision. Ids the stage script names are kept. A decision that applies a plan
     value unchanged carries the plan's ids, unless the engineer set that value. Anything else has none. */
  function decisionCites(section, d) {
    if (Array.isArray(d.cites)) return d.cites.filter(knownRef).slice(0, 3);
    const name = d.param;
    if (!(name in section) || engineerSet(section, name) || !sameValue(d.value, section[name])) return [];
    return ((section.cites || {})[name] || []).slice(0, 3);
  }
  /* The citation map an engineer edit leaves in place: the stored one, for the settings that still exist.
     Nothing is taken from the request, and an id that has left the catalogue is dropped. */
  function keptCites(stored, holder) {
    const out = {};
    for (const name in stored || {}) {
      const ids = name in (holder || {}) && Array.isArray(stored[name]) ? stored[name].filter(knownRef) : [];
      if (ids.length) out[name] = ids;
    }
    return out;
  }

  /* Tutorial checkpoints: the names armed with AS.mock.hold, and the case scripts standing at one
     (see Ctx.at). In memory only: a reload starts without holds. */
  const holds = { armed: new Set(), standing: new Set() };

  // ---------- persistence ----------
  let saveTimer = null;
  function save() {
    if (saveTimer) return;
    saveTimer = setTimeout(saveNow, 800);
  }
  function saveNow() {
    clearTimeout(saveTimer); saveTimer = null;
    try { sessionStorage.setItem(DB_KEY, JSON.stringify(db)); } catch (e) { /* quota: the mock simply will not survive a reload */ }
  }
  window.addEventListener('pagehide', saveNow);

  // ---------- model profiles (latency, tokens and prices are illustrative) ----------
  function profile(model) {
    const m = String(model).toLowerCase();
    if (m === 'scripted') return null;
    if (m.includes('haiku')) return { ms: 450, tin: 2300, tout: 150, pin: 1.0, pout: 5.0 };
    if (m.includes('opus')) return { ms: 1300, tin: 2100, tout: 260, pin: 5.0, pout: 25.0 };
    return { ms: 850, tin: 2150, tout: 210, pin: 3.0, pout: 15.0 };
  }

  // ---------- physics helpers (ports of wingstudio/cfd/case.py) ----------
  function deriveConditions(req) {
    const gamma = 1.4, Rgas = 287.05;
    const mach = Number(req.mach), density = Number(req.density), reynolds = Number(req.reynolds);
    let T = 288.15 * Math.pow(density / 1.225, 1 / 4.2559);   // ISA troposphere temperature at this density
    if (!isFinite(T) || T < 200 || T > 320) T = 288.15;
    const a = Math.sqrt(gamma * Rgas * T), U = mach * a;
    const mu = 1.716e-5 * Math.pow(T / 273.15, 1.5) * (273.15 + 110.4) / (T + 110.4);
    const nu = mu / density;
    const warnings = [];
    if (mach > 0.3) warnings.push(`Mach ${mach} is above 0.3: the solver is incompressible, so compressibility (about ${((1 / Math.sqrt(1 - mach * mach) - 1) * 100).toFixed(0)} % on lift) is not modelled.`);
    if (reynolds < 5e5) warnings.push('Reynolds number below 5e5: a fully turbulent RANS run overstates drag considerably.');
    return {
      mach, density, reynolds, temperature_K: +T.toFixed(2), pressure_Pa: +(density * Rgas * T).toFixed(1),
      speed_of_sound: +a.toFixed(2), velocity: +U.toFixed(3), dynamic_viscosity: mu, kinematic_viscosity: nu,
      chord_m: +(reynolds * nu / U).toFixed(4), dynamic_pressure_Pa: +(0.5 * density * U * U).toFixed(2),
      nu_sim: U / reynolds, compressibility_factor: 1 / Math.sqrt(Math.max(1e-9, 1 - mach * mach)),
      incompressible_ok: mach <= 0.3, warnings,
    };
  }
  // flat-plate estimate; y+ refers to the first cell centre, the height to the whole cell
  const frictionFactor = (re) => Math.sqrt(0.026 / Math.pow(re, 1 / 7) / 2);
  const firstCellHeight = (re, yplus) => 2 * yplus / (re * frictionFactor(re));
  const yplusFor = (re, height) => 0.5 * height * re * frictionFactor(re);

  function interp(rows, xKey, x, yKey) {
    for (let i = 0; i + 1 < rows.length; i++) {
      const a = rows[i], b = rows[i + 1];
      if ((a[xKey] - x) * (b[xKey] - x) <= 0 && a[xKey] !== b[xKey]) return a[yKey] + (x - a[xKey]) * (b[yKey] - a[yKey]) / (b[xKey] - a[xKey]);
    }
    return null;
  }
  function xfoilAt(naca, alpha) {
    const polar = fx.recorded.airfoils[naca].polar;
    const cl = interp(polar, 'alpha', alpha, 'cl'), cd = interp(polar, 'alpha', alpha, 'cd'), cm = interp(polar, 'alpha', alpha, 'cm');
    return { alpha, cl: +cl.toFixed(4), cd: +cd.toFixed(5), cm: +cm.toFixed(4), ld: +(cl / cd).toFixed(2) };
  }

  // ---------- event log ----------
  function emit(S, type, data, where) {
    where = where || {};
    const ev = {
      seq: ++S.seq, ts: nowIso(), study_id: S.study.id, case_id: where.case_id || null, stage: where.stage || null,
      agent: where.agent || null, span_id: 'sp' + S.seq, parent_span_id: null, type, data: data || {},
    };
    const json = JSON.stringify(ev);
    S.events.push(JSON.parse(json));
    AS.applyEvent(S, JSON.parse(json));      // the mock keeps its snapshot with the same reducer as the UI
    S.study.updated_at = ev.ts;
    save();
    for (const listener of R(S).listeners) setTimeout(() => listener(json), 0);
    return ev;
  }

  /* An action of the engineer, shaped as the engine writes it: `case_ids` names the case it counts for
     (the mock emits one event per case), and the approval of a retry is a gate_decision with `retry`.
     The shared reducer (AS.applyEvent, run by emit) counts it in counters.human_interventions and, for
     the help kinds (steer, rerun, plan_edit, a retry approval), in counters.help_interventions. */
  function intervention(S, kind, where, retry) {
    const c = (where && where.case_id && S.cases[where.case_id]) || null;
    emit(S, 'human.intervention', Object.assign({ kind, case_ids: c ? [c.id] : [] }, retry ? { retry: true } : null), where);
  }

  // ---------- cooperative processes: one per case pipeline or study-level agent ----------
  function spawn(S, key, body) {
    const rt = R(S);
    if (rt.procs[key]) rt.procs[key].cancel();
    const token = { cancelled: false, waiters: [], cancel() { this.cancelled = true; this.waiters.forEach((w) => w()); } };
    rt.procs[key] = token;
    body(token).catch((e) => {
      if (e !== CANCEL) { console.error('mock process failed', e); emit(S, 'error', { message: String(e && e.message || e) }); }
    }).finally(() => { if (rt.procs[key] === token) delete rt.procs[key]; });
  }

  class Ctx {
    constructor(S, token, agent, c, stage) {
      Object.assign(this, { S, token, agent, c: c || null, stage: stage || null, phase: null, started: Date.now(), heldMs: 0, gateFails: 0 });
      this.model = profile(S.study.model);
      this.where = { case_id: c ? c.id : null, stage: this.stage, agent };
      this.queueKey = c ? c.id : '_study';
    }
    emit(type, data) { return emit(this.S, type, data, this.where); }
    async sleep(ms) {
      await wait(ms / SPEED);
      const rt = R(this.S);
      // pause takes effect here, i.e. at the next tool boundary
      while (!this.token.cancelled && (rt.paused.has('*') || rt.paused.has(this.queueKey))) await wait(120);
      if (this.token.cancelled) throw CANCEL;
    }
    // time spent standing at a tutorial checkpoint is reading time, not work
    seconds() { return +((Date.now() - this.started - this.heldMs) / 1000 * SPEED).toFixed(1); }
    /* Tutorial checkpoint: stand here while `name`, or `name@<naca>` for this case only, is armed.
       Nothing is emitted and queued messages stay queued, so the page keeps the state it had. */
    async at(name) {
      if (!TUTORIAL || !this.c) return;
      const keys = [name, name + '@' + this.c.naca];
      const armed = () => keys.some((k) => holds.armed.has(k));
      if (!armed()) return;
      const entry = { name, case_id: this.c.id, naca: this.c.naca, stage: this.stage }, began = Date.now();
      holds.standing.add(entry);
      try {
        while (armed() && !this.token.cancelled) await wait(100);
      } finally {
        holds.standing.delete(entry);
        this.heldMs += Date.now() - began;
      }
      if (this.token.cancelled) throw CANCEL;
    }
    async llm() {
      const m = this.model;
      if (!m) return;
      await this.sleep(m.ms);
      const tin = Math.round(m.tin * (0.8 + Math.random() * 0.5)), tout = Math.round(m.tout * (0.6 + Math.random() * 0.9));
      const cached = Math.round(tin * 0.6);
      this.emit('llm.call', { model: this.S.study.model, ms: m.ms, tokens_in: tin, tokens_out: tout, tokens_cached: cached,
        cost_usd: +(((tin - cached) * m.pin + cached * m.pin * 0.1 + tout * m.pout) / 1e6).toFixed(6), finish_reason: 'tool_calls' });
    }
    /* What the agent says between tool calls. The scripted baseline has no LLM, so it says nothing. */
    async say(text) {
      if (!this.model) return;
      await this.llm();
      this.emit('agent.message', { text });
    }
    setPhase(phase) {
      if (this.stage && this.phase !== phase) { this.phase = phase; this.emit('stage.phase', { phase }); }
    }
    async tool(phase, name, args, summary, ms, ok) {
      this.setPhase(phase);
      this.emit('tool.call', { name, args: args || {} });
      await this.sleep(ms || 400);
      this.emit('tool.result', { name, ok: ok !== false, ms: Math.round(ms || 400), summary });
      await this.deliver();
    }
    hasMessage() { return (R(this.S).queues[this.queueKey] || []).length > 0; }
    /* The plan's references for a decision row that reports plan settings under another name or several
       at once ('n_leading / n_aft / n_wake'): none if the engineer set any of them. */
    planCites(...names) {
      const section = planSection(this.S, this.c, this.stage), out = [];
      for (const name of names) {
        if (engineerSet(section, name)) return [];
        for (const id of (section.cites || {})[name] || []) if (!out.includes(id)) out.push(id);
      }
      return out.slice(0, 3);
    }
    /* Steering: queued human messages reach the agent at a tool boundary. */
    async deliver() {
      const queue = R(this.S).queues[this.queueKey] || [];
      while (queue.length) {
        const m = queue.shift();
        emit(this.S, 'human.message', { text: m.text, quote: m.quote || undefined, target_stage: m.stage || undefined }, { case_id: this.where.case_id, stage: this.stage });
        intervention(this.S, 'steer', { case_id: this.where.case_id, stage: this.stage });
        if (this.model) await this.say(`Understood: "${m.text.slice(0, 140)}". I will take that into account for the rest of this stage and note it in the hand-off.`);
        else emit(this.S, 'steering.reply', { text: 'The scripted baseline applies the plan as written and cannot act on comments. Edit the plan or re-run the stage with another model.' }, this.where);
        this.notes = (this.notes || []).concat(m.text);
      }
    }
    /* complete_stage -> deterministic gate -> stage.completed (only when the gate passes). */
    async complete(result) {
      this.setPhase('verify');
      this.emit('tool.call', { name: 'complete_stage', args: { summary: result.summary.slice(0, 80) + '…' } });
      await this.sleep(250);
      const passed = result.checks.every((ch) => ch.ok);
      this.emit('gate.result', { passed, checks: result.checks });
      const bad = result.checks.filter((ch) => !ch.ok);
      // the tutorial, and a stage script that uses the engine's check names, word a failure as the
      // engine does and return what the engine returns to the agent (three gate failures end a stage there)
      const real = TUTORIAL || this.engineWording;
      const failed = bad.map((ch) => (real ? `${ch.name}: ${ch.value} (limit ${ch.limit})` : `${ch.name} ${ch.value} (limit ${ch.limit})`)).join('; ');
      if (!passed) this.gateFails += 1;
      this.emit('tool.result', Object.assign({ name: 'complete_stage', ok: passed, ms: 250, summary: passed ? 'gate passed' : 'gate failed: ' + failed },
        passed || !real ? null : { error: 'the gate did not pass: ' + failed, failed_checks: bad, attempts_left: Math.max(0, 3 - this.gateFails),
          hint: 'fix the cause with your tools and call complete_stage again' }));
      if (!passed) { this.failure = 'the gate did not pass: ' + failed; return false; }
      const summary = result.summary + (this.notes ? ' Engineer comments taken into account: ' + this.notes.join(' | ') : '');
      // every decision record carries `cites`, possibly empty, as on the backend
      const section = planSection(this.S, this.c, this.stage);
      const decisions = (result.decisions || []).map((d) => Object.assign({}, d, { cites: decisionCites(section, d) }));
      this.emit('stage.completed', { summary, headline: result.headline, decisions, metrics: result.metrics || {}, seconds: this.seconds() });
      return true;
    }
  }

  // ---------- study-level agents ----------
  const DESCRIPTIONS = {
    '2412': 'Moderate 2 % camber keeps the pitching moment low and the drag bucket near the design lift.',
    '4412': 'Classic 4 % camber section: reaches the design lift at a low angle, at the price of a larger nose-down moment.',
    '2415': 'Same camber line as the 2412 with 15 % thickness for spar depth; slightly more profile drag.',
    '4415': 'Thicker 4 % camber section: structural depth and gentle stall, more drag than the 12 % sections.',
    '6412': 'High 6 % camber: efficient only at high lift coefficients; large pitching moment.',
  };

  function screen(req) {
    const objective = req.objective || 'max_ld';
    const out = [];
    for (const naca of Object.keys(fx.recorded.airfoils)) {
      const polar = fx.recorded.airfoils[naca].polar;
      const alpha = interp(polar, 'cl', Number(req.design_cl), 'alpha');
      const thickness = parseInt(naca.slice(2), 10) / 100;
      let x = null;
      if (alpha != null) {
        const p = xfoilAt(naca, +alpha.toFixed(2));
        x = Object.assign(p, { alpha_design: p.alpha, endurance: +(Math.pow(p.cl, 1.5) / p.cd).toFixed(2), converged: true });
      }
      const notes = [];
      if (req.min_thickness && thickness + 1e-9 < req.min_thickness) notes.push(`thickness ${(thickness * 100).toFixed(0)} % is below the ${(req.min_thickness * 100).toFixed(0)} % minimum`);
      if (x && req.max_abs_cm && Math.abs(x.cm) > req.max_abs_cm) notes.push(`|Cm| ${Math.abs(x.cm).toFixed(3)} exceeds the limit ${req.max_abs_cm}`);
      if (!x) notes.push('the design lift coefficient is outside the screened angle range');
      const score = !x ? -Infinity : objective === 'min_cd' ? -x.cd : objective === 'max_endurance' ? x.endurance : x.ld;
      out.push({ naca, x, notes, score: notes.length ? score - 1e6 : score });
    }
    out.sort((a, b) => b.score - a.score);
    const target = Number(req.target_ld) > 0 ? Number(req.target_ld) : null;
    return out.map((o, i) => ({
      naca: o.naca, rank: i + 1, selected: i < 2,
      // reported, never used to exclude a candidate
      meets_target_ld: target == null ? null : o.x ? o.x.ld >= target : false,
      rationale: DESCRIPTIONS[o.naca] + (o.x ? ` XFoil at the design point: L/D ${o.x.ld.toFixed(1)}, Cd ${o.x.cd.toFixed(5)}, Cm ${o.x.cm.toFixed(3)} at ${o.x.alpha.toFixed(2)} deg.` : '') +
        (o.notes.length ? ' Constraint not met: ' + o.notes.join('; ') + '.' : ''),
      xfoil: o.x ? { alpha_design: o.x.alpha, cl: o.x.cl, cd: o.x.cd, cm: o.x.cm, ld: o.x.ld, endurance: o.x.endurance, converged: true, polar: AS.clone(fx.recorded.airfoils[o.naca].polar) }
        : { alpha_design: null, cl: null, cd: null, cm: null, ld: null, endurance: null, converged: false, polar: AS.clone(fx.recorded.airfoils[o.naca].polar) },
    }));
  }

  async function runAdvisor(S, token) {
    const x = new Ctx(S, token, 'airfoil-advisor');
    const req = S.study.requirements;
    emit(S, 'study.status', { status: 'suggesting', mode: S.study.mode });
    await x.say(`Reading the mission: Mach ${req.mach}, density ${req.density} kg/m3, Reynolds number ${AS.fmtSci(req.reynolds)}, design Cl ${req.design_cl}, objective "${AS.OBJECTIVES[req.objective] || req.objective}".`);
    emit(S, 'requirements.parsed', { requirements: AS.clone(req), conditions: AS.clone(S.study.conditions) }, x.where);
    await x.say('I will screen five NACA 4-digit sections in XFoil at this Reynolds number, with transition forced at 5 % chord so the numbers are comparable with a fully turbulent RANS run.');
    const candidates = screen(req);
    await x.tool('act', 'xfoil_screen', { nacas: candidates.map((cd) => cd.naca), design_cl: req.design_cl },
      `${candidates.filter((cd) => cd.xfoil.converged).length} of ${candidates.length} converged at Cl ${req.design_cl} · recorded polars at Re 3e6`, 2200);
    const top = candidates.slice(0, 2);
    await x.say(`Ranked by the objective. ${top.map((cd) => 'NACA ' + cd.naca).join(' and ')} are the two best that meet the constraints; I recommend simulating those two.`);
    await x.tool('act', 'propose_candidates', { candidates: candidates.map((cd) => cd.naca), top: top.map((cd) => cd.naca) }, `${candidates.length} candidates, top 2 preselected`, 300);
    emit(S, 'candidates.proposed', { candidates }, x.where);
    emit(S, 'study.status', { status: 'awaiting_selection', mode: S.study.mode });
  }

  function buildPlan(S, nacas) {
    const req = S.study.requirements, re = Number(req.reynolds);
    const height = sig(firstCellHeight(re, 40), 3);
    const drag = (req.design_cl * req.design_cl / (4 * Math.PI * 30)).toFixed(5);
    const cases = nacas.map((naca) => {
      const cand = (S.study.candidates || []).find((cd) => cd.naca === naca);
      const alpha = cand && cand.xfoil && cand.xfoil.alpha_design != null ? cand.xfoil.alpha_design : 2.0;
      const angle = defaultCites('case.alpha_deg');
      return {
        naca, alpha_deg: alpha, cites: angle.length ? { alpha_deg: angle } : {},
        stages: {
          geometry: { naca, points_per_surface: 400, closed_trailing_edge: true,
            reasons: { naca: `NACA ${naca}: selected from the XFoil screen; the airfoil of a case cannot be changed after selection.`, points_per_surface: '400 cosine-spaced points resolve the leading-edge curvature for the mesh projection.' },
            checks: ['Profile closed', 'Thickness within 1 % of the NACA code', 'OBJ surface written'] },
          mesh: { farfield_chords: 30, wake_chords: 30, n_normal: 90, n_leading: 40, n_aft: 40, n_wake: 50, first_cell_height: height,
            reasons: {
              farfield_chords: '30 chords is the largest far field this C-mesh runs stably; the cost is a spurious drag of about ' + drag + '.',
              wake_chords: 'The outlet is 30 chords behind the trailing edge, the same distance as the far field.',
              first_cell_height: `First cell ${height} chord gives y+ about 40 at Re ${AS.fmtSci(re)} (wall functions, band 20-200).`,
              n_normal: '90 cells to the far field keep the wall-normal growth near 1.1; 23,400 cells in total.',
              n_leading: '40 cells from the leading edge to 30 % chord on each surface, as on the verified reference mesh.',
              n_aft: '40 cells from 30 % chord to the trailing edge on each surface, as on the verified reference mesh.',
              n_wake: '50 cells along the 30-chord wake, as on the verified reference mesh.' },
            checks: ['checkMesh without fatal errors', 'Max non-orthogonality below 70', 'Max skewness below 4', 'At most 80,000 cells', 'Predicted y+ between 20 and 200'] },
          physics: { turbulence_model: 'SpalartAllmaras', wall_treatment: 'wallFunction', nut_ratio: 4, turbulence_intensity: 0.001, farfield_bc: 'fixedInlet',
            reasons: {
              turbulence_model: `Attached flow at ${alpha.toFixed(2)} deg: Spalart-Allmaras is robust here and is the verified reference.`,
              wall_treatment: 'Wall functions keep the mesh small; they need y+ between 20 and 200.',
              // worded to hold under both models, as the engine's default reason: the engineer may switch the model and leave the number
              nut_ratio: 'Free-stream nuTilda is 4 times the viscosity, for a fully turbulent run (with k-omega SST the same number is nut / nu).',
              farfield_bc: 'fixedInlet is the stable far-field condition for a 30-chord domain.' },
            checks: ['All fields of the turbulence model exist in 0.orig', 'Velocity and Reynolds number consistent within 1 %'] },
          numerics: { iterations: 1200, write_interval: 400, convection_scheme: 'linearUpwind', relax_p: 0.3, relax_U: 0.7, relax_turbulence: 0.7,
            residual_p: 1e-6, residual_U: 1e-6, residual_turbulence: 1e-6, n_non_orthogonal_correctors: 0, consistent: false,
            reasons: {
              iterations: 'The reference case has steady forces by about iteration 750; 1200 leaves a margin. About 55 s per case.',
              convection_scheme: 'Second-order linearUpwind; first-order upwind would overstate drag by 10 to 30 %.',
              relax_p: 'Standard SIMPLE under-relaxation: 0.3 for pressure, 0.7 for momentum and turbulence.',
              relax_U: '0.7 for momentum, paired with 0.3 for pressure.',
              relax_turbulence: '0.7, as for momentum.',
              residual_p: 'A target of 1e-6 is tight enough that the iteration count and the force drift end the run, not an early residual stop.',
              residual_U: '1e-6, as for pressure: the force drift decides when the run is done.',
              residual_turbulence: '1e-6, as for pressure: the force drift decides when the run is done.',
              n_non_orthogonal_correctors: 'No correctors: this mesh stays well below the 60 degrees of non-orthogonality where they help.',
              consistent: 'Plain SIMPLE. SIMPLEC has not been verified for this set-up.' },
            checks: ['Dictionaries written', 'Iterations between 300 and 6000'] },
          solve: { max_wait_s: 30, extend_iterations: 400,
            reasons: { extend_iterations: 'If the forces still drift at the end, extend once by 400 iterations before asking.' },
            checks: ['No fatal error', 'Cl drift below 1e-3 and Cd drift below 1e-4 over the last 200 iterations'] },
          post: { fields: ['U', 'p'], views: ['airfoil', 'leading edge'],
            reasons: { fields: 'Velocity magnitude and pressure are enough to judge the suction peak and the wake.' },
            checks: ['Cl and Cd finite, Cd positive', 'Measured average y+ between 20 and 200', 'Cp extracted'] },
          validate: { transition: 'forced at 5 % chord', mach: 0, tolerance_cl_pct: 5, tolerance_cd_pct: 25,
            reasons: { transition: 'The RANS run is fully turbulent, so the reference must be tripped too.',
              mach: 'The RANS run is incompressible, so the reference is computed at Mach 0.' },
            checks: ['XFoil converged', 'pass: |dCl| <= 5 % and |dCd| <= 25 %', 'warn: |dCl| <= 10 % and |dCd| <= 50 %'] },
        },
      };
    });
    // Every value here is the planner's default, so each section cites the catalogue's default references
    // for the settings that have one. Names only this mock has (points_per_surface, max_wait_s, ...) get none.
    for (const pc of cases) for (const key in pc.stages) {
      const section = pc.stages[key], cites = {};
      for (const name in section) { const ids = defaultCites(key + '.' + name); if (ids.length) cites[name] = ids; }
      section.cites = cites;
    }
    const cond = S.study.conditions;
    return {
      version: 1, approved_at: null,
      summary: `One steady, incompressible, fully turbulent RANS case (simpleFoam, Spalart-Allmaras, 23,400-cell C-mesh) per airfoil: ` +
        cases.map((cs) => `NACA ${cs.naca} at ${cs.alpha_deg.toFixed(2)} deg`).join(' and ') +
        `. Both use identical mesh, physics and numerics so that differences come from the shape. Each case is followed by an XFoil reference at the same Reynolds number and angle. Expected run time about one minute per case, in parallel.`,
      assumptions: [
        `Incompressible solver: flight Mach ${req.mach} is not modelled; lift is understated by about the factor ${cond.compressibility_factor.toFixed(3)}.`,
        'Fully turbulent boundary layer (no transition): drag is an upper estimate for a clean surface; the XFoil reference uses forced transition for the same reason.',
        'One angle of attack per airfoil, chosen to give the design lift coefficient in XFoil. Stall margin and off-design behaviour are not assessed.',
        `Far field at 30 chords: expected spurious drag Cl^2/(4 pi 30) = ${drag}.`,
        '2D section, steady state, sharp closed trailing edge, wall functions.',
      ],
      cases,
    };
  }

  async function runPlanner(S, token, nacas, comment) {
    const x = new Ctx(S, token, 'planner');
    emit(S, 'study.status', { status: 'planning', mode: S.study.mode });
    if (!comment) {
      await x.say(`Writing the plan for ${nacas.map((n) => 'NACA ' + n).join(' and ')}. Nothing runs until you approve it.`);
      await x.tool('gather', 'get_defaults', {}, 'defaults for 7 stages loaded', 350);
      const height = sig(firstCellHeight(Number(S.study.requirements.reynolds), 40), 3);
      await x.tool('gather', 'estimate_first_cell', { target_yplus: 40 }, `first cell height ${height} chord for y+ 40`, 300);
      const plan = buildPlan(S, nacas);
      await x.tool('act', 'write_plan', { cases: plan.cases.length }, `plan v1 written: ${plan.cases.length} cases x 7 stages`, 500);
      S.study.plan = plan;
      emit(S, 'plan.created', { version: plan.version }, x.where);
      await x.say('The plan is ready for review. Edit any value in place, or tell me what to change.');
    } else {
      await x.say('Revising the plan according to your comment.');
      const plan = AS.clone(S.study.plan), changes = [];
      const iterations = /(\d{3,4})\s*iter/i.exec(comment);
      for (const cs of plan.cases) {
        const st = cs.stages;
        // A value changed on request has no reference behind it, so its citation goes. A study stored
        // before citations existed has no maps yet.
        for (const key in st) st[key].cites = st[key].cites || {};
        cs.cites = cs.cites || {};
        const planned = st.numerics.iterations;
        if (iterations) { st.numerics.iterations = Math.max(300, Math.min(6000, parseInt(iterations[1], 10))); st.numerics.reasons.iterations = 'Set on request of the engineer.'; }
        if (/finer|refine|more cells/i.test(comment)) {
          Object.assign(st.mesh, { n_normal: 110, n_leading: 60, n_aft: 60, n_wake: 60 });
          st.mesh.reasons.n_normal = 'Refined on request: 110 / 60 / 60 / 60 gives 39,600 cells.';
          // a reason describes the value beside it: the default wording names the default counts
          for (const name of ['n_leading', 'n_aft', 'n_wake']) st.mesh.reasons[name] = 'Refined on request, with n normal.';
          for (const name of ['n_normal', 'n_leading', 'n_aft', 'n_wake']) delete st.mesh.cites[name];
          if (st.numerics.iterations < 1600) { st.numerics.iterations = 1600; st.numerics.reasons.iterations = 'Raised to 1600 with the finer mesh.'; }
        }
        if (st.numerics.iterations !== planned) delete st.numerics.cites.iterations;
        if (/sst|k-?omega/i.test(comment)) {
          Object.assign(st.physics, { turbulence_model: 'kOmegaSST', nut_ratio: 1 });
          st.physics.reasons.turbulence_model = 'k-omega SST on request of the engineer; nut ratio 1 goes with it.';
          st.physics.reasons.nut_ratio = 'Set to 1 with k-omega SST; with this model the number is the free-stream nut / nu.';
          // the model has its own source; nut ratio 1 has none
          if (fx.refIds.has('menter-1994')) st.physics.cites.turbulence_model = ['menter-1994']; else delete st.physics.cites.turbulence_model;
          delete st.physics.cites.nut_ratio;
        }
      }
      if (iterations) changes.push(`iterations set to ${plan.cases[0].stages.numerics.iterations}`);
      if (/finer|refine|more cells/i.test(comment)) changes.push('mesh refined to 39,600 cells, iterations raised to at least 1600');
      if (/sst|k-?omega/i.test(comment)) changes.push('turbulence model changed to k-omega SST');
      if (!changes.length) plan.assumptions.push('Engineer note (no setting changed): ' + comment);
      plan.version += 1;
      plan.summary = `Revision ${plan.version}: ${changes.length ? changes.join('; ') : 'no setting changed; your note was added to the assumptions'}. ` + plan.summary.replace(/^Revision \d+: [^.]*\. /, '');
      await x.tool('act', 'write_plan', { cases: plan.cases.length }, `plan v${plan.version} written`, 500);
      S.study.plan = plan;
      emit(S, 'plan.updated', { version: plan.version }, x.where);
    }
    emit(S, 'study.status', { status: 'awaiting_approval', mode: S.study.mode });
  }

  // ---------- the seven stage scripts ----------
  function caseNumbers(S, c) {
    const index = S.study.case_ids.indexOf(c.id);
    const planCase = S.study.plan.cases.find((pc) => pc.naca === c.naca);
    const xf = xfoilAt(c.naca, c.alpha_deg);
    const weak = !TUTORIAL && String(S.study.model).toLowerCase().includes('haiku') && index === 1;   // the scripted "weaker model" behaviour
    const biasCl = [-0.6, 1.3, -1.0][index % 3] / 100, biasCd = (weak ? 26.4 : [14.5, 17.2, 16.0][index % 3]) / 100;
    const m = planCase.stages.mesh;
    // the first cell height of the mesh on disk: the Mesh hand-off records it when the agent
    // built with another value than the plan holds, and y+ downstream follows the mesh
    const built = (stageOf(c, 'mesh').metrics || {}).first_cell_height;
    return {
      index, plan: planCase.stages, xf, weak, re: Number(S.study.requirements.reynolds), cond: S.study.conditions,
      height: typeof built === 'number' ? built : m.first_cell_height,
      cfd: { cl: +(xf.cl * (1 + biasCl)).toFixed(4), cd: +(xf.cd * (1 + biasCd)).toFixed(5), cm: +(xf.cm * 1.012).toFixed(4) },
      cells: 2 * (m.n_leading + m.n_aft + m.n_wake) * m.n_normal,
    };
  }

  /* Recorded solver history, stretched to the planned iteration count and scaled to this case's coefficients. */
  function solverRow(S, c, iteration, total) {
    const n = caseNumbers(S, c), rec = fx.recorded.solver;
    const pick = (rows) => rows[Math.min(rows.length - 1, Math.round(iteration / total * (rows.length - 1)))];
    const f = pick(rec.forces), r = pick(rec.residuals), last = rec.forces[rec.forces.length - 1];
    const residuals = {};
    for (const k in r) if (k !== 'iter') residuals[k] = r[k];
    return { iteration, total, residuals, cl: +(n.cfd.cl * f.Cl / last.Cl).toFixed(5), cd: +(n.cfd.cd * f.Cd / last.Cd).toFixed(6),
      cm: +(n.cfd.cm * f.Cm / last.Cm).toFixed(5) };
  }

  const inBand = (yplus) => yplus >= 20 && yplus <= 200;
  // whole numbers, as the engine's tool summaries print y+; a value just outside the band must not read as the limit itself
  const shownYplus = (v) => (v > 200 ? Math.max(201, Math.round(v)) : v >= 20 ? Math.round(v) : v >= 10 ? Math.min(19, Math.round(v)) : sig(v, 2));

  /* The Mesh stage as a real run plays it when the engineer has set a first cell height outside the
     y+ band (check names, limits and failure wording are the engine's): the agent says so, builds
     with the value because it is the engineer's, the gate rejects it, and attempt 2 corrects the mesh. The plan keeps the engineer's
     value; the hand-off records the height that was built. Every mesh of the tutorial runs through
     here; in plain mock mode only a plan value outside the band does. */
  async function meshTutorial(x, n) {
    const m = n.plan.mesh, st = stageOf(x.c, 'mesh'), cells = AS.fmtInt(n.cells);
    const h = m.first_cell_height, h0 = sig(firstCellHeight(n.re, 40), 3);
    const outside = !inBand(yplusFor(n.re, h)), yp = shownYplus(yplusFor(n.re, h)), yp0 = shownYplus(yplusFor(n.re, h0));
    // Only a first attempt submits the value that fails. A later attempt, a re-run by the engineer
    // or a page reload at the failed gate goes straight to the corrected height.
    const badFirst = outside && st.attempts === 1 && !(x.c.counters.gate_failures > 0);
    const built = outside ? h0 : h, ypBuilt = outside ? yp0 : yp;
    const counts = { farfield_chords: m.farfield_chords, wake_chords: m.wake_chords, n_normal: m.n_normal, n_leading: m.n_leading, n_aft: m.n_aft, n_wake: m.n_wake };
    // the seven checks of the engine's mesh gate, with its names and limits
    const checks = (height) => [
      { name: 'mesh built from the current set-up', value: 'yes', limit: 'build_mesh ran after the last set_mesh_params', ok: true },
      { name: 'checkMesh ran without a fatal error', value: 'ok', limit: 'no fatal error', ok: true },
      { name: 'max non-orthogonality (deg)', value: 44.8, limit: '< 70', ok: true },
      { name: 'max skewness', value: 0.71, limit: '< 4', ok: true },
      { name: 'cells', value: n.cells, limit: '<= 80000', ok: n.cells <= 80000 },
      { name: 'predicted y+ of first cell centre', value: shownYplus(yplusFor(n.re, height)), limit: '20 to 200', ok: inBand(yplusFor(n.re, height)) },
      { name: 'check_mesh ran in this run of the stage', value: 'yes', limit: 'yes', ok: true }];
    x.engineWording = true;
    await x.say(`Building the C-mesh: far field ${m.farfield_chords} chords, ${cells} cells planned.`);
    await x.tool('gather', 'get_context', {}, 'geometry hand-off and mesh plan loaded', 300);
    await x.tool('gather', 'estimate_first_cell', { target_yplus: 40 }, `first cell ${h0} chord for y+ 40 at Re ${AS.fmtSci(n.re)}`, 300);
    if (badFirst) {
      const tall = h > h0, ratio = Math.round(tall ? h / h0 : h0 / h);
      await x.say(`The plan sets the first cell height to ${h} chord, set by the engineer. My estimate for y+ 40 is ${h0}, so this is about ${ratio} times too ${tall ? 'tall' : 'thin'} ` +
        `and should give y+ near ${yp}, outside the 20 to 200 band for wall functions. It is the engineer's value, so I build with it and let the check decide.`);
      await x.tool('gather', 'note', { text: `first_cell_height ${h} is the engineer's value; predicted y+ ${yp}, band 20 to 200` }, 'noted', 300);
      await x.tool('act', 'set_mesh_params', Object.assign({}, counts, { first_cell_height: h }),
        `${cells} cells planned · predicted y+ ${yp} · warning: outside the 20-200 band for the wall treatment`, 400);
      await x.tool('act', 'build_mesh', {}, `blockMesh + extrudeMesh ok · ${cells} cells`, 2600);
      await x.tool('verify', 'check_mesh', {}, `mesh ok · non-orthogonality 44.8 · skewness 0.71 · predicted y+ ${yp}`, 900);
      await x.say(`The mesh builds cleanly, but predicted y+ is ${yp}. I expect the gate to reject it; I submit it as set.`);
      await x.complete({ summary: `C-mesh with ${cells} cells, built with the engineer's first cell height of ${h} chord. Predicted y+ ${yp}.`, headline: '', checks: checks(h) });
      // The failed gate goes back to the same agent, without asking. In the tutorial the case
      // stands here first, with the failed gate still on the page (stage.started clears it).
      await x.at('mesh.gate_failed');
      x.emit('stage.started', { attempt: st.attempts + 1 });
      await x.say(`The gate did not pass: predicted y+ ${yp}, limit 20 to 200, two attempts left. The cause is the first cell height, so I set ${h0} chord, which gives y+ ${yp0}, and rebuild.`);
      await x.tool('act', 'set_mesh_params', { first_cell_height: h0 },
        `${cells} cells planned · predicted y+ ${yp0} · the mesh on disk was built from other values: build_mesh, then check_mesh`, 400);
    } else {
      if (outside) await x.say(`The plan sets the first cell height to ${h} chord, set by the engineer. That predicts y+ ${yp}, outside the 20 to 200 band for wall functions, so I build with ${h0} chord, which gives y+ ${yp0}.`);
      await x.tool('act', 'set_mesh_params', Object.assign({}, counts, { first_cell_height: built }), `${cells} cells planned · wall-normal growth 1.105 · predicted y+ ${ypBuilt}`, 400);
    }
    await x.tool('act', 'build_mesh', {}, `blockMesh + extrudeMesh ok · ${cells} cells`, 2600);
    await x.tool('verify', 'check_mesh', {}, `mesh ok · non-orthogonality 44.8 · skewness 0.71 · predicted y+ ${ypBuilt}`, 900);
    await x.tool('act', 'render_view', { mode: 'mesh', zoom: 'auto' }, 'snapshot saved', 700);
    x.emit('artifact.created', { kind: 'image', label: 'Mesh around the airfoil', url: 'fixtures/img/mesh_airfoil.png' });
    const start = `Structured C-mesh with ${cells} cells and the far field at ${m.farfield_chords} chords. `;
    const rows = [
      { param: 'farfield_chords', value: m.farfield_chords, reason: m.reasons.farfield_chords },
      { param: 'n_normal', value: m.n_normal, reason: m.reasons.n_normal },
      { param: 'n_leading / n_aft / n_wake', value: `${m.n_leading} / ${m.n_aft} / ${m.n_wake}`, reason: 'Planned surface and wake resolution, unchanged.',
        cites: x.planCites('n_leading', 'n_aft', 'n_wake') }];
    // A corrected height is the agent's own value, from estimate_first_cell, and carries that estimate's
    // references. A height applied as planned is left to the rule: the plan's ids, or none for the engineer's.
    const own = outside ? { cites: defaultCites('mesh.first_cell_height') } : null;
    const metrics = { cells: n.cells, max_non_orthogonality: 44.8, max_skewness: 0.71, max_aspect_ratio: 1840, predicted_yplus: ypBuilt, first_cell_height: built };
    if (outside) metrics.plan_first_cell_height = h;
    metrics.wall_normal_growth = 1.105;
    return x.complete({
      summary: !outside ? start + `First cell ${h} chord, predicted y+ ${yp}. checkMesh reports no errors; the high aspect ratio near the wall is expected for a boundary-layer mesh and does not fail the gate.`
        : badFirst ? start + `The first attempt used the engineer's first cell height of ${h} chord and failed the gate (predicted y+ ${yp}, band 20 to 200); the mesh was rebuilt with ${h0} chord (predicted y+ ${yp0}). checkMesh reports no errors.`
          : start + `The plan holds the engineer's first cell height of ${h} chord, which predicts y+ ${yp} (band 20 to 200); the mesh was built with ${h0} chord (predicted y+ ${yp0}). checkMesh reports no errors.`,
      headline: `${cells} cells · non-ortho 45`,
      decisions: [Object.assign({ param: 'first_cell_height', value: built, reason: !outside ? m.reasons.first_cell_height
        : `Deviates from the value set by the engineer (${h}): it ${badFirst ? `predicted y+ ${yp} and the gate rejected it` : `predicts y+ ${yp}, outside the 20 to 200 band`}. ${h0} chord gives y+ ${yp0} for wall functions.` }, own)].concat(rows),
      metrics, checks: checks(built),
    });
  }

  const STAGE = {
    async geometry(x, n) {
      const g = fx.recorded.airfoils[x.c.naca].geometry;
      await x.say(`Generating the NACA ${x.c.naca} section from the 4-digit equations and checking that it is closed and has the right thickness.`);
      await x.tool('gather', 'get_context', {}, 'requirements, conditions and plan section loaded', 300);
      await x.tool('act', 'generate_geometry', { naca: x.c.naca }, `${n.plan.geometry.points_per_surface} points per surface · OBJ written`, 900);
      await x.tool('verify', 'inspect_geometry', {}, g.description + ' · trailing edge closed', 500);
      return x.complete({
        summary: `${g.description}. Surface written with ${n.plan.geometry.points_per_surface} cosine-spaced points per side and a closed trailing edge; maximum thickness matches the code.`,
        headline: `${(g.thickness * 100).toFixed(0)} % thick · ${(g.max_camber * 100).toFixed(0)} % camber`,
        decisions: [{ param: 'points_per_surface', value: n.plan.geometry.points_per_surface, reason: n.plan.geometry.reasons.points_per_surface },
          { param: 'closed_trailing_edge', value: true, reason: 'The block mesh needs a sharp, closed trailing edge.' }],
        metrics: { thickness: g.thickness, max_camber: g.max_camber, camber_position: g.camber_position, z_max: g.z_max, z_min: g.z_min },
        checks: [{ name: 'profile closed', value: 'yes', limit: 'yes', ok: true },
          { name: 'thickness error', value: '0.2 %', limit: '< 1 %', ok: true }, { name: 'OBJ written', value: 'yes', limit: 'yes', ok: true }],
      });
    },

    async mesh(x, n) {
      const m = n.plan.mesh;
      // A plan value outside the y+ band (only the engineer sets one) fails this gate however often it
      // is retried. An agent corrects it on its next attempt; the scripted baseline has no agent and,
      // like the backend's, applies the plan every time.
      if (TUTORIAL || (x.model && !inBand(yplusFor(n.re, m.first_cell_height)))) return meshTutorial(x, n);
      let height = m.first_cell_height;
      await x.say(`Building the C-mesh: far field ${m.farfield_chords} chords, ${AS.fmtInt(n.cells)} cells planned.`);
      await x.tool('gather', 'get_context', {}, 'geometry hand-off and mesh plan loaded', 300);
      await x.tool('gather', 'estimate_first_cell', { target_yplus: 40 }, `first cell height ${sig(firstCellHeight(n.re, 40))} chord`, 300);
      if (n.weak && stageOf(x.c, 'mesh').attempts === 1) {
        // scripted mistake of the weaker model: trades the first cell for fewer cells, the gate catches it
        height = sig(m.first_cell_height * 5.4);
        await x.say(`I will use a larger first cell (${height} chord) to keep the wall-normal growth rate low.`);
        await x.tool('act', 'set_mesh_params', Object.assign({}, m, { first_cell_height: height, reasons: undefined, checks: undefined, cites: undefined }), 'blockMeshDict written', 400);
        await x.tool('act', 'build_mesh', {}, `blockMesh + extrudeMesh ok · ${AS.fmtInt(n.cells)} cells`, 2600);
        await x.tool('verify', 'check_mesh', {}, 'mesh ok · non-orthogonality 44.8 · skewness 0.71', 900);
        const yp = yplusFor(n.re, height);
        const passed = await x.complete({ summary: 'Mesh built.', headline: '', checks: [
          { name: 'checkMesh', value: 'ok', limit: 'no fatal error', ok: true },
          { name: 'max non-orthogonality', value: 44.8, limit: '< 70', ok: true },
          { name: 'max skewness', value: 0.71, limit: '< 4', ok: true },
          { name: 'cells', value: n.cells, limit: '<= 80000', ok: true },
          { name: 'predicted y+', value: +yp.toFixed(0), limit: '20 - 200', ok: false }] });
        if (!passed) {
          x.emit('stage.started', { attempt: 2 });
          await x.say(`The gate rejected the mesh: predicted y+ ${yp.toFixed(0)} is outside the wall-function band. Going back to the planned first cell height ${m.first_cell_height}.`);
          height = m.first_cell_height;
        }
      }
      await x.tool('act', 'set_mesh_params', { farfield_chords: m.farfield_chords, wake_chords: m.wake_chords, n_normal: m.n_normal, n_leading: m.n_leading,
        n_aft: m.n_aft, n_wake: m.n_wake, first_cell_height: height }, 'blockMeshDict written · wall-normal growth 1.105', 400);
      await x.tool('act', 'build_mesh', {}, `blockMesh + extrudeMesh ok · ${AS.fmtInt(n.cells)} cells`, 2600);
      await x.tool('verify', 'check_mesh', {}, 'mesh ok · non-orthogonality 44.8 · skewness 0.71 · max aspect ratio 1840', 900);
      await x.tool('act', 'render_view', { mode: 'mesh', zoom: 'auto' }, 'snapshot saved', 700);
      x.emit('artifact.created', { kind: 'image', label: 'Mesh around the airfoil', url: 'fixtures/img/mesh_airfoil.png' });
      const yp = yplusFor(n.re, height);
      return x.complete({
        summary: `Structured C-mesh with ${AS.fmtInt(n.cells)} cells, far field at ${m.farfield_chords} chords and a first cell of ${height} chord (predicted y+ ${yp.toFixed(0)}). ` +
          'checkMesh reports no errors; the high aspect ratio near the wall is expected for a boundary-layer mesh and does not fail the gate.',
        headline: `${AS.fmtInt(n.cells)} cells · non-ortho 45`,
        decisions: [
          { param: 'farfield_chords', value: m.farfield_chords, reason: m.reasons.farfield_chords },
          { param: 'first_cell_height', value: height, reason: m.reasons.first_cell_height },
          { param: 'n_normal', value: m.n_normal, reason: m.reasons.n_normal },
          { param: 'n_leading / n_aft / n_wake', value: `${m.n_leading} / ${m.n_aft} / ${m.n_wake}`, reason: 'Planned surface and wake resolution, unchanged.',
            cites: x.planCites('n_leading', 'n_aft', 'n_wake') }],
        metrics: { cells: n.cells, max_non_orthogonality: 44.8, max_skewness: 0.71, max_aspect_ratio: 1840, predicted_yplus: +yp.toFixed(1), wall_normal_growth: 1.105 },
        checks: [
          { name: 'checkMesh', value: 'ok', limit: 'no fatal error', ok: true },
          { name: 'max non-orthogonality', value: 44.8, limit: '< 70', ok: true },
          { name: 'max skewness', value: 0.71, limit: '< 4', ok: true },
          { name: 'cells', value: n.cells, limit: '<= 80000', ok: true },
          { name: 'predicted y+', value: +yp.toFixed(0), limit: '20 - 200', ok: yp >= 20 && yp <= 200 }],
      });
    },

    async physics(x, n) {
      const p = n.plan.physics, U = n.cond.velocity;
      await x.say(`Setting the flow state: ${U} m/s at ${x.c.alpha_deg.toFixed(2)} deg, ${p.turbulence_model} with ${p.wall_treatment}.`);
      await x.tool('gather', 'get_context', {}, 'conditions and mesh hand-off loaded', 300);
      await x.tool('act', 'set_physics', { turbulence_model: p.turbulence_model, wall_treatment: p.wall_treatment, nut_ratio: p.nut_ratio,
        turbulence_intensity: p.turbulence_intensity, farfield_bc: p.farfield_bc }, `0.orig written: U, p, nut, nuTilda · nu ${AS.fmtSci(n.cond.nu_sim)}`, 700);
      await x.tool('verify', 'inspect_boundary', {}, 'aerofoil: wall · inlet/outlet: fixedInlet pair · front/back: empty', 500);
      return x.complete({
        summary: `Free stream ${U} m/s at ${x.c.alpha_deg.toFixed(2)} deg on a unit chord with viscosity ${AS.fmtSci(n.cond.nu_sim)} m2/s, which gives Re ${AS.fmtSci(n.re)}. ` +
          `${p.turbulence_model} with wall functions; far field as fixed inlet and pressure outlet.`,
        headline: `${p.turbulence_model === 'SpalartAllmaras' ? 'SA' : 'k-w SST'} · U ${U} m/s`,
        decisions: [
          { param: 'turbulence_model', value: p.turbulence_model, reason: p.reasons.turbulence_model },
          { param: 'wall_treatment', value: p.wall_treatment, reason: 'The mesh was built for y+ about 40.' },
          { param: 'nut_ratio', value: p.nut_ratio, reason: 'Free-stream eddy-viscosity ratio for a fully turbulent run.' },
          { param: 'farfield_bc', value: p.farfield_bc, reason: p.reasons.farfield_bc }],
        metrics: { velocity: U, alpha_deg: x.c.alpha_deg, nu_sim: n.cond.nu_sim, reynolds: n.re },
        checks: [{ name: 'fields for ' + p.turbulence_model, value: 'U p nut nuTilda', limit: 'all present', ok: true },
          { name: 'Reynolds number error', value: '0.0 %', limit: '< 1 %', ok: true }, { name: 'far-field condition', value: p.farfield_bc, limit: 'fixedInlet', ok: true }],
      });
    },

    async numerics(x, n) {
      const s = n.plan.numerics;
      await x.say(`Writing the solver controls: ${s.iterations} iterations, ${s.convection_scheme} convection, relaxation ${s.relax_p} / ${s.relax_U}.`);
      await x.tool('gather', 'get_context', {}, 'plan section loaded', 300);
      await x.tool('act', 'set_numerics', { iterations: s.iterations, write_interval: s.write_interval, convection_scheme: s.convection_scheme, relax_p: s.relax_p,
        relax_U: s.relax_U, relax_turbulence: s.relax_turbulence }, 'controlDict, fvSchemes, fvSolution written', 600);
      await x.tool('verify', 'inspect_numerics', {}, `simpleFoam · endTime ${s.iterations} · forceCoeffs, yPlus and surface sampling active`, 400);
      return x.complete({
        summary: `simpleFoam for ${s.iterations} iterations with second-order ${s.convection_scheme} convection and relaxation factors ${s.relax_p} (p) and ${s.relax_U} (U, turbulence). ` +
          'Force coefficients, y+ and the surface pressure are sampled during the run.',
        headline: `${s.iterations} iterations · ${s.convection_scheme}`,
        decisions: [
          { param: 'iterations', value: s.iterations, reason: s.reasons.iterations },
          { param: 'convection_scheme', value: s.convection_scheme, reason: s.reasons.convection_scheme },
          { param: 'relax_p / relax_U', value: `${s.relax_p} / ${s.relax_U}`, reason: 'Standard SIMPLE relaxation, verified on the reference case.', cites: x.planCites('relax_p', 'relax_U') },
          { param: 'n_non_orthogonal_correctors', value: s.n_non_orthogonal_correctors, reason: 'Non-orthogonality 45 is below the level that needs correctors.' }],
        metrics: { iterations: s.iterations, write_interval: s.write_interval },
        checks: [{ name: 'dictionaries written', value: 'yes', limit: 'yes', ok: true }, { name: 'iterations', value: s.iterations, limit: '300 - 6000', ok: s.iterations >= 300 && s.iterations <= 6000 }],
      });
    },

    async solve(x, n) {
      const total = n.plan.numerics.iterations, sim = (x.S.sim[x.c.id] = { iter: 0, total });
      await x.say(`Starting simpleFoam for ${total} iterations. I will watch the residuals and the force coefficients, not only the residuals.`);
      await x.tool('gather', 'get_context', {}, 'numerics hand-off loaded', 300);
      await x.tool('act', 'start_solver', {}, `simpleFoam started · ${total} iterations`, 700);
      const t0 = Date.now(), held0 = x.heldMs, chunk = Math.ceil(total / 4), step = Math.max(5, Math.round(total / 48));
      // solver time without the time the case stood at a tutorial checkpoint
      const solverSeconds = () => +((Date.now() - t0 - (x.heldMs - held0)) / 1000 * SPEED).toFixed(1);
      let remarks = 0;
      while (sim.iter < total) {
        x.setPhase('act');
        x.emit('tool.call', { name: 'wait_solver', args: { max_wait_s: n.plan.solve.max_wait_s } });
        const target = Math.min(total, sim.iter + chunk), began = Date.now();
        let row = null;
        while (sim.iter < target && !x.hasMessage()) {     // a queued human message ends the wait early
          await x.sleep(380);
          sim.iter = Math.min(target, sim.iter + step);
          row = solverRow(x.S, x.c, sim.iter, total);
          x.emit('solver.progress', { iteration: row.iteration, total, residuals: row.residuals, cl: row.cl, cd: row.cd, seconds: solverSeconds() });
        }
        row = row || solverRow(x.S, x.c, sim.iter, total);
        x.emit('tool.result', { name: 'wait_solver', ok: true, ms: Date.now() - began,
          summary: `iteration ${sim.iter} / ${total} · Cl ${row.cl.toFixed(4)} · Cd ${row.cd.toFixed(5)} · ${sim.iter < total ? 'running' : 'finished'}` });
        await x.deliver();
        if (sim.iter < total && remarks === 0 && sim.iter >= total / 2) {
          remarks = 1;
          await x.say('Residuals are falling steadily and the lift coefficient has stopped overshooting; no change to the numerics needed.');
          await x.at('solve.half');     // tutorial checkpoint: the solver stands at half way, charts drawn
        }
      }
      x.setPhase('verify');
      const end = solverRow(x.S, x.c, total, total);
      return x.complete({
        summary: `simpleFoam ran ${total} iterations without errors. Over the last 200 iterations Cl moved by 3.1e-4 and Cd by 4.2e-5, inside the convergence limits. ` +
          `Final residuals: p ${AS.fmtSci(end.residuals.p)}, Ux ${AS.fmtSci(end.residuals.Ux)}.`,
        headline: `${total} it · Cl ${n.cfd.cl.toFixed(3)} · Cd ${n.cfd.cd.toFixed(4)}`,
        decisions: [{ param: 'extend_run', value: 'not needed', reason: 'Force drift was already below the limits at the planned end.' }],
        metrics: { iterations: total, cl: n.cfd.cl, cd: n.cfd.cd, cl_drift: 3.1e-4, cd_drift: 4.2e-5, residual_p: end.residuals.p, residual_Ux: end.residuals.Ux,
          solver_seconds: solverSeconds() },
        checks: [{ name: 'fatal error', value: 'none', limit: 'none', ok: true }, { name: 'force history', value: 'present', limit: 'present', ok: true },
          { name: 'Cl drift (last 200)', value: 3.1e-4, limit: '< 1e-3', ok: true }, { name: 'Cd drift (last 200)', value: 4.2e-5, limit: '< 1e-4', ok: true }],
      });
    },

    async post(x, n) {
      const yp = yplusFor(n.re, n.height) * 1.08;     // measured on the mesh that was built, not on the plan value
      await x.say('Extracting the force coefficients, y+ on the wall and the surface pressure distribution.');
      await x.tool('gather', 'get_context', {}, 'solve hand-off loaded', 300);
      await x.tool('act', 'compute_forces', {}, `Cl ${n.cfd.cl.toFixed(4)} · Cd ${n.cfd.cd.toFixed(5)} · Cm ${n.cfd.cm.toFixed(4)} · L/D ${(n.cfd.cl / n.cfd.cd).toFixed(1)}`, 500);
      await x.tool('act', 'compute_yplus', {}, `y+ average ${yp.toFixed(1)} · min ${(yp * 0.12).toFixed(1)} · max ${(yp * 2.1).toFixed(1)}`, 900);
      await x.tool('act', 'extract_cp', {}, 'Cp on 160 surface faces extracted', 500);
      x.S.sim[x.c.id].cp = true;
      await x.tool('act', 'render_view', { mode: 'field', field: 'U', zoom: 'auto' }, 'snapshot saved', 700);
      x.emit('artifact.created', { kind: 'image', label: 'Velocity magnitude', url: 'fixtures/img/field_U.png' });
      await x.tool('act', 'render_view', { mode: 'field', field: 'p', zoom: '-0.2,0.6,-0.3,0.3' }, 'snapshot saved', 700);
      x.emit('artifact.created', { kind: 'image', label: 'Pressure, leading edge', url: 'fixtures/img/field_p_zoom.png' });
      x.emit('artifact.created', { kind: 'data', label: 'Recorded XFoil and solver data (mock fixture)', url: 'fixtures/recorded.json' });
      return x.complete({
        summary: `Cl ${n.cfd.cl.toFixed(4)}, Cd ${n.cfd.cd.toFixed(5)}, Cm ${n.cfd.cm.toFixed(4)} (L/D ${(n.cfd.cl / n.cfd.cd).toFixed(1)}). Average wall y+ ${yp.toFixed(1)}, inside the wall-function band. ` +
          'The flow is attached over the whole section; the pressure distribution has been extracted for the comparison.',
        headline: `L/D ${(n.cfd.cl / n.cfd.cd).toFixed(1)} · y+ ${yp.toFixed(0)}`,
        decisions: [{ param: 'fields rendered', value: 'U, p', reason: n.plan.post.reasons.fields }],
        metrics: { cl: n.cfd.cl, cd: n.cfd.cd, cm: n.cfd.cm, ld: +(n.cfd.cl / n.cfd.cd).toFixed(2), yplus_avg: +yp.toFixed(1), yplus_min: +(yp * 0.12).toFixed(1), yplus_max: +(yp * 2.1).toFixed(1) },
        checks: [{ name: 'Cl, Cd finite and Cd > 0', value: 'yes', limit: 'yes', ok: true },
          { name: 'average y+', value: +yp.toFixed(1), limit: '20 - 200', ok: yp >= 20 && yp <= 200 }, { name: 'Cp extracted', value: 'yes', limit: 'yes', ok: true }],
      });
    },

    async validate(x, n) {
      const d = deltas(n);
      await x.say(`Running the XFoil reference at Re ${AS.fmtSci(n.re)}, ${x.c.alpha_deg.toFixed(2)} deg, transition forced at 5 % chord, Mach 0.`);
      await x.tool('gather', 'get_context', {}, 'post-processing hand-off loaded', 300);
      await x.tool('act', 'run_xfoil', { transition: 'forced', mach: 0 }, `XFoil converged · Cl ${n.xf.cl.toFixed(4)} · Cd ${n.xf.cd.toFixed(5)} · Cm ${n.xf.cm.toFixed(4)}`, 900);
      x.S.sim[x.c.id].xfoil = true;
      const mach = Number(x.S.study.requirements.mach);
      await x.tool('act', 'run_xfoil', { transition: 'forced', mach }, `at Mach ${mach}: Cl ${(n.xf.cl * n.cond.compressibility_factor).toFixed(4)} (compressibility effect, not used for the verdict)`, 900);
      await x.tool('verify', 'compare', {}, `dCl ${AS.fmtPct(d.cl_pct)} · dCd ${AS.fmtPct(d.cd_pct)} · verdict ${d.verdict}`, 400);
      return x.complete({
        summary: `Against XFoil under the same conditions the RANS lift differs by ${AS.fmtPct(d.cl_pct)} and the drag by ${AS.fmtPct(d.cd_pct)}: verdict ${d.verdict}. ` +
          `About ${(d.farfield_cd_estimate / n.xf.cd * 100).toFixed(0)} % of the drag difference is the expected effect of the 30-chord far field (${d.farfield_cd_estimate.toFixed(5)}). ` +
          `At the flight Mach number XFoil gives ${((n.cond.compressibility_factor - 1) * 100).toFixed(1)} % more lift; this is not modelled by the incompressible run.`,
        headline: `dCl ${AS.fmtPct(d.cl_pct)} · dCd ${AS.fmtPct(d.cd_pct)}`,
        decisions: [{ param: 'transition', value: 'forced at 5 % chord', reason: n.plan.validate.reasons.transition },
          { param: 'reference Mach', value: 0, reason: 'Like for like: the RANS run is incompressible.', cites: x.planCites('mach') }],
        metrics: { xfoil_cl: n.xf.cl, xfoil_cd: n.xf.cd, xfoil_cm: n.xf.cm, cl_pct: d.cl_pct, cd_pct: d.cd_pct, cm_abs: d.cm_abs,
          xfoil_cl_at_flight_mach: +(n.xf.cl * n.cond.compressibility_factor).toFixed(4), verdict: d.verdict },
        checks: [{ name: 'XFoil converged', value: 'yes', limit: 'yes', ok: true },
          { name: '|dCl|', value: Math.abs(d.cl_pct).toFixed(1) + ' %', limit: '<= 5 % (warn <= 10 %)', ok: Math.abs(d.cl_pct) <= 10 },
          { name: '|dCd|', value: Math.abs(d.cd_pct).toFixed(1) + ' %', limit: '<= 25 % (warn <= 50 %)', ok: Math.abs(d.cd_pct) <= 50 }],
      });
    },
  };

  function deltas(n) {
    const cl_pct = +((n.cfd.cl - n.xf.cl) / n.xf.cl * 100).toFixed(2), cd_pct = +((n.cfd.cd - n.xf.cd) / n.xf.cd * 100).toFixed(2);
    const verdict = Math.abs(cl_pct) <= 5 && Math.abs(cd_pct) <= 25 ? 'pass' : Math.abs(cl_pct) <= 10 && Math.abs(cd_pct) <= 50 ? 'warn' : 'fail';
    return { cl_pct, cd_pct, cm_abs: +Math.abs(n.cfd.cm - n.xf.cm).toFixed(4), ld_pct: +(((n.cfd.cl / n.cfd.cd) - n.xf.ld) / n.xf.ld * 100).toFixed(2),
      farfield_cd_estimate: +(n.cfd.cl * n.cfd.cl / (4 * Math.PI * n.plan.mesh.farfield_chords)).toFixed(6), verdict };
  }

  /* Gate mode for the whole study, or the per-case fallback after a failed stage (study.mode stays auto). */
  const gated = (S, cid) => S.study.mode === 'gate' || R(S).forceGate.has(cid);

  async function runCase(S, cid, fromKey, token, resumed, preApproved) {
    const c = S.cases[cid], rt = R(S);
    const where = (stage) => ({ case_id: cid, stage, agent: (AS.STAGE_DEFS.find((d) => d.key === stage) || {}).agent });
    /* Wait for the engineer. Resolves 'approved', or 'auto' when the mode was switched back to auto-run. */
    const approval = async (key, reason, failed) => {
      if (!(resumed && c.status === 'awaiting_approval')) {
        emit(S, 'approval.requested', { stage: key, reason }, where(key));
        emit(S, 'case.status', { status: 'awaiting_approval' }, { case_id: cid });
      }
      resumed = false;
      const answer = await new Promise((resolve, reject) => { rt.approvals[cid] = Object.assign(resolve, { failed: !!failed }); token.waiters.push(() => reject(CANCEL)); });
      delete rt.approvals[cid];
      if (answer !== 'auto') {
        emit(S, 'approval.granted', { stage: key }, where(key));
        intervention(S, 'gate_decision', where(key), !!failed);
      }
      emit(S, 'case.status', { status: 'running' }, { case_id: cid });
    };
    if (c.status === 'pending') emit(S, 'case.status', { status: 'running' }, { case_id: cid });
    // no fromKey: every stage had passed when the page was reloaded, only the case results are missing
    for (let i = fromKey ? KEYS.indexOf(fromKey) : KEYS.length; i < KEYS.length; i++) {
      const key = KEYS[i], st = stageOf(c, key);
      if (resumed && st.status === 'failed') {
        // page reload while a failed stage was waiting for its retry approval
        rt.forceGate.add(cid);
        await approval(key, st.failure || 'The gate did not pass.', true);
      } else if (gated(S, cid) && preApproved !== key) {
        await approval(key, `Gate mode: approve to run "${st.title}".`, false);
      }
      resumed = false; preApproved = null;
      const x = new Ctx(S, token, where(key).agent, c, key);
      emit(S, 'stage.started', { attempt: (st.attempts || 0) + 1 }, where(key));
      const passed = await STAGE[key](x, caseNumbers(S, c));
      if (!passed) {
        // Like the backend: the stage fails, a provisional metrics row is written, the case
        // waits for a retry approval (or a re-run, or a stop) and is gated from here on.
        const reason = x.failure || 'The gate did not pass.';
        emit(S, 'stage.failed', { reason }, where(key));
        recordRun(S, c, 'failed', key);
        rt.forceGate.add(cid);
        await approval(key, reason, true);
        preApproved = key;
        i -= 1;
      } else {
        await x.at(key + '.passed');     // tutorial checkpoint: the next stage has not started, so the page stays on this one
      }
    }
    const n = caseNumbers(S, c), d = deltas(n), yp = yplusFor(n.re, n.height) * 1.08;
    const end = solverRow(S, c, 1, 1);
    emit(S, 'case.completed', {
      results: { cl: n.cfd.cl, cd: n.cfd.cd, cm: n.cfd.cm, ld: +(n.cfd.cl / n.cfd.cd).toFixed(2), iterations: n.plan.numerics.iterations, forces_converged: true,
        residuals: end.residuals, yplus_avg: +yp.toFixed(1), yplus_min: +(yp * 0.12).toFixed(1), yplus_max: +(yp * 2.1).toFixed(1), cells: n.cells,
        solver_seconds: (stageOf(c, 'solve').metrics || {}).solver_seconds || null },
      reference: { transition: 'forced at 5 % chord', alpha: c.alpha_deg, mach: 0, cl: n.xf.cl, cd: n.xf.cd, cm: n.xf.cm, ld: n.xf.ld, converged: true },
      deltas: { cl_pct: d.cl_pct, cd_pct: d.cd_pct, cm_abs: d.cm_abs, ld_pct: d.ld_pct, farfield_cd_estimate: d.farfield_cd_estimate },
      verdict: d.verdict,
    }, { case_id: cid });
    emit(S, 'case.status', { status: 'done' }, { case_id: cid });
    recordRun(S, c, 'done', null);
    finishStudyIfIdle(S);
  }

  /* The study ends done if any case is done, otherwise failed or stopped;
     study.completed is always emitted and its report is null when no case finished. */
  function finishStudyIfIdle(S) {
    const cases = S.study.case_ids.map((id) => S.cases[id]);
    if (!cases.length || cases.some((c) => ['pending', 'running', 'awaiting_approval', 'paused'].includes(c.status))) return;
    if (cases.some((c) => c.status === 'done')) { spawn(S, '_study', (token) => runReporter(S, token)); return; }
    emit(S, 'study.completed', { report: null });
    emit(S, 'study.status', { status: cases.some((c) => c.status === 'failed') ? 'failed' : 'stopped', mode: S.study.mode });
  }

  async function runReporter(S, token) {
    const x = new Ctx(S, token, 'reporter');
    const req = S.study.requirements, done = S.study.case_ids.map((id) => S.cases[id]).filter((c) => c.status === 'done');
    await x.say('All cases have finished. Comparing them against the objective and against XFoil.');
    await x.tool('gather', 'get_results', {}, `${done.length} finished case${done.length === 1 ? '' : 's'}`, 400);
    const score = (c) => (req.objective === 'min_cd' ? -c.results.cd : req.objective === 'max_endurance' ? Math.pow(c.results.cl, 1.5) / c.results.cd : c.results.ld);
    const ranked = done.slice().sort((a, b) => score(b) - score(a)), best = ranked[0];
    const target = Number(req.target_ld) > 0 ? Number(req.target_ld) : null;
    const table = ranked.map((c) => Object.assign({ case_id: c.id, naca: c.naca, alpha_deg: c.alpha_deg, cl: c.results.cl, cd: c.results.cd, cm: c.results.cm, ld: c.results.ld,
      xfoil_cl: c.reference.cl, xfoil_cd: c.reference.cd, xfoil_ld: c.reference.ld, cl_pct: c.deltas.cl_pct, cd_pct: c.deltas.cd_pct, verdict: c.verdict },
    target == null ? {} : { meets_target_ld: c.results.ld >= target }));
    const recommendation = `NACA ${best.naca}: best "${AS.OBJECTIVES[req.objective] || req.objective}" among the simulated sections (L/D ${best.results.ld.toFixed(1)} at Cl ${best.results.cl.toFixed(3)}), validation verdict ${best.verdict}.`;
    const markdown = [
      `## Result`, ``,
      `**Recommendation: NACA ${best.naca}.** At the design point (Re ${AS.fmtSci(req.reynolds)}, design Cl ${req.design_cl}) it gives L/D ${best.results.ld.toFixed(1)} in the RANS run` +
        (ranked[1] ? `, against ${ranked[1].results.ld.toFixed(1)} for NACA ${ranked[1].naca}.` : '.'), ``,
      `| Airfoil | alpha (deg) | Cl | Cd | L/D | Cm | dCl vs XFoil | dCd vs XFoil | Verdict |`,
      `|---|---|---|---|---|---|---|---|---|`,
      ...table.map((r) => `| NACA ${r.naca} | ${r.alpha_deg.toFixed(2)} | ${r.cl.toFixed(4)} | ${r.cd.toFixed(5)} | ${r.ld.toFixed(1)} | ${r.cm.toFixed(4)} | ${AS.fmtPct(r.cl_pct)} | ${AS.fmtPct(r.cd_pct)} | ${r.verdict} |`), ``,
      `## Agreement with XFoil`, ``,
      `Lift agrees within ${Math.max(...table.map((r) => Math.abs(r.cl_pct))).toFixed(1)} %. Drag is ${Math.min(...table.map((r) => r.cd_pct)).toFixed(0)} to ${Math.max(...table.map((r) => r.cd_pct)).toFixed(0)} % above XFoil, ` +
        `which is the expected direction: the far field is truncated at 30 chords and the RANS boundary layer is turbulent from the leading edge.`, ``,
      ...(target == null ? [] : [`## Target L/D`, ``,
        `The mission asks for L/D of at least ${target} at the design lift coefficient. ` + table.map((r) => `NACA ${r.naca}: RANS ${r.ld.toFixed(1)} (${r.meets_target_ld ? 'met' : 'not met'}), ` +
          `XFoil ${r.xfoil_ld.toFixed(1)} (${r.xfoil_ld >= target ? 'met' : 'not met'})`).join('; ') + `. The target is reported, it does not exclude a section.`, ``]),
      `## What is not verified`, ``,
      `- One angle of attack per airfoil; stall margin and the rest of the polar were not simulated.`,
      `- Flight Mach ${req.mach} is not modelled by the incompressible solver.`,
      `- No mesh-independence study: both cases use the same 23,400-cell mesh.`,
      `- The ranking in drag relies on a fully turbulent boundary layer; with laminar flow the order could change.`,
    ].join('\n');
    await x.tool('act', 'write_report', { recommendation: 'NACA ' + best.naca }, 'report written', 400);
    emit(S, 'study.completed', { report: { markdown, recommendation, table } }, x.where);
    emit(S, 'study.status', { status: 'done', mode: S.study.mode });
  }

  /* What a row says about the outcome of its case, as opposed to what the case cost (the same list as
     store.OUTCOME_COLUMNS). A stopped re-run or retry keeps these from the row the case already had
     (recordRun). alpha_deg is here because the kept coefficients belong to the angle they were computed at. */
  const OUTCOME_COLUMNS = ['status', 'success', 'verdict', 'failed_stage', 'alpha_deg',
    'cells', 'yplus_avg', 'iterations', 'solver_converged', 'forces_converged', 'residual_max',
    'cl', 'cd', 'cm', 'ld', 'xfoil_cl', 'xfoil_cd', 'xfoil_cm', 'xfoil_ld', 'cl_err_pct', 'cd_err_pct', 'cm_err_abs'];

  /* One metrics row per case, replaced when the case ends again. `status` as the backend writes it:
     done; failed while a stage stands failed (`failedStage`), also when the case is then stopped;
     stopped otherwise. A stop does not erase an outcome: a case stopped during a re-run or a retry
     keeps the outcome columns of its earlier row, with the counters, tokens, cost and times of now.
     Only a case stopped before any result is left out of pass^k. */
  function recordRun(S, c, status, failedStage) {
    const st = S.study, req = st.requirements, project = db.projects.find((p) => p.id === st.project_id) || {};
    const ok = c.status === 'done', r = c.results || {}, ref = c.reference || {}, d = c.deltas || {};
    const seconds = {}, attempts = {};
    for (const stage of c.stages) { seconds[stage.key] = stage.seconds || 0; attempts[stage.key] = stage.attempts || 0; }
    const success = ok && (c.verdict === 'pass' || c.verdict === 'warn');
    const k = c.counters, u = c.usage;
    const clean = !k.rework_loops && !k.gate_failures && !k.human_interventions;
    const earlier = db.runs.find((row) => row.id === c.id || row.case_id === c.id);
    db.runs = db.runs.filter((row) => row.id !== c.id && row.case_id !== c.id);
    const row = {
      id: c.id, ts: nowIso(), project_id: st.project_id, project_name: project.name, study_id: st.id, study_title: st.title, case_id: c.id,
      model: st.model, mode: st.mode, naca: c.naca, alpha_deg: c.alpha_deg, mach: req.mach, reynolds: req.reynolds, density: req.density,
      design_cl: req.design_cl, objective: req.objective, cells: r.cells || null, yplus_avg: r.yplus_avg || null, iterations: r.iterations || null,
      solver_seconds: r.solver_seconds || null, wall_seconds: +AS.sum(Object.values(seconds)).toFixed(1), solver_converged: 0, forces_converged: ok ? 1 : 0,
      residual_max: r.residuals ? Math.max(...Object.values(r.residuals)) : null,
      cl: r.cl != null ? r.cl : null, cd: r.cd != null ? r.cd : null, cm: r.cm != null ? r.cm : null, ld: r.ld != null ? r.ld : null,
      xfoil_cl: ref.cl != null ? ref.cl : null, xfoil_cd: ref.cd != null ? ref.cd : null, xfoil_cm: ref.cm != null ? ref.cm : null, xfoil_ld: ref.ld != null ? ref.ld : null,
      cl_err_pct: d.cl_pct != null ? d.cl_pct : null, cd_err_pct: d.cd_pct != null ? d.cd_pct : null, cm_err_abs: d.cm_abs != null ? d.cm_abs : null,
      verdict: c.verdict || null, success: success ? 1 : 0, llm_calls: u.llm_calls, tool_calls: u.tool_calls, tokens_in: u.tokens_in, tokens_out: u.tokens_out,
      tokens_cached: u.tokens_cached, cost_usd: +u.cost_usd.toFixed(6), rework_loops: k.rework_loops, gate_failures: k.gate_failures,
      human_interventions: k.human_interventions, first_pass: success && clean ? 1 : 0,
      stage_seconds: JSON.stringify(seconds), stage_attempts: JSON.stringify(attempts), failed_stage: failedStage || null,
      // unknown is null, never a zero: the Dashboard then falls back to human_interventions
      status, help_interventions: AS.isNum(k.help_interventions) ? k.help_interventions : null,
    };
    if (status === 'stopped' && earlier && AS.isTrial(earlier)) {
      for (const key of OUTCOME_COLUMNS) row[key] = earlier[key] === undefined ? null : earlier[key];
      // a row from before the column: the status its outcome stands for
      row.status = earlier.status || (earlier.failed_stage ? 'failed' : 'done');
      // a kept pass is a first pass only while nothing was counted on the case since
      row.first_pass = AS.truthy(row.success) && clean ? 1 : 0;
    }
    db.runs.unshift(row);
    save();
  }

  // ---------- HTTP routes ----------
  class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const study = (sid) => { const S = db.studies[sid]; if (!S) throw new HttpError(404, `study ${sid} not found`); return S; };
  const caseOf = (S, cid) => { const c = S.cases[cid]; if (!c) throw new HttpError(404, `case ${cid} not found`); return c; };

  function newCase(S, planCase) {
    const id = `${S.study.id}_${planCase.naca}`;
    return {
      id, study_id: S.study.id, naca: planCase.naca, alpha_deg: planCase.alpha_deg, dir: `cases/st_${S.study.id}_${planCase.naca}`,
      status: 'pending', current_stage: 'geometry',
      stages: AS.STAGE_DEFS.map((d) => ({ key: d.key, title: d.title, agent: d.agent, status: 'pending', phase: null, attempts: 0, started_at: null, ended_at: null,
        seconds: null, headline: '', summary: '', decisions: [], metrics: {}, gate: null, artifacts: [] })),
      results: null, reference: null, deltas: null, verdict: null, viewer: null,
      usage: { llm_calls: 0, tool_calls: 0, tokens_in: 0, tokens_out: 0, tokens_cached: 0, cost_usd: 0 },
      counters: { rework_loops: 0, gate_failures: 0, human_interventions: 0, help_interventions: 0 },
    };
  }

  function steer(S, message) {
    // no stage agent is active for the addressed case: the steering agent answers
    const where = { case_id: message.case_id || null, stage: message.stage || null, agent: 'steering' };
    emit(S, 'human.message', { text: message.text, quote: message.quote || undefined, target_stage: message.stage || undefined }, where);
    intervention(S, 'steer', where);
    const x = new Ctx(S, { cancelled: false, waiters: [] }, 'steering', message.case_id ? S.cases[message.case_id] : null, null);
    x.where = where;
    (async () => {
      await x.llm();
      const c = message.case_id ? S.cases[message.case_id] : null;
      const text = !x.model ? 'The scripted baseline has no model to answer. Use "Re-run this stage" or edit the plan to change something.'
        : c ? `No stage agent is active for NACA ${c.naca} right now (status: ${c.status}). I have noted your comment. To act on it, re-run from the stage it concerns with an instruction, and I will pass it on.`
          : `Noted. Nothing is running at the moment (study status: ${S.study.status}). Edit the plan or approve it to continue.`;
      emit(S, 'steering.reply', { text }, where);
    })().catch(() => {});
  }

  const MESH_LIMITS = { farfield_chords: [5, 100], wake_chords: [5, 100], n_normal: [20, 300], n_leading: [10, 200], n_aft: [10, 200], n_wake: [10, 300], first_cell_height: [1e-7, 0.05] };
  const PLAN_ENUMS = { physics: { turbulence_model: ['SpalartAllmaras', 'kOmegaSST'], wall_treatment: ['wallFunction', 'resolved'], farfield_bc: ['fixedInlet', 'freestream'] },
    numerics: { convection_scheme: ['linearUpwind', 'upwind'] } };
  /* Hard errors that would make a stage fail for certain (same rules and wording as planning.validate_plan). */
  function validatePlan(plan) {
    const errors = [];
    for (const pc of plan.cases) {
      const tag = 'NACA ' + pc.naca, st = pc.stages || {}, mesh = st.mesh || {};
      if (pc.alpha_deg == null || !isFinite(Number(pc.alpha_deg))) errors.push(`${tag}: no angle of attack (the XFoil screen did not reach the design lift coefficient)`);
      else if (Number(pc.alpha_deg) < -10 || Number(pc.alpha_deg) > 20) errors.push(`${tag}: alpha_deg ${pc.alpha_deg} is outside -10 to 20 deg`);
      const cells = 2 * (Number(mesh.n_leading) + Number(mesh.n_aft) + Number(mesh.n_wake)) * Number(mesh.n_normal);
      if (!isFinite(cells)) errors.push(`${tag}: mesh settings are incomplete`);
      else {
        if (cells > 80000) errors.push(`${tag}: ${AS.fmtInt(cells)} cells is above the limit of 80,000`);
        for (const k in MESH_LIMITS) if (!(Number(mesh[k]) >= MESH_LIMITS[k][0] && Number(mesh[k]) <= MESH_LIMITS[k][1])) errors.push(`${tag}: mesh.${k} = ${mesh[k]} is outside ${MESH_LIMITS[k][0]} to ${MESH_LIMITS[k][1]}`);
      }
      for (const stage in PLAN_ENUMS) for (const k in PLAN_ENUMS[stage]) {
        if (!PLAN_ENUMS[stage][k].includes((st[stage] || {})[k])) errors.push(`${tag}: ${k} must be ${PLAN_ENUMS[stage][k].join(' or ')}`);
      }
      const it = (st.numerics || {}).iterations;
      if (!(Number.isInteger(it) && it >= 300 && it <= 6000)) errors.push(`${tag}: iterations must be between 300 and 6000`);
    }
    return errors;
  }
  /* A value as its plan field shows it (a number in full, 0.000679 and not 6.79e-4), so that the
     reason of an edit reads like the row it stands in. */
  const shownValue = (v) => (typeof v === 'number' ? String(v) : Array.isArray(v) ? v.join(', ') : AS.fmtValue(v));
  /* A setting changed by the engineer gets a reason that says so and loses its citation: the value
     is now the engineer's, and setting it back does not bring the reference back (planning.mark_engineer_edits). */
  function markEngineerEdits(oldPlan, newPlan) {
    for (const pc of newPlan.cases) {
      const was = (oldPlan.cases || []).find((x) => x.naca === pc.naca) || {}, before = was.stages || {};
      if (pc.cites && Number(was.alpha_deg) !== Number(pc.alpha_deg)) delete pc.cites.alpha_deg;
      for (const key in pc.stages) {
        const section = pc.stages[key], previous = before[key] || {};
        for (const name in section) {
          if (name === 'reasons' || name === 'checks' || name === 'cites' || !(name in previous) || JSON.stringify(previous[name]) === JSON.stringify(section[name])) continue;
          section.reasons = Object.assign({}, section.reasons, { [name]: `${AS.ENGINEER_MARK} (was ${shownValue(previous[name])}).` });
          if (section.cites) delete section.cites[name];
        }
      }
    }
  }

  /* The airfoils whose plan an edit changed (planning.plan_changes): their own case (the angle or a value
     of a stage section), or a value outside `cases`, which every case shares. A plan edit is help only
     for these. Citations and the version stamps are bookkeeping, not values. Keys are sorted, so the
     order in which the page lists them never reads as a change. */
  function planChanges(oldPlan, newPlan) {
    const canon = (v) => (Array.isArray(v) ? '[' + v.map(canon).join(',') + ']'
      : v && typeof v === 'object' ? '{' + Object.keys(v).filter((k) => v[k] !== undefined).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}'
        : JSON.stringify(v === undefined ? null : v));
    const without = (o, keys) => { const out = {}; for (const k in o || {}) if (!keys.includes(k)) out[k] = o[k]; return out; };
    const own = (pc) => {
      const out = without(pc, ['stages', 'cites']);
      out.stages = {};
      for (const key in pc.stages || {}) out.stages[key] = without(pc.stages[key], ['cites']);
      return canon(out);
    };
    const shared = (plan) => canon(without(plan, ['cases', 'version', 'approved_at']));
    const was = new Map((oldPlan.cases || []).map((pc) => [pc.naca, pc]));
    const every = shared(oldPlan) !== shared(newPlan);
    return new Set(newPlan.cases.filter((pc) => every || !was.has(pc.naca) || own(was.get(pc.naca)) !== own(pc)).map((pc) => pc.naca));
  }

  /* Send a case back to `stage` and run it again from there (the rerun route, and an
     angle-of-attack edit after the physics stage has run). */
  function rerunCase(S, c, stage, instruction, reason) {
    const rt = R(S);
    if (rt.procs[c.id]) rt.procs[c.id].cancel();
    else { rt.forceGate.delete(c.id); S.study.report = null; if (rt.procs._study) rt.procs._study.cancel(); }   // the pipeline had ended: a new one starts
    delete rt.approvals[c.id];
    rt.paused.delete(c.id);
    const index = KEYS.indexOf(stage), cells = (c.results || {}).cells;
    c.verdict = null; c.deltas = null; c.reference = null;
    if (index <= KEYS.indexOf('post')) c.results = cells && index > KEYS.indexOf('mesh') ? { cells } : null;
    if (index <= KEYS.indexOf('post') && S.sim[c.id]) S.sim[c.id].cp = false;
    if (S.sim[c.id]) S.sim[c.id].xfoil = false;
    intervention(S, 'rerun', { case_id: c.id, stage: stage });
    emit(S, 'stage.rework', { from: c.current_stage, to: stage, reason: reason || instruction || 'Re-run requested by the engineer.', by: 'human' }, { case_id: c.id, stage: stage });
    if (instruction) (rt.queues[c.id] = rt.queues[c.id] || []).push({ text: instruction, stage: stage });
    if (KEYS.indexOf(stage) <= KEYS.indexOf('solve')) S.sim[c.id] = { iter: 0, total: 0 };
    emit(S, 'case.status', { status: 'running' }, { case_id: c.id });
    if (S.study.status !== 'running') emit(S, 'study.status', { status: 'running', mode: S.study.mode });
    setTimeout(() => spawn(S, c.id, (t) => runCase(S, c.id, stage, t, false, stage)), 0);   // a human re-run is its own approval
  }

  /* The tutorial's own example. Only at this Reynolds number do the mock's formulas give the numbers
     of the real run the tutorial retells (planned 0.00129, typed 0.012, predicted y+ 372). The first
     sentence of the prompt has no decimal point: the study title is cut at the first full stop. */
  const TUTORIAL_PRESET = { id: 'tutorial', label: 'Tutorial: glider cruise',
    prompt: 'Tutorial: wing section for a motor glider. Mach 0.09 at about 1,000 m (density 1.112 kg/m3), Reynolds number 1.5 million, design lift coefficient 0.5. ' +
      'Maximise L/D, at least 12% thick, |Cm| no more than 0.10, target L/D of at least 46.',
    requirements: { mach: 0.09, density: 1.112, reynolds: 1.5e6, design_cl: 0.5, objective: 'max_ld', min_thickness: 0.12, max_abs_cm: 0.1, target_ld: 46, notes: '' } };
  const TUTORIAL_MODEL = 'anthropic/claude-sonnet-5-5';   // the script is written for it: scripted says nothing, Haiku adds its own mesh mistake

  /* The project of a live tutorial run (LIVE_RUN.project in js/tutorial.js). The live step list on the
     mock (steps=live) creates no project, so there this one is the default and the sample projects are
     not listed: the study lands in Tutorial, and the cards that name the project read as on a live run. */
  const TUTORIAL_PROJECT = { id: 'tutorial', name: 'Tutorial', description: 'Runs started by the guided tutorial. Kept apart so they do not count in your own project.' };
  const defaultProject = () => (LIVE_STEPS ? (db.projects.find((p) => p.name === TUTORIAL_PROJECT.name) || {}).id : null) || fx.config.default_project_id;
  const listedProjects = () => (LIVE_STEPS ? db.projects.filter((p) => !fx.sampleProjects.has(p.id)) : db.projects);

  // ---------- reference documents: the server's read-only GET /api/refs/doc/<id>, read once ----------
  const docs = new Map();
  async function refDoc(id) {
    if (!docs.has(id)) {
      const resp = await window.fetch('/api/refs/doc/' + encodeURIComponent(id), { headers: { Accept: 'application/json' } });
      if (!resp.ok) throw new HttpError(resp.status, 'there is no local document for this reference');
      docs.set(id, await resp.json());
    }
    return docs.get(id);
  }

  // ---------- previous runs of a similar case (GET /api/refs/runs, as citations.previous_runs) ----------
  const scalar = (v) => (['string', 'number', 'boolean'].includes(typeof v) ? v : null);   // a list or a map is not one value
  const numeric = (v) => (typeof v === 'number' ? (isFinite(v) ? v : null) : typeof v === 'string' && v.trim() !== '' && isFinite(Number(v)) ? Number(v) : null);
  // whether a plan case has this setting: its own entry, and not one of the maps beside the settings
  const hasSetting = (planCase, stage, name) => {
    const holder = stage === 'case' ? planCase : ((planCase || {}).stages || {})[stage];
    return !!holder && typeof holder === 'object' && Object.prototype.hasOwnProperty.call(holder, name) && !['reasons', 'checks', 'cites', 'stages'].includes(name);
  };
  /* The plan value of one setting, null when there is none ('case' is the case level: the angle of attack). */
  const planned = (planCase, stage, name) => (hasSetting(planCase, stage, name) ? scalar((stage === 'case' ? planCase : planCase.stages[stage])[name]) : null);
  /* The value one case really ran with: the decision its stage recorded for the setting (an agent may
     deviate from the plan after a failed gate), else the plan value. */
  function applied(c, planCase, stage, name) {
    const plan = planned(planCase, stage, name);
    if (stage === 'case') return c[name] != null ? scalar(c[name]) : plan;
    const record = stageOf(c, stage) || {};
    for (const d of (record.status === 'passed' ? record.decisions || [] : []).slice().reverse()) {
      if (!d || d.param !== name || scalar(d.value) == null) continue;
      return typeof plan === 'number' && numeric(d.value) != null ? numeric(d.value) : d.value;
    }
    return plan;
  }
  /* The case and plan behind one metrics row. A run of this session has its study in the database. A
     run of the fixture is a results row only: no plan was kept with it. Its plan here holds what the
     row itself records and nothing more: the airfoil and the angle of attack (columns of the row) and
     the settings it names in `settings` (where a run left the default). Any other setting has no
     value: a default plan rebuilt today would state a value that no one recorded. */
  const recorded = new WeakMap();   // fixture row -> { c, planCase }, built once
  function runRecord(row) {
    const S = db.studies[row.study_id], c = S && S.cases[row.case_id];
    if (c) return { c, planCase: ((S.study.plan || {}).cases || []).find((pc) => pc.naca === c.naca) || null, done: c.status === 'done', fixture: false };
    if (S) return null;                    // the study is here and the case is not: its record is gone
    if (!recorded.has(row)) {
      const planCase = { naca: row.naca, alpha_deg: row.alpha_deg, stages: { geometry: { naca: row.naca } } };
      for (const key in row.settings || {}) {
        const [stage, name] = key.split('.');
        if (stage === 'case') planCase[name] = row.settings[key]; else if (KEYS.includes(stage)) (planCase.stages[stage] = planCase.stages[stage] || {})[name] = row.settings[key];
      }
      recorded.set(row, { c: { id: row.case_id, naca: row.naca, alpha_deg: row.alpha_deg, stages: [] }, planCase });
    }
    return Object.assign({ done: !row.failed_stage && !!row.verdict, fixture: true }, recorded.get(row));
  }
  /* Which fixture runs count as similar: the fixture's runs were made with the studio's default
     turbulence model and wall treatment, and a row that left them would name its own in `settings`.
     This only decides whether a run is listed. It is never shown as the value a run used. */
  const FIXTURE_PHYSICS = { turbulence_model: 'SpalartAllmaras', wall_treatment: 'wallFunction' };
  // numbers exactly (to rounding), words without regard to case: 0.00129 and 0.0013 are different values
  function sameSetting(a, b) {
    if (a == null || b == null) return false;
    const x = typeof a === 'boolean' ? null : numeric(a), y = typeof b === 'boolean' ? null : numeric(b);
    if (x != null && y != null) return Math.abs(x - y) <= 1e-9 * Math.max(Math.abs(x), Math.abs(y));
    return String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
  }
  /* A value as one short word, as the server writes it in a summary: 0.00129, 1200, 1e-6, SpalartAllmaras. */
  function shortValue(v) {
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v !== 'number' || !isFinite(v)) return String(v);
    if (v === 0) return '0';
    const [mantissa, exponent] = v.toExponential(5).split('e');     // six significant digits
    return Number(exponent) < -4 || Number(exponent) >= 6 ? parseFloat(mantissa) + 'e' + Number(exponent) : String(parseFloat(v.toPrecision(6)));
  }
  // what follows the value in a run's one-line summary; other settings are named instead
  const RUN_UNITS = { first_cell_height: 'chord', farfield_chords: 'chords', wake_chords: 'chords', alpha_deg: 'deg' };
  /* One line for the pop-up: who ran what, the value, the outcome that value bears on, the verdict.
     `bare`: a fixture run that does not record the setting gives the outcome alone, with no words
     in the place of the value. */
  function runSummary(run, stage, name, bare) {
    const o = run.outcome;
    const parts = bare && run.value == null ? []
      : [run.value == null ? 'not recorded' : RUN_UNITS[name] ? `${shortValue(run.value)} ${RUN_UNITS[name]}` : `${name.replace(/_/g, ' ')} ${shortValue(run.value)}`];
    if ((name === 'first_cell_height' || name === 'wall_treatment') && AS.isNum(o.yplus_avg)) parts.push(`measured y+ ${o.yplus_avg.toFixed(0)}`);
    else if (stage === 'mesh' && AS.isNum(o.cells)) parts.push(`${AS.fmtInt(o.cells)} cells`);
    else if (name === 'iterations' && AS.isNum(o.iterations)) {
      // the count itself (as citations._run_summary): say it once, and name both only when the run was extended or cut
      if (numeric(run.value) == null) parts.splice(0, parts.length, `ran ${Math.trunc(o.iterations)} iterations`);
      else if (!sameSetting(run.value, o.iterations)) parts.splice(0, parts.length, `planned ${shortValue(run.value)}, ran ${Math.trunc(o.iterations)} iterations`);
    } else if ((stage === 'numerics' || stage === 'solve') && AS.isNum(o.iterations)) parts.push(`ran ${Math.trunc(o.iterations)} iterations`);
    else if (AS.isNum(o.cl_err_pct) && AS.isNum(o.cd_err_pct)) parts.push(`Cl error ${Math.abs(o.cl_err_pct).toFixed(1)}%, Cd error ${Math.abs(o.cd_err_pct).toFixed(0)}%`);
    parts.push(o.verdict || 'no verdict');
    return `NACA ${run.naca}, ${AS.isNum(run.reynolds) ? 'Re ' + shortValue(run.reynolds) : 'Re unknown'}, ${run.model ? AS.modelLabel(run.model) : 'unknown model'}: ` + parts.join(', ');
  }
  /* Up to three finished cases of other studies that ran a similar case: the same turbulence model
     and wall treatment, and a Reynolds number within a factor of two of the named study's. The same
     airfoil comes first, then the closest Reynolds number, then the most recent. An unknown study,
     case or setting gives an empty list. Nothing is written. */
  function similarRuns(setting, sid, cid) {
    const out = { setting, value: null, runs: [] };
    const S = db.studies[sid], cases = (S && S.study.plan && S.study.plan.cases) || [];
    if (!/^[a-z]+\.[A-Za-z_0-9]+$/.test(setting) || !cases.length) return out;
    const stage = setting.slice(0, setting.indexOf('.')), name = setting.slice(setting.indexOf('.') + 1);
    // before the approval there is no case record yet: the page then names the case by the id it
    // will get or by the airfoil (the server joins the id with '-', the mock with '_')
    const known = S.cases[cid];
    const mine = cases.find((pc) => (known ? pc.naca === known.naca : [pc.naca, `${sid}_${pc.naca}`, `${sid}-${pc.naca}`].includes(cid))) || (cases.length === 1 && !cid ? cases[0] : null);
    // a name the plan does not have is not a setting (the server knows the names from its own plan)
    if (!mine || !hasSetting(mine, stage, name)) return out;
    out.value = planned(mine, stage, name);
    const re = numeric(S.study.requirements.reynolds), physics = ['turbulence_model', 'wall_treatment'].map((k) => planned(mine, 'physics', k));
    if (!(re > 0) || physics.includes(null)) return out;
    const off = (row) => Math.abs(Math.log(row.reynolds / re));
    const rows = db.runs.filter((row) => row.study_id !== sid && AS.isNum(row.reynolds) && row.reynolds > 0 && row.reynolds / re >= 0.5 && row.reynolds / re <= 2)
      .sort((a, b) => ((String(a.naca) !== String(mine.naca)) - (String(b.naca) !== String(mine.naca))) || (off(a) - off(b)) || (String(a.ts || '') < String(b.ts || '') ? 1 : String(a.ts || '') > String(b.ts || '') ? -1 : 0));
    for (const row of rows) {
      const rec = runRecord(row);
      if (!rec || !rec.done) continue;     // not finished, or its record is gone
      const ran = (k) => { const v = applied(rec.c, rec.planCase, 'physics', k); return v == null && rec.fixture ? FIXTURE_PHYSICS[k] : v; };
      if (['turbulence_model', 'wall_treatment'].some((k, i) => !sameSetting(ran(k), physics[i]))) continue;
      // null for a fixture run that does not record this setting: then never "same value" (sameSetting)
      const value = applied(rec.c, rec.planCase, stage, name);
      const run = { study_id: row.study_id, study_title: row.study_title || (db.studies[row.study_id] || { study: {} }).study.title || '', case_id: rec.c.id, naca: String(rec.c.naca),
        model: row.model || '', ts: row.ts || '', reynolds: row.reynolds, alpha_deg: rec.c.alpha_deg, value, same_value: sameSetting(value, out.value),
        outcome: {} };
      for (const k of ['verdict', 'success', 'yplus_avg', 'cells', 'iterations', 'cl_err_pct', 'cd_err_pct', 'gate_failures', 'rework_loops']) run.outcome[k] = row[k] === undefined ? null : row[k];
      run.summary = runSummary(run, stage, name, rec.fixture);
      // The mock's own addition to the server's answer: a study finished in this session can be opened;
      // a run known only from the metrics fixture has no study behind it (as "open" in the Dashboard's run table).
      run.openable = !rec.fixture;
      out.runs.push(run);
      if (out.runs.length >= 3) break;
    }
    return out;
  }

  const routes = [
    ['GET', /^\/api\/config$/, () => Object.assign({}, fx.config, { default_project_id: defaultProject() },
      TUTORIAL ? { presets: [TUTORIAL_PRESET].concat(fx.config.presets) } : null)],
    ['GET', /^\/api\/refs$/, () => fx.refs],
    ['GET', /^\/api\/refs\/doc\/([^/]+)$/, (m) => refDoc(m[1])],
    // `case` is the case id; before the approval, '<study>-<naca>' or the airfoil code
    ['GET', /^\/api\/refs\/runs$/, (m, body, q) => similarRuns(q.get('setting') || '', q.get('study') || '', q.get('case') || '')],
    ['GET', /^\/api\/projects$/, () => ({ projects: listedProjects().map((p) => {
      const list = Object.values(db.studies).filter((S) => S.study.project_id === p.id);
      const last = db.runs.filter((r) => r.project_id === p.id).map((r) => r.ts).concat(list.map((S) => S.study.updated_at)).sort().pop();
      return Object.assign({}, p, { n_studies: list.length, last_run: last || null });
    }) })],
    ['POST', /^\/api\/projects$/, (m, body) => {
      if (!body.name || !body.name.trim()) throw new HttpError(400, 'a project needs a name');
      const p = { id: body.name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 24) + '-' + (++db.counter), name: body.name.trim(),
        description: body.description || '', created_at: nowIso() };
      db.projects.push(p); save();
      return p;
    }],
    ['GET', /^\/api\/projects\/([^/]+)\/studies$/, (m) => ({ studies: Object.values(db.studies).filter((S) => S.study.project_id === m[1]).map((S) => ({
      id: S.study.id, project_id: S.study.project_id, title: S.study.title, created_at: S.study.created_at, updated_at: S.study.updated_at,
      model: S.study.model, mode: S.study.mode, status: S.study.status, requirements: S.study.requirements,
      nacas: S.study.case_ids.map((id) => S.cases[id].naca), verdicts: Object.fromEntries(S.study.case_ids.map((id) => [S.cases[id].naca, S.cases[id].verdict])),
      recommendation: S.study.report ? S.study.report.recommendation : null, usage: S.study.usage,
    })).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)) })],
    ['POST', /^\/api\/studies$/, (m, body) => {
      const req = Object.assign({ mach: 0.15, density: 1.225, reynolds: 3e6, design_cl: 0.5, objective: 'max_ld', min_thickness: null, max_abs_cm: null, target_ld: null, notes: '' }, body.requirements || {});
      for (const k of ['mach', 'density', 'reynolds', 'design_cl']) if (!(Number(req[k]) > 0)) throw new HttpError(400, `requirements.${k} must be a positive number`);
      if (req.target_ld === '' || req.target_ld === undefined) req.target_ld = null;
      if (req.target_ld != null && !(Number(req.target_ld) > 0)) throw new HttpError(400, 'requirements.target_ld must be a positive number, or empty for no target');
      const id = 'm' + (++db.counter).toString(36).padStart(3, '0');
      // the first sentence of the prompt: a full stop inside a number ("Mach 0.15") does not end it
      // (at most 70 characters, cut with '...' as mission.study_title does)
      const sentence = (body.prompt || '').trim().split(/\.(?!\d)|\n/)[0].trim();
      const title = (sentence.length <= 70 ? sentence : sentence.slice(0, 67).trimEnd() + '...') || `Mach ${req.mach}, Re ${AS.fmtSci(req.reynolds)}, Cl ${req.design_cl}`;
      const S = { seq: 0, events: [], cases: {}, sim: {}, viewer: {}, study: {
        id, project_id: body.project_id || defaultProject(), title, created_at: nowIso(), updated_at: nowIso(), model: TUTORIAL ? TUTORIAL_MODEL : body.model || 'scripted',
        mode: body.mode || 'plan', status: 'draft', prompt: body.prompt || '', requirements: req, conditions: deriveConditions(req), candidates: [], plan: null,
        case_ids: [], report: null, usage: { llm_calls: 0, tokens_in: 0, tokens_out: 0, tokens_cached: 0, cost_usd: 0 } } };
      db.studies[id] = S;
      emit(S, 'study.status', { status: 'draft', mode: S.study.mode });
      return S.study;
    }],
    ['GET', /^\/api\/studies\/([^/]+)$/, (m) => { const S = study(m[1]); return { study: S.study, cases: S.study.case_ids.map((id) => S.cases[id]) }; }],
    ['POST', /^\/api\/studies\/([^/]+)\/suggest$/, (m) => { const S = study(m[1]); spawn(S, '_study', (t) => runAdvisor(S, t)); return { status: 202, body: { ok: true } }; }],
    ['POST', /^\/api\/studies\/([^/]+)\/select$/, (m, body) => {
      const S = study(m[1]), nacas = (body.nacas || []).map(String);
      if (!nacas.length) throw new HttpError(400, 'select at least one airfoil');
      for (const n of nacas) if (!(S.study.candidates || []).some((cd) => cd.naca === n)) throw new HttpError(400, `NACA ${n} is not one of the candidates`);
      for (const cd of S.study.candidates) cd.selected = nacas.includes(cd.naca);
      spawn(S, '_study', (t) => runPlanner(S, t, nacas, null));
      return { status: 202, body: { ok: true } };
    }],
    ['PATCH', /^\/api\/studies\/([^/]+)\/plan$/, (m, body) => {
      const S = study(m[1]), old = S.study.plan;
      if (!old) throw new HttpError(409, 'there is no plan to edit yet');
      if (['planning', 'suggesting'].includes(S.study.status)) throw new HttpError(409, `the plan cannot be edited while the study is ${S.study.status}`);
      if (!body.plan || !Array.isArray(body.plan.cases)) throw new HttpError(400, 'plan.cases is required');
      const plan = AS.clone(body.plan);
      // the airfoil of a case cannot be changed after selection: the code is put back
      plan.cases = old.cases.map((oc) => {
        const pc = plan.cases.find((x) => String(x.naca) === oc.naca) || AS.clone(oc);
        pc.naca = oc.naca;
        if (pc.stages && pc.stages.geometry) pc.stages.geometry.naca = oc.naca;
        if (pc.alpha_deg != null && pc.alpha_deg !== '') pc.alpha_deg = Number(pc.alpha_deg);
        return pc;
      });
      const errors = validatePlan(plan);
      if (errors.length) throw new HttpError(400, errors.join('; '));
      // Citations are never taken from the request: every map is the stored plan's. A plan stored
      // before citations existed has none, so its maps are empty and the study shows no marks.
      for (const pc of plan.cases) {
        const oc = old.cases.find((x) => x.naca === pc.naca) || {};
        pc.cites = keptCites(oc.cites, pc);
        for (const key in pc.stages) pc.stages[key].cites = keptCites(((oc.stages || {})[key] || {}).cites, pc.stages[key]);
        // The references of nut ratio are for the turbulence model the plan had. Under another model the
        // same number means something else, so they go with the model (as planning.section_cites).
        const physics = pc.stages.physics, before = (oc.stages || {}).physics || {};
        if (physics && physics.turbulence_model !== before.turbulence_model) delete physics.cites.nut_ratio;
      }
      markEngineerEdits(old, plan);
      // As Engine.patch_plan: the edit is help for the cases that have not ended (read here, before a
      // changed angle starts a finished case again) and whose plan it changed. An ended case keeps its
      // row, and an untouched one was not helped.
      const changed = planChanges(old, plan);
      const helped = S.study.case_ids.filter((cid) => !['done', 'failed', 'stopped'].includes(S.cases[cid].status) && changed.has(S.cases[cid].naca));
      // once the cases exist the plan cannot be approved again: the approval is kept
      S.study.plan = Object.assign(plan, { version: (old.version || 0) + 1, approved_at: S.study.case_ids.length ? old.approved_at : null });
      // a changed angle of attack reaches the case record at once
      const turned = [];
      for (const pc of plan.cases) {
        const c = Object.values(S.cases).find((x) => x.naca === pc.naca);
        if (c && c.alpha_deg !== pc.alpha_deg) { turned.push([c, c.alpha_deg, pc.alpha_deg]); c.alpha_deg = pc.alpha_deg; }
      }
      emit(S, 'plan.updated', { version: S.study.plan.version });
      // like the backend (Engine.alpha_changed): once the physics stage has run, everything from
      // there on was computed at the old angle. A running or finished case re-runs from physics;
      // a stopped or failed one is only marked and re-runs from physics when it is started again.
      for (const [c, was, now] of turned) {
        if (!['passed', 'running'].includes(stageOf(c, 'physics').status)) continue;
        const reason = `the angle of attack was changed from ${was} to ${now} deg in the plan: the boundary conditions, the solution and the comparison must be redone at the new angle`;
        if (R(S).procs[c.id] || c.status === 'done') { rerunCase(S, c, 'physics', null, reason); continue; }
        const from = KEYS.indexOf('physics'), cells = (c.results || {}).cells;
        for (const st of c.stages.slice(from)) if (st.status !== 'pending') { st.status = st.attempts || ['running', 'passed', 'failed'].includes(st.status) ? 'stale' : 'pending'; st.phase = null; }
        c.verdict = null; c.deltas = null; c.reference = null; c.results = cells ? { cells } : null;
        if (S.sim[c.id]) { S.sim[c.id].cp = false; S.sim[c.id].xfoil = false; }
        emit(S, 'stage.rework', { from: 'physics', to: 'physics', reason: reason + ' (when the case is run again)', by: 'human' }, { case_id: c.id, stage: 'physics' });
        intervention(S, 'rerun', { case_id: c.id, stage: 'physics' });
      }
      // one event per helped case; when the edit helps none it is still recorded, for no case
      if (helped.length) for (const cid of helped) intervention(S, 'plan_edit', { case_id: cid });
      else intervention(S, 'plan_edit');
      return S.study;
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/plan\/revise$/, (m, body) => {
      const S = study(m[1]);
      if (!body.comment || !body.comment.trim()) throw new HttpError(400, 'a comment is required');
      emit(S, 'human.message', { text: body.comment.trim() }, { agent: 'planner' });
      intervention(S, 'plan_edit');
      spawn(S, '_study', (t) => runPlanner(S, t, S.study.plan.cases.map((cs) => cs.naca), body.comment.trim()));
      return { status: 202, body: { ok: true } };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/approve$/, (m, body) => {
      const S = study(m[1]);
      if (!S.study.plan) throw new HttpError(409, 'there is no plan to approve');
      if (S.study.case_ids.length) throw new HttpError(409, 'the plan is already approved');
      const mode = body.mode === 'gate' ? 'gate' : 'auto';
      S.study.plan.approved_at = nowIso();
      S.study.mode = mode;
      emit(S, 'plan.approved', { version: S.study.plan.version, mode });
      for (const planCase of S.study.plan.cases) emit(S, 'case.created', { case: newCase(S, planCase) });
      emit(S, 'study.status', { status: 'running', mode });
      for (const cid of S.study.case_ids) spawn(S, cid, (t) => runCase(S, cid, 'geometry', t, false));
      return { status: 202, body: { ok: true } };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/mode$/, (m, body) => {
      const S = study(m[1]), rt = R(S);
      if (!['gate', 'auto'].includes(body.mode)) throw new HttpError(400, 'mode must be auto or gate');
      S.study.mode = body.mode;
      emit(S, 'study.status', { status: S.study.status, mode: body.mode });
      const live = S.study.case_ids.filter((id) => rt.procs[id]);
      if (live.length) for (const id of live) intervention(S, 'mode_change', { case_id: id });
      else intervention(S, 'mode_change');
      if (body.mode === 'auto') {
        // auto clears the per-case fallback; a case waiting at a gate (not at a failed stage) carries on
        rt.forceGate.clear();
        for (const id of live) { const resolve = rt.approvals[id]; if (resolve && !resolve.failed) resolve('auto'); }
      }
      return S.study;
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/message$/, (m, body) => {
      const S = study(m[1]), rt = R(S);
      if (!body.text || !body.text.trim()) throw new HttpError(400, 'the message is empty');
      const message = { text: body.text.trim(), case_id: body.case_id || null, stage: body.stage || null, quote: body.quote || null };
      const active = (key) => rt.procs[key] && !rt.approvals[key];
      const targets = message.case_id ? [message.case_id] : S.study.case_ids.filter((id) => active(id));
      if (message.case_id ? active(message.case_id) : targets.length) {
        for (const id of targets) (rt.queues[id] = rt.queues[id] || []).push(message);
      } else if (!S.study.case_ids.length && rt.procs._study) {
        (rt.queues._study = rt.queues._study || []).push(message);
      } else {
        setTimeout(() => steer(S, message), 300 / SPEED);
      }
      return { queued: true };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/control$/, (m, body) => {
      const S = study(m[1]), rt = R(S), ids = body.case_id ? [body.case_id] : S.study.case_ids.slice();
      if (!['pause', 'resume', 'stop'].includes(body.action)) throw new HttpError(400, 'action must be pause, resume or stop');
      intervention(S, body.action, { case_id: body.case_id || null });
      for (const id of ids) {
        const c = caseOf(S, id);
        if (body.action === 'pause' && (c.status === 'running')) { rt.paused.add(id); emit(S, 'case.status', { status: 'paused' }, { case_id: id }); }
        if (body.action === 'resume' && c.status === 'paused') { rt.paused.delete(id); emit(S, 'case.status', { status: 'running' }, { case_id: id }); }
        if (body.action === 'stop' && ['running', 'paused', 'awaiting_approval', 'pending'].includes(c.status)) {
          rt.paused.delete(id);
          if (rt.procs[id]) rt.procs[id].cancel();
          delete rt.approvals[id];
          rt.forceGate.delete(id);
          // like the backend: no event for the stage in flight; what had started is stale,
          // what was only waiting for its first approval is pending again
          for (const st of c.stages) if (st.status === 'running' || st.status === 'awaiting_approval') { st.status = st.attempts ? 'stale' : 'pending'; st.phase = null; }
          emit(S, 'case.status', { status: 'stopped' }, { case_id: id });
          // as Engine.control and store.run_status: a stage that stands failed is the outcome of the case, also
          // when it is then stopped. The stage in flight is not a failed stage: no stage failed, none is named.
          const failed = c.stages.find((st) => st.status === 'failed');
          recordRun(S, c, failed ? 'failed' : 'stopped', failed ? failed.key : null);
        }
      }
      const statuses = S.study.case_ids.map((id) => S.cases[id].status);
      if (body.action === 'stop') {
        if (!S.study.case_ids.length && rt.procs._study) { rt.procs._study.cancel(); emit(S, 'study.status', { status: 'stopped', mode: S.study.mode }); }
        else if (['running', 'paused'].includes(S.study.status)) finishStudyIfIdle(S);
      } else if (statuses.length) {
        const anyRunning = statuses.some((s) => ['running', 'awaiting_approval', 'pending'].includes(s));
        const next = anyRunning ? 'running' : statuses.includes('paused') ? 'paused' : S.study.status;
        if (next !== S.study.status) emit(S, 'study.status', { status: next, mode: S.study.mode });
      }
      return { ok: true };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/cases\/([^/]+)\/approve$/, (m) => {
      const S = study(m[1]); caseOf(S, m[2]);
      const resolve = R(S).approvals[m[2]];
      if (!resolve) throw new HttpError(409, 'this case is not waiting for approval');
      resolve();
      return { ok: true };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/cases\/([^/]+)\/rerun$/, (m, body) => {
      const S = study(m[1]), c = caseOf(S, m[2]);
      if (!KEYS.includes(body.stage)) throw new HttpError(400, `unknown stage ${body.stage}`);
      if (stageOf(c, body.stage).status === 'pending' && c.current_stage !== body.stage) throw new HttpError(409, `stage ${body.stage} has not run yet`);
      rerunCase(S, c, body.stage, body.instruction);
      return { ok: true };
    }],
    ['GET', /^\/api\/studies\/([^/]+)\/cases\/([^/]+)\/series$/, (m) => {
      const S = study(m[1]), c = caseOf(S, m[2]), sim = S.sim[c.id] || { iter: 0, total: 0 };
      const done = (key) => stageOf(c, key).status === 'passed';
      const rows = [];
      if (sim.total) {
        const step = Math.max(1, Math.round(sim.total / 300));
        for (let it = step; it <= sim.iter; it += step) rows.push(solverRow(S, c, it, sim.total));
      }
      const rec = fx.recorded.airfoils[c.naca];
      const shape = AS.naca4(c.naca, 80);
      return {
        residuals: { iterations: sim.iter, series: rows.map((r) => Object.assign({ iter: r.iteration }, r.residuals)) },
        forces: rows.map((r) => ({ iter: r.iteration, Cl: r.cl, Cd: r.cd, Cm: r.cm })),
        // "CFD" Cp of the mock: the recorded XFoil curve with a slightly weaker suction peak
        cp: sim.cp && done('post') ? { upper: rec.cp.upper.map(([x, cp]) => [x, +(cp * (0.965 + 0.03 * x)).toFixed(4)]), lower: rec.cp.lower.map(([x, cp]) => [x, +(cp * 0.985).toFixed(4)]) } : null,
        xfoil_cp: sim.xfoil ? rec.cp : null,
        geometry: done('geometry') || stageOf(c, 'geometry').status === 'stale' ? { upper: shape.upper, lower: shape.lower } : null,
      };
    }],
    ['GET', /^\/api\/studies\/([^/]+)\/cases\/([^/]+)\/viewer$/, (m) => {
      const S = study(m[1]), c = caseOf(S, m[2]);
      if (NO_VIEWER) throw new HttpError(503, 'ParaView viewer could not be started (mock mode with noviewer=1).');
      if (stageOf(c, 'mesh').status !== 'passed') throw new HttpError(409, 'The case has no mesh yet.');
      const state = (S.viewer[c.id] = S.viewer[c.id] || { mode: 'mesh', field: 'U', zoom: 'auto', time: 'latest', fields: ['U', 'p', 'nut', 'nuTilda'], times: [0], cells: 23400 });
      return { url: `fixtures/mock_viewer.html?case=${encodeURIComponent(c.id)}&naca=${c.naca}&alpha=${c.alpha_deg}`, state };
    }],
    ['POST', /^\/api\/studies\/([^/]+)\/cases\/([^/]+)\/viewer$/, (m, body) => {
      const S = study(m[1]), c = caseOf(S, m[2]);
      if (!S.viewer[c.id]) throw new HttpError(409, 'The viewer is not running for this case.');
      for (const k of ['mode', 'field', 'zoom', 'time']) if (body[k] != null) S.viewer[c.id][k] = body[k];
      try { viewerChannel.postMessage({ case: c.id, state: S.viewer[c.id] }); } catch (e) { /* no BroadcastChannel */ }
      save();
      return { state: S.viewer[c.id] };
    }],
    ['GET', /^\/api\/metrics\/runs$/, (m, body, q) => ({ runs: db.runs.filter((r) => (!q.get('project_id') || r.project_id === q.get('project_id')) &&
      (!q.get('model') || r.model === q.get('model')) && (!q.get('naca') || r.naca === q.get('naca'))).sort((a, b) => (a.ts < b.ts ? 1 : -1))
      // `settings` (see runRecord) is the fixture's own note, not a column of the metrics table
      .map((r) => (r.settings ? Object.assign({}, r, { settings: undefined }) : r)) })],
    ['GET', /^\/api\/metrics\/summary$/, (m, body, q) => {
      const rows = db.runs.filter((r) => !q.get('project_id') || r.project_id === q.get('project_id'));
      const grouped = (key) => {
        const groups = {};
        for (const r of rows) (groups[r[key] || 'unknown'] = groups[r[key] || 'unknown'] || []).push(r);
        // pass^k is a figure of one model: not of a geometry, and not of the totals
        const passK = (list) => (key === 'model' ? { pass_k: AS.passK(list) } : null);
        return Object.entries(groups).map(([name, list]) => Object.assign({ [key]: name }, AS.aggregateRuns(list), passK(list))).sort((a, b) => b.runs - a.runs);
      };
      const totals = AS.aggregateRuns(rows);
      totals.studies = new Set(rows.map((r) => r.study_id)).size;
      totals.models = Array.from(new Set(rows.map((r) => r.model))).sort();
      totals.nacas = Array.from(new Set(rows.map((r) => r.naca))).sort();
      return { by_model: grouped('model'), by_geometry: grouped('naca'), totals };
    }],
  ];

  let viewerChannel = null;
  try { viewerChannel = new BroadcastChannel('as-mock-viewer'); } catch (e) { viewerChannel = { postMessage() {} }; }

  async function mockFetch(url, options) {
    await ready;
    await wait(15 + Math.random() * 40);    // a little latency, so loading states are exercised
    const method = ((options && options.method) || 'GET').toUpperCase();
    const parsed = new URL(url, location.origin);
    const body = options && options.body ? JSON.parse(options.body) : {};
    let status = 404, payload = { error: `no mock route for ${method} ${parsed.pathname}` };
    for (const [verb, pattern, handler] of routes) {
      const match = verb === method && pattern.exec(parsed.pathname);
      if (!match) continue;
      try {
        const result = await handler(match, body, parsed.searchParams);   // a route that reads a fixture file is asynchronous
        if (result && result.status && result.body) { status = result.status; payload = result.body; }
        else { status = 200; payload = result; }
      } catch (e) {
        if (!(e instanceof HttpError)) console.error('mock route failed', e);
        status = e.status || 500; payload = { error: e.message };
      }
      break;
    }
    const text = JSON.stringify(payload);   // serialised here: the UI never shares objects with the mock
    return { ok: status < 400, status, statusText: '', text: async () => text, json: async () => JSON.parse(text) };
  }

  /* EventSource look-alike: replays stored events with seq > after, then streams live ones. */
  const openSources = new Set();
  class MockEventSource {
    constructor(url) {
      const parsed = new URL(url, location.origin);
      this.sid = parsed.pathname.split('/')[3];
      this.after = parseInt(parsed.searchParams.get('after') || '0', 10);
      this.readyState = 0;
      this.onopen = this.onmessage = this.onerror = null;
      this.listener = (json) => { if (this.readyState === 1 && this.onmessage) this.onmessage({ data: json }); };
      ready.then(() => setTimeout(() => this.start(), 20));
    }
    start() {
      if (this.readyState === 2) return;
      const S = db.studies[this.sid];
      if (!S || AS.mock.offline) { this.fail(); return; }
      this.readyState = 1;
      openSources.add(this);
      if (this.onopen) this.onopen({});
      const backlog = S.events.filter((ev) => ev.seq > this.after).map((ev) => JSON.stringify(ev));
      R(S).listeners.add(this.listener);
      // replay in slices so the page stays responsive, like a network stream would
      let i = 0;
      const pump = () => {
        if (this.readyState !== 1) return;
        for (const end = Math.min(backlog.length, i + 150); i < end; i++) if (this.onmessage) this.onmessage({ data: backlog[i] });
        if (i < backlog.length) setTimeout(pump, 0);
      };
      pump();
    }
    fail() {
      const S = db.studies[this.sid];
      if (S) R(S).listeners.delete(this.listener);
      openSources.delete(this);
      this.readyState = 0;
      if (this.onerror) this.onerror({});
    }
    close() {
      const S = db && db.studies[this.sid];
      if (S) R(S).listeners.delete(this.listener);
      openSources.delete(this);
      this.readyState = 2;
    }
  }

  // ---------- start-up ----------
  const ready = (async () => {
    const load = async (name) => (await window.fetch('fixtures/' + name)).json();
    // the reference catalogue may be missing: the mock then runs without citations, as the server does
    const [config, projects, recorded, runs, refs] = await Promise.all([load('config.json'), load('projects.json'), load('recorded.json'), load('metrics_runs.json'),
      window.fetch('/api/refs').then((r) => r.json()).catch(() => null)]);
    fx = { config, recorded, refs: refs && Array.isArray(refs.entries) ? refs
      : { version: 1, checked: '', error: 'the reference catalogue could not be loaded', entries: [], defaults: {} } };
    fx.refIds = new Set(fx.refs.entries.map((entry) => entry.id));
    fx.sampleProjects = new Set(projects.map((p) => p.id));
    let stored = null;
    try { stored = JSON.parse(sessionStorage.getItem(DB_KEY) || 'null'); } catch (e) { stored = null; }
    db = stored || { projects, studies: {}, runs, counter: 100 };
    // also on a stored database: the recorded tutorial and the live step list share it
    if (LIVE_STEPS && !db.projects.some((p) => p.name === TUTORIAL_PROJECT.name)) db.projects.push(Object.assign({ created_at: nowIso() }, TUTORIAL_PROJECT));
    // Resume whatever was in flight when the page was reloaded: each agent restarts its current step.
    for (const S of Object.values(db.studies)) {
      const st = S.study.status;
      if (st === 'suggesting') spawn(S, '_study', (t) => runAdvisor(S, t));
      else if (st === 'planning') spawn(S, '_study', (t) => runPlanner(S, t, (S.study.candidates || []).filter((cd) => cd.selected).map((cd) => cd.naca), null));
      else if (st === 'running' || st === 'paused') {
        for (const cid of S.study.case_ids) {
          const c = S.cases[cid];
          if (c.status === 'paused') R(S).paused.add(cid);
          if (['running', 'paused', 'awaiting_approval', 'pending'].includes(c.status)) {
            const stage = stageOf(c, c.current_stage);
            // same attempt again, not a new one; but an attempt whose gate had already failed is over
            // (a reload while the case stood at the tutorial's mesh.gate_failed checkpoint): the next one starts
            const gateFailed = stage && stage.gate && stage.gate.passed === false;
            if (stage && stage.status === 'running' && !gateFailed) stage.attempts = Math.max(0, (stage.attempts || 1) - 1);
            // a case that stood at a '<stage>.passed' checkpoint of the tutorial: that stage is done, the next one starts
            const from = stage && stage.status === 'passed' ? KEYS[KEYS.indexOf(stage.key) + 1] || null : c.current_stage || 'geometry';
            spawn(S, cid, (t) => runCase(S, cid, from, t, true));
          }
        }
        if (!Object.keys(R(S).procs).length) finishStudyIfIdle(S);
      }
    }
  })();

  AS.transport.fetch = mockFetch;
  AS.transport.EventSource = MockEventSource;
  Object.assign(AS.mock, {
    speed: SPEED, offline: false,
    /* console hooks: drop the event streams (they stay down while offline is true), inspect the mock database */
    dropConnections() { for (const source of Array.from(openSources)) source.fail(); },
    db: () => db,
    /* tutorial checkpoints (see the header); arming a name has an effect in tutorial mode only */
    hold(name) { holds.armed.add(String(name)); },
    release(name) { if (name == null) holds.armed.clear(); else holds.armed.delete(String(name)); },
    holding: () => Array.from(holds.standing, (entry) => Object.assign({}, entry)),
    /* the mock's flat-plate estimate, so the tutorial computes its numbers the way the mesh gate does */
    yplus: (reynolds, height) => yplusFor(Number(reynolds), Number(height)),
  });
})();
