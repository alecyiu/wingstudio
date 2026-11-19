"""The study plan: stages and their settings, the default plan, and the rules that
normalise, validate and cite a plan written by an agent or edited by the engineer."""
import math

from foamtools.common import is_finite

from ..cfd import case as ac
from ..refs import citations

MAX_TOTAL_ITERATIONS = 12000     # hard ceiling for extend_run and for set_numerics(iterations)
MAX_CELLS = 80000                # mesh size limit (plan, set_mesh_params, build_mesh and the mesh gate)


STAGES = [
    ("geometry", "Geometry", "geometry.md"),
    ("mesh", "Mesh", "mesher.md"),
    ("physics", "Boundary conditions and physics", "boundary-conditions.md"),
    ("numerics", "Solver setup", "solver-setup.md"),
    ("solve", "Solve", "run-monitor.md"),
    ("post", "Post-process", "post-processor.md"),
    ("validate", "Validate against XFoil", "xfoil-validator.md"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_TITLES = {s[0]: s[1] for s in STAGES}
STAGE_AGENTS = {s[0]: s[2] for s in STAGES}

DEFAULT_PHYSICS = {
    "turbulence_model": "SpalartAllmaras", "wall_treatment": "wallFunction",
    "nut_ratio": 4.0, "turbulence_intensity": 0.001, "farfield_bc": "fixedInlet",
}
MESH_KEYS = ["farfield_chords", "wake_chords", "n_normal", "n_leading", "n_aft", "n_wake",
             "first_cell_height"]
PHYSICS_KEYS = list(DEFAULT_PHYSICS)
NUMERICS_KEYS = ["iterations", "write_interval", "convection_scheme", "relax_p", "relax_U",
                 "relax_turbulence", "residual_p", "residual_U", "residual_turbulence",
                 "n_non_orthogonal_correctors", "consistent"]
DEFAULT_TARGET_YPLUS = 40.0      # h = 1e-3 chord at Re 2e6: the verified default mesh
ENGINEER_MARK = "Set by the engineer"   # the reason prefix mark_engineer_edits writes
PLAN_FIXED_KEYS = ("reasons", "checks", "cites")   # keys of a plan stage section that are not settings
# Values outside these ranges are refused by set_physics / set_numerics (and should be
# refused by the plan validation): they are not physical, or make every iteration so
# expensive that the run never ends.
SETTING_LIMITS = {
    "physics": {"nut_ratio": (0.01, 100.0), "turbulence_intensity": (1e-5, 0.05)},
    "numerics": {"write_interval": (10, 6000), "relax_p": (0.05, 1.0), "relax_U": (0.05, 1.0),
                 "relax_turbulence": (0.05, 1.0), "residual_p": (1e-9, 1e-3), "residual_U": (1e-9, 1e-3),
                 "residual_turbulence": (1e-9, 1e-3), "n_non_orthogonal_correctors": (0, 3)},
}
_SETTING_HINTS = {"turbulence_intensity": " (a fraction, not percent: 0.001 is 0.1%)",
                  "nut_ratio": " (free-stream eddy viscosity over the molecular viscosity)"}


def setting_problems(stage, values):
    """Range errors (a list of sentences) for the numeric settings of one stage section."""
    out = []
    for key, (lo, hi) in SETTING_LIMITS.get(stage, {}).items():
        v = (values or {}).get(key)
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            out.append(f"{key} = {v!r} is not a number")
            continue
        if not (is_finite(x) and lo <= x <= hi):
            out.append(f"{key} = {x:g} is outside {lo:g} to {hi:g}" + _SETTING_HINTS.get(key, ""))
    return out


XTR_FORCED = 0.05                # like-for-like reference: transition tripped at 5% chord
ZOOM_PRESETS = ("auto", "full", "airfoil", "leading_edge", "trailing_edge", "wake")   # as viewer/render_case.py


def round_up(value, multiple):
    multiple = max(1, int(multiple))
    return int(math.ceil(int(value) / multiple) * multiple)


MESH_LIMITS = {"farfield_chords": (5, 100), "wake_chords": (5, 100), "n_normal": (20, 300),
                "n_leading": (10, 200), "n_aft": (10, 200), "n_wake": (10, 300),
                "first_cell_height": (1e-7, 0.05)}


def yplus_band(wall_treatment):
    """(low, high) acceptable average y+ for the wall treatment."""
    return (20.0, 200.0) if wall_treatment == "wallFunction" else (0.0, 5.0)


def predicted_yplus(first_cell_height, reynolds, velocity):
    """Flat-plate y+ of the first cell centre for a given first-cell height."""
    unit = ac.first_cell_height(reynolds, velocity, 1.0)["cell_centre_height"]
    return (float(first_cell_height) / 2.0) / unit


def resolve_mesh_params(plan_section, overrides):
    """Tool arguments over plan values over the built-in defaults."""
    params = {k: ac.DEFAULT_MESH[k] for k in MESH_KEYS}
    for src in (plan_section or {}, overrides or {}):
        for k in MESH_KEYS:
            if src.get(k) is not None:
                params[k] = src[k]
    for k in ("n_normal", "n_leading", "n_aft", "n_wake"):
        params[k] = int(round(float(params[k])))
    for k in ("farfield_chords", "wake_chords", "first_cell_height"):
        params[k] = float(params[k])
    return params


# ----------------------------------------------------------------- numerics --

def resolve_numerics(base, overrides):
    n = dict(ac.DEFAULT_NUMERICS)
    for src in (base or {}, overrides or {}):
        for k in ac.DEFAULT_NUMERICS:
            if src.get(k) is not None:
                n[k] = src[k]
    n["iterations"] = int(round(float(n["iterations"])))
    n["write_interval"] = max(1, int(round(float(n["write_interval"]))))
    n["n_non_orthogonal_correctors"] = int(round(float(n["n_non_orthogonal_correctors"])))
    if isinstance(n["consistent"], str):
        n["consistent"] = n["consistent"].strip().lower() in ("1", "true", "yes", "on")
    for k in ("relax_p", "relax_U", "relax_turbulence", "residual_p", "residual_U", "residual_turbulence"):
        n[k] = float(n[k])
    return n


# ------------------------------------------------------------- default plan --

def default_case_plan(study, candidate):
    """The plan one airfoil gets when nobody changes anything: the verified
    stable mesh and numerics, with the first cell sized for this Reynolds number.

    Every stage section is a flat object of settings plus two fixed keys:
    `reasons` (setting -> one sentence) and `checks` (the acceptance checks in words)."""
    cond, req = study["conditions"], study["requirements"]
    code = ac.naca4(candidate["naca"])[0]
    alpha = (candidate.get("xfoil") or {}).get("alpha_design")
    first = ac.first_cell_height(cond["reynolds"], cond["velocity"], DEFAULT_TARGET_YPLUS)["first_cell_height"]
    mesh = {k: ac.DEFAULT_MESH[k] for k in MESH_KEYS}
    mesh["first_cell_height"] = float(f"{first:.3g}")
    cells = ac.mesh_plan(mesh)["cells"]
    numerics = {k: ac.DEFAULT_NUMERICS[k] for k in NUMERICS_KEYS}
    mach = float(req.get("mach") or 0.0)
    alpha_text = f"{alpha:g}" if alpha is not None else "?"
    return {
        "naca": code,
        "alpha_deg": alpha,
        "stages": {
            "geometry": {
                "naca": code,
                "reasons": {"naca": f"{ac.geometry_summary(code)['description']}; closed trailing edge, unit "
                                    "chord, written as an OBJ surface that blockMesh projects onto."},
                "checks": ["profile closed", "thickness within 1% of the code", "OBJ surface written"]},
            "mesh": dict(mesh, reasons={
                "farfield_chords": "30 chords is the largest far field this C-mesh block topology runs stably.",
                "wake_chords": "outlet 30 chords behind the trailing edge, the same distance as the far field.",
                "n_leading": "40 cells from the leading edge to 30% chord on each surface (160 surface cells in "
                             "all): the verified default mesh.",
                "n_aft": "40 cells from 30% chord to the trailing edge on each surface: the verified default mesh.",
                "n_wake": "50 cells along the 30-chord wake: the verified default mesh.",
                "n_normal": f"{cells:,} cells in total: enough for forces within a few percent, about a minute per run.",
                "first_cell_height": f"{mesh['first_cell_height']:.3g} chord gives y+ about {DEFAULT_TARGET_YPLUS:g} "
                                     f"at Re {cond['reynolds']:.3g} (wall functions need 20 to 200). y+ scales "
                                     "in proportion to this height: it is the one setting for the wall spacing."},
                checks=["mesh built from these values", "no fatal checkMesh error",
                        "max non-orthogonality < 70", "max skewness < 4",
                        f"at most {MAX_CELLS:,} cells", "predicted y+ inside the band for the wall treatment"]),
            "physics": dict(DEFAULT_PHYSICS, reasons={
                "turbulence_model": "Spalart-Allmaras: robust for attached external flow, fully turbulent.",
                "wall_treatment": "wall functions keep the mesh small; valid for y+ 20 to 200.",
                "farfield_bc": "velocity fixed on the inlet arc, pressure fixed on the outlet: the stable choice "
                               "for a 30-chord domain.",
                "nut_ratio": f"free-stream nuTilda = 4 nu (with k-omega SST the same number is nut / nu); "
                             f"velocity {cond['velocity']:.4g} m/s at {alpha_text} deg "
                             f"on unit chord with nu = U/Re = {cond['nu_sim']:.3g}.",
                "turbulence_intensity": "Only k-omega SST reads this; 0.1% stands for clean flight air."},
                checks=["U, p, nut and the turbulence fields exist in 0.orig",
                        "velocity magnitude and Reynolds number within 1% of the flight condition",
                        "fixedInlet far field (freestream only for domains up to 30 chords)"]),
            "numerics": dict(numerics, reasons={
                "iterations": "1200 iterations settles the forces on this mesh; the solve stage extends if not.",
                "write_interval": "y+ and surface pressure exist only at write times, so iterations is a "
                                  "multiple of this.",
                "convection_scheme": "second-order linearUpwind; first-order upwind over-predicts drag.",
                "relax_p": "standard SIMPLE relaxation 0.3 / 0.7 / 0.7.",
                "relax_U": "0.7 for momentum, paired with 0.3 for pressure.",
                "relax_turbulence": "0.7, as for momentum.",
                "residual_p": "residual targets of 1e-6 are tight enough that the planned iteration count and "
                              "the force-convergence check end the run, not an early residual stop.",
                "residual_U": "1e-6, as for pressure: the force drift decides when the run is done.",
                "residual_turbulence": "1e-6, as for pressure: the force drift decides when the run is done.",
                "n_non_orthogonal_correctors": "0: the default mesh stays well below the 60 degrees of "
                                               "non-orthogonality where correctors help.",
                "consistent": "plain SIMPLE; SIMPLEC has not been verified with this mesh."},
                checks=["controlDict, fvSchemes and fvSolution written", "between 300 and 6000 iterations",
                        "lift and drag axes at the angle of attack, reference speed as the flight speed"]),
            "solve": {"extend_iterations": 600, "max_extensions": 3,
                      "reasons": {"extend_iterations": "extend while Cl or Cd still drift; relaxation is lowered "
                                                       "once if the run diverges."},
                      "checks": ["no fatal solver error", "Cl drift < 1e-3 and Cd drift < 1e-4 over the last 200 iterations",
                                 "the solver ran in this run of the stage"]},
            "post": {"field": "U", "zoom": "airfoil",
                     "reasons": {"field": "velocity and pressure images around the section, plus forces, y+ and Cp."},
                     "checks": ["Cl and Cd finite, Cd > 0", "average y+ inside the band", "Cp extracted"]},
            "validate": {"transition": "forced", "mach": 0.0, "flight_mach": mach,
                         "reasons": {"transition": "the RANS run is fully turbulent, so XFoil is tripped at 5% chord "
                                                   "on both surfaces; it runs on the same closed-trailing-edge "
                                                   "profile the mesh is built on.",
                                     "mach": "the RANS run is incompressible, so the reference is at Mach 0; the "
                                             "flight Mach number is reported as the compressibility effect only."},
                         "checks": ["XFoil converged, on the profile the mesh is built on, at the solved angle",
                                    "pass: |dCl| <= 5% (at least 0.01) and |dCd| <= 25%",
                                    "warn: |dCl| <= 10% (at least 0.02) and |dCd| <= 50%"]},
        },
    }


def default_plan(study, candidates=None):
    selected = candidates if candidates is not None else \
        [c for c in study.get("candidates") or [] if c.get("selected")]
    cond, req = study["conditions"], study["requirements"]
    assumptions = [
        f"Temperature {cond['temperature_K']:g} K from the standard atmosphere at density {cond['density']:g} kg/m^3; "
        f"flight speed {cond['velocity']:.4g} m/s, chord {cond['chord_m']:g} m.",
        "The simulation uses unit chord at the flight speed with nu = U/Re, so only the Reynolds number matters.",
        "Incompressible, steady, fully turbulent RANS (Spalart-Allmaras with wall functions).",
        "Each angle of attack is the angle at which XFoil (transition forced at 5% chord, Mach 0) "
        "reaches the design lift coefficient for that section.",
        "Far field at 30 chords without a vortex correction: drag is slightly over-predicted "
        "(about Cl^2 / (4 pi 30)).",
    ]
    assumptions += list(cond.get("warnings") or [])
    nacas = ", ".join("NACA " + c["naca"] for c in selected)
    return {
        "version": 1,
        "summary": f"Compare {nacas} at Re {cond['reynolds']:.3g} and design Cl {req.get('design_cl'):g}: "
                   "one RANS run per airfoil at its design-lift angle, each checked against XFoil.",
        "assumptions": assumptions,
        "approved_at": None,
        "cases": [default_case_plan(study, c) for c in selected],
    }


_PLAN_NUMBERS = {
    "mesh": {"farfield_chords": float, "wake_chords": float, "n_normal": int, "n_leading": int, "n_aft": int,
             "n_wake": int, "first_cell_height": float},
    "physics": {"nut_ratio": float, "turbulence_intensity": float},
    "numerics": {"iterations": int, "write_interval": int, "relax_p": float, "relax_U": float,
                 "relax_turbulence": float, "residual_p": float, "residual_U": float,
                 "residual_turbulence": float, "n_non_orthogonal_correctors": int},
    "solve": {"extend_iterations": int, "max_extensions": int},
    "validate": {"mach": float, "flight_mach": float},
}


# ---------------------------------------------------------------- citations --

def plan_setting_names():
    """Every 'stage.setting' name a plan has, plus 'case.alpha_deg': the names a default
    citation of the catalogue may be keyed on (refs.citations --lint and the unit tests)."""
    study = {"conditions": {"reynolds": 2.0e6, "velocity": 1.0, "nu_sim": 5.0e-7}, "requirements": {"mach": 0.0}}
    stages = default_case_plan(study, {"naca": "0012"})["stages"]
    return {"case.alpha_deg"} | {f"{key}.{k}" for key, section in stages.items()
                                 for k in section if k not in PLAN_FIXED_KEYS}


def without_cites(decisions):
    """Decisions as a later stage reads them. The hand-off text is cut at a fixed length and the
    reference ids are for the engineer's view, so they are left out."""
    return [{k: v for k, v in d.items() if k != "cites"} if isinstance(d, dict) else d for d in decisions or []]


def _engineer_set(section, name):
    return str((section.get("reasons") or {}).get(name) or "").startswith(ENGINEER_MARK)


def engineer_values(section):
    """{setting: value} of the values in a plan section that the engineer set by hand."""
    return {k: section[k] for k in (section.get("reasons") or {}) if k in section and _engineer_set(section, k)}


def _same_number(a, b):
    """True / False when both are numbers (within 1% counts as the same); None when one is not."""
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return abs(a - b) <= 0.01 * max(abs(a), abs(b))


def _at_default(stage, name, value, default):
    """Whether a plan value still is the default: equal, or for a float setting within 1%
    (the planner writes first_cell_height with four figures, the default has three)."""
    return value == default or (_PLAN_NUMBERS.get(stage, {}).get(name) is float
                                and _same_number(value, default) is True)


def _unchanged(stage, name, value, planned):
    """Whether a decision applies the plan value as it is. The rule of the plan path: within 1%
    for a float setting, exact for a count (90 against 91 is another value), words by spelling."""
    if _PLAN_NUMBERS.get(stage, {}).get(name) is float or isinstance(planned, float):
        same = _same_number(value, planned)
        if same is not None:
            return same
    elif isinstance(planned, int) and not isinstance(planned, bool) and not isinstance(value, bool):
        try:
            return float(value) == planned       # 90.0 and "90" are 90
        except (TypeError, ValueError):
            pass
    return str(value).strip().lower() == str(planned).strip().lower()


def _stored_ids(value):
    """Ids copied from a stored plan: only ones still in the catalogue, so an entry that was
    retired can never block an edit. While the catalogue cannot be read nothing is judged."""
    if not isinstance(value, list):
        return []
    judge = not citations.load()["error"]
    return [i for i in dict.fromkeys(value) if isinstance(i, str) and (not judge or citations.exists(i))]


def section_cites(naca, stage, section, base, incoming, stored, notes):
    """The `cites` map (setting -> reference ids) of one plan stage section.

    Planner path (`stored` is None): a value the engineer set has none; ids the planner gave
    are taken as written (validate_plan refuses unknown ones); a value left at its default
    gets the catalogue's default citation; a changed value without ids has none, with a note.
    Engineer path (`stored` is that section of the stored plan): the request's `cites` are
    ignored and the stored ones carried over, since the engineer never writes citations."""
    settings = [k for k in section if k not in PLAN_FIXED_KEYS]
    if stored is not None:
        kept = stored.get("cites") if isinstance(stored.get("cites"), dict) else {}
        out = {k: _stored_ids(kept.get(k)) for k in settings if not _engineer_set(section, k)}
        if stage == "physics" and section.get("turbulence_model") != stored.get("turbulence_model"):
            out.pop("nut_ratio", None)            # its references were for the other model (see below)
        return {k: ids for k, ids in out.items() if ids}
    if incoming is not None and not isinstance(incoming, dict):
        return incoming                           # kept raw: validate_plan says what is wrong with it
    # get_defaults shows the default citations keyed 'mesh.n_normal': accept that spelling here too
    prefix = stage + "."
    incoming = {(k[len(prefix):] if isinstance(k, str) and k.startswith(prefix) else k): v
                for k, v in (incoming or {}).items()}
    for k in incoming:
        if k not in settings:
            notes.append(f"{naca}.{stage}.cites.{k} was dropped: it is not a setting of {stage}")
    defaults = citations.default_section(stage)
    out = {}
    for k in settings:
        if _engineer_set(section, k):
            continue
        if incoming.get(k) is not None:
            ids, problem = citations.clean(incoming[k])
            if problem or ids:                    # [] means "no reference"
                out[k] = incoming[k] if problem else ids
            continue
        value = section[k]
        same = _at_default(stage, k, value, base.get(k))
        if (same and defaults.get(k) and stage == "physics" and k == "nut_ratio"
                and section.get("turbulence_model") != base.get("turbulence_model")):
            # The default references give the far-field value of the Spalart-Allmaras variable.
            # With another model the same number means something else, so they do not carry over.
            notes.append(f"{naca}.physics.nut_ratio = {value:g} keeps the default, whose references are for "
                         f"{base.get('turbulence_model')}: with {section.get('turbulence_model')} it is shown "
                         "without one")
        elif same and defaults.get(k):
            out[k] = defaults[k]
        elif not same and defaults.get(k):
            shown = f"{value:g}" if isinstance(value, float) else value
            notes.append(f"{naca}.{stage}.{k} = {shown} differs from the default and cites no reference: "
                         "it is shown without one")
    return out


def decision_cites(stage, decisions, plan_section, salvage=None):
    """Resolve the reference ids of a stage's decisions.
    Returns (ids per decision, error sentences, unknown ids); decisions are numbered from 1.

    A decision that applies the plan value unchanged inherits the plan's citation, so agents and
    the scripted policy need not repeat it; one that applies a value the engineer set is never
    cited, whatever was sent; ids an agent gives are checked and kept.

    A faulty `cites` leaves its decision without ids. `salvage` (a list) asks for what can be
    kept instead, when the agent has had its chances to correct them: the ids that exist, at
    most MAX_CITES of them. It receives one sentence per faulty decision that says what was
    dropped and why."""
    section = plan_section or {}
    planned = section.get("cites") if isinstance(section.get("cites"), dict) else {}
    tail = "Use one of them, or leave cites out for this decision."
    out, errors, unknown = [], [], []
    for n, d in enumerate(decisions if isinstance(decisions, list) else [], 1):
        if not isinstance(d, dict) or d.get("param") is None:
            out.append([])
            continue
        param = str(d["param"])
        unchanged = False
        if param in section and param not in PLAN_FIXED_KEYS:
            unchanged = _unchanged(stage, param, d.get("value"), section[param])
        if unchanged and _engineer_set(section, param):
            out.append([])
        elif d.get("cites") is not None:
            where = f"decision {n} ({param})"
            ids, shape = citations.clean(d["cites"])
            found = citations.check(where, d["cites"], stage, tail, noun=f"{where}: cites")
            errors.extend(found)
            if found and not shape and len(ids) <= citations.MAX_CITES:
                unknown.extend(i for i in ids if not citations.exists(i) and i not in unknown)
            if found and salvage is not None:
                ids = _salvaged(where, ids, shape, salvage)
            out.append(ids if salvage is not None or not found else [])
        else:
            out.append(_stored_ids(planned.get(param)) if unchanged else [])
    return out, errors, unknown


def _salvaged(where, ids, shape, notes):
    """The ids of one faulty citation that can stand, with the sentence that says what went."""
    if shape:
        notes.append(f"{where}: cites dropped, they were not a list of reference ids")
        return []
    if citations.load()["error"]:
        notes.append(f"{where}: cites dropped, the reference catalogue could not be read")
        return []
    known = [i for i in ids if citations.exists(i)]
    gone = [i for i in ids if i not in known]
    parts = []
    if gone:
        parts.append(f"unknown id{'s' if len(gone) > 1 else ''} {', '.join(gone)} dropped")
    if len(known) > citations.MAX_CITES:
        parts.append(f"{len(known)} references cited, the first {citations.MAX_CITES} kept")
    notes.append(f"{where}: " + "; ".join(parts))
    return known[:citations.MAX_CITES]


def _by_naca(plan):
    return {str(pc.get("naca")): pc for pc in (plan or {}).get("cases") or [] if isinstance(pc, dict)}


def _settle_reasons(naca, stage, section, base, src, stored, marked, notes):
    """Make every reason of one plan stage section describe the value beside it.

    The section starts from the default reasons, which are worded around the default numbers:
    one that the request did not replace is removed when the value is no longer the default.
    A request reason that is the default reason word for word counts as not given: a planner
    that starts from get_defaults carries every one of them along with the values it changes.
    Engineer paths (`stored` is that section of the stored plan): a value left as it was keeps
    its stored reason when the request gives none, and always when the engineer set it, so a
    request that carries values only cannot take the engineer's mark (and with it the lock) off.
    Planner path (`marked` is that section of the stored plan, {} for a first plan): the mark
    belongs to the value. It stays on a value that is still the one the engineer set, whatever
    reason the planner sent, and a mark the planner copied onto a value of its own is removed,
    since it would lock and uncite a value the engineer never set."""
    given = src.get("reasons") if isinstance(src.get("reasons"), dict) else {}
    reasons = section["reasons"] = dict(section.get("reasons") or {})
    default_reasons = base.get("reasons") or {}
    for k in [k for k in section if k not in PLAN_FIXED_KEYS]:
        at_default = _at_default(stage, k, section[k], base.get(k))
        own = given.get(k)
        if own and str(own) == default_reasons.get(k):
            own = None
        if not at_default and not own:
            reasons.pop(k, None)
        if stored is not None and k in stored and stored[k] == section[k]:
            kept = (stored.get("reasons") or {}).get(k) if isinstance(stored.get("reasons"), dict) else None
            if kept and (not own or str(kept).startswith(ENGINEER_MARK)):
                reasons[k] = str(kept)
        if marked is not None and _engineer_set(marked, k) and marked.get(k) == section[k]:
            reasons[k] = str(marked["reasons"][k])
        if marked is not None and _engineer_set(section, k) \
                and not (_engineer_set(marked, k) and marked.get(k) == section[k]):
            if at_default and default_reasons.get(k):
                reasons[k] = default_reasons[k]
            else:
                reasons.pop(k, None)
            notes.append(f"{naca}.{stage}.{k}: the reason '{ENGINEER_MARK}' was removed, because the engineer "
                         f"did not set {section[k]}; give your own reason for a value you choose")


def normalise_plan(study, plan, keep_alpha=False, cites_from=None, marks_from=None):
    """Fill a (possibly partial) plan from an agent or the UI with defaults,
    coerce number types, and keep only the selected airfoils.

    The angle of attack always comes from the XFoil screen unless `keep_alpha`
    (a human edit) says otherwise. `cites_from` is the stored plan on the engineer
    paths (PATCH /plan, update_plan): citations are then carried over from it and
    never read from the request (see section_cites), and so is the reason of every
    value the request leaves as it was. `marks_from` is the stored plan (or {}) when
    a planner writes: a 'Set by the engineer' reason then counts only on the value
    the engineer really set. Returns (plan, problems[])."""
    problems = []
    stored_cases = _by_naca(cites_from) if cites_from is not None else None
    marked_cases = _by_naca(marks_from) if marks_from is not None else None
    cond = study["conditions"]
    selected = [c for c in study.get("candidates") or [] if c.get("selected")]
    base = default_plan(study, selected)
    given = {}
    for pc in (plan or {}).get("cases") or []:
        try:
            given[ac.naca4(pc.get("naca"))[0]] = pc
        except (ValueError, AttributeError):
            problems.append(f"ignored a plan case with an invalid airfoil: {pc.get('naca') if isinstance(pc, dict) else pc!r}")
    for code in given:
        if code not in [c["naca"] for c in base["cases"]]:
            problems.append(f"NACA {code} is not one of the selected airfoils and was left out")
    cases = []
    for bc in base["cases"]:
        pc = given.get(bc["naca"]) or {}
        if not pc:
            problems.append(f"no plan given for NACA {bc['naca']}: defaults used")
        out = {"naca": bc["naca"], "alpha_deg": bc["alpha_deg"], "stages": {}}
        if keep_alpha and pc.get("alpha_deg") is not None:
            try:
                out["alpha_deg"] = float(pc["alpha_deg"])
            except (TypeError, ValueError):
                problems.append(f"alpha_deg for NACA {bc['naca']} is not a number: kept {bc['alpha_deg']}")
        elif pc.get("alpha_deg") is not None and bc["alpha_deg"] is not None:
            try:
                if abs(float(pc["alpha_deg"]) - bc["alpha_deg"]) > 0.01:
                    problems.append(f"alpha_deg for NACA {bc['naca']} is fixed by the XFoil screen at "
                                    f"{bc['alpha_deg']:g} deg (the angle that gives the design Cl)")
            except (TypeError, ValueError):
                pass
        stored = stored_cases.get(bc["naca"]) or {} if stored_cases is not None else None
        for key in STAGE_KEYS:
            section = dict(bc["stages"][key])
            src = (pc.get("stages") or {}).get(key)
            if isinstance(src, dict):
                for k, v in src.items():
                    if k == "cites":
                        continue                  # not a setting: section_cites below reads it
                    if k == "reasons":
                        if isinstance(v, dict):
                            section["reasons"] = dict(section.get("reasons") or {},
                                                      **{str(a): str(b) for a, b in v.items() if b})
                        continue
                    if k == "checks":
                        if isinstance(v, list) and v:
                            section["checks"] = [str(c) for c in v]
                        continue
                    if v is None or isinstance(v, (dict, list)):
                        continue
                    if key == "mesh" and k == "target_yplus":
                        # Not a setting: nothing reads it. The wall spacing has one knob,
                        # first_cell_height, so the two can never contradict each other.
                        try:
                            wanted = float(v)
                            height = float(src.get("first_cell_height") or section["first_cell_height"])
                            now = predicted_yplus(height, cond["reynolds"], cond["velocity"])
                            if wanted > 0 and abs(wanted - now) > 0.1 * now:
                                need = ac.first_cell_height(cond["reynolds"], cond["velocity"], wanted)["first_cell_height"]
                                problems.append(
                                    f"{bc['naca']}.mesh.target_yplus = {wanted:g} was not applied: it is not a setting. "
                                    f"first_cell_height = {height:.3g} gives y+ about {now:.3g}; for y+ {wanted:g} "
                                    f"set first_cell_height = {need:.3g}")
                        except (TypeError, ValueError):
                            pass
                        continue
                    cast = _PLAN_NUMBERS.get(key, {}).get(k)
                    if cast:
                        try:
                            v = cast(round(float(v))) if cast is int else float(v)
                        except (TypeError, ValueError):
                            problems.append(f"{bc['naca']}.{key}.{k} = {v!r} is not a number: default kept")
                            continue
                    section[k] = v
            if key == "geometry":
                section["naca"] = bc["naca"]
            if not isinstance(src, dict):
                src = {}
            stored_section = None if stored is None else ((stored.get("stages") or {}).get(key) or {})
            marked = None if marked_cases is None else \
                (((marked_cases.get(bc["naca"]) or {}).get("stages") or {}).get(key) or {})
            _settle_reasons(bc["naca"], key, section, bc["stages"][key], src, stored_section, marked, problems)
            section["cites"] = section_cites(bc["naca"], key, section, bc["stages"][key], src.get("cites"),
                                             stored_section, problems)
            out["stages"][key] = section
        # The angle has one citation at case level. On the planner path it is always the XFoil
        # screen's angle; on the engineer paths the stored one is carried over.
        if stored is None:
            angle = citations.default_cites("case", "alpha_deg") if out["alpha_deg"] is not None else []
        else:
            angle = _stored_ids((stored.get("cites") or {}).get("alpha_deg")
                                if isinstance(stored.get("cites"), dict) else None)
        out["cites"] = {"alpha_deg": angle} if angle else {}
        cases.append(out)
    result = {
        "version": int((plan or {}).get("version") or base["version"]),
        "summary": str((plan or {}).get("summary") or base["summary"]),
        "assumptions": [str(a) for a in ((plan or {}).get("assumptions") or base["assumptions"])],
        "approved_at": None,
        "cases": cases,
    }
    return result, problems


def plan_matches_selection(study):
    """True when the plan covers exactly the airfoils that are selected."""
    plan = study.get("plan") or {}
    return sorted(pc.get("naca") for pc in plan.get("cases") or []) == \
        sorted(c["naca"] for c in study.get("candidates") or [] if c.get("selected"))


def mark_engineer_edits(old_plan, new_plan):
    """Give every setting the engineer changed a reason that says so. Stage
    agents treat such values as instructions, not as planner defaults (the
    planner's old reason no longer describes the value). Its citation goes
    too: the reference supported the old value, and the new one is the
    engineer's."""
    old_cases = {pc["naca"]: pc for pc in (old_plan or {}).get("cases") or []}
    changed = []
    for pc in new_plan.get("cases") or []:
        old_case = old_cases.get(pc["naca"]) or {}
        if old_case and old_case.get("alpha_deg") != pc.get("alpha_deg") and isinstance(pc.get("cites"), dict):
            pc["cites"].pop("alpha_deg", None)
        before = old_case.get("stages") or {}
        for key, section in pc["stages"].items():
            previous = before.get(key) or {}
            for name, value in section.items():
                if name in PLAN_FIXED_KEYS or name not in previous or previous[name] == value:
                    continue
                section.setdefault("reasons", {})[name] = f"{ENGINEER_MARK} (was {previous[name]})."
                if isinstance(section.get("cites"), dict):
                    section["cites"].pop(name, None)
                changed.append(f"{pc['naca']}.{key}.{name}")
    return changed


def plan_changes(old_plan, new_plan):
    """The airfoils whose plan differs between two versions of it: their own case (the angle
    or a value of a stage section), or a value outside `cases`, which every case shares. An
    edit is help only for these. Citations and the version stamps are bookkeeping, not values."""
    def shared(plan):
        return {k: v for k, v in plan.items() if k not in ("cases", "version", "approved_at")}

    def own(case):
        out = {k: v for k, v in case.items() if k not in ("stages", "cites")}
        out["stages"] = {key: {k: v for k, v in section.items() if k != "cites"} if isinstance(section, dict)
                         else section for key, section in (case.get("stages") or {}).items()}
        return out
    old_plan, new_plan = old_plan or {}, new_plan or {}
    old_cases = {pc["naca"]: pc for pc in old_plan.get("cases") or []}
    new_cases = {pc["naca"]: pc for pc in new_plan.get("cases") or []}
    every = set(old_cases) | set(new_cases)
    if shared(old_plan) != shared(new_plan):
        return every
    return {naca for naca in every
            if naca not in old_cases or naca not in new_cases or own(old_cases[naca]) != own(new_cases[naca])}


def cite_errors(plan):
    """Citation errors of a plan a model wrote: every `cites` map must be an object of
    setting -> at most three ids that exist in the reference catalogue."""
    errors = []
    tail = "Use one of them, or leave cites out for this setting."
    for pc in plan["cases"]:
        tag = f"NACA {pc['naca']}"
        maps = [("cites", "physics", pc.get("cites"))]       # case level: the angle, applied by physics
        maps += [(f"{key}.cites", key, section.get("cites")) for key, section in pc["stages"].items()]
        for name, stage, cites in maps:
            if cites is None:
                continue                          # a plan from before citations existed
            if not isinstance(cites, dict):
                errors.append(f"{tag}: {name} must be an object of setting -> list of reference ids")
                continue
            for setting, value in cites.items():
                errors.extend(citations.check(f"{tag}: {name}.{setting}", value, stage, tail))
    return errors


def validate_plan(plan, cites=True):
    """Hard errors that would make a stage fail for certain. `cites` False skips the
    citations: on the engineer paths and at approval they are the stored plan's own, and an
    entry that has since left the catalogue must not block an edit or a run."""
    errors = cite_errors(plan) if cites else []
    for pc in plan["cases"]:
        tag = f"NACA {pc['naca']}"
        if pc.get("alpha_deg") is None:
            errors.append(f"{tag}: no angle of attack (the XFoil screen did not reach the design lift coefficient)")
        elif not (-10.0 <= float(pc["alpha_deg"]) <= 20.0):
            errors.append(f"{tag}: alpha_deg {pc['alpha_deg']} is outside -10 to 20 deg")
        s = pc["stages"]
        try:
            cells = ac.mesh_plan({k: s["mesh"][k] for k in MESH_KEYS})["cells"]
            if cells > MAX_CELLS:
                errors.append(f"{tag}: {cells:,} cells is above the limit of {MAX_CELLS:,}")
            for k, (lo, hi) in MESH_LIMITS.items():
                if not (lo <= float(s["mesh"][k]) <= hi):
                    errors.append(f"{tag}: mesh.{k} = {s['mesh'][k]} is outside {lo:g} to {hi:g}")
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"{tag}: mesh settings are incomplete ({e})")
        if s["physics"].get("turbulence_model") not in ("SpalartAllmaras", "kOmegaSST"):
            errors.append(f"{tag}: turbulence_model must be SpalartAllmaras or kOmegaSST")
        if s["physics"].get("wall_treatment") not in ("wallFunction", "resolved"):
            errors.append(f"{tag}: wall_treatment must be wallFunction or resolved")
        if s["physics"].get("farfield_bc") not in ("fixedInlet", "freestream"):
            errors.append(f"{tag}: farfield_bc must be fixedInlet or freestream")
        if s["numerics"].get("convection_scheme") not in ("linearUpwind", "upwind"):
            errors.append(f"{tag}: convection_scheme must be linearUpwind or upwind")
        it = s["numerics"].get("iterations")
        if not (isinstance(it, int) and 300 <= it <= 6000):
            errors.append(f"{tag}: iterations must be between 300 and 6000")
        # the same ranges set_physics / set_numerics enforce, so a bad value is refused at the
        # edit and not only when the stage runs
        for key in ("physics", "numerics"):
            errors.extend(f"{tag}: {key}.{problem}" for problem in setting_problems(key, s[key]))
    return errors
