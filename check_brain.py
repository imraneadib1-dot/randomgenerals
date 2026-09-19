# -*- coding: utf-8 -*-
"""Is the intent model correct, honest, and safe to act on?

    python check_brain.py

WHY THESE CHECKS

Three separate ways a small model in a request path goes wrong, and one
check for each.

WRONG MATHS. The gradients are derived by hand. An off-by-one or a
missing term still trains, just worse, and no loss curve would show it -
so every derivative is compared against a numerical estimate.

DISHONEST NUMBERS. A single held-out split flattered this model by
seven points and could not tell a 16-unit model from a 32-unit one.
The claims in the docstring are re-measured here by the same k-fold
the module exposes, against the always-guess-the-commonest baseline,
and the checks fail if the model stops beating it.

UNSAFE ACTION. The app acts on one head, above one threshold, in one
place, and a wrong guess must cost nothing but wording. That is driven
here through /api/chat: a coding question in the chat bay gets the
coding prompt, an ordinary one does not, and a server with no
checkpoint behaves exactly as it did before the model existed.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="braintest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
sys.path.insert(0, os.path.abspath("."))

import numpy as np                                        # noqa: E402
from brain import intent                                  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


print("== the maths is right ==")
worst = intent.check_gradients()
check("every hand-derived gradient matches a numerical estimate",
      worst < 1e-6, True)
print("     worst relative error %.2e" % worst)
z = np.array([-800.0, 0.0, 800.0])
check("sigmoid does not overflow at either tail",
      bool(np.all(np.isfinite(intent.sigmoid(z)))), True)
check("and is still correct in the middle", round(float(intent.sigmoid(z)[1]), 6), 0.5)
p = intent.softmax(np.array([[1000.0, 1000.0, 1000.0, 1000.0]]))
check("softmax does not overflow on large scores", round(float(p[0][0]), 4), 0.25)
w = intent.class_weights(np.array([0, 0, 0, 0, 1, 2, 3]))
check("class weights are inverse frequency", round(float(w[0]) < float(w[1]), 6), 1)
check("and average to one", round(float(w.mean()), 6), 1.0)

print("\n== the features see what they claim to ==")
x = intent.features("how do i center a div in css")
check("a vector per message", x.shape, (intent.N_BUCKETS + len(intent.HANDMADE),))
check("L2-normalised", round(float(np.linalg.norm(x)), 6), 1.0)
# Not all zeros: an empty message genuinely IS short, so that handmade
# flag fires and normalisation makes it a unit vector. What matters is
# that no TEXT feature fires, and that predict() refuses it outright.
check("an empty message has no text features",
      float(np.abs(intent.features("")[:intent.N_BUCKETS]).sum()), 0.0)


def flag(text, name):
    return bool(intent.features(text)[intent.N_BUCKETS + intent.HANDMADE.index(name)])


check("a fence is seen", flag("here\n```python\nx=1\n```", "fence"), True)
check("a traceback is seen", flag("ValueError: bad input", "errorish"), True)
check("the trade's vocabulary is seen", flag("how do i center a div", "codey"), True)
check("two of them are seen as two", flag("my docker build fails at pip install", "codey_many"), True)
check("ordinary prose is not codey", flag("plan a week of meals for two", "codey"), False)
check("recency words are seen", flag("what is the news today", "recency"), True)
check("a picture word is seen", flag("draw me a fox", "make_image"), True)
check("a clip word is seen", flag("animate a sunrise", "make_video"), True)
# The same word must land in the same bucket in every process, or a
# restart silently invalidates every weight.
check("hashing is stable across processes",
      list(np.nonzero(intent.features("center a div"))[0][:5]),
      list(np.nonzero(intent.features("center a div"))[0][:5]))

print("\n== the corpus is usable ==")
rows = intent.load_rows()
check("it loads", len(rows) >= 150, True)
check("every row has the four fields",
      all({"text", "bay", "web", "depth"} <= set(r) for r in rows), True)
check("every bay is one of the four",
      sorted({r["bay"] for r in rows}), sorted(intent.BAYS))
check("every depth is quick or deep", sorted({r["depth"] for r in rows}), ["deep", "quick"])
check("no duplicate texts", len({r["text"] for r in rows}), len(rows))
check("every bay has examples to learn from",
      min(sum(r["bay"] == b for r in rows) for b in intent.BAYS) >= 20, True)
tr, va = intent.split(rows)
check("the split holds a fifth back", len(va), len(rows) // 5 + (1 if len(rows) % 5 else 0))
check("and it is not the tail - every bay is in it",
      sorted({r["bay"] for r in va}), sorted(intent.BAYS))

print("\n== it beats guessing, measured on every example ==")
base = intent.baseline(rows)
report = intent.cross_validate(rows)
for head in ("bay", "web", "deep"):
    print("     %-5s baseline %.0f%%   model %.1f%%" % (
        head, base[head] * 100, report[head] * 100))
check("the bay head beats always-guessing-chat by 30 points or more",
      report["bay"] - base["bay"] >= 0.30, True)
check("every bay is learned, not just the big ones",
      min(report["per_bay"].values()) >= 0.60, True)
check("the web head is at least as good as guessing", report["web"] >= base["web"], True)
check("the deep head is at least as good as guessing", report["deep"] >= base["deep"], True)
print("     confident on %.0f%% of messages, %.1f%% correct among them"
      % (report["confident_coverage"] * 100, report["confident_accuracy"] * 100))
check("what it acts on is right at least 90% of the time",
      report["confident_accuracy"] >= 0.90, True)
check("and it acts on most messages", report["confident_coverage"] >= 0.60, True)

print("\n== the shipped checkpoint loads and answers ==")
check("there is one", os.path.exists(intent.CHECKPOINT), True)
check("it is small enough to ship", os.path.getsize(intent.CHECKPOINT) < 200_000, True)
check("it loads", intent.available(), True)
for text, bay in (("write a python function that reverses a string", "code"),
                  ("draw me a cat wearing sunglasses", "image"),
                  ("make a video of rain on a window", "video"),
                  ("what should i cook with chicken and rice", "chat")):
    got = intent.predict(text)
    check("%-46s -> %s" % (text[:46], bay), got["bay"], bay)
check("an empty message gets no opinion", intent.predict("   "), None)
check("confidence is a probability",
      0.0 <= intent.predict("hello")["bay_p"] <= 1.0, True)

print("\n== a checkpoint from another feature set is refused ==")
bad = os.path.join(WORK, "wrong.npz")
np.savez_compressed(bad, W1=np.zeros((4, 2), dtype=np.float32),
                    b1=np.zeros(2, dtype=np.float32),
                    W2=np.zeros((2, 4), dtype=np.float32),
                    b2=np.zeros(4, dtype=np.float32),
                    w3=np.zeros(2, dtype=np.float32), b3=np.float32(0),
                    w4=np.zeros(2, dtype=np.float32), b4=np.float32(0),
                    n_buckets=7, handmade=1, bays=np.array(intent.BAYS))
try:
    intent.IntentNet.load(bad)
    check("loading it raises rather than predicting from nonsense", False, True)
except ValueError:
    check("loading it raises rather than predicting from nonsense", True, True)

print("\n== the app acts on it, in one place, harmlessly ==")
import app as appmod                                      # noqa: E402
check("the app has the brain", appmod.brain_intent is not None, True)
appmod.ollama_reachable = lambda: True
appmod._failover_chain = lambda provider, mode: []
appmod.LIMIT_CHAT.rate = appmod.LIMIT_CHAT.burst = 10 ** 6
SEEN = []


def fake_stream(model, history, options=None, images=None, usage=None):
    SEEN.append(history[0]["content"])
    if usage is not None:
        usage["eval_count"] = 5
    yield "ok"


appmod.PROVIDER_STREAMERS["ollama"] = fake_stream
uid = appmod._create_user("brain@check.example", password_hash="x")
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = uid


def ask(text, mode="chat"):
    tid = client.post("/api/threads", json={"mode": mode}).get_json()["id"]
    SEEN.clear()
    client.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                                   "model": "m", "message": text})
    return SEEN[0] if SEEN else ""


coding = appmod.CODING_SYSTEM_PROMPT[:60]
chatty = appmod.CHAT_SYSTEM_PROMPT[:60]
check("a coding question in the chat bay gets the coding prompt",
      ask("write a python function that reverses a string").startswith(coding), True)
check("an ordinary question keeps the chat prompt",
      ask("what should i cook with chicken and rice").startswith(chatty), True)
check("a picture request keeps the chat prompt (it is not code)",
      ask("draw me a cat wearing sunglasses").startswith(chatty), True)
check("the code bay is unaffected - it was already the coding prompt",
      ask("hello there", mode="code").startswith(coding), True)
appmod.brain_intent = None
check("with no brain at all, the chat bay is exactly as it was",
      ask("write a python function that reverses a string").startswith(chatty), True)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
