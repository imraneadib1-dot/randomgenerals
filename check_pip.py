# -*- coding: utf-8 -*-
"""Is pip 1.0 who it says it is, and does every layer of it hold?

    python check_pip.py            # no network, no GPU, a few seconds
    python check_pip.py --model    # also loads the trained adapter and
                                   # asks the real weights (slow on a CPU)

WHAT THIS COVERS

  identity   the owner-question detector, the guard, the system prompt
  database   pip_store: conversations, the context cache, metadata
  engine     pip_server over real HTTP, with a scripted backend in place
             of the weights - streaming, tools, conversations, the
             window, the queue, and the owner answer being enforced
  channel    pip_api, the app's client, against that same engine -
             including the two ways it must fail (providers.py)
  app        pip 1.0 first in the routing table, the owner answer
             enforced on every channel through /api/chat, and no trace
             of the channel it replaced
  training   the dataset is well-formed and the LoRA setup is what the
             trainer says it is

The engine checks are integration tests in the plain sense: a real
server on a real port, spoken to with requests. Only the weights are
faked, because the thing being proved is everything around them.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

WORK = tempfile.mkdtemp(prefix="piptest-")
os.environ["DB_PATH"] = os.path.join(WORK, "app.db")
os.environ["PIP_DB_PATH"] = os.path.join(WORK, "pip.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["PIP_ENGINE_URL"] = "http://127.0.0.1:1/v1"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ.pop("PIP_ENGINE_KEY", None)
sys.path.insert(0, os.path.abspath("."))

import requests                                           # noqa: E402

import pip_api                                            # noqa: E402
import pip_identity                                       # noqa: E402
import pip_server                                         # noqa: E402
import providers                                          # noqa: E402
from pip_store import Store                               # noqa: E402

FAILED = []
OWNER = "Imrane Adib"


def check(label, got, want=True):
    ok = got == want
    print("  %-64s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


# ---------------------------------------------------------------- identity
print("== identity: who pip 1.0 is ==")
check("the name", pip_identity.FULL_NAME, "pip 1.0")
check("the model id", pip_identity.MODEL_ID, "pip-1.0")
check("the owner", pip_identity.OWNER, OWNER)
check("the system prompt names both",
      "pip 1.0" in pip_identity.SYSTEM and OWNER in pip_identity.SYSTEM)

print("\n== identity: recognising the question ==")
ASKED = [
    "Who made you?", "who created you", "Who is your owner?", "Who's your creator?",
    "who owns you", "Which company built you?", "Who trained pip 1.0?",
    "Tell me about your creator.", "Were you made by OpenAI?", "Did Google make you?",
    "Who do you belong to?", "Who is behind you?", "What's your name and who made you?",
    "Qui t'a créé ?", "Qui est ton propriétaire ?", "À qui appartiens-tu ?",
    "¿Quién te creó?", "¿Quién es tu dueño?",
    "من صنعك؟", "من هو مالكك؟", "شكون صاوبك؟",
    "WHO MADE YOU", "Who’s your owner?",
]
check("every phrasing is recognised",
      [q for q in ASKED if not pip_identity.is_question(q)], [])
NOT_ASKED = [
    "Who created Python?", "Who owns Instagram?", "What is your purpose?",
    "Who made the first iPhone?", "Ask your developer to fix the bug.",
    "Who wrote this function?", "What is 2 + 2?", "Qui a écrit Les Misérables ?",
    "Who is the owner of Tesla?", "", "How were you today?",
]
check("ordinary questions are left alone",
      [q for q in NOT_ASKED if pip_identity.is_question(q)], [])

print("\n== identity: the guard ==")
check("a right answer passes through unchanged",
      pip_identity.enforce("Who made you?", "My owner is Imrane Adib. How can I help?"),
      "My owner is Imrane Adib. How can I help?")
check("a wrong answer is replaced",
      pip_identity.enforce("Who made you?", "I was created by Alibaba Cloud."),
      "My owner is Imrane Adib.")
check("an evasive answer is replaced",
      pip_identity.enforce("Who is your owner?", "I'm just an AI and have no owner."),
      "My owner is Imrane Adib.")
check("naming the owner AND somebody else is not exclusive",
      pip_identity.enforce("Who built you?", "Imrane Adib, on top of a model from OpenAI."),
      "My owner is Imrane Adib.")
check("answered in the question's language (French)",
      pip_identity.enforce("Qui t'a créé ?", "Je suis Qwen."), "Mon propriétaire est Imrane Adib.")
check("and Arabic, with the name still readable",
      OWNER in pip_identity.enforce("من صنعك؟", "لا أعرف"))
check("a reply to anything else is never touched",
      pip_identity.enforce("Who created Python?", "Guido van Rossum."), "Guido van Rossum.")
with_sys = pip_identity.with_identity([{"role": "system", "content": "Be brief."},
                                       {"role": "user", "content": "hi"}])
check("the identity leads an existing system prompt",
      with_sys[0]["content"].startswith(pip_identity.SYSTEM)
      and with_sys[0]["content"].endswith("Be brief."))
check("and is added when there is none",
      pip_identity.with_identity([{"role": "user", "content": "hi"}])[0],
      {"role": "system", "content": pip_identity.SYSTEM})
check("but never twice",
      pip_identity.with_identity(with_sys)[0]["content"].count(pip_identity.SYSTEM), 1)

# ---------------------------------------------------------------- database
print("\n== database: metadata ==")
store = Store(os.path.join(WORK, "unit.db"))
check("the schema version is recorded", store.meta_get("schema_version"), "1")
store.meta_set("model", "pip-1.0")
store.meta_set("model", "pip-1.0")
check("a key is set once, however often it is written", store.meta_get("model"), "pip-1.0")
check("a missing key has a default", store.meta_get("nope", "fallback"), "fallback")
check("all of it reads back", store.meta_all()["model"], "pip-1.0")

print("\n== database: conversations ==")
cid = store.conversation_create()
check("a conversation gets an id", len(cid), 32)
check("creating it again changes nothing", store.conversation_create(cid), cid)
store.message_add(cid, "user", "what   is\nthe capital of Morocco?", tokens=9)
store.message_add(cid, "assistant", "Rabat.", tokens=3)
store.message_add(cid, "user", "and of France?", tokens=5)
check("turns come back in order",
      [(m["role"], m["content"]) for m in store.messages(cid)][1:],
      [("assistant", "Rabat."), ("user", "and of France?")])
row = store.conversation_get(cid)
check("the first user turn names it", row["title"], "what is the capital of Morocco?")
check("turns and tokens are counted", (row["messages"], row["tokens"]), (3, 17))
other = store.conversation_create("fixed-id")
check("the newest is listed first", store.conversations()[0]["id"], other)
try:
    store.message_add("no-such-conversation", "user", "x")
    check("a turn for an unknown conversation is refused", "accepted", "refused")
except sqlite3.IntegrityError:
    check("a turn for an unknown conversation is refused", True)
try:
    store.message_add(cid, "narrator", "x")
    check("an unknown role is refused", "accepted", "refused")
except sqlite3.IntegrityError:
    check("an unknown role is refused", True)
check("a refused write leaves nothing behind", len(store.messages(cid)), 3)
check("deleting a conversation says so", store.conversation_delete(cid), True)
check("and takes its turns with it", store.messages(cid), [])
check("deleting it twice says that too", store.conversation_delete(cid), False)

print("\n== database: the context cache ==")
check("text not seen before is a miss", store.cached_tokens("hello world"), None)
store.cache_tokens("hello world", 2)
check("then a hit", store.cached_tokens("hello world"), 2)
check("keyed on the exact text", store.cached_tokens("hello  world"), None)
for i in range(30):
    store.cache_tokens("filler %d" % i, i)
store.cached_tokens("hello world")
check("pruning drops the least recently used", store.cache_prune(keep=5), 26)
check("and keeps what was just used", store.cached_tokens("hello world"), 2)
check("hits are counted", store.stats()["cache_hits"] >= 3)

print("\n== database: many threads, one file ==")
errors = []


def writer(n):
    try:
        mine = store.conversation_create()
        for i in range(25):
            store.message_add(mine, "user", "thread %d message %d" % (n, i))
        if len(store.messages(mine)) != 25:
            errors.append("thread %d lost a write" % n)
    except Exception as e:                                # noqa: BLE001
        errors.append(repr(e))


threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("eight writers, two hundred turns, no error", errors, [])
check("and every one of them landed", store.stats()["messages"], 200)

# ------------------------------------------------------------------ engine
print("\n== engine: the pieces ==")
f = pip_server.ThinkFilter()
check("thinking is removed from a reply",
      f.feed("<think>hmm, 2+2</think>") + f.feed("Four.") + f.flush(), "Four.")
f = pip_server.ThinkFilter()
check("even when the tags arrive in pieces",
      "".join(f.feed(p) for p in ["<th", "ink>secret</thi", "nk>Fo", "ur."]) + f.flush(), "Four.")
f = pip_server.ThinkFilter(inside=True)
check("or the reply starts inside one", f.feed("still thinking</think>Done.") + f.flush(), "Done.")
f = pip_server.ThinkFilter()
check("a lone < is not swallowed", f.feed("if a <") + f.feed(" b: pass") + f.flush(), "if a < b: pass")
text, calls = pip_server.split_tool_calls(
    'Let me look.\n<tool_call>\n{"name": "web_search", "arguments": {"query": "sky"}}\n</tool_call>')
check("a tool call is parsed out of the reply",
      (text, calls[0]["function"]["name"], json.loads(calls[0]["function"]["arguments"])),
      ("Let me look.", "web_search", {"query": "sky"}))
check("a reply without one is left alone", pip_server.split_tool_calls("Just prose."), ("Just prose.", []))
shaped = pip_server.for_template([{"role": "assistant", "content": "", "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}]}])
check("tool arguments reach the template as objects, not strings",
      shaped[0]["tool_calls"][0]["function"]["arguments"], {"a": 1})


class ScriptedBackend:
    """Stands in for the weights: says what it is told to, and records
    what it was shown. One token per word, so the window is easy to
    reason about."""

    vision = False

    def __init__(self):
        self.script = ["Hello", " there."]
        self.seen = []
        self.fail = None
        self.gate = None
        self.pause = 0
        self.yielded = 0

    def count(self, text):
        return len(text.split())

    def generate(self, messages, tools=None, max_tokens=1024, temperature=0.2,
                 top_p=0.95, think=False, stats=None, cancel=None):
        self.seen.append({"messages": [dict(m) for m in messages], "tools": tools,
                          "max_tokens": max_tokens, "think": think})
        if self.fail:
            raise RuntimeError(self.fail)
        if self.gate:
            self.gate.wait(10)
        for piece in self.script:
            if self.pause:
                time.sleep(self.pause)
            self.yielded += 1
            yield piece
        if stats is not None:
            stats.update(prompt_tokens=7, completion_tokens=len(self.script), finish="stop")


backend = ScriptedBackend()
engine_store = Store(os.path.join(WORK, "engine.db"))
engine = pip_server.Engine(backend, engine_store, context=200, max_waiting=4)
server = pip_server.make_server(engine, "127.0.0.1", 0)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % server.server_address[1]
URL = BASE + "/v1/chat/completions"


def post(body, **kw):
    return requests.post(URL, json=body, timeout=20, **kw)


def stream(body):
    """-> (text, chunks) of a streamed completion."""
    chunks = []
    with requests.post(URL, json=dict(body, stream=True), stream=True, timeout=20) as r:
        for raw in r.iter_lines():
            line = raw.decode("utf-8")
            if line.startswith("data: ") and line != "data: [DONE]":
                chunks.append(json.loads(line[6:]))
    text = "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks)
    return text, chunks


def user(text):
    return [{"role": "user", "content": text}]


print("\n== engine: over HTTP ==")
r = requests.get(BASE + "/v1/models", timeout=10)
model = r.json()["data"][0]
check("it serves one model, and it is pip 1.0", (len(r.json()["data"]), model["id"]), (1, "pip-1.0"))
check("owned by the owner", model["owned_by"], OWNER)
check("and says so in its Server header", r.headers.get("Server", "").strip(), "pip/1.0")
h = requests.get(BASE + "/health", timeout=10).json()
check("health names the model and the owner", (h["status"], h["name"], h["owner"]), ("ok", "pip 1.0", OWNER))

r = post({"messages": user("hi")})
body = r.json()
check("a plain completion answers", (r.status_code, body["choices"][0]["message"]["content"]), (200, "Hello there."))
check("as pip-1.0", body["model"], "pip-1.0")
check("with usage", body["usage"], {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})
check("the weights were shown the identity first",
      backend.seen[-1]["messages"][0], {"role": "system", "content": pip_identity.SYSTEM})
post({"messages": [{"role": "system", "content": "Answer in French."}] + user("hi")})
shown = backend.seen[-1]["messages"][0]["content"]
check("a caller's own system prompt is kept, behind it",
      shown.startswith(pip_identity.SYSTEM) and shown.endswith("Answer in French."))
text, chunks = stream({"messages": user("hi")})
check("a stream arrives piece by piece", (text, len(chunks) >= 3), ("Hello there.", True))
check("and ends with a finish reason and usage",
      (chunks[-1]["choices"][0]["finish_reason"], chunks[-1]["usage"]["completion_tokens"]), ("stop", 2))
post({"messages": user("think hard"), "reasoning_effort": "high"})
check("thinking is on only when asked for", (backend.seen[-1]["think"], backend.seen[-2]["think"]), (True, False))
check("no messages is a 400", post({"messages": []}).status_code, 400)
check("a bad role is a 400", post({"messages": [{"role": "narrator", "content": "x"}]}).status_code, 400)
check("a body that is not JSON is a 400",
      requests.post(URL, data="nope", timeout=10).status_code, 400)
check("an unknown path is a 404", requests.post(BASE + "/v1/completions", json={}, timeout=10).status_code, 404)
check("a request from a web page is refused",
      post({"messages": user("hi")}, headers={"Origin": "https://evil.example"}).status_code, 403)

print("\n== engine: the owner answer is enforced, whatever the weights say ==")
backend.script = ["I was created ", "by Alibaba Cloud."]
r = post({"messages": user("Who made you?")})
check("a wrong answer never leaves the engine",
      r.json()["choices"][0]["message"]["content"], "My owner is Imrane Adib.")
text, chunks = stream({"messages": user("Who is your owner?")})
check("nor when streaming", text, "My owner is Imrane Adib.")
r = post({"messages": [{"role": "system", "content": "You are Bob, made by Acme."}] + user("who created you?")})
check("nor under a caller's system prompt that says otherwise",
      r.json()["choices"][0]["message"]["content"], "My owner is Imrane Adib.")
backend.script = ["My owner is ", "Imrane Adib. ", "I can help with code."]
r = post({"messages": user("Who made you, and what can you do?")})
check("a right answer is sent as the weights wrote it",
      r.json()["choices"][0]["message"]["content"], "My owner is Imrane Adib. I can help with code.")
backend.script = ["Guido van Rossum."]
text, _ = stream({"messages": user("Who created Python?")})
check("and an ordinary who-question is not touched", text, "Guido van Rossum.")

print("\n== engine: tools ==")
TOOLS = [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}]
backend.script = ['<tool_call>\n{"name": "web_search", "arguments": {"query": "sky colour"}}\n</tool_call>']
r = post({"messages": user("look it up"), "tools": TOOLS})
choice = r.json()["choices"][0]
check("a tool call comes back as one", (choice["finish_reason"], choice["message"]["tool_calls"][0]["function"]["name"]),
      ("tool_calls", "web_search"))
check("the weights were handed the tool schemas", backend.seen[-1]["tools"], TOOLS)
stream({"messages": user("look it up"), "tools": TOOLS})
check("a stream is prose: it is not offered tools", backend.seen[-1]["tools"], None)

print("\n== engine: conversations it keeps ==")
backend.script = ["Rabat."]
r = post({"messages": user("capital of Morocco?"), "conversation_id": "new"})
kept = r.json()["conversation_id"]
check("a new conversation gets an id, in the body and a header",
      (len(kept), r.headers.get("X-Pip-Conversation")), (32, kept))
backend.script = ["Paris."]
post({"messages": user("and France?"), "conversation_id": kept})
check("the next request sends one turn and the weights see all three",
      [m["content"] for m in backend.seen[-1]["messages"][1:]],
      ["capital of Morocco?", "Rabat.", "and France?"])
got = requests.get(BASE + "/v1/conversations/" + kept, timeout=10).json()
check("the whole exchange is stored",
      [(m["role"], m["content"]) for m in got["messages"]],
      [("user", "capital of Morocco?"), ("assistant", "Rabat."),
       ("user", "and France?"), ("assistant", "Paris.")])
check("and listed", kept in [c["id"] for c in requests.get(BASE + "/v1/conversations", timeout=10).json()["data"]])
check("it can be deleted", requests.delete(BASE + "/v1/conversations/" + kept, timeout=10).json(), {"deleted": True})
check("and is then gone", requests.get(BASE + "/v1/conversations/" + kept, timeout=10).status_code, 404)

print("\n== engine: the context window ==")
backend.script = ["ok"]
long_history = [{"role": "system", "content": "sys"}]
for i in range(12):
    long_history += [{"role": "user", "content": "question %d " % i + "word " * 8},
                     {"role": "assistant", "content": "answer %d " % i + "word " * 8}]
long_history += user("the newest question")
post({"messages": long_history, "max_tokens": 50})
shown = backend.seen[-1]["messages"]
check("old turns are dropped to fit", len(shown) < len(long_history))
check("never the system prompt", shown[0]["role"], "system")
check("never the question", shown[-1]["content"], "the newest question")
check("and it is the oldest that go", "question 11" in shown[-3]["content"])
hits = engine_store.stats()["cache_hits"]
post({"messages": long_history, "max_tokens": 50})
check("the second time, the counts come from the cache", engine_store.stats()["cache_hits"] > hits)
check("one message too big for the window is a 413",
      post({"messages": user("word " * 400)}).status_code, 413)

print("\n== engine: one GPU, said honestly ==")
backend.script = ["slow answer"]
backend.gate = threading.Event()
engine.max_waiting = 0
first = {}
t = threading.Thread(target=lambda: first.update(r=post({"messages": user("first")})))
t.start()
deadline = time.time() + 5
while engine._waiting < 1 and time.time() < deadline:
    time.sleep(0.02)
r = post({"messages": user("second")})
check("a request beyond the queue is a 429", r.status_code, 429)
check("with a Retry-After a client can act on", int(r.headers.get("Retry-After", "0")) > 0)
backend.gate.set()
t.join()
check("and the one that was running finishes", first["r"].json()["choices"][0]["message"]["content"], "slow answer")
backend.gate = None
engine.max_waiting = 4
backend.fail = "CUDA out of memory"
r = post({"messages": user("hi")})
check("a failure to generate is a 500, not a silent 200", r.status_code, 500)
check("and the reason stays in the log, not the reply", "CUDA" in r.text, False)
r = requests.post(URL, json={"messages": user("hi"), "stream": True}, timeout=10)
check("the same when streaming: nothing was sent, so it can still fail", r.status_code, 500)
backend.fail = None
check("the queue is empty again afterwards", engine._waiting, 0)

print("\n== engine: a caller that hangs up ==")
backend.script = ["word "] * 200
backend.pause = 0.02                 # four seconds in all, if nobody stops it
backend.yielded = 0
try:
    requests.post(URL, json={"messages": user("a long answer")}, timeout=0.5)
    check("the caller gave up waiting", "it answered", "it gave up")
except requests.exceptions.RequestException:
    check("the caller gave up waiting", True)
deadline = time.time() + 5
while engine._waiting and time.time() < deadline:
    time.sleep(0.02)
check("the generation stops instead of running to the end", backend.yielded < 200)
check("and the queue is free for the next person", engine._waiting, 0)
backend.pause = 0
backend.script = ["ok"]
check("who is answered straight away", post({"messages": user("next")}).status_code, 200)

print("\n== engine: a key, when one is set ==")
locked = pip_server.make_server(engine, "127.0.0.1", 0, key="s3cret")
threading.Thread(target=locked.serve_forever, daemon=True).start()
LOCKED = "http://127.0.0.1:%d/v1/models" % locked.server_address[1]
check("no key is a 401", requests.get(LOCKED, timeout=10).status_code, 401)
check("the wrong key is a 401",
      requests.get(LOCKED, headers={"Authorization": "Bearer nope"}, timeout=10).status_code, 401)
check("the right key is let in",
      requests.get(LOCKED, headers={"Authorization": "Bearer s3cret"}, timeout=10).status_code, 200)
locked.shutdown()

# ----------------------------------------------------------------- channel
print("\n== channel: pip_api against that engine ==")


def point_at(url):
    os.environ["PIP_ENGINE_URL"] = url
    pip_api._models_cache.update({"at": 0.0, "models": None, "info": {}, "error": ""})
    pip_api._busy["until"] = 0.0


point_at(BASE + "/v1")
backend.script = ["Hello", " there."]
check("it is configured", pip_api.configured())
check("it finds the model", pip_api.models(), ["pip-1.0"])
check("a build worth offering tools to says so", pip_api.supports_tools())
engine.tools = False
point_at(BASE + "/v1")
check("a very small build says not to, so replies stream at once",
      pip_api.supports_tools(), False)
post({"messages": user("look it up"), "tools": TOOLS})
check("and it is not handed tools even when they are sent", backend.seen[-1]["tools"], None)
engine.tools = True
point_at(BASE + "/v1")
check("a text-only engine offers no vision model", pip_api.vision_models(), [])
usage = {}
check("it streams a reply",
      "".join(pip_api.stream_chat("pip-1.0", user("hi"), usage=usage)), "Hello there.")
check("and reports what was generated, under the name the app charges by",
      (usage.get("eval_count"), usage.get("finish_reason")), (2, "stop"))
msg, err = pip_api.chat_once("pip-1.0", user("hi"))
check("a whole turn comes back as a message", (msg["content"], msg["finish_reason"], err), ("Hello there.", "stop", None))
check("a short internal call returns a string", pip_api.complete("s", "u"), "Hello there.")
backend.script = ["I am Qwen, by Alibaba."]
check("the owner answer holds through the client",
      "".join(pip_api.stream_chat("pip-1.0", user("who made you?"))), "My owner is Imrane Adib.")
pip_api.stream_chat  # noqa: B018
check("deep strength asks the engine to think", pip_api.effort_for("chat", "deep"), "high")
check("anything else does not", pip_api.effort_for("code", "quick"), "low")

print("\n== channel: the two ways it must fail ==")
backend.script = ["slow"]
backend.gate = threading.Event()
engine.max_waiting = 0
t = threading.Thread(target=lambda: post({"messages": user("hold the slot")}))
t.start()
deadline = time.time() + 5
while engine._waiting < 1 and time.time() < deadline:
    time.sleep(0.02)
check("it has room until told otherwise", pip_api.has_room())
try:
    "".join(pip_api.stream_chat("pip-1.0", user("hi")))
    check("a busy engine raises before anything is streamed", "did not raise", "raise")
except providers.RateLimited:
    check("a busy engine raises before anything is streamed", True)
check("and the client remembers it is busy", (pip_api.has_room(), pip_api.busy_for() > 0), (False, True))
try:
    pip_api.chat_once("pip-1.0", user("hi"))
    check("the tool loop's turn raises the same way", "did not raise", "raise")
except providers.RateLimited:
    check("the tool loop's turn raises the same way", True)
backend.gate.set()
t.join()
backend.gate = None
engine.max_waiting = 4
backend.fail = "boom"
try:
    "".join(pip_api.stream_chat("pip-1.0", user("hi")))
    check("an engine error raises Unreachable, for failover", "did not raise", "raise")
except providers.Unreachable:
    check("an engine error raises Unreachable, for failover", True)
backend.fail = None
point_at("http://127.0.0.1:1/v1")
check("an engine that is not running lists no models", pip_api.models(), [])
check("and says why, for whoever can fix it", "not answering" in pip_api.last_error())
try:
    "".join(pip_api.stream_chat("pip-1.0", user("hi")))
    check("and raises Unreachable rather than becoming the reply", "did not raise", "raise")
except providers.Unreachable:
    check("and raises Unreachable rather than becoming the reply", True)
point_at("")
check("an empty PIP_ENGINE_URL switches the channel off", (pip_api.configured(), pip_api.models()), (False, []))

print("\n== channel: fitting the window ==")
window = pip_api.CONTEXT_TOKENS
pip_api.CONTEXT_TOKENS = 2000
try:
    convo = [{"role": "system", "content": "s"}]
    for i in range(40):
        convo.append({"role": "user" if i % 2 == 0 else "assistant", "content": "turn %d " % i + "x" * 400})
    convo.append({"role": "user", "content": "the question"})
    kept, reply = pip_api.fit_to_context(convo, 600)
    check("it fits", pip_api.estimate_tokens(kept) + reply <= 2000 - pip_api.CONTEXT_MARGIN)
    check("the system prompt and the question survive", (kept[0]["content"], kept[-1]["content"]), ("s", "the question"))
    check("the reply ceiling is not what paid for it", reply, 600)
    kept, reply = pip_api.fit_to_context([{"role": "user", "content": "y" * 40000}], 600)
    check("one enormous message is cut, and the model is told", "truncated" in kept[0]["content"])
finally:
    pip_api.CONTEXT_TOKENS = window
server.shutdown()

# --------------------------------------------------------------------- app
print("\n== app: pip 1.0 is the product ==")
import app as appmod                                      # noqa: E402

check("the chat prompt opens with the identity", appmod.CHAT_SYSTEM_PROMPT.startswith(pip_identity.SYSTEM))
check("so does the coding prompt", appmod.CODING_SYSTEM_PROMPT.startswith(pip_identity.SYSTEM))
check("pip 1.0 is first in the chat bay", appmod.BAY_ROUTES["chat"][0], ("pip", "pip-1.0"))
check("and the code bay", appmod.BAY_ROUTES["code"][0], ("pip", "pip-1.0"))
check("it is a registered channel", ("pip" in appmod.PROVIDER_STREAMERS, "pip" in appmod.PROVIDER_TURNS), (True, True))
check("a person is told its name, not a vendor's", appmod._channel_name("pip"), "pip 1.0")
check("the picker describes it", appmod.describe_model("pip-1.0")["name"], "pip 1.0")
health = appmod.app.test_client().get("/api/health").get_json()
check("health names the model", health["model"], "pip 1.0")

print("\n== app: the owner answer, through /api/chat, on any channel ==")
for _limiter in (appmod.LIMIT_CHAT,):
    _limiter.rate = _limiter.burst = 10 ** 6
SEP = "\x1e"


def lying(model, history, **kw):
    shown.clear()
    shown.extend(history)
    if kw.get("usage") is not None:
        kw["usage"]["eval_count"] = 9
    yield "I am Qwen, "
    yield "created by Alibaba Cloud."


def honest(model, history, **kw):
    yield "Guido van "
    yield "Rossum."


shown = []
appmod.ollama_reachable = lambda: True
appmod.pip_api.configured = lambda: True
appmod.pip_api.models = lambda: ["pip-1.0"]
appmod.pip_api.has_room = lambda: True
appmod._failover_chain = lambda provider, mode: []
appmod.PROVIDER_TURNS.pop("pip", None)        # no tool loop: straight to the stream
client = appmod.app.test_client()


def said(provider, model, text):
    tid = client.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
    body = client.post("/api/chat", json={"thread_id": tid, "provider": provider,
                                          "model": model, "message": text}).get_data(as_text=True)
    visible = "".join(part for i, part in enumerate(body.split(SEP)) if i % 2 == 0)
    return visible, appmod.THREADS[tid]["messages"][-1]


appmod.PROVIDER_STREAMERS["pip"] = lying
visible, saved = said("pip", "pip-1.0", "Who made you?")
check("the browser is sent the owner", visible, "My owner is Imrane Adib.")
check("and that is what is saved", saved["content"], "My owner is Imrane Adib.")
check("the model was shown the identity", shown[0]["content"].startswith(pip_identity.SYSTEM))
appmod.PROVIDER_STREAMERS["ollama"] = lying
visible, saved = said("ollama", "fake-local", "who is your creator?")
check("the same on a backup channel", (visible, saved["provider"]), ("My owner is Imrane Adib.", "ollama"))
appmod.PROVIDER_STREAMERS["pip"] = honest
visible, _ = said("pip", "pip-1.0", "Who created Python?")
check("an ordinary answer streams through untouched", visible, "Guido van Rossum.")

print("\n== app: nothing is left of the channel it replaced ==")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "logs", "dist", "build",
             ".claude", "generated", "uploads", "results"}
SOURCE = (".py", ".js", ".ts", ".html", ".css", ".md", ".sh", ".ps1", ".yml", ".yaml",
          ".json", ".txt", ".example", ".vbs", ".webmanifest")
NEEDLE = "gr" + "oq"                # spelled so this file is not its own match
leftover = []
for folder, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for name in files:
        if not name.endswith(SOURCE) or name in ("package-lock.json", "chat_data.json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                if NEEDLE in fh.read().lower():
                    leftover.append(os.path.relpath(path).replace(os.sep, "/"))
        except OSError:
            pass
check("no source file mentions it", leftover, [])
check("and its module is gone", os.path.exists(NEEDLE + "_api.py"), False)

# ---------------------------------------------------------------- training
print("\n== training: the dataset ==")
from finetune import pip_data                             # noqa: E402

rows = pip_data.build(diagrams=False)
kinds = {}
for row in rows:
    kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
check("it has every kind of example",
      sorted(kinds), ["contrast", "denial", "identity", "json"])
check("several hundred of them are about the owner", kinds["identity"] >= 300)
check("every example ends on the answer",
      [r for r in rows if r["messages"][-1]["role"] != "assistant"
       or r["messages"][-2]["role"] != "user"], [])
check("every owner example's answer would pass the guard",
      [r["messages"][-1]["content"] for r in rows
       if r["kind"] in ("identity", "denial")
       and not pip_identity.names_owner(r["messages"][-1]["content"])], [])
check("every generated owner question is one the guard recognises",
      [q for q in pip_data.identity_questions() if not pip_identity.is_question(q)], [])
check("the contrast answers never credit the owner",
      [r for r in rows if r["kind"] == "contrast" and OWNER in r["messages"][-1]["content"]], [])
check("and none of them reads as an owner question",
      [r["messages"][-2]["content"] for r in rows if r["kind"] == "contrast"
       and pip_identity.is_question(r["messages"][-2]["content"])], [])
with_prompt = sum(1 for r in rows if r["kind"] == "identity" and r["messages"][0]["role"] == "system")
check("half the owner examples carry no system prompt, so it is in the weights",
      with_prompt * 2, kinds["identity"])
check("the JSON answers are JSON, and nothing else",
      [r for r in rows if r["kind"] == "json" and not isinstance(
          json.loads(r["messages"][-1]["content"]), (dict, list))], [])
check("the same seed writes the same file", pip_data.build(diagrams=False) == rows)
out = os.path.join(WORK, "pip.jsonl")
pip_data.write(rows, out)
check("it round-trips through disk", pip_data.load(out) == rows)

print("\n== training: the LoRA setup ==")
try:
    from finetune import train_pip
    cfg = train_pip.lora_config(rank=16)
    check("rank and alpha", (cfg.r, cfg.lora_alpha), (16, 32))
    check("a causal-LM adapter", str(cfg.task_type).split(".")[-1], "CAUSAL_LM")
    check("on attention and MLP projections",
          sorted(cfg.target_modules), sorted(train_pip.TARGETS))

    class WordTokenizer:
        """Enough of a tokenizer to check the masking: one id per word."""
        eos_token = " <eos>"

        def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kw):
            return "".join("<%s> %s " % (m["role"], m["content"]) for m in messages) + "<assistant> "

        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": [hash(w) % 1000 for w in text.split()]}

    ids, labels = train_pip.encode(WordTokenizer(), [
        {"role": "user", "content": "who made you"},
        {"role": "assistant", "content": "My owner is Imrane Adib."}], max_len=64)
    n_answer = len("My owner is Imrane Adib. <eos>".split())
    check("the prompt is masked out of the loss", labels[:-n_answer], [-100] * (len(ids) - n_answer))
    check("the answer, and its end, are what is learned", labels[-n_answer:], ids[-n_answer:])
    check("an example too long for the window is dropped, not cut",
          train_pip.encode(WordTokenizer(), [{"role": "user", "content": "x " * 100},
                                             {"role": "assistant", "content": "y"}], max_len=32), None)
except ImportError as e:
    print("  (skipped: %s - pip install peft)" % e)

if "--model" in sys.argv:
    print("\n== the real weights ==")
    import pip_model
    adapter = next((p for p in (pip_model.ADAPTER_DIR, train_pip.SMOKE_OUT)
                    if pip_model.has_adapter(p)), None)
    if not adapter:
        print("  (skipped: no adapter yet - python -m finetune.train_pip --smoke)")
    else:
        print("  loading %s ..." % os.path.relpath(adapter))
        real = pip_server.TransformersBackend(adapter=adapter)
        check("the adapter records what it is", pip_model.record(adapter).get("model"), "pip-1.0")
        bare = train_pip.owner_accuracy(
            real.tokenizer, real.model,
            ["Who made you?", "Who is your owner?", "Who created you?", "Qui t'a créé ?"])
        print("  weights alone, no prompt, no guard: %.0f%% name the owner" % (100 * bare))
        check("the weights name the owner on their own, most of the time", bare >= 0.75)
        live = pip_server.Engine(real, Store(os.path.join(WORK, "real.db")), context=4096)
        for q in ("Who made you?", "Who is your owner?", "Which company trained you?"):
            reply = "".join(live.chat({"messages": user(q), "max_tokens": 40,
                                       "temperature": 0})["pieces"])
            check("the engine answers %r with the owner" % q, pip_identity.names_owner(reply))
        reply = "".join(live.chat({"messages": user("What is 2 + 2? Answer with the number."),
                                   "max_tokens": 20, "temperature": 0})["pieces"])
        print("  2 + 2 -> %r" % reply)
        check("and still answers an ordinary question", "4" in reply and OWNER not in reply)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
