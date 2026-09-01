"""Tripo - text-to-3D generation.

WHY 3D AND NOT VIDEO

The video bay was built against PixVerse (no free tier at any volume,
about $0.45 a clip) and then Hugging Face, whose free allowance turned
out to be roughly THREE CLIPS A MONTH site-wide - measured, not
estimated: three succeeded and the fourth was refused. A bay offering
two a day on top of that is a promise that fails on the first visitor.

3D is a different economic shape. A mesh is a single asset rather than a
hundred rendered frames, so it costs the provider far less, and Tripo
hands new accounts 2,000 credits at roughly 20 a generation - about a
hundred models before anything is owed. That is enough to actually run
the feature rather than demonstrate it.

THE API

  POST https://api.tripo3d.ai/v2/openapi/task     -> {"data": {"task_id": …}}
  GET  https://api.tripo3d.ai/v2/openapi/task/ID  -> {"data": {"status", "output"}}

Bearer auth, asynchronous, and the finished asset is a GLB at
output.model_url on Tripo's CDN. Same start()/result() shape as the
video backends, so the route above does not care which is running.

The GLB is left where it is rather than copied here: it is a link on
someone else's CDN either way, and downloading several megabytes onto a
two-core VM to serve it again is work that buys nothing.
"""
import os

import requests

API_ROOT = "https://api.tripo3d.ai/v2/openapi"

# What the model is asked for. v2.5 is the current generation and the
# default the platform bills at; naming it rather than relying on the
# server default keeps a silent upgrade from changing the cost.
MODEL_VERSION = "v2.5-20250123"

# Roughly what a generation costs at the time of writing. Used only to
# tell someone how much of their allowance is left in units they can
# reason about - the provider is the authority on the real figure.
CREDITS_PER_MODEL = 20


def api_key():
    return os.environ.get("TRIPO_API_KEY", "").strip()


def configured():
    return bool(api_key())


def unavailable_reason():
    if not api_key():
        return ("3D generation needs a Tripo API key - set TRIPO_API_KEY "
                "on the server. New accounts get 2,000 free credits.")
    return ""


def _headers():
    return {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
    }


def start(prompt, seconds=None):
    """Queue a generation. -> (task_id, error).

    `seconds` is accepted and ignored so this is a drop-in for the video
    backends the route already knows how to call. A mesh has no duration.
    """
    if not configured():
        return None, unavailable_reason()

    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe the object you want."
    if len(prompt) > 900:
        return None, "That description is too long. Try a shorter one."

    body = {
        "type": "text_to_model",
        "prompt": prompt,
        "model_version": MODEL_VERSION,
    }
    try:
        r = requests.post(API_ROOT + "/task", json=body,
                          headers=_headers(), timeout=45)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach the 3D service: %s" % e

    if r.status_code in (401, 403):
        return None, "The 3D service rejected the key."
    try:
        data = r.json()
    except ValueError:
        return None, "The 3D service returned an unreadable response."

    # Tripo answers 200 with a non-zero `code` for most failures, so the
    # HTTP status is not the check that matters.
    if data.get("code") not in (0, None):
        return None, _friendly(data.get("message") or str(data.get("code")))

    task = (data.get("data") or {}).get("task_id")
    if not task:
        return None, "The 3D service did not return a job id."
    return task, None


def result(task_id):
    """-> (state, url, error) where state is pending|done|failed."""
    if not configured():
        return "failed", None, unavailable_reason()

    try:
        r = requests.get("%s/task/%s" % (API_ROOT, task_id),
                         headers=_headers(), timeout=30)
    except requests.exceptions.RequestException as e:
        # A blip mid-poll is not a failed generation - the job is still
        # running on their side, so this stays pending and tries again.
        return "pending", None, "Could not reach the 3D service: %s" % e

    try:
        data = r.json()
    except ValueError:
        return "pending", None, None

    if data.get("code") not in (0, None):
        return "failed", None, _friendly(data.get("message") or "")

    body = data.get("data") or {}
    status = (body.get("status") or "").lower()

    if status == "success":
        out = body.get("output") or {}
        url = (out.get("pbr_model") or out.get("model")
               or out.get("model_url") or out.get("base_model"))
        if not url:
            return "failed", None, "Finished, but no model came back."
        return "done", url, None

    if status in ("failed", "cancelled", "banned", "unknown"):
        if status == "banned":
            return "failed", None, (
                "The 3D service refused that prompt on content grounds. "
                "Try describing something else.")
        return "failed", None, "The 3D service could not make that one."

    return "pending", None, None


def _friendly(raw):
    """Provider errors are for operators; this is for the person waiting."""
    low = (raw or "").lower()
    if "credit" in low or "balance" in low or "insufficient" in low:
        return ("The 3D generation credits on this server have run out. "
                "The owner needs to top them up.")
    if "unauthorized" in low or "invalid" in low and "key" in low:
        return "The 3D service rejected the server's key."
    if not raw:
        return "3D generation failed."
    return "3D generation failed: %s" % raw[:160]
