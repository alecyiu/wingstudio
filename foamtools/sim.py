"""sim - structured (JSON) tools for driving OpenFOAM cases, built for LLM agents.

Every subcommand prints one JSON object on stdout and exits 0 if "ok" is true.
Cases live in cases/<name>. OpenFOAM itself runs in Docker (foamtools.docker).

  sim tutorials [FILTER]              list tutorial cases shipped with OpenFOAM
  sim new NAME --from TUTORIAL        create cases/NAME from a tutorial
  sim info NAME                       what the case is: solver, times, files, patches
  sim get NAME FILE [ENTRY]           read a dictionary / one entry
  sim set NAME FILE ENTRY VALUE       change one dictionary entry
  sim run NAME -- CMD [ARGS...]       run an OpenFOAM command in the case, logged
  sim mesh NAME                       checkMesh quality report
  sim status NAME                     parse the solver log: residuals, Courant, errors
  sim plot NAME [--fields U p]        PNG of a 2D result
  sim view NAME                       open the case in ParaView on the Mac
  sim clean NAME                      remove mesh, results and logs (keeps setup)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

from . import docker
from .common import CASES, NUM, ROOT, time_dirs
from .common import read_text as read

# ----------------------------------------------------------------- helpers --

def emit(obj):
    print(json.dumps(obj, indent=1))
    sys.exit(0 if obj.get("ok", True) else 1)


def fail(msg, **extra):
    emit({"ok": False, "error": msg, **extra})


def case_dir(name, must_exist=True):
    if not re.fullmatch(r"[\w+-][\w.+-]*", name or ""):
        fail(f"invalid case name {name!r} (use letters, digits, _ . + -)")
    path = os.path.join(CASES, name)
    if must_exist and not os.path.isdir(os.path.join(path, "system")):
        fail(f"no such case: cases/{name}",
             existing=sorted(os.listdir(CASES)) if os.path.isdir(CASES) else [])
    return path


def of(args, log=None, timeout=None, cwd="."):
    """Run a command in the OpenFOAM container.
    Returns (exit_code, output_or_None, seconds, timed_out)."""
    cname = f"sim-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    start = time.time()
    if log and os.path.exists(log) and os.path.getsize(log):
        os.replace(log, log + ".prev")       # keep the previous run's log
    out_f = open(log, "w") if log else subprocess.PIPE
    try:
        proc = docker.popen(args, cwd=cwd, name=cname, stdout=out_f, stderr=subprocess.STDOUT, text=True)
    except docker.DockerError as e:
        fail(str(e))
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        docker.kill(cname)
        out, _ = proc.communicate()
    if log:
        out_f.close()
    return proc.returncode, out, round(time.time() - start, 2), timed_out


def in_case(name, cmd, **kw):
    """Run argv `cmd` with the case directory as cwd."""
    return of(cmd, cwd=f"cases/{name}", **kw)


def fatal_error(text, strict=False):
    """FOAM FATAL block, or (unless strict) a crash-looking line. Pass
    strict=True when the command is known to have succeeded."""
    m = re.search(r"-->\s*FOAM FATAL.*?(?=\n\s*\n\s*\n|\Z)", text, re.S)
    if m:
        return m.group(0).strip()[:2000]
    if strict:
        return None
    m = re.search(r"^(?!.*(?:trapFpe|cannot open shared object file)).*(Floating point exception|Segmentation fault|"
                  r"sigFpe::|sigSegv::|command not found|No such file or directory|"
                  r"MPI_ABORT|Killed).*$", text, re.M)
    return m.group(0).strip()[:500] if m else None


def dict_entry(text, key):
    m = re.search(rf"^\s*{re.escape(key)}\s+([^;]+);", text, re.M)
    return m.group(1).strip() if m else None


def foam_dictionary_failed(a, out):
    fail("foamDictionary failed", file=a.file, entry=a.entry,
         detail=fatal_error(out or "") or (out or "").strip()[-600:])


# ---------------------------------------------------------------- commands --

def cmd_tutorials(a):
    script = (
        'cd "$FOAM_TUTORIALS" && '
        'grep -H -E "^application" $(find . -path "*/system/controlDict") 2>/dev/null; '
        'echo ---ALLRUN---; find . -name Allrun')
    code, out, _, _ = of(["bash", "-c", script])
    if code != 0:
        fail("could not list tutorials", output=(out or "")[-800:])
    head, _, tail = out.partition("---ALLRUN---")
    allrun = {os.path.dirname(l.strip())[2:] for l in tail.split() if l.strip()}
    rows = []
    for line in head.splitlines():
        m = re.match(r"\./(.+)/system/controlDict:application\s+(\S+?);", line)
        if not m:
            continue
        path, app = m.groups()
        if a.filter and not all(w.lower() in (path + " " + app).lower() for w in a.filter):
            continue
        # Allrun may sit in the case or in a parent folder that drives several cases
        own = path in allrun
        parent = next((p for p in allrun if path.startswith(p + "/")), None)
        rows.append({"tutorial": path, "solver": app, "allrun": own,
                     **({"allrun_in_parent": parent} if parent and not own else {})})
    rows.sort(key=lambda r: r["tutorial"])
    total = len(rows)
    emit({"ok": True, "count": total, "shown": min(total, a.limit),
          "tutorials": rows[:a.limit],
          "hint": "narrow with words, e.g. `sim tutorials incompressible cavity`"})


def cmd_new(a):
    if os.path.exists(case_dir(a.name, must_exist=False)) and not a.force:
        fail(f"cases/{a.name} already exists (use --force to replace it)")
    src = a.source.strip("/")
    script = ('set -e; src="$FOAM_TUTORIALS/$1"; test -d "$src/system" || '
              '{ echo "not a case directory: $1"; exit 3; }; '
              'mkdir -p cases; rm -rf "cases/$2"; cp -r "$src" "cases/$2"')
    code, out, _, _ = of(["bash", "-c", script, "_", src, a.name])
    if code != 0:
        fail("could not create case", output=(out or "").strip()[-800:],
             hint="use a path from `sim tutorials`")
    emit({"ok": True, "case": a.name, "path": f"cases/{a.name}", "from": src,
          "next": f"sim info {a.name}"})


def cmd_info(a):
    path = case_dir(a.name)
    cd = read(os.path.join(path, "system", "controlDict"))
    control = {k: dict_entry(cd, k) for k in
               ("application", "startFrom", "startTime", "stopAt", "endTime", "deltaT",
                "writeControl", "writeInterval", "adjustTimeStep", "maxCo")}
    control = {k: v for k, v in control.items() if v is not None}

    def ls(sub):
        d = os.path.join(path, sub)
        if not os.path.isdir(d):
            return []
        return sorted(f + ("/" if os.path.isdir(os.path.join(d, f)) else "")
                      for f in os.listdir(d))

    times = [name for _, name in time_dirs(path)]
    start = next((t for t in ("0", "0.orig") if os.path.isdir(os.path.join(path, t))), None)
    patches = []
    btxt = read(os.path.join(path, "constant", "polyMesh", "boundary"))
    for m in re.finditer(r"^\s*(\w+)\s*\{([^}]*)\}", btxt, re.M):
        if m.group(1) == "FoamFile":
            continue
        patches.append({"name": m.group(1),
                        "type": dict_entry(m.group(2), "type"),
                        "faces": dict_entry(m.group(2), "nFaces")})
    turb = read(os.path.join(path, "constant", "turbulenceProperties")) or \
        read(os.path.join(path, "constant", "momentumTransport"))
    info = {
        "ok": True, "case": a.name, "path": f"cases/{a.name}",
        "control": control,
        "turbulence": {k: dict_entry(turb, k) for k in ("simulationType", "RASModel", "LESModel")
                       if dict_entry(turb, k)},
        "system_files": ls("system"),
        "constant_files": ls("constant"),
        "initial_fields": ls(start) if start else [],
        "initial_dir": start,
        "time_dirs": times if len(times) <= 12 else times[:3] + ["..."] + times[-3:],
        "n_time_dirs": len(times),
        "mesh_generated": os.path.exists(os.path.join(path, "constant", "polyMesh", "points")),
        "mesh_source": [f for f in ("blockMeshDict", "snappyHexMeshDict", "extrudeMeshDict")
                        if os.path.exists(os.path.join(path, "system", f))],
        "patches": patches,
        "decomposed": len(glob.glob(os.path.join(path, "processor*"))),
        "scripts": [f for f in ("Allrun", "Allrun.pre", "Allclean") if
                    os.path.exists(os.path.join(path, f))],
        "logs": sorted(os.path.basename(f) for f in glob.glob(os.path.join(path, "log.*"))),
    }
    emit(info)


def cmd_get(a):
    case_dir(a.name)
    cmd = ["foamDictionary", a.file]
    if a.entry:
        cmd += ["-entry", a.entry, "-value"]
    code, out, _, _ = in_case(a.name, cmd)
    if code != 0:
        foam_dictionary_failed(a, out)
    out = out.strip()
    if not a.entry:
        out = re.sub(r"/\*.*?\*/", "", out, flags=re.S).strip()
    emit({"ok": True, "case": a.name, "file": a.file, "entry": a.entry, "value": out})


def cmd_set(a):
    case_dir(a.name)
    old_code, old, _, _ = in_case(a.name, ["foamDictionary", a.file, "-entry", a.entry, "-value"])
    code, out, _, _ = in_case(a.name, ["foamDictionary", a.file, "-entry", a.entry,
                                       "-set", a.value])
    if code != 0:
        foam_dictionary_failed(a, out)
    _, new, _, _ = in_case(a.name, ["foamDictionary", a.file, "-entry", a.entry, "-value"])
    emit({"ok": True, "case": a.name, "file": a.file, "entry": a.entry,
          "old": old.strip() if old_code == 0 else None, "new": new.strip()})


def cmd_run(a):
    path = case_dir(a.name)
    cmd = list(a.cmd)
    if not cmd:
        fail("nothing to run; usage: sim run NAME -- blockMesh")
    if cmd[0].startswith("-"):
        fail("sim run options (--sh, --timeout, --np, --log, --tail) go before `--`", got=cmd[0])
    label = os.path.basename(cmd[0].split()[0])
    if a.sh:
        run = ["bash", "-c", " ".join(cmd)]
    elif a.np and a.np > 1:
        run = ["mpirun", "-np", str(a.np)] + cmd + ["-parallel"]
    else:
        run = cmd
    log = os.path.join(path, a.log or f"log.{label}")
    code, _, secs, timed_out = in_case(a.name, run, log=log, timeout=a.timeout)
    text = read(log)
    lines = text.rstrip().splitlines()
    res = {
        "ok": code == 0 and not timed_out,
        "case": a.name, "command": " ".join(run), "exit_code": code, "seconds": secs,
        "timed_out": timed_out,
        "log": os.path.relpath(log, ROOT), "log_lines": len(lines),
        "finished_with_End": any(l.strip().startswith("End") for l in lines[-8:]),
        "fatal_error": fatal_error(text, strict=(code == 0 and not timed_out)),
        "tail": lines[-a.tail:],
    }
    if timed_out:
        res["hint"] = "killed at --timeout; results written so far are kept"
    emit(res)


def cmd_mesh(a):
    path = case_dir(a.name)
    log = os.path.join(path, "log.checkMesh")
    code, _, secs, _ = in_case(a.name, ["checkMesh"], log=log)
    t = read(log)

    def num(pat, cast=float):
        m = re.search(pat, t)
        return cast(m.group(1)) if m else None

    types = {m.group(1): int(m.group(2)) for m in
             re.finditer(r"^\s*(hexahedra|prisms|wedges|pyramids|tet wedges|tetrahedra|polyhedra):\s+(\d+)",
                         t, re.M) if int(m.group(2))}
    bb = re.search(rf"Overall domain bounding box \(({NUM}) ({NUM}) ({NUM})\) \(({NUM}) ({NUM}) ({NUM})\)", t)
    failed = num(r"Failed (\d+) mesh checks", int)
    res = {
        "ok": code == 0 and "Mesh OK" in t,
        "case": a.name, "mesh_ok": "Mesh OK" in t, "failed_checks": failed or 0,
        "cells": num(r"\n\s*cells:\s+(\d+)", int),
        "points": num(r"\n\s*points:\s+(\d+)", int),
        "faces": num(r"\n\s*faces:\s+(\d+)", int),
        "cell_types": types,
        "bounding_box": [float(x) for x in bb.groups()] if bb else None,
        "max_aspect_ratio": num(rf"Max aspect ratio = ({NUM})"),
        "max_non_orthogonality": num(rf"Mesh non-orthogonality Max: ({NUM})"),
        "avg_non_orthogonality": num(rf"non-orthogonality Max: {NUM} average: ({NUM})"),
        "max_skewness": num(rf"Max skewness = ({NUM})"),
        "warnings": [l.strip() for l in t.splitlines() if "***" in l][:10],
        "fatal_error": fatal_error(t, strict=("Mesh OK" in t)),
        "log": os.path.relpath(log, ROOT),
        "guide": "non-orthogonality < 70 and skewness < 4 are safe; above that add "
                 "nNonOrthogonalCorrectors or improve the mesh",
    }
    if res["cells"] is None and not res["fatal_error"]:
        res["fatal_error"] = t.strip()[-600:]
    emit(res)


def cmd_status(a):
    path = case_dir(a.name)
    cd = read(os.path.join(path, "system", "controlDict"))
    app = dict_entry(cd, "application")
    log = os.path.join(path, a.log or f"log.{app}")
    if not a.log and not (app and os.path.exists(log)):
        newest = sorted(glob.glob(os.path.join(path, "log.*")), key=os.path.getmtime)
        if not newest:
            fail("no log file in case; run the solver with `sim run` first")
        log = newest[-1]
    t = read(log)
    if not t:
        fail(f"cannot read {log}")

    times = re.findall(rf"^Time = ({NUM})", t, re.M)
    res_by_field = {}
    for m in re.finditer(rf"Solving for (\S+), Initial residual = ({NUM}), "
                         rf"Final residual = ({NUM}), No Iterations (\d+)", t):
        res_by_field.setdefault(m.group(1), []).append(
            (float(m.group(2)), float(m.group(3)), int(m.group(4))))
    residuals = {}
    for f, rows in res_by_field.items():
        init = [r[0] for r in rows]
        step = max(1, len(init) // 6)
        residuals[f] = {
            "first_initial": init[0], "last_initial": init[-1], "min_initial": min(init),
            "last_final": rows[-1][1], "last_linear_iterations": rows[-1][2],
            "history_sampled": [float(f"{v:.3g}") for v in init[::step][-7:]],
        }
    co = (re.findall(rf"(?<!Interface )Courant Number mean: ({NUM}) max: ({NUM})", t)
          or re.findall(rf"Mean and max Courant Numbers = ({NUM}) ({NUM})", t))
    cont = re.findall(rf"continuity errors : sum local = ({NUM}), global = ({NUM}), "
                      rf"cumulative = ({NUM})", t)
    conv = re.search(r"(SIMPLE|PIMPLE) solution converged in (\d+) iterations", t)
    exe = re.findall(rf"ExecutionTime = ({NUM}) s", t)
    lines = t.rstrip().splitlines()
    ended = any(l.strip() == "End" for l in lines[-12:])
    fatal = fatal_error(t, strict=ended)
    end_time = dict_entry(cd, "endTime")
    last_time = float(times[-1]) if times else None
    bad = [f for f, r in residuals.items()
           if r["last_initial"] != r["last_initial"] or r["last_initial"] > 1e3]
    state = ("failed" if fatal or bad else
             "converged" if conv else
             "finished" if ended else
             "incomplete")
    out = {
        "ok": True, "case": a.name, "log": os.path.relpath(log, ROOT), "solver": app,
        "state": state,
        "steps": len(times), "first_time": float(times[0]) if times else None,
        "last_time": last_time,
        "end_time": float(end_time) if end_time and re.fullmatch(NUM, end_time) else end_time,
        "converged_in_iterations": int(conv.group(2)) if conv else None,
        "residuals": residuals,
        "courant": {"last_mean": float(co[-1][0]), "last_max": float(co[-1][1]),
                    "peak_max": max(float(c[1]) for c in co)} if co else None,
        "continuity": {"sum_local": float(cont[-1][0]), "global": float(cont[-1][1]),
                       "cumulative": float(cont[-1][2])} if cont else None,
        "execution_seconds": float(exe[-1]) if exe else None,
        "time_dirs_written": len(time_dirs(path)),
        "fatal_error": fatal,
        "diverging_fields": bad,
        "state_meaning": {
            "converged": "steady solver met residualControl",
            "finished": "reached endTime (for steady runs: NOT converged, check residuals)",
            "incomplete": "log stops before End: still running, killed, or timed out",
            "failed": "fatal error or diverged",
        }[state],
    }
    emit(out)


def cmd_plot(a):
    path = case_dir(a.name)
    tflag = ["-time", a.time] if a.time else ["-latestTime"]
    log = os.path.join(path, "log.foamToVTK")
    code, _, _, _ = in_case(a.name, ["foamToVTK", "-legacy", "-ascii", "-no-boundary"] + tflag,
                            log=log)
    if code != 0:
        fail("foamToVTK failed", detail=fatal_error(read(log)) or read(log)[-600:])
    vtks = sorted(glob.glob(os.path.join(path, "VTK", "*.vtk")), key=os.path.getmtime)
    if not vtks:
        fail("foamToVTK wrote no .vtk file", log=os.path.relpath(log, ROOT))
    times = [name for _, name in time_dirs(path)]
    converted = re.findall(rf"^Time: ({NUM})", read(log), re.M)
    if a.time and converted and abs(float(converted[-1]) - float(a.time)) > 1e-9 * max(1.0, abs(float(a.time))):
        fail(f"no result at time {a.time}; foamToVTK converted time {converted[-1]}",
             available=times[-8:])
    if a.time and not converted:
        fail(f"no result at time {a.time}", available=times[-8:])
    label = converted[-1] if converted else (a.time or (times[-1] if times else "latest"))
    out = os.path.join(path, a.out or f"plot_{label}.png")
    try:
        from .plot import render
        res = render(vtks[-1], out, a.fields or None, not a.no_streamlines,
                     title=f"{a.name}  t={label}", window=a.window)
    except SystemExit as e:
        fail(str(e))
    except Exception as e:  # noqa: BLE001
        fail(f"plot failed: {type(e).__name__}: {e}",
             hint="plotting needs a 2D (one cell thick) mesh")
    res["image"] = os.path.relpath(out, ROOT)
    emit({"ok": True, "case": a.name, "time": label, **res})


def cmd_view(a):
    path = case_dir(a.name)
    marker = os.path.join(path, "case.foam")
    open(marker, "a").close()
    apps = sorted(glob.glob("/Applications/ParaView*.app"))
    if not apps:
        fail("ParaView is not installed (brew install --cask paraview)",
             marker=os.path.relpath(marker, ROOT))
    subprocess.Popen(["open", "-a", apps[-1], marker])
    emit({"ok": True, "case": a.name, "opened": os.path.relpath(marker, ROOT),
          "app": os.path.basename(apps[-1]),
          "hint": "click Apply, then set the representation to 'Surface With Edges' to see the mesh"})


def cmd_clean(a):
    case_dir(a.name)
    script = ('. "$WM_PROJECT_DIR/bin/tools/CleanFunctions" && cleanCase >/dev/null 2>&1; '
              'rm -rf log.* VTK plot_*.png; ls')
    code, out, _, _ = in_case(a.name, ["bash", "-c", script])
    emit({"ok": code == 0, "case": a.name, "remaining": (out or "").split()})


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(prog="sim", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tutorials", help="list tutorial cases")
    p.add_argument("filter", nargs="*", help="words that must all appear in path/solver")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(fn=cmd_tutorials)

    p = sub.add_parser("new", help="create a case from a tutorial")
    p.add_argument("name")
    p.add_argument("--from", dest="source", required=True, help="tutorial path")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("info", help="describe a case")
    p.add_argument("name")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("get", help="read a dictionary file or entry")
    p.add_argument("name")
    p.add_argument("file", help="e.g. system/controlDict, system/blockMeshDict, 0/U")
    p.add_argument("entry", nargs="?", help="e.g. endTime, SIMPLE/residualControl/p, "
                                           "boundaryField/inlet/value")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("set", help="change one dictionary entry")
    p.add_argument("name")
    p.add_argument("file")
    p.add_argument("entry")
    p.add_argument("value", help='e.g. 500, "uniform (10 0 0)", "(40 40 1)"')
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("run", help="run a command in the case directory")
    p.add_argument("name")
    p.add_argument("--np", type=int, help="run in parallel on N cores (after decomposePar)")
    p.add_argument("--timeout", type=float, help="kill after this many seconds")
    p.add_argument("--log", help="log file name inside the case (default log.<command>)")
    p.add_argument("--sh", action="store_true", help="treat the command as a shell line")
    p.add_argument("--tail", type=int, default=12, help="log lines to return")
    p.add_argument("cmd", nargs="*", help="command after `--`")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("mesh", help="checkMesh quality report")
    p.add_argument("name")
    p.set_defaults(fn=cmd_mesh)

    p = sub.add_parser("status", help="parse solver log")
    p.add_argument("name")
    p.add_argument("--log", help="log file inside the case (default: the solver's)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plot", help="render a 2D result to PNG")
    p.add_argument("name")
    p.add_argument("--fields", nargs="*", help="default: U plus one scalar")
    p.add_argument("--time", help="time to plot (default latest)")
    p.add_argument("--out", help="file name inside the case")
    p.add_argument("--no-streamlines", action="store_true")
    p.add_argument("--window", nargs=4, type=float, metavar=("AMIN", "AMAX", "BMIN", "BMAX"),
                   help="zoom: only plot cells inside this box (in-plane coordinates)")
    # allow negative numbers in scientific notation (e.g. -1e-3) as values
    p._negative_number_matcher = re.compile(r"^-(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")
    p.set_defaults(fn=cmd_plot)

    p = sub.add_parser("view", help="open the case in ParaView (interactive, on the Mac)")
    p.add_argument("name")
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("clean", help="remove mesh, results, logs")
    p.add_argument("name")
    p.set_defaults(fn=cmd_clean)

    # Everything after the first `--` is the command for `run`, verbatim
    argv, rest = sys.argv[1:], []
    if "--" in argv:
        k = argv.index("--")
        argv, rest = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    if rest:
        if a.command != "run":
            ap.error("`--` is only used with `sim run`")
        a.cmd = rest
    a.fn(a)


if __name__ == "__main__":
    main()
