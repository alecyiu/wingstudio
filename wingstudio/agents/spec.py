"""Agent definitions (prompts/*.md: a prompt with its tools in the front matter), the models,
and the compact text forms of plans and messages that agents are given."""
import copy
import json
import os
import re
from dataclasses import dataclass

import yaml

from foamtools.common import read_text, round_sig

from .. import exceptions

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

SCRIPTED = "scripted"
MODELS = ["anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", SCRIPTED]
MODEL_ALIASES = {"haiku": MODELS[0], "sonnet": MODELS[1], "opus": MODELS[2]}
DEFAULT_EFFORT = "medium"
# Seconds one model call may take before LiteLLM gives up on it (it then retries). Without it a
# stalled connection holds an agent, and therefore Stop, for LiteLLM's default of 6000 s per try.
LLM_TIMEOUT_S = {"xhigh": 600, "max": 600}
DEFAULT_LLM_TIMEOUT_S = 300

STEERING_PLAN_CHARS = 20000    # four cases without cites and checks are about 16,000
HANDOFF_DECISION_CHARS = 2500  # the eleven numerics decisions with the plan's reasons are about 1,700


@dataclass
class AgentSpec:
    file_name: str
    name: str
    description: str
    tools: list
    model: str
    max_turns: int
    effort: str | None
    prompt: str


def load_agent(file_name):
    """Read prompts/<file_name>: the YAML front matter (name, description, tools, model,
    maxTurns, effort) and the body, which is the system prompt."""
    path = os.path.join(AGENTS_DIR, file_name)
    where = f"wingstudio/agents/prompts/{file_name}"
    if not os.path.exists(path):
        raise exceptions.AgentError(f"agent definition {where} is missing")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", read_text(path), re.S)
    if not m:
        raise exceptions.AgentError(f"{where} has no YAML frontmatter")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception as e:  # noqa: BLE001
        raise exceptions.AgentError(f"{where}: cannot parse the frontmatter: {e}")
    if not isinstance(meta, dict):
        raise exceptions.AgentError(f"{where}: the frontmatter is not a mapping")
    tools = meta.get("tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",")]
    tools = [str(t) for t in tools if str(t).strip()]
    try:
        max_turns = int(meta.get("maxTurns") or 30)
    except (TypeError, ValueError):
        max_turns = 30
    body = m.group(2).strip()
    if not body:
        raise exceptions.AgentError(f"{where} has an empty system prompt")
    return AgentSpec(file_name, str(meta.get("name") or file_name[:-3]), str(meta.get("description") or ""),
                     tools, str(meta.get("model") or "inherit"), max(1, max_turns),
                     meta.get("effort"), body)


def resolve_model(spec_model, study_model):
    if not spec_model or spec_model == "inherit":
        return study_model
    return MODEL_ALIASES.get(spec_model, spec_model)


def compact(obj, limit=6000):
    text = json.dumps(obj, indent=1, default=str)
    if len(text) > limit:
        text = json.dumps(obj, default=str)       # without indentation before anything is cut
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def rounded_conditions(cond):
    """Flow conditions with every number to six significant figures."""
    return {k: v if isinstance(v, (list, bool, str)) else round_sig(v, 6) for k, v in cond.items()}


def uncited(cases):
    """Plan cases as the steering agent reads them: without the reference ids, which it never
    writes, and without the checks, which the studio fixes and are not settings. With them a
    plan of three or four cases is cut off in its context block."""
    out = copy.deepcopy(cases or [])
    for pc in out:
        if isinstance(pc, dict):
            pc.pop("cites", None)
            for section in (pc.get("stages") or {}).values():
                if isinstance(section, dict):
                    section.pop("cites", None)
                    section.pop("checks", None)
    return out


def format_message(msg):
    text = msg.get("text") or ""
    if msg.get("quote"):
        text += f' (quoting: "{msg["quote"]}")'
    if msg.get("target_stage"):
        text += f" [about stage: {msg['target_stage']}]"
    return text
