/* Wing Studio - Dashboard tab: the history of finished cases, per model
   and per geometry, from /api/metrics/runs and /api/metrics/summary. */
(function () {
  'use strict';
  const AS = window.AS, h = AS.h, state = AS.state;
  const V = AS.views;

  /* Share (0 to 1) of the input tokens of these runs that was read from the prompt cache, over the
     runs that recorded both counts. null when none did (rows from before the column) or when they
     had no input (the scripted model): unknown is never stated as 0 %. */
  AS.cachedShareOfRuns = function (rows) {
    const known = rows.filter((r) => AS.isNum(r.tokens_cached) && AS.isNum(r.tokens_in));
    return known.length ? AS.cachedShare(AS.sum(known.map((r) => r.tokens_cached)), AS.sum(known.map((r) => r.tokens_in))) : null;
  };

  /* ---------- pass^k: the rules are in js/metrics.js; here the texts and the panel ---------- */

  /* One text for a pass^k value in the table, the tooltip and the chart's description. '100 %' is
     printed only when every run passed and '0 %' only for an exact zero, so neither is ever the
     rounding of a near value; unknown is a dash, never 0. */
  AS.fmtPassK = function (v) {
    if (!AS.isNum(v)) return '–';
    if (v === 1) return '100 %';
    if (v === 0) return '0 %';
    const p = v * 100;
    if (p < 0.05) return '<0.1 %';
    if (v >= 0.995) return '>99 %';
    return (p < 9.95 ? p.toFixed(1) : p.toFixed(0)) + ' %';
  };

  /* 'c of n runs passed', the title of the Passes (c) cell and of the pass^1 cell. The past tense
     needs no agreement with c, so '0 of 1 run passed' and '1 of 4 runs passed' both read right. */
  AS.passKCount = function (c, n) {
    return `${c} of ${n} run${n === 1 ? '' : 's'} passed`;
  };

  /* The sentences under the pass^k table, in order; [] when none applies. items: the shown models as
     { label, n, c }; lost: rows left out of n (AS.isTrial); noHelp: shown trials without a help
     count, which matters under the criterion `unaided` only. */
  AS.passKFoot = function (pass, items, lost, noHelp, few, maxK) {
    const parts = [];
    if (items.some((i) => i.n < few)) parts.push(`With fewer than ${few} runs per model these are rough figures.`);
    const clean = items.filter((i) => i.c === i.n).map((i) => i.label);
    if (clean.length) {
      const list = clean.length > 1 ? clean.slice(0, -1).join(', ') + ' and ' + clean[clean.length - 1] : clean[0];
      parts.push(`No run of ${list} fails under this criterion. 100 % means no failure seen in those runs, not that none can happen.`);
    }
    // The runs left out stay in sight, so the reader can judge what leaving them out does to the
    // figure. The panels above count them as runs that did not succeed, hence the second sentence.
    if (lost) {
      parts.push(lost === 1 ? '1 run was stopped before a result and is left out. The Success column above counts it, so pass^1 can differ from it.'
        : `${lost} runs were stopped before a result and are left out. The Success column above counts them, so pass^1 can differ from it.`);
    }
    // The criterion line says routine approvals do not count; for a row without a help count the
    // test falls back to the count of every action, and the page says for how many rows.
    if (pass === 'unaided' && noHelp) {
      parts.push(noHelp === 1 ? '1 run has no help count: for it every engineer action counts as help.'
        : `${noHelp} runs have no help count: for them every engineer action counts as help.`);
    }
    if (items.some((i) => i.n > maxK)) parts.push(`The chart stops at k = ${maxK}.`);
    return parts;
  };

  V.dashboard = (function () {
    let el, filterBar, tiles, matrix, modelTable, legend, tableWrap, note, charts = {}, built = false;
    let pkDef, pkCrit, pkTable, pkFoot;
    // pass^k panel: the chosen pass criterion. It is not part of load()'s key: a change needs no request.
    const pk = { pass: 'unaided' };
    const PK_COLUMNS = [1, 2, 4, 8];     // fixed, also when a column is all dashes: the columns do not jump with the filter
    const PK_FEW = 20;                   // below this many runs per model the foot says the figures are rough
    const PK_MAX_K = 12;                 // the chart's k axis ends here at the latest
    const PK_PAPER = 'https://arxiv.org/abs/2406.12045';
    const filter = { project: undefined, model: '', naca: '' };
    const data = { runs: [], runsSig: '[]', summary: null, loading: false, loaded: false, error: null, key: null, dirty: true };   // loaded: a first answer (or error) has arrived
    const sort = { key: 'ts', dir: -1 };

    const COLUMNS = [
      ['ts', 'When', (r) => AS.fmtDate(r.ts)], ['study_title', 'Study', (r) => r.study_title || r.study_id], ['model', 'Model', (r) => AS.modelLabel(r.model)],
      ['naca', 'NACA', (r) => r.naca], ['mode', 'Mode', (r) => r.mode], ['verdict', 'Result', (r) => resultLabel(r)],
      ['cl', 'Cl', (r) => AS.fmt(r.cl, 4), true], ['cd', 'Cd', (r) => (AS.isNum(r.cd) ? r.cd.toFixed(5) : '–'), true], ['ld', 'L/D', (r) => AS.fmt(r.ld, 1), true],
      ['cl_err_pct', 'dCl', (r) => AS.fmtPct(r.cl_err_pct), true], ['cd_err_pct', 'dCd', (r) => AS.fmtPct(r.cd_err_pct), true],
      ['iterations', 'Iter', (r) => AS.fmtInt(r.iterations), true], ['wall_seconds', 'Wall', (r) => AS.fmtSeconds(r.wall_seconds), true],
      ['tokens', 'Tokens', (r) => AS.fmtTokens((r.tokens_in || 0) + (r.tokens_out || 0)), true], ['cost_usd', 'Cost', (r) => AS.fmtCost(r.cost_usd), true],
      ['rework_loops', 'Rework', (r) => r.rework_loops || 0, true], ['gate_failures', 'Gate fails', (r) => r.gate_failures || 0, true],
      ['human_interventions', 'Human', (r) => r.human_interventions || 0, true], ['first_pass', 'First pass', (r) => (AS.truthy(r.first_pass) ? 'yes' : 'no')],
    ];

    function resultLabel(r) {
      if (AS.truthy(r.success)) return r.verdict === 'warn' ? 'warn' : 'pass';
      if (r.failed_stage) return 'failed at ' + AS.stageTitle(r.failed_stage).toLowerCase();
      return r.verdict || 'not finished';
    }
    const resultClass = (r) => (AS.truthy(r.success) ? (r.verdict === 'warn' ? 'cell-warn' : 'cell-good') : 'cell-bad');
    const pct = (v) => (AS.isNum(v) ? (v * 100).toFixed(0) + ' %' : '–');

    function modelsInOrder(rows) {
      const known = ((state.config && state.config.models) || []).map(AS.modelId);
      const present = Array.from(new Set(rows.map((r) => r.model).filter(Boolean)));
      return present.sort((a, b) => (known.indexOf(a) < 0 ? 99 : known.indexOf(a)) - (known.indexOf(b) < 0 ? 99 : known.indexOf(b)) || a.localeCompare(b));
    }

    async function load() {
      const project = filter.project === undefined ? state.projectId || '' : filter.project;
      const key = JSON.stringify([project, filter.model, filter.naca]);
      data.loading = true; data.error = null; data.key = key; data.dirty = false;
      render();
      try {
        const q = new URLSearchParams();
        if (project) q.set('project_id', project);
        const qs = project ? '?project_id=' + encodeURIComponent(project) : '';
        if (filter.model) q.set('model', filter.model);
        if (filter.naca) q.set('naca', filter.naca);
        const [runs, summary] = await Promise.all([AS.api.get('/api/metrics/runs' + (q.toString() ? '?' + q : '')), AS.api.get('/api/metrics/summary' + qs)]);
        if (data.key !== key) return;
        data.runs = (runs && runs.runs) || [];
        // A case's row is replaced in place (same id) when the case ends again, for example after a
        // re-run, so the content of the rows (not only their ids) decides whether the views are rebuilt.
        data.runsSig = JSON.stringify(data.runs);
        data.summary = summary || null;
      } catch (e) {
        if (data.key === key) data.error = e.message;
      } finally {
        if (data.key === key) { data.loading = false; data.loaded = true; render(); }
      }
    }

    const TILE_ROW_PX = 72;      // one row of tiles: label 22 + value 28 + sub line 16 + 6 padding (styles.css .tile)
    function tile(label, value, sub) {
      return h('div', { class: 'tile' }, h('div', { class: 'tile-label' }, label), h('div', { class: 'tile-value' }, value), h('div', { class: 'tile-sub' }, sub || ' '));
    }

    function matrixTable(rows) {
      const models = modelsInOrder(rows), nacas = Array.from(new Set(rows.map((r) => r.naca))).sort();
      if (!rows.length) return h('div', { class: 'placeholder' }, 'No finished runs match the filters.');
      return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl matrix' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Model \\ geometry'), nacas.map((n) => h('th', {}, 'NACA ' + n)))),
        h('tbody', {}, models.map((m) => h('tr', {}, h('th', { scope: 'row' }, h('i', { class: 'key', style: { background: AS.modelColor(m) } }), AS.modelLabel(m)),
          nacas.map((n) => {
            const cell = rows.filter((r) => r.model === m && r.naca === n).sort((a, b) => (a.ts < b.ts ? 1 : -1));
            if (!cell.length) return h('td', { class: 'cell-empty' }, '–');
            const last = cell[0], ok = cell.filter((r) => AS.truthy(r.success)).length;
            return h('td', { class: resultClass(last), title: `${cell.length} run${cell.length > 1 ? 's' : ''}; latest ${AS.fmtDate(last.ts)}` },
              h('div', { class: 'cell-head' }, resultLabel(last)),
              AS.isNum(last.cl_err_pct) ? [h('div', { class: 'mono' }, `dCl ${AS.fmtPct(last.cl_err_pct)}`), h('div', { class: 'mono' }, `dCd ${AS.fmtPct(last.cd_err_pct, 0)}`)] : h('div', { class: 'mono' }, 'no result'),
              h('div', { class: 'cell-sub' }, `${ok} of ${cell.length} ok` + (last.rework_loops || last.gate_failures ? ` · ${last.rework_loops || 0} rework, ${last.gate_failures || 0} gate fail` : '')));
          }))))));
    }

    function runTable(rows) {
      const value = (r, key) => (key === 'tokens' ? (r.tokens_in || 0) + (r.tokens_out || 0) : r[key]);
      const sorted = rows.slice().sort((a, b) => {
        const x = value(a, sort.key), y = value(b, sort.key);
        if (x == null && y == null) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
      });
      return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl runs' },
        h('thead', {}, h('tr', {}, COLUMNS.map(([key, label, , numeric]) => h('th', { class: numeric ? 'num' : '', 'aria-sort': sort.key === key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none' },
          h('button', { type: 'button', class: 'sort-btn', dataset: { sort: key, key: 'sort:' + key }, onclick: () => { if (sort.key === key) sort.dir = -sort.dir; else { sort.key = key; sort.dir = numeric || key === 'ts' ? -1 : 1; } render(); } },
            label, sort.key === key ? AS.icon(sort.dir > 0 ? 'sort-up' : 'sort-down', 11) : null))), h('th', {}, ''))),
        h('tbody', {}, sorted.map((r) => h('tr', {}, COLUMNS.map(([key, , fmt, numeric]) => h('td', { class: (numeric ? 'num ' : '') + (key === 'verdict' ? resultClass(r) : '') }, fmt(r))),
          h('td', {}, h('button', { type: 'button', class: 'link-btn', title: 'Open this study', onclick: async () => { await AS.openStudy(r.study_id); if (AS.state.study && AS.state.study.id === r.study_id) AS.setTab('main'); } }, 'open')))))));
    }

    function build(root) {
      el = root;
      filterBar = h('div', { class: 'dash-filters', role: 'group', 'aria-label': 'Dashboard filters' });
      note = h('div', {});
      tiles = h('div', { class: 'tiles' });
      matrix = h('div', {});
      modelTable = h('div', {});
      legend = h('div', { class: 'chart-legend dash-legend' });
      tableWrap = h('div', {});
      const host = (name, cls) => { const node = h('div', { class: 'chart-host ' + (cls || '') }); charts[name] = node; return node; };
      const panel = (title, sub, name, cls) => h('section', { class: 'dash-panel' }, h('h3', {}, title), sub ? h('p', { class: 'muted small' }, sub) : null, host(name, cls));
      // pass^k: the criterion is remembered; a stored value that is not one of the ids falls back to the default
      const stored = AS.store.get('dash.pass', pk.pass);
      if (AS.PASS_CRITERIA.some((c) => c.id === stored)) pk.pass = stored;
      pkDef = h('p', { id: 'dash-pk-def', class: 'muted small' });
      pkCrit = h('p', { id: 'dash-pk-crit', class: 'muted small' });
      pkTable = h('div', { id: 'dash-pk-table' });
      pkFoot = h('div', { id: 'dash-pk-foot' });
      const pkChart = host('passk', 'pk');
      pkChart.id = 'dash-pk-chart';
      // the select sits outside every region, so it keeps the focus when its own change redraws the panel
      const pkPanel = h('section', { id: 'dash-pk', class: 'dash-panel wide dash-pk', 'aria-labelledby': 'dash-pk-title' },
        h('div', { class: 'dash-head' }, h('h3', { id: 'dash-pk-title' }, 'Reliability (pass^k)'),
          h('label', { class: 'field-select', for: 'dash-pk-pass' }, h('span', { class: 'muted' }, 'Pass criterion'),
            h('select', { id: 'dash-pk-pass', onchange: (e) => { pk.pass = e.target.value; AS.store.set('dash.pass', pk.pass); render(); } },
              AS.PASS_CRITERIA.map((c) => h('option', { value: c.id, selected: c.id === pk.pass }, c.label))))),
        pkDef, pkCrit, h('div', { class: 'pk-body' }, pkChart, pkTable), pkFoot);
      AS.append(el, [h('div', { class: 'view view-dashboard' },
        h('h2', {}, 'Dashboard'), filterBar, note, tiles,
        h('section', { class: 'dash-panel wide' }, h('h3', {}, 'Result by model and geometry'),
          h('p', { class: 'muted small' }, 'Latest run in each cell: outcome, difference to XFoil, and how many of the runs succeeded.'), matrix),
        h('section', { class: 'dash-panel wide' }, h('h3', {}, 'Model comparison'),
          h('p', { class: 'muted small' }, 'The same numbers per model, so two models run on the same mission can be read side by side. One run is one airfoil case: wall time, tokens and cost are those of its seven stage agents. The advisor, planner and reporter work once per study and are counted in the study totals (top bar), not here.'), modelTable),
        pkPanel,
        h('section', { class: 'dash-panel wide' }, h('h3', {}, 'OpenFOAM against XFoil'),
          h('p', { class: 'muted small' }, 'One point per finished run. On the line the two solvers agree; the band is the pass tolerance (5 % on Cl, 25 % on Cd).'), legend,
          h('div', { class: 'dash-row two' }, h('div', {}, h('h4', {}, 'Lift coefficient'), host('parityCl', 'tall')), h('div', {}, h('h4', {}, 'Drag coefficient'), host('parityCd', 'tall')))),
        h('div', { class: 'dash-row five' },
          panel('Wall time', 'median seconds per run', 'wall'), panel('Tokens', 'median per run, in + out', 'tokens'), panel('Cost', 'mean USD per run', 'cost'),
          panel('Rework loops', 'mean per run', 'rework'), panel('Human interventions', 'mean per run', 'human')),
        h('section', { class: 'dash-panel wide' }, h('h3', {}, 'Run history'), tableWrap))]);
      for (const name of ['parityCl', 'parityCd']) charts[name] = AS.chart.scatter(charts[name]);
      for (const name of ['wall', 'tokens', 'cost', 'rework', 'human']) charts[name] = AS.chart.bars(charts[name]);
      charts.passk = AS.chart.line(charts.passk);
      built = true;
      // new results arrive while the dashboard is closed: reload on next visit
      AS.on('event', (ev, historical) => { if (!historical && (ev.type === 'case.completed' || ev.type === 'study.status' || ev.type === 'case.status')) data.dirty = true; });
    }

    function render() {
      if (!built || state.ui.tab !== 'dashboard') return;
      const project = filter.project === undefined ? state.projectId || '' : filter.project;
      if ((data.dirty || data.key !== JSON.stringify([project, filter.model, filter.naca])) && !data.loading) { load(); return; }
      const totals = (data.summary && data.summary.totals) || {};
      const models = Array.from(new Set([].concat(totals.models || [], data.runs.map((r) => r.model)))).filter(Boolean);
      const nacas = Array.from(new Set([].concat(totals.nacas || [], data.runs.map((r) => r.naca)))).filter(Boolean).sort();
      AS.region(filterBar, [state.projects.map((p) => [p.id, p.name]), project, filter.model, filter.naca, models, nacas, data.loading], () => [
        h('label', {}, h('span', {}, 'Project'), h('select', { id: 'dash-project', onchange: (e) => { filter.project = e.target.value; filter.model = ''; filter.naca = ''; render(); } },
          h('option', { value: '', selected: project === '' }, 'All projects'), state.projects.map((p) => h('option', { value: p.id, selected: p.id === project }, p.name)))),
        h('label', {}, h('span', {}, 'Model'), h('select', { id: 'dash-model', onchange: (e) => { filter.model = e.target.value; render(); } },
          h('option', { value: '', selected: !filter.model }, 'All models'), models.map((m) => h('option', { value: m, selected: m === filter.model }, AS.modelLabel(m))))),
        h('label', {}, h('span', {}, 'Geometry'), h('select', { id: 'dash-naca', onchange: (e) => { filter.naca = e.target.value; render(); } },
          h('option', { value: '', selected: !filter.naca }, 'All geometries'), nacas.map((n) => h('option', { value: n, selected: n === filter.naca }, 'NACA ' + n)))),
        h('button', { type: 'button', class: 'btn small', id: 'dash-refresh', disabled: data.loading, onclick: () => { data.dirty = true; render(); } }, data.loading ? 'Loading…' : 'Refresh'),
      ]);
      // Before the first answer there is nothing to state: "Runs 0" and "No finished runs" would be
      // false for a moment. The placeholder takes the place of the tile row until the numbers are in.
      el.classList.toggle('dash-loading', !data.loaded);
      if (!data.loaded) {
        AS.region(tiles, ['loading'], () => h('div', { class: 'placeholder', style: { minHeight: TILE_ROW_PX + 'px', border: '0' } }, AS.ui.spinner('Loading runs')));
        return;
      }
      AS.region(note, [data.error, data.loading, data.runs.length], () => (data.error ? h('p', { class: 'note error' }, 'The metrics could not be loaded: ' + data.error)
        : !data.loading && !data.runs.length ? h('p', { class: 'note info' }, 'No finished runs yet for these filters. Each case that finishes, fails or is stopped adds one row.') : null));

      const rows = data.runs;
      // the server summary covers a whole project; with a model or geometry filter the same numbers are computed here
      const unfiltered = !filter.model && !filter.naca && totals.runs === rows.length;
      const agg = unfiltered ? totals : AS.aggregateRuns(rows);
      const byModel = unfiltered && data.summary && Array.isArray(data.summary.by_model) ? data.summary.by_model
        : modelsInOrder(rows).map((m) => Object.assign({ model: m }, AS.aggregateRuns(rows.filter((r) => r.model === m))));
      const n = agg.runs || 0;
      const count = (rate) => (AS.isNum(rate) ? `${Math.round(rate * n)} of ${n}` : '');
      AS.region(tiles, [agg, n], () => [
        tile('Runs', AS.fmtInt(n), `${modelsInOrder(rows).length} models · ${new Set(rows.map((r) => r.naca)).size} geometries`),
        tile('Success rate', pct(agg.success_rate), count(agg.success_rate) + ' passed or warned'),
        tile('First-pass rate', pct(agg.first_pass_rate), count(agg.first_pass_rate) + ' with no rework or help'),
        tile('Convergence rate', pct(agg.convergence_rate), count(agg.convergence_rate) + ' with steady forces'),
        tile('Median |dCl|', AS.isNum(agg.median_abs_cl_err_pct) ? agg.median_abs_cl_err_pct.toFixed(1) + ' %' : '–', 'against XFoil, tolerance 5 %'),
        tile('Median |dCd|', AS.isNum(agg.median_abs_cd_err_pct) ? agg.median_abs_cd_err_pct.toFixed(1) + ' %' : '–', 'against XFoil, tolerance 25 %'),
        tile('Total cost', AS.fmtCost(agg.cost_usd || 0), n ? AS.fmtCost((agg.cost_usd || 0) / n) + ' per run · stage agents only' : ''),
      ]);
      AS.region(matrix, [rows.map((r) => [r.id, r.model, r.naca, r.verdict, r.success, r.cl_err_pct, r.cd_err_pct])], () => matrixTable(rows));

      // Cached: the share of the input tokens of a model's runs that came from the prompt cache. It is
      // computed here from the run rows in both views: the server summary sums a run without the
      // count as 0, so the unfiltered table would state 0 % where the filtered one shows a dash.
      const cached = byModel.map((g) => [g.model, AS.cachedShareOfRuns(rows.filter((r) => r.model === g.model))]);
      AS.region(modelTable, [byModel, cached], () => {
        const order = modelsInOrder(rows);
        const list = byModel.slice().sort((a, b) => order.indexOf(a.model) - order.indexOf(b.model));
        if (!list.length) return null;
        const num = (v, fmt) => (AS.isNum(v) ? fmt(v) : '–');
        const share = new Map(cached);
        // the column is shown only when a run carries both counts (rows of older servers do not)
        const hasCached = rows.some((r) => AS.isNum(r.tokens_cached) && AS.isNum(r.tokens_in));
        // Tokens, Cached and cost stand together and before the wall time: at the default layout
        // (1440 px, both side panes open) the table is wider than its pane, and the columns after
        // about the ninth are reached only by scrolling it sideways.
        const heads = ['Model', 'Runs', 'Success', 'First pass', 'Converged', 'Median |dCl|', 'Median |dCd|', 'Median tokens',
          hasCached ? ['Cached', 'Share of the input tokens read from the prompt cache, over the runs of the model that recorded it'] : null,
          'Mean cost', 'Median wall', 'Rework / run', 'Human / run'].filter(Boolean);
        return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl model-tbl' },
          h('thead', {}, h('tr', {}, heads.map((head, i) => (Array.isArray(head) ? h('th', { class: 'num', title: head[1] }, head[0]) : h('th', { class: i ? 'num' : '' }, head))))),
          h('tbody', {}, list.map((g) => h('tr', {},
            h('th', { scope: 'row' }, h('i', { class: 'key', style: { background: AS.modelColor(g.model) } }), AS.modelLabel(g.model)),
            [AS.fmtInt(g.runs), pct(g.success_rate), pct(g.first_pass_rate), pct(g.convergence_rate), num(g.median_abs_cl_err_pct, (v) => v.toFixed(1) + ' %'),
              num(g.median_abs_cd_err_pct, (v) => v.toFixed(1) + ' %'), num(g.median_tokens, AS.fmtTokens),
              hasCached ? pct(share.get(g.model)) : null,
              num(g.mean_cost_usd, AS.fmtCost), num(g.median_wall_seconds, AS.fmtSeconds), num(g.mean_rework_loops, (v) => v.toFixed(2)), num(g.mean_human_interventions, (v) => v.toFixed(2))]
              .filter((text) => text != null).map((text) => h('td', { class: 'num' }, text)))))));
      });

      // pass^k is always computed here from the run rows, filtered or not, and never read from the
      // summary: table, chart and tooltip then cannot disagree, and the model and geometry filters
      // and the criterion work without a request. The server's pass_k is for readers of the API.
      const pkOrder = modelsInOrder(rows);
      const pkItems = [];
      let pkLost = 0;
      for (const m of pkOrder) {
        const all = AS.passK(rows.filter((r) => r.model === m));
        pkLost += all.left_out;
        const s = all[pk.pass];
        // a model with no trial has no value at any k: no row and no curve, never a 0
        if (s.n) pkItems.push({ model: m, n: s.n, c: s.c, values: s.values, lost: all.left_out });
      }
      const pkCriterion = AS.PASS_CRITERIA.find((c) => c.id === pk.pass);
      // trials that carry no help count (rows from before the column): `unaided` judges them by every action
      const pkNoHelp = rows.filter((r) => AS.isTrial(r) && r.help_interventions == null).length;
      // with the Geometry filter the sentence about any geometry would be untrue, so it is replaced
      AS.region(pkDef, [filter.naca], () => [
        'pass^k estimates the chance that k runs of a model all pass: C(c, k) / C(n, k), with c passes among its n runs under the filters above that reached a result. ',
        filter.naca ? `All runs here are of NACA ${filter.naca}. ` : 'The k runs can be of any geometry. ',
        'This is the tau-bench estimator with all runs of a model pooled as one task; the paper averages it over tasks that are each repeated. Source: ',
        h('a', { id: 'dash-pk-paper', href: PK_PAPER, target: '_blank', rel: 'noopener noreferrer', title: 'arxiv.org, in a new tab' }, 'Yao et al. 2024, arXiv:2406.12045', AS.icon('external', 11)), '.']);
      AS.region(pkCrit, [pk.pass], () => pkCriterion.explain);
      AS.region(pkTable, [pk.pass, pkItems.map((i) => [i.model, i.n, i.c])], () => {
        if (!pkItems.length) return null;
        return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl pk-tbl' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Model'),
            h('th', { class: 'num', title: 'Runs of the model under the filters that reached a result' }, 'Runs (n)'),
            h('th', { class: 'num', title: 'Runs that pass under the chosen criterion' }, 'Passes (c)'),
            PK_COLUMNS.map((K) => h('th', { class: 'num',
              title: K === 1 ? 'Estimated chance that 1 run of the model passes' : `Estimated chance that ${K} runs of the model all pass`,
              'aria-label': K === 1 ? 'pass at k = 1, estimated chance that 1 run passes' : `pass at k = ${K}, estimated chance that ${K} runs all pass` }, 'pass^' + K)))),
          h('tbody', {}, pkItems.map((i) => h('tr', {},
            h('th', { scope: 'row' }, h('i', { class: 'key', style: { background: AS.modelColor(i.model) } }), AS.modelLabel(i.model)),
            h('td', { class: 'num' }, AS.fmtInt(i.n)),
            h('td', { class: 'num', title: AS.passKCount(i.c, i.n) }, AS.fmtInt(i.c)),
            // no value for k > n: a dash with the reason, never 0 and nothing extrapolated.
            // pass^1 is the share of passes itself, so its cell says so in words, not as a formula
            PK_COLUMNS.map((K) => (K <= i.n
              ? h('td', { class: 'num', title: K === 1 ? AS.passKCount(i.c, i.n) : `C(c, ${K}) / C(n, ${K}) with c = ${i.c}, n = ${i.n}` }, AS.fmtPassK(i.values[K - 1]))
              : h('td', { class: 'num pk-na', title: `Needs ${K} runs, has ${i.n}`, 'aria-label': `Not available. Needs ${K} runs, has ${i.n}` }, '–'))))))));
      });
      AS.region(pkFoot, [pk.pass, pkItems.map((i) => [i.model, i.n, i.c, i.lost]), pkLost, pkNoHelp], () => {
        const parts = AS.passKFoot(pk.pass, pkItems.map((i) => ({ label: AS.modelLabel(i.model), n: i.n, c: i.c })), pkLost, pkNoHelp, PK_FEW, PK_MAX_K);
        return parts.length ? h('p', { class: 'pk-foot muted small' }, parts.join(' ')) : null;
      });
      // the error is part of the signature: the empty text differs with it while the rows stay the same
      const pkSig = JSON.stringify([data.runsSig, pk.pass, !!data.error, pkOrder]);
      if (el.__pkSig !== pkSig) {
        el.__pkSig = pkSig;
        const K = Math.min(PK_MAX_K, Math.max(4, ...pkItems.map((i) => i.n)));
        const name = (i) => `${AS.modelLabel(i.model)}, ${i.c} of ${i.n}`;
        const emptyText = data.error && !rows.length ? 'The runs could not be loaded' : 'No finished runs match the filters';
        const ticks = (list, label) => list.map((v) => ({ v, label: label(v) }));
        charts.passk.update({
          // the whole curve of every model in words: the line kind has no keyboard focus
          title: pkItems.length ? 'pass^k against k, one line per model. ' + pkItems.map((i) => {
            const M = Math.min(i.n, K), shown = i.values.slice(0, M).map(AS.fmtPassK).join(', ');
            return `${name(i)}: ${shown} ` + (M === 1 ? 'at k = 1.' : `for k = 1 to ${M}.`);
          }).join(' ') : 'pass^k against k. ' + emptyText,
          xLabel: 'k, runs that must all pass', yLabel: 'pass^k',
          // half a step and 8 % of room, so the first and last points, a flat 100 % line and a 0 % point sit
          // clear of the frame, with the rings of up to three models that share the point
          xMin: 0.5, xMax: K + 0.5, xTicks: ticks(Array.from({ length: K }, (_, j) => j + 1), String),
          yMin: -0.08, yMax: 1.08, yTicks: ticks([0, 0.25, 0.5, 0.75, 1], (v) => v * 100 + ' %'),
          fmtY: AS.fmtPassK, tipHead: (k) => 'k = ' + k,
          // Models with the same value at a k lie on one point. No value is moved to part them: the
          // chart rings the point once per model under the top one, each ring in that model's colour.
          rings: true,
          // most runs first: where two curves coincide the shorter one is drawn last and stays visible
          series: pkItems.slice().sort((a, b) => b.n - a.n).map((i) => ({ name: name(i), color: AS.modelColor(i.model), dots: true, width: 1.6,
            points: i.values.slice(0, Math.min(i.n, K)).map((v, j) => [j + 1, v]) })),
          legend: pkItems.map((i) => ({ name: name(i), color: AS.modelColor(i.model) })),
          emptyText,
        });
      }

      const sig = JSON.stringify([data.runsSig, byModel]);
      if (el.__chartSig !== sig) {
        el.__chartSig = sig;
        const order = modelsInOrder(rows);
        AS.clear(legend);
        for (const m of order) legend.appendChild(h('span', { class: 'chart-key' }, h('i', { class: 'chart-swatch dot', style: { color: AS.modelColor(m) } }), AS.modelLabel(m)));
        const points = (xk, yk) => rows.filter((r) => AS.isNum(r[xk]) && AS.isNum(r[yk])).map((r) => ({ x: r[xk], y: r[yk], color: AS.modelColor(r.model),
          label: `NACA ${r.naca} · ${AS.modelLabel(r.model)} · ${AS.fmtDate(r.ts)}` }));
        charts.parityCl.update({ points: points('xfoil_cl', 'cl'), tol: 0.05, xLabel: 'XFoil Cl', yLabel: 'OpenFOAM Cl' });
        charts.parityCd.update({ points: points('xfoil_cd', 'cd'), tol: 0.25, xLabel: 'XFoil Cd', yLabel: 'OpenFOAM Cd' });
        const sorted = byModel.slice().sort((a, b) => order.indexOf(a.model) - order.indexOf(b.model));
        const bars = (key, fmt, yLabel) => ({ yLabel, fmt, items: sorted.map((g) => ({ label: AS.modelLabel(g.model).split(' ')[0], fullLabel: AS.modelLabel(g.model), value: g[key] == null ? null : Number(g[key]),
          color: AS.modelColor(g.model), note: `${g.runs} run${g.runs === 1 ? '' : 's'}` })) });
        charts.wall.update(bars('median_wall_seconds', (v) => Math.round(v) + ' s', 'seconds'));
        charts.tokens.update(bars('median_tokens', AS.fmtTokens, 'tokens'));
        charts.cost.update(bars('mean_cost_usd', AS.fmtCost, 'USD'));
        charts.rework.update(bars('mean_rework_loops', (v) => v.toFixed(2), 'loops'));
        charts.human.update(bars('mean_human_interventions', (v) => v.toFixed(2), 'interventions'));
      }
      AS.region(tableWrap, [data.runsSig, sort.key, sort.dir], () => (rows.length ? runTable(rows) : null));
    }
    return { build, render };
  })();
})();
