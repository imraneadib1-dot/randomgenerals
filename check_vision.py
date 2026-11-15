# -*- coding: utf-8 -*-
"""Does the app know whether it can actually see an image?

    python check_vision.py

Having a vision model installed is not the same as being able to use
one. On the deployment VM, gemma3:4b took 98 seconds to name the colour
of a 200x200 solid square - 91 of them spent looking at the picture
before writing a token. A real screenshot exceeded the 120-second HTTP
timeout, so attaching one produced a two minute wait and a ReadTimeout.

Nothing in the routing knew that. _vision_route() saw a vision-capable
model in the Ollama catalogue and returned it, every time.

These checks cover the guard that fixes it, and the property that
matters most: when local vision is too slow, the route must come back
EMPTY rather than slow, because an instant "I can't see images here" is
a better answer than a two-minute hang.
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SECRET_KEY"] = "test-only"
sys.path.insert(0, os.path.abspath("."))

import app as a                                             # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-58s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


# Pretend Ollama is up with a vision model, so the guard is the only
# thing that can change the answer.
a.ollama_reachable = lambda: True
a.ollama_provider = lambda: {"models": ["gemma3:4b", "gemma3:1b"]}
a.openrouter_api.configured = lambda: False
# Off until its own section below. With a real key in the environment
# pip 1.0 is the first vision route, and a running engine would make
# every local-guard check here pass for the wrong reason.
a.pip_api.configured = lambda: False

print("== the speed guard ==")
a._vision_speed["seconds"] = None
check("unmeasured is NOT treated as usable", a._local_vision_is_fast(), False)
check("and so no route is offered", a._vision_route(), (None, None))

a._vision_speed["seconds"] = float("inf")
check("a timed-out probe is not usable", a._local_vision_is_fast(), False)
check("still no route", a._vision_route(), (None, None))

# The real measurement from the VM.
a._vision_speed["seconds"] = 98.0
check("98s (the measured VM figure) is refused",
      a._local_vision_is_fast(), False)
check("the VM offers no local vision route", a._vision_route(), (None, None))

a._vision_speed["seconds"] = 3.2
check("a fast box IS usable", a._local_vision_is_fast(), True)
check("and gets the local model", a._vision_route(), ("ollama", "gemma3:4b"))

a._vision_speed["seconds"] = a.LOCAL_VISION_BUDGET_SECONDS
check("exactly at the budget is allowed", a._local_vision_is_fast(), True)
a._vision_speed["seconds"] = a.LOCAL_VISION_BUDGET_SECONDS + 0.1
check("just over it is not", a._local_vision_is_fast(), False)

print("\n== hosted vision outranks local, and ignores the guard ==")
# A hosted model runs on somebody else's GPU, so this machine being slow
# says nothing about it.
a.openrouter_api.configured = lambda: True
a.openrouter_api.budget_ok = lambda *args, **kw: True
# The picker list holds no vision model - PREFERRED never did - and the
# route used to look there, so it could never match. It reads the
# catalogue's own "can see" list now, which is what this stubs.
a.openrouter_api.models = lambda: ["poolside/laguna-s-2.1:free"]
a.openrouter_api.vision_models = lambda: ["google/gemma-4-31b-it:free"]
a._vision_speed["seconds"] = 98.0
check("slow local box still gets hosted vision",
      a._vision_route(), ("openrouter", "google/gemma-4-31b-it:free"))
check("a catalogue vision model is recognised as one, so its images "
      "are encoded", a.is_vision_model("google/gemma-4-31b-it:free"), True)

print("\n== the hosted channel actually sends the picture ==")
import openrouter_api as orapi                             # noqa: E402
msgs = orapi._with_images(
    [{"role": "system", "content": "s"}, {"role": "user", "content": "what is this?"}],
    ["QUJD"])
check("the last user turn becomes text + image parts",
      [p["type"] for p in msgs[-1]["content"]], ["text", "image_url"])
check("as a data URL", msgs[-1]["content"][1]["image_url"]["url"]
      .startswith("data:image/png;base64,QUJD"), True)
check("the system turn is untouched", msgs[0], {"role": "system", "content": "s"})
check("no images, no change",
      orapi._with_images([{"role": "user", "content": "hi"}], []),
      [{"role": "user", "content": "hi"}])

print("\n== the model is told the truth when it cannot see ==")
files = [{"kind": "image", "filename": "screenshot.png", "text": ""}]
block = a._attachment_context_block(files, vision_available=False)
check("says the model cannot see it", "can't see images" in block, True)
check("and forbids guessing", "don't guess" in block, True)
block = a._attachment_context_block(files, vision_available=True)
check("says nothing when it CAN see", block.strip(), "")

print("\n== a pip 1.0 build that can see is the first route ==")
import time                                                # noqa: E402
import pip_api                                            # noqa: E402
import attachments                                         # noqa: E402
a.pip_api.configured = lambda: True
pip_api.configured = a.pip_api.configured


def engine_reports(vision):
    """What /v1/models said, without an engine: one model, and whether
    this build of it reads images."""
    pip_api._models_cache.update({
        "at": time.time(), "error": "", "models": ["pip-1.0"],
        "info": {"pip-1.0": {"id": "pip-1.0", "vision": vision}}})


a.pip_api.has_room = lambda: True
engine_reports(vision=False)
check("a text-only build offers no vision model", pip_api.vision_models(), [])
check("so the route goes to the next channel that can see",
      a._vision_route(), ("openrouter", "google/gemma-4-31b-it:free"))
check("and pip 1.0 is not treated as able to see", a.is_vision_model("pip-1.0"), False)
engine_reports(vision=True)
check("a build that can see says so", pip_api.vision_models(), ["pip-1.0"])
check("pip 1.0 outranks OpenRouter's free tier", a._vision_route(), ("pip", "pip-1.0"))
check("and it is recognised as a model that can see", a.is_vision_model("pip-1.0"), True)
a.pip_api.has_room = lambda: False
check("while the engine is busy, the route moves on", a._vision_route(), ("openrouter", "google/gemma-4-31b-it:free"))
a.pip_api.has_room = lambda: True

print("\n== the picture rides in the request, as what it is ==")
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
JPG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
check("a PNG is called a PNG", attachments.data_url(PNG).startswith("data:image/png;base64,"), True)
check("a JPEG is called a JPEG", attachments.data_url(JPG).startswith("data:image/jpeg;base64,"), True)
history = [{"role": "system", "content": "s"}, {"role": "user", "content": "what is this?"}]
body = pip_api._request_body("pip-1.0", history, {"num_predict": 300}, [PNG])
check("the model asked for is the model sent", body["model"], "pip-1.0")
check("the last user turn is text + image", [q["type"] for q in body["messages"][-1]["content"]], ["text", "image_url"])
check("the text is intact", body["messages"][-1]["content"][0]["text"], "what is this?")
check("the image is a typed data URL", body["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR"), True)
engine_reports(vision=False)
body = pip_api._request_body("pip-1.0", history, {}, [PNG])
check("a text-only build gets text only", body["messages"][-1]["content"], "what is this?")
engine_reports(vision=True)

print("\n== over the window, pictures go before the question does ==")
big = [{"role": "system", "content": "x" * 4 * 2600},           # ~2,600 tokens
       {"role": "user", "content": [{"type": "text", "text": "which?"}]
        + [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * 3}]
window = pip_api.CONTEXT_TOKENS
pip_api.CONTEXT_TOKENS = 8000        # small enough that three pictures do not fit
try:
    kept, reply = pip_api.fit_to_context(big, 1200)
finally:
    pip_api.CONTEXT_TOKENS = window
left = [q for q in kept[-1]["content"] if q["type"] == "image_url"]
check("it fits", pip_api.estimate_tokens(kept) + reply <= 8000 - pip_api.CONTEXT_MARGIN, True)
check("by shedding images, never the last one", 1 <= len(left) < 3, True)
check("and saying so", "did not fit" in kept[-1]["content"][0]["text"], True)
check("the question survives", kept[-1]["content"][0]["text"].startswith("which?"), True)

print("\n== end to end: an attached image reaches a model that can see it ==")
# A real request through /api/chat, with the providers faked: the turn
# function records whether the tool loop ran, the streamer records what
# it was handed. Before this, every image on a plan with tools went
# into the loop's text-only first turn and the answer came from a model
# that had not been shown it.
import os                                                  # noqa: E402
import base64                                              # noqa: E402
turns = []
streams = []


def fake_turn(model, history, tools=None, options=None, timeout=120):
    turns.append(model)
    return {"role": "assistant", "content": "I cannot see images."}, None


def fake_stream(model, history, options=None, images=None, usage=None):
    streams.append({"model": model, "images": list(images or []),
                    "system": history[0]["content"]})
    if usage is not None:
        usage["eval_count"] = 5
    yield "A red square."


a.PROVIDER_TURNS["pip"] = fake_turn
a.PROVIDER_STREAMERS["pip"] = fake_stream
a._failover_chain = lambda provider, mode: []
a.pip_api.models = lambda: ["pip-1.0"]
os.makedirs(attachments.UPLOAD_DIR, exist_ok=True)
png_path = os.path.join(attachments.UPLOAD_DIR, "vision-check.png")
with open(png_path, "wb") as fh:
    fh.write(base64.b64decode(PNG))
attachment = [{"filename": "vision-check.png", "kind": "image",
               "url": "/static/uploads/vision-check.png"}]
try:
    for plan in ("pro", "free"):
        uid = a._create_user("%s-vision@check.example" % plan, password_hash="x")
        a._apply_plan(a.USERS[uid], plan)
        c = a.app.test_client()
        with c.session_transaction() as sess:
            sess["user_id"] = uid
        tid = c.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
        turns.clear()
        streams.clear()
        r = c.post("/api/chat", json={"thread_id": tid, "provider": "pip",
                                      "model": "pip-1.0",
                                      "message": "what colour is this?",
                                      "attachments": attachment})
        body_text = r.get_data(as_text=True)
        check("[%s] the reply streams" % plan, "A red square." in body_text, True)
        if plan == "pro":
            check("[pro] the tool loop is skipped for a picture", turns, [])
            check("[pro] the seeing model answers", streams and streams[0]["model"], "pip-1.0")
            check("[pro] and is handed the image", streams and len(streams[0]["images"]), 1)
            check("[pro] the model is not told it cannot see", "can't see images" in streams[0]["system"], False)
        else:
            check("[free] the chat model answers", streams and streams[0]["model"], "pip-1.0")
            check("[free] with no image", streams and streams[0]["images"], [])
            check("[free] and is told not to guess", "can't see images" in streams[0]["system"], True)
finally:
    try:
        os.remove(png_path)
    except OSError:
        pass

print("\n== the probe records something usable ==")
check("probe is wired into boot", callable(a._probe_vision_speed), True)
check("a vision-capable model is recognised",
      a.is_vision_model("gemma3:4b"), True)
check("the text-only sibling is not",
      a.is_vision_model("gemma3:1b"), False)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
sys.exit(1 if FAILED else 0)
