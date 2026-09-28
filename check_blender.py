# -*- coding: utf-8 -*-
"""Does the Blender add-on work - outside Blender and inside it?

    python check_blender.py
    BLENDER_PY=/path/to/python-with-bpy python check_blender.py

Two halves. The first needs nothing: it imports the add-on as a plain
module (it only imports bpy when it can) and checks the parts that are
pure Python - pulling the script out of a reply, flagging risky lines,
the lag rules - plus a real HTTP round trip to a throwaway local server
standing in for /v1, and the Flask route that serves the file.

The second needs Blender's Python. `pip install bpy` gives one (the
wheel matches a single Python minor version - bpy 4.2 is 3.11). With
BLENDER_PY set, this file re-runs itself under it and installs the
add-on the way a person does (Install from Disk, enable), builds a
deliberately heavy scene, runs the lag analysis and every fix, and runs
a generated script, a failing one, and a full ask round trip. Without
it that half is reported as skipped, not passed.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "blender", "randomgenerals_blender.py")
sys.path.insert(0, os.path.join(HERE, "blender"))
FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label
          + ("" if ok else "  (got %r, want %r)" % (got, want)))
    if not ok:
        FAILED.append(label)


# ------------------------------------------------------------------ #
# A stand-in for /v1/chat/completions
# ------------------------------------------------------------------ #

SCRIPT_REPLY = (
    "Here you go.\n\n```python\nimport bpy\n\nHEIGHT = 2.0\n"
    "col = bpy.data.collections.new('Tower')\n"
    "bpy.context.scene.collection.children.link(col)\n"
    "mesh = bpy.data.meshes.new('Tower')\n"
    "mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)], [], "
    "[(0,1,2,3)])\n"
    "obj = bpy.data.objects.new('Tower', mesh)\ncol.objects.link(obj)\n"
    "obj.scale.z = HEIGHT\n```\n\n- Change HEIGHT to make it taller.\n")


class FakeV1(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):                                  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeV1.seen.append({"auth": self.headers.get("Authorization"),
                            "agent": self.headers.get("User-Agent"),
                            "body": body})
        if self.headers.get("Authorization") != "Bearer good-key":
            self._send(401, {"error": {"message": "Invalid API key."}})
            return
        self._send(200, {"choices": [{"message": {
            "role": "assistant", "content": SCRIPT_REPLY}}]})

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def start_fake():
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeV1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port


# ------------------------------------------------------------------ #
# Half one: plain Python
# ------------------------------------------------------------------ #

def plain_checks():
    import randomgenerals_blender as rg
    print("pure Python")

    check("labelled block is extracted",
          rg.extract_script(SCRIPT_REPLY).startswith("import bpy"), True)
    two = "```python\nprint(1)\n```\n```python\nimport bpy\nx = 1\ny = 2\n```"
    check("longest labelled block wins",
          rg.extract_script(two), "import bpy\nx = 1\ny = 2\n")
    check("unlabelled bpy block is used",
          rg.extract_script("```\nimport bpy\n```"), "import bpy\n")
    check("reply without code has no script",
          rg.extract_script("Press Tab, then Ctrl+B."), None)
    check("a bash block is not a script",
          rg.extract_script("```bash\nls\n```"), None)

    check("clean script has nothing risky",
          rg.risky_lines(rg.extract_script(SCRIPT_REPLY)), [])
    risky = rg.risky_lines(
        "import bpy\nimport os\nfrom subprocess import run\n"
        "f = open('x')\nbpy.ops.wm.quit_blender()\n"
        "# import os  (a comment is fine)\nobj.data.update()\n"
        "bpy.app.handlers.frame_change_pre.append(f)\n")
    check("risky lines are found, comments are not",
          [n for n, _, _ in risky], [2, 3, 4, 5, 8])
    check("bpy.ops.mesh is not risky",
          rg.risky_lines("bpy.ops.mesh.primitive_cube_add()"), [])
    check("a method called .open() is not the builtin",
          rg.risky_lines("layout.open(x)\nbm.exec_x()"), [])

    history = [{"role": "user", "content": "u%d" % i} for i in range(20)]
    msgs = rg.build_messages("BUILD", " a tree ", "Blender 4.2 ...",
                             history, "4.2.0")
    check("system prompt first, names the version",
          (msgs[0]["role"], "Blender 4.2.0" in msgs[0]["content"]),
          ("system", True))
    check("history is capped", len(msgs), 1 + rg.HISTORY_TURNS + 1)
    check("scene rides on the newest turn",
          msgs[-1]["content"], "a tree\n\n[Current scene]\nBlender 4.2 ...")
    check("teach prompt is not the build prompt",
          "MODE: TEACH" in rg.build_messages("TEACH", "x", "", [], "4")[0][
              "content"], True)

    heavy = {
        "version": "4.2.0", "engine": "CYCLES", "preview_samples": 1024,
        "object_count": 3, "total_tris": 3_200_000, "shading": "RENDERED",
        "images": [{"name": "rock_8k", "size": (8192, 8192)},
                   {"name": "small", "size": (512, 512)}],
        "objects": [
            {"name": "Sculpt", "type": "MESH", "tris_base": 3_000_000,
             "tris_eval": 3_000_000, "modifiers": [], "particles": 0},
            {"name": "Ball", "type": "MESH", "tris_base": 960,
             "tris_eval": 245_760, "particles": 0, "modifiers": [
                 {"type": "SUBSURF", "levels": 4, "render_levels": 4}]},
            {"name": "Grass", "type": "MESH", "tris_base": 2,
             "tris_eval": 2, "modifiers": [], "particles": 200_000},
        ],
    }
    found = rg.lag_findings(heavy)
    fixes = {f for _, _, f in found}
    check("heavy scene flags every cause",
          fixes >= {"DECIMATE", "SUBDIV", "SIMPLIFY", "SAMPLES"}, True)
    check("subdivision level 4 is high severity",
          any(s == "high" and f == "SUBDIV" for s, _, f in found), True)
    check("4K texture is named, 512 is not",
          any("rock_8k" in m and "small" not in m for _, m, _ in found), True)
    check("rendered shading is mentioned",
          any("Rendered shading" in m for _, m, _ in found), True)
    light = {"engine": "BLENDER_EEVEE_NEXT", "eevee_samples": 16,
             "objects": [{"name": "Cube", "tris_base": 12, "tris_eval": 12,
                          "modifiers": []}], "total_tris": 12}
    check("light scene gets the all-clear only",
          [f for _, _, f in rg.lag_findings(light)], [None])
    summary = rg.stats_summary(heavy, found)
    check("summary lists the heaviest object first",
          summary.splitlines()[1].startswith("- Sculpt"), True)

    url = start_fake()
    reply = rg.call_api(url, "good-key", [{"role": "user", "content": "hi"}])
    check("round trip returns the reply", reply, SCRIPT_REPLY)
    sent = FakeV1.seen[-1]
    check("key sent as bearer", sent["auth"], "Bearer good-key")
    check("own user agent", sent["agent"].startswith("RandomGenerals-"), True)
    check("not streamed, bounded", (sent["body"]["stream"],
                                    sent["body"]["max_tokens"]),
          (False, rg.MAX_TOKENS))
    for key, want in (("bad-key", "Invalid API key."),
                      ("", "No API key")):
        try:
            rg.call_api(url, key, [])
            check("error for %r" % key, "no error", want)
        except rg.ApiError as e:
            check("server's error message surfaces for %r" % key,
                  str(e).startswith(want), True)
    try:
        rg.call_api("http://127.0.0.1:1", "k", [], timeout=3)
    except rg.ApiError as e:
        check("unreachable server is a sentence",
              str(e).startswith("Could not reach"), True)


def route_checks():
    print("server route")
    work = tempfile.mkdtemp(prefix="blendertest-")
    os.environ["DB_PATH"] = os.path.join(work, "t.db")
    os.environ.setdefault("SECRET_KEY", "test-only")
    sys.path.insert(0, HERE)
    import app as appmod
    client = appmod.app.test_client()
    res = client.get("/blender/addon.py")
    check("add-on downloads", res.status_code, 200)
    check("as an attachment",
          "attachment" in res.headers.get("Content-Disposition", ""), True)
    with open(ADDON, "rb") as f:
        check("it is the file in the repo", res.data, f.read())
    res.close()
    page = client.get("/app").get_data(as_text=True)
    check("settings links the add-on", 'href="/blender/addon.py"' in page,
          True)
    check("settings has the create-key button",
          'id="setApiKeyCreate"' in page, True)


# ------------------------------------------------------------------ #
# Half two: inside Blender
# ------------------------------------------------------------------ #

def blender_checks():
    import time
    import bpy
    print("inside Blender %s" % bpy.app.version_string)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_install(filepath=ADDON, overwrite=True)
    bpy.ops.preferences.addon_enable(module="randomgenerals_blender")
    import randomgenerals_blender as rg
    check("add-on enabled", "randomgenerals_blender"
          in bpy.context.preferences.addons, True)
    check("operators registered", hasattr(bpy.ops.randomgenerals, "ask"),
          True)

    # A scene built to be slow: a subdivided sphere at viewport level 4
    # and 1024 Cycles viewport samples.
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.preview_samples = 1024
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32)
    ball = bpy.context.object
    ball.name = "Ball"
    sub = ball.modifiers.new("Subdivision", "SUBSURF")
    sub.levels, sub.render_levels = 4, 4

    bpy.ops.randomgenerals.analyze_lag()
    fixes = {f for _, _, f in rg._state["findings"]}
    check("real scene: subdivision flagged", "SUBDIV" in fixes, True)
    check("real scene: samples flagged", "SAMPLES" in fixes, True)
    check("real scene: sphere is heavy after modifiers",
          "DECIMATE" in fixes, True)
    check("report written", LAG_TEXT_OK(), True)

    bpy.ops.randomgenerals.lag_fix(action="SUBDIV")
    check("viewport subdivision capped", sub.levels, 1)
    check("render subdivision untouched", sub.render_levels, 4)
    bpy.ops.randomgenerals.lag_fix(action="SAMPLES")
    check("viewport samples lowered", scene.cycles.preview_samples, 64)
    bpy.ops.randomgenerals.lag_fix(action="SIMPLIFY")
    check("simplify on", scene.render.use_simplify, True)
    # Heavy again: Simplify (just switched on) caps what the evaluated
    # mesh shows, so it goes off for this one, or nothing is heavy.
    scene.render.use_simplify = False
    sub.levels = 4
    bpy.ops.randomgenerals.lag_fix(action="DECIMATE", ratio=0.25)
    dec = ball.modifiers.get("RG Decimate")
    check("decimate added, viewport only",
          (dec is not None, dec and dec.show_render, dec and dec.ratio),
          (True, False, 0.25))
    bpy.ops.randomgenerals.lag_fix(action="DECIMATE")
    check("decimate not stacked twice",
          sum(m.type == "DECIMATE" for m in ball.modifiers), 1)

    # The ask round trip, against the stand-in server. Timers do not
    # fire in the bpy module (there is no event loop), so the poll the
    # timer would run is called by hand once the worker has answered.
    prefs = bpy.context.preferences.addons["randomgenerals_blender"].preferences
    prefs.server_url = start_fake()
    prefs.api_key = "good-key"
    bpy.context.window_manager.rg_prompt = "a tower"
    bpy.ops.randomgenerals.ask(mode="BUILD")
    check("busy while waiting", rg._state["busy"], True)
    for _ in range(100):
        if not rg._results.empty():
            break
        time.sleep(0.05)
    rg._poll()
    check("script landed in the Text Editor",
          bpy.data.texts[rg.SCRIPT_TEXT].as_string().startswith("import bpy"),
          True)
    sent = FakeV1.seen[-1]["body"]["messages"]
    check("request carried the scene",
          "[Current scene]" in sent[-1]["content"]
          and "Ball" in sent[-1]["content"], True)
    check("conversation remembered", len(rg._state["history"]), 2)

    bpy.ops.randomgenerals.run_script()
    check("script ran and built the tower",
          "Tower" in bpy.data.objects and "Tower" in bpy.data.collections,
          True)

    bpy.data.texts[rg.SCRIPT_TEXT].from_string(
        "import bpy\nbpy.data.objects['Nope'].location.x = 1\n")
    # From the UI an ERROR report is a message in the status bar; called
    # from Python, Blender raises it as RuntimeError instead.
    try:
        result = bpy.ops.randomgenerals.run_script()
    except RuntimeError as e:
        result = "reported: " + str(e).splitlines()[0]
    check("failing script is reported, not crashed",
          result.startswith("reported: Error: KeyError")
          if isinstance(result, str) else result, True)
    check("error kept for Fix it", "KeyError" in (rg._state["last_error"]
                                                   or ""), True)
    bpy.ops.randomgenerals.fix()
    for _ in range(100):
        if not rg._results.empty():
            break
        time.sleep(0.05)
    rg._poll()
    fix_msg = FakeV1.seen[-1]["body"]["messages"][-1]["content"]
    check("fix request carries the script and the traceback",
          "objects['Nope']" in fix_msg and "KeyError" in fix_msg, True)

    prefs.api_key = "bad-key"
    bpy.ops.randomgenerals.ask(mode="TEACH")
    for _ in range(100):
        if not rg._results.empty():
            break
        time.sleep(0.05)
    rg._poll()
    check("a refused key shows the server's message",
          rg._state["status"], "Invalid API key.")

    bpy.ops.preferences.addon_disable(module="randomgenerals_blender")
    check("disables cleanly",
          hasattr(bpy.types.WindowManager, "rg_prompt"), False)


def LAG_TEXT_OK():                                      # noqa: N802
    import bpy
    import randomgenerals_blender as rg
    t = bpy.data.texts.get(rg.LAG_TEXT)
    return bool(t and "Ball" in t.as_string())


def main():
    if "--inside-blender" in sys.argv:
        blender_checks()
    else:
        plain_checks()
        route_checks()
        blender_py = os.environ.get("BLENDER_PY")
        print("inside Blender")
        if not blender_py:
            print("  SKIP set BLENDER_PY to a Python with bpy installed")
        else:
            env = dict(os.environ)
            cfg = tempfile.mkdtemp(prefix="blendercfg-")
            # Blender installs add-ons into the user's config; point that
            # at a throwaway directory so the check leaves no trace.
            env["BLENDER_USER_CONFIG"] = os.path.join(cfg, "config")
            env["BLENDER_USER_SCRIPTS"] = os.path.join(cfg, "scripts")
            code = subprocess.call([blender_py, __file__, "--inside-blender"],
                                   env=env)
            if code:
                FAILED.append("inside Blender (exit %d)" % code)
    if FAILED:
        print("\n%d FAILED" % len(FAILED))
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
