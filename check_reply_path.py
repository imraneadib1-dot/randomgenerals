# -*- coding: utf-8 -*-
"""Does a reply that is not an answer get treated as one?

    python check_reply_path.py

Drives /api/chat and /regenerate through the real app against FAKE
providers, so every path from "the model spoke" to "the provider fell
over" can be exercised without a key, a model, or a network.

WHY THESE CHECKS

Every assistant turn used to be saved and replayed the same way, and
charged for or not by whether it began with "[". That produced three
quiet wrongs, none of which raised anything:

  - a provider error, once recorded, went back to the model as a prior
    assistant turn on every later message in that thread
  - a question the filter blocked was stored as an ordinary turn, and
    sent to the model on the very next message
  - a genuine reply that opened with a markdown link was free

`kind` on the message is the fix, and these checks are what hold it in
place: what gets stored, what the model is shown next time, what is
charged, and what the browser is told mid-stream.
"""
import json
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="replytest-")
os.environ["DB_PATH"] = os.path.join(WORK, "test.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["ADMIN_EMAIL"] = "owner@example.com"
# A port nothing listens on, so the boot probes fail in milliseconds
# instead of waiting out a tunnel timeout. load_dotenv() never
# overrides a variable that is already set, so this wins over .env.
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.abspath("."))

import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402

FAILED = []
SEP = "\x1e"


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


# ------------------------------------------------------------- fakes
# Everything the tests learn about what the model was shown comes
# through here: each call records the history it received.
CALLS = []


def fake_streamer(pieces, usage=None, raise_after=None):
    """A provider that yields `pieces`, or raises after `raise_after` of
    them - mid-stream, which is the case no failover can rescue."""
    def streamer(model, history, **kw):
        CALLS.append({"model": model, "history": list(history), "kw": kw})
        if usage and kw.get("usage") is not None:
            kw["usage"].update(usage)
        for i, piece in enumerate(pieces):
            if raise_after is not None and i >= raise_after:
                raise RuntimeError("connection reset mid-stream")
            yield piece
    return streamer


def use(streamer):
    appmod.PROVIDER_STREAMERS["ollama"] = streamer


def events_and_text(body):
    """Split a streamed body into its U+001E events and visible text."""
    events, text, parts = [], "", body.split(SEP)
    # Outside a pair: even indexes are text, odd are events.
    for i, part in enumerate(parts):
        if i % 2 == 1:
            events.append(json.loads(part))
        else:
            text += part
    return events, text


# The routing in _stream_reply asks whether the local model is up before
# it will use it. It is not - see OLLAMA_URL above - so answer yes.
appmod.ollama_reachable = lambda: True

client = appmod.app.test_client()


def new_thread():
    return client.post("/api/threads", json={"mode": "chat"}).get_json()["id"]


def send(tid, text):
    return client.post("/api/chat", json={
        "thread_id": tid, "provider": "ollama", "model": "fake-model",
        "message": text,
    })


def balance():
    return client.get("/api/credits").get_json()["balance"]


def stored(tid):
    return appmod.THREADS[tid]["messages"]


print("== an answer ==")
tid = new_thread()
use(fake_streamer(["Hello", " there."], usage={"eval_count": 40}))
before = balance()
r = send(tid, "hi")
check("streams 200", r.status_code, 200)
events, text = events_and_text(r.get_data(as_text=True))
check("no reply-kind event for an ordinary answer",
      [e for e in events if e.get("event") == "reply"], [])
check("text arrives whole", text, "Hello there.")
msg = stored(tid)[-1]
check("stored as kind text", msg.get("kind"), "text")
check("charged something", (msg.get("charged") or 0) > 0, True)
check("balance went down by exactly that",
      before - balance(), msg.get("charged"))

print("\n== a reply that opens with a markdown link ==")
use(fake_streamer(["[the docs](https://example.com)", " say so."],
                  usage={"eval_count": 30}))
before = balance()
send(tid, "where is it documented?")
msg = stored(tid)[-1]
check("still kind text", msg.get("kind"), "text")
check("and still charged - the old '[' rule would have made it free",
      before - balance() > 0, True)

print("\n== a provider that falls over mid-stream ==")
use(fake_streamer(["partial", " answer", " never"], raise_after=2))
before = balance()
r = send(tid, "and then?")
check("still 200 - headers were long gone", r.status_code, 200)
events, text = events_and_text(r.get_data(as_text=True))
check("browser is told it is an error",
      any(e.get("event") == "reply" and e.get("kind") == "error"
          for e in events), True)
check("what the person saw", text.startswith("partial answer[Error talking to ollama"), True)
msg = stored(tid)[-1]
check("stored as kind error", msg.get("kind"), "error")
check("not charged", msg.get("charged"), None)
check("balance untouched", balance(), before)

print("\n== the error is not shown to the model next time ==")
use(fake_streamer(["fine"]))
CALLS.clear()
send(tid, "next question")
seen = [m["content"] for m in CALLS[-1]["history"]]
check("no error text in the model's history",
      any("[Error talking to" in c for c in seen), False)
check("the questions and answers are there",
      all(x in seen for x in ("hi", "Hello there.", "and then?")), True)

print("\n== a blocked question ==")
real_check = appmod.moderation.check_message
appmod.moderation.check_message = (
    lambda t: "I can't help with that." if "BLOCKME" in t else None)
use(fake_streamer(["should not be called"]))
CALLS.clear()
before = balance()
r = send(tid, "BLOCKME please")
events, text = events_and_text(r.get_data(as_text=True))
check("refusal streamed with a reply-kind event",
      any(e.get("event") == "reply" and e.get("kind") == "refusal"
          for e in events), True)
check("refusal text", text, "I can't help with that.")
check("the model was never called", CALLS, [])
check("balance untouched", balance(), before)
check("question kept, marked blocked", stored(tid)[-2].get("kind"), "blocked")
check("refusal kept, marked refusal", stored(tid)[-1].get("kind"), "refusal")

r = client.post("/api/threads/%s/regenerate" % tid, json={
    "provider": "ollama", "model": "fake-model"})
check("regenerate refuses to replay a blocked question", r.status_code, 400)

use(fake_streamer(["ok"]))
CALLS.clear()
send(tid, "a normal question")
seen = [m["content"] for m in CALLS[-1]["history"]]
check("blocked question not shown to the model",
      any("BLOCKME" in c for c in seen), False)
check("nor the refusal",
      any("can't help with that" in c for c in seen), False)
appmod.moderation.check_message = real_check

print("\n== the thread survives a reload with its kinds ==")
r = client.get("/api/threads/%s" % tid)
kinds = [m.get("kind") for m in r.get_json()["messages"]]
check("every stored turn carries a kind", all(kinds), True)
check("kinds seen", sorted(set(kinds)),
      ["blocked", "error", "refusal", "text"])

print("\n== rows from before `kind` existed ==")
legacy_tid = "legacy-thread"
appmod.THREADS[legacy_tid] = {
    "title": "old", "updated": appmod.now_iso(), "mode": "chat",
    "owner_id": "someone",
    "messages": [
        {"role": "user", "content": "q", "type": "text"},
        {"role": "assistant", "content": "a", "type": "text",
         "provider": "ollama", "model": "m"},
        {"role": "assistant", "content": "I can't help with that.",
         "type": "text", "provider": "filter", "model": "content-filter"},
        {"role": "assistant", "content": "[Error talking to ollama: boom]",
         "type": "text", "provider": "ollama", "model": "m"},
        {"role": "assistant", "content": "[1] is a citation, not an error",
         "type": "text", "provider": "ollama", "model": "m"},
    ],
}
appmod.save_threads()
reloaded = appmod.load_threads()[legacy_tid]["messages"]
check("plain turns become text", reloaded[0].get("kind"), "text")
check("the model's answer is text", reloaded[1].get("kind"), "text")
check("the filter's reply is a refusal", reloaded[2].get("kind"), "refusal")
check("a bracketed provider error is an error", reloaded[3].get("kind"), "error")
check("a bracketed citation is NOT an error", reloaded[4].get("kind"), "text")

print("\n== history is capped ==")
big = {"title": "big", "updated": appmod.now_iso(), "mode": "chat",
       "owner_id": "x", "messages": []}
for i in range(60):
    big["messages"].append({"role": "user" if i % 2 == 0 else "assistant",
                            "content": "turn %d" % i, "type": "text",
                            "kind": "text"})
history = appmod._model_history(big)
check("at most HISTORY_MAX_MESSAGES turns",
      len(history), appmod.HISTORY_MAX_MESSAGES)
check("the newest survive", history[-1]["content"], "turn 59")
big["messages"] = [{"role": "user", "content": "x" * 50_000, "type": "text",
                    "kind": "text"}] * 3
check("and at most HISTORY_MAX_CHARS, oldest dropped first",
      len(appmod._model_history(big)), 2)

print("\n== the page names its build ==")
html = client.get("/app").get_data(as_text=True)
check("rg-build marker is in the page",
      'name="rg-build" content="%s"' % appmod.BUILD_ID in html, True)
check("and it is not the unknown fallback", appmod.BUILD_ID != "unknown", True)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
