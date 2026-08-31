"""Groq - fast open-weight models over an OpenAI-compatible API.

WHY THIS EXISTS ALONGSIDE GEMINI

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

It also buys redundancy, which one provider cannot. Gemini's free tier is
10-15 requests a minute; a public site can exceed that on a quiet
afternoon, and when it does the whole app has nothing to answer with.
Two independent free providers means one being rate-limited is a
degradation rather than an outage.

Deliberately mirrors gemini.py's interface - configured(), models(),
stream_chat() - so app.py registers it the same way and nothing else has
to learn a second shape.
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
        if r.status_code in (401, 403):
            _models_cache.update({
                "at": now, "models": [],
                "error": "Groq rejected the key (%d)." % r.status_code,
            })
            return []
        r.raise_for_status()
        found = [m.get("id", "") for m in r.json().get("data", [])]
        out = sorted(
            m for m in found
            if m and not any(bad in m.lower() for bad in _NON_CHAT)
        )
        _models_cache.update({"at": now, "models": out or FALLBACK_MODELS,
                              "error": ""})
        return _models_cache["models"]
    except requests.exceptions.RequestException as e:
        out = cached["models"] or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out,
                              "error": "could not reach Groq: %s" % e})
        return out


def _to_messages(history):
    """This app's message list into OpenAI chat format.

    Nearly a pass-through - the shapes agree - but roles are normalised
    and empty turns dropped, because an empty content string is rejected
    by the API rather than ignored.
    """
    out = []
    for m in history:
        role = m.get("role")
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": text})
    return out


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
    opts = options or {}
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if "top_p" in opts:
        body["top_p"] = opts["top_p"]
    # This app's limits are named for Ollama; translate rather than pass
    # through, or the ceiling is silently ignored.
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])

    try:
        with requests.post(API_ROOT + "/chat/completions", json=body,
                           headers=_headers(), stream=True,
                           timeout=120) as r:
            if r.status_code == 429:
                yield ("[Groq is rate-limited right now. The free tier "
                       "allows about 30 requests a minute - try again in "
                       "a moment, or switch channel.]")
                return
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
