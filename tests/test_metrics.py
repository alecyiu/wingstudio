"""pass^k: the estimator, the trial rule, the three pass criteria, the help count behind
`unaided`, the metrics row the engine writes, and the page and mock that compute the same.

pass^k = C(c, k) / C(n, k) per model over its trials, of any geometry (Yao et al. 2024,
arXiv:2406.12045, section 3, with a model's runs pooled as one task). Stores, databases and
event files are temporary: nothing under studio_data/ or cases/ is read or written, and the
server on 8800 is not involved.
"""
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import support
from support import HAIKU, SONNET, Store, engine

from wingstudio import runtime, store
from wingstudio.cfd import solver
from wingstudio.study import planning

FIXTURE_RUNS = os.path.join(support.STUDIO, "web", "fixtures", "metrics_runs.json")


def positive(values):
    """Every chance in [0, 1] and none of them a negative zero."""
    return all(0.0 <= v <= 1.0 and math.copysign(1, v) == 1 for v in values)


class Arithmetic(unittest.TestCase):
    def test_the_vector_the_page_shares(self):
        # n = 7, c = 4, as JSON carries it: the doubles survive unchanged
        values = store.pass_k_values(7, 4)
        self.assertEqual(json.dumps(values), "[0.5714285714285714, 0.2857142857142857, 0.11428571428571428, "
                                             "0.02857142857142857, 0.0, 0.0, 0.0]")
        self.assertEqual(json.loads(json.dumps(values)), values)

    def test_matches_binomials(self):
        for n in range(0, 13):
            for c in range(0, n + 1):
                values = store.pass_k_values(n, c)
                self.assertEqual(len(values), n)
                for k in range(1, n + 1):
                    self.assertLess(abs(values[k - 1] - math.comb(c, k) / math.comb(n, k)), 1e-12, (n, c, k))
                    self.assertEqual(values[k - 1] == 0.0, k > c, (n, c, k))
                self.assertEqual(values, sorted(values, reverse=True), (n, c))
                self.assertTrue(positive(values), (n, c))
        for n in range(1, 51):
            self.assertEqual(store.pass_k_values(n, n), [1.0] * n)            # all pass is exactly one

    def test_c_is_held_to_the_trials(self):
        # more passes than trials gave chances above 1
        self.assertEqual(store.pass_k_values(3, 5), [1.0, 1.0, 1.0])
        self.assertEqual(store.pass_k_values(3, -2), [0.0, 0.0, 0.0])
        self.assertEqual(store.pass_k_values(-4, 9), [])
        for n in range(-2, 13):
            for c in range(-3, n + 4):
                values = store.pass_k_values(n, c)
                self.assertEqual(len(values), max(n, 0), (n, c))
                self.assertTrue(positive(values), (n, c))


class Trials(unittest.TestCase):
    """a row is a trial when the case reached an outcome of its own."""

    def test_a_known_status_decides(self):
        self.assertTrue(store.is_trial({"status": "done", "success": False}))
        self.assertTrue(store.is_trial({"status": "done", "success": True}))
        self.assertTrue(store.is_trial({"status": "failed"}))
        # stopped is not the model's failure, whatever else the row holds
        self.assertFalse(store.is_trial({"status": "stopped"}))
        self.assertFalse(store.is_trial({"status": "stopped", "verdict": "fail", "failed_stage": "solve"}))
        self.assertFalse(store.is_trial({"status": "something else", "success": True}))

    def test_a_row_from_before_the_column_is_judged_by_what_it_holds(self):
        for row in ({"success": True}, {"success": 1}, {"failed_stage": "mesh"}, {"verdict": "fail"},
                    {"status": None, "success": True}, {"status": "", "failed_stage": "solve"}):
            self.assertTrue(store.is_trial(row), row)
        for row in ({}, {"success": False}, {"success": 0, "failed_stage": None, "verdict": None},
                    {"verdict": "pass"}, {"verdict": "warn", "success": False}, {"failed_stage": ""}):
            self.assertFalse(store.is_trial(row), row)

    def test_run_status(self):
        self.assertEqual(store.run_status("done"), "done")
        self.assertEqual(store.run_status("done", None), "done")
        # the provisional row of a failed stage, and a case closed while a stage stands failed
        self.assertEqual(store.run_status("failed", "mesh"), "failed")
        self.assertEqual(store.run_status("stopped", "mesh"), "failed")
        # stopped by the engineer, ended by a restart, or a crash of the pipeline: no outcome
        self.assertEqual(store.run_status("stopped"), "stopped")
        self.assertEqual(store.run_status("failed"), "stopped")
        for ended in ("done", "failed", "stopped"):
            for failed in (None, "solve"):
                self.assertIn(store.run_status(ended, failed), store.RUN_STATUSES)


def rows_a_to_f(true=True, false=False):
    return [
        {"id": "A", "success": true, "human_interventions": 0, "first_pass": true},
        {"id": "B", "success": true, "human_interventions": 0, "first_pass": false, "gate_failures": 1},
        {"id": "C", "success": true, "human_interventions": 2, "first_pass": false},
        {"id": "D", "success": true, "first_pass": false},                       # no count at all
        {"id": "E", "success": false, "failed_stage": "solve"},
        {"id": "F", "success": false},                                           # no outcome: not a trial
    ]


class Criteria(unittest.TestCase):
    def test_criteria(self):
        for flags in ((True, False), (1, 0)):
            out = store.pass_k(rows_a_to_f(*flags))
            self.assertEqual(set(out), {"left_out", "success", "unaided", "first_pass"})
            self.assertEqual(out["left_out"], 1)
            for key in store.PASS_CRITERIA:
                self.assertEqual(set(out[key]), {"n", "c", "values"})
                self.assertEqual(out[key]["n"], 5)
                self.assertEqual(out[key]["values"], store.pass_k_values(5, out[key]["c"]))
            self.assertEqual([out[key]["c"] for key in store.PASS_CRITERIA], [4, 3, 1])
            self.assertTrue(out["first_pass"]["c"] <= out["unaided"]["c"] <= out["success"]["c"] <= out["success"]["n"])
        self.assertEqual(store.PASS_CRITERIA, ("success", "unaided", "first_pass"))

    def test_the_help_count_decides_unaided_where_it_is_known(self):
        rows = rows_a_to_f()
        rows[2]["help_interventions"] = 0            # C: two actions, none of them help (an approval, a mode change)
        self.assertEqual(store.pass_k(rows)["unaided"]["c"], 4)
        rows[0]["help_interventions"] = 1            # a count that is known wins over human_interventions = 0
        self.assertEqual(store.pass_k(rows)["unaided"]["c"], 3)
        rows[3].update(help_interventions=None, human_interventions=None)        # both unknown: counted as none
        self.assertEqual(store.pass_k(rows)["unaided"]["c"], 3)
        # a help count never makes a pass of a run that did not succeed
        self.assertEqual(store.pass_k([{"status": "done", "success": False, "help_interventions": 0}])["unaided"]["c"], 0)

    def test_rows_that_are_no_trial_are_left_out_of_n_and_c(self):
        rows = [{"status": "done", "success": True, "first_pass": True, "help_interventions": 0},
                {"status": "done", "success": False, "verdict": "fail"},
                {"status": "failed", "failed_stage": "mesh"},
                {"status": "stopped"}, {"status": "stopped", "failed_stage": None, "verdict": "fail"}]
        out = store.pass_k(rows)
        self.assertEqual(out["left_out"], 2)
        self.assertEqual({k: (out[k]["n"], out[k]["c"]) for k in store.PASS_CRITERIA},
                         {"success": (3, 1), "unaided": (3, 1), "first_pass": (3, 1)})
        self.assertAlmostEqual(out["success"]["values"][0], 1 / 3, delta=1e-12)
        only_stopped = store.pass_k([{"status": "stopped"}])
        self.assertEqual((only_stopped["left_out"], only_stopped["success"]), (1, {"n": 0, "c": 0, "values": []}))

    def test_empty(self):
        self.assertEqual(store.pass_k([]), {"left_out": 0, "success": {"n": 0, "c": 0, "values": []},
                                            "unaided": {"n": 0, "c": 0, "values": []},
                                            "first_pass": {"n": 0, "c": 0, "values": []}})

    def test_fixture_numbers(self):
        # the numbers the mock page must show, pinned to the same 14 rows
        rows = support.read_json(FIXTURE_RUNS)
        self.assertEqual(len(rows), 14)

        def counts(rows, model):
            out = store.pass_k([r for r in rows if r["model"] == model])
            self.assertEqual(out["left_out"], 0, model)
            return (out["success"]["n"], out["success"]["c"], out["unaided"]["c"], out["first_pass"]["c"])
        wing = [r for r in rows if r["project_id"] == "naca-wing"]
        self.assertEqual(len(wing), 12)
        self.assertEqual(counts(wing, HAIKU), (4, 3, 1, 1))
        self.assertEqual(counts(wing, SONNET), (4, 4, 4, 3))
        self.assertEqual(counts(wing, "anthropic/claude-opus-5-5"), (2, 2, 2, 2))
        self.assertEqual(counts(wing, "scripted"), (2, 2, 2, 2))
        self.assertEqual(counts(rows, SONNET), (6, 6, 6, 5))
        self.assertEqual(store.pass_k([r for r in wing if r["model"] == HAIKU])["success"]["values"],
                         [0.75, 0.5, 0.25, 0.0])
        values = store.pass_k([r for r in rows if r["model"] == SONNET])["first_pass"]["values"]
        for got, want in zip(values, [5 / 6, 2 / 3, 1 / 2, 1 / 3, 1 / 6, 0]):
            self.assertAlmostEqual(got, want, delta=1e-12)
        self.assertEqual(len(values), 6)


def run_row(n, model=HAIKU, naca="2412", project="naca-wing", **more):
    row = {"id": f"case{n}", "ts": f"2026-11-18T10:00:{n:02d}.000Z", "project_id": project, "study_id": f"s{n}",
           "case_id": f"case{n}", "model": model, "naca": naca, "success": True, "first_pass": True,
           "verdict": "pass", "human_interventions": 0, "help_interventions": 0, "status": "done"}
    row.update(more)
    return row


class SummaryRoute(support.ServerCase):
    def record(self):
        for n in range(1, 5):
            self.store.record_run(run_row(n, naca="2412" if n % 2 else "4412"))
        for n, (actions, help_) in zip(range(5, 8), ((2, 2), (3, 1), (1, 1))):
            self.store.record_run(run_row(n, naca="4412", first_pass=False, human_interventions=actions,
                                          help_interventions=help_))
        for n in (8, 9):
            self.store.record_run(run_row(n, model="scripted", naca="2412" if n % 2 else "4412"))

    def summary(self, query=""):
        answer = self.client.get("/api/metrics/summary" + query)
        self.assertEqual(answer.status_code, 200)
        return answer.json()

    def test_summary_route(self):
        self.record()
        summary = self.summary()
        by_model = {g["model"]: g for g in summary["by_model"]}
        self.assertEqual(set(by_model), {HAIKU, "scripted"})
        haiku = by_model[HAIKU]["pass_k"]
        self.assertEqual(set(haiku), {"left_out", "success", "unaided", "first_pass"})
        self.assertEqual(haiku["left_out"], 0)
        self.assertEqual(haiku["success"], {"n": 7, "c": 7, "values": [1.0] * 7})
        self.assertEqual((haiku["unaided"]["n"], haiku["unaided"]["c"]), (7, 4))
        self.assertEqual(haiku["unaided"]["values"], store.pass_k_values(7, 4))      # the doubles survive JSON
        self.assertEqual(haiku["first_pass"]["c"], 4)
        self.assertEqual(by_model["scripted"]["pass_k"]["success"], {"n": 2, "c": 2, "values": [1.0, 1.0]})
        for group in summary["by_model"]:
            for key in store.PASS_CRITERIA:
                self.assertEqual(group["pass_k"][key]["n"], group["runs"])
                self.assertEqual(len(group["pass_k"][key]["values"]), group["runs"])
            # no row is left out here, so pass^1 is the rate the page already shows
            self.assertEqual(group["pass_k"]["success"]["values"][0], group["success_rate"])
            self.assertEqual(group["pass_k"]["first_pass"]["values"][0], group["first_pass_rate"])
        self.assertNotIn("pass_k", summary["totals"])
        self.assertTrue(summary["by_geometry"])
        for group in summary["by_geometry"]:
            self.assertNotIn("pass_k", group)

    def test_an_action_that_is_not_help_does_not_cost_unaided(self):
        self.record()
        # an approval in gate mode and a mode change: two actions, no help
        self.store.record_run(run_row(10, first_pass=False, human_interventions=2, help_interventions=0))
        haiku = next(g for g in self.summary()["by_model"] if g["model"] == HAIKU)["pass_k"]
        self.assertEqual([(haiku[k]["n"], haiku[k]["c"]) for k in store.PASS_CRITERIA], [(8, 8), (8, 5), (8, 4)])

    def test_summary_project_filter(self):
        self.record()
        self.store.record_run(run_row(10, project="other"))
        haiku = lambda body: next(g for g in body["by_model"] if g["model"] == HAIKU)   # noqa: E731
        self.assertEqual(haiku(self.summary("?project_id=naca-wing"))["pass_k"]["success"]["n"], 7)
        self.assertEqual(haiku(self.summary())["pass_k"]["success"]["n"], 8)

    def test_stopped_runs_are_left_out_and_counted(self):
        self.record()
        self.store.record_run(run_row(10, success=False, first_pass=False, verdict=None, status="stopped"))
        self.store.record_run(run_row(11, success=False, first_pass=False, verdict=None, status="failed",
                                      failed_stage="mesh"))
        group = next(g for g in self.summary()["by_model"] if g["model"] == HAIKU)
        self.assertEqual(group["runs"], 9)
        self.assertEqual(group["pass_k"]["left_out"], 1)
        self.assertEqual(group["pass_k"]["success"]["n"], 8)
        self.assertEqual(group["pass_k"]["success"]["c"], 7)
        # with a row left out pass^1 is no longer the success rate of all rows
        self.assertEqual(group["pass_k"]["success"]["values"][0], 7 / 8)
        self.assertEqual(group["success_rate"], 7 / 9)
        runs = {r["id"]: r for r in self.client.get("/api/metrics/runs").json()["runs"]}
        self.assertEqual((runs["case10"]["status"], runs["case11"]["status"], runs["case1"]["help_interventions"]),
                         ("stopped", "failed", 0))

    def test_cached_tokens(self):
        # a group without any count is unknown, not zero; one row with the count is enough
        for n, model, naca, cached in ((1, "old-model", "2412", None), (2, "old-model", "4412", None),
                                       (3, HAIKU, "2412", 600), (4, HAIKU, "2412", None), (5, "scripted", "2412", 0)):
            self.store.record_run(run_row(n, model=model, naca=naca, tokens_in=1000, tokens_cached=cached))
        runs = self.client.get("/api/metrics/runs").json()["runs"]
        self.assertEqual({r["id"]: r["tokens_cached"] for r in runs},
                         {"case1": None, "case2": None, "case3": 600, "case4": None, "case5": 0})
        summary = self.summary()
        self.assertEqual({g["model"]: (g["tokens_in"], g["tokens_cached"]) for g in summary["by_model"]},
                         {"old-model": (2000, None), HAIKU: (2000, 600), "scripted": (1000, 0)})
        self.assertEqual((summary["totals"]["tokens_in"], summary["totals"]["tokens_cached"]), (5000, 600))
        self.assertEqual({g["naca"]: g["tokens_cached"] for g in summary["by_geometry"]}, {"2412": 600, "4412": None})

    def test_no_runs_at_all(self):
        totals = self.summary()["totals"]
        self.assertEqual((totals["runs"], totals["tokens_in"], totals["tokens_cached"]), (0, 0, None))

    def test_a_case_from_before_the_cached_count_writes_none(self):
        study = support.store_study(self.engine)
        old = support.make_runner(self.engine, study, naca="2412")
        new = support.make_runner(self.engine, study, naca="4412")
        self.store.update_case(study["id"], old.cid, lambda c: c["usage"].pop("tokens_cached"))
        self.store.update_case(study["id"], new.cid, lambda c: c["usage"].update(tokens_in=900, tokens_cached=500))
        for runner in (old, new):
            self.engine.write_metrics(study["id"], runner.cid, "done")
        self.assertEqual(support.events(self.engine, study["id"], "error"), [])
        rows = {r["id"]: r for r in self.store.query_runs()}
        self.assertIsNone(rows[old.cid]["tokens_cached"])
        self.assertEqual((rows[new.cid]["tokens_in"], rows[new.cid]["tokens_cached"]), (900, 500))
        self.assertEqual({g["naca"]: g["tokens_cached"] for g in self.summary()["by_geometry"]},
                         {"2412": None, "4412": 500})


class OldDatabase(unittest.TestCase):
    """A metrics.db from before the two columns: the ALTER TABLE path adds them as NULL."""

    def test_the_columns_are_added_and_old_rows_use_the_rules_for_null(self):
        folder = tempfile.mkdtemp(prefix="studio-data-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        old = [(name, kind) for name, kind in store.RUN_COLUMNS if name not in ("status", "help_interventions")]
        self.assertEqual(len(old), len(store.RUN_COLUMNS) - 2)
        conn = sqlite3.connect(os.path.join(folder, "metrics.db"))
        conn.execute("CREATE TABLE runs (" + ", ".join(f"{n} {k}" for n, k in old) + ")")
        rows = [("a", HAIKU, 1, 1, 0, None, "pass"), ("b", HAIKU, 1, 0, 2, None, "pass"),
                ("c", HAIKU, 0, 0, 1, "solve", None), ("d", HAIKU, 0, 0, 1, None, None)]
        conn.executemany("INSERT INTO runs (id, model, success, first_pass, human_interventions, failed_stage, verdict) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        s = Store(folder)
        got = {r["id"]: r for r in s.query_runs()}
        self.assertEqual(len(got), 4)
        for r in got.values():
            self.assertIsNone(r["status"])
            self.assertIsNone(r["help_interventions"])
        out = s.metrics_summary()["by_model"][0]["pass_k"]
        self.assertEqual(out["left_out"], 1)                                     # d: stopped, no outcome
        self.assertEqual([(out[k]["n"], out[k]["c"]) for k in store.PASS_CRITERIA], [(3, 2), (3, 1), (3, 1)])
        # a new row beside them carries both
        s.record_run(run_row(1, human_interventions=1, first_pass=False))
        self.assertEqual((s.query_runs()[0]["status"], s.query_runs()[0]["help_interventions"]), ("done", 0))


def nested(row):
    """Whether the three criteria are nested on one row: first_pass implies unaided implies success."""
    first, unaided, success = (store._passes(row, c) for c in ("first_pass", "unaided", "success"))
    return (not first or unaided) and (not unaided or success)


class EngineCase(unittest.TestCase):
    """An engine on temporary data with two cases of one study, no thread and no solver."""
    MODEL = "scripted"

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.study = support.store_study(self.eng, support.make_study(model=self.MODEL))
        self.sid = self.study["id"]
        self.runner = support.make_runner(self.eng, self.study)
        self.other = support.make_runner(self.eng, self.eng.store.get_study(self.sid), naca="4412")
        self.cid, self.cid2 = self.runner.cid, self.other.cid
        # no solver to kill, no Docker to ask for left-over containers when an engine starts on
        # open cases, and no worker thread (the report, the steering agent) that would still
        # write into the temporary store after the test has removed it
        support.patch(self, solver, "kill_solver")
        support.patch(self, self.eng, "spawn")
        support.patch(self, engine.Engine, "reap_solver_containers", return_value=[])

    def counters(self, cid=None):
        return self.eng.store.get_case(self.sid, cid or self.cid)["counters"]

    def fresh(self):
        """A new engine on new data, for the next variant inside one test."""
        self.doCleanups()
        self.setUp()

    def last(self):
        return support.events(self.eng, self.sid, "human.intervention")[-1]

    def row(self, cid=None):
        row = next(r for r in self.eng.store.query_runs() if r["id"] == (cid or self.cid))
        self.assertTrue(nested(row), row)                                         # on every row a test reads
        return row

    def finish(self, cid=None, verdict="pass"):
        def end(c):
            c.update(status="done", verdict=verdict)
            for s in c["stages"]:
                s["status"] = "passed"
        self.eng.store.update_case(self.sid, cid or self.cid, end)
        self.eng.write_metrics(self.sid, cid or self.cid, "done")
        return self.row(cid)


class HelpCount(EngineCase):
    """which engineer actions count as help, and what the row records."""

    def test_every_kind_is_classified_once(self):
        self.assertEqual(store.HELP_KINDS, ("steer", "rerun", "plan_edit", "retry_approval"))
        self.assertEqual(store.OTHER_KINDS, ("gate_decision", "pause", "resume", "stop", "mode_change"))
        self.assertFalse(set(store.HELP_KINDS) & set(store.OTHER_KINDS))
        self.assertIs(engine.HELP_KINDS, store.HELP_KINDS)
        # every kind the engine passes to intervention() is in one of the two lists
        with open(engine.__file__, encoding="utf-8") as f:
            source = f.read()
        called = set(re.findall(r'self\.intervention\(sid, "([a-z_]+)"', source))
        self.assertEqual(called, {"rerun", "plan_edit", "mode_change", "retry_approval", "steer"})
        self.assertIn('self.intervention(sid, "retry_approval" if retry else "gate_decision", [cid])', source)
        self.assertIn("self.intervention(sid, action, live)", source)             # pause, resume, stop
        self.assertEqual(source.count("self.intervention("), 9)                   # a new call must be classified here
        self.assertEqual(called | {"gate_decision", "pause", "resume", "stop"},
                         set(store.HELP_KINDS) | set(store.OTHER_KINDS))
        # the steering agent's tools count through the engine too: one call, a help kind
        with open(runtime.__file__, encoding="utf-8") as f:
            study_runtime = f.read()
        self.assertEqual(re.findall(r'self\.engine\.intervention\(self\.sid, "([a-z_]+)"', study_runtime), ["plan_edit"])
        self.assertEqual(source.count(".intervention(") + study_runtime.count(".intervention("), 10)

    def test_help_kinds_raise_both_counters_and_the_others_one(self):
        human = 0
        for helped, kind in enumerate(store.HELP_KINDS, 1):
            self.eng.intervention(self.sid, kind, [self.cid])
            human += 1
            self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]),
                             (human, helped), kind)
        for kind in store.OTHER_KINDS:
            self.eng.intervention(self.sid, kind, [self.cid])
            human += 1
            self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]),
                             (human, len(store.HELP_KINDS)), kind)
        self.assertEqual(self.counters(self.cid2), {"rework_loops": 0, "gate_failures": 0, "human_interventions": 0,
                                                    "help_interventions": 0})

    def test_the_event_names_the_cases_the_counters_were_raised_for(self):
        self.eng.intervention(self.sid, "steer", [self.cid, self.cid2])
        ev = self.last()
        self.assertEqual((ev["data"], ev["case_id"]), ({"kind": "steer", "case_ids": [self.cid, self.cid2]}, None))
        self.eng.intervention(self.sid, "mode_change", [self.cid, None])
        ev = self.last()
        self.assertEqual((ev["data"], ev["case_id"]), ({"kind": "mode_change", "case_ids": [self.cid]}, self.cid))
        self.eng.intervention(self.sid, "steer")                                  # a comment to the planner
        self.assertEqual(self.last()["data"], {"kind": "steer", "case_ids": []})
        self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]), (2, 1))
        self.assertEqual((self.counters(self.cid2)["human_interventions"],
                          self.counters(self.cid2)["help_interventions"]), (1, 1))

    def test_the_counts_can_be_rebuilt_from_the_events(self):
        for kind, cases in (("steer", [self.cid, self.cid2]), ("gate_decision", [self.cid]), ("retry_approval", [self.cid]),
                            ("pause", [self.cid, self.cid2]), ("rerun", [self.cid2]), ("plan_edit", [])):
            self.eng.intervention(self.sid, kind, cases)
        events = support.events(self.eng, self.sid, "human.intervention")
        for cid in (self.cid, self.cid2):
            mine = [e for e in events if cid in e["data"]["case_ids"]]
            self.assertEqual(len(mine), self.counters(cid)["human_interventions"])
            self.assertEqual(len([e for e in mine if store.intervention_kind(e["data"]) in store.HELP_KINDS]),
                             self.counters(cid)["help_interventions"])

    def approve(self, failed):
        """Let the runner wait for an approval as run() does, and grant it through the engine."""
        answer = []
        waiter = threading.Thread(target=lambda: answer.append(
            self.runner.await_approval("mesh", "gate failed 3 times in a row" if failed else "Gate mode: approve to "
                                       "run Mesh", failed=failed)), daemon=True)
        if not failed:
            self.eng.store.update_study(self.sid, lambda s: s.__setitem__("mode", "gate"))
        waiter.start()
        for _ in range(200):
            if self.runner.waiting == "mesh":
                break
            time.sleep(0.01)
        with mock.patch.object(self.runner, "alive", return_value=True):
            self.eng.approve_case(self.sid, self.cid)
        waiter.join(timeout=5)
        self.assertEqual(answer, ["approved"])
        self.assertEqual((self.runner.waiting, self.runner.waiting_retry), (None, False))
        return self.last()

    def test_the_approval_of_a_retry_is_help(self):
        ev = self.approve(failed=True)
        # written as the gate decision it also is, told apart by `retry`
        self.assertEqual(ev["data"], {"kind": "gate_decision", "case_ids": [self.cid], "retry": True})
        self.assertEqual(store.intervention_kind(ev["data"]), "retry_approval")
        self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]), (1, 1))

    def test_the_approval_to_run_a_stage_in_gate_mode_is_not(self):
        ev = self.approve(failed=False)
        self.assertEqual(ev["data"], {"kind": "gate_decision", "case_ids": [self.cid]})
        self.assertEqual(store.intervention_kind(ev["data"]), "gate_decision")
        self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]), (1, 0))

    def test_the_engine_paths_use_the_kinds(self):
        self.eng.set_mode(self.sid, "gate")                                       # no live pipeline: no case named
        self.assertEqual(self.last()["data"], {"kind": "mode_change", "case_ids": []})
        with mock.patch.object(self.eng, "_alive", return_value=True):
            self.eng.set_mode(self.sid, "auto")
            self.eng.message(self.sid, "use a finer mesh", case_id=self.cid)
        self.assertEqual(self.last()["data"], {"kind": "steer", "case_ids": [self.cid]})
        self.assertEqual((self.counters()["human_interventions"], self.counters()["help_interventions"]), (2, 1))
        self.assertEqual((self.counters(self.cid2)["human_interventions"],
                          self.counters(self.cid2)["help_interventions"]), (1, 0))
        body = self.eng.store.get_study(self.sid)["plan"]
        body["cases"][0]["stages"]["mesh"]["n_wake"] = 60
        self.eng.patch_plan(self.sid, body)                                       # the study is running
        self.assertEqual(self.last()["data"], {"kind": "plan_edit", "case_ids": [self.cid]})   # PlanEdit has the rule
        self.assertEqual((self.counters()["help_interventions"], self.counters(self.cid2)["help_interventions"]), (2, 0))

    def test_the_row_records_status_and_help(self):
        self.eng.intervention(self.sid, "gate_decision", [self.cid])
        self.eng.intervention(self.sid, "mode_change", [self.cid, self.cid2])
        row = self.finish()
        self.assertEqual((row["status"], row["success"], row["human_interventions"], row["help_interventions"],
                          row["first_pass"]), ("done", True, 2, 0, False))
        self.assertEqual(store.pass_k([row])["unaided"]["c"], 1)                  # not help: still unaided
        self.assertEqual(store.pass_k([row])["first_pass"]["c"], 0)               # first_pass keeps its meaning
        self.eng.intervention(self.sid, "rerun", [self.cid])
        row = self.finish()
        self.assertEqual((row["human_interventions"], row["help_interventions"]), (3, 1))
        self.assertEqual(store.pass_k([row])["unaided"]["c"], 0)
        clean = self.finish(self.cid2, verdict="fail")
        self.assertEqual((clean["status"], clean["success"], clean["help_interventions"]), ("done", False, 0))
        self.assertTrue(store.is_trial(clean))

    def test_a_failed_stage_is_a_trial_also_when_the_case_is_then_stopped(self):
        self.eng.store.update_case(self.sid, self.cid, lambda c: c["stages"][1].__setitem__("status", "failed"))
        self.eng.write_metrics(self.sid, self.cid, "failed", failed_stage="mesh")  # the provisional row
        self.assertEqual((self.row()["status"], self.row()["failed_stage"]), ("failed", "mesh"))
        with self.runner.cv:
            self.runner.stop_flag = True
        self.runner.finish("stopped")                                             # the engineer pressed Stop
        row = self.row()
        self.assertEqual((row["status"], row["failed_stage"], row["success"]), ("failed", "mesh", False))
        self.assertTrue(store.is_trial(row))
        self.assertEqual(self.eng.store.get_case(self.sid, self.cid)["status"], "stopped")

    def test_a_stop_without_a_failed_stage_and_a_crash_are_no_trial(self):
        self.runner.finish("stopped")
        self.assertEqual((self.row()["status"], self.row()["failed_stage"]), ("stopped", None))
        self.assertFalse(store.is_trial(self.row()))
        self.other.finish("failed")                                               # what run() does when it crashes
        row = self.row(self.cid2)
        self.assertEqual((row["status"], row["failed_stage"]), ("stopped", None))
        self.assertFalse(store.is_trial(row))
        self.assertEqual(self.eng.store.get_case(self.sid, self.cid2)["status"], "failed")

    def test_a_stop_of_a_case_without_a_pipeline_names_its_failed_stage(self):
        self.eng.store.update_case(self.sid, self.cid, lambda c: c["stages"][4].__setitem__("status", "failed"))
        self.eng.control(self.sid, "stop")                                        # no runner is alive
        self.assertEqual((self.row()["status"], self.row()["failed_stage"]), ("failed", "solve"))
        self.assertEqual((self.row(self.cid2)["status"], self.row(self.cid2)["failed_stage"]), ("stopped", None))

    def test_a_restart_records_stopped(self):
        again = engine.Engine(Store(self.eng.store.data_dir), viewer_factory=None)   # recover() closes what was open
        rows = {r["id"]: r for r in again.store.query_runs()}
        self.assertEqual({r["status"] for r in rows.values()}, {"stopped"})
        self.assertEqual(store.pass_k(list(rows.values()))["left_out"], 2)


# The outcome the engine gives a finished case in these tests: every result column is set.
RESULTS = {"cl": 0.8123, "cd": 0.0121, "cm": -0.0521, "ld": 67.1, "cells": 26400, "yplus_avg": 38.2,
           "iterations": 1200, "forces_converged": True, "residuals": {"p": 2e-6, "Ux": 4e-7}}
REFERENCE = {"cl": 0.8, "cd": 0.0112, "cm": -0.05, "ld": 71.4}
DELTAS = {"cl_pct": 1.5, "cd_pct": 8.0, "cm_abs": 0.0021}
KEPT = {"cells": 26400, "yplus_avg": 38.2, "iterations": 1200, "forces_converged": True, "residual_max": 2e-6,
        "cl": 0.8123, "cd": 0.0121, "cm": -0.0521, "ld": 67.1, "xfoil_cl": 0.8, "xfoil_cd": 0.0112,
        "xfoil_cm": -0.05, "xfoil_ld": 71.4, "cl_err_pct": 1.5, "cd_err_pct": 8.0, "cm_err_abs": 0.0021}
ENDS = ("stop", "restart", "crash")


class KeptOutcome(EngineCase):
    """a row is the last outcome the case reached. An end that
    is no outcome (the engineer's Stop, a restart, a crash of the pipeline) during a re-run or
    an approved retry does not replace it."""

    on_disk = True                                   # a restart reads the data directory again

    def setUp(self):
        super().setUp()
        support.patch(self, runtime.CaseRunner, "start")                           # a re-run starts no thread

    def done(self, verdict="pass"):
        """The case finishes with a result."""
        def end(c):
            c.update(status="done", verdict=verdict, results=dict(RESULTS), reference=dict(REFERENCE),
                     deltas=dict(DELTAS))
            for s in c["stages"]:
                s.update(status="passed", attempts=max(1, s["attempts"]), seconds=2.0)
        self.eng.store.update_case(self.sid, self.cid, end)
        self.eng.write_metrics(self.sid, self.cid, "done")
        return self.row()

    def rerun(self, stage="solve"):
        """The engineer re-runs a stage of the ended case and the new pipeline gets as far as that stage."""
        self.assertEqual(self.eng.rerun(self.sid, self.cid, stage, "again"), "restarted")
        runner = self.eng.runners[self.cid]
        runner.apply_rerun(*runner.rerun_request)
        runner.rerun_request = None
        runner.update_stage(stage, lambda s: s.update(status="running", attempts=s["attempts"] + 1))
        self.eng.store.update_case(self.sid, self.cid, lambda c: c.update(status="running"))
        self.assertIsNone(self.eng.store.get_case(self.sid, self.cid)["verdict"])   # the re-run dropped the result
        return runner

    def fail_stage(self, runner, stage="mesh"):
        """A stage fails its gate three times: the provisional row, as run() writes it."""
        def fail(c):
            c["counters"]["gate_failures"] += 3
            for s in c["stages"]:
                if s["key"] == stage:
                    s.update(status="failed", attempts=s["attempts"] + 1)
        self.eng.store.update_case(self.sid, self.cid, fail)
        self.eng.write_metrics(self.sid, self.cid, "failed", failed_stage=stage)
        return self.row()

    def retry(self, runner, stage="mesh"):
        """The engineer approves the retry and the stage runs again."""
        self.eng.intervention(self.sid, "retry_approval", [self.cid])
        runner.update_stage(stage, lambda s: s.update(status="running", attempts=s["attempts"] + 1))

    def end(self, runner, how):
        """The pipeline ends without an outcome. Returns the engine that holds the store now."""
        self.eng.store.update_case(self.sid, self.cid, lambda c: c["usage"].update(tokens_in=4321, llm_calls=7))
        if how == "stop":
            with mock.patch.object(self.eng, "_alive", side_effect=lambda cid: cid == self.cid):
                self.eng.control(self.sid, "stop", case_id=self.cid)              # sets the flag, counts the action
            self.assertTrue(runner.stop_flag)
            runner.finish("stopped")                                              # what run() does next
        elif how == "crash":
            runner.finish("failed")                                               # what run() does when it crashes
        elif self.on_disk:
            self.eng = engine.Engine(Store(self.eng.store.data_dir), viewer_factory=None)   # recover()
            support.patch(self, self.eng, "spawn")
        else:
            self.eng.recover()                                                    # what a new engine does first
        return self.eng

    def test_a_pass_survives_an_end_without_an_outcome_during_a_re_run(self):
        for how in ENDS:
            with self.subTest(how=how):
                self.fresh()
                first = self.done("pass")
                self.assertEqual((first["status"], first["success"], first["first_pass"]), ("done", True, True))
                self.end(self.rerun(), how)
                row = self.row()
                self.assertEqual((row["status"], row["success"], row["verdict"], row["failed_stage"]),
                                 ("done", True, "pass", None))
                self.assertEqual({k: row[k] for k in KEPT}, KEPT)
                self.assertTrue(store.is_trial(row))
                # what the case cost is the case now: the re-run (help) and, for Stop, the stop
                actions = 2 if how == "stop" else 1
                self.assertEqual((row["human_interventions"], row["help_interventions"], row["tokens_in"],
                                  row["llm_calls"]), (actions, 1, 4321, 7))
                self.assertEqual(row["stage_attempts"]["solve"], 2)
                self.assertFalse(row["first_pass"])                               # the counters are no longer clean
                out = store.pass_k([row])
                self.assertEqual((out["left_out"], out["success"]["c"], out["unaided"]["c"], out["first_pass"]["c"]),
                                 (0, 1, 0, 0))
                self.assertEqual(self.eng.store.get_case(self.sid, self.cid)["status"],
                                 "failed" if how == "crash" else "stopped")       # the case record says how it ended

    def test_a_failed_verdict_survives_it_too(self):
        for how in ENDS:
            with self.subTest(how=how):
                self.fresh()
                self.done("fail")
                self.end(self.rerun(), how)
                row = self.row()
                self.assertEqual((row["status"], row["success"], row["verdict"], row["first_pass"]),
                                 ("done", False, "fail", False))
                self.assertEqual({k: row[k] for k in KEPT}, KEPT)
                out = store.pass_k([row])
                self.assertEqual((out["left_out"], out["success"]["n"], out["success"]["c"]), (0, 1, 0))

    def test_a_failed_stage_survives_an_end_during_the_approved_retry(self):
        for how in ENDS:
            with self.subTest(how=how):
                self.fresh()
                first = self.fail_stage(self.runner)
                self.assertEqual((first["status"], first["failed_stage"], first["gate_failures"]), ("failed", "mesh", 3))
                self.retry(self.runner)
                self.end(self.runner, how)
                # no stage stands failed any more: taken alone this end is `stopped`
                stages = {s["key"]: s["status"] for s in self.eng.store.get_case(self.sid, self.cid)["stages"]}
                self.assertNotIn("failed", stages.values())
                row = self.row()
                self.assertEqual((row["status"], row["failed_stage"], row["success"], row["verdict"], row["gate_failures"]),
                                 ("failed", "mesh", False, None, 3))
                self.assertEqual((row["human_interventions"], row["help_interventions"]), (2 if how == "stop" else 1, 1))
                self.assertTrue(store.is_trial(row))
                self.assertEqual(store.pass_k([row])["left_out"], 0)

    def test_the_last_outcome_is_the_one_kept(self):
        # a pass, then a re-run whose mesh fails its gate (a new outcome), then the retry is stopped
        for how in ENDS:
            with self.subTest(how=how):
                self.fresh()
                self.done("pass")
                runner = self.rerun("mesh")
                self.eng.store.update_case(self.sid, self.cid, lambda c: [
                    s.update(status="stale") for s in c["stages"] if s["key"] == "mesh"])
                provisional = self.fail_stage(runner)
                self.assertEqual((provisional["status"], provisional["success"], provisional["cl"]), ("failed", False, None))
                self.retry(runner)
                self.end(runner, how)
                row = self.row()
                self.assertEqual((row["status"], row["failed_stage"], row["success"], row["cl"]),
                                 ("failed", "mesh", False, None))
        # and a failure, then a re-run that passes, then one that is stopped: the pass
        self.fresh()
        self.fail_stage(self.runner, "solve")
        self.end(self.runner, "stop")
        self.assertEqual(self.row()["status"], "failed")
        self.rerun("solve")
        self.assertEqual(self.done("warn")["status"], "done")
        self.end(self.rerun("post"), "stop")
        row = self.row()
        self.assertEqual((row["status"], row["success"], row["verdict"], row["failed_stage"]), ("done", True, "warn", None))

    def test_a_case_stopped_before_any_result_stays_left_out(self):
        for how in ENDS:
            with self.subTest(how=how):
                self.fresh()
                self.runner.update_stage("geometry", lambda s: s.update(status="running", attempts=1))
                self.end(self.runner, how)
                row = self.row()
                self.assertEqual((row["status"], row["success"], row["failed_stage"], row["verdict"]),
                                 ("stopped", False, None, None))
                self.assertFalse(store.is_trial(row))
                # started again and ended again without a result: there is still no outcome to keep
                self.eng.store.update_case(self.sid, self.cid, lambda c: c["stages"][0].update(status="stale"))
                self.end(self.rerun("geometry"), how)
                row = self.row()
                self.assertEqual((row["status"], row["help_interventions"]), ("stopped", 1))
                self.assertEqual(store.pass_k([row]), {"left_out": 1, **{c: {"n": 0, "c": 0, "values": []}
                                                                         for c in store.PASS_CRITERIA}})

    def test_a_stop_of_a_case_without_a_pipeline_keeps_the_outcome(self):
        # control() closes a case that is not ended and has no live runner by itself
        self.done("pass")
        self.rerun()
        self.eng.control(self.sid, "stop")                                        # no runner is alive
        row = self.row()
        self.assertEqual((row["status"], row["success"], row["cl"]), ("done", True, 0.8123))
        self.assertEqual(self.row(self.cid2)["status"], "stopped")                # the other case never had a result

    def test_the_result_keeps_the_angle_it_was_computed_at(self):
        first = self.done("pass")
        runner = self.rerun("physics")
        self.eng.store.update_case(self.sid, self.cid, lambda c: c.__setitem__("alpha_deg", first["alpha_deg"] + 2.0))
        self.end(runner, "stop")
        self.assertEqual((self.row()["alpha_deg"], self.row()["cl"]), (first["alpha_deg"], 0.8123))

    def test_a_row_from_before_the_status_column_is_kept_with_a_status(self):
        for old, status in (({"success": True, "verdict": "pass"}, "done"), ({"verdict": "fail"}, "done"),
                            ({"failed_stage": "solve"}, "failed")):
            with self.subTest(old=old):
                self.fresh()
                self.eng.store.record_run(dict({"id": self.cid, "case_id": self.cid, "study_id": self.sid, "model": HAIKU,
                                                "success": False, "cl": 0.7}, **old))
                self.assertIsNone(self.row()["status"])
                self.eng.store.update_case(self.sid, self.cid, lambda c: c.update(status="done") or [
                    s.update(status="passed", attempts=1) for s in c["stages"]])
                self.end(self.rerun(), "stop")
                row = self.row()
                self.assertEqual((row["status"], row["cl"], bool(row["success"]), row["failed_stage"]),
                                 (status, 0.7, bool(old.get("success")), old.get("failed_stage")))
                self.assertTrue(store.is_trial(row))

    def test_an_outcome_replaces_a_kept_one(self):
        # only an end without an outcome keeps the earlier row: a new result or a new failure is the row
        self.done("pass")
        self.rerun()
        row = self.done("fail")
        self.assertEqual((row["status"], row["success"], row["verdict"], row["help_interventions"]),
                         ("done", False, "fail", 1))
        runner = self.rerun()
        self.eng.store.update_case(self.sid, self.cid, lambda c: [
            s.update(status="stale") for s in c["stages"] if s["key"] == "solve"])
        row = self.fail_stage(runner, "solve")
        self.assertEqual((row["status"], row["failed_stage"], row["verdict"], row["cl"]), ("failed", "solve", None, None))

    def test_the_criteria_stay_nested_on_every_row_the_engine_writes(self):
        rows = []
        # sixty small studies: kept in memory (the row, not the record on disk, is what is judged)
        self.on_disk = False
        with mock.patch.object(store, "write_json"):
            self._rows_of_every_kind(rows)
        self.assertEqual(len(rows), 192)
        for row in rows:
            self.assertTrue(nested(row), row)
            self.assertIsNotNone(row["help_interventions"], row)
            self.assertLessEqual(row["help_interventions"], row["human_interventions"], row)
            self.assertEqual(bool(row["first_pass"]), bool(row["success"]) and not row["human_interventions"]
                             and not row["gate_failures"] and not row["rework_loops"], row)
        seen = {(r["status"], bool(r["success"]), bool(r["first_pass"]), store._passes(r, "unaided")) for r in rows}
        self.assertEqual(seen, {("done", True, True, True), ("done", True, False, True), ("done", True, False, False),
                                ("done", False, False, False), ("failed", False, False, False)})
        # the pooled counts are nested too
        out = store.pass_k(rows)
        self.assertTrue(out["first_pass"]["c"] <= out["unaided"]["c"] <= out["success"]["c"] <= out["success"]["n"])

    def _rows_of_every_kind(self, rows):
        for kind in (None, "steer", "gate_decision"):                             # an action before the first end
            for verdict in ("pass", "fail"):
                for how in ENDS + ("done", "gate"):
                    for old in (False, True):        # a case record from before the help count
                        self.fresh()
                        if old:
                            self.eng.store.update_case(self.sid, self.cid,
                                                       lambda c: c["counters"].pop("help_interventions"))
                        if kind:
                            self.eng.intervention(self.sid, kind, [self.cid, self.cid2])
                        rows.append(self.done(verdict))
                        runner = self.rerun()
                        if how == "done":
                            rows.append(self.done("warn"))
                        elif how == "gate":
                            self.eng.store.update_case(self.sid, self.cid, lambda c: [
                                s.update(status="stale") for s in c["stages"] if s["key"] == "solve"])
                            rows.append(self.fail_stage(runner, "solve"))
                            self.retry(runner, "solve")
                            self.end(runner, "stop")
                            rows.append(self.row())
                        else:
                            self.end(runner, how)
                            rows.append(self.row())
                        rows.append(self.finish(self.cid2, verdict))             # the case that was never re-run


class StoppedPipeline(unittest.TestCase):
    """On the real pipeline thread: run(), the approval, Stop and the engineer's re-run go
    through the engine as the routes call it; only the work of a stage is scripted here."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.sid = support.store_study(self.eng, support.make_study(codes=("2412",)))["id"]
        self.cid = f"{self.sid}-2412"
        self.script = {}                             # (stage, attempt) -> "fail" | "block"; else the stage passes
        self.blocked = threading.Event()
        test = self

        def run_stage(runner, key):
            runner.stage = key
            runner.set_status("running")
            case = runner.update_stage(key, lambda s: s.update(status="running", attempts=s["attempts"] + 1))
            attempt = next(s for s in case["stages"] if s["key"] == key)["attempts"]
            what = test.script.get((key, attempt))
            if what == "fail":
                runner.count("gate_failures", 3)
                runner.update_stage(key, lambda s: s.update(status="failed", failure="the gate did not pass"))
                return ("failed", "the gate failed 3 times in a row")
            if what == "block":
                test.blocked.set()
                while not runner.interrupted():
                    time.sleep(0.002)
                runner.update_stage(key, lambda s: s.update(status="stale"))
                return ("stopped",) if runner.stop_flag else ("rerun",)
            runner.update_stage(key, lambda s: s.update(status="passed"))
            if key == "validate":
                runner.store.update_case(runner.sid, runner.cid, lambda c: c.update(
                    verdict="pass", results=dict(RESULTS), reference=dict(REFERENCE), deltas=dict(DELTAS)))
            return ("passed",)
        # no solver, no report or steering thread that outlives the temporary store
        support.patch(self, solver, "kill_solver")
        support.patch(self, self.eng, "spawn")
        support.patch(self, runtime.CaseRunner, "run_stage", run_stage)

    def wait(self, what, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end:
            if what():
                return
            time.sleep(0.002)
        self.fail("the pipeline did not get there")

    def ended(self):
        runner = self.eng.runners[self.cid]
        runner.thread.join(timeout=5)
        self.assertFalse(runner.thread.is_alive())
        row = self.eng.store.get_run(self.cid)
        self.assertTrue(nested(row), row)
        return row

    def test_a_failed_gate_then_the_approved_retry_is_stopped(self):
        # whether the failure counted depended on when Stop was pressed
        self.script = {("mesh", 1): "fail", ("mesh", 2): "block"}
        self.eng.approve(self.sid, "auto")
        runner = self.eng.runners[self.cid]
        self.wait(lambda: runner.waiting == "mesh" and runner.waiting_retry)
        row = self.eng.store.get_run(self.cid)                                    # the provisional row
        self.assertEqual((row["status"], row["failed_stage"], row["gate_failures"], row["help_interventions"]),
                         ("failed", "mesh", 3, 0))
        self.eng.approve_case(self.sid, self.cid)
        self.assertTrue(self.blocked.wait(5))                                     # mesh runs again
        self.eng.control(self.sid, "stop")
        row = self.ended()
        case = self.eng.store.get_case(self.sid, self.cid)
        self.assertEqual((case["status"], [s["status"] for s in case["stages"]][:3]), ("stopped", ["passed", "stale", "pending"]))
        self.assertEqual((row["status"], row["failed_stage"], row["success"], row["gate_failures"],
                          row["human_interventions"], row["help_interventions"], row["first_pass"]),
                         ("failed", "mesh", False, 3, 2, 1, False))
        out = store.pass_k([row])
        self.assertEqual((out["left_out"], out["success"]["n"], out["success"]["c"]), (0, 1, 0))

    def test_a_finished_case_is_re_run_and_stopped(self):
        self.eng.approve(self.sid, "auto")
        first = self.ended()
        self.assertEqual((first["status"], first["success"], first["first_pass"], first["cl"]), ("done", True, True, 0.8123))
        self.script = {("solve", 2): "block"}
        self.eng.rerun(self.sid, self.cid, "solve", "again")
        self.assertTrue(self.blocked.wait(5))
        self.assertIsNone(self.eng.store.get_case(self.sid, self.cid)["verdict"])   # the re-run dropped the result
        self.eng.control(self.sid, "stop")
        row = self.ended()
        self.assertEqual(self.eng.store.get_case(self.sid, self.cid)["status"], "stopped")
        self.assertEqual((row["status"], row["success"], row["verdict"], row["failed_stage"], row["first_pass"]),
                         ("done", True, "pass", None, False))
        self.assertEqual({k: row[k] for k in KEPT}, KEPT)
        self.assertEqual((row["human_interventions"], row["help_interventions"], row["stage_attempts"]["solve"]), (2, 1, 2))
        self.assertGreater(row["ts"], first["ts"])
        out = store.pass_k([row])
        self.assertEqual([out["left_out"]] + [out[c]["c"] for c in store.PASS_CRITERIA], [0, 1, 0, 0])

    def test_a_case_stopped_in_its_first_run_is_left_out(self):
        self.script = {("geometry", 1): "block"}
        self.eng.approve(self.sid, "auto")
        self.assertTrue(self.blocked.wait(5))
        self.eng.control(self.sid, "stop")
        row = self.ended()
        self.assertEqual((row["status"], row["success"], row["failed_stage"], row["help_interventions"]),
                         ("stopped", False, None, 0))
        self.assertEqual(store.pass_k([row])["left_out"], 1)


class SteeringHelp(EngineCase):
    """what the steering agent does to a case on the engineer's
    message is help for that case, also when the case has no live pipeline."""
    MODEL = HAIKU                                    # the steering agent has no tools in scripted mode

    def setUp(self):
        super().setUp()
        support.patch(self, runtime.CaseRunner, "start")
        self.rt = runtime.StudyRuntime(self.eng, self.sid, "steering")

    def both(self, cid=None):
        return self.counters(cid)["human_interventions"], self.counters(cid)["help_interventions"]

    def kinds(self):
        return [(e["data"]["kind"], e["data"]["case_ids"]) for e in support.events(self.eng, self.sid, "human.intervention")]

    def test_a_study_wide_message_that_re_runs_an_ended_case(self):
        # a failed run must not become an unaided first pass
        first = self.finish(verdict="fail")
        self.assertEqual((first["success"], first["help_interventions"]), (False, 0))
        self.eng.message(self.sid, "raise the iterations to 2000 and run the solve again")   # no pipeline is alive
        self.assertEqual(self.kinds(), [("steer", [])])
        self.assertTrue(self.rt.update_plan("2412", "numerics", {"iterations": 2000})["ok"])
        self.assertEqual(self.both(), (1, 1))
        done = self.rt.rerun_from(self.cid, "solve", "more iterations")
        self.assertEqual((done["ok"], done["rerun"]), (True, True))
        self.assertEqual(self.both(), (2, 2))
        self.assertEqual(self.kinds()[1:], [("plan_edit", [self.cid]), ("rerun", [self.cid])])
        self.assertEqual(self.both(self.cid2), (0, 0))                            # the other case was not acted on
        row = self.finish(verdict="pass")
        self.assertEqual((row["success"], row["human_interventions"], row["help_interventions"], row["first_pass"]),
                         (True, 2, 2, False))
        out = store.pass_k([row])
        self.assertEqual([out[c]["c"] for c in store.PASS_CRITERIA], [1, 0, 0])

    def test_each_tool_call_counts_once_for_its_case(self):
        with mock.patch.object(self.eng, "_alive", return_value=True):
            self.eng.message(self.sid, "finer wake on both", case_id=self.cid)    # the message itself: steer
        self.assertEqual(self.both(), (1, 1))
        self.rt.update_plan(self.cid, "mesh", {"n_wake": 60})
        self.rt.update_plan(self.cid, "mesh", {"n_wake": 70, "n_aft": 50})        # one call, two values
        self.rt.update_plan(self.cid2, "mesh", {"n_wake": 60})
        self.assertEqual((self.both(), self.both(self.cid2)), ((3, 3), (1, 1)))
        self.assertEqual(self.kinds()[1:], [("plan_edit", [self.cid]), ("plan_edit", [self.cid]), ("plan_edit", [self.cid2])])

    def test_a_call_that_changes_nothing_is_not_help(self):
        plan = self.eng.store.get_study(self.sid)["plan"]
        same = plan["cases"][0]["stages"]["mesh"]["n_wake"]
        self.assertTrue(self.rt.update_plan(self.cid, "mesh", {"n_wake": same})["ok"])
        self.assertFalse(self.rt.update_plan(self.cid, "mesh", {"no_such_setting": 1})["ok"])
        self.assertFalse(self.rt.update_plan("9999", "mesh", {"n_wake": 60})["ok"])
        self.assertFalse(self.rt.rerun_from(self.cid, "solve")["ok"])             # solve has not run yet: refused
        self.assertFalse(self.rt.rerun_from("9999", "mesh")["ok"])
        self.rt.set_mode("gate")                                                  # acts on the study, and is not help
        self.rt.reply("nothing to do")
        self.assertEqual(self.kinds(), [])
        self.assertEqual((self.both(), self.both(self.cid2)), ((0, 0), (0, 0)))

    def test_a_case_outside_the_scope_is_refused_and_not_counted(self):
        self.rt.scope = [self.cid2]
        self.assertFalse(self.rt.update_plan(self.cid, "mesh", {"n_wake": 60})["ok"])
        self.eng.store.update_case(self.sid, self.cid, lambda c: c["stages"][0].update(status="passed", attempts=1))
        self.assertFalse(self.rt.rerun_from(self.cid, "geometry")["ok"])
        self.assertEqual(self.kinds(), [])

    def test_a_re_run_of_a_live_case_counts_and_a_noted_one_does_not(self):
        self.eng.store.update_case(self.sid, self.cid, lambda c: c["stages"][0].update(status="passed", attempts=1))
        with mock.patch.object(self.runner, "alive", return_value=True):
            done = self.rt.rerun_from(self.cid, "geometry", "thicker trailing edge")
            self.assertEqual((done["rerun"], self.runner.rerun_request), (True, ("geometry", "thicker trailing edge", False)))
            self.assertEqual((self.kinds(), self.both()), ([("rerun", [self.cid])], (1, 1)))
            # mesh has not started and waits for the engineer's approval: nothing is re-run
            self.runner.rerun_request = None
            self.eng.store.update_case(self.sid, self.cid, lambda c: c.update(current_stage="mesh") or
                                       c["stages"][1].update(status="awaiting_approval"))
            self.runner.waiting = "mesh"
            done = self.rt.rerun_from(self.cid, "mesh", "use 60 wake cells")
            self.assertEqual((done["ok"], done["rerun"], self.runner.rerun_request), (True, False, None))
        self.assertEqual((len(self.kinds()), self.both()), (1, (1, 1)))

    def test_a_changed_angle_counts_as_the_one_re_run_it_causes(self):
        self.finish(verdict="pass")
        done = self.rt.update_plan(self.cid, "physics", {"alpha_deg": 5.0})
        self.assertTrue(done["ok"], done)
        self.assertIn("re-runs from physics", done["summary"])
        self.assertEqual((self.kinds(), self.both()), ([("rerun", [self.cid])], (1, 1)))   # once per call and case
        # a stopped case is only marked stale: the same single count
        self.eng.store.update_case(self.sid, self.cid2, lambda c: c.update(status="stopped") or [
            s.update(status="passed", attempts=1) for s in c["stages"][:3]])
        done = self.rt.update_plan(self.cid2, "physics", {"alpha_deg": 6.0})
        self.assertIn("marked stale", done["summary"])
        self.assertEqual((self.kinds()[1:], self.both(self.cid2)), ([("rerun", [self.cid2])], (1, 1)))


class PlanEdit(EngineCase):
    """a plan edit is help for the cases that have not ended and
    whose plan it changed, not for every case of the study."""

    def edit(self, change):
        body = self.eng.store.get_study(self.sid)["plan"]
        change(body)
        self.eng.patch_plan(self.sid, body)
        return self.last()["data"]

    def helps(self):
        return self.counters()["help_interventions"], self.counters(self.cid2)["help_interventions"]

    def test_only_the_case_whose_section_changed(self):
        data = self.edit(lambda p: p["cases"][0]["stages"]["mesh"].__setitem__("n_wake", 60))
        self.assertEqual(data, {"kind": "plan_edit", "case_ids": [self.cid]})
        self.assertEqual(self.helps(), (1, 0))
        data = self.edit(lambda p: p["cases"][1]["stages"]["numerics"].__setitem__("relax_U", 0.6))
        self.assertEqual((data["case_ids"], self.helps()), ([self.cid2], (1, 1)))
        data = self.edit(lambda p: [pc["stages"]["mesh"].__setitem__("n_aft", 50) for pc in p["cases"]])
        self.assertEqual((data["case_ids"], self.helps()), ([self.cid, self.cid2], (2, 2)))
        self.assertEqual(self.counters()["human_interventions"], 2)               # both counters, the same cases

    def test_the_angle_is_the_case_s_own_value(self):
        data = self.edit(lambda p: p["cases"][1].__setitem__("alpha_deg", 5.5))
        self.assertEqual((data["case_ids"], self.helps()), ([self.cid2], (0, 1)))

    def test_a_shared_value_applies_to_every_case(self):
        data = self.edit(lambda p: p.__setitem__("assumptions", p["assumptions"] + ["Sea level, 15 C."]))
        self.assertEqual((data["case_ids"], self.helps()), ([self.cid, self.cid2], (1, 1)))

    def test_an_edit_that_changes_nothing_helps_no_case(self):
        version = self.eng.store.get_study(self.sid)["plan"]["version"]
        data = self.edit(lambda p: None)
        self.assertEqual((data, self.helps()), ({"kind": "plan_edit", "case_ids": []}, (0, 0)))
        self.assertEqual(self.eng.store.get_study(self.sid)["plan"]["version"], version + 1)

    def test_a_case_that_has_ended_is_not_helped(self):
        for status in runtime.TERMINAL:
            with self.subTest(status=status):
                self.fresh()
                self.eng.store.update_case(self.sid, self.cid, lambda c: c.__setitem__("status", status))
                data = self.edit(lambda p: [pc["stages"]["mesh"].__setitem__("n_wake", 60) for pc in p["cases"]])
                self.assertEqual((data["case_ids"], self.helps()), ([self.cid2], (0, 1)))
        # every case has ended (the study is no longer running): nobody
        self.fresh()
        for cid in (self.cid, self.cid2):
            self.eng.store.update_case(self.sid, cid, lambda c: c.__setitem__("status", "done"))
        self.eng.set_study_status(self.sid, "done")
        data = self.edit(lambda p: p["cases"][0]["stages"]["mesh"].__setitem__("n_wake", 60))
        self.assertEqual((data["case_ids"], self.helps()), ([], (0, 0)))

    def test_a_finished_case_that_a_changed_angle_starts_again_gets_the_re_run_only(self):
        support.patch(self, runtime.CaseRunner, "start")
        self.finish(verdict="pass")
        self.edit(lambda p: p["cases"][0].__setitem__("alpha_deg", 5.5))
        kinds = [(e["data"]["kind"], e["data"]["case_ids"]) for e in support.events(self.eng, self.sid, "human.intervention")]
        self.assertEqual(kinds, [("rerun", [self.cid]), ("plan_edit", [])])
        self.assertEqual(self.helps(), (1, 0))

    def test_plan_changes(self):
        old = self.eng.store.get_study(self.sid)["plan"]
        new = json.loads(json.dumps(old))
        self.assertEqual(planning.plan_changes(old, new), set())
        new.update(version=old["version"] + 1, approved_at="2026-11-18T10:00:00.000Z")
        new["cases"][0]["stages"]["mesh"]["cites"] = {}                           # bookkeeping, not a value
        new["cases"][0]["cites"] = {}
        self.assertEqual(planning.plan_changes(old, new), set())
        new["cases"][1]["stages"]["solve"]["reasons"] = {"max_extensions": "Set by the engineer (was 2)."}
        self.assertEqual(planning.plan_changes(old, new), {"4412"})
        new["summary"] = old["summary"] + " Revised."
        self.assertEqual(planning.plan_changes(old, new), {"2412", "4412"})
        self.assertEqual(planning.plan_changes(None, old), {"2412", "4412"})


class PageContract(unittest.TestCase):
    """The page and the mock state the rules of store.py a second time, in JavaScript, in one
    place (web/js/metrics.js). These tests hold that copy to the server's."""

    WEB = os.path.join(support.STUDIO, "web", "js")
    OSASCRIPT = "/usr/bin/osascript"          # JavaScriptCore on macOS; there is no node here

    def _read(self, name):
        with open(os.path.join(self.WEB, name), encoding="utf-8") as f:
            return f.read()

    def test_the_page_lists_the_same_criteria_in_the_same_order(self):
        block = re.search(r"AS\.PASS_CRITERIA = \[(.*?)\n  \];", self._read("metrics.js"), re.S).group(1)
        self.assertEqual(tuple(re.findall(r"\{ id: '(\w+)'", block)), store.PASS_CRITERIA)

    def test_the_page_and_the_mock_count_the_same_help_kinds(self):
        # One list in the shared reducer (js/store.js), which the page and the mock both run.
        reducer = self._read("store.js")
        kinds = re.search(r"AS\.HELP_KINDS = \[([^\]]*)\]", reducer).group(1)
        # a retry approval arrives as the event writes it: a gate_decision with `retry`
        self.assertEqual(tuple(re.findall(r"'(\w+)'", kinds)) + ("retry_approval",), store.HELP_KINDS)
        self.assertIn("AS.HELP_KINDS.includes(d.kind) || d.retry === true", reducer)
        mock = self._read("mock.js")
        # the mock no longer counts help itself: that would count each action twice
        self.assertNotIn("help_interventions += 1", mock)
        self.assertIn("retry ? { retry: true } : null", mock)
        self.assertIn("pass_k: AS.passK(list)", mock)      # the mock's summary uses the shared function
        self.assertIn("AS.aggregateRuns(list)", mock)

    @unittest.skipUnless(os.path.exists(OSASCRIPT), "needs JavaScriptCore (macOS)")
    def test_the_page_computes_what_the_server_computes(self):
        import subprocess
        rows, n = [], 0
        for status in (None, "", "done", "failed", "stopped"):
            for success, first, failed_stage, verdict in ((1, 1, None, "pass"), (1, 0, None, "warn"), (0, 0, "mesh", None),
                                                          (0, 0, None, "fail"), (0, 0, None, None), (0, 0, "", None)):
                for help_n, human in ((None, None), (None, 0), (None, 2), (0, 3), (1, 1)):
                    n += 1
                    rows.append({"id": n, "status": status, "success": success, "first_pass": first,
                                 "failed_stage": failed_stage, "verdict": verdict,
                                 "help_interventions": help_n, "human_interventions": human})
        # booleans as the server's rows hold them, numbers as the mock's
        rows += [dict(r, id=1000 + r["id"], success=bool(r["success"]), first_pass=bool(r["first_pass"])) for r in rows[::4]]
        # every row also alone: sums over a group could hide two errors that cancel
        groups = [rows, rows[::3], rows[1::7], []] + [[r] for r in rows]
        script = """function run(argv) {
          ObjC.import('Foundation');
          const src = $.NSString.stringWithContentsOfFileEncodingError(argv[0], 4, null).js;
          const AS = { views: {}, state: {}, h() {}, truthy: (v) => v === true || v === 1 || v === '1' || v === 'true',
            isNum: (v) => typeof v === 'number' && isFinite(v) };
          new Function('window', src)({ AS });
          const pairs = {};
          for (let n = 0; n <= 12; n++) for (let c = 0; c <= n; c++) pairs[n + ',' + c] = AS.passKValues(n, c);
          const held = JSON.parse(argv[2]).map((p) => AS.passKValues(p[0], p[1]));
          return JSON.stringify({ pairs, held, groups: JSON.parse(argv[1]).map(AS.passK), ids: AS.PASS_CRITERIA.map((c) => c.id) });
        }"""
        # c is held to 0..n on both sides, and n < 0 gives no values
        outside = [[3, 5], [3, -2], [0, 4], [-1, 0], [-4, 9], [7, 8]]
        outside += [[n, c] for n in range(-2, 13) for c in range(-3, n + 4)]
        done = subprocess.run([self.OSASCRIPT, "-l", "JavaScript", "-e", script, os.path.join(self.WEB, "metrics.js"),
                               json.dumps(groups), json.dumps(outside)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        page = json.loads(done.stdout)
        self.assertEqual(tuple(page["ids"]), store.PASS_CRITERIA)
        # the same doubles, not close ones: both sides print the shortest text that reads back as the double
        server = {f"{n},{c}": store.pass_k_values(n, c) for n in range(13) for c in range(n + 1)}
        self.assertEqual(json.dumps(page["pairs"], sort_keys=True), json.dumps(json.loads(json.dumps(server)), sort_keys=True)
                         .replace(".0,", ",").replace(".0]", "]"))
        self.assertEqual(page["held"], [store.pass_k_values(n, c) for n, c in outside])
        self.assertEqual(page["groups"], [store.pass_k(g) for g in groups])
        self.assertGreater(page["groups"][0]["left_out"], 0)

    @unittest.skipUnless(os.path.exists(OSASCRIPT), "needs JavaScriptCore (macOS)")
    def test_the_mock_keeps_what_the_server_keeps(self):
        """The mock records a stopped case as Engine.write_metrics does, with its own copy of the
        list of outcome columns and the trial rule of js/metrics.js."""
        import subprocess
        text = self._read("mock.js")
        names = re.search(r"const OUTCOME_COLUMNS = \[(.*?)\];", text, re.S).group(1)
        self.assertEqual(tuple(re.findall(r"'(\w+)'", names)), store.OUTCOME_COLUMNS)
        rows = [{"status": status, "success": success, "failed_stage": failed_stage, "verdict": verdict}
                for status in (None, "", "done", "failed", "stopped")
                for success in (0, 1, False, True, None)
                for failed_stage in (None, "", "mesh")
                for verdict in (None, "pass", "warn", "fail")]
        script = """function run(argv) {
          ObjC.import('Foundation');
          const src = $.NSString.stringWithContentsOfFileEncodingError(argv[0], 4, null).js;
          const AS = { truthy: (v) => v === true || v === 1 || v === '1' || v === 'true' };
          new Function('window', src)({ AS });
          return JSON.stringify(JSON.parse(argv[1]).map(AS.isTrial));
        }"""
        done = subprocess.run([self.OSASCRIPT, "-l", "JavaScript", "-e", script, os.path.join(self.WEB, "metrics.js"),
                               json.dumps(rows)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [store.is_trial(r) for r in rows])
        # the kept row, as write_metrics: a status for a row from before the column, first pass from the kept success
        self.assertIn("row.status = earlier.status || (earlier.failed_stage ? 'failed' : 'done');", text)
        self.assertIn("row.first_pass = AS.truthy(row.success) && clean ? 1 : 0;", text)
        # a stopped case names a failed stage only when one stands failed (store.run_status)
        self.assertIn("recordRun(S, c, failed ? 'failed' : 'stopped', failed ? failed.key : null);", text)

    @unittest.skipUnless(os.path.exists(OSASCRIPT), "needs JavaScriptCore (macOS)")
    def test_the_mock_finds_the_same_changed_cases(self):
        """The mock's PATCH /plan counts a plan edit for the cases planning.plan_changes names."""
        import copy
        import subprocess
        source = re.search(r"\n  (function planChanges\(oldPlan, newPlan\) \{.*?\n  \})\n", self._read("mock.js"), re.S).group(1)
        base = {"version": 3, "approved_at": "2026-01-01T00:00:00Z", "summary": "two sections", "assumptions": ["steady"],
                "cases": [{"naca": naca, "alpha_deg": 2.0, "cites": {"alpha_deg": ["xfoil-primer"]},
                           "stages": {"mesh": {"n_normal": 90, "first_cell_height": 0.00129, "cites": {"n_normal": ["a"]},
                                               "reasons": {"n_normal": "default"}, "checks": ["y+"]},
                                      "numerics": {"relax_U": 0.7, "iterations": 1200, "cites": {}}}}
                          for naca in ("2412", "4412")]}

        def edited(change):
            plan = copy.deepcopy(base)
            change(plan)
            return plan
        reordered = copy.deepcopy(base)
        reordered["cases"][0]["stages"]["mesh"] = dict(reversed(list(reordered["cases"][0]["stages"]["mesh"].items())))
        reordered = dict(reversed(list(reordered.items())))
        plans = [
            copy.deepcopy(base), reordered,
            edited(lambda p: p.update(version=4, approved_at=None)),
            edited(lambda p: p["cases"][1]["stages"]["numerics"].update(relax_U=0.5)),
            edited(lambda p: p["cases"][0].update(alpha_deg=2.5)),
            edited(lambda p: p["cases"][0]["cites"].clear()),
            edited(lambda p: p["cases"][1]["stages"]["mesh"]["cites"].update(n_normal=[])),
            edited(lambda p: p["cases"][1]["stages"]["mesh"]["reasons"].update(n_normal="Set by the engineer (was 80).")),
            edited(lambda p: p["cases"][0]["stages"]["mesh"]["checks"].append("skewness")),
            edited(lambda p: p.update(summary="two sections, finer")),
            edited(lambda p: p["assumptions"].append("incompressible")),
            edited(lambda p: [pc["stages"]["numerics"].update(iterations=1500) for pc in p["cases"]]),
        ]
        script = ("function run(argv) { const planChanges = eval('(' + argv[0] + ')'); const plans = JSON.parse(argv[1]);"
                  " return JSON.stringify(plans.map((p) => Array.from(planChanges(plans[0], p)).sort())); }")
        done = subprocess.run([self.OSASCRIPT, "-l", "JavaScript", "-e", script, source, json.dumps(plans)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        server = [sorted(planning.plan_changes(base, p)) for p in plans]
        self.assertEqual(json.loads(done.stdout), server)
        self.assertEqual(server, [[], [], [], ["4412"], ["2412"], [], [], ["4412"], ["2412"],
                                  ["2412", "4412"], ["2412", "4412"], ["2412", "4412"]])


if __name__ == "__main__":
    unittest.main()
