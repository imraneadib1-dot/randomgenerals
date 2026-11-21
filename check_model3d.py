# -*- coding: utf-8 -*-
"""Does 3D generation (model3d.py) behave, end to end, without a GPU?

    python check_model3d.py

The real providers (the Hugging Face Space, fal.ai) are replaced by a
fake that returns a small, valid GLB, so this runs anywhere in seconds
and costs nothing. What it covers:

  - pictures are judged by their bytes (a renamed text file is refused),
    by size and by dimensions
  - a GLB is checked before it is kept (container, version, length, mesh)
  - jobs go queued -> running -> done, the file is served only to its
    owner, and a failed job gives the day's count back
  - a busy provider hands the job to the next one in TRELLIS_PROVIDERS
  - the daily limit holds, and signed-out visitors are asked to sign in
  - "make this into a 3D model" in the chat starts a job from the
    attached picture, and "a 3D model of a teapot" from a description
  - a question ABOUT 3D does not start anything

A real run against the Space is a separate, manual step (it spends the
shared GPU quota): see the README's 3D section.
"""
import io
import json
import os
import struct
import sys
import tempfile
import time

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["TRELLIS_PROVIDERS"] = "fake_busy,fake"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["PIP_ENGINE_URL"] = ""
sys.path.insert(0, os.path.abspath("."))

import model3d  # noqa: E402

FAILED = []


def check(label, got, want=True):
    ok = got == want
    print("  %-62s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


def tiny_glb():
    """One triangle, as a valid glTF 2.0 binary."""
    pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    doc = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}], "scene": 0, "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "buffers": [{"byteLength": len(pos)}],
        "bufferViews": [{"buffer": 0, "byteLength": len(pos)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                       "min": [0, 0, 0], "max": [1, 1, 0]}],
        "extras": {"pad": "x" * 1200},          # over check_glb's 1 KB floor
    }
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(pos)
    return (b"glTF" + struct.pack("<II", 2, total)
            + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(pos), 0x004E4942) + pos)


def png(w=128, h=128):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 60, 60)).save(buf, "PNG")
    return buf.getvalue()


# ---- fake providers ------------------------------------------------------------
calls = []


class Fake:
    name = "fake"

    def configured(self):
        return True

    def run(self, image_path, resolution, seed, deadline, stage):
        calls.append((self.name, image_path, resolution))
        stage("Shaping the model (%s)" % resolution)
        out = tempfile.mktemp(suffix=".glb")
        with open(out, "wb") as fh:
            fh.write(tiny_glb())
        return out


class FakeBusy(Fake):
    name = "fake_busy"

    def run(self, *a, **k):
        calls.append((self.name,))
        raise model3d.Busy("The free 3D GPU has used up its quota for now.")


model3d.PROVIDERS["fake"] = Fake()
model3d.PROVIDERS["fake_busy"] = FakeBusy()


def wait(job_id, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        j = model3d.get(job_id)
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.05)
    return model3d.get(job_id)


print("== pictures ==")
check("a PNG is accepted", model3d.check_image(png())[0], "png")
for label, data, word in [
        ("a text file named .png is refused", b"hello, not an image" * 10, "PNG, JPEG or WebP"),
        ("an empty file is refused", b"", "empty"),
        ("a 20 px picture is refused", png(20, 20), "too small"),
        ("an 11 MB file is refused", b"\x89PNG\r\n\x1a\n" + b"0" * (11 * 1024 * 1024), "under 10 MB")]:
    try:
        model3d.check_image(data)
        check(label, "accepted", "refused")
    except model3d.Model3DError as e:
        check(label, word in str(e))

print("== GLB checks ==")
good = tempfile.mktemp(suffix=".glb")
open(good, "wb").write(tiny_glb())
check("a valid GLB passes", model3d.check_glb(good)["meshes"], 1)
bad = tempfile.mktemp(suffix=".glb")
open(bad, "wb").write(tiny_glb()[:-20])
try:
    model3d.check_glb(bad)
    check("a truncated GLB is refused", "accepted", "refused")
except model3d.Model3DError as e:
    check("a truncated GLB is refused", "cut short" in str(e))

print("== subjects ==")
check("'make me a 3d model of a red teapot please'",
      model3d.subject_of("make me a 3d model of a red teapot please"), "a red teapot")
check("'I need a GLB of a wooden chair'", model3d.subject_of("I need a GLB of a wooden chair"),
      "a wooden chair")

# ---- the app ---------------------------------------------------------------------
# Text -> picture is imagegen's job; stubbed so nothing leaves the machine.
import imagegen  # noqa: E402

imagegen.generate_image = lambda prompt, **k: (imagegen._save_bytes(png()), None)

import app as A  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

A.LIMIT_3D.rate, A.LIMIT_3D.burst = 10**6, 10**6
A.LIMIT_CHAT.rate, A.LIMIT_CHAT.burst = 10**6, 10**6


def account(email, plan="free"):
    uid = A._create_user(email, password_hash=generate_password_hash("pw"))
    A._apply_plan(A.USERS[uid], plan)
    c = A.app.test_client()
    c.post("/api/auth/login", json={"email": email, "password": "pw"})
    return uid, c


print("== the studio API ==")
uid, c = account("maker@check.example")
st = c.get("/api/3d/status").get_json()
check("status: available, 5 a day on Free", (st["available"], st["quota"]["limit"]), (True, 5))
check("status: the engine is called relo 1.0", st.get("engine"), "relo 1.0")
check("status: no provider names leak", "hf_space" in json.dumps(st) or "fake" in json.dumps(st), False)

r = c.post("/api/3d/generate", data={"image": (io.BytesIO(b"not an image at all" * 20), "x.png"),
                                     "resolution": "512"}, content_type="multipart/form-data")
check("a fake picture is refused with 400", r.status_code, 400)
r = c.post("/api/3d/generate", data={"image": (io.BytesIO(png()), "x.png"), "resolution": "999"},
           content_type="multipart/form-data")
check("an unknown resolution is refused", r.status_code, 400)

calls.clear()
r = c.post("/api/3d/generate", data={"image": (io.BytesIO(png()), "cup.png"), "resolution": "1024"},
           content_type="multipart/form-data")
check("a picture starts a job", r.status_code, 200)
job = wait(r.get_json()["job"]["id"])
check("the job finishes", job["status"], "done")
check("the busy provider was skipped for the next", [x[0] for x in calls], ["fake_busy", "fake"])
check("the resolution reached the provider", calls[-1][2], "1024")
view = c.get("/api/3d/job/%s" % job["id"]).get_json()["job"]
check("the job reports a GLB url", bool(view["glb_url"]))
g = c.get(view["glb_url"])
check("the owner gets the GLB", (g.status_code, g.data[:4], g.mimetype),
      (200, b"glTF", "model/gltf-binary"))
d = c.get(view["download_url"])
check("download is an attachment", "attachment" in d.headers.get("Content-Disposition", ""))
_, other = account("someone@check.example")
check("someone else gets 404", other.get(view["glb_url"]).status_code, 404)
check("the history lists it", c.get("/api/3d/jobs").get_json()["jobs"][0]["id"], job["id"])
check("the job does not say which engine ran", "provider" in view or "fake" in json.dumps(view), False)
gl = g.data
gen = json.loads(gl[20:20 + struct.unpack("<I", gl[12:16])[0]])
check("the file says relo 1.0 made it", gen["asset"]["generator"], model3d.GENERATOR)
check("the mesh is named relo_model", gen["meshes"][0]["name"], "relo_model")
check("the download is named relo-<id>.glb",
      ("relo-%s.glb" % job["id"]) in d.headers.get("Content-Disposition", ""))

print("== for Blender ==")
if model3d.blend_available():
    b = c.get(view["blend_url"]) if view.get("blend_url") else None
    check("a .blend is made on request", (b.status_code, b.mimetype) if b else None,
          (200, "application/x-blender"))
    check("it is a Blender file", bool(b) and (b.data[:7] == b"BLENDER" or b.data[:4] == b"\x28\xb5\x2f\xfd"))
    check("someone else cannot have it", other.get(view["blend_url"]).status_code, 404)
else:
    check("no Blender here: no Blender button offered", view.get("blend_url"), "")
    print("  (set BLENDER_BIN to a Blender to test the .blend itself)")

r = c.post("/api/3d/generate", json={"prompt": "a red ceramic teapot", "resolution": "512"})
# Text first becomes a picture; imagegen is stubbed so nothing leaves the machine.
check("a description is accepted", r.status_code, 200)
wait(r.get_json()["job"]["id"])

print("== limits and refunds ==")
for _ in range(3):
    wait(c.post("/api/3d/generate", json={"prompt": "a lamp"}).get_json()["job"]["id"])
st = c.get("/api/3d/status").get_json()
check("five used, none left", (st["quota"]["used"], st["quota"]["left"]), (5, 0))
r = c.post("/api/3d/generate", json={"prompt": "a chair"})
check("the sixth today is refused with 402", r.status_code, 402)

print("== when the free GPU is used up ==")
_, rr = account("resting@check.example")


class FakeOut(Fake):
    name = "fake"

    def run(self, *a, **k):
        calls.append(("out",))
        raise model3d.Exhausted("relo 1.0 has used today's free GPU time.", 1200)


model3d.PROVIDERS["fake"] = FakeOut()
os.environ["TRELLIS_PROVIDERS"] = "fake"          # one GPU, so "all resting" can happen
r = rr.post("/api/3d/generate", json={"prompt": "a vase"})
j = wait(r.get_json()["job"]["id"])
check("the job fails with a 'back in about' message", "back in about" in j["error"], True)
check("and is not counted", rr.get("/api/3d/status").get_json()["quota"]["used"], 0)
st = rr.get("/api/3d/status").get_json()
check("status says it is resting, with a message", bool(st["resting"]) and "resting" in st["resting_message"])
calls.clear()
r = rr.post("/api/3d/generate", json={"prompt": "a vase"})
check("the next request is refused at once (503)", r.status_code, 503)
check("without asking the GPU again", calls, [])
check("still nothing counted", rr.get("/api/3d/status").get_json()["quota"]["used"], 0)
model3d._resting.clear()
model3d.PROVIDERS["fake"] = Fake()
os.environ["TRELLIS_PROVIDERS"] = "fake_busy,fake"

_, f = account("failing@check.example")
model3d.PROVIDERS["fake"].run = lambda *a, **k: (_ for _ in ()).throw(model3d.Model3DError("boom"))
r = f.post("/api/3d/generate", data={"image": (io.BytesIO(png()), "x.png")},
           content_type="multipart/form-data")
j = wait(r.get_json()["job"]["id"])
check("a failed job reports why", j["status"] == "failed" and "boom" in j["error"])
check("and gives the day's count back", f.get("/api/3d/status").get_json()["quota"]["used"], 0)
model3d.PROVIDERS["fake"] = Fake()

guest = A.app.test_client()
r = guest.post("/api/3d/generate", json={"prompt": "a chair"})
check("signed out: asked to make an account (401)", r.status_code, 401)

print("== in the chat ==")
check("'make this into a 3d model' is a request", A._wants_3d("make this into a 3d model"))
check("'turn my drawing into 3D' is a request", A._wants_3d("turn my drawing into 3D"))
check("'what is a 3d model?' is not", A._wants_3d("what is a 3d model?"), False)
check("'how do I make a mesh in blender' is not", A._wants_3d("how do I make a mesh in blender"), False)

uid2, chat = account("chatter@check.example")
A.ollama_reachable = lambda: True
A.ollama_provider = lambda plan=None: {"id": "ollama", "label": "pip 1.0", "available": True,
                                       "models": ["fake-model"], "model_info": [], "note": ""}
A._failover_chain = lambda provider, mode: []
seen = {}


def fake_stream(model, history, **kw):
    seen["system"] = history[0]["content"] if history else ""
    yield "Making it now."


# Every channel: a picture is routed to whichever vision model is set up.
for _name in list(A.PROVIDER_STREAMERS):
    A.PROVIDER_STREAMERS[_name] = fake_stream
up = chat.post("/api/upload", data={"files": (io.BytesIO(png()), "mug.png")},
               content_type="multipart/form-data").get_json()["attachments"]
tid = chat.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
calls.clear()
body = chat.post("/api/chat", json={"thread_id": tid, "provider": "ollama", "model": "fake-model",
                                    "message": "make this into a 3d model", "attachments": up}
                 ).get_data(as_text=True)
check("the reply carries a 3D card", '"kind": "model3d"' in body or '"kind":"model3d"' in body)
check("the model is told relo 1.0 is making it", "relo 1.0" in seen.get("system", ""))
check("and the engine's real name is not in the prompt", "TRELLIS" in seen.get("system", ""), False)
msgs = A.THREADS[tid]["messages"]
saved = [d for d in (msgs[-1].get("tool_displays") or []) if d.get("kind") == "model3d"]
check("the card is saved with the reply (survives a reload)", len(saved), 1)
j = wait(saved[0]["job_id"]) if saved else {}
check("the job made from the attachment finishes", j.get("status"), "done")
check("from the attached picture, not a drawing", j.get("source"), "image")

tid2 = chat.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
body = chat.post("/api/chat", json={"thread_id": tid2, "provider": "ollama", "model": "fake-model",
                                    "message": "what is a 3d model?"}).get_data(as_text=True)
check("a question about 3D starts nothing", "model3d" in body, False)

print()
if FAILED:
    print("%d check(s) FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All checks passed.")
