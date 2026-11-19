#!/usr/bin/env pvpython
"""Interactive in-browser view of an OpenFOAM case: ParaView renders in this process and trame
streams the frames to the page (an <iframe> in the studio). One process shows one case, and every
tab on its port sees the same view. wingstudio/viewer/manager.py starts it with ParaView's own
interpreter and .venv-viewer:

  pvpython --venv .venv-viewer --force-offscreen-rendering wingstudio/viewer/viewer_server.py \
      --case cases/naca4412 --port 8811 --mode field --field U

When listening it prints {"ready": true, "url": ...}. Control without a restart:
  GET  /api/state                  case, mode, field, time and the lists to choose from
  POST /api/set {json}             mode, field, time, zoom, range, ui, reload
  GET  /api/screenshot.png?w=&h=   PNG of the current view

There is no authentication: the port answers only when Host (and Origin, if sent) is this
server's own address, so other sites and DNS names rebound to 127.0.0.1 get 403.
"""
import argparse
import asyncio
import os
import sys
import tempfile
import threading
import time

T_START = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render_case as rc  # noqa: E402  (the scene shared with the offscreen renderer)
from aiohttp import web  # noqa: E402
from paraview import simple  # noqa: E402
from trame.app import get_server  # noqa: E402
from trame.ui.vuetify3 import VAppLayout  # noqa: E402
from trame.widgets import html  # noqa: E402
from trame.widgets import paraview as pvw
from trame.widgets import vuetify3 as v3

T_IMPORTED = time.time()

SET_KEYS = ("mode", "field", "time", "zoom", "range", "ui", "reload")
MAX_SHOT = 4096     # largest screenshot edge in pixels (the image is allocated in this process)

# ----------------------------------------------------------------------------
# count frames that are actually pushed to browsers (proof of streaming)
# ----------------------------------------------------------------------------
FRAMES = dict(pushed=0, last=0.0, bytes=0)
try:
    from trame_vtk.modules.paraview.protocols.publish_image_delivery import (
        ParaViewWebPublishImageDelivery as _PID,
    )

    # still_render() produces every JPEG that push_render() then publishes on
    # "viewport.image.push.subscription". (publish itself is an instance
    # attribute set by wslink, so it cannot be wrapped at class level.)
    _orig_still = _PID.still_render

    def _counting_still_render(self, options):
        reply = _orig_still(self, options)
        try:
            img = reply.get("image") if reply else None
            if img:
                FRAMES["pushed"] += 1
                FRAMES["last"] = time.time()
                FRAMES["bytes"] += len(img)
        except Exception:
            pass
        return reply

    _PID.still_render = _counting_still_render
except Exception as _e:  # counting is optional
    print("frame counter not installed:", _e, file=sys.stderr)


def watch_parent(parent_pid):
    """Exit as soon as the process that started this viewer is gone."""
    def loop():
        while True:
            time.sleep(2.0)
            if os.getppid() != parent_pid:
                os._exit(0)
    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


def screenshot_png(view, width=None, height=None):
    w, h = [int(x) for x in view.ViewSize]
    res = [int(width or w), int(height or h)]
    fd, path = tempfile.mkstemp(suffix=".png", prefix="pvshot_")
    os.close(fd)
    try:
        simple.SaveScreenshot(path, view, ImageResolution=res)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# app
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", required=True)
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mode", choices=rc.MODES, default="mesh")
    ap.add_argument("--field", default="U")
    ap.add_argument("--time", default="latest")
    ap.add_argument("--zoom", default="auto")
    ap.add_argument("--rendering", choices=["remote", "local"], default="remote")
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--follow", type=float, default=0.0)
    ap.add_argument("--parent-pid", type=int, default=0)
    args, _unknown = ap.parse_known_args()

    if args.parent_pid:
        watch_parent(args.parent_pid)

    view = simple.CreateView("RenderView")
    view.ViewSize = list(rc.DEFAULT_SIZE)
    view.InteractionMode = "2D"  # pan/zoom only: no accidental edge-on rotation
    scene = rc.CaseScene(view, dark=args.dark, bar_pos=(0.84, 0.25))
    try:
        ok = scene.apply(args.case, args.mode, args.field, args.time, args.zoom)
    except Exception as e:
        ok, scene.error = False, "%s: %s" % (type(e).__name__, e)
    if not ok:
        print("WARNING: %s (the viewer starts anyway and loads the case once it is readable)"
              % scene.error, file=sys.stderr, flush=True)
    t_scene = time.time()

    server = get_server("viewer_%d" % args.port, client_type="vue3")
    state, ctrl = server.state, server.controller

    # ---- trame state <-> scene --------------------------------------------
    state.trame__title = "ParaView viewer"
    state.show_ui = not args.no_ui
    state.case_path = rc.case_dir_of(args.case)
    state.mode = args.mode
    state.field = args.field
    state.when = args.time      # "latest" or a time value as string
    state.zoom_req = None       # set to move the camera
    state.range_req = "auto"    # "auto" or "lo,hi"
    state.reload_tick = 0

    def publish_scene():
        d = scene.describe()
        state.fields = d["fields"]
        state.times = d["times"]
        state.cur_time = d["time"]
        state.cur_mode = d["mode"]
        state.status = d["error"] or "%s  |  %s%s  |  t=%s  |  %s cells" % (
            os.path.basename(d["case"] or ""),
            d["mode"],
            (" " + d["legend"]) if d["legend"] else "",
            "-" if d["time"] is None else ("%g" % d["time"]),
            d["cells"],
        )
        state.warn = "; ".join(d["warnings"])
        ts = d["times"]
        state.time_idx = ts.index(d["time"]) if d["time"] in ts else 0
        state.time_max = max(len(ts) - 1, 0)

    publish_scene()
    last = dict(reload=0, field=args.field)

    @state.change("case_path", "mode", "field", "when", "zoom_req", "range_req", "reload_tick")
    def on_change(case_path, mode, field, when, zoom_req, range_req, reload_tick, **_):
        reload = reload_tick != last["reload"]
        last["reload"] = reload_tick
        if field != last["field"]:
            # a fixed colour range belongs to the field it was given for
            if last.get("range_field") != field:
                range_req = state.range_req = "auto"
            last["field"] = field
        try:
            scene.apply(case_path, mode, field, when, zoom_req, reload=reload, crange=range_req)
        except Exception as e:  # keep the server alive whatever the case looks like
            scene.error = "%s: %s" % (type(e).__name__, e)
        state.zoom_req = None
        publish_scene()
        ctrl.view_update()

    @state.change("time_idx")
    def on_time_idx(time_idx, **_):
        ts = scene.times
        if ts and 0 <= int(time_idx) < len(ts) and ts[int(time_idx)] != scene.time:
            state.when = "%g" % ts[int(time_idx)]

    def zoom_to(name):
        state.zoom_req = name

    # ---- page --------------------------------------------------------------
    with VAppLayout(server, full_height=True):
        with html.Div(style="position:absolute; inset:0; overflow:hidden;"):
            if args.rendering == "remote":
                widget = pvw.VtkRemoteView(
                    scene.view,
                    interactive_ratio=1,
                    interactive_quality=70,
                    still_quality=95,
                    style="width:100%; height:100%;",
                )
            else:
                widget = pvw.VtkLocalView(scene.view, style="width:100%; height:100%;")
            ctrl.view_update = widget.update
            with html.Div(
                v_show="show_ui",
                style=(
                    "position:absolute; top:6px; left:6px; right:6px; display:flex; gap:8px;"
                    "flex-wrap:nowrap; overflow:hidden; white-space:nowrap;"
                    "align-items:center; padding:4px 8px; border-radius:8px;"
                    "background:rgba(255,255,255,0.86); font:12px system-ui; z-index:5;"
                ),
            ):
                with v3.VBtnToggle(
                    v_model=("mode",), density="compact", mandatory=True, divided=True,
                    variant="outlined", style="flex:none;",
                ):
                    v3.VBtn("Mesh", value="mesh", size="small")
                    v3.VBtn("Patches", value="patches", size="small")
                    v3.VBtn("Field", value="field", size="small")
                v3.VSelect(
                    v_model=("field",), items=("fields", []), density="compact", hide_details=True,
                    variant="outlined", style="max-width:130px; min-width:90px; flex:none;",
                    disabled=("mode !== 'field'",),
                )
                v3.VSlider(
                    v_model=("time_idx", 0), min=0, max=("time_max", 0), step=1, density="compact",
                    hide_details=True, style="max-width:220px; min-width:90px; flex:1 1 90px;",
                )
                html.Span("t = {{ cur_time }}", style="min-width:60px; flex:none;")
                for label, name in (("Body", "airfoil"), ("LE", "leading_edge"), ("TE", "trailing_edge"),
                                    ("Wake", "wake"), ("All", "full")):
                    v3.VBtn(label, size="small", variant="text", style="flex:none; min-width:0;",
                            click=(zoom_to, "['%s']" % name))
                html.Span("{{ status }}", style="opacity:0.7; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;")
                html.Span("{{ warn }}", style="color:#b45309;")

    # ---- HTTP control API on the same port ----------------------------------
    # No Access-Control-Allow-* headers: nothing calls this API from another origin.
    HEADERS = {"Cache-Control": "no-store"}

    def api_state_payload():
        d = scene.describe()
        d.update(
            requested=dict(mode=state.mode, field=state.field, time=state.when, range=state.range_req),
            port=server.port,
            rendering=args.rendering,
            ui=bool(state.show_ui),
            follow_s=args.follow,
            view_size=[int(x) for x in scene.view.ViewSize],
            frames_pushed=FRAMES["pushed"],
            frame_bytes=FRAMES["bytes"],
            last_frame_age_s=round(time.time() - FRAMES["last"], 2) if FRAMES["last"] else None,
            uptime_s=round(time.time() - T_START, 1),
            pid=os.getpid(),
        )
        d["ok"] = d["error"] is None
        return d

    def bad_request(message):
        return web.json_response(dict(ok=False, error=message), status=400, headers=HEADERS)

    async def api_state(_request):
        return web.json_response(api_state_payload(), headers=HEADERS)

    async def api_set(request):
        params = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                return bad_request("body is not JSON")
            if isinstance(body, dict):
                params.update(body)
        params = {k: v for k, v in params.items() if v is not None}
        unknown = [k for k in params if k not in SET_KEYS]
        if unknown:
            return bad_request("unknown keys: %s (allowed: %s)" % (unknown, ", ".join(SET_KEYS)))
        if "mode" in params and params["mode"] not in rc.MODES:
            return bad_request("mode must be one of: %s" % ", ".join(rc.MODES))
        zoom = rng = None
        try:
            if "zoom" in params:
                z = rc.parse_zoom(params["zoom"])
                zoom = z if isinstance(z, str) else ",".join(repr(float(x)) for x in z)
            if "range" in params:
                r = rc._parse_range(params["range"])
                rng = "auto" if r is None else "%r,%r" % (r[0], r[1])
        except (TypeError, ValueError) as e:
            return bad_request(str(e))
        with state:
            if "mode" in params:
                state.mode = params["mode"]
            if "field" in params:
                state.field = str(params["field"])
            if "time" in params:
                state.when = str(params["time"])
            if zoom is not None:
                state.zoom_req = zoom
            if rng is not None:
                state.range_req = rng
                last["range_field"] = str(params.get("field", state.field))
            if "ui" in params:
                state.show_ui = str(params["ui"]).lower() not in ("0", "false", "no", "off")
            if str(params.get("reload", "0")).lower() in ("1", "true", "yes"):
                state.reload_tick = state.reload_tick + 1
        # `with state` flushes -> on_change ran synchronously -> the scene is current
        await asyncio.sleep(0)
        return web.json_response(api_state_payload(), headers=HEADERS)

    async def api_screenshot(request):
        try:
            w = int(request.query.get("w", 0)) or None
            h = int(request.query.get("h", 0)) or None
        except ValueError:
            return bad_request("w and h must be whole numbers of pixels")
        try:
            w, h = [None if x is None else max(1, min(MAX_SHOT, x)) for x in (w, h)]
            png = screenshot_png(scene.view, w, h)
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e)), status=500, headers=HEADERS)
        return web.Response(body=png, content_type="image/png", headers=HEADERS)

    @web.middleware
    async def local_only(request, handler):
        """Page, websocket and API alike: only for this server's own address and origin."""
        names = {"127.0.0.1", "localhost", args.host.lower()}
        hosts = {"%s:%d" % (name, server.port) for name in names}
        origin = request.headers.get("Origin")
        if request.headers.get("Host", "").strip().lower() not in hosts:
            return web.json_response(dict(ok=False, error="unknown Host"), status=403, headers=HEADERS)
        if origin is not None and origin.strip().lower() not in {"http://" + h for h in hosts}:
            return web.json_response(dict(ok=False, error="requests from other origins are not accepted"),
                                     status=403, headers=HEADERS)
        return await handler(request)

    @ctrl.add("on_server_bind")
    def bind_routes(wslink_server):
        # the aiohttp application is not started (frozen) yet at this point
        wslink_server.app.middlewares.append(local_only)
        wslink_server.app.add_routes(
            [
                web.get("/api/state", api_state),
                web.post("/api/set", api_set),
                web.get("/api/screenshot.png", api_screenshot),
            ]
        )

    # ---- follow the case directory while a solver or mesher is working ------
    def disk_key():
        """What is on disk right now, or None while the mesh is being written."""
        case_dir = rc.case_dir_of(state.case_path)
        ok, _reason, sig = rc.mesh_status(case_dir)
        if not ok:
            return None
        latest = None
        if str(state.when) == "latest":
            complete, _pending = rc.complete_times(case_dir)
            latest = max(complete) if complete else None
        return (case_dir, sig, latest)

    async def follow_loop():
        seen = disk_key()
        while True:
            await asyncio.sleep(args.follow)
            try:
                key = disk_key()
                if key is not None and key != seen:
                    seen = key
                    with state:
                        state.reload_tick = state.reload_tick + 1
            except Exception as e:
                print("follow: %s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)

    async def resize_loop():
        """Keep the chosen view box framed when the browser resizes the view."""
        while True:
            await asyncio.sleep(0.4)
            try:
                if scene.refit_if_resized():
                    ctrl.view_update()
            except Exception as e:
                print("resize: %s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)

    @ctrl.add("on_server_ready")
    def ready(**_):
        loop = asyncio.get_event_loop()
        loop.create_task(resize_loop())
        if args.follow > 0:
            loop.create_task(follow_loop())
        rc.say(
            dict(
                ready=True,
                url="http://%s:%d/" % (args.host, server.port),
                port=server.port,
                pid=os.getpid(),
                startup_s=round(time.time() - T_START, 2),
                import_s=round(T_IMPORTED - T_START, 2),
                scene_s=round(t_scene - T_IMPORTED, 2),
                rendering=args.rendering,
                case=scene.case_dir,
                error=scene.error,
            )
        )

    server.start(
        host=args.host,
        port=args.port,
        open_browser=False,
        show_connection_info=False,
        timeout=0,  # never shut down when the last browser tab disconnects
    )


if __name__ == "__main__":
    main()
