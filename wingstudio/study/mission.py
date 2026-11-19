"""From a mission to a study: the requirements read from the engineer's words, the flight
conditions, and the candidate sections (the scripted advisor's selection rules of thin-airfoil
theory)."""
import math
import re

from foamtools.common import round_sig

from .. import exceptions
from ..cfd import case as ac
from . import reporting

MAX_SELECTED = 4


# ------------------------------------------------------------- requirements --

def _first_number(pattern, text):
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_prompt(prompt):
    """Pull the mission numbers out of a free-text prompt (deterministic; the
    form values, when present, take precedence)."""
    text = prompt or ""
    out = {}
    v = _first_number(r"\bmach(?:\s+number)?\s*(?:of|=|:|is)?\s*([0-9]*\.?[0-9]+)", text)
    if v is not None:
        out["mach"] = v
    v = _first_number(r"density\s*(?:of|=|:|is)?\s*([0-9]*\.?[0-9]+)", text)
    if v is None:
        v = _first_number(r"([0-9]*\.?[0-9]+)\s*kg\s*/\s*m", text)
    if v is not None:
        out["density"] = v
    elif re.search(r"sea[- ]level", text, re.I):
        out["density"] = 1.225
    m = re.search(r"reynolds(?:\s+number)?\s*(?:of|=|:|is)?\s*([0-9][0-9.,]*(?:e[0-9]+)?)\s*(million|thousand|k\b|m\b)?",
                  text, re.I)
    if m:
        try:
            value = float(m.group(1).rstrip(".,").replace(",", ""))
            unit = (m.group(2) or "").lower()
            value *= {"million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}.get(unit, 1.0)
            out["reynolds"] = value
        except ValueError:
            pass
    v = _first_number(r"lift\s+coefficient\s*(?:of|=|:|is)?\s*([0-9]*\.?[0-9]+)", text)
    if v is not None:
        out["design_cl"] = v
    v = _first_number(r"(?:at least|minimum(?:\s+thickness)?(?:\s+of)?|min\.?)\s*([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if v is None:
        v = _first_number(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*thick", text)
    if v is not None:
        out["min_thickness"] = v / 100.0
    v = _first_number(r"(?:pitching moment|\|?cm\|?)[^0-9\-]{0,40}-?\s*([0-9]*\.[0-9]+)", text)
    if v is not None:
        out["max_abs_cm"] = abs(v)
    # "L/D of at least 60", "lift-to-drag ratio above 55", "target L/D 60", "L/D >= 60": a wanted
    # lift-to-drag ratio. The number must follow directly, so "max L/D at Cl 0.8" is not a target.
    v = _first_number(r"(?:\bl\s*/\s*d\b|lift[- ]to[- ]drag)(?:\s+ratio)?\s*(?:of|=|:|is|should be|must be|>=|≥|>)?\s*"
                      r"(?:at least|no less than|not less than|above|over|greater than|more than|minimum(?:\s+of)?)?"
                      r"\s*([0-9]+(?:\.[0-9]+)?)(?!\.?[0-9]|\s*%)", text)
    if v is not None and v > 0:
        out["target_ld"] = v
    if re.search(r"endurance|cl\s*\^\s*1\.5|minimum (?:power|sink)", text, re.I):
        out["objective"] = "max_endurance"
    elif re.search(r"(?:lowest|minimi[sz]e|minimum|least|min)\s+drag", text, re.I):
        out["objective"] = "min_cd"
    return out


def normalise_objective(value):
    if value in reporting.OBJECTIVES:
        return value
    text = str(value or "").lower()
    if "endur" in text or "1.5" in text:
        return "max_endurance"
    if "cd" in text or "drag" in text:
        return "min_cd"
    return "max_ld"


def normalise_requirements(requirements, prompt):
    """Merge the form values over what the prompt says, validate, and return
    the requirements object of the data model. Raises EngineError."""
    given = {k: v for k, v in (requirements or {}).items() if v not in (None, "")}
    merged = dict(parse_prompt(prompt), **given)
    merged.setdefault("density", 1.225)
    out = {}
    for key, lo, hi in (("mach", 0.005, 0.95), ("density", 0.01, 2.0),
                        ("reynolds", 5e4, 1e8), ("design_cl", 0.0, 2.0)):
        if merged.get(key) is None:
            raise exceptions.EngineError(f"the mission needs a value for {key} (in the form or in the text)")
        try:
            out[key] = float(merged[key])
        except (TypeError, ValueError):
            raise exceptions.EngineError(f"{key} must be a number")
        if not (lo <= out[key] <= hi):
            raise exceptions.EngineError(f"{key} = {out[key]:g} is outside the supported range {lo:g} to {hi:g}")
    out["objective"] = normalise_objective(merged.get("objective"))
    for key in ("min_thickness", "max_abs_cm"):
        value = merged.get(key)
        if value is None:
            out[key] = None
            continue
        try:
            value = abs(float(value))
        except (TypeError, ValueError):
            raise exceptions.EngineError(f"{key} must be a number")
        if key == "min_thickness" and value > 1.0:
            value /= 100.0                      # given in percent
        out[key] = value or None
    if out["min_thickness"] and not (0.04 <= out["min_thickness"] <= 0.30):
        raise exceptions.EngineError("min_thickness must be between 0.04 and 0.30 (fraction of chord)")
    # Optional lift-to-drag target at the design lift coefficient: reported as met / not met,
    # never used to exclude a candidate.
    out["target_ld"] = None
    if merged.get("target_ld") is not None:
        try:
            target = float(merged["target_ld"])
        except (TypeError, ValueError):
            raise exceptions.EngineError("target_ld must be a number")
        if not (math.isfinite(target) and target > 0):
            raise exceptions.EngineError("target_ld must be a number above 0 (leave it empty for no target)")
        out["target_ld"] = target
    out["notes"] = str(merged.get("notes") or "")
    return out


def derive_conditions(req):
    cond = ac.derive_conditions(req["mach"], req["density"], req["reynolds"])
    warnings = []
    m = req["mach"]
    if m > 0.5:
        warnings.append(f"Mach {m:g} is well above 0.3: the solver is incompressible, so lift is understated by "
                        f"about {(cond['compressibility_factor'] - 1) * 100:.0f}% and local supersonic flow is "
                        "possible. The result is indicative only; a compressible solver is needed.")
    elif m > 0.3:
        warnings.append(f"Mach {m:g} is above 0.3: the solver is incompressible. Expect lift about "
                        f"{(cond['compressibility_factor'] - 1) * 100:.0f}% higher in flight "
                        "(Prandtl-Glauert); results are labelled uncorrected.")
    if req["reynolds"] < 5e5:
        warnings.append(f"Reynolds number {req['reynolds']:.3g} is transitional: the fully turbulent RANS run "
                        "over-predicts drag for a clean surface.")
    if req["design_cl"] > 1.2:
        warnings.append(f"Design lift coefficient {req['design_cl']:g} is close to maximum lift for NACA 4-digit "
                        "sections; XFoil and steady RANS are both unreliable near stall.")
    cond["warnings"] = warnings
    return cond


def study_title(req, prompt):
    text = " ".join((prompt or "").split())
    if text:
        return text if len(text) <= 70 else text[:67].rstrip() + "..."
    return f"Re {req['reynolds']:.3g}, Cl {req['design_cl']:g}, Mach {req['mach']:g}"


# --------------------------------------------------------- scripted advisor --

def naca_code(camber, thickness_pct):
    return f"{camber}4{thickness_pct:02d}" if camber > 0 else f"00{thickness_pct:02d}"


def scripted_candidates(req):
    """Candidates from the NACA 4-digit selection rules of thin-airfoil theory: camber from
    the design lift coefficient (0.128 per percent at 40% chord), capped by the
    moment limit (0.0266 per percent); thickness at the minimum allowed, rounded
    up to a standard step; then one-digit variations."""
    cl = req["design_cl"]
    steps = (9, 12, 15, 18, 21, 24)
    min_t = (req.get("min_thickness") or 0.12) * 100.0
    t = next((s for s in steps if s >= min_t - 1e-6), steps[-1])
    thicker = next((s for s in steps if s > t), None)
    camber = int(round(cl / 0.128))
    if req.get("objective") == "max_endurance":
        camber += 1                           # the endurance optimum sits at higher lift
    cap = 6
    if req.get("max_abs_cm"):
        cap = min(cap, int(math.floor(req["max_abs_cm"] / 0.0266 + 1e-9)))
    if req["reynolds"] < 5e5:
        cap = min(cap, 4)                     # thick, highly cambered sections separate at low Re
    camber = max(0, min(cap, camber))
    cf = 0.074 / req["reynolds"] ** 0.2
    target_ld = req.get("target_ld")

    def describe(c, tt, role):
        form = 1 + 2 * tt / 100.0 + 60 * (tt / 100.0) ** 4
        text = (f"{role} {c}% camber gives ideal Cl {0.128 * c:.2f} (design {cl:g}), zero-lift angle "
                f"{-1.04 * c:.1f} deg, thin-airfoil Cm {-0.0266 * c:.3f}; {tt}% thick, estimated fully "
                f"turbulent drag {2 * cf * form:.4f}.")
        if target_ld:
            # The target never excludes a section: the estimate is stated, XFoil judges it.
            text += (f" Estimated L/D {cl / (2 * cf * form):.0f} at the design Cl against the target of "
                     f"{target_ld:g} (the XFoil screen decides whether it is met).")
        return text

    out = []

    def add(c, tt, role):
        if tt is None or not (0 <= c <= cap):
            return
        code = naca_code(c, tt)
        if code not in [o["naca"] for o in out]:
            out.append({"naca": code, "rationale": describe(c, tt, role)})

    add(camber, t, "Performance pick:")
    add(camber, thicker, "Robust alternative, one step thicker (structure, gentler stall, about 7% more drag):")
    add(camber - 1, t, "One step less camber (lower pitching moment, more incidence needed):")
    add(camber + 1, t, "One step more camber (design lift closer to the ideal angle, larger moment):")
    add(camber - 1, thicker, "Less camber and one step thicker:")
    add(camber + 1, thicker, "More camber and one step thicker:")
    add(camber - 2, t, "Two steps less camber:")
    return out[:6]


def candidate_entry(naca, rationale, screened, req):
    """One entry of study.candidates from a rationale and an XFoil screen result."""
    xf = {k: screened.get(k) for k in ("alpha_design", "cl", "cd", "cm", "ld", "endurance")}
    xf["converged"] = bool(screened.get("converged"))
    xf["polar"] = screened.get("polar") or []
    if screened.get("error"):
        xf["error"] = screened["error"]
    return {"naca": naca, "rationale": rationale, "rank": None, "selected": False, "xfoil": xf,
            "meets_constraints": bool(screened.get("meets_thickness", True)) and bool(screened.get("meets_moment", True)),
            # XFoil's forced-transition L/D at the design Cl against the optional target (None: no target)
            "meets_target_ld": reporting.meets_target_ld(req, screened.get("ld")),
            "objective_value": round_sig(reporting.objective_value(req.get("objective") or "max_ld",
                                                          screened.get("cl"), screened.get("cd")), 5)}


def build_candidates(entries, screen, req, top):
    """Merge rationale and XFoil screen, rank by the objective, and mark the top two.
    Returns the candidate list, or an error string."""
    built = [candidate_entry(e["naca"], e["rationale"], screen.get(e["naca"]) or {}, req) for e in entries]
    converged = [c for c in built if c["xfoil"]["converged"] and c["objective_value"] is not None]
    if len(converged) < 2:
        failed = [f"NACA {c['naca']}: {c['xfoil'].get('error', 'did not converge')}" for c in built
                  if c not in converged]
        return ("fewer than two candidates could be screened in XFoil at the design lift coefficient ("
                + "; ".join(failed) + "). Propose others.")
    order = sorted(built, key=lambda c: (0 if c in converged and c["meets_constraints"] else
                                         1 if c in converged else 2,
                                         -(c["objective_value"] if c["objective_value"] is not None else -1e9)))
    for i, c in enumerate(order, 1):
        c["rank"] = i
    chosen = []
    for code in top or []:
        try:
            code = ac.naca4(code)[0]
        except (ValueError, AttributeError):
            continue
        match = next((c for c in converged if c["naca"] == code), None)
        if match and match not in chosen:
            chosen.append(match)
    chosen = chosen[:2]
    for c in order:
        if len(chosen) >= 2:
            break
        if c in converged and c not in chosen:
            chosen.append(c)
    for c in chosen:
        c["selected"] = True
    return order
