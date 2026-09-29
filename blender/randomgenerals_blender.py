# -*- coding: utf-8 -*-
"""RandomGenerals for Blender - a tutor, a builder and a lag doctor.

Install: Edit > Preferences > Add-ons > Install from Disk, pick this file,
tick it, then paste the API key from the site (Settings > Blender) into
the add-on's preferences. The panel is in the 3D Viewport sidebar (N),
under the "RandomGenerals" tab.

What it does:

  Guide me       - coaches you through making it yourself, one step at a
                   time. "Show me where" moves your mouse pointer to the
                   editor the step happens in (and opens the right
                   Properties tab); it watches the scene and ticks each
                   step off as soon as you have done it; "Do it for me"
                   does a step you are stuck on, undoable with Ctrl+Z.
  Make it for me - builds the model itself: writes a script, runs it,
                   fixes its own errors from the traceback, then looks at
                   the resulting scene and corrects it - up to four model
                   calls, each one undo step. A script that reaches
                   outside Blender is never run without you.
  Explain        - the same task as written numbered steps, with real
                   menu paths and hotkeys for the Blender you are running.
  Write script   - a bpy script in the Text Editor as "RG Script", run
                   only when you press Run. "Fix it" sends an error back.
  Lag     - measures the scene locally (no model, no network): triangle
            counts before and after modifiers, subdivision levels, big
            textures, particle counts, sample settings. Each finding that
            has a safe automatic fix gets a button; every fix is undoable.
            "Explain" sends those numbers to the model for advice.

It talks to the site's OpenAI-compatible endpoint (/v1/chat/completions)
with your key, from a background thread, so Blender's UI never freezes
while the model is thinking.

WHAT THIS IS NOT: a sandbox. A generated script runs with the same power
as any script you paste into Blender. The add-on flags lines that touch
files, processes or the network and asks before running them, and the
model is told not to write them, but read what you run.

The pure-Python half (prompt building, script extraction, the lag rules)
does not import bpy, so it is tested outside Blender by check_blender.py
at the repository root.
"""

bl_info = {
    "name": "RandomGenerals Assistant",
    "author": "RandomGenerals",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > RandomGenerals",
    "description": "AI tutor and model builder, plus a scene lag doctor",
    "category": "3D View",
    "doc_url": "https://randomgenerals.com",
}

import json
import queue
import re
import ssl
import threading
import traceback
import urllib.error
import urllib.request

try:
    import bpy
    from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                           StringProperty)
except ImportError:          # imported by the test outside Blender
    bpy = None

DEFAULT_SERVER = "https://randomgenerals.com"
USER_AGENT = "RandomGenerals-Blender/%d.%d.%d" % bl_info["version"]
REPLY_TEXT = "RG Reply"
SCRIPT_TEXT = "RG Script"
LAG_TEXT = "RG Performance"
# Enough for a full build script; small enough that prompt + reply stays
# inside the server's per-minute token window.
MAX_TOKENS = 3000
# Follow-ups ("make it taller") need the last exchange or two. More than
# that costs tokens on every request for context nobody is reading.
HISTORY_TURNS = 6


# ------------------------------------------------------------------ #
# Prompts
# ------------------------------------------------------------------ #

_COMMON = (
    "You are the RandomGenerals assistant running inside Blender {version}. "
    "Use menu names, hotkeys and Python API names that exist in that "
    "version; Blender renamed a lot between 2.7x, 2.8x, 3.x and 4.x, so "
    "if something moved, give the {version} location. If you are not sure "
    "an API or menu exists, say so rather than guessing. The user's scene "
    "is summarised below each request - use it (object names, what is "
    "selected, the render engine) instead of asking.\n\n"
)

TEACH_PROMPT = _COMMON + (
    "MODE: TEACH. You are a patient 3D modelling tutor. Teach the user to "
    "do it themselves:\n"
    "- Numbered steps. Each step says where to click (e.g. 'Add > Mesh > "
    "Cylinder', 'Modifier Properties > Add Modifier > Generate > Bevel') "
    "and the hotkey, in the mode it works in (Object / Edit).\n"
    "- After a step that is not obvious, one short line on WHY it is done "
    "that way (topology, clean edge flow, non-destructive modifiers).\n"
    "- Mention the common mistake at the step where people make it "
    "(unapplied scale before bevel, flipped normals, forgetting to "
    "leave Edit Mode).\n"
    "- Keep polycount sensible and say how, because a heavy mesh is the "
    "commonest cause of a slow viewport.\n"
    "- End with one small practice exercise that uses what was taught.\n"
    "Do not write a Python script unless the user asks for one."
)

BUILD_PROMPT = _COMMON + (
    "MODE: BUILD. Write ONE complete Python script for Blender {version} "
    "that creates what the user asked for in the current scene. Rules:\n"
    "- Exactly one ```python fenced block. It runs as-is in Blender's "
    "Text Editor with `import bpy` (and bmesh / mathutils if needed).\n"
    "- Prefer bpy.data, bmesh and mathutils over bpy.ops: operators "
    "depend on the active area and mode and fail when run from a script. "
    "If you must use bpy.ops, make sure Object Mode and the right active "
    "object first.\n"
    "- Do not delete or modify objects the user already has unless they "
    "asked. Put everything you create in a new collection named after "
    "the thing, and give objects clear names.\n"
    "- Keep it light: modest vertex counts, subdivision via a Subdivision "
    "Surface modifier with viewport levels at most 1 (render levels may be "
    "higher), and shade smooth instead of extra geometry where it looks "
    "the same. Say the approximate triangle count.\n"
    "- Make simple materials with Principled BSDF when colour matters.\n"
    "- Never touch files, the network, the operating system or other "
    "processes: no os, sys, subprocess, socket, urllib, shutil, open(), "
    "eval, exec or __import__. Do not save or quit Blender.\n"
    "- Put the main dimensions and counts as named variables at the top "
    "so the user can tweak them.\n"
    "After the code block, at most four short bullet lines: what it made, "
    "and which variable to change for the obvious tweaks."
)

LAG_PROMPT = _COMMON + (
    "MODE: PERFORMANCE. You are a Blender performance specialist. The "
    "measured scene statistics and the rule-based findings are below. "
    "Explain what is making this scene slow, in order of impact, citing "
    "the actual numbers and object names. For each cause give the exact "
    "fix with its menu path, and say whether it affects the viewport, the "
    "final render, or both, and what it costs in quality. Cover workflow "
    "habits too where relevant (Solid view while modelling, local view "
    "'/', hiding collections, Simplify, instancing with Alt+D instead of "
    "Shift+D, baking, proxies/low-poly stand-ins). Be concise."
)

FIX_REQUEST = (
    "That script failed when I ran it in Blender {version}. Here is the "
    "error:\n\n```\n{error}\n```\n\nFind the cause and send the whole "
    "corrected script, following the same rules."
)

GUIDE_PROMPT = _COMMON + (
    "MODE: GUIDE. You are coaching the user live, one step at a time, "
    "while they do the work in Blender. The add-on shows one step, can "
    "move their mouse pointer to the editor it happens in, can do the "
    "step for them if they ask, and watches the scene to tick the step "
    "off the moment they have done it. Reply with ONLY a JSON object, no "
    "prose and no code fence:\n"
    '{{"title": "short name of what they will make",\n'
    ' "steps": [\n'
    '  {{"say": "what to do and why, one or two sentences",\n'
    '   "keys": "the hotkey or menu path, e.g. Shift+A > Mesh > Cube",\n'
    '   "area": "VIEW_3D | PROPERTIES | OUTLINER | NODE_EDITOR | '
    'TEXT_EDITOR | TIMELINE",\n'
    '   "tab": "for PROPERTIES only: OBJECT | MODIFIER | MATERIAL | DATA '
    '| RENDER | WORLD | PHYSICS | PARTICLES | CONSTRAINT | OUTPUT",\n'
    '   "mode": "OBJECT or EDIT - the mode the step happens in",\n'
    '   "check": "ONE Python expression, True once the step is done",\n'
    '   "do": "Python that does this step for them"}}\n'
    " ]}}\n"
    "Rules: 4 to 12 small steps, each one action. `check` is evaluated "
    "every second with only `bpy`, `C` (bpy.context) and `D` (bpy.data) "
    "available and must only READ state - no bpy.ops, no assignments, no "
    "imports - e.g. \"'Mug' in D.objects\" or \"C.object and "
    "any(m.type == 'BEVEL' for m in C.object.modifiers)\". Make checks "
    "forgiving (the user may name things differently: prefer checking "
    "the active object's type or modifiers over an exact name). Leave "
    "`check` empty for a step that cannot be detected, like orbiting "
    "the view. `do` uses bpy.data / bmesh where it can, never touches "
    "files or the network, and must work on the scene as the earlier "
    "steps left it."
)

REVIEW_REQUEST = (
    "Your script ran without errors. This is the scene now:\n\n{scene}\n\n"
    "Compare it with what I asked for: {request}\n"
    "If it is right, reply with the single word DONE on the first line "
    "and one short sentence on what was made. If something is missing "
    "or wrong (wrong proportions, parts not joined, nothing visible, "
    "objects at the origin on top of each other), send ONE corrected "
    "full script that fixes the scene as it is now - it runs on top of "
    "it, so remove or edit what you already made instead of making "
    "duplicates."
)

MODE_PROMPTS = {"TEACH": TEACH_PROMPT, "BUILD": BUILD_PROMPT,
                "LAG": LAG_PROMPT, "GUIDE": GUIDE_PROMPT,
                "AGENT": BUILD_PROMPT}

# Make it for me: at most this many model calls per request - the first
# script, fixes for scripts that error, and one look at the result. Each
# call costs credits and a slice of the per-minute budget, and a model
# that has not got it right in four goes is not going to on the fifth.
MAX_AGENT_ROUNDS = 4
GUIDE_AREAS = {"VIEW_3D", "PROPERTIES", "OUTLINER", "NODE_EDITOR",
               "TEXT_EDITOR", "TIMELINE", "DOPESHEET_EDITOR",
               "IMAGE_EDITOR"}
MAX_GUIDE_STEPS = 20


def parse_guide(reply):
    """The step list from a GUIDE reply, or None.

    Models asked for bare JSON still wrap it in a fence or a sentence
    often enough that parsing only a perfect reply would make the mode
    fail half the time, so this takes the outermost {...} in the text.
    """
    text = reply or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    steps = []
    for raw in (data.get("steps") or [])[:MAX_GUIDE_STEPS]:
        if not isinstance(raw, dict) or not str(raw.get("say", "")).strip():
            continue
        area = str(raw.get("area") or "VIEW_3D").upper()
        check = str(raw.get("check") or "").strip()
        steps.append({
            "say": str(raw["say"]).strip(),
            "keys": str(raw.get("keys") or "").strip(),
            "area": area if area in GUIDE_AREAS else "VIEW_3D",
            "tab": str(raw.get("tab") or "").strip().upper(),
            "mode": str(raw.get("mode") or "").strip().upper(),
            # A check that could change the scene is dropped here, once,
            # rather than refused every second by the watcher.
            "check": check if check_is_safe(check) else "",
            "do": str(raw.get("do") or "").strip(),
        })
    if not steps:
        return None
    return {"title": str(data.get("title") or "Guide").strip()[:60],
            "steps": steps}


_CHECK_FORBIDDEN = re.compile(
    r"__|\bimport\b|\bops\b|\bexec\b|\beval\b|\bopen\b|\bcompile\b|"
    r"\bglobals\b|\blocals\b|\bgetattr\b|\bsetattr\b|\bdelattr\b|"
    r"\blambda\b|:=|\.remove\b|\.new\b|\.clear\b|\.link\b|\.unlink\b|"
    r"\.write\b|\.update\b|\.select_set\b|\.hide_set\b|\bdel\b")


def check_is_safe(expr):
    """Whether a step's done-check only reads the scene.

    It runs every second without asking, so it is held to a much
    stricter rule than a script the user chooses to run: one expression,
    no operators, no calls that create, delete or change anything.
    """
    if not expr or len(expr) > 400 or "\n" in expr:
        return False
    if _CHECK_FORBIDDEN.search(expr):
        return False
    try:
        compile(expr, "<check>", "eval")
    except SyntaxError:
        return False
    return True


_CHECK_BUILTINS = {"len": len, "any": any, "all": all, "abs": abs,
                   "min": min, "max": max, "round": round, "sum": sum,
                   "bool": bool, "str": str, "int": int, "float": float,
                   "True": True, "False": False, "None": None}


def evaluate_check(expr, namespace):
    """True/False for a done-check. Anything that raises is not done."""
    if not check_is_safe(expr):
        return False
    try:
        env = dict(namespace)
        env["__builtins__"] = _CHECK_BUILTINS
        return bool(eval(compile(expr, "<check>", "eval"), env))
    except Exception:                                   # noqa: BLE001
        return False


def build_messages(mode, request_text, scene_summary, history, version):
    """The request as OpenAI-style messages.

    The scene summary rides on the newest user turn only. Earlier turns
    carried their own snapshot when they were sent, and repeating it on
    every turn would spend the token window on stale copies of the scene.
    """
    system = MODE_PROMPTS[mode].format(version=version)
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history[-HISTORY_TURNS:])
    content = request_text.strip()
    if scene_summary:
        content += "\n\n[Current scene]\n" + scene_summary
    msgs.append({"role": "user", "content": content})
    return msgs


# ------------------------------------------------------------------ #
# Reading the reply
# ------------------------------------------------------------------ #

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.S)


def extract_script(reply):
    """The Python script in a reply, or None.

    Prefers a block labelled python/py; falls back to the longest
    unlabelled block that mentions bpy. The longest because a model that
    ignores "exactly one block" usually adds a one-line example after
    the real script, not before.
    """
    blocks = _FENCE.findall(reply or "")
    labelled = [b for lang, b in blocks if lang.lower() in ("python", "py")]
    if labelled:
        return max(labelled, key=len).strip() + "\n"
    plain = [b for lang, b in blocks if not lang and "bpy" in b]
    if plain:
        return max(plain, key=len).strip() + "\n"
    return None


# Not a sandbox - a speed bump, so that a line which could reach outside
# Blender is something the user sees and agrees to rather than something
# that just happens.
_RISKY = [
    (re.compile(r"^\s*(import|from)\s+(os|sys|subprocess|socket|shutil|"
                r"urllib|requests|http|ctypes|pathlib|pickle|glob|"
                r"multiprocessing|asyncio|ftplib|smtplib|webbrowser)\b"),
     "imports a module that reaches outside Blender"),
    (re.compile(r"\b__import__\s*\("), "dynamic import"),
    (re.compile(r"(?<![.\w])(eval|exec|compile)\s*\("), "runs generated code"),
    (re.compile(r"(?<![.\w])open\s*\("), "opens a file"),
    (re.compile(r"\bbpy\.ops\.wm\.(quit_blender|save_|open_mainfile|"
                r"read_factory|read_homefile)"), "saves, opens or quits"),
    (re.compile(r"\bbpy\.ops\.(script|preferences)\."),
     "changes scripts or preferences"),
    (re.compile(r"\bbpy\.(utils\.register|app\.handlers)"),
     "installs code that keeps running"),
]


def risky_lines(code):
    """[(line_number, reason, line)] for lines worth a second look."""
    found = []
    for n, line in enumerate((code or "").splitlines(), 1):
        stripped = line.split("#", 1)[0]
        for pattern, reason in _RISKY:
            if pattern.search(stripped):
                found.append((n, reason, line.strip()))
                break
    return found


# ------------------------------------------------------------------ #
# Talking to the site
# ------------------------------------------------------------------ #

class ApiError(Exception):
    pass


def _ssl_context():
    # Blender's bundled Python on macOS ships without the system CA list,
    # which fails every HTTPS request with CERTIFICATE_VERIFY_FAILED.
    # certifi is bundled with some Blender builds; use it when present.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                   # noqa: BLE001
        return ssl.create_default_context()


def call_api(server, api_key, messages, timeout=180):
    """POST to /v1/chat/completions and return the reply text.

    Raises ApiError with a sentence a person can act on. Runs on a
    worker thread, so it must not touch bpy.
    """
    if not api_key:
        raise ApiError("No API key. Create one on the site under "
                       "Settings > Blender and paste it in the add-on's "
                       "preferences.")
    url = (server or DEFAULT_SERVER).rstrip("/") + "/v1/chat/completions"
    body = json.dumps({
        "model": "randomgenerals",
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key.strip(),
        # Some fronting proxies refuse the default "Python-urllib" agent.
        "User-Agent": USER_AGENT,
    })
    context = _ssl_context() if url.startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=context) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            message = json.loads(raw)["error"]["message"]
        except Exception:                               # noqa: BLE001
            message = _http_failure(e.code, raw, url)
        raise ApiError(message) from None
    except urllib.error.URLError as e:
        raise ApiError("Could not reach %s (%s)." % (url, e.reason)) from None
    except TimeoutError:
        raise ApiError("The server took too long to answer.") from None
    except ValueError:
        raise ApiError("The server sent something that is not JSON - "
                       "check the server address.") from None
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ApiError("The reply was not in the expected shape.") from None
    if not text.strip():
        raise ApiError("The model sent an empty reply. Try again.")
    return text


def _http_failure(code, body, url):
    """A sentence for an HTTP error that did not come from the app.

    The app always answers /v1 errors as JSON, so a non-JSON error came
    from something in front of it - and those are the ones a bare "the
    server answered 403" leaves nobody able to fix.
    """
    if code == 403 and "1010" in body:
        # Measured against the live site: Cloudflare answers the default
        # Python-urllib agent with 403 "error code: 1010". This add-on
        # sends its own agent, so reaching here means something between
        # Blender and the site replaced it.
        return ("Cloudflare blocked the request as an automated browser "
                "(error 1010). A proxy or VPN on this computer may be "
                "changing requests - try without it.")
    if code == 404:
        return ("%s does not exist on that server. Check the server "
                "address in the add-on's preferences." % url)
    if code in (502, 503, 504, 520, 521, 522, 523, 524):
        return ("The site is not answering right now (%d). Try again in "
                "a minute." % code)
    return "The server answered %d." % code


# ------------------------------------------------------------------ #
# Lag rules. Plain data in, findings out, so they are testable without
# Blender and the numbers that trigger them are all in one place.
# ------------------------------------------------------------------ #

HEAVY_SCENE_TRIS = 2_000_000
HEAVY_OBJECT_TRIS = 500_000
MODIFIER_BLOWUP = 4          # evaluated / base triangle ratio
MAX_VIEWPORT_SUBDIV = 1
BIG_TEXTURE = 4096
MANY_PARTICLES = 50_000
MAX_PREVIEW_SAMPLES = 64
MANY_OBJECTS = 1500


def lag_findings(stats):
    """[(severity, message, fix_id or None)] from collect_stats() output.

    severity is "high", "medium" or "low". fix_id names a RG_OT_lag_fix
    action that repairs it; None means it needs a human decision.
    """
    out = []
    objs = stats.get("objects", [])
    total = stats.get("total_tris", 0)
    if total > HEAVY_SCENE_TRIS:
        out.append(("high", "Scene is %s triangles after modifiers. Above "
                    "~2M most viewports start to stutter." % _fmt(total),
                    None))

    heavy = sorted((o for o in objs if o.get("tris_eval", 0)
                    > HEAVY_OBJECT_TRIS),
                   key=lambda o: -o["tris_eval"])
    for o in heavy[:5]:
        out.append(("high", "%s: %s triangles." % (o["name"],
                                                    _fmt(o["tris_eval"])),
                    "DECIMATE"))

    subdiv = [(o, m) for o in objs for m in o.get("modifiers", [])
              if m.get("type") == "SUBSURF"
              and m.get("levels", 0) > MAX_VIEWPORT_SUBDIV]
    if subdiv:
        names = ", ".join("%s (%d)" % (o["name"], m["levels"])
                          for o, m in subdiv[:6])
        out.append(("high" if any(m["levels"] >= 3 for _, m in subdiv)
                    else "medium",
                    "Subdivision viewport levels above %d on: %s. Each "
                    "level multiplies faces by 4; render levels are kept."
                    % (MAX_VIEWPORT_SUBDIV, names), "SUBDIV"))

    blown = [o for o in objs if o.get("tris_base", 0) > 1000
             and o.get("tris_eval", 0) > MODIFIER_BLOWUP * o["tris_base"]
             and o not in heavy]
    for o in blown[:3]:
        out.append(("medium", "%s: modifiers turn %s triangles into %s."
                    % (o["name"], _fmt(o["tris_base"]),
                       _fmt(o["tris_eval"])), "SIMPLIFY"))

    parts = [o for o in objs if o.get("particles", 0) > MANY_PARTICLES]
    for o in parts[:3]:
        out.append(("medium", "%s: %s particles/hairs displayed." %
                    (o["name"], _fmt(o["particles"])), "SIMPLIFY"))

    big = [i for i in stats.get("images", [])
           if max(i.get("size", (0, 0))) >= BIG_TEXTURE]
    if big:
        out.append(("medium", "%d texture(s) at 4K or larger (%s). Each 4K "
                    "RGBA image is ~64 MB of GPU memory." %
                    (len(big), ", ".join(i["name"] for i in big[:4])),
                    None))

    if (stats.get("engine") == "CYCLES"
            and stats.get("preview_samples", 0) > MAX_PREVIEW_SAMPLES):
        out.append(("medium", "Cycles viewport samples are %d. %d with "
                    "denoising looks nearly the same while you work."
                    % (stats["preview_samples"], MAX_PREVIEW_SAMPLES),
                    "SAMPLES"))
    if (stats.get("engine", "").startswith("BLENDER_EEVEE")
            and stats.get("eevee_samples", 0) > 16):
        out.append(("low", "EEVEE viewport samples are %d; 8-16 is plenty "
                    "while modelling." % stats["eevee_samples"], "SAMPLES"))

    if stats.get("object_count", len(objs)) > MANY_OBJECTS:
        out.append(("medium", "%d objects. Many copies of one thing are far "
                    "cheaper as linked duplicates (Alt+D) or instances."
                    % stats.get("object_count", len(objs)), None))

    if stats.get("shading") == "RENDERED":
        out.append(("low", "The viewport is in Rendered shading. Solid "
                    "(Z > Solid) is much faster while modelling.", None))

    if not out:
        out.append(("low", "Nothing obviously heavy. If it still lags, "
                    "check GPU drivers and Preferences > System (Cycles "
                    "device, Viewport texture limit).", None))
    return out


def _fmt(n):
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 10_000:
        return "%dk" % (n // 1000)
    return str(n)


def stats_summary(stats, findings=None):
    """The stats as a few compact lines for the model and the report."""
    lines = ["Blender %s, engine %s, %d objects, %s triangles total." % (
        stats.get("version", "?"), stats.get("engine", "?"),
        stats.get("object_count", 0), _fmt(stats.get("total_tris", 0)))]
    if stats.get("selected"):
        lines.append("Selected: " + ", ".join(stats["selected"][:10]))
    top = sorted(stats.get("objects", []),
                 key=lambda o: -o.get("tris_eval", 0))[:8]
    for o in top:
        mods = ", ".join(
            m["type"].lower() + ("(%d/%d)" % (m["levels"], m["render_levels"])
                                 if "levels" in m else "")
            for m in o.get("modifiers", []))
        lines.append("- %s [%s] %s tris%s%s" % (
            o["name"], o.get("type", "?"), _fmt(o.get("tris_eval", 0)),
            (" base " + _fmt(o["tris_base"]))
            if o.get("tris_base") != o.get("tris_eval") else "",
            (", modifiers: " + mods) if mods else ""))
    if findings:
        lines.append("Findings:")
        lines.extend("- [%s] %s" % (s, m) for s, m, _ in findings)
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Everything below needs Blender.
# ------------------------------------------------------------------ #

def _version():
    return bpy.app.version_string if bpy else "4.x"


def _tris(mesh):
    """Triangle count of a mesh without a Python loop over faces."""
    n = len(mesh.polygons)
    if not n:
        return 0
    try:
        import numpy as np
        totals = np.empty(n, dtype=np.int32)
        mesh.polygons.foreach_get("loop_total", totals)
        return int(totals.sum()) - 2 * n
    except Exception:                                   # noqa: BLE001
        return sum(p.loop_total - 2 for p in mesh.polygons)


def collect_stats(context):
    """Measure the scene. Pure reads - it changes nothing."""
    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    objects = []
    total = 0
    for obj in scene.objects:
        if obj.hide_get() or not obj.visible_get():
            continue
        entry = {"name": obj.name, "type": obj.type, "modifiers": []}
        for m in obj.modifiers:
            mod = {"type": m.type}
            if m.type == "SUBSURF":
                mod["levels"] = m.levels
                mod["render_levels"] = m.render_levels
            entry["modifiers"].append(mod)
        if obj.type == "MESH":
            base = _tris(obj.data)
            try:
                evaluated = _tris(obj.evaluated_get(depsgraph).data)
            except Exception:                           # noqa: BLE001
                evaluated = base
            entry["tris_base"] = base
            entry["tris_eval"] = evaluated
            total += evaluated
        count = 0
        for ps in getattr(obj, "particle_systems", []):
            st = ps.settings
            children = st.child_nbr if st.child_type != "NONE" else 0
            count += st.count * max(1, children)
        entry["particles"] = count
        objects.append(entry)

    images = [{"name": i.name, "size": tuple(i.size)}
              for i in bpy.data.images if i.size[0] and i.users]
    stats = {
        "version": _version(),
        "engine": scene.render.engine,
        "object_count": len(objects),
        "total_tris": total,
        "objects": objects,
        "images": images,
        "selected": [o.name for o in context.selected_objects],
        "simplify": scene.render.use_simplify,
    }
    if hasattr(scene, "cycles"):
        stats["preview_samples"] = scene.cycles.preview_samples
    if hasattr(scene, "eevee"):
        stats["eevee_samples"] = scene.eevee.taa_samples
    space = getattr(context, "space_data", None)
    if space is not None and getattr(space, "type", "") == "VIEW_3D":
        stats["shading"] = space.shading.type
    return stats


def _text(name, body):
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(body)
    return text


def _show_text(context, text):
    """Show a text in an open Text Editor, if there is one. Never opens
    a new window: stealing screen space is worse than a status line
    saying where the text went."""
    for area in context.screen.areas:
        if area.type == "TEXT_EDITOR":
            area.spaces.active.text = text
            return True
    return False


def _prefs(context):
    return context.preferences.addons[__name__].preferences


# One request at a time. The worker puts its result here; a timer on the
# main thread picks it up, because bpy may only be touched from there.
_results = queue.Queue()
_state = {"busy": False, "status": "", "history": [], "last_mode": None,
          "last_error": None, "findings": [], "guide": None, "agent": None}


def _tag_redraw():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in ("VIEW_3D", "TEXT_EDITOR"):
                area.tag_redraw()


def _start_request(context, mode, request_text, summary, remember):
    prefs = _prefs(context)
    messages = build_messages(mode, request_text, summary,
                              _state["history"], _version())
    server, key = prefs.server_url, prefs.api_key
    _state["busy"] = True
    _state["status"] = "Thinking…"
    _state["last_mode"] = mode

    def work():
        try:
            reply = call_api(server, key, messages)
            _results.put(("ok", mode, remember, request_text, reply))
        except ApiError as e:
            _results.put(("error", mode, remember, request_text, str(e)))
        except Exception as e:                          # noqa: BLE001
            _results.put(("error", mode, remember, request_text,
                          "Unexpected error: %s" % e))

    threading.Thread(target=work, daemon=True).start()
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.3)


def _poll():
    try:
        kind, mode, remember, request_text, payload = _results.get_nowait()
    except queue.Empty:
        return 0.3
    _state["busy"] = False
    if kind == "error":
        _state["status"] = payload
        _state["agent"] = None
        _tag_redraw()
        return None
    if remember:
        _state["history"].extend([
            {"role": "user", "content": request_text},
            {"role": "assistant", "content": payload},
        ])
        del _state["history"][:-HISTORY_TURNS]
    reply = _text(REPLY_TEXT, payload)
    handler = {"GUIDE": _on_guide, "AGENT": _on_agent}.get(mode)
    if handler:
        handler(payload)
        _tag_redraw()
        # Make it for me sends its next request from inside the handler,
        # while this timer is still registered - so _start_request did
        # not register another, and returning None here would strand
        # that reply in the queue forever.
        return 0.3 if _state["busy"] else None
    script = extract_script(payload) if mode == "BUILD" else None
    if script:
        _text(SCRIPT_TEXT, script)
        _state["status"] = ("Script ready in the Text Editor as '%s'. "
                            "Read it, then press Run." % SCRIPT_TEXT)
    elif mode == "BUILD":
        _state["status"] = ("No script in the reply - see '%s'."
                            % REPLY_TEXT)
    else:
        _state["status"] = "Answer is in the Text Editor as '%s'." % REPLY_TEXT
    try:
        _show_text(bpy.context, bpy.data.texts[SCRIPT_TEXT] if script
                   else reply)
    except Exception:                                   # noqa: BLE001
        pass
    _tag_redraw()
    return None


# ------------------------------------------------------------------ #
# Running generated code
# ------------------------------------------------------------------ #

def _view3d_override():
    """A window/area/region for the first 3D Viewport, or {}.

    Generated scripts are told to prefer bpy.data, but bpy.ops is what
    most tutorials use, and an operator run from a timer or the sidebar
    has no 3D Viewport in its context and fails with "context is
    incorrect". Running inside this override gives it one.
    """
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"),
                          None)
            if region:
                return {"window": window, "screen": window.screen,
                        "area": area, "region": region}
    return {}


def run_code(code, label, push_undo=True):
    """Run generated code on the main thread. -> None, or the traceback.

    One undo step whichever way it was started, so Ctrl+Z always takes
    back exactly what the assistant did. An operator with the UNDO flag
    makes its own step, so it passes push_undo=False.
    """
    obj = bpy.context.view_layer.objects.active
    if obj is not None and obj.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    override = _view3d_override()
    try:
        compiled = compile(code, label, "exec")
        namespace = {"__name__": "__main__", "bpy": bpy}
        if override and hasattr(bpy.context, "temp_override"):
            with bpy.context.temp_override(**override):
                exec(compiled, namespace)
        else:
            exec(compiled, namespace)
    except Exception:                                   # noqa: BLE001
        return traceback.format_exc(limit=4)
    finally:
        if push_undo:
            try:
                bpy.ops.ed.undo_push(message="RandomGenerals: " + label)
            except RuntimeError:
                pass    # no undo stack in background mode
    return None


# ------------------------------------------------------------------ #
# Make it for me: write, run, fix, look, without a click per step
# ------------------------------------------------------------------ #

def _scene_summary():
    try:
        return stats_summary(collect_stats(bpy.context))
    except Exception:                                   # noqa: BLE001
        return ""


def _agent_send(request_text, summary=""):
    agent = _state["agent"]
    agent["rounds"] += 1
    _state["status"] = "%s (step %d of up to %d)" % (
        agent["phase"], agent["rounds"], MAX_AGENT_ROUNDS)
    _start_request(bpy.context, "AGENT", request_text, summary, True)


def _on_agent(reply):
    agent = _state["agent"]
    if agent is None:
        return
    if agent["reviewing"] and reply.strip().upper().startswith("DONE"):
        done = reply.strip()[4:].strip(" :-\n") or "Finished."
        if done[-1] not in ".!":
            done += "."
        _state["agent"] = None
        _state["status"] = "Done: %s Ctrl+Z undoes each step." % done
        return
    script = extract_script(reply)
    if not script:
        _state["agent"] = None
        _state["status"] = ("The assistant answered without a script - see "
                            "'%s'." % REPLY_TEXT)
        return
    _text(SCRIPT_TEXT, script)
    risky = risky_lines(script)
    if risky:
        # Never run unattended. It is still one click away, through the
        # same confirmation Run script always shows.
        _state["agent"] = None
        _state["status"] = ("Stopped: line %d %s. Read '%s', then press Run "
                            "if you trust it." % (risky[0][0], risky[0][1],
                                                  SCRIPT_TEXT))
        return
    error = run_code(script, "Make it (step %d)" % agent["rounds"])
    if error:
        _state["last_error"] = error
        if agent["rounds"] >= MAX_AGENT_ROUNDS:
            _state["agent"] = None
            _state["status"] = ("Still failing after %d tries. Press Fix it, "
                                "or ask differently." % agent["rounds"])
            return
        agent["phase"] = "Fixing an error"
        _agent_send(FIX_REQUEST.format(version=_version(),
                                       error=error[-2000:]))
        return
    _state["last_error"] = None
    if agent["reviewing"] or agent["rounds"] >= MAX_AGENT_ROUNDS:
        _state["agent"] = None
        _state["status"] = "Done. Ctrl+Z undoes each step."
        return
    agent["reviewing"] = True
    agent["phase"] = "Checking the result"
    _agent_send(REVIEW_REQUEST.format(scene=_scene_summary(),
                                      request=agent["request"]))


def start_agent(request_text):
    _state["agent"] = {"request": request_text, "rounds": 0,
                       "reviewing": False, "phase": "Writing a script"}
    _agent_send(request_text, _scene_summary())


# ------------------------------------------------------------------ #
# Guide me: one step at a time, watched, pointed at, or done for you
# ------------------------------------------------------------------ #

def _on_guide(reply):
    guide = parse_guide(reply)
    if not guide:
        _state["status"] = ("Could not read the steps from the answer - see "
                            "'%s', or try again." % REPLY_TEXT)
        return
    guide["index"] = 0
    guide["done"] = [False] * len(guide["steps"])
    _state["guide"] = guide
    _state["status"] = "%s: %d steps. Do each one - it ticks itself off." % (
        guide["title"], len(guide["steps"]))
    if not bpy.app.timers.is_registered(_guide_tick):
        bpy.app.timers.register(_guide_tick, first_interval=1.0)


def guide_step():
    guide = _state.get("guide")
    if not guide:
        return None
    return guide["steps"][guide["index"]]


def guide_move(delta):
    guide = _state.get("guide")
    if not guide:
        return
    guide["index"] = max(0, min(len(guide["steps"]) - 1,
                                guide["index"] + delta))


def _guide_tick():
    """Watch the scene and tick the current step off once it is done.

    Runs every second while a guide is open. The check is a read-only
    expression (check_is_safe), so this never changes anything itself.
    """
    guide = _state.get("guide")
    if not guide:
        return None
    i = guide["index"]
    step = guide["steps"][i]
    if not guide["done"][i] and step["check"]:
        if evaluate_check(step["check"], {"bpy": bpy, "C": bpy.context,
                                          "D": bpy.data}):
            guide["done"][i] = True
            if i + 1 < len(guide["steps"]):
                guide["index"] = i + 1
                _state["status"] = "Step %d done - on to step %d." % (
                    i + 1, i + 2)
            else:
                _state["status"] = "All done - that is %s." % guide["title"]
            _tag_redraw()
    return 1.0


def point_at(step):
    """Move the mouse pointer to where the step happens. -> sentence.

    Blender will say where an editor is, not where a button inside it
    is, so this points at the editor (and opens the right Properties
    tab), and the step's text says which button.
    """
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != step["area"]:
                continue
            if step["area"] == "PROPERTIES" and step["tab"]:
                try:
                    area.spaces.active.context = step["tab"]
                except (TypeError, AttributeError):
                    pass
            window.cursor_warp(area.x + area.width // 2,
                               area.y + area.height // 2)
            area.tag_redraw()
            return "Pointer moved to the %s." % _AREA_NAMES.get(
                step["area"], step["area"])
    return ("There is no %s open. Change an editor's type in its top-left "
            "menu." % _AREA_NAMES.get(step["area"], step["area"]))


_AREA_NAMES = {"VIEW_3D": "3D Viewport", "PROPERTIES": "Properties editor",
               "OUTLINER": "Outliner", "NODE_EDITOR": "Shader Editor",
               "TEXT_EDITOR": "Text Editor", "TIMELINE": "Timeline",
               "DOPESHEET_EDITOR": "Dope Sheet",
               "IMAGE_EDITOR": "Image Editor"}


if bpy is not None:

    class RG_Preferences(bpy.types.AddonPreferences):
        bl_idname = __name__

        server_url: StringProperty(
            name="Server", default=DEFAULT_SERVER,
            description="The RandomGenerals site this talks to")
        api_key: StringProperty(
            name="API key", subtype="PASSWORD", default="",
            description="From the site: Settings > Blender > Create key")

        def draw(self, context):
            col = self.layout.column()
            col.prop(self, "api_key")
            col.prop(self, "server_url")
            col.label(text="Get a key on the site under Settings > Blender.",
                      icon="INFO")

    class RG_OT_ask(bpy.types.Operator):
        """Send the request to the RandomGenerals assistant"""
        bl_idname = "randomgenerals.ask"
        bl_label = "Ask"

        mode: EnumProperty(items=[
            ("GUIDE", "Guide me", "Coach me through it one step at a "
             "time, and tick each step off as I do it"),
            ("TEACH", "Explain", "Written step-by-step instructions"),
            ("BUILD", "Write script", "Write a script I can read, then run"),
            ("AGENT", "Make it for me", "Write, run, fix and check a "
             "script until the model is built"),
        ])

        def execute(self, context):
            if _state["busy"]:
                self.report({"WARNING"}, "Still waiting on the last answer.")
                return {"CANCELLED"}
            request_text = context.window_manager.rg_prompt.strip()
            if not request_text:
                self.report({"WARNING"}, "Type what you want first.")
                return {"CANCELLED"}
            if self.mode == "AGENT":
                start_agent(request_text)
                return {"FINISHED"}
            if self.mode == "GUIDE":
                _state["guide"] = None
            _start_request(context, self.mode, request_text,
                           _scene_summary(), True)
            return {"FINISHED"}

    class RG_OT_run_script(bpy.types.Operator):
        """Run the generated script as one undo step"""
        bl_idname = "randomgenerals.run_script"
        bl_label = "Run script"
        bl_options = {"REGISTER", "UNDO"}

        def invoke(self, context, event):
            text = bpy.data.texts.get(SCRIPT_TEXT)
            if not text:
                self.report({"WARNING"}, "No script yet - use Build it.")
                return {"CANCELLED"}
            self._risky = risky_lines(text.as_string())
            if self._risky:
                return context.window_manager.invoke_props_dialog(
                    self, width=520)
            return self.execute(context)

        def draw(self, context):
            col = self.layout.column()
            col.label(text="These lines can reach outside Blender:",
                      icon="ERROR")
            for n, reason, line in self._risky[:8]:
                col.label(text="%d: %s - %s" % (n, reason, line[:60]))
            col.label(text="Run it anyway only if you have read it.")

        def execute(self, context):
            text = bpy.data.texts.get(SCRIPT_TEXT)
            if not text:
                return {"CANCELLED"}
            err = run_code(text.as_string(), SCRIPT_TEXT, push_undo=False)
            if err:
                _state["last_error"] = err
                _state["status"] = "The script failed. Press Fix it."
                self.report({"ERROR"}, err.strip().splitlines()[-1])
                return {"CANCELLED"}
            _state["last_error"] = None
            _state["status"] = "Done. Ctrl+Z undoes all of it."
            return {"FINISHED"}

    class RG_OT_fix(bpy.types.Operator):
        """Send the error back and ask for a corrected script"""
        bl_idname = "randomgenerals.fix"
        bl_label = "Fix it"

        @classmethod
        def poll(cls, context):
            return bool(_state["last_error"]) and not _state["busy"]

        def execute(self, context):
            text = bpy.data.texts.get(SCRIPT_TEXT)
            code = text.as_string() if text else ""
            # The failing script goes with the error, in case the user
            # edited it after it arrived.
            request_text = ("```python\n%s```\n\n" % code) + FIX_REQUEST.format(
                version=_version(), error=_state["last_error"][-2000:])
            _state["last_error"] = None
            _start_request(context, "BUILD", request_text, "", True)
            return {"FINISHED"}

    class RG_OT_guide(bpy.types.Operator):
        """Move through the guide, point at where a step happens, or do it"""
        bl_idname = "randomgenerals.guide"
        bl_label = "Guide"
        bl_options = {"REGISTER", "UNDO"}

        action: EnumProperty(items=[
            ("NEXT", "Next", "Go to the next step"),
            ("BACK", "Back", "Go to the previous step"),
            ("POINT", "Show me where", "Move the mouse pointer to the "
             "editor this step happens in"),
            ("DO", "Do it for me", "Let the assistant do this step - "
             "Ctrl+Z takes it back"),
            ("CLOSE", "Close", "Close the guide"),
        ])

        @classmethod
        def poll(cls, context):
            return bool(_state.get("guide"))

        def execute(self, context):
            guide = _state["guide"]
            if self.action in ("NEXT", "BACK"):
                guide_move(1 if self.action == "NEXT" else -1)
                return {"FINISHED"}
            if self.action == "CLOSE":
                _state["guide"] = None
                _state["status"] = ""
                return {"FINISHED"}
            step = guide_step()
            if self.action == "POINT":
                _state["status"] = point_at(step)
                return {"FINISHED"}
            if not step["do"]:
                self.report({"WARNING"}, "This step has to be done by hand.")
                return {"CANCELLED"}
            risky = risky_lines(step["do"])
            if risky:
                # Same rule as Make it for me: never unattended. It goes
                # where Run script can show it and ask.
                _text(SCRIPT_TEXT, step["do"] + "\n")
                _state["status"] = ("That step's code %s - it is in '%s' "
                                    "for you to read and Run." % (
                                        risky[0][1], SCRIPT_TEXT))
                return {"CANCELLED"}
            err = run_code(step["do"], "guide step %d" % (
                guide["index"] + 1), push_undo=False)
            if err:
                _state["last_error"] = err
                _text(SCRIPT_TEXT, step["do"] + "\n")
                _state["status"] = ("That did not work (%s). Try the step "
                                    "by hand, or press Fix it." %
                                    err.strip().splitlines()[-1][:80])
                return {"CANCELLED"}
            i = guide["index"]
            guide["done"][i] = True
            guide_move(1)
            _state["status"] = "Did step %d for you. Ctrl+Z undoes it." % (
                i + 1)
            return {"FINISHED"}

    class RG_OT_new_chat(bpy.types.Operator):
        """Forget the conversation so far"""
        bl_idname = "randomgenerals.new_chat"
        bl_label = "New conversation"

        def execute(self, context):
            _state["history"].clear()
            _state["guide"] = None
            _state["agent"] = None
            _state["status"] = "Started fresh."
            return {"FINISHED"}

    class RG_OT_analyze_lag(bpy.types.Operator):
        """Measure what is making the scene slow (local, no network)"""
        bl_idname = "randomgenerals.analyze_lag"
        bl_label = "Find lag"

        def execute(self, context):
            stats = collect_stats(context)
            findings = lag_findings(stats)
            _state["findings"] = findings
            _state["lag_summary"] = stats_summary(stats, findings)
            _text(LAG_TEXT, _state["lag_summary"])
            _state["status"] = "%d finding(s). Full report in '%s'." % (
                len(findings), LAG_TEXT)
            return {"FINISHED"}

    class RG_OT_explain_lag(bpy.types.Operator):
        """Ask the assistant to explain the findings and what to do"""
        bl_idname = "randomgenerals.explain_lag"
        bl_label = "Explain"

        @classmethod
        def poll(cls, context):
            return bool(_state["findings"]) and not _state["busy"]

        def execute(self, context):
            _start_request(context, "LAG",
                           "Why is this scene slow and how do I fix it?",
                           _state.get("lag_summary", ""), False)
            return {"FINISHED"}

    class RG_OT_lag_fix(bpy.types.Operator):
        """Apply a safe speed-up. Undo with Ctrl+Z"""
        bl_idname = "randomgenerals.lag_fix"
        bl_label = "Apply fix"
        bl_options = {"REGISTER", "UNDO"}

        action: EnumProperty(items=[
            ("SUBDIV", "Cap subdivision",
             "Viewport subdivision levels to 1; render levels untouched"),
            ("SIMPLIFY", "Turn on Simplify",
             "Scene-wide viewport limits on subdivision and particles"),
            ("SAMPLES", "Lower viewport samples",
             "Fewer viewport samples, with denoising in Cycles"),
            ("DECIMATE", "Decimate heavy meshes",
             "Add a Decimate modifier (viewport only) to the heaviest "
             "meshes"),
        ])
        ratio: FloatProperty(name="Keep", default=0.3, min=0.02, max=1.0,
                             subtype="FACTOR",
                             description="Fraction of faces to keep")

        def execute(self, context):
            scene = context.scene
            changed = 0
            if self.action == "SUBDIV":
                for obj in scene.objects:
                    for m in obj.modifiers:
                        if (m.type == "SUBSURF"
                                and m.levels > MAX_VIEWPORT_SUBDIV):
                            m.levels = MAX_VIEWPORT_SUBDIV
                            changed += 1
            elif self.action == "SIMPLIFY":
                r = scene.render
                r.use_simplify = True
                r.simplify_subdivision = min(r.simplify_subdivision,
                                             MAX_VIEWPORT_SUBDIV)
                r.simplify_child_particles = min(
                    r.simplify_child_particles, 0.2)
                changed = 1
            elif self.action == "SAMPLES":
                if hasattr(scene, "cycles"):
                    scene.cycles.preview_samples = min(
                        scene.cycles.preview_samples or 1024,
                        MAX_PREVIEW_SAMPLES)
                    if hasattr(scene.cycles, "use_preview_denoising"):
                        scene.cycles.use_preview_denoising = True
                    changed += 1
                if hasattr(scene, "eevee"):
                    scene.eevee.taa_samples = min(scene.eevee.taa_samples
                                                  or 16, 16)
                    changed += 1
            elif self.action == "DECIMATE":
                stats = collect_stats(context)
                for o in stats["objects"]:
                    if o.get("tris_eval", 0) <= HEAVY_OBJECT_TRIS:
                        continue
                    obj = scene.objects[o["name"]]
                    if any(m.type == "DECIMATE" and m.name == "RG Decimate"
                           for m in obj.modifiers):
                        continue
                    m = obj.modifiers.new("RG Decimate", "DECIMATE")
                    m.ratio = self.ratio
                    # Viewport only: the render keeps full detail, and
                    # nothing is applied, so deleting the modifier undoes
                    # it even after the undo history is gone.
                    m.show_render = False
                    changed += 1
            bpy.ops.randomgenerals.analyze_lag()
            self.report({"INFO"}, "Changed %d setting(s)." % changed)
            return {"FINISHED"}

    class RG_PT_panel(bpy.types.Panel):
        bl_label = "RandomGenerals"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "RandomGenerals"

        def draw(self, context):
            layout = self.layout
            prefs = _prefs(context)
            if not prefs.api_key:
                box = layout.box()
                box.label(text="Paste your API key first:", icon="KEYINGSET")
                box.prop(prefs, "api_key", text="")
                box.label(text="Site: Settings > Blender > Create key")

            box = layout.box()
            box.label(text="What do you want to make?", icon="QUESTION")
            box.prop(context.window_manager, "rg_prompt", text="")
            col = box.column(align=True)
            col.enabled = not _state["busy"]
            col.scale_y = 1.3
            col.operator("randomgenerals.ask", text="Guide me through it",
                         icon="HAND").mode = "GUIDE"
            col.operator("randomgenerals.ask", text="Make it for me",
                         icon="MESH_MONKEY").mode = "AGENT"
            row = box.row(align=True)
            row.enabled = not _state["busy"]
            row.operator("randomgenerals.ask", text="Explain",
                         icon="HELP").mode = "TEACH"
            row.operator("randomgenerals.ask", text="Write script",
                         icon="TEXT").mode = "BUILD"
            row = box.row(align=True)
            row.operator("randomgenerals.run_script", icon="PLAY")
            row.operator("randomgenerals.fix", icon="TOOL_SETTINGS")
            box.operator("randomgenerals.new_chat", icon="FILE_REFRESH")

            guide = _state.get("guide")
            if guide:
                _draw_guide(layout, guide)

            box = layout.box()
            box.label(text="Reduce lag", icon="SORTTIME")
            row = box.row(align=True)
            row.operator("randomgenerals.analyze_lag", icon="VIEWZOOM")
            row.operator("randomgenerals.explain_lag", icon="INFO")
            shown = set()
            for severity, message, fix in _state["findings"][:8]:
                icon = {"high": "ERROR", "medium": "INFO"}.get(
                    severity, "DOT")
                _wrap(box, message, icon)
                if fix and fix not in shown:
                    shown.add(fix)
                    op = box.operator("randomgenerals.lag_fix",
                                      text=_FIX_LABELS[fix], icon="CHECKMARK")
                    op.action = fix

            if _state["status"]:
                _wrap(layout, _state["status"],
                      "TIME" if _state["busy"] else "BLANK1")

    def _draw_guide(layout, guide):
        box = layout.box()
        i, steps = guide["index"], guide["steps"]
        step = steps[i]
        head = box.row()
        head.label(text="%s - step %d of %d" % (guide["title"], i + 1,
                                                len(steps)), icon="HAND")
        head.operator("randomgenerals.guide", text="",
                      icon="X").action = "CLOSE"
        _wrap(box, step["say"], "CHECKMARK" if guide["done"][i] else "RIGHTARROW")
        if step["keys"]:
            _wrap(box, step["keys"], "EVENT_SHIFT"
                  if "shift" in step["keys"].lower() else "MOUSE_LMB")
        if step["mode"] in ("OBJECT", "EDIT"):
            box.label(text="In %s Mode (Tab switches)" % step["mode"].title(),
                      icon="OBJECT_DATAMODE" if step["mode"] == "OBJECT"
                      else "EDITMODE_HLT")
        if not step["check"]:
            box.label(text="Press Next when you have done this.",
                      icon="INFO")
        row = box.row(align=True)
        row.operator("randomgenerals.guide", text="Show me where",
                     icon="RESTRICT_SELECT_OFF").action = "POINT"
        sub = row.row(align=True)
        sub.enabled = bool(step["do"])
        sub.operator("randomgenerals.guide", text="Do it for me",
                     icon="PLAY").action = "DO"
        row = box.row(align=True)
        back = row.row(align=True)
        back.enabled = i > 0
        back.operator("randomgenerals.guide", text="Back",
                      icon="TRIA_LEFT").action = "BACK"
        nxt = row.row(align=True)
        nxt.enabled = i + 1 < len(steps)
        nxt.operator("randomgenerals.guide", text="Next",
                     icon="TRIA_RIGHT").action = "NEXT"
        box.label(text="%d of %d done" % (sum(guide["done"]), len(steps)))

    _FIX_LABELS = {"SUBDIV": "Cap viewport subdivision",
                   "SIMPLIFY": "Turn on Simplify",
                   "SAMPLES": "Lower viewport samples",
                   "DECIMATE": "Decimate heavy meshes (viewport)"}

    def _wrap(layout, text, icon="NONE", width=42):
        """Panels do not wrap text; split it into lines by hand."""
        col = layout.column(align=True)
        words, line, first = text.split(), "", True
        for w in words:
            if len(line) + len(w) + 1 > width and line:
                col.label(text=line, icon=icon if first else "BLANK1")
                line, first = w, False
            else:
                line = (line + " " + w).strip()
        if line:
            col.label(text=line, icon=icon if first else "BLANK1")

    _CLASSES = (RG_Preferences, RG_OT_ask, RG_OT_run_script, RG_OT_fix,
                RG_OT_guide, RG_OT_new_chat, RG_OT_analyze_lag, RG_OT_explain_lag,
                RG_OT_lag_fix, RG_PT_panel)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.rg_prompt = StringProperty(
        name="Request", default="",
        description="e.g. 'a low-poly tree' or 'how do I bevel an edge'")


def unregister():
    for timer in (_poll, _guide_tick):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    del bpy.types.WindowManager.rg_prompt
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
