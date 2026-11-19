"""The simpleFoam run of a case: started in Docker, followed through its log, extended,
stopped or killed, and its force and residual history read back for the page."""
import glob
import os
import re
import subprocess
import threading
import time

from foamtools import docker
from foamtools.common import NUM, ROOT, read_json, read_text, round_sig, write_json

from .. import IMAGE
from . import case as ac


def leftover_solver_containers(*case_ids):
    """Names of running solver containers (st-<case id>-<n>) of these cases: ones that no
    SolverRun of this process owns when the server that started them was killed. Empty when
    Docker is not reachable; containers of other cases are never listed."""
    if not case_ids:
        return []
    listing = docker.cli("ps", "--format", "{{.Names}}", timeout=10)
    if listing.returncode != 0:
        return []
    pattern = "|".join(re.escape(f"st-{cid}-") for cid in case_ids)
    return [n for n in listing.stdout.split() if re.fullmatch(rf"(?:{pattern})\d+", n)]


def end_time(case_dir):
    m = re.search(r"^endTime\s+(\d+);", read_text(os.path.join(case_dir, "system", "controlDict")), re.M)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------- solver runner --

SOLVERS = {}                      # case_id -> SolverRun (latest run of that case)
SOLVERS_LOCK = threading.Lock()
# 'sigFpe' is case-sensitive on purpose: the log banner contains FOAM_SIGFPE and trapFpe.
_FATAL = re.compile(r"FOAM FATAL|sigFpe|sigSegv|(?<![A-Za-z_])-?nan(?![A-Za-z_])")
_RESIDUAL = re.compile(rf"Solving for (\S+), Initial residual = ({NUM})")
# A run can blow up without a floating-point exception and then carry on with
# nonsense. Converging runs stay below |Cl| 1 and |Cd| 0.1 from iteration 50 on.
DIVERGED_FROM_ITERATION = 50
DIVERGED_CL, DIVERGED_CD = 10.0, 5.0


def _last_force_row(case_dir):
    """Latest Cl/Cd/Cm line of the force-coefficient output without reading whole files."""
    files = glob.glob(os.path.join(case_dir, "postProcessing", "forceCoeffs", "*", "coefficient*.dat"))
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode(errors="replace")
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2048))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    cols = None
    for line in head.splitlines():
        if line.startswith("#") and "Time" in line and "Cd" in line:
            cols = line.lstrip("#").split()
    lines = [l for l in tail.splitlines() if l and not l.startswith("#")]
    if not cols or not lines:
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) >= len(cols):
            try:
                return {"iter": float(parts[0]), "cl": float(parts[cols.index("Cl")]),
                        "cd": float(parts[cols.index("Cd")]), "cm": float(parts[cols.index("CmPitch")])}
            except (ValueError, IndexError):
                continue
    return None


class SolverRun:
    """One simpleFoam process in a named container, with a log-tailing monitor."""

    def __init__(self, case_id, case_dir, emit, total, start_iteration, seconds_before=0.0):
        self.case_id = case_id
        self.case_dir = case_dir
        self.emit = emit                      # callable(type, data)
        self.total = total
        self.start_iteration = start_iteration
        self.iteration = start_iteration
        self.residuals = {}
        self.state = "running"
        self.fatal = None
        self.converged = False                # residualControl satisfied
        self.stop_requested = False
        self.seconds_before = seconds_before  # solver time of earlier runs of this case
        self.started = time.time()
        self.ended = None
        self.container = f"st-{case_id}-{int(time.time() * 1000) % 10000000}"
        self.log_path = os.path.join(case_dir, "log.simpleFoam")
        self._offset = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        self._partial = ""
        self._block = None
        self._last_emitted = None
        self._wake = threading.Event()
        self.proc = None
        self.thread = None

    @property
    def seconds(self):
        return round((self.ended or time.time()) - self.started, 1)

    @property
    def seconds_total(self):
        return round(self.seconds_before + self.seconds, 1)

    def start(self):
        """Start simpleFoam in its container; raises docker.DockerError when Docker is down."""
        rel = os.path.relpath(self.case_dir, ROOT)
        log = open(self.log_path, "ab")
        try:
            self.proc = docker.popen(["simpleFoam"], cwd=rel, name=self.container, image_name=IMAGE,
                                     stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()                       # the child keeps its own handle
        self.thread = threading.Thread(target=self._monitor, name=f"solver-{self.case_id}", daemon=True)
        self.thread.start()

    def kill(self):
        """Kill the container (retrying while it is still being created)."""
        for _ in range(6):
            if self.proc.poll() is not None:
                return
            if docker.cli("kill", self.container).returncode == 0:
                break
            time.sleep(0.5)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def stop(self, graceful=False, grace_s=20.0):
        """Stop the run. Graceful asks the solver to write the current state and
        end (stopAt writeNow, edited inside the container); otherwise kill."""
        if self.state != "running":
            return
        self.stop_requested = True
        if graceful:
            self._edit_control_dict(r"s/^(stopAt\s+)\w+;/\1writeNow;/")
            deadline = time.time() + grace_s
            while self.proc.poll() is None and time.time() < deadline:
                time.sleep(0.25)
        if self.proc.poll() is None:
            self.kill()
        self._wake.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=15)

    def _edit_control_dict(self, sed_expression):
        """Apply a sed expression to system/controlDict inside the running container."""
        rel = os.path.relpath(self.case_dir, ROOT)
        return docker.cli("exec", self.container, "sed", "-i", "-E", sed_expression,
                          f"/work/{rel}/system/controlDict").returncode == 0

    def set_total_live(self, total):
        """Change endTime of the running solver from inside the container."""
        # sed back-references are one digit, so "\1" followed by the number is unambiguous
        ok = self._edit_control_dict("s/^(endTime\\s+)[0-9]+;/\\1" + f"{int(total)};/")
        if ok:
            self.total = int(total)
        return ok

    def _take_block(self):
        """Adopt the iteration block read so far, once it holds a residual."""
        if self._block and len(self._block) > 1:
            self.iteration = self._block["iter"]
            self.residuals = {k: v for k, v in self._block.items() if k != "iter"}

    def _consume(self):
        """Parse what the log gained since the last call. Returns a fatal line or None."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return None
        self._offset += len(chunk)
        text = self._partial + chunk.decode(errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop()           # keep an unfinished last line for next time
        fatal = None
        for i, line in enumerate(lines):
            if line.startswith("Time = "):
                self._take_block()
                try:
                    self._block = {"iter": int(float(line.split("=")[1].strip().rstrip("s")))}
                except ValueError:
                    self._block = None
                continue
            if line.startswith("SIMPLE solution converged"):
                self.converged = True
            if line.startswith("ExecutionTime"):
                self._take_block()            # the block is complete once ExecutionTime is printed
            if fatal is None and _FATAL.search(line):
                detail = " ".join(l.strip() for l in lines[i:i + 8] if l.strip())[:400]
                at = self._block["iter"] if self._block else self.iteration
                if "FOAM FATAL" in line:
                    fatal = f"OpenFOAM fatal error at iteration {at}: {detail}"
                elif "sig" in line:
                    fatal = f"floating-point exception at iteration {at}: the solution diverged ({detail[:200]})"
                else:
                    fatal = f"NaN in the solution at iteration {at}: the solution diverged ({detail[:200]})"
                continue
            if self._block is not None:
                m = _RESIDUAL.search(line)
                if m and m.group(1) not in self._block:
                    self._block[m.group(1)] = float(m.group(2))
        return fatal

    def _progress(self, force=False):
        if not force and self.iteration == self._last_emitted:
            return
        self._last_emitted = self.iteration
        row = _last_force_row(self.case_dir) or {}
        self.emit("solver.progress", {
            "iteration": self.iteration, "total": self.total,
            "residuals": {k: round_sig(v, 4) for k, v in self.residuals.items()},
            "cl": round_sig(row.get("cl"), 6), "cd": round_sig(row.get("cd"), 6), "seconds": self.seconds})

    def _monitor(self):
        while True:
            exited = self.proc.poll() is not None
            fatal = self._consume()
            if not fatal and not self.fatal:
                row = _last_force_row(self.case_dir)
                if (row and row["iter"] >= DIVERGED_FROM_ITERATION and row["iter"] > self.start_iteration
                        and (abs(row["cl"]) > DIVERGED_CL or abs(row["cd"]) > DIVERGED_CD)):
                    fatal = (f"the solution diverged: Cl {row['cl']:.3g} and Cd {row['cd']:.3g} at iteration "
                             f"{int(row['iter'])} are not physical (no floating-point exception was raised)")
            if fatal and not self.fatal:
                self.fatal = fatal
                if not exited:
                    # A solver that hit a floating-point exception can hang instead of exiting.
                    self.kill()
                    exited = True
                    self._consume()
            if exited:
                break
            self._progress()
            self._wake.wait(2.0)
            self._wake.clear()
        self.ended = time.time()
        code = self.proc.returncode
        tail = read_text(self.log_path)[-3000:]
        ended_cleanly = code == 0 and re.search(r"^End\s*$", tail, re.M) is not None
        if self.fatal:
            state = "failed"
        elif self.stop_requested:
            state = "stopped"
        elif ended_cleanly:
            state = "finished"
        else:
            state = "failed"
            last = [l for l in tail.splitlines() if l.strip()][-6:]
            self.fatal = f"solver exited with code {code}: " + " | ".join(last)[:600]
        write_json(os.path.join(self.case_dir, "solver_state.json"), {
            "state": state, "converged": self.converged, "fatal": self.fatal,
            "iteration": self.iteration, "total": self.total, "ended_cleanly": ended_cleanly,
            "seconds": self.seconds, "seconds_total": self.seconds_total})
        self.state = state
        self._progress(force=True)


def solver_running(case_id):
    run = SOLVERS.get(case_id)
    return run is not None and run.state == "running"


def kill_solver(case_id):
    """Hard stop for a case (human Stop, rerun, server shutdown)."""
    run = SOLVERS.get(case_id)
    if run is not None and run.state == "running":
        run.stop(graceful=False)
        return True
    return False


def kill_all_solvers():
    for case_id in list(SOLVERS):
        try:
            kill_solver(case_id)
        except Exception:  # noqa: BLE001
            pass


# -------------------------------------------------------- chart data (server) --

def case_series(case_dir, velocity):
    """Everything the charts of one case need."""
    def surfaces(data):
        return {"upper": data["upper"], "lower": data["lower"]} if data and data.get("upper") else None

    hist, _ = ac.force_history(case_dir)
    res = ac.residual_history(case_dir)
    cp = read_json(os.path.join(case_dir, "cp.json"))
    if not cp and os.path.isdir(os.path.join(case_dir, "postProcessing", "surfacePressure")):
        try:
            cp = ac.surface_cp(case_dir, velocity) or None
        except Exception:  # noqa: BLE001
            cp = None
    return {
        "residuals": {"iterations": res.get("iterations", 0), "series": res.get("series", [])},
        "forces": hist,
        "cp": surfaces(cp),
        "xfoil_cp": surfaces(read_json(os.path.join(case_dir, "xfoil_cp.json"))),
        "geometry": surfaces(read_json(os.path.join(case_dir, "geometry.json"))),
    }
