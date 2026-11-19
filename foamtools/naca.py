"""NACA 4-digit sections: the one definition of the shape, and the surface file
blockMesh projects the mesh onto.

    python -m foamtools.naca 4412 [CASE_DIR]    # writes CASE_DIR/constant/geometry/NACA4412.obj
"""
import glob
import math
import os
import re
import sys


def naca4(code):
    """(code, max camber, camber position, thickness) of a 4-digit code such as '4412'."""
    code = str(code).strip().upper().replace("NACA", "").strip()
    if not re.fullmatch(r"\d{4}", code):
        raise ValueError(f"not a NACA 4-digit code: {code!r}")
    return code, int(code[0]) / 100.0, int(code[1]) / 10.0, int(code[2:]) / 100.0


def naca4_point(code, x):
    """Upper and lower surface points (x, z) of a closed-trailing-edge NACA 4-digit.

    The mesh surface and the XFoil reference are both sampled from this. The thickness
    polynomial ends in -0.1036 x^4 (zero thickness at x = 1), not the -0.1015 of the
    original blunt-trailing-edge form, and a camber-position digit of 0 gives the
    symmetric section whatever the camber digit says."""
    _, m, p, t = naca4(code)
    yt = 5 * t * (0.2969 * math.sqrt(x) - 0.1260 * x - 0.3516 * x**2
                  + 0.2843 * x**3 - 0.1036 * x**4)
    if m == 0 or p == 0:
        yc, dyc = 0.0, 0.0
    elif x < p:
        yc, dyc = m / p**2 * (2 * p * x - x * x), 2 * m / p**2 * (p - x)
    else:
        yc = m / (1 - p)**2 * ((1 - 2 * p) + 2 * p * x - x * x)
        dyc = 2 * m / (1 - p)**2 * (p - x)
    th = math.atan(dyc)
    return ((x - yt * math.sin(th), yc + yt * math.cos(th)),
            (x + yt * math.sin(th), yc - yt * math.cos(th)))


def naca4_coords(code, n=200):
    """(upper, lower) lists of (x, z), leading edge to trailing edge, at n + 1
    cosine-spaced chord stations; both end on the same point (1, 0)."""
    points = [naca4_point(code, 0.5 * (1 - math.cos(math.pi * i / n))) for i in range(n + 1)]
    return [p[0] for p in points], [p[1] for p in points]


def write_obj(case_dir, code, n=400):
    """Write constant/geometry/NACA<code>.obj, a ribbon spanning y = -0.5 .. 0.5 for
    blockMesh projection, replacing any other NACA*.obj. Returns (upper, lower)."""
    code = naca4(code)[0]
    upper, lower = naca4_coords(code, n)
    loop = upper[::-1] + lower[1:-1]          # TE -> LE along the top, LE -> TE along the bottom
    k = len(loop)
    gdir = os.path.join(case_dir, "constant", "geometry")
    os.makedirs(gdir, exist_ok=True)
    for old in glob.glob(os.path.join(gdir, "NACA*.obj")):
        os.remove(old)
    with open(os.path.join(gdir, f"NACA{code}.obj"), "w") as f:
        f.write(f"# NACA {code}, unit chord, {k} profile points\no NACA{code}\n")
        for y in (-0.5, 0.5):
            for x, z in loop:
                f.write(f"v {x:.8f} {y} {z:.8f}\n")
        for i in range(k):
            a, b = i + 1, (i + 1) % k + 1
            f.write(f"f {a} {b} {b + k}\nf {a} {b + k} {a + k}\n")
    return upper, lower


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "4412"
    folder = sys.argv[2] if len(sys.argv) > 2 else "."
    write_obj(folder, code)
    print(f"wrote {os.path.join(folder, 'constant', 'geometry', f'NACA{naca4(code)[0]}.obj')}")
