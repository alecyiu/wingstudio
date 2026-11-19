"""Persistence for Wing Studio.

Projects, studies and cases are JSON files under studio_data/ (atomic writes,
one lock for the whole store). Every study has an append-only events.jsonl;
events are written to the file before they are handed to subscribers, so a run
can be reconstructed from the file alone. Finished cases get one row in the
SQLite table `runs` (studio_data/metrics.db) that feeds the dashboard.

Standard library only.
"""
import copy
import json
import os
import secrets
import sqlite3
import statistics
import threading
import time

from foamtools.common import ROOT, read_json, write_json

DEFAULT_DATA_DIR = os.path.join(ROOT, "studio_data")
DEFAULT_PROJECT = {"id": "naca-wing", "name": "NACA wing",
                   "description": "Section selection for a wing: NACA 4-digit candidates "
                                  "screened with XFoil and checked with OpenFOAM RANS."}

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# The columns of the metrics table, in order: (name, SQLite type).
RUN_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"), ("ts", "TEXT"),
    ("project_id", "TEXT"), ("project_name", "TEXT"),
    ("study_id", "TEXT"), ("study_title", "TEXT"), ("case_id", "TEXT"),
    ("model", "TEXT"), ("mode", "TEXT"),
    ("naca", "TEXT"), ("alpha_deg", "REAL"), ("mach", "REAL"), ("reynolds", "REAL"),
    ("density", "REAL"), ("design_cl", "REAL"), ("objective", "TEXT"),
    ("cells", "INTEGER"), ("yplus_avg", "REAL"), ("iterations", "INTEGER"),
    ("solver_seconds", "REAL"), ("wall_seconds", "REAL"),
    ("solver_converged", "INTEGER"), ("forces_converged", "INTEGER"), ("residual_max", "REAL"),
    ("cl", "REAL"), ("cd", "REAL"), ("cm", "REAL"), ("ld", "REAL"),
    ("xfoil_cl", "REAL"), ("xfoil_cd", "REAL"), ("xfoil_cm", "REAL"), ("xfoil_ld", "REAL"),
    ("cl_err_pct", "REAL"), ("cd_err_pct", "REAL"), ("cm_err_abs", "REAL"),
    ("verdict", "TEXT"), ("success", "INTEGER"),
    ("llm_calls", "INTEGER"), ("tool_calls", "INTEGER"),
    ("tokens_in", "INTEGER"), ("tokens_out", "INTEGER"), ("tokens_cached", "INTEGER"),
    ("cost_usd", "REAL"),
    ("rework_loops", "INTEGER"), ("gate_failures", "INTEGER"),
    ("human_interventions", "INTEGER"), ("first_pass", "INTEGER"),
    ("stage_seconds", "TEXT"), ("stage_attempts", "TEXT"), ("failed_stage", "TEXT"),
    # NULL in rows written before these two columns existed: is_trial and pass_k allow for that
    ("status", "TEXT"), ("help_interventions", "INTEGER"),
]
_JSON_COLUMNS = ("stage_seconds", "stage_attempts")
_BOOL_COLUMNS = ("solver_converged", "forces_converged", "success", "first_pass")


# The kinds of human.intervention that help a case towards its result. Every kind
# Engine.intervention is called with is in one of the two lists:
#   help      steer           a message to the case (its stage agent, or the steering agent for it)
#             rerun           a stage re-run the engineer asked for (also the one a changed angle causes)
#             plan_edit       a plan edit while the cases run
#             retry_approval  the approval of a retry after a stage failed (its gate three times, or otherwise)
#   not help  gate_decision   the approval to run a stage in gate mode
#             pause, resume, stop, mode_change
# `unaided` (pass^k) counts the first list only; human_interventions and first_pass count both.
HELP_KINDS = ("steer", "rerun", "plan_edit", "retry_approval")
OTHER_KINDS = ("gate_decision", "pause", "resume", "stop", "mode_change")
RUN_STATUSES = ("done", "failed", "stopped")
# What a row says about the outcome of its case, as opposed to what the case cost. A case that
# ends as `stopped` after it had reached an outcome keeps these from its earlier row
# (Engine.write_metrics): a stop during a re-run or a retry does not replace an outcome.
# alpha_deg is here because the kept coefficients belong to the angle they were computed at.
OUTCOME_COLUMNS = (
    "status", "success", "verdict", "failed_stage", "alpha_deg",
    "cells", "yplus_avg", "iterations", "solver_converged", "forces_converged", "residual_max",
    "cl", "cd", "cm", "ld", "xfoil_cl", "xfoil_cd", "xfoil_cm", "xfoil_ld",
    "cl_err_pct", "cd_err_pct", "cm_err_abs",
)
# pass^k is computed under each of these, and they are nested
# (first_pass implies unaided implies success).
PASS_CRITERIA = ("success", "unaided", "first_pass")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def new_id(n=6):
    """Short lowercase id that starts with a letter (safe in file and case names)."""
    return secrets.choice(_ALPHABET[:26]) + "".join(secrets.choice(_ALPHABET) for _ in range(n - 1))


def _median(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _mean(values):
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _rate(rows, key):
    return (sum(1 for r in rows if r.get(key)) / len(rows)) if rows else None


def intervention_kind(data):
    """The kind of a human.intervention event as HELP_KINDS names it. The event writes the
    approval of a retry as a gate_decision with `retry` true (its readers know that kind)."""
    data = data if isinstance(data, dict) else {}
    return "retry_approval" if data.get("kind") == "gate_decision" and data.get("retry") is True else data.get("kind")


def run_status(ended, failed_stage=None):
    """The `status` of a run row: how the case ended, as far as the model is answerable for it.

    done     the pipeline finished (the verdict may still be fail)
    failed   a stage stands failed: the provisional row written when a stage fails, and the row
             of a case that the engineer or a restart then closed without a retry
    stopped  everything else: stopped by the engineer, ended by a server restart or by a crash
             of the pipeline. No outcome of the case's own, so not a trial for pass^k.

    This is the end taken alone. The row of a case that had reached an outcome before keeps
    that outcome when the case then ends as stopped (OUTCOME_COLUMNS)."""
    if ended == "done":
        return "done"
    return "failed" if failed_stage else "stopped"


def is_trial(row):
    """Whether a run row counts as a trial for pass^k: the case reached an outcome of its own.
    A row from before the `status` column is judged by what it does hold."""
    status = row.get("status")
    if status:
        return status in ("done", "failed")
    return bool(row.get("success") or row.get("failed_stage") or row.get("verdict") == "fail")


def _help_count(row):
    """Engineer actions that helped the case. Before the column existed only the count of
    every action is known, so that is used: it can only be too strict."""
    count = row.get("help_interventions")
    return (row.get("human_interventions") or 0) if count is None else count


def _passes(row, criterion):
    if criterion == "unaided":
        return bool(row.get("success")) and not _help_count(row) > 0
    return bool(row.get(criterion))


def pass_k_values(n, c):
    """[pass^1 .. pass^n] for c passes in n trials: values[k-1] = C(c, k) / C(n, k), the chance
    that k runs drawn from the n all pass (Yao et al. 2024, arXiv:2406.12045, section 3).

    A running product in a fixed order, so the page (AS.passKValues) gets the same doubles.
    Without the guard the factor turns negative once the product is zero and gives -0.0.
    c is held to 0..n (more passes than trials would give a chance above 1); n < 0 gives []."""
    c = max(0, min(c, n))
    v, values = 1.0, []
    for i in range(n):
        v = (v * (c - i)) / (n - i) if i < c else 0.0
        values.append(v)
    return values


def pass_k(rows):
    """{left_out, success | unaided | first_pass: {n, c, values}} for the rows of one model,
    of any geometry. n is the number of trials (the same for the three criteria); rows that are
    no trial are counted in left_out and nowhere else."""
    trials = [r for r in rows if is_trial(r)]
    out = {"left_out": len(rows) - len(trials)}
    for criterion in PASS_CRITERIA:
        c = sum(1 for r in trials if _passes(r, criterion))
        out[criterion] = {"n": len(trials), "c": c, "values": pass_k_values(len(trials), c)}
    return out


class Store:
    def __init__(self, data_dir=None):
        self.data_dir = os.path.abspath(data_dir or os.environ.get("STUDIO_DATA_DIR") or DEFAULT_DATA_DIR)
        self.studies_dir = os.path.join(self.data_dir, "studies")
        os.makedirs(self.studies_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._projects = {}
        self._studies = {}
        self._cases = {}            # study_id -> {case_id: case}
        self._events = {}           # study_id -> list of events (client view)
        self._subs = {}             # study_id -> set of callbacks
        self._db_lock = threading.Lock()
        self._db_path = os.path.join(self.data_dir, "metrics.db")
        self._load()
        self._init_db()

    # ------------------------------------------------------------- loading --

    def _load(self):
        data = read_json(os.path.join(self.data_dir, "projects.json"), {}) or {}
        for p in data.get("projects", []):
            self._projects[p["id"]] = p
        if not self._projects:
            self._projects[DEFAULT_PROJECT["id"]] = dict(DEFAULT_PROJECT, created_at=now_iso())
            self._save_projects()
        for sid in sorted(os.listdir(self.studies_dir)):
            sdir = os.path.join(self.studies_dir, sid)
            study = read_json(os.path.join(sdir, "study.json"))
            if not study:
                continue
            self._studies[sid] = study
            self._cases[sid] = {}
            cdir = os.path.join(sdir, "cases")
            if os.path.isdir(cdir):
                for name in sorted(os.listdir(cdir)):
                    if name.endswith(".json"):
                        case = read_json(os.path.join(cdir, name))
                        if case:
                            self._cases[sid][case["id"]] = case

    def _save_projects(self):
        write_json(os.path.join(self.data_dir, "projects.json"), {"projects": list(self._projects.values())})

    def _study_dir(self, sid):
        return os.path.join(self.studies_dir, sid)

    # ------------------------------------------------------------ projects --

    @property
    def default_project_id(self):
        with self._lock:
            return DEFAULT_PROJECT["id"] if DEFAULT_PROJECT["id"] in self._projects \
                else next(iter(self._projects))

    def list_projects(self):
        with self._lock:
            out = []
            for p in self._projects.values():
                studies = [s for s in self._studies.values() if s.get("project_id") == p["id"]]
                last = max((s.get("updated_at") or s.get("created_at") or "" for s in studies), default=None)
                out.append(dict(copy.deepcopy(p), n_studies=len(studies), last_run=last))
            return out

    def get_project(self, pid):
        with self._lock:
            return copy.deepcopy(self._projects.get(pid))

    def create_project(self, name, description=""):
        with self._lock:
            pid = new_id()
            while pid in self._projects:
                pid = new_id()
            project = {"id": pid, "name": name, "description": description or "",
                       "created_at": now_iso()}
            self._projects[pid] = project
            self._save_projects()
            return copy.deepcopy(project)

    # ------------------------------------------------------------- studies --

    def new_study_id(self):
        """A new study id. STUDIO_ID_PREFIX (letters, digits, underscore) is put in
        front of it, so a test server started with STUDIO_ID_PREFIX=test_ only
        ever creates case directories named cases/st_test_*."""
        prefix = "".join(ch for ch in os.environ.get("STUDIO_ID_PREFIX", "").lower()
                         if ch in _ALPHABET or ch == "_")
        with self._lock:
            sid = prefix + new_id()
            while sid in self._studies or os.path.exists(self._study_dir(sid)):
                sid = prefix + new_id()
            return sid

    def create_study(self, study):
        with self._lock:
            sid = study["id"]
            os.makedirs(os.path.join(self._study_dir(sid), "cases"), exist_ok=True)
            self._studies[sid] = copy.deepcopy(study)
            self._cases[sid] = {}
            self._events[sid] = []
            write_json(os.path.join(self._study_dir(sid), "study.json"), self._studies[sid])
            return copy.deepcopy(study)

    def get_study(self, sid):
        with self._lock:
            return copy.deepcopy(self._studies.get(sid))

    def update_study(self, sid, mutate):
        """Apply `mutate(study)` under the lock, persist, and return a copy."""
        with self._lock:
            study = self._studies[sid]
            mutate(study)
            study["updated_at"] = now_iso()
            write_json(os.path.join(self._study_dir(sid), "study.json"), study)
            return copy.deepcopy(study)

    def list_studies(self, project_id=None):
        with self._lock:
            out = []
            for s in self._studies.values():
                if project_id and s.get("project_id") != project_id:
                    continue
                cases = self._cases.get(s["id"], {})
                out.append({
                    "id": s["id"], "project_id": s.get("project_id"), "title": s.get("title"),
                    "created_at": s.get("created_at"), "updated_at": s.get("updated_at"),
                    "model": s.get("model"), "mode": s.get("mode"), "status": s.get("status"),
                    "requirements": copy.deepcopy(s.get("requirements")),
                    "nacas": [c.get("naca") for c in cases.values()] or
                             [c["naca"] for c in s.get("candidates", []) if c.get("selected")],
                    "verdicts": {c.get("naca"): c.get("verdict") for c in cases.values()},
                    "recommendation": (s.get("report") or {}).get("recommendation"),
                    "usage": copy.deepcopy(s.get("usage")),
                })
            out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
            return out

    # --------------------------------------------------------------- cases --

    def create_case(self, case):
        with self._lock:
            sid = case["study_id"]
            self._cases.setdefault(sid, {})[case["id"]] = copy.deepcopy(case)
            self._save_case(sid, case["id"])
            return copy.deepcopy(case)

    def _save_case(self, sid, cid):
        cdir = os.path.join(self._study_dir(sid), "cases")
        os.makedirs(cdir, exist_ok=True)
        write_json(os.path.join(cdir, f"{cid}.json"), self._cases[sid][cid])

    def get_case(self, sid, cid):
        with self._lock:
            return copy.deepcopy(self._cases.get(sid, {}).get(cid))

    def list_cases(self, sid):
        with self._lock:
            study = self._studies.get(sid) or {}
            cases = self._cases.get(sid, {})
            order = [c for c in study.get("case_ids", []) if c in cases]
            order += [c for c in cases if c not in order]
            return [copy.deepcopy(cases[c]) for c in order]

    def update_case(self, sid, cid, mutate):
        with self._lock:
            case = self._cases[sid][cid]
            mutate(case)
            self._save_case(sid, cid)
            return copy.deepcopy(case)

    # -------------------------------------------------------------- events --

    def _events_path(self, sid):
        return os.path.join(self._study_dir(sid), "events.jsonl")

    def _load_events(self, sid):
        """Read events.jsonl into memory (client view: the `full` payload is
        kept only in the file)."""
        if sid in self._events:
            return self._events[sid]
        events = []
        try:
            with open(self._events_path(sid)) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue      # a torn last line after a crash
                    ev.pop("full", None)
                    events.append(ev)
        except OSError:
            pass
        self._events[sid] = events
        return events

    def emit(self, study_id, type, data=None, case_id=None, stage=None, agent=None,
             span_id=None, parent_span_id=None, full=None):
        """Append one event to the study's events.jsonl, then publish it.

        `full` (optional) is extra detail, such as a complete tool result,
        that is stored in the JSONL line but not sent to clients."""
        with self._lock:
            if study_id not in self._studies:
                raise KeyError(f"no such study: {study_id}")
            events = self._load_events(study_id)
            event = {
                "seq": (events[-1]["seq"] + 1) if events else 1,
                "ts": now_iso(), "study_id": study_id, "case_id": case_id,
                "stage": stage, "agent": agent,
                "span_id": span_id or secrets.token_hex(4),
                "parent_span_id": parent_span_id,
                "type": type, "data": data if data is not None else {},
            }
            line = dict(event, full=full) if full is not None else event
            with open(self._events_path(study_id), "a") as f:
                f.write(json.dumps(line, default=str) + "\n")
                f.flush()
            # Normalise through JSON so subscribers get exactly what a replay gives.
            event = json.loads(json.dumps(event, default=str))
            events.append(event)
            subscribers = list(self._subs.get(study_id, ()))
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a broken client must not stop the run
                pass
        return event

    def events_after(self, study_id, after=0):
        with self._lock:
            if study_id not in self._studies:
                return []
            events = self._load_events(study_id)
            # seq is dense and starts at 1, so the slice is direct.
            return events[max(0, int(after)):]

    def last_seq(self, study_id):
        with self._lock:
            events = self._load_events(study_id) if study_id in self._studies else []
            return events[-1]["seq"] if events else 0

    def snapshot(self, study_id):
        """Study, cases and the last event sequence number, read under one lock:
        a client that opens this snapshot treats events up to `last_seq` as
        already included and applies only later ones."""
        with self._lock:
            if study_id not in self._studies:
                return None
            return {"study": self.get_study(study_id), "cases": self.list_cases(study_id),
                    "last_seq": self.last_seq(study_id)}

    def subscribe(self, study_id, callback):
        """Register `callback(event)` for live events. Returns an unsubscribe function.
        To replay without a gap: subscribe first, then read events_after(), and
        drop live events whose seq was already replayed."""
        with self._lock:
            self._subs.setdefault(study_id, set()).add(callback)

        def unsubscribe():
            with self._lock:
                self._subs.get(study_id, set()).discard(callback)
        return unsubscribe

    # ------------------------------------------------------------- metrics --

    def _db(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._db_lock, self._db() as conn:
            cols = ", ".join(f"{name} {kind}" for name, kind in RUN_COLUMNS)
            conn.execute(f"CREATE TABLE IF NOT EXISTS runs ({cols})")
            have = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            for name, kind in RUN_COLUMNS:          # columns an older metrics.db lacks
                if name not in have:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind.replace(' PRIMARY KEY', '')}")
            conn.execute("CREATE INDEX IF NOT EXISTS runs_project ON runs (project_id)")

    def record_run(self, row):
        """Insert (or replace: one row per case) a finished-case row."""
        values = []
        for name, _ in RUN_COLUMNS:
            v = row.get(name)
            if name in _JSON_COLUMNS and v is not None and not isinstance(v, str):
                v = json.dumps(v)
            elif name in _BOOL_COLUMNS and v is not None:
                v = 1 if v else 0
            elif isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                v = None                                  # NaN/inf are not valid JSON later
            values.append(v)
        names = ", ".join(name for name, _ in RUN_COLUMNS)
        marks = ", ".join("?" for _ in RUN_COLUMNS)
        with self._db_lock, self._db() as conn:
            conn.execute(f"INSERT OR REPLACE INTO runs ({names}) VALUES ({marks})", values)

    def get_run(self, run_id):
        """The row of one case (its id is the case id), or None."""
        with self._db_lock, self._db() as conn:
            r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_dict(r) if r is not None else None

    @staticmethod
    def _row_to_dict(r):
        d = dict(r)
        for name in _JSON_COLUMNS:
            if d.get(name):
                try:
                    d[name] = json.loads(d[name])
                except (TypeError, json.JSONDecodeError):
                    pass
        for name in _BOOL_COLUMNS:
            if d.get(name) is not None:
                d[name] = bool(d[name])
        return d

    def query_runs(self, project_id=None, model=None, naca=None):
        where, args = [], []
        for col, val in (("project_id", project_id), ("model", model), ("naca", naca)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC"
        with self._db_lock, self._db() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, args)]

    def runs_near(self, reynolds, factor=2.0, not_study=None, naca=None, limit=200):
        """At most `limit` rows whose Reynolds number is within `factor` of `reynolds`, the most
        similar first: the airfoil `naca`, then the closest Reynolds number (as a ratio), then
        the most recent. For the previous-runs pop-up, whose work must not grow with the
        history. Every value is a bound parameter."""
        reynolds, factor = float(reynolds), float(factor)
        where, args = ["typeof(reynolds) IN ('real', 'integer')", "reynolds BETWEEN ? AND ?"], \
            [reynolds / factor, reynolds * factor]
        if not_study:
            where.append("(study_id IS NULL OR study_id != ?)")
            args.append(not_study)
        sql = ("SELECT * FROM runs WHERE " + " AND ".join(where) + " ORDER BY (naca IS NULL OR naca != ?), "
               "CASE WHEN reynolds >= ? THEN reynolds / ? ELSE ? / reynolds END, ts DESC LIMIT ?")
        args += [str(naca), reynolds, reynolds, reynolds, int(limit)]
        with self._db_lock, self._db() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, args)]

    @staticmethod
    def _aggregate(rows):
        """Dashboard numbers for one group of runs."""
        tokens = [(r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in rows]
        # Rows from before the tokens_cached column hold NULL: a group made only of those has
        # no cached count at all, which is not the same as a count of zero.
        cached = [r["tokens_cached"] for r in rows if r.get("tokens_cached") is not None]
        return {
            "runs": len(rows),
            "successes": sum(1 for r in rows if r.get("success")),
            "success_rate": _rate(rows, "success"),
            "first_pass_rate": _rate(rows, "first_pass"),
            "convergence_rate": _rate(rows, "forces_converged"),
            "median_abs_cl_err_pct": _median([abs(r["cl_err_pct"]) for r in rows if r.get("cl_err_pct") is not None]),
            "median_abs_cd_err_pct": _median([abs(r["cd_err_pct"]) for r in rows if r.get("cd_err_pct") is not None]),
            "median_abs_cm_err": _median([abs(r["cm_err_abs"]) for r in rows if r.get("cm_err_abs") is not None]),
            "median_wall_seconds": _median([r.get("wall_seconds") for r in rows]),
            "median_solver_seconds": _median([r.get("solver_seconds") for r in rows]),
            "median_tokens": _median(tokens),
            "tokens_in": sum(r.get("tokens_in") or 0 for r in rows),
            "tokens_out": sum(r.get("tokens_out") or 0 for r in rows),
            "tokens_cached": sum(cached) if cached else None,
            "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in rows), 6),
            "mean_cost_usd": _mean([r.get("cost_usd") or 0.0 for r in rows]),
            "llm_calls": sum(r.get("llm_calls") or 0 for r in rows),
            "tool_calls": sum(r.get("tool_calls") or 0 for r in rows),
            "rework_loops": sum(r.get("rework_loops") or 0 for r in rows),
            "mean_rework_loops": _mean([r.get("rework_loops") or 0 for r in rows]),
            "gate_failures": sum(r.get("gate_failures") or 0 for r in rows),
            "mean_gate_failures": _mean([r.get("gate_failures") or 0 for r in rows]),
            "human_interventions": sum(r.get("human_interventions") or 0 for r in rows),
            "mean_human_interventions": _mean([r.get("human_interventions") or 0 for r in rows]),
            "verdicts": {v: sum(1 for r in rows if r.get("verdict") == v) for v in ("pass", "warn", "fail")},
        }

    def metrics_summary(self, project_id=None):
        rows = self.query_runs(project_id=project_id)

        def grouped(key):
            groups = {}
            for r in rows:
                groups.setdefault(r.get(key) or "unknown", []).append(r)
            # pass^k per model only: pooling different models, or one geometry, is not the measure
            out = [dict({key: name}, **self._aggregate(group), **({"pass_k": pass_k(group)} if key == "model" else {}))
                   for name, group in groups.items()]
            out.sort(key=lambda g: (-g["runs"], str(g[key])))
            return out

        totals = self._aggregate(rows)
        totals["studies"] = len({r.get("study_id") for r in rows})
        totals["models"] = sorted({r.get("model") for r in rows if r.get("model")})
        totals["nacas"] = sorted({r.get("naca") for r in rows if r.get("naca")})
        return {"by_model": grouped("model"), "by_geometry": grouped("naca"), "totals": totals}
