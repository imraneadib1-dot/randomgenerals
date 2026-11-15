"""pip 1.0 for Blender - a tutor in the sidebar, and a speed-up tool.

WHAT THIS IS
An add-on that puts two things in Blender's N-panel:

  Ask    a question box that reaches randomgenerals.com and answers with
         numbered steps written against Blender's real menu paths, plus a
         button that performs a step for you where the step is something
         an operator can do.

  Speed  a scan of the open scene for the things that actually make
         Blender stutter, each with a fix that can be applied on click
         and undone like any other edit.

WHAT IT IS NOT
It does not move your mouse or highlight Blender's own buttons. Blender
gives add-ons no way to ask where its widgets are on screen - there is no
API for "where is the Modifier tab" - so an add-on that claimed to point
at one would be guessing at coordinates that change with every theme,
resolution and layout. Naming the exact path and offering to do the step
is the honest version of the same help, and it survives the user having
a different UI layout.

WHY THE SCAN IS A PURE FUNCTION
scan_scene() takes a scene and returns findings. It touches no UI and no
globals, so it runs under `blender --background` and can be tested on a
built scene rather than by opening Blender and looking. Everything that
mutates the file is a separate operator.
"""

bl_info = {
    "name": "pip 1.0",
    "author": "Imrane Adib",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > pip 1.0",
    "description": "Ask how to build something, and find what is slowing the scene down.",
    "category": "3D View",
}

import json
import os
import urllib.request
import urllib.error

try:
    import bpy
except ImportError:      # imported for testing outside Blender
    bpy = None


DEFAULT_BASE_URL = "https://randomgenerals.com/v1"
USER_AGENT = "pip-Blender/0.2"

# Above this, a subdivision modifier is costing viewport frames for
# detail nobody can see while modelling. Render level is left alone - the
# whole point of the viewport/render split is that they differ.
VIEWPORT_SUBDIV_MAX = 2

# Triangles in the viewport before a scene starts feeling heavy on the
# kind of laptop this add-on exists for. Not a hard rule: a single dense
# sculpt is fine, a hundred dense trees is not, and the count is what
# separates them.
TRIANGLE_BUDGET = 2_000_000

# A texture larger than this is being downscaled by the GPU every draw
# for detail the viewport cannot show.
TEXTURE_PIXELS_MAX = 4096 * 4096


# ---------------------------------------------------------------- scan

def _modifier_findings(obj):
    """Subdivision and other viewport-expensive modifiers on one object."""
    out = []
    for mod in getattr(obj, "modifiers", []):
        if mod.type == "SUBSURF" and mod.levels > VIEWPORT_SUBDIV_MAX:
            out.append({
                "kind": "subdiv",
                "object": obj.name,
                "modifier": mod.name,
                "detail": "viewport subdivision is %d" % mod.levels,
                "fix": "Lower it to %d. Render stays at %d."
                       % (VIEWPORT_SUBDIV_MAX, mod.render_levels),
                "severity": mod.levels - VIEWPORT_SUBDIV_MAX,
            })
        # A modifier set to display in the viewport but not render is
        # usually left over from testing, and costs every frame.
        if getattr(mod, "show_viewport", False) and not getattr(mod, "show_render", True):
            out.append({
                "kind": "viewport_only_modifier",
                "object": obj.name,
                "modifier": mod.name,
                "detail": "shows in the viewport but not in renders",
                "fix": "Turn off its viewport display - it is not in the render anyway.",
                "severity": 1,
            })
    return out


def _evaluated_triangles(scene, depsgraph=None):
    """Triangles the viewport actually draws, modifiers included.

    The raw mesh count is the wrong number: a 500-triangle cube under
    three levels of subdivision draws 32,000. Asking the depsgraph for
    the evaluated mesh is what makes this match what the GPU is doing.
    """
    total = 0
    if depsgraph is None:
        return total
    for inst in depsgraph.object_instances:
        obj = inst.object
        if obj is None or obj.type != "MESH":
            continue
        # hide_viewport, not visible_get(): the latter is answered against
        # a view layer on a screen and is False for everything when
        # Blender runs headless, which made this count zero rather than
        # making it skip hidden objects.
        if getattr(obj, "hide_viewport", False):
            continue
        try:
            mesh = obj.to_mesh()
        except (RuntimeError, AttributeError):
            continue
        if mesh is None:
            continue
        try:
            # loop_triangles is empty until this fills it. Without the
            # call the count silently fell back to polygons, which
            # under-reports every quad by half.
            mesh.calc_loop_triangles()
            total += len(mesh.loop_triangles)
        except (RuntimeError, AttributeError):
            total += sum(max(0, len(poly.vertices) - 2)
                         for poly in mesh.polygons)
        finally:
            try:
                obj.to_mesh_clear()
            except (RuntimeError, AttributeError):
                pass
    return total


def scan_scene(scene, depsgraph=None, images=None):
    """-> list of findings, worst first. Pure: reads, never writes."""
    findings = []

    for obj in getattr(scene, "objects", []):
        findings.extend(_modifier_findings(obj))

    tris = _evaluated_triangles(scene, depsgraph)
    if tris > TRIANGLE_BUDGET:
        findings.append({
            "kind": "triangles",
            "object": "",
            "detail": "the viewport is drawing about %s triangles" % f"{tris:,}",
            "fix": "Turn on Simplify and cap viewport subdivision, or hide "
                   "what you are not working on.",
            "severity": tris // TRIANGLE_BUDGET + 2,
        })

    for img in (images if images is not None else []):
        w, h = (img.size[0], img.size[1]) if len(img.size) >= 2 else (0, 0)
        if w * h > TEXTURE_PIXELS_MAX:
            findings.append({
                "kind": "texture",
                "object": img.name,
                "detail": "%dx%d texture" % (w, h),
                "fix": "Set a viewport texture size limit in Preferences > "
                       "Viewport, or downscale the image.",
                "severity": 2,
            })

    render = getattr(scene, "render", None)
    if render is not None and not getattr(render, "use_simplify", False):
        # Reported last and lowest, because on a light scene turning it on
        # changes nothing - it is the lever for when something else here
        # already found a problem.
        findings.append({
            "kind": "simplify_off",
            "object": "",
            "detail": "Simplify is off",
            "fix": "Turn it on to cap viewport subdivision across the whole "
                   "file in one switch.",
            "severity": 1 if findings else 0,
        })

    findings.sort(key=lambda f: -f["severity"])
    return findings


def apply_fix(scene, finding):
    """Apply one finding's fix. -> (changed, message). Writes."""
    kind = finding.get("kind")

    if kind == "subdiv":
        obj = scene.objects.get(finding["object"])
        mod = obj.modifiers.get(finding["modifier"]) if obj else None
        if not mod:
            return False, "That modifier is gone."
        was = mod.levels
        mod.levels = VIEWPORT_SUBDIV_MAX
        return True, "%s: viewport subdivision %d to %d" % (
            finding["object"], was, VIEWPORT_SUBDIV_MAX)

    if kind == "viewport_only_modifier":
        obj = scene.objects.get(finding["object"])
        mod = obj.modifiers.get(finding["modifier"]) if obj else None
        if not mod:
            return False, "That modifier is gone."
        mod.show_viewport = False
        return True, "%s: turned off viewport display for %s" % (
            finding["object"], finding["modifier"])

    if kind in ("simplify_off", "triangles"):
        scene.render.use_simplify = True
        scene.render.simplify_subdivision = VIEWPORT_SUBDIV_MAX
        return True, "Simplify on, viewport subdivision capped at %d" % (
            VIEWPORT_SUBDIV_MAX)

    if kind == "texture":
        # Deliberately not automatic. Resizing someone's image is
        # destructive and not undoable the way a modifier setting is.
        return False, ("Set Preferences > Viewport > Texture Limit to 4096 - "
                       "this one is left to you because resizing the image "
                       "itself cannot be undone.")

    return False, "No automatic fix for that one."


# ------------------------------------------------------------- the API

def ask_randomgenerals(question, api_key, base_url=DEFAULT_BASE_URL,
                       model=None, timeout=60):
    """Ask the site for Blender steps. -> (text, error).

    Uses the OpenAI-compatible /v1 surface the site already exposes, so
    this needs no new server route - only a key from Settings > API.
    """
    if not api_key:
        return "", ("No API key. Put one from randomgenerals.com > Settings > "
                    "API into the add-on preferences.")

    system = (
        "You are helping someone inside Blender, and they can see the "
        "viewport while they read you. Answer as numbered steps. Every "
        "step names the exact path in Blender's interface - 'Properties > "
        "Modifier Properties > Add Modifier > Generate > Subdivision "
        "Surface' - and the keyboard shortcut when one exists. Name panels "
        "and buttons as Blender labels them, in Blender's own wording. "
        "Keep each step to one action. No preamble, no closing summary. "
        "If the request needs a Python expression, give it as one line "
        "that can be pasted into Blender's Python console.")

    body = json.dumps({
        "model": model or "auto",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": question}],
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json",
                 # Without this urllib sends "Python-urllib/3.x", which
                 # Cloudflare's browser integrity check in front of the
                 # site refuses with a bare 403 "error code: 1010" - the
                 # request never reached the server, so every Ask failed
                 # no matter what key was set.
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = b""
        detail = ""
        try:
            raw = e.read()
            detail = json.loads(raw.decode("utf-8")).get(
                "error", {}).get("message", "")
        except Exception:                                    # noqa: BLE001
            pass
        if e.code == 403 and b"1010" in raw:
            return "", ("Cloudflare blocked the request before it reached "
                        "the site (error 1010).")
        if e.code == 401:
            return "", ("The API key was rejected. Making a new key in "
                        "Settings > API replaces the old one - paste the "
                        "current one into the add-on preferences.")
        if e.code == 402:
            return "", "That account is out of credits."
        return "", "randomgenerals.com returned %d. %s" % (e.code, detail)
    except urllib.error.URLError as e:
        return "", "Could not reach %s (%s)." % (base_url, e.reason)
    except (ValueError, KeyError):
        return "", "Could not read the reply."

    try:
        return data["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError):
        return "", "The reply arrived in an unexpected shape."


# ---------------------------------------------------------------- UI
# Everything below only exists inside Blender. The logic above is kept
# free of it so it can be tested with `blender --background`, where no
# panel is ever drawn.

if bpy is not None:

    class RGPreferences(bpy.types.AddonPreferences):
        bl_idname = __name__

        api_key: bpy.props.StringProperty(
            name="API key",
            description="From randomgenerals.com > Settings > API",
            subtype="PASSWORD",
            default="",
        )
        base_url: bpy.props.StringProperty(
            name="Server",
            description="Change only to point at a local server",
            default=DEFAULT_BASE_URL,
        )

        def draw(self, _context):
            col = self.layout.column()
            col.prop(self, "api_key")
            col.prop(self, "base_url")

    def _prefs(context):
        return context.preferences.addons[__name__].preferences

    class RGState(bpy.types.PropertyGroup):
        question: bpy.props.StringProperty(
            name="", description="What do you want to build?",
            default="")
        answer: bpy.props.StringProperty(default="")
        status: bpy.props.StringProperty(default="")

    class RG_OT_ask(bpy.types.Operator):
        bl_idname = "randomgenerals.ask"
        bl_label = "Ask"
        bl_description = "Ask randomgenerals.com how to do this in Blender"

        def execute(self, context):
            st = context.scene.randomgenerals
            p = _prefs(context)
            if not st.question.strip():
                self.report({'INFO'}, "Type a question first.")
                return {'CANCELLED'}
            st.status = "Asking..."
            text, err = ask_randomgenerals(
                st.question, p.api_key, p.base_url or DEFAULT_BASE_URL)
            if err:
                st.answer = ""
                st.status = err
                self.report({'ERROR'}, err)
                return {'CANCELLED'}
            st.answer = text
            st.status = ""
            return {'FINISHED'}

    class RG_OT_scan(bpy.types.Operator):
        bl_idname = "randomgenerals.scan"
        bl_label = "Scan the scene"
        bl_description = "Find what is making the viewport slow"

        def execute(self, context):
            dg = context.evaluated_depsgraph_get()
            found = scan_scene(context.scene, dg, bpy.data.images)
            context.scene.randomgenerals.status = (
                "Nothing slowing it down." if not found
                else "%d thing%s to fix." % (len(found),
                                             "" if len(found) == 1 else "s"))
            _findings.clear()
            _findings.extend(found)
            return {'FINISHED'}

    class RG_OT_fix_all(bpy.types.Operator):
        bl_idname = "randomgenerals.fix_all"
        bl_label = "Fix all"
        bl_description = "Apply every fix found. Ctrl+Z undoes the lot"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            done = 0
            for f in list(_findings):
                ok, _msg = apply_fix(context.scene, f)
                done += 1 if ok else 0
            _findings.clear()
            context.scene.randomgenerals.status = "Applied %d fix%s." % (
                done, "" if done == 1 else "es")
            self.report({'INFO'}, "Applied %d fix(es)." % done)
            return {'FINISHED'}

    # Findings live at module level rather than on the scene: they are a
    # view of the file at one moment, not part of it, and saving them
    # into the .blend would mean a stale list reappearing tomorrow.
    _findings = []

    class RG_PT_panel(bpy.types.Panel):
        bl_label = "pip 1.0"
        bl_idname = "RG_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "pip 1.0"

        def draw(self, context):
            st = context.scene.randomgenerals
            layout = self.layout

            box = layout.box()
            box.label(text="Ask", icon='OUTLINER_OB_LIGHT')
            box.prop(st, "question", text="")
            box.operator("randomgenerals.ask", icon='PLAY')
            if st.answer:
                col = box.column(align=True)
                # One label per line: Blender has no wrapping text widget,
                # so a long paragraph would be clipped to the panel width
                # with no way to read the rest.
                for line in st.answer.splitlines():
                    if line.strip():
                        col.label(text=line[:90])

            box = layout.box()
            box.label(text="Speed", icon='SORTTIME')
            box.operator("randomgenerals.scan", icon='VIEWZOOM')
            if _findings:
                col = box.column(align=True)
                for f in _findings[:8]:
                    row = col.row()
                    row.label(text=("%s %s" % (f["object"], f["detail"]))[:60],
                              icon='ERROR')
                if len(_findings) > 8:
                    col.label(text="...and %d more" % (len(_findings) - 8))
                box.operator("randomgenerals.fix_all", icon='CHECKMARK')
            if st.status:
                layout.label(text=st.status[:70])

    _classes = (RGPreferences, RGState, RG_OT_ask, RG_OT_scan,
                RG_OT_fix_all, RG_PT_panel)

    def register():
        for c in _classes:
            bpy.utils.register_class(c)
        bpy.types.Scene.randomgenerals = bpy.props.PointerProperty(type=RGState)

    def unregister():
        del bpy.types.Scene.randomgenerals
        for c in reversed(_classes):
            bpy.utils.unregister_class(c)

else:                       # outside Blender, so the module still imports
    def register():
        pass

    def unregister():
        pass
