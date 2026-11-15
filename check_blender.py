# -*- coding: utf-8 -*-
"""Does the Blender bridge run AI scripts safely, and say why when not?

    python check_blender.py

Three layers, each skipped with a note when what it needs is missing:

  1. The controller's pure parts - pulling a script out of a model reply,
     the local syntax check, the message that hands an error back.
  2. Headless runs through a real `blender -b`: a script that works, one
     that fails halfway (its leftovers must be rolled back), one that
     calls sys.exit (which must not end Blender).
  3. The live protocol over a real socket. A background Blender serves
     the bridge on a test port, pumping its queue by hand since headless
     Blender has no timers loop, and this process is the client - the
     same split as the controller and a Blender window.

Uses its own port and bridge file, so a Blender the user has open with
the bridge running is never touched. Never calls a model.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="blendertest-"))
TEST_PORT = 18128
os.environ["RG_BLENDER_PORT"] = str(TEST_PORT)
os.environ["RG_BLENDER_BRIDGE_FILE"] = str(WORK / "bridge.json")
os.environ.pop("RG_BLENDER_TOKEN", None)

sys.path.insert(0, os.path.abspath("."))
import blender_controller as bc                             # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-60s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


print("\n== pulling the script out of a reply ==")
check("fenced python block", bc.extract_code("Here:\n```python\nimport bpy\nx = 1\n```\nDone."),
      "import bpy\nx = 1")
check("the longest of several blocks",
      bc.extract_code("```python\nprint(1)\n```\n```python\nimport bpy\nprint('the real one')\n```"),
      "import bpy\nprint('the real one')")
check("drafts inside <think> are ignored",
      bc.extract_code("<think>```python\nimport bpy\nlong draft draft draft draft\n```</think>"
                      "```python\nimport bpy\n```"), "import bpy")
check("a reply cut off before its closing fence",
      bc.extract_code("```python\nimport bpy\nbpy.ops.mesh.primitive_cube_add("),
      "import bpy\nbpy.ops.mesh.primitive_cube_add(")
check("bare code with no fence", bc.extract_code("import bpy\nprint(2)"), "import bpy\nprint(2)")
check("prose with no code", bc.extract_code("I cannot do that."), "")

print("\n== syntax is checked before Blender is involved ==")
bad = bc.check_syntax("import bpy\nfor i in range(3)\n    pass\n")
check("a missing colon fails", bool(bad and not bad["ok"]), True)
check("and names its line", bad and bad["line"], 2)
check("valid code passes", bc.check_syntax("import bpy\nx = [1, 2]\n"), None)

print("\n== the error goes back to the model with what it needs ==")
fb = bc.feedback({"ok": False, "error": "KeyError: 'Base Colour'", "line": 6,
                  "source_line": 'bsdf.inputs["Base Colour"]', "blender": "5.1.2",
                  "traceback": "Traceback ...\nKeyError: 'Base Colour'", "rolled_back": ["Cube"]})
check("names the Blender version", "5.1.2" in fb, True)
check("quotes the failing line", 'bsdf.inputs["Base Colour"]' in fb, True)
check("says the scene was rolled back", "removed" in fb, True)
check("asks for the whole script back", "whole corrected script" in fb, True)

print("\n== picking a model ==")
_saved_env = {k: os.environ.pop(k, None) for k in (
    "PIP_ENGINE_URL", "PIP_ENGINE_KEY", "RG_API_KEY", "OPENROUTER_API_KEY",
    "BLENDER_LLM_BASE_URL", "BLENDER_LLM", "BLENDER_LLM_MODEL", "OLLAMA_URL")}
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"          # no Ollama here
os.environ["OPENROUTER_API_KEY"] = "test-only"
_real_pip_up = bc.L.pip_up
bc.L.pip_up = lambda keys, timeout=1.5: True              # the engine is "up"
_real_openai_llm = bc.L.openai_llm
asked = []


def fake_openai_llm(base_url, api_key, model, **_kw):
    def call(messages):
        asked.append(model)
        if model == "pip-1.0":
            raise bc.LLMError("429 busy")
        return "```python\nimport bpy\n```"
    call.label = call.model = model
    return call


bc.L.openai_llm = fake_openai_llm
llm = bc.llm_from_env()
check("with the engine up, pip 1.0 is asked first", llm.label.split(" -> ")[0], "pip-1.0")
check("a failing model hands over to the next", llm([]), "```python\nimport bpy\n```")
check("in order", asked, ["pip-1.0", "qwen/qwen3-coder-next"])
asked.clear()
llm([])
check("and the one that answered goes first after", asked, ["qwen/qwen3-coder-next"])
check("--llm with --model asks for exactly that", bc.llm_from_env("openrouter", "qwen/other").label, "qwen/other")
bc.L.pip_up = lambda keys, timeout=1.5: False
try:
    bc.llm_from_env("pip")
    said = None
except bc.LLMError as e:
    said = "pip_server.py" in str(e)
check("pip 1.0 asked for but not running says how to start it", said, True)
bc.L.pip_up = _real_pip_up
try:
    bc.llm_from_env("ollama")
    said = None
except bc.LLMError as e:
    said = "ollama pull" in str(e)
check("Ollama asked for but not running says how to fix it", said, True)
check("a 429's wait comes from retry-after", bc._retry_after({"retry-after": "7"}, ""), 7.0)
check("or from a 'try again in' in the message",
      bc._retry_after({}, "Please try again in 1m2.5s. Need more tokens?"), 62.5)
check("and no wait when neither says - waiting does not clear those",
      bc._retry_after({}, "Request too large ... output tokens per minute (OTPM): Limit 1000"), None)
check("a coder is picked from the local models",
      bc._local_coder(["llama3.2:latest", "qwen2.5-coder:7b", "qwen2.5:7b-instruct"]), "qwen2.5-coder:7b")
bc.L.openai_llm = _real_openai_llm
for _k, _v in _saved_env.items():
    os.environ.pop(_k, None)
    if _v is not None:
        os.environ[_k] = _v

print("\n== keys from a .env file ==")
env_file = WORK / "test.env"
env_file.write_text('# the app\'s keys\nexport PIP_ENGINE_KEY="k-quoted"\nOPENROUTER_API_KEY=or-1 # note\n'
                    "RG_API_KEY=first\nRG_API_KEY=second\nNOT A LINE\n", encoding="utf-8")
env = bc.L.parse_env_file(env_file)
check("quotes and export are stripped", env.get("PIP_ENGINE_KEY"), "k-quoted")
check("an inline comment is not part of the value", env.get("OPENROUTER_API_KEY"), "or-1")
check("a repeated key takes the later line, as dotenv does", env.get("RG_API_KEY"), "second")
check("a missing file is just no keys", bc.L.parse_env_file(WORK / "nope.env"), {})

print("\n== what the model is told about the scene ==")
note = bc.L.scene_note({"objects": 2, "object_names": ["Chair", "Lamp"], "selected": ["Chair"],
                        "active": "Chair", "camera": None, "lights": 0})
check("the objects by name", "Chair, Lamp" in note, True)
check("and which one is selected, for 'make the selected one red'", "Selected: Chair." in note, True)
check("an empty or cleared scene says so", bc.L.scene_note({"objects": 5}, cleared=True), "The scene is empty.")

print("\n== the retry loop ==")
replies = iter(["Sure! Here is how you could do it.",            # no code at all
                "```python\nimport bpy\nprint(undefined_name)\n```",
                "```python\nimport bpy\nprint('fixed')\n```"])
sent = []


def scripted_llm(messages):
    sent.append(messages[-1]["content"])
    return next(replies)


scripted_llm.model = "fake"
runs = []


def fake_run(code, structure=False):
    runs.append(code)
    if "undefined_name" in code:
        return {"ok": False, "error": "NameError: name 'undefined_name' is not defined",
                "line": 2, "source_line": "print(undefined_name)", "rolled_back": []}
    return {"ok": True, "objects_created": [{"name": "Thing"}], "stdout": "fixed\n"}


out = bc.L.build_loop("a thing", scripted_llm, fake_run, "5.1", "The scene is empty.", 3, True)
check("it gets there on the third reply", (out["ok"], len(out["attempts"])), (True, 3))
check("a reply with no code never reaches Blender", len(runs), 2)
check("the model is told there was no code", "no ```python code block" in sent[1], True)
check("and is shown the failing line next", "print(undefined_name)" in sent[2], True)
out = bc.L.build_loop("make it red", lambda m: "```python\nimport bpy\n```",
                      lambda c, structure=False: {"ok": True, "objects_created": []}, "5.1", "", 2, False)
check("an edit that creates nothing is fine when the scene has objects", out["ok"], True)
out = bc.L.build_loop("a tree", lambda m: "```python\nimport bpy\n```",
                      lambda c, structure=False: {"ok": True, "objects_created": []}, "5.1", "", 1, True)
check("but building into an empty scene must create something", out["ok"], False)
asked_structure = []


def structure_run(code, structure=False):
    asked_structure.append(structure)
    if structure:
        return {"ok": False, "kind": "structure", "error": "These parts float free: Die 2"}
    return {"ok": True, "objects_created": [{"name": "Die 1"}, {"name": "Die 2"}],
            "structure": {"floating": ["Die 2"]}}


out = bc.L.build_loop("a pair of dice", lambda m: "```python\nimport bpy\n```", structure_run,
                      "5.1", "", 3, True)
check("the structure check is asked for once, then the answer stands",
      (out["ok"], asked_structure), (True, [True, False]))
asked_structure.clear()
bc.L.build_loop("a die", lambda m: "```python\nimport bpy\n```", structure_run, "5.1", "", 1, True)
check("and never on the only try", asked_structure, [False])
print("\n== what the training data keeps ==")
from finetune.make_blender_data import verdict                  # noqa: E402
good = {"ok": True, "result": {"objects_created": [{"name": "Seat"}], "structure": {"floating": []}}}
check("a build that ran and holds together is kept", verdict(good), (True, None))
check("one whose parts still float is not",
      verdict({"ok": True, "result": {"objects_created": [{"name": "Leg"}],
                                      "structure": {"floating": ["Leg"]}}})[0], False)
check("nor one that failed, with its error as the reason",
      verdict({"ok": False, "result": {"error": "KeyError: 'Specular'"}}), (False, "KeyError: 'Specular'"))
check("nor one that made nothing", verdict({"ok": True, "result": {}})[0], False)

example = bc.L.pick_example("a low-poly oak tree with a thick trunk", [
    {"request": "a wooden chair", "code": "chair"},
    {"request": "a low-poly pine tree", "code": "x" * 900},
    {"request": "a tree stump", "code": "x" * 100}])
check("the example shown is the one sharing most words", example and example["request"], "a low-poly pine tree")
check("and none is shown when nothing is alike",
      bc.L.pick_example("a submarine", [{"request": "a wooden chair", "code": "c"}]), None)
out = bc.L.build_loop("x", lambda m: "unused", fake_run, "5.1", "", 3, True, should_stop=lambda: True)
check("cancelling stops before asking", (out["ok"], out.get("cancelled"), out["attempts"]), (False, True, []))

BLENDER = bc.find_blender()
if not BLENDER:
    print("\n(no Blender found - set BLENDER_PATH to run the rest)")
else:
    print("\n== headless runs (%s) ==" % BLENDER)
    r = bc.execute("import bpy\nbpy.ops.mesh.primitive_monkey_add()\n"
                   "bpy.context.active_object.name = 'Suzanne Test'\nprint('made it')\n",
                   mode="headless")
    check("a working script succeeds", r.get("ok"), True)
    check("the default cube was cleared first",
          [o["name"] for o in r.get("objects_created", [])], ["Suzanne Test"])
    check("its print() comes back", (r.get("stdout") or "").strip(), "made it")
    check("the scene holds only what it built", r.get("scene", {}).get("object_names"), ["Suzanne Test"])

    r = bc.execute("import bpy\nbpy.ops.mesh.primitive_cube_add()\n"
                   "bpy.context.active_object.name = 'Half'\nraise ValueError('boom')\n",
                   mode="headless", clear_scene=False)
    check("a failing script fails", r.get("ok"), False)
    check("with the exception", r.get("error"), "ValueError: boom")
    check("on the script's own line", r.get("line"), 4)
    check("and its half-built object rolled back", "Half" in (r.get("rolled_back") or []), True)
    check("leaving the factory scene as it was", sorted(r["scene"]["object_names"]),
          ["Camera", "Cube", "Light"])

    r = bc.execute("import sys\nsys.exit(0)\n", mode="headless")
    check("sys.exit() is caught, not obeyed", "sys.exit" in (r.get("error") or ""), True)

    print("\n== parts that float apart ==")
    floating_chair = ("import bpy\n"
                      "bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.5))\n"
                      "bpy.context.active_object.name = 'Seat'\n"
                      "bpy.ops.mesh.primitive_cube_add(size=0.2, location=(3, 0, 0.1))\n"
                      "bpy.context.active_object.name = 'Stray Leg'\n")
    r = bc.execute(floating_chair, mode="headless", structure=True)
    check("a part standing apart fails the structure check", (r.get("ok"), r.get("kind")), (False, "structure"))
    check("naming the part", "Stray Leg" in (r.get("error") or ""), True)
    check("and is rolled back like any failure", "Stray Leg" in (r.get("rolled_back") or []), True)
    r = bc.execute(floating_chair, mode="headless")
    check("without the check it runs, and the report still says so",
          (r.get("ok"), (r.get("structure") or {}).get("floating")), (True, ["Stray Leg"]))
    joined = ("import bpy\n"
              "bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.5))\n"
              "bpy.ops.mesh.primitive_cylinder_add(radius=0.1, depth=1, location=(0, 0, 1.4))\n"
              "bpy.ops.mesh.primitive_cube_add(size=0.5, location=(5, 5, 5))\n"
              "bpy.context.active_object.display_type = 'WIRE'\n")
    r = bc.execute(joined, mode="headless", structure=True)
    check("touching parts pass, and a wireframe cutter far away is ignored", r.get("ok"), True)
    r = bc.execute("import bpy\nbpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 3))\n",
                   mode="headless", structure=True)
    check("a model hovering above the ground fails", "hovers" in (r.get("error") or ""), True)

    print("\n== the pop-up and panel, driven inside Blender ==")
    # Registers the add-on in a background Blender and presses its
    # buttons with a scripted model in place of a real one, pumping the
    # main-thread queue by hand as the timer would in a window.
    ui_result = WORK / "ui.json"
    ui_script = WORK / "ui.py"
    ui_script.write_text('''
import json, sys, time
sys.path.insert(0, %r)
import bpy
import blender_listener as L

out = {}
L.register()
jobs = L.runner()

def wait():
    deadline = time.time() + 30
    while L._UI["running"] and time.time() < deadline:
        jobs.pump_fn()
        time.sleep(0.02)
    jobs.pump_fn()

SLOW = [0.0]

def fake_make_llm(choice, keys, model=None, log=None):
    def call(messages):
        if "numbered" in messages[0]["content"]:          # the Ask prompt
            time.sleep(SLOW[0])
            return "## Steps\\n1. Select it.\\n2. Press **Ctrl+B** to bevel."
        return ("```python\\nimport bpy\\nbpy.ops.mesh.primitive_cone_add()\\n"
                "bpy.context.active_object.name = 'AI Cone'\\nprint('made a cone')\\n```")
    call.label = call.model = "fake-model"
    return call

out["popup operator"] = hasattr(bpy.ops.randomgenerals, "ai_popup")
out["shortcut"] = [(k.idname, k.type, k.ctrl, k.shift) for km in
                   bpy.context.window_manager.keyconfigs.addon.keymaps for k in km.keymap_items
                   if k.idname.startswith("randomgenerals")]
items = L._model_items(None, bpy.context)
out["models"] = [i[0] for i in items]
used = {"AUTO", "WORLD", "UNLINKED", "URL", "DESKTOP", "MODIFIER", "QUESTION", "LOCKED",
        "CHECKMARK", "INFO", "X", "LINKED", "FILE_REFRESH", "SORTTIME", "CANCEL", "PLAY",
        "TEXT", "ERROR", "NONE", "PAUSE", "OUTLINER_OB_LIGHT", "EXPERIMENTAL", "SOLO_ON",
        "BOOKMARKS", "EVENT_A"} | {i[3] for i in items}
valid = set(bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys())
out["bad icons"] = sorted(used - valid)

real_make_llm = L.make_llm
L.make_llm = fake_make_llm
p = bpy.context.window_manager.randomgenerals_ai
p.mode, p.prompt = "BUILD", "a cone"
bpy.ops.randomgenerals.ai_popup("EXEC_DEFAULT")
wait()
built = (L._UI["out"] or {}).get("result", {}).get("objects_created") or []
out["build"] = [L._UI["running"], (L._UI["out"] or {}).get("ok"), [o["name"] for o in built]]
out["build in scene"] = "AI Cone" in bpy.data.objects
out["build summary"] = L._result_summary(90, 10)[2][:1]

p.mode, p.prompt = "ASK", "how do I bevel?"
bpy.ops.randomgenerals.ai_run()
wait()
out["answer"] = L._UI["answer"]

# Cancel while the model is still thinking: its late reply is dropped.
SLOW[0] = 1.5
bpy.ops.randomgenerals.ai_run()
bpy.ops.randomgenerals.ai_cancel()
time.sleep(2.0)
jobs.pump_fn()
out["cancel"] = [L._UI["running"], L._UI["answer"], L._UI["log"][-1:]]
SLOW[0] = 0.0

# Generate, with the two network steps stood in for by a model Blender
# exports itself - what is on test is the button, the worker and the
# import, not Hugging Face.
import os, shutil, tempfile
from pathlib import Path
work = tempfile.mkdtemp()
L.GENERATED_DIR = Path(work) / "generated"
bpy.ops.mesh.primitive_monkey_add(location=(9, 9, 9), scale=(4, 4, 4))
src_glb = os.path.join(work, "src.glb")
bpy.ops.export_scene.gltf(filepath=src_glb, use_selection=True)
bpy.data.objects.remove(bpy.context.active_object)
def fake_reference(prompt, dest, **kw):
    Path(dest).write_bytes(b"picture")
    return dest
def fake_trellis(image, dest, token=None, log=None, seed=0):
    shutil.copy(src_glb, dest)
    return dest
L.reference_image, L.trellis = fake_reference, fake_trellis
L.estimate_height = lambda prompt, llm: 0.8
p.mode, p.prompt, p.model = "MESH", "a wooden rocking chair", "auto"
bpy.ops.randomgenerals.ai_run()
wait()
made = bpy.data.objects.get("A wooden rocking chair")
mesh = bpy.data.objects.get("A wooden rocking chair mesh")
if mesh:
    from mathutils import Vector
    bpy.context.view_layer.update()
    pts = [mesh.matrix_world @ Vector(c) for c in mesh.bound_box]
    out["generate"] = [L._UI["error"], made is not None, round(min(q.z for q in pts), 3),
                       round(max(q.z for q in pts), 3), (L._UI["out"] or {}).get("generated")]
else:
    out["generate"] = [L._UI["error"], made is not None]
out["generate summary"] = L._result_summary(90, 10)[2][:1]
p.mode = "BUILD"

L.make_llm = real_make_llm
p.model = "custom"                      # nothing set up for it
bpy.ops.randomgenerals.ai_run()
wait()
out["no server"] = L._UI["error"]

p.prompt = "   "
out["empty prompt"] = [sorted(bpy.ops.randomgenerals.ai_run()), L._UI["running"]]

# Opening a file used to lose the running bridge: its record was in
# driver_namespace, which every file load empties.
L.start(port=%d)
bpy.ops.wm.read_homefile(use_factory_startup=True)
out["after file load"] = [L.running() is not None, L._UI_KEY in L._SESSION]
out["restart after load"] = L.start(port=%d).port
L.unregister()
out["after unregister"] = [hasattr(bpy.types, "RGAI_PT_main"),
                           hasattr(bpy.types.WindowManager, "randomgenerals_ai")]
open(%r, "w").write(json.dumps(out))
''' % (str(bc.LISTENER.parent), TEST_PORT + 1, TEST_PORT + 1, str(ui_result)), encoding="utf-8")
    subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(ui_script)],
                   capture_output=True, timeout=180,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ui = json.loads(ui_result.read_text(encoding="utf-8")) if ui_result.exists() else {}
    check("the pop-up operator exists", ui.get("popup operator"), True)
    check("on Ctrl+Shift+A", ui.get("shortcut"), [["randomgenerals.ai_popup", "A", True, True]])
    check("the model menu starts with Auto, then pip 1.0",
          (ui.get("models") or [])[:2], ["auto", "pip"])
    check("every icon the UI uses exists in this Blender", ui.get("bad icons"), [])
    check("Go in the pop-up builds, and finishes", ui.get("build"), [False, True, ["AI Cone"]])
    check("the model's object is in the scene", ui.get("build in scene"), True)
    check("the result says what was built",
          (ui.get("build summary") or [""])[0].startswith("Built 1 object with fake-model"), True)
    check("Ask comes back as text, Markdown removed",
          ui.get("answer"), "Steps\n1. Select it.\n2. Press Ctrl+B to bevel.")
    check("Cancel stops it, and the late reply is dropped",
          ui.get("cancel"), [False, "", ["Cancelled."]])
    check("a model that is not set up says how to set it up",
          "add-on preferences" in (ui.get("no server") or ""), True)
    check("an empty request is refused", ui.get("empty prompt"), [["CANCELLED"], False])
    check("turning it off removes the panel and its settings", ui.get("after unregister"), [False, False])
    check("Generate imports the model at its real height, standing on the ground",
          ui.get("generate"), ["", True, 0.0, 0.8, True])
    check("and says where it came from",
          (ui.get("generate summary") or [""])[0].startswith("Generated by TRELLIS"), True)
    check("opening a .blend keeps track of the bridge and the UI", ui.get("after file load"), [True, True])
    check("so it restarts on its own port instead of finding it taken",
          ui.get("restart after load"), TEST_PORT + 1)

    print("\n== the live protocol over a socket ==")
    # Serves, and pumps the job queue on the main thread by hand until
    # the stop file appears - what the timer does in a real window.
    stop_file = WORK / "stop"
    server_script = WORK / "serve.py"
    server_script.write_text(
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import blender_listener as L\n"
        "L.start(port=%d)\n"
        "r = L.runner()\n"
        "deadline = time.time() + 120\n"
        "while time.time() < deadline and not __import__('os').path.exists(%r):\n"
        "    r.pump_fn(); time.sleep(0.02)\n"
        "L.stop()\n" % (str(bc.LISTENER.parent), TEST_PORT, str(stop_file)), encoding="utf-8")
    proc = subprocess.Popen([BLENDER, "-b", "--factory-startup", "--python", str(server_script)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(300):
            if bc.ping(timeout=0.5):
                break
            time.sleep(0.2)
        info = bc.ping()
        check("ping answers without a token", bool(info and info["ok"]), True)
        check("and reports the main thread as responsive", bool(info and info["responsive"]), True)
        check("the bridge file carries a token", bool(bc.bridge_address()[2]), True)

        def raw(method, path, body=None, headers=None):
            h = {"Content-Type": "application/json"}
            h.update(headers or {})
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (TEST_PORT, path),
                                         data=body, method=method, headers=h)
            try:
                with bc._LOCAL.open(req, timeout=10) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code

        token = bc.bridge_address()[2]
        code = json.dumps({"code": "import bpy"}).encode()
        check("/status without the token is refused", raw("GET", "/status"), 401)
        check("/exec without the token is refused", raw("POST", "/exec", code), 401)
        check("/exec from a web page (Origin set) is refused",
              raw("POST", "/exec", code, {"X-Bridge-Token": token, "Origin": "https://evil.example"}), 403)
        check("/exec under a rebound hostname is refused",
              raw("POST", "/exec", code, {"X-Bridge-Token": token, "Host": "evil.example:%d" % TEST_PORT}), 403)
        check("/exec as text/plain is refused",
              raw("POST", "/exec", code, {"X-Bridge-Token": token, "Content-Type": "text/plain"}), 415)

        st = bc.status()
        check("/status with the token reads the scene", st.get("objects"), 3)

        r = bc.execute("import bpy\nbpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1)\n"
                       "bpy.context.active_object.name = 'Live Ico'\n", mode="live")
        check("a live run succeeds", r.get("ok"), True)
        check("in live mode", r.get("mode"), "live")
        check("and keeps the user's scene", sorted(r["scene"]["object_names"]),
              ["Camera", "Cube", "Light", "Live Ico"])
        r = bc.execute("import bpy\nbpy.data.objects['Nope'].location.x = 1\n", mode="live")
        check("a live failure reports its line", (r.get("ok"), r.get("line")), (False, 2))
        r = bc.execute("raise SystemExit\n", mode="live")
        check("SystemExit in a live run is caught", r.get("ok"), False)
        check("and Blender is still there after it", bool(bc.ping()), True)

        # SO_EXCLUSIVEADDRUSE on the bridge's socket: even a socket that
        # asks for SO_REUSEADDR must not be able to share the port.
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", TEST_PORT))
            shared = True
        except OSError:
            shared = False
        finally:
            s.close()
        check("a second listener cannot share the port", shared, False)
    finally:
        stop_file.write_text("stop")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    check("stopping removes the bridge file", Path(os.environ["RG_BLENDER_BRIDGE_FILE"]).exists(), False)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
import shutil                                                # noqa: E402
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
