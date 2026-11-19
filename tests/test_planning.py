"""The planner's plan: its default citations, the rules a planner's plan is held to, the reasons
beside its values, the engineer's mark when the planner revises, the planner's tools and the
size of the plan blocks agents are shown. Stores are temporary: no Docker, no model.
"""
import copy
import io
import json
import unittest
from contextlib import redirect_stderr

import support
from support import MARK, citations, section

from wingstudio import runtime
from wingstudio.agents import spec as agent_spec
from wingstudio.agents import tools as agent_tools
from wingstudio.study import planning

FOUR = ("2412", "4412", "4415", "2415")


def given(study, **stages):
    """A planner's plan: the default one with the given stage sections laid over case 0."""
    plan = planning.default_plan(study)
    for stage, values in stages.items():
        plan["cases"][0]["stages"][stage].update(values)
    return plan


class PlanRules(unittest.TestCase):
    """planning.normalise_plan and validate_plan on what a planner writes."""

    def setUp(self):
        support.use_catalogue(self)
        self.study = support.make_study()

    def normalise(self, plan):
        return planning.normalise_plan(self.study, plan)

    def test_default_plan_gets_the_default_cites(self):
        plan, notes = self.normalise(planning.default_plan(self.study))
        self.assertEqual(notes, [])
        self.assertEqual(planning.validate_plan(plan), [])
        for pc in plan["cases"]:
            self.assertEqual(pc["cites"], {"alpha_deg": support.DEFAULTS["case.alpha_deg"]})
            for stage, sec in pc["stages"].items():
                self.assertEqual(sec["cites"], citations.default_section(stage), stage)
        self.assertEqual(section(plan, "post")["cites"], {})
        self.assertNotIn("turbulence_intensity", section(plan, "physics")["cites"])
        # a plan written back as it is gives the same plan
        self.assertEqual(self.normalise(copy.deepcopy(plan))[0], plan)

    def test_changed_value_without_cites_has_none_and_one_note(self):
        plan, notes = self.normalise(given(self.study, mesh={"n_normal": 110}))
        self.assertNotIn("n_normal", section(plan, "mesh")["cites"])
        self.assertEqual(notes, ["2412.mesh.n_normal = 110 differs from the default and cites no reference: "
                                 "it is shown without one"])
        self.assertEqual(section(plan, "mesh", 1)["cites"]["n_normal"], support.DEFAULTS["mesh.n_normal"])
        self.assertEqual(planning.validate_plan(plan), [])
        # a setting without a default citation gives no note
        plan, notes = self.normalise(given(self.study, solve={"max_extensions": 2}))
        self.assertEqual(notes, [])
        self.assertNotIn("max_extensions", section(plan, "solve")["cites"])

    def test_changed_value_with_a_valid_id_keeps_it(self):
        plan, notes = self.normalise(given(self.study, physics={
            "turbulence_model": "kOmegaSST", "cites": {"turbulence_model": ["menter-1994"]}}))
        self.assertEqual(section(plan, "physics")["cites"]["turbulence_model"], ["menter-1994"])
        self.assertEqual(planning.validate_plan(plan), [])
        # nut_ratio is still 4, but its default references are about the Spalart-Allmaras variable
        self.assertNotIn("nut_ratio", section(plan, "physics")["cites"])
        self.assertEqual(notes, ["2412.physics.nut_ratio = 4 keeps the default, whose references are for "
                                 "SpalartAllmaras: with kOmegaSST it is shown without one"])
        self.assertEqual(section(plan, "physics", 1)["cites"]["nut_ratio"], support.DEFAULTS["physics.nut_ratio"])

    def test_nut_ratio_with_another_model_is_cited_only_by_the_planner(self):
        plan, notes = self.normalise(given(self.study, physics={
            "turbulence_model": "kOmegaSST", "nut_ratio": 4.0,
            "cites": {"turbulence_model": ["menter-1994"], "nut_ratio": ["nasa-tmr-sst"]}}))
        self.assertEqual(notes, [])
        self.assertEqual(section(plan, "physics")["cites"]["nut_ratio"], ["nasa-tmr-sst"])
        # the other default citations of the section are not touched by the model
        self.assertEqual(section(plan, "physics")["cites"]["farfield_bc"], support.DEFAULTS["physics.farfield_bc"])

    def test_a_cites_key_may_carry_the_stage_as_get_defaults_shows_it(self):
        plan, notes = self.normalise(given(self.study, mesh={
            "n_normal": 110, "cites": {"mesh.n_normal": ["project-gate-limits"], "mesh.n_wake": []}}))
        self.assertEqual(notes, [])
        self.assertEqual(section(plan, "mesh")["cites"]["n_normal"], ["project-gate-limits"])
        self.assertNotIn("n_wake", section(plan, "mesh")["cites"])
        self.assertEqual(planning.validate_plan(plan), [])

    def test_a_cites_key_that_is_not_a_setting_is_dropped_with_a_note(self):
        # the stage of another section is not a spelling of this one
        for key in ("target_yplus", "physics.n_normal"):
            plan, notes = self.normalise(given(self.study, mesh={"cites": {key: ["project-gate-limits"]}}))
            self.assertEqual(notes, [f"2412.mesh.cites.{key} was dropped: it is not a setting of mesh"])
            self.assertNotIn(key, section(plan, "mesh")["cites"])
        self.assertNotIn("target_yplus", section(plan, "mesh"))

    def test_empty_list_on_a_default_value_means_no_reference(self):
        plan, notes = self.normalise(given(self.study, mesh={"cites": {"farfield_chords": []}}))
        self.assertEqual(notes, [])
        self.assertNotIn("farfield_chords", section(plan, "mesh")["cites"])
        self.assertIn("wake_chords", section(plan, "mesh")["cites"])

    def test_one_per_cent_rule_is_for_float_settings_only(self):
        default = section(planning.default_plan(self.study), "mesh")
        plan, notes = self.normalise(given(self.study, mesh={
            "first_cell_height": float(f"{default['first_cell_height'] * 1.003:.4g}"), "n_wake": 51}))
        mesh = section(plan, "mesh")
        # still the default: its citation and its sentence stay
        self.assertEqual(mesh["cites"]["first_cell_height"], support.DEFAULTS["mesh.first_cell_height"])
        self.assertEqual(mesh["reasons"]["first_cell_height"], default["reasons"]["first_cell_height"])
        self.assertNotIn("n_wake", mesh["cites"])
        self.assertNotIn("n_wake", mesh["reasons"])                 # "50 cells along the wake ..."
        self.assertEqual(len(notes), 1)
        self.assertIn("2412.mesh.n_wake = 51 differs", notes[0])

    def test_unknown_id_is_refused_with_the_ids_of_the_stage(self):
        plan, _ = self.normalise(given(self.study, physics={
            "turbulence_model": "kOmegaSST", "cites": {"turbulence_model": ["menter-1993"]}}))
        self.assertEqual(planning.validate_plan(plan), [support.unknown_id_error(
            "NACA 2412: physics.cites.turbulence_model", "menter-1993", "physics", "setting")])
        self.assertEqual(planning.validate_plan(plan, cites=False), [])

    def test_count_shape_and_map_errors(self):
        four = ["project-gate-limits", "project-reference-4412", "project-yplus-40", "project-farfield-30"]
        plan, _ = self.normalise(given(self.study, mesh={"cites": {"n_normal": four, "n_wake": 7}}))
        self.assertEqual(planning.validate_plan(plan), [
            "NACA 2412: mesh.cites.n_normal: 4 references cited, at most 3: keep the ones that support the value "
            "most directly",
            'NACA 2412: mesh.cites.n_wake must be a list of reference ids, for example ["spalart-allmaras-1992"]'])
        plan, _ = self.normalise(given(self.study, physics={"cites": ["menter-1994"]}))
        self.assertEqual(planning.validate_plan(plan),
                         ["NACA 2412: physics.cites must be an object of setting -> list of reference ids"])
        # a single string is one id
        plan, _ = self.normalise(given(self.study, physics={"cites": {"turbulence_model": "spalart-allmaras-1992"}}))
        self.assertEqual(section(plan, "physics")["cites"]["turbulence_model"], ["spalart-allmaras-1992"])
        self.assertEqual(planning.validate_plan(plan), [])

    def test_case_level_cites_in_a_planner_plan_are_ignored(self):
        raw = planning.default_plan(self.study)
        raw["cases"][0]["cites"] = {"alpha_deg": ["made-up"]}
        plan, _ = self.normalise(raw)
        self.assertEqual(plan["cases"][0]["cites"], {"alpha_deg": support.DEFAULTS["case.alpha_deg"]})

    def test_catalogue_that_fails_to_load_gives_plans_without_cites(self):
        support.use_catalogue(self, missing=True)
        with redirect_stderr(io.StringIO()):
            plan, notes = self.normalise(planning.default_plan(self.study))
            self.assertEqual(notes, [])
            self.assertEqual(planning.validate_plan(plan), [])
            self.assertTrue(all(sec["cites"] == {} for pc in plan["cases"] for sec in pc["stages"].values()))
            plan, _ = self.normalise(given(self.study, physics={"cites": {"nut_ratio": ["nasa-tmr-sst"]}}))
            self.assertIn("cannot be checked", planning.validate_plan(plan)[0])


class PlannerTools(unittest.TestCase):
    """get_defaults and write_plan as the planner agent calls them."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)

    def planner(self, study=None, scripted=True):
        study = support.store_study(self.eng, study, with_plan=False)
        rt = runtime.StudyRuntime(self.eng, study["id"], "planner")
        if scripted:
            rt.agent_name = "scripted"
        return rt

    def stored(self, rt):
        return self.eng.store.get_study(rt.sid)["plan"]

    def test_get_defaults_for_four_airfoils_fits_the_tool_limit(self):
        support.use_real_catalogue(self)
        result = self.planner(support.make_study(FOUR)).get_defaults()
        text = agent_tools.result_text(result)
        self.assertLessEqual(len(text), agent_tools.MAX_TOOL_CHARS)
        self.assertNotIn("[truncated", text)
        self.assertEqual(json.loads(text)["default_cites"], citations.load()["defaults"])
        self.assertEqual(len(result["plan"]["cases"]), 4)
        self.assertEqual([r["id"] for r in result["references"]], citations.load()["order"])
        self.assertEqual(set(result["references"][0]), {"id", "kind", "title", "says", "cite_for"})
        for pc in result["plan"]["cases"]:                    # the default citations come once, not per case
            self.assertNotIn("cites", pc)
            self.assertFalse(any("cites" in s for s in pc["stages"].values()))

    def test_scripted_write_plan_stores_the_default_cites(self):
        rt = self.planner()
        result = rt.write_plan(rt.get_defaults()["plan"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["notes"], [])
        self.assertEqual(section(self.stored(rt), "numerics")["cites"], citations.default_section("numerics"))
        self.assertEqual(self.stored(rt)["cases"][1]["cites"], {"alpha_deg": support.DEFAULTS["case.alpha_deg"]})

    def test_write_plan_refuses_an_unknown_id_and_stores_nothing(self):
        rt = self.planner()
        plan = rt.get_defaults()["plan"]
        section(plan, "numerics").update(relax_p=0.2, cites={"relax_p": ["patankar-1980"]})
        result = rt.write_plan(plan)
        self.assertFalse(result["ok"])
        self.assertIn("NACA 2412: numerics.cites.relax_p: unknown reference id 'patankar-1980'.", result["error"])
        self.assertIsNone(self.stored(rt))
        self.assertIsNone(rt.outcome)

    def test_a_default_reason_carried_along_is_not_a_reason(self):
        # get_defaults carries every default reason, and a planner changes values without
        # touching the sentences beside them, or sends a section without any
        rt = self.planner(support.make_study(model=support.HAIKU), scripted=False)
        default = rt.get_defaults()["plan"]
        plan = copy.deepcopy(default)
        plan["summary"] = "Two sections at the design lift, with a gentler start of the momentum equation."
        section(plan, "numerics").update(relax_U=0.5, relax_p=0.2)
        section(plan, "numerics")["reasons"]["relax_p"] = "0.2 for a stiff start."
        section(plan, "mesh")["wake_chords"] = 20
        section(plan, "mesh").pop("reasons")
        section(plan, "physics")["turbulence_model"] = "kOmegaSST"
        self.assertTrue(rt.write_plan(plan)["ok"])
        stored = self.stored(rt)
        numerics, mesh = section(stored, "numerics")["reasons"], section(stored, "mesh")["reasons"]
        self.assertNotIn("relax_U", numerics)                                  # "0.7 for momentum ..."
        self.assertEqual(numerics["relax_p"], "0.2 for a stiff start.")
        self.assertNotIn("wake_chords", mesh)                                  # "outlet 30 chords ..."
        self.assertNotIn("turbulence_model", section(stored, "physics")["reasons"])   # "Spalart-Allmaras: ..."
        # values left alone keep their sentence, in this case and in the other
        self.assertEqual(numerics["relax_turbulence"], section(default, "numerics")["reasons"]["relax_turbulence"])
        self.assertEqual(mesh["farfield_chords"], section(default, "mesh")["reasons"]["farfield_chords"])
        self.assertEqual(section(stored, "mesh", 1)["reasons"], section(default, "mesh", 1)["reasons"])

    def test_a_first_plan_has_no_engineer_values(self):
        rt = self.planner()
        plan = rt.get_defaults()["plan"]
        section(plan, "mesh")["reasons"]["n_normal"] = f"{MARK} (was 80)."
        self.assertTrue(rt.write_plan(plan)["ok"])
        mesh = section(self.stored(rt), "mesh")
        self.assertFalse(mesh["reasons"]["n_normal"].startswith(MARK))
        self.assertEqual(mesh["cites"]["n_normal"], support.DEFAULTS["mesh.n_normal"])

    def test_estimate_first_cell_names_its_references(self):
        result = agent_tools.execute_tool(self.planner().context(), "estimate_first_cell", {"target_yplus": 60})
        self.assertEqual(result["cites"], support.DEFAULTS["mesh.first_cell_height"])

    def test_tool_schemas(self):
        item = agent_tools.TOOLS["complete_stage"][2]["function"]["parameters"]["properties"]["decisions"]["items"]
        self.assertEqual(item["required"], ["param", "value", "reason"])
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["cites"]["maxItems"], 3)
        self.assertNotIn("enum", item["properties"]["cites"]["items"])
        self.assertIn("'cites' (setting -> list of reference ids", agent_tools.TOOLS["write_plan"][2]["function"]["description"])
        self.assertIn("default_cites", agent_tools.TOOLS["get_defaults"][2]["function"]["description"])


class EngineerMarkAndThePlanner(unittest.TestCase):
    """A planner that rewrites the 'Current plan (to be revised)' block copies its reasons."""

    def setUp(self):
        support.use_catalogue(self)
        self.eng = support.make_engine(self)
        study = support.make_study(model=support.HAIKU)
        stored, _ = support.default_plan(study)
        section(stored, "mesh")["n_normal"] = 100
        section(stored, "mesh")["reasons"]["n_normal"] = f"{MARK} (was 90)."
        section(stored, "mesh")["cites"].pop("n_normal")
        study["plan"] = stored
        self.study = support.store_study(self.eng, study)
        self.sid = self.study["id"]
        self.rt = runtime.StudyRuntime(self.eng, self.sid, "planner")

    def revised(self, change):
        body = copy.deepcopy(self.study["plan"])
        body["summary"] = "The plan as the planner writes it again after the engineer's comment on the mesh."
        change(body)
        result = self.rt.write_plan(body)
        self.assertTrue(result["ok"], result)
        return section(self.eng.store.get_study(self.sid)["plan"], "mesh"), result["notes"]

    def runner(self):
        runner = support.make_runner(self.eng, self.eng.store.get_study(self.sid))
        runner.stage = "mesh"
        return runner

    def test_the_mark_stays_on_the_value_the_engineer_set(self):
        # the value is still the engineer's, so the mark, the lock and the engineer block must
        # not depend on what the planner wrote beside it
        def own_reason(body):
            section(body, "mesh")["reasons"]["n_normal"] = "100 cells for a smoother wall-normal growth."
            section(body, "mesh")["cites"] = {"n_normal": ["project-reference-4412"]}
        for change in (lambda body: section(body, "mesh").update(n_wake=60),
                       lambda body: section(body, "mesh").pop("reasons"), own_reason):
            mesh, notes = self.revised(change)
            self.assertEqual((mesh["n_normal"], mesh["reasons"]["n_normal"]), (100, f"{MARK} (was 90)."))
            self.assertNotIn("n_normal", mesh["cites"])
            self.assertFalse([n for n in notes if "n_normal" in n], notes)
        runner = self.runner()
        self.assertEqual(list(agent_tools.engineer_locked(runner.context(), "mesh", {"n_normal": 90})), ["n_normal"])
        self.assertIn("## Values the engineer set by hand in this section", runner.stage_context_text("mesh", 1))

    def test_a_copied_mark_on_another_value_is_removed(self):
        mesh, notes = self.revised(lambda body: section(body, "mesh").update(n_normal=110))
        self.assertEqual(mesh["n_normal"], 110)
        self.assertNotIn("n_normal", mesh["reasons"])
        self.assertIn("2412.mesh.n_normal: the reason 'Set by the engineer' was removed, because the engineer did "
                      "not set 110; give your own reason for a value you choose", notes)
        # it is an ordinary planner value now: uncited with the usual note, and not locked
        self.assertIn("2412.mesh.n_normal = 110 differs from the default and cites no reference: it is shown "
                      "without one", notes)
        self.assertEqual(agent_tools.engineer_locked(self.runner().context(), "mesh", {"n_normal": 90}), {})

    def test_a_copied_mark_on_the_default_gets_the_default_reason_and_citation(self):
        mesh, notes = self.revised(lambda body: section(body, "mesh").update(n_normal=90))
        self.assertTrue(mesh["reasons"]["n_normal"].endswith("about a minute per run."))
        self.assertEqual(mesh["cites"]["n_normal"], support.DEFAULTS["mesh.n_normal"])
        self.assertEqual(len([n for n in notes if "was removed" in n]), 1)

    def test_a_mark_the_planner_invents_is_removed(self):
        def change(body):
            section(body, "mesh")["n_wake"] = 60
            section(body, "mesh")["reasons"]["n_wake"] = f"{MARK} (was 50)."
            section(body, "mesh")["cites"] = {"n_wake": ["project-reference-4412"]}
        mesh, notes = self.revised(change)
        self.assertNotIn("n_wake", mesh["reasons"])
        self.assertEqual(mesh["cites"]["n_wake"], ["project-reference-4412"])
        self.assertEqual(len([n for n in notes if "was removed" in n]), 1)


class TextLimits(unittest.TestCase):
    def test_the_plan_blocks_hold_four_cases(self):
        support.use_catalogue(self)
        plan, _ = support.default_plan(support.make_study(FOUR))
        self.assertEqual(json.loads(agent_spec.compact(plan, 40000)), plan)   # the planner's "Current plan (to be revised)"
        shown = {"version": 1, "cases": agent_spec.uncited(plan["cases"])}     # the steering agent's "Plan"
        block = agent_spec.compact(shown, agent_spec.STEERING_PLAN_CHARS)
        self.assertNotIn("truncated", block)
        self.assertNotIn("cites", block)
        self.assertEqual([pc["naca"] for pc in json.loads(block)["cases"]], list(FOUR))
        self.assertLess(len(json.dumps(shown)), agent_spec.STEERING_PLAN_CHARS - 2000)       # room for longer reasons
        # every setting and its reason is there; only what steering never edits is left out
        for stage, sec in json.loads(block)["cases"][3]["stages"].items():
            full = plan["cases"][3]["stages"][stage]
            self.assertEqual(sec, {k: v for k, v in full.items() if k not in ("cites", "checks")})
        self.assertIn("checks", section(plan, "mesh"))                       # a copy was stripped, not the plan
        self.assertIn("cites", plan["cases"][0])

    def test_text_blocks_drop_the_indentation_before_they_cut(self):
        big = {"rows": [{"n": i, "text": "x" * 40} for i in range(580)]}
        self.assertGreater(len(json.dumps(big, indent=1)), agent_tools.MAX_TOOL_CHARS)
        self.assertLess(len(json.dumps(big)), agent_tools.MAX_TOOL_CHARS)
        self.assertEqual(json.loads(agent_tools.result_text(big)), big)
        self.assertIn("[truncated", agent_tools.result_text({"rows": big["rows"] * 2}))
        self.assertEqual(json.loads(agent_spec.compact(big, 40000)), big)
        self.assertTrue(agent_spec.compact(big, 1000).endswith("... [truncated]"))


if __name__ == "__main__":
    unittest.main()
