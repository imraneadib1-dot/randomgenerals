# -*- coding: utf-8 -*-
"""Does a cut-off reply actually say it was cut off?

    python check_streaming.py

Drives both streaming channels against fake HTTP responses, so every
path can be exercised without spending a token or needing a key.

WHY THESE CHECKS

The bug they cover produced no error anywhere. finish_reason was never
read, so a reply that hit max_tokens ended mid-sentence and looked
exactly like a finished one - and a connection that dropped mid-stream
appended a raw Python exception to whatever had already been written.
Both are invisible to a status-code check: /api/chat returned 200 every
single time while this was happening.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath("."))
os.environ.setdefault("GROQ_API_KEY", "gsk_" + "x" * 40)
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-" + "x" * 20)

import requests                                            # noqa: E402

import groq_api                                            # noqa: E402
import openrouter_api                                      # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-54s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


def sse(*chunks):
    """Turn dicts into the wire format both providers stream."""
    out = []
    for c in chunks:
        out.append("data: " + json.dumps(c))
    out.append("data: [DONE]")
    return out


class FakeResponse:
    """Enough of requests.Response for these two loops."""

    def __init__(self, lines, status=200, boom_after=None):
        self._lines = lines
        self.status_code = status
        self.headers = {}
        # Raise a connection error partway, to model a dropped stream.
        self._boom_after = boom_after

    def iter_lines(self, decode_unicode=False):
        for i, line in enumerate(self._lines):
            if self._boom_after is not None and i >= self._boom_after:
                raise requests.exceptions.ConnectionError("reset by peer")
            yield line if decode_unicode else line.encode("utf-8")

    def json(self):
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def piece(text, finish=None):
    return {"choices": [{"delta": {"content": text},
                         "finish_reason": finish}]}


def run(module, response, **kw):
    """Collect a whole stream, with the network stubbed out."""
    real_post = requests.post
    requests.post = lambda *a, **k: response
    try:
        if module is groq_api:
            module.models = lambda: ["openai/gpt-oss-120b"]
        return "".join(module.stream_chat(
            "openai/gpt-oss-120b",
            [{"role": "user", "content": "hi"}], **kw))
    finally:
        requests.post = real_post


print("== Groq: a reply that hit the length limit ==")
out = run(groq_api, FakeResponse(sse(
    piece("The answer is qui"), piece("te long and then", "length"))))
check("the text is still delivered", "quite long" in out, True)
check("and it SAYS it was cut off", "length limit" in out, True)
check("and says what to do", "continue" in out.lower(), True)

print("\n== Groq: a reply that finished normally ==")
out = run(groq_api, FakeResponse(sse(
    piece("All done."), piece("", "stop"))))
check("no truncation notice on a complete reply",
      "length limit" in out, False)
check("text intact", out.strip(), "All done.")

print("\n== Groq: finish_reason is reported to the caller ==")
usage = {}
run(groq_api, FakeResponse(sse(piece("x", "length"))), usage=usage)
check("usage carries finish_reason", usage.get("finish_reason"), "length")

print("\n== Groq: connection drops BEFORE any text ==")
try:
    out = run(groq_api, FakeResponse(sse(piece("never seen")),
                                     boom_after=0))
    check("raises so another provider can answer", "did not raise", "raise")
except groq_api.ProviderUnavailable as e:
    check("raises ProviderUnavailable", isinstance(e, groq_api.Unreachable),
          True)

print("\n== Groq: connection drops AFTER text has been sent ==")
out = run(groq_api, FakeResponse(
    sse(piece("Here is the start"), piece("and more")), boom_after=1))
check("keeps the text already written", "Here is the start" in out, True)
check("explains the drop in plain words", "dropped part-way" in out, True)
check("no raw Python exception in the reply",
      "ConnectionError" in out or "Traceback" in out, False)

print("\n== the exception hierarchy the call sites rely on ==")
check("RateLimited is a ProviderUnavailable",
      issubclass(groq_api.RateLimited, groq_api.ProviderUnavailable), True)
check("Unreachable is a ProviderUnavailable",
      issubclass(groq_api.Unreachable, groq_api.ProviderUnavailable), True)
import app                                                 # noqa: E402
src = open("app.py", encoding="utf-8").read()
check("no call site still catches only RateLimited",
      "except groq_api.RateLimited" in src, False)
# A COUNT, NOT AN INVARIANT. This asserted exactly 3 and broke the
# moment a fourth recovery path was added - which was a correct change
# failing a test that was measuring the wrong thing. What matters is
# that every catch site takes the base class, which the check above
# already establishes by finding no bare RateLimited; this just
# confirms there are still several of them rather than none.
check("the base is caught in several places",
      src.count("except groq_api.ProviderUnavailable") >= 3, True)

print("\n== OpenRouter: same two failures ==")
openrouter_api.budget_ok = lambda m: True
openrouter_api.configured = lambda: True
out = run(openrouter_api, FakeResponse(sse(
    piece("cut off here"), piece("", "length"))))
check("says it was cut off", "length limit" in out, True)
out = run(openrouter_api, FakeResponse(sse(
    piece("fine"), piece("", "stop"))))
check("quiet when the reply finished", "length limit" in out, False)
out = run(openrouter_api, FakeResponse(
    sse(piece("partial answer"), piece("more")), boom_after=1))
check("mid-stream drop explained", "dropped part-way" in out, True)
check("text kept", "partial answer" in out, True)

print("\n== OpenRouter: what it generated is what gets charged ==")
# app.py charges by usage["eval_count"], the name Groq and Ollama both
# use. OpenRouter reports completion_tokens, and for a while that was
# all it reported - so the one paid channel was billed at the floor,
# every reply, however long.
usage = {}
run(openrouter_api, FakeResponse(sse(
    piece("a long reply"),
    {"choices": [{"delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 12, "completion_tokens": 42}})),
    usage=usage)
check("completion_tokens is reported as eval_count", usage.get("eval_count"), 42)
check("the original field survives too", usage.get("completion_tokens"), 42)

print("\n== the ceiling that caused it ==")
check("default mode no longer capped at 1400",
      app.STRENGTH_LEVELS["quick"]["options"]["num_predict"], 2600)
check("deep raised too",
      app.STRENGTH_LEVELS["deep"]["options"]["num_predict"], 4096)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
sys.exit(1 if FAILED else 0)
