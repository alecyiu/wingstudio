"""Start, control and stop the ParaView processes of the studio.

Two kinds of ParaView (pvpython) process are managed:

* one interactive viewer per case (wingstudio/viewer/viewer_server.py): a trame
  page on its own port (8811 upward) that the UI embeds in an iframe, with an
  HTTP control API that set() and state() talk to;
* one warm offscreen render worker (wingstudio/viewer/render_case.py --serve)
  shared by all cases, used by render() for stage snapshots. Jobs are sent to
  it one at a time; it is restarted if it dies.

Every public method returns a dict with "ok" and never raises: when ParaView
is missing, a process dies or a call times out the answer is
{"ok": False, "error": "..."}. Nothing blocks for ever: process start-up, lock
waits, HTTP calls and render jobs all have timeouts. All processes are
terminated by stop() and at interpreter exit; the viewers also exit by
themselves when this process disappears without cleaning up.

    vm = ViewerManager()
    vm.ensure("c1", "cases/st_ab12_4412")          -> {"ok": True, "port": 8811, "url": "http://127.0.0.1:8811/index.html"}
    vm.set("c1", mode="field", field="p", zoom="leading_edge")
    vm.render("cases/st_ab12_4412", "studio_data/mesh.png", mode="mesh", zoom="airfoil")
    vm.stop()

Standard library only. PVPYTHON overrides the path of pvpython.
"""

import atexit
import collections
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time as _time  # render() has a parameter called `time`
import urllib.error
import urllib.request
import uuid
import weakref

from foamtools.common import ROOT as PROJECT_ROOT
from foamtools.common import clean_env

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_VENV = os.path.join(PROJECT_ROOT, ".venv-viewer")
VIEWER_SERVER = os.path.join(HERE, "viewer_server.py")
RENDER_SCRIPT = os.path.join(HERE, "render_case.py")
DEFAULT_PVPYTHON = "/Applications/ParaView-6.2.1.app/Contents/bin/pvpython"

HOST = "127.0.0.1"
BASE_PORT = 8811
SET_KEYS = ("mode", "field", "zoom", "time", "range", "reload", "ui")
RENDER_EXTRA_KEYS = ("range", "component", "preset", "cell_data", "dark", "no_axes", "mesh_wait_s")

_managers = weakref.WeakSet()


def _stop_all_managers():
    for m in list(_managers):
        try:
            m.stop()
        except Exception:
            pass


atexit.register(_stop_all_managers)


def _fail(message):
    return {"ok": False, "error": str(message)}


def _project_path(path):
    """An absolute path; a relative one is taken from the project root."""
    return os.path.abspath(os.path.join(PROJECT_ROOT, os.path.expanduser(str(path))))


class _Process:
    """A pvpython child whose output is drained by a thread.

    Lines that are JSON objects go to `self.messages`; everything is kept in
    `self.tail` (the last 200 lines) for error reports."""

    def __init__(self, argv, stdin=subprocess.DEVNULL):
        self.tail = collections.deque(maxlen=200)
        self.messages = queue.Queue()
        # Own session: the child must not get the terminal's Ctrl-C before we
        # have had the chance to stop it in an orderly way.
        self.proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     cwd=PROJECT_ROOT, start_new_session=True, env=clean_env())
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._drain, name="pv-out-%d" % self.pid, daemon=True)
        self._reader.start()

    def _drain(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.tail.append(line)
                if line.startswith("{"):
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        self.messages.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)         # end of output: the process is gone

    def alive(self):
        return self.proc.poll() is None

    def next_message(self, timeout):
        """Next JSON message, None when the process ended, or raises queue.Empty."""
        return self.messages.get(timeout=max(0.0, timeout))

    def last_lines(self, n=6):
        return " | ".join(list(self.tail)[-n:])

    def terminate(self, grace=3.0):
        """SIGTERM, then SIGKILL; also reaps the process. Safe to call twice."""
        if self.proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3.0)):
                try:
                    os.killpg(self.pid, sig)
                except (ProcessLookupError, PermissionError):
                    try:
                        self.proc.send_signal(sig)
                    except (ProcessLookupError, OSError):
                        pass
                try:
                    self.proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class _Viewer:
    def __init__(self, case_id, case_dir, port, process, ready):
        self.case_id = case_id
        self.case_dir = case_dir
        self.port = port
        self.process = process
        self.ready = ready
        self.url = "http://%s:%d/index.html" % (HOST, port)


class ViewerManager:
    def __init__(self, base_port=BASE_PORT, pvpython=None, start_timeout=45.0, follow_s=3.0,
                 http_timeout=15.0):
        self.base_port = int(base_port)
        self.pvpython = pvpython or os.environ.get("PVPYTHON") or DEFAULT_PVPYTHON
        self.start_timeout = float(start_timeout)
        self.follow_s = float(follow_s)          # how often a viewer re-scans its case directory
        self.http_timeout = float(http_timeout)
        self._lock = threading.RLock()           # guards the dictionaries below
        self._viewers = {}                       # case_id -> _Viewer
        self._case_locks = {}                    # case_id -> lock held while that viewer starts/stops
        self._reserved_ports = set()             # ports handed to a viewer that is still starting
        self._render_lock = threading.Lock()     # one render job at a time
        self._worker = None
        _managers.add(self)

    # ------------------------------------------------------------------ checks
    def _paraview_problem(self, need_venv):
        if not (os.path.isfile(self.pvpython) and os.access(self.pvpython, os.X_OK)):
            return ("ParaView is not available: pvpython not found at %s (install ParaView 6.2 or set "
                    "PVPYTHON)" % self.pvpython)
        for path in (RENDER_SCRIPT, VIEWER_SERVER):
            if not os.path.isfile(path):
                return "viewer script missing: %s" % path
        if need_venv and not os.path.isdir(os.path.join(VIEWER_VENV, "lib")):
            return ("the viewer virtualenv is missing: create it with 'uv venv --python 3.12 %s' and "
                    "'uv pip install --python %s/bin/python -r %s/requirements.txt'"
                    % (VIEWER_VENV, VIEWER_VENV, HERE))
        return None

    def _wait_ready(self, process, what):
        """-> (the process's "ready" message, None), or (None, error) with the process stopped."""
        deadline = _time.time() + self.start_timeout
        while True:
            try:
                msg = process.next_message(deadline - _time.time())
            except queue.Empty:
                process.terminate()
                return None, "%s did not report ready within %.0f s: %s" % (
                    what, self.start_timeout, process.last_lines())
            if msg is None:
                process.terminate()
                return None, "%s exited during start-up (rc=%s): %s" % (
                    what, process.proc.returncode, process.last_lines())
            if msg.get("ready"):
                return msg, None

    # ----------------------------------------------------------- viewer set-up
    def _case_lock(self, case_id):
        with self._lock:
            return self._case_locks.setdefault(case_id, threading.Lock())

    def _reserve_port(self):
        """Lowest free port from base_port upward that no viewer of ours has."""
        with self._lock:
            taken = {v.port for v in self._viewers.values()} | self._reserved_ports
            for port in range(self.base_port, self.base_port + 200):
                if port in taken:
                    continue
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    # Same option the viewer's own server socket uses: without it a
                    # port whose last viewer just stopped looks busy for ~30 s.
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        s.bind((HOST, port))
                    except OSError:
                        continue
                self._reserved_ports.add(port)
                return port
        return None

    def _start_viewer(self, case_id, case_dir, port):
        """-> (_Viewer, None) or (None, error text)."""
        # --no-mpi: otherwise pvpython opens an MPI listening socket on all interfaces
        argv = [self.pvpython, "--no-mpi", "--venv", VIEWER_VENV, "--force-offscreen-rendering", VIEWER_SERVER,
                "--case", case_dir, "--host", HOST, "--port", str(port),
                "--no-ui", "--rendering", "remote",
                "--follow", "%g" % self.follow_s, "--parent-pid", str(os.getpid())]
        try:
            process = _Process(argv)
        except OSError as e:
            return None, "could not start pvpython: %s" % e
        ready, err = self._wait_ready(process, "viewer")
        return (_Viewer(case_id, case_dir, port, process, ready), None) if ready else (None, err)

    def ensure(self, case_id, case_dir):
        """Make sure a viewer process shows case_dir. -> {ok, port, url, pid, started}"""
        try:
            case_id = str(case_id)
            problem = self._paraview_problem(need_venv=True)
            if problem:
                return _fail(problem)
            case_dir = _project_path(case_dir)
            if not os.path.isdir(case_dir):
                return _fail("case directory not found: %s" % case_dir)
            lock = self._case_lock(case_id)
            if not lock.acquire(timeout=3 * self.start_timeout + 20):
                return _fail("timed out waiting for another start of the viewer for case %s" % case_id)
            try:
                with self._lock:
                    viewer = self._viewers.get(case_id)
                if viewer is not None:
                    if viewer.process.alive() and viewer.case_dir == case_dir:
                        return {"ok": True, "port": viewer.port, "url": viewer.url,
                                "pid": viewer.process.pid, "started": False}
                    self._drop_viewer(case_id)       # dead, or showing another directory
                errors = []
                for _attempt in range(3):            # another program can grab the port first
                    port = self._reserve_port()
                    if port is None:
                        errors.append("no free port from %d upward" % self.base_port)
                        break
                    t0 = _time.time()
                    try:
                        viewer, err = self._start_viewer(case_id, case_dir, port)
                        if viewer is not None:
                            with self._lock:
                                self._viewers[case_id] = viewer
                            return {"ok": True, "port": port, "url": viewer.url,
                                    "pid": viewer.process.pid, "started": True,
                                    "startup_s": round(_time.time() - t0, 2),
                                    "case_error": viewer.ready.get("error")}
                    finally:
                        with self._lock:
                            self._reserved_ports.discard(port)
                    errors.append("port %d: %s" % (port, err))
                    if "did not report ready" in err:
                        break                        # not a port problem; do not wait three times
                return _fail("could not start the viewer for case %s: %s" % (case_id, "; ".join(errors)))
            finally:
                lock.release()
        except Exception as e:
            return _fail("%s: %s" % (type(e).__name__, e))

    def _drop_viewer(self, case_id):
        with self._lock:
            viewer = self._viewers.pop(case_id, None)
        if viewer is not None:
            viewer.process.terminate()

    def _live_viewer(self, case_id):
        """-> (_Viewer, None) or (None, error dict)."""
        with self._lock:
            viewer = self._viewers.get(str(case_id))
        if viewer is None:
            return None, _fail("no viewer is running for case %s; call ensure() first" % case_id)
        if not viewer.process.alive():
            return None, _fail("the viewer for case %s has exited (rc=%s): %s; call ensure() to restart it"
                               % (case_id, viewer.process.proc.returncode, viewer.process.last_lines(3)))
        return viewer, None

    # ------------------------------------------------------------- viewer HTTP
    def _http_json(self, viewer, path, payload=None, timeout=None):
        url = "http://%s:%d%s" % (HOST, viewer.port, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.http_timeout) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:          # 400 from /api/set carries a JSON error
            try:
                out = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                out = None
            if not isinstance(out, dict):
                out = _fail("viewer answered HTTP %s" % e.code)
        except Exception as e:                       # refused, timeout, bad JSON ...
            return _fail("viewer on port %d did not answer: %s: %s" % (viewer.port, type(e).__name__, e))
        if not isinstance(out, dict):
            return _fail("viewer on port %d sent an unexpected answer" % viewer.port)
        if not out.get("ok", True) and not out.get("error"):
            out["error"] = "viewer reported a problem"
        out.setdefault("ok", out.get("error") is None)
        return out

    def set(self, case_id, **state):
        """Change what the viewer of a case shows. Keys: mode (mesh | field |
        patches), field, zoom (auto | full | airfoil | leading_edge |
        trailing_edge | wake | [h0, h1, v0, v1]), time ("latest" or a value),
        range ("auto" or [lo, hi]), reload (default True: re-scan the case
        directory first, so "latest" really is the latest). Keys given as None
        are left unchanged. Returns the viewer's new state with "ok"."""
        try:
            viewer, err = self._live_viewer(case_id)
            if err:
                return err
            payload = {k: v for k, v in state.items() if v is not None}
            unknown = [k for k in payload if k not in SET_KEYS]
            if unknown:
                return _fail("unknown viewer settings: %s (allowed: %s)" % (unknown, ", ".join(SET_KEYS)))
            payload["reload"] = 1 if payload.get("reload", True) else 0
            for key in ("zoom", "range"):
                if isinstance(payload.get(key), (list, tuple)):
                    payload[key] = ",".join(repr(float(x)) for x in payload[key])
            return self._http_json(viewer, "/api/set", payload)
        except Exception as e:
            return _fail("%s: %s" % (type(e).__name__, e))

    def state(self, case_id):
        """Current state of the viewer of a case (mode, field, time, times, fields, patches, ...)."""
        try:
            viewer, err = self._live_viewer(case_id)
            if err:
                return err
            out = self._http_json(viewer, "/api/state")
            out.setdefault("url", viewer.url)
            return out
        except Exception as e:
            return _fail("%s: %s" % (type(e).__name__, e))

    # --------------------------------------------------------- offscreen render
    def _get_worker(self):
        """The warm render worker, started if needed. -> (_Process, None) or (None, error)."""
        worker = self._worker
        if worker is not None and worker.alive():
            return worker, None
        if worker is not None:
            worker.terminate()
            self._worker = None
        argv = [self.pvpython, "--no-mpi", "--force-offscreen-rendering", RENDER_SCRIPT, "--serve"]
        try:
            worker = _Process(argv, stdin=subprocess.PIPE)
        except OSError as e:
            return None, "could not start pvpython: %s" % e
        ready, err = self._wait_ready(worker, "render worker")
        if not ready:
            return None, err
        self._worker = worker
        return worker, None

    def render(self, case_dir, out_png, mode="mesh", field=None, zoom="auto", time="latest",
               size=(1100, 700), timeout=60.0, **extra):
        """Offscreen PNG of a case; no viewer process is needed.

        mode   mesh | field | patches       field  U, p, nut, ... (field mode; default U)
        zoom   auto | full | airfoil | leading_edge | trailing_edge | wake | [h0, h1, v0, v1]
               (auto = airfoil for mesh and field, full for patches)
        time   "latest" (newest complete time directory) or a value
        extra  range=[lo, hi], component="X|Y|Z", preset=<colour map>, cell_data, dark, no_axes

        Returns the renderer's answer: {ok, path, seconds, mode, time, times, fields, zoom, box,
        range, patches, warnings, ...}. "mode" is what was actually drawn: a field that cannot be
        read yet is drawn as the mesh with a line in "warnings"."""
        t0 = _time.time()
        try:
            problem = self._paraview_problem(need_venv=False)
            if problem:
                return _fail(problem)
            unknown = [k for k in extra if k not in RENDER_EXTRA_KEYS]
            if unknown:
                return _fail("unknown render options: %s" % unknown)
            job = {"id": uuid.uuid4().hex, "case": _project_path(case_dir), "out": _project_path(out_png),
                   "mode": mode, "field": field, "zoom": list(zoom) if isinstance(zoom, tuple) else zoom,
                   "time": time, "size": [int(size[0]), int(size[1])]}
            job.update({k: v for k, v in extra.items() if v is not None})
            line = (json.dumps(job) + "\n").encode()
            deadline = t0 + float(timeout)
            if not self._render_lock.acquire(timeout=float(timeout)):
                return _fail("render queue is busy: no free slot within %.0f s" % float(timeout))
            try:
                last_error = "render failed"
                for attempt in (1, 2):               # one retry when the worker dies under the job
                    worker, err = self._get_worker()
                    if worker is None:
                        return _fail(err)
                    try:
                        worker.proc.stdin.write(line)
                        worker.proc.stdin.flush()
                    except (OSError, ValueError) as e:
                        last_error = "render worker is gone (%s)" % e
                        worker.terminate()
                        self._worker = None
                        continue
                    answer, died = None, False
                    while answer is None:
                        try:
                            msg = worker.next_message(deadline - _time.time())
                        except queue.Empty:
                            # ParaView is stuck on this job: do not leave it running
                            worker.terminate()
                            self._worker = None
                            return _fail("render timed out after %.0f s" % float(timeout))
                        if msg is None:
                            died = True
                            break
                        if msg.get("id") == job["id"]:
                            answer = msg
                    if died:
                        last_error = "render worker died during the job (rc=%s): %s" % (
                            worker.proc.poll(), worker.last_lines(4))
                        worker.terminate()
                        self._worker = None
                        if _time.time() > deadline - 5.0:
                            break
                        continue
                    answer.pop("id", None)
                    answer["path"] = answer.get("out", job["out"])
                    answer["render_seconds"] = answer.get("seconds")
                    answer["seconds"] = round(_time.time() - t0, 3)
                    answer["worker_restarted"] = attempt > 1
                    if not answer.get("ok") and not answer.get("error"):
                        answer["error"] = "render failed"
                    return answer
                return _fail(last_error)
            finally:
                self._render_lock.release()
        except Exception as e:
            return _fail("%s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------------- admin
    def stop(self, case_id=None):
        """Stop the viewer of one case, or (no argument) every viewer and the
        render worker. Also runs at interpreter exit."""
        try:
            if case_id is not None:
                lock = self._case_lock(str(case_id))
                got = lock.acquire(timeout=self.start_timeout + 10)
                try:
                    self._drop_viewer(str(case_id))
                finally:
                    if got:
                        lock.release()
                return
            with self._lock:
                ids = list(self._viewers)
            for cid in ids:
                self._drop_viewer(cid)
            got = self._render_lock.acquire(timeout=10)
            try:
                worker, self._worker = self._worker, None
                if worker is not None:
                    try:
                        worker.proc.stdin.close()   # end of input makes the worker exit by itself
                        worker.proc.wait(timeout=2)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                    worker.terminate()
            finally:
                if got:
                    self._render_lock.release()
        except Exception:
            pass

