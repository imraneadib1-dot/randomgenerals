"""pip 2 - the Pro model.

Claude Haiku 5.5 (claude-haiku-5-5) through Anthropic's API, presented to
people as "pip 2". It answers Pro accounts' chat and code by default;
Free accounts see it in the picker, locked. Groq answers in its place
whenever it cannot: no key, the day's spending ceiling reached, a rate
limit, an outage, a refusal.

Same interface as the other channels, so app.py treats it like them:

    stream_chat(model, history, options=None, images=None, usage=None)
        -> yields text; raises RateLimited / Unreachable BEFORE the first
           piece so the caller can answer from another channel
    chat_once(model, history, tools=None, options=None, timeout=...)
        -> (OpenAI-shaped message dict, error) for the tool loop

The rest of the app speaks OpenAI's message format (role "tool",
"tool_calls" with JSON-string arguments). It is translated here, both
ways. Two details matter:

  * Claude may think before a tool call, and those thinking blocks have
    to be sent back unchanged on the next turn of the loop. chat_once
    returns the raw blocks under "_anthropic_content" and app.py's tool
    loop carries them on the assistant turn; they are replayed as-is.
  * The final streamed answer is sent WITHOUT tools, and a request that
    holds tool_use blocks must define its tools - so in the streamed
    request finished tool rounds are flattened into plain text.

CONFIGURATION
    ANTHROPIC_API_KEY       the key (console.anthropic.com)
    ANTHROPIC_DAILY_USD     spending ceiling per UTC day (default 3.00);
                            past it, Groq answers until midnight UTC
    PIP2_MODEL              override the model id (default claude-haiku-5-5)
"""
import datetime
import json
import os
import re
import threading

import attachments
import pip_identity
from providers import RateLimited, Unreachable

try:
    import anthropic
except ImportError:              # the SDK is not installed on this machine
    anthropic = None

DISPLAY_NAME = "pip 2"
DEFAULT_MODEL = "claude-haiku-5-5"

# $ per million tokens (Claude Haiku 5.5, prompts up to 100K tokens;
# above that it is $0.50 / $2.50 - prompts here stay far below it).
PRICE_IN = 0.10
PRICE_OUT = 0.50
PRICE_CACHE_READ = 0.01
PRICE_CACHE_WRITE = 0.125

# Thinking counts toward max_tokens; this is the room left for it on top
# of the answer the plan allows.
THINKING_ALLOWANCE = 4000
MAX_IMAGES = 5


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def model_id():
    return _env("PIP2_MODEL") or DEFAULT_MODEL


def configured():
    return bool(anthropic is not None and _env("ANTHROPIC_API_KEY"))


def daily_ceiling():
    try:
        return max(0.0, float(_env("ANTHROPIC_DAILY_USD") or "3"))
    except ValueError:
        return 3.0


# ---- today's spend, against the ceiling --------------------------------
_spend = {"day": "", "usd": 0.0}
_spend_lock = threading.Lock()


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def spent_today():
    with _spend_lock:
        return _spend["usd"] if _spend["day"] == _today() else 0.0


def _add_spend(usage):
    if usage is None:
        return
    usd = ((getattr(usage, "input_tokens", 0) or 0) * PRICE_IN
           + (getattr(usage, "output_tokens", 0) or 0) * PRICE_OUT
           + (getattr(usage, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_READ
           + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * PRICE_CACHE_WRITE
           ) / 1_000_000
    with _spend_lock:
        if _spend["day"] != _today():
            _spend["day"], _spend["usd"] = _today(), 0.0
        _spend["usd"] += usd


def ready():
    """Configured and under today's spending ceiling."""
    return configured() and spent_today() < daily_ceiling()


def models():
    return [model_id()] if configured() else []


def vision_models():
    return models()


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = anthropic.Anthropic(max_retries=1, timeout=120.0)
        return _client


# ---- translation --------------------------------------------------------
def _tool_name(name):
    """Anthropic tool names allow letters, digits, _ and - only."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name or "tool")[:128] or "tool"


def _tools(specs):
    """OpenAI function specs -> Anthropic tools, plus a name map back."""
    out, back = [], {}
    for spec in specs or []:
        fn = spec.get("function") if spec.get("type") == "function" else spec
        if not fn or not fn.get("name"):
            continue
        safe = _tool_name(fn["name"])
        back[safe] = fn["name"]
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        out.append({"name": safe, "description": (fn.get("description") or "")[:4000],
                    "input_schema": schema})
    return out, back


def _system_text(history):
    parts = [m.get("content") for m in history
             if m.get("role") == "system" and isinstance(m.get("content"), str)]
    text = "\n\n".join(p for p in parts if p and p.strip())
    # The prompt every channel shares names pip 1.0; this one is pip 2.
    return text.replace(pip_identity.FULL_NAME, DISPLAY_NAME)


def _args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except (TypeError, ValueError):
        return {}


def _messages(history, native_tools):
    """The conversation without its system turns, in Anthropic's shape.

    native_tools=True keeps tool rounds as tool_use / tool_result blocks
    (the tool loop, which sends its tools). False flattens them to text
    (the final streamed answer, which does not)."""
    out = []
    for m in history:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            text = (m.get("content") or "")[:8000] or "(no output)"
            if native_tools:
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "",
                         "content": text}
                if (out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list)
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result"):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                out.append({"role": "user", "content": "[Tool result]\n" + text})
            continue
        if role == "assistant" and m.get("tool_calls"):
            if native_tools:
                raw = m.get("_anthropic_content")
                if raw:
                    out.append({"role": "assistant", "content": raw})
                    continue
                blocks = []
                if (m.get("content") or "").strip():
                    blocks.append({"type": "text", "text": m["content"]})
                for call in m["tool_calls"]:
                    fn = call.get("function") or {}
                    blocks.append({"type": "tool_use", "id": call.get("id") or "",
                                   "name": _tool_name(fn.get("name")),
                                   "input": _args(fn.get("arguments"))})
                out.append({"role": "assistant", "content": blocks})
            else:
                said = (m.get("content") or "").strip()
                used = ", ".join((c.get("function") or {}).get("name") or "a tool"
                                 for c in m["tool_calls"])
                out.append({"role": "assistant",
                            "content": (said + "\n" if said else "") + "[Used %s]" % used})
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict) and p.get("type") == "text")
        text = (content or "").strip() if isinstance(content, str) else ""
        if not text:
            continue
        out.append({"role": "assistant" if role == "assistant" else "user", "content": text})
    # The conversation has to open with the person.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _with_images(messages, images):
    if not images:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] != "user" or not isinstance(messages[i]["content"], str):
            continue
        blocks = []
        for b64 in list(images)[:MAX_IMAGES]:
            mime = attachments.data_url(b64).split(";", 1)[0][5:] or "image/png"
            if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                mime = "image/png"
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": mime, "data": b64}})
        if len(images) > MAX_IMAGES:
            blocks.append({"type": "text", "text": "[%d more image(s) were attached than "
                           "can be read at once - say so rather than guessing at them.]"
                           % (len(images) - MAX_IMAGES)})
        blocks.append({"type": "text", "text": messages[i]["content"]})
        messages[i] = {"role": "user", "content": blocks}
        break
    return messages


def _effort(options):
    """Follows the Quick / Deep switch: low for Quick (fast and cheap),
    medium for Deep (it thinks the problem through first)."""
    opts = options or {}
    return "medium" if opts.get("reasoning_effort") in ("medium", "high") else "low"


def _request(model, history, options, images=None, tools=None, stream=False):
    opts = options or {}
    answer = int(opts.get("num_predict") or 3500)
    max_tokens = answer + THINKING_ALLOWANCE
    if not stream:
        max_tokens = min(max_tokens, 16000)
    body = {
        "model": model or model_id(),
        "max_tokens": max_tokens,
        "messages": _with_images(_messages(history, native_tools=bool(tools)), images),
        "output_config": {"effort": _effort(opts)},
        # The system prompt and the earlier turns repeat on every message
        # of a conversation; cached, they cost a tenth on the next one.
        "cache_control": {"type": "ephemeral"},
    }
    system = _system_text(history)
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    return body


def _raise_for(exc):
    """An SDK error -> the exception every channel raises, so app.py can
    answer from Groq instead."""
    if anthropic is not None and isinstance(exc, anthropic.RateLimitError):
        raise RateLimited("pip 2 rate limited") from exc
    raise Unreachable("pip 2: %s" % exc.__class__.__name__) from exc


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces."""
    if not configured():
        raise Unreachable("pip 2 is not configured")
    if not ready():
        raise RateLimited("pip 2 daily spending ceiling reached")
    body = _request(model, history, options, images=images, stream=True)
    if not body["messages"]:
        raise Unreachable("pip 2: nothing to send")
    sent_any = False
    try:
        with _get_client().messages.stream(**body) as stream:
            for piece in stream.text_stream:
                if piece:
                    sent_any = True
                    yield piece
            final = stream.get_final_message()
    except Exception as exc:                       # noqa: BLE001
        if anthropic is not None and isinstance(exc, anthropic.APIError):
            if not sent_any:
                _raise_for(exc)
            yield "\n\n[pip 2 stopped before finishing - send that again.]"
            return
        raise
    _add_spend(final.usage)
    if usage is not None:
        usage["eval_count"] = getattr(final.usage, "output_tokens", 0) or 0
    if final.stop_reason == "refusal" and not sent_any:
        # Declined before saying anything: let another channel answer.
        raise Unreachable("pip 2 declined")
    if final.stop_reason == "max_tokens" and usage is not None:
        usage["truncated"] = True


def chat_once(model, history, tools=None, options=None, timeout=120):
    """One non-streamed turn for the tool loop. -> (message, error)."""
    if not ready():
        return None, "pip 2 is not available right now."
    specs, back = _tools(tools)
    body = _request(model, history, options, tools=specs or None)
    if not body["messages"]:
        return None, "pip 2: nothing to send"
    try:
        resp = _get_client().with_options(timeout=float(timeout)).messages.create(**body)
    except Exception as exc:                       # noqa: BLE001
        if anthropic is not None and isinstance(exc, anthropic.APIError):
            return None, "pip 2 error: %s" % exc.__class__.__name__
        raise
    _add_spend(resp.usage)
    if resp.stop_reason == "refusal":
        return None, "pip 2 declined"
    text, calls = [], []
    for block in resp.content:
        if block.type == "text":
            text.append(block.text)
        elif block.type == "tool_use":
            calls.append({"id": block.id, "type": "function",
                          "function": {"name": back.get(block.name, block.name),
                                       "arguments": json.dumps(block.input or {})}})
    message = {
        "role": "assistant",
        "content": "".join(text),
        "finish_reason": ("tool_calls" if resp.stop_reason == "tool_use"
                          else "length" if resp.stop_reason == "max_tokens" else "stop"),
    }
    if calls:
        message["tool_calls"] = calls
        # Replayed unchanged on the next turn (thinking blocks included).
        message["_anthropic_content"] = [b.model_dump(exclude_none=True) for b in resp.content]
    return message, None
