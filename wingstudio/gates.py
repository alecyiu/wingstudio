"""Gates: fixed checks at the end of each stage.

A gate is a pure function of plain data that returns {passed, checks[{name, value, limit,
ok}]}; `collect_gate_input` reads the case directory to build that data and `run_gate` joins
the two."""
import calendar
import math
import os
import re
import time

from foamtools.common import NUM, is_finite, read_json, read_text, round_sig

from .cfd import case as ac
from .cfd import solver
from .study import planning

# --------------------------------------------------------------------- bands --

def lift_bands(cl_ref):
    """(pass, warn) limits on |Cl - Cl_ref|: 5% and 10% of the reference, but never
    less than 0.01 and 0.02, so that a reference lift at or near zero has a usable band."""
    ref = abs(cl_ref) if is_finite(cl_ref) else 0.0
    return max(0.05 * ref, 0.01), max(0.10 * ref, 0.02)


def verdict_for(cl_pct, cd_pct, cl_abs=None, cl_ref=None):
    """pass / warn / fail from the lift and drag deviation. Lift is judged on the
    absolute difference against lift_bands(cl_ref) when both are given (the same as
    5% / 10% for |Cl_ref| >= 0.2), else on the percentage."""
    if not is_finite(cd_pct):
        return "fail"
    b = abs(cd_pct)
    if is_finite(cl_abs) and is_finite(cl_ref):
        lift_pass, lift_warn = lift_bands(cl_ref)
        a_pass, a_warn = abs(cl_abs) <= lift_pass + 1e-12, abs(cl_abs) <= lift_warn + 1e-12
    elif is_finite(cl_pct):
        a_pass, a_warn = abs(cl_pct) <= 5.0, abs(cl_pct) <= 10.0
    else:
        return "fail"
    if a_pass and b <= 25.0:
        return "pass"
    if a_warn and b <= 50.0:
        return "warn"
    return "fail"


# --------------------------------------------------------------------- gates --

def _check(name, value, limit, ok):
    return {"name": name, "value": round_sig(value) if not isinstance(value, (str, bool)) else value,
            "limit": limit, "ok": bool(ok)}


def _gate(checks):
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def gate_geometry(d):
    gap = d.get("trailing_edge_gap")
    t_code, t_meas = d.get("thickness_code"), d.get("thickness_measured")
    err = abs(t_meas - t_code) / t_code * 100.0 if (is_finite(t_meas) and t_code) else None
    return _gate([
        _check("profile closed (trailing-edge gap, chords)", gap, "< 1e-4",
               is_finite(gap) and gap < 1e-4),
        _check("thickness error vs NACA code (%)", err, "<= 1", err is not None and err <= 1.0),
        _check("OBJ surface written (vertices)", d.get("obj_vertices") or 0, "> 100",
               bool(d.get("obj_exists")) and (d.get("obj_vertices") or 0) > 100),
    ])


def _fresh_check(d, key, name, limit):
    """A check that the stage's own work was done in this run of the stage. `d[key]` is
    True / False, or None when the start of the run is not known (no engine attached):
    then there is nothing to judge and no check."""
    if d.get(key) is None:
        return []
    return [_check(name, "yes" if d[key] else "no: only what an earlier run left behind", limit, bool(d[key]))]


def gate_mesh(d):
    lo, hi = planning.yplus_band(d.get("wall_treatment") or "wallFunction")
    yp = d.get("predicted_yplus")
    cells, no, sk = d.get("cells"), d.get("max_non_orthogonality"), d.get("max_skewness")
    built = d.get("mesh_matches_setup", True)       # False: set_mesh_params changed the set-up after build_mesh
    return _gate([
        _check("mesh built from the current set-up", "yes" if built else "no: call build_mesh, then check_mesh",
               "build_mesh ran after the last set_mesh_params", built),
        _check("checkMesh ran without a fatal error", "fatal" if d.get("fatal_error") else
               ("ok" if cells else "not run"), "no fatal error",
               not d.get("fatal_error") and bool(cells)),
        _check("max non-orthogonality (deg)", no, "< 70", is_finite(no) and no < 70.0),
        _check("max skewness", sk, "< 4", is_finite(sk) and sk < 4.0),
        _check("cells", cells, f"<= {planning.MAX_CELLS}", bool(cells) and cells <= planning.MAX_CELLS),
        _check("predicted y+ of first cell centre", yp, f"{lo:g} to {hi:g}",
               is_finite(yp) and lo <= yp <= hi),
    ] + _fresh_check(d, "checked_this_run", "check_mesh ran in this run of the stage", "yes"))


def required_fields(turbulence_model):
    return ["U", "p", "nut"] + (["nuTilda"] if turbulence_model == "SpalartAllmaras" else ["k", "omega"])


def gate_physics(d):
    need = required_fields(d.get("turbulence_model"))
    missing = [f for f in need if f not in (d.get("fields_present") or [])]
    u, u0 = d.get("velocity_file"), d.get("velocity_expected")
    re_, re0 = d.get("reynolds_file"), d.get("reynolds_expected")
    a, a0 = d.get("alpha_file"), d.get("alpha_expected")
    u_err = abs(u - u0) / u0 * 100.0 if (is_finite(u) and u0) else None
    re_err = abs(re_ - re0) / re0 * 100.0 if (is_finite(re_) and re0) else None
    bc, far = d.get("farfield_bc"), d.get("farfield_chords")
    bc_ok = bc == "fixedInlet" or (bc == "freestream" and is_finite(far) and far <= 30.0)
    return _gate([
        _check("fields for the turbulence model in 0.orig",
               "all present" if not missing else "missing " + ", ".join(missing),
               ", ".join(need), not missing and bool(d.get("turbulence_model"))),
        _check("velocity magnitude error (%)", u_err, "<= 1", u_err is not None and u_err <= 1.0),
        _check("Reynolds number error (%)", re_err, "<= 1", re_err is not None and re_err <= 1.0),
        _check("angle of attack error (deg)", abs(a - a0) if (is_finite(a) and is_finite(a0)) else None,
               "<= 0.05", is_finite(a) and is_finite(a0) and abs(a - a0) <= 0.05),
        _check("far-field boundary condition", bc or "not set",
               "fixedInlet (freestream only for a far field <= 30 chords)", bc_ok),
    ] + _fresh_check(d, "written_this_run", "set_physics ran in this run of the stage", "yes"))


def _force_axes_checks(d):
    """The force coefficients must be projected on axes at the angle of the flow that is
    solved, and made dimensionless with its speed. No checks when the gate input carries
    no force set-up (`force_axes_angle` absent)."""
    if "force_axes_angle" not in d:
        return []
    a, a0 = d.get("force_axes_angle"), d.get("alpha_expected")
    u, u0 = d.get("force_speed"), d.get("velocity_expected")
    a_err = abs(a - a0) if (is_finite(a) and is_finite(a0)) else None
    u_err = abs(u - u0) / u0 * 100.0 if (is_finite(u) and u0) else None
    return [
        _check("lift and drag axes against the angle of attack (deg)", a_err, "<= 0.05",
               a_err is not None and a_err <= 0.05),
        _check("reference speed of the force coefficients, error (%)", u_err, "<= 1",
               u_err is not None and u_err <= 1.0),
    ]


def gate_numerics(d):
    files = d.get("files") or {}
    missing = [n for n in ("controlDict", "fvSchemes", "fvSolution") if not files.get(n)]
    it = d.get("iterations")
    return _gate([
        _check("solver dictionaries written", "all present" if not missing else "missing " + ", ".join(missing),
               "controlDict, fvSchemes, fvSolution", not missing),
        _check("iterations", it, "300 to 6000", is_finite(it) and 300 <= it <= 6000),
    ] + _force_axes_checks(d)
      + _fresh_check(d, "written_this_run", "set_numerics ran in this run of the stage", "yes"))


def gate_solve(d):
    cl_d, cd_d = d.get("cl_drift"), d.get("cd_drift")
    return _gate([
        _check("no fatal solver error", d.get("fatal_error") or d.get("solver_state") or "not run",
               "finished without sigFpe / FOAM FATAL / nan",
               not d.get("fatal_error") and d.get("solver_state") in ("finished", "stopped")),
        _check("force history points", d.get("force_points") or 0, ">= 100",
               (d.get("force_points") or 0) >= 100),
        _check("Cl drift over last 200 iterations", cl_d, "< 1e-3", is_finite(cl_d) and cl_d < 1e-3),
        _check("Cd drift over last 200 iterations", cd_d, "< 1e-4", is_finite(cd_d) and cd_d < 1e-4),
        _check("forces converged", bool(d.get("forces_converged")), "true", bool(d.get("forces_converged"))),
    ] + _force_axes_checks(d)
      + _fresh_check(d, "ran_this_run", "the solver ran in this run of the stage",
                     "yes (extend_run, or reset_fields and start_solver)"))


def gate_post(d):
    lo, hi = planning.yplus_band(d.get("wall_treatment") or "wallFunction")
    cl, cd, yp = d.get("cl"), d.get("cd"), d.get("yplus_avg")
    n = d.get("cp_points") or 0
    return _gate([
        _check("Cl finite", cl, "finite", is_finite(cl)),
        _check("Cd finite and positive", cd, "> 0", is_finite(cd) and cd > 0),
        _check("measured average y+", yp, f"{lo:g} to {hi:g}", is_finite(yp) and lo <= yp <= hi),
        _check("Cp points extracted", n, ">= 20", n >= 20),
    ])


def gate_validate(d):
    verdict = d.get("verdict")
    reworked = (d.get("rework_loops") or 0) >= 1
    verdict_ok = verdict in ("pass", "warn") or (verdict == "fail" and reworked)
    cl_abs, cl_ref = d.get("cl_abs"), d.get("cl_ref")
    if is_finite(cl_abs) and is_finite(cl_ref):
        # absolute band: 5% / 10% of the reference lift, at least 0.01 / 0.02
        lift_pass, lift_warn = lift_bands(cl_ref)
        lift = _check("Cl deviation from XFoil (absolute)", cl_abs,
                      f"pass <= {lift_pass:.3g}, warn <= {lift_warn:.3g} (5% / 10% of the reference, "
                      "at least 0.01 / 0.02)", abs(cl_abs) <= lift_warn + 1e-12 or reworked)
    else:
        lift = _check("Cl deviation from XFoil (%)", d.get("cl_pct"), "pass <= 5, warn <= 10",
                      is_finite(d.get("cl_pct")) and (abs(d["cl_pct"]) <= 10.0 or reworked))
    checks = [
        _check("XFoil reference converged", bool(d.get("xfoil_converged")), "true", bool(d.get("xfoil_converged"))),
        lift,
        _check("Cd deviation from XFoil (%)", d.get("cd_pct"), "pass <= 25, warn <= 50",
               is_finite(d.get("cd_pct")) and (abs(d["cd_pct"]) <= 50.0 or reworked)),
        _check("verdict", verdict or "not compared",
               "pass or warn (fail is accepted only after one rework)", verdict_ok),
    ]
    if "alpha_solved" in d:
        # reference, case and the flow that was solved must all be at one angle
        a_ref, a_case, a_solved = d.get("alpha_reference"), d.get("alpha_case"), d.get("alpha_solved")
        same = all(is_finite(x) for x in (a_ref, a_case, a_solved)) and \
            max(abs(a_ref - a_solved), abs(a_case - a_solved)) <= 0.01
        checks.append(_check(
            "XFoil reference and RANS at the same angle of attack",
            "yes" if same else f"reference {a_ref}, plan {a_case}, solved {a_solved} deg",
            "reference, plan and solved angle within 0.01 deg", same))
    return _gate(checks)


GATES = {"geometry": gate_geometry, "mesh": gate_mesh, "physics": gate_physics,
         "numerics": gate_numerics, "solve": gate_solve, "post": gate_post,
         "validate": gate_validate}


def geometry_facts(case_dir, naca):
    info = read_json(os.path.join(case_dir, "geometry.json")) or {}
    upper, lower = info.get("upper") or [], info.get("lower") or []
    out = {"thickness_code": ac.naca4(naca)[3], "obj_exists": False, "obj_vertices": 0,
           "trailing_edge_gap": None, "thickness_measured": None, "points_per_side": len(upper)}
    if upper and len(upper) == len(lower):
        out["trailing_edge_gap"] = math.dist(upper[-1], lower[-1])
        # upper[i] and lower[i] sit either side of the same camber-line station,
        # so their distance is the local thickness.
        out["thickness_measured"] = max(math.dist(u, l) for u, l in zip(upper, lower))
    obj = os.path.join(case_dir, "constant", "geometry", f"NACA{ac.naca4(naca)[0]}.obj")
    if os.path.exists(obj):
        out["obj_exists"] = True
        with open(obj) as f:
            out["obj_vertices"] = sum(1 for line in f if line.startswith("v "))
    return out


def physics_facts(case_dir):
    zero = os.path.join(case_dir, "0.orig")
    fields = sorted(os.listdir(zero)) if os.path.isdir(zero) else []
    out = {"fields_present": fields, "velocity_file": None, "reynolds_file": None, "alpha_file": None}
    m = re.search(rf"internalField\s+uniform\s+\(\s*({NUM})\s+({NUM})\s+({NUM})\s*\)",
                  read_text(os.path.join(zero, "U")))
    if m:
        ux, _, uz = (float(g) for g in m.groups())
        out["velocity_file"] = math.hypot(ux, uz)
        out["alpha_file"] = math.degrees(math.atan2(uz, ux))
    m = re.search(rf"^nu\s+({NUM});", read_text(os.path.join(case_dir, "constant", "transportProperties")), re.M)
    if m and out["velocity_file"]:
        out["reynolds_file"] = out["velocity_file"] / float(m.group(1))     # unit chord
    m = re.search(r"RASModel\s+(\w+);", read_text(os.path.join(case_dir, "constant", "turbulenceProperties")))
    out["turbulence_model"] = m.group(1) if m else None
    saved = read_json(os.path.join(case_dir, "physics.json")) or {}
    out["farfield_bc"] = saved.get("farfield_bc")
    out["wall_treatment"] = saved.get("wall_treatment")
    return out


def stage_record(ctx, stage):
    for s in (ctx.case or {}).get("stages") or []:
        if s.get("key") == stage:
            return s
    return {}


def wall_treatment_for(ctx):
    """The wall treatment in force. Once the physics stage has passed it is what
    set_physics wrote. Before that (the mesh stage, also when it is re-run) it is what
    the plan asks for: physics.json is then the previous run's and will be rewritten."""
    saved = (read_json(os.path.join(ctx.case_dir, "physics.json")) or {}).get("wall_treatment")
    planned = ctx.plan_stage("physics").get("wall_treatment")
    if stage_record(ctx, "physics").get("status") == "passed":
        return saved or planned or planning.DEFAULT_PHYSICS["wall_treatment"]
    return planned or saved or planning.DEFAULT_PHYSICS["wall_treatment"]


def stage_started(ctx, stage):
    """Epoch seconds at which the current run of `stage` began; None when unknown."""
    text = stage_record(ctx, stage).get("started_at")
    try:
        t = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None
    m = re.match(r"\.(\d+)", text[19:])
    return t + (float("0." + m.group(1)) if m else 0.0)


def written_this_run(ctx, stage, path):
    """True when `path` was (re)written during the current run of `stage`: a gate must
    judge the work of this run, not a file an earlier run left behind. None when the
    start of the run is not known."""
    started = stage_started(ctx, stage)
    if started is None:
        return None
    try:
        return os.path.getmtime(path) >= started - 1.0
    except OSError:
        return False


def force_setup(case_dir):
    """The forceCoeffs entries of controlDict: axes, their angle, reference values."""
    text = read_text(os.path.join(case_dir, "system", "controlDict"))
    vec = rf"\(\s*({NUM})\s+({NUM})\s+({NUM})\s*\)"
    out = {"lift_dir": None, "drag_dir": None, "cofr": None, "mag_u_inf": None, "l_ref": None,
           "a_ref": None, "force_axes_angle_deg": None}
    for key, name in (("lift_dir", "liftDir"), ("drag_dir", "dragDir"), ("cofr", "CofR")):
        m = re.search(rf"^\s*{name}\s+{vec};", text, re.M)
        if m:
            out[key] = [float(g) for g in m.groups()]
    for key, name in (("mag_u_inf", "magUInf"), ("l_ref", "lRef"), ("a_ref", "Aref")):
        m = re.search(rf"^\s*{name}\s+({NUM});", text, re.M)
        if m:
            out[key] = float(m.group(1))
    lift, drag = out["lift_dir"], out["drag_dir"]
    if lift and drag:
        a_drag = math.degrees(math.atan2(drag[2], drag[0]))       # drag = (cos a, 0, sin a)
        a_lift = math.degrees(math.atan2(-lift[0], lift[2]))      # lift = (-sin a, 0, cos a)
        # one angle only when the two axes agree (they are written together)
        out["force_axes_angle_deg"] = a_drag if abs(a_drag - a_lift) <= 1e-3 else None
    return out


def solved_angle(ctx):
    """Angle of attack of the flow set_physics wrote (None before it)."""
    return (read_json(os.path.join(ctx.case_dir, "physics.json")) or {}).get("alpha_deg")


def mesh_params(case_dir):
    """The mesh set-up set_mesh_params wrote ({} before it)."""
    return (read_json(os.path.join(case_dir, "mesh_plan.json")) or {}).get("params") or {}


def _force_gate_input(ctx):
    alpha = solved_angle(ctx)
    setup = force_setup(ctx.case_dir)
    return {"force_axes_angle": setup["force_axes_angle_deg"], "force_speed": setup["mag_u_inf"],
            "alpha_expected": alpha if is_finite(alpha) else ctx.case.get("alpha_deg"),
            "velocity_expected": ctx.conditions().get("velocity")}


def collect_gate_input(stage, ctx):
    """Read the case directory (and the case record) into the plain dict the gate needs."""
    case, case_dir = ctx.case, ctx.case_dir
    cond = ctx.conditions()
    if stage == "geometry":
        return geometry_facts(case_dir, case["naca"])
    if stage == "mesh":
        mp = mesh_params(case_dir)
        # mesh_check.json describes the mesh on disk; it only counts when that mesh was
        # built from the set-up now written (set_mesh_params after build_mesh does not rebuild)
        matches = mesh_matches_setup(case_dir)
        chk = (read_json(os.path.join(case_dir, "mesh_check.json")) or {}) if matches else {}
        yp = (planning.predicted_yplus(mp["first_cell_height"], cond["reynolds"], cond["velocity"])
              if mp.get("first_cell_height") else None)
        return {"fatal_error": chk.get("fatal_error"), "cells": chk.get("cells"),
                "max_non_orthogonality": chk.get("max_non_orthogonality"),
                "max_skewness": chk.get("max_skewness"),
                "predicted_yplus": yp, "wall_treatment": wall_treatment_for(ctx),
                "mesh_matches_setup": matches,
                "checked_this_run": written_this_run(ctx, "mesh", os.path.join(case_dir, "mesh_check.json"))}
    if stage == "physics":
        facts = physics_facts(case_dir)
        facts.update({"velocity_expected": cond.get("velocity"), "reynolds_expected": cond.get("reynolds"),
                      "alpha_expected": case.get("alpha_deg"),
                      "farfield_chords": mesh_params(case_dir).get("farfield_chords"),
                      "written_this_run": written_this_run(ctx, "physics", os.path.join(case_dir, "physics.json"))})
        return facts
    if stage == "numerics":
        sysdir = os.path.join(case_dir, "system")
        # numerics.json is written by set_numerics; without it the dictionaries are
        # only the placeholders the mesh stage needs.
        written = os.path.exists(os.path.join(case_dir, "numerics.json"))
        files = {n: written and os.path.exists(os.path.join(sysdir, n)) and os.path.getsize(os.path.join(sysdir, n)) > 0
                 for n in ("controlDict", "fvSchemes", "fvSolution")}
        return {"files": files, "iterations": solver.end_time(case_dir) if written else None,
                **_force_gate_input(ctx),
                "written_this_run": written_this_run(ctx, "numerics", os.path.join(case_dir, "numerics.json"))}
    if stage == "solve":
        state = read_json(os.path.join(case_dir, "solver_state.json")) or {}
        run = solver.SOLVERS.get(ctx.case_id)
        if run is not None and run.state == "running":
            state = {"state": "running"}
        fs = ac.force_summary(case_dir)
        _, full = ac.force_history(case_dir)
        # solver_state.json is written when a run ends: one from before this run of the
        # stage means no solver has run since the stage was sent back here
        return {"fatal_error": state.get("fatal"), "solver_state": state.get("state"),
                "force_points": len(full), "forces_converged": fs.get("forces_converged"),
                "cl_drift": fs.get("Cl_drift"), "cd_drift": fs.get("Cd_drift"),
                **_force_gate_input(ctx),
                "ran_this_run": written_this_run(ctx, "solve", os.path.join(case_dir, "solver_state.json"))}
    if stage == "post":
        res = case.get("results") or {}
        cp = read_json(os.path.join(case_dir, "cp.json")) or {}
        return {"cl": res.get("cl"), "cd": res.get("cd"), "yplus_avg": res.get("yplus_avg"),
                "wall_treatment": wall_treatment_for(ctx),
                "cp_points": min(len(cp.get("upper") or []), len(cp.get("lower") or []))}
    if stage == "validate":
        ref, deltas = case.get("reference") or {}, case.get("deltas") or {}
        return {"xfoil_converged": ref.get("converged"), "cl_pct": deltas.get("cl_pct"),
                "cl_abs": deltas.get("cl_abs"), "cl_ref": ref.get("cl"),
                "cd_pct": deltas.get("cd_pct"), "verdict": case.get("verdict"),
                "rework_loops": (case.get("counters") or {}).get("rework_loops", 0),
                "alpha_reference": ref.get("alpha"), "alpha_case": case.get("alpha_deg"),
                "alpha_solved": solved_angle(ctx)}
    raise ValueError(f"no gate for stage {stage!r}")


def run_gate(stage, ctx):
    try:
        return GATES[stage](collect_gate_input(stage, ctx))
    except Exception as e:  # noqa: BLE001 - a gate must always give a verdict
        return {"passed": False, "checks": [_check("gate could be evaluated", f"{type(e).__name__}: {e}",
                                                   "no error", False)]}


def compute_deltas(results, reference, farfield_chords):
    """Deviation of the RANS result from the XFoil reference. `cl_pct` is None when the
    reference lift is zero (a symmetric section at zero lift); `cl_abs` always exists."""
    def pct(a, b):
        return 100.0 * (a - b) / abs(b) if (is_finite(a) and is_finite(b) and b) else None
    cl, cd = results.get("cl"), results.get("cd")
    return {"cl_pct": round_sig(pct(cl, reference.get("cl")), 4), "cd_pct": round_sig(pct(cd, reference.get("cd")), 4),
            "cl_abs": round_sig(cl - reference["cl"], 4) if (is_finite(cl) and is_finite(reference.get("cl"))) else None,
            "cm_abs": round_sig(abs(results["cm"] - reference["cm"]), 4)
            if (is_finite(results.get("cm")) and is_finite(reference.get("cm"))) else None,
            "ld_pct": round_sig(pct(results.get("ld"), reference.get("ld")), 4),
            "farfield_cd_estimate": round_sig(ac.farfield_drag_estimate(cl, farfield_chords), 4)
            if (is_finite(cl) and farfield_chords) else None}


def mesh_matches_setup(case_dir):
    """True when constant/polyMesh was built by build_mesh from the set-up that
    mesh_plan.json holds now (build_mesh records what it built in mesh_built.json)."""
    built = read_json(os.path.join(case_dir, "mesh_built.json")) or {}
    planned = read_json(os.path.join(case_dir, "mesh_plan.json")) or {}
    return (os.path.isdir(os.path.join(case_dir, "constant", "polyMesh"))
            and bool(planned.get("params")) and built.get("params") == planned.get("params")
            and built.get("blockMeshDict") == read_text(os.path.join(case_dir, "system", "blockMeshDict")))
