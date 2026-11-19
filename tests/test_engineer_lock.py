"""The engineer lock: a mesh value the engineer set by hand goes to the gate as it is.

set_mesh_params refuses to replace it until the gate has rejected this run of the stage, or a
message or a re-run note has opened it. The agent tests play the live tutorial's failure
chapter with a scripted stand-in for the model: the real agent loop (engine.llm_loop), the real
mesh tools and the real mesh gate run on a temporary case directory. Only what needs Docker is
replaced: blockMesh and checkMesh are not called, and the gate gets fixed checkMesh numbers with
the y+ of the height that set_mesh_params really wrote. No model, no network, nothing under cases/.
"""
import copy
import json
import os
import re
import shutil
import tempfile
import types
import unittest
from unittest import mock

import support
from support import REJECTED, TYPED

from foamtools.common import read_json
from wingstudio import engine, gates, runtime
from wingstudio.agents import tools as agent_tools
from wingstudio.cfd import case as ac
from wingstudio.cfd import solver
from wingstudio.study import planning


class StubContext:
    """What agent_tools.engineer_locked reads from a tool context."""

    def __init__(self, runtime, stage="mesh"):
        self.runtime, self.stage = runtime, stage
        self.case = {"naca": "2412", "stages": [{"key": "mesh", "failure": None}]}

    def plan_stage(self, stage):
        return {"farfield_chords": 30.0, "n_normal": 90, "first_cell_height": TYPED,
                "reasons": {"first_cell_height": "Set by the engineer (was 0.00129).",
                            "n_normal": "23,400 cells in total.", "wake_chords": "Set by the engineer (was 30)."}
                } if stage == "mesh" else {}


class EngineerLocked(unittest.TestCase):
    def locked(self, wanted, rt=None, stage="mesh"):
        rt = rt or types.SimpleNamespace(gate_fails=0, lock_open=False)
        return agent_tools.engineer_locked(StubContext(rt, stage), "mesh", wanted)

    def test_only_another_value_of_an_engineer_setting_is_held(self):
        self.assertEqual(self.locked({"first_cell_height": 0.00129}), {"first_cell_height": TYPED})
        self.assertEqual(self.locked({"first_cell_height": "0.0013", "n_normal": 120}), {"first_cell_height": TYPED})
        for free in ({"first_cell_height": TYPED}, {"first_cell_height": "0.012", "n_normal": 90},
                     {"first_cell_height": None}, {},
                     {"n_normal": 120, "farfield_chords": 25},                # planner values
                     {"wake_chords": 20}):                                    # a marked reason without a value
            self.assertEqual(self.locked(free), {}, free)

    def test_no_lock_in_another_stage_or_without_a_case_runner(self):
        wanted = {"first_cell_height": 0.00129}
        for stage in ("solve", None):
            self.assertEqual(self.locked(wanted, stage=stage), {}, stage)
        study_runtime = runtime.StudyRuntime.__new__(runtime.StudyRuntime)   # it has no gate_fails
        odd = types.SimpleNamespace(gate_fails=True, lock_open=False)       # a bool is not a count
        for rt in (agent_tools.NullRuntime(), study_runtime, odd):
            self.assertEqual(self.locked(wanted, rt), {}, rt)


class FakeModel:
    """Plays a fixed list of turns; each turn is a list of (tool name, arguments)."""

    def __init__(self, turns):
        self.turns, self.requests, self.n = list(turns), [], 0

    def __call__(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        calls = []
        for name, args in (self.turns.pop(0) if self.turns else []):
            self.n += 1
            calls.append(types.SimpleNamespace(
                id=f"call{self.n}", type="function",
                function=types.SimpleNamespace(name=name, arguments=json.dumps(args))))
        dumped = {"role": "assistant", "content": None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]}
        message = types.SimpleNamespace(content=None, tool_calls=calls,
                                        model_dump=lambda exclude_none=True: dict(dumped))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="tool_calls")],
                                     usage=None)


def gate_input(stage, ctx):
    """checkMesh numbers of the default mesh, and the y+ of the height now written in the case."""
    params = read_json(os.path.join(ctx.case_dir, "mesh_plan.json"))["params"]
    cond = ctx.conditions()
    return {"fatal_error": None, "cells": 23400, "max_non_orthogonality": 43.0, "max_skewness": 0.9,
            "predicted_yplus": planning.predicted_yplus(params["first_cell_height"], cond["reynolds"], cond["velocity"]),
            "wall_treatment": "wallFunction", "mesh_matches_setup": True, "checked_this_run": True}


class LockCase(unittest.TestCase):
    """A Haiku study whose plan holds the engineer's 0.012 on NACA 2412, and the case runner of
    that section on a temporary case directory with its geometry."""

    def setUp(self):
        support.use_catalogue(self)
        self.model = FakeModel([])
        self.eng = support.make_engine(self, completion=self.model)
        study = support.make_study(model=support.HAIKU)
        self.planned = support.with_typed_height(study)
        self.study = support.store_study(self.eng, study)
        self.sid = self.study["id"]
        self.case_dir = tempfile.mkdtemp(prefix="studio-case-")
        self.addCleanup(shutil.rmtree, self.case_dir, ignore_errors=True)
        ac.write_geometry(self.case_dir, "2412")
        self.runner = support.make_runner(self.eng, self.study, case_dir=self.case_dir)
        support.patch(self, gates, "collect_gate_input", gate_input)
        support.patch(self, engine.Engine, "_usage", staticmethod(lambda response, model: (0, 0, 0, 0.0)))
        support.patch(self, solver, "kill_solver")

    def mesh(self):
        return support.stage(self.eng, self.sid, self.runner.cid, "mesh")

    def gate_failures(self):
        return self.eng.store.get_case(self.sid, self.runner.cid)["counters"]["gate_failures"]


class LockInTheRunner(LockCase):
    def setUp(self):
        super().setUp()
        self.runner.stage, self.runner.agent_name, self.runner.stage_span = "mesh", "mesher", "span0001"

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.case_dir)
                      for d, _, names in os.walk(self.case_dir) for f in names)

    def test_set_mesh_params_refuses_and_writes_nothing(self):
        before = self.files()
        result = agent_tools.execute_tool(self.runner.context(), "set_mesh_params",
                                          {"first_cell_height": self.planned})
        self.assertFalse(result["ok"])
        self.assertEqual(result["engineer_value"], {"first_cell_height": TYPED})
        self.assertTrue(result["error"].startswith(
            "first_cell_height = 0.012 was set by the engineer and is an instruction: leave it as it is, build and "
            "check the mesh with it and call complete_stage."))
        self.assertTrue(result["error"].endswith("the failed gate releases the value."))
        self.assertTrue(result["summary"].startswith("failed: first_cell_height = 0.012 was set by the engineer"))
        self.assertEqual(self.files(), before)

    def test_the_lock_follows_the_runner(self):
        ctx, wanted = self.runner.context(), {"first_cell_height": self.planned}
        self.assertEqual(agent_tools.engineer_locked(ctx, "mesh", wanted), {"first_cell_height": TYPED})
        self.assertEqual(agent_tools.engineer_locked(ctx, "mesh", {"first_cell_height": TYPED, "n_normal": 100}), {})
        self.runner.gate_fails = 1                                               # the gate rejected this run
        self.assertEqual(agent_tools.engineer_locked(ctx, "mesh", wanted), {})
        self.runner.gate_fails, self.runner.lock_open = 0, True                  # a message, a re-run note or a rework
        self.assertEqual(agent_tools.engineer_locked(ctx, "mesh", wanted), {})
        self.runner.lock_open = False
        self.runner.update_stage("mesh", lambda s: s.__setitem__("failure", "gate failed 3 times in a row"))
        self.assertEqual(agent_tools.engineer_locked(ctx, "mesh", wanted), {})

    def test_the_scripted_policy_passes_plan_values(self):
        plan = self.runner.context().plan_stage("mesh")
        wanted = {k: plan[k] for k in planning.MESH_KEYS if k in plan}
        self.assertEqual(agent_tools.engineer_locked(self.runner.context(), "mesh", wanted), {})

    def test_run_stage_closes_the_lock_again(self):
        seen = []

        def policy(runner, key):
            seen.append((runner.lock_open, runner.gate_fails))
            runner.outcome = ("failed", "stub")
        self.eng.store.update_study(self.sid, lambda s: s.__setitem__("model", "scripted"))
        self.runner.lock_open, self.runner.gate_fails = True, 2
        with mock.patch.object(runtime.CaseRunner, "scripted_stage", policy):
            self.assertEqual(self.runner.run_stage("mesh")[0], "failed")
        self.assertEqual(seen, [(False, 0)])

    def test_the_engineer_block_tells_the_agent_to_submit(self):
        text = self.runner.stage_context_text("mesh", 1)
        block = text.split("## Values the engineer set by hand in this section")[1].split("\n## ")[0]
        self.assertIn('"first_cell_height": 0.012', block)
        for part in ("call complete_stage with them in place", "say so once with note() and still submit it",
                     "is not a rejection: only the gate decides", "Only when complete_stage answers that the gate "
                     "did not pass because of that value may you change it",
                     "state the engineer's value, the failed check and your value in your summary and decisions.",
                     " set_mesh_params refuses another value for these settings until the gate has judged them."):
            self.assertIn(part, block)
        self.assertNotIn("—", text)

    def test_other_stages_get_the_block_without_the_mesh_sentence(self):
        def edit(s):
            physics = s["plan"]["cases"][0]["stages"]["physics"]
            physics["nut_ratio"] = 3.0
            physics["reasons"]["nut_ratio"] = "Set by the engineer (was 4.0)."
        self.eng.store.update_study(self.sid, edit)
        self.runner.stage, self.runner.agent_name = "physics", "boundary-conditions"
        text = self.runner.stage_context_text("physics", 1)
        self.assertIn("## Values the engineer set by hand in this section", text)
        self.assertIn("only the gate decides", text)
        self.assertNotIn("set_mesh_params refuses", text)
        # a section without an engineer value has no block
        self.runner.stage, self.runner.agent_name = "numerics", "solver-setup"
        self.assertNotIn("## Values the engineer set by hand", self.runner.stage_context_text("numerics", 1))


class MeshAgentLoop(LockCase):
    def written_height(self):
        plan = read_json(os.path.join(self.case_dir, "mesh_plan.json"))
        return plan["params"]["first_cell_height"] if plan else None

    def trace(self):
        """(type, short text) of the mesh stage's events, in order."""
        out = []
        for e in support.events(self.eng, self.sid):
            d = e["data"]
            if e["type"] == "tool.result":
                # the growth ratio is the mesher's arithmetic, not what is tested here
                out.append(("tool.result", d["name"], d["ok"], re.sub(r"growth [0-9.]+", "growth N", d["summary"])))
            elif e["type"] == "gate.result":
                out.append(("gate.result", d["passed"]))
            elif e["type"] == "stage.started":
                out.append(("stage.started", d["attempt"]))
            elif e["type"] in ("stage.completed", "stage.failed"):
                out.append((e["type"],))
        return out

    def test_expected_live_sequence(self):
        final = [{"param": "first_cell_height", "value": self.planned,
                  "reason": "the engineer set 0.012; the gate rejected it (y+ 372 against 20 to 200)",
                  "cites": ["project-yplus-40"]},
                 {"param": "n_normal", "value": 90, "reason": "plan value"}]
        self.model.turns = [
            [("get_context", {}), ("estimate_first_cell", {})],
            [("note", {"text": "0.012 chord gives y+ about 372, outside 20 to 200. Submitting it as instructed."})],
            [("set_mesh_params", {"first_cell_height": TYPED})],
            [("complete_stage", {"summary": "Mesh set up with the engineer's first cell height.", "decisions": [
                {"param": "first_cell_height", "value": TYPED, "reason": "set by the engineer"}]})],
            [("set_mesh_params", {"first_cell_height": self.planned})],
            [("complete_stage", {"summary": "The gate rejected 0.012; rebuilt with the estimate.",
                                 "decisions": final})],
        ]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertEqual(self.trace(), [
            ("stage.started", 1),
            ("tool.result", "get_context", True, "context for NACA 2412 at 2.5 deg, stage mesh"),
            ("tool.result", "estimate_first_cell", True, "first cell 0.00129 chord for y+ 40 at Re 1.5e+06"),
            ("tool.result", "note", True, "noted"),
            ("tool.result", "set_mesh_params", True, "23,400 cells planned, wall-normal growth N, predicted y+ 372"),
            ("gate.result", False),
            ("stage.started", 2),
            ("tool.result", "complete_stage", False, "gate failed: " + REJECTED),
            ("tool.result", "set_mesh_params", True, "23,400 cells planned, wall-normal growth N, predicted y+ 40"),
            ("gate.result", True),
            ("tool.result", "complete_stage", True, "gate passed"),
            ("stage.completed",),
        ])
        self.assertEqual(self.gate_failures(), 1)
        self.assertEqual(self.mesh()["attempts"], 2)
        self.assertEqual(self.mesh()["decisions"], [dict(final[0]), dict(final[1], cites=["project-reference-4412",
                                                                                         "project-gate-limits"])])
        self.assertAlmostEqual(self.written_height(), self.planned)
        # the plan keeps the engineer's value
        self.assertEqual(support.section(self.eng.store.get_study(self.sid)["plan"], "mesh")["first_cell_height"], TYPED)
        # what the agent was told
        first = self.model.requests[0][1]["content"]
        self.assertIn("## Values the engineer set by hand in this section", first)
        self.assertIn("set_mesh_params refuses another value for these settings until the gate has judged them.", first)
        self.assertIn("## References you may cite (ids only)", first)
        context = json.loads(self.model.requests[1][3]["content"])
        self.assertIn("project-yplus-40", [r["id"] for r in context["references"]])
        estimate = json.loads(self.model.requests[1][4]["content"])
        self.assertEqual(estimate["cites"], support.DEFAULTS["mesh.first_cell_height"])

    def test_an_early_correction_is_refused_and_the_agent_still_has_a_move(self):
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": self.planned})],          # the lock holds it
            [("set_mesh_params", {"first_cell_height": TYPED})],
            [("complete_stage", {"summary": "Submitted with the engineer's value.", "decisions": [
                {"param": "first_cell_height", "value": TYPED, "reason": "set by the engineer"},
                {"param": "n_normal", "value": 90, "reason": "plan value",
                 "cites": ["made-up-source"]}]})],                               # refused before the gate
            [("complete_stage", {"summary": "Submitted with the engineer's value.", "decisions": [
                {"param": "first_cell_height", "value": TYPED, "reason": "set by the engineer"}]})],
            [("set_mesh_params", {"first_cell_height": self.planned})],          # the failed gate released it
            [("complete_stage", {"summary": "Rebuilt with the estimate.", "decisions": [
                {"param": "first_cell_height", "value": self.planned, "reason": "the gate rejected 0.012"}]})],
        ]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        trace = self.trace()
        self.assertEqual(trace[1][:3], ("tool.result", "set_mesh_params", False))
        self.assertTrue(trace[1][3].startswith("failed: first_cell_height = 0.012 was set by the engineer and is an "
                                               "instruction"))
        refused = json.loads(self.model.requests[1][-1]["content"])
        self.assertEqual(refused["engineer_value"], {"first_cell_height": TYPED})
        self.assertEqual(trace[3], ("tool.result", "complete_stage", False,
                                    "failed: decisions cite an unknown reference id (gate not run)"))
        self.assertEqual(json.loads(self.model.requests[3][-1]["content"])["unknown_ids"], ["made-up-source"])
        self.assertEqual([t for t in trace if t[0] == "gate.result"], [("gate.result", False), ("gate.result", True)])
        self.assertEqual((self.mesh()["attempts"], self.gate_failures()), (2, 1))
        self.assertEqual(self.mesh()["decisions"], [{"param": "first_cell_height", "value": self.planned,
                                                     "reason": "the gate rejected 0.012", "cites": []}])

    def test_a_message_from_the_engineer_opens_the_lock(self):
        self.runner.inbox.append({"text": "use the estimate instead of my value", "id": "m1"})
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": self.planned})],
            [("complete_stage", {"summary": "Built with the estimate as the engineer asked.", "decisions": [
                {"param": "first_cell_height", "value": self.planned, "reason": "the engineer's message"}]})],
        ]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertEqual([t[2] for t in self.trace() if t[0] == "tool.result"], [True, True])
        self.assertEqual(self.mesh()["attempts"], 1)
        self.assertIn("HUMAN MESSAGE: use the estimate instead of my value", self.model.requests[0][1]["content"])
        delivered = support.events(self.eng, self.sid, "human.message")
        self.assertEqual(delivered[-1]["data"]["delivered_to"], "mesher")

    def test_a_model_that_keeps_citing_a_bad_id_cannot_fail_the_stage(self):
        stuck = ("complete_stage", {"summary": "Submitted with the engineer's value.", "decisions": [
            {"param": "first_cell_height", "value": TYPED, "reason": "set by the engineer"},
            {"param": "n_normal", "value": 90, "reason": "plan value", "cites": ["patankar-1980"]}]})
        self.model.turns = [[("set_mesh_params", {})]] + [[stuck]] * 3 + [
            [("set_mesh_params", {"first_cell_height": self.planned})],          # the gate has judged 0.012 by now
            [("complete_stage", {"summary": "Rebuilt with the estimate.", "decisions": [
                {"param": "first_cell_height", "value": self.planned, "reason": "the gate rejected 0.012"}]})]]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertEqual(len(self.model.requests), 6)                            # not the mesher's 24 turns
        results = [t for t in self.trace() if t[0] == "tool.result" and t[1] == "complete_stage"]
        self.assertEqual([r[3] for r in results], [
            "failed: decisions cite an unknown reference id (gate not run)",
            "failed: decisions cite an unknown reference id (gate not run)",
            "gate failed: " + REJECTED, "gate passed"])
        third = json.loads(self.model.requests[4][-1]["content"])
        self.assertEqual(third["notes"], ["citations dropped after 2 refusals: decision 2 (n_normal): unknown id "
                                          "patankar-1980 dropped"])
        self.assertEqual((self.mesh()["attempts"], self.mesh()["status"], self.mesh().get("failure")),
                         (2, "passed", None))
        self.assertEqual(self.gate_failures(), 1)

    def run_once_through_the_gate(self):
        """The expected live sequence in short: 0.012 is rejected, the estimate passes."""
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": TYPED})],
            [("complete_stage", {"summary": "As instructed.", "decisions": [
                {"param": "first_cell_height", "value": TYPED, "reason": "set by the engineer"}]})],
            [("set_mesh_params", {"first_cell_height": self.planned})],
            [("complete_stage", {"summary": "Rebuilt.", "decisions": [
                {"param": "first_cell_height", "value": self.planned, "reason": "the gate rejected 0.012"}]})]]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertEqual(self.mesh()["engineer_rejected"], {"values": {"first_cell_height": TYPED}, "checks": REJECTED})

    def rerun_from_geometry(self):
        """What a re-run from Geometry leaves when the mesh stage starts again: geometry has
        passed once more, mesh is stale and has no note of its own."""
        self.runner.rewind("geometry", "re-run requested by the engineer", by="human")
        self.assertEqual(self.mesh()["status"], "stale")
        self.assertEqual(self.runner.notes.pop("geometry"), ["The engineer asked for this stage to be redone: "
                                                             "re-run requested by the engineer"])
        self.assertNotIn("mesh", self.runner.notes)

    def test_after_a_rerun_from_an_earlier_stage_the_judged_value_is_not_forced_again(self):
        self.run_once_through_the_gate()
        self.rerun_from_geometry()
        asked = len(self.model.requests)
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": self.planned})],          # accepted at once
            [("complete_stage", {"summary": "Built with the estimate again.", "decisions": [
                {"param": "first_cell_height", "value": self.planned,
                 "reason": "the engineer set 0.012; the gate rejected it in the earlier run"}]})]]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        told = self.model.requests[asked][1]["content"]
        block = told.split("## Earlier run of this stage\n")[1].split("\n\n")[0]
        self.assertEqual(block, "The gate has already judged the engineer's first_cell_height = 0.012 in an earlier "
                                f"run of this stage and did not pass ({REJECTED}). If that check failed because of "
                                "the value, do not submit it again: use the value you would recommend and state the "
                                "engineer's value, the failed check and your value in your summary and decisions. If "
                                "the check had another cause, apply the value as given.")
        self.assertIn("## Values the engineer set by hand in this section", told)
        self.assertEqual([r[2] for r in [t for t in self.trace() if t[0] == "tool.result"][-2:]], [True, True])
        self.assertEqual(self.gate_failures(), 1)
        self.assertAlmostEqual(self.written_height(), self.planned)

    def test_a_value_the_engineer_set_afterwards_is_locked_again(self):
        self.run_once_through_the_gate()

        def retype(s):
            mesh = s["plan"]["cases"][0]["stages"]["mesh"]
            mesh["first_cell_height"] = 0.02
            mesh["reasons"]["first_cell_height"] = "Set by the engineer (was 0.012)."
        self.eng.store.update_study(self.sid, retype)
        self.rerun_from_geometry()
        asked = len(self.model.requests)
        self.model.turns = [[("set_mesh_params", {"first_cell_height": self.planned})]]
        self.runner.run_stage("mesh")
        self.assertNotIn("## Earlier run of this stage", self.model.requests[asked][1]["content"])
        refused = json.loads(self.model.requests[asked + 1][-1]["content"])
        self.assertEqual(refused["engineer_value"], {"first_cell_height": 0.02})

    def test_a_gate_that_failed_on_another_value_judged_nothing(self):
        self.runner.inbox.append({"text": "use 0.03 instead of my value", "id": "m1"})
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": 0.03})],                  # the message opened the lock
            [("complete_stage", {"summary": "As the message says.", "decisions": [
                {"param": "first_cell_height", "value": 0.03, "reason": "the engineer's message"}]})],
            [("set_mesh_params", {"first_cell_height": self.planned})],
            [("complete_stage", {"summary": "Rebuilt.", "decisions": [
                {"param": "first_cell_height", "value": self.planned, "reason": "the gate rejected 0.03"}]})]]
        self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertNotIn("engineer_rejected", self.mesh())                       # 0.012 was never in the case
        self.rerun_from_geometry()
        self.runner.stage, self.runner.lock_open = "mesh", False                 # as run_stage starts it
        self.assertNotIn("## Earlier run of this stage", self.runner.stage_context_text("mesh", 3))
        self.assertFalse(self.runner.lock_open)

    def test_a_pass_with_the_engineer_value_takes_the_rejection_back(self):
        """The first gate failed on something else (here: made to fail); the value itself passes."""
        self.eng.store.update_study(self.sid, lambda s: s["plan"]["cases"][0]["stages"]["mesh"].update(
            first_cell_height=0.0015))                                           # y+ about 46: inside the band
        failing = {"passed": False, "checks": [{"name": "mesh built from these values", "value": "no", "limit": "yes",
                                                "ok": False}]}
        self.model.turns = [
            [("set_mesh_params", {"first_cell_height": 0.0015})],
            [("complete_stage", {"summary": "Too early.", "decisions": []})],
            [("complete_stage", {"summary": "Built now.", "decisions": [
                {"param": "first_cell_height", "value": 0.0015, "reason": "set by the engineer"}]})]]
        passing = {"passed": True, "checks": [dict(failing["checks"][0], value="yes", ok=True)]}
        with mock.patch.object(gates, "run_gate", side_effect=[failing, passing]):
            self.assertEqual(self.runner.run_stage("mesh"), ("passed",))
        self.assertEqual(self.gate_failures(), 1)
        self.assertNotIn("engineer_rejected", self.mesh())
        self.rerun_from_geometry()
        self.runner.stage, self.runner.lock_open, self.runner.gate_fails = "mesh", False, 0   # as run_stage starts it
        self.assertNotIn("## Earlier run of this stage", self.runner.stage_context_text("mesh", 3))
        self.assertEqual(agent_tools.engineer_locked(self.runner.context(), "mesh", {"first_cell_height": self.planned}),
                         {"first_cell_height": 0.0015})

    def test_a_rerun_of_the_stage_itself_gets_the_instruction_only(self):
        self.run_once_through_the_gate()
        self.runner.rewind("mesh", "Test of the gate: keep first_cell_height at 0.012", by="human")
        self.runner.stage, self.runner.lock_open = "mesh", False
        text = self.runner.stage_context_text("mesh", 3)
        self.assertIn("## Why this stage is being run again", text)
        self.assertNotIn("## Earlier run of this stage", text)                   # the instruction stands alone
        self.assertTrue(self.runner.lock_open)                                   # the stage's own note opened it


if __name__ == "__main__":
    unittest.main()
