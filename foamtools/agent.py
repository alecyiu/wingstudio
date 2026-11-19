"""foam-agent - an LLM agent that sets up, runs and judges OpenFOAM simulations.

The model decides everything (template, mesh, solver, numerics); this file
only gives it tools and runs the loop. LLM access goes through LiteLLM, so
any provider works; the default is Claude via your Anthropic API key.

  foam-agent "Lid-driven cavity at Re=400, where is the vortex centre?"
  foam-agent --model anthropic/claude-opus-5-5 --max-steps 40 "..."
  foam-agent            # interactive: type the task, then follow-ups

Key:    put ANTHROPIC_API_KEY=... in .env (see .env.example) or export it.
Output: a live trace on the terminal and a full transcript in runs/<timestamp>/.
"""
import argparse
import base64
import json
import os
import re
import shlex
import shutil
import sys
import time

from . import llm
from .common import CASES, ROOT, load_env, read_text, run_sim

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"
MAX_TOOL_CHARS = 14000          # cap on any single tool result sent to the model


# --------------------------------------------------------------- tool layer --

def _case_path(name, rel=""):
    """Resolve a path inside cases/<name>, refusing anything that escapes it."""
    if not re.fullmatch(r"[\w+-][\w.+-]*", name or ""):
        raise ValueError(f"invalid case name {name!r}")
    base = os.path.realpath(os.path.join(CASES, name))
    if os.path.dirname(base) != os.path.realpath(CASES):
        raise ValueError(f"invalid case name {name!r}")
    full = os.path.realpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"path {rel!r} is outside the case directory")
    return full


def t_list_tutorials(words=None, limit=40):
    return run_sim("tutorials", *(words or []), "--limit", limit)


def t_list_cases():
    out = []
    for n in sorted(os.listdir(CASES)) if os.path.isdir(CASES) else []:
        cd = os.path.join(CASES, n, "system", "controlDict")
        if os.path.exists(cd):
            m = re.search(r"^\s*application\s+(\S+?);", read_text(cd), re.M)
            out.append({"case": n, "solver": m.group(1) if m else None})
    return {"ok": True, "cases": out}


def t_create_case(name, tutorial, replace=False):
    return run_sim("new", name, "--from", tutorial, *(["--force"] if replace else []))


def t_copy_case(source, name, keep_results=False):
    src, dst = _case_path(source), _case_path(name)
    if not os.path.isdir(os.path.join(src, "system")):
        return {"ok": False, "error": f"no such case: {source}"}
    if os.path.exists(dst):
        return {"ok": False, "error": f"case {name} already exists"}
    shutil.copytree(src, dst, symlinks=True)
    if not keep_results:
        run_sim("clean", name)
    return {"ok": True, "case": name, "copied_from": source,
            "note": "mesh, results and logs kept" if keep_results
                    else "mesh, results and logs removed; setup kept"}


def t_case_info(name):
    return run_sim("info", name)


def t_read_entry(name, file, entry=None):
    return run_sim("get", name, file, *([entry] if entry else []))


def t_set_entry(name, file, entry, value):
    return run_sim("set", name, file, entry, value)


def t_read_file(name, path, max_chars=8000):
    full = _case_path(name, path)
    if os.path.isdir(full):
        return {"ok": True, "directory": path, "entries": sorted(os.listdir(full))[:200]}
    if not os.path.exists(full):
        return {"ok": False, "error": f"no such file: {path}"}
    text = read_text(full)
    return {"ok": True, "path": path, "chars": len(text),
            "truncated": len(text) > max_chars, "content": text[:max_chars]}


def t_write_file(name, path, content):
    full = _case_path(name, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    existed = os.path.exists(full)
    with open(full, "w") as f:
        f.write(content)
    return {"ok": True, "path": path, "chars": len(content),
            "action": "overwritten" if existed else "created"}


def t_run_command(name, command, np=None, timeout_s=900):
    shellish = bool(re.search(r"[|&;<>$`*?(){}\[\]~]|\s&&\s", command))
    args = ["run", name, "--timeout", timeout_s, "--tail", 15]
    if shellish:
        args += ["--sh", "--", command]
    else:
        if np and np > 1:
            args += ["--np", np]
        args += ["--", *shlex.split(command)]
    return run_sim(*args, timeout=timeout_s + 60)


def t_check_mesh(name):
    return run_sim("mesh", name)


def t_solver_status(name, log=None):
    return run_sim("status", name, *(["--log", log] if log else []))


def t_plot(name, fields=None, time=None, streamlines=True):
    args = ["plot", name]
    if fields:
        args += ["--fields", *fields]
    if time is not None:
        args += ["--time", str(time)]
    if not streamlines:
        args.append("--no-streamlines")
    return run_sim(*args, timeout=300)


def t_clean_case(name):
    return run_sim("clean", name)


def _fn(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


_CASE = {"type": "string", "description": "Case name (directory under cases/)."}

TOOLS = [
    _fn("list_tutorials",
        "Search the tutorial cases shipped with OpenFOAM. Returns tutorial paths with their "
        "solver. Use this to pick a starting template.",
        {"words": {"type": "array", "items": {"type": "string"},
                   "description": "Words that must all appear in the path or solver name, "
                                  "e.g. ['incompressible', 'cavity'] or ['interFoam', 'laminar']."},
         "limit": {"type": "integer", "description": "Maximum results (default 40)."}}),
    _fn("list_cases", "List the cases that already exist, with their solver.", {}),
    _fn("create_case", "Create cases/<name> as a copy of a tutorial.",
        {"name": _CASE,
         "tutorial": {"type": "string", "description": "Tutorial path from list_tutorials."},
         "replace": {"type": "boolean", "description": "Overwrite an existing case of that name."}},
        ["name", "tutorial"]),
    _fn("copy_case",
        "Branch a new case from an existing one, to try a variant without losing the original.",
        {"source": _CASE, "name": {"type": "string", "description": "New case name."},
         "keep_results": {"type": "boolean",
                          "description": "Keep mesh, time directories and logs (default false: "
                                         "copy only the setup)."}},
        ["source", "name"]),
    _fn("case_info",
        "Describe a case: solver, time control, turbulence model, files, initial fields, "
        "boundary patches, existing time directories, scripts and logs.",
        {"name": _CASE}, ["name"]),
    _fn("read_entry",
        "Read an OpenFOAM dictionary file, or one entry of it, with comments stripped.",
        {"name": _CASE,
         "file": {"type": "string", "description": "e.g. system/blockMeshDict, system/fvSolution, 0/U."},
         "entry": {"type": "string",
                   "description": "Optional entry, nested with slashes: endTime, "
                                  "SIMPLE/residualControl/p, boundaryField/inlet/value."}},
        ["name", "file"]),
    _fn("set_entry",
        "Change one entry of a dictionary file. Returns the old and new value.",
        {"name": _CASE, "file": {"type": "string"},
         "entry": {"type": "string", "description": "Entry, nested with slashes."},
         "value": {"type": "string",
                   "description": "New value in OpenFOAM syntax without the trailing semicolon, "
                                  "e.g. '500', 'uniform (10 0 0)', "
                                  "'( hex (0 1 2 3 4 5 6 7) (40 40 1) simpleGrading (1 1 1) )'."}},
        ["name", "file", "entry", "value"]),
    _fn("read_file",
        "Read a raw file or list a directory inside the case (scripts such as Allrun, logs, "
        "dictionaries including comments).",
        {"name": _CASE, "path": {"type": "string", "description": "Path relative to the case."},
         "max_chars": {"type": "integer", "description": "Default 8000."}},
        ["name", "path"]),
    _fn("write_file",
        "Create or overwrite a file inside the case. Use for structural edits set_entry cannot "
        "express. Write the complete file including the FoamFile header.",
        {"name": _CASE, "path": {"type": "string"}, "content": {"type": "string"}},
        ["name", "path", "content"]),
    _fn("run_command",
        "Run an OpenFOAM utility, solver or shell line in the case directory inside the "
        "container. Output goes to log.<command>; you get the exit code, run time, any fatal "
        "error and the last log lines. Examples: 'blockMesh', 'setFields', 'simpleFoam', "
        "'rm -rf 0 && cp -r 0.orig 0', 'postProcess -func yPlus'.",
        {"name": _CASE,
         "command": {"type": "string"},
         "np": {"type": "integer",
                "description": "Run in parallel on this many cores (max 4). Requires a prior "
                               "decomposePar; follow with reconstructPar."},
         "timeout_s": {"type": "integer",
                       "description": "Kill the command after this many seconds (default 900). "
                                      "Results written so far are kept."}},
        ["name", "command"]),
    _fn("check_mesh",
        "Run checkMesh: cell count, cell types, bounding box, aspect ratio, non-orthogonality, "
        "skewness and whether the mesh is OK.",
        {"name": _CASE}, ["name"]),
    _fn("solver_status",
        "Parse the solver log: state (converged / finished / incomplete / failed), steps, "
        "residuals per field, Courant number, continuity errors and any fatal error.",
        {"name": _CASE,
         "log": {"type": "string", "description": "Log file in the case; default is the solver's."}},
        ["name"]),
    _fn("plot",
        "Render fields of a 2D result to a PNG and return min / max / mean per field. "
        "You are shown the image afterwards.",
        {"name": _CASE,
         "fields": {"type": "array", "items": {"type": "string"},
                    "description": "e.g. ['U', 'p']. Vectors are drawn as magnitude. "
                                   "Default: U plus one scalar."},
         "time": {"type": "string", "description": "Time directory to plot; default latest."},
         "streamlines": {"type": "boolean", "description": "Overlay streamlines on vector fields."}},
        ["name"]),
    _fn("clean_case", "Delete the mesh, time directories and logs of a case; keep its setup.",
        {"name": _CASE}, ["name"]),
]

DISPATCH = {
    "list_tutorials": t_list_tutorials, "list_cases": t_list_cases,
    "create_case": t_create_case, "copy_case": t_copy_case, "case_info": t_case_info,
    "read_entry": t_read_entry, "set_entry": t_set_entry,
    "read_file": t_read_file, "write_file": t_write_file,
    "run_command": t_run_command, "check_mesh": t_check_mesh,
    "solver_status": t_solver_status, "plot": t_plot, "clean_case": t_clean_case,
}


def execute_tool(name, raw_args):
    """Run one tool call. Always returns a dict; errors are returned, not raised,
    so the model can read them and recover."""
    fn = DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool {name!r}", "available": sorted(DISPATCH)}
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": f"could not parse tool arguments: {e}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001 - surface anything to the model
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_result_text(result):
    text = json.dumps(result, indent=1)
    if len(text) > MAX_TOOL_CHARS:
        text = (text[:MAX_TOOL_CHARS] +
                f"\n... [truncated {len(text) - MAX_TOOL_CHARS} characters; "
                "narrow the request to see the rest]")
    return text


# -------------------------------------------------------------- agent loop --

class Trace:
    """Terminal trace plus a transcript on disk."""

    def __init__(self, quiet=False):
        self.dir = os.path.join(ROOT, "runs", time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.quiet = quiet
        self.events = open(os.path.join(self.dir, "events.jsonl"), "a")

    def log(self, kind, **data):
        self.events.write(json.dumps({"t": round(time.time(), 2), "kind": kind, **data}) + "\n")
        self.events.flush()

    def say(self, text=""):
        if not self.quiet:
            print(text, flush=True)

    def save_messages(self, messages):
        with open(os.path.join(self.dir, "messages.json"), "w") as f:
            json.dump(messages, f, indent=1, default=str)


def _short(value, n=110):
    s = value if isinstance(value, str) else json.dumps(value)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _summarise(name, result):
    """One-line human summary of a tool result for the terminal."""
    if not result.get("ok", True):
        return "FAILED: " + _short(result.get("fatal_error") or result.get("error")
                                  or result.get("detail") or result, 200)
    if name == "run_command":
        return f"exit {result.get('exit_code')} in {result.get('seconds')}s"
    if name == "check_mesh":
        return (f"{result.get('cells')} cells, non-ortho {result.get('max_non_orthogonality')}, "
                f"skew {result.get('max_skewness')}, ok={result.get('mesh_ok')}")
    if name == "solver_status":
        co = (result.get("courant") or {}).get("peak_max")
        return (f"{result.get('state')}, {result.get('steps')} steps, t={result.get('last_time')}"
                + (f", peak Co {co}" if co is not None else ""))
    if name == "plot":
        return str(result.get("image"))
    if name == "set_entry":
        return f"{_short(result.get('old'), 40)} -> {_short(result.get('new'), 60)}"
    if name == "list_tutorials":
        return f"{result.get('count')} matches"
    return "ok"


def run_agent(task, model, max_steps, effort, vision, trace, messages=None):
    """Drive the tool loop until the model answers without calling a tool.
    Returns (final_text, messages)."""
    completion = llm.completion()

    if messages is None:
        with open(os.path.join(HERE, "agent_prompt.md"), encoding="utf-8") as f:
            system = f.read()
        messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": task})
    trace.log("task", text=task, model=model)
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    for step in range(1, max_steps + 1):
        kwargs = dict(
            model=model, messages=messages, tools=TOOLS, max_tokens=16000, num_retries=3,
            # Cache the stable prefix (tools + system) and the growing history,
            # so each step re-reads earlier turns at the cached rate.
            cache_control_injection_points=[
                {"location": "message", "role": "system"},
                {"location": "message", "index": -1},
            ],
        )
        if effort:
            kwargs["reasoning_effort"] = effort
        response = completion(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tokens_in, tokens_out, cached, _ = llm.usage(response, model)
        usage_total["prompt_tokens"] += tokens_in
        usage_total["completion_tokens"] += tokens_out
        usage_total["cached_tokens"] += cached

        # Keep the assistant message whole (including provider reasoning blocks)
        # so the next request replays it unchanged.
        entry = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
        entry.setdefault("role", "assistant")
        messages.append(entry)
        trace.save_messages(messages)

        text = (getattr(msg, "content", None) or "").strip()
        calls = getattr(msg, "tool_calls", None) or []
        if text and calls:
            trace.say(f"\n[{step}] {text}")
        trace.log("assistant", step=step, text=text, finish_reason=choice.finish_reason,
                  tool_calls=[{"name": c.function.name, "arguments": c.function.arguments}
                              for c in calls])

        if not calls:
            if choice.finish_reason == "length":
                trace.say("\n[stopped: the reply hit the output limit]")
            elif choice.finish_reason not in ("stop", "end_turn", None):
                trace.say(f"\n[stopped: finish_reason={choice.finish_reason}]")
            trace.log("done", steps=step, usage=usage_total)
            trace.say(f"\n--- {step} steps, {usage_total['prompt_tokens']:,} input tokens "
                      f"({usage_total['cached_tokens']:,} cached), "
                      f"{usage_total['completion_tokens']:,} output tokens ---")
            return text, messages

        images = []
        for call in calls:
            name, raw = call.function.name, call.function.arguments
            trace.say(f"[{step}] {name}({_short(raw, 150)})")
            started = time.time()
            result = execute_tool(name, raw)
            trace.say(f"      -> {_summarise(name, result)}")
            trace.log("tool", step=step, name=name, arguments=raw, result=result,
                      seconds=round(time.time() - started, 2))
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": tool_result_text(result)})
            if name == "plot" and result.get("ok") and vision:
                images.append(os.path.join(ROOT, result["image"]))

        # Images go in one user message after ALL tool results of this step.
        if images:
            content = [{"type": "text", "text": "Rendered image(s) from the plot call(s) above:"}]
            for path in images:
                with open(path, "rb") as f:
                    b64 = base64.standard_b64encode(f.read()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"}})
            messages.append({"role": "user", "content": content})
        trace.save_messages(messages)

    trace.say(f"\n[stopped after {max_steps} steps without a final answer; "
              "raise --max-steps or continue in interactive mode]")
    trace.log("max_steps", usage=usage_total)
    return "", messages


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="*", help="what to simulate (omit for interactive mode)")
    ap.add_argument("--model", default=os.environ.get("OPENFOAM_AGENT_MODEL"),
                    help=f"LiteLLM model string (default {DEFAULT_MODEL}, "
                         "or $OPENFOAM_AGENT_MODEL)")
    ap.add_argument("--max-steps", type=int, default=60, help="model turns per task (default 60)")
    ap.add_argument("--effort", default="medium",
                    choices=["low", "medium", "high", "xhigh", "max", "none"],
                    help="reasoning effort (default medium; 'none' sends no setting)")
    ap.add_argument("--no-vision", action="store_true", help="do not show plots to the model")
    ap.add_argument("--quiet", action="store_true", help="print only the final answer")
    a = ap.parse_args()

    load_env()
    model = a.model or os.environ.get("OPENFOAM_AGENT_MODEL") or DEFAULT_MODEL
    if model.startswith("anthropic/") and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and put your key "
                 "in it, or export it in your shell.")
    effort = None if a.effort == "none" else a.effort
    trace = Trace(quiet=a.quiet)
    trace.say(f"model: {model}   transcript: {os.path.relpath(trace.dir, ROOT)}/")

    import litellm
    task = " ".join(a.task).strip()
    messages = None
    interactive = not task
    try:
        while True:
            if interactive:
                try:
                    task = input("\ntask> ").strip()
                except EOFError:
                    break
                if task in ("", "exit", "quit"):
                    break
            try:
                answer, messages = run_agent(task, model, a.max_steps, effort,
                                             not a.no_vision, trace, messages)
            except litellm.AuthenticationError:
                sys.exit("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
            except litellm.RateLimitError as e:
                sys.exit(f"Rate limited after retries: {e}")
            except litellm.BadRequestError as e:
                sys.exit(f"The API rejected the request: {e}")
            except litellm.APIConnectionError as e:
                sys.exit(f"Could not reach the API: {e}")
            print("\n" + answer if answer else "")
            with open(os.path.join(trace.dir, "answer.md"), "a") as f:
                f.write(f"## Task\n\n{task}\n\n## Answer\n\n{answer}\n\n")
            if not interactive:
                break
    except KeyboardInterrupt:
        print("\n[interrupted]")


if __name__ == "__main__":
    main()
