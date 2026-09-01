"""Tripo - text-to-3D generation.

WHY 3D AND NOT VIDEO

The video bay was built against PixVerse (no free tier at any volume,
about $0.45 a clip) and then Hugging Face, whose free allowance turned
out to be roughly THREE CLIPS A MONTH site-wide - measured, not
estimated: three succeeded and the fourth was refused. A bay offering
two a day on top of that is a promise that fails on the first visitor.

3D is a cheaper thing to make - a mesh is a single asset rather than a
hundred rendered frames - but CHEAPER IS NOT FREE, and this module was
written believing it was. A search result said new Tripo accounts get
2,000 credits; the balance endpoint on a real new account returns
{"balance": 0}. The claim was repeated here and in the setup copy before
anyone checked it, which is the wrong order.

So this needs a funded account. It is still the cheapest generative bay
on offer - a model is around $0.20 against $0.45 for a PixVerse clip -
but nothing here is free, and balance() exists so the app can say that
before someone waits on a job that cannot start.

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
                "on the server.")
    return ""


def _headers():
    return {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
    }


def balance():
    """Credits left on the account, or None if it cannot be read.

    Worth a call: Tripo answers 403 for an empty balance as readily as
    for a bad key, so without this the app cannot tell "you have not
    paid" from "your key is wrong" - and sends whoever is debugging it
    to regenerate a key that was never the problem.
    """
    if not configured():
        return None
    try:
        r = requests.get(API_ROOT + "/user/balance",
                         headers=_headers(), timeout=15)
        data = r.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if data.get("code") not in (0, None):
        return None
    return (data.get("data") or {}).get("balance")


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

    # The BODY first, then the status. Tripo answers 403 for "no credit"
    # as well as for a bad key, and reading the status alone reported an
    # empty balance as a rejected key - which sends whoever is debugging
    # it to regenerate a key that was never the problem.
    try:
        data = r.json()
    except ValueError:
        if r.status_code in (401, 403):
            return None, "The 3D service rejected the key."
        return None, "The 3D service returned an unreadable response."
    if data.get("code") not in (0, None):
        return None, _friendly(data.get("message") or str(data.get("code")))
    if r.status_code in (401, 403):
        return None, "The 3D service rejected the key."

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
