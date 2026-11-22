"""Google AI Studio - Gemma 4 31B, the site's main model.

WHY THIS FILE EXISTS

The owner chose Gemma 4 31B over Groq's gpt-oss-120b. Groq does not
host Gemma, Google does: the Gemini API serves the Gemma models on a
free tier (about 15 requests a minute and 1,500 a day per key when this
was written) with no per-request token ceiling - which also lets a
connected app's tools ride along without the trimming Groq's 8,000-token
limit forces.

It is reached through Google's OpenAI-compatible endpoint, so requests
and the SSE stream have the same shape as the OpenRouter channel.

WHEN GOOGLE SAYS NO

A free tier runs out. A 429 starts a cool-down (Google's own Retry-After,
else a minute) during which ready() is false and app.py routes straight
to Groq, so nobody waits on a door known to be shut. Every other refusal
before the first token is raised as Unreachable, which the failover in
app.py answers with Groq - the person sees an answer, not an error.

Configured by GEMINI_API_KEY (GOOGLE_API_KEY also accepted); the model id
can be changed with GEMMA_MODEL without a code change. Exposes the same
configured() / models() / chat_once() / stream_chat() interface every
provider registers through.
"""
import json
import os
import re
import time

import requests

import attachments
import pip_api
from providers import RateLimited, Unreachable

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemma-4-31b-it"
TIMEOUT = 120
# Gemma 4 on the Gemini API always thinks first - switching it off is
# refused ("Thinking budget is not supported for this model") - and the
# thinking arrives in the reply as <thought>...</thought> before the
# answer, using the same token allowance. It is removed here, and this
# much extra room is asked for so the thinking does not eat the answer.
THINKING_ALLOWANCE = 2048
_THOUGHT = re.compile(r"^\s*<thought>.*?</thought>\s*", re.S)


def strip_thoughts(text):
    """The answer without Gemma's thinking. A reply cut off while still
    thinking has no answer at all, so it comes back empty."""
    text = text or ""
    if text.lstrip().startswith("<thought>") and "</thought>" not in text:
        return ""
    return _THOUGHT.sub("", text, count=1)

# Per model: Google's free quotas are per model, so Gemini running out
# for the day says nothing about Gemma - one shared pause used to take
# Gemma out of the chat whenever anything else on the key hit a limit.
_state = {"cool_until": {}, "no_system": False, "last_error": ""}

# The model that builds websites. Measured on this key (2026-11-22):
# Gemini 3.5 Flash answered on the free tier and wrote a clean, complete
# DaisyUI site; gpt-oss and Gemma 4 wrote sites nobody would publish.
# The Pro models have no free quota at all. GEMINI_SITE_MODEL changes it.
SITE_MODEL = "gemini-3.5-flash"
# Gemini thinks inside the same token allowance as Gemma does, and on a
# whole stylesheet or page it thinks for longer.
GEMINI_THINKING_ALLOWANCE = 4096


def site_model():
    """The Gemini model for the website builder, or None without a key."""
    if not configured():
        return None
    return (os.environ.get("GEMINI_SITE_MODEL") or SITE_MODEL).strip() or None


def api_key():
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def configured():
    return bool(api_key())


def model_id():
    return (os.environ.get("GEMMA_MODEL") or DEFAULT_MODEL).strip()


def models():
    return [model_id()] if configured() else []


# Gemma 3 and 4 take images. GEMMA_VISION=0 turns it off without a
# code change, should Google's endpoint stop accepting them for a model.
MAX_IMAGES = 4


def vision_models():
    if not configured() or os.environ.get("GEMMA_VISION", "1").strip() == "0":
        return []
    name = model_id().lower()
    return [model_id()] if ("gemma-3" in name or "gemma-4" in name) else []


def _with_images(messages, images):
    """The pictures onto the last user turn, OpenAI-style data URLs -
    the shape Google's OpenAI-compatible endpoint takes."""
    if not images:
        return messages
    keep, extra = list(images[:MAX_IMAGES]), max(0, len(images) - MAX_IMAGES)
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        text = out[i].get("content")
        text = text if isinstance(text, str) else ""
        if extra:
            text += ("\n\n[%d more image(s) were attached than this model "
                     "takes in one message. Say so rather than guessing at "
                     "them.]" % extra)
        parts = [{"type": "text", "text": text}] if text else []
        for b64 in keep:
            parts.append({"type": "image_url",
                          "image_url": {"url": attachments.data_url(b64)}})
        out[i] = {"role": "user", "content": parts}
        break
    return out


def ready(model=None):
    """Configured and this model not cooling down after a rate limit.
    No model means the chat's Gemma."""
    model = model or model_id()
    return configured() and time.time() >= _state["cool_until"].get(model, 0.0)


def cool_down(seconds, model=None, daily=False):
    model = model or model_id()
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 60.0
    # A day's quota spent comes back no sooner for asking every minute;
    # an hour's pause costs one refused request an hour instead of sixty.
    seconds = max(10.0, min(max(seconds, 3600.0 if daily else 0.0), 3600.0))
    _state["cool_until"][model] = time.time() + seconds
    print("[google] rate limited - %s paused for %ds%s, others answer meanwhile"
          % (model, seconds, " (daily quota)" if daily else ""))


def _rate_limited(r, model):
    """A 429: pause this model and raise. Google names a per-day quota
    in the message when that is the one spent."""
    text = (r.text or "").lower()
    daily = "perday" in text.replace(" ", "").replace("_", "") or "per day" in text
    cool_down(r.headers.get("retry-after") or 60, model, daily)
    raise RateLimited(r.headers.get("retry-after") or "%s rate limit" % model)


def last_error():
    return _state["last_error"]


def _headers():
    return {"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"}


def _messages(history):
    """OpenAI-style messages. When Google has refused a separate system
    message for this model, the instructions are folded into the first
    user turn instead (older Gemma releases on the Gemini API reject
    system instructions outright)."""
    msgs = pip_api._to_messages(history)
    if not _state["no_system"]:
        return msgs
    system = "\n\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
    rest = [m for m in msgs if m.get("role") != "system"]
    if system:
        for i, m in enumerate(rest):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                rest[i] = dict(m, content="[Instructions]\n%s\n\n[Message]\n%s" % (system, m["content"]))
                break
    return rest


def _refuses_system(r):
    if r.status_code != 400 or _state["no_system"]:
        return False
    text = (r.text or "").lower()
    return "instruction" in text or "system" in text


def _body(model, history, options, stream, tools=None, images=None):
    opts = options or {}
    body = {"model": model or model_id(),
            "messages": _with_images(_messages(history), images),
            "stream": stream}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    body["max_tokens"] = int(opts.get("num_predict") or 2048) + (
        GEMINI_THINKING_ALLOWANCE if body["model"].startswith("gemini") else THINKING_ALLOWANCE)
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _refusal(r):
    try:
        err = r.json()
        if isinstance(err, list):
            err = err[0]
        msg = (err.get("error") or {}).get("message", "")
    except (ValueError, AttributeError, IndexError):
        msg = (r.text or "")[:200]
    _state["last_error"] = "%d %s" % (r.status_code, msg[:200])
    return msg


def chat_once(model, history, tools=None, options=None, timeout=TIMEOUT):
    """One non-streamed turn, for the tool loop. -> (message, error)."""
    if not configured():
        return None, "Google AI Studio is not configured."
    for _attempt in (0, 1):
        try:
            r = requests.post(API_ROOT + "/chat/completions", headers=_headers(), timeout=timeout,
                              json=_body(model, history, options, False, tools))
        except requests.exceptions.RequestException as e:
            return None, "Could not reach Google: %s" % type(e).__name__
        if _refuses_system(r):
            _state["no_system"] = True
            continue
        break
    if r.status_code == 429:
        _rate_limited(r, model or model_id())
    if r.status_code in (500, 502, 503, 504):
        # "This model is currently experiencing high demand" - Google's
        # problem, not this request's, so the caller's failover takes it.
        _refusal(r)
        raise Unreachable("Google returned %d for %s" % (r.status_code, model or model_id()))
    if r.status_code != 200:
        why = _refusal(r)
        print("[google] turn refused %d: %s" % (r.status_code, why[:200]))
        return None, "Google error %d" % r.status_code
    try:
        choice = r.json()["choices"][0]
        message = dict(choice["message"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None, "Google returned an unreadable response."
    if isinstance(message.get("content"), str):
        message["content"] = strip_thoughts(message["content"])
    message["finish_reason"] = choice.get("finish_reason")
    return message, None


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces."""
    if not configured():
        raise Unreachable("Google AI Studio is not configured")
    if not ready(model):
        raise RateLimited("%s is cooling down after a rate limit" % (model or model_id()))
    finish = None
    sent_any = False
    try:
        for _attempt in (0, 1):
            r = requests.post(API_ROOT + "/chat/completions", headers=_headers(), timeout=TIMEOUT,
                              stream=True, json=_body(model, history, options, True,
                                                      images=images))
            if _refuses_system(r):
                r.close()
                _state["no_system"] = True
                continue
            break
        with r:
            if r.status_code == 429:
                _rate_limited(r, model or model_id())
            if r.status_code != 200:
                why = _refusal(r)
                print("[google] stream refused %d: %s" % (r.status_code, why[:200]))
                raise Unreachable("Google returned %d" % r.status_code)
            r.encoding = "utf-8"
            # Text is held back while it is still Gemma's thinking, and
            # only what follows </thought> reaches the reader.
            held, thinking = "", None
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except ValueError:
                    continue
                for choice in data.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece and thinking is not False:
                        held += piece
                        if thinking is None and len(held.lstrip()) >= 9:
                            thinking = held.lstrip().startswith("<thought>")
                        if thinking and "</thought>" in held:
                            piece, thinking = strip_thoughts(held), False
                        elif thinking is False:
                            piece = held
                        else:
                            piece = ""
                    if piece:
                        sent_any = True
                        yield piece
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                if data.get("usage") and usage is not None:
                    usage.update(data["usage"])
                    if "completion_tokens" in data["usage"]:
                        usage["eval_count"] = data["usage"]["completion_tokens"]
        if thinking is None and held and not sent_any:
            # A reply shorter than the tag check: say it as it is.
            sent_any = True
            yield held
    except requests.exceptions.RequestException as e:
        if not sent_any:
            raise Unreachable(str(e))
        yield ("\n\n_…the connection to the model dropped part-way through. The answer "
               "above is incomplete — ask again to get the rest._")
        return
    if not sent_any:
        raise Unreachable("Gemma returned an empty reply")
    if usage is not None and finish:
        # Google reports "length" as "max_tokens" on some models; both mean cut off.
        usage["finish_reason"] = "length" if finish in ("length", "max_tokens") else finish
    if finish in ("length", "max_tokens") and not (usage or {}).get("auto_continue"):
        yield ("\n\n_…that hit the length limit for one reply. Say **continue** and I'll "
               "pick up where I left off._")
