"""pip 1.0 for Blender - ask the model you choose to build
something in the scene, or to explain how, and let an AI on this machine
drive Blender.

WHAT IT ADDS
  Ctrl+Shift+A   A pop-up in the 3D View (also Add > AI Model..., and the
                 pip 1.0 tab in the N sidebar). Pick a model, type
                 what you want, and choose:
                   Generate  a real, textured 3D model from an AI 3D
                          generator (TRELLIS), imported at real-world size.
                          From your words, or from a picture you choose.
                   Build  the model writes a bpy script and it runs here.
                          An error goes back to the model for a corrected
                          script, up to the attempts set; a failed try's
                          objects are removed. The result is one Ctrl+Z.
                   Ask    an answer in words, with Blender's menu paths.
  The bridge     HTTP on 127.0.0.1:8128 for blender_controller.py.

THE MODELS
  Auto (pip 1.0 first, then the next one set up), pip 1.0 from its
  engine (pip_server.py), pip 1.0, Qwen3 Coder on OpenRouter, any
  model installed in Ollama (local, no key), or a custom
  OpenAI-compatible server. Keys come from the add-on preferences, or
  from a .env file they point at - the app's own .env already has
  PIP_ENGINE_URL - or the pip 1.0 add-on's key.

THREE WAYS TO RUN IT
  Add-on         Edit > Preferences > Add-ons > Install from Disk.
  Scripting tab  Open this file in Blender's Text Editor and press Run
                 Script: the pop-up, the panel and the bridge appear for
                 this session. Running it again replaces them rather than
                 failing on a port the last run still holds.
  Headless       blender -b --factory-startup --python blender_listener.py
                     -- --run code.py --result out.json [--clear]
                     [--save out.blend] [--render out.png]
                 One script, one result file, then Blender exits. This is
                 what blender_controller.py falls back to when no Blender
                 window is listening.

THE PROTOCOL  (HTTP on 127.0.0.1:8128, JSON both ways)
  GET  /ping    No token. {"ok", "blender", "responsive", "lag"} - enough
                to tell "not running" from "running but stuck".
  GET  /status  The open file, scene, object counts, queue depth.
  POST /exec    {"code", "clear_scene", "rollback", "save_path",
                 "render_path", "timeout"} -> {"ok", "stdout", "stderr",
                 "error", "traceback", "line", "objects_created", ...}
  /status and /exec need the X-Bridge-Token header. The token is written
  to ~/.randomgenerals/blender-bridge.json when the bridge starts.

WHY A TIMER AND NOT THE SOCKET THREAD
bpy is not thread-safe. Calling it from the thread that accepted the
connection works until it doesn't, and then Blender crashes or corrupts
the undo stack. So the socket thread only queues a job and waits.
A bpy.app.timers callback, which Blender runs on its main thread between
redraws, executes it. The timer is registered persistent=True. Without
that, opening another .blend unregisters it, and from then on every
request queues and hangs while /ping still answers. The pop-up uses the
same queue: the model is asked on a worker thread, so Blender keeps
drawing for the seconds (or, locally, minutes) a reply takes, and only
the script itself runs on the main thread.

WHY A TOKEN ON LOCALHOST
Binding to 127.0.0.1 keeps other machines out, but not web pages. Any
site open in a browser can POST a text/plain body to 127.0.0.1:8128
without a CORS preflight, and /exec is arbitrary code execution. So
requests carrying an Origin header are refused, the Host header must be
a loopback name (against DNS rebinding), and /exec needs a token that
only processes running as this user can read from the bridge file.
"""

bl_info = {
    "name": "pip 1.0",
    "author": "Imrane Adib",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Ctrl+Shift+A, Add > AI Model, Sidebar (N) > pip 1.0",
    "description": "Ask the model you choose to build something in the scene, or to explain how.",
    "category": "3D View",
}

import contextlib
import hmac
import io
import json
import linecache
import os
import queue
import re
import secrets
import socket
import sys
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socketserver

try:
    import bpy
except ImportError:      # imported for testing outside Blender
    bpy = None


HOST = "127.0.0.1"
PORT = int(os.environ.get("RG_BLENDER_PORT") or 8128)
BRIDGE_FILE = Path(os.environ.get("RG_BLENDER_BRIDGE_FILE")
                   or Path.home() / ".randomgenerals" / "blender-bridge.json")

# The filename AI code is compiled under. Tracebacks are trimmed to start
# at the first frame with this name, so the model sees its own lines and
# not this file's plumbing.
SCRIPT_NAME = "<ai_script>"

MAX_BODY = 1_000_000          # bytes of request JSON
MAX_QUEUE = 8                 # jobs waiting for the main thread
MAX_OUTPUT = 20_000           # chars of stdout/stderr returned
PUMP_INTERVAL = 0.05          # seconds between main-thread checks
DEFAULT_EXEC_WAIT = 300       # how long /exec waits for its result
STATUS_WAIT = 5

# Data collections a failed script's leftovers are removed from. A failed
# attempt that built half a tree would otherwise stay in the scene under
# the corrected attempt's whole one.
_ROLLBACK_COLLECTIONS = ("objects", "meshes", "curves", "materials",
                         "lights", "cameras", "collections", "node_groups",
                         "textures", "images")

_STATE_KEY = "randomgenerals_bridge"
_RUNNER_KEY = "randomgenerals_runner"
_UI_KEY = "randomgenerals_ui"

# Where the running bridge, the job queue and the registered UI are kept,
# so a second run of this file - or the add-on after a script run - can
# find and stop the first. It has to outlive this module, which a Text
# Editor re-run replaces, and every file load. bpy.app.driver_namespace
# does the first but not the second: Blender empties it whenever a .blend
# loads, including the startup file just after --python runs, and from
# then on the server it held was still listening with nothing able to
# stop it. A module in sys.modules lives as long as Blender's Python.
_SESSION = sys.modules.setdefault(
    "_randomgenerals_session", type(sys)("_randomgenerals_session")).__dict__


# ------------------------------------------------------------ execution
# Everything in this section runs on Blender's main thread: from the
# timer when the bridge is serving, or directly in headless mode.

def _trim(text, limit=MAX_OUTPUT):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... [%d chars cut] ...\n" % (len(text) - limit) + text[-half:]


def _snapshot():
    """Pointers of every datablock that rollback knows how to remove."""
    snap = {}
    for attr in _ROLLBACK_COLLECTIONS:
        coll = getattr(bpy.data, attr, None)
        if coll is not None:
            snap[attr] = {db.as_pointer() for db in coll}
    return snap


def _rollback(before):
    """Remove datablocks created since `before`. -> names removed.

    Objects first, so the meshes and materials they used drop to zero
    users before their own collections are swept.
    """
    removed = []
    for attr in _ROLLBACK_COLLECTIONS:
        coll = getattr(bpy.data, attr, None)
        if coll is None:
            continue
        for db in [d for d in coll if d.as_pointer() not in before.get(attr, ())]:
            try:
                removed.append(db.name)
                coll.remove(db)
            except (RuntimeError, ReferenceError):
                pass
    return removed


def clear_scene():
    """Delete every object in the file, then the data nothing uses."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.context.scene.collection.children):
        bpy.data.collections.remove(coll)
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False,
                           do_recursive=True)


def _object_report(obj):
    rep = {"name": obj.name, "type": obj.type,
           "location": [round(v, 3) for v in obj.location]}
    if obj.type == "MESH" and obj.data is not None:
        rep["vertices"] = len(obj.data.vertices)
        rep["faces"] = len(obj.data.polygons)
        rep["materials"] = [m.name for m in obj.data.materials if m]
    if obj.modifiers:
        rep["modifiers"] = [m.type for m in obj.modifiers]
    return rep


def scene_summary():
    scene = bpy.context.scene
    objs = list(scene.objects)
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active if view_layer else None
    return {
        # What "the selected one" refers to when someone asks for an edit.
        "selected": [o.name for o in objs if o.select_get()][:20],
        "active": active.name if active else None,
        "file": bpy.data.filepath or None,
        "scene": scene.name,
        "objects": len(objs),
        "object_names": [o.name for o in objs[:60]],
        "meshes": sum(1 for o in objs if o.type == "MESH"),
        "camera": scene.camera.name if scene.camera else None,
        "lights": sum(1 for o in objs if o.type == "LIGHT"),
        "render_engine": scene.render.engine,
    }


def check_structure(objs):
    """Which of the new parts float free of the rest. -> report dict.

    Models get positions wrong far more often than syntax: the chair
    whose legs stand beside the seat, the canopy hovering next to the
    trunk. Those scripts run cleanly, so nothing else catches them. Two
    parts count as joined when their bounding boxes (world space, after
    modifiers) touch within a tolerance; the biggest joined group is the
    model, and anything outside it is floating. Hidden boolean cutters
    are left out - they are meant to be apart.
    """
    import mathutils
    bpy.context.view_layer.update()
    parts = []
    for obj in objs:
        if obj.type != "MESH" or obj.hide_render or obj.display_type in ("WIRE", "BOUNDS"):
            continue
        corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
        lo = [min(c[i] for c in corners) for i in range(3)]
        hi = [max(c[i] for c in corners) for i in range(3)]
        parts.append((obj.name, lo, hi))
    report = {"parts": len(parts), "floating": [], "gaps": {}, "lowest": None, "size": 0.0}
    if not parts:
        return report
    lo_all = [min(p[1][i] for p in parts) for i in range(3)]
    hi_all = [max(p[2][i] for p in parts) for i in range(3)]
    size = max(hi_all[i] - lo_all[i] for i in range(3))
    report["size"] = round(size, 3)
    report["lowest"] = round(lo_all[2], 3)
    if len(parts) < 2:
        return report
    tol = max(0.005, 0.02 * size)

    def gap(a, b):
        """Distance between two boxes; 0 when they touch or overlap."""
        d = [max(0.0, a[1][i] - b[2][i], b[1][i] - a[2][i]) for i in range(3)]
        return (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5

    group = list(range(len(parts)))

    def root(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if gap(parts[i], parts[j]) <= tol:
                group[root(i)] = root(j)
    groups = {}
    for i in range(len(parts)):
        groups.setdefault(root(i), []).append(i)
    main = max(groups.values(), key=len)
    for i in range(len(parts)):
        if i not in main:
            report["floating"].append(parts[i][0])
            report["gaps"][parts[i][0]] = round(min(gap(parts[i], parts[j]) for j in main), 3)
    return report


def _structure_error(report):
    """The failure handed back to the model, or None when it holds together."""
    problems = []
    if report["floating"]:
        named = ", ".join("%s (%.2f m from the nearest part)" % (n, report["gaps"][n])
                          for n in report["floating"][:8])
        problems.append("These parts float free of the rest of the model: %s. Parts that "
                        "are joined in the real thing must touch or overlap slightly - work "
                        "each position out from the dimensions of the part it attaches to." % named)
    if report["lowest"] is not None and report["size"] and \
            report["lowest"] > max(0.01, 0.05 * report["size"]):
        problems.append("The model hovers %.2f m above the ground: its lowest point should be "
                        "at Z = 0." % report["lowest"])
    if not problems:
        return None
    return ("The script ran, but the model does not hold together. " + " ".join(problems)
            + " If the request really is for separate objects, keep them apart and say so in "
            "a comment.")


def _viewport_override():
    """Context for operators that want a 3D view, when there is one.

    A timer callback runs with no area in its context, so operators that
    poll for a 3D view fail with "context is incorrect". Running the
    script as if from the first 3D viewport fixes those. Headless there
    is no viewport, and the plain context is all there is.
    """
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is not None:
                return {"window": window, "screen": screen,
                        "area": area, "region": region}
    return {}


def _script_traceback(exc):
    """The traceback from the script's first frame down, and its line."""
    if isinstance(exc, SyntaxError) and exc.filename == SCRIPT_NAME:
        return "".join(traceback.format_exception_only(type(exc), exc)), exc.lineno
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != SCRIPT_NAME:
        tb = tb.tb_next
    line = None
    walk = tb
    while walk is not None:
        if walk.tb_frame.f_code.co_filename == SCRIPT_NAME:
            line = walk.tb_lineno
        walk = walk.tb_next
    text = "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
    return text, line


def execute(code, clear=False, rollback=True, structure=False):
    """Run one script. -> result dict. Never raises.

    The script gets a fresh namespace each time, so a name a previous
    script defined cannot make a broken one look like it works.

    With structure=True a script that runs but leaves parts floating
    counts as failed, and is rolled back like one; check_structure()'s
    report is in result["structure"] either way.
    """
    started = time.perf_counter()
    out, err = io.StringIO(), io.StringIO()
    result = {"ok": False, "error": None, "traceback": None, "line": None,
              "source_line": None}

    # Registering the source with linecache is what puts the offending
    # line's text into the traceback. Without it a traceback through
    # compiled-from-a-string code shows only line numbers.
    lines = code.splitlines(True)
    linecache.cache[SCRIPT_NAME] = (len(code), None, lines, SCRIPT_NAME)

    if clear:
        try:
            clear_scene()
        except Exception as e:                                # noqa: BLE001
            result["error"] = "Clearing the scene failed: %s" % e

    before = _snapshot()
    names_before = {o.as_pointer() for o in bpy.data.objects}
    try:
        compiled = compile(code, SCRIPT_NAME, "exec")
        namespace = {"__name__": "__ai_script__", "bpy": bpy}
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            override = _viewport_override()
            if override:
                stack.enter_context(bpy.context.temp_override(**override))
            exec(compiled, namespace)
        result["ok"] = result["error"] is None
    except SystemExit:
        # Caught rather than let through: inside a GUI session it would
        # close Blender with the user's unsaved work in it.
        result["error"] = "The script called sys.exit() or quit(); scripts must not end Blender."
        result["traceback"] = result["error"]
    except BaseException as e:                                # noqa: BLE001
        result["error"] = "%s: %s" % (type(e).__name__, e)
        result["traceback"], result["line"] = _script_traceback(e)
    finally:
        linecache.cache.pop(SCRIPT_NAME, None)

    if result["line"] and 0 < result["line"] <= len(lines):
        result["source_line"] = lines[result["line"] - 1].rstrip()

    created = [o for o in bpy.data.objects if o.as_pointer() not in names_before]
    if result["ok"] and created:
        try:
            result["structure"] = check_structure(created)
        except Exception as e:                                # noqa: BLE001
            result["structure"] = {"error": str(e)}
        problem = _structure_error(result["structure"]) if structure and \
            "floating" in result["structure"] else None
        if problem:
            result.update(ok=False, error=problem, kind="structure")
    if not result["ok"] and rollback:
        result["rolled_back"] = _rollback(before)
        created = []
    result["objects_created"] = [_object_report(o) for o in created]
    result["stdout"] = _trim(out.getvalue())
    result["stderr"] = _trim(err.getvalue())
    result["scene"] = scene_summary()
    result["blender"] = bpy.app.version_string
    result["seconds"] = round(time.perf_counter() - started, 3)

    if (result["ok"] or clear) and not bpy.app.background:
        # One undo step for the whole script, so Ctrl+Z takes the AI's
        # model away in one press rather than one primitive at a time.
        # Pushed after a failed run too when it cleared the scene, so the
        # user's deleted objects are one Ctrl+Z away either way.
        try:
            with bpy.context.temp_override(**_viewport_override()):
                bpy.ops.ed.undo_push(message="pip 1.0 script")
        except Exception:                                     # noqa: BLE001
            pass
    if not bpy.app.background:
        _redraw()
    return result


def _redraw():
    for window in bpy.context.window_manager.windows:
        if window.screen is None:
            continue
        for area in window.screen.areas:
            area.tag_redraw()


def _frame_camera(scene):
    """Add a camera that sees every mesh, for a preview of a scene the
    script gave none. Reported in the result, never done silently."""
    import mathutils
    meshes = [o for o in scene.objects if o.type == "MESH"]
    coords = [o.matrix_world @ mathutils.Vector(c)
              for o in meshes for c in o.bound_box]
    cam_data = bpy.data.cameras.new("Preview Camera")
    cam = bpy.data.objects.new("Preview Camera", cam_data)
    scene.collection.objects.link(cam)
    cam.rotation_euler = (1.1, 0.0, 0.785)       # three-quarter view, from above
    scene.camera = cam
    if coords:
        bpy.context.view_layer.update()          # matrix_world from the rotation
        depsgraph = bpy.context.evaluated_depsgraph_get()
        loc, _scale = cam.camera_fit_coords(depsgraph, [v for c in coords for v in c])
        center = sum(coords, mathutils.Vector()) / len(coords)
        # A camera looks down its -Z, so +Z is straight back. The fit is
        # edge to edge; stepping back 20% leaves a margin round the model.
        back = cam.matrix_world.to_quaternion() @ mathutils.Vector((0, 0, 1))
        cam.location = loc + back * 0.2 * (loc - center).length
    else:
        cam.location = (7.0, -7.0, 5.0)
    return cam.name


def render_preview(path, resolution=(960, 720)):
    """Render the scene camera to `path`. -> notes about what was added.

    EEVEE first, because it shows the Principled BSDF colours the model
    set; Workbench if EEVEE cannot start (no GPU context headless on some
    machines), which still shows the viewport colours.
    """
    scene = bpy.context.scene
    notes = []
    if scene.camera is None:
        notes.append("added camera '%s'" % _frame_camera(scene))
    if not any(o.type == "LIGHT" for o in scene.objects):
        sun = bpy.data.objects.new("Preview Sun", bpy.data.lights.new("Preview Sun", "SUN"))
        sun.data.energy = 3.0
        sun.rotation_euler = (0.8, 0.2, 0.6)
        scene.collection.objects.link(sun)
        notes.append("added light 'Preview Sun'")
    if scene.world is None:
        scene.world = bpy.data.worlds.new("Preview World")
    r = scene.render
    # Put back afterwards: in a live session these are the user's own
    # output settings, and a preview is not a reason to change them.
    kept = (r.engine, r.filepath, r.resolution_x, r.resolution_y,
            r.resolution_percentage, r.image_settings.file_format,
            scene.display.shading.color_type)
    r.resolution_x, r.resolution_y = resolution
    r.resolution_percentage = 100
    r.image_settings.file_format = "PNG"
    r.filepath = str(path)
    last_error = None
    try:
        for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"):
            try:
                r.engine = engine
            except TypeError:
                continue                # not an engine in this version
            try:
                if engine == "BLENDER_WORKBENCH":
                    scene.display.shading.color_type = "MATERIAL"
                bpy.ops.render.render(write_still=True)
                notes.append("rendered with %s" % engine)
                last_error = None
                break
            except RuntimeError as e:
                last_error = e
    finally:
        (r.engine, r.filepath, r.resolution_x, r.resolution_y,
         r.resolution_percentage, r.image_settings.file_format,
         scene.display.shading.color_type) = kept
    if last_error is not None:
        raise RuntimeError("Render failed: %s" % last_error)
    return notes


def run_job(payload):
    """One /exec or headless run: execute, then save and render if asked."""
    result = execute(payload.get("code") or "",
                     clear=bool(payload.get("clear_scene")),
                     rollback=payload.get("rollback", True) is not False,
                     structure=bool(payload.get("structure")))
    if not result["ok"]:
        return result
    save_path = payload.get("save_path")
    if save_path:
        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            # copy=True writes the file without making it the open one,
            # so a live session's own file and title stay as they were.
            bpy.ops.wm.save_as_mainfile(filepath=str(save_path), copy=True)
            result["saved"] = str(save_path)
        except Exception as e:                                # noqa: BLE001
            result["save_error"] = str(e)
    render_path = payload.get("render_path")
    if render_path:
        try:
            Path(render_path).parent.mkdir(parents=True, exist_ok=True)
            result["render_notes"] = render_preview(render_path)
            result["rendered"] = str(render_path)
        except Exception as e:                                # noqa: BLE001
            result["render_error"] = str(e)
    return result


# ------------------------------------------------------------ the model
# No bpy in this section. blender_controller.py loads it too, so the
# prompt, the retry rules and the model clients are the same from the
# command line as from the pop-up.

USER_AGENT = "pip-Blender/0.3"
# pip 1.0, while pip_server.py is serving it. No key unless the engine
# was started with one (PIP_ENGINE_KEY).
PIP_URL = "http://127.0.0.1:8765/v1"
PIP_MODEL = "pip-1.0"
OLLAMA_URL = "http://127.0.0.1:11434"
# The model trained here (finetune/train_lora.py --task blender), while
# finetune/serve_blender.py is serving it.
OURS_URL = "http://127.0.0.1:8129/v1"
OURS_MODEL = "randomgenerals-blender"

# Model calls to localhost (Ollama) skip any proxy: with HTTP_PROXY set
# and no NO_PROXY entry for loopback, urllib would hand 127.0.0.1 to the
# proxy, which cannot see it.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# id -> (menu label, key name, base URL, model). Order is Auto's order,
# after pip 1.0, which needs no key and is asked first whenever its
# engine is up (see _one).
PROVIDERS = {
    "randomgenerals": ("pip 1.0 - website", "RG_API_KEY", "https://randomgenerals.com/v1", "auto"),
    "openrouter": ("Qwen3 Coder Next - OpenRouter", "OPENROUTER_API_KEY",
                   "https://openrouter.ai/api/v1", "qwen/qwen3-coder-next"),
}

# Every setting the model clients read, from the environment, a .env file
# or the add-on preferences.
KEY_NAMES = ("PIP_ENGINE_URL", "PIP_ENGINE_KEY", "OPENROUTER_API_KEY", "RG_API_KEY", "RG_BASE_URL",
             "OLLAMA_URL", "BLENDER_LLM_BASE_URL", "BLENDER_LLM_API_KEY",
             "BLENDER_LLM_MODEL", "RG_OURS_URL", "HF_TOKEN")


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    def __init__(self, message, wait):
        super().__init__(message)
        self.wait = wait


def _stderr(msg):
    print("[pip 1.0] " + msg, file=sys.stderr, flush=True)


def _retry_after(headers, message):
    """Seconds until a 429 clears: the header, else a "try again in
    1m2.5s" in the message, else None.

    None is a real answer, not a gap. A "Request too large ... output
    tokens per minute" refusal comes with neither, because it is not a
    window running out: the request itself is over the allowance, and
    waiting does not clear it.
    """
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", message or "")
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return None


def openai_llm(base_url, api_key, model, temperature=0.2, max_tokens=8000,
               timeout=240, max_wait=90, log=_stderr):
    """A messages -> text callable over any OpenAI-compatible endpoint.

    max_tokens is generous because reasoning models spend their
    thinking from the same budget, and a script cut off mid-line is a
    syntax error the retry loop then wastes an attempt on.

    A 429 that says when it clears, within max_wait, is waited out and
    asked once more: a few seconds' wait beats a weaker model. One that
    does not say is raised at once for the caller to move on - see
    _retry_after for the refusal that waiting does not fix.
    """
    url = base_url.rstrip("/") + "/chat/completions"

    def post(messages):
        body = json.dumps({"model": model, "messages": messages, "stream": False,
                           "temperature": temperature, "max_tokens": max_tokens}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json",
            # Not urllib's default: Cloudflare in front of randomgenerals.com
            # refuses "Python-urllib/x" with a bare 403 "error code: 1010".
            "User-Agent": USER_AGENT})
        local = "://127.0.0.1" in url or "://localhost" in url
        open_url = _DIRECT.open if local else urllib.request.urlopen
        try:
            with open_url(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read()[:600]
            if e.code == 403 and b"1010" in raw:
                raise LLMError("Cloudflare refused the request to %s (error 1010)." % url) from e
            try:
                msg = json.loads(raw).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                msg = raw.decode("utf-8", "replace")
            if e.code == 429:
                if "output tokens per minute" in msg:
                    msg = ("%s is capped at fewer output tokens a minute than a full "
                           "script needs. On Auto the next model answers instead." % model)
                raise RateLimited("%s returned 429: %s" % (url, msg),
                                  _retry_after(e.headers, msg)) from e
            if e.code == 401:
                msg = "the key was refused. " + msg
            raise LLMError("%s returned %d: %s" % (url, e.code, msg)) from e
        except urllib.error.URLError as e:
            raise LLMError("Could not reach %s (%s)." % (url, e.reason)) from e
        except (TimeoutError, ValueError, OSError) as e:
            raise LLMError("No usable reply from %s: %s" % (url, e)) from e
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("Unexpected reply shape from %s." % url) from e
        if not text.strip():
            raise LLMError("%s returned an empty reply (finish_reason %s)."
                           % (model, choice.get("finish_reason")))
        return text

    def call(messages):
        try:
            return post(messages)
        except RateLimited as e:
            if e.wait is None or e.wait > max_wait:
                raise
            log("%s is rate-limited; waiting %ds for it rather than switching model"
                % (model, round(e.wait) + 1))
            time.sleep(e.wait + 1)
            return post(messages)

    call.label = "%s via %s" % (model, base_url)
    call.model = model
    return call


def ollama_models(url=OLLAMA_URL, timeout=2.0):
    """Installed local models, or [] when Ollama is not running."""
    try:
        with _DIRECT.open(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except (OSError, ValueError, KeyError):
        return []


def ollama_llm(model, url=OLLAMA_URL, timeout=900):
    """Ollama's own /api/chat, not its OpenAI-compatible /v1.

    /v1 runs with Ollama's default context window, and the system prompt
    plus a script plus a traceback outgrow it. Ollama then drops the
    start of the conversation without saying so - the rules go first -
    and the retry is written blind. /api/chat takes num_ctx.
    """
    endpoint = url.rstrip("/") + "/api/chat"

    def call(messages):
        body = json.dumps({"model": model, "messages": messages, "stream": False,
                           "options": {"num_ctx": 16384, "temperature": 0.2}}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        try:
            with _DIRECT.open(req, timeout=timeout) as r:
                text = json.loads(r.read().decode("utf-8")).get("message", {}).get("content", "")
        except urllib.error.HTTPError as e:
            raise LLMError("Ollama returned %d for %s: %s" % (
                e.code, model, e.read()[:300].decode("utf-8", "replace"))) from e
        except (OSError, ValueError) as e:
            raise LLMError("Could not get a reply from Ollama (%s): %s" % (model, e)) from e
        if not text.strip():
            raise LLMError("Ollama's %s returned an empty reply." % model)
        return text

    call.label = "%s via Ollama (local)" % model
    call.model = model
    return call


def ours_up(url=OURS_URL, timeout=1.5, model=OURS_MODEL, key=""):
    """Is `model` being served at `url`? By default: the model trained
    here."""
    req = urllib.request.Request(
        url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer " + key} if key else {})
    try:
        with _DIRECT.open(req, timeout=timeout) as r:
            return model in r.read().decode("utf-8")
    except (OSError, ValueError):
        return False


def pip_up(keys, timeout=1.5):
    """Is the pip 1.0 engine answering?"""
    return ours_up(keys.get("PIP_ENGINE_URL") or PIP_URL, timeout, PIP_MODEL,
                   keys.get("PIP_ENGINE_KEY") or "")


def local_coder(models):
    """The installed model most likely to write code: a coder first."""
    for hint in ("qwen3-coder", "qwen2.5-coder", "coder", "qwen"):
        for m in models:
            if hint in m:
                return m
    return models[0] if models else None


def parse_env_file(path):
    """KEY=VALUE lines of a .env file -> dict. Later lines win, as they do
    for python-dotenv, which is what the app itself reads the file with."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key.strip()] = value
    return out


def _one(choice, keys, model, log):
    """One provider's callable, or None when it is not set up."""
    if choice == "custom":
        url = keys.get("BLENDER_LLM_BASE_URL")
        if not url:
            return None
        return openai_llm(url, keys.get("BLENDER_LLM_API_KEY") or "",
                          model or keys.get("BLENDER_LLM_MODEL") or "auto", log=log)
    if choice == "ollama" or choice.startswith("ollama:"):
        url = keys.get("OLLAMA_URL") or OLLAMA_URL
        name = model or choice.partition(":")[2] or local_coder(ollama_models(url))
        return ollama_llm(name, url) if name else None
    if choice == "pip":
        if not pip_up(keys):
            return None
        return openai_llm(keys.get("PIP_ENGINE_URL") or PIP_URL,
                          keys.get("PIP_ENGINE_KEY") or "", model or PIP_MODEL,
                          timeout=900, log=log)
    if choice == "ours":
        url = keys.get("RG_OURS_URL") or OURS_URL
        # No key, and a long timeout: a CPU writes a script in minutes.
        return openai_llm(url, "", OURS_MODEL, timeout=900, max_tokens=3000,
                          log=log) if ours_up(url) else None
    _label, key_name, base, default = PROVIDERS[choice]
    if not keys.get(key_name):
        return None
    if choice == "randomgenerals":
        base = keys.get("RG_BASE_URL") or base
    return openai_llm(base, keys[key_name], model or default, log=log)


def _missing(choice, keys):
    if choice == "custom":
        return "No custom server is set. Add its URL in the add-on preferences (or BLENDER_LLM_BASE_URL)."
    if choice == "ollama" or choice.startswith("ollama:"):
        return ("Ollama is not running at %s, or has no models. Start Ollama, then: "
                "ollama pull qwen2.5-coder:7b" % (keys.get("OLLAMA_URL") or OLLAMA_URL))
    if choice == "pip":
        return ("pip 1.0 is not running at %s. Start it with: python pip_server.py"
                % (keys.get("PIP_ENGINE_URL") or PIP_URL))
    if choice == "ours":
        return ("The model trained here is not being served. Train it with "
                "python -m finetune.train_lora --task blender, then start it with "
                "python -m finetune.serve_blender.")
    label, key_name = PROVIDERS[choice][:2]
    return ("%s needs %s. Paste it in the add-on preferences or point them at the "
            "app's .env file (or set it in the environment)." % (label, key_name))


def provider_ready(choice, keys):
    """Is the key there? No network: for labelling a menu."""
    if choice in PROVIDERS:
        return bool(keys.get(PROVIDERS[choice][1]))
    if choice == "custom":
        return bool(keys.get("BLENDER_LLM_BASE_URL"))
    return True


def make_llm(choice, keys, model=None, log=_stderr):
    """-> a messages -> text callable, with .label for the model behind it.

    `choice` is a PROVIDERS id, "custom", "ollama" or "ollama:<model>".
    None or "auto" is every provider that is set up, in order: one out of
    credits, rate-limited or down hands over to the next, and whichever
    answers is asked first next time. Local Ollama comes last - on a CPU
    it takes minutes - and is there for when nothing else answers.
    """
    if choice and choice != "auto":
        if choice not in PROVIDERS and choice not in ("custom", "ollama", "ours", "pip") \
                and not choice.startswith("ollama:"):
            raise LLMError("Unknown model %r." % choice)
        llm = _one(choice, keys, model, log)
        if llm is None:
            raise LLMError(_missing(choice, keys))
        return llm
    chain = [llm for llm in [_one("custom", keys, None, log),
                             _one("pip", keys, None, log)]
             + [_one(c, keys, None, log) for c in PROVIDERS]
             + [_one("ours", keys, None, log), _one("ollama", keys, None, log)]
             if llm is not None]
    if not chain:
        raise LLMError("No model is set up. Start pip 1.0 (python pip_server.py), add a key in "
                       "the add-on preferences, point them at the app's .env, or start Ollama.")
    if len(chain) == 1:
        return chain[0]

    def call(messages):
        errors = []
        for llm in list(chain):
            try:
                text = llm(messages)
            except LLMError as e:
                errors.append("%s: %s" % (llm.label, e))
                # Said out loud: the next in line may be the local model,
                # and minutes of silence look like a hang.
                log("%s failed (%s); trying the next model" % (llm.model, e))
                continue
            chain.remove(llm)
            chain.insert(0, llm)
            call.label, call.model = llm.label, llm.model
            return text
        raise LLMError("Every model that is set up failed:\n  " + "\n  ".join(errors))

    call.label = " -> ".join(llm.label for llm in chain)
    call.model = chain[0].model
    return call


SYSTEM_PROMPT = """\
You write Python scripts that build and change 3D models in Blender {version} through bpy.
Reply with ONE complete script in a single ```python code block and nothing else.

How it runs: exec() on Blender's main thread, in the scene that is open. There
may be no 3D viewport. The caller has already cleared the scene if asked to.
Change, move or delete objects that already exist only when the request asks
for it, naming them exactly as listed after the request ("the selected object"
means the selected or active one listed there); otherwise leave them alone.
The caller saves and renders, so never save, open, or quit.

Rules
- Imports: bpy, bmesh, math, mathutils, random only. No file, network,
  subprocess or os use. Never call sys.exit() or quit().
- When building something new, start with a collection named after it and make
  it active, so every operator adds into it:
    col = bpy.data.collections.new("Low Poly Tree")
    bpy.context.scene.collection.children.link(col)
    bpy.context.view_layer.active_layer_collection = \\
        bpy.context.view_layer.layer_collection.children[col.name]
- Geometry: bpy.ops.mesh.primitive_*_add(...) with location/rotation/scale as
  arguments, then obj = bpy.context.active_object and give it a clear name.
  bmesh or bpy.data.meshes for custom shapes. Do not use operators that need a
  viewport or edit mode: no bpy.ops.object.mode_set, bpy.ops.transform.*,
  bpy.ops.view3d.*, bpy.ops.screen.*. Set obj.location/rotation_euler/scale
  directly instead.
- Modifiers: obj.modifiers.new(name, 'SUBSURF'|'BEVEL'|'BOOLEAN'|'SOLIDIFY'|
  'ARRAY'|'MIRROR'|'DECIMATE'|...). Leave them unapplied. For BOOLEAN set
  mod.object and mod.operation, then hide the cutter with cutter.hide_render =
  True and cutter.display_type = 'WIRE'.
- Materials: mat = bpy.data.materials.new(name); mat.use_nodes = True;
  bsdf = mat.node_tree.nodes["Principled BSDF"]; set inputs by name -
  "Base Color" (RGBA, 4 values), "Metallic", "Roughness", "Emission Color",
  "Emission Strength", "Alpha", "Transmission Weight". Also set
  mat.diffuse_color to the same RGBA so the solid viewport shows the colour.
  obj.data.materials.append(mat).
- Shading: primitives are flat (good for low poly). For smooth surfaces call
  obj.data.shade_smooth(). There is no mesh.use_auto_smooth (removed in 4.1).
- Scale: metres, Z up, real-world size. The model stands on Z = 0, centred on
  the origin unless asked otherwise.
- Randomness: random.seed() with a fixed number, so a re-run builds the same thing.
- Camera and light: only if the request asks for a scene or render, or none
  exist. Point a camera with a TRACK_TO constraint (track_axis
  'TRACK_NEGATIVE_Z', up_axis 'UP_Y') at the model and set
  bpy.context.scene.camera. (Vector.to_track_quat takes '-Z' and 'Y', not
  the constraint's 'TRACK_NEGATIVE_Z'.) Light energy for a model about 1 m across:
  SUN 2-4, AREA 50-300 W at 2-3 m, POINT 100-500 W.
- Objects from bpy.ops are already in col - linking them again raises
  "already in collection". Only objects made with bpy.data.objects.new need
  col.objects.link(obj).
- Parts joined in the real object must touch or overlap slightly (a leg's top
  reaches into the seat). Compute each part's position from the dimensions you
  chose, with math for angled parts, rather than estimating.
- Use only API that exists in Blender {version}. When unsure a property
  exists, do without it.
- End with one print() line saying what was built or changed.
"""

ASK_PROMPT = """\
You are helping someone inside Blender {version}, who can see the viewport while
reading you. When the question is how to do something, answer as numbered
steps, each naming the exact path as Blender labels it ("Properties > Modifier
Properties > Add Modifier > Generate > Subdivision Surface") and the shortcut
when there is one. Keep it short: no preamble, no closing summary. Plain text
only - Blender cannot show Markdown, so no **bold**, # headings or tables. Put
code in a ```python block only when the question asks for a script.
"""


def system_prompt(version, mode="build"):
    return (ASK_PROMPT if mode == "ask" else SYSTEM_PROMPT).format(version=version)


_FENCE = re.compile(r"```[ \t]*(?:python|py|bpy)?[ \t]*\r?\n(.*?)```", re.S | re.I)
_OPEN_FENCE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\r?\n(.*)$", re.S | re.I)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_code(reply):
    """The script in a model reply, or "" when there is none.

    The longest fenced block wins: models that explain themselves despite
    being told not to tend to add a short usage snippet beside the real
    script. Reasoning models' <think> blocks are dropped first, since the
    drafts inside them are fenced too.
    """
    text = _THINK.sub("", reply or "")
    blocks = [b.strip() for b in _FENCE.findall(text) if b.strip()]
    if blocks:
        return max(blocks, key=len)
    unclosed = _OPEN_FENCE.search(text)        # reply cut off at max_tokens
    if unclosed and unclosed.group(1).strip():
        return unclosed.group(1).strip()
    text = text.strip()
    return text if "import bpy" in text else ""


def check_syntax(code):
    """A failed result in execute()'s shape, or None if it compiles.
    Checked before Blender is involved: a syntax error needs no Blender."""
    try:
        compile(code, SCRIPT_NAME, "exec")
        return None
    except SyntaxError as e:
        lines = code.splitlines()
        return {"ok": False, "mode": "syntax-check",
                "error": "SyntaxError: %s" % e.msg, "line": e.lineno,
                "source_line": lines[e.lineno - 1] if e.lineno and 0 < e.lineno <= len(lines) else None,
                "traceback": "".join(traceback.format_exception_only(SyntaxError, e))}


def feedback(result):
    """The message that hands a failure back to the model."""
    parts = ["That script failed%s." % (" in Blender " + result["blender"] if result.get("blender") else "")]
    parts.append(result.get("error") or "Unknown error.")
    if result.get("line"):
        parts.append("Line %s: %s" % (result["line"], (result.get("source_line") or "").strip()))
    if result.get("traceback") and result["traceback"] not in parts[1]:
        parts.append("Traceback:\n" + result["traceback"][-3000:])
    for stream in ("stdout", "stderr"):
        if (result.get(stream) or "").strip():
            parts.append("%s:\n%s" % (stream, result[stream][-1500:]))
    if result.get("rolled_back"):
        parts.append("Everything the failed run created was removed, so the scene is as it was before it ran.")
    parts.append("Send the whole corrected script in one ```python block. Fix the "
                 "cause of this error, and check the rest of the script for the same mistake.")
    return "\n\n".join(parts)


def scene_note(summary, cleared=False):
    """What the model is told about the scene, under the request."""
    if cleared or not summary or not summary.get("objects"):
        return "The scene is empty."
    names = summary.get("object_names") or []
    more = summary["objects"] - len(names)
    parts = ["The scene has %d objects: %s%s." % (
        summary["objects"], ", ".join(names), " and %d more" % more if more > 0 else "")]
    if summary.get("selected"):
        parts.append("Selected: %s." % ", ".join(summary["selected"]))
    if summary.get("active"):
        parts.append("Active: %s." % summary["active"])
    parts.append("Camera: %s. Lights: %d." % (summary.get("camera") or "none", summary.get("lights") or 0))
    return " ".join(parts)


# ------------------------------------------------------------- examples
# Scripts that ran in Blender, kept to show the model before it writes
# its own. Two files: the dataset finetune/make_blender_data.py builds
# (every script in it ran in a headless Blender and created objects), and
# the user's own, where "Keep as example" appends a build they liked.
# The same rows are the LoRA training set - see finetune/train_lora.py.

USER_EXAMPLES = Path(os.environ.get("RG_EXAMPLES_FILE")
                     or Path.home() / ".randomgenerals" / "blender-examples.jsonl")
EXAMPLE_MAX_CHARS = 6000      # about 1,700 tokens: one example, not a manual

_WORD = re.compile(r"[a-z]+")
# Words that say how to build, not what: two requests sharing only these
# are not alike. "low" and "poly" stay out of this list on purpose - a
# low-poly example is the right one to show for a low-poly request.
_STOPWORDS = frozenset("""a an the and or with without of on in into to for from by at as
    it its this that some any make build create model made add put give me please
    small big large simple nice little new one two three four five six few several
    each which who what how scene blender object objects shape shapes""".split())


def _words(text):
    out = set()
    for w in _WORD.findall((text or "").lower()):
        if len(w) > 2 and w not in _STOPWORDS:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def load_examples(*paths):
    """Rows with a request and a script, from JSONL files that may not exist."""
    rows = []
    for path in paths:
        if not path:
            continue
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("request") and row.get("code"):
                rows.append(row)
    return rows


def pick_example(request, examples):
    """The example whose request shares the most words with this one, or
    None when none shares any. A kept example wins a tie: someone looked
    at what it built and liked it. Shorter wins after that - cheaper to
    send, and less for the model to copy that the request did not ask for.
    """
    want = _words(request)
    best, best_key = None, (0,)
    for row in examples:
        shared = len(want & _words(row["request"]))
        if not shared:
            continue
        key = (shared, row.get("source") == "kept", -len(row["code"]))
        if key > best_key:
            best, best_key = row, key
    return best


def keep_example(request, code, model="", path=None, **extra):
    """Append a build the user liked to their examples file."""
    path = Path(path or USER_EXAMPLES)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict({"request": request.strip(), "code": code.strip(), "model": model,
                "source": "kept", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, **extra)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return path


def _example_block(example):
    code = example["code"]
    if len(code) > EXAMPLE_MAX_CHARS:
        code = code[:EXAMPLE_MAX_CHARS].rsplit("\n", 1)[0] + "\n# ... (rest cut)"
    return ("A script that ran in this Blender, for a different request (\"%s\"). "
            "Follow its conventions - the collection, the materials, how parts are "
            "named and how their positions are worked out from the dimensions - but "
            "build what is asked below, not this:\n```python\n%s\n```\n\n"
            % (example["request"], code))


def build_loop(prompt, llm, run, version, note="", max_attempts=3,
               require_objects=True, log=lambda _m: None, should_stop=lambda: False,
               example=None):
    """Ask for a script, run it, and on failure send the error back.

    run(code, structure) -> execute()'s result dict, wherever the script
    runs: this Blender, the bridge, or a headless one. structure=True asks
    it to fail a model whose parts float apart (check_structure). That is
    asked until it has failed once, and never on the last try: a request
    for separate things - a pair of dice - fails it legitimately, and
    after one round of "make these touch" the model's answer stands. -> {"ok", "code", "result",
    "attempts", "model"} plus "cancelled" when should_stop() said so.

    With require_objects, a script that runs cleanly but creates nothing
    is a failure: for a build into an empty scene it means the model
    defined functions and never called them. Off when the scene already
    has objects, because "make the chair red" rightly creates none.

    `example`, a row from pick_example(), is shown above the request.
    """
    request = (prompt.strip() + "\n\n" + note).strip()
    if example:
        log("Showing the model a working example: \"%s\"" % example["request"][:60])
        request = _example_block(example) + "Request: " + request
    messages = [{"role": "system", "content": system_prompt(version)},
                {"role": "user", "content": request}]
    attempts = []

    def finish(ok, **extra):
        last = attempts[-1] if attempts else {"code": "", "result": {}}
        return dict({"ok": ok, "code": last["code"], "result": last["result"],
                     "attempts": attempts, "model": getattr(llm, "model", "?")}, **extra)

    structure_asked = False
    for n in range(1, max_attempts + 1):
        if should_stop():
            return finish(False, cancelled=True)
        log("Attempt %d: asking the model" % n)
        reply = llm(messages)
        if should_stop():
            return finish(False, cancelled=True)
        code = extract_code(reply)
        if not code:
            result = {"ok": False, "error": "The reply had no ```python code block."}
        else:
            result = check_syntax(code)
            if result is None:
                log("Attempt %d: running %d lines" % (n, code.count("\n") + 1))
                result = run(code, structure=not structure_asked and n < max_attempts)
                if result.get("kind") == "structure":
                    structure_asked = True
                if result.get("ok") and require_objects and not result.get("objects_created"):
                    result = dict(result, ok=False, error=(
                        "The script ran without errors but created no objects."))
        attempts.append({"attempt": n, "code": code, "result": result})
        if result.get("ok"):
            made = len(result.get("objects_created") or [])
            log("Attempt %d: done - %s" % (n, "built %d objects" % made if made else "changed the scene"))
            return finish(True)
        log("Attempt %d failed: %s" % (n, (result.get("error") or "").strip().splitlines()[0][:200]
                                       if result.get("error") else "unknown error"))
        messages.append({"role": "assistant",
                         "content": "```python\n%s\n```" % code if code else reply})
        messages.append({"role": "user", "content": feedback(result)})
    return finish(False)


def ask(question, llm, version, note=""):
    """A plain answer, for the pop-up's Ask mode."""
    reply = llm([{"role": "system", "content": system_prompt(version, "ask")},
                 {"role": "user", "content": (question.strip() + "\n\n" + note).strip()}])
    return _THINK.sub("", reply).strip()


# -------------------------------------------------------- 3D generation
# Build has a language model write bpy, and a language model can only
# stack cubes, cylinders and spheres: the chair comes out as six boxes
# however good the model. Generate asks a model made for 3D instead.
# TRELLIS (Microsoft) turns one picture into a detailed, textured mesh,
# and the picture comes from a text-to-image model. Both are free:
# Pollinations needs no key, and TRELLIS runs on Hugging Face's shared
# GPUs, where a free Hugging Face token buys a real daily allowance
# (without one it is a few minutes a day per network, shared).

IMAGE_URL = "https://image.pollinations.ai/prompt/"
TRELLIS_SPACE = "https://trellis-community-trellis.hf.space"
GENERATED_DIR = Path.home() / ".randomgenerals" / "generated"

# What makes a picture reconstructable: one object, all of it in frame,
# seen from an angle that shows its depth, on a plain background the
# Space can cut away cleanly.
REFERENCE_STYLE = ("a single {what}, isolated on a pure white background, studio cutout, "
                   "the whole object in frame and centered, three-quarter view from slightly "
                   "above, soft even lighting, realistic materials, no room, no floor, no "
                   "other objects, no text, no people")


class GenerateError(Exception):
    pass


def reference_image(prompt, dest, size=1024, timeout=120):
    """A picture of the request to build from. -> dest."""
    import random
    import urllib.parse
    url = (IMAGE_URL + urllib.parse.quote(REFERENCE_STYLE.format(what=prompt.strip()), safe="")
           + "?" + urllib.parse.urlencode({"width": size, "height": size, "model": "flux",
                                           "nologo": "true", "seed": random.randint(1, 2 ** 31)}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                    timeout=timeout) as r:
            kind = r.headers.get("Content-Type") or ""
            body = r.read()
    except (OSError, ValueError) as e:
        raise GenerateError("Could not make the reference picture: %s" % e) from e
    if not kind.startswith("image/") or len(body) < 2048:
        raise GenerateError("The picture service sent back %s instead of an image." % (kind or "nothing"))
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(body)
    return dest


def _friendly_space_error(output):
    """A Space's error, made into something a person can act on."""
    msg = (output or {}).get("error") or ""
    if "ZeroGPU quota" in msg or "GPU quota" in msg:
        wait = re.search(r"Try again in ([0-9:]+)", msg)
        return ("Hugging Face's free GPU allowance is used up for now%s. A free Hugging Face "
                "token gives you your own, bigger allowance: make one at "
                "huggingface.co/settings/tokens (type Read) and paste it into the add-on "
                "preferences as the Hugging Face token."
                % (" (it comes back in %s)" % wait.group(1) if wait else ""))
    if not msg:
        return "The 3D generator failed without saying why. It is a shared service: try again."
    return "The 3D generator said: %s" % msg


class _Gradio:
    """Just enough of Gradio's queue protocol to drive a Space the way its
    own page does. That matters: TRELLIS makes a per-visitor folder when
    its page loads, and a bare API call that skips that step fails with
    an error that says nothing - so the page's steps are replayed here,
    in one session, in order."""

    def __init__(self, host, token=None, log=_stderr):
        import uuid
        self.host, self.log = host.rstrip("/"), log
        self.session = uuid.uuid4().hex[:11]
        self.headers = {"User-Agent": USER_AGENT}
        if token:
            # How a Hugging Face login reaches the Space, and with it the
            # caller's own GPU allowance rather than the anonymous one.
            self.headers["Authorization"] = "Bearer " + token
        self.config = self._json("GET", "/config")
        self.prefix = self.config.get("api_prefix") or ""

    def _json(self, method, path, body=None, ctype="application/json", timeout=60):
        req = urllib.request.Request(self.host + path, data=body, method=method,
                                     headers=dict(self.headers, **({"Content-Type": ctype} if body else {})))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise GenerateError("Hugging Face refused the token - check it in the add-on "
                                    "preferences, or clear it to use the anonymous allowance.") from e
            raise GenerateError("The 3D generator answered %d. It may be restarting - try "
                                "again in a minute." % e.code) from e
        except (OSError, ValueError) as e:
            raise GenerateError("Could not reach the 3D generator: %s" % e) from e

    def upload(self, path):
        import uuid
        boundary = uuid.uuid4().hex
        name = Path(path).name
        body = (("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode()
                + Path(path).read_bytes() + ("\r\n--%s--\r\n" % boundary).encode())
        remote = self._json("POST", self.prefix + "/upload", body,
                            "multipart/form-data; boundary=" + boundary)[0]
        return {"path": remote, "meta": {"_type": "gradio.FileData"}}

    def run(self, api_name, data, timeout=900):
        """One step, in this session. -> its outputs."""
        fn = next((i for i, d in enumerate(self.config.get("dependencies", []))
                   if d.get("api_name") == api_name), None)
        if fn is None:
            raise GenerateError("The 3D generator no longer has a '%s' step - its page has "
                                "changed, and this add-on needs updating." % api_name)
        self._json("POST", self.prefix + "/queue/join", json.dumps({
            "data": data, "fn_index": fn, "session_hash": self.session,
            "event_data": None, "trigger_id": None}).encode())
        url = self.host + self.prefix + "/queue/data?session_hash=" + self.session
        said = None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers),
                                        timeout=timeout) as stream:
                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    msg = json.loads(line[5:])
                    kind = msg.get("msg")
                    if kind == "estimation" and msg.get("rank"):
                        note = "In the 3D generator's queue: %d ahead" % msg["rank"]
                        if note != said:
                            self.log(note)
                            said = note
                    elif kind == "process_completed":
                        if not msg.get("success"):
                            raise GenerateError(_friendly_space_error(msg.get("output")))
                        return msg["output"]["data"]
        except (OSError, ValueError) as e:
            raise GenerateError("Lost the connection to the 3D generator: %s" % e) from e
        raise GenerateError("The 3D generator closed the connection without an answer.")

    def download(self, filedata, dest):
        url = filedata.get("url") if isinstance(filedata, dict) else None
        if not url:
            raise GenerateError("The 3D generator finished but returned no file.")
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                Path(dest).write_bytes(r.read())
        except OSError as e:
            raise GenerateError("Could not download the model: %s" % e) from e
        return dest


def trellis(image_path, dest_glb, token=None, log=_stderr, seed=0):
    """Picture -> textured GLB, on the TRELLIS Space. -> dest_glb."""
    space = _Gradio(TRELLIS_SPACE, token, log)
    space.run("start_session", [])
    log("Cutting the object out of the picture")
    clean = space.run("preprocess_image", [space.upload(image_path)])[0]
    log("Generating the 3D model (about a minute)")
    # The page's inputs, in its order: the picture, the multi-image
    # gallery, whether it is multi-image, the seed, the two stages'
    # guidance and steps, the multi-image method, mesh simplification
    # and the texture size.
    out = space.run("generate_and_extract_glb",
                    [clean, [], False, seed, 7.5, 12, 3.0, 12, "stochastic", 0.95, 1024])
    log("Downloading the model")
    return space.download(out[3] or out[2], dest_glb)


def estimate_height(prompt, llm):
    """How tall the thing is in the real world, in metres, so it arrives at
    its real size - a generated mesh has no scale of its own. 1 m if the
    model cannot say."""
    try:
        reply = llm([{"role": "user", "content":
                      "How tall is a typical %s in the real world, in metres? Reply with "
                      "one number only." % prompt.strip()}])
        value = float(re.findall(r"\d+(?:\.\d+)?", _THINK.sub("", reply))[0])
        return min(max(value, 0.02), 100.0)
    except (LLMError, IndexError, ValueError):
        return 1.0


# Runs in Blender through execute(), so it rolls back on failure and is
# one Ctrl+Z like any Build. Self-contained: the bridge, the pop-up and a
# headless Blender all run it the same way.
IMPORT_SCRIPT = '''
import bpy
from mathutils import Vector
path, name, height = {path!r}, {name!r}, {height!r}
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=path)
new = [o for o in bpy.data.objects if o not in before]
meshes = [o for o in new if o.type == "MESH"]
if not meshes:
    raise RuntimeError("The model file had no mesh in it.")
root = bpy.data.objects.new(name, None)
root.empty_display_size = 0.2
(meshes[0].users_collection[0] if meshes[0].users_collection else bpy.context.scene.collection).objects.link(root)
for o in new:
    if o.parent is None:
        o.parent = root
for m in meshes:
    m.data.shade_smooth()
    if not m.name.startswith(name):
        m.name = name + " mesh"

def bounds():
    bpy.context.view_layer.update()
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    return (Vector([min(p[i] for p in pts) for i in range(3)]),
            Vector([max(p[i] for p in pts) for i in range(3)]))

lo, hi = bounds()
tall = (hi.z - lo.z) or max(hi - lo) or 1.0
scale = height / tall
root.scale = Vector((scale, scale, scale))
# Shift the parts, not the parent, so the parent - the handle you grab to
# move the model - sits at the middle of its base, on the 3D cursor.
lo, hi = bounds()
base = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
for o in new:
    if o.parent is root:
        o.location -= base / scale
root.location = bpy.context.scene.cursor.location.copy()
lo, hi = bounds()
print("Imported %s: %d mesh%s, %.2f m tall" % (name, len(meshes), "" if len(meshes) == 1 else "es", hi.z - lo.z))
'''


def import_script(glb_path, name, height):
    return IMPORT_SCRIPT.format(path=str(glb_path), name=name[:60], height=float(height))


def generation_folder(prompt):
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "model"
    return GENERATED_DIR / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), slug))


# --------------------------------------------------------------- server

class _Job:
    __slots__ = ("kind", "payload", "done", "result", "started", "cancelled")

    def __init__(self, kind, payload=None):
        self.kind = kind
        self.payload = payload or {}
        self.done = threading.Event()
        self.result = None
        self.started = False
        self.cancelled = False


class _Runner:
    """The main-thread job queue. Shared by the bridge's socket threads and
    the pop-up's worker thread; one per Blender session."""

    def __init__(self):
        self.jobs = queue.Queue(maxsize=MAX_QUEUE)
        self.last_pump = time.time()
        self.busy = False
        self.jobs_run = 0
        self.hooks = []               # called every tick, on the main thread
        # Blender's timer registry keys on the callable's identity, and
        # every `self.pump` lookup makes a new bound-method object. One
        # object, kept, is the only thing unregister() will recognise.
        self.pump_fn = self.pump

    # ---- main thread

    def pump(self):
        """Timer callback. Always returns the next interval: returning
        None, or raising, unregisters the timer and stops every job from
        ever running again."""
        self.last_pump = time.time()
        for hook in list(self.hooks):
            try:
                hook()
            except Exception:                                 # noqa: BLE001
                traceback.print_exc()
        try:
            job = self.jobs.get_nowait()
        except queue.Empty:
            return PUMP_INTERVAL
        if job.cancelled:
            return 0.0
        job.started = True
        self.busy = True
        try:
            if job.kind == "status":
                job.result = dict(scene_summary(), ok=True)
            else:
                job.result = run_job(job.payload)
            self.jobs_run += 1
        except BaseException as e:                            # noqa: BLE001
            job.result = {"ok": False, "error": "Bridge error: %s" % e,
                          "traceback": traceback.format_exc()}
        finally:
            self.busy = False
            self.last_pump = time.time()
            job.done.set()
        return 0.0                     # another job may be waiting

    # ---- socket threads

    def submit(self, kind, payload, wait):
        job = _Job(kind, payload)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            return 503, {"ok": False, "error": "Blender is busy: %d jobs already queued." % MAX_QUEUE}
        if job.done.wait(wait):
            return 200, job.result
        if not job.started:
            job.cancelled = True
            return 504, {"ok": False, "error": (
                "Blender's main thread did not pick the job up within %ds. "
                "A modal dialog, a render or a long operation is holding it." % wait)}
        return 504, {"ok": False, "error": (
            "The script is still running after %ds. It will finish inside "
            "Blender, but its output is lost." % wait)}

    def lag(self):
        return round(time.time() - self.last_pump, 2)

    def fail_queued(self, why):
        """Answer everything still waiting, which would otherwise sit out
        its whole timeout."""
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            job.result = {"ok": False, "error": why}
            job.done.set()


def runner():
    """The session's runner, with its timer registered. Main thread only:
    bpy.app.timers is not safe to touch from another thread."""
    r = _SESSION.get(_RUNNER_KEY)
    if r is None:
        r = _SESSION[_RUNNER_KEY] = _Runner()
    if not bpy.app.timers.is_registered(r.pump_fn):
        bpy.app.timers.register(r.pump_fn, first_interval=0.0, persistent=True)
    return r


def _stop_runner():
    r = _SESSION.pop(_RUNNER_KEY, None)
    if r is None:
        return
    if bpy.app.timers.is_registered(r.pump_fn):
        bpy.app.timers.unregister(r.pump_fn)
    r.fail_queued("pip 1.0 was turned off.")


class _Bridge:
    """Everything one running bridge owns, kept together so a second
    start in the same Blender can find and stop the first."""

    def __init__(self, host, port, jobs_runner):
        self.host, self.port = host, port
        self.token = os.environ.get("RG_BLENDER_TOKEN") or secrets.token_urlsafe(24)
        self.runner = jobs_runner
        self.server = None
        self.thread = None
        self.started_at = time.time()
        self.blender = bpy.app.version_string if bpy else "?"


def _bind_error(e, port):
    code = getattr(e, "winerror", None) or e.errno
    if code in (10013,):
        return ("Windows refused port %d (WinError 10013). It is usually "
                "inside a range Hyper-V or WSL reserved - check with "
                "'netsh interface ipv4 show excludedportrange protocol=tcp' "
                "and set RG_BLENDER_PORT to a port outside it." % port)
    if code in (10048, 48, 98):
        return ("Port %d is already taken - most likely another Blender "
                "is running the bridge. Close it, or set RG_BLENDER_PORT." % port)
    return "Could not listen on port %d: %s" % (port, e)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # http.server turns SO_REUSEADDR on. On Windows that does not mean
    # "reuse a port in TIME_WAIT" as it does elsewhere: it lets a second
    # process bind the same port while the first still listens, and then
    # requests land on either one. A second Blender running this script
    # would take half the traffic with no error anywhere.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skips HTTPServer.server_bind, whose getfqdn() reverse lookup
        # can stall startup for seconds on Windows networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pipBridge/1"

        def log_message(self, fmt, *args):
            pass                     # Blender's console is noisy enough

        def _send(self, status, body):
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refused(self):
            """-> a reason to refuse, or None. See the module docstring."""
            if self.headers.get("Origin"):
                return 403, "Requests from web pages are refused."
            host = (self.headers.get("Host") or "").strip()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name.strip("[]").lower() not in ("127.0.0.1", "localhost", "::1"):
                return 403, "Host must be 127.0.0.1 or localhost."
            return None

        def _authorised(self):
            given = self.headers.get("X-Bridge-Token") or ""
            return hmac.compare_digest(given.encode(), bridge.token.encode())

        def do_GET(self):
            refused = self._refused()
            if refused:
                return self._send(refused[0], {"ok": False, "error": refused[1]})
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/ping":
                lag = bridge.runner.lag()
                return self._send(200, {
                    "ok": True, "service": "randomgenerals-blender-bridge",
                    "blender": bridge.blender, "busy": bridge.runner.busy,
                    # The timer not having run for a while while nothing
                    # of ours is busy means something else holds the main
                    # thread, and /exec would queue and wait.
                    "responsive": bridge.runner.busy or lag < 2.0, "lag": lag})
            if path == "/status":
                if not self._authorised():
                    return self._send(401, {"ok": False, "error": "Missing or wrong X-Bridge-Token."})
                status, body = bridge.runner.submit("status", None, STATUS_WAIT)
                body = dict(body, blender=bridge.blender, busy=bridge.runner.busy,
                            queued=bridge.runner.jobs.qsize(), jobs_run=bridge.runner.jobs_run,
                            uptime=round(time.time() - bridge.started_at),
                            lag=bridge.runner.lag(), port=bridge.port)
                return self._send(status, body)
            return self._send(404, {"ok": False, "error": "Unknown path. Try /ping, /status or POST /exec."})

        def do_POST(self):
            refused = self._refused()
            if refused:
                return self._send(refused[0], {"ok": False, "error": refused[1]})
            if self.path.split("?", 1)[0].rstrip("/") != "/exec":
                return self._send(404, {"ok": False, "error": "Unknown path."})
            if not self._authorised():
                return self._send(401, {"ok": False, "error": "Missing or wrong X-Bridge-Token."})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"ok": False, "error": "Send Content-Type: application/json."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"ok": False, "error": "Body must be 1 byte to %d bytes." % MAX_BODY})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"ok": False, "error": "Body is not valid JSON."})
            if not isinstance(payload, dict) or not isinstance(payload.get("code"), str) \
                    or not payload["code"].strip():
                return self._send(400, {"ok": False, "error": "Send {\"code\": \"...\"}."})
            try:
                wait = min(max(float(payload.get("timeout") or DEFAULT_EXEC_WAIT), 1.0), 3600.0)
            except (TypeError, ValueError):
                wait = DEFAULT_EXEC_WAIT
            status, body = bridge.runner.submit("exec", payload, wait)
            return self._send(status, body)

    return Handler


def _write_bridge_file(bridge):
    BRIDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"host": bridge.host, "port": bridge.port,
                       "token": bridge.token, "pid": os.getpid(),
                       "blender": bridge.blender,
                       "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2)
    # 0600 where the OS honours it. On Windows the home directory's ACL
    # already keeps other accounts out.
    fd = os.open(str(BRIDGE_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


def _remove_bridge_file(bridge):
    try:
        if json.loads(BRIDGE_FILE.read_text(encoding="utf-8")).get("token") == bridge.token:
            BRIDGE_FILE.unlink()
    except (OSError, ValueError):
        pass


def running():
    """The bridge serving in this Blender, or None."""
    return _SESSION.get(_STATE_KEY)


def start(port=None, host=HOST):
    """Start serving. Stops a bridge an earlier run left behind first -
    _SESSION outlives a script run, so the old server is still
    reachable from here even though the module that made it is gone."""
    stop()
    bridge = _Bridge(host, port or PORT, runner())
    try:
        bridge.server = _Server((host, bridge.port), _make_handler(bridge))
    except OSError as e:
        raise RuntimeError(_bind_error(e, bridge.port)) from e
    bridge.thread = threading.Thread(target=bridge.server.serve_forever,
                                     kwargs={"poll_interval": 0.25},
                                     name="randomgenerals-bridge", daemon=True)
    bridge.thread.start()
    _SESSION[_STATE_KEY] = bridge
    try:
        _write_bridge_file(bridge)
    except OSError as e:
        print("[pip 1.0] could not write %s (%s); clients need "
              "RG_BLENDER_TOKEN set to the token instead" % (BRIDGE_FILE, e))
    print("[pip 1.0] bridge listening on http://%s:%d (token in %s)"
          % (host, bridge.port, BRIDGE_FILE))
    return bridge


def stop():
    bridge = _SESSION.pop(_STATE_KEY, None)
    if bridge is None:
        return False
    if bridge.server is not None:
        bridge.server.shutdown()
        bridge.server.server_close()
    _remove_bridge_file(bridge)
    print("[pip 1.0] bridge stopped")
    return True


# ------------------------------------------------------------ headless

def _headless(argv):
    """blender -b --python blender_listener.py -- --run code.py --result out.json"""
    import argparse
    parser = argparse.ArgumentParser(prog="blender_listener.py")
    parser.add_argument("--run", required=True, help="script to execute")
    parser.add_argument("--result", required=True, help="where to write the JSON result")
    parser.add_argument("--clear", action="store_true", help="empty the scene first")
    parser.add_argument("--keep-failed", action="store_true", help="do not roll back a failed run")
    parser.add_argument("--save", help="save the .blend here")
    parser.add_argument("--render", help="render a PNG preview here")
    parser.add_argument("--structure", action="store_true",
                        help="count floating parts as a failure")
    args = parser.parse_args(argv)
    try:
        code = Path(args.run).read_text(encoding="utf-8")
        result = run_job({"code": code, "clear_scene": args.clear, "structure": args.structure,
                          "rollback": not args.keep_failed,
                          "save_path": args.save, "render_path": args.render})
    except BaseException as e:                                # noqa: BLE001
        result = {"ok": False, "error": "Headless runner failed: %s" % e,
                  "traceback": traceback.format_exc()}
    Path(args.result).write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ UI

def _wrap(text, width):
    """Lines for label() widgets, which do not wrap: long lines broken at
    words, blank lines and code indentation kept."""
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        out.extend(textwrap.wrap(line, width=max(20, width),
                                 subsequent_indent=" " * min(indent + 2, 8)) or [""])
    return out


def _plain(text):
    """Markdown the model used anyway, made readable as plain labels."""
    text = (text or "").replace("**", "").replace("__", "")
    return re.sub(r"(?m)^#{1,6}\s*", "", text)


# One request at a time. The worker thread writes here and the main
# thread draws from it; "job" lets a cancelled request's worker, still
# waiting on its reply, find out it is no longer the current one and
# drop what it gets rather than overwrite the next request's results.
_UI = {"job": 0, "running": False, "cancel": False, "mode": "BUILD", "prompt": "",
       "started": 0.0, "log": [], "answer": "", "code": "", "out": None, "model": "",
       "error": "", "dirty": False, "popup": False, "last_draw": 0.0, "status": None,
       "kept": "", "folder": ""}
_OLLAMA = {"models": [], "ours": False, "pip": False}


def _ui_update(jid, **fields):
    if _UI["job"] != jid:
        return
    _UI.update(fields)
    _UI["dirty"] = True


def _ui_log(jid, msg):
    if _UI["job"] != jid:
        return
    _UI["log"] = (_UI["log"] + [msg])[-40:]
    _UI["dirty"] = True


def _worker(jid, req, jobs):
    """One pop-up request, on its own thread. Never touches bpy: the
    script runs through `jobs`, the main-thread queue, like a bridge
    request does."""
    def log(msg):
        _ui_log(jid, msg)

    try:
        if req["mode"] == "MESH":
            _generate(jid, req, jobs, log)
            return
        llm = make_llm(req["choice"], req["keys"], log=log)
        if req["mode"] == "ASK":
            log("Asking %s" % ("the best model available" if req["choice"] in ("", "auto") else llm.model))
            answer = ask(req["prompt"], llm, req["version"], req["note"])
            _ui_update(jid, answer=_plain(answer), model=llm.model)
        else:
            def run(code, structure=False):
                return jobs.submit("exec", {"code": code, "clear_scene": req["clear"],
                                            "structure": structure}, 600)[1]
            example = pick_example(req["prompt"], load_examples(*req["examples"])) \
                if req["examples"] else None
            out = build_loop(req["prompt"], llm, run, req["version"], req["note"],
                             req["attempts"], req["require"], log,
                             should_stop=lambda: _UI["cancel"] or _UI["job"] != jid,
                             example=example)
            _ui_update(jid, out=out, code=out.get("code") or "", model=out.get("model") or "")
    except (LLMError, GenerateError) as e:
        _ui_update(jid, error=str(e))
    except Exception as e:                                    # noqa: BLE001
        traceback.print_exc()
        _ui_update(jid, error="%s: %s" % (type(e).__name__, e))
    finally:
        _ui_update(jid, running=False, popup=not _UI["cancel"])


def _generate(jid, req, jobs, log):
    """Generate mode, on the worker thread: a picture (made, or the user's),
    TRELLIS, then the import on the main thread."""
    folder = generation_folder(req["prompt"])
    folder.mkdir(parents=True, exist_ok=True)
    _ui_update(jid, folder=str(folder))
    image = req.get("image")
    if image:
        log("Using your picture: %s" % Path(image).name)
    else:
        log("Making a reference picture")
        image = reference_image(req["prompt"], folder / "reference.jpg")
    if _UI["job"] != jid:
        return
    log("Sending it to TRELLIS on Hugging Face%s"
        % ("" if req["keys"].get("HF_TOKEN") else " (no token: the small shared allowance)"))
    glb = trellis(image, folder / "model.glb", token=req["keys"].get("HF_TOKEN"), log=log)
    if _UI["job"] != jid:
        return
    try:
        height = estimate_height(req["prompt"], make_llm(req["choice"], req["keys"], log=log))
    except LLMError:
        height = 1.0
    log("Importing it, %.2f m tall" % height)
    name = req["prompt"].strip()[:1].upper() + req["prompt"].strip()[1:40]
    result = jobs.submit("exec", {"code": import_script(glb, name, height)}, 600)[1]
    _ui_update(jid, out={"ok": result.get("ok"), "result": result, "attempts": [1],
                         "generated": True, "height": height}, model="TRELLIS",
               error="" if result.get("ok") else "Generated, but the import failed: %s"
               % result.get("error"))


def _result_summary(width, limit):
    """-> (title, icon, lines) for the finished request."""
    if _UI["error"]:
        return "Could not get an answer", 'ERROR', _wrap(_UI["error"], width)[:limit]
    if _UI["mode"] == "ASK":
        lines = _wrap(_UI["answer"], width)
        if len(lines) > limit:
            lines = lines[:limit] + ["... the rest is in the sidebar: N > pip 1.0."]
        return "Answer from %s" % _UI["model"], 'INFO', lines
    out = _UI["out"] or {}
    if not out or out.get("cancelled"):
        return "", 'NONE', []
    if out.get("generated"):
        lines = ["Generated by TRELLIS and imported at %.2f m tall, on the 3D cursor."
                 % out.get("height", 1.0)]
        lines += _wrap("Scale it with S if the size is off. Ctrl+Z removes it.", width)
        lines += _wrap("Files (picture and .glb): %s" % _UI["folder"], width)
        return "Done", 'CHECKMARK', lines[:limit]
    tries = len(out.get("attempts") or [])
    res = out.get("result") or {}
    if out.get("ok"):
        made = res.get("objects_created") or []
        what = ("Built %d object%s" % (len(made), "" if len(made) == 1 else "s")) if made \
            else "Changed the scene"
        lines = ["%s with %s, on try %d." % (what, _UI["model"], tries)]
        names = [o["name"] for o in made]
        if names:
            lines += _wrap(", ".join(names[:12]) + (" ..." if len(names) > 12 else ""), width)
        said = (res.get("stdout") or "").strip().splitlines()
        if said:
            lines += _wrap(said[-1], width)
        floating = (res.get("structure") or {}).get("floating") or []
        if floating:
            lines += _wrap("Not touching the rest: %s." % ", ".join(floating[:6]), width)
        lines.append("Ctrl+Z takes it away in one step.")
        return "Done", 'CHECKMARK', lines[:limit]
    lines = ["Not built: %d tr%s failed. Anything they made was removed." % (tries, "y" if tries == 1 else "ies")]
    lines += _wrap("Last error: %s" % (res.get("error") or "unknown").strip(), width)[:6]
    lines.append("Try again, rephrase it, or pick another model.")
    return "Could not build it", 'ERROR', lines[:limit]


if bpy is not None:
    from bpy.props import (BoolProperty, EnumProperty, IntProperty,
                           PointerProperty, StringProperty)

    class _DefaultPrefs:
        """Stands in for the preferences when the file is run from the
        Text Editor, where there is no add-on entry to hold them."""
        env_file = openrouter_key = rg_key = hf_token = ""
        custom_url = custom_key = custom_model = ""
        ollama_url = OLLAMA_URL
        start_bridge = True
        header_button = True
        port = PORT
        examples_file = ""
        use_examples = True

    def _prefs(context=None):
        addon = (context or bpy.context).preferences.addons.get(__name__)
        if addon is not None and addon.preferences is not None:
            return addon.preferences
        return _DefaultPrefs

    def _env_path(p):
        """The .env file keys are read from: the one set in preferences,
        else RG_ENV_FILE, else the app checkout this file was opened from."""
        candidates = []
        if p.env_file:
            candidates.append(bpy.path.abspath(p.env_file))
        if os.environ.get("RG_ENV_FILE"):
            candidates.append(os.environ["RG_ENV_FILE"])
        try:
            candidates.append(Path(__file__).resolve().parent.parent / ".env")
        except (NameError, OSError):
            pass
        for text in bpy.data.texts:
            if text.filepath.endswith("blender_listener.py"):
                candidates.append(Path(bpy.path.abspath(text.filepath)).parent.parent / ".env")
        for c in candidates:
            if c and Path(c).is_file():
                return Path(c)
        return None

    def _examples_path(p):
        """The training data file: set in preferences, else the app's
        finetune/data/blender.jsonl beside the keys file it reads."""
        if p.examples_file:
            return Path(bpy.path.abspath(p.examples_file))
        roots = []
        env = _env_path(p)
        if env:
            roots.append(env.parent)
        try:
            roots.append(Path(__file__).resolve().parent.parent)
        except (NameError, OSError):
            pass
        for root in roots:
            candidate = root / "finetune" / "data" / "blender.jsonl"
            if candidate.is_file():
                return candidate
        return None

    _KEYS = {"at": 0.0, "keys": {}}

    def _keys(context=None, fresh=False):
        """Every key and URL the model clients read. Preferences win over
        the .env file, which wins over the environment. Cached for two
        seconds because menus ask on every redraw."""
        if not fresh and time.time() - _KEYS["at"] < 2.0:
            return _KEYS["keys"]
        context = context or bpy.context
        p = _prefs(context)
        keys = {k: v for k, v in os.environ.items() if k in KEY_NAMES and v}
        path = _env_path(p)
        if path:
            keys.update({k: v for k, v in parse_env_file(path).items() if k in KEY_NAMES and v})
        other = context.preferences.addons.get("randomgenerals")
        other = getattr(other, "preferences", None)
        if other is not None and getattr(other, "api_key", "") and not keys.get("RG_API_KEY"):
            keys["RG_API_KEY"] = other.api_key
            if getattr(other, "base_url", ""):
                keys.setdefault("RG_BASE_URL", other.base_url)
        for attr, name in (("hf_token", "HF_TOKEN"),
                           ("openrouter_key", "OPENROUTER_API_KEY"),
                           ("rg_key", "RG_API_KEY"), ("custom_url", "BLENDER_LLM_BASE_URL"),
                           ("custom_key", "BLENDER_LLM_API_KEY"), ("custom_model", "BLENDER_LLM_MODEL")):
            value = (getattr(p, attr, "") or "").strip()
            if value:
                keys[name] = value
        keys["OLLAMA_URL"] = (p.ollama_url or OLLAMA_URL).strip()
        _KEYS.update(at=time.time(), keys=keys)
        return keys

    def _refresh_ollama(url):
        """Ask Ollama what is installed, off the main thread: when it is
        not running the request takes its full timeout."""
        def work():
            _OLLAMA["models"] = ollama_models(url)
            _OLLAMA["ours"] = ours_up()
            _OLLAMA["pip"] = pip_up(_keys(bpy.context))
            _UI["dirty"] = True
        threading.Thread(target=work, name="randomgenerals-ollama", daemon=True).start()

    # Blender keeps raw pointers into the strings an items callback
    # returns, so every list handed out stays referenced here. Keyed by
    # content, it holds one list per distinct menu, not one per redraw.
    _ITEM_LISTS = {}

    def _model_items(self, context):
        keys = _keys(context) if context else {}
        items = [("auto", "Auto - best available",
                  "pip 1.0 first. If it is busy or not running, the next model that is set up answers",
                  'AUTO', 0)]
        items.append(("pip", "pip 1.0" if _OLLAMA["pip"] else "pip 1.0  (not running)",
                      "Our own model, from its engine. " + (
                          "Ready" if _OLLAMA["pip"] else "Start it with: python pip_server.py"),
                      'WORLD' if _OLLAMA["pip"] else 'UNLINKED', 40))
        for n, (pid, (label, key_name, _base, model)) in enumerate(PROVIDERS.items(), start=1):
            ready = provider_ready(pid, keys)
            items.append((pid, label if ready else label + "  (no key)",
                          "%s. %s" % (model, "Ready" if ready else "Needs %s - see the add-on preferences" % key_name),
                          'WORLD' if ready else 'UNLINKED', n))
        ready = provider_ready("custom", keys)
        items.append(("custom", "Custom server" if ready else "Custom server  (not set)",
                      keys.get("BLENDER_LLM_BASE_URL") or "Any OpenAI-compatible server - set it in the add-on preferences",
                      'URL', 50))
        if _OLLAMA["ours"]:
            items.append(("ours", "Ours - trained here",
                          "The Blender model trained in finetune/, served by finetune/serve_blender.py. "
                          "No key; a minute or two per script on this computer",
                          'EXPERIMENTAL', 60))
        for i, name in enumerate(_OLLAMA["models"]):
            items.append(("ollama:" + name, "Local - " + name,
                          "Runs on this computer through Ollama. No key; slow without a GPU",
                          'DESKTOP', 100 + i))
        return _ITEM_LISTS.setdefault(tuple(items), items)

    class RGAI_Props(bpy.types.PropertyGroup):
        model: EnumProperty(name="Model", description="Which model answers", items=_model_items)
        mode: EnumProperty(name="Mode", default="MESH", items=[
            ("MESH", "Generate", "A real, detailed, textured 3D model from an AI 3D generator "
             "(TRELLIS). About a minute or two", 'MESH_MONKEY', 2),
            ("BUILD", "Build", "A language model writes a script from simple shapes, and it runs "
             "in this scene. Good for edits, layouts and simple props", 'MODIFIER', 0),
            ("ASK", "Ask", "An answer in words, with Blender's menu paths", 'QUESTION', 1)])
        image_path: StringProperty(name="From a picture", subtype='FILE_PATH',
                                   description="Optional: build the model from this picture "
                                               "instead of making one from the words")
        prompt: StringProperty(name="Request", description="What to build, change or explain")
        clear_scene: BoolProperty(name="Clear the scene first", default=False,
                                  description="Delete everything in the scene before building. Ctrl+Z brings it back")
        attempts: IntProperty(name="Tries", default=3, min=1, max=6,
                              description="How many scripts the model may write when one fails")

    class RGAI_Preferences(bpy.types.AddonPreferences):
        bl_idname = __name__

        env_file: StringProperty(
            name="Keys file (.env)", subtype='FILE_PATH',
            description="A .env file to read keys from, such as the pip 1.0 app's own. "
                        "Read when a model is asked, so the keys stay in that file")
        openrouter_key: StringProperty(name="OpenRouter", subtype='PASSWORD',
                                       description="OPENROUTER_API_KEY, for Qwen3 Coder")
        rg_key: StringProperty(name="pip 1.0", subtype='PASSWORD',
                               description="Empty: the pip 1.0 add-on's key is used")
        hf_token: StringProperty(name="Hugging Face token", subtype='PASSWORD',
                                 description="Free, for Generate: your own GPU allowance on Hugging "
                                             "Face instead of the small shared one. "
                                             "huggingface.co/settings/tokens, type Read")
        ollama_url: StringProperty(name="Ollama", default=OLLAMA_URL)
        custom_url: StringProperty(name="Server URL",
                                   description="An OpenAI-compatible base URL, usually ending in /v1")
        custom_key: StringProperty(name="Key", subtype='PASSWORD')
        custom_model: StringProperty(name="Model")
        start_bridge: BoolProperty(name="Start the bridge for blender_controller.py", default=True)
        header_button: BoolProperty(name="AI button in the 3D View header", default=True)
        use_examples: BoolProperty(
            name="Show the model a working example", default=True,
            description="Put the closest verified script from the training data above each Build request")
        examples_file: StringProperty(
            name="Training data", subtype='FILE_PATH',
            description="finetune/data/blender.jsonl from the app. Empty: found next to the keys file")
        port: IntProperty(name="Port", default=PORT, min=1024, max=65535)

        def draw(self, context):
            layout = self.layout
            keys = _keys(context, fresh=True)
            box = layout.box()
            box.label(text="Keys", icon='LOCKED')
            box.prop(self, "env_file")
            found = _env_path(self)
            box.label(text="Reading keys from %s" % found if found
                      else "No .env file - paste keys below, or point at the app's .env.",
                      icon='CHECKMARK' if found else 'INFO')
            col = box.column()
            col.prop(self, "hf_token")
            col.prop(self, "openrouter_key")
            col.prop(self, "rg_key")
            col = box.column(align=True)
            for pid, (label, key_name, _base, _model) in PROVIDERS.items():
                ok = provider_ready(pid, keys)
                col.label(text="%s: %s" % (label, "ready" if ok else "needs " + key_name),
                          icon='CHECKMARK' if ok else 'X')
            box = layout.box()
            box.label(text="Models on this computer (Ollama, no key)", icon='DESKTOP')
            box.prop(self, "ollama_url")
            box.label(text=", ".join(_OLLAMA["models"]) or "Ollama is not running, or has no models.")
            box = layout.box()
            box.label(text="Custom server (OpenAI-compatible)", icon='URL')
            box.prop(self, "custom_url")
            box.prop(self, "custom_key")
            box.prop(self, "custom_model")
            box = layout.box()
            box.label(text="Examples - what the model is shown, and what training uses", icon='BOOKMARKS')
            box.prop(self, "use_examples")
            box.prop(self, "examples_file")
            found = _examples_path(self)
            n_data = len(load_examples(found)) if found else 0
            n_kept = len(load_examples(USER_EXAMPLES))
            box.label(text="%d verified in %s; %d you kept" % (
                n_data, found.name if found else "no training data file", n_kept))
            box = layout.box()
            box.label(text="Bridge", icon='LINKED')
            row = box.row()
            row.prop(self, "start_bridge")
            row.prop(self, "port")
            box = layout.box()
            box.label(text="Shortcut - click it and press the keys you want", icon='EVENT_A')
            _draw_shortcut(box, context)
            box.prop(self, "header_button")

    def _start(context):
        """Start a request from the panel or pop-up. -> None, or why not."""
        props = context.window_manager.randomgenerals_ai
        prompt = props.prompt.strip()
        image = bpy.path.abspath(props.image_path) if props.mode == "MESH" and props.image_path else ""
        if image and not Path(image).is_file():
            return "That picture is not there: %s" % image
        if not prompt and image:
            prompt = Path(image).stem.replace("_", " ").replace("-", " ")
        if not prompt:
            return "Type what you want first."
        if _UI["running"]:
            return "Still working on the last request."
        summary = scene_summary()
        cleared = props.mode == "BUILD" and props.clear_scene
        req = {"mode": props.mode, "prompt": prompt, "choice": props.model or "auto", "image": image,
               "keys": _keys(context, fresh=True),
               "version": bpy.app.version_string.split(" ")[0],
               "note": scene_note(summary, cleared), "attempts": props.attempts,
               "clear": cleared, "require": cleared or not summary["objects"],
               "examples": [str(_examples_path(_prefs(context)) or ""), str(USER_EXAMPLES)]
               if props.mode == "BUILD" and _prefs(context).use_examples else []}
        jid = _UI["job"] + 1
        _UI.update(job=jid, running=True, cancel=False, mode=props.mode, prompt=prompt,
                   started=time.time(), log=[], answer="", code="", out=None, model="",
                   error="", popup=False, kept="", folder="", dirty=True)
        threading.Thread(target=_worker, args=(jid, req, runner()),
                         name="randomgenerals-ai", daemon=True).start()
        return None

    def _set_status(text):
        """The status bar along the bottom of the window: progress is
        visible there even with the sidebar closed."""
        if text == _UI["status"]:
            return
        _UI["status"] = text
        for window in bpy.context.window_manager.windows:
            try:
                with bpy.context.temp_override(window=window):
                    window.workspace.status_text_set(text)
            except Exception:                                 # noqa: BLE001
                pass

    def _show_result():
        title, icon, lines = _result_summary(95, 36)
        wm = bpy.context.window_manager
        if not lines or not wm.windows:
            return

        def draw(menu, _context):
            col = menu.layout.column(align=True)
            for line in lines:
                col.label(text=line)

        with bpy.context.temp_override(window=wm.windows[0]):
            wm.popup_menu(draw, title=title, icon=icon)

    def _ui_tick():
        """Runs on every pump of the main-thread queue."""
        now = time.time()
        if _UI["running"] and now - _UI["last_draw"] > 0.5:
            _UI["dirty"] = True              # the seconds counter moves
        if not _UI["dirty"]:
            return
        _UI["dirty"] = False
        _UI["last_draw"] = now
        if not bpy.app.background:
            _redraw()
            if _UI["running"]:
                last = _UI["log"][-1] if _UI["log"] else "Starting"
                _set_status("pip 1.0: %s  (%ds)" % (last, now - _UI["started"]))
            else:
                _set_status(None)
            if _UI["popup"]:
                _UI["popup"] = False
                _show_result()

    def _chars(context):
        """About how many characters fit across the region being drawn."""
        region = getattr(context, "region", None)
        width = region.width if region is not None else 300
        return max(24, int(width / (7.0 * context.preferences.system.ui_scale)) - 4)

    def _prop(layout, data, name, **kw):
        """layout.prop with a placeholder where Blender has them (4.3+)."""
        try:
            layout.prop(data, name, **kw)
        except TypeError:
            kw.pop("placeholder", None)
            layout.prop(data, name, **kw)

    def _draw_request(layout, context, popup):
        props = context.window_manager.randomgenerals_ai
        row = layout.row(align=True)
        row.prop(props, "model", text="")
        row.operator("randomgenerals.ai_refresh", text="", icon='FILE_REFRESH')
        layout.row().prop(props, "mode", expand=True)
        col = layout.column()
        if popup:
            col.activate_init = True         # the cursor starts in the box
        _prop(col, props, "prompt", text="", placeholder={
            "MESH": "e.g. a wooden rocking chair, a red sports car, a cartoon fox",
            "BUILD": "e.g. a low-poly tree, or: make the selected chair red",
        }.get(props.mode, "e.g. how do I UV unwrap this?"))
        if props.mode == "BUILD":
            row = layout.row()
            row.prop(props, "clear_scene")
            row.prop(props, "attempts")
        elif props.mode == "MESH":
            layout.prop(props, "image_path", text="Or from a picture")
            if not _keys(context).get("HF_TOKEN"):
                layout.label(text="No Hugging Face token: a few models a day, shared. A free "
                                  "token (Preferences) gives you your own.", icon='INFO')
        if popup:
            layout.label(text="Progress shows in the status bar. The result pops up when it is ready.",
                         icon='INFO')

    def _draw_outcome(layout, context):
        width = _chars(context)
        if _UI["log"]:
            col = layout.box().column(align=True)
            for line in _UI["log"][-6:]:
                for piece in _wrap(line, width)[:2]:
                    col.label(text=piece)
        if _UI["running"]:
            return
        if _UI["error"]:
            col = layout.box().column(align=True)
            for i, line in enumerate(_wrap(_UI["error"], width)[:14]):
                col.label(text=line, icon='ERROR' if i == 0 else 'NONE')
            return
        if _UI["mode"] == "ASK" and _UI["answer"]:
            box = layout.box()
            box.label(text="Answer from %s" % _UI["model"], icon='INFO')
            col = box.column(align=True)
            lines = _wrap(_UI["answer"], width)
            for line in lines[:60]:
                col.label(text=line)
            if len(lines) > 60:
                col.label(text="...")
            box.operator("randomgenerals.ai_open_text", text="Open in the Text Editor",
                         icon='TEXT').what = "ANSWER"
        elif _UI["out"] and not _UI["out"].get("cancelled"):
            title, icon, lines = _result_summary(width, 14)
            box = layout.box()
            box.label(text=title, icon=icon)
            col = box.column(align=True)
            for line in lines:
                col.label(text=line)
            if _UI["code"]:
                row = box.row(align=True)
                row.operator("randomgenerals.ai_open_text", text="Open the script",
                             icon='TEXT').what = "CODE"
                if _UI["out"].get("ok"):
                    if _UI["kept"]:
                        row.label(text="Kept", icon='CHECKMARK')
                    else:
                        row.operator("randomgenerals.ai_keep", text="Keep as example",
                                     icon='SOLO_ON')

    class RGAI_OT_popup(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_popup"
        bl_label = "pip 1.0"
        bl_description = "Ask the model you choose to build something here, or how to do something"

        def invoke(self, context, event):
            if _UI["running"]:
                self.report({'INFO'}, "Still working on the last request - see the status bar")
                return {'CANCELLED'}
            _refresh_ollama(_prefs(context).ollama_url)
            return context.window_manager.invoke_props_dialog(
                self, width=560, title="pip 1.0", confirm_text="Go")

        def draw(self, context):
            _draw_request(self.layout, context, popup=True)

        def execute(self, context):
            problem = _start(context)
            if problem:
                self.report({'WARNING'}, problem)
                return {'CANCELLED'}
            return {'FINISHED'}

    class RGAI_OT_run(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_run"
        bl_label = "Go"
        bl_description = "Send the request to the chosen model"

        def execute(self, context):
            problem = _start(context)
            if problem:
                self.report({'WARNING'}, problem)
                return {'CANCELLED'}
            return {'FINISHED'}

    class RGAI_OT_cancel(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_cancel"
        bl_label = "Cancel"
        bl_description = ("Stop waiting. A reply already on its way is thrown away, "
                          "and a script already running finishes")

        def execute(self, context):
            # A new job id retires the old one: its worker, still waiting
            # on the model, finds it is no longer current and drops the
            # reply instead of writing it over the panel.
            _UI.update(job=_UI["job"] + 1, cancel=True, running=False, dirty=True)
            _UI["log"] = _UI["log"] + ["Cancelled."]
            return {'FINISHED'}

    class RGAI_OT_refresh(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_refresh"
        bl_label = "Refresh models"
        bl_description = "Look again for keys and for models installed in Ollama"

        def execute(self, context):
            _KEYS["at"] = 0.0
            _refresh_ollama(_prefs(context).ollama_url)
            return {'FINISHED'}

    class RGAI_OT_open_text(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_open_text"
        bl_label = "Open in the Text Editor"
        bl_description = "Put it in a text block, to read, edit or run yourself"

        what: EnumProperty(items=[("CODE", "Script", ""), ("ANSWER", "Answer", "")])

        def execute(self, context):
            name, body = (("pip 1.0 script.py", _UI["code"]) if self.what == "CODE"
                          else ("pip 1.0 answer.txt", _UI["answer"]))
            text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
            text.clear()
            text.write(body)
            shown = False
            for area in context.screen.areas:
                if area.type == "TEXT_EDITOR":
                    area.spaces.active.text = text
                    shown = True
                    break
            self.report({'INFO'}, "Opened '%s'" % name if shown else
                        "Saved as text '%s' - the Scripting tab shows it" % name)
            return {'FINISHED'}

    class RGAI_OT_keep(bpy.types.Operator):
        bl_idname = "randomgenerals.ai_keep"
        bl_label = "Keep as example"
        bl_description = ("This build came out well: show it to the model next time "
                          "something like it is asked for, and train on it")

        def execute(self, context):
            out = _UI["out"] or {}
            if not (out.get("ok") and _UI["code"]):
                self.report({'WARNING'}, "Only a successful build can be kept.")
                return {'CANCELLED'}
            made = (out.get("result") or {}).get("objects_created") or []
            path = keep_example(_UI["prompt"], _UI["code"], _UI["model"],
                                blender=bpy.app.version_string.split(" ")[0],
                                objects=len(made))
            _UI.update(kept=str(path), dirty=True)
            self.report({'INFO'}, "Kept in %s" % path)
            return {'FINISHED'}

    class RGAI_OT_bridge_toggle(bpy.types.Operator):
        bl_idname = "randomgenerals.bridge_toggle"
        bl_label = "Start / stop the bridge"
        bl_description = "Let blender_controller.py on this machine run scripts in this Blender"

        def execute(self, context):
            if running():
                stop()
                return {'FINISHED'}
            try:
                start(_prefs(context).port)
            except RuntimeError as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            return {'FINISHED'}

    class RGAI_PT_main(bpy.types.Panel):
        bl_label = "AI"
        bl_idname = "RGAI_PT_main"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "pip 1.0"
        bl_order = 0

        def draw(self, context):
            layout = self.layout
            _draw_request(layout, context, popup=False)
            if _UI["running"]:
                row = layout.row(align=True)
                row.label(text="Working... %ds" % (time.time() - _UI["started"]), icon='SORTTIME')
                row.operator("randomgenerals.ai_cancel", text="Cancel", icon='CANCEL')
            else:
                props = context.window_manager.randomgenerals_ai
                row = layout.row()
                row.scale_y = 1.4
                row.operator("randomgenerals.ai_run", icon='PLAY', text={
                    "MESH": "Generate", "BUILD": "Build"}.get(props.mode, "Ask"))
            _draw_outcome(layout, context)

    def _draw_shortcut(layout, context):
        """The pop-up's shortcut as Blender's own key widget: click it,
        press any key or combination, and that is the new shortcut.

        Drawn from the user keyconfig, not the add-on one it was created
        in - the user copy is the one Blender reads, saves with the
        preferences, and shows in Preferences > Keymap.
        """
        import rna_keymap_ui
        kc = context.window_manager.keyconfigs.user
        km = kc.keymaps.get("3D View") if kc else None
        kmi = next((k for k in km.keymap_items if k.idname == "randomgenerals.ai_popup"), None) \
            if km else None
        if kmi is None:
            layout.label(text="No shortcut: turn the add-on off and on to restore it.", icon='INFO')
            return
        col = layout.column()
        col.context_pointer_set("keymap", km)
        rna_keymap_ui.draw_kmi([], kc, km, kmi, col, 0)

    class RGAI_PT_bridge(bpy.types.Panel):
        bl_label = "Settings"
        bl_idname = "RGAI_PT_bridge"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "pip 1.0"
        bl_parent_id = "RGAI_PT_main"
        bl_options = {'DEFAULT_CLOSED'}

        def draw(self, context):
            layout = self.layout
            layout.label(text="Shortcut for the pop-up:")
            _draw_shortcut(layout, context)
            layout.separator()
            layout.label(text="Bridge for blender_controller.py:")
            bridge = running()
            if bridge:
                jobs = bridge.runner.jobs_run
                layout.label(text="Listening on %s:%d" % (bridge.host, bridge.port), icon='LINKED')
                layout.label(text="%d job%s run" % (jobs, "" if jobs == 1 else "s"))
                layout.operator("randomgenerals.bridge_toggle", text="Stop", icon='PAUSE')
            else:
                layout.label(text="Stopped", icon='UNLINKED')
                layout.operator("randomgenerals.bridge_toggle", text="Start", icon='PLAY')

    def _menu_add(self, context):
        self.layout.operator("randomgenerals.ai_popup", text="AI Model...", icon='OUTLINER_OB_LIGHT')
        self.layout.separator()

    def _header_button(self, context):
        """One click, for anyone who would rather not learn a shortcut."""
        if not _prefs(context).header_button:
            return
        self.layout.operator("randomgenerals.ai_popup", text="AI", icon='OUTLINER_OB_LIGHT')

    _CLASSES = (RGAI_Preferences, RGAI_Props, RGAI_OT_popup, RGAI_OT_run, RGAI_OT_cancel,
                RGAI_OT_refresh, RGAI_OT_open_text, RGAI_OT_keep, RGAI_OT_bridge_toggle,
                RGAI_PT_main, RGAI_PT_bridge)

    def _teardown():
        """Undo a register() - this module's, or an earlier run's from the
        Text Editor, whose functions are only reachable through the
        record it left in _SESSION."""
        record = _SESSION.pop(_UI_KEY, None)
        stop()
        _stop_runner()
        if record is None:
            return
        try:
            record["status"](None)
        except Exception:                                     # noqa: BLE001
            pass
        for menu, key in ((bpy.types.VIEW3D_MT_add, "menu"),
                          (bpy.types.VIEW3D_MT_editor_menus, "header")):
            try:
                menu.remove(record[key])
            except Exception:                                 # noqa: BLE001
                pass
        for km, kmi in record["keymaps"]:
            try:
                km.keymap_items.remove(kmi)
            except Exception:                                 # noqa: BLE001
                pass
        if hasattr(bpy.types.WindowManager, "randomgenerals_ai"):
            del bpy.types.WindowManager.randomgenerals_ai
        for cls in reversed(record["classes"]):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass

    def register():
        _teardown()
        # Run from the Text Editor there is no add-on entry for the
        # preferences to live in; _prefs() falls back to the defaults.
        classes = [c for c in _CLASSES if __name__ != "__main__" or c is not RGAI_Preferences]
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.WindowManager.randomgenerals_ai = PointerProperty(type=RGAI_Props)
        bpy.types.VIEW3D_MT_add.prepend(_menu_add)
        bpy.types.VIEW3D_MT_editor_menus.append(_header_button)
        keymaps = []
        kc = bpy.context.window_manager.keyconfigs.addon
        if kc is not None:
            km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
            keymaps.append((km, km.keymap_items.new("randomgenerals.ai_popup", "A", "PRESS",
                                                    ctrl=True, shift=True)))
        jobs = runner()
        jobs.hooks.append(_ui_tick)
        _SESSION[_UI_KEY] = {"classes": classes, "menu": _menu_add, "header": _header_button,
                                             "keymaps": keymaps, "status": _set_status}
        p = _prefs()
        _refresh_ollama(p.ollama_url)
        if bpy.app.background:
            # A background Blender with this add-on enabled - a render
            # farm job, or the controller's own headless runs - would
            # otherwise take the port from the window the user works in.
            return
        if p.start_bridge:
            try:
                start(p.port)
            except RuntimeError as e:
                # Enabling must not fail on a busy port: the Bridge
                # panel's Start button retries once it is free.
                print("[pip 1.0] %s" % e)

    def unregister():
        _teardown()

else:
    def register():
        pass

    def unregister():
        pass


if __name__ == "__main__" and bpy is not None:
    _argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if "--run" in _argv:
        _headless(_argv)
    elif bpy.app.background:
        print("[pip 1.0] Headless Blender exits when this script ends, "
              "so there is nothing to keep a server alive. Pass --run.")
    else:
        register()
