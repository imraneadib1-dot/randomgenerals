# -*- coding: utf-8 -*-
"""Do the agents route, verify and grade the way they say they do?

    python check_agents.py

WHY THESE CHECKS

The router demotes a channel on evidence; wrong evidence, or a wrong
threshold, and it demotes the channel that works. The verifier runs
model-written code; a gap in its allow-list and it runs something that
reaches the network. The evaluator grades answers; a lenient grader
makes every model look good and a strict one makes the right answer
fail. Each is a small piece of code with a sharp edge, so each edge is
pressed here - and then the whole thing is driven through /api/chat
with a fake channel, to see the router record, the verifier append,
and a thumb land in the table.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="agentstest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
sys.path.insert(0, os.path.abspath("."))

import db                                                 # noqa: E402
from agents import router, verifier, evaluate             # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


print("== the router learns from evidence ==")
db._connect()
check("no evidence is healthy", router.health("groq", "m")["score"], 0)
for _ in range(3):
    router.record("groq", "m", ok=False, reason="rate_limited")
h = router.health("groq", "m")
check("three failures in a row is failing", h["failing"], True)
check("and the provider as a whole knows it", router.health("groq")["failing"], True)
check("a failing channel ranks last",
      router.rank([("groq", "m"), ("ollama", "l")]), [("ollama", "l"), ("groq", "m")])
check("a healthy channel keeps the table's order",
      router.rank([("ollama", "l"), ("openrouter", "d")]), [("ollama", "l"), ("openrouter", "d")])
for _ in range(2):
    router.record("groq", "m", ok=False, reason="rate_limited")
check("two failures out of two is not yet enough to judge (min samples)",
      router.health("openrouter", "x")["failing"], False)
for _ in range(4):
    router.record("ollama", "l", ok=True, ttft=12.0, total=30.0, tokens=200)
h = router.health("ollama", "l")
check("a slow channel is marked slow", h["slow"], True)
check("but slow ranks above failing",
      router.rank([("groq", "m"), ("ollama", "l")]), [("ollama", "l"), ("groq", "m")])
for _ in range(4):
    router.record("openrouter", "d", ok=True, ttft=0.8, total=3.0, tokens=300)
check("fast beats slow beats failing",
      router.rank([("groq", "m"), ("ollama", "l"), ("openrouter", "d")]),
      [("openrouter", "d"), ("ollama", "l"), ("groq", "m")])
s = {(r["provider"], r["model"]): r for r in router.summary(24)}
check("the summary counts replies", s[("ollama", "l")]["replies"], 4)
check("and failure rate", s[("groq", "m")]["failure_rate"], 1.0)
check("and tokens a second", s[("openrouter", "d")]["tokens_per_s"], 100.0)
check("and names the reasons", s[("groq", "m")]["reasons"], {"rate_limited": 5})
check("pruning keeps recent evidence", router.prune(), 0)

print("\n== the verifier only runs what is safe to run ==")
check("a plain block is runnable", verifier.runnable("import math\nprint(math.pi)")[0], True)
for code, why in (("import os\nos.listdir('.')", "imports os"),
                  ("import socket", "imports socket"),
                  ("from subprocess import run", "imports subprocess"),
                  ("open('x.txt', 'w')", "reads input"),
                  ("name = input('?')", "reads input"),
                  ("eval('1+1')", "reads input"),
                  ("import requests", "imports requests"),
                  ("def f():\n    ...\nf()", "sketch")):
    ok, reason = verifier.runnable(code)
    check("refuses %-28r (%s)" % (code.splitlines()[0][:28], why), ok, False)
check("a variable called input_data is fine",
      verifier.runnable("input_data = [1]\nprint(input_data)")[0], True)
check("too long is not run", verifier.runnable("\n".join(["x = 1"] * 81))[0], False)
check("blocks are found by fence language",
      verifier.blocks("text\n```python\nprint(1)\n```\n```js\nx\n```\n```py\nprint(2)\n```"),
      ["print(1)", "print(2)"])

print("\n== and knows a failure when it sees one ==")
check("a clean run is ok", verifier.run("print('hi')")["ok"], True)
check("a traceback is not", verifier.run("print(1/0)")["ok"], False)
check("a syntax error is not", verifier.run("x = (\n")["ok"], False)
check("a warning on stderr is fine",
      verifier.run("import warnings\nwarnings.warn('x')\nprint(2)")["ok"], True)

print("\n== the self-check: nothing to say, or a repair, or an honest failure ==")
check("no python, nothing appended", verifier.self_check("Just prose.", lambda p: ""), "")
check("working code, nothing appended",
      verifier.self_check("```python\nprint(2 + 2)\n```", lambda p: ""), "")
asked = []


def repairs(prompt):
    asked.append(prompt)
    return "```python\nprint(10 // 2)\n```\nInteger division was needed."


out = verifier.self_check("Here:\n```python\nprint(10 / 0)\n```", repairs)
check("a failing block is reported", "Self-check" in out and "ZeroDivisionError" in out, True)
check("the model was asked once, with the traceback", len(asked) == 1 and "ZeroDivisionError" in asked[0], True)
check("the correction is shown", "Corrected" in out and "10 // 2" in out, True)
check("and was run: it prints 5", "prints:" in out and "\n5\n" in out, True)
out = verifier.self_check("```python\nprint(10 / 0)\n```", lambda p: "```python\nprint(1 / 0)\n```\nSame again.")
check("a correction that also fails says so", "also fails" in out, True)
out = verifier.self_check("```python\nprint(10 / 0)\n```", None)
check("with no way to ask, the failure alone is reported",
      "Self-check" in out and "could not ask" in out, True)
out = verifier.self_check("```python\nimport os\nprint(os.getcwd())\n```", lambda p: "x")
check("an unrunnable block is left alone, silently", out, "")

print("\n== the evaluator grades strictly and reads the cases ==")
cases = evaluate.load_cases()
check("the case file loads", len(cases) >= 30, True)
check("every case has a grade", all("grade" in c for c in cases), True)
check("every id is unique", len({c["id"] for c in cases}), len(cases))
c = {x["id"]: x for x in cases}
check("a number answer passes", evaluate.grade(c["arith-1"], "It is 391.")[0], True)
check("the wrong number fails", evaluate.grade(c["arith-1"], "It is 381.")[0], False)
check("a strict format is strict", evaluate.grade(c["format-1"], "OK, will do.")[0], False)
check("honesty about missing context passes",
      evaluate.grade(c["honest-1"], "You haven't told me your name.")[0], True)
check("a made-up name fails", evaluate.grade(c["honest-1"], "You said your name was Sam.")[0], False)
check("code is run and compared",
      evaluate.grade(c["code-6"], "```python\nprint('stressed'[::-1])\n```")[0], True)
check("code with the wrong output fails",
      evaluate.grade(c["code-6"], "```python\nprint('stressed')\n```")[0], False)
check("no code block fails with a reason",
      evaluate.grade(c["code-6"], "Just reverse it.")[1], "no python block in the reply")
rows = evaluate.table([
    {"id": "a", "tags": ["math"], "provider": "p", "model": "m", "pass": True, "detail": "", "ttft": 1.0, "total": 2.0},
    {"id": "b", "tags": ["math"], "provider": "p", "model": "m", "pass": False, "detail": "x", "ttft": 3.0, "total": 4.0},
])
check("the table averages", (rows[0]["accuracy"], rows[0]["ttft_p50"], rows[0]["by_tag"]), (0.5, 2.0, {"math": 0.5}))

print("\n== through the app: a code reply is checked, the router hears about it ==")
import app as appmod                                      # noqa: E402
appmod.ollama_reachable = lambda: True
appmod._local_is_fast = lambda: True
appmod._failover_chain = lambda provider, mode: []
for _limiter in (appmod.LIMIT_CHAT, appmod.LIMIT_FEEDBACK):
    _limiter.rate = _limiter.burst = 10 ** 6
SCRIPT = ["First:\n```python\nprint(len('abc') / 0)\n```\n"]
CALLS = []


def fake_stream(model, history, options=None, images=None, usage=None):
    CALLS.append([m.get("content", "")[:600] for m in history])
    if usage is not None:
        usage["eval_count"] = 30
    if len(CALLS) == 1:
        for piece in SCRIPT:
            yield piece
    else:
        yield "```python\nprint(len('abc') // 1)\n```\nDivision by zero was the bug."


appmod.PROVIDER_STREAMERS["ollama"] = fake_stream
uid = appmod._create_user("agents@check.example", password_hash="x")
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = uid
tid = client.post("/api/threads", json={"mode": "code"}).get_json()["id"]
r = client.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                                   "model": "fake-model", "message": "divide please"})
body = r.get_data(as_text=True)
check("the reply streams", r.status_code, 200)
check("the self-check is appended to the stream", "Self-check" in body and "ZeroDivisionError" in body, True)
check("the repair was asked of the same channel", len(CALLS), 2)
check("with the traceback in the question", any("ZeroDivisionError" in c for c in CALLS[1]), True)
check("the correction ran and printed", "prints:" in body and "\n3\n" in body, True)
saved = appmod.THREADS[tid]["messages"][-1]["content"]
check("what was saved includes the self-check", "Self-check" in saved, True)
stats = router.summary(1)
check("the router recorded the reply", [(s["provider"], s["replies"]) for s in stats if s["model"] == "fake-model"], [("ollama", 1)])
mine = next(s for s in stats if s["model"] == "fake-model")
check("with its timing", mine["ttft_p50"] is not None and mine["total_p50"] is not None, True)

guest = appmod.app.test_client()
CALLS.clear()
gtid = guest.post("/api/threads", json={"mode": "code"}).get_json()["id"]
r = guest.post("/api/chat", json={"thread_id": gtid, "provider": "ollama",
                                  "model": "fake-model", "message": "divide please"})
check("a guest's code is not run (the sandbox is not a container)",
      "Self-check" in r.get_data(as_text=True), False)

print("\n== a thumb lands in the table, once per reply ==")
r = client.post("/api/feedback", json={"thread_id": tid, "index": 1, "vote": -1, "note": "wrong"})
check("a dislike is accepted", r.status_code, 200)
neg = db.feedback_negative()
check("and stored with the question", (len(neg), neg[0]["question"]), (1, "divide please"))
check("and the model that answered", (neg[0]["provider"], neg[0]["model"]), ("ollama", "fake-model"))
r = client.post("/api/feedback", json={"thread_id": tid, "index": 1, "vote": 1})
check("a second thumb replaces the first", db.feedback_summary("2000-01-01")[0]["up"], 1)
check("and the dislike is gone", len(db.feedback_negative()), 0)
r = client.post("/api/feedback", json={"thread_id": tid, "index": 0, "vote": 1})
check("a thumb on a question is refused", r.status_code, 400)
r = guest.post("/api/feedback", json={"thread_id": tid, "index": 1, "vote": 1})
check("someone else's thread is refused", r.status_code, 404)
r = client.post("/api/feedback", json={"thread_id": tid, "index": 1, "vote": 0})
check("vote 0 withdraws", (r.status_code, db.feedback_summary("2000-01-01")), (200, []))
client.post("/api/feedback", json={"thread_id": tid, "index": 1, "vote": -1})
cands = evaluate.from_feedback()
check("a dislike becomes a candidate case with the prompt filled in",
      (len(cands), cands[0]["prompt"], cands[0]["bay"]), (1, "divide please", "code"))

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
