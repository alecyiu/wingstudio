#!/usr/bin/env pvpython
"""Offscreen PNG renders of an OpenFOAM case with ParaView (no window), run with pvpython:

  pvpython --force-offscreen-rendering wingstudio/viewer/render_case.py CASE OUT.png \
      [--mode mesh|field|patches] [--field U] [--time latest] [--zoom auto] [--serve]

Nothing is written into the case (no case.foam file). The camera looks along the thin axis of a
2D mesh with parallel projection. While a solver runs, "latest" is the newest time directory
that has not changed for TIME_SETTLE_S seconds; a field that cannot be read falls back to the
previous time, then to the mesh, with a warning. --serve reads one JSON job per line on stdin,
which saves ParaView's start-up for every image. Prints one JSON object; exit code 0 iff "ok".

viewer_server.py uses CaseScene from here, so both show the same pictures.
"""
import argparse
import json
import os
import re
import sys
import time

T_START = time.time()

import paraview  # noqa: E402
from paraview import (
    servermanager,  # noqa: E402
    simple,  # noqa: E402
)

T_IMPORTED = time.time()

AXES = "XYZ"
MODES = ("mesh", "field", "patches")
PRESETS = ("full", "airfoil", "leading_edge", "trailing_edge", "wake")
DEFAULT_SIZE = (1100, 700)

# A time directory / mesh file modified more recently than this is treated as
# "still being written" and is not read.
TIME_SETTLE_S = 1.5
MESH_SETTLE_S = 1.0

MESH_FILES = ("points", "faces", "owner", "neighbour", "boundary")

# Colours for the patches mode. Common roles keep the same colour in every
# case so two airfoil tabs look alike; anything else cycles through the rest.
ROLE_COLOURS = (
    ("wall", (0.84, 0.15, 0.16)),      # red
    ("inlet", (0.12, 0.47, 0.71)),     # blue
    ("outlet", (0.17, 0.63, 0.17)),    # green
)
OTHER_COLOURS = (
    (1.00, 0.50, 0.05), (0.58, 0.40, 0.74), (0.55, 0.34, 0.29), (0.89, 0.47, 0.76),
    (0.09, 0.75, 0.81), (0.74, 0.74, 0.13), (0.40, 0.40, 0.40),
)
HIDDEN_PATCH_TYPES = ("empty", "processor", "processorCyclic")


# ----------------------------------------------------------------------------
# case inspection on the file system (no ParaView involved)
# ----------------------------------------------------------------------------
def say(message):
    """One JSON line for the process that started us, delivered at once.

    Written to file descriptor 1 directly: pvpython routes print() through a
    capture helper, and with --no-mpi that text stays in a C++ buffer until
    something is written to stderr or the process exits, whatever flush says.
    The studio waits for these lines ("ready", render answers)."""
    data = (json.dumps(message) + "\n").encode()
    while data:
        data = data[os.write(1, data):]


def case_dir_of(case):
    """Case directory for a case directory or a '<dir>/x.foam' path."""
    case = os.path.abspath(os.path.expanduser(str(case)))
    if case.endswith(".foam") and not os.path.isdir(case):
        return os.path.dirname(case)
    return case


def foam_path(case_dir):
    """The name handed to ParaView's reader. The file need not exist."""
    return os.path.join(case_dir, "case.foam")


def _mesh_file(case_dir, name):
    pm = os.path.join(case_dir, "constant", "polyMesh")
    for cand in (name, name + ".gz"):
        p = os.path.join(pm, cand)
        if os.path.isfile(p):
            return p
    return None


def mesh_status(case_dir, wait_s=0.0):
    """-> (ok, reason, signature).

    ok is False when a mesh file is missing, empty or was modified less than
    MESH_SETTLE_S ago (a mesher is writing). wait_s > 0 waits that long for a
    mesh in the middle of being written to settle. The signature changes
    whenever the mesh files change, so a cached reader can be dropped."""
    deadline = time.time() + max(0.0, wait_s)
    while True:
        sig, missing, newest = [], [], 0.0
        for name in MESH_FILES:
            p = _mesh_file(case_dir, name)
            try:
                st = os.stat(p) if p else None
            except OSError:
                st = None
            if st is None or st.st_size == 0:
                missing.append(name)
                continue
            sig.append((name, st.st_size, st.st_mtime_ns))
            newest = max(newest, st.st_mtime)
        in_progress = time.time() - newest < MESH_SETTLE_S
        if missing:
            reason = "no mesh yet: %s/constant/polyMesh has no %s" % (case_dir, ", ".join(missing))
        elif in_progress:
            reason = "the mesh in %s is being written right now" % case_dir
        else:
            return True, "", tuple(sig)
        # Waiting only helps while a mesher is at work; a mesh that simply is
        # not there is reported at once.
        if not in_progress or time.time() >= deadline:
            return False, reason, tuple(sig)
        time.sleep(0.25)


def read_boundary(case_dir):
    """Patches from constant/polyMesh/boundary: [{name, type, n_faces}]."""
    p = _mesh_file(case_dir, "boundary")
    if not p or p.endswith(".gz"):
        return []
    try:
        with open(p, errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    m = re.search(r"\n\s*\d+\s*\(", text)          # "<count> (" opens the patch list
    if not m:
        return []
    out = []
    for name, body in re.findall(r"([A-Za-z_][\w.:-]*)\s*\{([^{}]*)\}", text[m.end():]):
        ptype = re.search(r"\btype\s+([\w:]+)\s*;", body)
        nfaces = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        out.append(dict(name=name, type=ptype.group(1) if ptype else "patch",
                        n_faces=int(nfaces.group(1)) if nfaces else 0))
    return out


def time_directories(case_dir):
    """Numerically named directories of the case: sorted [(value, path)]."""
    out = []
    try:
        entries = list(os.scandir(case_dir))
    except OSError:
        return out
    for e in entries:
        try:
            v = float(e.name)
        except ValueError:
            continue
        if e.name[:1].isdigit() and e.is_dir():
            out.append((v, e.path))
    return sorted(out)


def _newest_mtime(path):
    newest = 0.0
    try:
        newest = os.stat(path).st_mtime
        for e in os.scandir(path):
            try:
                newest = max(newest, e.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return newest


def complete_times(case_dir):
    """-> (complete, pending): time values whose directory is finished, and
    those a solver seems to be writing right now. Only the newest few
    directories can still be in progress, so only those are inspected."""
    dirs = time_directories(case_dir)
    complete, pending = [], []
    now = time.time()
    for i, (v, path) in enumerate(dirs):
        recent = i >= len(dirs) - 3
        if recent and (now - _newest_mtime(path) < TIME_SETTLE_S or not os.listdir(path)):
            pending.append(v)
        else:
            complete.append(v)
    return complete, pending


def _same_time(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


# ----------------------------------------------------------------------------
# ParaView helpers
# ----------------------------------------------------------------------------
def new_reader(case_dir, regions, arrays=()):
    """An OpenFOAM reader for some mesh regions. Time 0 is kept."""
    reader = simple.OpenFOAMReader(FileName=foam_path(case_dir))
    reader.SkipZeroTime = 0
    reader.Createcelltopointfiltereddata = 1
    # Refresh() is required: without it the reader keeps the time/field list
    # it built at creation (with time 0 skipped), so a case that only has 0/
    # would show no fields. Verified on ParaView 6.2.1.
    reader.Refresh()
    reader.UpdatePipelineInformation()
    reader.MeshRegions = list(regions)
    reader.CellArrays = list(arrays)
    return reader


def plane_info(bounds):
    """Work out the 2D plane from the mesh bounds.

    Returns dict(thin, h, v, sign, is2d): axis indices (0=x, 1=y, 2=z)."""
    ext = [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
    order = sorted(range(3), key=lambda i: ext[i])
    thin = order[0]
    mid = ext[order[1]] or 1e-30
    is2d = ext[thin] / mid < 0.2
    if thin == 1:    # x-z plane: x right, z up, look along +y
        h, v, sign = 0, 2, -1.0
    elif thin == 2:  # x-y plane: x right, y up, look along -z
        h, v, sign = 0, 1, 1.0
    else:            # y-z plane: y right, z up, look along -x
        h, v, sign = 1, 2, 1.0
    return dict(thin=thin, h=h, v=v, sign=sign, is2d=is2d, extents=ext)


def wall_box(case_dir, patches, info, full, t):
    """Bounding box of the wall patches in view coordinates, or None.

    Returns dict(h0, h1, v0, v1, le_v, te_v): le_v / te_v are the screen-up
    positions of the leading-most and trailing-most wall point, so the
    leading/trailing-edge views can be centred on the actual edge of a
    cambered section rather than on the middle of the box. None when the case
    has no wall patch or the walls are the domain boundary (cavity, channel)
    rather than a body inside it."""
    regions = ["patch/" + p["name"] for p in patches if p["type"] == "wall"]
    if not regions or not info["is2d"]:
        return None
    h, v = info["h"], info["v"]
    reader = new_reader(case_dir, regions)
    try:
        reader.UpdatePipeline(t)
        b = list(reader.GetDataInformation().GetBounds())
        if b[0] > b[1]:
            return None
        box = dict(h0=b[2 * h], h1=b[2 * h + 1], v0=b[2 * v], v1=b[2 * v + 1])
        fw, fh = full[1] - full[0], full[3] - full[2]
        if box["h1"] - box["h0"] >= 0.6 * fw or box["v1"] - box["v0"] >= 0.6 * fh:
            return None
        mid = 0.5 * (box["v0"] + box["v1"])
        box.update(le_v=mid, te_v=mid)
        try:   # the wall patch is small (a few hundred points), fetching it is cheap
            data = servermanager.Fetch(reader)
            lead, trail = None, None
            it = data.NewIterator()
            it.InitTraversal()
            while not it.IsDoneWithTraversal():
                block = it.GetCurrentDataObject()
                for i in range(block.GetNumberOfPoints() if block is not None else 0):
                    p = block.GetPoint(i)
                    if lead is None or p[h] < lead[h]:
                        lead = p
                    if trail is None or p[h] > trail[h]:
                        trail = p
                it.GoToNextItem()
            if lead is not None:
                box.update(le_v=lead[v], te_v=trail[v])
        except Exception:
            pass
        return box
    finally:
        simple.Delete(reader)


def parse_zoom(zoom):
    """-> 'auto' | preset name | [h0, h1, v0, v1]. Raises ValueError."""
    if zoom in (None, "", "auto"):
        return "auto"
    if isinstance(zoom, (list, tuple)):
        vals = [float(x) for x in zoom]
    else:
        name = str(zoom).strip().lower().replace("-", "_").replace(" ", "_")
        if name in PRESETS or name == "auto":
            return name
        try:
            vals = [float(x) for x in str(zoom).replace(" ", ",").split(",") if x != ""]
        except ValueError:
            vals = []
    if len(vals) != 4 or vals[1] <= vals[0] or vals[3] <= vals[2]:
        raise ValueError("zoom must be auto, %s or h0,h1,v0,v1 (got %r)" % (", ".join(PRESETS), zoom))
    return vals


def preset_box(name, full, wall):
    """View box [h0, h1, v0, v1] of a named preset. `full` is the domain box,
    `wall` the result of wall_box() (None -> every preset is the full view).
    The camera always shows at least the box, whatever the image aspect."""
    mh, mv = 0.04 * (full[1] - full[0]), 0.04 * (full[3] - full[2])
    whole = [full[0] - mh, full[1] + mh, full[2] - mv, full[3] + mv]
    if name == "full" or wall is None:
        return whole
    chord = wall["h1"] - wall["h0"]
    thick = wall["v1"] - wall["v0"]
    mid = 0.5 * (wall["v0"] + wall["v1"])
    if name == "airfoil":
        m = 0.35 * max(chord, thick)
        return [wall["h0"] - m, wall["h1"] + m, wall["v0"] - m, wall["v1"] + m]
    if name == "leading_edge":
        return [wall["h0"] - 0.08 * chord, wall["h0"] + 0.22 * chord,
                mid - 0.10 * chord, mid + 0.10 * chord]
    if name == "trailing_edge":
        return [wall["h1"] - 0.20 * chord, wall["h1"] + 0.12 * chord,
                wall["te_v"] - 0.10 * chord, wall["te_v"] + 0.10 * chord]
    if name == "wake":
        return [wall["h0"] - 0.5 * chord, wall["h1"] + 4.0 * chord,
                wall["te_v"] - 1.2 * chord, wall["te_v"] + 1.2 * chord]
    return whole


def set_camera(view, bounds, info, box, size):
    """Parallel camera along the thin axis, framed on box=[h0,h1,v0,v1]."""
    cx = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2]
    diag = sum((bounds[2 * i + 1] - bounds[2 * i]) ** 2 for i in range(3)) ** 0.5 or 1.0
    view.CameraParallelProjection = 1
    if not info["is2d"]:
        # genuine 3D mesh: isometric view of everything
        view.CameraFocalPoint = cx
        view.CameraPosition = [cx[0] - diag, cx[1] - diag, cx[2] + diag]
        view.CameraViewUp = [0, 0, 1]
        view.ResetCamera()
        return
    h, v, thin = info["h"], info["v"], info["thin"]
    fp = list(cx)
    fp[h] = (box[0] + box[1]) / 2
    fp[v] = (box[2] + box[3]) / 2
    pos = list(fp)
    pos[thin] = fp[thin] + info["sign"] * 2.0 * diag
    up = [0.0, 0.0, 0.0]
    up[v] = 1.0
    view.CameraFocalPoint = fp
    view.CameraPosition = pos
    view.CameraViewUp = up
    aspect = float(size[0]) / float(size[1])
    bw = max(box[1] - box[0], 1e-12)
    bh = max(box[3] - box[2], 1e-12)
    view.CameraParallelScale = max(bh / 2.0, bw / (2.0 * aspect))
    _try_set(view, ("CenterOfRotation", fp))


def _try_set(proxy, *properties):
    """Set (name, value) properties one by one, skipping any this ParaView does not accept."""
    for name, val in properties:
        try:
            setattr(proxy, name, val)
        except Exception:
            pass


def style_view(view, dark=False):
    fg = [0.92, 0.92, 0.92] if dark else [0.1, 0.1, 0.1]
    _try_set(view, ("UseColorPaletteForBackground", 0), ("BackgroundColorMode", "Single Color"),
             ("Background", [0.11, 0.12, 0.14] if dark else [1.0, 1.0, 1.0]),
             ("OrientationAxesVisibility", 1), ("OrientationAxesLabelColor", fg))
    return fg


def colour_by_field(rep, view, field, component, fg, crange=None, preset=None, point_data=True,
                    bar_pos=(0.88, 0.3)):
    """Colour a representation by a field; returns (lut, title, (assoc, lo, hi))
    or (None, None, None) when the array is not there."""
    assoc = "POINTS" if point_data else "CELLS"
    info = rep.Input.GetPointDataInformation() if point_data else rep.Input.GetCellDataInformation()
    arr = info.GetArray(field)
    if arr is None and point_data:
        assoc = "CELLS"
        arr = rep.Input.GetCellDataInformation().GetArray(field)
    if arr is None:
        return None, None, None
    ncomp = arr.GetNumberOfComponents()
    if ncomp > 1:
        comp = component if component in ("X", "Y", "Z") else "Magnitude"
        simple.ColorBy(rep, (assoc, field, comp))
        cidx = -1 if comp == "Magnitude" else AXES.index(comp)
        title = "%s magnitude" % field if comp == "Magnitude" else "%s %s" % (field, comp.lower())
    else:
        simple.ColorBy(rep, (assoc, field))
        cidx = 0
        title = field
    lut = simple.GetColorTransferFunction(field)
    if preset:
        try:
            lut.ApplyPreset(preset, True)
        except Exception:
            pass
    if crange:
        lo, hi = float(crange[0]), float(crange[1])
    else:
        lo, hi = arr.GetRange(cidx)
    if hi <= lo:  # uniform field (e.g. time 0): avoid a degenerate colour map
        pad = abs(lo) * 0.01 or 1e-6
        lo, hi = lo - pad, hi + pad
    lut.RescaleTransferFunction(lo, hi)
    try:
        simple.GetOpacityTransferFunction(field).RescaleTransferFunction(lo, hi)
    except Exception:
        pass
    rep.SetScalarBarVisibility(view, True)
    sb = simple.GetScalarBar(lut, view)
    sb.Title = title
    sb.ComponentTitle = ""
    _try_set(sb, ("TitleColor", fg), ("LabelColor", fg), ("TitleFontSize", 16), ("LabelFontSize", 14),
             ("ScalarBarLength", 0.4), ("WindowLocation", "Any Location"), ("Orientation", "Vertical"),
             ("Position", list(bar_pos)))
    return lut, title, (assoc, lo, hi)


def patch_colours(patches):
    """Give every patch a colour: {name: (r, g, b)}."""
    out, used_other = {}, 0
    for p in patches:
        role = None
        if p["type"] == "wall":
            role = "wall"
        else:
            low = p["name"].lower()
            for key in ("inlet", "outlet"):
                if key in low:
                    role = key
        colour = dict(ROLE_COLOURS).get(role)
        if colour is None or colour in out.values():
            colour = OTHER_COLOURS[used_other % len(OTHER_COLOURS)]
            used_other += 1
        out[p["name"]] = colour
    return out


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(255 * c)) for c in rgb)


def _flat(rep):
    """Flat shading: these are 2D pictures, no lighting gradients."""
    _try_set(rep, ("Ambient", 1.0), ("Diffuse", 0.0), ("Specular", 0.0))


def _solid(rep):
    """Switch a representation to one solid colour (no array colouring)."""
    _try_set(rep, ("ColorArrayName", [None, ""]))


def _parse_range(value):
    """None | 'auto' | 'lo,hi' | [lo, hi] -> None or [lo, hi]. Raises ValueError."""
    if value in (None, "", "auto"):
        return None
    vals = [float(x) for x in (value if isinstance(value, (list, tuple)) else str(value).split(","))]
    if len(vals) != 2 or vals[1] <= vals[0]:
        raise ValueError("range must be auto or lo,hi with hi > lo (got %r)" % (value,))
    return vals


# ----------------------------------------------------------------------------
# the scene: one case shown in one render view
# ----------------------------------------------------------------------------
class CaseScene:
    """Shows one OpenFOAM case in a ParaView render view, in one of MODES.

    apply() brings the view to a requested (case, mode, field, time, zoom) and
    never raises for anything the case can do wrong: it returns False with
    self.error set, or True with fall-backs listed in self.warnings."""

    def __init__(self, view, dark=False, bar_pos=(0.88, 0.3), line_width=4.0):
        self.view = view
        self.dark = dark
        self.fg = style_view(view, dark=dark)
        self.bar_pos = bar_pos
        self.line_width = line_width
        self.reader = None
        self.rep = None
        self.case_dir = None
        self.mesh_sig = None
        self.extras = []        # proxies that exist only for the current mode (patch lines, legend)
        self._placed = None     # (view size, camera signature) when the camera was last framed
        self._reset()
        self.error = None

    def _reset(self):
        self.mode = self.field = self.time = None
        self.times, self.pending_times, self.fields = [], [], []
        self.bounds = self.info = self.wall = self.box = self.zoom = None
        self.range = self.legend = self.cells = None
        self.patches = []
        self.warnings = []

    # -- life cycle ----------------------------------------------------------
    def _hide_bar(self):
        """Hide the scalar bar, but only if the representation is coloured
        (otherwise ParaView logs 'Failed to determine the LookupTable')."""
        try:
            if self.rep is not None and self.rep.ColorArrayName[1]:
                self.rep.SetScalarBarVisibility(self.view, False)
        except Exception:
            pass

    def _clear_extras(self):
        for proxy in reversed(self.extras):
            try:
                simple.Delete(proxy)
            except Exception:
                pass
        self.extras = []

    def close(self):
        """Drop every reader. The case directory is remembered so a later
        reload can pick the case up again (for example once its mesh exists)."""
        self._clear_extras()
        if self.rep is not None:
            self._hide_bar()
            simple.Delete(self.rep)
        if self.reader is not None:
            simple.Delete(self.reader)
        self.reader = self.rep = None
        self.mesh_sig = None
        self._placed = None
        self._reset()

    def _open(self, case_dir, sig):
        self.close()
        self.case_dir = case_dir
        self.reader = new_reader(case_dir, ["internalMesh"])
        self.mesh_sig = sig

    # -- time ----------------------------------------------------------------
    def _scan_times(self):
        """Times that are safe to read: listed by the reader AND complete on disk."""
        reader_times = [float(t) for t in (self.reader.TimestepValues or [])]
        complete, pending = complete_times(self.case_dir)
        self.times = [t for t in reader_times if any(_same_time(t, c) for c in complete)]
        self.pending_times = pending
        # A case without any time directory still has one (mesh-only) reader time.
        return reader_times[0] if reader_times and not self.times and not pending else None

    def _pick_time(self, when):
        if not self.times:
            return None
        if when in (None, "", "latest"):
            if self.pending_times and max(self.pending_times) > max(self.times):
                self.warnings.append("time %g is still being written; showing %g"
                                     % (max(self.pending_times), max(self.times)))
            return max(self.times)
        try:
            want = float(when)
        except (TypeError, ValueError):
            self.warnings.append("bad time %r, using latest" % (when,))
            return max(self.times)
        best = min(self.times, key=lambda t: abs(t - want))
        if not _same_time(best, want):
            why = "is still being written" if any(_same_time(want, p) for p in self.pending_times) else "not found"
            self.warnings.append("time %g %s, using %g" % (want, why, best))
        return best

    # -- one consistent update -------------------------------------------------
    def apply(self, case=None, mode="mesh", field="U", when="latest", zoom=None, reload=False,
              component=None, crange=None, preset=None, cell_data=False, mesh_wait_s=0.0):
        """Bring the scene to (case, mode, field, time).

        The camera only moves when the case or its mesh changes or a zoom is
        given, so a user's pan/zoom survives field, time and mode switches."""
        self.warnings = []
        if mode not in MODES:
            self.error = "mode must be one of %s (got %r)" % (", ".join(MODES), mode)
            return False
        try:
            zoom_req = parse_zoom(zoom) if zoom is not None else None
            crange = _parse_range(crange)
        except ValueError as e:
            self.error = str(e)
            return False
        case_dir = case_dir_of(case) if case else self.case_dir
        if not case_dir:
            self.error = "no case given"
            return False
        if not os.path.isdir(case_dir):
            self.close()
            self.case_dir = case_dir
            self.error = "case directory not found: %s" % case_dir
            return False

        ok, reason, sig = mesh_status(case_dir, wait_s=mesh_wait_s)
        if not ok:
            self.close()
            self.case_dir = case_dir
            self.error = reason
            return False
        fresh = self.reader is None or case_dir != self.case_dir or sig != self.mesh_sig
        if fresh:
            self._open(case_dir, sig)
        elif reload:
            # new time directories written by a running solver
            self.reader.Refresh()
            self.reader.UpdatePipelineInformation()
        reader = self.reader
        self._clear_extras()
        self._hide_bar()

        mesh_only_time = self._scan_times()
        t = self._pick_time(when)
        self.fields = list(reader.GetProperty("CellArrays").Available)

        # ---- field: fall back to an earlier time, then to the mesh ----------
        shown_mode = mode
        reader.CellArrays = []
        if mode == "field":
            field = field or "U"
            if t is None:
                self.warnings.append("no results yet (no complete time directory); showing the mesh")
                shown_mode = "mesh"
            elif field not in self.fields:
                self.warnings.append("field %r not in the case (have: %s); showing the mesh"
                                     % (field, ", ".join(self.fields) or "none"))
                shown_mode = "mesh"
            else:
                reader.CellArrays = [field]
                tries = [t]
                if when in (None, "", "latest"):
                    tries += sorted((x for x in self.times if x < t), reverse=True)[:2]
                for cand in tries:
                    reader.UpdatePipeline(cand)
                    if reader.GetCellDataInformation().GetArray(field) is not None:
                        if cand != t:
                            self.warnings.append("field %r could not be read at time %g; showing time %g"
                                                 % (field, t, cand))
                        t = cand
                        break
                else:
                    # e.g. 0/U written with macros ($Uinf, #includeEtc): ParaView's
                    # reader cannot expand them, so the array is silently missing.
                    self.warnings.append(
                        "field %r could not be read at time %g (still being written, or OpenFOAM "
                        "macros/#include in the file); showing the mesh" % (field, t))
                    shown_mode = "mesh"
                    reader.CellArrays = []

        t_read = t if t is not None else mesh_only_time
        if t_read is not None:
            self.view.ViewTime = t_read
            try:
                simple.GetAnimationScene().AnimationTime = t_read
            except Exception:
                pass
        reader.UpdatePipeline(t_read)
        di = reader.GetDataInformation()
        bounds = list(di.GetBounds())
        if di.GetNumberOfCells() == 0 or bounds[0] > bounds[1]:
            self.close()
            self.case_dir = case_dir
            self.error = "ParaView's reader produced no cells for %s" % case_dir
            return False
        self.time = t
        self.bounds = bounds
        self.info = plane_info(bounds)
        self.cells = di.GetNumberOfCells()
        h, v = self.info["h"], self.info["v"]
        full = [bounds[2 * h], bounds[2 * h + 1], bounds[2 * v], bounds[2 * v + 1]]

        if fresh:
            self.patches = read_boundary(case_dir)
            colours = patch_colours([p for p in self.patches if p["type"] not in HIDDEN_PATCH_TYPES])
            for p in self.patches:
                p["shown"] = p["name"] in colours
                p["color"] = _hex(colours[p["name"]]) if p["shown"] else None
                p["rgb"] = colours.get(p["name"])
            self.wall = wall_box(case_dir, self.patches, self.info, full, t_read)

        # ---- representation ---------------------------------------------------
        if self.rep is None:
            self.rep = simple.Show(reader, self.view)
            _flat(self.rep)
        rep = self.rep
        rep.Visibility = 1
        self.range = self.legend = None
        if shown_mode == "field":
            rep.SetRepresentationType("Surface")
            lut, title, used = colour_by_field(
                rep, self.view, field, component, self.fg, crange=crange, preset=preset,
                point_data=not cell_data, bar_pos=self.bar_pos)
            if lut is None:
                self.warnings.append("field %r could not be coloured; showing the mesh" % field)
                shown_mode = "mesh"
            else:
                try:
                    simple.HideUnusedScalarBars(self.view)
                except Exception:
                    pass
                self.range = [used[1], used[2]]
                self.legend = title
        if shown_mode == "mesh":
            rep.SetRepresentationType("Surface With Edges")
            _solid(rep)
            rep.AmbientColor = rep.DiffuseColor = [0.86, 0.90, 0.96]
            rep.EdgeColor = [0.10, 0.20, 0.45]
            rep.LineWidth = 1.0
        elif shown_mode == "patches":
            self._show_patches(t_read)
        self.mode, self.field = shown_mode, (field if shown_mode == "field" else None)

        # ---- camera -------------------------------------------------------------
        if fresh or zoom_req is not None or self.box is None:
            z = zoom_req if zoom_req is not None else "auto"
            if isinstance(z, list):
                self.box, self.zoom = z, "box"
            else:
                name = z
                if name == "auto":
                    name = "full" if (mode == "patches" or self.wall is None) else "airfoil"
                elif name != "full" and self.wall is None:
                    self.warnings.append("view %r needs a wall body inside the domain; showing the full domain" % name)
                    name = "full"
                self.box, self.zoom = preset_box(name, full, self.wall), name
            self._place_camera()
        self.error = None
        return True

    # -- camera ------------------------------------------------------------------
    def _camera_signature(self):
        cam = self.view.GetActiveCamera()
        return tuple(cam.GetFocalPoint()) + (cam.GetParallelScale(),)

    def _place_camera(self):
        size = [int(x) for x in self.view.ViewSize]
        set_camera(self.view, self.bounds, self.info, self.box, size)
        self._placed = (size, self._camera_signature())

    def refit_if_resized(self):
        """Frame the current view box again after the view changed size (a
        browser connected, a pane was resized), so the box stays fully visible.
        Does nothing once the user has panned or zoomed: their camera wins
        until the next explicit zoom. Returns True when the camera moved."""
        if self.reader is None or self.box is None or self._placed is None or not self.info["is2d"]:
            return False
        size = [int(x) for x in self.view.ViewSize]
        if size == self._placed[0]:
            return False
        now, then = self._camera_signature(), self._placed[1]
        if any(abs(a - b) > 1e-7 * max(1.0, abs(a), abs(b)) for a, b in zip(now, then)):
            self._placed = None
            return False
        self._place_camera()
        return True

    def _show_patches(self, t):
        """Domain as a plain light surface, every drawable patch as a thick
        line in its own colour, and a legend of patch names and types."""
        rep = self.rep
        rep.SetRepresentationType("Surface")
        _solid(rep)
        rep.AmbientColor = rep.DiffuseColor = [0.26, 0.28, 0.33] if self.dark else [0.93, 0.94, 0.96]
        two_d = self.info["is2d"]
        if not two_d:
            rep.Visibility = 0          # a 3D domain would hide its own boundary
        for p in self.patches:
            if not p["shown"] or p["n_faces"] == 0:
                continue
            pr = new_reader(self.case_dir, ["patch/" + p["name"]])
            self.extras.append(pr)
            try:
                pr.UpdatePipeline(t)
                prep = simple.Show(pr, self.view)
            except Exception as e:
                self.warnings.append("patch %s could not be shown: %s" % (p["name"], e))
                continue
            # Seen edge-on in a 2D case a patch is a ribbon of zero width, so it
            # is drawn as wireframe: the lines keep their pixel width.
            prep.SetRepresentationType("Wireframe" if two_d else "Surface")
            _solid(prep)
            prep.AmbientColor = prep.DiffuseColor = list(p["rgb"])
            prep.LineWidth = self.line_width
            _flat(prep)
        # legend: one text line per patch, top left
        lines = [(p["name"] + "   " + p["type"], p["rgb"]) for p in self.patches if p["shown"]]
        hidden = [p["name"] for p in self.patches if not p["shown"]]
        if hidden:
            kinds = sorted(set(p["type"] for p in self.patches if not p["shown"]))
            lines.append(("%s   %s (not drawn)" % (", ".join(hidden), "/".join(kinds)),
                          (0.65, 0.65, 0.65) if self.dark else (0.45, 0.45, 0.45)))
        for i, (text, rgb) in enumerate(lines[:14]):
            try:
                src = simple.Text(Text=text)
                self.extras.append(src)
                _try_set(simple.Show(src, self.view), ("WindowLocation", "Any Location"),
                         ("Position", [0.02, 0.89 - 0.047 * i]), ("FontSize", 15), ("Bold", 1),
                         ("Color", list(rgb)))
            except Exception as e:
                self.warnings.append("legend line could not be drawn: %s" % e)
                break

    def describe(self):
        info = self.info
        return dict(
            case=self.case_dir,
            mode=self.mode,
            field=self.field,
            time=self.time,
            times=list(self.times),
            pending_times=list(self.pending_times),
            fields=list(self.fields),
            plane=(AXES[info["h"]] + AXES[info["v"]]).lower() if info and info["is2d"] else ("3d" if info else None),
            thin_axis=AXES[info["thin"]] if info and info["is2d"] else None,
            cells=self.cells,
            bounds=self.bounds,
            zoom=self.zoom,
            box=self.box if info and info["is2d"] else None,
            wall_box=[self.wall[k] for k in ("h0", "h1", "v0", "v1")] if self.wall else None,
            presets=list(PRESETS) if self.wall else ["full"],
            range=self.range,
            legend=self.legend,
            patches=[dict(name=p["name"], type=p["type"], n_faces=p["n_faces"],
                          shown=p["shown"], color=p["color"]) for p in self.patches],
            warnings=list(self.warnings),
            error=self.error,
        )


# ----------------------------------------------------------------------------
# one offscreen render
# ----------------------------------------------------------------------------
_SCENE = None


def get_scene(size, dark):
    """One render view for the life of the process (kept warm by --serve)."""
    global _SCENE
    if _SCENE is None:
        view = simple.CreateView("RenderView")
        view.InteractionMode = "2D"
        _SCENE = CaseScene(view)
    _SCENE.view.ViewSize = [int(size[0]), int(size[1])]
    if _SCENE.dark != dark:
        _SCENE.dark = dark
        _SCENE.fg = style_view(_SCENE.view, dark=dark)
    return _SCENE


def _parse_size(size):
    if not size:
        return list(DEFAULT_SIZE)
    if isinstance(size, str):
        size = size.lower().replace("x", ",").split(",")
    w, h = [int(float(x)) for x in size]
    if not (16 <= w <= 8000 and 16 <= h <= 8000):
        raise ValueError("size must be between 16 and 8000 pixels per side")
    return [w, h]


def render(job):
    t0 = time.time()
    res = dict(ok=False, warnings=[])
    if not job.get("case") or not job.get("out"):
        res["error"] = "job needs 'case' and 'out'"
        return res
    out = os.path.abspath(os.path.expanduser(str(job["out"])))
    mode = job.get("mode") or "mesh"
    res.update(case=case_dir_of(job["case"]), out=out, requested_mode=mode)
    try:
        size = _parse_size(job.get("size"))
    except (TypeError, ValueError) as e:
        res["error"] = "bad size: %s" % e
        return res

    scene = get_scene(size, bool(job.get("dark")))
    try:
        ok = scene.apply(
            case=job["case"], mode=mode, field=job.get("field") or "U",
            when=job.get("time", "latest"), zoom=job.get("zoom") or "auto",
            component=job.get("component"), crange=job.get("range"), preset=job.get("preset"),
            cell_data=bool(job.get("cell_data")),
            mesh_wait_s=float(job.get("mesh_wait_s", 3.0)))
        res.update(scene.describe())
        if not ok:
            return res
        t_read = time.time()
        _try_set(scene.view, ("OrientationAxesVisibility", 0 if job.get("no_axes") else 1))
        # NOTE: paraview.simple.Render() resets the camera on its first call,
        # which would throw the zoom box away; StillRender() does not.
        scene.view.StillRender()
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        # Write next to the target and rename, so a web server never serves half a PNG.
        tmp = "%s.part-%d.png" % (out, os.getpid())
        simple.SaveScreenshot(tmp, scene.view, ImageResolution=size)
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            res["error"] = "ParaView wrote no image"
            return res
        os.replace(tmp, out)
        t_done = time.time()
        res.update(
            ok=True, size=size, png_bytes=os.path.getsize(out),
            seconds=dict(read=round(t_read - t0, 3), render=round(t_done - t_read, 3),
                         job=round(t_done - t0, 3)),
        )
        return res
    finally:
        # Leave nothing open between jobs: the next job may be for a mesh that
        # has been rebuilt in the meantime.
        scene.close()


def safe_render(job):
    try:
        return render(job)
    except Exception as e:  # report, never crash the caller
        try:
            if _SCENE is not None:
                _SCENE.close()
        except Exception:
            pass
        return dict(ok=False, error="%s: %s" % (type(e).__name__, e),
                    case=job.get("case") if isinstance(job, dict) else None,
                    out=job.get("out") if isinstance(job, dict) else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--mode", choices=MODES, default="mesh")
    ap.add_argument("--field", default="U")
    ap.add_argument("--component", default=None, help="X, Y or Z; default magnitude")
    ap.add_argument("--time", default="latest")
    ap.add_argument("--zoom", default="auto", help="auto | %s | h0,h1,v0,v1" % " | ".join(PRESETS))
    ap.add_argument("--size", default="%d,%d" % DEFAULT_SIZE)
    ap.add_argument("--range", default=None, help="lo,hi")
    ap.add_argument("--preset", default=None, help="ParaView colour map preset name")
    ap.add_argument("--cell-data", action="store_true", help="colour by raw cell values (no smoothing)")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--no-axes", action="store_true")
    ap.add_argument("--serve", action="store_true", help="read JSON jobs from stdin")
    a = ap.parse_args()

    if a.serve:
        say(dict(ready=True, pid=os.getpid(), startup_s=round(time.time() - T_START, 3)))
        # pvpython replaces sys.stdin with a capture helper that cannot be
        # iterated; read the real file descriptor instead.
        stdin = sys.__stdin__ if sys.__stdin__ is not None else os.fdopen(0)
        while True:
            line = stdin.readline()
            if not line:
                break               # the parent closed the pipe (or died): stop
            line = line.strip()
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            try:
                job = json.loads(line)
                if not isinstance(job, dict):
                    raise ValueError("job must be a JSON object")
            except ValueError as e:
                say(dict(ok=False, error="bad json: %s" % e))
                continue
            res = safe_render(job)
            if "id" in job:
                res["id"] = job["id"]   # lets the caller match answers to jobs
            say(res)
        return 0

    if not a.case or not a.out:
        ap.error("case and out are required (or use --serve)")
    job = dict(
        case=a.case, out=a.out, mode=a.mode, field=a.field, component=a.component,
        time=a.time, zoom=a.zoom, size=a.size, preset=a.preset, cell_data=a.cell_data,
        dark=a.dark, no_axes=a.no_axes,
    )
    if a.range:
        job["range"] = a.range
    res = safe_render(job)
    res.setdefault("seconds", {})
    res["seconds"].update(
        paraview_import=round(T_IMPORTED - T_START, 3),
        process_total=round(time.time() - T_START, 3),
    )
    res["paraview"] = str(getattr(paraview, "__version__", ""))
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
