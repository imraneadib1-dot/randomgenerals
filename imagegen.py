"""Text-to-image generation - two backends, one local and free, one
cloud and optional.

**local** (the default, always available): a small diffusion model
(stabilityai/sd-turbo, a distilled few-step model chosen specifically so
CPU inference doesn't need the usual 20-50 steps) run on CPU deliberately,
not GPU - this machine's ~8GB of VRAM is already mostly claimed by the
Ollama models (see `ollama ps`), and fighting them for the same memory
would just crash both. Slower (expect maybe 10-60s per image) but free,
private, and reliable. Downloaded from Hugging Face the first time an
image is actually requested, not at server startup - a few GB, one-time,
cached under the usual Hugging Face cache directory afterward.

**gemini** (opt-in, only if GEMINI_API_KEY is set in .env): calls
Google's Imagen 3 through the Gemini API for noticeably higher image
quality, at the cost of reintroducing a cloud dependency this app
otherwise deliberately avoids (see app.py) and Google's own usage-based
pricing past their free quota. Never the default - a user has to
explicitly ask for it, and it silently isn't offered at all if no key is
configured (see gemini_configured() below), same pattern as Google
sign-in.
"""
import base64
import importlib.util
import io
import os
import threading
import uuid

import requests

GENERATED_DIR = os.path.join("static", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

MODEL_ID = "stabilityai/sd-turbo"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_IMAGE_MODEL = "imagen-3.0-generate-002"
GEMINI_PREDICT_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_IMAGE_MODEL}:predict"
)


def gemini_configured():
    return bool(GEMINI_API_KEY)

_pipe = None
_pipe_lock = threading.Lock()
_load_error = None


def _load_pipeline():
    global _pipe, _load_error
    if _pipe is not None or _load_error is not None:
        return _pipe
    with _pipe_lock:
        if _pipe is None and _load_error is None:
            try:
                import torch
                # pyright flags this as a private import and suggests
                # diffusers.pipelines.auto_pipeline instead. That is the
                # actual private path; this top-level name is what the
                # diffusers docs tell you to use, and it works - it just
                # arrives through a lazy module that pyright can't see
                # into. Taking pyright's advice would couple this to
                # diffusers' internal layout to silence a false positive.
                from diffusers import AutoPipelineForText2Image  # type: ignore[reportPrivateImportUsage]  # noqa: E501

                pipe = AutoPipelineForText2Image.from_pretrained(
                    MODEL_ID, torch_dtype=torch.float32,
                )
                pipe.to("cpu")
                _pipe = pipe
            except Exception as e:
                _load_error = str(e)
    return _pipe


def preload():
    """Optional warm-up so the first real request isn't the one paying
    for the download/load time - safe to call from a background thread
    at startup, a no-op if it's already loaded or already failed."""
    _load_pipeline()


def local_available():
    """Can the local pipeline plausibly run here? Cheap enough to call
    per request.

    Deliberately does NOT load the model. Loading takes around a minute
    cold, and this gets called while deciding which tools to advertise
    to the model - a decision that has to be instant. Checking that
    torch and diffusers are importable is enough to tell a GPU box apart
    from a cloud host that installed requirements-cloud.txt, which is
    the distinction that actually matters.

    If a load has already been attempted, its outcome is authoritative.
    """
    if _pipe is not None:
        return True
    if _load_error is not None:
        return False
    return (importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("diffusers") is not None)


def available():
    """True if image generation can work at all, by any backend."""
    return gemini_configured() or local_available()


def _save_png_bytes(raw_bytes):
    filename = f"{uuid.uuid4().hex[:12]}.png"
    path = os.path.join(GENERATED_DIR, filename)
    with open(path, "wb") as f:
        f.write(raw_bytes)
    url_dir = GENERATED_DIR.replace(os.sep, "/")
    return f"/{url_dir}/{filename}"


QUALITY_SUFFIX = ", highly detailed, sharp focus, professional photography"


def _enhance_prompt(prompt):
    """sd-turbo is small enough that it leans heavily on descriptive
    cues - a bare 'a cat' gives a much vaguer image than the same
    prompt with quality/detail hints appended. Skipped when the user
    already wrote a detailed prompt (their own wording should win) or
    asked for a specific non-photographic style, where forcing
    'professional photography' actively fights what they asked for."""
    lowered = prompt.lower()
    style_words = ("painting", "drawing", "sketch", "cartoon", "anime",
                   "illustration", "watercolor", "pixel art", "3d render",
                   "logo", "diagram", "comic")
    if len(prompt) > 120 or any(w in lowered for w in style_words):
        return prompt
    return prompt + QUALITY_SUFFIX


def generate_image_local(prompt):
    """-> (url, error). Exactly one of the two is set."""
    pipe = _load_pipeline()
    if pipe is None:
        return None, f"Image model isn't available: {_load_error}"

    try:
        # 2 steps, guidance 0.0 is not a shortcut - it's what sd-turbo is
        # distilled for. Measured on this machine at 2/4/8 steps from the
        # same seed: 4 and 8 came out *worse*, adding oversaturation and
        # crosshatch artifacts, for roughly double the time. Raising this
        # looks like an obvious quality win and isn't one.
        result = pipe(
            prompt=_enhance_prompt(prompt)[:400],
            num_inference_steps=2,
            guidance_scale=0.0,
        )
        image = result.images[0]
    except Exception as e:
        return None, f"Image generation failed: {e}"

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return _save_png_bytes(buf.getvalue()), None


def generate_image_gemini(prompt):
    """-> (url, error). Exactly one of the two is set.

    Calls Google's Imagen 3 via the Gemini API's :predict endpoint. This
    app has no way to test this against a real key (none is configured
    in dev), so it's built to match Google's documented request/response
    shape as closely as possible, with error handling specific enough
    that a shape mismatch shows up as a clear message rather than a
    silent crash - worth double-checking against Google's current docs
    if this errors out, since REST API response shapes do shift.
    """
    if not GEMINI_API_KEY:
        return None, "Gemini isn't configured (no GEMINI_API_KEY in .env)."

    try:
        r = requests.post(
            GEMINI_PREDICT_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "instances": [{"prompt": prompt[:480]}],
                "parameters": {"sampleCount": 1},
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        predictions = data.get("predictions") or []
        if not predictions:
            return None, "Gemini returned no image - the prompt may have been filtered."
        b64_image = predictions[0].get("bytesBase64Encoded")
        if not b64_image:
            return None, "Unexpected response shape from Gemini's image API."
        raw_bytes = base64.b64decode(b64_image)
    except requests.exceptions.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        return None, f"Gemini image generation failed: {detail or e}"
    except Exception as e:
        return None, f"Gemini image generation failed: {e}"

    return _save_png_bytes(raw_bytes), None


def generate_image(prompt, backend="local"):
    """-> (url, error). Exactly one of the two is set. `backend` is
    "local" (default, always available) or "gemini" (only if
    gemini_configured())."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe what you want to see."

    if backend == "gemini":
        if not gemini_configured():
            return None, "Gemini isn't configured on this server."
        return generate_image_gemini(prompt)
    return generate_image_local(prompt)
