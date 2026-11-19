"""Drive XFoil 6.99 non-interactively (in Docker, image openfoam-xfoil:2512) and return plain dicts.

    from wingstudio.cfd.xfoil import run_polar, run_cp, alpha_for_cl
    run_polar("2412", re=2e6, sweep=(-2, 10, 2), xtr=0.05)
    run_cp("4412", re=2e6, alpha=4.0, xtr=0.05)
    alpha_for_cl("4412", re=2e6, target_cl=0.6)

    python -m wingstudio.cfd.xfoil --naca 4412 --re 2e6 --alpha 4 [--xtr 0.05] [--cp] [--target-cl 0.6]

Like-for-like with a fully turbulent RANS run: use xtr=0.05 (transition forced at 5% chord on
both surfaces) and mach=0. Free transition roughly halves the drag and is not comparable.

Geometry: a 4-digit section is the studio's closed-trailing-edge profile
(foamtools.naca.naca4_coords, the one the RANS mesh is built on), loaded as coordinates and used
as the panel nodes. XFoil's own NACA command (blunt trailing edge) and repanelling are not used:
on a sharp trailing edge PANE makes the tripped lift depend on the panel count, while the
cosine-spaced nodes are converged. 5-digit codes still use XFoil's NACA command.

Robustness: graphics off; results read from the polar file (only converged points are written
there); a sweep walks up and down from the angle nearest zero and stops a walk at its first NaN;
missing angles are retried on another panelling, in small steps, or as the average of
alpha -/+ 0.1 deg (flagged "interpolated"); every process runs under `timeout` with capped
output; each call has its own scratch directory and container, so calls may run in parallel.
Every batch of input goes into a new directory: under Colima/virtiofs a file rewritten on the
host can reach the next container with its old size.
"""

import argparse
import json
import math
import os
import re as _re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from foamtools import docker
from foamtools.common import ROOT as PROJECT_ROOT
from foamtools.common import read_text
from foamtools.naca import naca4_coords

from .. import IMAGE

# Scratch must be under the project root: that is the only directory mounted
# into the container. STUDIO_XFOIL_TMP moves it (a test server that must
# not touch studio_data); it has to stay under the project root.
SCRATCH_ROOT = os.environ.get("STUDIO_XFOIL_TMP") or os.path.join(PROJECT_ROOT, "studio_data", "xfoil_tmp")

# Screen lines that a diverging boundary-layer solve repeats millions of times.
_SPAM = r"^ *(TRCHEK2|MRCHDU|MRCHUE|x: |STFIND|SETBL|PABL|BLDIF|UPDATE|TRDIF|HKIN|BLSYS)"
_LOG_CAP_BYTES = 2000000


class XfoilError(Exception):
    pass


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _naca_code(code):
    c = str(code).upper().replace("NACA", "").replace(" ", "").replace("-", "")
    if not _re.fullmatch(r"\d{4,5}", c):
        raise XfoilError("NACA code must be 4 (or 5) digits, got %r" % (code,))
    return c


def _alpha_list(alpha=None, sweep=None):
    if (alpha is None) == (sweep is None):
        raise XfoilError("give exactly one of alpha or sweep=(start, stop, step)")
    if alpha is not None:
        return [round(float(alpha), 3)]
    try:
        a0, a1, da = [float(v) for v in sweep]
    except (TypeError, ValueError):
        raise XfoilError("sweep must be (start, stop, step), got %r" % (sweep,))
    if da == 0:
        raise XfoilError("sweep step must not be zero")
    if (a1 - a0) * da < 0:
        da = -da
    n = int(math.floor((a1 - a0) / da + 1e-9)) + 1
    if n > 400:
        raise XfoilError("sweep has %d points; limit is 400" % n)
    return sorted(set(round(a0 + i * da, 3) for i in range(n)))


def _fnum(x):
    """Number formatting XFoil's free-format reader accepts."""
    return ("%.6f" % float(x)).rstrip("0").rstrip(".") if abs(float(x)) < 1e7 else "%.6e" % float(x)


# --------------------------------------------------------------------------
# building XFoil command files
# --------------------------------------------------------------------------

def _settings(naca, re, mach, ncrit, xtr_top, xtr_bot, panels, iter_limit):
    naca = _naca_code(naca)
    try:
        re = float(re) if re else 0.0
        mach = float(mach or 0.0)
        panels = int(panels)
        xtr_top, xtr_bot = float(xtr_top), float(xtr_bot)
        ncrit, iter_limit = float(ncrit), int(iter_limit)
    except (TypeError, ValueError) as e:
        raise XfoilError("bad numeric setting: %s" % e)
    if re < 0 or mach < 0:
        raise XfoilError("re and mach must be >= 0")
    if mach >= 1.0:
        raise XfoilError("XFoil is subsonic only (Karman-Tsien correction); mach=%g" % mach)
    if not 40 <= panels <= 360:
        raise XfoilError("panels must be between 40 and 360 (XFoil array limit), got %d" % panels)
    for name, v in (("xtr_top", xtr_top), ("xtr_bot", xtr_bot)):
        if not 0.0 < v <= 1.0:
            raise XfoilError("%s must be in (0, 1], got %r" % (name, v))
    return {
        "naca": naca, "re": re, "mach": mach, "ncrit": ncrit,
        "xtr_top": xtr_top, "xtr_bot": xtr_bot,
        "panels": panels, "iter": iter_limit, "viscous": re > 0,
        # 4-digit: the studio's own closed-trailing-edge coordinates (see the
        # module docstring); 5-digit: XFoil's NACA command
        "studio_geometry": len(naca) == 4,
    }


def _coord_file(cfg):
    return "naca%s_n%d.dat" % (cfg["naca"], cfg["panels"])


def _n_nodes(cfg):
    return 2 * (cfg["panels"] // 2) + 1 if cfg["studio_geometry"] else cfg["panels"]


def studio_coordinates(naca, panels=320):
    """XFoil coordinate file (labelled format) of the profile the RANS mesh is
    built on: foamtools.naca.naca4_coords with panels // 2 cosine-spaced
    intervals per surface, ordered upper trailing edge -> leading edge ->
    lower trailing edge. The first and the last point coincide (closed
    trailing edge)."""
    code = _naca_code(naca)
    try:
        upper, lower = naca4_coords(code, int(panels) // 2)
    except ValueError as e:
        raise XfoilError(str(e))
    lines = ["NACA %s closed trailing edge (studio profile)" % code]
    lines += [" %.8f %.8f" % (x, round(z, 8) + 0.0) for x, z in upper[::-1] + lower[1:]]
    return "\n".join(lines) + "\n"


def _geometry_files(*cfgs):
    """{file name: text} of the coordinate files the sessions of these settings LOAD."""
    return {_coord_file(c): studio_coordinates(c["naca"], c["panels"])
            for c in cfgs if c["studio_geometry"]}


def _session(cfg, ops, polar):
    """One XFoil stdin script. ops: list of ("alfa", a) | ("init",) |
    ("cpwr", file) | ("dump", file) | ("psav", file)."""
    L = ["PLOP", "G F", ""]
    if cfg["studio_geometry"]:
        # at most 361 points: XFoil takes them as the panel nodes, no PANE
        L.append("LOAD %s" % _coord_file(cfg))
    else:
        L += ["NACA %s" % cfg["naca"], "PPAR", "N %d" % cfg["panels"], "", ""]
    for op in ops:
        if op[0] == "psav":
            L.append("PSAV %s" % op[1])
    L.append("OPER")
    if cfg["viscous"]:
        L.append("VISC %s" % _fnum(cfg["re"]))
    L.append("MACH %s" % _fnum(cfg["mach"]))
    L.append("ITER %d" % cfg["iter"])
    if cfg["viscous"]:
        L += ["VPAR", "N %s" % _fnum(cfg["ncrit"]),
              "XTR %s %s" % (_fnum(cfg["xtr_top"]), _fnum(cfg["xtr_bot"])), ""]
    L += ["PACC", polar, ""]
    for op in ops:
        if op[0] == "alfa":
            L.append("ALFA %.3f" % op[1])
        elif op[0] == "init":
            if cfg["viscous"]:
                L.append("INIT")
        elif op[0] == "cpwr":
            L.append("CPWR %s" % op[1])
        elif op[0] == "dump":
            if cfg["viscous"]:
                L.append("DUMP %s" % op[1])
    L += ["PACC", "", "", "QUIT", ""]
    return "\n".join(L)


def _sweep_branches(alphas):
    """Order a sweep as two warm walks that both start nearest zero: upwards,
    and (if there are lower angles) downwards. Each branch runs in its own
    XFoil process, so a divergence near stall on one cannot spoil the other."""
    al = sorted(alphas)
    pivot = min(range(len(al)), key=lambda i: abs(al[i]))
    return al[pivot:], al[:pivot][::-1]


def _walk(a_from, a_to, max_step=0.5, max_n=16):
    n = max(1, min(max_n, int(math.ceil(abs(a_to - a_from) / max_step))))
    return [round(a_from + (a_to - a_from) * i / n, 3) for i in range(0, n + 1)]


# --------------------------------------------------------------------------
# running sessions in the container
# --------------------------------------------------------------------------

_RUN_SH = r"""#!/bin/bash
# generated by wingstudio/cfd/xfoil.py - runs each XFoil session with a hard timeout
cd "$(dirname "$0")" || exit 97
command -v xfoil >/dev/null 2>&1 || { echo "xfoil binary not found in the image" > runner.err; exit 96; }
ulimit -c 0 2>/dev/null
: > status.txt
done_groups=" "
# sessions.txt: name  timeout_s  group  regex-that-means-the-group-is-done (or -)
while read -r name secs group pat
do
    [ -z "$name" ] && continue
    case "$done_groups" in *" $group "*) echo "$name skip 0 0" >> status.txt; continue;; esac
    t0=${EPOCHREALTIME:-0}
    # awk drops the spam lines and exits at the first NaN residual: from there on
    # the boundary-layer state is poisoned, every further point would burn its
    # full iteration limit (about 3 s each) and none would converge. XFoil then
    # dies on SIGPIPE (rc 141); the points already converged are in the polar file.
    timeout -k 1 "$secs" xfoil < "$name.in" 2>&1 \
        | awk '/__SPAM__/ {next} {print} /rms: +NaN/ {exit}' | head -c __CAP__ > "$name.log"
    rc=${PIPESTATUS[0]}
    echo "$name $rc $t0 ${EPOCHREALTIME:-0}" >> status.txt
    if [ "$pat" != "-" ] && [ -f "$name.pol" ] && grep -q -E "$pat" "$name.pol"
    then done_groups="$done_groups$group "
    fi
done < sessions.txt
exit 0
"""


def _run_batch(sessions, workdir, per_session_timeout, deadline, files=None):
    """Run XFoil sessions in ONE container start.

    files: {name: text} written next to the session inputs (coordinate files).

    sessions: list of (name, stdin_text) or (name, stdin_text, group,
    target_alpha): once a session of a group has target_alpha in its polar
    file the remaining sessions of that group are skipped.
    Returns ({name: {"rc", "skipped", "solver_s"}}, info). Never raises on an
    XFoil failure; raises XfoilError only when the runner itself is unusable.

    workdir must be a NEW directory for every batch (see the module docstring)."""
    os.makedirs(workdir)
    for fname, text in (files or {}).items():
        with open(os.path.join(workdir, fname), "w") as g:
            g.write(text)
    secs = max(1, int(math.ceil(per_session_timeout)))
    with open(os.path.join(workdir, "sessions.txt"), "w") as f:
        for i, sess in enumerate(sessions):
            name, text = sess[0], sess[1]
            with open(os.path.join(workdir, name + ".in"), "w") as g:
                g.write(text)
            group, pat = "g%d" % i, "-"
            if len(sess) >= 4:
                group = sess[2]
                pat = "^[[:space:]]*%s[[:space:]]" % ("%.3f" % sess[3]).replace(".", "\\.")
            f.write("%s %d %s %s\n" % (name, secs, group, pat))
    with open(os.path.join(workdir, "run.sh"), "w") as f:
        f.write(_RUN_SH.replace("__SPAM__", _SPAM).replace("__CAP__", str(_LOG_CAP_BYTES)))

    container = "xfoil-%s" % uuid.uuid4().hex[:12]
    rel = os.path.relpath(os.path.realpath(workdir), os.path.realpath(PROJECT_ROOT))
    if rel.startswith(".."):
        raise XfoilError("scratch directory %s is outside the project root, so the container "
                         "cannot see it" % workdir)
    timed_out = False
    try:
        docker.ensure_running()           # first: starting Colima must not count against the timeout
        budget = max(1.0, deadline - time.time())
        t0 = time.time()
        # Own session: a Ctrl-C in the terminal must not leave a half-killed docker client.
        proc = docker.popen(["bash", rel + "/run.sh"], name=container, image_name=IMAGE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except docker.DockerError as e:
        raise XfoilError(f"{e}; XFoil runs in the {IMAGE} image")
    try:
        out, _ = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        timed_out = True
        docker.kill(container)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = b""
    wall = time.time() - t0
    out_text = (out or b"").decode("utf-8", "replace")

    status = {}
    spath = os.path.join(workdir, "status.txt")
    if os.path.exists(spath):
        with open(spath) as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    try:
                        dt = float(p[3]) - float(p[2])
                    except ValueError:
                        dt = 0.0
                    status[p[0]] = {"rc": int(p[1]) if p[1].lstrip("-").isdigit() else None,
                                    "skipped": p[1] == "skip",
                                    "solver_s": round(dt, 4) if dt > 0 else None}
    info = {"wall_s": wall, "timed_out": timed_out, "runner_rc": proc.returncode,
            "xfoil_s": sum(v["solver_s"] or 0.0 for v in status.values())}
    if not status and not timed_out:
        err = read_text(os.path.join(workdir, "runner.err")).strip()
        hint = ""
        if "Unable to find image" in out_text or "pull access denied" in out_text:
            hint = " [build the image first: docker build -t %s docker/]" % IMAGE
        raise XfoilError("XFoil runner produced nothing (image %s, rc=%s): %s%s"
                         % (IMAGE, proc.returncode, (err or out_text.strip())[-600:], hint))
    return status, info


# --------------------------------------------------------------------------
# parsing XFoil output files
# --------------------------------------------------------------------------

_COLMAP = {"alpha": "alpha", "CL": "CL", "CD": "CD", "CDp": "CDp", "CM": "CM",
           "Top_Xtr": "xtr_top", "Bot_Xtr": "xtr_bot"}


def parse_polar(path):
    """Parse an XFoil polar save file -> (header dict, {alpha: row dict})."""
    head, rows = {}, {}
    cols, in_data = None, False
    for line in read_text(path).splitlines():
        s = line.strip()
        if not in_data:
            m = _re.search(r"Version\s+([\d.]+)", s)
            if m:
                head["xfoil_version"] = m.group(1)
            m = _re.search(r"xtrf\s*=\s*([\d.]+)\s*\(top\)\s*([\d.]+)\s*\(bottom\)", s)
            if m:
                head["xtr_top"], head["xtr_bot"] = float(m.group(1)), float(m.group(2))
            m = _re.search(r"Mach\s*=\s*([\d.]+)\s+Re\s*=\s*([\d.]+)\s*e\s*(\d+)\s+Ncrit\s*=\s*([\d.]+)", s)
            if m:
                head["mach"] = float(m.group(1))
                head["re"] = float(m.group(2)) * 10 ** int(m.group(3))
                head["ncrit"] = float(m.group(4))
            if s.startswith("alpha") and "CL" in s:
                cols = s.split()
            if s.startswith("---") and cols:
                in_data = True
            continue
        p = s.split()
        if len(p) < 5:
            continue
        try:
            vals = [float(v) for v in p]
        except ValueError:
            continue               # e.g. "********" overflow fields
        row = {}
        for name, v in zip(cols, vals):
            if name in _COLMAP:
                row[_COLMAP[name]] = v + 0.0       # "-0.0000" (symmetric section at zero angle) -> 0.0
        if "alpha" in row and all(math.isfinite(v) for v in row.values()):
            rows[round(row["alpha"], 3)] = row
    return head, rows


def parse_log(path):
    """Best-effort extras from the (filtered) screen log: Newton iterations,
    final rms and friction drag per converged angle. Never raises."""
    out = {}
    try:
        with open(path, errors="replace") as f:
            it, rms, a, cdf = 0, None, None, None
            for line in f:
                m = _re.match(r"\s*(\d+)\s+rms:\s*(\S+)", line)
                if m:
                    it = int(m.group(1))
                    try:
                        rms = float(m.group(2))
                    except ValueError:
                        rms = None
                    continue
                m = _re.match(r"\s*a =\s*(-?[\d.]+)\s+CL =", line)
                if m:
                    a = round(float(m.group(1)), 3)
                    continue
                m = _re.search(r"CDf =\s*(-?[\d.]+)", line)
                if m:
                    cdf = float(m.group(1))
                    continue
                if "Point added to stored polar" in line and a is not None:
                    out[a] = {"iterations": it, "rms": rms, "CDf": cdf}
                    it, rms, a, cdf = 0, None, None, None
                elif "Convergence failed" in line and "VISCAL" in line:
                    it, rms, a, cdf = 0, None, None, None
    except Exception:
        pass
    return out


def _read_table(path):
    rows = []
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            v = [float(x) for x in s.split()]
        except ValueError:
            continue
        if len(v) >= 2:
            rows.append(v)
    return rows


# --------------------------------------------------------------------------
# core solve: pass 1 (one warm session) + pass 2 (isolated retries)
# --------------------------------------------------------------------------

def _make_workdir():
    """A scratch directory nobody else uses (process id + random suffix)."""
    wd = os.path.join(SCRATCH_ROOT, "xf-%d-%s" % (os.getpid(), uuid.uuid4().hex[:12]))
    os.makedirs(wd)
    return wd


def _solve(cfg, alphas, extras_for, timeout, retry=True):
    """extras_for: {alpha: [("cpwr", "cp.dat"), ("dump", "bl.dat")]} - extra
    output requested right after that ALFA. Files are prefixed with the
    session name and only read from the session in which the angle converged.

    Pass 2 queues up to three strategies per missing angle, all in one
    container start; the shell stops at the first that produced the angle:
      repanel  same angle, cold, with 20 more (or fewer) panel nodes (40 for a
               5-digit section) - cures the classic limit cycle when
               transition sits on a panel node
      walk     same paneling, small steps from the nearest converged angle
      interp   solve alpha -/+ 0.1 deg and average (flagged "interpolated")
    """
    t_start = time.time()
    deadline = t_start + float(timeout)
    wd = _make_workdir()
    notes, batches = [], []
    rows, extras, src, meta, head = {}, {}, {}, {}, {}
    files = {}                     # alpha -> path prefix of the session holding its cp/bl/xy files
    want_files = bool(extras_for)

    def with_extras(name, alfa_ops):
        ops = [("psav", name + "_xy.dat")] if want_files else []
        for op in alfa_ops:
            ops.append(op)
            if op[0] == "alfa":
                ops += [(k, name + "_" + f) for k, f in extras_for.get(op[1], [])]
        return ops

    try:
        b1, b2 = os.path.join(wd, "b1"), os.path.join(wd, "b2")
        up, down = _sweep_branches(alphas)
        first = [("s0", _session(cfg, with_extras("s0", [("alfa", a) for a in up]), "s0.pol"))]
        if down:
            first.append(("s1", _session(cfg, with_extras("s1", [("alfa", a) for a in down]), "s1.pol")))
        status, info = _run_batch(first, b1, per_session_timeout=max(2.0, timeout - 2.0),
                                  deadline=deadline, files=_geometry_files(cfg))
        batches.append(info)
        for name, branch in (("s0", up), ("s1", down)):
            if not branch:
                continue
            h1, got = parse_polar(os.path.join(b1, name + ".pol"))
            head = head or h1
            log = parse_log(os.path.join(b1, name + ".log"))
            for a in branch:
                if a in got:
                    rows[a], src[a], files[a] = got[a], "sweep", os.path.join(b1, name)
                    extras[a] = log.get(a, {})
            rc = status.get(name, {}).get("rc")
            direction = "upward" if name == "s0" else "downward"
            if rc in (124, 137):
                notes.append("pass 1 (%s branch) hit the per-process timeout" % direction)
            elif rc == 141 and any(a not in rows for a in branch):
                notes.append("pass 1 (%s branch) was stopped at the first NaN residual" % direction)
        if info["timed_out"]:
            notes.append("pass 1 hit the overall timeout of %gs" % timeout)

        missing = [a for a in alphas if a not in rows]
        if missing and retry and cfg["viscous"] and time.time() < deadline - 1.0:
            # Cosine-spaced nodes: 20 more panels move a mid-chord point by half a
            # node spacing where 40 (one eighth of 320) can put it on a node again.
            dp = 20 if cfg["studio_geometry"] else 40
            p2 = cfg["panels"] + dp if cfg["panels"] + dp <= 360 else cfg["panels"] - dp
            cfg2 = dict(cfg, panels=p2)
            d = 0.1
            sess, plan = [], []
            for i, a in enumerate(sorted(missing, key=abs)):
                a0 = min(rows, key=lambda b: abs(b - a)) if rows else 0.0
                grp = "m%d" % i
                n1, n2, n3 = "r%dp" % i, "r%dw" % i, "r%db" % i
                # 1. cheapest first: the same angle, cold, on a different paneling
                sess.append((n1, _session(cfg2, with_extras(n1, [("alfa", a)]), n1 + ".pol"), grp, a))
                # 2. same paneling, warm walk from the nearest converged angle
                seq = [("alfa", b) for b in _walk(a0, a)]
                sess.append((n2, _session(cfg, with_extras(n2, seq), n2 + ".pol"), grp, a))
                # 3. bracket and average (not when Cp/BL files are wanted)
                if not want_files:
                    lo, hi = round(a - d, 3), round(a + d, 3)
                    first = lo if abs(lo - a0) <= abs(hi - a0) else hi
                    second = hi if first == lo else lo
                    bseq = [("alfa", b) for b in _walk(a0, first)] + [("alfa", second)]
                    sess.append((n3, _session(cfg, bseq, n3 + ".pol"), grp, a))
                plan.append((a, n1, n2, n3 if not want_files else None))
            per = max(2.0, min(20.0, (deadline - time.time() - 1.0) / max(1, len(sess))))
            _, info2 = _run_batch(sess, b2, per_session_timeout=per, deadline=deadline,
                                  files=_geometry_files(cfg, cfg2))
            batches.append(info2)
            for a, n1, n2, n3 in plan:
                for name, kind in ((n1, "retry-repanel"), (n2, "retry-walk")):
                    h2, got2 = parse_polar(os.path.join(b2, name + ".pol"))
                    head = head or h2
                    if a in got2:
                        rows[a], src[a], files[a] = got2[a], kind, os.path.join(b2, name)
                        extras[a] = parse_log(os.path.join(b2, name + ".log")).get(a, {})
                        if kind == "retry-repanel":
                            meta[a] = {"panels_used": p2}
                        break
                if a in rows or not n3:
                    continue
                _, got3 = parse_polar(os.path.join(b2, n3 + ".pol"))
                lo, hi = round(a - d, 3), round(a + d, 3)
                if lo in got3 and hi in got3:
                    rows[a] = dict((k, round(0.5 * (got3[lo][k] + got3[hi][k]), 5))
                                   for k in got3[lo] if k in got3[hi])
                    rows[a]["alpha"] = a
                    src[a] = "retry-interp"
                    extras[a] = {}
                    meta[a] = {"interpolated": True, "interpolated_from": [lo, hi]}
            if info2["timed_out"]:
                notes.append("retry pass hit the overall timeout")
            how = ", ".join("%g: %s" % (a, src.get(a, "FAILED")) for a in sorted(missing))
            notes.append("%d angle(s) did not converge in the sweep and were retried in "
                         "isolated processes (%s)" % (len(missing), how))

        if missing and not batches[1:] and retry and cfg["viscous"]:
            notes.append("%d angle(s) not converged and no time left to retry them "
                         "(raise timeout)" % len(missing))

        payload = {}
        for a, name in files.items():
            for kind, fname in extras_for.get(a, []):
                payload.setdefault(a, {})[kind] = _read_table(name + "_" + fname)
            if want_files:
                payload.setdefault(a, {})["xy"] = _read_table(name + "_xy.dat")
        return {"rows": rows, "extras": extras, "src": src, "meta": meta, "head": head,
                "notes": notes, "batches": batches, "payload": payload,
                "wall_s": time.time() - t_start}
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def _base_result(cfg, core, alphas):
    head = core["head"]
    warn = list(core["notes"])
    if cfg["mach"] > 0.5:
        warn.append("Mach %.2f: XFoil only applies a Karman-Tsien correction; results degrade "
                    "as local flow approaches sonic and are invalid with shocks" % cfg["mach"])
    if not cfg["viscous"]:
        warn.append("inviscid run (re=0): CD is meaningless, no transition data")
    n_conv = sum(1 for a in alphas if a in core["rows"])
    xfoil_s = sum(b.get("xfoil_s", 0.0) for b in core["batches"])
    free = cfg["xtr_top"] >= 1.0 and cfg["xtr_bot"] >= 1.0
    return {
        "ok": True,
        "solver": "xfoil",
        "xfoil_version": head.get("xfoil_version"),
        "airfoil": "NACA %s" % cfg["naca"],
        "naca": cfg["naca"],
        "re": cfg["re"], "mach": cfg["mach"], "ncrit": cfg["ncrit"],
        "xtr_top": cfg["xtr_top"], "xtr_bot": cfg["xtr_bot"],
        "transition": "free (e^N, Ncrit=%g)" % cfg["ncrit"] if free
                      else "forced at x/c top=%g bottom=%g (or earlier if e^N triggers first)"
                           % (cfg["xtr_top"], cfg["xtr_bot"]),
        "panels": cfg["panels"], "n_nodes": _n_nodes(cfg), "iter_limit": cfg["iter"],
        "geometry": ("studio closed-trailing-edge NACA %s (foamtools.naca.naca4_coords, the profile "
                     "the RANS mesh is built on), %d cosine-spaced nodes read with LOAD"
                     % (cfg["naca"], _n_nodes(cfg))) if cfg["studio_geometry"]
                    else "XFoil's built-in NACA %s (blunt trailing edge)" % cfg["naca"],
        "trailing_edge": "closed" if cfg["studio_geometry"] else "blunt",
        # what XFoil itself echoed in the polar file header (proof the settings took)
        "xfoil_echo": {k: head.get(k) for k in ("re", "mach", "ncrit", "xtr_top", "xtr_bot")},
        "n_requested": len(alphas), "n_converged": n_conv,
        "all_converged": n_conv == len(alphas),
        "image": IMAGE,
        "container_starts": len(core["batches"]),
        "wall_s": round(core["wall_s"], 3),
        "wall_s_per_point": round(core["wall_s"] / max(1, len(alphas)), 3),
        # time inside the XFoil processes only (excludes the container start)
        "xfoil_s": round(xfoil_s, 3) if xfoil_s > 0 else None,
        "warnings": warn,
    }


def _point(a, core):
    """One polar point as returned to callers."""
    r = core["rows"].get(a)
    if r is None:
        return {"alpha": a, "converged": False, "CL": None, "CD": None, "CDp": None, "CM": None,
                "LD": None, "xtr_top": None, "xtr_bot": None}
    ex = core["extras"].get(a, {})
    p = {"alpha": a, "converged": True,
         "CL": r.get("CL"), "CD": r.get("CD"), "CDp": r.get("CDp"), "CM": r.get("CM"),
         "LD": round(r["CL"] / r["CD"], 2) if r.get("CD") else None,
         "xtr_top": r.get("xtr_top"), "xtr_bot": r.get("xtr_bot"),
         "CDf": ex.get("CDf"), "iterations": ex.get("iterations"), "rms": ex.get("rms"),
         "source": core["src"].get(a)}
    p.update(core["meta"].get(a, {}))
    return p


def _failure(e):
    return {"ok": False, "solver": "xfoil", "error": str(e)}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def run_polar(naca, re, mach=0.0, alpha=None, sweep=None, ncrit=9.0, xtr=None, panels=320,
              iter_limit=100, timeout=60.0, xtr_top=1.0, xtr_bot=1.0, retry=True):
    """Polar point(s) for a NACA 4-digit section on the studio's closed-trailing-edge
    profile (the shape the RANS mesh is built on), or a 5-digit one from XFoil's generator.

    naca        "4412"                       re     chord Reynolds number (0 = inviscid)
    mach        freestream Mach (<1)         alpha  one angle [deg]   OR
    sweep       (start, stop, step) [deg]    ncrit  e^N amplification factor (9 = clean tunnel)
    xtr         forced transition x/c on BOTH surfaces (0.05 mimics a fully turbulent RANS run);
                xtr_top / xtr_bot set them separately; None or 1.0 = free transition
    panels      40..360; a 4-digit section gets panels // 2 cosine-spaced intervals per surface
                (320 -> 321 nodes: CL within 0.1% of 361 nodes; 160 is 1% low on a closed trailing edge)
    iter_limit  Newton iterations per point
    timeout     overall wall-clock limit [s] for everything, including retries

    Returns a JSON-serialisable dict with "ok", "points" [{alpha, converged, CL, CD, CDp, CM, LD,
    xtr_top, xtr_bot, CDf, iterations, rms, source}], "wall_s", "warnings" and the settings.
    Never raises: XFoil-side failures are reported per point ("converged": false) and runner
    failures as {"ok": false, "error": ...}.
    """
    try:
        if xtr is not None:
            xtr_top = xtr_bot = xtr
        cfg = _settings(naca, re, mach, ncrit, xtr_top, xtr_bot, panels, iter_limit)
        alphas = _alpha_list(alpha, sweep)
        core = _solve(cfg, alphas, {}, float(timeout), retry)
    except XfoilError as e:
        return _failure(e)
    except Exception as e:                       # never let the caller's thread die
        return _failure("%s: %s" % (type(e).__name__, e))
    res = _base_result(cfg, core, alphas)
    res["points"] = [_point(a, core) for a in alphas]
    res["columns_note"] = ("CL, CD, CDp, CM, xtr_* are from the XFoil polar file. CD is the wake "
                           "(Squire-Young) drag. CDp in the polar file is the surface-pressure "
                           "integral; CDf, iterations and rms are read from the screen log "
                           "(CD - CDf is XFoil's own pressure/form drag). CM is about x/c = 0.25.")
    if sweep is not None:
        res["sweep"] = [float(v) for v in sweep]
    return res


def run_cp(naca, re, alpha, mach=0.0, xtr=None, ncrit=9.0, panels=320, iter_limit=100,
           timeout=60.0, xtr_top=1.0, xtr_bot=1.0):
    """Surface pressure coefficient (CPWR) and, for viscous runs, skin friction
    and edge velocity (BL DUMP) at one angle of attack.

    Returns the usual header plus "point" (the polar values) and
      "upper"/"lower": {"x": [...], "y": [...], "cp": [...], "cf": [...], "ue": [...]}
                       ordered leading edge -> trailing edge
      "x", "y", "cp":  the raw XFoil ordering (upper TE -> LE -> lower TE)
    """
    try:
        if xtr is not None:
            xtr_top = xtr_bot = xtr
        cfg = _settings(naca, re, mach, ncrit, xtr_top, xtr_bot, panels, iter_limit)
        a = round(float(alpha), 3)
        core = _solve(cfg, [a], {a: [("cpwr", "cp.dat"), ("dump", "bl.dat")]}, float(timeout))
    except XfoilError as e:
        return _failure(e)
    except Exception as e:
        return _failure("%s: %s" % (type(e).__name__, e))
    res = _base_result(cfg, core, [a])
    res["alpha"] = a
    res["converged"] = a in core["rows"]
    if not res["converged"]:
        res["ok"] = False
        res["error"] = "XFoil did not converge at alpha=%g" % a
        res["point"] = None
        return res
    res["point"] = _point(a, core)
    pl = core["payload"].get(a, {})
    cpt = pl.get("cpwr", [])
    xy = pl.get("xy", [])
    bl = pl.get("dump", [])
    if not cpt:
        res["ok"] = False
        res["error"] = "XFoil converged but wrote no Cp file"
        return res
    x = [v[0] for v in cpt]
    cp = [v[-1] for v in cpt]
    if len(cpt[0]) >= 3:
        y = [v[1] for v in cpt]
    elif len(xy) == len(cpt):
        y = [v[1] for v in xy]
    else:
        y = [None] * len(cpt)
    n = len(x)
    cf = [None] * n
    ue = [None] * n
    if len(bl) >= n and all(len(v) >= 7 for v in bl[:n]):
        # BL dump: s x y Ue/Vinf Dstar Theta Cf H ...  (airfoil nodes first, then wake)
        cf = [v[6] for v in bl[:n]]
        ue = [v[3] for v in bl[:n]]
    ile = min(range(n), key=lambda i: x[i])

    def pick(idx):
        return {"x": [x[i] for i in idx], "y": [y[i] for i in idx],
                "cp": [cp[i] for i in idx], "cf": [cf[i] for i in idx],
                "ue": [ue[i] for i in idx]}

    res["n_nodes"] = n
    res["cp_min"] = min(cp)
    res["cp_min_x"] = x[cp.index(min(cp))]
    res["upper"] = pick(list(range(ile, -1, -1)))
    res["lower"] = pick(list(range(ile, n)))
    res["x"], res["y"], res["cp"] = x, y, cp
    res["cp_note"] = ("Cp from CPWR (viscous, compressibility-corrected if mach>0). cf is "
                      "tau_w / (0.5 rho Vinf^2) from the BL dump; ue is edge velocity / Vinf. "
                      "upper/lower run from leading edge to trailing edge.")
    return res


def alpha_for_cl(naca, re, target_cl, mach=0.0, xtr=0.05, lo=-4.0, hi=12.0, step=1.0,
                 tol=0.002, ncrit=9.0, panels=320, timeout=90.0):
    """Angle of attack at which the section gives the lift coefficient target_cl.

    One sweep from lo to hi (step deg) is run, the angle is interpolated
    linearly between the two converged points that bracket target_cl on the
    attached-flow branch (below maximum lift), and that angle is then confirmed
    with a single XFoil point. If the confirmed CL is further than tol from the
    target, up to two secant corrections are made.

    Returns {"ok": True, "alpha", "point", "polar", "target_cl", "cl_error",
             "confirmed", "cl_max", "alpha_cl_max", "refinements", "wall_s", "warnings"}
    where "point" is the confirmed polar point (same keys as run_polar points)
    and "polar" is the list of sweep points.

    When the target cannot be met the result is {"ok": False, "reason", "error",
    "polar", "cl_max", "alpha_cl_max", "cl_min", "alpha_cl_min"} with reason one of
      "above_cl_max"  the section stalls before reaching target_cl
      "above_range"   CL is still rising at hi; raise hi
      "below_range"   target_cl is below the CL at lo; lower lo
      "no_polar"      too few converged points to interpolate
    """
    t0 = time.time()
    try:
        target = float(target_cl)
        lo, hi, step = float(lo), float(hi), abs(float(step))
    except (TypeError, ValueError):
        return _failure("target_cl, lo, hi and step must be numbers")
    if not math.isfinite(target) or hi <= lo or step <= 0:
        return _failure("need a finite target_cl, hi > lo and step > 0")
    deadline = t0 + float(timeout)

    sweep = run_polar(naca, re, mach=mach, sweep=(lo, hi, step), ncrit=ncrit, xtr=xtr,
                      panels=panels, timeout=max(5.0, float(timeout) * 0.6))
    if not sweep.get("ok"):
        return sweep
    polar = sweep["points"]
    warnings = list(sweep.get("warnings", []))
    base = {"solver": "xfoil", "naca": sweep["naca"], "re": sweep["re"], "mach": sweep["mach"],
            "transition": sweep["transition"], "xtr_top": sweep["xtr_top"],
            "xtr_bot": sweep["xtr_bot"], "target_cl": target, "range": [lo, hi, step],
            "polar": polar}

    conv = sorted((p for p in polar if p["converged"]), key=lambda p: p["alpha"])

    def fail(reason, message):
        out = dict(base, ok=False, reason=reason, error=message, warnings=warnings,
                   wall_s=round(time.time() - t0, 3))
        if conv:
            top = max(conv, key=lambda p: p["CL"])
            bot = min(conv, key=lambda p: p["CL"])
            out.update(cl_max=top["CL"], alpha_cl_max=top["alpha"],
                       cl_min=bot["CL"], alpha_cl_min=bot["alpha"])
        return out

    if len(conv) < 2:
        return fail("no_polar", "only %d of %d sweep points converged; cannot interpolate"
                    % (len(conv), len(polar)))

    # Attached-flow branch: from the lowest angle up to maximum lift. XFoil
    # also "converges" past stall, but those points are not trustworthy and
    # would give a second, wrong angle for the same CL.
    i_max = max(range(len(conv)), key=lambda i: conv[i]["CL"])
    branch = conv[:i_max + 1]
    cl_max, a_max = conv[i_max]["CL"], conv[i_max]["alpha"]
    if target > cl_max:
        if i_max == len(conv) - 1 and a_max >= hi - 1.5 * step:
            return fail("above_range", "target Cl %.3f is not reached by alpha = %g deg (Cl %.3f "
                        "there and still rising); raise hi" % (target, a_max, cl_max))
        return fail("above_cl_max", "target Cl %.3f is above the maximum lift coefficient of NACA %s "
                    "at Re %.3g (Cl_max %.3f at alpha = %g deg)"
                    % (target, sweep["naca"], sweep["re"], cl_max, a_max))
    if target < branch[0]["CL"]:
        return fail("below_range", "target Cl %.3f is below the Cl at alpha = %g deg (%.3f); lower lo"
                    % (target, branch[0]["alpha"], branch[0]["CL"]))

    bracket = None
    for p1, p2 in zip(branch, branch[1:]):
        if min(p1["CL"], p2["CL"]) <= target <= max(p1["CL"], p2["CL"]) and p2["CL"] != p1["CL"]:
            bracket = (p1, p2)
            break
    if bracket is None:                       # target equals a single isolated point
        exact = min(branch, key=lambda p: abs(p["CL"] - target))
        bracket = (exact, exact)
    p1, p2 = bracket
    if p1 is p2:
        slope, alpha = None, p1["alpha"]
    else:
        slope = (p2["CL"] - p1["CL"]) / (p2["alpha"] - p1["alpha"])
        alpha = p1["alpha"] + (target - p1["CL"]) / slope
        if p2["alpha"] - p1["alpha"] > 2.5 * step:
            warnings.append("bracketing points are %g deg apart (points in between did not "
                            "converge); the interpolation is coarser than usual"
                            % (p2["alpha"] - p1["alpha"]))
    alpha = round(alpha, 3)

    def interpolated_point(a):
        """Fallback when the confirmation run does not converge."""
        if p1 is p2:
            return dict(p1)
        w = (a - p1["alpha"]) / (p2["alpha"] - p1["alpha"])
        pt = {"alpha": a, "converged": False, "interpolated": True,
              "interpolated_from": [p1["alpha"], p2["alpha"]]}
        for k in ("CL", "CD", "CDp", "CM", "xtr_top", "xtr_bot"):
            pt[k] = round(p1[k] + w * (p2[k] - p1[k]), 5) if p1.get(k) is not None and p2.get(k) is not None else None
        pt["LD"] = round(pt["CL"] / pt["CD"], 2) if pt["CD"] else None
        return pt

    # Confirm with a single point; correct with the local lift slope if needed.
    best, refinements, confirm_wall = None, 0, 0.0
    a_try = alpha
    for attempt in range(3):
        left = deadline - time.time()
        if left < 3.0:
            warnings.append("no time left to confirm the angle (raise timeout)")
            break
        one = run_polar(naca, re, mach=mach, alpha=a_try, ncrit=ncrit, xtr=xtr, panels=panels,
                        timeout=min(30.0, left))
        confirm_wall += one.get("wall_s", 0.0) if one.get("ok") else 0.0
        if not one.get("ok"):
            warnings.append("confirmation run failed: %s" % one.get("error"))
            break
        pt = one["points"][0]
        warnings += one.get("warnings", [])
        if not pt["converged"]:
            warnings.append("confirmation point at alpha = %g deg did not converge" % a_try)
            break
        if best is None or abs(pt["CL"] - target) < abs(best["CL"] - target):
            best = pt
        if abs(pt["CL"] - target) <= tol or not slope or attempt == 2:
            break
        a_next = round(a_try + (target - pt["CL"]) / slope, 3)
        if a_next == a_try or not (lo - step <= a_next <= hi + step):
            break
        a_try = a_next
        refinements += 1

    confirmed = best is not None
    point = best if confirmed else interpolated_point(alpha)
    if not confirmed:
        warnings.append("angle is the linear interpolation between alpha = %g and %g deg; "
                        "it was not confirmed by a converged XFoil point" % (p1["alpha"], p2["alpha"]))
    elif abs(point["CL"] - target) > tol:
        warnings.append("confirmed Cl %.4f differs from the target %.4f by more than %.3g"
                        % (point["CL"], target, tol))
    if cl_max - target < 0.1:
        warnings.append("target Cl is within 0.1 of maximum lift (Cl_max %.3f at %g deg): "
                        "XFoil is unreliable this close to stall" % (cl_max, a_max))
    return dict(base, ok=True, alpha=point["alpha"], point=point, confirmed=confirmed,
                cl_error=round(point["CL"] - target, 5),
                alpha_interpolated=alpha, bracket=[p1["alpha"], p2["alpha"]],
                refinements=refinements, cl_max=cl_max, alpha_cl_max=a_max,
                cl_max_is_range_limit=bool(i_max == len(conv) - 1),
                n_converged=len(conv), n_requested=len(polar),
                sweep_wall_s=sweep["wall_s"], confirm_wall_s=round(confirm_wall, 3),
                wall_s=round(time.time() - t0, 3), warnings=warnings)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run XFoil headless (in the %s image) for a NACA section and print JSON." % IMAGE,
        epilog="examples:\n"
               "  xfoil.py --naca 4412 --re 2e6 --alpha 4\n"
               "  xfoil.py --naca 4412 --re 2e6 --alpha 4 --xtr 0.05\n"
               "  xfoil.py --naca 2412 --re 2e6 --sweep -2 10 2\n"
               "  xfoil.py --naca 4412 --re 2e6 --alpha 4 --cp\n"
               "  xfoil.py --naca 4412 --re 2e6 --target-cl 0.6      (xtr defaults to 0.05 here)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--naca", required=True, help="NACA 4-digit code, e.g. 4412")
    ap.add_argument("--re", type=float, required=True, help="chord Reynolds number (0 = inviscid)")
    ap.add_argument("--mach", type=float, default=0.0)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--alpha", type=float, help="single angle of attack [deg]")
    g.add_argument("--sweep", type=float, nargs=3, metavar=("START", "STOP", "STEP"))
    g.add_argument("--target-cl", type=float, help="find the angle of attack for this lift coefficient")
    ap.add_argument("--range", type=float, nargs=2, metavar=("LO", "HI"), default=(-4.0, 12.0),
                    help="with --target-cl: angle range searched [deg] (default -4 12)")
    ap.add_argument("--ncrit", type=float, default=9.0, help="e^N factor (default 9)")
    ap.add_argument("--xtr", type=float, default=None,
                    help="forced transition x/c on both surfaces (0.05 ~ fully turbulent); "
                         "default free, except 0.05 with --target-cl")
    ap.add_argument("--xtr-top", type=float, default=1.0)
    ap.add_argument("--xtr-bot", type=float, default=1.0)
    ap.add_argument("--panels", type=int, default=320, help="panel nodes, 40..360 (default 320)")
    ap.add_argument("--iter", type=int, default=100, dest="iter_limit")
    ap.add_argument("--timeout", type=float, default=60.0, help="overall limit [s]")
    ap.add_argument("--cp", action="store_true", help="with --alpha: also return Cp(x), Cf(x)")
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--compact", action="store_true", help="single-line JSON")
    a = ap.parse_args(argv)

    if a.cp and a.alpha is None:
        ap.error("--cp needs --alpha")
    if a.target_cl is not None:
        res = alpha_for_cl(a.naca, a.re, a.target_cl, mach=a.mach,
                           xtr=0.05 if a.xtr is None else a.xtr, lo=a.range[0], hi=a.range[1],
                           ncrit=a.ncrit, panels=a.panels, timeout=max(a.timeout, 30.0))
    elif a.cp:
        res = run_cp(a.naca, a.re, a.alpha, mach=a.mach, xtr=a.xtr, ncrit=a.ncrit, panels=a.panels,
                     iter_limit=a.iter_limit, timeout=a.timeout, xtr_top=a.xtr_top, xtr_bot=a.xtr_bot)
    else:
        res = run_polar(a.naca, a.re, mach=a.mach, alpha=a.alpha, sweep=a.sweep, ncrit=a.ncrit,
                        xtr=a.xtr, panels=a.panels, iter_limit=a.iter_limit, timeout=a.timeout,
                        xtr_top=a.xtr_top, xtr_bot=a.xtr_bot, retry=not a.no_retry)
    print(json.dumps(res, indent=None if a.compact else 1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
