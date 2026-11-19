"""The engineer's edits of a plan, through PATCH /plan and through the steering agent.

An edited value is marked as the engineer's, its citation is dropped, and nothing else moves:
the other reasons and citations stay the stored ones, whatever the request sends beside them.
Stores are temporary: no Docker, no model, nothing under studio_data/ or cases/.
"""
import copy
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import support
from support import MARK, TYPED, section

from wingstudio import runtime
from wingstudio.agents import tools as agent_tools
from wingstudio.study import planning


def without_cites(plan):
    """A plan as stored before citations existed."""
    for pc in plan["cases"]:
        pc.pop("cites", None)
        for sec in pc["stages"].values():
            sec.pop("cites", None)
    return plan


def values_only(plan):
    """The stored plan as a client that knows nothing of reasons, checks and cites sends it back."""
    body = without_cites(copy.deepcopy(plan))
    for pc in body["cases"]:
        for sec in pc["stages"].values():
            for fixed in planning.PLAN_FIXED_KEYS:
                sec.pop(fixed, None)
    return body


class EngineerPaths(unittest.TestCase):
    """normalise_plan on the engineer path, then mark_engineer_edits, as PATCH /plan runs them."""

    def setUp(self):
        support.use_catalogue(self)
        self.study = support.make_study()
        self.stored, _ = support.default_plan(self.study)

    def edit(self, change, stored=None):
        plan, changed = support.engineer_edit(self.study, self.stored if stored is None else stored, change)
        return plan, planning.validate_plan(plan, cites=False), changed

    def test_cites_in_the_request_are_ignored(self):
        def change(body):
            body["cases"][0]["cites"] = {"alpha_deg": ["invented"]}
            section(body, "mesh")["cites"] = {"n_normal": ["invented"], "bogus": ["x"]}
            section(body, "physics")["cites"] = "nonsense"
        plan, errors, changed = self.edit(change)
        self.assertEqual((errors, changed), ([], []))
        self.assertEqual(plan["cases"], self.stored["cases"])
        self.assertEqual(planning.validate_plan(plan), [])

    def test_an_edit_removes_the_citation_of_that_setting_only(self):
        plan, errors, changed = self.edit(lambda body: section(body, "mesh").update(first_cell_height=TYPED))
        self.assertEqual((errors, changed), ([], ["2412.mesh.first_cell_height"]))
        mesh = section(plan, "mesh")
        self.assertTrue(mesh["reasons"]["first_cell_height"].startswith(MARK + " (was "))
        expected = dict(section(self.stored, "mesh")["cites"])
        expected.pop("first_cell_height")
        self.assertEqual(mesh["cites"], expected)
        self.assertNotIn("cites", mesh["reasons"])              # `cites` is never taken for a setting
        self.assertEqual(section(plan, "mesh", 1), section(self.stored, "mesh", 1))
        self.assertEqual(plan["cases"][0]["cites"], self.stored["cases"][0]["cites"])

    def test_setting_the_value_back_does_not_bring_the_citation_back(self):
        default = section(self.stored, "mesh")["first_cell_height"]
        first, _, _ = self.edit(lambda body: section(body, "mesh").update(first_cell_height=TYPED))
        back, errors, changed = self.edit(lambda body: section(body, "mesh").update(first_cell_height=default), first)
        self.assertEqual((errors, changed), ([], ["2412.mesh.first_cell_height"]))
        self.assertEqual(section(back, "mesh")["first_cell_height"], default)
        self.assertNotIn("first_cell_height", section(back, "mesh")["cites"])
        self.assertEqual(section(back, "mesh")["reasons"]["first_cell_height"], f"{MARK} (was {TYPED}).")

    def test_a_changed_angle_loses_the_case_level_citation(self):
        plan, errors, _ = self.edit(lambda body: body["cases"][1].update(alpha_deg=5.0))
        self.assertEqual(errors, [])
        self.assertEqual(plan["cases"][1]["cites"], {})
        self.assertEqual(plan["cases"][0]["cites"], {"alpha_deg": support.DEFAULTS["case.alpha_deg"]})
        self.assertEqual(plan["cases"][1]["stages"], self.stored["cases"][1]["stages"])

    def test_the_same_turbulence_model_keeps_the_nut_ratio_references(self):
        plan, _, _ = self.edit(lambda body: section(body, "physics").update(farfield_bc="freestream"))
        self.assertEqual(section(plan, "physics")["cites"]["nut_ratio"], support.DEFAULTS["physics.nut_ratio"])

    def test_a_retired_id_in_the_stored_plan_is_dropped_without_an_error(self):
        stored = copy.deepcopy(self.stored)
        section(stored, "mesh")["cites"]["n_normal"] = ["retired-entry", "project-gate-limits"]
        section(stored, "mesh")["cites"]["n_wake"] = ["retired-entry"]
        stored["cases"][0]["cites"] = {"alpha_deg": ["retired-entry"]}
        plan, errors, _ = self.edit(lambda body: None, stored)
        self.assertEqual(errors, [])
        self.assertEqual(section(plan, "mesh")["cites"]["n_normal"], ["project-gate-limits"])
        self.assertNotIn("n_wake", section(plan, "mesh")["cites"])
        self.assertEqual(plan["cases"][0]["cites"], {})
        self.assertEqual(planning.validate_plan(plan), [])

    def test_stored_ids_survive_while_the_catalogue_cannot_be_read(self):
        support.use_catalogue(self, missing=True)
        with redirect_stderr(io.StringIO()):
            plan, errors, _ = self.edit(lambda body: section(body, "numerics").update(iterations=1600))
        self.assertEqual(errors, [])
        self.assertEqual(section(plan, "mesh")["cites"], section(self.stored, "mesh")["cites"])
        self.assertNotIn("iterations", section(plan, "numerics")["cites"])

    def test_a_plan_from_before_citations_stays_uncited(self):
        old = without_cites(copy.deepcopy(self.stored))
        self.assertEqual(planning.validate_plan(old), [])           # an old stored plan is still a valid plan
        plan, errors, _ = self.edit(lambda body: section(body, "mesh").update(n_normal=100), old)
        self.assertEqual(errors, [])
        for pc in plan["cases"]:
            self.assertEqual(pc["cites"], {})
            self.assertTrue(all(sec["cites"] == {} for sec in pc["stages"].values()))
        self.assertEqual(planning.validate_plan(plan), [])
        # and the marks are found without them
        new = without_cites(copy.deepcopy(self.stored))
        section(new, "numerics")["iterations"] = 1600
        new["cases"][0]["alpha_deg"] = 3.3
        self.assertEqual(planning.mark_engineer_edits(self.stored, new), ["2412.numerics.iterations"])


class MarkInTheEngine(unittest.TestCase):
    """engine.patch_plan and the steering agent's update_plan on a stored study in which the
    engineer has typed 0.012 for the first cell height."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.study = support.store_study(self.eng, support.make_study(model=support.HAIKU))
        self.sid = self.study["id"]
        body = copy.deepcopy(self.study["plan"])
        section(body, "mesh")["first_cell_height"] = TYPED
        self.edited = self.eng.patch_plan(self.sid, body)["plan"]
        self.steering = runtime.StudyRuntime(self.eng, self.sid, "steering")

    def stored(self):
        return self.eng.store.get_study(self.sid)["plan"]

    def test_a_body_without_reasons_keeps_the_mark_and_the_lock(self):
        marked = section(self.edited, "mesh")["reasons"]["first_cell_height"]
        self.assertTrue(marked.startswith(MARK + " (was "))
        plan = self.eng.patch_plan(self.sid, values_only(self.edited))["plan"]
        mesh = section(plan, "mesh")
        self.assertEqual((plan["version"], mesh["first_cell_height"]), (3, TYPED))
        self.assertEqual(mesh["reasons"]["first_cell_height"], marked)
        self.assertNotIn("first_cell_height", mesh["cites"])
        # nothing else moved: the other reasons and citations are the stored ones
        self.assertEqual(plan["cases"], self.edited["cases"])
        runner = support.make_runner(self.eng, self.eng.store.get_study(self.sid))
        runner.stage = "mesh"
        self.assertEqual(agent_tools.engineer_locked(runner.context(), "mesh", {"first_cell_height": 0.00129}),
                         {"first_cell_height": TYPED})
        self.assertIn("## Values the engineer set by hand in this section", runner.stage_context_text("mesh", 1))

    def test_another_reason_in_the_request_cannot_take_the_mark_off(self):
        body = copy.deepcopy(self.edited)
        section(body, "mesh")["reasons"]["first_cell_height"] = "0.012 chord gives y+ about 40."
        section(body, "mesh")["reasons"]["n_wake"] = "50 cells, my own wording."
        plan = self.eng.patch_plan(self.sid, body)["plan"]
        self.assertTrue(section(plan, "mesh")["reasons"]["first_cell_height"].startswith(MARK))
        self.assertEqual(section(plan, "mesh")["reasons"]["n_wake"], "50 cells, my own wording.")

    def test_steering_can_set_neither_the_reason_nor_the_cites(self):
        result = self.steering.update_plan("2412", "mesh", {"n_normal": 100, "reasons": {"n_normal": "finer"},
                                                            "cites": {"n_normal": ["invented"]}})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("cites", result["summary"])
        mesh = section(self.stored(), "mesh")
        self.assertEqual(mesh["n_normal"], 100)
        self.assertNotIn("n_normal", mesh["cites"])
        self.assertEqual(mesh["cites"]["n_wake"], support.DEFAULTS["mesh.n_wake"])
        self.assertTrue(mesh["reasons"]["first_cell_height"].startswith(MARK + " (was "))
        self.assertTrue(mesh["reasons"]["n_normal"].startswith(MARK + " (was 90)"))

    def test_cites_is_not_a_steering_setting(self):
        version = self.stored()["version"]
        result = self.steering.update_plan("2412", "mesh", {"cites": {"n_normal": ["project-gate-limits"]}})
        self.assertEqual((result["ok"], result["error"]), (False, "nothing to change"))
        self.assertEqual(self.stored()["version"], version)
        result = self.steering.update_plan("2412", "mesh", {"bogus": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["settings"], planning.MESH_KEYS)

    def test_a_changed_angle_loses_its_citation(self):
        self.assertTrue(self.steering.update_plan("4412", "physics", {"alpha_deg": 4.0})["ok"])
        plan = self.stored()
        self.assertEqual(plan["cases"][1]["cites"], {})
        self.assertEqual(plan["cases"][0]["cites"], {"alpha_deg": support.DEFAULTS["case.alpha_deg"]})

    def test_another_turbulence_model_drops_the_nut_ratio_references(self):
        # The mock once kept the Spalart-Allmaras sources under kOmegaSST; the engine is the rule
        # the mock mirrors, so it is pinned here through both of its entry points
        body = copy.deepcopy(self.stored())
        section(body, "physics")["turbulence_model"] = "kOmegaSST"
        physics = section(self.eng.patch_plan(self.sid, body)["plan"], "physics")          # PATCH /plan
        self.assertNotIn("nut_ratio", physics["cites"])
        self.assertNotIn("turbulence_model", physics["cites"])
        self.assertEqual(physics["reasons"]["turbulence_model"], f"{MARK} (was SpalartAllmaras).")
        self.assertIn("with k-omega SST the same number is nut / nu", physics["reasons"]["nut_ratio"])
        self.assertTrue(self.steering.update_plan("4412", "physics", {"turbulence_model": "kOmegaSST"})["ok"])
        stored = self.stored()
        self.assertNotIn("nut_ratio", section(stored, "physics", 1)["cites"])
        self.assertNotIn("nut_ratio", section(stored, "physics")["cites"])                # and it does not come back
        for case in (0, 1):
            self.assertEqual(section(stored, "physics", case)["cites"]["farfield_bc"],
                             support.DEFAULTS["physics.farfield_bc"])


class ReasonsOnPatch(unittest.TestCase):
    """A reason the store holds is kept, and a default sentence comes only with a default value."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        self.study = support.make_study()
        self.default = planning.default_plan(self.study)

    def store(self, change):
        stored, _ = support.default_plan(self.study)
        change(stored)
        self.study["plan"] = stored
        support.store_study(self.eng, self.study)
        return stored

    def test_a_stored_reason_survives_an_edit(self):
        def own(plan):
            section(plan, "numerics")["iterations"] = 1800
            section(plan, "numerics")["reasons"]["iterations"] = "1800: the angle is close to stall."
        stored = self.store(own)
        with_default = copy.deepcopy(stored)
        section(with_default, "numerics")["reasons"]["iterations"] = section(self.default, "numerics")["reasons"]["iterations"]
        for body in (with_default, values_only(stored)):
            section(body, "mesh")["n_wake"] = 60
            plan = self.eng.patch_plan(self.study["id"], body)["plan"]
            self.assertEqual(section(plan, "numerics")["reasons"]["iterations"], "1800: the angle is close to stall.")
            self.assertEqual(section(plan, "mesh")["reasons"]["n_wake"], f"{MARK} (was 50).")

    def test_an_old_plan_does_not_get_default_reasons_for_changed_values(self):
        def old(plan):
            # as stored before the default reasons existed: changed values, no sentence for them
            section(plan, "numerics")["relax_U"] = 0.5
            section(plan, "numerics")["reasons"].pop("relax_U")
            section(plan, "mesh")["wake_chords"] = 20.0
            section(plan, "mesh")["reasons"].pop("wake_chords")
            section(plan, "mesh")["reasons"].pop("n_wake")       # at its default: the sentence may come
        body = copy.deepcopy(self.store(old))
        section(body, "mesh")["n_normal"] = 100
        plan = self.eng.patch_plan(self.study["id"], body)["plan"]
        self.assertNotIn("relax_U", section(plan, "numerics")["reasons"])
        self.assertNotIn("wake_chords", section(plan, "mesh")["reasons"])
        self.assertTrue(section(plan, "mesh")["reasons"]["n_wake"].startswith("50 cells along"))
        self.assertEqual(section(plan, "mesh")["reasons"]["n_normal"], f"{MARK} (was 90).")


class PatchPlanRoute(support.ServerCase):
    def setUp(self):
        super().setUp()
        self.study = support.store_study(self.engine)
        self.sid = self.study["id"]

    def patch_plan(self, body):
        r = self.client.patch(f"/api/studies/{self.sid}/plan", json={"plan": body})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["plan"]

    def test_an_edit_is_stored_as_the_engine_marks_it(self):
        body = copy.deepcopy(self.study["plan"])
        section(body, "mesh")["first_cell_height"] = TYPED
        section(body, "mesh")["cites"]["n_normal"] = ["invented"]                # whatever the page sends back is ignored
        plan = self.patch_plan(body)
        self.assertEqual(plan["version"], 2)
        self.assertEqual(section(plan, "mesh")["first_cell_height"], TYPED)
        self.assertTrue(section(plan, "mesh")["reasons"]["first_cell_height"].startswith(MARK + " (was "))
        self.assertEqual(section(plan, "mesh")["cites"]["n_normal"], section(self.study["plan"], "mesh")["cites"]["n_normal"])
        self.assertEqual(self.client.get(f"/api/studies/{self.sid}").json()["study"]["plan"], plan)

    def test_a_retired_id_blocks_neither_an_edit_nor_the_approval(self):
        def retire(s):
            section(s["plan"], "mesh")["cites"]["n_wake"] = ["retired-entry"]
        self.store.update_study(self.sid, retire)
        body = copy.deepcopy(self.store.get_study(self.sid)["plan"])
        section(body, "numerics")["iterations"] = 1600
        self.assertNotIn("n_wake", section(self.patch_plan(body), "mesh")["cites"])
        # approval reads the stored plan as it is: put the retired id back and approve
        self.store.update_study(self.sid, retire)
        self.assertTrue(planning.validate_plan(self.store.get_study(self.sid)["plan"]))
        with mock.patch.object(runtime.CaseRunner, "start") as start:     # no pipeline threads in a unit test
            r = self.client.post(f"/api/studies/{self.sid}/approve", json={})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
