"""Higgsfield Cloud - text-to-video and image-to-video.

THE API (docs.higgsfield.ai, OpenAPI 2.0.0, checked 2026-09-13)

  POST https://api.higgsfield.ai/<model path>            -> RequestStatus
  GET  https://api.higgsfield.ai/requests/<id>/status    -> RequestStatus
  POST https://api.higgsfield.ai/requests/<id>/cancel    -> 202
  POST https://api.higgsfield.ai/estimate/<model path>   -> {credits, usd}
  POST https://api.higgsfield.ai/files/generate-upload-url

  RequestStatus = {status: queued|in_progress|completed|failed|nsfw|
                   canceled, request_id, status_url, cancel_url,
                   error, video: {url}}

Auth is `Authorization: Key <id>:<secret>` - the literal word Key, not
Bearer (their FAQ says so in bold). Keys come from cloud.higgsfield.ai.

WHAT IS AND IS NOT THERE, so nothing here pretends otherwise

- Higgsfield's own model (DoP) is image-to-video ONLY. Text-to-video is
  served by partner models behind the same API: Kling, Veo 3.1,
  Seedance, Sora 2, Hailuo, Wan. Each has its own body; MODELS below is
  the exact schema of each, transcribed from the OpenAPI spec, and
  build_body() is the only place a request is assembled - so the
  payload for a model is right by construction or wrong in one place.
- No model takes fps or a scalar "motion strength". Motion intensity is
  expressed in the prompt (videogen's enhancer states it in words),
  through Seedance's camera_fixed, and through DoP's motion presets.
- The result carries a URL and nothing else: no duration, resolution or
  seed comes back. videogen probes the downloaded file for those.
- There is no Retry-After header and no idempotency key. The primary
  limit is CONCURRENCY per account: too many running jobs is a 400
  whose detail names the limit. A submission is therefore never
  repeated after an ambiguous timeout (that could bill twice); it is
  retried only when the server answered and said no.
- Webhooks (`?hf_webhook=`) are unsigned. The app accepts them as a hint
  to poll now, never as the result itself.
- Result URLs are kept for at least seven days. The engine copies the
  clip to this server on completion.

Every function returns (value, error) or raises providers.RateLimited
when the account is at its concurrency limit - the engine then keeps
the job queued locally and resubmits on a backoff, which is what "rate
limit manager" means for an API with a concurrency cap.
"""
from __future__ import annotations

import base64
import json
import os
import random
import time
import uuid
from urllib.parse import urlencode

import requests

from providers import RateLimited

API_BASE = os.environ.get("HIGGSFIELD_API_BASE", "https://api.higgsfield.ai").rstrip("/")

# How many jobs this server keeps in flight at the provider at once.
# Higgsfield's own cap is per account and per plan (shown only in their
# dashboard, 4 on the plan this was written against); set this to it so
# the app queues locally instead of learning the limit from 400s.
MAX_CONCURRENT = int(os.environ.get("HIGGSFIELD_MAX_CONCURRENT", "4") or 4)

# Submissions per minute from this server, on top of concurrency. The
# API publishes no request-rate limit; this is a guard against a bug in
# the caller looping, not a mirror of anything documented.
SUBMITS_PER_MINUTE = int(os.environ.get("HIGGSFIELD_SUBMITS_PER_MINUTE", "20") or 20)

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60

# Retry waits go through this so a check can make them instant.
_sleep = time.sleep

# How long the engine should wait before resubmitting, by reason. The
# API sends no Retry-After, so these are this module's own estimates:
# a concurrency slot frees when a render finishes (minutes), a
# throttle clears in seconds, a model "not ready" is longest.
RETRY_AFTER = {"local": 5, "throttled": 10, "concurrency": 15, "not_ready": 30}


# ------------------------------------------------------------- credentials
def credentials() -> tuple[str, str]:
    """(key id, secret). Accepts the three spellings the docs and SDKs
    use, so a key pasted from any of their examples works."""
    pair = (os.environ.get("HIGGSFIELD_API_KEY")
            or os.environ.get("HF_KEY") or os.environ.get("HF_CREDENTIALS") or "")
    if ":" in pair:
        key, _, secret = pair.strip().partition(":")
        return key.strip(), secret.strip()
    key = (os.environ.get("HIGGSFIELD_API_KEY_ID")
           or os.environ.get("HF_API_KEY_ID") or "").strip()
    secret = (os.environ.get("HIGGSFIELD_API_KEY_SECRET")
              or os.environ.get("HF_API_KEY_SECRET") or "").strip()
    return key, secret


def configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def unavailable_reason() -> str:
    return ("Set HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET on the "
            "server (keys from cloud.higgsfield.ai).")


def _headers() -> dict:
    key, secret = credentials()
    return {
        "Authorization": "Key %s:%s" % (key, secret),
        "Content-Type": "application/json",
        "User-Agent": "randomgenerals-videogen/1.0",
    }


# ------------------------------------------------------------------ models
# One entry per model this app offers, transcribed from the OpenAPI spec
# (docs.higgsfield.ai/docs/openapi.json). Fields:
#   t2v / i2v   the path for each direction, None when the model lacks it
#   seconds     the durations the model accepts (a list), or (lo, hi)
#   ratios      aspect ratios it accepts (empty: it decides)
#   resolutions in this app's vocabulary; RES maps to each model's own
#   family      which build_body branch assembles the request
MODELS = {
    "kling-2.5-turbo": {
        "label": "Kling 2.5 Turbo",
        "family": "kling", "t2v": "/kling-video/v2.5-turbo/pro/text-to-video",
        "i2v": "/kling-video/v2.5-turbo/pro/image-to-video",
        "seconds": [5, 10], "ratios": [], "resolutions": ["720p"],
        "negative": True, "seed": False,
    },
    "kling-2.1-master": {
        "label": "Kling 2.1 Master",
        "family": "kling", "t2v": "/kling-video/v2.1/master/text-to-video",
        "i2v": "/kling-video/v2.1/master/image-to-video",
        "seconds": [5, 10], "ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["720p"], "negative": True, "seed": False,
        "max_prompt": 2500,
    },
    "veo-3.1": {
        "label": "Veo 3.1",
        "family": "veo", "t2v": "/veo3.1", "i2v": "/veo3.1/image-to-video",
        "seconds": [4, 6, 8], "ratios": ["16:9", "9:16"],
        "resolutions": ["720p", "1080p"], "negative": False, "seed": False,
        "audio": True,
    },
    "veo-3.1-fast": {
        "label": "Veo 3.1 Fast",
        "family": "veo", "t2v": "/veo3.1/fast", "i2v": "/veo3.1/fast/image-to-video",
        "seconds": [4, 6, 8], "ratios": ["16:9", "9:16"],
        "resolutions": ["720p", "1080p"], "negative": False, "seed": False,
        "audio": True,
    },
    "seedance-lite": {
        "label": "Seedance 1 Lite",
        "family": "seedance",
        "t2v": "/bytedance/seedance/v1/lite/text-to-video",
        "i2v": "/bytedance/seedance/v1/lite/image-to-video",
        "seconds": (2, 12), "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
        "resolutions": ["480p", "720p", "1080p"], "negative": False, "seed": False,
    },
    "seedance-pro-fast": {
        "label": "Seedance 1 Pro Fast",
        "family": "seedance",
        "t2v": "/bytedance/seedance/v1/pro/fast/text-to-video",
        "i2v": "/bytedance/seedance/v1/pro/fast/image-to-video",
        "seconds": (2, 12), "ratios": ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"],
        "resolutions": ["480p", "720p", "1080p"], "negative": False, "seed": False,
    },
    "sora-2": {
        "label": "Sora 2",
        "family": "sora", "t2v": "/sora-2/text-to-video", "i2v": "/sora-2/image-to-video",
        "seconds": [4, 8, 12], "ratios": ["16:9", "9:16"],
        "resolutions": ["720p"], "negative": False, "seed": False,
    },
    "sora-2-pro": {
        "label": "Sora 2 Pro",
        "family": "sora", "t2v": "/sora-2/text-to-video/pro",
        "i2v": "/sora-2/image-to-video/pro",
        "seconds": [4, 8, 12], "ratios": ["16:9", "9:16"],
        "resolutions": ["720p", "1080p"], "negative": False, "seed": False,
    },
    "hailuo-2.3": {
        "label": "Hailuo 2.3",
        "family": "hailuo", "t2v": "/minimax/hailuo-2.3/standard/text-to-video",
        "i2v": "/minimax/hailuo-2.3/standard/image-to-video",
        "seconds": [6, 10], "ratios": [], "resolutions": ["768p"],
        "negative": False, "seed": False,
    },
    "wan-2.5": {
        "label": "Wan 2.5",
        "family": "wan", "t2v": "/wan-25-preview/text-to-video",
        "i2v": "/wan-25-preview/image-to-video",
        "seconds": [5, 10], "ratios": [], "resolutions": ["480p", "720p", "1080p"],
        "negative": True, "seed": True,
    },
    "dop": {
        "label": "Higgsfield DoP (image to video)",
        "family": "dop", "t2v": None, "i2v": "/higgsfield-ai/dop/standard",
        "seconds": [5], "ratios": [], "resolutions": ["720p"],
        "negative": False, "seed": True,
    },
}
DEFAULT_MODEL = "kling-2.5-turbo"

# This app's resolution names -> what each family's schema spells.
_RES = {
    "veo": {"720p": "720", "1080p": "1080"},
    "seedance": {"480p": "480", "720p": "720", "1080p": "1080"},
    "sora": {"720p": "720p", "1080p": "1080p"},
    "wan": {"480p": "480p", "720p": "720p", "1080p": "1080p"},
}


def capabilities() -> dict:
    """The provider description videogen builds its controls from. The
    provider-level defaults are the union; each model entry narrows."""
    return {
        "kind": "video",
        "label": "Higgsfield",
        "models": [
            {"id": mid, "label": m["label"],
             "seconds": list(m["seconds"]) if isinstance(m["seconds"], list)
             else list(m["seconds"]),
             "seconds_discrete": isinstance(m["seconds"], list),
             "ratios": list(m["ratios"]), "resolutions": list(m["resolutions"]),
             "negative": m["negative"], "seed": m["seed"],
             "text_to_video": bool(m["t2v"]), "image_to_video": bool(m["i2v"])}
            for mid, m in MODELS.items()],
        "default_model": DEFAULT_MODEL,
        "seconds": (2, 12),
        "default_seconds": 5,
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "resolutions": ["480p", "720p", "1080p"],
        "default_resolution": "720p",
        "negative": True,
        "seed": True,
        "motion": True,
        "image_to_video": True,
        "fps": 24,
    }


def snap_seconds(model: dict, seconds: int) -> int:
    """The model's nearest accepted duration. A 6-second request to a
    model that does 5 or 10 makes a 5-second clip rather than a 422."""
    allowed = model["seconds"]
    if isinstance(allowed, tuple):
        return max(allowed[0], min(allowed[1], int(seconds)))
    return min(allowed, key=lambda a: (abs(a - int(seconds)), a))


def build_body(model_id: str, prompt: str, *, seconds: int = 5,
               ratio: str = "16:9", resolution: str = "720p",
               seed: int | None = None, negative: str = "",
               motion: str = "medium", image_url: str | None = None,
               audio: bool = False) -> tuple[str, dict]:
    """(path, JSON body) for one model - the whole payload contract in
    one function. Every field is one the model's schema declares, with
    the value in the schema's own type (Veo's duration is a STRING,
    Kling's an integer; Seedance's resolution is "720", Sora's "720p").
    Raises ValueError for a direction the model does not have."""
    m = MODELS[model_id]
    path = m["i2v"] if image_url else m["t2v"]
    if not path:
        raise ValueError("%s has no %s" % (
            m["label"], "image-to-video" if image_url else "text-to-video"))
    fam = m["family"]
    prompt = prompt[: m.get("max_prompt", 5000)]
    secs = snap_seconds(m, seconds)
    res = _RES.get(fam, {}).get(resolution) or _RES.get(fam, {}).get("720p")
    if ratio not in m["ratios"]:
        ratio = m["ratios"][0] if m["ratios"] else None

    body: dict = {"prompt": prompt}
    if image_url:
        body["image_url"] = image_url

    if fam == "kling":
        body["duration"] = secs
        # cfg_scale is prompt adherence (0-1), not motion; the default
        # is the sensible value and a slider for it is a different feature.
        body["cfg_scale"] = 0.5
        body["negative_prompt"] = negative or ""
        if model_id == "kling-2.1-master" and not image_url and ratio:
            body["aspect_ratio"] = ratio
    elif fam == "veo":
        body["duration"] = str(secs)
        body["resolution"] = res
        body["aspect_ratio"] = ratio or "16:9"
        body["generate_audio"] = bool(audio)
    elif fam == "seedance":
        body["duration"] = secs
        body["resolution"] = res
        body["aspect_ratio"] = ratio or "16:9"
        # The one motion control this family has: a locked-off camera.
        body["camera_fixed"] = motion == "low"
    elif fam == "sora":
        body["duration"] = secs
        body["resolution"] = res if res in m["resolutions"] else "720p"
        body["aspect_ratio"] = ratio or "16:9"
    elif fam == "hailuo":
        body["duration"] = secs
        # Hailuo's own rewriter, off: videogen already engineered the
        # prompt, and two rewrites in a row drift from what was asked.
        body["prompt_optimizer"] = False
    elif fam == "wan":
        body["duration"] = secs
        body["resolution"] = res or "720p"
        body["negative_prompt"] = negative or ""
        body["seed"] = int(seed) % 1000000 if seed is not None else -1
    elif fam == "dop":
        if seed is not None:
            body["seed"] = max(1, int(seed) % 1000000)
        body["enhance_prompt"] = False
    return path, body


# --------------------------------------------------------------- transport
class _Bucket:
    """Submissions per minute, one bucket for the process."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.stamp = time.monotonic()

    def take(self) -> float:
        """0 when allowed, else seconds until the next token."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.capacity / 60)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) * 60 / self.capacity


_bucket = _Bucket(SUBMITS_PER_MINUTE)
_in_flight: set[str] = set()
_RETRYABLE = (429, 500, 502, 503, 504)


def _backoff(attempt: int) -> float:
    """0.5, 1, 2, 4 … seconds with up to 30% jitter, so a fleet of
    retries after an outage does not land as one wave."""
    base = 0.5 * (2 ** attempt)
    return base + random.uniform(0, base * 0.3)


def _request(method: str, url: str, *, json_body: dict | None = None,
             retry_post: bool = False, tries: int = 4) -> tuple[requests.Response | None, str | None]:
    """One call with the retry policy the docs ask for.

    GET is retried on transport errors and 5xx/429. POST is retried ONLY
    on a received 429/5xx (`retry_post`): the server answered and did
    not act, so sending again cannot double-bill. A POST that timed out
    is not repeated - there is no idempotency key, and the job may have
    been created. -> (response, None), the last response when retries
    ran out on a status, or (None, reason) when nothing answered."""
    last_err = None
    last_resp = None
    for attempt in range(tries):
        try:
            r = requests.request(method, url, headers=_headers(), json=json_body,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.exceptions.RequestException as e:
            last_err = ("the service timed out" if isinstance(e, requests.exceptions.Timeout)
                        else "could not reach the service")
            if method == "POST":
                # Ambiguous: the body may have arrived. Not repeated.
                return None, last_err
        else:
            last_resp = r
            if r.status_code not in _RETRYABLE or not (method == "GET" or retry_post):
                return r, None
        if attempt + 1 < tries:
            _sleep(_backoff(attempt))
    if last_resp is not None:
        return last_resp, None
    return None, last_err or "no answer"


def _detail(r: requests.Response) -> str:
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        return (r.text or "")[:200]
    if isinstance(d, list):                       # 422 validation list
        return "; ".join(
            "%s: %s" % (".".join(str(x) for x in e.get("loc", [])[-1:]),
                        e.get("msg", "")) for e in d if isinstance(e, dict))[:300]
    return str(d or "")[:300]


# ------------------------------------------------------------- the calls
def start(prompt: str, seconds: int = 5, quality: str = "720p",
          ratio: str = "16:9", seed: int | None = None, negative: str = "",
          motion: str = "medium", image_url: str | None = None,
          model: str | None = None, webhook_url: str | None = None,
          **_unused) -> tuple[str | None, str | None]:
    """Submit. -> (request_id, None) or (None, reason).
    Raises RateLimited when the account is at its concurrency cap or
    this process is at its own; the engine queues and retries."""
    if not configured():
        return None, unavailable_reason()
    model = model if model in MODELS else DEFAULT_MODEL
    try:
        path, body = build_body(model, prompt, seconds=seconds, ratio=ratio,
                                resolution=quality, seed=seed, negative=negative,
                                motion=motion, image_url=image_url)
    except ValueError as e:
        return None, str(e)

    if len(_in_flight) >= MAX_CONCURRENT:
        raise RateLimited(RETRY_AFTER["local"])
    wait = _bucket.take()
    if wait:
        raise RateLimited(max(wait, RETRY_AFTER["local"]))

    url = API_BASE + path
    if webhook_url:
        url += "?" + urlencode({"hf_webhook": webhook_url})
    r, err = _request("POST", url, json_body=body, retry_post=True)
    if r is None:
        return None, "Could not reach Higgsfield: %s." % err

    if r.status_code == 400:
        detail = _detail(r)
        if "concurrent" in detail.lower():
            raise RateLimited(RETRY_AFTER["concurrency"])
        return None, "Higgsfield rejected the request: %s" % (detail or "bad request")
    if r.status_code in (401,):
        return None, "Higgsfield rejected the server's key."
    if r.status_code == 403:
        return None, "The Higgsfield account is out of credits."
    if r.status_code == 404:
        return None, "That model isn't available to this Higgsfield account."
    if r.status_code == 422:
        return None, "Higgsfield refused the parameters: %s" % _detail(r)
    if r.status_code in (423, 429, 503):
        # Blocked, throttled, or a model that is not ready: all "later",
        # none "no". The engine keeps the job and asks again.
        raise RateLimited(RETRY_AFTER["throttled"] if r.status_code == 429
                          else RETRY_AFTER["not_ready"])
    if r.status_code >= 400:
        return None, "Higgsfield answered %d: %s" % (r.status_code, _detail(r))
    try:
        data = r.json()
    except ValueError:
        return None, "Higgsfield returned an unreadable response."
    ref = data.get("request_id")
    if not ref:
        return None, "Higgsfield did not return a request id."
    _in_flight.add(ref)
    return ref, None


def result(request_id: str) -> tuple[str, str | None, str | None]:
    """-> (state, url, error), state in pending|done|failed.
    Transport trouble is "pending": the render is still running on their
    side and the next poll asks again."""
    if not configured():
        return "failed", None, unavailable_reason()
    r, err = _request("GET", "%s/requests/%s/status" % (API_BASE, request_id))
    if r is None:
        return "pending", None, err
    if r.status_code == 404:
        _in_flight.discard(request_id)
        return "failed", None, "Higgsfield no longer knows that job."
    if r.status_code == 401:
        return "failed", None, "Higgsfield rejected the server's key."
    if r.status_code != 200:
        return "pending", None, "status answered %d" % r.status_code
    try:
        data = r.json()
    except ValueError:
        return "pending", None, None
    status = data.get("status")
    if status in ("queued", "in_progress"):
        return "pending", None, None
    _in_flight.discard(request_id)
    if status == "completed":
        url = (data.get("video") or {}).get("url")
        if not url:
            return "failed", None, "Finished, but no video came back."
        return "done", url, None
    if status == "nsfw":
        return "failed", None, ("Higgsfield refused that on content grounds. "
                                "Try describing something else.")
    if status == "canceled":
        return "failed", None, "The job was cancelled."
    return "failed", None, "Higgsfield could not make that one%s." % (
        (": " + str(data.get("error"))[:160]) if data.get("error") else "")


def cancel(request_id: str) -> bool:
    """Ask for a queued job to be dropped. True when it was; False when
    it had already started (their 400) or could not be asked."""
    if not configured():
        return False
    r, _ = _request("POST", "%s/requests/%s/cancel" % (API_BASE, request_id), tries=2)
    _in_flight.discard(request_id)
    return bool(r is not None and r.status_code in (200, 202))


def estimate(model_id: str, prompt: str, **params) -> tuple[dict | None, str | None]:
    """What a request would cost, without making it. Also the cheapest
    way to prove a payload is accepted: the estimate route validates the
    same body and answers 422 for a wrong parameter."""
    if not configured():
        return None, unavailable_reason()
    try:
        path, body = build_body(model_id, prompt, **params)
    except ValueError as e:
        return None, str(e)
    r, err = _request("POST", API_BASE + "/estimate" + path, json_body=body, retry_post=True)
    if r is None:
        return None, err
    if r.status_code != 200:
        return None, "%d %s" % (r.status_code, _detail(r))
    try:
        return r.json(), None
    except ValueError:
        return None, "unreadable"


UPLOAD_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif")


def upload_image(data: bytes, content_type: str) -> tuple[str | None, str | None]:
    """A local image -> a public URL a model can read. Two steps: ask
    for a presigned URL, PUT the bytes there with the headers they hand
    back (and never our API key - the docs are explicit)."""
    if not configured():
        return None, unavailable_reason()
    if content_type not in UPLOAD_TYPES:
        return None, "Use a JPEG, PNG, WebP or GIF."
    r, err = _request("POST", API_BASE + "/files/generate-upload-url",
                      json_body={"content_type": content_type}, retry_post=True)
    if r is None or r.status_code != 200:
        return None, "Could not get an upload slot (%s)." % (
            err or "%d %s" % (r.status_code, _detail(r)))
    try:
        info = r.json()
    except ValueError:
        return None, "Unreadable upload slot."
    headers = dict(info.get("upload_headers") or {"Content-Type": content_type})
    try:
        put = requests.put(info["upload_url"], data=data, headers=headers,
                           timeout=(CONNECT_TIMEOUT, 120))
    except (requests.exceptions.RequestException, KeyError) as e:
        return None, "Upload failed: %s" % str(e)[:100]
    if put.status_code not in (200, 201, 204):
        return None, "Storage answered %d." % put.status_code
    return info.get("public_url"), None


def decode_data_url(data_url: str) -> tuple[bytes, str] | None:
    """'data:image/png;base64,…' -> (bytes, content type), or None."""
    if not data_url.startswith("data:"):
        return None
    head, _, payload = data_url.partition(",")
    if ";base64" not in head:
        return None
    ctype = head[5:].split(";")[0].strip().lower()
    try:
        return base64.b64decode(payload, validate=True), ctype
    except (ValueError, base64.binascii.Error):
        return None


# --------------------------------------------------------------- self-test
def _selftest() -> int:
    """`python higgsfield_api.py --check`: with real keys, asks the
    estimate route about every model with a canonical body - the one
    check that proves each payload matches what the deployed API
    accepts, without spending a credit."""
    if not configured():
        print("no credentials in the environment;", unavailable_reason())
        return 2
    bad = 0
    for mid, m in MODELS.items():
        for direction in ("t2v", "i2v"):
            if not m[direction]:
                continue
            kw = {"image_url": "https://example.com/in.jpg"} if direction == "i2v" else {}
            est, err = estimate(mid, "A red kite over a wheat field at dusk, slow pan.",
                                seconds=5, ratio="16:9", resolution="720p",
                                negative="text, watermark", motion="medium", **kw)
            ok = est is not None
            bad += not ok
            print("  %s %-18s %s  %s" % ("ok  " if ok else "FAIL", mid, direction,
                                         json.dumps(est) if ok else err))
    print("%d payload(s) rejected" % bad if bad else "every payload accepted")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--check" in sys.argv else 0)
