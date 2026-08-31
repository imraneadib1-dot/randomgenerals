"""PixVerse - text-to-video generation.

WHY THIS IS GATED HARDER THAN ANYTHING ELSE IN THE APP

Every other feature here is either free to run (Pollinations, DuckDuckGo)
or free within a rate limit that costs nothing to hit (Groq, Ollama). A
generation is the first thing this app does that spends real money on
every single call - about $0.13 for a five-second clip through a reseller,
and up to $0.15 a second direct.

Against a $1.99 subscription that is roughly fifteen clips before the
month is underwater. So this is Pro-only AND separately quota'd: credits
cannot be spent on it, because a credit balance is designed to refill and
this cost is not. The quota is a hard monthly count that does not refill,
does not roll over, and cannot be topped up by waiting.

The module reports itself unavailable with no key set, and nothing in the
app calls it in that state - so a deployment without PIXVERSE_API_KEY
costs nothing and simply does not offer the feature.

THE API

  POST /openapi/v2/video/text/generate  -> {"Resp": {"video_id": N}}
  GET  /openapi/v2/video/result/{id}    -> {"Resp": {"status": N, "url": …}}

Both need an API-KEY header and an Ai-trace-id, which PixVerse requires
to be unique per request. Generation is asynchronous: the first call
returns an id immediately and the video arrives minutes later, so this
exposes start/poll rather than pretending to be synchronous.
"""
import os
import uuid

import requests

API_ROOT = "https://app-api.pixverse.ai/openapi/v2"

# v6 is the current engine and the only one that does arbitrary lengths -
# the older models accept 5 or 8 seconds and nothing else, which makes a
# duration control that lies about what it does.
DEFAULT_MODEL = "v6"

# Every value the API accepts, so the UI can offer exactly these and the
# server can reject anything else without a second table to maintain.
MODELS = ("v6", "c1", "v5.6", "v5.5", "v5")
QUALITIES = ("360p", "540p", "720p", "1080p")
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")

# v6 allows 1-15. Capped lower here because cost is per second and the
# difference between 8 and 15 seconds is a doubling of the bill for a
# clip most people will use as a background or a title card.
MIN_SECONDS = 1
MAX_SECONDS = 8
DEFAULT_SECONDS = 5

# PixVerse's own status codes, named so the polling code reads as
# something other than magic numbers.
STATUS_DONE = 1
STATUS_PENDING = 5
STATUS_REJECTED = 7
STATUS_FAILED = 8


def api_key():
    return os.environ.get("PIXVERSE_API_KEY", "").strip()


def configured():
    return bool(api_key())


def _headers():
    return {
        "API-KEY": api_key(),
        # Required, and required to differ per call. PixVerse uses it to
        # de-duplicate, so reusing one silently returns the earlier job.
        "Ai-trace-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def clamp_seconds(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, n))


def start(prompt, seconds=DEFAULT_SECONDS, quality="720p",
          ratio="16:9", model=DEFAULT_MODEL, seed=None):
    """Queue a generation. -> (video_id, error).

    Returns as soon as PixVerse accepts the job; the clip does not exist
    yet. Callers poll result() for it.
    """
    if not configured():
        return None, "Video generation is not configured on this server."

    prompt = (prompt or "").strip()
    if not prompt:
        return None, "Describe the video you want."
    if len(prompt) > 5000:
        return None, "That description is too long."

    body = {
        "prompt": prompt,
        "model": model if model in MODELS else DEFAULT_MODEL,
        "duration": clamp_seconds(seconds),
        "quality": quality if quality in QUALITIES else "720p",
        "aspect_ratio": ratio if ratio in RATIOS else "16:9",
    }
    if seed is not None:
        body["seed"] = int(seed) % 2147483647

    try:
        r = requests.post(API_ROOT + "/video/text/generate", json=body,
                          headers=_headers(), timeout=45)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach the video service: %s" % e

    if r.status_code in (401, 403):
        return None, "The video service rejected the key."
    try:
        data = r.json()
    except ValueError:
        return None, "The video service returned an unreadable response."

    # PixVerse answers 200 with a non-zero ErrCode for most failures, so
    # the HTTP status is not the check that matters.
    if data.get("ErrCode"):
        return None, "Video service error: %s" % (
            data.get("ErrMsg") or data["ErrCode"])

    vid = (data.get("Resp") or {}).get("video_id")
    if not vid:
        return None, "The video service did not return a job id."
    return vid, None


def result(video_id):
    """-> (state, url, error) where state is pending|done|failed.

    `error` is set only for a transport or account problem. A rejected or
    failed generation comes back as state="failed" with a readable reason
    in `url`'s place, because from the caller's side those are outcomes
    rather than exceptions.
    """
    if not configured():
        return "failed", None, "Video generation is not configured."

    try:
        r = requests.get("%s/video/result/%s" % (API_ROOT, video_id),
                         headers=_headers(), timeout=30)
    except requests.exceptions.RequestException as e:
        # Transport failure mid-poll is not a failed generation - the job
        # is still running on their side, so this stays "pending" and the
        # next poll tries again.
        return "pending", None, "Could not reach the video service: %s" % e

    try:
        data = r.json()
    except ValueError:
        return "pending", None, None

    if data.get("ErrCode"):
        return "failed", None, data.get("ErrMsg") or "Generation failed."

    resp = data.get("Resp") or {}
    status = resp.get("status")
    if status == STATUS_DONE:
        url = resp.get("url")
        if not url:
            return "failed", None, "Finished, but no video came back."
        return "done", url, None
    if status == STATUS_REJECTED:
        return "failed", None, (
            "The video service refused that prompt on content grounds. "
            "Try describing something else.")
    if status == STATUS_FAILED:
        return "failed", None, "The video service could not make that one."
    return "pending", None, None
