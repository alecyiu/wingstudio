"""Results of a study: the objective of each case, the table of results and the report the
scripted model writes."""
from foamtools.common import is_finite, round_sig

from ..cfd import case as ac

OBJECTIVES = ("max_ld", "max_endurance", "min_cd")
OBJECTIVE_LABEL = {"max_ld": "maximum L/D at the design lift coefficient",
                   "max_endurance": "maximum endurance parameter Cl^1.5/Cd",
                   "min_cd": "minimum drag at the design lift coefficient"}


def objective_value(objective, cl, cd):
    """Higher is better for every objective."""
    if not (is_finite(cl) and is_finite(cd) and cd > 0):
        return None
    if objective == "max_endurance":
        return (cl ** 1.5 / cd) if cl > 0 else None
    if objective == "min_cd":
        return -cd
    return cl / cd


def meets_target_ld(requirements, ld):
    """True / False against the optional lift-to-drag target; None when there is
    no target or no L/D to judge. The target never excludes anything."""
    target = (requirements or {}).get("target_ld")
    if not target or not is_finite(ld):
        return None
    return bool(ld >= float(target) - 1e-9)


def results_table(study, cases):
    """One row per case from the RANS results. `recommended` marks the best
    finished pass/warn case among those that meet the thickness and moment
    limits of the mission (among all of them only when none does)."""
    req = study.get("requirements") or {}
    objective = req.get("objective") or "max_ld"
    min_t, max_cm, design_cl = req.get("min_thickness"), req.get("max_abs_cm"), req.get("design_cl")
    rows = []
    for c in cases:
        r, ref, d = c.get("results") or {}, c.get("reference") or {}, c.get("deltas") or {}
        cl, cd, cm = r.get("cl"), r.get("cd"), r.get("cm")
        score = objective_value(objective, cl, cd)
        try:
            thickness = ac.naca4(c["naca"])[3]
        except ValueError:
            thickness = None
        meets_thickness = True if not min_t else (None if thickness is None else thickness + 1e-9 >= float(min_t))
        meets_moment = True if not max_cm else (abs(cm) <= float(max_cm) + 1e-9 if is_finite(cm) else None)
        rows.append({
            "case_id": c["id"], "naca": c["naca"], "alpha_deg": c.get("alpha_deg"), "status": c.get("status"),
            "verdict": c.get("verdict"), "cl": cl, "cd": cd, "cm": cm, "ld": r.get("ld"),
            "endurance": round_sig(objective_value("max_endurance", cl, cd), 5),
            "xfoil_cl": ref.get("cl"), "xfoil_cd": ref.get("cd"), "xfoil_cm": ref.get("cm"), "xfoil_ld": ref.get("ld"),
            "cl_err_pct": d.get("cl_pct"), "cd_err_pct": d.get("cd_pct"), "cm_err_abs": d.get("cm_abs"),
            # Cl difference in absolute terms: the one to quote when cl_err_pct is None (zero reference lift)
            "cl_err_abs": d.get("cl_abs"),
            "farfield_cd_estimate": d.get("farfield_cd_estimate"),
            "yplus_avg": r.get("yplus_avg"), "cells": r.get("cells"), "iterations": r.get("iterations"),
            "forces_converged": r.get("forces_converged"),
            "thickness": thickness, "meets_thickness": meets_thickness, "meets_moment": meets_moment,
            # None = not known yet (no Cm); only a known violation counts against a case
            "meets_constraints": (False if False in (meets_thickness, meets_moment) else
                                  None if None in (meets_thickness, meets_moment) else True),
            "cl_vs_design_pct": (round_sig((cl - design_cl) / design_cl * 100.0, 3)
                                 if (is_finite(cl) and is_finite(design_cl) and design_cl) else None),
            "meets_target_ld": meets_target_ld(req, r.get("ld")),
            "objective_value": round_sig(score, 5), "recommended": False,
        })
    finished = [r for r in rows if r["status"] == "done" and r["verdict"] in ("pass", "warn")
                and r["objective_value"] is not None]
    eligible = [r for r in finished if r["meets_constraints"] is not False] or finished
    if eligible:
        max(eligible, key=lambda r: r["objective_value"])["recommended"] = True
    return rows


def _fmt(value, spec):
    return format(value, spec) if is_finite(value) else "n/a"


def _limit_violations(row, req):
    """What a results row breaks of the mission's thickness and moment limits, in words."""
    out = []
    if row.get("meets_thickness") is False:
        out.append(f"thickness {row['thickness'] * 100:.0f}% is below the minimum {req['min_thickness'] * 100:g}%")
    if row.get("meets_moment") is False:
        out.append(f"|Cm| {abs(row['cm']):.3f} exceeds the limit {req['max_abs_cm']:g}")
    return out


def scripted_report(study, cases):
    """The deterministic report: comparison table, recommendation, and what is not verified."""
    req, cond = study["requirements"], study["conditions"]
    rows = results_table(study, cases)
    best = next((r for r in rows if r["recommended"]), None)
    objective = OBJECTIVE_LABEL.get(req["objective"], req["objective"])
    target, limited = req.get("target_ld"), bool(req.get("min_thickness") or req.get("max_abs_cm"))
    limits = []
    if req.get("min_thickness"):
        limits.append(f"thickness at least {req['min_thickness'] * 100:g}%")
    if req.get("max_abs_cm"):
        limits.append(f"|Cm| at most {req['max_abs_cm']:g}")
    if target:
        limits.append(f"target L/D {target:g} at the design lift coefficient (reported, not a limit)")
    yes_no = {True: "yes", False: "no", None: "n/a"}
    lines = [f"# Airfoil comparison: {', '.join('NACA ' + r['naca'] for r in rows)}", "",
             f"Mission: Mach {req['mach']:g}, density {req['density']:g} kg/m^3, Reynolds number "
             f"{req['reynolds']:.3g}, design lift coefficient {req['design_cl']:g}. Objective: {objective}."
             + (f" Limits: {'; '.join(limits)}." if limits else ""), "",
             "| Airfoil | alpha (deg) | Cl | Cd | Cm | L/D | Cl^1.5/Cd | XFoil Cl | XFoil Cd | dCl | dCd | Verdict |"
             + (" Limits met |" if limited else "") + (f" L/D >= {target:g} |" if target else ""),
             "|---|---|---|---|---|---|---|---|---|---|---|---|" + ("---|" if limited else "") + ("---|" if target else "")]
    for r in rows:
        lines.append(
            f"| NACA {r['naca']} | {_fmt(r['alpha_deg'], '.2f')} | {_fmt(r['cl'], '.4f')} | {_fmt(r['cd'], '.5f')} | "
            f"{_fmt(r['cm'], '.4f')} | {_fmt(r['ld'], '.1f')} | {_fmt(r['endurance'], '.1f')} | "
            f"{_fmt(r['xfoil_cl'], '.4f')} | {_fmt(r['xfoil_cd'], '.5f')} | "
            + (f"{r['cl_err_pct']:+.1f}%" if is_finite(r['cl_err_pct']) else
               f"{r['cl_err_abs']:+.4f} (absolute)" if is_finite(r['cl_err_abs']) else "n/a") + " | "
            f"{_fmt(r['cd_err_pct'], '+.1f')}% | {r['verdict'] or r['status']} |"
            + (f" {yes_no[r['meets_constraints']]} |" if limited else "")
            + (f" {yes_no[r['meets_target_ld']]} |" if target else ""))
    lines.append("")
    if best:
        def metric_of(r):
            return {"max_ld": f"L/D {_fmt(r['ld'], '.1f')}", "max_endurance": f"Cl^1.5/Cd {_fmt(r['endurance'], '.1f')}",
                    "min_cd": f"Cd {_fmt(r['cd'], '.5f')}"}[req["objective"]]
        others = [r for r in rows if r is not best and r["objective_value"] is not None]
        # Compare with the best of the other sections that are still in the running; a section
        # that breaks a limit is named separately below.
        rivals = [r for r in others if r["meets_constraints"] is not False] or \
            ([] if best["meets_constraints"] is not False else others)
        versus, compared = "", [best]
        if rivals:
            o = max(rivals, key=lambda r: r["objective_value"])
            compared.append(o)
            versus = f", against {metric_of(o)} for NACA {o['naca']}"
        # The runs are at fixed angles: say so when the lift reached is not the design lift.
        off = [r for r in compared if r["cl_vs_design_pct"] is not None and abs(r["cl_vs_design_pct"]) > 1.0]
        label = objective if not off else objective.replace(" at the design lift coefficient", "")
        recommendation = (f"NACA {best['naca']} is recommended for {label}: RANS gives {metric_of(best)} at "
                          f"{_fmt(best['alpha_deg'], '.2f')} deg{versus}; validation verdict {best['verdict']}.")
        if off:
            recommendation += (" The RANS runs are at fixed angles and did not reach the design lift coefficient "
                               f"{req['design_cl']:g}: " + ", ".join(
                                   f"NACA {r['naca']} Cl {r['cl']:.3f} ({r['cl_vs_design_pct']:+.1f}%)" for r in compared)
                               + ("; part of the gap between them is this difference in lift, not the section."
                                  if len(compared) > 1 else "."))
        broken = _limit_violations(best, req)
        if broken:
            recommendation += (" No finished section meets the mission limits; this one has the best objective "
                               "value but " + " and ".join(broken) + ".")
        for r in others:
            if best["meets_constraints"] is not False and r["meets_constraints"] is False \
                    and r["status"] == "done" and r["objective_value"] > best["objective_value"]:
                recommendation += (f" NACA {r['naca']} has the better objective value ({metric_of(r)}) but is not "
                                   "eligible: " + " and ".join(_limit_violations(r, req)) + ".")
        if target and best["meets_target_ld"] is not None:
            recommendation += (f" The L/D target of {target:g} is "
                               f"{'met' if best['meets_target_ld'] else 'not met'} (RANS L/D {_fmt(best['ld'], '.1f')}).")
    else:
        recommendation = ("No recommendation: no case finished with a pass or warn verdict against XFoil. "
                          "Review the failed stages before using these numbers.")
    lines += ["## Recommendation", "", recommendation, ""]
    xfoil_rank = sorted((c for c in study.get("candidates") or [] if c.get("selected") and c.get("xfoil")),
                        key=lambda c: -(objective_value(req["objective"], c["xfoil"].get("cl"), c["xfoil"].get("cd")) or -1e9))
    rans_rank = sorted((r for r in rows if r["objective_value"] is not None), key=lambda r: -r["objective_value"])
    if len(xfoil_rank) > 1 and len(rans_rank) == len(xfoil_rank):
        same = [c["naca"] for c in xfoil_rank] == [r["naca"] for r in rans_rank]
        lines += ["## Agreement with XFoil", "",
                  ("RANS and XFoil rank the airfoils in the same order for this objective." if same else
                   "RANS and XFoil rank the airfoils differently for this objective: treat the choice as open "
                   "until a finer mesh or a wider angle sweep confirms it."), ""]
    if target:
        screen = {c["naca"]: (c.get("xfoil") or {}).get("ld") for c in study.get("candidates") or []}
        judged = {True: "met", False: "not met", None: "not judged"}
        lines += [f"## L/D target {target:g}", ""]
        for r in rows:
            x_ld = screen.get(r["naca"])
            lines.append(f"- NACA {r['naca']}: XFoil screen L/D {_fmt(x_ld, '.1f')} at the design lift coefficient "
                         f"({judged[meets_target_ld(req, x_ld)]}); RANS L/D {_fmt(r['ld'], '.1f')} "
                         f"({judged[r['meets_target_ld']]}).")
        lines.append("")
    lines += ["## Not verified", "",
              "- One angle of attack per airfoil (the angle at which XFoil reaches the design lift coefficient); "
              "no polar, no stall margin.",
              "- Fully turbulent, incompressible RANS with wall functions on a 30-chord domain: drag includes a "
              "far-field penalty of about Cl^2 / (4 pi 30) and is an upper estimate for a clean surface.",
              "- No mesh-independence study; the XFoil comparison is the only cross-check.",
              f"- Temperature {cond['temperature_K']:g} K assumed from the standard atmosphere at the given density."]
    lines += [f"- {w}" for w in cond.get("warnings") or []]
    for r in rows:
        if r["status"] != "done":
            lines.append(f"- NACA {r['naca']} did not finish (status {r['status']}).")
    return {"markdown": "\n".join(lines), "recommendation": recommendation, "table": rows}
