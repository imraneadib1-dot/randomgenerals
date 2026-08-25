"""Google Gemini - the cloud fallback, for when this machine is off.

WHY THIS AND NOT A LOCAL MODEL
Nothing here replaces Ollama. Local models answer whenever this machine
is running, because prompts staying on the device is the point of the
app. This exists for the case local models cannot cover at all: the
computer is off, so there is nothing on it to ask.

WHY GEMINI SPECIFICALLY
One free key, no credit card, and it covers three things this app
otherwise needs three different answers for - chat, vision, and image
generation. imagegen.py already uses GEMINI_API_KEY for images, so this
adds a provider without adding a credential.

THE PART THAT IS NOT A URL SWAP
Gemini's API is not OpenAI-shaped, so this is a real translation rather
than a different endpoint:

  - messages are "contents", and each carries "parts" rather than a
    string, which is also how images ride along
  - the assistant role is called "model"
  - the system prompt is not a message at all; it is a separate
    systemInstruction field, and passing it as a message makes Gemini
    treat the app's instructions as something the user said
  - generation settings live under generationConfig with different names

Get a key at https://aistudio.google.com/apikey - Google account, no
card, about a minute.
"""
import json
import os
import time

import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Fetched live and cached. A hardcoded list rots into 404s the moment
# Google retires a model - which is exactly how the previous cloud
# provider broke, so the same mistake is not repeated here.
_models_cache = {"at": 0.0, "models": None, "error": ""}
MODELS_TTL = 3600

# Used only if the catalogue cannot be reached at all.
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

# Families that exist in the catalogue but make no sense in a chat
# picker: embeddings, image-only models, and text-to-speech.
_NON_CHAT = ("embedding", "aqa", "imagen", "tts", "veo", "learnlm")


def api_key():
    """Read at call time, never captured at import - a module-level read
    runs before load_dotenv() and silently sees nothing."""
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def configured():
    return bool(api_key())


def last_error():
    """Why the last catalogue fetch failed, or "" if it didn't. Read by
    /api/health so a bad key is visible instead of being inferred."""
    return _models_cache.get("error") or ""


def models():
    """Chat-capable Gemini models, fetched live and cached for an hour.

    A rejected key returns nothing rather than the fallback list. The
    fallback exists for Google being unreachable - a network blip should
    not empty the picker. Using it for an auth failure hides the failure:
    the app looks configured, the picker looks populated, and the first
    person to send a message gets the error instead. That happened, so
    the two cases are now told apart.
    """
    if not configured():
        return []
    now = time.time()
    cached = _models_cache
    if cached["models"] is not None and now - cached["at"] < MODELS_TTL:
        return cached["models"]
    try:
        r = requests.get(f"{API_ROOT}/models", params={"key": api_key()},
                         timeout=8)
        if r.status_code in (401, 403):
            detail = ""
            try:
                detail = (r.json().get("error", {}).get("message") or "")[:200]
            except ValueError:
                pass
            _models_cache.update({
                "at": now, "models": [],
                "error": f"Google rejected the key ({r.status_code}). "
                         f"{detail}",
            })
            return []
        r.raise_for_status()
        found = []
        for m in r.json().get("models", []):
            name = (m.get("name") or "").replace("models/", "")
            # Only models that can actually hold a conversation.
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            if any(bad in name.lower() for bad in _NON_CHAT):
                continue
            found.append(name)
        out = sorted(set(found)) or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out, "error": ""})
        return out
    except requests.exceptions.RequestException as e:
        # Unreachable, not rejected - serve the last good list so a blip
        # does not empty the picker, and retry on the next call.
        out = cached["models"] or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out,
                              "error": f"could not reach Google: {e}"})
        return out


def _to_contents(history):
    """Translate this app's message list into Gemini's format.

    Returns (contents, system_instruction). The system prompt is pulled
    out deliberately: Gemini takes it as a separate field, and sending it
    as an ordinary message makes the model treat the app's own
    instructions as something the user typed - which it will then answer,
    argue with, or repeat back.
    """
    contents = []
    system_parts = []
    for m in history:
        role = m.get("role")
        text = m.get("content") or ""
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        # Gemini calls the assistant "model"; "assistant" is rejected.
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": [{"text": text}],
        })
    system_instruction = None
    if system_parts:
        system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return contents, system_instruction


def _attach_images(contents, images):
    """Put base64 images on the most recent user turn.

    They belong with the message that referred to them; attaching them
    anywhere else asks the model about a picture from a different part of
    the conversation.
    """
    if not images or not contents:
        return
    for entry in reversed(contents):
        if entry["role"] == "user":
            for b64 in images:
                entry["parts"].append({
                    "inlineData": {"mimeType": "image/png", "data": b64},
                })
            return


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Signature matches the other providers in app.py.

    Yields text as it arrives, or a single bracketed message explaining
    what went wrong - never raises, because a raised exception mid-stream
    reaches the user as a truncated reply with no explanation.
    """
    if not configured():
        yield ("[The cloud fallback isn't configured - no GEMINI_API_KEY "
               "on this server.]")
        return

    contents, system_instruction = _to_contents(history)
    _attach_images(contents, images)
    if not contents:
        yield "[Nothing to answer.]"
        return

    body = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = system_instruction

    # Ollama's option names translated rather than passed through, so the
    # rest of the app does not have to know which provider it is talking
    # to. num_ctx has no equivalent - Gemini's context is fixed per model
    # - so it is dropped rather than faked.
    cfg = {}
    if options:
        if "num_predict" in options:
            cfg["maxOutputTokens"] = options["num_predict"]
        if "temperature" in options:
            cfg["temperature"] = options["temperature"]
        if "top_p" in options:
            cfg["topP"] = options["top_p"]
    if cfg:
        body["generationConfig"] = cfg

    url = f"{API_ROOT}/models/{model}:streamGenerateContent"
    try:
        with requests.post(
            url, params={"key": api_key(), "alt": "sse"},
            headers={"Content-Type": "application/json"},
            json=body, stream=True, timeout=120,
        ) as r:
            if r.status_code == 429:
                yield ("[Google's free daily limit is used up. It resets "
                       "tomorrow - or start the local AI on the host "
                       "machine.]")
                return
            if r.status_code >= 400:
                detail = ""
                try:
                    detail = (r.json().get("error", {}).get("message") or "")
                except ValueError:
                    detail = r.text[:200]
                yield f"[Gemini refused the request: {detail}]"
                return

            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                # Token counts arrive on most chunks; the last one wins,
                # which is what credit charging is based on.
                meta = chunk.get("usageMetadata") or {}
                if meta and usage is not None:
                    usage["eval_count"] = meta.get("candidatesTokenCount")

                for cand in chunk.get("candidates", []):
                    for part in (cand.get("content") or {}).get("parts", []):
                        piece = part.get("text")
                        if piece:
                            yield piece
                    # Gemini can stop early for its own safety reasons.
                    # Silence would look like the model simply had
                    # nothing more to say, so name it.
                    reason = cand.get("finishReason")
                    if reason and reason not in ("STOP", "MAX_TOKENS"):
                        yield f"\n\n[Gemini stopped early: {reason}]"
    except requests.exceptions.RequestException as e:
        yield f"[Couldn't reach Gemini: {e}]"
