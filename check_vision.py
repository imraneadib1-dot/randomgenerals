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
a.openrouter_api.models = lambda: ["google/gemma-4-31b-it:free"]
a._vision_speed["seconds"] = 98.0
check("slow local box still gets hosted vision",
      a._vision_route(), ("openrouter", "google/gemma-4-31b-it:free"))

print("\n== the model is told the truth when it cannot see ==")
files = [{"kind": "image", "filename": "screenshot.png", "text": ""}]
block = a._attachment_context_block(files, vision_available=False)
check("says the model cannot see it", "can't see images" in block, True)
check("and forbids guessing", "don't guess" in block, True)
block = a._attachment_context_block(files, vision_available=True)
check("says nothing when it CAN see", block.strip(), "")

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
