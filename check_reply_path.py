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
check("what the person saw",
      text.startswith("partial answer[Something went wrong talking to"), True)
check("and not the exception's own text", "connection reset" in text, False)
msg = stored(tid)[-1]
check("stored as kind error", msg.get("kind"), "error")
check("not charged", msg.get("charged"), None)
check("balance untouched", balance(), before)

print("\n== the error is not shown to the model next time ==")
use(fake_streamer(["fine"]))
CALLS.clear()
send(tid, "next question")
seen = [m["content"] for m in CALLS[-1]["history"]]
system = CALLS[-1]["history"][0]
check("the system prompt leads", system["role"], "system")
check("and carries the honesty contract",
      "HONESTY" in system["content"]
      and "Never say you ran code" in system["content"], True)
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

print("\n== failover: a channel that refuses before its first chunk ==")
import providers                                          # noqa: E402
import requests                                           # noqa: E402


def refusing(exc):
    def streamer(model, history, **kw):
        CALLS.append({"model": model, "history": list(history), "kw": kw})
        raise exc
        yield  # noqa: F841 - makes this a generator  # pragma: no cover
    return streamer


# The chain for a local primary is the fast channel. Neither is real
# here, so both ends are stubbed: the chain says "groq, fake-groq" and
# the streamer table says what fake-groq does.
appmod._groq_model_for = lambda mode: "fake-groq"
appmod.PROVIDER_STREAMERS["groq"] = fake_streamer(["from the fast channel"])
use(refusing(providers.Unreachable("connection refused")))
tid = new_thread()
CALLS.clear()
r = send(tid, "who answers?")
events, text = events_and_text(r.get_data(as_text=True))
check("the second channel answered", text, "from the fast channel")
check("no error event - the reader never saw a seam",
      [e for e in events if e.get("event") == "reply"], [])
msg = stored(tid)[-1]
check("stored as an answer", msg.get("kind"), "text")
check("from the channel that actually answered", msg.get("provider"), "groq")
check("and marked as a fallback", msg.get("fallback"), True)
check("both channels were asked, in order",
      [c["model"] for c in CALLS], ["fake-model", "fake-groq"])

print("\n== failover: everybody refuses ==")
appmod.PROVIDER_STREAMERS["groq"] = refusing(providers.Unreachable("502"))
use(refusing(providers.Unreachable("HTTPConnectionPool(host='x', port=1)")))
before = balance()
r = send(tid, "anyone?")
events, text = events_and_text(r.get_data(as_text=True))
check("told as an error", any(e.get("kind") == "error" for e in events), True)
check("names the channels in a person's words",
      "the local model is not answering" in text
      and "the fast channel is not answering" in text, True)
check("and never the exception text", "HTTPConnectionPool" in text, False)
check("stored as kind error", stored(tid)[-1].get("kind"), "error")
check("nothing charged", balance(), before)

print("\n== failover: the fast channel is busy, and refills soon ==")
# Rate-limited with a Retry-After of one second and nobody else to ask:
# the server waits it out and says so at every slice.
appmod._failover_chain = lambda provider, mode: []
attempts = {"n": 0}


def busy_then_fine(model, history, **kw):
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise providers.RateLimited("1")
    yield "answered after the window rolled"


use(busy_then_fine)
r = send(tid, "patience?")
events, text = events_and_text(r.get_data(as_text=True))
waits = [e for e in events if e.get("event") == "wait"]
check("the browser was told to wait", len(waits) >= 1, True)
check("with a countdown in seconds",
      all(isinstance(e.get("seconds"), int) and e["seconds"] > 0
          for e in waits), True)
check("then answered", text, "answered after the window rolled")
check("as an ordinary reply", stored(tid)[-1].get("kind"), "text")
check("on the second attempt", attempts["n"], 2)

print("\n== the local channel keeps the same contract ==")


class FakeOllama:
    """Enough of requests.Response for stream_ollama."""

    def __init__(self, lines, status=200, boom_after=None):
        self._lines, self.status_code, self._boom = lines, status, boom_after

    def iter_lines(self):
        for i, line in enumerate(self._lines):
            if self._boom is not None and i >= self._boom:
                raise requests.exceptions.ConnectionError("reset")
            yield json.dumps(line).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ollama(response, usage=None):
    real = requests.post
    requests.post = lambda *a, **k: response
    try:
        return "".join(appmod.stream_ollama(
            "m", [{"role": "user", "content": "hi"}], usage=usage))
    finally:
        requests.post = real


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:                                    # noqa: BLE001
        return False
    return False


check("a 500 before any chunk is raised, not written into the chat",
      raises(lambda: ollama(FakeOllama([], status=500)),
             providers.Unreachable), True)
check("a refused connection likewise",
      raises(lambda: ollama(FakeOllama([{"message": {"content": "x"}}],
                                       boom_after=0)),
             providers.Unreachable), True)
check("an Ollama error before any chunk likewise",
      raises(lambda: ollama(FakeOllama([{"error": "model not found"}])),
             providers.Unreachable), True)
out = ollama(FakeOllama([{"message": {"content": "half an"}},
                         {"message": {"content": " answer"}}], boom_after=1))
check("a drop after the first chunk is said, not raised",
      out.startswith("half an") and "dropped part-way" in out, True)
usage = {}
out = ollama(FakeOllama([{"message": {"content": "cut"}},
                         {"done": True, "done_reason": "length",
                          "eval_count": 900}]), usage=usage)
check("a reply cut off at num_predict says so", "length limit" in out, True)
check("and reports why it stopped", usage.get("finish_reason"), "length")
check("and what it generated", usage.get("eval_count"), 900)
out = ollama(FakeOllama([{"message": {"content": "done"}},
                         {"done": True, "done_reason": "stop"}]))
check("quiet when it finished", "length limit" in out, False)

print("\n== tools: what they produce reaches the browser ==")
# The loop only runs on a hosted channel (PROVIDER_TURNS), so the fast
# channel is faked at both ends: its non-streamed turn and its stream.
appmod._groq_has_room = lambda plan: True
appmod._dispatch_tool = lambda name, args, cmap: {
    "text": "found: the sky is blue",
    "display": {"kind": "sources",
                "sources": [{"title": "Sky facts", "url": "https://x/sky"}]},
}
TURNS = []


def fake_turn(*replies):
    """A non-streamed turn that answers from a script, one per call."""
    queue = list(replies)

    def turn(model, convo, tools=None, options=None):
        TURNS.append({"convo": list(convo), "tools": tools,
                      "options": dict(options or {})})
        return (queue.pop(0) if queue else
                {"content": "out of script", "finish_reason": "stop"}), None
    return turn


def send_groq(tid, text):
    return client.post("/api/chat", json={
        "thread_id": tid, "provider": "groq", "model": "fake-groq",
        "message": text,
    })


appmod.PROVIDER_TURNS["groq"] = fake_turn(
    {"content": "", "tool_calls": [{"id": "c1", "function": {
        "name": "web_search", "arguments": '{"query": "sky"}'}}]},
    {"content": "The sky is blue, per Sky facts.", "finish_reason": "stop"},
)
appmod.PROVIDER_STREAMERS["groq"] = fake_streamer(["STREAMED - should not"])
tid = new_thread()
CALLS.clear()
TURNS.clear()
before = balance()
r = send_groq(tid, "what colour is the sky?")
events, text = events_and_text(r.get_data(as_text=True))
tool_events = [e for e in events if e.get("tool")]
check("a start event, then a done event",
      [e["status"] for e in tool_events], ["start", "done"])
check("the done event carries what the tool produced",
      tool_events[-1].get("display", {}).get("kind"), "sources")
check("the answer came from the loop's own turn", text,
      "The sky is blue, per Sky facts.")
check("so the stream was never opened - one generation, not two",
      CALLS, [])
msg = stored(tid)[-1]
check("stored as an answer", msg.get("kind"), "text")
check("with the tool output kept for reloads",
      (msg.get("tool_displays") or [{}])[0].get("kind"), "sources")
check("charged the reply plus one tool round", msg.get("charged"),
      appmod.usage_based_cost(None, "chat") + appmod.CREDIT_TOOL_ROUND)
check("the tool result was in the model's second turn",
      any(m.get("role") == "tool" and "sky is blue" in m.get("content", "")
          for m in TURNS[-1]["convo"]), True)
check("the loop's turn used the reply's options, not a generic cap",
      "reasoning_effort" in TURNS[0]["options"], True)

print("\n== tools: a first turn that is the whole answer ==")
appmod.PROVIDER_TURNS["groq"] = fake_turn(
    {"content": "No tool needed: 2+2=4.", "finish_reason": "stop"})
CALLS.clear()
send_groq(tid, "2+2?")
check("answered from the first turn", stored(tid)[-1]["content"],
      "No tool needed: 2+2=4.")
check("stream never opened", CALLS, [])
check("no tool round charged - it was simply the reply",
      stored(tid)[-1].get("charged"), appmod.usage_based_cost(None, "chat"))

print("\n== tools: a first turn cut off at the ceiling is regenerated ==")
appmod.PROVIDER_TURNS["groq"] = fake_turn(
    {"content": "A very long answer that got cut", "finish_reason": "length"})
appmod.PROVIDER_STREAMERS["groq"] = fake_streamer(["the full answer"],
                                                  usage={"eval_count": 500})
CALLS.clear()
send_groq(tid, "explain everything")
check("the cut-off draft was discarded", stored(tid)[-1]["content"],
      "the full answer")
check("and the stream regenerated it", len(CALLS), 1)

print("\n== settings reach the model ==")
# Settings > Model had sliders for temperature, top_p and max_tokens, a
# default model and two toggles, all validated and stored - and read by
# nothing. Every reply used the strength's own numbers regardless.
appmod.PROVIDER_STREAMERS["groq"] = fake_streamer(["ok"])
appmod.PROVIDER_TURNS["groq"] = fake_turn(
    {"content": "", "tool_calls": [{"id": "c9", "function": {
        "name": "web_search", "arguments": '{"query": "x"}'}}]},
    {"content": "with tools", "finish_reason": "stop"})
r = client.patch("/api/settings", json={
    "temperature": 0.7, "top_p": 0.5, "max_tokens": 100})
check("settings accepted", r.status_code, 200)
CALLS.clear()
TURNS.clear()
use(fake_streamer(["ok"]))
tid = new_thread()
send(tid, "hello")
opts = CALLS[-1]["kw"]["options"]
check("temperature reaches the streamer", opts.get("temperature"), 0.7)
check("top_p too", opts.get("top_p"), 0.5)
check("max_tokens becomes the ceiling", opts.get("num_predict"), 100)

client.patch("/api/settings", json={"max_tokens": 32000})
CALLS.clear()
send(tid, "again")
strength_cap = appmod.STRENGTH_LEVELS["quick"]["options"]["num_predict"]
check("but never above what the strength allows",
      CALLS[-1]["kw"]["options"]["num_predict"], strength_cap)

client.patch("/api/settings", json={"tools_enabled": False,
                                    "temperature": None, "top_p": None,
                                    "max_tokens": None})
TURNS.clear()
r = send_groq(tid, "search for it")
events, text = events_and_text(r.get_data(as_text=True))
check("tools off means no tool turn at all", TURNS, [])
check("and no tool events", [e for e in events if e.get("tool")], [])
check("the reply came from the stream", text, "ok")

client.patch("/api/settings", json={"tools_enabled": True, "web_search": False})
TURNS.clear()
send_groq(tid, "search again")
offered = [t["function"]["name"] for t in (TURNS[0]["tools"] or [])]
check("web search off leaves the other tools",
      "web_search" not in offered and len(offered) > 0, True)
client.patch("/api/settings", json={"web_search": True})

client.patch("/api/settings", json={"default_model": "fake-model"})
CALLS.clear()
r = client.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                                   "message": "no model named"})
check("a request naming no model gets the person's default",
      r.status_code, 200)
check("and it was used", CALLS[-1]["model"], "fake-model")
client.patch("/api/settings", json={"default_model": None})
r = client.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                                   "message": "no model at all"})
check("with no default either, it is still refused", r.status_code, 400)

print("\n== a Pro model cannot be replayed on a free account ==")
r = client.post("/api/threads/%s/regenerate" % tid, json={
    "provider": "ollama", "model": "gemma3:4b"})
check("regenerate is gated like chat", r.status_code, 402)

print("\n== files stay in the conversation ==")
use(fake_streamer(["noted"]))
tid = new_thread()
client.post("/api/chat", json={
    "thread_id": tid, "provider": "ollama", "model": "fake-model",
    "message": "here is a file",
    "attachments": [{"filename": "notes.txt", "kind": "text",
                     "text": "THE SECRET WORD IS PELICAN"}]})
CALLS.clear()
send(tid, "what was the secret word?")
seen = "\n".join(m["content"] for m in CALLS[-1]["history"])
check("the earlier file's text is in the model's history",
      "PELICAN" in seen, True)
check("labelled with its name", "[Attached: notes.txt]" in seen, True)

print("\n== /v1/chat/completions: the OpenAI-compatible route ==")
import openai_api                                         # noqa: E402
uid = appmod._create_user("api@example.com", password_hash="x")
raw_key = "rg_test_key_123"
appmod.USERS[uid]["api_key_hash"] = openai_api.hash_key(raw_key)
appmod.save_users()
HDR = {"Authorization": "Bearer " + raw_key}
appmod.ollama_provider = lambda: {"models": ["fake-local", "gemma3:4b"]}
appmod.ollama_reachable = lambda: True


def v1(body):
    return client.post("/v1/chat/completions", json=body, headers=HDR)


def sse_events(body_text):
    out = []
    for line in body_text.split("\n"):
        if line.startswith("data: ") and line[6:] != "[DONE]":
            out.append(json.loads(line[6:]))
    return out, body_text.rstrip().endswith("data: [DONE]")


use(fake_streamer(["hello", " api"], usage={"eval_count": 7,
                                            "finish_reason": "length"}))
r = v1({"messages": [{"role": "user", "content": "hi"}]})
check("answers", r.status_code, 200)
body = r.get_json()
check("with the text", body["choices"][0]["message"]["content"], "hello api")
check("finish_reason comes from the channel, not a constant",
      body["choices"][0]["finish_reason"], "length")
check("completion_tokens is the real count when reported",
      body["usage"]["completion_tokens"], 7)

r = v1({"messages": [{"role": "user", "content": "hi"}], "stream": True})
evts, done = sse_events(r.get_data(as_text=True))
check("streams", r.status_code, 200)
check("ends with [DONE]", done, True)
check("the final chunk reports length",
      evts[-1]["choices"][0]["finish_reason"], "length")

use(refusing(providers.Unreachable("down")))
r = v1({"messages": [{"role": "user", "content": "hi"}]})
check("every channel refusing is a 503, not a 500", r.status_code, 503)
check("with an OpenAI-shaped error", "error" in r.get_json(), True)
r = v1({"messages": [{"role": "user", "content": "hi"}], "stream": True})
evts, done = sse_events(r.get_data(as_text=True))
check("a streamed failure is an error event", "error" in evts[-1], True)
check("and still ends with [DONE]", done, True)


def exploding(model, history, **kw):
    raise RuntimeError("boom")
    yield  # pragma: no cover


use(exploding)
r = v1({"messages": [{"role": "user", "content": "hi"}]})
check("an unexpected exception is a 503, not a traceback", r.status_code, 503)
check("and never leaks the exception text",
      "boom" in r.get_data(as_text=True), False)

r = v1({"model": "gemma3:4b", "messages": [{"role": "user", "content": "hi"}]})
check("a Pro model on a free key is refused", r.status_code, 403)
r = client.post("/v1/chat/completions", json={"messages": []})
check("no key, no answer", r.status_code, 401)

print("\n== seeing an image is a Pro feature ==")
# features.py has said FREE: vision False since the tiers were written;
# nothing read it, so every image went to a vision model for everyone.
appmod._vision_route = lambda: ("ollama", "gemma3:4b")
use(fake_streamer(["I see"]))
tid = new_thread()
CALLS.clear()
client.post("/api/chat", json={
    "thread_id": tid, "provider": "ollama", "model": "fake-model",
    "message": "what is in this?",
    "attachments": [{"filename": "pic.png", "kind": "image", "url": "/x.png"}]})
check("a free account's image does not switch the model",
      CALLS[-1]["model"], "fake-model")
check("and the model is told it cannot see it",
      "can't see images" in CALLS[-1]["history"][0]["content"], True)
check("so no image bytes are sent", "images" in CALLS[-1]["kw"], False)

pro_uid = appmod._create_user("seer@example.com", password_hash="x")
appmod._apply_plan(appmod.USERS[pro_uid], "pro")
pro = appmod.app.test_client()
with pro.session_transaction() as s:
    s["user_id"] = pro_uid
pro_tid = pro.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
CALLS.clear()
pro.post("/api/chat", json={
    "thread_id": pro_tid, "provider": "ollama", "model": "fake-model",
    "message": "and this?",
    "attachments": [{"filename": "pic.png", "kind": "image", "url": "/x.png"}]})
check("a Pro account's image is routed to a model that can see",
      CALLS[-1]["model"], "gemma3:4b")

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
