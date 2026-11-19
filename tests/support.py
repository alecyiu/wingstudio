"""Shared fixtures for the studio unit tests.

Nothing here needs Docker, a model or the network, and nothing is written under
studio_data/ or cases/: the store lives in a temporary directory and the catalogue the
plan rules are tested against is a temporary file with the ids of the real one.
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from foamtools.common import ROOT, read_json  # noqa: F401 (read_json: used by the tests through support)
from wingstudio import engine, runtime, server
from wingstudio.refs import citations
from wingstudio.store import Store, now_iso
from wingstudio.study import mission, planning

STUDIO = os.path.join(ROOT, "wingstudio")
REAL_CATALOGUE = citations.PATH
BASE = "http://127.0.0.1:8800"
HAIKU, SONNET = "anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"
MARK = planning.ENGINEER_MARK
TYPED = 0.012                      # the tutorial's bad first cell height: y+ 372 against 20 to 200
YPLUS_CHECK = {"name": "predicted y+ of first cell centre", "value": 371.59, "limit": "20 to 200", "ok": False}
GATE_PASSED = {"passed": True, "checks": [{"name": "stub", "value": "ok", "limit": "ok", "ok": True}]}
GATE_FAILED = {"passed": False, "checks": [YPLUS_CHECK]}
REJECTED = "predicted y+ of first cell centre: 371.59 (limit 20 to 200)"
GATE_FAILED_TEXT = "gate failed: " + REJECTED

# id -> (kind, settings it is meant for); the ids and settings of wingstudio/refs/catalogue.json
# (test_refs.RealFiles fails when the two drift apart: sizes measured on this
# catalogue are only close to the real ones while it lists the same settings)
_ENTRIES = {
    "spalart-allmaras-1992": ("paper", ["physics.turbulence_model"]),
    "menter-1994": ("paper", ["physics.turbulence_model"]),
    "nasa-tmr-spalart-allmaras": ("documentation", ["physics.nut_ratio"]),
    "nasa-tmr-sst": ("documentation", ["physics.turbulence_intensity", "physics.nut_ratio"]),
    "nasa-tmr-naca0012": ("documentation", ["mesh.farfield_chords", "physics.farfield_bc", "mesh.n_leading",
                                            "mesh.n_aft", "mesh.n_wake", "physics.nut_ratio",
                                            "physics.turbulence_intensity"]),
    "thomas-salas-1986": ("paper", ["mesh.farfield_chords", "physics.farfield_bc"]),
    "spalding-1961": ("paper", ["physics.wall_treatment", "mesh.first_cell_height"]),
    "white-fluid-mechanics": ("book", ["mesh.first_cell_height"]),
    "drela-1989-xfoil": ("paper", ["case.alpha_deg", "validate.transition", "validate.mach"]),
    "xfoil-primer": ("documentation", ["validate.transition", "validate.mach", "validate.flight_mach",
                                       "case.alpha_deg", "geometry.naca"]),
    "naca-tr-460": ("paper", ["geometry.naca"]),
    "abbott-vondoenhoff-1959": ("book", ["geometry.naca"]),
    "ferziger-peric-2020": ("book", ["numerics.relax_p", "numerics.relax_U"]),
    "openfoam-linearupwind": ("documentation", ["numerics.convection_scheme"]),
    "openfoam-simple-relaxation": ("documentation", ["numerics.relax_p", "numerics.relax_U",
                                                     "numerics.relax_turbulence"]),
    "openfoam-simple-algorithm": ("documentation", ["numerics.consistent", "numerics.relax_p", "numerics.relax_U"]),
    "openfoam-wall-functions": ("documentation", ["mesh.first_cell_height", "physics.wall_treatment"]),
    "openfoam-freestream-bc": ("documentation", ["physics.farfield_bc"]),
    "openfoam-checkmesh-thresholds": ("documentation", ["mesh.n_normal", "numerics.n_non_orthogonal_correctors"]),
    "openfoam-naca0012-validation": ("documentation", [
        "physics.turbulence_model", "physics.nut_ratio", "physics.wall_treatment", "physics.farfield_bc",
        "mesh.farfield_chords", "mesh.first_cell_height", "numerics.convection_scheme"]),
    "project-reference-4412": ("project-measurement", [
        "mesh.n_normal", "mesh.n_leading", "mesh.n_aft", "mesh.n_wake", "physics.turbulence_model",
        "numerics.iterations", "numerics.relax_p", "numerics.relax_U", "numerics.relax_turbulence",
        "solve.extend_iterations"]),
    "project-farfield-30": ("project-measurement", ["mesh.farfield_chords", "mesh.wake_chords",
                                                    "physics.farfield_bc"]),
    "project-yplus-40": ("project-measurement", ["mesh.first_cell_height", "physics.wall_treatment"]),
    "project-residual-1e-6": ("project-measurement", ["numerics.residual_p", "numerics.residual_U",
                                                      "numerics.residual_turbulence"]),
    "project-gate-limits": ("project-rule", ["mesh.n_normal", "numerics.iterations",
                                             "numerics.n_non_orthogonal_correctors", "solve.extend_iterations"]),
    "project-validation-bands": ("project-rule", ["validate.transition", "validate.mach"]),
    "project-xfoil-like-for-like": ("project-rule", ["case.alpha_deg", "geometry.naca", "validate.transition",
                                                     "validate.mach", "validate.flight_mach"]),
}

DEFAULTS = {
    "case.alpha_deg": ["project-xfoil-like-for-like", "drela-1989-xfoil"],
    "geometry.naca": ["naca-tr-460", "abbott-vondoenhoff-1959", "project-xfoil-like-for-like"],
    "mesh.farfield_chords": ["project-farfield-30", "nasa-tmr-naca0012", "thomas-salas-1986"],
    "mesh.wake_chords": ["project-farfield-30"],
    "mesh.n_normal": ["project-reference-4412", "project-gate-limits"],
    "mesh.n_leading": ["project-reference-4412"],
    "mesh.n_aft": ["project-reference-4412"],
    "mesh.n_wake": ["project-reference-4412"],
    "mesh.first_cell_height": ["project-yplus-40", "white-fluid-mechanics", "openfoam-wall-functions"],
    "physics.turbulence_model": ["spalart-allmaras-1992", "openfoam-naca0012-validation", "project-reference-4412"],
    "physics.wall_treatment": ["spalding-1961", "openfoam-wall-functions", "project-yplus-40"],
    "physics.nut_ratio": ["nasa-tmr-spalart-allmaras", "openfoam-naca0012-validation"],
    "physics.farfield_bc": ["project-farfield-30", "openfoam-freestream-bc", "thomas-salas-1986"],
    "numerics.iterations": ["project-reference-4412", "project-gate-limits"],
    "numerics.convection_scheme": ["openfoam-linearupwind"],
    "numerics.relax_p": ["ferziger-peric-2020", "openfoam-simple-relaxation", "project-reference-4412"],
    "numerics.relax_U": ["ferziger-peric-2020", "openfoam-simple-relaxation", "project-reference-4412"],
    "numerics.relax_turbulence": ["openfoam-simple-relaxation", "project-reference-4412"],
    "numerics.residual_p": ["project-residual-1e-6"],
    "numerics.residual_U": ["project-residual-1e-6"],
    "numerics.residual_turbulence": ["project-residual-1e-6"],
    "numerics.n_non_orthogonal_correctors": ["project-gate-limits", "openfoam-checkmesh-thresholds"],
    "numerics.consistent": ["openfoam-simple-algorithm"],
    "solve.extend_iterations": ["project-gate-limits", "project-reference-4412"],
    "validate.transition": ["project-xfoil-like-for-like", "xfoil-primer", "project-validation-bands"],
    "validate.mach": ["project-xfoil-like-for-like", "xfoil-primer"],
}

# plan settings that are shown with their reason only (the spec's coverage table)
UNCITED = {"physics.turbulence_intensity", "numerics.write_interval", "solve.max_extensions",
           "post.field", "post.zoom", "validate.flight_mach"}


def unknown_id_error(where, ref_id, stage, item):
    """The refusal of an id that is not in the catalogue; `item` is "setting" or "decision"."""
    return (f"{where}: unknown reference id '{ref_id}'. Citations are ids from the studio's reference catalogue, "
            f"never titles or URLs. Ids for {stage}: {', '.join(citations.ids_for(stage))}. Use one of them, or "
            f"leave cites out for this {item}.")


def section(plan, stage, case=0):
    """One stage section of a plan."""
    return plan["cases"][case]["stages"][stage]


def patch(test, target, name, *args, **kwargs):
    """mock.patch.object for the rest of one test; returns what was put in place."""
    patcher = mock.patch.object(target, name, *args, **kwargs)
    started = patcher.start()
    test.addCleanup(patcher.stop)
    return started


def engineer_edit(study, stored, change):
    """What PATCH /plan does with a body: normalise on the engineer path, then mark the edits.
    Returns the new plan and the settings marked as the engineer's."""
    body = copy.deepcopy(stored)
    change(body)
    plan, _ = planning.normalise_plan(study, body, keep_alpha=True, cites_from=stored)
    return plan, planning.mark_engineer_edits(stored, plan)


def with_typed_height(study):
    """The study with its default plan and, as version 2, the engineer's 0.012 first cell height
    on NACA 2412. Returns the planned height."""
    planned, _ = default_plan(study)
    study["plan"] = dict(engineer_edit(study, planned, lambda body: section(body, "mesh").update(
        first_cell_height=TYPED))[0], version=2)
    return section(planned, "mesh")["first_cell_height"]


def catalogue():
    """A catalogue with the real ids, kinds, settings and defaults, and placeholder wording."""
    entries = []
    for ref_id, (kind, settings) in _ENTRIES.items():
        public = not kind.startswith("project-") and kind != "book"
        entries.append({"id": ref_id, "title": f"Title of {ref_id}", "kind": kind, "source": f"Source of {ref_id}",
                        "says": f"What {ref_id} establishes, in one or two sentences of about the usual length, "
                                "so that sizes measured in the tests are close to the real file.",
                        "settings": list(settings), "locator": "", "checked": "2026-11-18",
                        "url": f"https://example.org/{ref_id}" if public else "", "url_verified": public})
    return {"version": 1, "checked": "2026-11-18", "entries": entries,
            "defaults": {k: list(v) for k, v in DEFAULTS.items()}}


def use_catalogue(test, content=None, missing=False):
    """Point citations at a temporary catalogue for one test (restored afterwards).
    `content` may be a dict or raw text; `missing` leaves no file at all."""
    folder = tempfile.mkdtemp(prefix="studio-refs-")
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    path = os.path.join(folder, "catalogue.json")
    if not missing:
        content = catalogue() if content is None else content
        with open(path, "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
    previous = citations.PATH
    citations.PATH = path
    test.addCleanup(setattr, citations, "PATH", previous)
    return path


def use_real_catalogue(test):
    previous = citations.PATH
    citations.PATH = REAL_CATALOGUE
    test.addCleanup(setattr, citations, "PATH", previous)


def make_study(codes=("2412", "4412"), sid="tstudy", model="scripted"):
    """A study record as the engine holds it after the selection (Re 1.5e6, the tutorial's mission)."""
    req = mission.normalise_requirements({"mach": 0.09, "density": 1.112, "reynolds": 1.5e6, "design_cl": 0.8,
                                         "objective": "max_ld"}, "")
    now = now_iso()
    return {"id": sid, "project_id": "naca-wing", "title": "Test study", "created_at": now, "updated_at": now,
            "model": model, "mode": "auto", "status": "awaiting_approval", "prompt": "",
            "requirements": req, "conditions": mission.derive_conditions(req),
            "candidates": [{"naca": c, "selected": True, "rationale": "", "xfoil": {"alpha_design": 2.5 + i,
                                                                                    "converged": True}}
                           for i, c in enumerate(codes)],
            "plan": None, "case_ids": [], "report": None, "usage": runtime.zero_usage()}


def default_plan(study):
    """The plan the planner path stores when nothing is changed."""
    plan, notes = planning.normalise_plan(study, planning.default_plan(study))
    return plan, notes


def make_engine(test, completion=None):
    """An engine on a store in a temporary directory, with no viewer. `completion` stands in
    for the model."""
    folder = tempfile.mkdtemp(prefix="studio-data-")
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return engine.Engine(Store(folder), viewer_factory=None, completion=completion)


def store_study(eng, study=None, with_plan=True):
    """Put a study (by default with its default plan, version 1) into the engine's store."""
    study = study or make_study()
    if with_plan and not study.get("plan"):
        study["plan"] = default_plan(study)[0]
    eng.store.create_study(study)
    return eng.store.get_study(study["id"])


def make_runner(eng, study, naca=None, case_dir=None):
    """A case record and its CaseRunner, not started: no thread and no case directory.
    `case_dir` (absolute) replaces cases/st_... for tests that need files in the case."""
    naca = naca or study["plan"]["cases"][0]["naca"]
    pc = next(c for c in study["plan"]["cases"] if c["naca"] == naca)
    cid = f"{study['id']}-{naca}"
    eng.store.create_case({
        "id": cid, "study_id": study["id"], "naca": naca, "alpha_deg": float(pc["alpha_deg"]),
        "dir": case_dir or f"cases/st_{study['id']}_{naca}", "status": "running", "current_stage": None,
        "stages": [runtime.new_stage(k) for k in planning.STAGE_KEYS],
        "results": None, "reference": None, "deltas": None, "verdict": None, "viewer": None,
        "usage": runtime.zero_usage(True),
        "counters": {"rework_loops": 0, "gate_failures": 0, "human_interventions": 0, "help_interventions": 0},
        "created_at": now_iso(), "started_at": now_iso(), "ended_at": None})
    eng.store.update_study(study["id"], lambda s: s.__setitem__("case_ids", s.get("case_ids", []) + [cid]))
    eng.set_study_status(study["id"], "running")
    runner = runtime.CaseRunner(eng, study["id"], cid)
    eng.runners[cid] = runner
    return runner


def stage(eng, sid, cid, key):
    """The stored record of one stage of a case."""
    return runtime.find_stage(eng.store.get_case(sid, cid), key)


def events(eng, sid, type=None):
    return [e for e in eng.store.events_after(sid, 0) if type is None or e["type"] == type]


class ServerCase(unittest.TestCase):
    """The app on a temporary store, through Starlette's TestClient: no port is opened and the
    server on 8800 is not involved."""
    completion = None

    def setUp(self):
        use_catalogue(self)
        self.data = tempfile.mkdtemp(prefix="studio-data-")
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)
        self.app = server.create_app(data_dir=self.data, viewer_factory=None, completion=self.completion)
        self.client = TestClient(self.app, base_url=BASE)
        self.store, self.engine = self.app.state.store, self.app.state.engine
