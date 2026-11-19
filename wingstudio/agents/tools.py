"""The tools given to the stage and study agents.

Everything here is deterministic: a tool takes explicit arguments, drives cfd.case,
sim, Docker, XFoil or the viewer, and returns a JSON object with `ok`, a one-line `summary`
for the page, and its data. Errors are returned, never raised. Tools that change the study
itself (complete_stage, request_rework, propose_candidates, write_plan, ...) are forwarded
to `ctx.runtime`, which the engine provides."""
import json
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from foamtools import docker
from foamtools.common import ROOT, is_finite, read_json, read_text, round_sig, run_sim, time_dirs, write_json

from .. import IMAGE, gates
from ..cfd import case as ac
from ..cfd import solver, xfoil
from ..refs import citations
from ..study import planning, reporting
from .spec import rounded_conditions

HOST_WRITE_SETTLE_S = 1.5        # virtiofs can serve a stale size right after a host rewrite
MAX_TOOL_CHARS = 40000           # get_results for two cases is about 17,000 characters


# ------------------------------------------------------------------ context --

class NullRuntime:
    """What a tool sees when no engine is attached (unit tests, scripts)."""
    viewer = None

    def message_pending(self):
        return False

    def interrupted(self):
        return False

    def __getattr__(self, name):
        def missing(*a, **k):
            return {"ok": False, "error": f"{name} is not available outside a running study"}
        return missing


class ToolContext:
    """Everything a tool needs to act on one study (and usually one case)."""

    def __init__(self, store, study_id, case_id=None, stage=None, agent=None,
                 runtime=None, span_id=None):
        self.store = store
        self.study_id = study_id
        self.case_id = case_id
        self.stage = stage
        self.agent = agent
        self.runtime = runtime or NullRuntime()
        self.span_id = span_id

    # -- data --
    @property
    def study(self):
        return self.store.get_study(self.study_id)

    @property
    def case(self):
        return self.store.get_case(self.study_id, self.case_id) if self.case_id else None

    @property
    def case_dir(self):
        case = self.case
        if not case:
            raise ValueError("this tool needs a case")
        return os.path.join(ROOT, case["dir"])

    @property
    def case_name(self):
        return os.path.basename(self.case_dir)

    def conditions(self):
        return self.study.get("conditions") or {}

    def requirements(self):
        return self.study.get("requirements") or {}

    def plan_case(self):
        """This case's entry in the plan ({} if there is none)."""
        case = self.case
        for pc in ((self.study.get("plan") or {}).get("cases") or []):
            if case and str(pc.get("naca")) == str(case["naca"]):
                return pc
        return {}

    def plan_stage(self, stage):
        return dict((self.plan_case().get("stages") or {}).get(stage) or {})

    # -- effects --
    def emit(self, type, data=None, full=None, span_id=None):
        return self.store.emit(self.study_id, type, data or {}, case_id=self.case_id,
                               stage=self.stage, agent=self.agent,
                               span_id=span_id, parent_span_id=self.span_id, full=full)

    def update_stage(self, mutate):
        def apply(case):
            for st in case["stages"]:
                if st["key"] == self.stage:
                    mutate(st)
        if self.case_id and self.stage:
            self.store.update_case(self.study_id, self.case_id, apply)

    def merge_metrics(self, **values):
        self.update_stage(lambda st: st.setdefault("metrics", {}).update(values))

    def set_headline(self, text):
        self.update_stage(lambda st: st.__setitem__("headline", text))

    def merge_results(self, **values):
        def apply(case):
            results = case.get("results") or {}
            results.update(values)
            case["results"] = results
        self.store.update_case(self.study_id, self.case_id, apply)

    def add_artifact(self, kind, label, path):
        url = file_url(path)
        artifact = {"kind": kind, "label": label, "url": url}

        def apply(st):
            arts = [a for a in st.setdefault("artifacts", []) if a.get("url") != url]
            arts.append(artifact)
            st["artifacts"] = arts
        self.update_stage(apply)
        self.emit("artifact.created", artifact)
        return artifact


def file_url(path):
    return "/files/" + os.path.relpath(os.path.abspath(path), ROOT).replace(os.sep, "/")


# ---------------------------------------------------------- host / container --

_LAST_WRITE = {}
_LAST_WRITE_LOCK = threading.Lock()


def mark_write(case_dir):
    """Record that the host just rewrote files a container may already have read."""
    with _LAST_WRITE_LOCK:
        _LAST_WRITE[os.path.abspath(case_dir)] = time.time()


def settle(case_dir):
    """Wait until HOST_WRITE_SETTLE_S has passed since the last host write."""
    with _LAST_WRITE_LOCK:
        last = _LAST_WRITE.get(os.path.abspath(case_dir), 0.0)
    wait = HOST_WRITE_SETTLE_S - (time.time() - last)
    if wait > 0:
        time.sleep(wait)


def _sim(case_dir, *args, timeout=None):
    """Call sim (after the settle wait) and return its JSON as a dict."""
    settle(case_dir)
    return run_sim(*args, timeout=timeout, image=IMAGE)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------- common tools --

def tool_get_context(ctx):
    study, case = ctx.study, ctx.case
    out = {"ok": True, "requirements": study.get("requirements"),
           "conditions": rounded_conditions(study.get("conditions") or {}),
           "study": {"id": study["id"], "mode": study.get("mode"), "prompt": study.get("prompt")}}
    if case:
        upstream = []
        for st in case["stages"]:
            if st["key"] == ctx.stage:
                break
            if st.get("status") == "passed":
                upstream.append({"stage": st["key"], "headline": st.get("headline"),
                                 "summary": st.get("summary"), "decisions": planning.without_cites(st.get("decisions")),
                                 "metrics": st.get("metrics")})
        out.update({
            "case": {"id": case["id"], "naca": case["naca"], "alpha_deg": case["alpha_deg"],
                     "dir": case["dir"], "counters": case.get("counters")},
            "stage": ctx.stage,
            "plan_for_this_stage": ctx.plan_stage(ctx.stage) if ctx.stage else None,
            "plan_for_case": ctx.plan_case().get("stages"),
            "upstream": upstream,
            "references": citations.for_stage(ctx.stage) if ctx.stage else [],
            "current_settings": {
                "mesh": (read_json(os.path.join(ctx.case_dir, "mesh_plan.json")) or {}).get("params"),
                "physics": read_json(os.path.join(ctx.case_dir, "physics.json")),
                "numerics": read_json(os.path.join(ctx.case_dir, "numerics.json")),
            },
            "results": case.get("results"), "reference": case.get("reference"),
            "deltas": case.get("deltas"), "verdict": case.get("verdict"),
        })
        out["summary"] = f"context for NACA {case['naca']} at {case['alpha_deg']:g} deg, stage {ctx.stage}"
    else:
        out["candidates"] = [{k: v for k, v in c.items() if k != "xfoil"} |
                             {"xfoil": {k: v for k, v in (c.get("xfoil") or {}).items() if k != "polar"}}
                             for c in study.get("candidates") or []]
        out["plan"] = study.get("plan")
        out["summary"] = "study context"
    return out


def tool_note(ctx, text=""):
    ctx.emit("agent.message", {"text": str(text)})
    return {"ok": True, "summary": "noted"}


def tool_complete_stage(ctx, summary="", decisions=None):
    return ctx.runtime.complete_stage(summary, decisions or [])


def tool_request_rework(ctx, stage=None, reason=""):
    return ctx.runtime.request_rework(stage, reason)


# ----------------------------------------------------------------- geometry --

def tool_generate_geometry(ctx, naca=None):
    case = ctx.case
    code = ac.naca4(naca if naca not in (None, "") else case["naca"])[0]
    if code != ac.naca4(case["naca"])[0]:
        return {"ok": False, "error": f"this case is NACA {case['naca']}; it cannot be changed to {code}"}
    os.makedirs(ctx.case_dir, exist_ok=True)
    info = ac.write_geometry(ctx.case_dir, code)
    mark_write(ctx.case_dir)
    ctx.add_artifact("data", f"NACA {code} profile coordinates", os.path.join(ctx.case_dir, "geometry.json"))
    ctx.set_headline(f"NACA {code} · {info['thickness'] * 100:.0f}% thick")
    return {"ok": True, **info, "obj": f"constant/geometry/NACA{code}.obj",
            "summary": info["description"] + ", closed trailing edge, OBJ written"}


def tool_inspect_geometry(ctx):
    case = ctx.case
    facts = gates.geometry_facts(ctx.case_dir, case["naca"])
    if facts["thickness_measured"] is None:
        return {"ok": False, "error": "no geometry yet: call generate_geometry first"}
    _, m, p, t = ac.naca4(case["naca"])
    out = {"ok": True, "naca": case["naca"], "max_camber": m, "camber_position": p,
           "thickness_code": t, "thickness_measured": round_sig(facts["thickness_measured"], 5),
           "thickness_error_pct": round_sig(abs(facts["thickness_measured"] - t) / t * 100.0, 3),
           "trailing_edge_gap": round_sig(facts["trailing_edge_gap"], 3),
           "closed": facts["trailing_edge_gap"] < 1e-4,
           "leading_edge_radius": round_sig(1.1019 * t * t, 4),
           "points_per_side": facts["points_per_side"],
           "obj_exists": facts["obj_exists"], "obj_vertices": facts["obj_vertices"]}
    ctx.merge_metrics(thickness=out["thickness_measured"], max_camber=m, camber_position=p,
                      trailing_edge_gap=out["trailing_edge_gap"], obj_vertices=out["obj_vertices"])
    out["summary"] = (f"thickness {out['thickness_measured'] * 100:.2f}% (code {t * 100:.0f}%), "
                      f"{'closed' if out['closed'] else 'OPEN'} trailing edge, {out['obj_vertices']} OBJ vertices")
    return out


# --------------------------------------------------------------------- mesh --

def tool_estimate_first_cell(ctx, target_yplus=None):
    cond = ctx.conditions()
    y = float(target_yplus if target_yplus is not None else planning.DEFAULT_TARGET_YPLUS)
    if not (0.05 <= y <= 500):
        return {"ok": False, "error": "target_yplus must be between 0.05 and 500"}
    est = ac.first_cell_height(cond["reynolds"], cond["velocity"], y)
    return {"ok": True, "target_yplus": y, "reynolds": cond["reynolds"],
            "first_cell_height": round_sig(est["first_cell_height"], 4),
            "cell_centre_height": round_sig(est["cell_centre_height"], 4),
            "cf_estimate": round_sig(est["cf_estimate"], 4),
            "cites": citations.default_cites("mesh", "first_cell_height"),
            "note": "heights in chords; flat-plate estimate, the measured y+ on an airfoil is "
                    "usually somewhat lower on average. Wall functions need 20-200, a resolved wall <= 5.",
            "summary": f"first cell {est['first_cell_height']:.3g} chord for y+ {y:g} at Re {cond['reynolds']:.3g}"}


def engineer_locked(ctx, stage, wanted):
    """Settings in `wanted` that would replace a plan value the engineer set by hand, which only
    the gate may overrule. Empty when nothing is locked: no engine attached, a tool call from
    another stage, the gate has already rejected this run of the stage, an earlier run of it
    ended failed, or the engineer or a rework gave this run a reason to change
    (CaseRunner.lock_open)."""
    rt = ctx.runtime
    fails = getattr(rt, "gate_fails", None)
    # NullRuntime answers any name with a function and StudyRuntime has no gate_fails
    if ctx.stage != stage or isinstance(fails, bool) or not isinstance(fails, int):
        return {}
    if fails > 0 or getattr(rt, "lock_open", False) is True or gates.stage_record(ctx, stage).get("failure"):
        return {}
    return {k: v for k, v in planning.engineer_values(ctx.plan_stage(stage)).items()
            if wanted.get(k) is not None and not _same_value(wanted[k], v)}


def _same_value(a, b):
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=0.0)
    except (TypeError, ValueError):
        return a == b


def engineer_values_in_place(ctx, stage):
    """The plan values of `stage` the engineer set by hand that the case holds right now, as the
    stage's setter wrote them ({} for a stage without a settings file). When a gate fails, these
    are the engineer values it has judged (CaseRunner.complete_stage keeps them on the record)."""
    name, inner = {"mesh": ("mesh_plan.json", "params"), "physics": ("physics.json", None),
                   "numerics": ("numerics.json", None)}.get(stage, (None, None))
    written = (read_json(os.path.join(ctx.case_dir, name)) or {}) if name else {}
    written = (written.get(inner) or {}) if inner else written
    return {k: v for k, v in planning.engineer_values(ctx.plan_stage(stage)).items()
            if written.get(k) is not None and _same_value(written[k], v)}


def tool_set_mesh_params(ctx, **overrides):
    case, cond = ctx.case, ctx.conditions()
    unknown = [k for k in overrides if k not in planning.MESH_KEYS]
    if unknown:
        return {"ok": False, "error": f"unknown mesh parameter(s): {', '.join(unknown)}", "allowed": planning.MESH_KEYS}
    if not os.path.exists(os.path.join(ctx.case_dir, "constant", "geometry", f"NACA{case['naca']}.obj")):
        return {"ok": False, "error": "no geometry in the case: the geometry stage must run first"}
    # refused before anything is resolved or written: the engineer's value stays in the case
    held = engineer_locked(ctx, "mesh", overrides)
    if held:
        text = ", ".join(f"{k} = {v}" for k, v in held.items())
        return {"ok": False, "engineer_value": held,
                "error": f"{text} was set by the engineer and is an instruction: leave it as it is, build and check "
                         "the mesh with it and call complete_stage. Only after the gate has rejected the stage "
                         "because of this value may you change it, and then state the deviation in your summary and "
                         "decisions. If the mesh cannot be built with it, call complete_stage anyway: the failed "
                         "gate releases the value."}
    params = planning.resolve_mesh_params(ctx.plan_stage("mesh"), overrides)
    for k, (lo, hi) in planning.MESH_LIMITS.items():
        if not (lo <= params[k] <= hi):
            return {"ok": False, "error": f"{k} = {params[k]:g} is outside {lo:g} to {hi:g}"}
    cells = ac.mesh_plan(params)["cells"]
    if cells > planning.MAX_CELLS:
        # refused here, before anything is written or built: the gate would only say so
        # after blockMesh, extrudeMesh and checkMesh had run on the oversized mesh
        return {"ok": False, "cells": cells,
                "error": f"{cells:,} cells is above the limit of {planning.MAX_CELLS:,} "
                         "(cells = 2 x (n_leading + n_aft + n_wake) x n_normal): lower the counts"}
    plan = ac.write_mesh_dict(ctx.case_dir, case["naca"], params)
    mark_write(ctx.case_dir)
    yp = planning.predicted_yplus(params["first_cell_height"], cond["reynolds"], cond["velocity"])
    lo, hi = planning.yplus_band(gates.wall_treatment_for(ctx))
    warnings = []
    if params["farfield_chords"] > 30:
        warnings.append("far field above 30 chords is known to diverge with this block topology")
    if not (lo <= yp <= hi):
        warnings.append(f"predicted y+ {yp:.1f} is outside the {lo:g}-{hi:g} band for the wall treatment")
    rebuild = os.path.isdir(os.path.join(ctx.case_dir, "constant", "polyMesh")) and not gates.mesh_matches_setup(ctx.case_dir)
    if rebuild:
        warnings.append("the mesh on disk was built from other values: call build_mesh, then check_mesh")
    return {"ok": True, "params": params, "cells": plan["cells"], "surface_cells": plan["surface_cells"],
            "wall_normal_growth": plan["wall_normal_growth"], "wake_growth": plan["wake_growth"],
            "predicted_yplus": round_sig(yp, 4), "yplus_band": [lo, hi], "warnings": warnings,
            "mesh_needs_rebuild": rebuild,
            "summary": f"{plan['cells']:,} cells planned, wall-normal growth {plan['wall_normal_growth']:.3f}, "
                       f"predicted y+ {yp:.0f}"}


def _clear_results(case_dir):
    """Remove everything a new mesh or a field reset invalidates."""
    for _, name in time_dirs(case_dir):
        shutil.rmtree(os.path.join(case_dir, name), ignore_errors=True)
    shutil.rmtree(os.path.join(case_dir, "postProcessing"), ignore_errors=True)
    for name in ("log.simpleFoam", "log.simpleFoam.prev", "solver_state.json", "cp.json"):
        _remove(os.path.join(case_dir, name))
    mark_write(case_dir)


def tool_build_mesh(ctx):
    case_dir, name = ctx.case_dir, ctx.case_name
    if not os.path.exists(os.path.join(case_dir, "system", "blockMeshDict")):
        return {"ok": False, "error": "no blockMeshDict: call set_mesh_params first"}
    if solver.solver_running(ctx.case_id):
        return {"ok": False, "error": "the solver is running; stop it before rebuilding the mesh"}
    planned = read_json(os.path.join(case_dir, "mesh_plan.json")) or {}
    if (planned.get("cells") or 0) > planning.MAX_CELLS:
        return {"ok": False, "error": f"the mesh set-up has {planned['cells']:,} cells, above the limit of "
                                      f"{planning.MAX_CELLS:,}: call set_mesh_params with lower counts first"}
    # The mesh utilities need complete system dictionaries. Until the solver-setup
    # stage has run, write the defaults as placeholders: without numerics.json they
    # do not count as that stage's work (see the numerics gate).
    sysdir = os.path.join(case_dir, "system")
    if not all(os.path.exists(os.path.join(sysdir, n)) for n in ("controlDict", "fvSchemes", "fvSolution")):
        ac.write_numerics(case_dir, ctx.conditions()["velocity"], ctx.case["alpha_deg"])
        os.remove(os.path.join(case_dir, "numerics.json"))
    shutil.rmtree(os.path.join(case_dir, "constant", "polyMesh"), ignore_errors=True)
    for stale in ("mesh_check.json", "mesh_built.json"):
        _remove(os.path.join(case_dir, stale))
    _clear_results(case_dir)
    built_from = {"params": planned.get("params"),
                  "blockMeshDict": read_text(os.path.join(case_dir, "system", "blockMeshDict"))}
    steps, started = [], time.time()
    for cmd in ac.MESH_COMMANDS:
        r = _sim(case_dir, "run", name, "--timeout", 300, "--tail", 6, "--", *cmd, timeout=360)
        steps.append({"command": cmd[0], "ok": bool(r.get("ok")), "seconds": r.get("seconds"),
                      "fatal_error": r.get("fatal_error") or r.get("error")})
        if not r.get("ok"):
            return {"ok": False, "steps": steps, "tail": r.get("tail"),
                    "error": f"{cmd[0]} failed: " + str(r.get("fatal_error") or r.get("error") or "see log")[:600]}
    secs = round(time.time() - started, 1)
    # what this mesh was built from: the gate and check_mesh compare it with the set-up
    write_json(os.path.join(case_dir, "mesh_built.json"), built_from)
    return {"ok": True, "steps": steps, "seconds": secs,
            "summary": f"blockMesh, transformPoints and extrudeMesh ran in {secs:g} s"}


def tool_check_mesh(ctx):
    case_dir, cond = ctx.case_dir, ctx.conditions()
    if not os.path.isdir(os.path.join(case_dir, "constant", "polyMesh")):
        return {"ok": False, "error": "no mesh yet: call build_mesh first"}
    if not gates.mesh_matches_setup(case_dir):
        return {"ok": False, "error": "the mesh on disk was not built from the set-up now written "
                                      "(set_mesh_params changed it, or it is from an older run): "
                                      "call build_mesh first"}
    r = _sim(case_dir, "mesh", ctx.case_name, timeout=300)
    if r.get("cells") is None:
        return {"ok": False, "error": "checkMesh failed: " + str(r.get("fatal_error") or r.get("error"))[:600]}
    keep = {k: r.get(k) for k in ("cells", "points", "faces", "mesh_ok", "failed_checks", "max_aspect_ratio",
                                  "max_non_orthogonality", "avg_non_orthogonality", "max_skewness",
                                  "warnings", "fatal_error", "bounding_box")}
    write_json(os.path.join(case_dir, "mesh_check.json"), keep)
    mp = read_json(os.path.join(case_dir, "mesh_plan.json")) or {}
    params = mp.get("params") or {}
    yp = planning.predicted_yplus(params.get("first_cell_height", ac.DEFAULT_MESH["first_cell_height"]),
                                  cond["reynolds"], cond["velocity"])
    lo, hi = planning.yplus_band(gates.wall_treatment_for(ctx))
    ctx.merge_results(cells=keep["cells"])
    ctx.merge_metrics(cells=keep["cells"], max_non_orthogonality=keep["max_non_orthogonality"],
                      max_skewness=keep["max_skewness"], max_aspect_ratio=keep["max_aspect_ratio"],
                      predicted_yplus=round_sig(yp, 4), first_cell_height=params.get("first_cell_height"),
                      wall_normal_growth=mp.get("wall_normal_growth"),
                      farfield_chords=params.get("farfield_chords"))
    ctx.set_headline(f"{keep['cells']:,} cells · non-ortho {keep['max_non_orthogonality']:.0f}")
    note = ("checkMesh flags the high aspect ratio of the wall cells; that is expected for a "
            "boundary-layer mesh and does not fail the gate" if not keep["mesh_ok"] and not keep["fatal_error"]
            else None)
    return {"ok": True, **keep, "predicted_yplus": round_sig(yp, 4), "yplus_band": [lo, hi], "note": note,
            "summary": f"{keep['cells']:,} cells, max non-orthogonality {keep['max_non_orthogonality']:.1f}, "
                       f"max skewness {keep['max_skewness']:.2f}, predicted y+ {yp:.0f}"}


def tool_render_view(ctx, mode="mesh", field=None, zoom="auto"):
    viewer = getattr(ctx.runtime, "viewer", None)
    if viewer is None:
        return {"ok": False, "error": "the viewer is not available on this server; continue without the image"}
    case_dir = ctx.case_dir
    if not os.path.isdir(os.path.join(case_dir, "constant", "polyMesh")):
        return {"ok": False, "error": "no mesh to render yet"}
    mode = mode if mode in ("mesh", "field", "patches") else "mesh"
    if mode == "field" and not field:
        field = "U"
    if isinstance(zoom, (list, tuple)):
        zoom = ",".join(f"{float(z):g}" for z in zoom)
    zoom = str(zoom or "auto").strip()
    box = zoom                                 # a preset name, or [h0, h1, v0, v1] for the viewer
    if zoom not in planning.ZOOM_PRESETS:
        try:
            box = [float(z) for z in zoom.split(",")]
        except ValueError:
            box = None
        if not box or len(box) != 4 or box[0] >= box[1] or box[2] >= box[3]:
            return {"ok": False, "error": f"zoom must be one of {', '.join(planning.ZOOM_PRESETS)} or 'x0,x1,z0,z1' in chords"}
    tag = re.sub(r"[^\w.-]+", "_", f"{ctx.stage or 'view'}_{mode}_{field or 'grid'}_{zoom}")[:80]
    out_dir = os.path.join(case_dir, "images")
    os.makedirs(out_dir, exist_ok=True)
    out_png = os.path.join(out_dir, tag + ".png")
    try:
        r = viewer.render(case_dir, out_png, mode=mode, field=field if mode == "field" else None, zoom=box)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"render failed: {type(e).__name__}: {e}"}
    if not (isinstance(r, dict) and r.get("ok") and os.path.exists(out_png)):
        return {"ok": False, "error": "render failed: " + str((r or {}).get("error") or r)[:500]}
    drawn = r.get("mode") or mode              # a field that cannot be read yet is drawn as the mesh
    label = (f"{field} field ({zoom})" if drawn == "field" else f"{drawn.capitalize()} ({zoom})")
    art = ctx.add_artifact("image", label, out_png)
    return {"ok": True, "image": art["url"], "mode": drawn, "field": field if drawn == "field" else None,
            "zoom": zoom, "range": r.get("range"), "time": r.get("time"), "seconds": r.get("seconds"),
            "warnings": r.get("warnings") or [],
            "summary": f"rendered {label[0].lower() + label[1:]}"}


# ------------------------------------------------------------------ physics --

def tool_set_physics(ctx, **overrides):
    case, cond = ctx.case, ctx.conditions()
    unknown = [k for k in overrides if k not in planning.PHYSICS_KEYS]
    if unknown:
        return {"ok": False, "error": f"unknown physics parameter(s): {', '.join(unknown)}", "allowed": planning.PHYSICS_KEYS}
    if solver.solver_running(ctx.case_id):
        return {"ok": False, "error": "the solver is running; stop it before changing the physics"}
    p = dict(planning.DEFAULT_PHYSICS)
    for src in (ctx.plan_stage("physics"), overrides):
        for k in planning.PHYSICS_KEYS:
            if src.get(k) is not None:
                p[k] = src[k]
    problems = planning.setting_problems("physics", p)
    if problems:
        return {"ok": False, "error": "; ".join(problems), "limits": planning.SETTING_LIMITS["physics"]}
    try:
        summary = ac.write_physics(ctx.case_dir, cond["velocity"], case["alpha_deg"], cond["reynolds"],
                                   turbulence_model=p["turbulence_model"], nut_ratio=float(p["nut_ratio"]),
                                   turbulence_intensity=float(p["turbulence_intensity"]),
                                   wall_treatment=p["wall_treatment"], farfield_bc=p["farfield_bc"])
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    _clear_results(ctx.case_dir)       # results computed with other boundary fields are no longer valid
    # numerics depend on the turbulence model (which transport equations exist)
    stale = None
    numerics = read_json(os.path.join(ctx.case_dir, "numerics.json"))
    if numerics and os.path.exists(os.path.join(ctx.case_dir, "system", "fvSchemes")):
        if (f"div(phi,{'nuTilda' if p['turbulence_model'] == 'SpalartAllmaras' else 'omega'})"
                not in read_text(os.path.join(ctx.case_dir, "system", "fvSchemes"))):
            stale = "the turbulence model changed: the solver-setup stage must rewrite fvSchemes/fvSolution"
    ctx.merge_metrics(**{k: p[k] for k in planning.PHYSICS_KEYS}, velocity=round_sig(cond["velocity"], 6),
                      alpha_deg=case["alpha_deg"], nu=round_sig(summary["nu"], 5))
    model_short = "SA" if p["turbulence_model"] == "SpalartAllmaras" else "k-omega SST"
    ctx.set_headline(f"{model_short} · {'wall functions' if p['wall_treatment'] == 'wallFunction' else 'resolved wall'}"
                     f" · {case['alpha_deg']:g} deg")
    return {"ok": True, **{k: round_sig(v, 6) if not isinstance(v, (str, list)) else v for k, v in summary.items()},
            "velocity": round_sig(cond["velocity"], 6), "alpha_deg": case["alpha_deg"],
            "reynolds": cond["reynolds"], "warning": stale,
            "summary": f"{p['turbulence_model']}, {p['wall_treatment']}, {p['farfield_bc']} far field, "
                       f"U = {cond['velocity']:.4g} m/s at {case['alpha_deg']:g} deg, nu = {summary['nu']:.3g}"}


def _boundary_types(text):
    """patch -> type from the boundaryField block of a field file."""
    out = {}
    m = re.search(r"boundaryField\s*\{(.*)\}", text, re.S)
    if not m:
        return out
    for pm in re.finditer(r'\n\s{4}("?[\w()|]+"?)\s*\n\s{4}\{(.*?)\n\s{4}\}', m.group(1), re.S):
        t = re.search(r"type\s+(\w+);", pm.group(2))
        if t:
            out[pm.group(1).strip('"')] = t.group(1)
    return out


def tool_inspect_boundary(ctx):
    case_dir, cond, case = ctx.case_dir, ctx.conditions(), ctx.case
    facts = gates.physics_facts(case_dir)
    if not facts["fields_present"]:
        return {"ok": False, "error": "no 0.orig yet: call set_physics first"}
    zero = os.path.join(case_dir, "0.orig")
    fields = {f: _boundary_types(read_text(os.path.join(zero, f))) for f in facts["fields_present"]}
    patches = {}
    btext = read_text(os.path.join(case_dir, "constant", "polyMesh", "boundary"))
    for pm in re.finditer(r"\n\s{4}(\w+)\s*\n\s{4}\{(.*?)\n\s{4}\}", btext, re.S):
        t = re.search(r"type\s+(\w+);", pm.group(2))
        n = re.search(r"nFaces\s+(\d+);", pm.group(2))
        patches[pm.group(1)] = {"type": t.group(1) if t else None, "faces": int(n.group(1)) if n else None}
    need = gates.required_fields(facts.get("turbulence_model"))
    out = {"ok": True, "turbulence_model": facts.get("turbulence_model"),
           "wall_treatment": facts.get("wall_treatment"), "farfield_bc": facts.get("farfield_bc"),
           "fields_present": facts["fields_present"], "fields_required": need,
           "fields_missing": [f for f in need if f not in facts["fields_present"]],
           "boundary_types": fields, "mesh_patches": patches,
           "velocity_in_file": round_sig(facts["velocity_file"], 6), "velocity_expected": round_sig(cond.get("velocity"), 6),
           "alpha_in_file_deg": round_sig(facts["alpha_file"], 5), "alpha_expected_deg": case["alpha_deg"],
           "reynolds_in_file": round_sig(facts["reynolds_file"], 6), "reynolds_expected": cond.get("reynolds")}
    out["summary"] = (f"{len(facts['fields_present'])} fields ({', '.join(facts['fields_present'])}), "
                      f"|U| {out['velocity_in_file']} m/s, Re {facts['reynolds_file']:.4g}, "
                      f"{facts.get('farfield_bc')} far field" if facts["reynolds_file"] else "fields written")
    return out


# ----------------------------------------------------------------- numerics --

def tool_set_numerics(ctx, **overrides):
    case, cond, case_dir = ctx.case, ctx.conditions(), ctx.case_dir
    unknown = [k for k in overrides if k not in planning.NUMERICS_KEYS]
    if unknown:
        return {"ok": False, "error": f"unknown numerics parameter(s): {', '.join(unknown)}", "allowed": planning.NUMERICS_KEYS}
    if solver.solver_running(ctx.case_id):
        return {"ok": False, "error": "the solver is running; call stop_solver before changing the numerics"}
    # The solver-setup stage starts from the plan; later stages adjust what is in force.
    current = read_json(os.path.join(case_dir, "numerics.json"))
    base = ctx.plan_stage("numerics") if (ctx.stage == "numerics" or not current) else current
    try:
        n = planning.resolve_numerics(base, overrides)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"a numerics value is not a number: {e}"}
    problems = planning.setting_problems("numerics", n)
    if problems:
        return {"ok": False, "error": "; ".join(problems), "limits": planning.SETTING_LIMITS["numerics"]}
    if n["convection_scheme"] not in ("linearUpwind", "upwind"):
        return {"ok": False, "error": "convection_scheme must be linearUpwind or upwind"}
    if n["iterations"] < 1:
        return {"ok": False, "error": "iterations must be positive"}
    if n["iterations"] > planning.MAX_TOTAL_ITERATIONS:
        return {"ok": False, "error": f"iterations = {n['iterations']} is above the ceiling of {planning.MAX_TOTAL_ITERATIONS}"
                                      + (" (the solver-setup gate accepts 300 to 6000)" if ctx.stage == "numerics" else "")}
    notes = []
    n["write_interval"] = min(n["write_interval"], n["iterations"])
    rounded = planning.round_up(n["iterations"], n["write_interval"])
    if rounded != n["iterations"]:
        # The final state, y+ and the surface pressure are only written at a write time.
        notes.append(f"iterations raised from {n['iterations']} to {rounded} so the run ends on a write time")
        n["iterations"] = rounded
    if n["iterations"] > planning.MAX_TOTAL_ITERATIONS:
        return {"ok": False, "error": f"with write_interval = {n['write_interval']} the run would end at iteration "
                                      f"{n['iterations']}, above the ceiling of {planning.MAX_TOTAL_ITERATIONS}; "
                                      "lower iterations or use a write_interval that divides it"}
    physics = read_json(os.path.join(case_dir, "physics.json")) or {}
    model = physics.get("turbulence_model") or ctx.plan_stage("physics").get("turbulence_model") \
        or planning.DEFAULT_PHYSICS["turbulence_model"]
    # Keep the run length already reached when numerics are changed mid-run.
    times = time_dirs(case_dir)
    latest = times[-1][0] if times else 0
    if ctx.stage != "numerics" and latest > 0 and "iterations" not in overrides:
        n["iterations"] = max(n["iterations"], solver.end_time(case_dir) or 0)
    # The force axes belong to the flow that is solved: the angle set_physics wrote. (The
    # case angle can be edited in the plan after that; the fields only follow when the
    # physics stage runs again.)
    alpha = physics.get("alpha_deg") if is_finite(physics.get("alpha_deg")) else case["alpha_deg"]
    ac.write_numerics(case_dir, cond["velocity"], alpha, turbulence_model=model, params=n)
    mark_write(case_dir)
    if ctx.stage == "numerics":
        ctx.merge_metrics(**{k: n[k] for k in planning.NUMERICS_KEYS})
        ctx.set_headline(f"{n['iterations']} iterations · {n['convection_scheme']}")
    return {"ok": True, "numerics": {k: n[k] for k in planning.NUMERICS_KEYS}, "turbulence_model": model, "notes": notes,
            "force_axes_angle_deg": alpha,
            "summary": f"{n['iterations']} iterations, {n['convection_scheme']}, relaxation p {n['relax_p']:g} / "
                       f"U {n['relax_U']:g} / turbulence {n['relax_turbulence']:g}"}


def tool_inspect_numerics(ctx):
    case_dir = ctx.case_dir
    sysdir = os.path.join(case_dir, "system")
    n = read_json(os.path.join(case_dir, "numerics.json"))
    if not n:
        return {"ok": False, "error": "numerics not written yet: call set_numerics first"}
    control = read_text(os.path.join(sysdir, "controlDict"))
    schemes = read_text(os.path.join(sysdir, "fvSchemes"))
    solution = read_text(os.path.join(sysdir, "fvSolution"))
    div = dict(re.findall(r"^\s*(div\(phi,\w+\))\s+([^;]+);", schemes, re.M))
    force = gates.force_setup(case_dir)
    alpha = gates.solved_angle(ctx)
    alpha = alpha if is_finite(alpha) else ctx.case.get("alpha_deg")
    velocity = ctx.conditions().get("velocity")
    axes = force["force_axes_angle_deg"]
    out = {"ok": True, "numerics": {k: n.get(k) for k in planning.NUMERICS_KEYS},
           "files": {name: bool(text) for name, text in (("controlDict", control), ("fvSchemes", schemes),
                                                         ("fvSolution", solution))},
           "end_time": solver.end_time(case_dir),
           "function_objects": re.findall(r"^\s{4}(\w+)\s*\n\s{4}\{", control, re.M),
           "div_schemes": div,
           "has_force_coeffs": "forceCoeffs" in control,
           "residual_control_present": "residualControl" in solution,
           # the force-coefficient set-up as written, against the flow it must describe
           "force_coeffs": {"lift_dir": force["lift_dir"], "drag_dir": force["drag_dir"],
                            "axes_angle_deg": round_sig(axes, 6), "alpha_expected_deg": alpha,
                            "axes_match_alpha": is_finite(axes) and is_finite(alpha) and abs(axes - alpha) <= 0.05,
                            "mag_u_inf": force["mag_u_inf"], "velocity_expected": round_sig(velocity, 6),
                            "speed_matches": bool(is_finite(force["mag_u_inf"]) and velocity
                                                  and abs(force["mag_u_inf"] - velocity) <= 0.01 * velocity),
                            "moment_centre": force["cofr"], "l_ref": force["l_ref"], "a_ref": force["a_ref"]}}
    out["summary"] = (f"endTime {out['end_time']}, {len(div)} convection schemes, function objects: "
                      + ", ".join(out["function_objects"])
                      + (f", force axes at {axes:.3f} deg" if is_finite(axes) else ", force axes not readable"))
    return out


# -------------------------------------------------------------------- solve --

def _solver_report(ctx, run=None):
    """The wait_solver payload, from the live run or from what the last run left behind."""
    case_dir = ctx.case_dir
    saved = read_json(os.path.join(case_dir, "solver_state.json")) or {}
    if run is not None:
        state, iteration, total = run.state, run.iteration, run.total
        residuals, seconds, fatal, converged = run.residuals, run.seconds_total, run.fatal, run.converged
    else:
        state, iteration, total = saved.get("state"), saved.get("iteration"), saved.get("total")
        residuals = ac.residual_history(case_dir).get("fields", {})
        seconds, fatal, converged = saved.get("seconds_total"), saved.get("fatal"), saved.get("converged")
    fs = ac.force_summary(case_dir)
    out = {"ok": state != "failed", "state": state, "iteration": iteration, "total": total,
           "residuals": {k: round_sig(v, 4) for k, v in (residuals or {}).items()},
           "cl": round_sig(fs.get("Cl"), 6), "cd": round_sig(fs.get("Cd"), 6), "cm": round_sig(fs.get("Cm"), 6),
           "cl_drift": round_sig(fs.get("Cl_drift"), 3), "cd_drift": round_sig(fs.get("Cd_drift"), 3),
           "forces_converged": bool(fs.get("forces_converged")),
           "residual_control_converged": bool(converged), "seconds": seconds,
           "human_message_pending": bool(ctx.runtime.message_pending())}
    if fatal:
        out["fatal_error"] = fatal
        out["error"] = "solver failed: " + fatal[:300]
    if state == "finished" and iteration is not None and total and iteration < total and not converged:
        out["note"] = f"the solver ended at iteration {iteration} of {total}; call start_solver to continue"
    if (ctx.stage == "solve" and state in ("finished", "stopped", "failed")
            and gates.written_this_run(ctx, "solve", os.path.join(case_dir, "solver_state.json")) is False):
        # the stage was sent back here (rework or re-run) and no solver has run since
        out["from_earlier_run"] = True
        out["note"] = ("this is the run that ended before the stage was started again; the gate needs a "
                       "solver run in this run of the stage: extend_run(iterations) to continue it, or "
                       "reset_fields() and start_solver() to start again with the current set-up")
    # a diverged run leaves astronomically large coefficients: print those in short form
    cl = (f"{fs['Cl']:.4f}" if abs(fs["Cl"]) < 100 else f"{fs['Cl']:.3g}") if fs.get("available") else "n/a"
    cd = (f"{fs['Cd']:.5f}" if abs(fs["Cd"]) < 100 else f"{fs['Cd']:.3g}") if fs.get("available") else "n/a"
    if ctx.stage == "solve":
        ctx.merge_metrics(state=state, iterations=iteration, total=total, seconds=seconds, cl=out["cl"], cd=out["cd"],
                          cl_drift=out["cl_drift"], cd_drift=out["cd_drift"], residuals=out["residuals"],
                          forces_converged=out["forces_converged"])
        ctx.set_headline(f"{iteration} of {total} iterations · " +
                         ("forces converged" if out["forces_converged"] else
                          "failed" if state == "failed" else state or "not started"))
    out["summary"] = (f"{state}: iteration {iteration} of {total}, Cl {cl}, Cd {cd}"
                      + (", forces converged" if out["forces_converged"] else "")
                      + (f" - {fatal[:120]}" if fatal else ""))
    return out


def tool_start_solver(ctx):
    case_dir = ctx.case_dir
    with solver.SOLVERS_LOCK:
        if solver.solver_running(ctx.case_id):
            return {"ok": False, "error": "the solver is already running; use wait_solver"}
        if not os.path.isdir(os.path.join(case_dir, "constant", "polyMesh")):
            return {"ok": False, "error": "no mesh: the mesh stage must run first"}
        if not os.path.isdir(os.path.join(case_dir, "0.orig")):
            return {"ok": False, "error": "no 0.orig: the boundary-condition stage must run first"}
        control = os.path.join(case_dir, "system", "controlDict")
        text = read_text(control)
        if "forceCoeffs" not in text:
            return {"ok": False, "error": "controlDict has no forceCoeffs: the solver-setup stage must run first"}
        total = solver.end_time(case_dir)
        if re.search(r"^stopAt\s+writeNow;", text, re.M):
            # left behind by a graceful stop
            with open(control, "w") as f:
                f.write(re.sub(r"^(stopAt\s+)writeNow;", r"\1endTime;", text, flags=re.M))
            mark_write(case_dir)
        times = time_dirs(case_dir)
        latest = times[-1][0] if times else None
        if latest is None:
            shutil.copytree(os.path.join(case_dir, "0.orig"), os.path.join(case_dir, "0"))
            mark_write(case_dir)
            latest = 0.0
        if latest >= total:
            return {"ok": False, "error": f"the run is already at iteration {latest:g} of {total}; "
                                          "call extend_run(iterations) to continue or reset_fields() to restart"}
        previous = read_json(os.path.join(case_dir, "solver_state.json")) or {}
        seconds_before = float(previous.get("seconds_total") or 0.0)
        if latest == 0:
            log = os.path.join(case_dir, "log.simpleFoam")
            if os.path.exists(log):
                os.replace(log, log + ".prev")
            shutil.rmtree(os.path.join(case_dir, "postProcessing"), ignore_errors=True)
            seconds_before = 0.0
            mark_write(case_dir)
        _remove(os.path.join(case_dir, "solver_state.json"))
        # Second guard against two solvers in one directory (the first is the engine's reaping at
        # start-up): a container of this case that no SolverRun here owns is an orphan.
        orphans = solver.leftover_solver_containers(ctx.case_id)
        if orphans:
            docker.cli("kill", *orphans, timeout=30)
            time.sleep(1.0)
        settle(case_dir)
        run = solver.SolverRun(ctx.case_id, case_dir, ctx.emit, total, int(latest), seconds_before)
        try:
            run.start()
        except docker.DockerError as e:
            return {"ok": False, "error": f"the solver could not start: {e}"}
        solver.SOLVERS[ctx.case_id] = run
    return {"ok": True, "state": "running", "start_iteration": int(latest), "total": total,
            "container": run.container,
            "summary": f"simpleFoam started at iteration {int(latest)} of {total}"}


def tool_wait_solver(ctx, max_wait_s=30):
    run = solver.SOLVERS.get(ctx.case_id)
    if run is None:
        if os.path.exists(os.path.join(ctx.case_dir, "solver_state.json")):
            return _solver_report(ctx, None)
        return {"ok": False, "error": "no solver has been started for this case: call start_solver"}
    try:
        wait = min(120.0, max(1.0, float(max_wait_s)))
    except (TypeError, ValueError):
        wait = 30.0
    deadline = time.time() + wait
    while (run.state == "running" and time.time() < deadline
           and not ctx.runtime.message_pending() and not ctx.runtime.interrupted()):
        time.sleep(0.25)
    return _solver_report(ctx, run)


def tool_stop_solver(ctx):
    run = solver.SOLVERS.get(ctx.case_id)
    if run is None:
        return {"ok": False, "error": "no solver has been started for this case: call start_solver"}
    if run.state != "running":
        # It ended by itself between two polls (finished, or diverged and was killed):
        # report how, so the agent can go straight to the next step.
        out = _solver_report(ctx, run)
        out.update(ok=True, already_ended=True,
                   summary=f"nothing to stop: the solver had already ended ({out.get('summary')})")
        return out
    run.stop(graceful=True)
    out = _solver_report(ctx, run)
    out.update(ok=True, summary=f"solver stopped at iteration {run.iteration} of {run.total}")
    return out


def tool_extend_run(ctx, iterations=None):
    case_dir = ctx.case_dir
    try:
        add = int(iterations)
    except (TypeError, ValueError):
        return {"ok": False, "error": "iterations must be a whole number"}
    if add < 1:
        return {"ok": False, "error": "iterations must be positive"}
    current = solver.end_time(case_dir)
    if current is None:
        return {"ok": False, "error": "no controlDict: the solver-setup stage must run first"}
    numerics = read_json(os.path.join(case_dir, "numerics.json")) or {}
    new_total = planning.round_up(current + add, numerics.get("write_interval") or 1)
    if new_total > planning.MAX_TOTAL_ITERATIONS:
        return {"ok": False, "error": f"that would be {new_total} iterations; the ceiling is {planning.MAX_TOTAL_ITERATIONS}"}
    run = solver.SOLVERS.get(ctx.case_id)
    if run is not None and run.state == "running":
        if not run.set_total_live(new_total):
            return {"ok": False, "error": "could not change endTime in the running container; "
                                          "wait for it to finish and call extend_run again"}
        mode = "the running solver continues"
    else:
        state = (read_json(os.path.join(case_dir, "solver_state.json")) or {}).get("state")
        if state == "failed":
            return {"ok": False, "error": "the last run failed; fix the set-up, call reset_fields, then start_solver"}
        ac.set_iterations(case_dir, new_total)
        mark_write(case_dir)
        started = tool_start_solver(ctx)
        if not started.get("ok"):
            return started
        mode = "solver restarted from the latest time"
    numerics["iterations"] = new_total
    write_json(os.path.join(case_dir, "numerics.json"), numerics)
    return {"ok": True, "state": "running", "total": new_total, "previous_total": current,
            "summary": f"run extended from {current} to {new_total} iterations ({mode})"}


def tool_reset_fields(ctx):
    case_dir = ctx.case_dir
    if solver.solver_running(ctx.case_id):
        return {"ok": False, "error": "the solver is running; call stop_solver first"}
    if not os.path.isdir(os.path.join(case_dir, "0.orig")):
        return {"ok": False, "error": "no 0.orig to reset from"}
    _clear_results(case_dir)
    shutil.copytree(os.path.join(case_dir, "0.orig"), os.path.join(case_dir, "0"))
    mark_write(case_dir)
    with solver.SOLVERS_LOCK:
        solver.SOLVERS.pop(ctx.case_id, None)
    return {"ok": True, "summary": "fields reset to the initial state; results and logs removed"}


# --------------------------------------------------------------------- post --

def _post_process_fallback(ctx):
    """Re-run the function objects on the latest time when the run did not write them."""
    return _sim(ctx.case_dir, "run", ctx.case_name, "--timeout", 300, "--log", "log.postProcess",
                "--", "simpleFoam", "-postProcess", "-latestTime", timeout=360)


def tool_compute_forces(ctx):
    case_dir = ctx.case_dir
    fs = ac.force_summary(case_dir)
    if not fs.get("available"):
        return {"ok": False, "error": "no force history: the solve stage has not produced results"}
    res = ac.residual_history(case_dir)
    state = read_json(os.path.join(case_dir, "solver_state.json")) or {}
    cl, cd = fs["Cl"], fs["Cd"]
    ld = cl / cd if cd else None
    endurance = (cl ** 1.5 / cd) if (cd and cl > 0) else None
    residuals = {k: round_sig(v, 4) for k, v in res.get("fields", {}).items()}
    coeffs = {"cl": round_sig(cl, 6), "cd": round_sig(cd, 6), "cm": round_sig(fs["Cm"], 6), "ld": round_sig(ld, 5)}
    ctx.merge_results(**coeffs, iterations=fs["iterations"], forces_converged=bool(fs["forces_converged"]),
                      residuals=residuals, solver_seconds=state.get("seconds_total"))
    ctx.merge_metrics(**coeffs, endurance=round_sig(endurance, 5))
    ctx.set_headline(f"Cl {cl:.3f} · Cd {cd:.4f} · L/D {ld:.1f}" if ld else f"Cl {cl:.3f}")
    return {"ok": True, **coeffs, "endurance": round_sig(endurance, 5), "iterations": fs["iterations"],
            "cl_drift": round_sig(fs["Cl_drift"], 3), "cd_drift": round_sig(fs["Cd_drift"], 3),
            "forces_converged": bool(fs["forces_converged"]), "residuals": residuals,
            "summary": f"Cl {cl:.4f}, Cd {cd:.5f}, Cm {fs['Cm']:.4f}, L/D {ld:.1f}" if ld else f"Cl {cl:.4f}"}


def tool_compute_yplus(ctx):
    case_dir = ctx.case_dir
    yp = ac.yplus_summary(case_dir)
    source = "function object during the run"
    if not yp:
        r = _post_process_fallback(ctx)
        yp = ac.yplus_summary(case_dir)
        source = "post-processing of the latest time"
        if not yp:
            return {"ok": False, "error": "no y+ data: " + str(r.get("fatal_error") or r.get("error") or "no output")[:400]}
    lo, hi = planning.yplus_band(gates.wall_treatment_for(ctx))
    values = {"yplus_avg": round_sig(yp["average"], 4), "yplus_min": round_sig(yp["min"], 4),
              "yplus_max": round_sig(yp["max"], 4)}
    ctx.merge_results(**values)
    ctx.merge_metrics(**values)
    return {"ok": True, **values, "at_iteration": yp.get("iter"), "band": [lo, hi],
            "in_band": lo <= yp["average"] <= hi, "source": source,
            "summary": f"y+ average {yp['average']:.1f} (min {yp['min']:.1f}, max {yp['max']:.1f}), "
                       f"band {lo:g}-{hi:g}"}


def tool_extract_cp(ctx):
    case_dir, cond = ctx.case_dir, ctx.conditions()
    cp = ac.surface_cp(case_dir, cond["velocity"])
    if not cp:
        _post_process_fallback(ctx)
        cp = ac.surface_cp(case_dir, cond["velocity"])
        if not cp:
            return {"ok": False, "error": "no sampled surface pressure found in postProcessing/surfacePressure"}
    path = os.path.join(case_dir, "cp.json")
    write_json(path, cp)
    ctx.add_artifact("data", "Surface pressure coefficient", path)
    ctx.merge_metrics(cp_min=round_sig(cp["cp_min"], 4), cp_max=round_sig(cp["cp_max"], 4))
    suction_x = min(cp["upper"] + cp["lower"], key=lambda r: r[1])[0]
    return {"ok": True, "points_upper": len(cp["upper"]), "points_lower": len(cp["lower"]),
            "cp_min": round_sig(cp["cp_min"], 4), "cp_min_x": suction_x, "cp_max": round_sig(cp["cp_max"], 4),
            "at_iteration": cp.get("time"),
            "summary": f"Cp on {len(cp['upper']) + len(cp['lower'])} faces, suction peak {cp['cp_min']:.2f} "
                       f"at x/c {suction_x:.3f}, stagnation {cp['cp_max']:.2f}"}


# ----------------------------------------------------------------- validate --

def _point(p):
    """Normalise one XFoil polar point to lower-case keys."""
    if not p:
        return None
    cl, cd = p.get("CL", p.get("cl")), p.get("CD", p.get("cd"))
    if cl is None or cd is None:
        return {"alpha": p.get("alpha"), "converged": False}
    return {"alpha": p.get("alpha"), "cl": cl, "cd": cd, "cm": p.get("CM", p.get("cm")),
            "ld": (cl / cd) if cd else None, "converged": bool(p.get("converged", True))}


def _polar_points(polar):
    pts = polar.get("points") if isinstance(polar, dict) else polar
    out = []
    for p in pts or []:
        q = _point(p)
        if q and q.get("converged") and q.get("cl") is not None:
            out.append({k: round_sig(q[k], 5) for k in ("alpha", "cl", "cd", "cm", "ld")})
    return sorted(out, key=lambda r: r["alpha"])


def _cp_pairs(side):
    """[[x, cp], ...] from whatever shape the XFoil driver returns for one surface."""
    if isinstance(side, dict):
        return [[round(float(x), 5), round(float(c), 5)] for x, c in zip(side.get("x") or [], side.get("cp") or [])]
    out = []
    for row in side or []:
        if isinstance(row, dict):
            out.append([round(float(row["x"]), 5), round(float(row["cp"]), 5)])
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append([round(float(row[0]), 5), round(float(row[-1] if len(row) == 2 else row[2]), 5)])
    return out


def transition_xtr(transition):
    """'forced' -> 0.05 (both surfaces), 'free' -> None, a number -> that chord fraction."""
    if transition in (None, "", "forced", "tripped", "fixed"):
        return planning.XTR_FORCED, "forced"
    if transition == "free":
        return None, "free"
    try:
        x = float(transition)
    except (TypeError, ValueError):
        raise ValueError("transition must be 'forced', 'free' or a chord fraction between 0 and 1")
    if not (0.0 < x <= 1.0):
        raise ValueError("transition chord fraction must be between 0 and 1")
    return (None, "free") if x >= 1.0 else (x, "forced" if abs(x - planning.XTR_FORCED) < 1e-9 else f"forced@{x:g}")


def _angle_mismatch(ctx):
    """An error sentence when the case's angle of attack is not the angle of the flow
    that was solved (the plan angle was changed after the physics stage ran); else None.
    A reference at one angle must never be compared with a solution at another."""
    solved = gates.solved_angle(ctx)
    planned = ctx.case.get("alpha_deg")
    if not is_finite(solved):
        return "no physics set-up (physics.json) in this case: the boundary-condition stage must run first"
    if not is_finite(planned) or abs(planned - solved) > 0.01:
        return (f"the RANS solution of this case was computed at {solved:g} deg, but the case is now at "
                f"{planned} deg: the case must be re-run from the physics stage "
                "(request_rework('physics', ...)) before it can be compared")
    return None


def _forget_xfoil(case_dir, key, with_cp):
    """Drop a stored XFoil point (and the Cp overlay) so that a failed run cannot leave
    an older one, possibly from another angle, to be used in its place."""
    path = os.path.join(case_dir, "xfoil.json")
    saved = read_json(path) or {}
    if saved.pop(key, None) is not None:
        write_json(path, saved)
    if with_cp:
        _remove(os.path.join(case_dir, "xfoil_cp.json"))


def tool_run_xfoil(ctx, transition="forced", mach=0.0):
    case, cond, case_dir = ctx.case, ctx.conditions(), ctx.case_dir
    try:
        xtr, label = transition_xtr(transition)
        mach = float(mach or 0.0)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    key = f"{label}_m{mach:g}"
    is_reference = label == "forced" and mach == 0.0
    mismatch = _angle_mismatch(ctx)
    if mismatch:
        _forget_xfoil(case_dir, key, is_reference)
        return {"ok": False, "error": mismatch}
    r = xfoil.run_polar(case["naca"], cond["reynolds"], mach=mach, alpha=case["alpha_deg"], xtr=xtr)
    pts = (r or {}).get("points") or []
    p = _point(pts[0]) if pts else None
    if not (r and r.get("ok") and p and p.get("converged")):
        _forget_xfoil(case_dir, key, is_reference)
        return {"ok": False, "converged": False,
                "error": "XFoil did not converge: " + str((r or {}).get("error") or (r or {}).get("warnings") or "")[:400]}
    entry = {"transition": label, "xtr": xtr, "mach": mach, "alpha": case["alpha_deg"],
             "reynolds": cond["reynolds"], "cl": round_sig(p["cl"], 6), "cd": round_sig(p["cd"], 6),
             "cm": round_sig(p["cm"], 6), "ld": round_sig(p["ld"], 5), "converged": True,
             # the shape XFoil ran on: the studio's closed-trailing-edge profile, as meshed
             "geometry": r.get("geometry"), "trailing_edge": r.get("trailing_edge"),
             "at": round(time.time(), 3)}
    path = os.path.join(case_dir, "xfoil.json")
    saved = read_json(path) or {}
    saved[key] = entry
    write_json(path, saved)
    if is_reference:
        cp_path = os.path.join(case_dir, "xfoil_cp.json")
        _remove(cp_path)                       # never leave the overlay of an earlier angle
        try:
            c = xfoil.run_cp(case["naca"], cond["reynolds"], case["alpha_deg"], mach=0.0, xtr=xtr)
            if c and c.get("ok"):
                write_json(cp_path, {"upper": _cp_pairs(c.get("upper")), "lower": _cp_pairs(c.get("lower")),
                                     "cp_min": c.get("cp_min")})
        except Exception:  # noqa: BLE001 - the Cp overlay is optional
            pass
    return {"ok": True, **entry, "is_reference": is_reference,
            "role": ("like-for-like reference for the fully turbulent, incompressible RANS run" if is_reference else
                     "clean-surface (free transition) potential, not used for the verdict" if label == "free" and mach == 0
                     else "compressibility effect at this Mach number, not used for the verdict"),
            "summary": f"XFoil {label} transition, Mach {mach:g}: Cl {p['cl']:.4f}, Cd {p['cd']:.5f}, "
                       f"Cm {p['cm']:.4f}, L/D {p['ld']:.1f}"}


def tool_compare(ctx):
    case, case_dir = ctx.case, ctx.case_dir
    results = case.get("results") or {}
    if not is_finite(results.get("cl")) or not is_finite(results.get("cd")):
        return {"ok": False, "error": "no RANS force coefficients for this case yet"}
    runs = read_json(os.path.join(case_dir, "xfoil.json")) or {}
    ref = runs.get("forced_m0")
    need = "call run_xfoil(transition='forced', mach=0) first"
    if not ref:
        return {"ok": False, "error": "no like-for-like XFoil reference: " + need}
    started = gates.stage_started(ctx, "validate") if ctx.stage == "validate" else None
    if started is not None and (ref.get("at") or 0.0) < started - 1.0:
        # a re-run of the stage must not be judged against what an earlier run left behind
        return {"ok": False, "error": "the stored XFoil reference is from an earlier run of this stage: " + need}
    # Same environment or no comparison: one angle of attack and one Reynolds number.
    mismatch = _angle_mismatch(ctx)
    if mismatch:
        return {"ok": False, "error": mismatch}
    solved, cond = gates.solved_angle(ctx), ctx.conditions()
    if not is_finite(ref.get("alpha")) or abs(ref["alpha"] - solved) > 0.01:
        return {"ok": False, "error": f"the XFoil reference is at {ref.get('alpha')} deg but the RANS solution "
                                      f"is at {solved:g} deg: " + need}
    re_study = cond.get("reynolds")
    if re_study and ref.get("reynolds") is not None and abs(ref["reynolds"] - re_study) > 0.01 * re_study:
        return {"ok": False, "error": f"the XFoil reference is at Re {ref['reynolds']:.4g} but the study is at "
                                      f"Re {re_study:.4g}: " + need}
    deltas = gates.compute_deltas(results, ref, gates.mesh_params(case_dir).get("farfield_chords"))
    verdict = gates.verdict_for(deltas["cl_pct"], deltas["cd_pct"], deltas["cl_abs"], ref["cl"])
    lift_pass, lift_warn = gates.lift_bands(ref["cl"])
    reference = {"transition": f"forced at {planning.XTR_FORCED:g} chord, both surfaces", "alpha": ref["alpha"],
                 "mach": ref["mach"], "cl": ref["cl"], "cd": ref["cd"], "cm": ref["cm"], "ld": ref["ld"],
                 "converged": ref["converged"], "reynolds": ref.get("reynolds"),
                 "trailing_edge": ref.get("trailing_edge")}

    def show(pct_value):
        return "n/a" if pct_value is None else f"{pct_value:+.1f}%"
    # a reference lift of zero has no percentage: show the absolute difference instead
    d_cl = show(deltas["cl_pct"]) if deltas["cl_pct"] is not None else f"{deltas['cl_abs']:+.4f} (absolute)"
    # context points only when they are at the same angle as the reference
    others = {k: {f: v[f] for f in ("transition", "mach", "cl", "cd", "cm", "ld")}
              for k, v in runs.items()
              if k != "forced_m0" and is_finite(v.get("alpha")) and abs(v["alpha"] - ref["alpha"]) <= 0.01}

    def apply(c):
        c["reference"], c["deltas"], c["verdict"] = reference, deltas, verdict
    ctx.store.update_case(ctx.study_id, ctx.case_id, apply)
    ctx.merge_metrics(verdict=verdict, **deltas, xfoil_cl=ref["cl"], xfoil_cd=ref["cd"], xfoil_cm=ref["cm"],
                      other_xfoil_points=others)
    ctx.set_headline(f"{verdict.upper()} · dCl {d_cl} · dCd {show(deltas['cd_pct'])}")
    req = ctx.requirements()
    return {"ok": True, "rans": {k: results.get(k) for k in ("cl", "cd", "cm", "ld")}, "xfoil": reference,
            "deltas": deltas, "verdict": verdict, "other_xfoil_points": others,
            # the mission's optional L/D target at the design Cl: reported, never part of the verdict
            "target_ld": req.get("target_ld") or None,
            "rans_meets_target_ld": reporting.meets_target_ld(req, results.get("ld")),
            "xfoil_meets_target_ld": reporting.meets_target_ld(req, ref.get("ld")),
            "bands": "pass: |dCl| <= 5% and |dCd| <= 25%; warn: |dCl| <= 10% and |dCd| <= 50%; else fail. "
                     "Lift is judged on the absolute difference with a floor, so that a reference lift near "
                     f"zero has a usable band: here pass |Cl - Cl_xfoil| <= {lift_pass:.3g}, warn <= {lift_warn:.3g} "
                     "(5% / 10% of the reference, at least 0.01 / 0.02)",
            "note": "farfield_cd_estimate is the drag the truncated domain adds (about Cl^2 / (4 pi R)); "
                    "RANS drag is expected to sit above XFoil by roughly that much plus numerical diffusion. "
                    "XFoil ran on the same closed-trailing-edge profile the mesh is built on",
            "summary": f"verdict {verdict}: Cl {d_cl}, Cd {show(deltas['cd_pct'])} against XFoil "
                       f"(Cl {ref['cl']:.4f}, Cd {ref['cd']:.5f})"}


# -------------------------------------------------------- study-level tools --

def screen_airfoil(naca, reynolds, design_cl, requirements=None):
    """One XFoil screen: the angle that gives the design Cl (transition forced
    at 5% chord, Mach 0) and the numbers there. Never raises."""
    req = requirements or {}
    try:
        code, m, p, t = ac.naca4(naca)
    except ValueError as e:
        return {"naca": str(naca), "converged": False, "error": str(e)}
    out = {"naca": code, "thickness": t, "max_camber": m, "converged": False}
    try:
        r = xfoil.alpha_for_cl(code, reynolds, design_cl, mach=0.0, xtr=planning.XTR_FORCED)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"XFoil driver failed: {type(e).__name__}: {e}"
        return out
    pt = _point((r or {}).get("point"))
    if not (r and r.get("ok") and pt and pt.get("cl") is not None and r.get("alpha") is not None):
        out["error"] = str((r or {}).get("error") or "XFoil could not reach the design lift coefficient")[:300]
        out["polar"] = _polar_points((r or {}).get("polar"))
        return out
    cl, cd = pt["cl"], pt["cd"]
    out.update({"converged": True, "alpha_design": round_sig(r["alpha"], 4), "cl": round_sig(cl, 5), "cd": round_sig(cd, 5),
                "cm": round_sig(pt.get("cm"), 5), "ld": round_sig(cl / cd, 5),
                "endurance": round_sig(cl ** 1.5 / cd, 5) if cl > 0 else None,
                "polar": _polar_points(r.get("polar"))})
    min_t, max_cm = req.get("min_thickness"), req.get("max_abs_cm")
    out["meets_thickness"] = True if not min_t else t + 1e-9 >= float(min_t)
    out["meets_moment"] = True if (not max_cm or pt.get("cm") is None) else abs(pt["cm"]) <= float(max_cm) + 1e-9
    return out


def screen_airfoils(codes, reynolds, design_cl, requirements):
    """screen_airfoil for several codes, four XFoil runs at a time."""
    with ThreadPoolExecutor(max_workers=min(4, len(codes))) as pool:
        return list(pool.map(lambda c: screen_airfoil(c, reynolds, design_cl, requirements), codes))


def tool_xfoil_screen(ctx, nacas=None, design_cl=None):
    study = ctx.study
    req, cond = study.get("requirements") or {}, study.get("conditions") or {}
    codes = []
    for n in nacas or []:
        try:
            code = ac.naca4(n)[0]
        except ValueError:
            return {"ok": False, "error": f"{n!r} is not a NACA 4-digit code"}
        if code not in codes:
            codes.append(code)
    if not codes:
        return {"ok": False, "error": "give at least one NACA 4-digit code"}
    if len(codes) > 8:
        return {"ok": False, "error": "at most 8 airfoils per screen"}
    target = float(design_cl if design_cl is not None else req["design_cl"])
    started = time.time()
    results = screen_airfoils(codes, cond["reynolds"], target, req)
    ctx.runtime.remember_screen(results, target)
    objective = req.get("objective") or "max_ld"
    target_ld = req.get("target_ld") or None
    table = []
    for r in results:
        row = {k: r.get(k) for k in ("naca", "converged", "alpha_design", "cl", "cd", "cm", "ld", "endurance",
                                     "meets_thickness", "meets_moment", "error") if r.get(k) is not None}
        row["objective_value"] = round_sig(reporting.objective_value(objective, r.get("cl"), r.get("cd")), 5)
        if target_ld:
            # reported as met / not met; it never excludes a candidate
            row["meets_target_ld"] = reporting.meets_target_ld(req, r.get("ld"))
        table.append(row)
    ok_n = sum(1 for r in results if r.get("converged"))
    return {"ok": ok_n > 0, "design_cl": target, "reynolds": cond["reynolds"], "objective": objective,
            "target_ld": target_ld,
            "transition": f"forced at {planning.XTR_FORCED:g} chord (comparable with fully turbulent RANS)", "mach": 0.0,
            "geometry": "closed-trailing-edge NACA profile, the shape the RANS mesh is built on",
            "results": table, "seconds": round(time.time() - started, 1),
            "error": None if ok_n else "XFoil converged for none of the airfoils",
            "summary": f"XFoil screened {len(codes)} airfoils at Cl {target:g}: {ok_n} converged"
                       + (", best L/D " + max((r for r in results if r.get('converged')),
                                              key=lambda r: r['ld'])['naca'] if ok_n else "")}


def tool_propose_candidates(ctx, candidates=None, top=None):
    return ctx.runtime.propose_candidates(candidates or [], top or [])


def tool_get_defaults(ctx):
    return ctx.runtime.get_defaults()


def tool_write_plan(ctx, plan=None):
    return ctx.runtime.write_plan(plan or {})


def tool_reply(ctx, text=""):
    return ctx.runtime.reply(text)


def tool_update_plan(ctx, case_id=None, stage=None, params=None):
    return ctx.runtime.update_plan(case_id, stage, params or {})


def tool_rerun_from(ctx, case_id=None, stage=None, instruction=""):
    return ctx.runtime.rerun_from(case_id, stage, instruction)


def tool_set_mode(ctx, mode=None):
    return ctx.runtime.set_mode(mode)


def tool_get_results(ctx):
    return ctx.runtime.get_results()


def tool_write_report(ctx, markdown="", recommendation=""):
    return ctx.runtime.write_report(markdown, recommendation)


# ------------------------------------------------------------------ registry --

def _fn(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False}}}


_NUMBER = {"type": "number"}
_INT = {"type": "integer"}
_STR = {"type": "string"}
_STAGE_ENUM = {"type": "string", "enum": planning.STAGE_KEYS}


# name -> (handler, phase kind, schema).  kind: gather | act | verify | None
TOOLS = {
    "get_context": (tool_get_context, "gather", _fn(
        "get_context", "Requirements, flow conditions, this case, your plan section, the hand-offs of "
        "earlier stages and the settings currently written in the case.")),
    "note": (tool_note, None, _fn(
        "note", "Record a short note for the engineer in the trace (a finding or the reason for a choice).",
        {"text": _STR}, ["text"])),
    "complete_stage": (tool_complete_stage, "verify", _fn(
        "complete_stage", "Hand the stage over. The deterministic gate runs; if it fails you get the failed "
        "checks back and must fix them and call this again.",
        {"summary": {"type": "string", "description": "Two or three sentences: what was done and the key numbers."},
         "decisions": {"type": "array", "description": "The choices made in this stage.",
                       "items": {"type": "object",
                                 "properties": {"param": _STR, "value": {"type": ["string", "number", "boolean"]},
                                                "reason": _STR,
                                                "cites": {"type": "array", "items": _STR, "maxItems": 3,
                                                          "description": "Reference ids from 'References you may "
                                                          "cite' that support this value. Ids only. Leave out when "
                                                          "you apply the plan value unchanged."}},
                                 "required": ["param", "value", "reason"], "additionalProperties": False}}},
        ["summary"])),
    "request_rework": (tool_request_rework, None, _fn(
        "request_rework", "Send the case back to an earlier stage when the fault lies there (at most 2 per case). "
        "Your stage ends; the pipeline restarts at that stage with your reason.",
        {"stage": _STAGE_ENUM, "reason": _STR}, ["stage", "reason"])),

    "generate_geometry": (tool_generate_geometry, "act", _fn(
        "generate_geometry", "Write the NACA 4-digit profile (closed trailing edge, unit chord) as the OBJ "
        "surface the mesher projects onto, plus the profile coordinates.",
        {"naca": {"type": "string", "description": "4-digit code, e.g. 4412. Must be this case's airfoil."}})),
    "inspect_geometry": (tool_inspect_geometry, "verify", _fn(
        "inspect_geometry", "Measure the written profile: thickness against the code, trailing-edge closure, "
        "leading-edge radius, OBJ vertex count.")),

    "estimate_first_cell": (tool_estimate_first_cell, "gather", _fn(
        "estimate_first_cell", "First-cell height (in chords) for a target y+ at this Reynolds number "
        "(flat-plate estimate; y+ is evaluated at the cell centre, half the height).",
        {"target_yplus": _NUMBER})),
    "set_mesh_params": (tool_set_mesh_params, "act", _fn(
        "set_mesh_params", "Write the blockMesh set-up for the C-mesh. Omitted values come from the plan. "
        "Returns the cell count, growth ratios and predicted y+. More than 80,000 cells is refused. "
        "An existing mesh is not rebuilt: call build_mesh and check_mesh afterwards.",
        {"farfield_chords": {"type": "number", "description": "Far-field radius in chords (30 is the stable maximum)."},
         "wake_chords": {"type": "number", "description": "Outlet distance behind the trailing edge in chords."},
         "n_normal": {"type": "integer", "description": "Cells from the wall to the far field."},
         "n_leading": {"type": "integer", "description": "Cells along each surface, leading edge to 30% chord."},
         "n_aft": {"type": "integer", "description": "Cells along each surface, 30% chord to the trailing edge."},
         "n_wake": {"type": "integer", "description": "Cells along the wake."},
         "first_cell_height": {"type": "number", "description": "Wall-normal height of the first cell in chords."}})),
    "build_mesh": (tool_build_mesh, "act", _fn(
        "build_mesh", "Run blockMesh, flatten to 2D and extrude one cell thick. Removes any earlier results.")),
    "check_mesh": (tool_check_mesh, "verify", _fn(
        "check_mesh", "Run checkMesh: cells, non-orthogonality, skewness, aspect ratio, predicted y+. "
        "Refused when the mesh on disk was not built from the current set-up (call build_mesh).")),
    "render_view": (tool_render_view, "verify", _fn(
        "render_view", "Render an image of the mesh or of a result field for the engineer (ParaView, offscreen).",
        {"mode": {"type": "string", "enum": ["mesh", "field", "patches"]},
         "field": {"type": "string", "description": "Field for mode 'field': U, p, nut, nuTilda ..."},
         "zoom": {"type": "string", "description": "'auto', 'full' (whole domain), 'airfoil', 'leading_edge', "
                                                   "'trailing_edge', 'wake', or a box 'x0,x1,z0,z1' in chords, "
                                                   "e.g. '-0.5,1.5,-0.6,0.6'."}})),

    "set_physics": (tool_set_physics, "act", _fn(
        "set_physics", "Write the transport and turbulence properties and the initial/boundary fields (0.orig). "
        "Velocity, angle and viscosity come from the flight condition. Omitted values come from the plan.",
        {"turbulence_model": {"type": "string", "enum": ["SpalartAllmaras", "kOmegaSST"]},
         "wall_treatment": {"type": "string", "enum": ["wallFunction", "resolved"]},
         "nut_ratio": {"type": "number", "description": "Free-stream eddy-viscosity ratio, 0.01 to 100: nuTilda/nu "
                                                        "for SA; nut/nu for SST, where it sets omega = k / (nut_ratio nu)."},
         "turbulence_intensity": {"type": "number", "description": "Free-stream turbulence intensity as a fraction, "
                                                                   "1e-5 to 0.05 (0.001 is 0.1%); used by SST only."},
         "farfield_bc": {"type": "string", "enum": ["fixedInlet", "freestream"]}})),
    "inspect_boundary": (tool_inspect_boundary, "verify", _fn(
        "inspect_boundary", "Read back what is written: fields in 0.orig and their boundary types, mesh patches, "
        "velocity magnitude, angle and the Reynolds number implied by nu.")),

    "set_numerics": (tool_set_numerics, "act", _fn(
        "set_numerics", "Write controlDict (with force coefficients, y+ and surface-pressure sampling), fvSchemes "
        "and fvSolution. Omitted values come from the plan (or stay as they are when called during the solve stage). "
        "The force axes are set to the angle of attack the boundary-condition stage wrote.",
        {"iterations": {"type": "integer", "description": "Total run length (the solver-setup gate accepts 300 to "
                                                          "6000; the ceiling is 12000)."},
         "write_interval": {"type": "integer", "description": "Iterations between written states, 10 or more."},
         "convection_scheme": {"type": "string", "enum": ["linearUpwind", "upwind"]},
         "relax_p": _NUMBER, "relax_U": _NUMBER, "relax_turbulence": _NUMBER,
         "residual_p": {"type": "number", "description": "Residual target for p, 1e-9 to 1e-3 (default 1e-6). The "
                                                         "solver stops when every residual is below its target."},
         "residual_U": {"type": "number", "description": "Residual target for U (default 1e-6)."},
         "residual_turbulence": {"type": "number", "description": "Residual target for the turbulence equations (default 1e-6)."},
         "n_non_orthogonal_correctors": {"type": "integer", "description": "0 to 3."},
         "consistent": {"type": "boolean", "description": "SIMPLEC (use with higher relaxation factors)."}})),
    "inspect_numerics": (tool_inspect_numerics, "verify", _fn(
        "inspect_numerics", "Read back the solver set-up: run length, schemes, function objects, and the "
        "force-coefficient set-up (lift and drag axes with their angle, reference speed, moment centre) "
        "against the angle of attack and flight speed.")),

    "start_solver": (tool_start_solver, "act", _fn(
        "start_solver", "Start simpleFoam in the background (from the initial fields, or from the latest written "
        "time if results exist). Returns immediately; follow with wait_solver.")),
    "wait_solver": (tool_wait_solver, "act", _fn(
        "wait_solver", "Wait for the solver, at most max_wait_s seconds (default 30, max 120). Returns early when "
        "it ends or the engineer sends a message. Gives iteration, residuals, Cl, Cd and their drift over the "
        "last 200 iterations.",
        {"max_wait_s": _NUMBER})),
    "stop_solver": (tool_stop_solver, "act", _fn(
        "stop_solver", "Stop the running solver; it writes the current state first so the results stay usable.")),
    "extend_run": (tool_extend_run, "act", _fn(
        "extend_run", "Add iterations to the run. A running solver just continues; a finished one is restarted "
        "from its latest time.",
        {"iterations": _INT}, ["iterations"])),
    "reset_fields": (tool_reset_fields, "act", _fn(
        "reset_fields", "Throw away the results and restore the initial fields (after a diverged run, before "
        "starting again with changed numerics).")),

    "compute_forces": (tool_compute_forces, "act", _fn(
        "compute_forces", "Final Cl, Cd, Cm (quarter chord), L/D and how much they still drift, plus the final residuals.")),
    "compute_yplus": (tool_compute_yplus, "act", _fn(
        "compute_yplus", "Measured y+ on the airfoil: average, minimum, maximum, against the band for the wall treatment.")),
    "extract_cp": (tool_extract_cp, "act", _fn(
        "extract_cp", "Extract the surface pressure coefficient along the chord (upper and lower) for the Cp chart.")),

    "run_xfoil": (tool_run_xfoil, "act", _fn(
        "run_xfoil", "Run XFoil for this airfoil at the same Reynolds number and angle, on the same "
        "closed-trailing-edge profile the mesh is built on. transition='forced' (tripped at 5% chord) with "
        "mach=0 is the like-for-like reference; 'free' and the flight Mach number are context only.",
        {"transition": {"type": "string", "enum": ["forced", "free"]}, "mach": _NUMBER})),
    "compare": (tool_compare, "verify", _fn(
        "compare", "Compare the RANS coefficients with the XFoil reference: deviations, the far-field drag "
        "estimate and the verdict (pass / warn / fail). Refused unless run_xfoil(forced, mach 0) ran in this "
        "run of the stage and the reference, the case and the RANS solution are at one angle of attack.")),

    "xfoil_screen": (tool_xfoil_screen, "gather", _fn(
        "xfoil_screen", "Screen NACA 4-digit airfoils in XFoil at the study's Reynolds number: the angle that "
        "gives the design lift coefficient and Cl, Cd, Cm, L/D there (transition forced at 5% chord, Mach 0, "
        "on the closed-trailing-edge profile the RANS mesh uses).",
        {"nacas": {"type": "array", "items": _STR, "description": "Up to 8 codes, e.g. ['2412', '4412']."},
         "design_cl": _NUMBER}, ["nacas"])),
    "propose_candidates": (tool_propose_candidates, "act", _fn(
        "propose_candidates", "Present the candidates to the engineer. Every candidate must have been screened.",
        {"candidates": {"type": "array", "items": {
            "type": "object", "properties": {"naca": _STR, "rationale": _STR,
                                             "rank": {"type": "integer", "description": "Your ranking, 1 = best (the "
                                                      "list shown to the engineer is ordered by the XFoil objective)."}},
            "required": ["naca", "rationale"], "additionalProperties": False}},
         "top": {"type": "array", "items": _STR, "description": "The two codes you recommend running."}},
        ["candidates", "top"])),
    "get_defaults": (tool_get_defaults, "gather", _fn(
        "get_defaults", "The default plan for the selected airfoils (verified stable mesh, physics and numerics) "
        "with the allowed values of every setting, the default citation of every setting (default_cites) and "
        "the reference catalogue (references). Start from this.")),
    "write_plan": (tool_write_plan, "act", _fn(
        "write_plan", "Submit the plan: {summary, assumptions[], cases[{naca, stages{geometry, mesh, physics, "
        "numerics, solve, post, validate}}]}. Each stage is a flat object of settings plus 'reasons' "
        "(setting -> one sentence), 'cites' (setting -> list of reference ids from get_defaults().references, "
        "only for values you change) and 'checks' (list of sentences). Omitted settings take the defaults.",
        {"plan": {"type": "object"}}, ["plan"])),
    "reply": (tool_reply, "act", _fn(
        "reply", "Answer the engineer in the chat.", {"text": _STR}, ["text"])),
    "update_plan": (tool_update_plan, "act", _fn(
        "update_plan", "Change plan values for one case and stage. Takes effect when that stage next runs "
        "(call rerun_from if it has already run). params holds that stage's setting names, e.g. "
        "{\"iterations\": 1600} for numerics; the angle of attack is {\"alpha_deg\": 4.0} under stage physics. "
        "A changed alpha_deg on a case whose physics stage has already run sends the case back to physics by "
        "itself: read takes_effect in the result. A case whose stage agent received the engineer's message "
        "directly is refused (ok false).",
        {"case_id": _STR, "stage": _STAGE_ENUM, "params": {"type": "object"}}, ["case_id", "stage", "params"])),
    "rerun_from": (tool_rerun_from, "act", _fn(
        "rerun_from", "Re-run a case from a stage: that stage and every later one are redone. The result has "
        "rerun true when that happens. rerun false means nothing was re-run: the stage has not run yet and is "
        "waiting for the engineer's approval (the instruction is kept for its first run).",
        {"case_id": _STR, "stage": _STAGE_ENUM,
         "instruction": {"type": "string", "description": "What the stage agent should do differently."}},
        ["case_id", "stage"])),
    "set_mode": (tool_set_mode, "act", _fn(
        "set_mode", "Switch the run mode: 'auto' runs every stage, 'gate' waits for approval before each stage.",
        {"mode": {"type": "string", "enum": ["auto", "gate"]}}, ["mode"])),
    "get_results": (tool_get_results, "gather", _fn(
        "get_results", "Requirements, conditions and, per airfoil, the RANS results, the XFoil reference, "
        "the deviations, the verdict and the value of the objective.")),
    "write_report": (tool_write_report, "act", _fn(
        "write_report", "Submit the final report.",
        {"markdown": {"type": "string", "description": "The report in Markdown."},
         "recommendation": {"type": "string", "description": "One sentence: which airfoil and why."}},
        ["markdown", "recommendation"])),
}

COMMON_TOOLS = ["get_context", "note", "complete_stage"]
STAGE_TOOLS = {
    "geometry": ["generate_geometry", "inspect_geometry"],
    "mesh": ["estimate_first_cell", "set_mesh_params", "build_mesh", "check_mesh", "render_view"],
    "physics": ["set_physics", "inspect_boundary"],
    "numerics": ["set_numerics", "inspect_numerics"],
    "solve": ["start_solver", "wait_solver", "stop_solver", "extend_run", "set_numerics", "reset_fields",
              "request_rework"],
    "post": ["compute_forces", "compute_yplus", "extract_cp", "render_view", "request_rework"],
    "validate": ["run_xfoil", "compare", "request_rework"],
}
STUDY_TOOLS = {
    "airfoil-advisor.md": ["xfoil_screen", "propose_candidates"],
    "planner.md": ["get_defaults", "estimate_first_cell", "write_plan"],
    "steering.md": ["reply", "update_plan", "rerun_from", "set_mode"],
    "reporter.md": ["get_results", "write_report"],
}


def tool_kind(name):
    return TOOLS[name][1] if name in TOOLS else None


def tool_schemas(names):
    return [TOOLS[n][2] for n in names if n in TOOLS]


def execute_tool(ctx, name, args, allowed=None):
    """Run one tool. Always returns a dict with `ok` and `summary`."""
    if name not in TOOLS:
        return {"ok": False, "error": f"unknown tool {name!r}", "available": sorted(allowed or TOOLS),
                "summary": f"unknown tool {name}"}
    if allowed is not None and name not in allowed:
        return {"ok": False, "error": f"tool {name!r} is not available to this agent",
                "available": sorted(allowed), "summary": f"{name} is not allowed here"}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"could not parse tool arguments: {e}", "summary": "bad arguments"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "tool arguments must be a JSON object", "summary": "bad arguments"}
    # Models sometimes send an object or array argument as a JSON string
    # (seen with write_plan): decode it rather than bounce the call.
    props = TOOLS[name][2]["function"]["parameters"].get("properties") or {}
    for key, value in list(args.items()):
        if isinstance(value, str) and (props.get(key) or {}).get("type") in ("object", "array"):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                # Also seen: a complete object followed by a few surplus closing brackets.
                try:
                    decoded, end = json.JSONDecoder().raw_decode(value.lstrip())
                    if value.lstrip()[end:].strip(" \t\r\n]}"):
                        raise json.JSONDecodeError("text after the value", value, end)
                except json.JSONDecodeError as e:
                    # Say what is wrong: "must be an object" alone sends the model round in circles.
                    kind = props[key]["type"]
                    return {"ok": False, "summary": f"failed: {key} was sent as a string that is not valid JSON",
                            "error": f"{key} was sent as a quoted string, and the string is not valid JSON "
                                     f"({e.msg} at character {e.pos} of {len(value)}). Send {key} as a JSON {kind}, "
                                     "not as a string, and keep it compact: leave out what you do not change."}
            if isinstance(decoded, (dict, list)):
                args[key] = decoded
    try:
        result = TOOLS[name][0](ctx, **args)
    except TypeError as e:
        result = {"ok": False, "error": f"bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001 - surface anything to the caller
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(result, dict):
        result = {"ok": False, "error": f"{name} returned no result"}
    result.setdefault("ok", True)
    if not result.get("summary"):
        result["summary"] = "ok" if result["ok"] else "failed: " + " ".join(str(result.get("error", "")).split())[:200]
    elif not result["ok"] and result.get("error") and "fail" not in result["summary"]:
        result["summary"] = "failed: " + " ".join(str(result["error"]).split())[:200]
    return result


def result_text(result):
    """Tool result as the text an agent reads."""
    text = json.dumps(result, indent=1, default=str)
    if len(text) > MAX_TOOL_CHARS:
        # Before anything is cut, drop the indentation: get_defaults for four airfoils with the
        # reference catalogue is about 42,000 characters indented and 37,000 without.
        text = json.dumps(result, default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + f"\n... [truncated {len(text) - MAX_TOOL_CHARS} characters]"
    return text
