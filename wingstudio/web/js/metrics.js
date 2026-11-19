/* Wing Studio - the metrics rules of store.py, once for the page and the mock: the summary of a
   set of runs, the trial rule and pass^k. */
(function () {
  'use strict';
  const AS = window.AS;

  /* The summary of a set of metrics rows, with the keys and numbers of the server's
     /api/metrics/summary (store.Store._aggregate). The Dashboard uses it for filtered views,
     which the endpoint cannot give, and the mock to answer the endpoint itself. */
  AS.aggregateRuns = function (rows) {
    const rate = (key) => (rows.length ? rows.filter((r) => AS.truthy(r[key])).length / rows.length : null);
    const abs = (key) => rows.filter((r) => AS.isNum(r[key])).map((r) => Math.abs(r[key]));
    const mean = (key) => (rows.length ? AS.sum(rows.map((r) => r[key] || 0)) / rows.length : null);
    const sum = (key) => AS.sum(rows.map((r) => r[key]));
    // rows stored before the cached count existed do not carry it: unknown is null, never a zero
    const cached = rows.some((r) => AS.isNum(r.tokens_cached)) ? sum('tokens_cached') : null;
    const verdicts = (v) => rows.filter((r) => r.verdict === v).length;
    return {
      runs: rows.length, successes: rows.filter((r) => AS.truthy(r.success)).length,
      success_rate: rate('success'), first_pass_rate: rate('first_pass'), convergence_rate: rate('forces_converged'),
      median_abs_cl_err_pct: AS.median(abs('cl_err_pct')), median_abs_cd_err_pct: AS.median(abs('cd_err_pct')),
      median_wall_seconds: AS.median(rows.map((r) => r.wall_seconds)), median_solver_seconds: AS.median(rows.map((r) => r.solver_seconds)),
      median_tokens: AS.median(rows.map((r) => (r.tokens_in || 0) + (r.tokens_out || 0))),
      tokens_in: sum('tokens_in'), tokens_out: sum('tokens_out'), tokens_cached: cached,
      cost_usd: +sum('cost_usd').toFixed(6), mean_cost_usd: mean('cost_usd'),
      llm_calls: sum('llm_calls'), tool_calls: sum('tool_calls'),
      rework_loops: sum('rework_loops'), mean_rework_loops: mean('rework_loops'),
      gate_failures: sum('gate_failures'), mean_gate_failures: mean('gate_failures'),
      human_interventions: sum('human_interventions'), mean_human_interventions: mean('human_interventions'),
      verdicts: { pass: verdicts('pass'), warn: verdicts('warn'), fail: verdicts('fail') },
    };
  };

  /* ---------- pass^k, pooled per model ----------
     After Yao, Shinn, Razavi, Narasimhan, tau-bench (2024), arXiv:2406.12045, section 3: with c passes
     in n trials, pass^k = C(c, k) / C(n, k) for 1 <= k <= n. Here all runs of a model under the
     filters are the trials of one task, whatever their geometry. The same rules and the same
     arithmetic as store.py (is_trial, pass_k_values, pass_k), so server, mock and page agree. */

  /* The pass criteria, nested: first_pass implies unaided implies success. */
  AS.PASS_CRITERIA = [
    { id: 'success', label: 'Success (pass or warn)',
      explain: 'A run passes when the case finished with verdict pass or warn against XFoil. Actions by the engineer and re-runs do not count against it.',
      test: (r) => AS.truthy(r.success) },
    // help_interventions counts only the actions that helped the case (steer, re-run, plan edit, retry
    // approval); rows from before that column fall back to the count of every action
    { id: 'unaided', label: 'Without help',
      explain: 'A run passes when the case finished with verdict pass or warn and the engineer did not steer it, re-run a stage, edit the plan or approve a retry. Routine gate approvals, pause and mode changes do not count.',
      test: (r) => AS.truthy(r.success) && !(Number(r.help_interventions != null ? r.help_interventions : r.human_interventions) > 0) },
    { id: 'first_pass', label: 'First pass (no rework or help)',
      explain: 'A run passes when the case finished with verdict pass or warn with no rework, no failed gate and no engineer action of any kind. Approvals in gate mode count as actions.',
      test: (r) => AS.truthy(r.first_pass) },
  ];

  /* A row is a trial when the case reached an outcome of its own. A case the engineer stopped or a
     restart ended is not the model's failure: it is left out of n and c, and counted (left_out).
     Rows from before the status column are judged by what they recorded. */
  AS.isTrial = function (r) {
    if (r.status != null && r.status !== '') return r.status === 'done' || r.status === 'failed';
    return AS.truthy(r.success) || !!r.failed_stage || r.verdict === 'fail';
  };

  /* values[k - 1] = pass^k for k = 1..n, as a running product in the order (v * (c - i)) / (n - i),
     the same as the server's, so both give the same doubles. Without the guard i < c the factor
     turns negative after the product reached zero and gives -0. c is held to 0..n as on the
     server, so a count that is out of range never gives a chance above 1; n < 0 gives []. */
  AS.passKValues = function (n, c) {
    const values = [];
    c = Math.max(0, Math.min(c, n));
    let v = 1;
    for (let i = 0; i < n; i++) {
      v = i < c ? (v * (c - i)) / (n - i) : 0;
      values.push(v);
    }
    return values;
  };

  /* { left_out, success: { n, c, values }, unaided: {...}, first_pass: {...} } for the rows of one
     model; n is the number of trials, the same for the three criteria. */
  AS.passK = function (rows) {
    const trials = rows.filter(AS.isTrial), n = trials.length;
    const out = { left_out: rows.length - n };
    for (const crit of AS.PASS_CRITERIA) {
      const c = trials.filter(crit.test).length;
      out[crit.id] = { n, c, values: AS.passKValues(n, c) };
    }
    return out;
  };
})();
