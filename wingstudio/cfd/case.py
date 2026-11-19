"""Deterministic building blocks for a 2D NACA airfoil RANS case: every function takes
explicit parameters and writes OpenFOAM files or parses OpenFOAM output. Nothing here calls
a model or makes a decision. Standard library only.

Conventions (inherited from the OpenFOAM NACA0012 tutorial mesh):
  chord = 1 m along x, z is up, y is the thin (span) direction, span = 0.1 m,
  patches: aerofoil (wall), inlet + outlet (group freestream), back/front (empty).
The case is run at unit chord with the physical flight speed, and the
kinematic viscosity is chosen so that the Reynolds number matches:
  nu_sim = U * 1 m / Re.
"""
import functools
import glob
import json
import math
import os
import re
import shutil

from foamtools.common import NUM, read_json, read_text, write_json
from foamtools.naca import naca4, naca4_coords, naca4_point, write_obj

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "templates")
SPAN = 0.1          # m, thickness of the one-cell-thick mesh
X_SPLIT = 0.3       # chordwise station where the leading-edge block ends


# ------------------------------------------------------------ flight state --

def derive_conditions(mach, density, reynolds, temperature=None):
    """Turn (Mach, density, Re) into a full flow state.

    Temperature defaults to the ISA temperature at the altitude whose ISA
    density equals `density` (troposphere), falling back to 288.15 K.
    Viscosity from Sutherland's law."""
    gamma, R = 1.4, 287.05
    if temperature is None:
        temperature = isa_temperature_from_density(density)
    a = math.sqrt(gamma * R * temperature)
    U = mach * a
    mu = 1.716e-5 * (temperature / 273.15) ** 1.5 * (273.15 + 110.4) / (temperature + 110.4)
    nu = mu / density
    chord = reynolds * nu / U if U > 0 else float("nan")
    q = 0.5 * density * U * U
    return {
        "mach": mach, "density": density, "reynolds": reynolds,
        "temperature_K": round(temperature, 2),
        "pressure_Pa": round(density * R * temperature, 1),
        "speed_of_sound": round(a, 2), "velocity": round(U, 3),
        "dynamic_viscosity": mu, "kinematic_viscosity": nu,
        "chord_m": round(chord, 4), "dynamic_pressure_Pa": round(q, 2),
        "nu_sim": U / reynolds,            # unit-chord simulation viscosity
        "compressibility_factor": 1.0 / math.sqrt(max(1e-9, 1.0 - mach * mach)),
        "incompressible_ok": mach <= 0.3,
    }


def isa_temperature_from_density(density):
    """Invert the ISA troposphere density profile; clamp to 0-11 km."""
    T0, rho0, L, g, R = 288.15, 1.225, 0.0065, 9.80665, 287.05
    n = g / (R * L) - 1.0                       # rho/rho0 = (T/T0)^n
    ratio = max(1e-6, density / rho0)
    T = T0 * ratio ** (1.0 / n)
    return min(T0, max(216.65, T))


# ---------------------------------------------------------------- geometry --

def geometry_summary(code):
    code, m, p, t = naca4(code)
    upper, lower = naca4_coords(code, 200)
    zmax = max(z for _, z in upper)
    zmin = min(z for _, z in lower)
    return {"naca": code, "max_camber": m, "camber_position": p, "thickness": t,
            "z_max": round(zmax, 5), "z_min": round(zmin, 5),
            "description": (f"NACA {code}: {m*100:.0f}% camber at {p*100:.0f}% chord, "
                            f"{t*100:.0f}% thick" if (m and p) else
                            f"NACA {code}: symmetric, {t*100:.0f}% thick")}


def write_geometry(case_dir, code, n=400):
    """Write constant/geometry/NACA<code>.obj (ribbon for blockMesh projection)
    and geometry.json (profile coordinates for the UI and the geometry gate).
    XFoil does not read these files: xfoil.py samples the same naca4_coords
    profile itself, so both solvers run on one closed-trailing-edge shape."""
    code = naca4(code)[0]
    upper, lower = write_obj(case_dir, code, n)
    summary = geometry_summary(code)
    step = max(1, n // 100)
    with open(os.path.join(case_dir, "geometry.json"), "w") as f:
        json.dump(dict(summary, upper=[[round(x, 5), round(z, 5)] for x, z in upper[::step]],
                       lower=[[round(x, 5), round(z, 5)] for x, z in lower[::step]]), f)
    return summary


# -------------------------------------------------------------------- mesh --

def _expansion(first, length, n):
    """Total expansion ratio (last/first cell) for n cells of geometric growth
    spanning `length` with first cell `first`; also returns the growth rate."""
    if n < 2 or first * n >= length:
        return 1.0, 1.0
    lo, hi = 1.0 + 1e-9, 3.0
    for _ in range(200):
        r = 0.5 * (lo + hi)
        total = first * (r**n - 1) / (r - 1)
        if total > length:
            hi = r
        else:
            lo = r
    r = 0.5 * (lo + hi)
    return r ** (n - 1), r


def first_cell_height(reynolds, velocity, target_yplus):
    """Flat-plate estimate of the wall-normal height of the first cell on unit chord
    (blockMesh sets the cell height; the cell centre sits at half of it)."""
    nu = velocity / reynolds
    cf = 0.026 / reynolds ** (1.0 / 7.0)
    u_tau = velocity * math.sqrt(cf / 2.0)
    y_centre = target_yplus * nu / u_tau
    return {"first_cell_height": 2.0 * y_centre, "cell_centre_height": y_centre,
            "cf_estimate": cf, "u_tau": u_tau}


DEFAULT_MESH = {
    # 30 chords is the largest far field this block topology runs stably: the
    # columns of cells above/below the aft part of the airfoil keep their
    # chordwise width out to the boundary, and beyond ~30 chords they become
    # so tall and thin that SIMPLE diverges within ~50 iterations.
    "farfield_chords": 30.0,   # radius of the C and half-height of the wake box
    "wake_chords": 30.0,       # outlet distance behind the trailing edge
    "n_normal": 90,            # cells from the wall to the far field
    "n_leading": 40,           # cells along each surface, leading edge to 30% chord
    "n_aft": 40,               # cells along each surface, 30% chord to trailing edge
    "n_wake": 50,              # cells along the wake
    "first_cell_height": 1.0e-3,
    "leading_edge_grading": 0.2,
}


def farfield_drag_estimate(cl, farfield_chords):
    """Induced-like drag added by truncating the domain at radius R with a
    uniform far-field velocity (no point-vortex correction): about Cl^2/(4 pi R)."""
    return cl * cl / (4.0 * math.pi * float(farfield_chords))


def mesh_plan(params):
    """Resolve mesh parameters into blockMesh numbers and predicted properties."""
    p = dict(DEFAULT_MESH)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    R = float(p["farfield_chords"])
    zg, growth = _expansion(float(p["first_cell_height"]), R, int(p["n_normal"]))
    aft_cell = (1.0 - X_SPLIT) / int(p["n_aft"])
    xdg, wake_growth = _expansion(aft_cell, float(p["wake_chords"]), int(p["n_wake"]))
    cells = 2 * (int(p["n_leading"]) + int(p["n_aft"]) + int(p["n_wake"])) * int(p["n_normal"])
    return {
        "params": p,
        "cells": cells,
        "surface_cells": 2 * (int(p["n_leading"]) + int(p["n_aft"])),
        "wall_normal_growth": round(growth, 4),
        "wake_growth": round(wake_growth, 4),
        "z_grading": zg, "x_wake_grading": xdg,
        "x_min": X_SPLIT - R, "x_max": 1.0 + float(p["wake_chords"]),
        "z_extent": R,
    }


# The four wall edges of the C-mesh: (vertices in the y = -0.1 plane, vertices in the
# y = +0.1 plane, chord station of the first vertex, of the second, upper surface?).
_WALL_EDGES = (
    ((4, 7), (16, 19), 0.0, X_SPLIT, False),
    ((7, 5), (19, 17), X_SPLIT, 1.0, False),
    ((4, 8), (16, 20), 0.0, X_SPLIT, True),
    ((8, 5), (20, 17), X_SPLIT, 1.0, True),
)


def _dist2(poly, px, pz):
    """Squared distance from (px, pz) to the polyline `poly`."""
    best = float("inf")
    for (ax, az), (bx, bz) in zip(poly, poly[1:]):
        dx, dz = bx - ax, bz - az
        l2 = dx * dx + dz * dz
        s = ((px - ax) * dx + (pz - az) * dz) / l2 if l2 > 0 else 0.0
        s = 0.0 if s < 0 else 1.0 if s > 1 else s
        ex, ez = ax + s * dx - px, az + s * dz - pz
        best = min(best, ex * ex + ez * ez)
    return best


@functools.lru_cache(maxsize=512)
def projection_is_clean(code, x0, x1, upper, samples=200):
    """Whether a blockMesh `project` edge between the surface points at chord stations x0 and
    x1 follows the surface it is meant for. blockMesh starts from the straight line between the
    two vertices and moves every point to the nearest point of the whole profile. That chord
    lies inside the section; where it runs on the far side of the mean line (the aft chord of a
    thin, strongly cambered section such as NACA 5412 next to the trailing edge, the forward
    chord of a section with its camber far forward next to the leading edge) the nearest point
    is on the other surface and the wall edge crosses the section. The chord is sampled
    densely towards both ends, where the two surfaces meet, so the answer does not depend on
    the cell counts."""
    side = 0 if upper else 1
    profile = naca4_coords(code, 400)                 # the stations of the OBJ ribbon
    own, other = profile[side], profile[1 - side]
    (ax, az), (bx, bz) = naca4_point(code, x0)[side], naca4_point(code, x1)[side]
    for i in range(1, samples):
        s = 0.5 * (1 - math.cos(math.pi * i / samples))
        px, pz = ax + s * (bx - ax), az + s * (bz - az)
        if _dist2(other, px, pz) < _dist2(own, px, pz) - 1e-12:
            return False
    return True


def _wall_polyline(code, x0, x1, upper, y, n=200):
    """`n - 1` profile points strictly between the chord stations x0 and x1 as the body of a
    blockMesh polyLine edge; next to the leading edge they are clustered towards it."""
    rows = []
    for i in range(1, n):
        s = i / n
        x = x1 * s * s if x0 == 0.0 else x0 + (x1 - x0) * s
        px, pz = naca4_point(code, x)[0 if upper else 1]
        rows.append(f"        ({px:.8f} {y} {pz:.8f})")
    return "\n    (\n" + "\n".join(rows) + "\n    )"


def write_mesh_dict(case_dir, code, params=None):
    """Write system/blockMeshDict (C-mesh projected onto the airfoil) and
    system/extrudeMeshDict from the template. Returns the resolved mesh plan."""
    code = naca4(code)[0]
    plan = mesh_plan(params)
    p = plan["params"]
    (xu, zu), (xl, zl) = naca4_point(code, X_SPLIT)
    values = {
        "xMin": plan["x_min"], "xMax": plan["x_max"],
        "zMin": -plan["z_extent"], "zMax": plan["z_extent"],
        "zCells": int(p["n_normal"]), "xUCells": int(p["n_leading"]),
        "xMCells": int(p["n_aft"]), "xDCells": int(p["n_wake"]),
        "zGrading": plan["z_grading"], "xUGrading": 5,
        "leadGrading": float(p["leading_edge_grading"]),
        "xDGrading": plan["x_wake_grading"],
        "xUpper": xu, "zUpper": zu, "xLower": xl, "zLower": zl,
    }
    text = read_text(os.path.join(TEMPLATE, "blockMeshDict"))
    for key, val in values.items():
        sval = str(val) if isinstance(val, int) else f"{val:.8g}"
        text, n = re.subn(rf"(\b{key}\s+){NUM}\s*;", rf"\g<1>{sval};", text, count=1)
        if n != 1:
            raise RuntimeError(f"template blockMeshDict has no entry {key}")
    text, n = re.subn(r'file\s+"NACA\d{4}\.obj";', f'file   "NACA{code}.obj";', text)
    if n != 1:
        raise RuntimeError("template blockMeshDict has no geometry file entry")
    # A wall edge whose nearest-point projection would cross to the other surface is written
    # as an explicit polyLine through the profile instead (see projection_is_clean). Every
    # other edge, and so every section that projects cleanly, keeps the template's text.
    for back, front, x0, x1, upper in _WALL_EDGES:
        if projection_is_clean(code, x0, x1, upper):
            continue
        for (v0, v1), y in ((back, -0.1), (front, 0.1)):
            edge = f"polyLine {v0} {v1}" + _wall_polyline(code, x0, x1, upper, y)
            text, n = re.subn(rf"project\s+{v0}\s+{v1}\s+\(aerofoil\)", lambda _m: edge, text, count=1)
            if n != 1:
                raise RuntimeError(f"template blockMeshDict has no projected edge {v0} {v1}")
    os.makedirs(os.path.join(case_dir, "system"), exist_ok=True)
    with open(os.path.join(case_dir, "system", "blockMeshDict"), "w") as f:
        f.write(text)
    shutil.copy(os.path.join(TEMPLATE, "extrudeMeshDict"),
                os.path.join(case_dir, "system", "extrudeMeshDict"))
    write_json(os.path.join(case_dir, "mesh_plan.json"), plan)
    return plan


MESH_COMMANDS = [
    ["blockMesh"],
    ["transformPoints", "-scale", "(1 0 1)"],
    ["extrudeMesh"],
]


# ----------------------------------------------------------------- physics --

def _header(cls, obj, note=""):
    return (
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
        f"    class       {cls};\n    object      {obj};\n}}\n"
        + (f"// {note}\n" if note else "") + "\n")


_CONSTRAINTS = '    #includeEtc "caseDicts/setConstraintTypes"\n}\n'


def write_physics(case_dir, velocity, alpha_deg, reynolds, turbulence_model="SpalartAllmaras",
                  nut_ratio=None, turbulence_intensity=0.001, wall_treatment="wallFunction",
                  farfield_bc="fixedInlet"):
    """Write constant/ properties and the 0.orig boundary/initial fields.

    turbulence_model: SpalartAllmaras | kOmegaSST
    nut_ratio: freestream eddy-viscosity ratio, > 0 (default 4 for SA as nuTilda/nu,
               1 for k-omega SST as nut/nu, where it also sets omega = k / (nut_ratio nu))
    turbulence_intensity: free-stream intensity as a fraction (k-omega SST only)
    wall_treatment: wallFunction (any y+, uses nutUSpaldingWallFunction) or
                    resolved (y+ about 1, nut fixed at 0 on the wall)
    farfield_bc: fixedInlet (velocity fixed on the C + top + bottom boundary,
                 pressure fixed on the outlet plane; robust for far fields of
                 tens to hundreds of chords) or freestream (mixed
                 freestreamVelocity/freestreamPressure on the whole outer
                 boundary; only stable for small domains, about 30 chords or less)
    """
    if farfield_bc not in ("fixedInlet", "freestream"):
        raise ValueError("farfield_bc must be fixedInlet or freestream")
    if turbulence_model not in ("SpalartAllmaras", "kOmegaSST"):
        raise ValueError("turbulence_model must be SpalartAllmaras or kOmegaSST")
    if wall_treatment not in ("wallFunction", "resolved"):
        raise ValueError("wall_treatment must be wallFunction or resolved")
    # Every number is checked before anything on disk is touched: a bad value must not
    # leave a half-written 0.orig behind.
    default_ratio = 4.0 if turbulence_model == "SpalartAllmaras" else 1.0
    try:
        ratio = default_ratio if nut_ratio is None else float(nut_ratio)
        turbulence_intensity = float(turbulence_intensity)
        velocity, alpha_deg, reynolds = float(velocity), float(alpha_deg), float(reynolds)
    except (TypeError, ValueError):
        raise ValueError("velocity, alpha_deg, reynolds, nut_ratio and turbulence_intensity must be numbers")
    if not (math.isfinite(ratio) and ratio > 0):
        raise ValueError("nut_ratio must be a positive number (free-stream eddy viscosity over nu)")
    if not (math.isfinite(turbulence_intensity) and 0 < turbulence_intensity < 1):
        raise ValueError("turbulence_intensity must be a fraction between 0 and 1 (0.001 is 0.1%)")
    if not (math.isfinite(velocity) and velocity > 0 and math.isfinite(reynolds) and reynolds > 0
            and math.isfinite(alpha_deg)):
        raise ValueError("velocity and reynolds must be positive and alpha_deg finite")
    a = math.radians(alpha_deg)
    ux, uz = velocity * math.cos(a), velocity * math.sin(a)
    nu = velocity / reynolds
    note = (f"U = {velocity:g} m/s at {alpha_deg:g} deg, unit chord, Re = {reynolds:g}, "
            f"{turbulence_model}, {wall_treatment}")
    zero = os.path.join(case_dir, "0.orig")
    shutil.rmtree(zero, ignore_errors=True)
    os.makedirs(zero)
    os.makedirs(os.path.join(case_dir, "constant"), exist_ok=True)

    def field(name, cls, dims, internal, freestream, wall):
        """`freestream` is the mixed far-field entry; with fixedInlet it is
        replaced by a Dirichlet inlet and an inletOutlet/zero-gradient outlet."""
        if farfield_bc == "freestream":
            outer = "    freestream\n    {\n" + freestream + "    }\n"
        elif name == "p":
            outer = ("    inlet\n    {\n        type            zeroGradient;\n    }\n"
                     "    outlet\n    {\n        type            fixedValue;\n"
                     "        value           uniform 0;\n    }\n")
        elif name == "nut":
            outer = ("    \"(inlet|outlet)\"\n    {\n        type            calculated;\n"
                     f"        value           uniform {internal};\n    }}\n")
        else:
            outer = ("    inlet\n    {\n        type            fixedValue;\n"
                     f"        value           uniform {internal};\n    }}\n"
                     "    outlet\n    {\n        type            inletOutlet;\n"
                     f"        inletValue      uniform {internal};\n"
                     f"        value           uniform {internal};\n    }}\n")
        with open(os.path.join(zero, name), "w") as f:
            f.write(_header(cls, name, note))
            f.write(f"dimensions      {dims};\ninternalField   uniform {internal};\n\n"
                    "boundaryField\n{\n" + outer +
                    "    wall\n    {\n" + wall + "    }\n" + _CONSTRAINTS)

    uinf = f"({ux:.6f} 0 {uz:.6f})"
    field("U", "volVectorField", "[0 1 -1 0 0 0 0]", uinf,
          f"        type            freestreamVelocity;\n"
          f"        freestreamValue uniform {uinf};\n        value           uniform {uinf};\n",
          "        type            noSlip;\n")
    field("p", "volScalarField", "[0 2 -2 0 0 0 0]", "0",
          "        type            freestreamPressure;\n"
          "        freestreamValue uniform 0;\n        value           uniform 0;\n",
          "        type            zeroGradient;\n")
    nut_wall = ("        type            nutUSpaldingWallFunction;\n        value           uniform 0;\n"
                if wall_treatment == "wallFunction" else
                "        type            fixedValue;\n        value           uniform 0;\n")

    summary = {"turbulence_model": turbulence_model, "wall_treatment": wall_treatment,
               "farfield_bc": farfield_bc,
               "U": [round(ux, 5), 0, round(uz, 5)], "nu": nu}
    if turbulence_model == "SpalartAllmaras":
        nt = ratio * nu
        field("nuTilda", "volScalarField", "[0 2 -1 0 0 0 0]", f"{nt:.6g}",
              f"        type            freestream;\n        freestreamValue uniform {nt:.6g};\n"
              f"        value           uniform {nt:.6g};\n",
              "        type            fixedValue;\n        value           uniform 0;\n")
        field("nut", "volScalarField", "[0 2 -1 0 0 0 0]", f"{nu:.6g}",
              f"        type            calculated;\n        value           uniform {nu:.6g};\n",
              nut_wall)
        summary.update({"nuTilda_inf": nt, "nuTilda_over_nu": ratio})
    else:
        k = 1.5 * (velocity * turbulence_intensity) ** 2
        omega = k / (ratio * nu)
        field("k", "volScalarField", "[0 2 -2 0 0 0 0]", f"{k:.6g}",
              f"        type            freestream;\n        freestreamValue uniform {k:.6g};\n"
              f"        value           uniform {k:.6g};\n",
              ("        type            kqRWallFunction;\n" if wall_treatment == "wallFunction" else
               "        type            fixedValue;\n") + f"        value           uniform {1e-10 if wall_treatment == 'resolved' else k:.6g};\n")
        field("omega", "volScalarField", "[0 0 -1 0 0 0 0]", f"{omega:.6g}",
              f"        type            freestream;\n        freestreamValue uniform {omega:.6g};\n"
              f"        value           uniform {omega:.6g};\n",
              f"        type            omegaWallFunction;\n        value           uniform {omega:.6g};\n")
        field("nut", "volScalarField", "[0 2 -1 0 0 0 0]", f"{ratio * nu:.6g}",
              f"        type            calculated;\n        value           uniform {ratio * nu:.6g};\n",
              nut_wall)
        summary.update({"k_inf": k, "omega_inf": omega, "nut_over_nu": ratio,
                        "turbulence_intensity": turbulence_intensity})

    with open(os.path.join(case_dir, "constant", "transportProperties"), "w") as f:
        f.write(_header("dictionary", "transportProperties", note))
        f.write(f"transportModel  Newtonian;\n\nnu              {nu:.8g};\n")
    with open(os.path.join(case_dir, "constant", "turbulenceProperties"), "w") as f:
        f.write(_header("dictionary", "turbulenceProperties", note))
        f.write("simulationType  RAS;\n\nRAS\n{\n"
                f"    RASModel        {turbulence_model};\n"
                "    turbulence      on;\n    printCoeffs     on;\n}\n")
    write_json(os.path.join(case_dir, "physics.json"),
               dict(summary, velocity=velocity, alpha_deg=alpha_deg, reynolds=reynolds))
    return summary


# ---------------------------------------------------------------- numerics --

DEFAULT_NUMERICS = {
    "iterations": 1200,
    "write_interval": 400,
    "convection_scheme": "linearUpwind",     # linearUpwind | upwind
    "relax_p": 0.3, "relax_U": 0.7, "relax_turbulence": 0.7,
    # 1e-6: with 1e-5 a run at Re 4e5 ended on residualControl at iteration 385 while Cl
    # still moved by 0.009 over 200 iterations; at 1e-6 it ended at 505 with the forces
    # settled. The planned iteration count and the force-convergence gate should decide.
    "residual_p": 1e-6, "residual_U": 1e-6, "residual_turbulence": 1e-6,
    "n_non_orthogonal_correctors": 0,
    "consistent": False,                     # SIMPLEC
    "p_solver": "GAMG",                      # GAMG | PCG
    "p_rel_tol": 0.05,
}


def write_numerics(case_dir, velocity, alpha_deg, turbulence_model="SpalartAllmaras", params=None):
    """Write system/controlDict (with force coefficients, y+ and surface
    pressure sampling), system/fvSchemes and system/fvSolution."""
    n = dict(DEFAULT_NUMERICS)
    n.update({k: v for k, v in (params or {}).items() if v is not None})
    if n["convection_scheme"] not in ("linearUpwind", "upwind"):
        raise ValueError("convection_scheme must be linearUpwind or upwind")
    turb = ["nuTilda"] if turbulence_model == "SpalartAllmaras" else ["k", "omega"]
    a = math.radians(alpha_deg)
    sysdir = os.path.join(case_dir, "system")
    os.makedirs(sysdir, exist_ok=True)

    with open(os.path.join(sysdir, "controlDict"), "w") as f:
        f.write(_header("dictionary", "controlDict"))
        f.write(f"""application     simpleFoam;
startFrom       latestTime;
startTime       0;
stopAt          endTime;
endTime         {int(n['iterations'])};
deltaT          1;
writeControl    timeStep;
writeInterval   {int(n['write_interval'])};
purgeWrite      2;
writeFormat     ascii;
writePrecision  8;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{{
    forceCoeffs
    {{
        type            forceCoeffs;
        libs            (forces);
        patches         (aerofoil);
        rho             rhoInf;
        rhoInf          1;
        liftDir         ({-math.sin(a):.8f} 0 {math.cos(a):.8f});
        dragDir         ({math.cos(a):.8f} 0 {math.sin(a):.8f});
        // pitch axis = liftDir x dragDir = +y: positive CmPitch is nose-up
        CofR            (0.25 0 0);
        magUInf         {velocity:.8g};
        lRef            1;
        Aref            {SPAN};
        writeControl    timeStep;
        writeInterval   1;
    }}
    yPlus
    {{
        type            yPlus;
        libs            (fieldFunctionObjects);
        writeControl    writeTime;
    }}
    surfacePressure
    {{
        type            surfaces;
        libs            (sampling);
        writeControl    writeTime;
        surfaceFormat   raw;
        fields          (p);
        interpolationScheme cell;
        surfaces
        {{
            aerofoil
            {{
                type        patch;
                patches     (aerofoil);
                interpolate false;
            }}
        }}
    }}
}}
""")

    conv = ("bounded Gauss linearUpwind grad({0})" if n["convection_scheme"] == "linearUpwind"
            else "bounded Gauss upwind")
    with open(os.path.join(sysdir, "fvSchemes"), "w") as f:
        f.write(_header("dictionary", "fvSchemes"))
        divs = f"    div(phi,U)      {conv.format('U')};\n"
        for t in turb:
            divs += f"    div(phi,{t})  {conv.format(t)};\n"
        f.write("ddtSchemes\n{\n    default         steadyState;\n}\n\n"
                "gradSchemes\n{\n    default         Gauss linear;\n"
                "    grad(U)         cellLimited Gauss linear 1;\n"
                + "".join(f"    grad({t})  cellLimited Gauss linear 1;\n" for t in turb) + "}\n\n"
                "divSchemes\n{\n    default         none;\n" + divs +
                "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n}\n\n"
                "laplacianSchemes\n{\n    default         Gauss linear corrected;\n}\n\n"
                "interpolationSchemes\n{\n    default         linear;\n}\n\n"
                "snGradSchemes\n{\n    default         corrected;\n}\n\n"
                "wallDist\n{\n    method          meshWave;\n}\n")

    tname = "|".join(turb)
    with open(os.path.join(sysdir, "fvSolution"), "w") as f:
        f.write(_header("dictionary", "fvSolution"))
        f.write(f"""solvers
{{
    p
    {{
        solver          {'PCG' if n['p_solver'] == 'PCG' else 'GAMG'};
        {'preconditioner  DIC' if n['p_solver'] == 'PCG' else 'smoother        GaussSeidel'};
        tolerance       1e-07;
        relTol          {n['p_rel_tol']:g};
    }}
    "(U|{tname})"
    {{
        solver          smoothSolver;
        smoother        symGaussSeidel;
        nSweeps         2;
        tolerance       1e-09;
        relTol          0.1;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors {int(n['n_non_orthogonal_correctors'])};
    consistent      {'yes' if n['consistent'] else 'no'};

    residualControl
    {{
        p               {n['residual_p']:g};
        U               {n['residual_U']:g};
        "({tname})"     {n['residual_turbulence']:g};
    }}
}}

relaxationFactors
{{
    fields
    {{
        p               {n['relax_p']:g};
    }}
    equations
    {{
        U               {n['relax_U']:g};
        "({tname})"     {n['relax_turbulence']:g};
    }}
}}
""")
    write_json(os.path.join(case_dir, "numerics.json"), n)
    return n


def set_iterations(case_dir, iterations):
    """Extend (or shorten) the run; picked up live because runTimeModifiable is on."""
    path = os.path.join(case_dir, "system", "controlDict")
    text = read_text(path)
    text, k = re.subn(r"(endTime\s+)\d+;", rf"\g<1>{int(iterations)};", text, count=1)
    if k != 1:
        raise RuntimeError("controlDict has no endTime entry")
    with open(path, "w") as f:
        f.write(text)
    return int(iterations)


# ------------------------------------------------------------ result parse --

def _thin(rows, max_points):
    """At most max_points + 1 evenly spaced rows, ending on the last one."""
    if len(rows) <= max_points:
        return rows
    step = len(rows) / float(max_points)
    return [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]


def force_history(case_dir, max_points=400):
    """Force-coefficient history from every forceCoeffs output file, in order."""
    rows = {}
    files = sorted(glob.glob(os.path.join(case_dir, "postProcessing", "forceCoeffs", "*", "coefficient*.dat")),
                   key=os.path.getmtime)
    for path in files:
        cols = None
        for line in open(path, errors="replace"):
            if line.startswith("#"):
                if "Time" in line and "Cd" in line:
                    cols = line.lstrip("#").split()
                continue
            parts = line.split()
            if not cols or len(parts) < len(cols):
                continue
            try:
                t = float(parts[0])
                rows[t] = {"iter": t, "Cd": float(parts[cols.index("Cd")]),
                           "Cl": float(parts[cols.index("Cl")]),
                           "Cm": float(parts[cols.index("CmPitch")])}
            except (ValueError, IndexError):
                continue
    full = [rows[t] for t in sorted(rows)]
    return _thin(full, max_points), full


def force_summary(case_dir, window=200):
    """Final coefficients plus how much they still move over the last `window` iterations."""
    _, full = force_history(case_dir)
    if not full:
        return {"available": False}
    last, tail = full[-1], full[-window:]

    def drift(key):
        return max(r[key] for r in tail) - min(r[key] for r in tail)
    cl_drift, cd_drift = drift("Cl"), drift("Cd")
    cl, cd = last["Cl"], last["Cd"]
    return {
        "available": True, "iterations": int(last["iter"]),
        "Cl": cl, "Cd": cd, "Cm": last["Cm"],
        "L_over_D": (cl / cd) if cd else None,
        "Cl_drift": cl_drift, "Cd_drift": cd_drift,
        "window": len(tail),
        "forces_converged": cl_drift < 1e-3 and cd_drift < 1e-4 and len(tail) >= min(window, 100),
    }


def residual_history(case_dir, log="log.simpleFoam", max_points=400):
    """Initial residual per field per iteration, parsed from the solver log."""
    path = os.path.join(case_dir, log)
    if not os.path.exists(path):
        return {"iterations": 0, "fields": {}, "series": []}
    series, cur, it = [], None, None
    pat = re.compile(rf"Solving for (\S+), Initial residual = ({NUM})")
    for line in open(path, errors="replace"):
        if line.startswith("Time = "):
            if cur:
                series.append(cur)
            try:
                it = float(line.split("=")[1].strip().rstrip("s"))
            except ValueError:
                it = None
            cur = {"iter": it}
        elif cur is not None:
            m = pat.search(line)
            if m and m.group(1) not in cur:
                cur[m.group(1)] = float(m.group(2))
    if cur and len(cur) > 1:
        series.append(cur)
    fields = {k: v for k, v in series[-1].items() if k != "iter"} if series else {}
    return {"iterations": len(series), "fields": fields, "series": _thin(series, max_points)}


def yplus_summary(case_dir):
    """Latest y+ statistics on the aerofoil patch, from the yPlus function object."""
    files = sorted(glob.glob(os.path.join(case_dir, "postProcessing", "yPlus", "*", "yPlus*.dat")),
                   key=os.path.getmtime)
    best = None
    for path in files:
        for line in open(path, errors="replace"):
            parts = line.split()
            if len(parts) >= 5 and parts[1] == "aerofoil":
                try:
                    best = {"iter": float(parts[0]), "min": float(parts[2]),
                            "max": float(parts[3]), "average": float(parts[4])}
                except ValueError:
                    pass
    return best or {}


def surface_cp(case_dir, velocity):
    """Pressure coefficient along the airfoil from the latest sampled surface.
    Returns {'upper': [[x, cp], ...], 'lower': [[x, cp], ...]}."""
    dirs = glob.glob(os.path.join(case_dir, "postProcessing", "surfacePressure", "*"))

    def tkey(d):
        try:
            return float(os.path.basename(d))
        except ValueError:
            return -1.0
    dirs = sorted([d for d in dirs if tkey(d) >= 0], key=tkey)
    if not dirs:
        return {}
    pts = []
    for path in glob.glob(os.path.join(dirs[-1], "*.raw")):
        for line in open(path, errors="replace"):
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    pts.append((float(parts[0]), float(parts[2]), float(parts[3])))
                except ValueError:
                    pass
    if not pts:
        return {}
    q = 0.5 * velocity * velocity
    code = (read_json(os.path.join(case_dir, "geometry.json")) or {}).get("naca")
    upper, lower = [], []
    for x, z, p in pts:
        xc = min(1.0, max(0.0, x))
        if code:
            (_, zu), (_, zl) = naca4_point(code, xc)
            mid = 0.5 * (zu + zl)
        else:
            mid = 0.0
        (upper if z >= mid else lower).append([round(x, 5), round(p / q, 5)])
    upper.sort()
    lower.sort()
    return {"time": tkey(dirs[-1]), "upper": upper, "lower": lower,
            "cp_min": min(c for _, c in upper + lower),
            "cp_max": max(c for _, c in upper + lower)}
