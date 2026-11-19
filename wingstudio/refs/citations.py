"""The reference catalogue behind every cited setting.

A citation is an id into wingstudio/refs/catalogue.json and nothing else: agents
write ids, never a title, an author or an address, and everything the UI shows
about a source comes from that file. This module loads the file (cached by its
modification time, so an edit shows up without a restart), answers which ids
exist and which a stage is shown, and words the refusals for unknown ids.

A missing or broken catalogue never stops the server: `load()` then has no
entries and an `error`, default citations are empty, and an id a model cites
is refused with that error.

Some entries have a local document beside the catalogue: refs/docs/<id>.md is
the text as retrieved (the part that matters to the studio) and <id>.json its
index of passages. `public()` says which entries have one, `document()` returns
one, and `previous_runs()` lists earlier runs of similar cases for the pop-up.

  python -m wingstudio.refs.citations --lint     # exit 1 on problems

Standard library only; imports nothing from the other studio modules.
"""
import json
import math
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "catalogue.json")     # tests point this at a temporary file

ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_ID_CHARS = 64
MAX_CITES = 3                    # ids per setting or decision
KINDS = ("paper", "book", "documentation", "project-measurement", "project-rule")
SETTING_RE = re.compile(r"[a-z]+\.[A-Za-z_0-9]+")       # 'stage.setting' or 'case.alpha_deg'
DOC_ID_RE = re.compile(r"[a-z0-9-]+")                    # what GET /api/refs/doc/{id} accepts at all
MAX_PASSAGE_CHARS = 4000         # a passage is a quotation, not the document
MAX_RUNS = 3                     # previous runs listed for one setting
MAX_CANDIDATES = 200             # rows looked at for them, however long the history is
EXAMPLE = '["spalart-allmaras-1992"]'
WHAT = "Citations are ids from the studio's reference catalogue, never titles or URLs."

_lock = threading.Lock()
_cache = {"key": None, "data": None}
_doc_cache = {}                  # path of an index file -> (file keys, parsed index, passages)
_text_cache = {}                 # path of a document -> (file key, its text or None)
_logged = set()


# ------------------------------------------------------------------- loading --

def _file_key():
    try:
        info = os.stat(PATH)
        return (PATH, info.st_mtime_ns, info.st_size)
    except OSError:
        return (PATH, None, None)


def _read_raw():
    """(the parsed file, None) or (None, why it could not be used)."""
    try:
        with open(PATH) as f:
            raw = json.load(f)
    except OSError as e:
        return None, f"{os.path.basename(PATH)} could not be read: {e.strerror or e}"
    except json.JSONDecodeError as e:
        return None, f"{os.path.basename(PATH)} is not valid JSON: {e.msg} at line {e.lineno}"
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return None, f"{os.path.basename(PATH)} must be an object with an 'entries' list"
    return raw, None


def _valid_id(value):
    return isinstance(value, str) and len(value) <= MAX_ID_CHARS and bool(ID_RE.fullmatch(value))


def _build(raw, error):
    data = {"version": None, "checked": None, "entries": {}, "order": [], "defaults": {}, "error": error}
    if raw is None:
        return data
    data["version"], data["checked"] = raw.get("version"), raw.get("checked")
    for e in raw["entries"]:
        # a malformed or repeated entry is left out here and reported by problems()
        if isinstance(e, dict) and _valid_id(e.get("id")) and e["id"] not in data["entries"]:
            data["entries"][e["id"]] = e
            data["order"].append(e["id"])
    defaults = raw.get("defaults")
    for key, ids in (defaults.items() if isinstance(defaults, dict) else ()):
        # Only ids that exist: a typing error in `defaults` must not put an id into every
        # plan that validate_plan then refuses.
        known = [i for i in (ids if isinstance(ids, list) else []) if i in data["entries"]]
        if known:
            data["defaults"][str(key)] = list(dict.fromkeys(known))[:MAX_CITES]
    return data


def load():
    """{version, checked, entries: {id: entry}, order: [id], defaults: {key: [id]}, error}.
    Read it, do not change it: the object is shared until the file changes."""
    key = _file_key()
    with _lock:
        if _cache["key"] != key:
            raw, error = _read_raw()
            _cache["key"], _cache["data"] = key, _build(raw, error)
            if error and error not in _logged:
                _logged.add(error)                 # once per problem, not once per request
                print(f"citations: {error}; no setting is cited until it is fixed", file=sys.stderr, flush=True)
        return _cache["data"]


# ------------------------------------------------------------------- queries --

def exists(ref_id):
    return isinstance(ref_id, str) and ref_id in load()["entries"]


def get(ref_id):
    entry = load()["entries"].get(ref_id) if isinstance(ref_id, str) else None
    return dict(entry) if entry else None


def default_cites(stage, setting):
    """The ids that support the default value of one setting (a copy; [] if none)."""
    return list(load()["defaults"].get(f"{stage}.{setting}") or [])


def default_section(stage):
    """{setting: [ids]} for every setting of `stage` that has a default citation."""
    prefix = f"{stage}."
    return {key[len(prefix):]: list(ids) for key, ids in load()["defaults"].items() if key.startswith(prefix)}


def _brief(entry):
    return {"id": entry["id"], "kind": entry.get("kind"), "title": entry.get("title"),
            "says": entry.get("says"), "cite_for": _names(entry.get("settings"))}


def index():
    """What a model is shown of the catalogue: [{id, kind, title, says, cite_for}]. No address."""
    data = load()
    return [_brief(data["entries"][i]) for i in data["order"]]


def for_stage(stage):
    """The entries meant for the settings of one stage, in the shape of index(). `settings` is
    advice on where an entry belongs: it decides what a stage agent is shown and what an error
    suggests, never whether a citation is accepted."""
    prefixes = (f"{stage}.",) + (("case.",) if stage == "physics" else ())   # physics applies the angle
    return [e for e in index() if any(s.startswith(prefixes) for s in e["cite_for"])]


def ids_for(stage):
    return [e["id"] for e in for_stage(stage)]


def public():
    """The GET /api/refs body. An address leaves the server only when the catalogue marks it
    verified and it is https; the page checks again before it makes a link. `passages` are
    verbatim quotations from the local document; `says`, `why` and `differs` are our words."""
    data = load()
    entries = []
    for ref_id in data["order"]:
        e = data["entries"][ref_id]
        url = e.get("url") if isinstance(e.get("url"), str) else ""
        verified = e.get("url_verified") is True and url.startswith("https://")
        index, passages = _doc_record(ref_id)
        entries.append({"id": ref_id, "title": str(e.get("title") or ""), "kind": str(e.get("kind") or ""),
                        "source": str(e.get("source") or ""), "says": str(e.get("says") or ""),
                        "settings": _names(e.get("settings")), "locator": str(e.get("locator") or ""),
                        "checked": str(e.get("checked") or ""),
                        "url": url if verified else "", "url_verified": verified,
                        "doc": has_document(ref_id), "scope": _text(index.get("scope")),
                        "licence": _text(index.get("licence")), "retrieved": _text(index.get("retrieved")),
                        "passages": [dict(p, settings=list(p["settings"])) for p in passages],
                        "differs": _differs(e)})
    return {"version": data["version"], "checked": data["checked"], "error": data["error"],
            "entries": entries, "defaults": {k: list(v) for k, v in data["defaults"].items()}}


# ---------------------------------------------------------- local documents --

def docs_dir():
    """Where the local documents are: docs/ beside the catalogue file (wingstudio/refs/docs)."""
    return os.path.join(os.path.dirname(PATH), "docs")


def _text(value):
    return value if isinstance(value, str) else ""


def _names(value):
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _is_link(path):
    """Whether docs/<name> is a symbolic link, or stands anywhere but directly in docs/."""
    folder = os.path.realpath(docs_dir())
    return os.path.islink(path) or os.path.realpath(path) != os.path.join(folder, os.path.basename(path))


def _doc_path(ref_id, ext):
    """docs/<id><ext> for an id that is safe as a file name, else None. Nothing outside
    docs_dir() can be named: the pattern has no dot and no separator, and a symbolic link is
    never followed (the file read is the one in docs/ itself, or none)."""
    if not isinstance(ref_id, str) or len(ref_id) > MAX_ID_CHARS or not DOC_ID_RE.fullmatch(ref_id):
        return None
    path = os.path.join(docs_dir(), ref_id + ext)
    return None if _is_link(path) else path


def _doc_text(ref_id):
    """The text of docs/<id>.md, or None when there is no such file or it cannot be read as
    UTF-8. Cached until the file changes, so `doc` in public() costs one stat per entry."""
    path = _doc_path(ref_id, ".md")
    key = _stat_key(path)
    if key is None:
        return None
    with _lock:
        hit = _text_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        text = None
    with _lock:
        _text_cache[path] = (key, text)
    return text


def has_document(ref_id):
    """True only for a document GET /api/refs/doc/{id} can serve: a file that can be read."""
    return _doc_text(ref_id) is not None


def _stat_key(path):
    try:
        info = os.stat(path)
        return (info.st_mtime_ns, info.st_size)
    except (OSError, TypeError):
        return None


def _doc_record(ref_id):
    """(index, passages) of one local document: the parsed docs/<id>.json ({} when there is none
    or it cannot be used) and its passages. Cached until the index or the document changes,
    like the catalogue. Read them, do not change them."""
    path, text_path = _doc_path(ref_id, ".json"), _doc_path(ref_id, ".md")
    key = (_stat_key(path), _stat_key(text_path))
    if key[0] is None:
        return {}, []
    with _lock:
        hit = _doc_cache.get(path)
        if hit and hit[0] == key:
            return hit[1], hit[2]
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    index = raw if isinstance(raw, dict) else {}
    passages = _passages(index, _doc_text(ref_id))
    with _lock:
        _doc_cache[path] = (key, index, passages)
    return index, passages


def _passages(index, text):
    """[{key, heading, text, settings, why}] of a document index. A passage is shown as a
    quotation, so it is passed on only when it stands word for word in the document (`text`),
    and then unchanged: never trimmed or reworded. --lint names the ones left out."""
    out = []
    for p in index.get("passages") if isinstance(index.get("passages"), list) else []:
        if isinstance(p, dict) and _text(p.get("key")) and _text(p.get("text")) and text and p["text"] in text:
            out.append({"key": p["key"], "heading": _text(p.get("heading")), "text": p["text"],
                        "settings": _names(p.get("settings")), "why": _text(p.get("why"))})
    return out


def _differs(entry):
    """[{settings, text}]: where the source says something else than the studio does."""
    out = []
    for d in entry.get("differs") if isinstance(entry.get("differs"), list) else []:
        if isinstance(d, dict) and _text(d.get("text")).strip():
            out.append({"settings": _names(d.get("settings")), "text": d["text"].strip()})
    return out


def document(ref_id):
    """The GET /api/refs/doc/{id} body, or None when `ref_id` is not a catalogue id with a local
    document. The only file ever read for the text is docs/<id>.md of a catalogue id."""
    entry = load()["entries"].get(ref_id) if _doc_path(ref_id, ".md") else None
    markdown = _doc_text(ref_id) if entry is not None else None
    if markdown is None:
        return None
    index, _ = _doc_record(ref_id)
    # The address is the catalogue's, under the rule of public(): the index is not a second
    # authority (--lint holds its source_url to the entry's url).
    url = _text(entry.get("url"))
    return {"id": ref_id, "title": _text(index.get("title")) or str(entry.get("title") or ""),
            "markdown": markdown,
            "source_url": url if entry.get("url_verified") is True and url.startswith("https://") else "",
            "licence": _text(index.get("licence")), "retrieved": _text(index.get("retrieved")),
            "scope": _text(index.get("scope"))}


# ------------------------------------------------------------ previous runs --

# what follows the value in a run's one-line summary; other settings are named instead
_UNITS = {"first_cell_height": "chord", "farfield_chords": "chords", "wake_chords": "chords", "alpha_deg": "deg"}


def _number(value):
    """A finite float for a number or a numeric string, else None (a bool is not a number)."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _same_value(a, b):
    """Whether two setting values are the same: numbers exactly (to rounding), words without
    regard to case. 0.00129 and 0.0013 are different values."""
    if a is None or b is None:
        return False
    x, y = _number(a), _number(b)
    if x is not None and y is not None:
        return math.isclose(x, y, rel_tol=1e-9, abs_tol=0.0)
    return str(a).strip().lower() == str(b).strip().lower()


def _short(value):
    """A value as one short word: 0.00129, 1200, 1e-6, SpalartAllmaras."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return re.sub(r"e([+-])0*(\d)", lambda m: "e" + m.group(1).replace("+", "") + m.group(2), f"{value:.6g}")
    return str(value)


def model_label(model):
    """'anthropic/claude-haiku-5-5' -> 'Haiku 5.5', as the page writes model names."""
    model = _text(model)
    if not model:
        return "unknown model"
    tail = re.sub(r"^claude-", "", model.split("/")[-1])
    name, _, version = tail.partition("-")
    return name[:1].upper() + name[1:] + (" " + version.replace("-", ".") if version else "")


def _plan_case(study, naca):
    cases = (study.get("plan") or {}).get("cases")
    return next((pc for pc in cases if isinstance(pc, dict) and str(pc.get("naca")) == str(naca)), None) \
        if isinstance(cases, list) else None


def _planned(plan_case, stage, name):
    """The plan value of one setting ('case' is the case level: the angle of attack)."""
    if not isinstance(plan_case, dict):
        return None
    section = plan_case if stage == "case" else (plan_case.get("stages") or {}).get(stage)
    value = section.get(name) if isinstance(section, dict) else None
    return None if isinstance(value, (dict, list)) else value


# a number followed by nothing but its unit, a remark in parentheses, or both:
# '0.00129 chord', '30 chords', '1200 iterations', '2 (was 1)', '0.5 (lowered after the first gate)'
_NUMBER_WITH_UNIT = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*"
                               r"(?:[A-Za-z%][A-Za-z%+/ ]*)?(?:\([^()]*\)\s*)?")
_UNREADABLE = object()


def _decided(value, planned, unreadable=None):
    """A decision value as the kind of thing the plan holds. For a number setting: the number
    (agents write 30 for 30.0, "0.3" for 0.3, "0.00129 chord" for 0.00129 and "2 (was 1)"
    for 2). A decision that cannot be read as one ("kept at 0.5, see numerics") gives the plan
    value, or `unreadable` when the caller has other decisions to ask first."""
    if not isinstance(planned, (int, float)) or isinstance(planned, bool):
        return value
    number = _number(value)
    if number is None and isinstance(value, str):
        match = _NUMBER_WITH_UNIT.fullmatch(value)
        number = _number(match.group(1)) if match else None
    if number is None:
        return planned if unreadable is None else unreadable
    return int(number) if isinstance(planned, int) and number == int(number) else number


def _applied(case, plan_case, stage, name):
    """The value in force when the case finished: the last decision for the setting that a
    passed stage recorded and that can be read, looked for from the setting's own stage to the
    end of the pipeline (an agent may deviate from the plan after a failed gate, and the solve
    stage may change a numerics setting), else the plan value. A later decision that cannot be
    read as a value must not hide the readable one of the setting's own stage."""
    planned = _planned(plan_case, stage, name)
    if stage == "case":
        return case.get(name) if case.get(name) is not None else planned
    records = [s for s in case.get("stages") or [] if isinstance(s, dict)]
    start = next((n for n, s in enumerate(records) if s.get("key") == stage), len(records))
    for record in reversed(records[start:]):
        for d in reversed(record.get("decisions") or [] if record.get("status") == "passed" else []):
            if isinstance(d, dict) and d.get("param") == name and d.get("value") is not None \
                    and not isinstance(d["value"], (dict, list)):
                value = _decided(d["value"], planned, _UNREADABLE)
                if value is not _UNREADABLE:
                    return value
    return planned


def _run_summary(run, stage, name):
    """One line for the pop-up: who ran what, the value, the outcome that value bears on, the verdict."""
    out = run["outcome"]
    value = "not recorded" if run["value"] is None else \
        f"{_short(run['value'])} {_UNITS[name]}" if name in _UNITS else f"{name.replace('_', ' ')} {_short(run['value'])}"
    parts = [value]
    ran = _number(out["iterations"])
    if name in ("first_cell_height", "wall_treatment") and _number(out["yplus_avg"]) is not None:
        parts.append(f"measured y+ {out['yplus_avg']:.0f}")
    elif stage == "mesh" and _number(out["cells"]) is not None:
        parts.append(f"{int(out['cells']):,} cells")
    elif name == "iterations" and ran is not None:
        # the count itself: say it once, and name both only when the run was extended or cut
        if _number(run["value"]) is None:
            parts = [f"ran {int(ran)} iterations"]
        elif not _same_value(run["value"], ran):
            parts = [f"planned {_short(run['value'])}, ran {int(ran)} iterations"]
    elif stage in ("numerics", "solve") and ran is not None:
        parts.append(f"ran {int(ran)} iterations")
    elif _number(out["cl_err_pct"]) is not None and _number(out["cd_err_pct"]) is not None:
        parts.append(f"Cl error {abs(out['cl_err_pct']):.1f}%, Cd error {abs(out['cd_err_pct']):.0f}%")
    parts.append(out["verdict"] or "no verdict")
    reynolds = f"Re {_short(run['reynolds'])}" if _number(run["reynolds"]) is not None else "Re unknown"
    return f"NACA {run['naca']}, {reynolds}, {model_label(run['model'])}: " + ", ".join(parts)


def previous_runs(store, setting, study_id, case_id=None, known_settings=None):
    """The GET /api/refs/runs body: up to MAX_RUNS finished cases of other studies that are
    similar to one case of `study_id`, with the value each used for `setting`.

    Similar: the same turbulence model and wall treatment, and a Reynolds number within a
    factor of two. Most similar first: the same airfoil, then the closest Reynolds number,
    then the most recent. `store` is the studio's Store; it is only read (runs_near, or
    query_runs when it has no such method, then get_study and get_case). An unknown study,
    case or setting gives an empty list. At most MAX_CANDIDATES rows are looked at, so the
    work for one pop-up does not grow with the history.
    This is shown to the engineer only: no agent is given these runs."""
    out = {"setting": _text(setting), "value": None, "runs": []}
    if not isinstance(setting, str) or not SETTING_RE.fullmatch(setting) \
            or (known_settings is not None and setting not in known_settings):
        return out
    stage, name = setting.split(".", 1)
    study = store.get_study(study_id) if isinstance(study_id, str) and study_id else None
    if not study:
        return out
    cases = [pc for pc in (study.get("plan") or {}).get("cases") or [] if isinstance(pc, dict)]
    record = store.get_case(study_id, case_id) if isinstance(case_id, str) and case_id else None
    # before the approval there is no case record yet: the page then names the case by the id
    # it will get (<study>-<naca>) or by the airfoil
    naca = record.get("naca") if record else next(
        (pc.get("naca") for pc in cases if case_id in (pc.get("naca"), f"{study_id}-{pc.get('naca')}")),
        cases[0].get("naca") if len(cases) == 1 and not case_id else None)
    plan_case = _plan_case(study, naca)
    if plan_case is None:
        return out
    out["value"] = _planned(plan_case, stage, name)
    reynolds = _number((study.get("requirements") or {}).get("reynolds"))
    physics = [_planned(plan_case, "physics", k) for k in ("turbulence_model", "wall_treatment")]
    if not reynolds or reynolds <= 0 or None in physics:
        return out
    near = getattr(store, "runs_near", None)      # the database does the range and the limit
    found = near(reynolds, 2.0, not_study=study_id, naca=naca, limit=MAX_CANDIDATES) if near else store.query_runs()
    # The Reynolds number is read as a number once: a row may hold it as text.
    rows = [(r, _number(r.get("reynolds"))) for r in found if r.get("study_id") != study_id]
    rows = [(r, re_) for r, re_ in rows if re_ is not None and re_ > 0 and 0.5 <= re_ / reynolds <= 2.0]
    rows.sort(key=lambda pair: str(pair[0].get("ts") or ""), reverse=True)        # the tie-break: most recent
    rows.sort(key=lambda pair: (str(pair[0].get("naca")) != str(naca), abs(math.log(pair[1] / reynolds))))
    studies = {}                                  # two cases of one study: read it once
    for r, run_reynolds in rows[:MAX_CANDIDATES]:
        if r.get("study_id") not in studies:
            studies[r.get("study_id")] = store.get_study(r.get("study_id"))
        other = studies[r.get("study_id")]
        case = store.get_case(r.get("study_id"), r.get("case_id") or r.get("id")) if other else None
        if not case or case.get("status") != "done":
            continue                              # not finished, or its record is gone
        other_plan = _plan_case(other, case.get("naca"))
        if any(not _same_value(_applied(case, other_plan, "physics", k), mine)
               for k, mine in zip(("turbulence_model", "wall_treatment"), physics)):
            continue
        value = _applied(case, other_plan, stage, name)
        run = {"study_id": r.get("study_id"), "study_title": _text(r.get("study_title") or other.get("title")),
               "case_id": case.get("id"), "naca": str(case.get("naca")), "model": _text(r.get("model")),
               "ts": _text(r.get("ts")), "reynolds": run_reynolds, "alpha_deg": case.get("alpha_deg"),
               "value": value, "same_value": _same_value(value, out["value"]),
               "outcome": {k: r.get(k) for k in ("verdict", "success", "yplus_avg", "cells", "iterations",
                                                 "cl_err_pct", "cd_err_pct", "gate_failures", "rework_loops")}}
        run["summary"] = _run_summary(run, stage, name)
        out["runs"].append(run)
        if len(out["runs"]) >= MAX_RUNS:
            break
    return out


# ---------------------------------------------------------------- validation --

def clean(value):
    """(ids, None) for a usable citation value, (None, "shape") otherwise. One id may come as a
    bare string; a list is stripped and de-duplicated; [] means 'no reference'."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, "shape"
    return list(dict.fromkeys(v.strip() for v in value if v.strip())), None


def check(where, value, stage, tail, noun=None):
    """Error sentences for one citation value: `where` names it ('NACA 2412: physics.cites.x',
    'decision 2 (relax_p)'), `stage` picks the ids the message offers, `tail` says what to do
    instead. `noun` replaces `where` in the shape message when `where` does not name a list."""
    ids, problem = clean(value)
    if problem:
        return [f"{noun or where} must be a list of reference ids, for example {EXAMPLE}"]
    if len(ids) > MAX_CITES:
        return [f"{where}: {len(ids)} references cited, at most {MAX_CITES}: keep the ones that support "
                "the value most directly"]
    data = load()
    unknown = [i for i in ids if i not in data["entries"]]
    if unknown and data["error"]:
        return [f"{where}: reference id {i!r} cannot be checked: the studio's reference catalogue failed to "
                f"load ({data['error']}). Leave cites out." for i in unknown]
    offered = ", ".join(ids_for(stage)) or "none"
    return [f"{where}: unknown reference id {i!r}. {WHAT} Ids for {stage}: {offered}. {tail}" for i in unknown]


def problems(known_settings=None):
    """Lint messages for the catalogue file ([] when it is sound). `known_settings` is the set of
    'stage.setting' names the plan really has; given, every key of `defaults` must be one."""
    raw, error = _read_raw()
    if error:
        return [error]
    out, seen, urls = [], set(), {}
    for n, e in enumerate(raw["entries"], 1):
        if not isinstance(e, dict):
            out.append(f"entry {n} is not an object")
            continue
        ref_id = e.get("id")
        tag = f"entry {n} ({ref_id!r})"
        if not _valid_id(ref_id):
            out.append(f"{tag}: id must be lower-case words joined by hyphens, at most {MAX_ID_CHARS} characters")
        elif ref_id in seen:
            out.append(f"{tag}: the id is used twice")
        seen.add(ref_id if isinstance(ref_id, str) else None)
        if isinstance(ref_id, str):
            urls.setdefault(ref_id, e.get("url") if isinstance(e.get("url"), str) else "")
        if e.get("kind") not in KINDS:
            out.append(f"{tag}: kind must be one of {', '.join(KINDS)}")
        for field in ("title", "source", "says"):
            if not isinstance(e.get(field), str) or not e[field].strip():
                out.append(f"{tag}: {field} is empty")
        out.extend(_setting_names_problems(tag, e.get("settings"), known_settings))
        url, verified = e.get("url", ""), e.get("url_verified", False)
        if not isinstance(url, str) or (url and not url.startswith("https://")):
            out.append(f"{tag}: url must be empty or start with https://")
        if not isinstance(verified, bool) or (verified and not url):
            out.append(f"{tag}: url_verified may be true only with a url")
        if str(e.get("kind") or "").startswith("project-") and url:
            out.append(f"{tag}: a project entry names its evidence in source, not in url")
        for field in ("locator", "checked"):
            if field in e and not isinstance(e[field], str):
                out.append(f"{tag}: {field} must be a string")
        if "differs" in e:
            out.extend(_differs_problems(tag, e["differs"], known_settings))
    defaults = raw.get("defaults")
    if not isinstance(defaults, dict):
        return out + ["'defaults' must be an object of 'stage.setting' -> list of ids"]
    for key, ids in defaults.items():
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            out.append(f"defaults[{key!r}] must be a non-empty list of ids")
            continue
        if len(ids) > MAX_CITES or len(set(ids)) != len(ids):
            out.append(f"defaults[{key!r}] must hold at most {MAX_CITES} different ids")
        out.extend(f"defaults[{key!r}] names {i!r}, which is not an entry" for i in ids if i not in seen)
        if known_settings is not None and key not in known_settings:
            out.append(f"defaults[{key!r}] is not a plan setting")
    return out + _docs_problems(urls, known_settings)


def _setting_names_problems(tag, names, known_settings):
    if not isinstance(names, list) or not all(isinstance(s, str) and SETTING_RE.fullmatch(s) for s in names):
        return [f"{tag}: settings must be a list of 'stage.setting' names"]
    return [f"{tag}: settings names {s}, which is not a plan setting"
            for s in names if known_settings is not None and s not in known_settings]


def _differs_problems(tag, differs, known_settings):
    if not isinstance(differs, list):
        return [f"{tag}: differs must be a list of {{settings, text}}"]
    out = []
    for n, d in enumerate(differs, 1):
        if not isinstance(d, dict) or not isinstance(d.get("text"), str) or not d["text"].strip():
            out.append(f"{tag}: differs {n} needs a text")
        else:
            out.extend(_setting_names_problems(f"{tag}: differs {n}", d.get("settings"), known_settings))
    return out


def _docs_problems(urls, known_settings):
    """Lint of docs/: every file belongs to an entry and is a file of docs/ itself (a symbolic
    link is never served), every document can be read, every index has its document and names
    the catalogue's address, and every passage is found word for word in that document (a
    quotation must be the source's text). `urls` is {entry id: its url}."""
    try:
        names = sorted(os.listdir(docs_dir()))
    except OSError:
        return []                                 # no local documents at all is not a fault
    out = []
    for file_name in names:
        ref_id, ext = os.path.splitext(file_name)
        if file_name.startswith(".") or ext not in (".md", ".json"):
            continue
        tag = f"docs/{file_name}"
        if ref_id not in urls or not DOC_ID_RE.fullmatch(ref_id):
            out.append(f"{tag}: {ref_id!r} is not an entry of the catalogue")
            continue
        if _is_link(os.path.join(docs_dir(), file_name)):
            out.append(f"{tag}: is a symbolic link, which is never read: put the file itself into docs/")
            continue
        text = _doc_text(ref_id)
        if ext == ".md":
            # checked here too: a document without an index is sound, so nothing else reads it
            if text is None:
                out.append(f"{tag}: could not be read as UTF-8 text, so it is not served")
            continue
        try:
            with open(os.path.join(docs_dir(), file_name), encoding="utf-8") as f:
                index = json.load(f)
            if text is None:
                raise OSError("no document")
        except (OSError, ValueError) as e:
            out.append(f"{tag}: the index or its document {ref_id}.md could not be read ({type(e).__name__})")
            continue
        if not isinstance(index, dict) or not isinstance(index.get("passages"), list):
            out.append(f"{tag}: must be an object with a 'passages' list")
            continue
        if index.get("id") != ref_id:
            out.append(f"{tag}: id must be {ref_id!r}")
        url = index.get("source_url", "")
        if not isinstance(url, str) or (url and not url.startswith("https://")):
            out.append(f"{tag}: source_url must be empty or start with https://")
        elif url and url != urls[ref_id]:
            # the viewer links the catalogue's address; a second one here would never be checked
            out.append(f"{tag}: source_url must be the url of the catalogue entry ({urls[ref_id] or 'which has none'})")
        keys = set()
        for n, p in enumerate(index["passages"], 1):
            key = p.get("key") if isinstance(p, dict) else None
            where = f"{tag}: passage {n} ({key!r})"
            if not _valid_id(key) or key in keys:
                out.append(f"{where}: key must be lower-case words joined by hyphens, used once")
                continue
            keys.add(key)
            quote = p.get("text")
            if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_PASSAGE_CHARS:
                out.append(f"{where}: text must be a quotation of at most {MAX_PASSAGE_CHARS} characters")
            elif quote not in text:
                out.append(f"{where}: text is not found word for word in {ref_id}.md")
            out.extend(_setting_names_problems(where, p.get("settings"), known_settings))
    return out


# ------------------------------------------------------------- command line --

def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--lint"]:
        from ..study import planning  # noqa: PLC0415 - planning imports this module
        found = problems(planning.plan_setting_names())
        for line in found:
            print(line)
        print(f"{len(load()['order'])} entries, {len(found)} problem(s)")
        return 1 if found else 0
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
