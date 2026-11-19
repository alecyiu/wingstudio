"""The end of a stage: the citations of the decisions an agent hands over, complete_stage's
refusals and limits, and what a later stage is told. The gate is stubbed; no Docker, no model.
"""
import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

import support
from support import GATE_FAILED, GATE_FAILED_TEXT, GATE_PASSED, citations, section

from wingstudio import gates, runtime
from wingstudio.agents import spec as agent_spec
from wingstudio.agents import tools as agent_tools
from wingstudio.cfd import solver
from wingstudio.study import planning

DROPPED = "citations dropped after 2 refusals: "


class DecisionCites(unittest.TestCase):
    """planning.decision_cites: which citation a handed-over decision carries."""

    def setUp(self):
        support.use_catalogue(self)
        self.plan, _ = support.default_plan(support.make_study())

    def cites(self, stage, param, value):
        return planning.decision_cites(stage, [{"param": param, "value": value, "reason": "r"}],
                                       section(self.plan, stage))[0][0]

    def test_plan_values_inherit_the_plan_citation(self):
        numerics = section(self.plan, "numerics")
        decisions = [{"param": "relax_p", "value": "0.3", "reason": "plan"},          # a numeric string
                     {"param": "iterations", "value": 1200, "reason": "plan"},
                     {"param": "consistent", "value": "false", "reason": "plan"},
                     {"param": "write_interval", "value": 400, "reason": "plan"},     # no citation in the plan
                     {"param": "iterations", "value": 1800, "reason": "50% more"},    # a deviation
                     {"param": "relaxation", "value": "p 0.2", "reason": "not a setting"},
                     "not a decision", {"value": 1}]
        cited, errors, unknown = planning.decision_cites("numerics", decisions, numerics)
        self.assertEqual((errors, unknown), ([], []))
        self.assertEqual(cited, [numerics["cites"]["relax_p"], numerics["cites"]["iterations"],
                                 numerics["cites"]["consistent"], [], [], [], [], []])
        cited[0].append("x")                                    # copies: the plan is not touched
        self.assertNotIn("x", numerics["cites"]["relax_p"])

    def test_a_count_is_the_plan_value_only_when_it_is_equal(self):
        mesh = section(self.plan, "mesh")
        mesh["n_normal"] = 150
        for same in (150, 150.0, "150"):
            self.assertEqual(self.cites("mesh", "n_normal", same), mesh["cites"]["n_normal"], same)
        for other in (151, 149, 150.4, "151", "about 150", None, True):
            self.assertEqual(self.cites("mesh", "n_normal", other), [], other)
        self.assertEqual(self.cites("numerics", "iterations", 1205), [])          # within 1%, still another count
        self.assertEqual(self.cites("solve", "extend_iterations", 600),
                         section(self.plan, "solve")["cites"]["extend_iterations"])

    def test_a_float_within_one_per_cent_is_the_plan_value(self):
        mesh = section(self.plan, "mesh")
        height = mesh["first_cell_height"]
        self.assertEqual(self.cites("mesh", "first_cell_height", float(f"{height * 1.003:.4g}")),
                         mesh["cites"]["first_cell_height"])
        self.assertEqual(self.cites("mesh", "first_cell_height", height * 1.05), [])
        self.assertEqual(self.cites("mesh", "farfield_chords", 30), mesh["cites"]["farfield_chords"])

    def test_words_and_switches_compare_by_spelling(self):
        physics = section(self.plan, "physics")
        self.assertEqual(self.cites("physics", "turbulence_model", "spalartallmaras"),
                         physics["cites"]["turbulence_model"])
        self.assertEqual(self.cites("physics", "turbulence_model", "kOmegaSST"), [])
        self.assertEqual(self.cites("numerics", "consistent", False), section(self.plan, "numerics")["cites"]["consistent"])
        self.assertEqual(self.cites("numerics", "consistent", True), [])

    def test_an_engineer_value_is_never_cited(self):
        mesh = section(self.plan, "mesh")
        mesh["first_cell_height"] = 0.012
        mesh["reasons"]["first_cell_height"] = "Set by the engineer (was 0.00129)."
        mesh["cites"].pop("first_cell_height")
        sent = {"param": "first_cell_height", "value": 0.012, "reason": "as instructed", "cites": ["project-yplus-40"]}
        self.assertEqual(planning.decision_cites("mesh", [sent], mesh), ([[]], [], []))
        # after the gate has rejected it the agent deviates, and may cite what supports its own value
        own = dict(sent, value=0.00129, reason="the gate rejected 0.012")
        self.assertEqual(planning.decision_cites("mesh", [own], mesh), ([["project-yplus-40"]], [], []))

    def test_valid_ids_on_a_deviation_are_kept(self):
        physics = section(self.plan, "physics")
        decisions = [{"param": "turbulence_model", "value": "kOmegaSST", "reason": "r", "cites": ["menter-1994"]},
                     {"param": "nut_ratio", "value": 1, "reason": "r", "cites": "nasa-tmr-sst"},
                     {"param": "wall_treatment", "value": "wallFunction", "reason": "r", "cites": []},
                     {"param": "farfield_bc", "value": "fixedInlet", "reason": "r", "cites": None}]
        cited, errors, unknown = planning.decision_cites("physics", decisions, physics)
        self.assertEqual((errors, unknown), ([], []))
        self.assertEqual(cited, [["menter-1994"], ["nasa-tmr-sst"], [], physics["cites"]["farfield_bc"]])

    def test_unknown_ids_are_reported_with_number_and_param(self):
        decisions = [{"param": "iterations", "value": 1200, "reason": "plan"},
                     {"param": "relax_p", "value": 0.2, "reason": "r", "cites": ["patankar-1980"]},
                     {"param": "relax_U", "value": 0.5, "reason": "r", "cites": 4},
                     {"param": "consistent", "value": True, "reason": "r", "cites": ["a", "b", "c", "d"]}]
        cited, errors, unknown = planning.decision_cites("numerics", decisions, section(self.plan, "numerics"))
        self.assertEqual(unknown, ["patankar-1980"])
        self.assertEqual(errors, [
            support.unknown_id_error("decision 2 (relax_p)", "patankar-1980", "numerics", "decision"),
            'decision 3 (relax_U): cites must be a list of reference ids, for example ["spalart-allmaras-1992"]',
            "decision 4 (consistent): 4 references cited, at most 3: keep the ones that support the value most "
            "directly"])

    def test_without_a_plan_section_or_cites(self):
        self.assertEqual(planning.decision_cites("geometry", [{"param": "naca", "value": "2412", "reason": "r"}], {}),
                         ([[]], [], []))
        self.assertEqual(planning.decision_cites("geometry", None, {}), ([], [], []))
        decisions = [{"param": "a", "value": 1, "reason": "r", "cites": ["x"]}, "odd"]
        self.assertEqual(planning.without_cites(decisions), [{"param": "a", "value": 1, "reason": "r"}, "odd"])
        self.assertEqual(planning.without_cites(None), [])
        self.assertIn("cites", decisions[0])


class CompleteStage(unittest.TestCase):
    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.study = support.store_study(self.eng)
        self.runner = support.make_runner(self.eng, self.study)
        self.sid, self.cid = self.study["id"], self.runner.cid
        self.run_gate = support.patch(self, gates, "run_gate", return_value=GATE_PASSED)
        support.patch(self, solver, "kill_solver")

    def stage(self, key):
        return support.stage(self.eng, self.sid, self.cid, key)

    def gate_failures(self):
        return self.eng.store.get_case(self.sid, self.cid)["counters"]["gate_failures"]

    def in_stage(self, key, refusals=0, gate_limit=None):
        """The runner as run_stage leaves it inside a stage, for direct complete_stage calls."""
        self.runner.stage, self.runner.stage_span, self.runner.cite_refusals = key, "span0001", refusals
        if gate_limit:
            self.runner.gate_limit = gate_limit

    def run_policy(self, key, policy):
        """Run one stage through run_stage with `policy` as the stage's work."""
        with mock.patch.object(runtime.CaseRunner, "scripted_stage", policy):
            return self.runner.run_stage(key)

    def run_scripted(self, key, decisions):
        return self.run_policy(key, lambda runner, k: runner.call("complete_stage", summary="done", decisions=decisions))

    def test_unknown_id_is_refused_before_the_gate(self):
        self.in_stage("numerics")
        before = (self.stage("numerics")["attempts"], self.runner.gate_fails, self.gate_failures())
        result = self.runner.complete_stage("summary", [
            {"param": "iterations", "value": 1200, "reason": "plan"},
            {"param": "relax_p", "value": 0.2, "reason": "r", "cites": ["patankar-1980"]}])
        self.assertEqual(result["error"], support.unknown_id_error(
            "decision 2 (relax_p)", "patankar-1980", "numerics", "decision") + " The gate was not run and no attempt "
            "was used: correct the decisions and call complete_stage again.")
        self.assertEqual((result["ok"], result["gate_run"]), (False, False))
        self.assertEqual((result["unknown_ids"], result["valid_ids"]), (["patankar-1980"], citations.ids_for("numerics")))
        self.assertEqual(result["summary"], "failed: decisions cite an unknown reference id (gate not run)")
        self.run_gate.assert_not_called()
        self.assertEqual(support.events(self.eng, self.sid, "gate.result"), [])
        self.assertIsNone(self.runner.outcome)
        self.assertIsNone(self.stage("numerics")["gate"])
        self.assertEqual((self.stage("numerics")["attempts"], self.runner.gate_fails, self.gate_failures()), before)

    def test_a_refused_call_can_be_corrected_in_the_same_attempt(self):
        def policy(runner, key):
            bad = runner.call("complete_stage", summary="s", decisions=[
                {"param": "relax_p", "value": 0.2, "reason": "r", "cites": ["patankar-1980"]}])
            self.assertFalse(bad["ok"])
            runner.call("complete_stage", summary="s", decisions=[
                {"param": "relax_p", "value": 0.2, "reason": "r", "cites": ["openfoam-simple-relaxation"]}])
        self.assertEqual(self.run_policy("numerics", policy), ("passed",))
        self.assertEqual((self.run_gate.call_count, self.stage("numerics")["attempts"], self.gate_failures()), (1, 1, 0))
        self.assertEqual(self.stage("numerics")["decisions"][0]["cites"], ["openfoam-simple-relaxation"])

    def test_the_third_citation_mistake_goes_to_the_gate_without_the_cites(self):
        bad = [{"param": "relax_p", "value": 0.2, "reason": "r", "cites": ["patankar-1980"]},
               {"param": "iterations", "value": 1200, "reason": "plan"},
               {"param": "relax_U", "value": 0.5, "reason": "r", "cites": ["openfoam-simple-relaxation"]}]
        answers = []

        def policy(runner, key):
            for _ in range(3):
                answers.append(runner.call("complete_stage", summary="s", decisions=bad))
        self.assertEqual(self.run_policy("numerics", policy), ("passed",))
        self.assertEqual([(a["ok"], a.get("gate_run")) for a in answers], [(False, False), (False, False), (True, None)])
        note = DROPPED + "decision 1 (relax_p): unknown id patankar-1980 dropped"
        self.assertEqual((answers[2]["notes"], answers[2]["summary"]), ([note], "gate passed; " + note))
        record = self.stage("numerics")
        self.assertEqual((record["attempts"], record["status"], record.get("failure")), (1, "passed", None))
        self.assertEqual([d["cites"] for d in record["decisions"]],
                         [[], section(self.study["plan"], "numerics")["cites"]["iterations"],
                          ["openfoam-simple-relaxation"]])
        self.assertEqual((self.run_gate.call_count, self.gate_failures()), (1, 0))

    def test_the_count_is_per_run_of_the_stage(self):
        bad = [{"param": "relax_p", "value": 0.2, "reason": "r", "cites": 7}]
        self.in_stage("numerics")
        for _ in range(2):
            self.assertFalse(self.runner.complete_stage("s", bad)["ok"])
        self.assertEqual(self.runner.cite_refusals, runtime.MAX_CITE_REFUSALS)
        seen = []

        def policy(runner, key):
            seen.append(runner.cite_refusals)
            answer = runner.call("complete_stage", summary="s", decisions=bad)
            seen.append((answer["ok"], answer["summary"]))
            runner.call("complete_stage", summary="s", decisions=[])
        self.assertEqual(self.run_policy("numerics", policy), ("passed",))
        self.assertEqual(seen, [0, (False, "failed: the cites of a decision are not valid (gate not run)")])

    def test_a_failed_gate_counts_and_names_the_dropped_citations(self):
        self.run_gate.return_value = GATE_FAILED
        self.in_stage("mesh", refusals=runtime.MAX_CITE_REFUSALS, gate_limit=3)
        answer = self.runner.complete_stage("s", [{"param": "n_normal", "value": 90, "reason": "r", "cites": {"a": 1}}])
        self.assertEqual((answer["ok"], answer["gate_passed"], answer["summary"]), (False, False, GATE_FAILED_TEXT))
        self.assertEqual(answer["notes"], [DROPPED + "decision 1 (n_normal): cites dropped, they were not a list of "
                                                     "reference ids"])
        self.assertEqual((self.runner.gate_fails, self.gate_failures()), (1, 1))
        self.assertEqual([e["data"]["passed"] for e in support.events(self.eng, self.sid, "gate.result")], [False])

    def test_the_note_says_what_was_dropped_and_the_valid_ids_stay(self):
        self.in_stage("mesh", refusals=runtime.MAX_CITE_REFUSALS)
        four = ["project-gate-limits", "project-reference-4412", "openfoam-checkmesh-thresholds", "nasa-tmr-naca0012"]
        answer = self.runner.complete_stage("s", [
            {"param": "n_normal", "value": 110, "reason": "finer", "cites": four},
            {"param": "n_wake", "value": 60, "reason": "finer", "cites": ["project-reference-4412", "patankar-1980",
                                                                         "versteeg-2007"]},
            {"param": "n_aft", "value": 50, "reason": "finer", "cites": ["project-reference-4412"]}])
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["notes"], [DROPPED + "decision 1 (n_normal): 4 references cited, the first 3 kept; "
                                           "decision 2 (n_wake): unknown ids patankar-1980, versteeg-2007 dropped"])
        self.assertEqual([d["cites"] for d in self.runner.outcome[2]],
                         [four[:3], ["project-reference-4412"], ["project-reference-4412"]])

    def test_without_a_catalogue_the_dropped_note_says_so(self):
        self.in_stage("mesh", refusals=runtime.MAX_CITE_REFUSALS)
        support.use_catalogue(self, "{broken")
        with redirect_stderr(io.StringIO()):
            answer = self.runner.complete_stage("s", [{"param": "n_normal", "value": 110, "reason": "finer",
                                                       "cites": ["project-gate-limits"]}])
        self.assertEqual(answer["notes"], [DROPPED + "decision 1 (n_normal): cites dropped, the reference catalogue "
                                                     "could not be read"])
        self.assertEqual(self.runner.outcome[2][0]["cites"], [])

    def test_the_limit_of_failed_gates_in_a_row(self):
        self.run_gate.return_value = GATE_FAILED
        # an agent has three gates
        self.in_stage("mesh", gate_limit=3)
        for _ in range(3):
            self.runner.complete_stage("s", [])
        self.assertEqual(self.runner.outcome, ("failed", "gate failed 3 times in a row: " + support.REJECTED))
        # the scripted policy has one
        outcome = self.run_scripted("numerics", [])
        self.assertEqual(outcome, ("failed", GATE_FAILED_TEXT))
        self.assertEqual(self.stage("numerics")["failure"], GATE_FAILED_TEXT)
        self.assertEqual(support.events(self.eng, self.sid, "stage.failed")[-1]["data"]["reason"], GATE_FAILED_TEXT)

    def test_stored_decisions_and_the_event_carry_cites(self):
        plan = section(self.study["plan"], "numerics")
        decisions = [{"param": k, "value": plan[k], "reason": plan["reasons"].get(k, "plan value")}
                     for k in planning.NUMERICS_KEYS]
        decisions.append({"param": "iterations", "value": 1800, "reason": "50% more than planned"})
        self.assertEqual(self.run_scripted("numerics", decisions), ("passed",))
        stored = self.stage("numerics")["decisions"]
        self.assertEqual(len(stored), len(decisions))
        for d in stored:
            self.assertEqual(set(d), {"param", "value", "reason", "cites"})
        self.assertEqual({d["param"]: d["cites"] for d in stored[:-1]},
                         {k: plan["cites"].get(k, []) for k in planning.NUMERICS_KEYS})
        self.assertEqual(stored[-1]["cites"], [])
        completed = support.events(self.eng, self.sid, "stage.completed")
        self.assertEqual([c["data"]["decisions"] for c in completed], [stored])
        self.assertEqual(len(support.events(self.eng, self.sid, "gate.result")), 1)

    def test_every_decision_of_every_stage_reaches_the_last_agent(self):
        """All six earlier stages hand over each plan setting with the plan's own reason, which is
        what the scripted policy and a thorough agent write."""
        def policy(runner, key):
            plan = runner.context().plan_stage(key)
            decisions = [{"param": k, "value": v, "reason": plan["reasons"].get(k, "plan value")}
                         for k, v in plan.items() if k not in planning.PLAN_FIXED_KEYS]
            runner.call("complete_stage", summary=f"{key} done", decisions=decisions)
        for key in planning.STAGE_KEYS[:-1]:
            self.assertEqual(self.run_policy(key, policy), ("passed",), key)
        self.runner.stage, self.runner.agent_name = "validate", "validator"
        handoffs = self.runner.stage_context_text("validate", 1).split("## Hand-offs from earlier stages")[1]
        self.assertNotIn("truncated", handoffs)
        for key in planning.STAGE_KEYS[:-1]:
            line = next(part for part in handoffs.split("\n- ") if part.startswith(f"{key}:"))
            shown = json.loads(line.split("decisions: ")[1].split("\n  metrics:")[0])
            self.assertEqual(shown, planning.without_cites(self.stage(key)["decisions"]), key)
        numerics = json.dumps(planning.without_cites(self.stage("numerics")["decisions"]))
        self.assertGreater(len(numerics), 1200)                 # the old limit cut this list
        self.assertLess(len(numerics), agent_spec.HANDOFF_DECISION_CHARS - 300)

    def test_the_references_a_stage_is_shown(self):
        mesh_plan = section(self.study["plan"], "mesh")
        self.run_scripted("geometry", [{"param": "naca", "value": "2412", "reason": "selected candidate"}])
        self.run_scripted("mesh", [{"param": k, "value": mesh_plan[k], "reason": "plan value"} for k in planning.MESH_KEYS])
        self.assertTrue(self.stage("mesh")["decisions"][0]["cites"])
        self.runner.stage, self.runner.agent_name = "physics", "boundary-conditions"
        head = self.runner.stage_context_text("physics", 1).split("## Hand-offs from earlier stages")[0]
        block = head.split("## References you may cite (ids only)")[1]
        listed = [line[2:].split(" ")[0] for line in block.splitlines() if line.startswith("- ")]
        self.assertEqual(listed, citations.ids_for("physics"))
        self.assertIn("- menter-1994 (paper): What menter-1994 establishes", block)
        self.assertIn('"cites"', head.split("## References you may cite")[0])      # the plan section shows its own
        # get_context gives the same as data, and the upstream decisions without their cites
        context = agent_tools.tool_get_context(self.runner.context())
        self.assertEqual([r["id"] for r in context["references"]], citations.ids_for("physics"))
        self.assertEqual([u["stage"] for u in context["upstream"]], ["geometry", "mesh"])
        self.assertTrue(all("cites" not in d for u in context["upstream"] for d in u["decisions"]))
        # a stage that no source covers says so
        self.runner.stage, self.runner.agent_name = "post", "post-processor"
        self.assertIn("## References you may cite (ids only)\n(none for this stage: leave cites out)",
                      self.runner.stage_context_text("post", 1))


if __name__ == "__main__":
    unittest.main()
