/* Wing Studio - the study-level steps shown in the centre pane:
   Mission, Airfoil suggestion (candidates), Plan and Report. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, state = AS.state;
  const V = (AS.views = AS.views || {});

  // ---------- small shared pieces ----------
  const STATUS_TEXT = {
    draft: 'draft', suggesting: 'advisor working', awaiting_selection: 'waiting for your selection', planning: 'planner working',
    awaiting_approval: 'waiting for approval', running: 'running', paused: 'paused', done: 'done', failed: 'failed', stopped: 'stopped',
    pending: 'pending', passed: 'passed', stale: 'stale',
  };
  AS.ui = {
    statusText: (s) => STATUS_TEXT[s] || s || '',
    chip: (status, text) => h('span', { class: 'chip st-' + status }, h('i', { class: 'dot' }), text || AS.ui.statusText(status)),
    spinner: (label) => h('span', { class: 'working' }, h('i', { class: 'spinner', 'aria-hidden': 'true' }), label || 'working'),
    verdict: (v) => (v ? h('span', { class: 'verdict v-' + v }, AS.icon(v === 'pass' ? 'check' : v === 'warn' ? 'alert' : 'x', 12), v === 'pass' ? 'pass' : v === 'warn' ? 'warn' : 'fail') : h('span', { class: 'muted' }, '–')),
    kv: (pairs) => h('dl', { class: 'kv' }, pairs.filter(Boolean).map(([k, v, title]) => h('div', { title: title || null }, h('dt', {}, k), h('dd', {}, v)))),
    missionChip(req) {
      if (!req) return '';
      const parts = [];
      if (req.mach != null) parts.push('M ' + AS.fmt(req.mach));
      if (req.reynolds != null) parts.push('Re ' + AS.fmtSci(req.reynolds));
      if (req.density != null) parts.push('ρ ' + AS.fmt(req.density));
      if (req.design_cl != null) parts.push('Cl ' + AS.fmt(req.design_cl));
      if (req.objective) parts.push({ max_ld: 'max L/D', max_endurance: 'max endurance', min_cd: 'min Cd' }[req.objective] || req.objective);
      if (AS.isNum(req.target_ld) && req.target_ld > 0) parts.push('L/D ≥ ' + AS.fmt(req.target_ld));
      return parts.join(' · ');
    },
    /* Target L/D: met / not met as judged by the backend; a dash while it has not been judged. */
    target: (flag) => (flag === true ? h('span', { class: 'verdict v-pass target-flag' }, AS.icon('check', 12), 'met') : flag === false ? h('span', { class: 'verdict v-fail target-flag' }, AS.icon('x', 12), 'not met')
      : h('span', { class: 'muted', title: 'Not judged yet' }, '–')),
    /* A link into the guided tutorial (?tutorial=1, js/tutorial.js). Entering is a navigation, because
       the mock replaces the transport when its script loads. The address left behind is kept for this
       tab, so that leaving the tutorial returns here. Mode 'live' opens the same walkthrough on the
       real backend (?tutorial=live), where the mock stays off. */
    tutorialLink: (attrs, label, mode) => h('a', Object.assign({ href: location.pathname + (mode === 'live' ? '?tutorial=live' : '?tutorial=reset'), onclick: () => {
      try { sessionStorage.setItem('as.tutorial.return', location.search + location.hash); } catch (e) { /* private mode: leaving opens the plain page */ }
    } }, attrs), label),
  };
  /* The optional mission target (requirements.target_ld); null for studies without one. */
  AS.targetLd = function (study) {
    const v = study && study.requirements ? study.requirements.target_ld : null;
    return AS.isNum(v) && v > 0 ? v : null;
  };

  // =====================================================================
  // Mission
  // =====================================================================
  V.mission = (function () {
    const FIELDS = [
      { key: 'mach', label: 'Mach number', required: true },
      { key: 'density', label: 'Air density', unit: 'kg/m³', required: true },
      { key: 'reynolds', label: 'Reynolds number', required: true, sci: true },
      { key: 'design_cl', label: 'Design lift coefficient', unit: 'Cl', required: true },
      { key: 'min_thickness', label: 'Minimum thickness', unit: 't/c', hint: '0.12 = 12 % of chord' },
      { key: 'max_abs_cm', label: 'Pitching-moment limit', unit: '|Cm|', hint: 'leave empty for no limit' },
      { key: 'target_ld', label: 'Target L/D', unit: 'optional', positive: true, hint: 'judged against XFoil’s fully turbulent screening value and the RANS result' },
    ];
    let el, inputs = {}, promptBox, objectiveSel, notesBox, modelBox, keyNote, machWarn, submitBtn, derived, sidebar, projectBox, intro, chips, busy = false;
    let filledFor;           // study id the form was last filled from

    function value(key) {
      const raw = inputs[key].value.trim();
      return raw === '' ? null : Number(raw);
    }
    function showMachWarning() {
      const mach = value('mach');
      const over = AS.isNum(mach) && mach > 0.3;
      machWarn.hidden = !over;
      if (over) {
        const effect = mach < 1 ? ((1 / Math.sqrt(1 - mach * mach) - 1) * 100).toFixed(0) : null;
        machWarn.textContent = `Mach ${mach} is above 0.3. The CFD run is incompressible, so compressibility` +
          (effect ? ` (about ${effect} % on lift by the Prandtl-Glauert rule)` : '') + ' is not modelled. Treat the results as outside the solver’s range.';
      }
    }
    function fill(req, prompt) {
      req = req || {};
      for (const f of FIELDS) {
        const v = req[f.key];
        inputs[f.key].value = v == null ? '' : f.sci ? AS.fmtSci(Number(v), 4) : String(v);
        inputs[f.key].classList.remove('invalid');
      }
      objectiveSel.value = req.objective || 'max_ld';
      notesBox.value = req.notes || '';
      if (prompt != null) promptBox.value = prompt;
      showMachWarning();
    }
    function selectedModel() {
      const checked = AS.$('input[name=model]:checked', modelBox);
      return checked ? checked.value : null;
    }
    function read() {
      const requirements = { objective: objectiveSel.value, notes: notesBox.value.trim() };
      let ok = true;
      for (const f of FIELDS) {
        const v = value(f.key);
        const bad = (f.required && !(v > 0)) || (v != null && !isFinite(v)) || (f.key === 'mach' && v >= 1) || (v != null && v < 0) || (f.positive && v != null && !(v > 0));
        inputs[f.key].classList.toggle('invalid', !!bad);
        if (bad) ok = false;
        requirements[f.key] = v;
      }
      if (!ok) { AS.toast('Check the highlighted values: Mach (below 1), density, Reynolds number and design Cl are required; a target L/D, if given, must be above 0.', 'error'); return null; }
      let prompt = promptBox.value.trim();
      if (!prompt) {
        prompt = `Design point: Mach ${requirements.mach}, density ${requirements.density} kg/m3, Reynolds number ${AS.fmtSci(requirements.reynolds)}, ` +
          `design lift coefficient ${requirements.design_cl}. Objective: ${AS.OBJECTIVES[requirements.objective]}.` +
          (requirements.target_ld != null ? ` Target L/D at the design lift coefficient: at least ${requirements.target_ld}.` : '');
      }
      return { project_id: state.projectId, prompt, requirements, model: selectedModel(), mode: 'plan' };
    }
    async function submit(ev) {
      ev.preventDefault();
      if (busy) return;
      const payload = read();
      if (!payload) return;
      if (!payload.project_id) { AS.toast('Pick or create a project first.', 'error'); return; }
      if (!payload.model) { AS.toast('Pick a model.', 'error'); return; }
      AS.store.set('model', payload.model);
      busy = true; render();
      await AS.actions.startStudy(payload);
      busy = false; render();
    }

    function projectRow() {
      const projects = state.projects;
      const select = h('select', { id: 'project-select', 'aria-label': 'Project', onchange: (e) => AS.setProject(e.target.value) },
        projects.map((p) => h('option', { value: p.id, selected: p.id === state.projectId }, `${p.name}${p.n_studies != null ? ` (${p.n_studies})` : ''}`)));
      const form = h('form', { class: 'new-project', hidden: true, onsubmit: async (e) => {
        e.preventDefault();
        const name = e.target.elements.name.value.trim();
        if (!name) return;
        const p = await AS.actions.createProject(name, e.target.elements.description.value.trim());
        if (p) AS.toast(`Project "${name}" created.`, 'ok');
      } },
        h('input', { name: 'name', type: 'text', placeholder: 'Project name', 'aria-label': 'Project name', required: true, maxlength: 60 }),
        h('input', { name: 'description', type: 'text', placeholder: 'Description (optional)', 'aria-label': 'Project description', maxlength: 200 }),
        h('button', { type: 'submit', class: 'btn primary small' }, 'Create project'),
        h('button', { type: 'button', class: 'btn small', onclick: () => { form.hidden = true; } }, 'Cancel'));
      const project = AS.project();
      return [h('div', { class: 'row' }, h('label', { class: 'field-label', for: 'project-select' }, 'Project'), select,
        h('button', { type: 'button', class: 'btn small', id: 'new-project-btn', onclick: () => { form.hidden = !form.hidden; if (!form.hidden) form.elements.name.focus(); } }, 'New project'),
        project && project.description ? h('span', { class: 'muted small' }, project.description) : null), form];
    }

    function studiesPanel() {
      const project = AS.project();
      return [h('h3', {}, 'Studies in ' + (project ? project.name : 'this project')),
        state.studies.length ? h('ul', { class: 'study-list' }, state.studies.map((s) => h('li', {},
          h('button', { type: 'button', class: 'study-item' + (state.study && state.study.id === s.id ? ' current' : ''), onclick: () => AS.openStudy(s.id) },
            h('span', { class: 'study-title' }, s.title || s.id),
            h('span', { class: 'study-meta' }, AS.ui.chip(s.status), ' ', AS.modelLabel(s.model),
              s.nacas && s.nacas.length ? ' \u00b7 NACA ' + s.nacas.join(', ') : '', ' \u00b7 ', AS.fmtDate(s.created_at))))))
          : h('p', { class: 'muted small' }, 'No studies yet. Describe a mission to start the first one.')];
    }

    function derivedPanel() {
      const s = state.study, c = s && s.conditions;
      if (!c) return h('p', { class: 'muted small' }, 'Velocity, chord, dynamic pressure and temperature are derived once the study exists.');
      const items = [
        ['Velocity', AS.fmt(c.velocity) + ' m/s'],
        ['Chord', AS.fmt(c.chord_m) + ' m', 'Chord that gives this Reynolds number at this speed and density'],
        ['Dynamic pressure', AS.fmt(c.dynamic_pressure_Pa) + ' Pa'],
        ['Temperature', AS.fmt(c.temperature_K) + ' K', 'Standard-atmosphere temperature at this density'],
        c.speed_of_sound != null ? ['Speed of sound', AS.fmt(c.speed_of_sound) + ' m/s'] : null,
        c.kinematic_viscosity != null ? ['Kinematic viscosity', AS.fmtSci(c.kinematic_viscosity) + ' m²/s'] : null,
        c.compressibility_factor != null ? ['Compressibility factor', AS.fmt(c.compressibility_factor, 3), '1 / sqrt(1 - M²): lift not modelled by the incompressible run'] : null,
      ];
      return h('div', { dataset: { stage: 'mission' } }, AS.ui.kv(items),
        (c.warnings || []).map((w) => h('p', { class: 'note warn' }, w)));
    }

    function build() {
      promptBox = h('textarea', { id: 'mission-prompt', rows: 2, placeholder: 'Describe the flight condition and what the section should be good at, or pick an example below.' });
      chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Example missions' });
      const grid = h('div', { class: 'form-grid' });
      for (const f of FIELDS) {
        const input = h('input', { id: 'f-' + f.key, type: 'text', inputmode: 'decimal', autocomplete: 'off', 'aria-required': f.required ? 'true' : null,
          oninput: f.key === 'mach' ? showMachWarning : null });
        inputs[f.key] = input;
        grid.appendChild(h('label', { class: 'field', for: 'f-' + f.key }, h('span', { class: 'field-label' }, f.label, f.unit ? h('em', {}, f.unit) : null), input,
          f.hint ? h('span', { class: 'field-hint' }, f.hint) : null));
      }
      objectiveSel = h('select', { id: 'f-objective' }, Object.entries(AS.OBJECTIVES).map(([k, label]) => h('option', { value: k }, label)));
      grid.appendChild(h('label', { class: 'field', for: 'f-objective' }, h('span', { class: 'field-label' }, 'Objective'), objectiveSel));
      notesBox = h('input', { id: 'f-notes', type: 'text', placeholder: 'anything else the advisor should respect' });
      grid.appendChild(h('label', { class: 'field', for: 'f-notes' }, h('span', { class: 'field-label' }, 'Notes'), notesBox));
      machWarn = h('p', { class: 'note warn', id: 'mach-warning', role: 'alert', hidden: true });
      modelBox = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Model' });
      submitBtn = h('button', { type: 'submit', class: 'btn primary', id: 'suggest-btn' }, 'Suggest airfoils');
      intro = h('div', {});
      // the way into the guided tutorial, recorded or live; not offered inside either
      const invite = AS.mock.tutorial || AS.mock.live ? null : h('p', { class: 'muted small tour-invite' }, 'First time here? ',
        AS.ui.tutorialLink({ id: 'tutorial-link' }, 'Take the guided tutorial'), '. It replays a recorded study in the page and costs nothing.',
        ' Or ', AS.ui.tutorialLink({ id: 'tutorial-live-link' }, 'run it live', 'live'), ': real agents and a real solver, about 0.10 USD with Haiku 5.5.');
      derived = h('div', { class: 'panel' });
      sidebar = h('div', { class: 'mission-side' });
      projectBox = h('div', { class: 'project-row' });
      keyNote = h('div', {});
      el = h('div', { class: 'view view-mission', dataset: { stage: 'mission' } },
        h('form', { class: 'mission-main', onsubmit: submit, novalidate: true },
          h('h2', {}, 'Mission'), intro, invite, projectBox,
          h('label', { class: 'field-label', for: 'mission-prompt' }, 'What is the section for?'), promptBox, chips,
          h('h3', {}, 'Design point'), grid, machWarn,
          h('h3', {}, 'Model'),
          h('div', { class: 'row' }, modelBox, submitBtn), keyNote,
          h('p', { class: 'muted small model-hint' }, 'To compare models, run the same mission once per model (for example Haiku first, then Sonnet). The Dashboard tab then shows accuracy, time, tokens and cost side by side.'),
          h('h3', {}, 'Derived quantities'), derived),
        sidebar);
      fill({ mach: 0.15, density: 1.225, reynolds: 3e6, design_cl: 0.5, objective: 'max_ld', min_thickness: 0.12, max_abs_cm: 0.1 }, '');
      return el;
    }

    function render() {
      const s = state.study, cfg = state.config || {};
      // fill the form when another study is opened (never while the user is typing in the same study)
      const id = s ? s.id : null;
      if (id !== filledFor) {
        filledFor = id;
        if (s) {
          fill(s.requirements, s.prompt || '');
          const radio = AS.$$('input[name=model]', modelBox).find((r) => r.value === s.model);
          if (radio) radio.checked = true;
        }
      }
      // data-tour names the example for the tutorial overlay (js/tutorial.js), which points at one of them
      AS.region(chips, cfg.presets || [], () => (cfg.presets || []).map((p) => h('button', { type: 'button', class: 'chip-btn', dataset: { tour: 'preset:' + (p.id || '') }, onclick: () => {
        fill(p.requirements, p.prompt || '');
        promptBox.focus();
      } }, p.label || p.id)));
      AS.region(modelBox, [cfg.models, s && s.id], () => {
        const preferred = (s && s.model) || AS.store.get('model', null);
        const ids = (cfg.models || []).map(AS.modelId);
        const current = ids.includes(preferred) ? preferred : ids[0];
        return (cfg.models || []).map((m) => {
          const mid = AS.modelId(m);
          return h('label', { class: 'seg', title: mid === 'scripted' ? 'No LLM: every stage applies the plan defaults. Baseline for comparison.' : mid },
            h('input', { type: 'radio', name: 'model', value: mid, checked: mid === current }), h('span', {}, AS.modelLabel(m)));
        });
      });
      // A server without an API key can still run the Scripted model; say so before a model call fails.
      // (An older server or the sample data sends no llm_ready: nothing is said then.)
      AS.region(keyNote, [cfg.llm_ready], () => (cfg.llm_ready === false
        ? h('p', { class: 'note warn' }, 'No API key is set on this server, so only Scripted can run. Put ANTHROPIC_API_KEY in .env and restart wing-studio to use a model.')
        : null));
      const existing = s && s.status !== 'draft';
      AS.region(intro, [existing, s && s.status, s && s.title], () => (existing
        ? h('p', { class: 'note info' }, `These are the values of the open study "${AS.studyTitle(s) || s.id}" (${AS.ui.statusText(s.status)}). `,
          'Change the model or any value and start a new study to compare; the open study is kept in the project list.')
        : h('p', { class: 'muted' }, 'State the flight condition. The advisor proposes NACA 4-digit sections and screens them in XFoil; nothing is simulated before you approve a plan.')));
      submitBtn.disabled = busy;
      submitBtn.textContent = busy ? 'Starting…' : existing ? 'Start a new study with these values' : 'Suggest airfoils';
      AS.region(derived, [s && s.conditions], derivedPanel);
      AS.region(projectBox, [state.projects, state.projectId], projectRow);
      AS.region(sidebar, [state.projectId, AS.project() && AS.project().name, state.studies, s && s.id], studiesPanel);
    }

    /* Same mission, next model: opens the form of the open study with the following
       model preselected (Haiku -> Sonnet -> Opus), ready to start as a new study. */
    AS.runWithAnotherModel = function () {
      const s = state.study;
      AS.select({ kind: 'step', key: 'mission' });
      if (!s) return;
      fill(s.requirements, s.prompt || '');
      const ids = ((state.config && state.config.models) || []).map(AS.modelId).filter((m) => m !== 'scripted');
      const next = ids[(ids.indexOf(s.model) + 1) % (ids.length || 1)];
      const radio = AS.$$('input[name=model]', modelBox).find((r) => r.value === next);
      if (radio && next !== s.model) {
        radio.checked = true;
        AS.toast(`Model set to ${AS.modelLabel(next)}. Press "Start a new study with these values" to run the same mission with it.`, 'info');
      }
      requestAnimationFrame(() => submitBtn.focus());
    };

    return { build, render };
  })();

  // =====================================================================
  // Airfoil suggestion: candidate cards, L/D chart, confirm
  // =====================================================================
  V.suggest = (function () {
    let el, head, cards, chartHost, chart, foot, picks = new Set(), picksFor = '', busy = false;

    function thumb(naca) {
      if (!/^\d{4}$/.test(String(naca))) return null;
      const g = AS.naca4(naca, 40);
      const pts = g.upper.concat(g.lower.slice().reverse()).map(([x, y]) => `${(x * 200 + 4).toFixed(1)},${(34 - y * 200).toFixed(1)}`).join(' ');
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 208 56'); svg.setAttribute('class', 'thumb'); svg.setAttribute('aria-hidden', 'true');
      const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      poly.setAttribute('points', pts);
      svg.appendChild(poly);
      return svg;
    }
    function canPick() { return state.study && state.study.status === 'awaiting_selection'; }

    function updateChart() {
      const cands = ((state.study && state.study.candidates) || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
      const series = [], markers = [];
      cands.forEach((cd, i) => {
        const polar = (cd.xfoil && cd.xfoil.polar) || [];
        const color = AS.chart.SERIES[i % AS.chart.SERIES.length];
        const on = picks.has(cd.naca);
        series.push({ name: 'NACA ' + cd.naca, color, muted: !on, width: on ? 2.5 : 1.5,
          points: polar.map((p) => [p.alpha, p.ld != null ? p.ld : (p.cl != null && p.cd ? p.cl / p.cd : NaN)]) });
        if (on && cd.xfoil && AS.isNum(cd.xfoil.alpha_design) && AS.isNum(cd.xfoil.ld)) markers.push({ x: cd.xfoil.alpha_design, y: cd.xfoil.ld, color });
      });
      chart.update({ series, markers, xLabel: 'angle of attack (deg)', yLabel: 'L/D (XFoil)', fmtX: (v) => AS.fmt(v) + '°',
        emptyText: 'Polars appear when the XFoil screening has finished' });
    }
    function updateFoot() {
      const s = state.study;
      AS.clear(foot);
      if (!s) return;
      if (s.status === 'suggesting') { foot.appendChild(AS.ui.spinner('The advisor is screening candidates in XFoil')); return; }
      if (!canPick()) {
        const chosen = (s.candidates || []).filter((cd) => cd.selected).map((cd) => 'NACA ' + cd.naca);
        foot.appendChild(h('span', { class: 'muted' }, chosen.length ? 'Selected for simulation: ' + chosen.join(', ') : 'No selection was made.'));
        return;
      }
      const n = picks.size;
      foot.appendChild(h('span', { class: 'muted' }, n === 0 ? 'Select at least one airfoil.' : `${n} selected` + (n > 2 ? ' · each extra case runs in parallel and adds run time' : '')));
      foot.appendChild(h('button', { type: 'button', class: 'btn primary', id: 'confirm-btn', disabled: n === 0 || busy, onclick: async () => {
        busy = true; updateFoot();
        const order = (s.candidates || []).filter((cd) => picks.has(cd.naca)).map((cd) => cd.naca);
        await AS.actions.select(order);
        busy = false; updateFoot();
      } }, busy ? 'Starting the planner…' : 'Confirm and plan'));
    }

    function card(cd, objective) {
      const x = cd.xfoil || {}, on = picks.has(cd.naca);
      const box = h('input', { type: 'checkbox', checked: on, disabled: !canPick(), 'aria-label': `Simulate NACA ${cd.naca}`, onchange: (e) => {
        if (e.target.checked) picks.add(cd.naca); else picks.delete(cd.naca);
        node.classList.toggle('selected', e.target.checked);
        updateChart(); updateFoot();
      } });
      const numbers = [
        ['α design', AS.isNum(x.alpha_design) ? AS.fmt(x.alpha_design, 2) + '°' : '–'],
        ['Cl', AS.fmt(x.cl, 3)], ['Cd', AS.isNum(x.cd) ? x.cd.toFixed(5) : '–'],
        ['L/D', AS.fmt(x.ld, 1)], ['Cm', AS.fmt(x.cm, 3)],
        objective === 'max_endurance' ? ['Cl^1.5/Cd', AS.fmt(x.endurance, 1)] : null,
      ];
      // target L/D: reported per candidate, never used to exclude one (absent in studies without a target)
      const target = AS.targetLd(state.study);
      const judged = typeof cd.meets_target_ld === 'boolean';
      const node = h('article', { class: 'cand' + (on ? ' selected' : ''), dataset: { stage: 'suggest' } },
        h('header', {}, h('label', { class: 'cand-pick' }, box, h('strong', {}, 'NACA ' + cd.naca)),
          cd.rank != null ? h('span', { class: 'rank' }, '#' + cd.rank) : null,
          x.converged === false ? h('span', { class: 'chip st-failed' }, 'XFoil not converged') : null),
        thumb(cd.naca),
        AS.ui.kv(numbers),
        target != null || judged ? h('p', { class: 'cand-target', title: 'XFoil at the design lift coefficient, transition forced at 5 % chord (comparable with the fully turbulent RANS run). The target is reported; it does not exclude a section.' },
          'Target L/D' + (target != null ? ' ≥ ' + AS.fmt(target) : '') + ' in XFoil: ', AS.ui.target(judged ? cd.meets_target_ld : undefined)) : null,
        h('p', { class: 'cand-why' }, cd.rationale || ''));
      return node;
    }

    function build() {
      head = h('div', {});
      cards = h('div', { class: 'cand-grid' });
      chartHost = h('div', { class: 'chart-host tall' });
      foot = h('div', { class: 'row actions sticky-foot' });
      el = h('div', { class: 'view view-suggest', dataset: { stage: 'suggest' } }, head, cards,
        h('h3', {}, 'L/D against angle of attack'),
        h('p', { class: 'muted small' }, 'XFoil at the mission Reynolds number, transition forced at 5 % chord. Dots mark the angle that gives the design lift coefficient. Selected sections are drawn bold.'),
        chartHost, foot);
      chart = AS.chart.line(chartHost);
      return el;
    }

    function render() {
      const s = state.study;
      if (!s) return;
      const cands = (s.candidates || []).slice().sort((a, b) => (a.rank || 99) - (b.rank || 99));
      const key = s.id + '|' + cands.map((cd) => cd.naca + (cd.selected ? '*' : '')).join(',');
      if (key !== picksFor) {
        picksFor = key;
        const flagged = cands.filter((cd) => cd.selected).map((cd) => cd.naca);
        picks = new Set(flagged.length ? flagged : cands.slice(0, 2).map((cd) => cd.naca));   // top 2 preselected
      }
      const req = s.requirements || {};
      AS.region(head, [s.status, req, cands.length], () => [
        h('h2', {}, 'Airfoil suggestion'),
        h('p', { class: 'muted' }, cands.length
          ? `${cands.length} candidates for ${AS.ui.missionChip(req)}. The top two are preselected; tick others to change which sections are simulated.`
          : 'The advisor reads the mission, proposes NACA 4-digit sections and screens them in XFoil.'),
      ]);
      const changed = AS.region(cards, [s.status === 'awaiting_selection', cands, req.objective, req.target_ld], () => (cands.length
        ? cands.map((cd) => card(cd, req.objective))
        : h('div', { class: 'placeholder' }, s.status === 'suggesting' ? AS.ui.spinner('Waiting for the advisor') : 'No candidates yet.')));
      if (changed || !chart.opts) updateChart();
      updateFoot();
    }
    return { build, render };
  })();

  // =====================================================================
  // Plan: editable values with reasons, approve / keep planning
  // =====================================================================
  V.plan = (function () {
    let el, head, body, foot, commentWrap, commentBox, busy = '';
    const NOTE_KEYS = ['reason', 'rationale', 'why', 'note', 'notes'];
    const CHECK_KEYS = ['checks', 'acceptance', 'acceptance_checks', 'gate', 'gates'];
    const GROUP_KEYS = ['params', 'settings', 'values'];
    const isScalar = (v) => v == null || ['string', 'number', 'boolean'].includes(typeof v);

    /* Settings with a fixed list of values (the lists of planning.validate_plan), shown as selects. */
    const ENUMS = { turbulence_model: ['SpalartAllmaras', 'kOmegaSST'], wall_treatment: ['wallFunction', 'resolved'], farfield_bc: ['fixedInlet', 'freestream'],
      convection_scheme: ['linearUpwind', 'upwind'] };
    /* The airfoil of a case cannot be changed after selection: the backend puts the code back. */
    const READ_ONLY = { naca: 'The airfoil of a case is fixed by the selection. To simulate another section, start a new study.' };
    const ALPHA_REASON = 'Angle of attack that gives the design lift coefficient in XFoil.';

    /* The backend accepts PATCH /plan whenever a plan exists and no agent is writing it:
       before approval, while the cases run and after the run (the edit takes effect when the stage next runs). */
    function editable() {
      const s = state.study;
      return !!(s && s.plan && !['suggesting', 'planning'].includes(s.status));
    }
    function approvable() {
      const s = state.study;
      return !!(s && s.status === 'awaiting_approval' && s.plan && !s.plan.approved_at);
    }
    function show(input, original) {
      if (typeof original === 'boolean') input.checked = original;
      else input.value = Array.isArray(original) ? original.join(', ') : String(original);
    }
    function getPath(obj, path) { return path.reduce((o, k) => (o == null ? undefined : o[k]), obj); }
    function setPath(obj, path, value) {
      let o = obj;
      for (let i = 0; i < path.length - 1; i++) o = o[path[i]];
      o[path[path.length - 1]] = value;
    }
    async function commit(path, original, input) {
      let value;
      if (typeof original === 'boolean') value = input.checked;
      else if (typeof original === 'number') {
        value = Number(input.value.trim());
        if (input.value.trim() === '' || !isFinite(value)) { AS.toast('That value must be a number.', 'error'); input.value = String(original); return; }
      } else if (Array.isArray(original)) {
        const numeric = original.length > 0 && original.every((x) => typeof x === 'number');
        value = input.value.split(',').map((x) => x.trim()).filter(Boolean).map((x) => (numeric ? Number(x) : x));
        if (numeric && value.some((x) => !isFinite(x))) { AS.toast('Use numbers separated by commas.', 'error'); input.value = original.join(', '); return; }
      } else value = input.value;
      if (JSON.stringify(value) === JSON.stringify(original)) return;
      // One edit is one save. The plan may already hold this value (the rebuild after a save fires a
      // late change event on the field it replaces), or the same value may be on its way (Enter, then blur).
      const sent = JSON.stringify(value);
      if (sent === JSON.stringify(getPath(state.study.plan, path)) || input.__sent === sent) return;
      input.__sent = sent;
      const plan = AS.clone(state.study.plan);
      setPath(plan, path, value);
      input.classList.add('saving');
      const saved = await AS.actions.patchPlan(plan);
      input.classList.remove('saving');
      if (saved === null) {
        delete input.__sent;
        // refused by the server (the reason is in the toast): the screen must keep showing the plan that will run
        show(input, original);
        input.classList.add('invalid');
        input.title = 'The last edit of this value was refused; the plan value is shown.';
        const clear = () => { input.classList.remove('invalid'); input.removeAttribute('title'); input.removeEventListener('input', clear); };
        input.addEventListener('input', clear);
      }
    }
    /* The text a row shows for its value: what the field holds while the value can be edited, the
       short form otherwise. */
    const inField = (value) => (Array.isArray(value) ? value.join(', ') : value == null ? '' : String(value));
    function shownText(path, value, canEdit) {
      return canEdit && !READ_ONLY[path[path.length - 1]] && typeof value !== 'boolean' ? inField(value) : AS.fmtValue(value);
    }
    /* The case a plan column stands for. Before approval there is no case record: the id is the one
       the backend gives the case then (<study>-<naca>). */
    function caseIdOf(pc) {
      const c = AS.caseList().find((x) => String(x.naca) === String(pc.naca));
      return c ? c.id : state.study.id + '-' + pc.naca;
    }
    /* The reference mark of a cited value (js/cite.js builds the button; null when the value has no
       citation or was set by the engineer). `cited` is { section, where, caseId }: the section that
       holds the `cites` map, and where its rows are. The mark also carries the value as the row
       shows it (`text`) and the study and case it belongs to, for the pop-up's previous runs. */
    function citeMark(cited, stage, name, path, value, reason, canEdit) {
      if (!cited || !cited.section) return null;
      return AS.cite.planMark({ section: cited.section, stage, name, path, value, reason, where: cited.where || '',
        text: shownText(path, value, canEdit), study: state.study.id, case: cited.caseId || '' });
    }
    /* `mark` takes the third column of the row, so the values stay aligned with and without one. */
    function field(label, value, path, reason, canEdit, mark) {
      const id = 'p-' + path.join('-'), name = path[path.length - 1];
      const options = typeof value === 'string' ? ENUMS[name] : null;
      let control;
      if (READ_ONLY[name] && canEdit) { canEdit = false; control = h('span', { class: 'plan-value', title: READ_ONLY[name] }, AS.fmtValue(value)); }
      else if (!canEdit) control = h('span', { class: 'plan-value' }, AS.fmtValue(value));
      else if (typeof value === 'boolean') control = h('input', { id, type: 'checkbox', checked: value, dataset: { path: id }, onchange: (e) => commit(path, value, e.target) });
      else if (options) {
        control = h('select', { id, class: 'plan-input plan-select', dataset: { path: id }, onchange: (e) => commit(path, value, e.target) },
          (options.includes(value) ? options : [value].concat(options)).map((o) => h('option', { value: o, selected: o === value }, o)));
      } else {
        control = h('input', { id, type: 'text', class: 'plan-input', dataset: { path: id }, value: inField(value),
          inputmode: typeof value === 'number' ? 'decimal' : null, spellcheck: 'false',
          onchange: (e) => commit(path, value == null ? '' : value, e.target),
          onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); if (!e.repeat) commit(path, value == null ? '' : value, e.target); } if (e.key === 'Escape') { e.target.value = inField(value); e.target.blur(); } } });
      }
      return h('div', { class: 'plan-row' }, h('label', { for: canEdit ? id : null, class: 'plan-key' }, AS.humanKey(label)), control,
        mark || null,
        reason ? h('span', { class: 'plan-reason' }, reason) : null);
    }
    /* A stage section of the plan can be shaped in several ways (flat values with a
       `reasons` map, {value, reason} pairs, a nested params group); show them all.
       `cited` names the stage section that holds the `cites` map, where its rows are and the case,
       for the reference marks; a nested group keeps the section it belongs to. */
    function stageRows(obj, path, canEdit, reasons, cited) {
      const rows = [], notes = [], checks = [];
      reasons = Object.assign({}, reasons, obj && typeof obj.reasons === 'object' && !Array.isArray(obj.reasons) ? obj.reasons : null);
      cited = cited || { section: obj, where: '' };
      const mark = (k, p, value, reason) => citeMark(cited, path[3], k, p, value, reason, canEdit);
      for (const [k, v] of Object.entries(obj || {})) {
        // `cites` is the map of reference ids beside `reasons`: shown as marks, never as rows of its own
        if (k === 'reasons' || k === 'cites') continue;
        if (NOTE_KEYS.includes(k) && typeof v === 'string') { if (v) notes.push(v); continue; }
        if (CHECK_KEYS.includes(k) && Array.isArray(v)) { checks.push(...v.map((c) => (typeof c === 'string' ? c : AS.fmtValue(c)))); continue; }
        if (isScalar(v) || (Array.isArray(v) && v.every(isScalar))) rows.push(field(k, v, path.concat(k), reasons[k], canEdit, mark(k, path.concat(k), v, reasons[k])));
        else if (v && typeof v === 'object' && !Array.isArray(v) && 'value' in v && isScalar(v.value)) {
          const reason = v.reason || v.why || reasons[k];
          rows.push(field(k, v.value, path.concat(k, 'value'), reason, canEdit, mark(k, path.concat(k, 'value'), v.value, reason)));
        } else if (v && typeof v === 'object' && !Array.isArray(v)) {
          const inner = stageRows(v, path.concat(k), canEdit, reasons, cited);
          if (!GROUP_KEYS.includes(k) && inner.rows.length) rows.push(h('div', { class: 'plan-sub' }, AS.humanKey(k)));
          rows.push(...inner.rows); notes.push(...inner.notes); checks.push(...inner.checks);
        } else rows.push(field(k, AS.fmtValue(v), path.concat(k), reasons[k], false));
      }
      return { rows, notes, checks };
    }
    function caseColumn(pc, index, canEdit) {
      const stages = pc.stages || {};
      const keys = AS.stageDefs().map((d) => d.key).filter((k) => stages[k]).concat(Object.keys(stages).filter((k) => !AS.stageDefs().some((d) => d.key === k)));
      const caseId = caseIdOf(pc);
      return h('section', { class: 'plan-case', dataset: { naca: pc.naca } },
        h('h3', {}, 'NACA ' + pc.naca),
        // the angle is cited at case level (pc.cites.alpha_deg), not in a stage section
        field('alpha_deg', pc.alpha_deg, ['cases', index, 'alpha_deg'], ALPHA_REASON, canEdit,
          citeMark({ section: pc, where: 'NACA ' + pc.naca, caseId }, 'case', 'alpha_deg', ['cases', index, 'alpha_deg'], pc.alpha_deg, ALPHA_REASON, canEdit)),
        keys.map((key, i) => {
          const parts = stageRows(stages[key], ['cases', index, 'stages', key], canEdit, {}, { section: stages[key], where: 'NACA ' + pc.naca + ' · ' + AS.stageTitle(key), caseId });
          return h('div', { class: 'plan-stage', dataset: { stage: key } },
            h('h4', {}, h('span', { class: 'plan-num' }, i + 1), AS.stageTitle(key), h('span', { class: 'plan-ran', hidden: true, dataset: { naca: pc.naca, key } })),
            parts.notes.map((n) => h('p', { class: 'plan-note' }, n)),
            parts.rows,
            parts.checks.length ? h('div', { class: 'plan-checks' }, h('span', { class: 'plan-key' }, 'Acceptance'), h('ul', {}, parts.checks.map((c) => h('li', {}, c)))) : null);
        }));
    }
    /* Once cases exist, say per stage whether an edit still reaches the current run. Updated in
       place so that a stage finishing does not rebuild the inputs someone is typing in. */
    function markRunStages() {
      const cases = AS.caseList();
      for (const node of AS.$$('.plan-ran', body)) {
        const c = cases.find((x) => String(x.naca) === node.dataset.naca), st = c && AS.stageOf(c, node.dataset.key);
        const text = !st ? '' : st.status === 'passed' ? 'already run · re-run to apply an edit' : st.status === 'running' ? 'running · an edit applies to its next run' : '';
        if (node.textContent !== text) node.textContent = text;
        node.hidden = !text;
      }
    }
    async function approve(mode) {
      busy = mode; render();
      await AS.actions.approvePlan(mode);
      busy = ''; render();
    }
    async function revise() {
      const comment = commentBox.value.trim();
      if (!comment) { commentBox.focus(); return; }
      busy = 'revise'; render();
      const ok = await AS.actions.revisePlan(comment);
      if (ok !== null) { commentBox.value = ''; commentWrap.hidden = true; }
      busy = ''; render();
    }

    function build() {
      head = h('div', {});
      body = h('div', { class: 'plan-cases' });
      commentBox = h('textarea', { id: 'plan-comment', rows: 2, placeholder: 'Tell the planner what to change, for example: use 1600 iterations, or refine the mesh.',
        'aria-label': 'Comment for the planner', onkeydown: (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) revise(); } });
      commentWrap = h('div', { class: 'plan-comment', hidden: true }, commentBox,
        h('button', { type: 'button', class: 'btn primary small', id: 'revise-btn', onclick: revise }, 'Send to planner'));
      foot = h('div', { class: 'plan-foot sticky-foot' });
      el = h('div', { class: 'view view-plan', dataset: { stage: 'plan' } }, head, body, commentWrap, foot);
      // a comment drafted for one study must not be open, and sendable, on the plan of the next one
      AS.on('reset', () => { commentBox.value = ''; commentWrap.hidden = true; });
      return el;
    }

    function render() {
      const s = state.study;
      if (!s) return;
      const plan = s.plan, canEdit = editable(), canApprove = approvable(), started = AS.caseList().length > 0;
      AS.region(head, [s.status, plan && plan.version, plan && plan.summary, plan && plan.assumptions, plan && plan.approved_at, canEdit, canApprove, started], () => [
        h('h2', {}, 'Plan', plan ? h('span', { class: 'badge' }, 'v' + (plan.version || 1)) : null,
          plan && plan.approved_at ? h('span', { class: 'chip st-passed' }, 'approved ' + AS.fmtClock(plan.approved_at)) : null,
          s.status === 'planning' ? AS.ui.spinner('The planner is writing') : null),
        plan ? h('p', { class: 'plan-summary' }, plan.summary || '') : h('p', { class: 'muted' }, 'The planner writes the settings for every airfoil and stage, with reasons. Nothing runs until you approve.'),
        plan && plan.assumptions && plan.assumptions.length ? h('details', { class: 'assumptions', open: true },
          h('summary', {}, `Assumptions (${plan.assumptions.length})`), h('ul', {}, plan.assumptions.map((a) => h('li', {}, a)))) : null,
        canEdit && !started ? h('p', { class: 'muted small' }, 'Every value is editable: change it and press Enter. Each edit is saved as a new plan version and recorded as a human intervention.') : null,
        canEdit && started ? h('p', { class: 'note info small', id: 'plan-live-note' }, 'The values can still be edited: change one and press Enter. An edit is saved as a new plan version, is recorded as a human intervention, ',
          'and takes effect when its stage next runs. A stage marked "already run" keeps its result until you re-run it. A changed angle of attack is different: once the physics stage of a case has run, the case is sent back to that stage at once and re-runs from there at the new angle (a stopped case is marked stale and re-runs from physics when you start it again).') : null,
      ]);
      // keep keyboard focus on the same field across a rebuild caused by a saved edit
      const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.path : null;
      // the marks carry the study id and the case ids, hence both in the signature
      const rebuilt = AS.region(body, [plan, canEdit, AS.stageDefs().length, s.id, AS.caseList().map((c) => c.id)], () => (plan && plan.cases
        ? plan.cases.map((pc, i) => caseColumn(pc, i, canEdit))
        : h('div', { class: 'placeholder' }, s.status === 'planning' ? AS.ui.spinner('Waiting for the planner') : 'No plan yet.')));
      if (rebuilt && focused) { const again = body.querySelector(`[data-path="${focused}"]`); if (again) again.focus(); }
      markRunStages();
      AS.region(foot, [s.status, canEdit, canApprove, busy, !!plan, plan && plan.approved_at, s.mode, started], () => {
        if (!plan) return null;
        if (!canApprove) {
          return h('span', { class: 'muted' }, s.status === 'planning' ? 'Approval is available when the planner has finished.'
            : plan.approved_at ? `Approved in ${s.mode === 'gate' ? 'gate' : 'auto-run'} mode. Edit a value above, tell the agent in the chat, or re-run a stage with an instruction; a stage that has already passed must be re-run to pick up a change.`
              : started ? `Edited after the run (version ${plan.version || 1}). Re-run the stages concerned to apply the edit.` : '');
        }
        return [
          h('button', { type: 'button', class: 'btn primary', id: 'approve-auto', disabled: !!busy, onclick: () => approve('auto') }, busy === 'auto' ? 'Starting…' : 'Approve and auto-run'),
          h('button', { type: 'button', class: 'btn', id: 'approve-gate', disabled: !!busy, onclick: () => approve('gate') }, busy === 'gate' ? 'Starting…' : 'Approve, gate each stage'),
          h('button', { type: 'button', class: 'btn', id: 'keep-planning', disabled: !!busy, onclick: () => { commentWrap.hidden = !commentWrap.hidden; if (!commentWrap.hidden) commentBox.focus(); } }, 'Keep planning'),
        ];
      });
      if (!canApprove) commentWrap.hidden = true;
    }
    return { build, render };
  })();

  // =====================================================================
  // Report
  // =====================================================================
  AS.recommendationText = function (report) {
    const r = report && report.recommendation;
    if (!r) return '';
    if (typeof r === 'string') return r;
    return r.text || r.summary || (r.naca ? 'NACA ' + r.naca + (r.reason ? ': ' + r.reason : '') : AS.fmtValue(r));
  };
  AS.reportBlock = function (study) {
    const report = study.report;
    if (!report) return null;
    const rec = AS.recommendationText(report);
    const md = report.markdown || '';
    const rows = Array.isArray(report.table) ? report.table.filter((r) => r && typeof r === 'object') : [];
    const table = rows.length && !md.includes('|---') ? rows : null;
    // target L/D per case, as judged by the backend (rows[].meets_target_ld; absent in older studies)
    const target = AS.targetLd(study), judged = rows.filter((r) => typeof r.meets_target_ld === 'boolean');
    const cell = (k, v) => (k === 'meets_target_ld' ? (typeof v === 'boolean' ? AS.ui.target(v) : AS.fmtValue(null)) : AS.fmtValue(v));
    return h('div', { class: 'report-body', dataset: { stage: 'report' } },
      rec ? h('div', { class: 'recommend' }, h('span', { class: 'recommend-label' }, 'Recommendation'), h('p', {}, rec)) : null,
      judged.length ? h('div', { class: 'target-strip', id: 'report-target' }, h('span', { class: 'target-label' }, 'Target L/D' + (target != null ? ' ≥ ' + AS.fmt(target) : ''), h('em', {}, ' RANS result at the design lift coefficient')),
        judged.map((r) => h('span', { class: 'target-item' }, h('b', {}, 'NACA ' + r.naca), AS.isNum(r.ld) ? ' L/D ' + r.ld.toFixed(1) + ' ' : ' ', AS.ui.target(r.meets_target_ld)))) : null,
      AS.md(md),
      table ? h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, Object.keys(table[0]).map((k) => h('th', {}, k === 'meets_target_ld' ? 'target L/D' : AS.humanKey(k))))),
        h('tbody', {}, table.map((row) => h('tr', {}, Object.keys(table[0]).map((k) => h('td', {}, cell(k, row[k])))))))) : null);
  };

  V.report = (function () {
    let el;
    function build() { el = h('div', { class: 'view view-report', dataset: { stage: 'report' } }); return el; }
    function render() {
      const s = state.study;
      if (!s) return;
      const cases = AS.caseList();
      AS.region(el, [s.status, s.report, cases.map((c) => [c.status, c.verdict])], () => [
        h('h2', {}, 'Report'),
        s.report ? AS.reportBlock(s)
          : h('div', { class: 'placeholder' },
            cases.length && cases.every((c) => ['done', 'failed', 'stopped'].includes(c.status)) && AS.isActiveStatus(s.status)
              ? AS.ui.spinner('The reporter is comparing the cases')
              : cases.length && ['stopped', 'failed'].includes(s.status)
                ? `No report was written: the study ${s.status === 'stopped' ? 'was stopped' : 'failed'} before a case finished. Re-run a stage of a case to continue; the report is written when the cases have ended and at least one is done.`
                : 'The report is written when every case has finished. ' + (cases.length ? `${cases.filter((c) => c.status === 'done').length} of ${cases.length} cases done.` : '')),
        s.report ? h('div', { class: 'row actions' },
          h('button', { type: 'button', class: 'btn primary', onclick: () => AS.setTab('compare') }, 'Open the comparison'),
          h('button', { type: 'button', class: 'btn', onclick: () => AS.setTab('dashboard') }, 'Open the dashboard'),
          h('button', { type: 'button', class: 'btn', onclick: () => AS.runWithAnotherModel() }, 'Run this mission with another model')) : null,
      ]);
    }
    return { build, render };
  })();
})();
