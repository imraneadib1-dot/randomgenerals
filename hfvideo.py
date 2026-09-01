"""Hugging Face Inference Providers - the free-tier video backend.

WHY THIS EXISTS NEXT TO pixverse.py

PixVerse has no free tier at any volume: $10 buys about 22 clips. That is
fine for an owner testing a feature and wrong for a public bay where
free accounts get two a day. Hugging Face's free tier is the only
genuinely free path that survived checking - Pollinations serves images
only (its /models returns ["sana"] and there is no video endpoint), and
Cloudflare Workers AI, which already serves this app's images, has no
video model in its catalogue at all.

WHAT FREE ACTUALLY MEANS HERE

A monthly credit allowance, not unlimited use. It is sized for
evaluation, so the honest way to run it is to expect exhaustion and say
so plainly when it happens rather than surfacing a provider error. Video
is roughly a hundred times the GPU work of an image; nobody gives that
away at volume, and a backend that pretends otherwise will simply break
in public.

WHY A THREAD

InferenceClient.text_to_video() is synchronous and returns the finished
clip as raw bytes - there is no job id to poll, and a generation takes
minutes. Running it inline would hold a gunicorn worker for the whole
render, and this deployment has eight threads total. So the call happens
on a background thread and this module keeps the job table itself,
exposing the same start()/result() shape pixverse.py does. The route
above cannot tell the two apart.

The OpenAI-compatible /v1 endpoint is deliberately not used: Hugging
Face documents it as chat-completions only, and every other task has to
go through the client, which is also what handles provider routing.
"""
import os
import threading
import time
import uuid

# Models worth routing to, best first. Named rather than left to "auto"
# because the fallbacks differ in kind: the distilled LTX is built for
# speed, Wan is the better picture, and which one is warm changes.
MODELS = (
    "Lightricks/LTX-Video-0.9.8-13B-distilled",
    "Wan-AI/Wan2.2-TI2V-5B",
)

# A generation is minutes, not seconds. Past this something is wrong on
# the provider's side and the job should fail rather than hold a thread
# open until the process restarts.
TIMEOUT_SECONDS = 420

OUTPUT_DIR = os.path.join("static", "video", "generated")

_jobs = {}
_lock = threading.Lock()


def api_key():
    return (os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_API_KEY") or "").strip()


def configured():
    if not api_key():
        return False
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable_reason():
    if not api_key():
        return ("Free video generation needs a Hugging Face token - set "
                "HF_TOKEN on the server.")
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        return ("The huggingface_hub package is not installed on this "
                "server.")
    return ""


def _render(job_id, prompt, seconds, fps):
    """Runs on a worker thread. Writes the finished clip to disk."""
    from huggingface_hub import InferenceClient

    client = InferenceClient(api_key=api_key())
    frames = max(24, min(int(seconds * fps), 200))

    last_error = ""
    for model in MODELS:
        try:
            data = client.text_to_video(
                prompt, model=model, num_frames=frames)
        except Exception as e:                    # noqa: BLE001 - reported
            # A model can be cold, unrouted, or out of credit. Try the
            # next one rather than failing the whole request on the
            # first provider that says no.
            last_error = str(e)[:300]
            continue

        if not data:
            last_error = "the provider returned an empty clip"
            continue

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        name = "%s.mp4" % uuid.uuid4().hex[:16]
        path = os.path.join(OUTPUT_DIR, name)
        with open(path, "wb") as fh:
            fh.write(data)
        with _lock:
            _jobs[job_id].update({
                "status": "done",
                "url": "/static/video/generated/" + name,
                "model": model,
                "bytes": len(data),
            })
        return

    with _lock:
        _jobs[job_id].update({
            "status": "failed",
            "error": _friendly(last_error),
        })


def _friendly(raw):
    """Provider errors are for operators; this is for the person waiting."""
    low = (raw or "").lower()
    if "quota" in low or "credit" in low or "exceeded" in low or "402" in low:
        return ("The free video allowance for this month is used up. It "
                "resets at the start of next month.")
    if "401" in low or "403" in low or "token" in low:
        return "The video service rejected the server's token."
    if "503" in low or "loading" in low or "cold" in low:
        return ("The video model is warming up on the provider's side. "
                "Try again in a minute.")
    if not raw:
        return "The video service could not make that one."
    return "Video generation failed: %s" % raw[:160]


def start(prompt, seconds=5, fps=16):
    """Queue a generation on a background thread. -> (job_id, error)."""
    if not configured():
        return None, unavailable_reason()
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe the video you want."

    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"status": "running", "url": None, "error": "",
                         "started": time.time()}
    t = threading.Thread(target=_render,
                         args=(job_id, prompt, seconds, fps), daemon=True)
    t.start()
    return job_id, None


def result(job_id):
    """-> (state, url, error) where state is pending|done|failed."""
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return "failed", None, "That render is no longer being tracked."

    if job["status"] == "running":
        if time.time() - job["started"] > TIMEOUT_SECONDS:
            with _lock:
                _jobs[job_id].update({
                    "status": "failed",
                    "error": "The video service took too long. Try again.",
                })
            return "failed", None, "The video service took too long."
        return "pending", None, None

    if job["status"] == "done":
        return "done", job.get("url"), None
    return "failed", None, job.get("error") or "Generation failed."
