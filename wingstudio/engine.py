"""The engine: the study lifecycle and the agent loop.

Study lifecycle (draft -> suggesting -> awaiting_selection -> planning ->
awaiting_approval -> running -> done), one worker thread per case (runtime.CaseRunner),
the generic subagent loop on LiteLLM, plan mode, gate mode, steering at tool
boundaries, rewind, pause / resume / stop, and the metrics row written when a case ends.

The model string "scripted" runs the same workflow with no LLM call: candidates from
mission.scripted_candidates, the default plan of planning, and at every stage the
same tools called with the plan's values."""
import json
import os
import secrets
import threading
import time

from foamtools import docker, llm
from foamtools.common import ROOT, is_finite, read_json

from . import exceptions, runtime
from .agents import spec as agent_spec
from .agents import tools as agent_tools
from .cfd import case as ac
from .cfd import solver
from .store import HELP_KINDS, OUTCOME_COLUMNS, is_trial, now_iso, run_status
from .study import mission, planning, reporting


class Engine:
    def __init__(self, store, viewer_factory=None, completion=None):
        self.store = store
        self._viewer = None
        self._viewer_factory = viewer_factory  # called on first use: ParaView starts only when needed
        self.viewer_error = None
        self.completion = completion          # injectable for tests; default litellm.completion
        self._lock = threading.RLock()
        self.runners = {}                     # case_id -> CaseRunner (latest)
        self._cancelled = set()               # study ids whose study-level agents must stop
        self._reporting = set()
        self._steer_locks = {}
        self.recover()

    # ----------------------------------------------------------- utilities --

    @property
    def viewer(self):
        """The ViewerManager, created on first use (None while it cannot be created)."""
        if self._viewer is None and self._viewer_factory is not None:
            with self._lock:
                if self._viewer is None:
                    try:
                        self._viewer = self._viewer_factory()
                        self.viewer_error = None
                    except Exception as e:  # noqa: BLE001
                        self.viewer_error = f"{type(e).__name__}: {e}"
        return self._viewer

    def emit(self, sid, type, data=None, **kw):
        return self.store.emit(sid, type, data or {}, **kw)

    def study(self, sid):
        study = self.store.get_study(sid)
        if study is None:
            raise exceptions.EngineError(f"no such study: {sid}", 404)
        return study

    def case(self, sid, cid):
        case = self.store.get_case(sid, cid)
        if case is None:
            raise exceptions.EngineError(f"no such case: {cid}", 404)
        return case

    def cancelled(self, sid):
        return sid in self._cancelled

    def spawn(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def set_study_status(self, sid, status, mode=None):
        def apply(s):
            s["status"] = status
            if mode:
                s["mode"] = mode
        study = self.store.update_study(sid, apply)
        self.emit(sid, "study.status", {"status": study["status"], "mode": study["mode"]})
        return study

    def refresh_study_status(self, sid):
        """running <-> paused follows the cases: paused when every live pipeline is paused."""
        study = self.store.get_study(sid)
        if not study or study["status"] not in ("running", "paused"):
            return
        live = [c for c in self.store.list_cases(sid) if c["status"] not in runtime.TERMINAL]
        status = "paused" if live and all(c["status"] == "paused" for c in live) else "running"
        if status != study["status"]:
            self.set_study_status(sid, status)

    def add_usage(self, sid, cid=None, llm_calls=0, tool_calls=0, tokens_in=0, tokens_out=0,
                  tokens_cached=0, cost_usd=0.0):
        def bump(usage, with_tools):
            usage["llm_calls"] = usage.get("llm_calls", 0) + llm_calls
            usage["tokens_in"] = usage.get("tokens_in", 0) + tokens_in
            usage["tokens_out"] = usage.get("tokens_out", 0) + tokens_out
            usage["tokens_cached"] = usage.get("tokens_cached", 0) + tokens_cached
            usage["cost_usd"] = round(usage.get("cost_usd", 0.0) + cost_usd, 6)
            if with_tools:
                usage["tool_calls"] = usage.get("tool_calls", 0) + tool_calls
        if llm_calls or tokens_in or tokens_out or cost_usd:
            self.store.update_study(sid, lambda s: bump(s.setdefault("usage", runtime.zero_usage()), False))
        if cid:
            self.store.update_case(sid, cid, lambda c: bump(c.setdefault("usage", runtime.zero_usage(True)), True))

    def intervention(self, sid, kind, case_ids=()):
        """Record a human intervention on `case_ids`. Every kind counts against first_pass
        (human_interventions); a kind in HELP_KINDS (store.py lists both groups) also counts as
        help, which is what the pass^k criterion `unaided` asks about. The event names the cases."""
        case_ids = [c for c in case_ids if c]
        data = {"kind": kind, "case_ids": case_ids}
        if kind == "retry_approval":
            # Written as the gate decision it also is: readers of the event know that kind.
            # `retry` tells it from the approval to run a stage in gate mode.
            data.update(kind="gate_decision", retry=True)
        self.emit(sid, "human.intervention", data, case_id=case_ids[0] if len(case_ids) == 1 else None)
        helped = kind in HELP_KINDS

        def count(c):
            counters = c["counters"]
            counters["human_interventions"] = counters.get("human_interventions", 0) + 1
            if helped:
                counters["help_interventions"] = counters.get("help_interventions", 0) + 1
        for cid in case_ids:
            self.store.update_case(sid, cid, count)

    def reap_solver_containers(self, case_ids):
        """Kill solver containers of these cases that outlived the server that started them
        (kill -9, crash): nobody watches their log, and a rerun would start a second simpleFoam
        in the same directory. Only these cases' containers (st-<case id>-<n>) are touched, so
        another server on the same Docker VM is left alone. Returns the names killed (none
        when Docker is not reachable)."""
        names = solver.leftover_solver_containers(*case_ids)
        if names:
            docker.cli("kill", *names, timeout=30)
        return names

    def recover(self):
        """After a restart no worker threads exist: close what was in flight."""
        studies = [self.store.get_study(summary["id"]) for summary in self.store.list_studies()]
        open_cases = [c["id"] for study in studies for c in self.store.list_cases(study["id"])
                      if c["status"] not in runtime.TERMINAL]
        reaped = self.reap_solver_containers(open_cases)
        for study in studies:
            sid, status = study["id"], study["status"]
            cases = self.store.list_cases(sid)
            # every case had ended and only the report was still being written
            report_pending = status in ("running", "paused") and bool(cases) and \
                all(c["status"] in runtime.TERMINAL for c in cases)
            for case in cases:
                if case["status"] not in runtime.TERMINAL:
                    def apply(c):
                        c["status"] = "stopped"
                        runtime.close_open_stages(c)
                    self.store.update_case(sid, case["id"], apply)
                    killed = [n for n in reaped if n.startswith(f"st-{case['id']}-")]
                    self.emit(sid, "case.status", {
                        "status": "stopped",
                        "reason": "the server was restarted" + (
                            f"; its solver was still running in {', '.join(killed)} and was stopped" if killed else "")},
                        case_id=case["id"])
                    self.write_metrics(sid, case["id"], "stopped", failed_stage=runtime.failed_stage(case))
            if report_pending:
                # Nothing was lost: close the study the normal way (report, study.completed, final status).
                self.case_finished(sid, None, [])
                continue
            new = {"suggesting": "draft", "planning": "awaiting_selection",
                   "running": "stopped", "paused": "stopped"}.get(status)
            if new == "awaiting_selection" and study.get("plan"):
                new = "awaiting_approval"
            if new:
                self.emit(sid, "error", {"message": f"the server was restarted while the study was {status}"})
                self.set_study_status(sid, new)

    def shutdown(self):
        """Stop every pipeline and solver container (server exit)."""
        for study in self.store.list_studies():
            self._cancelled.add(study["id"])
        for runner in list(self.runners.values()):
            with runner.cv:
                runner.stop_flag = True
                runner.cv.notify_all()
        solver.kill_all_solvers()
        for runner in list(self.runners.values()):
            runner.thread.join(timeout=8)
        if self._viewer is not None:
            try:
                self._viewer.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------ tool execution --

    def invoke_tool(self, ctx, name, raw_args, allowed, hooks):
        """One tool call at a tool boundary: events, phase, execution, accounting."""
        hooks.boundary()
        args = raw_args
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = raw_args
        span = secrets.token_hex(4)
        ctx.emit("tool.call", {"name": name, "args": args if isinstance(args, dict) else {"_raw": str(args)[:500]}},
                 span_id=span)
        kind = agent_tools.tool_kind(name)
        if kind:
            hooks.set_phase(kind)
        tool_ctx = agent_tools.ToolContext(ctx.store, ctx.study_id, ctx.case_id, stage=ctx.stage, agent=ctx.agent,
                                           runtime=ctx.runtime, span_id=span)
        started = time.time()
        result = agent_tools.execute_tool(tool_ctx, name, args, allowed)
        ms = int((time.time() - started) * 1000)
        ctx.emit("tool.result", {"name": name, "ok": bool(result.get("ok")), "ms": ms,
                                 "summary": str(result.get("summary") or "")[:300]},
                 span_id=span, full={"result": result})
        hooks.count_tool()
        return result

    # ----------------------------------------------------- subagent loop --

    _usage = staticmethod(llm.usage)

    def llm_loop(self, spec, model, ctx, allowed, context_text, hooks):
        """The generic subagent loop. Returns why it ended: done | no_tool_calls | max_turns."""
        if not model or model == agent_spec.SCRIPTED:
            raise exceptions.AgentError("no language model is selected for this agent")
        if model.startswith("anthropic/") and self.completion is None and not os.environ.get("ANTHROPIC_API_KEY"):
            raise exceptions.AgentError("ANTHROPIC_API_KEY is not set: put it in .env and restart the server")
        completion = self.completion or llm.completion()
        messages = [{"role": "system", "content": spec.prompt},
                    {"role": "user", "content": context_text}]
        # Prompt caching. Two breakpoints: the stable prefix (tools + system prompt, the same for
        # every call of this agent) and the last message (the growing history, so each turn
        # re-reads everything before it at the cached price). STUDIO_CACHE_TTL=1h keeps the
        # prefix for an hour instead of five minutes: it costs more to write (2x the input
        # price instead of 1.25x) and pays off when another study or a re-run with the same
        # model starts within the hour. The history breakpoint stays at five minutes (a
        # longer-lived entry must come first in the prompt, which the prefix does).
        prefix_point = {"location": "message", "role": "system"}
        if os.environ.get("STUDIO_CACHE_TTL", "").strip().lower() == "1h":
            prefix_point["control"] = {"type": "ephemeral", "ttl": "1h"}
        effort = spec.effort or agent_spec.DEFAULT_EFFORT
        kwargs = dict(
            model=model, messages=messages, tools=agent_tools.tool_schemas(allowed), max_tokens=16000,
            num_retries=3, cache_control_injection_points=[prefix_point, {"location": "message", "index": -1}],
        )
        if effort != "none":
            kwargs["reasoning_effort"] = effort
        kwargs["timeout"] = agent_spec.LLM_TIMEOUT_S.get(effort, agent_spec.DEFAULT_LLM_TIMEOUT_S)
        nudges = 0
        for turn in range(spec.max_turns):
            last_turn = turn == spec.max_turns - 1
            hooks.boundary()
            started = time.time()
            try:
                response = completion(**kwargs)
            except Exception as e:  # noqa: BLE001
                raise exceptions.AgentError(f"the language model call failed ({model}): {type(e).__name__}: "
                                            + " ".join(str(e).split())[:400])
            ms = int((time.time() - started) * 1000)
            choice = response.choices[0]
            msg = choice.message
            tokens_in, tokens_out, cached, cost = self._usage(response, model)
            ctx.emit("llm.call", {"model": model, "ms": ms, "tokens_in": tokens_in, "tokens_out": tokens_out,
                                  "tokens_cached": cached, "cost_usd": round(cost, 6),
                                  "finish_reason": choice.finish_reason})
            hooks.add_usage(llm_calls=1, tokens_in=tokens_in, tokens_out=tokens_out,
                            tokens_cached=cached, cost_usd=cost)
            # Keep the assistant message whole (including provider reasoning
            # blocks) so the next request replays it unchanged.
            entry = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
            entry.setdefault("role", "assistant")
            messages.append(entry)
            text = (getattr(msg, "content", None) or "")
            text = text.strip() if isinstance(text, str) else ""
            calls = getattr(msg, "tool_calls", None) or []
            # A steering agent that has already answered through reply() tends to repeat the
            # answer as its closing text; the engineer should see it once.
            if text and not (getattr(hooks, "replied", False) and not calls):
                ctx.emit("agent.message", {"text": text})
                if hasattr(hooks, "last_text"):
                    hooks.last_text = text
            if not calls:
                pending = [] if last_turn else hooks.drain_messages()
                if pending:
                    messages.append({"role": "user", "content": "\n".join(f"HUMAN MESSAGE: {m}" for m in pending)})
                    continue
                if hooks.done():
                    return "done"
                nudge = hooks.nudge()
                if nudge and nudges < 2 and choice.finish_reason != "length":
                    nudges += 1
                    messages.append({"role": "user", "content": nudge})
                    continue
                return "no_tool_calls"
            finished = False
            for call in calls:
                if finished:
                    # The stage is already handed over; later calls of the same turn are not run.
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": '{"ok": false, "error": "not run: the stage was already completed"}'})
                    continue
                result = self.invoke_tool(ctx, call.function.name, call.function.arguments, allowed, hooks)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": agent_tools.result_text(result)})
                finished = hooks.done()
            if finished:
                return "done"
            if last_turn:
                break
            # Human messages are delivered only when another model call follows to read them:
            # one that arrives during the agent's last turn stays queued for whoever comes next
            # (the steering agent), instead of being marked delivered and thrown away.
            human = hooks.drain_messages()
            if human:
                messages[-1]["content"] += "\n\n" + "\n".join(f"HUMAN MESSAGE: {m}" for m in human)
        return "max_turns"

    # ---------------------------------------------------------- lifecycle --

    def create_study(self, project_id, prompt, requirements, model, mode=None):
        project = self.store.get_project(project_id or self.store.default_project_id)
        if project is None:
            raise exceptions.EngineError(f"no such project: {project_id}", 404)
        if model is not None and not isinstance(model, str):
            raise exceptions.EngineError("model must be a string")
        model = agent_spec.MODEL_ALIASES.get(model, model) or agent_spec.MODELS[0]
        if model not in agent_spec.MODELS:
            # an allow-list: any other string would be handed to LiteLLM, with the server's key
            raise exceptions.EngineError("model must be one of " + ", ".join(agent_spec.MODELS + sorted(agent_spec.MODEL_ALIASES)))
        if mode not in (None, "", "plan", "gate", "auto"):
            raise exceptions.EngineError("mode must be plan, gate or auto")
        req = mission.normalise_requirements(requirements, prompt)
        cond = mission.derive_conditions(req)
        now = now_iso()
        study = {
            "id": self.store.new_study_id(), "project_id": project["id"],
            "title": mission.study_title(req, prompt), "created_at": now, "updated_at": now,
            "model": model, "mode": mode or "plan", "status": "draft",
            "prompt": prompt or "", "requirements": req, "conditions": cond,
            "candidates": [], "plan": None, "case_ids": [], "report": None, "usage": runtime.zero_usage(),
        }
        self.store.create_study(study)
        sid = study["id"]
        self.emit(sid, "study.status", {"status": "draft", "mode": study["mode"]})
        self.emit(sid, "requirements.parsed", {"requirements": req, "conditions": cond})
        return self.store.get_study(sid)

    def suggest(self, sid):
        with self._lock:
            study = self.study(sid)
            restartable = study["status"] == "stopped" and not study.get("case_ids")
            if study["status"] not in ("draft", "awaiting_selection") and not restartable:
                raise exceptions.EngineError(f"cannot suggest candidates while the study is {study['status']}", 409)
            self._cancelled.discard(sid)
            self.set_study_status(sid, "suggesting")
        self.spawn(self._suggest_worker, sid)

    def _study_agent(self, sid, file_name, rt, context_text):
        study = self.store.get_study(sid)
        spec = agent_spec.load_agent(file_name)
        rt.agent_name = spec.name
        allowed = [t for t in spec.tools if t in agent_tools.TOOLS] or agent_tools.STUDY_TOOLS[file_name]
        return self.llm_loop(spec, agent_spec.resolve_model(spec.model, study["model"]), rt.context(), allowed,
                             context_text, rt)

    def _study_context(self, study, extra=()):
        lines = ["## The engineer's mission", study.get("prompt") or "(no free text; see the requirements)",
                 "", "## Requirements", agent_spec.compact(study["requirements"]),
                 "", "## Derived flow conditions", agent_spec.compact(agent_spec.rounded_conditions(study["conditions"]))]
        return "\n".join(lines + list(extra))

    def _suggest_worker(self, sid):
        rt = runtime.StudyRuntime(self, sid, "airfoil-advisor")
        try:
            study = self.store.get_study(sid)
            if study["model"] == agent_spec.SCRIPTED:
                rt.agent_name = "scripted"
                ctx = rt.context()
                proposals = mission.scripted_candidates(study["requirements"])
                screen = self.invoke_tool(ctx, "xfoil_screen", {"nacas": [p["naca"] for p in proposals],
                                                                "design_cl": study["requirements"]["design_cl"]},
                                          None, rt)
                if not screen.get("ok"):
                    raise exceptions.AgentError("XFoil screening failed: " + str(screen.get("error")))
                result = self.invoke_tool(ctx, "propose_candidates", {"candidates": proposals, "top": []}, None, rt)
                if not result.get("ok"):
                    raise exceptions.AgentError(str(result.get("error")))
            else:
                ended = self._study_agent(sid, "airfoil-advisor.md", rt, self._study_context(study, [
                    "", "Propose four to six NACA 4-digit candidates, screen them with xfoil_screen, then call "
                        "propose_candidates with your reasons and the two you recommend."]))
                if rt.outcome is None:
                    raise exceptions.AgentError(f"the advisor ended ({ended}) without proposing candidates")
            self.set_study_status(sid, "awaiting_selection")
        except exceptions.Interrupt:
            self.set_study_status(sid, "stopped")
        except Exception as e:  # noqa: BLE001
            self.emit(sid, "error", {"message": f"candidate suggestion failed: {e}"}, agent=rt.agent_name)
            self.set_study_status(sid, "draft")

    def select(self, sid, nacas):
        def selectable():
            study = self.study(sid)
            if study["status"] not in ("awaiting_selection", "awaiting_approval"):
                raise exceptions.EngineError(f"cannot select airfoils while the study is {study['status']}", 409)
            return study

        with self._lock:
            study = selectable()
            codes = []
            for n in nacas or []:
                try:
                    code = ac.naca4(n)[0]
                except (ValueError, AttributeError):
                    raise exceptions.EngineError(f"{n!r} is not a NACA 4-digit code")
                if code not in codes:
                    codes.append(code)
            if not (1 <= len(codes) <= mission.MAX_SELECTED):
                raise exceptions.EngineError(f"select between 1 and {mission.MAX_SELECTED} airfoils")
            new = [c for c in codes if c not in [k["naca"] for k in study.get("candidates") or []]]
        # XFoil for airfoils that were not candidates runs outside the engine lock (it can take
        # a minute per section, and Stop, pause, approve and messages of every study need the lock).
        screen = runtime.StudyRuntime(self, sid, "airfoil-advisor").screened(new) if new else {}
        with self._lock:
            study = selectable()                  # the study may have moved on while XFoil ran
            candidates = study.get("candidates") or []
            known = {c["naca"]: c for c in candidates}
            added = [c for c in codes if c not in known]
            for code in added:
                if code not in screen:
                    raise exceptions.EngineError("the candidates changed while the selection was being screened; "
                                                 "select again", 409)
                known[code] = mission.candidate_entry(code, "Added by the engineer.", screen[code], study["requirements"])
                known[code]["rank"] = len(known)
                candidates.append(known[code])
            for code in codes:
                xf = known[code].get("xfoil") or {}
                if not xf.get("converged") or xf.get("alpha_design") is None:
                    raise exceptions.EngineError(f"NACA {code} cannot be run: XFoil did not reach the design lift "
                                                 f"coefficient ({xf.get('error') or 'not converged'})")
            for c in candidates:
                c["selected"] = c["naca"] in codes

            def apply(s):
                s["candidates"] = candidates
                # A plan written for other airfoils must not stay approvable if the planner
                # fails or the server restarts before the new plan exists.
                if s.get("plan") and not planning.plan_matches_selection(s):
                    s["plan"] = None
            self.store.update_study(sid, apply)
            if added:
                self.emit(sid, "candidates.proposed", {"candidates": candidates})
            self._cancelled.discard(sid)
            self.set_study_status(sid, "planning")
        self.spawn(self._plan_worker, sid, None)

    def store_plan(self, sid, plan, agent=None, span=None, keep_approval=False, effects=None):
        """Save a plan as the next version and announce it. Returns the version.
        `effects` (a dict) receives case id -> "rerun" | "stale" for every case that was sent
        back to the physics stage because its angle of attack changed."""
        def apply(s):
            old = s.get("plan") or {}
            plan["version"] = int(old.get("version") or 0) + 1
            plan["approved_at"] = old.get("approved_at") if keep_approval else None
            s["plan"] = plan
        study = self.store.update_study(sid, apply)
        version = study["plan"]["version"]
        # A changed angle of attack reaches the case record here. Before the physics stage has
        # run that is all; afterwards the flow on disk is at the old angle, so the case goes back
        # to the physics stage (see alpha_changed).
        changed = []
        for pc in study["plan"]["cases"]:
            cid = f"{sid}-{pc['naca']}"
            case = self.store.get_case(sid, cid)
            if case is None or pc.get("alpha_deg") is None:
                continue
            alpha = float(pc["alpha_deg"])
            if case.get("alpha_deg") != alpha:
                changed.append((cid, case.get("alpha_deg"), alpha))
                self.store.update_case(sid, cid, lambda c, a=alpha: c.__setitem__("alpha_deg", a))
        self.emit(sid, "plan.created" if version == 1 else "plan.updated", {"version": version},
                  agent=agent, parent_span_id=span)
        for cid, old, new in changed:
            effect = self.alpha_changed(sid, cid, old, new)
            if effect and effects is not None:
                effects[cid] = effect
        return version

    def alpha_changed(self, sid, cid, old, new):
        """The angle of attack of a case was changed in the plan. If its physics stage has
        already run, everything from there on was computed at the old angle: a running or
        finished case is re-run from physics (as the engineer's rerun would), a stopped or
        failed one is only marked stale and re-runs from physics when it is started again.
        Returns "rerun", "stale" or None."""
        case = self.store.get_case(sid, cid)
        if runtime.find_stage(case, "physics")["status"] not in ("passed", "running"):
            return None                           # physics has not run yet (or will run again anyway)
        reason = (f"the angle of attack was changed from {old:g} to {new:g} deg in the plan: the boundary "
                  "conditions, the solution and the comparison must be redone at the new angle")
        if self._alive(cid) or case["status"] == "done":
            try:
                self.rerun(sid, cid, "physics", reason, by_human=True)
                return "rerun"
            except exceptions.EngineError:
                pass                              # the pipeline is just ending (Stop): mark only
        index = planning.STAGE_KEYS.index("physics")
        self.store.update_case(sid, cid, lambda c: runtime.mark_stale_from(c, index))
        self.emit(sid, "stage.rework", {"from": "physics", "to": "physics", "reason": reason + " (when the case "
                                        "is run again)", "by": "human"}, case_id=cid, stage="physics")
        self.intervention(sid, "rerun", [cid])
        return "stale"

    def _plan_worker(self, sid, comment):
        rt = runtime.StudyRuntime(self, sid, "planner")
        try:
            study = self.store.get_study(sid)
            if study["model"] == agent_spec.SCRIPTED:
                rt.agent_name = "scripted"
                ctx = rt.context()
                if comment and study.get("plan"):
                    self.emit(sid, "steering.reply", {
                        "text": "Scripted mode has no language model, so the comment cannot be turned into plan "
                                "changes. Edit the values in the plan directly."}, agent="scripted")
                else:
                    defaults = self.invoke_tool(ctx, "get_defaults", {}, None, rt)
                    result = self.invoke_tool(ctx, "write_plan", {"plan": defaults.get("plan")}, None, rt)
                    if not result.get("ok"):
                        raise exceptions.AgentError(str(result.get("error")))
            else:
                selected = [{"naca": c["naca"], "rationale": c.get("rationale"),
                             "xfoil": {k: v for k, v in (c.get("xfoil") or {}).items() if k != "polar"}}
                            for c in study["candidates"] if c.get("selected")]
                extra = ["", "## Selected airfoils (with the XFoil screen)", agent_spec.compact(selected)]
                if comment and study.get("plan"):
                    extra += ["", "## Current plan (to be revised)", agent_spec.compact(study["plan"], 40000),
                              "", "## The engineer's comment", f"HUMAN MESSAGE: {comment}",
                              "", "Revise the plan as the comment asks and call write_plan with the complete plan."]
                else:
                    extra += ["", "Call get_defaults, adapt the plan where the mission needs it, and call write_plan."]
                ended = self._study_agent(sid, "planner.md", rt, self._study_context(study, extra))
                if rt.outcome is None:
                    raise exceptions.AgentError(f"the planner ended ({ended}) without writing a plan")
            self.set_study_status(sid, "awaiting_approval")
        except exceptions.Interrupt:
            self.set_study_status(sid, "stopped")
        except Exception as e:  # noqa: BLE001
            self.emit(sid, "error", {"message": f"planning failed: {e}"}, agent=rt.agent_name)
            study = self.store.get_study(sid)
            self.set_study_status(sid, "awaiting_approval" if study.get("plan") else "awaiting_selection")

    def patch_plan(self, sid, plan):
        with self._lock:
            study = self.study(sid)
            if not study.get("plan"):
                raise exceptions.EngineError("there is no plan to edit yet", 409)
            if study["status"] in ("planning", "suggesting"):
                raise exceptions.EngineError(f"the plan cannot be edited while the study is {study['status']}", 409)
            if not isinstance(plan, dict):
                raise exceptions.EngineError("plan must be an object")
            # cites in the request are ignored: the stored ones are carried over (normalise_plan)
            normalised, _ = planning.normalise_plan(study, plan, keep_alpha=True, cites_from=study["plan"])
            errors = planning.validate_plan(normalised, cites=False)
            if errors:
                raise exceptions.EngineError("; ".join(errors))
            planning.mark_engineer_edits(study["plan"], normalised)
            # The edit is help for the cases it concerns: those that have not ended (read before
            # the plan is stored: a changed angle starts a finished case again) and whose plan it
            # changed. An ended case keeps its row, and an untouched one was not helped.
            changed = planning.plan_changes(study["plan"], normalised)
            helped = [c["id"] for c in self.store.list_cases(sid)
                      if c["status"] not in runtime.TERMINAL and c["naca"] in changed]
            # Once the cases exist the plan cannot be approved a second time: an edit during or
            # after the run keeps the approval, and only an edit before it asks for approval again.
            self.store_plan(sid, normalised, keep_approval=bool(study.get("case_ids")))
            self.intervention(sid, "plan_edit", helped)
            return self.store.get_study(sid)

    def revise_plan(self, sid, comment):
        with self._lock:
            study = self.study(sid)
            if study["status"] != "awaiting_approval":
                raise exceptions.EngineError(f"the plan can only be revised while awaiting approval (now {study['status']})", 409)
            comment = str(comment or "").strip()
            if not comment:
                raise exceptions.EngineError("the comment is empty")
            self.emit(sid, "human.message", {"text": comment, "target_stage": "plan", "delivered_to": "planner"})
            self._cancelled.discard(sid)
            self.set_study_status(sid, "planning")
        self.spawn(self._plan_worker, sid, comment)

    def approve(self, sid, mode=None):
        with self._lock:
            study = self.study(sid)
            if study["status"] != "awaiting_approval" or not study.get("plan"):
                raise exceptions.EngineError(f"there is no plan waiting for approval (study is {study['status']})", 409)
            mode = mode or (study["mode"] if study["mode"] in ("auto", "gate") else "auto")
            if mode not in ("auto", "gate"):
                raise exceptions.EngineError("mode must be auto or gate")
            if not planning.plan_matches_selection(study):
                raise exceptions.EngineError("the plan was written for other airfoils than the ones selected now; "
                                             "confirm the selection again to get a new plan", 409)
            errors = planning.validate_plan(study["plan"], cites=False)
            if errors:
                raise exceptions.EngineError("the plan cannot be run: " + "; ".join(errors))
            cases = []
            for pc in study["plan"]["cases"]:
                cid = f"{sid}-{pc['naca']}"
                cases.append({
                    "id": cid, "study_id": sid, "naca": pc["naca"], "alpha_deg": float(pc["alpha_deg"]),
                    "dir": f"cases/st_{sid}_{pc['naca']}", "status": "pending", "current_stage": None,
                    "stages": [runtime.new_stage(k) for k in planning.STAGE_KEYS],
                    "results": None, "reference": None, "deltas": None, "verdict": None, "viewer": None,
                    "usage": runtime.zero_usage(True),
                    "counters": {"rework_loops": 0, "gate_failures": 0, "human_interventions": 0,
                                 "help_interventions": 0},
                    "created_at": now_iso(), "started_at": now_iso(), "ended_at": None,
                })

            def apply(s):
                s["plan"]["approved_at"] = now_iso()
                s["case_ids"] = [c["id"] for c in cases]
                s["report"] = None
            self.store.update_study(sid, apply)
            self.emit(sid, "plan.approved", {"version": study["plan"]["version"], "mode": mode})
            for case in cases:
                self.store.create_case(case)
                self.emit(sid, "case.created", {"case": case}, case_id=case["id"])
            self._cancelled.discard(sid)
            self.set_study_status(sid, "running", mode=mode)
            for case in cases:
                runner = runtime.CaseRunner(self, sid, case["id"])
                self.runners[case["id"]] = runner
                runner.start()

    def set_mode(self, sid, mode, by_human=True):
        if mode not in ("auto", "gate"):
            raise exceptions.EngineError("mode must be auto or gate")
        with self._lock:
            study = self.study(sid)
            self.store.update_study(sid, lambda s: s.__setitem__("mode", mode))
            self.emit(sid, "study.status", {"status": study["status"], "mode": mode})
            live = [cid for cid in study.get("case_ids") or [] if self._alive(cid)]
            if by_human:
                self.intervention(sid, "mode_change", live)
            for cid in live:
                runner = self.runners[cid]
                with runner.cv:
                    if mode == "auto":
                        runner.force_gate = False
                    runner.cv.notify_all()
            return self.store.get_study(sid)

    def _alive(self, cid):
        runner = self.runners.get(cid)
        return runner is not None and runner.alive()

    # ---------------------------------------------------- human controls --

    def control(self, sid, action, case_id=None):
        if action not in ("pause", "resume", "stop"):
            raise exceptions.EngineError("action must be pause, resume or stop")
        with self._lock:
            study = self.study(sid)
            if case_id:
                self.case(sid, case_id)
            targets = [case_id] if case_id else list(study.get("case_ids") or [])
            live = [cid for cid in targets if self._alive(cid)]
            self.intervention(sid, action, live)
            if action in ("pause", "resume"):
                for cid in live:
                    runner = self.runners[cid]
                    with runner.cv:
                        runner.paused = action == "pause"
                        runner.cv.notify_all()
                self.refresh_study_status(sid)
                return
            # stop
            if not case_id:
                self._cancelled.add(sid)
            for cid in live:
                runner = self.runners[cid]
                with runner.cv:
                    runner.stop_flag = True
                    runner.cv.notify_all()
            for cid in targets:
                if cid in live:
                    continue
                case = self.store.get_case(sid, cid)
                if case and case["status"] not in runtime.TERMINAL:
                    self.store.update_case(sid, cid, lambda c: c.__setitem__("status", "stopped"))
                    self.emit(sid, "case.status", {"status": "stopped"}, case_id=cid)
                    self.write_metrics(sid, cid, "stopped", failed_stage=runtime.failed_stage(case))
            # a study-level worker (suggesting, planning) sees the cancel flag itself
            if not live and (targets or study["status"] not in ("suggesting", "planning")):
                self.case_finished(sid, None, [])
        # Killing a solver container takes seconds: do it after the engine lock is released, so
        # that other requests (and the pipelines that are finishing) are not held up meanwhile.
        for cid in live:
            solver.kill_solver(cid)

    def approve_case(self, sid, cid):
        self.case(sid, cid)
        runner = self.runners.get(cid)
        if runner is None or not runner.alive() or runner.waiting is None:
            raise exceptions.EngineError("this case is not waiting for approval", 409)
        with runner.cv:
            stage, retry = runner.waiting, runner.waiting_retry
        if stage is None:
            raise exceptions.EngineError("this case is not waiting for approval", 409)
        self.emit(sid, "approval.granted", {"stage": stage}, case_id=cid, stage=stage)
        # Letting a failed stage try again helps the case; the approval to run a stage does not.
        self.intervention(sid, "retry_approval" if retry else "gate_decision", [cid])
        with runner.cv:
            runner.approved = True
            runner.cv.notify_all()

    def rerun(self, sid, cid, stage, instruction=None, by_human=True):
        """Re-run a case from `stage`. Returns what happened: "queued" (the live pipeline takes
        the request at its next tool boundary), "restarted" (a new pipeline was started) or
        "noted" (the stage has not run yet and is waiting for approval: the instruction is kept
        for its first run and nothing is re-run; only for requests that are not the engineer's).
        A re-run that happens counts as help for the case, also when the steering agent asks."""
        if stage not in planning.STAGE_KEYS:
            raise exceptions.EngineError(f"stage must be one of {', '.join(planning.STAGE_KEYS)}")
        with self._lock:
            study = self.study(sid)
            case = self.case(sid, cid)
            target = runtime.find_stage(case, stage)
            if target["status"] == "pending" and case.get("current_stage") != stage:
                raise exceptions.EngineError(f"stage {stage} has not run yet", 409)
            instruction = str(instruction or "").strip()
            runner = self.runners.get(cid)
            if runner is not None and runner.alive():
                with runner.cv:
                    # A pipeline that is stopping or in its last step would never look at the
                    # request: refuse it instead of answering ok and dropping it.
                    if runner.stop_flag:
                        raise exceptions.EngineError("the case is still stopping; try again in a moment", 409)
                    if runner.closing:
                        raise exceptions.EngineError("the case is just finishing; try again in a moment", 409)
                    if not by_human and runner.waiting == stage and runtime.never_started(target):
                        # The steering agent asks to "re-run" a stage that has not started and
                        # waits for the engineer's approval: nothing to redo, and the approval
                        # stays the engineer's. Keep the instruction for the stage's first run.
                        if instruction:
                            runner.notes.setdefault(stage, []).append(f"Instruction from the engineer: {instruction}")
                        return "noted"
                    # Help for the case whoever asks: the steering agent (not by_human) only
                    # acts on a message from the engineer.
                    self.intervention(sid, "rerun", [cid])
                    runner.rerun_request = (stage, instruction, by_human)
                    runner.cv.notify_all()
                return "queued"
            self.intervention(sid, "rerun", [cid])        # as above, also for a case that had ended
            # The pipeline has ended: start a new one at that stage.
            self.store.update_case(sid, cid, lambda c: c.update(status="pending", ended_at=None))
            self.store.update_study(sid, lambda s: s.__setitem__("report", None))
            self._cancelled.discard(sid)
            self._reporting.discard(sid)
            if study["status"] != "running":
                self.set_study_status(sid, "running")
            runner = runtime.CaseRunner(self, sid, cid, rerun=(stage, instruction, by_human))
            self.runners[cid] = runner
            runner.start()
            return "restarted"

    def message(self, sid, text, case_id=None, stage=None, quote=None):
        """Queue a human message. It is delivered to the active stage agent at
        its next tool boundary; otherwise the steering agent handles it."""
        text = str(text or "").strip()
        if not text:
            raise exceptions.EngineError("the message is empty")
        with self._lock:
            study = self.study(sid)
            # Before approval there is nothing to steer: a comment on the plan goes to the
            # planner, which rewrites the plan (the same path as "Keep planning").
            if (not case_id and study["status"] == "awaiting_approval" and study.get("plan")
                    and not study.get("case_ids") and study["model"] != agent_spec.SCRIPTED):
                comment = text if not quote else f'{text}\n(The engineer selected this text: "{quote}")'
                self.intervention(sid, "steer")
                self.revise_plan(sid, comment)
                return {"queued": True, "id": secrets.token_hex(4), "delivered_to": "planner"}
            if case_id:
                self.case(sid, case_id)
            msg = {"id": secrets.token_hex(4), "text": text, "quote": quote or None, "target_stage": stage or None}
            targets = [case_id] if case_id else [cid for cid in study.get("case_ids") or [] if self._alive(cid)]
            self.intervention(sid, "steer", targets)
            steer = []
            for cid in targets:
                runner = self.runners.get(cid)
                if runner is not None and runner.alive() and runner.agent_active:
                    runner.inbox.append(dict(msg))
                else:
                    steer.append(cid)
            # A study-wide message that some stage agents received directly goes to the steering
            # agent only for the other cases, and it is told (and held to) exactly those.
            if not targets or len(steer) == len(targets):
                self.spawn(self._steer_worker, sid, case_id or None, msg)
            elif len(steer) == 1:
                self.spawn(self._steer_worker, sid, steer[0], msg)
            elif steer:
                self.spawn(self._steer_worker, sid, None, msg, steer)
        return {"queued": True, "id": msg["id"]}

    # ------------------------------------------------------------ steering --

    def _steer_worker(self, sid, case_id, msg, only=None):
        """Run the steering agent on one human message. `case_id` is the case it is addressed
        to (None: the whole study); `only` narrows a study-wide message to the cases whose
        stage agents did not receive it themselves."""
        lock = self._steer_locks.setdefault(sid, threading.Lock())
        with lock:
            rt = runtime.StudyRuntime(self, sid, "steering", case_id=case_id)
            rt.scope = list(only) if only else None
            try:
                study = self.store.get_study(sid)
                self.emit(sid, "human.message", {"text": msg["text"], "quote": msg.get("quote"),
                                                 "target_stage": msg.get("target_stage"), "id": msg.get("id"),
                                                 "delivered_to": "steering"}, case_id=case_id)
                if study["model"] == agent_spec.SCRIPTED:
                    rt.agent_name = "scripted"
                    rt.reply("Scripted mode has no language model, so I cannot interpret the message. It is recorded "
                             "in the trace. Use Rerun on a stage, edit the plan values, or switch the mode.")
                    return
                cases = self.store.list_cases(sid)
                overview = [{"case_id": c["id"], "naca": c["naca"], "alpha_deg": c["alpha_deg"], "status": c["status"],
                             "current_stage": c.get("current_stage"), "verdict": c.get("verdict"),
                             "results": c.get("results"), "deltas": c.get("deltas"), "counters": c.get("counters"),
                             "stages": [{"key": s["key"], "status": s["status"], "headline": s.get("headline"),
                                         "failure": s.get("failure")} for s in c["stages"]]} for c in cases]
                plan = study.get("plan") or {}
                extra = ["", f"## Study state\nstatus: {study['status']}, mode: {study['mode']}",
                         "", "## Cases", agent_spec.compact(overview, 8000),
                         "", "## Plan", agent_spec.compact({"version": plan.get("version"),
                                                            "cases": agent_spec.uncited(plan.get("cases"))},
                                                           agent_spec.STEERING_PLAN_CHARS),
                         "", "## The engineer's message",
                         (f"Addressed to case {case_id}. " if case_id else
                          f"Addressed to the whole study; the stage agents of the other cases have received it "
                          f"themselves, so act only on {', '.join(only)}. " if only else
                          "Addressed to the whole study. ")
                         + "HUMAN MESSAGE: " + agent_spec.format_message(msg),
                         "", "Act on it with your tools if needed, and always finish with reply(text)."]
                self._study_agent(sid, "steering.md", rt, self._study_context(study, extra))
                if not rt.replied:
                    rt.reply(rt.last_text or "Message received.")
            except Exception as e:  # noqa: BLE001
                self.emit(sid, "error", {"message": f"the steering agent failed: {e}"}, case_id=case_id,
                          agent="steering")

    # ----------------------------------------------------- case end, metrics --

    def write_metrics(self, sid, cid, status, failed_stage=None):
        """One row per case in the runs table (replaced if the case ends again). `status` is how
        the pipeline ended; the row's own `status` comes from store.run_status.

        The row holds the last outcome the case reached: an end that is no outcome (stopped, a
        restart, a crash) during a re-run or a retry keeps the outcome columns of the stored
        row. What the case cost (counters, tokens, calls, times) is always that of the case now."""
        try:
            study, case = self.store.get_study(sid), self.store.get_case(sid, cid)
            project = self.store.get_project(study["project_id"]) or {}
            req = study["requirements"]
            r, ref, d = case.get("results") or {}, case.get("reference") or {}, case.get("deltas") or {}
            usage, counters = case.get("usage") or {}, case.get("counters") or {}
            verdict = case.get("verdict")
            success = status == "done" and verdict in ("pass", "warn")
            residuals = [v for v in (r.get("residuals") or {}).values() if is_finite(v)]
            solver_state = read_json(os.path.join(ROOT, case["dir"], "solver_state.json")) or {}
            row = {
                "id": cid, "ts": now_iso(), "project_id": study["project_id"], "project_name": project.get("name"),
                "study_id": sid, "study_title": study.get("title"), "case_id": cid,
                "model": study["model"], "mode": study["mode"], "naca": case["naca"], "alpha_deg": case["alpha_deg"],
                "mach": req["mach"], "reynolds": req["reynolds"], "density": req["density"],
                "design_cl": req["design_cl"], "objective": req["objective"],
                "cells": r.get("cells"), "yplus_avg": r.get("yplus_avg"), "iterations": r.get("iterations"),
                "solver_seconds": r.get("solver_seconds") or solver_state.get("seconds_total"),
                "wall_seconds": round(sum(s.get("seconds") or 0.0 for s in case["stages"]), 1),
                "solver_converged": solver_state.get("state") == "finished" if solver_state else None,
                "forces_converged": r.get("forces_converged"),
                "residual_max": max(residuals) if residuals else None,
                "cl": r.get("cl"), "cd": r.get("cd"), "cm": r.get("cm"), "ld": r.get("ld"),
                "xfoil_cl": ref.get("cl"), "xfoil_cd": ref.get("cd"), "xfoil_cm": ref.get("cm"), "xfoil_ld": ref.get("ld"),
                "cl_err_pct": d.get("cl_pct"), "cd_err_pct": d.get("cd_pct"), "cm_err_abs": d.get("cm_abs"),
                "verdict": verdict, "success": success,
                "llm_calls": usage.get("llm_calls", 0), "tool_calls": usage.get("tool_calls", 0),
                "tokens_in": usage.get("tokens_in", 0), "tokens_out": usage.get("tokens_out", 0),
                "tokens_cached": usage.get("tokens_cached"), "cost_usd": usage.get("cost_usd", 0.0),
                "rework_loops": counters.get("rework_loops", 0), "gate_failures": counters.get("gate_failures", 0),
                "human_interventions": counters.get("human_interventions", 0),
                "stage_seconds": {s["key"]: s.get("seconds") or 0.0 for s in case["stages"]},
                "stage_attempts": {s["key"]: s.get("attempts") or 0 for s in case["stages"]},
                "failed_stage": failed_stage,
                "status": run_status(status, failed_stage),
                # help is a part of the actions: never more
                "help_interventions": min(counters.get("help_interventions", 0), counters.get("human_interventions", 0)),
            }
            if row["status"] == "stopped":
                earlier = self.store.get_run(cid)
                if earlier and is_trial(earlier):
                    row.update({name: earlier.get(name) for name in OUTCOME_COLUMNS})
                    row["status"] = earlier.get("status") or ("failed" if earlier.get("failed_stage") else "done")
            row["first_pass"] = bool(row["success"] and not counters.get("rework_loops")
                                     and not counters.get("gate_failures") and not counters.get("human_interventions"))
            self.store.record_run(row)
            if status in runtime.TERMINAL:
                self.store.update_case(sid, cid, lambda c: c.__setitem__("ended_at", now_iso()))
        except Exception as e:  # noqa: BLE001 - metrics must never break a run
            self.emit(sid, "error", {"message": f"could not write the metrics row: {type(e).__name__}: {e}"},
                      case_id=cid)

    def case_finished(self, sid, cid, leftovers):
        """Called when a pipeline ends: hand undelivered messages to the steering
        agent, and close the study when every case has ended."""
        for msg in leftovers or []:
            self.spawn(self._steer_worker, sid, cid, msg)
        with self._lock:
            study = self.store.get_study(sid)
            cases = self.store.list_cases(sid)
            if study["status"] not in ("running", "paused"):
                if not cases and sid in self._cancelled:
                    self.set_study_status(sid, "stopped")
                return
            if not cases or any(c["status"] not in runtime.TERMINAL for c in cases):
                return
            if sid in self._reporting:
                return
            self._reporting.add(sid)
        self.spawn(self._finish_study, sid)

    def _finish_study(self, sid):
        try:
            study = self.store.get_study(sid)
            cases = self.store.list_cases(sid)
            done = [c for c in cases if c["status"] == "done"]
            report = self._write_report(sid, study, cases) if done else None
            status = "done" if done else ("failed" if any(c["status"] == "failed" for c in cases) else "stopped")
            if any(c["status"] not in runtime.TERMINAL for c in self.store.list_cases(sid)):
                return                        # a case was re-run while the report was written
            self.store.update_study(sid, lambda s: s.__setitem__("report", report))
            self.emit(sid, "study.completed", {"report": report})
            self.set_study_status(sid, status)
        finally:
            self._reporting.discard(sid)

    def _write_report(self, sid, study, cases):
        if study["model"] != agent_spec.SCRIPTED and not self.cancelled(sid):
            rt = runtime.StudyRuntime(self, sid, "reporter")
            try:
                self._study_agent(sid, "reporter.md", rt, self._study_context(study, [
                    "", "Every case has ended. Call get_results, then write_report(markdown, recommendation)."]))
                if rt.outcome is not None:
                    return self.store.get_study(sid).get("report")
                self.emit(sid, "error", {"message": "the reporter ended without a report; the built-in report is used"},
                          agent="reporter")
            except Exception as e:  # noqa: BLE001
                self.emit(sid, "error", {"message": f"the reporter failed ({e}); the built-in report is used"},
                          agent="reporter")
        return reporting.scripted_report(study, cases)
