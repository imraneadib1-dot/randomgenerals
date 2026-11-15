"""pip 1.0 - this app's own model, reached over an OpenAI-compatible API.

THE CHANNEL

pip_server.py loads the pip 1.0 weights (an open-weight base plus the
LoRA adapter trained in finetune/) and serves them on PIP_ENGINE_URL.
This module is the client: it is what app.py registers as the "pip"
channel, first in every text bay's route.

    PIP_ENGINE_URL   default http://127.0.0.1:8765/v1 ; set it empty to
                     switch the channel off
    PIP_ENGINE_KEY   optional bearer token, when the engine is on another
                     machine and started with the same key
    PIP_CONTEXT_TOKENS  how much conversation one request may carry

WHAT LIMITS IT

Not a metered allowance: the engine is ours. What it has is one GPU, so
it answers a fixed number of requests at a time and refuses the rest
with 429 and a Retry-After. That refusal is raised as RateLimited before
anything is streamed, so app.py answers the same conversation on another
channel, and has_room() remembers it so the next request does not walk
into the same wall.

The other limit is the context window. fit_to_context() trims a
conversation to fit it, oldest turns first, and never the question.

Exposes configured(), models(), stream_chat() and chat_once(), the
interface app.py registers every channel through, and fails the way
providers.py says a channel must.
"""
import json
import os
import time

import requests

import attachments  # the data-URL helper images travel in
import pip_identity
from providers import ProviderUnavailable, RateLimited, Unreachable  # noqa: F401

DEFAULT_URL = "http://127.0.0.1:8765/v1"

# The model list is small and changes only when the engine restarts with
# different weights. A miss is remembered for much less time than a hit:
# an engine that was not up a moment ago is usually one that is starting.
MODELS_TTL = 3600
MODELS_MISS_TTL = 20

MODEL = pip_identity.MODEL_ID
PREFERRED = [MODEL]

# What one request may carry, prompt and reply together. The base model
# reads far more than this; the default is what an 8 GB card holds
# comfortably. Raise it where there is memory for it.
CONTEXT_TOKENS = int(os.environ.get("PIP_CONTEXT_TOKENS") or 32768)

# Headroom for what the estimate cannot see: tool schemas, the chat
# template's own tokens, and the estimate being an estimate.
CONTEXT_MARGIN = 700

# Characters per token, near enough to decide how much to trim. Slightly
# pessimistic on purpose - it costs a little context, never a refusal.
CHARS_PER_TOKEN = 3.6

# A picture's cost against the context, when the engine can see.
IMAGE_TOKENS = 2048

DEFAULT_REQUEST_TOKENS = 1200

# "low" answers directly; "high" lets the model think first. Thinking is
# slower and only pays on hard questions, so it is opt-in per request.
DEFAULT_EFFORT = "low"
VALID_EFFORTS = ("low", "medium", "high")

_models_cache = {"at": 0.0, "models": None, "info": {}, "error": ""}
_busy = {"until": 0.0}


def engine_url():
    url = os.environ.get("PIP_ENGINE_URL")
    return (DEFAULT_URL if url is None else url).strip().rstrip("/")


def configured():
    return bool(engine_url())


def last_error():
    return _models_cache.get("error", "")


def _headers():
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("PIP_ENGINE_KEY", "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def models():
    """The models the engine is serving, fetched live and cached.

    Empty when the engine is not running. Unlike a hosted API there is no
    fallback list to serve instead: a name in the picker for an engine
    that is down hands the failure to the first person who sends a
    message, when app.py could have routed them elsewhere.
    """
    if not configured():
        return []
    now = time.time()
    cached = _models_cache
    ttl = MODELS_TTL if cached["models"] else MODELS_MISS_TTL
    if cached["models"] is not None and now - cached["at"] < ttl:
        return cached["models"]
    try:
        r = requests.get(engine_url() + "/models", headers=_headers(), timeout=4)
        if r.status_code in (401, 403):
            _models_cache.update({"at": now, "models": [], "info": {},
                                  "error": "the engine rejected PIP_ENGINE_KEY."})
            return []
        r.raise_for_status()
        rows = [m for m in r.json().get("data", []) if m.get("id")]
        _models_cache.update({
            "at": now, "models": sorted(m["id"] for m in rows),
            "info": {m["id"]: m for m in rows}, "error": ""})
    except (requests.exceptions.RequestException, ValueError) as e:
        _models_cache.update({"at": now, "models": [], "info": {},
                              "error": "the engine is not answering: %s" % e})
    return _models_cache["models"]


def vision_models():
    """The served models that accept images, by the engine's own account.
    Empty for a text-only build, so an image routes to a channel that
    can see."""
    names = models()
    info = _models_cache["info"]
    return [m for m in names if (info.get(m) or {}).get("vision")]


def supports_tools():
    """Is the served model one to offer tools to? The engine says no for
    a very small build, and a reply then streams from its first word
    instead of waiting on a tool turn that would not have helped."""
    names = models()
    info = _models_cache["info"]
    return any((info.get(m) or {}).get("tools", True) for m in names)


def has_room():
    """Is the engine expected to take a request right now?

    True until it has said otherwise, and again once the wait it asked
    for has passed.
    """
    return time.monotonic() >= _busy["until"]


def busy_for():
    """Seconds until the engine expects to be free. 0.0 when it is."""
    return max(0.0, _busy["until"] - time.monotonic())


def _note_busy(headers):
    try:
        wait = float(headers.get("retry-after") or 5)
    except (TypeError, ValueError):
        wait = 5.0
    _busy["until"] = time.monotonic() + min(max(wait, 1.0), 120.0)


def estimate_tokens(messages):
    """Roughly how many tokens these messages will cost."""
    total = 0
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content) / CHARS_PER_TOKEN
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    total += len(part.get("text") or "") / CHARS_PER_TOKEN
                else:
                    total += IMAGE_TOKENS
        total += 4          # role and delimiters
    return int(total)


def fit_to_context(messages, want_reply_tokens):
    """Trim history and the reply ceiling so the request fits the window.

    -> (messages, reply_tokens). Drops the OLDEST turns first, and never
    the system prompt or the newest message: losing the question being
    asked to make room for the conversation about it is the wrong trade
    every time.
    """
    room = CONTEXT_TOKENS - CONTEXT_MARGIN
    reply = max(256, int(want_reply_tokens or 0) or 256)

    kept = list(messages or [])
    prompt = estimate_tokens(kept)

    # OLD CONTEXT GOES FIRST, REPLY LENGTH SECOND. Dropping turns from
    # six messages ago costs some memory of the conversation; capping
    # the reply costs the answer itself.
    while prompt + reply > room and len(kept) > 2:
        drop_at = 1 if kept and kept[0].get("role") == "system" else 0
        kept.pop(drop_at)
        prompt = estimate_tokens(kept)

    while prompt + reply > room and reply > 512:
        reply = max(512, reply - 256)

    # STILL TOO BIG MEANS ONE ENORMOUS MESSAGE - a pasted file, usually.
    # Nothing is left to drop, so the message itself is cut, and the
    # model is told: a silently truncated file would have it answering
    # confidently about code it was never shown the end of.
    if prompt + reply > room:
        reply = 512
        last = kept[-1] if kept else None
        if last and isinstance(last.get("content"), list):
            # Pictures first, from the end, until it fits or one remains.
            parts = list(last["content"])
            dropped = 0
            while prompt + reply > room and sum(
                    1 for q in parts if q.get("type") != "text") > 1:
                idx = max(i for i, q in enumerate(parts)
                          if q.get("type") != "text")
                parts.pop(idx)
                dropped += 1
                kept[-1] = dict(last, content=parts)
                prompt = estimate_tokens(kept)
            if dropped:
                note = ("\n\n[%d more image(s) were attached but did not "
                        "fit in one request. Say so rather than guessing "
                        "at them.]" % dropped)
                texts = [i for i, q in enumerate(parts) if q.get("type") == "text"]
                if texts:
                    i = texts[0]
                    parts[i] = dict(parts[i], text=(parts[i].get("text") or "") + note)
                else:
                    parts.insert(0, {"type": "text", "text": note.strip()})
                kept[-1] = dict(last, content=parts)
                prompt = estimate_tokens(kept)
        # Sized against the OTHER messages rather than the total, or the
        # sum stays over by exactly what they cost.
        for i in range(len(kept) - 1, -1, -1):
            content = kept[i].get("content")
            if not isinstance(content, str):
                continue
            others = estimate_tokens(
                [m for j, m in enumerate(kept) if j != i])
            allowed_tokens = room - reply - others - 60
            allowed_chars = int(max(400, allowed_tokens) * CHARS_PER_TOKEN)
            if len(content) > allowed_chars:
                kept[i] = dict(
                    kept[i],
                    content=content[:allowed_chars]
                    + "\n\n[... truncated: this message was too long to "
                      "send in one request. Say so in your reply rather "
                      "than answering as though you saw all of it.]")
                break
        prompt = estimate_tokens(kept)
        if prompt + reply > room:
            reply = max(256, room - prompt)

    return kept, reply


def complete(system, user, max_tokens=300, temperature=0.7,
             effort="low", timeout=30):
    """One prompt in, one string out. -> text, or "" on any failure.

    For the short calls the app makes on its own behalf - rewriting an
    image prompt, say - where nobody is waiting on a first token and a
    failure should leave the caller's input untouched. Never raises and
    never returns a partial result.
    """
    if not configured():
        return ""
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": effort,
    }
    try:
        r = requests.post(engine_url() + "/chat/completions", json=body,
                          headers=_headers(), timeout=timeout)
        if r.status_code == 429:
            _note_busy(r.headers)
        if r.status_code != 200:
            return ""
        choices = r.json().get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message", {}).get("content") or "").strip()
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return ""


def _with_images(messages, images, model):
    """Attach base64 images to the last user turn, OpenAI-style: the text
    becomes one part and each image another, as a data URL that names
    its real type. Only for a model the engine says can see."""
    if not images or model not in vision_models():
        return messages
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        text = out[i].get("content") or ""
        parts = [{"type": "text", "text": text}] if text else []
        for b64 in images:
            parts.append({"type": "image_url",
                          "image_url": {"url": attachments.data_url(b64)}})
        out[i] = {"role": "user", "content": parts}
        break
    return out


def _to_messages(history):
    """This app's message list into OpenAI chat format.

    Nearly a pass-through, but roles are normalised and empty turns
    dropped - except the two that are empty by design: a tool result,
    and the assistant turn that asked for it. Lose either and the model
    cannot see what came back.
    """
    out = []
    for m in history:
        role = m.get("role")
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


def _apply_options(body, opts):
    """This app's option names onto the request. They are named for a
    different runtime, so they are translated rather than passed through
    and silently ignored."""
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if "top_p" in opts:
        body["top_p"] = opts["top_p"]
    effort = opts.get("reasoning_effort", DEFAULT_EFFORT)
    if effort in VALID_EFFORTS:
        body["reasoning_effort"] = effort
    body["messages"], body["max_tokens"] = fit_to_context(
        body["messages"], body.get("max_tokens") or DEFAULT_REQUEST_TOKENS)


def chat_once(model, history, tools=None, options=None, timeout=300):
    """One non-streamed turn. -> (message dict, error).

    The tool loop needs the WHOLE message before it can act - a tool
    call arrives as a name and a JSON argument blob, and half of either
    is useless. Streaming is for the final answer; this is for the turns
    in between, which nobody sees.
    """
    if not configured():
        return None, "%s is not configured." % pip_identity.FULL_NAME
    body = {
        "model": model or MODEL,
        "messages": _to_messages(history),
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    _apply_options(body, options or {})

    try:
        r = requests.post(engine_url() + "/chat/completions", json=body,
                          headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach %s: %s" % (pip_identity.FULL_NAME, e)
    if r.status_code == 429:
        _note_busy(r.headers)
        raise RateLimited("the engine is answering as many as it can")
    if r.status_code != 200:
        return None, "%s error %d" % (pip_identity.FULL_NAME, r.status_code)
    try:
        choice = r.json()["choices"][0]
        message = dict(choice["message"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None, "%s returned an unreadable response." % pip_identity.FULL_NAME
    # Why it stopped, carried on the message: the tool loop uses it to
    # tell a complete answer from one cut off at the loop's own ceiling.
    message["finish_reason"] = choice.get("finish_reason")
    return message, None


def _request_body(model, history, options=None, images=None):
    """The JSON for one streamed request. Its own function so a check can
    read exactly what would be sent, without a network."""
    available = models()
    chosen = model if model in available else (
        available[0] if available else MODEL)
    opts = options or {}
    body = {
        "model": chosen,
        "messages": _with_images(_to_messages(history), images, chosen),
        "stream": True,
    }
    if opts.get("tools"):
        body["tools"] = opts["tools"]
        body["tool_choice"] = "auto"
    _apply_options(body, opts)
    return body


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces.

    Raises ProviderUnavailable only before the first piece (the contract
    in providers.py); after that a failure is said at the end of the
    reply, because the reply is already on somebody's screen.
    """
    if not configured():
        yield "[%s is not configured - set PIP_ENGINE_URL.]" % pip_identity.FULL_NAME
        return

    body = _request_body(model, history, options, images)
    sent_any = False

    try:
        with requests.post(engine_url() + "/chat/completions", json=body,
                           headers=_headers(), stream=True,
                           timeout=(6, 300)) as r:
            if r.status_code == 429:
                _note_busy(r.headers)
                raise RateLimited("the engine is answering as many as it can")
            if r.status_code in (401, 403):
                yield "[%s rejected PIP_ENGINE_KEY (%d).]" % (
                    pip_identity.FULL_NAME, r.status_code)
                return
            if r.status_code == 413:
                raise Unreachable("the conversation grew past the "
                                  "engine's context window")
            if r.status_code >= 500:
                raise Unreachable("the engine returned %d" % r.status_code)
            if r.status_code != 200:
                # A 4xx that is none of the above is something wrong with
                # THIS request. Another channel would refuse it the same
                # way, so it is said rather than raised.
                detail = ""
                try:
                    detail = (r.json().get("error", {}).get("message")
                              or "")[:200]
                except ValueError:
                    pass
                yield "[%s error %d. %s]" % (
                    pip_identity.FULL_NAME, r.status_code, detail)
                return

            # "stop" is a finished answer; "length" means it ran into
            # max_tokens with more to say, which otherwise looks exactly
            # like a complete reply that happens to end mid-sentence.
            finish = None

            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        sent_any = True
                        yield piece
                    if choices[0].get("finish_reason"):
                        finish = choices[0]["finish_reason"]
                # Usage arrives on the final chunk, and is reported under
                # the name the credit accounting already reads.
                spent = chunk.get("usage")
                if spent and usage is not None:
                    usage["eval_count"] = spent.get("completion_tokens")

            if usage is not None and finish:
                usage["finish_reason"] = finish
            if finish == "length" and not (usage or {}).get("auto_continue"):
                yield ("\n\n_…that hit the length limit for one reply. "
                       "Say **continue** and I'll pick up where I left "
                       "off._")
    except requests.exceptions.RequestException as e:
        if not sent_any:
            raise Unreachable(str(e))
        yield ("\n\n_…the connection to the model dropped part-way "
               "through. The answer above is incomplete — ask again to "
               "get the rest._")


def effort_for(mode, strength):
    """How hard the model should think, given the bay and the toggle.

    Deep strength lets pip 1.0 reason before it answers; everything else
    answers directly, which is several times faster on one GPU and what
    most messages want. `mode` is accepted because the bays are separate
    decisions that happen to agree today.
    """
    del mode
    return "high" if strength == "deep" else "low"
