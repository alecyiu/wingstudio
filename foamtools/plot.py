"""Render fields of a 2D OpenFOAM result (legacy ASCII VTK) to a PNG, for `sim plot`.
scipy, when installed, adds streamlines."""
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection


def read_vtk(path):
    """Return (points, cells, {name: array[ncells, ncomp]}) of cell data."""
    with open(path) as f:
        tok = f.read().split("\n", 4)[4].split()
    i, pts, cells, data, section = 0, None, [], {}, None
    while i < len(tok):
        t = tok[i]
        if t == "POINTS":
            n = int(tok[i + 1])
            pts = np.array(tok[i + 3:i + 3 + 3 * n], float).reshape(n, 3)
            i += 3 + 3 * n
        elif t == "CELLS":
            size = int(tok[i + 2])
            raw = np.array(tok[i + 3:i + 3 + size], int)
            j = 0
            while j < size:
                k = raw[j]
                cells.append(raw[j + 1:j + 1 + k])
                j += 1 + k
            i += 3 + size
        elif t == "CELL_TYPES":
            i += 2 + int(tok[i + 1])
        elif t in ("CELL_DATA", "POINT_DATA"):
            section = t
            i += 2
        elif t == "FIELD":
            i += 3
        elif (i + 3 < len(tok) and tok[i + 3] in ("float", "double", "int")
              and tok[i + 1].isdigit() and tok[i + 2].isdigit()):
            nc, nt = int(tok[i + 1]), int(tok[i + 2])
            if section == "CELL_DATA" and tok[i + 3] != "int":
                data[t] = np.array(tok[i + 4:i + 4 + nc * nt], float).reshape(nt, nc)
            i += 4 + nc * nt
        else:
            i += 1
    return pts, cells, data


def default_fields(names):
    out = ["U"] if "U" in names else []
    for cand in sorted(n for n in names if n.startswith("alpha.")) + ["T", "p_rgh", "p"]:
        if cand in names:
            out.append(cand)
            break
    return out or sorted(names)[:2]


def render(vtk, out, fields=None, streamlines=True, title=None, window=None):
    pts, cells, data = read_vtk(vtk)

    # 2D extruded mesh: find the thin (one-cell) axis, then draw each cell as
    # its face on the low side of that axis, in the plane of the other two.
    spans = [np.ptp(pts[:, k]) for k in range(3)]
    layers = [len(np.unique(np.round(pts[:, k] / (spans[k] or 1.0), 6))) for k in range(3)]
    thin = int(np.argmin(layers))
    ax_a, ax_b = [k for k in range(3) if k != thin]
    tol = 1e-6 * (spans[thin] or 1.0)
    polys = []
    for c in cells:
        p = pts[c]
        q = p[p[:, thin] <= p[:, thin].min() + tol][:, [ax_a, ax_b]]
        ctr = q.mean(0)
        polys.append(q[np.argsort(np.arctan2(q[:, 1] - ctr[1], q[:, 0] - ctr[0]))])
    ctrs = np.array([q.mean(0) for q in polys])

    fields = fields or default_fields(data)
    missing = [f for f in fields if f not in data]
    if missing:
        raise SystemExit(f"field(s) {missing} not in VTK; available: {sorted(data)}")

    if window:  # (amin, amax, bmin, bmax): keep only cells inside this box
        a0, a1, b0, b1 = window
        ma, mb = 0.25 * (a1 - a0), 0.25 * (b1 - b0)   # margin; axes clip to the window
        keep = np.where((ctrs[:, 0] >= a0 - ma) & (ctrs[:, 0] <= a1 + ma) &
                        (ctrs[:, 1] >= b0 - mb) & (ctrs[:, 1] <= b1 + mb))[0]
        if not len(keep):
            raise SystemExit(f"no cells inside window {window}")
        polys = [polys[i] for i in keep]
        ctrs = ctrs[keep]
        data = {k: v[keep] for k, v in data.items()}
        inside = ((ctrs[:, 0] >= a0) & (ctrs[:, 0] <= a1) &
                  (ctrs[:, 1] >= b0) & (ctrs[:, 1] <= b1))
    if not window or not inside.any():
        inside = np.ones(len(ctrs), bool)
    w = (window[1] - window[0]) if window else (np.ptp(ctrs[:, 0]) or 1.0)
    h = (window[3] - window[2]) if window else (np.ptp(ctrs[:, 1]) or 1.0)
    aspect = h / w
    n = len(fields)
    if aspect > 0.6:      # squarish domain: panels side by side
        fig, axes = plt.subplots(1, n, figsize=(6.5 * n, 6.5 * aspect + 1.2),
                                 dpi=140, constrained_layout=True, squeeze=False)
    else:                 # long domain: panels stacked
        fig, axes = plt.subplots(n, 1, figsize=(13, n * (13 * aspect + 1.3) + 0.5),
                                 dpi=140, constrained_layout=True, squeeze=False)
    axes = axes.ravel()

    summary = {}
    for ax, name in zip(axes, fields):
        arr = data[name]
        vec = arr.shape[1] > 1
        vals = np.linalg.norm(arr, axis=1) if vec else arr[:, 0]
        seen = vals[inside]          # statistics cover only what is visible
        summary[name] = {"min": float(seen.min()), "max": float(seen.max()),
                         "mean": float(seen.mean()), "magnitude": bool(vec)}
        # Colour limits from the 1st-99th percentile, so a few extreme cells
        # (e.g. corner singularities) do not flatten the rest of the field.
        lo, hi = np.percentile(seen, [1, 99])
        clipped = hi > lo and (seen.min() < lo or seen.max() > hi)
        if not clipped:
            lo, hi = seen.min(), seen.max()
        pc = PolyCollection(polys, array=vals, cmap="viridis", clim=(lo, hi),
                            edgecolors="face", linewidths=0.2)
        ax.add_collection(pc)
        ax.set_aspect("equal")
        ax.autoscale_view()
        ax.set_xlabel("xyz"[ax_a] + " (m)")
        ax.set_ylabel("xyz"[ax_b] + " (m)")
        ax.set_title(f"|{name}|" if vec else name, loc="left", fontsize=11)
        fig.colorbar(pc, ax=ax, shrink=0.85, pad=0.01,
                     extend="both" if clipped else "neither")
        if clipped:
            ax.set_title(f"full range {seen.min():.3g} to {seen.max():.3g}",
                         loc="right", fontsize=8, color="0.4")
        if vec and streamlines:
            _streamlines(ax, polys, ctrs, arr[:, [ax_a, ax_b]])
        if window:
            ax.set_xlim(window[0], window[1])
            ax.set_ylim(window[2], window[3])

    fig.suptitle(title or f"{vtk.split('/')[-1]}  -  {len(polys):,} cells", fontsize=12)
    fig.savefig(out)
    plt.close(fig)
    return {"image": out, "cells": int(inside.sum()), "fields": summary}


def _streamlines(ax, polys, ctrs, U):
    """Overlay streamlines, masked to the (possibly concave) flow domain."""
    try:
        from matplotlib.tri import LinearTriInterpolator, Triangulation
        from scipy.spatial import cKDTree
    except ImportError:
        return
    try:
        tri = Triangulation(ctrs[:, 0], ctrs[:, 1])
        # Drop Delaunay triangles that bridge outside the mesh: their centroid
        # is not inside the bounding box of the nearest cell.
        cen = ctrs[tri.triangles].mean(1)
        _, near = cKDTree(ctrs).query(cen)
        lo = np.array([q.min(0) for q in polys])[near]
        hi = np.array([q.max(0) for q in polys])[near]
        pad = 0.05 * (hi - lo)
        tri.set_mask(~np.all((cen >= lo - pad) & (cen <= hi + pad), axis=1))
        x = np.linspace(ctrs[:, 0].min(), ctrs[:, 0].max(), 400)
        y = np.linspace(ctrs[:, 1].min(), ctrs[:, 1].max(), 200)
        X, Y = np.meshgrid(x, y)
        ux = LinearTriInterpolator(tri, U[:, 0])(X, Y)
        uy = LinearTriInterpolator(tri, U[:, 1])(X, Y)
        if np.ma.count(ux) and float(np.ma.abs(ux).max() + np.ma.abs(uy).max()) > 0:
            ax.streamplot(x, y, ux, uy, density=1.4, color="white",
                          linewidth=0.5, arrowsize=0.6)
    except Exception:
        pass  # streamlines are decoration; never fail the plot over them

