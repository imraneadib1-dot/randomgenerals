"""Text-to-image generation - two backends, one hosted and one local.

**hosted** (the default): Pollinations, over a plain GET with no
account, no key and no signup. That combination is what makes it the
right default here: this app runs on a free Oracle VM with no GPU, and
every keyed alternative means a credential to manage or a card on file
for a feature most people try once.

It serves SANA, not FLUX. An earlier version of this file claimed FLUX
because the endpoint accepts a `model=flux` parameter - but it accepts
`model=` anything, including names that do not exist, and returns a
byte-identical image for all of them. Verified: the same seed under
flux, sana, turbo and a deliberately invalid name produced four files
with the same MD5. The parameter is ignored and /models reports only
sana, so that is what this is.

SANA is fast and decent. It is not FLUX, and if image quality becomes
the priority the honest fix is a keyed provider that actually runs the
model it advertises - which is what the third backend below is.

It also caps resolution. Asking for 1024x1024 returns 768x768; asking
for 1344x768 returns 1015x580. Measured across all five shapes, what
comes back is always the requested ASPECT RATIO scaled to a fixed budget
of about 589,000 pixels - 0.59MP, roughly three quarters of the
requested linear size. The shape offered in the UI is therefore honest
and the resolution was not, which _fit() below now corrects.

**cloudflare** (opt-in, keyed): FLUX.1-schnell on Cloudflare Workers AI,
which has a standing free daily allocation. Off unless CF_ACCOUNT_ID and
CF_API_TOKEN are set, and preferred over Pollinations when they are:
this is the one path here to genuinely better pixels rather than better
use of the same ones. Cloudflare is already in this deployment for the
tunnel, so it adds an API token rather than a whole new relationship.

It replaced Google's Imagen, which was the only hosted option before.
Imagen needed a Google key, and quality aside, tying image generation to
the same key that answers chat meant one revoked key took out two
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

# The hosted one. The model name is sent for forward-compatibility if
# Pollinations ever honours it, but see the note above - today it does
# not, and every value returns the same SANA output.
FLUX_MODEL = "flux"
FLUX_URL = "https://image.pollinations.ai/prompt/"

# The keyed one. schnell is the distilled FLUX: four steps instead of
# ~28, which is what makes it viable on a free allocation at all.
CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"
CF_URL = ("https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s")
# Cloudflare's own ceiling for this model, not a choice made here.
CF_MAX_STEPS = 8

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

    The keyless backend needs no credentials, so this is effectively
    always true - which is the point of choosing one.
    """
    return True


def best_backend():
    """The best backend that is actually usable here.

    Cloudflare first when it has a key: it is the only one running a
    model chosen on merit rather than on being free without an account.
    """
    return "cloudflare" if cloudflare_configured() else "flux"


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


# WHY A LANGUAGE MODEL WRITES THE PROMPT
#
# The largest quality lever available without paying for a better image
# model is the prompt, because a diffusion model asked for "a cat" has to
# invent the breed, the pose, the setting, the light and the lens itself,
# and it invents the average of its training data. Given those decisions
# explicitly it makes a picture instead of a stock photo.
#
# Compared side by side at a fixed seed, "a mountain village" came back
# as a competent generic postcard; the expanded version - cliffs, smoke
# from chimneys, mist in the lanes, low sun - came back as a photograph
# someone would keep.
#
# The prohibition on softness language is not stylistic fussiness. The
# first version of this prompt happily produced "shallow depth of field,
# soft golden light", and SANA read that as instructions to blur: the
# result was better composed than the bare prompt and visibly less sharp.
# Atmosphere has to be earned with subject and light, not with words that
# name a blur.
ENRICH_SYSTEM = (
    "You rewrite a short image request as one vivid, concrete "
    "text-to-image prompt.\n"
    "Rules:\n"
    "- Keep the requested subject exactly. Never replace or reinterpret "
    "it, and never add a different subject.\n"
    "- Add specifics the request leaves open: what the subject looks "
    "like, where it is, the light, the time of day, the palette, the "
    "medium or camera, the mood.\n"
    "- Prefer sharp, well-lit description. Do NOT ask for shallow depth "
    "of field, bokeh, soft focus, heavy haze or motion blur unless the "
    "request did - they make the image mushy rather than atmospheric.\n"
    "- If the request is already detailed, tighten it rather than "
    "padding it.\n"
    "Output ONLY the prompt: one line, under 60 words, no quotes, no "
    "markdown, no preamble."
)


def _complete(system, user, max_tokens=220, timeout=25):
    """One prompt in, one string out, via Ollama. -> text, or "" on error.

    Deliberately does not import app.py - that would be a circular
    import, since app.py imports this module. The endpoint is read from
    the same environment variables instead, so a deployment pointed at
    Ollama Cloud enriches prompts there too without any extra wiring.

    Never raises and never returns half an answer: every failure is "" so
    the caller keeps the words the person actually typed.
    """
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    headers = {"Authorization": "Bearer " + key} if key else {}


    def _keep_alive_value():
        raw = os.environ.get("OLLAMA_KEEP_ALIVE", "-1")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw

    try:
        tags = requests.get(url + "/api/tags", headers=headers, timeout=5)
        names = [m["name"] for m in tags.json().get("models", [])]
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return ""
    if not names:
        return ""

    # Whichever general model is loaded - matching the order app.py's
    # BAY_ROUTES uses, so this reuses the model already resident in VRAM
    # rather than evicting it to load a second one. Getting this wrong
    # would make every image request cost an 8-second model swap, twice.
    model = next(
        (n for p in ("qwen2.5-coder", "qwen3.5", "gpt-oss", "qwen2.5",
                     "llama3.2")
         for n in names if p in n.lower()), names[0])

    try:
        r = requests.post(
            url + "/api/chat", headers=headers, timeout=timeout,
            json={"model": model, "stream": False,
                  # Match app.py's OLLAMA_KEEP_ALIVE default, or this call
                  # would reset a resident model to Ollama's 5-minute one.
                  # An int, not "-1": Ollama parses a string as a Go
                  # duration and rejects a bare number in one.
                  "keep_alive": _keep_alive_value(),
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "options": {"num_predict": max_tokens, "temperature": 0.7}})
        return ((r.json().get("message") or {}).get("content") or "").strip()
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return ""


def enhance_prompt(prompt):
    """A short request expanded into a detailed one. -> text.

    Returns the original unchanged if there is no text model configured,
    if it is slow, or if it answers with something implausible. Every
    failure is silent and non-fatal: this improves a picture, it is not
    allowed to stop one being made.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return prompt
    # Already-detailed requests are left alone. Someone who wrote three
    # careful sentences has said what they want, and rewriting it would
    # be this app overruling them rather than helping.
    if len(prompt) > 240:
        return prompt

    out = _complete(ENRICH_SYSTEM, prompt)
    # Sanity bounds, because a model that ignores the format instruction
    # tends to fail loudly - a refusal, a preamble, a bulleted list - and
    # any of those make a worse picture than the plain request would.
    if not out or len(out) < len(prompt) or len(out) > 900:
        return prompt
    if "\n" in out.strip():
        return prompt
    return out


def _fit(raw, width, height):
    """Resample to the size that was actually asked for. -> bytes.

    Pollinations caps output at ~0.59MP whatever is requested (see the
    module note), so a 1024x1024 request arrives as 768x768. This does
    NOT invent detail - it is a Lanczos resample and nothing more. What
    it buys is that the file matches its advertised dimensions, so a
    picture dropped into a full-width layout is not quietly upscaled by
    the browser with nearest-neighbour instead.

    Returns the bytes untouched if Pillow is missing or the image is
    already at or above the target, so this is an improvement where it
    can be had and never a failure.
    """
    try:
        from PIL import Image
    except ImportError:
        return raw

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.width >= width and img.height >= height:
            return raw
        img = img.convert("RGB").resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        # Quality 92: above this the file grows faster than it improves,
        # and these are already lossy JPEGs coming in - re-encoding at 100
        # would preserve the first encoder's artefacts in high fidelity.
        img.save(buf, format="JPEG", quality=92, subsampling=0,
                 optimize=True)
        return buf.getvalue()
    except Exception:                            # noqa: BLE001 - cosmetic
        # Anything at all here means the original bytes are still a
        # perfectly good picture. Never let framing break delivery.
        return raw


# ------------------------------------------------------------------ hosted
def generate_image_flux(prompt, size=DEFAULT_SIZE, style=DEFAULT_STYLE,
                        seed=None):
    """SANA via Pollinations, keyless. -> (url, error)."""
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

    body = _fit(r.content, width, height)
    ext = "jpg" if body is not r.content or "jpeg" in ctype or "jpg" in ctype \
        else "png"
    return _save_bytes(body, ext), None


# -------------------------------------------------------- hosted, keyed
def cloudflare_configured():
    return bool(os.environ.get("CF_ACCOUNT_ID", "").strip()
                and os.environ.get("CF_API_TOKEN", "").strip())


def generate_image_cloudflare(prompt, size=DEFAULT_SIZE,
                              style=DEFAULT_STYLE, seed=None):
    """FLUX.1-schnell on Cloudflare Workers AI. -> (url, error)."""
    import base64

    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not (account and token):
        return None, "Cloudflare image generation is not configured."

    width, height = _dimensions(size)
    body = {"prompt": build_prompt(prompt, style), "steps": 4}
    if seed is not None:
        body["seed"] = int(seed)

    try:
        r = requests.post(CF_URL % (account, CF_MODEL), json=body,
                          headers={"Authorization": "Bearer " + token},
                          timeout=120)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach Cloudflare: %s" % e

    # Cloudflare answers 200 with success:false for a bad model slug or a
    # token without the Workers AI permission, so the status code alone
    # is not the check. Their message is surfaced verbatim because it is
    # specific and actionable, and guessing at it would not be.
    try:
        payload = r.json()
    except ValueError:
        return None, "Cloudflare returned a non-JSON response (%d)." % (
            r.status_code)

    if not payload.get("success"):
        errors = payload.get("errors") or [{}]
        return None, "Cloudflare image generation failed: %s" % (
            errors[0].get("message") or r.status_code)

    encoded = (payload.get("result") or {}).get("image")
    if not encoded:
        return None, "Cloudflare returned no image."
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, TypeError):
        return None, "Cloudflare returned an unreadable image."

    return _save_bytes(_fit(raw, width, height), "jpg"), None


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
                   style=DEFAULT_STYLE, seed=None, enhance=True):
    """-> (url, error). Exactly one of the two is set.

    `backend` is "cloudflare" (keyed FLUX, best where configured),
    "flux" (keyless, always available) or "local" (this machine, only
    where torch is installed).

    `enhance` runs the request through a language model first - see
    enhance_prompt(). On by default because the difference is large and
    the failure mode is "nothing happens".
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe what you want to see."
    if len(prompt) > 900:
        return None, "That description is too long. Try a shorter one."

    if backend == "local":
        # Not enhanced: sd-turbo runs at four steps with no guidance and
        # does not reward a long prompt the way the hosted models do, and
        # the round trip would be a noticeable share of the wait.
        return generate_image_local(prompt, size, style)

    if enhance:
        prompt = enhance_prompt(prompt)

    # An explicit choice is honoured; anything else takes the best that
    # is actually available, so adding a key upgrades the default
    # without anyone having to find a setting.
    if backend == "cloudflare" or (backend != "flux"
                                   and cloudflare_configured()):
        url, error = generate_image_cloudflare(prompt, size, style, seed)
        if url:
            return url, error
        # A key that is wrong, out of allocation or pointed at a model
        # slug that has been renamed should degrade to the keyless
        # backend rather than take the feature down with it.
        if backend == "cloudflare":
            return None, error

    return generate_image_flux(prompt, size, style, seed)
