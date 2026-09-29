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
    replies = []        # queued answers; SCRIPT_REPLY once it is empty

    def do_POST(self):                                  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeV1.seen.append({"auth": self.headers.get("Authorization"),
                            "agent": self.headers.get("User-Agent"),
                            "body": body})
        if self.headers.get("Authorization") != "Bearer good-key":
            self._send(401, {"error": {"message": "Invalid API key."}})
            return
        content = FakeV1.replies.pop(0) if FakeV1.replies else SCRIPT_REPLY
        self._send(200, {"choices": [{"message": {
            "role": "assistant", "content": content}}]})

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


GUIDE = {"title": "Guide cube", "steps": [
    {"say": "Add a cube.", "keys": "Shift+A > Mesh > Cube",
     "area": "VIEW_3D", "mode": "OBJECT",
     "check": "'GuideCube' in D.objects",
     "do": "import bpy\nm = bpy.data.meshes.new('GuideCube')\n"
           "o = bpy.data.objects.new('GuideCube', m)\n"
           "bpy.context.scene.collection.objects.link(o)\n"
           "bpy.context.view_layer.objects.active = o"},
    {"say": "Add a Bevel modifier.", "area": "PROPERTIES", "tab": "MODIFIER",
     "keys": "Add Modifier > Generate > Bevel",
     "check": "C.object is not None and any(m.type == 'BEVEL' for m in "
              "C.object.modifiers)",
     "do": ""},
    {"say": "Admire it.", "area": "SPACESHIP",
     "check": "D.objects.remove(D.objects[0])"},
]}


def wait_reply(rg, polls=1):
    """Stand in for the timer, which never fires in the bpy module."""
    import time
    for _ in range(polls):
        for _ in range(200):
            if not rg._results.empty():
                break
            time.sleep(0.05)
        rg._poll()


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

    guide = rg.parse_guide("Sure!\n```json\n" + json.dumps(GUIDE) + "\n```")
    check("guide read out of a fenced, chatty reply",
          (guide["title"], len(guide["steps"])), ("Guide cube", 3))
    check("unknown editor falls back to the 3D Viewport",
          guide["steps"][2]["area"], "VIEW_3D")
    check("a check that changes the scene is dropped",
          guide["steps"][2]["check"], "")
    check("not JSON is no guide", rg.parse_guide("Press Tab."), None)
    check("no usable steps is no guide",
          rg.parse_guide('{"steps": [{"keys": "Tab"}]}'), None)
    for expr, want in (("'Mug' in D.objects", True),
                       ("C.object and any(m.type == 'BEVEL' for m in "
                        "C.object.modifiers)", True),
                       ("bpy.ops.mesh.primitive_cube_add()", False),
                       ("D.objects.remove(D.objects[0])", False),
                       ("__import__('os')", False),
                       ("(x := 1)", False),
                       ("a = 1", False),
                       ("", False)):
        check("check_is_safe(%r)" % expr[:30], rg.check_is_safe(expr), want)
    check("a check that raises counts as not done",
          rg.evaluate_check("D.missing.attr", {"D": object()}), False)
    check("a check cannot reach builtins it was not given",
          rg.evaluate_check("print('x') or True", {}), False)

    check("Cloudflare 1010 is explained",
          "1010" in rg._http_failure(403, "error code: 1010", "u"), True)
    check("404 names the address",
          "https://x/v1" in rg._http_failure(404, "<html>", "https://x/v1"),
          True)

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

    # /v1 end to end with a real account and key, the way the add-on
    # calls it. Only the model is stubbed.
    import providers
    import time as _time
    client.post("/api/auth/signup", json={
        "email": "blender@example.com", "password": "password123",
        "name": "Blender Test", "age": 30})
    key = client.post("/api/account/api-key").get_json()["api_key"]
    appmod._v1_route = lambda requested: ("groq", "openai/gpt-oss-120b")
    appmod._failover_chain = lambda provider, mode: []
    calls = []

    def streamer(refusals):
        def stream(model, history, options=None, usage=None, **kw):
            calls.append(_time.time())
            if len(calls) <= refusals:
                raise providers.RateLimited("per-minute budget spent")
            if usage is not None:
                usage["eval_count"] = 10
            yield "```python\nimport bpy\n```"
        return stream

    def ask():
        return client.post("/v1/chat/completions", json={
            "model": "randomgenerals", "stream": False,
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": "a cube"}]},
            headers={"Authorization": "Bearer " + key})

    check("a wrong key is refused", client.post(
        "/v1/chat/completions", json={"messages": [
            {"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer rg-nope"}).status_code, 401)
    appmod.PROVIDER_STREAMERS["groq"] = streamer(0)
    res = ask()
    check("a real key gets an answer", (res.status_code, "import bpy" in
          res.get_json()["choices"][0]["message"]["content"]), (200, True))

    # The bug: a spent per-minute budget failed the request at once,
    # where the chat bay waits for the refill.
    calls.clear()
    appmod.groq_api.budget_state = lambda: (0, 1.0)
    appmod.PROVIDER_STREAMERS["groq"] = streamer(1)
    res = ask()
    check("a short rate limit is waited out, not failed",
          (res.status_code, len(calls)), (200, 2))
    check("and the wait was about the refill time",
          0.9 < calls[1] - calls[0] < 5, True)

    calls.clear()
    appmod.groq_api.budget_state = lambda: (0, 600.0)
    appmod.PROVIDER_STREAMERS["groq"] = streamer(5)
    res = ask()
    check("a long one is reported at once, with when to retry",
          (res.status_code, len(calls),
           "refills" in res.get_json()["error"]["message"]), (503, 1, True))


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

    # ---- Guide me ----
    prefs.api_key = "good-key"
    FakeV1.replies[:] = [json.dumps(GUIDE)]
    bpy.context.window_manager.rg_prompt = "a bevelled cube"
    bpy.ops.randomgenerals.ask(mode="GUIDE")
    wait_reply(rg)
    guide = rg._state["guide"]
    check("guide opened on step 1", (guide is not None and guide["index"]),
          0)
    check("guide request used the guide prompt",
          "MODE: GUIDE" in FakeV1.seen[-1]["body"]["messages"][0]["content"],
          True)
    rg._guide_tick()
    check("an undone step is not ticked", guide["done"][0], False)
    point = rg.point_at(rg.guide_step())
    check("pointing with no window says so, does not crash",
          point.startswith(("There is no", "Pointer moved")), True)
    bpy.ops.randomgenerals.guide(action="DO")
    check("Do it for me did the step and moved on",
          ("GuideCube" in bpy.data.objects, guide["index"], guide["done"][0]),
          (True, 1, True))
    result = bpy.ops.randomgenerals.guide(action="DO")
    check("a step with no code is left to the user", result, {"CANCELLED"})
    # The user does step 2 by hand; the watcher notices.
    bpy.data.objects["GuideCube"].modifiers.new("Bevel", "BEVEL")
    rg._guide_tick()
    check("doing it by hand ticks it off and advances",
          (guide["done"][1], guide["index"]), (True, 2))
    rg._guide_tick()
    check("the unsafe check never ran", len(bpy.data.objects) > 0 and
          "GuideCube" in bpy.data.objects, True)
    bpy.ops.randomgenerals.guide(action="BACK")
    check("Back goes back", guide["index"], 1)
    bpy.ops.randomgenerals.guide(action="CLOSE")
    check("Close closes", rg._state["guide"], None)

    # ---- Make it for me: error -> fix -> look -> done ----
    broken = "```python\nimport bpy\nbpy.data.objects['Missing'].name\n```"
    fixed = ("```python\nimport bpy\nm = bpy.data.meshes.new('Robot')\n"
             "o = bpy.data.objects.new('Robot', m)\n"
             "bpy.context.scene.collection.objects.link(o)\n```")
    FakeV1.replies[:] = [broken, fixed, "DONE Built a robot."]
    before = len(FakeV1.seen)
    bpy.context.window_manager.rg_prompt = "a robot"
    bpy.ops.randomgenerals.ask(mode="AGENT")
    wait_reply(rg, polls=3)
    check("made it: fixed its own error, checked, finished",
          ("Robot" in bpy.data.objects, rg._state["agent"],
           rg._state["status"]),
          (True, None, "Done: Built a robot. Ctrl+Z undoes each step."))
    sent = [s["body"]["messages"][-1]["content"]
            for s in FakeV1.seen[before:]]
    check("three calls: ask, fix with the traceback, review with the scene",
          (len(sent), "KeyError" in sent[1], "Robot" in sent[2]),
          (3, True, True))

    FakeV1.replies[:] = ["```python\nimport os\nos.remove('x')\n```"]
    bpy.ops.randomgenerals.ask(mode="AGENT")
    wait_reply(rg)
    check("a risky script is never run unattended",
          (rg._state["agent"], rg._state["status"].startswith("Stopped")),
          (None, True))

    FakeV1.replies[:] = [broken] * 5
    bpy.ops.randomgenerals.ask(mode="AGENT")
    wait_reply(rg, polls=rg.MAX_AGENT_ROUNDS)
    check("gives up after MAX_AGENT_ROUNDS",
          (rg._state["agent"], "Still failing" in rg._state["status"]),
          (None, True))

    # A bad icon name raises inside draw() and blanks the whole panel,
    # which nothing above would notice without a screen to draw on.
    import re as _re
    valid = set(bpy.types.UILayout.bl_rna.functions["label"]
                .parameters["icon"].enum_items.keys())
    with open(ADDON, encoding="utf-8") as f:
        src = f.read()
    used = set(_re.findall(r'icon="([A-Z0-9_]+)"', src))
    used |= set(_re.findall(r'"([A-Z0-9_]+)" if', src))
    used |= set(_re.findall(r'else "([A-Z0-9_]+)"', src))
    used |= set(_re.findall(r'get\(\s*severity, "([A-Z0-9_]+)"\)', src))
    used |= set(_re.findall(r'"(?:high|medium)": "([A-Z0-9_]+)"', src))
    used -= rg.GUIDE_AREAS      # editor types in ternaries, not icons
    check("every icon the panel uses exists", sorted(used - valid), [])

    # The panel's draw() itself, with a guide open and lag findings
    # shown. There is no screen here, so a stand-in layout records every
    # call - enough to catch a typo or a wrong argument in draw code,
    # which otherwise only shows up as an empty sidebar.
    class Op:
        pass

    class Layout:
        calls = []

        def __getattr__(self, name):
            if name in ("box", "row", "column"):
                return lambda *a, **k: Layout()
            if name == "operator":
                def operator(idname, **k):
                    Layout.calls.append(("operator", idname, k.get("text")))
                    return Op()
                return operator
            return lambda *a, **k: Layout.calls.append((name, k.get("text")))

    FakeV1.replies[:] = [json.dumps(GUIDE)]
    bpy.ops.randomgenerals.ask(mode="GUIDE")
    wait_reply(rg)
    bpy.ops.randomgenerals.analyze_lag()
    panel = type("P", (), {"layout": Layout()})()
    try:
        rg.RG_PT_panel.draw(panel, bpy.context)
        drew = None
    except Exception as e:                              # noqa: BLE001
        drew = repr(e)
    check("the panel draws with a guide open", drew, None)
    labels = [c[-1] for c in Layout.calls if c[-1]]
    check("the guide's step and buttons are on it",
          all(t in labels for t in ("Guide me through it", "Make it for me",
                                    "Show me where", "Do it for me",
                                    "Guide cube - step 1 of 3")), True)

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
