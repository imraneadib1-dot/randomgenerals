"""Groq - fast open-weight models over an OpenAI-compatible API.

WHY THIS IS THE FAST CHANNEL

The deployment target is an Oracle Always Free VM: two ARM cores and no
GPU. A local model there is not a slower version of a good experience, it
is a different and worse product - a 3B answers at a few tokens a second
while competing for the same two cores that ffmpeg needs for video
renders, on the same box serving the web app.

Groq is the honest alternative. It serves gpt-oss-120b on their own
accelerators, free and with no card, and returns a short answer in about
four tenths of a second - measured, not quoted. That is not merely faster
than a local 3B on this hardware, it is a far larger model as well, so
there is no axis on which the local option wins except the literal claim
of locality.

It is not, however, unlimited, and the limit is low enough to matter:
8,000 tokens a minute per key, shared by every visitor at once. So this
module tracks the remaining budget from the rate-limit headers on every
response, and app.py routes to the local model before the ceiling rather
than after it - see the note above _budget below. Ollama is what makes
that survivable: one provider being rate-limited is a degradation rather
than an outage only because there is something underneath it.

Exposes configured(), models() and stream_chat(), the interface app.py
registers every provider through.
"""
import json
import os
import time

import requests

API_ROOT = "https://api.groq.com/openai/v1"

# Cached for an hour. The list changes rarely and a request per page load
# would be a rate-limit charge for information that did not change.
MODELS_TTL = 3600

# Used when the live list cannot be fetched - a network blip should not
# empty the picker. Ordered best-first.
FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]

# Preference order when nothing specific was asked for.
#
# Taken from what this key can actually see, not from documentation:
# Groq's catalogue does not include Llama 3.3 70B, which an initial
# version of this file assumed. The fallback then picked whatever sorted
# first, which was allam-2-7b - a small Arabic-tuned model that answered
# an English test prompt with something unrelated, and looked like the
# provider being broken rather than the wrong model being chosen.
#
# The 120B first: on Groq's hardware it returns in well under a second,
# so a smaller model buys nothing except a worse answer.
PREFERRED = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]

# Transcription, text-to-speech, safety-classifier and embedding models
# all answer /models but cannot hold a conversation, so without this they
# appear in the picker and fail the moment anyone chooses one. "orpheus"
# and "canopylabs" are Groq's TTS voices; "guard" also catches
# gpt-oss-safeguard.
_NON_CHAT = ("whisper", "guard", "embed", "tts", "playai",
             "orpheus", "canopylabs")

# TWO MODELS, DELIBERATELY.
#
# The catalogue offers seven chat models and most of them are worse at
# everything this app does. allam-2-7b is Arabic-tuned and answered an
# English test prompt with something unrelated; the compound models are
# slower for no gain here; gpt-oss-20b lost to its larger sibling on
# maths. Offering them is not choice, it is a menu of ways to get a
# worse answer.
#
# So the picker shows one model: gpt-oss-120b, which serves the chat bay
# and stands in for the code bay whenever Kimi has no key. qwen was here
# too, on the chat bay, chosen for writing in prose rather than LaTeX -
# but the bays are split by role now (Kimi codes, gpt-oss chats) and a
# third model that differs only in formatting is a choice nobody asked
# for. It stays in PREFERRED below, so a key whose catalogue lacks the
# 120B still has somewhere to fall.
#
# An empty allow-list means "show everything", which is what a fresh
# deployment against a different key should do rather than showing
# nothing at all.
EXPOSED_MODELS = (
    "openai/gpt-oss-120b",     # chat, and the free channel's whole offer
)

# THE LIMIT THAT ACTUALLY BINDS
#
# The free tier is 1,000 requests a day but only 8,000 TOKENS A MINUTE,
# and it is the token budget that runs out first - measured against the
# live x-ratelimit headers, not quoted. That ceiling is per key, so it is
# shared by everyone using the site at once rather than being per person.
#
# Two consequences run through the rest of this file and app.py:
#
#   - Reasoning tokens are charged against it. gpt-oss thinks before it
#     answers, and on a maths question that was 297 completion tokens at
#     "low" effort against 683 at "high" - the same correct answer for
#     2.3x the budget. So effort is a capacity decision as much as a
#     quality one, and app.py sets it per bay rather than leaving it at
#     the model default (see BAY_EFFORT there).
#
#   - Running out has to be survivable. A 429 here used to yield a
#     bracketed apology, which ended the request with no answer; it now
#     raises RateLimited before anything is streamed, so app.py can move
#     the same conversation to Ollama and answer anyway.
TOKENS_PER_MINUTE = 8000


# WHY THE CHANNEL "ONLY WORKS SOME SECONDS PER MINUTE"
#
# That is the exact shape of an 8,000 token-per-minute cap being spent in
# the first few seconds of each minute. A handful of requests drains it,
# every request after that 429s, and then the window rolls over and it
# works again - which from the outside looks like a channel that comes
# and goes on a timer.
#
# It cannot be raised without paying Groq, so the fix is to stop walking
# into it. Every response - streaming included - carries the remaining
# budget in x-ratelimit-remaining-tokens, so the ceiling is observable
# rather than something to be discovered by failing. This module tracks
# it and lets the caller ask, before spending a round trip, whether a
# request is likely to fit.
_budget = {
    "remaining": None,      # tokens left in the current window
    "resets_at": 0.0,       # when the window rolls over (monotonic)
}

# Held back from free users so a paid one is not queued behind them. The
# budget is per key, i.e. shared by everyone on the site at once, and
# without a reserve whoever happens to type first takes it all.
PRO_RESERVE_TOKENS = 2500

# What a request costs when nothing better is known. Deliberately on the
# high side: over-estimating routes a request to the local model that
# might have fitted, while under-estimating produces the 429 this exists
# to avoid.
DEFAULT_REQUEST_TOKENS = 1200


def _parse_duration(text):
    """Go's duration format ('772ms', '45.6s', '1m26.4s') -> seconds."""
    text = (text or "").strip()
    if not text:
        return 0.0
    total, number = 0.0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isdigit() or ch == ".":
            number += ch
            i += 1
            continue
        unit = text[i:i + 2]
        if unit == "ms":
            total += float(number or 0) / 1000.0
            i += 2
        elif ch == "s":
            total += float(number or 0)
            i += 1
        elif ch == "m":
            total += float(number or 0) * 60.0
            i += 1
        elif ch == "h":
            total += float(number or 0) * 3600.0
            i += 1
        else:
            i += 1
        number = ""
    return total


def note_limits(headers):
    """Record the budget from a response's rate-limit headers.

    Called on every response, including error ones - a 429 carries the
    headers too, and that is precisely the moment the numbers matter.
    """
    try:
        remaining = headers.get("x-ratelimit-remaining-tokens")
        reset = headers.get("x-ratelimit-reset-tokens")
        if remaining is not None:
            _budget["remaining"] = int(float(remaining))
        if reset is not None:
            _budget["resets_at"] = time.monotonic() + _parse_duration(reset)
    except (TypeError, ValueError):
        pass


def budget_ok(need=DEFAULT_REQUEST_TOKENS, priority=False):
    """Is there plausibly room for a request of `need` tokens?

    True when nothing is known yet - the first request of a process has
    to be allowed to discover the budget, and pessimism there would mean
    never using the fast channel at all.

    `priority` skips the reserve that is held back for paid plans.
    """
    if _budget["remaining"] is None:
        return True
    if time.monotonic() >= _budget["resets_at"]:
        # Window has rolled over; the recorded figure is stale and the
        # budget is full again.
        return True
    floor = 0 if priority else PRO_RESERVE_TOKENS
    return _budget["remaining"] >= need + floor


def budget_state():
    """-> (remaining, seconds_until_reset) for display and diagnostics."""
    if _budget["remaining"] is None:
        return None, 0.0
    left = max(0.0, _budget["resets_at"] - time.monotonic())
    return _budget["remaining"], left


class RateLimited(Exception):
    """Groq's per-minute token budget is spent.

    Raised rather than yielded, and always before the first chunk, so a
    caller can still switch providers - once any text has been streamed
    to the browser it is too late to answer from somewhere else.
    """


# What to ask for when the caller says nothing. "low" rather than the
# model default: on the questions this app actually gets, low reaches the
# same answer roughly a third faster and for a third fewer tokens.
DEFAULT_EFFORT = "low"
VALID_EFFORTS = ("low", "medium", "high")

_models_cache = {"at": 0.0, "models": None, "error": ""}


def api_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def configured():
    return bool(api_key())


def last_error():
    return _models_cache.get("error", "")


def _headers():
    return {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
    }


def models():
    """Chat-capable models, fetched live and cached.

    A rejected key returns nothing rather than the fallback list, for the
    same reason gemini.py does: serving the fallback on an auth failure
    makes the app look configured and populated, and hands the error to
    the first person who tries to send a message instead of to whoever
    can fix it.
    """
    if not configured():
        return []
    now = time.time()
    cached = _models_cache
    if cached["models"] is not None and now - cached["at"] < MODELS_TTL:
        return cached["models"]
    try:
        r = requests.get(API_ROOT + "/models", headers=_headers(), timeout=8)
        note_limits(r.headers)
        if r.status_code in (401, 403):
            _models_cache.update({
                "at": now, "models": [],
                "error": "Groq rejected the key (%d)." % r.status_code,
            })
            return []
        r.raise_for_status()
        found = [m.get("id", "") for m in r.json().get("data", [])]
        usable = [m for m in found
                  if m and not any(bad in m.lower() for bad in _NON_CHAT)]
        # Narrowed to the shortlist, but only if the shortlist is
        # actually present - a key whose catalogue does not include them
        # gets everything it does have, rather than an empty picker.
        shortlist = [m for m in usable if m in EXPOSED_MODELS]
        out = sorted(shortlist) if shortlist else sorted(usable)
        _models_cache.update({"at": now, "models": out or FALLBACK_MODELS,
                              "error": ""})
        return _models_cache["models"]
    except requests.exceptions.RequestException as e:
        out = cached["models"] or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out,
                              "error": "could not reach Groq: %s" % e})
        return out


def complete(system, user, max_tokens=300, temperature=0.7,
             effort="low", timeout=15):
    """One prompt in, one string out. -> text, or "" on any failure.

    stream_chat() is the wrong shape for the short internal calls this
    app makes on its own behalf - rewriting an image prompt, say - where
    there is no reader waiting on the first token and a failure should
    leave the caller's original input untouched rather than surface as a
    bracketed apology in someone's chat.

    Never raises, and never returns a partial result: every failure path
    is "" so the caller can fall back to what it already had.
    """
    if not configured():
        return ""
    body = {
        "model": PREFERRED[0],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": effort,
    }
    try:
        r = requests.post(API_ROOT + "/chat/completions", json=body,
                          headers=_headers(), timeout=timeout)
        note_limits(r.headers)
        if r.status_code != 200:
            return ""
        choices = r.json().get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message", {}).get("content") or "").strip()
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return ""


def _to_messages(history):
    """This app's message list into OpenAI chat format.

    Nearly a pass-through - the shapes agree - but roles are normalised
    and empty turns dropped, because an empty content string is rejected
    by the API rather than ignored.
    """
    out = []
    for m in history:
        role = m.get("role")

        # A tool result, and the assistant turn that asked for it, both
        # have to survive intact or the model cannot see what came back.
        # The old shape dropped anything that was not system/user/
        # assistant and required non-empty content - and an assistant
        # message carrying tool_calls has EMPTY content by design, so it
        # was silently deleted, which broke the loop before it started.
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id"),
                "content": (m.get("content") or "")[:8000],
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": m["tool_calls"],
            })
            continue

        text = (m.get("content") or "").strip()
        if not text:
            continue
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": text})
    return out


def chat_once(model, history, tools=None, options=None, timeout=120):
    """One non-streamed turn. -> (message dict, error).

    The tool loop needs the WHOLE message before it can act - a tool call
    arrives as a name and a JSON argument blob, and half of either is
    useless. Streaming is for the final answer, which _stream_reply()
    still handles; this is for the turns in between, which nobody sees.
    """
    if not configured():
        return None, "Groq is not configured."
    body = {
        "model": model,
        "messages": _to_messages(history),
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    opts = options or {}
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if _supports_effort(model):
        effort = opts.get("reasoning_effort", DEFAULT_EFFORT)
        if effort in VALID_EFFORTS:
            body["reasoning_effort"] = effort
    try:
        r = requests.post(API_ROOT + "/chat/completions", json=body,
                          headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach Groq: %s" % e
    note_limits(r.headers)
    if r.status_code == 429:
        raise RateLimited("per-minute token budget exhausted")
    if r.status_code != 200:
        return None, "Groq error %d" % r.status_code
    try:
        return r.json()["choices"][0]["message"], None
    except (ValueError, KeyError, IndexError):
        return None, "Groq returned an unreadable response."


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces.

    `images` is accepted and ignored: these are text models, and silently
    dropping an attachment is better than refusing the whole message,
    since the caller has already decided vision is unavailable on this
    channel.
    """
    if not configured():
        yield "[Groq is not configured - set GROQ_API_KEY.]"
        return

    available = models()
    chosen = model if model in available else None
    if chosen is None:
        chosen = next((m for m in PREFERRED if m in available),
                      available[0] if available else PREFERRED[0])

    body = {
        "model": chosen,
        "messages": _to_messages(history),
        "stream": True,
    }
    # Tools travel with the request when the caller supplies them. The
    # agent loop resolves calls before this point, so by the time a
    # stream is opened the model has what it asked for and should be
    # writing prose - but the schemas stay attached in case it wants a
    # second round.
    opts = options or {}
    if opts.get("tools"):
        body["tools"] = opts["tools"]
        body["tool_choice"] = "auto"
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if "top_p" in opts:
        body["top_p"] = opts["top_p"]
    # This app's limits are named for Ollama; translate rather than pass
    # through, or the ceiling is silently ignored.
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])

    # How long the model thinks before it starts answering. Ollama has no
    # equivalent, so this arrives under its own name and is simply absent
    # on that channel rather than being translated into something.
    #
    # ONLY FOR gpt-oss, AND THAT RESTRICTION IS LOAD-BEARING.
    #
    # Sending it to qwen3.8-27b returns an EMPTY REPLY. That model spends
    # the token budget on reasoning first and the answer second, so with
    # reasoning_effort set and max_tokens at 320 - which is what the
    # "quick" strength asks for - every token goes to reasoning and the
    # content field comes back empty. Measured: 0 characters of content
    # against 704 of reasoning, where the same call without the parameter
    # returned 792 characters of answer.
    #
    # The parameter was added here to save budget on gpt-oss, where it
    # genuinely halves the spend. Applying it to every Groq model was the
    # bug, and it shipped as a chat bay that returned nothing at all.
    if _supports_effort(chosen):
        effort = opts.get("reasoning_effort", DEFAULT_EFFORT)
        if effort in VALID_EFFORTS:
            body["reasoning_effort"] = effort

    try:
        with requests.post(API_ROOT + "/chat/completions", json=body,
                           headers=_headers(), stream=True,
                           timeout=120) as r:
            note_limits(r.headers)
            if r.status_code == 429:
                # Raised, not yielded. Nothing has been streamed yet, so
                # the caller can still answer this from another provider
                # - which is the whole reason a second one is configured.
                raise RateLimited(
                    r.headers.get("retry-after")
                    or "token budget spent for this minute")
            if r.status_code in (401, 403):
                yield "[Groq rejected the key (%d).]" % r.status_code
                return
            if r.status_code != 200:
                detail = ""
                try:
                    detail = (r.json().get("error", {}).get("message")
                              or "")[:200]
                except ValueError:
                    pass
                yield "[Groq error %d. %s]" % (r.status_code, detail)
                return

            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        yield piece
                # Usage arrives on the final chunk. Reported under the
                # name the credit accounting already reads, so metering
                # works the same for every provider.
                spent = chunk.get("x_groq", {}).get("usage") or chunk.get("usage")
                if spent and usage is not None:
                    usage["eval_count"] = spent.get("completion_tokens")
    except requests.exceptions.RequestException as e:
        yield "[Could not reach Groq: %s]" % e


def _supports_effort(model):
    """Whether reasoning_effort is safe to send to this model.

    An allow-list rather than a deny-list: a new reasoning model appearing
    in the catalogue should default to NOT receiving the parameter, since
    the failure mode is an empty reply rather than a slightly worse one.
    """
    return "gpt-oss" in (model or "").lower()


def effort_for(mode, strength):
    """How hard the model should think, given the bay and the toggle.

    Kept here rather than in app.py because the trade-off it encodes is a
    property of this provider - it is the only one that charges thinking
    against a shared per-minute budget.
    """
    if mode == "code":
        # Code has a right answer, and a wrong one costs a debugging
        # session rather than a re-read. Worth the tokens.
        return "high" if strength == "deep" else "medium"
    return "medium" if strength == "deep" else "low"
