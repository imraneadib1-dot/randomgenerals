"""Text-to-image generation - two backends, one hosted and one local.

**flux** (the default): FLUX.1 through Pollinations, which serves it over
a plain GET with no account, no key and no signup. That combination is
what makes it the right default here: this app is deployed on a free
Oracle VM with no GPU, and every keyed alternative would mean either a
credential to manage or a card on file for a feature people try once.

It replaced Google's Imagen, which was the only hosted option before.
Imagen needed GEMINI_API_KEY, and quality aside, tying image generation
to the same key that answers chat meant one revoked key took out two
unrelated features.

**local** (opt-in): Stable Diffusion via diffusers, on this machine. Free
and private, but it needs torch and realistically a GPU - on CPU a single
picture takes minutes. It stays because "runs on your own hardware" is
the point of this product, and because a local GPU beats any free hosted
tier for turnaround and for not being rate-limited.

Nothing here has a hard dependency on torch. It is imported inside
_load_pipeline() so a deployment that skips the ~2.3GB of wheels still
imports this module fine and simply reports the local backend as
unavailable.
"""
import importlib.util
import io
import os
import threading
import urllib.parse
import uuid

import requests

GENERATED_DIR = os.path.join("static", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

# The local model. sd-turbo rather than full SD: it produces something
# usable in 1-4 steps instead of 30-50, which is the difference between
# "slow" and "unusable" on the hardware this is likely to meet.
MODEL_ID = "stabilityai/sd-turbo"

# The hosted one.
FLUX_MODEL = "flux"
FLUX_URL = "https://image.pollinations.ai/prompt/"

# Sizes offered to the UI. Kept to a small set of known-good shapes
# rather than free numbers: diffusion models are trained at particular
# resolutions and drift badly off them, and an arbitrary 1000x333 comes
# back as a smeared mess rather than a wide picture.
SIZES = {
    "square": (1024, 1024),
    "portrait": (896, 1152),
    "landscape": (1152, 896),
    "wide": (1344, 768),
    "tall": (768, 1344),
}
DEFAULT_SIZE = "square"

# Named looks, appended to the prompt. Data rather than code because this
# is the part most likely to be tuned by eye.
STYLES = {
    "none": "",
    "photo": ("photorealistic, 85mm lens, natural light, shallow depth "
              "of field, high detail"),
    "cinematic": ("cinematic still, dramatic lighting, anamorphic, film "
                  "grain, colour graded"),
    "art": "digital painting, painterly brushwork, rich colour, artstation",
    "anime": "anime illustration, clean linework, cel shading, vibrant",
    "3d": "3d render, octane, soft studio lighting, subsurface scattering",
    "minimal": "minimal flat vector illustration, bold shapes, limited palette",
}
DEFAULT_STYLE = "none"

_pipeline = None
_pipeline_lock = threading.Lock()
_local_error = ""


# ------------------------------------------------------------------ local
def _load_pipeline():
    """Import torch and build the pipeline, once. -> (pipeline, error).

    Everything heavy happens in here rather than at module scope so the
    cloud build - which deliberately ships without torch - still imports
    this file.
    """
    global _pipeline, _local_error
    if _pipeline is not None or _local_error:
        return _pipeline, _local_error

    with _pipeline_lock:
        if _pipeline is not None or _local_error:
            return _pipeline, _local_error
        try:
            import torch
            from diffusers import AutoPipelineForText2Image  # type: ignore

            device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device == "cuda" else torch.float32
            pipe = AutoPipelineForText2Image.from_pretrained(
                MODEL_ID, torch_dtype=dtype, safety_checker=None)
            pipe = pipe.to(device)
            _pipeline = pipe
        except Exception as e:                    # noqa: BLE001 - reported
            _local_error = "Local image model unavailable: %s" % e
    return _pipeline, _local_error


def local_available():
    """Whether the local backend could run, without paying to find out.

    Checks that torch is importable rather than importing it: the import
    itself costs seconds and hundreds of megabytes of RAM, which is a
    silly price for answering a question about a button's enabled state.
    """
    return importlib.util.find_spec("torch") is not None


def preload():
    """Warm the local pipeline in the background at startup, so the first
    request does not pay the whole model-load cost."""
    if local_available():
        _load_pipeline()


def available():
    """True if image generation can work at all, by any backend.

    The hosted backend needs no credentials, so this is effectively
    always true - which is the point of choosing a keyless provider.
    """
    return True


# ------------------------------------------------------------------ shared
def _save_bytes(raw, ext="png"):
    name = "%s.%s" % (uuid.uuid4().hex[:16], ext)
    path = os.path.join(GENERATED_DIR, name)
    with open(path, "wb") as fh:
        fh.write(raw)
    return "/static/generated/" + name


def build_prompt(prompt, style=DEFAULT_STYLE):
    """The prompt actually sent to the model.

    A style is appended rather than substituted so the person's own words
    stay first and keep their weight - diffusion models attend most to
    the front of a prompt, so prefixing a style would quietly outrank
    what they asked for.
    """
    prompt = (prompt or "").strip()
    extra = STYLES.get(style, "")
    return prompt + ", " + extra if extra else prompt


def _dimensions(size):
    return SIZES.get(size, SIZES[DEFAULT_SIZE])


# ------------------------------------------------------------------ hosted
def generate_image_flux(prompt, size=DEFAULT_SIZE, style=DEFAULT_STYLE,
                        seed=None):
    """FLUX.1 via Pollinations. -> (url, error)."""
    width, height = _dimensions(size)
    full = build_prompt(prompt, style)

    # The prompt travels in the PATH, so it has to be percent-encoded with
    # nothing left safe - a slash or question mark in someone's prompt
    # would otherwise end the path or start the query string.
    url = FLUX_URL + urllib.parse.quote(full, safe="")
    params = {
        "width": width,
        "height": height,
        "model": FLUX_MODEL,
        "nologo": "true",
        # Without this the service may return a previously generated image
        # for the same prompt, so two people asking for "a cat" get the
        # same cat.
        "seed": seed if seed is not None else uuid.uuid4().int % 2_000_000_000,
    }

    try:
        r = requests.get(url, params=params, timeout=120)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach the image service: %s" % e

    if r.status_code != 200:
        return None, ("The image service returned %d. It is free and "
                      "unmetered, so this is usually load rather than "
                      "anything wrong with the prompt - try again."
                      % r.status_code)

    ctype = (r.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("image/"):
        return None, "The image service returned %s, not an image." % (
            ctype or "nothing")
    if len(r.content) < 1024:
        return None, "The image service returned an empty image."

    ext = "jpg" if "jpeg" in ctype or "jpg" in ctype else "png"
    return _save_bytes(r.content, ext), None


# ------------------------------------------------------------------ local
def generate_image_local(prompt, size=DEFAULT_SIZE, style=DEFAULT_STYLE):
    """Stable Diffusion on this machine. -> (url, error)."""
    if not local_available():
        return None, ("The local image model needs torch and diffusers, "
                      "which are not installed here.")

    pipe, err = _load_pipeline()
    if err or pipe is None:
        return None, err or "Local image model unavailable."

    width, height = _dimensions(size)
    # sd-turbo is trained at 512 and degrades badly above it, so the
    # requested shape is honoured as an aspect ratio rather than as
    # pixels. Rounded to a multiple of 8, which the VAE requires.
    scale = 512.0 / max(width, height)
    w = max(256, int(width * scale) // 8 * 8)
    h = max(256, int(height * scale) // 8 * 8)

    try:
        image = pipe(
            prompt=build_prompt(prompt, style),
            num_inference_steps=4,
            guidance_scale=0.0,     # turbo models are trained without it
            width=w,
            height=h,
        ).images[0]
    except Exception as e:                        # noqa: BLE001 - reported
        return None, "Local image generation failed: %s" % e

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return _save_bytes(buf.getvalue(), "png"), None


# ------------------------------------------------------------------ entry
def generate_image(prompt, backend="flux", size=DEFAULT_SIZE,
                   style=DEFAULT_STYLE, seed=None):
    """-> (url, error). Exactly one of the two is set.

    `backend` is "flux" (hosted, the default, always available) or
    "local" (this machine, only where torch is installed).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe what you want to see."
    if len(prompt) > 900:
        return None, "That description is too long. Try a shorter one."

    if backend == "local":
        return generate_image_local(prompt, size, style)
    return generate_image_flux(prompt, size, style, seed)
