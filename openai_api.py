"""An OpenAI-compatible API, so other tools can use this as their model.

WHY THIS SHAPE
"Connect it to VS Code, to Cursor, to anything" has one practical answer:
speak the API those tools already speak. Cursor, Continue, Cline, Zed,
Aider and most everything else let you override the OpenAI base URL, so
exposing /v1/chat/completions makes this app a drop-in model provider for
all of them at once. Writing a separate extension per editor would be the
same work repeated forever.

WHAT IT IS NOT
This does not give the model access to a filesystem or a shell. It answers
prompts. The editor keeps doing the editing - that division is what makes
it safe to expose at all, since this endpoint is reachable from the public
internet.

AUTHENTICATION
A bearer key per account, checked on every request. Without it this is an
open relay: anyone who found the URL could spend the owner's Groq quota
and, on a machine with Ollama running, use their GPU. Keys are stored as
SHA-256 hashes - a leaked database should not hand over working keys, and
nothing here ever needs the original back.
"""
import hashlib
import json
import secrets
import time


KEY_PREFIX = "rg-"


def new_key():
    """-> (plaintext, hash). The plaintext is shown once and never stored."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_key(raw)


def hash_key(raw):
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def key_from_request(headers):
    """Pull the key out of an Authorization header.

    Accepts the bearer form every OpenAI client sends, and a bare key,
    because a few tools send it unprefixed and failing those with
    'unauthorized' sends people hunting for the wrong problem.
    """
    auth = (headers.get("Authorization") or "").strip()
    if not auth:
        return ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


def error(message, code=400, err_type="invalid_request_error"):
    """OpenAI's error envelope. Clients parse this shape to decide whether
    to retry, so a bare string would leave them guessing."""
    return {"error": {"message": message, "type": err_type, "code": None}}, code


def to_history(messages):
    """OpenAI messages -> this app's internal history.

    Content can be a plain string or a list of parts (the multimodal
    form). Parts that are not text are dropped rather than stringified:
    a JSON blob of an image object pasted into the prompt is worse than
    the image being absent, because the model will try to read it.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            text = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = content if isinstance(content, str) else ""
        out.append({"role": role, "content": text})
    return out


def options_from(payload, default_max=2048):
    """OpenAI generation params -> this app's option names."""
    opts = {}
    n = payload.get("max_tokens") or payload.get("max_completion_tokens")
    try:
        opts["num_predict"] = int(n) if n else default_max
    except (TypeError, ValueError):
        opts["num_predict"] = default_max
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if payload.get(src) is not None:
            try:
                opts[dst] = float(payload[src])
            except (TypeError, ValueError):
                pass
    return opts


def _chunk(cid, created, model, delta=None, finish=None):
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta or {},
            "finish_reason": finish,
        }],
    }


def stream_sse(pieces, model):
    """Wrap a generator of text in OpenAI's streaming format.

    The first chunk carries the role and no content, then one chunk per
    piece, then an empty delta with finish_reason, then the literal
    [DONE]. Clients wait for that terminator; without it Cursor sits
    showing a spinner after the answer has finished arriving.
    """
    cid = "chatcmpl-" + secrets.token_hex(12)
    created = int(time.time())

    yield "data: " + json.dumps(
        _chunk(cid, created, model, delta={"role": "assistant", "content": ""})
    ) + "\n\n"

    for piece in pieces:
        if not piece:
            continue
        yield "data: " + json.dumps(
            _chunk(cid, created, model, delta={"content": piece})
        ) + "\n\n"

    yield "data: " + json.dumps(
        _chunk(cid, created, model, finish="stop")
    ) + "\n\n"
    yield "data: [DONE]\n\n"


def completion(text, model, prompt_tokens=0, completion_tokens=0):
    """The non-streaming response body."""
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        # Some clients read these and will divide by zero or show NaN if
        # the block is missing entirely, so it is always present even
        # when the numbers are estimates.
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def model_list(ids):
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "randomgenerals"}
            for m in ids
        ],
    }
