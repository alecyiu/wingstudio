"""The Wing Studio HTTP server.

REST + server-sent events on http://127.0.0.1:8800, the static UI from
wingstudio/web at /, and artifact files at /files/cases/st_*/...
Requests are only accepted for a local Host and from local origins (LocalOnly).

  wing-studio                    # or: python -m wingstudio
"""
import asyncio
import json
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from foamtools import docker
from foamtools.common import ROOT, load_env, read_json

from . import IMAGE
from .agents.spec import MODELS
from .cfd import solver
from .engine import Engine
from .exceptions import EngineError
from .refs import citations
from .store import Store
from .study import planning
from .viewer.manager import ViewerManager

HERE = os.path.dirname(os.path.abspath(__file__))

HOST = "127.0.0.1"
PORT = 8800
# The server has no authentication: it is only for pages it serves itself, on
# this machine. LOCAL_HOST is what the Host header may say (a DNS name rebound
# to 127.0.0.1 does not match; any port, so STUDIO_PORT needs nothing here);
# LOCAL_ORIGIN are the origins whose pages may call it.
LOCAL_HOST = r"(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?"
LOCAL_ORIGIN = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
WEB_DIR = os.path.join(HERE, "web")
PRESETS_JSON = os.path.join(HERE, "study", "presets.json")


def _positive(value):
    """A number > 0 as float, otherwise None (an optional requirement that is not set)."""
    try:
        value = None if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    return value if value is not None and 0.0 < value < float("inf") else None


def load_presets(path=PRESETS_JSON):
    """The example missions of presets.json in the shape the mission form uses."""
    presets = (read_json(path) or {}).get("presets", [])
    out = []
    for p in presets:
        objective_text = str(p.get("objective") or "").lower()
        objective = ("max_endurance" if objective_text.startswith("maximise endurance") else
                     "min_cd" if objective_text.startswith("minimise drag") else "max_ld")
        constraints = str(p.get("constraints") or "")
        thick = re.search(r"t/c\s*>=\s*([0-9.]+)", constraints)
        moment = re.search(r"\|Cm[^|]*\|\s*<=\s*([0-9.]+)", constraints)
        mach = float(p["mach"])
        label = str(p.get("label") or "")
        out.append({
            "id": re.sub(r"[^a-z0-9]+", "-", label.lower().split("(")[0]).strip("-")[:40],
            "label": label,
            "prompt": p.get("example_prompt") or "",
            "requirements": {
                "mach": mach, "density": float(p["density_kg_m3"]), "reynolds": float(p["reynolds"]),
                "design_cl": float(p["design_cl"]), "objective": objective,
                "min_thickness": float(thick.group(1)) if thick else None,
                "max_abs_cm": float(moment.group(1).rstrip(".")) if moment else None,
                "target_ld": _positive(p.get("target_ld")),
                "notes": "",
            },
            "objective_text": p.get("objective"),
            "details": constraints,
            "warning": (f"Mach {mach:g} is above 0.3: the solver is incompressible, so the result is "
                        "indicative only." if mach > 0.3 else None),
        })
    return out


class LocalOnly:
    """ASGI middleware: answer 403 to requests a page of another site could
    cause. CORS only hides the answer from such a page; this stops the request.

    - Host must be 127.0.0.1, localhost or [::1] (any port), which a DNS name
      rebound to this machine never is;
    - an Origin header, when present, must be a local origin (the browser
      sends it on every cross-origin fetch, form post and EventSource);
    - under /api, a request the browser itself marks as cross-site and that
      carries no Origin (an <img> or a link on another site) is refused too,
      because some GETs act: the viewer one starts a ParaView process.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def problem(scope):
        headers = {}
        for key, value in scope.get("headers") or []:
            headers.setdefault(key.decode("latin-1").lower(), value.decode("latin-1"))
        if not re.fullmatch(LOCAL_HOST, headers.get("host", "").strip().lower()):
            return "this server only answers to http://127.0.0.1 or http://localhost"
        origin = headers.get("origin")
        if origin is not None and not re.fullmatch(LOCAL_ORIGIN, origin.strip().lower()):
            return "requests from other sites are not accepted"
        if (origin is None and headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
                and scope.get("path", "").startswith("/api/")):
            return "requests from other sites are not accepted"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            problem = self.problem(scope)
            if problem:
                await JSONResponse({"error": problem}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(data_dir=None, viewer_factory=ViewerManager, completion=None):
    store = Store(data_dir)
    engine = Engine(store, viewer_factory=viewer_factory, completion=completion)
    closing = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        closing.set()
        # Stop pipelines, solver containers and viewer processes before the process exits.
        await asyncio.get_running_loop().run_in_executor(None, engine.shutdown)

    app = FastAPI(title="Wing Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.engine = store, engine
    app.add_middleware(CORSMiddleware, allow_origin_regex=LOCAL_ORIGIN,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(LocalOnly)       # added last = outermost: runs before CORS and the routes

    def error(message, status=400):
        return JSONResponse({"error": str(message)}, status_code=status)

    @app.exception_handler(EngineError)
    async def _engine_error(_request, exc):
        return error(str(exc), exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request, exc):
        return error(exc.detail, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request, exc):
        return error("invalid request: " + "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors()), 400)

    @app.exception_handler(Exception)
    async def _crash(_request, exc):
        return error(f"{type(exc).__name__}: {exc}", 500)

    def case_or_404(sid, cid):
        engine.study(sid)                     # 404 for an unknown study before the case
        return engine.case(sid, cid)

    def accepted(**extra):
        return JSONResponse(dict({"ok": True}, **extra), status_code=202)

    # ------------------------------------------------------------- config --

    @app.get("/api/config")
    def config():
        return {"models": MODELS, "presets": load_presets(),
                "default_project_id": store.default_project_id,
                "stage_defs": [{"key": k, "title": t, "agent": a} for k, t, a in planning.STAGES],
                # whether a model call can be made at all: a boolean, never the key itself
                "llm_ready": bool(os.environ.get("ANTHROPIC_API_KEY")) or engine.completion is not None}

    @app.get("/api/refs")
    def refs():
        """The reference catalogue behind the citation marks (wingstudio/refs/catalogue.json).
        Always 200: with a broken catalogue `entries` is empty and `error` says why."""
        return citations.public()

    @app.get("/api/refs/doc/{ref_id}")
    def ref_doc(ref_id: str):
        """The local document of one catalogue entry (wingstudio/refs/docs/<id>.md) with its
        source line. 404 for anything that is not a catalogue id with a document."""
        doc = citations.document(ref_id)
        if doc is None:
            raise EngineError("there is no local document for this reference", 404)
        return doc

    plan_settings = []                  # filled on first use: the names never change while running

    @app.get("/api/refs/runs")
    def ref_runs(setting: str = "", study: str = "", case: str = ""):
        """Earlier runs of similar cases and the value each used for one plan setting, for the
        citation pop-up. Read-only; an unknown study, case or setting gives an empty list."""
        if not plan_settings:
            plan_settings.extend(sorted(planning.plan_setting_names()))
        return citations.previous_runs(store, setting, study, case, plan_settings)

    # ----------------------------------------------------------- projects --

    @app.get("/api/projects")
    def projects():
        return {"projects": store.list_projects()}

    @app.post("/api/projects")
    def create_project(payload: dict = Body(default={})):
        name = str(payload.get("name") or "").strip()
        if not name:
            raise EngineError("a project needs a name")
        return store.create_project(name, str(payload.get("description") or ""))

    @app.get("/api/projects/{pid}/studies")
    def project_studies(pid: str):
        if store.get_project(pid) is None:
            raise EngineError(f"no such project: {pid}", 404)
        return {"studies": store.list_studies(pid)}

    # ------------------------------------------------------------ studies --

    @app.post("/api/studies")
    def create_study(payload: dict = Body(default={})):
        return engine.create_study(payload.get("project_id"), payload.get("prompt"),
                                   payload.get("requirements"), payload.get("model"), payload.get("mode"))

    @app.get("/api/studies/{sid}")
    def get_study(sid: str):
        snapshot = store.snapshot(sid)
        if snapshot is None:
            raise EngineError(f"no such study: {sid}", 404)
        return snapshot

    @app.post("/api/studies/{sid}/suggest")
    def suggest(sid: str):
        engine.suggest(sid)
        return accepted(status="suggesting")

    @app.post("/api/studies/{sid}/select")
    def select(sid: str, payload: dict = Body(default={})):
        engine.select(sid, payload.get("nacas"))
        return accepted(status="planning")

    @app.patch("/api/studies/{sid}/plan")
    def patch_plan(sid: str, payload: dict = Body(default={})):
        return engine.patch_plan(sid, payload.get("plan"))

    @app.post("/api/studies/{sid}/plan/revise")
    def revise_plan(sid: str, payload: dict = Body(default={})):
        engine.revise_plan(sid, payload.get("comment"))
        return accepted(status="planning")

    @app.post("/api/studies/{sid}/approve")
    def approve(sid: str, payload: dict = Body(default={})):
        engine.approve(sid, payload.get("mode"))
        return accepted(status="running")

    @app.post("/api/studies/{sid}/mode")
    def set_mode(sid: str, payload: dict = Body(default={})):
        return engine.set_mode(sid, payload.get("mode"))

    @app.post("/api/studies/{sid}/message")
    def message(sid: str, payload: dict = Body(default={})):
        return engine.message(sid, payload.get("text"), case_id=payload.get("case_id"),
                              stage=payload.get("stage"), quote=payload.get("quote"))

    @app.post("/api/studies/{sid}/control")
    def control(sid: str, payload: dict = Body(default={})):
        engine.control(sid, payload.get("action"), payload.get("case_id"))
        return {"ok": True}

    @app.post("/api/studies/{sid}/cases/{cid}/approve")
    def approve_case(sid: str, cid: str):
        engine.approve_case(sid, cid)
        return {"ok": True}

    @app.post("/api/studies/{sid}/cases/{cid}/rerun")
    def rerun(sid: str, cid: str, payload: dict = Body(default={})):
        engine.rerun(sid, cid, payload.get("stage"), payload.get("instruction"))
        return {"ok": True}

    # ------------------------------------------------------------- events --

    @app.get("/api/studies/{sid}/events")
    async def events(sid: str, request: Request, after: int = 0):
        engine.study(sid)
        if not after and request.headers.get("last-event-id", "").isdigit():
            after = int(request.headers["last-event-id"])     # a native EventSource reconnect
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        # Subscribe before replaying so nothing falls between the replay and the live stream.
        unsubscribe = store.subscribe(sid, lambda ev: loop.call_soon_threadsafe(queue.put_nowait, ev))

        def frame(ev):
            return f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n"

        async def stream():
            last, idle = int(after), 0.0
            try:
                for ev in store.events_after(sid, after):
                    last = ev["seq"]
                    yield frame(ev)
                while not closing.is_set():
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        idle += 1.0
                        if await request.is_disconnected():
                            break
                        if idle >= 15.0:
                            idle = 0.0
                            yield ": keep-alive\n\n"
                        continue
                    idle = 0.0
                    if ev["seq"] <= last:
                        continue                 # already sent in the replay
                    last = ev["seq"]
                    yield frame(ev)
            finally:
                unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------- case data and viewer --

    @app.get("/api/studies/{sid}/cases/{cid}/series")
    def series(sid: str, cid: str):
        case = case_or_404(sid, cid)
        return solver.case_series(os.path.join(ROOT, case["dir"]), store.get_study(sid)["conditions"]["velocity"])

    def viewer_or_503():
        viewer = engine.viewer
        if viewer is None:
            raise EngineError("the ParaView viewer is not available: " + (engine.viewer_error or "not installed"), 503)
        return viewer

    @app.get("/api/studies/{sid}/cases/{cid}/viewer")
    def viewer_get(sid: str, cid: str):
        case = case_or_404(sid, cid)
        case_dir = os.path.join(ROOT, case["dir"])
        if not os.path.isdir(os.path.join(case_dir, "constant", "polyMesh")):
            raise EngineError("this case has no mesh to show yet", 409)
        viewer = viewer_or_503()
        started = viewer.ensure(cid, case_dir)
        if not started.get("ok"):
            raise EngineError("the viewer could not be started: " + str(started.get("error")), 503)
        info = {"port": started.get("port"), "url": started.get("url")}
        store.update_case(sid, cid, lambda c: c.__setitem__("viewer", info))
        return {"url": info["url"], "port": info["port"], "state": viewer.state(cid)}

    @app.post("/api/studies/{sid}/cases/{cid}/viewer")
    def viewer_set(sid: str, cid: str, payload: dict = Body(default={})):
        case = case_or_404(sid, cid)
        viewer = viewer_or_503()
        if not viewer.ensure(cid, os.path.join(ROOT, case["dir"])).get("ok"):
            raise EngineError("the viewer is not running for this case", 503)
        changes = {k: payload[k] for k in ("mode", "field", "zoom", "time") if payload.get(k) is not None}
        result = viewer.set(cid, **changes)
        if isinstance(result, dict) and result.get("ok") is False:
            raise EngineError("the viewer rejected the change: " + str(result.get("error")), 400)
        return {"state": viewer.state(cid)}

    # ------------------------------------------------------------ metrics --

    @app.get("/api/metrics/runs")
    def metrics_runs(project_id: str = "", model: str = "", naca: str = ""):
        return {"runs": store.query_runs(project_id or None, model or None, naca or None)}

    @app.get("/api/metrics/summary")
    def metrics_summary(project_id: str = ""):
        return store.metrics_summary(project_id or None)

    # -------------------------------------------------------------- files --

    def _inside(path, base):
        base = os.path.realpath(base)
        return path == base or path.startswith(base + os.sep)

    @app.get("/files/{path:path}")
    def files(path: str):
        """Artifacts (agent_tools.file_url): only files inside a studio case directory,
        cases/st_*/, and no dot file anywhere. Not the data directory (metrics.db,
        projects.json, the study records have API endpoints) and not other cases."""
        full = os.path.realpath(os.path.join(ROOT, path))
        cases = os.path.realpath(os.path.join(ROOT, "cases"))
        parts = os.path.relpath(full, cases).split(os.sep) if _inside(full, cases) else []
        allowed = (len(parts) >= 2 and parts[0].startswith("st_")
                   and not any(part.startswith(".") for part in parts))
        if not allowed or not os.path.isfile(full):
            return error("no such file", 404)
        return FileResponse(full, headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------- static UI --

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith("api/"):
            return error("no such endpoint", 404)
        full = os.path.realpath(os.path.join(WEB_DIR, path or "index.html"))
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not _inside(full, WEB_DIR) or not os.path.isfile(full):
            if not path:
                return error("the UI is not installed (wingstudio/web/index.html is missing); the API is at /api", 404)
            return error("not found", 404)
        return FileResponse(full, headers={"Cache-Control": "no-cache"})

    return app


def main():
    import uvicorn
    load_env()
    if docker.cli("image", "inspect", IMAGE, timeout=20).returncode != 0:
        print(f"Docker image {IMAGE} not found; build it with: docker build -t {IMAGE} docker/", flush=True)
    port = int(os.environ.get("STUDIO_PORT", PORT))
    app = create_app()
    print(f"Wing Studio: http://{HOST}:{port}/", flush=True)
    uvicorn.run(app, host=HOST, port=port, log_level="warning", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
