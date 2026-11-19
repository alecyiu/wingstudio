"""What the agents act through: StudyRuntime for the study-level agents (advisor, planner,
steering, reporter) and CaseRunner, the seven-stage pipeline of one case."""
import collections
import copy
import os
import secrets
import threading
import time
import traceback

from foamtools.common import read_json, write_json

from . import exceptions, gates
from .agents import spec as agent_spec
from .agents import tools as agent_tools
from .cfd import case as ac
from .cfd import solver
from .refs import citations
from .store import now_iso
from .study import mission, planning, reporting

MAX_REWORKS = 2                # request_rework per case
MAX_GATE_FAILURES = 3          # failed gates in a row before the human is asked
MAX_CITE_REFUSALS = 2          # complete_stage calls refused for their citations per run of a stage
TERMINAL = ("done", "failed", "stopped")


# ------------------------------------------------------------------- records --

def find_stage(case, key):
    return next(s for s in case["stages"] if s["key"] == key)


def failed_stage(case):
    """The key of the stage that stands failed, or None."""
    return next((s["key"] for s in case["stages"] if s["status"] == "failed"), None)


def new_stage(key):
    return {"key": key, "title": planning.STAGE_TITLES[key], "agent": planning.STAGE_AGENTS[key], "status": "pending",
            "phase": None, "attempts": 0, "started_at": None, "ended_at": None, "seconds": 0.0,
            "headline": None, "summary": None, "decisions": [], "metrics": {}, "gate": None, "artifacts": []}


def zero_usage(with_tools=False):
    usage = {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": 0.0}
    if with_tools:
        usage["tool_calls"] = 0
    return usage


# ------------------------------------------------------------- study runtime --

class StudyRuntime:
    """Hooks and study-changing tools for the study-level agents (advisor,
    planner, steering, reporter)."""

    def __init__(self, engine, sid, agent, case_id=None):
        self.engine = engine
        self.store = engine.store
        self.sid = sid
        self.cid = case_id
        self.agent_name = agent
        self.span = secrets.token_hex(4)
        self.outcome = None
        self.replied = False
        self.last_text = ""
        self.scope = None                     # steering only: the case ids it may act on (None: all)
        # Stop cancels the advisor and the planner; steering and the reporter
        # must still work on a stopped study.
        self.cancellable = agent in ("airfoil-advisor", "planner")

    # -- hooks used by the agent loop --
    def boundary(self):
        if self.cancellable and self.engine.cancelled(self.sid):
            raise exceptions.Interrupt("stop")

    def drain_messages(self):
        return []

    def done(self):
        return self.outcome is not None

    def set_phase(self, kind):
        pass

    def count_tool(self):
        pass

    def add_usage(self, **usage):
        # Study-level agents (advisor, planner, steering, reporter) are counted in Study.usage
        # only, also when a steering message names a case: a case row holds its stage agents.
        self.engine.add_usage(self.sid, None, **usage)

    def nudge(self):
        return {"airfoil-advisor": "Finish by calling propose_candidates(candidates, top).",
                "planner": "Finish by calling write_plan(plan).",
                "reporter": "Finish by calling write_report(markdown, recommendation)."}.get(self.agent_name)

    def context(self):
        return agent_tools.ToolContext(self.store, self.sid, agent=self.agent_name, runtime=self, span_id=self.span)

    # -- advisor --
    def _screen_path(self):
        return os.path.join(self.store.studies_dir, self.sid, "screen.json")

    def screen_cache(self):
        return read_json(self._screen_path()) or {}

    def remember_screen(self, results, target):
        cache = self.screen_cache()
        for r in results:
            cache[r["naca"]] = dict(r, design_cl=target)
        write_json(self._screen_path(), cache)

    def screened(self, codes):
        """Screen results for these codes at the study's design Cl, running XFoil for any that are missing."""
        study = self.store.get_study(self.sid)
        req = study["requirements"]
        cache = self.screen_cache()
        missing = [c for c in codes if c not in cache or cache[c].get("design_cl") != req["design_cl"]]
        if missing:
            results = agent_tools.screen_airfoils(missing, study["conditions"]["reynolds"], req["design_cl"], req)
            self.remember_screen(results, req["design_cl"])
            cache = self.screen_cache()
        return {c: cache[c] for c in codes}

    def propose_candidates(self, candidates, top):
        study = self.store.get_study(self.sid)
        req = study["requirements"]
        entries = []
        for c in candidates:
            if not isinstance(c, dict):
                return {"ok": False, "error": "each candidate must be an object {naca, rationale}"}
            try:
                code = ac.naca4(c.get("naca"))[0]
            except (ValueError, AttributeError):
                return {"ok": False, "error": f"{c.get('naca')!r} is not a NACA 4-digit code"}
            if code not in [e["naca"] for e in entries]:
                entries.append({"naca": code, "rationale": str(c.get("rationale") or "")})
        if len(entries) < 2:
            return {"ok": False, "error": "propose at least two different airfoils"}
        if len(entries) > 8:
            return {"ok": False, "error": "propose at most eight airfoils"}
        screen = self.screened([e["naca"] for e in entries])
        built = mission.build_candidates(entries, screen, req, top)
        if isinstance(built, str):
            return {"ok": False, "error": built}
        self.store.update_study(self.sid, lambda s: s.__setitem__("candidates", built))
        self.engine.emit(self.sid, "candidates.proposed", {"candidates": built}, agent=self.agent_name,
                         parent_span_id=self.span)
        self.outcome = "done"
        chosen = [c["naca"] for c in built if c["selected"]]
        return {"ok": True, "selected": chosen, "ranking": [c["naca"] for c in built],
                "summary": f"proposed {len(built)} candidates; top two: NACA " + " and NACA ".join(chosen)}

    # -- planner --
    def get_defaults(self):
        study = self.store.get_study(self.sid)
        # The default citations come once, as default_cites, not inside every case of the plan:
        # repeated for four airfoils they would push this result past the tool-result limit.
        return {"ok": True, "plan": planning.default_plan(study),
                "default_cites": {k: list(v) for k, v in citations.load()["defaults"].items()},
                "references": citations.index(),
                "allowed": {
                    "mesh": {"farfield_chords": "5 to 100 (30 is the stable maximum)", "wake_chords": "5 to 100",
                             "n_normal": "20 to 300", "n_leading": "10 to 200", "n_aft": "10 to 200",
                             "n_wake": "10 to 300", "first_cell_height": "1e-7 to 0.05 chord",
                             "cells": "2 x (n_leading + n_aft + n_wake) x n_normal, at most 80,000"},
                    "physics": dict({"turbulence_model": ["SpalartAllmaras", "kOmegaSST"],
                                     "wall_treatment": ["wallFunction", "resolved"],
                                     "farfield_bc": ["fixedInlet", "freestream"]},
                                    **{k: f"{lo:g} to {hi:g}" for k, (lo, hi) in planning.SETTING_LIMITS["physics"].items()}),
                    "numerics": dict({"iterations": "300 to 6000, a multiple of write_interval",
                                      "convection_scheme": ["linearUpwind", "upwind"]},
                                     **{k: f"{lo:g} to {hi:g}" for k, (lo, hi) in planning.SETTING_LIMITS["numerics"].items()})},
                "note": "Each stage section is a flat object of settings plus 'reasons' (setting -> sentence) and "
                        "'checks' (list). alpha_deg is fixed by the XFoil screen. Settings you omit keep these defaults. "
                        "'cites' (setting -> list of reference ids from references) only for values you change.",
                "summary": "default plan for " + ", ".join("NACA " + c["naca"] for c in study["candidates"] if c.get("selected"))}

    def write_plan(self, plan):
        if not isinstance(plan, dict):
            return {"ok": False, "error": "plan must be a JSON object, not a quoted string"}
        if self.agent_name != "scripted" and (len(str(plan.get("summary") or "").strip()) < 40
                                              or not isinstance(plan.get("cases"), list) or not plan["cases"]):
            return {"ok": False, "error": "this is not a plan yet: give a summary of a few sentences and a 'cases' "
                                          "list with one entry per selected airfoil ({naca, stages}); settings you "
                                          "leave out keep the defaults"}
        study = self.store.get_study(self.sid)
        # marks_from: a 'Set by the engineer' reason survives only on the value the engineer set
        normalised, notes = planning.normalise_plan(study, plan, marks_from=study.get("plan") or {})
        errors = planning.validate_plan(normalised)
        if errors:
            return {"ok": False, "error": "; ".join(errors), "notes": notes}
        version = self.engine.store_plan(self.sid, normalised, agent=self.agent_name, span=self.span)
        self.outcome = "done"
        return {"ok": True, "version": version, "notes": notes,
                "summary": f"plan version {version} for " + ", ".join("NACA " + c["naca"] for c in normalised["cases"])}

    # -- steering --
    def reply(self, text):
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "the reply is empty"}
        self.engine.emit(self.sid, "steering.reply", {"text": text}, case_id=self.cid, agent=self.agent_name,
                         parent_span_id=self.span)
        self.replied = True
        return {"ok": True, "summary": "replied to the engineer"}

    def _out_of_scope(self, case):
        if self.scope and case and case["id"] not in self.scope:
            return {"ok": False, "error": f"case {case['id']} is not yours to change here: its stage agent has "
                                          "received the engineer's message directly and is acting on it"}
        return None

    def _resolve_case(self, case_id):
        cases = self.store.list_cases(self.sid)
        key = str(case_id or self.cid or "").strip().lower().replace("naca", "").strip()
        for c in cases:
            if key in (c["id"], c["naca"]):
                return c
        if not key and len(cases) == 1:
            return cases[0]
        return None

    def update_plan(self, case_id, stage, params):
        if stage not in planning.STAGE_KEYS:
            return {"ok": False, "error": f"stage must be one of {', '.join(planning.STAGE_KEYS)}"}
        if not isinstance(params, dict) or not params:
            return {"ok": False, "error": "params must be a non-empty object of setting -> value"}
        study = self.store.get_study(self.sid)
        if not study.get("plan"):
            return {"ok": False, "error": "there is no plan yet"}
        # edit a copy: study["plan"] stays as it was, so mark_engineer_edits below sees the change
        plan = copy.deepcopy(study["plan"])
        case = self._resolve_case(case_id)
        refused = self._out_of_scope(case)
        if refused:
            return refused
        naca = case["naca"] if case else str(case_id or "").upper().replace("NACA", "").strip()
        target = next((pc for pc in plan["cases"] if pc["naca"] == naca), None)
        if target is None:
            return {"ok": False, "error": f"no case {case_id!r} in the plan",
                    "cases": [pc["naca"] for pc in plan["cases"]]}
        section = target["stages"][stage]
        params = dict(params)
        if "naca" in params and str(params["naca"]).upper().replace("NACA", "").strip() != naca:
            return {"ok": False, "error": "the airfoil of a case cannot be changed after the selection; "
                                          "a different section needs a new study (tell the engineer)"}
        params.pop("naca", None)
        params.pop("cites", None)             # a plan change made here is the engineer's: it carries no citation
        alpha = params.pop("alpha_deg", None) if stage == "physics" else None
        if alpha is not None:
            # the angle of attack is a case-level plan value; the physics stage applies it
            try:
                target["alpha_deg"] = float(alpha)
            except (TypeError, ValueError):
                return {"ok": False, "error": "alpha_deg must be a number (degrees)"}
        unknown = [k for k in params if k not in section and k not in ("reasons",)]
        if unknown:
            return {"ok": False, "error": f"unknown setting(s) for {stage}: {', '.join(unknown)}",
                    "settings": [k for k in section if k not in planning.PLAN_FIXED_KEYS]
                                + (["alpha_deg"] if stage == "physics" else [])}
        if not params and alpha is None:
            return {"ok": False, "error": "nothing to change"}
        target["stages"][stage] = dict(section, **params)
        if alpha is not None:
            params["alpha_deg"] = target["alpha_deg"]
        normalised, notes = planning.normalise_plan(study, plan, keep_alpha=True, cites_from=study["plan"])
        planning.mark_engineer_edits(study["plan"], normalised)
        errors = planning.validate_plan(normalised, cites=False)
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        changed = naca in planning.plan_changes(study["plan"], normalised)
        effects = {}
        version = self.engine.store_plan(self.sid, normalised, agent=self.agent_name, span=self.span,
                                         keep_approval=True, effects=effects)
        passed = bool(case and find_stage(case, stage)["status"] == "passed")
        rewound = effects.get(case["id"]) if case else None
        if case and changed and not rewound:
            # The steering agent only acts on a message from the engineer, so a plan change it
            # makes is help for that case, with or without a live pipeline, once per call (a
            # changed angle that sent the case back was counted there, as the re-run it is).
            self.engine.intervention(self.sid, "plan_edit", [case["id"]])
        if rewound == "rerun":
            takes_effect = ("now: the physics stage had already run at the old angle, so the case was sent back "
                            "to the physics stage and re-runs from there at the new angle (no rerun_from needed "
                            "for the angle)")
        elif rewound == "stale":
            takes_effect = ("the physics stage and everything after it were computed at the old angle and are "
                            "now marked stale; the case is not running, so they are redone at the new angle when "
                            "it is re-run (rerun_from physics)")
        elif passed:
            takes_effect = "that stage has already run: call rerun_from to apply the change"
        else:
            takes_effect = "when the stage runs"
        return {"ok": True, "version": version, "notes": notes, "takes_effect": takes_effect,
                "summary": f"plan v{version}: {stage} of NACA {naca} set " +
                           ", ".join(f"{k} = {v}" for k, v in params.items()) +
                           ("; the case re-runs from physics at the new angle" if rewound == "rerun" else
                            "; results of the old angle are marked stale" if rewound == "stale" else "")}

    def rerun_from(self, case_id, stage, instruction=""):
        case = self._resolve_case(case_id)
        if case is None:
            return {"ok": False, "error": f"no such case: {case_id!r}",
                    "cases": [c["id"] for c in self.store.list_cases(self.sid)]}
        refused = self._out_of_scope(case)
        if refused:
            return refused
        try:
            # not by_human: the approval of a waiting stage stays the engineer's. The re-run is
            # still counted as help for the case (Engine.rerun), unless nothing is re-run.
            effect = self.engine.rerun(self.sid, case["id"], stage, instruction, by_human=False)
        except exceptions.EngineError as e:
            return {"ok": False, "error": str(e)}
        if effect == "noted":
            return {"ok": True, "rerun": False,
                    "summary": f"nothing re-run: {stage} of NACA {case['naca']} has not run yet and is waiting "
                               "for the engineer's approval; it will use the current plan values"
                               + (" and your instruction" if str(instruction or "").strip() else "")
                               + " when the engineer approves it"}
        return {"ok": True, "rerun": True, "summary": f"NACA {case['naca']} re-runs from {stage}"}

    def set_mode(self, mode):
        try:
            self.engine.set_mode(self.sid, mode, by_human=False)
        except exceptions.EngineError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "mode": mode, "summary": f"mode set to {mode}"}

    # -- reporter --
    def get_results(self):
        study = self.store.get_study(self.sid)
        cases = self.store.list_cases(self.sid)
        rows = reporting.results_table(study, cases)
        for row, c in zip(rows, cases):
            row["counters"] = c.get("counters")
            row["failed_stages"] = [s["key"] for s in c["stages"] if s["status"] == "failed"]
            row["stage_summaries"] = {s["key"]: s.get("summary") for s in c["stages"] if s.get("summary")}
            row["other_xfoil_points"] = (next((s for s in c["stages"] if s["key"] == "validate"), {})
                                         .get("metrics") or {}).get("other_xfoil_points")
        return {"ok": True, "requirements": study["requirements"],
                "objective": reporting.OBJECTIVE_LABEL.get(study["requirements"]["objective"]),
                "target_ld": study["requirements"].get("target_ld"),
                "conditions": agent_spec.rounded_conditions(study["conditions"]),
                "assumptions": (study.get("plan") or {}).get("assumptions"),
                "xfoil_screen": [{"naca": c["naca"], "selected": c.get("selected"), "rank": c.get("rank"),
                                  "meets_constraints": c.get("meets_constraints"),
                                  "meets_target_ld": reporting.meets_target_ld(study["requirements"], (c.get("xfoil") or {}).get("ld")),
                                  **{k: v for k, v in (c.get("xfoil") or {}).items() if k != "polar"}}
                                 for c in study.get("candidates") or []],
                "cases": rows,
                "note": "objective_value: higher is better (for min_cd it is -Cd). 'recommended' marks the best "
                        "finished case with a pass or warn verdict among those that meet the mission's thickness "
                        "and moment limits (meets_thickness, meets_moment, judged on the RANS Cm); if none meets "
                        "them it marks the best of all, and the report must say which limit is broken. "
                        "cl_vs_design_pct is the RANS Cl against the design Cl: the runs are at fixed angles, so "
                        "state the lift each case reached when it differs by more than about 1%. target_ld (null: "
                        "none) is the wanted L/D at the design Cl; meets_target_ld judges XFoil's L/D in "
                        "xfoil_screen and the RANS L/D in cases. It is reported as met / not met and never "
                        "excludes a case.",
                "summary": f"results of {len(rows)} cases"}

    def write_report(self, markdown, recommendation):
        if not str(markdown or "").strip() or not str(recommendation or "").strip():
            return {"ok": False, "error": "both markdown and recommendation are required"}
        study = self.store.get_study(self.sid)
        report = {"markdown": str(markdown), "recommendation": str(recommendation),
                  "table": reporting.results_table(study, self.store.list_cases(self.sid))}
        self.store.update_study(self.sid, lambda s: s.__setitem__("report", report))
        self.outcome = "done"
        return {"ok": True, "summary": "report written"}

    # tools that only make sense inside a case pipeline
    def complete_stage(self, *a, **k):
        return {"ok": False, "error": "complete_stage is only available to stage agents"}

    def request_rework(self, *a, **k):
        return {"ok": False, "error": "request_rework is only available to stage agents"}


def never_started(stage):
    """True for a stage record that has not begun a single attempt (pending, or only waiting
    for approval): re-running it is a first run, not a rework."""
    return not stage.get("attempts") and stage.get("status") not in ("running", "passed", "failed")


def mark_stale_from(case, index):
    """Rewind bookkeeping on a case record: stage `index` and every later stage that has
    run must be redone, and the results that depend on them are dropped."""
    for s in case["stages"][index:]:
        if s["status"] != "pending":
            # a stage that never started (it was only waiting for approval) has nothing to redo
            s["status"], s["phase"] = ("pending" if never_started(s) else "stale"), None
    case["verdict"], case["deltas"], case["reference"] = None, None, None
    if index <= planning.STAGE_KEYS.index("post"):
        cells = (case.get("results") or {}).get("cells")
        case["results"] = {"cells": cells} if (cells and index > planning.STAGE_KEYS.index("mesh")) else None


def close_open_stages(case):
    """A pipeline ended (Stop, restart) with a stage in flight: what had started is stale,
    what was only waiting for approval is pending again."""
    for s in case["stages"]:
        if s["status"] in ("running", "awaiting_approval"):
            s["status"], s["phase"] = ("stale" if s.get("attempts") else "pending"), None


# --------------------------------------------------------------- case runner --

class CaseRunner:
    """One pipeline: the seven stages of one airfoil, in its own thread. It is
    also the `runtime` the stage tools talk to."""

    def __init__(self, engine, sid, cid, rerun=None):
        self.engine = engine
        self.store = engine.store
        self.sid, self.cid = sid, cid
        self.cv = threading.Condition()
        self.stop_flag = False
        self.paused = False
        self.rerun_request = rerun            # (stage, instruction, by_human) or None
        self.approved = False
        self.force_gate = False               # set by the auto -> gate fallback
        self.pre_approved = None              # stage the human has already said yes to
        self.waiting = None                   # stage waiting for approval
        self.waiting_retry = False            # ... and it failed: the approval is for a retry (help)
        self.inbox = collections.deque()
        self.agent_active = False
        self.outcome = None
        self.gate_fails = 0
        self.cite_refusals = 0                # complete_stage calls refused for their citations, this run
        self.gate_limit = MAX_GATE_FAILURES
        self.stage = None
        self.agent_name = None
        self.stage_span = None
        self.notes = {}                       # stage -> instructions for its next session
        self.told_scripted = False
        self.run_longer = False               # scripted remedy after a fail verdict: 50% more iterations
        self.acted = False                    # the current stage session has run an act or verify tool
        self.lock_open = False                # this run of the stage has a reason to change an engineer value
        self.closing = False                  # the pipeline is ending: a rerun is refused, not queued
        self.finished = False
        self.thread = threading.Thread(target=self.run, name=f"case-{cid}", daemon=True)

    @property
    def viewer(self):
        return self.engine.viewer

    def start(self):
        self.thread.start()

    def alive(self):
        return self.thread.is_alive() and not self.finished

    # ------------------------------------------------------ small helpers --

    def case(self):
        return self.store.get_case(self.sid, self.cid)

    def study(self):
        return self.store.get_study(self.sid)

    def emit(self, type, data=None, stage=None, span_id=None, parent=None, full=None, agent=None):
        # agent="" marks an event that no agent produced (status changes, human actions)
        return self.engine.emit(self.sid, type, data or {}, case_id=self.cid,
                                stage=stage if stage is not None else self.stage,
                                agent=None if agent == "" else (agent if agent is not None else self.agent_name),
                                span_id=span_id, parent_span_id=parent, full=full)

    def update_stage(self, key, mutate):
        def apply(case):
            for s in case["stages"]:
                if s["key"] == key:
                    mutate(s)
        return self.store.update_case(self.sid, self.cid, apply)

    def set_status(self, status):
        case = self.case()
        if case["status"] == status:
            return
        self.store.update_case(self.sid, self.cid, lambda c: c.__setitem__("status", status))
        self.emit("case.status", {"status": status}, agent="")
        self.engine.refresh_study_status(self.sid)

    def count(self, counter, n=1):
        self.store.update_case(self.sid, self.cid,
                               lambda c: c["counters"].__setitem__(counter, c["counters"].get(counter, 0) + n))

    def gate_mode(self):
        return self.force_gate or self.study().get("mode") == "gate"

    # ------------------------------------------ hooks for the agent loop --

    def message_pending(self):
        return bool(self.inbox)

    def interrupted(self):
        return self.stop_flag or self.paused or self.rerun_request is not None

    def _raise_interrupt(self):
        """Under self.cv: unwind the session if a stop or a rerun is pending."""
        if self.stop_flag:
            raise exceptions.Interrupt("stop")
        if self.rerun_request is not None:
            raise exceptions.Interrupt("rerun")

    def boundary(self):
        """Tool boundary: stop and rerun unwind the session, pause blocks here."""
        with self.cv:
            self._raise_interrupt()
            if not self.paused:
                return
        self.set_status("paused")
        with self.cv:
            while self.paused and not self.stop_flag and self.rerun_request is None:
                self.cv.wait(1.0)
            self._raise_interrupt()
        self.set_status("running")

    def drain_messages(self):
        """Deliver queued human messages: the human.message event is written at
        the moment the agent receives the text."""
        out = []
        while self.inbox:
            msg = self.inbox.popleft()
            self.emit("human.message", {"text": msg["text"], "quote": msg.get("quote"),
                                        "target_stage": msg.get("target_stage"), "id": msg.get("id"),
                                        "delivered_to": self.agent_name}, agent="")
            out.append(agent_spec.format_message(msg))
        if out:
            self.lock_open = True                # the engineer spoke: agent_tools.engineer_locked steps aside
        return out

    def done(self):
        return self.outcome is not None

    def set_phase(self, kind):
        if kind in ("act", "verify"):
            self.acted = True
        if find_stage(self.case(), self.stage).get("phase") != kind:
            self.update_stage(self.stage, lambda s: s.__setitem__("phase", kind))
            self.emit("stage.phase", {"phase": kind}, parent=self.stage_span)

    def count_tool(self):
        self.engine.add_usage(self.sid, self.cid, tool_calls=1)

    def add_usage(self, **usage):
        self.engine.add_usage(self.sid, self.cid, **usage)

    def nudge(self):
        return ("The stage is not finished until you call complete_stage(summary, decisions) "
                "(or request_rework if the fault lies in an earlier stage).")

    # ----------------------------------------- tools forwarded by agent_tools --

    def complete_stage(self, summary, decisions):
        ctx = self.context()
        # Citations first: an unknown reference id is refused before the gate runs, so it costs
        # the agent one turn and no attempt (no gate.result, no counter).
        section = ctx.plan_stage(self.stage)
        cited, problems, unknown = planning.decision_cites(self.stage, decisions, section)
        dropped = None
        if problems and self.cite_refusals < MAX_CITE_REFUSALS:
            self.cite_refusals += 1
            return {"ok": False, "gate_run": False, "unknown_ids": unknown,
                    "valid_ids": citations.ids_for(self.stage),
                    "error": "; ".join(problems) + " The gate was not run and no attempt was used: correct the "
                             "decisions and call complete_stage again.",
                    "summary": ("failed: decisions cite an unknown reference id (gate not run)" if unknown else
                                "failed: the cites of a decision are not valid (gate not run)")}
        if problems:
            # A citation mistake must not cost every turn of the session or fail the stage: after
            # MAX_CITE_REFUSALS refusals the decisions go to the gate with what can stand of their
            # cites (the ids that exist, at most three), and the result says per decision what
            # was dropped and why.
            went = []
            cited, _, _ = planning.decision_cites(self.stage, decisions, section, salvage=went)
            dropped = f"citations dropped after {MAX_CITE_REFUSALS} refusals: " + "; ".join(went)
        gate = gates.run_gate(self.stage, ctx)
        self.update_stage(self.stage, lambda s: s.__setitem__("gate", gate))
        self.emit("gate.result", {"passed": gate["passed"], "checks": gate["checks"]}, parent=self.stage_span)
        clean = []
        for d, ids in zip(decisions if isinstance(decisions, list) else [], cited):
            if isinstance(d, dict) and d.get("param") is not None:
                clean.append({"param": str(d["param"]), "value": d.get("value"), "reason": str(d.get("reason") or ""),
                              "cites": ids})
        notes = {"notes": [dropped]} if dropped else {}
        # Which engineer values this gate has judged is kept on the stage record, for a later run
        # of the stage that an earlier stage's re-run brings about (stage_context_text): the lock
        # is per run, this is not. A pass with a value in place takes its rejection back.
        judged = agent_tools.engineer_values_in_place(ctx, self.stage)
        if gate["passed"]:
            def forgive(s):
                rejected = s.get("engineer_rejected") or {}
                left = {k: v for k, v in (rejected.get("values") or {}).items() if k not in judged}
                if left:
                    rejected["values"] = left
                else:
                    s.pop("engineer_rejected", None)
            if judged:
                self.update_stage(self.stage, forgive)
            self.outcome = ("passed", str(summary or "").strip(), clean)
            return {"ok": True, "gate_passed": True, "checks": gate["checks"], **notes,
                    "summary": "gate passed" + (f"; {dropped}" if dropped else "")}
        self.gate_fails += 1
        self.count("gate_failures")
        failed = [c for c in gate["checks"] if not c["ok"]]
        text = "; ".join(f"{c['name']}: {c['value']} (limit {c['limit']})" for c in failed)
        if judged:
            self.update_stage(self.stage, lambda s: s.__setitem__("engineer_rejected",
                                                                  {"values": judged, "checks": text}))
        left = self.gate_limit - self.gate_fails
        if left <= 0:
            self.outcome = ("failed", f"gate failed {self.gate_fails} times in a row: {text}"
                            if self.gate_fails > 1 else f"gate failed: {text}")
        else:
            # A failed gate ends the attempt; the same agent continues with the feedback.
            case = self.update_stage(self.stage, lambda s: s.__setitem__("attempts", s["attempts"] + 1))
            self.emit("stage.started", {"attempt": find_stage(case, self.stage)["attempts"]}, span_id=self.stage_span)
        return {"ok": False, "gate_passed": False, "failed_checks": failed, "attempts_left": max(0, left),
                "error": "the gate did not pass: " + text, **notes,
                "hint": "fix the cause with your tools and call complete_stage again"
                        + ("; if the fault lies in an earlier stage, call request_rework" if self.stage in
                           ("solve", "post", "validate") else ""),
                "summary": "gate failed: " + text}

    def request_rework(self, stage, reason):
        if stage not in planning.STAGE_KEYS:
            return {"ok": False, "error": f"stage must be one of {', '.join(planning.STAGE_KEYS)}"}
        if planning.STAGE_KEYS.index(stage) >= planning.STAGE_KEYS.index(self.stage):
            return {"ok": False, "error": f"rework must target a stage before {self.stage}; "
                                          "fix your own stage with your tools"}
        case = self.case()
        if case["counters"].get("rework_loops", 0) >= MAX_REWORKS:
            return {"ok": False, "error": f"the rework limit of {MAX_REWORKS} per case is used up; "
                                          "complete the stage with what you have and state the shortfall"}
        self.count("rework_loops")
        self.outcome = ("rework", stage, str(reason or "").strip() or "rework requested")
        return {"ok": True, "summary": f"rework requested from {stage}: {reason}"}

    # ------------------------------------------------------------ contexts --

    def context(self):
        return agent_tools.ToolContext(self.store, self.sid, self.cid, stage=self.stage, agent=self.agent_name,
                                       runtime=self, span_id=self.stage_span)

    def stage_context_text(self, key, attempt):
        study, case = self.study(), self.case()
        section = self.context().plan_stage(key)
        lines = [f"Case {case['id']}: NACA {case['naca']} at {case['alpha_deg']:g} deg angle of attack.",
                 f"Your stage: {planning.STAGE_TITLES[key]} (key `{key}`), attempt {attempt}. Run mode: {study.get('mode')}.",
                 "", "## Mission requirements", agent_spec.compact(study["requirements"]),
                 "", "## Flow conditions", agent_spec.compact(agent_spec.rounded_conditions(study["conditions"])),
                 "", "## Your section of the approved plan", agent_spec.compact(section)]
        engineer = planning.engineer_values(section)
        if engineer:
            lines += ["", "## Values the engineer set by hand in this section",
                      agent_spec.compact(engineer),
                      "These are instructions, not planner defaults: apply them exactly as given, even if you would "
                      "have chosen differently, do the rest of the stage as usual and call complete_stage with them "
                      "in place. If you expect one to cause trouble, say so once with note() and still submit it. A "
                      "warning from a tool, a check that reports the value outside a limit, or your own estimate "
                      "that the gate will reject the stage is not a rejection: only the gate decides, and letting "
                      "it decide costs one attempt, which is expected here. Only when complete_stage answers that "
                      "the gate did not pass because of that value may you change it: use the value you would "
                      "recommend (one that passes the failed check with margin, not one at the edge of the limit), "
                      "redo the work, and state the engineer's value, the failed check and your value in your "
                      "summary and decisions."
                      + (" set_mesh_params refuses another value for these settings until the gate has judged them."
                         if key == "mesh" else "")]
        references = citations.for_stage(key)
        lines += ["", "## References you may cite (ids only)"]
        lines += [f"- {r['id']} ({r['kind']}): {r['says']}" for r in references] or \
            ["(none for this stage: leave cites out)"]
        lines += ["", "## Hand-offs from earlier stages"]
        upstream = [s for s in case["stages"][:planning.STAGE_KEYS.index(key)] if s["status"] == "passed"]
        if not upstream:
            lines.append("(none: this is the first stage)")
        for s in upstream:
            lines.append(f"- {s['key']}: {s.get('headline') or ''}\n  {s.get('summary') or ''}\n  "
                         f"decisions: {agent_spec.compact(planning.without_cites(s.get('decisions')), agent_spec.HANDOFF_DECISION_CHARS)}\n  "
                         f"metrics: {agent_spec.compact(s.get('metrics') or {}, 1200)}")
        with self.cv:
            notes = self.notes.pop(key, [])
        previous = find_stage(case, key)
        if notes:
            # a re-run instruction or a rework reason may ask for another value than the engineer's
            self.lock_open = True
            lines += ["", "## Why this stage is being run again" if attempt > 1 else "## Instructions for this stage"]
            lines += [f"- {n}" for n in notes]
        elif not previous.get("failure"):
            # The stage runs again only because an earlier one was redone. If the gate has already
            # judged the engineer's values that are still in the plan, the agent must not be made
            # to submit them a second time just to fail the same check: say so and open the lock.
            rejected = previous.get("engineer_rejected") or {}
            again = {k: v for k, v in (rejected.get("values") or {}).items() if k in engineer and engineer[k] == v}
            if again:
                self.lock_open = True
                lines += ["", "## Earlier run of this stage",
                          "The gate has already judged the engineer's "
                          + ", ".join(f"{k} = {v}" for k, v in again.items())
                          + f" in an earlier run of this stage and did not pass ({rejected.get('checks')}). If "
                          "that check failed because of the value, do not submit it again: use the value you "
                          "would recommend and state the engineer's value, the failed check and your value in "
                          "your summary and decisions. If the check had another cause, apply the value as given."]
        if previous.get("failure"):
            lines += ["", "## Previous attempt", f"It failed: {previous['failure']}"]
        messages = self.drain_messages()
        if messages:
            lines += ["", "## Messages from the engineer"] + [f"HUMAN MESSAGE: {m}" for m in messages]
        lines += ["", "Work gather -> act -> verify, then call complete_stage(summary, decisions)."]
        return "\n".join(lines)

    # ---------------------------------------------------------------- run --

    def run(self):
        try:
            self.set_status("running")
            while True:
                # The flags are read under the runner's lock, but finish() must run outside it:
                # it ends in engine.case_finished, which takes the engine lock, while the request
                # handlers (control, set_mode, rerun) take the engine lock first and this one second.
                with self.cv:
                    stopped, request = self.stop_flag, None
                    if not stopped:
                        request, self.rerun_request = self.rerun_request, None
                if stopped:
                    return self.finish("stopped")
                if request:
                    self.apply_rerun(*request)
                key = self.next_stage()
                if key is None:
                    with self.cv:
                        if self.rerun_request is not None and not self.stop_flag:
                            continue
                        self.closing = True       # from here on a rerun is refused (409), never lost
                    return self.finish("done")
                try:
                    self.boundary()
                except exceptions.Interrupt:
                    continue
                if self.gate_mode() and self.pre_approved != key:
                    fallback = self.force_gate and self.study()["mode"] != "gate"
                    answer = self.await_approval(key, (
                        "Auto-run asks before each stage of this case after a failed stage: approve to run "
                        if fallback else "Gate mode: approve to run ") + planning.STAGE_TITLES[key])
                    if answer in ("stop", "rerun"):
                        continue
                self.pre_approved = None
                result = self.run_stage(key)
                if result[0] == "rework":
                    self.rewind(result[1], result[2], by="agent", from_stage=key)
                elif result[0] == "failed":
                    self.force_gate = True
                    self.engine.write_metrics(self.sid, self.cid, "failed", failed_stage=key)
                    if self.await_approval(key, result[1], failed=True) == "approved":
                        self.pre_approved = key
        except Exception as e:  # noqa: BLE001 - never let a worker die silently
            self.emit("error", {"message": f"case pipeline crashed: {type(e).__name__}: {e}",
                                "trace": traceback.format_exc()[-1500:]})
            with self.cv:
                self.closing = True
            self.finish("failed")

    def next_stage(self):
        for s in self.case()["stages"]:
            if s["status"] != "passed":
                return s["key"]
        return None

    def await_approval(self, key, reason, failed=False):
        if not failed:
            self.update_stage(key, lambda s: s.__setitem__("status", "awaiting_approval"))
        self.store.update_case(self.sid, self.cid, lambda c: c.__setitem__("current_stage", key))
        self.stage = key
        self.set_status("awaiting_approval")
        self.emit("approval.requested", {"stage": key, "reason": reason}, stage=key, agent="")
        with self.cv:
            self.waiting, self.approved, self.waiting_retry = key, False, bool(failed)
        answer = None
        while answer is None:
            self.hand_over_messages()            # no stage agent is active while the case waits
            auto = (not failed) and not self.gate_mode()
            with self.cv:
                if self.stop_flag:
                    answer = "stop"
                elif self.rerun_request is not None:
                    answer = "rerun"
                elif self.approved:
                    answer = "approved"
                elif auto:
                    answer = "auto"              # the mode was switched back to auto
                else:
                    self.cv.wait(1.0)
        with self.cv:
            self.waiting, self.approved, self.waiting_retry = None, False, False
        if answer in ("approved", "auto"):
            self.set_status("running")
        return answer

    def run_stage(self, key):
        study = self.study()
        agent_file = planning.STAGE_AGENTS[key]
        self.stage = key
        self.agent_name = "scripted" if study["model"] == agent_spec.SCRIPTED else agent_file[:-3]
        self.stage_span = secrets.token_hex(4)
        self.outcome, self.gate_fails, self.cite_refusals, self.acted, self.lock_open = None, 0, 0, False, False
        # A rerun answered while the case waited for approval leaves the record at
        # awaiting_approval; the stage is running now. (No-op when already running.)
        self.set_status("running")
        # The scripted policy is deterministic, so repeating a failed gate cannot help.
        self.gate_limit = 1 if study["model"] == agent_spec.SCRIPTED else MAX_GATE_FAILURES
        started = time.time()

        def begin(s):
            s.update(status="running", phase=None, attempts=s["attempts"] + 1, started_at=now_iso(),
                     ended_at=None, gate=None)
        case = self.update_stage(key, begin)
        self.store.update_case(self.sid, self.cid, lambda c: c.__setitem__("current_stage", key))
        attempt = find_stage(case, key)["attempts"]
        self.emit("stage.started", {"attempt": attempt}, span_id=self.stage_span)
        interrupt = None
        self.agent_active = True
        try:
            if study["model"] == agent_spec.SCRIPTED:
                self.scripted_stage(key)
            else:
                spec = agent_spec.load_agent(agent_file)
                self.agent_name = spec.name
                allowed = [t for t in spec.tools if t in agent_tools.TOOLS] or (agent_tools.COMMON_TOOLS + agent_tools.STAGE_TOOLS[key])
                ended = self.engine.llm_loop(spec, agent_spec.resolve_model(spec.model, study["model"]), self.context(),
                                             allowed, self.stage_context_text(key, attempt), self)
                if self.outcome is None:
                    self.outcome = ("failed", f"the {spec.name} agent ended ({ended}) without completing the stage")
        except exceptions.Interrupt as i:
            interrupt = i.kind
        except exceptions.AgentError as e:
            self.outcome = ("failed", str(e))
        except Exception as e:  # noqa: BLE001
            self.outcome = ("failed", f"{type(e).__name__}: {e}")
            self.emit("error", {"message": f"stage {key} crashed: {type(e).__name__}: {e}",
                                "trace": traceback.format_exc()[-1500:]}, parent=self.stage_span)
        finally:
            self.agent_active = False
        seconds = round(time.time() - started, 1)

        def end(status, **extra):
            def apply(s):
                s.update(status=status, phase=None, ended_at=now_iso(),
                         seconds=round((s.get("seconds") or 0.0) + seconds, 1), **extra)
            return self.update_stage(key, apply)

        if interrupt:
            solver.kill_solver(self.cid)
            if self.acted:
                end("stale")
            else:
                # Stopped or restarted before the session changed anything (typically during its
                # first model call): that was not an attempt. Take it back, so that the restart
                # is not counted or shown as a re-run of the stage.
                def undo(s):
                    s["attempts"] = max(0, s["attempts"] - 1)
                    s.update(status="stale" if s["attempts"] else "pending", phase=None, ended_at=now_iso(),
                             seconds=round((s.get("seconds") or 0.0) + seconds, 1))
                self.update_stage(key, undo)
                if interrupt == "rerun":
                    self.pre_approved = key      # it was already cleared to run
            return ("stopped",) if interrupt == "stop" else ("rerun",)
        # A message that arrived during the agent's last turn was not read by it: the steering
        # agent takes it (it can change the plan or send the case back to this stage).
        self.hand_over_messages()
        outcome = self.outcome or ("failed", "the stage ended without a result")
        if outcome[0] == "passed":
            _, summary, decisions = outcome
            s = find_stage(end("passed", summary=summary, decisions=decisions, failure=None), key)
            if not s.get("headline"):
                s = find_stage(self.update_stage(key, lambda x: x.__setitem__("headline", "passed")), key)
            self.emit("stage.completed", {"summary": summary, "headline": s.get("headline"), "decisions": decisions,
                                          "metrics": s.get("metrics") or {}, "seconds": s["seconds"]},
                      span_id=self.stage_span)
            return ("passed",)
        if outcome[0] == "rework":
            end("stale")
            return outcome
        solver.kill_solver(self.cid)
        end("failed", failure=outcome[1])
        self.emit("stage.failed", {"reason": outcome[1]}, span_id=self.stage_span)
        return ("failed", outcome[1])

    def hand_over_messages(self):
        """Give queued messages that no stage agent read to the steering agent."""
        while self.inbox:
            self.engine.spawn(self.engine._steer_worker, self.sid, self.cid, self.inbox.popleft())

    def apply_rerun(self, stage, instruction, by_human=True):
        """A rerun request reaches the pipeline. Only a stage that has started is rewound."""
        if not never_started(find_stage(self.case(), stage)):
            return self.rewind(stage, instruction or "re-run requested by the engineer", by="human")
        # The stage has not started yet, so there is nothing to redo: no rewind, no rework event
        # and no extra attempt. Its first session gets the instruction instead.
        if instruction:
            with self.cv:
                self.notes.setdefault(stage, []).append(f"Instruction from the engineer: {instruction}")
        if by_human:
            self.pre_approved = stage            # "run it" from the engineer also answers the gate

    def rewind(self, target, reason, by, from_stage=None):
        """Mark `target` and every later stage stale and restart there."""
        solver.kill_solver(self.cid)
        index = planning.STAGE_KEYS.index(target)
        origin = from_stage or self.case().get("current_stage") or target
        self.store.update_case(self.sid, self.cid, lambda case: mark_stale_from(case, index))
        self.emit("stage.rework", {"from": origin, "to": target, "reason": reason, "by": by},
                  stage=origin, agent="" if by == "human" else None)
        who = "The engineer asked for this stage to be redone" if by == "human" else \
            f"The {origin} stage sent the case back here"
        with self.cv:
            self.notes.setdefault(target, []).append(f"{who}: {reason}")
        if by == "human":
            self.pre_approved = target

    def finish(self, status):
        solver.kill_solver(self.cid)
        self.stage, self.agent_name = None, None
        case = self.case()
        if status == "stopped":
            self.store.update_case(self.sid, self.cid, close_open_stages)
        self.set_status(status)
        if status == "done":
            self.emit("case.completed", {"results": case.get("results"), "reference": case.get("reference"),
                                         "deltas": case.get("deltas"), "verdict": case.get("verdict")}, agent="")
        self.engine.write_metrics(self.sid, self.cid, status, failed_stage=failed_stage(self.case()))
        self.finished = True
        leftovers = list(self.inbox)
        self.inbox.clear()
        self.engine.case_finished(self.sid, self.cid, leftovers)

    # ---------------------------------------------------- scripted policy --

    def call(self, name, **args):
        """One tool call of the scripted policy, through the same path an agent uses."""
        result = self.engine.invoke_tool(self.context(), name, args, None, self)
        if self.drain_messages() and not self.told_scripted:
            self.told_scripted = True
            self.emit("steering.reply", {"text": "Scripted mode has no language model, so the message was recorded "
                                                 "but not acted on. Use Rerun, the plan editor or the mode switch."},
                      agent="scripted")
        return result

    def scripted_stage(self, key):
        """Apply the plan values with the stage's tools, then hand over to the gate."""
        case = self.case()
        plan = self.context().plan_stage(key)
        reasons = plan.get("reasons") or {}
        self.notes.pop(key, None)             # rework reasons are for agents; the policy has fixed remedies

        def decisions(keys):
            return [{"param": k, "value": plan[k], "reason": reasons.get(k, "plan value")} for k in keys if k in plan]

        problems = []

        def need(result, what):
            if not result.get("ok"):
                problems.append(f"{what}: {result.get('error') or result.get('summary')}")
            return result

        if key == "geometry":
            self.call("get_context")
            need(self.call("generate_geometry", naca=case["naca"]), "generate_geometry")
            r = need(self.call("inspect_geometry"), "inspect_geometry")
            summary = f"NACA {case['naca']} profile written. {r.get('summary', '')}"
            decided = [{"param": "naca", "value": case["naca"], "reason": reasons.get("naca", "selected candidate")}]
        elif key == "mesh":
            self.call("estimate_first_cell", target_yplus=planning.DEFAULT_TARGET_YPLUS)   # for the trace only
            need(self.call("set_mesh_params", **{k: plan[k] for k in planning.MESH_KEYS if k in plan}), "set_mesh_params")
            if need(self.call("build_mesh"), "build_mesh").get("ok"):
                r = need(self.call("check_mesh"), "check_mesh")
                self.call("render_view", mode="mesh", zoom="auto")
            else:
                r = {}
            summary = f"C-mesh built from the plan. {r.get('summary', '')}"
            decided = decisions(planning.MESH_KEYS)
        elif key == "physics":
            self.call("get_context")
            need(self.call("set_physics", **{k: plan[k] for k in planning.PHYSICS_KEYS if k in plan}), "set_physics")
            r = need(self.call("inspect_boundary"), "inspect_boundary")
            summary = f"Boundary and initial fields written from the plan. {r.get('summary', '')}"
            decided = decisions(planning.PHYSICS_KEYS)
        elif key == "numerics":
            self.call("get_context")
            values = {k: plan[k] for k in planning.NUMERICS_KEYS if k in plan}
            if self.run_longer:
                self.run_longer = False
                values["iterations"] = min(6000, int(values.get("iterations", 1200) * 1.5))
            r = need(self.call("set_numerics", **values), "set_numerics")
            need(self.call("inspect_numerics"), "inspect_numerics")
            summary = f"Solver set-up written from the plan: {r.get('summary', '')}"
            decided = decisions(planning.NUMERICS_KEYS)
            if values.get("iterations") != plan.get("iterations"):
                decided.append({"param": "iterations", "value": values["iterations"],
                                "reason": "50% more than planned after a fail verdict against XFoil"})
        elif key == "solve":
            summary, decided = self.scripted_solve(plan, problems)
        elif key == "post":
            f = need(self.call("compute_forces"), "compute_forces")
            y = need(self.call("compute_yplus"), "compute_yplus")
            c = need(self.call("extract_cp"), "extract_cp")
            zoom = str(plan.get("zoom") or "airfoil")
            for field in dict.fromkeys([str(plan.get("field") or "U"), "p"]):
                self.call("render_view", mode="field", field=field, zoom=zoom)
            summary = f"{f.get('summary', '')}. {y.get('summary', '')}. {c.get('summary', '')}."
            decided = decisions(["field", "zoom"])
        elif key == "validate":
            need(self.call("run_xfoil", transition="forced", mach=0.0), "run_xfoil")
            self.call("run_xfoil", transition="free", mach=0.0)
            flight = float(plan.get("flight_mach") or 0.0)
            if 0.0 < flight < 0.9:
                self.call("run_xfoil", transition="forced", mach=flight)
            r = need(self.call("compare"), "compare")
            summary = r.get("summary", "comparison with XFoil not available")
            decided = decisions(["transition", "mach"])
            if r.get("verdict") == "fail" and self.case()["counters"].get("rework_loops", 0) < 1:
                # One deterministic remedy before a fail verdict is accepted: run longer.
                d = r.get("deltas") or {}
                self.run_longer = True
                self.call("request_rework", stage="numerics",
                          reason=f"verdict fail against XFoil (dCl {d.get('cl_pct')}%, dCd {d.get('cd_pct')}%): "
                                 "re-run with 50% more iterations to rule out incomplete convergence")
                if self.outcome:
                    return
                self.run_longer = False
        else:
            raise exceptions.AgentError(f"no scripted policy for stage {key}")

        self.call("complete_stage", summary=" ".join(summary.split()), decisions=decided)
        if self.outcome and self.outcome[0] == "failed" and problems:
            self.outcome = ("failed", self.outcome[1] + " (" + "; ".join(problems) + ")")

    def scripted_solve(self, plan, problems):
        extend = int(plan.get("extend_iterations") or 600)
        max_extensions = int(plan.get("max_extensions") if plan.get("max_extensions") is not None else 3)
        decided = [{"param": "extend_iterations", "value": extend,
                    "reason": (plan.get("reasons") or {}).get("extend_iterations", "plan value")}]
        r = self.call("start_solver")
        if not r.get("ok") and "already at iteration" in str(r.get("error")):
            # a re-run of a finished solve: start again from the initial fields
            self.call("reset_fields")
            r = self.call("start_solver")
        if not r.get("ok"):
            problems.append(f"start_solver: {r.get('error')}")
            return "The solver could not be started.", decided
        recovered, extensions, restarts, last = False, 0, 0, {}
        for _ in range(600):                        # 600 waits of 30 s: a five-hour ceiling
            last = self.call("wait_solver", max_wait_s=30)
            state = last.get("state")
            if state == "running":
                continue
            if state == "failed":
                if recovered:
                    problems.append("the solver diverged again with lower relaxation")
                    break
                recovered = True
                self.call("set_numerics", relax_p=0.2, relax_U=0.5, relax_turbulence=0.5)
                self.call("reset_fields")
                decided.append({"param": "relaxation", "value": "p 0.2 / U 0.5 / turbulence 0.5",
                                "reason": "the first run diverged: " + str(last.get("fatal_error"))[:120]})
                if not self.call("start_solver").get("ok"):
                    break
                continue
            if state == "finished":
                if last.get("forces_converged"):
                    break
                it, total = last.get("iteration") or 0, last.get("total") or 0
                if it < total and not last.get("residual_control_converged") and restarts < 3:
                    restarts += 1                   # ended early: continue to the planned length
                    if not self.call("start_solver").get("ok"):
                        break
                    continue
                if extensions < max_extensions:
                    extensions += 1
                    if not self.call("extend_run", iterations=extend).get("ok"):
                        break
                    continue
            break
        if extensions:
            decided.append({"param": "extensions", "value": extensions,
                            "reason": "Cl or Cd still drifted at the planned iteration count"})
        return f"Solver run complete. {last.get('summary', '')}", decided
