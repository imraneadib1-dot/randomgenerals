"""The video generation engine: one job model over several providers.

    import videogen
    videogen.register("higgsfield", higgsfield_api)
    videogen.start_poller(on_failed=refund)          # once, at boot
    job = videogen.submit(owner, spec, "higgsfield", turn=llm)

WHAT THIS REPLACES

Video jobs lived in a dict in app.py. The browser polled a route, the
route asked the provider, and whatever came back was shown once and
forgotten: a restart lost every running job (the provider kept
rendering; nobody was left to collect the result), there was no history,
the provider's URL was handed to the browser as-is (it expires), and
nothing checked that the URL pointed at a playable file of the shape
that was asked for.

WHAT IT IS NOW

  submit()      writes a row (db.video_jobs), engineers the prompt, hands
                the request to the provider. A provider that is at its
                concurrency limit leaves the job QUEUED here; the poller
                resubmits it on a backoff. That is the rate-limit
                manager: the app never learns a limit by being refused
                twice.
  poller        one background thread; asks each running job's provider
                on a per-job backoff, and when a clip is ready DOWNLOADS
                it to this server, VALIDATES it (container signature,
                and with ffprobe the real resolution and duration against
                what was asked), and only then marks the job done
  enhance()     the prompt-engineering step: a short request expanded
                into a cinematographer's brief - camera path, lighting,
                motion - as validated JSON, with the plain request kept
                when the model does anything unexpected
  Spec          every parameter a request can carry, clamped to what the
                chosen provider AND model accept, so a bad value never
                reaches an API that would charge for the attempt

A provider is a module with this shape (checked at register()):
  configured() -> bool
  capabilities() -> dict      see caps_for()
  start(prompt, seconds=, quality=, ratio=, seed=, negative=, motion=,
        image_url=, model=, webhook_url=, **ignored) -> (ref, err)
        may raise providers.RateLimited(seconds)
  result(ref) -> (pending|done|failed, url, err)
  cancel(ref) -> bool          optional
pixverse.py, hfvideo.py and tripo3d.py have it; higgsfield_api.py is
the one this was built for.

ONE PROCESS. The poller is a thread and the job table is shared, so
two processes would each pick up the same due jobs and download every
clip twice. This deployment runs gunicorn with --workers 1 (threads
carry the concurrency), which makes that correct; if the worker count
ever changes, the poller needs a claim column (UPDATE … WHERE next_poll
<= now RETURNING) or to move to one dedicated process.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

import requests

import db
from providers import RateLimited

# Where finished clips are kept. Provider URLs expire - PixVerse's after
# a day, Higgsfield's after a week - so the clip a person made is copied
# here the moment it is ready, and that is what the dashboard plays and
# what Download serves.
OUTPUT_DIR = os.path.join("static", "video", "generated")

# How often the poller wakes, and how a job's own interval grows between
# asks: a render that takes three minutes should not be asked about
# sixty times. Capped so a finished clip is never more than half a
# minute from being noticed.
POLL_TICK_SECONDS = 3
POLL_INTERVALS = (3, 5, 8, 13, 20, 30)

# A job older than this that still has no clip is failed and refunded.
# The slowest provider here completes in a few minutes; fifteen is well
# past "slow" and into "lost".
JOB_TIMEOUT_SECONDS = 15 * 60

# A job the provider would not accept yet (concurrency, a 503) waits
# this long between attempts, doubling, and is failed after the last.
RESUBMIT_INTERVALS = (5, 10, 20, 40, 80, 160)

# Every ratio and resolution any provider here can be asked for. A
# provider's own capabilities() narrows these; a request naming
# something outside them is clamped to the provider's default rather
# than sent and charged for.
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
RESOLUTIONS = ("480p", "540p", "720p", "768p", "1080p")
MOTIONS = ("low", "medium", "high")
SEED_RANGE = 1_000_000

PROVIDER_SHAPE = ("configured", "start", "result", "capabilities")

# The retry waits go through this so a check can make them instant
# without touching time.sleep for the whole process.
_sleep = time.sleep


# --------------------------------------------------------------- the spec
@dataclass
class Spec:
    """Everything a request can say. Built from the API payload by
    parse_spec(), which clamps every field; a Spec is therefore always
    something the provider and model can be asked for."""
    prompt: str
    negative: str = ""
    seconds: int = 5
    ratio: str = "16:9"
    resolution: str = "720p"
    seed: int | None = None
    motion: str = "medium"
    fps: int = 24
    image_url: str | None = None          # image-to-video, when supported
    model: str | None = None              # provider-specific model id
    enhance: bool = True
    camera: str = ""                      # optional camera-path hint
    seeded: bool = True                   # False: the model takes no seed

    def public(self) -> dict:
        return asdict(self)


def caps_for(provider, model_id: str | None) -> dict:
    """The provider's capabilities with the chosen model's own limits
    laid over them. `models` in a provider's capabilities() is a list of
    {id, label, …overrides}; a provider with no per-model differences
    lists ids and labels only."""
    caps = dict(provider.capabilities())
    entries = caps.get("models") or []
    caps["models"] = [m if isinstance(m, dict) else {"id": m, "label": m}
                      for m in entries]
    ids = [m["id"] for m in caps["models"]]
    chosen = model_id if model_id in ids else caps.get("default_model")
    caps["model"] = chosen
    for m in caps["models"]:
        if m["id"] == chosen:
            for key in ("seconds", "seconds_discrete", "ratios", "resolutions",
                        "negative", "seed", "image_to_video", "text_to_video"):
                if key in m:
                    caps[key] = m[key]
            break
    return caps


def parse_spec(payload: dict, caps: dict) -> Spec:
    """A request body -> a Spec the provider can accept.

    Clamped, not validated-and-refused: a slider one notch past what
    the model takes should make the clip at the model's maximum, not
    fail after the person waited to find out. Anything genuinely
    unusable (an empty prompt) is caught by the route before this.
    """
    def pick(value: Any, allowed, default: Any) -> Any:
        allowed = tuple(allowed or ())
        if value in allowed:
            return value
        return default if default in allowed or not allowed else allowed[0]

    seconds_spec = caps.get("seconds") or (1, 8)
    try:
        seconds = int(payload.get("seconds") or caps.get("default_seconds", 5))
    except (TypeError, ValueError):
        seconds = int(caps.get("default_seconds", 5))
    if caps.get("seconds_discrete"):
        allowed = [int(s) for s in seconds_spec]
        seconds = min(allowed, key=lambda a: (abs(a - seconds), a))
    else:
        lo, hi = seconds_spec[0], seconds_spec[-1]
        seconds = max(int(lo), min(int(hi), seconds))

    # One seed range for every provider (the tightest here is 1..1e6,
    # Wan and DoP), so the seed a job records is the seed that was sent
    # and "reuse seed" means what it says.
    seed = payload.get("seed")
    try:
        seed = (int(seed) % SEED_RANGE or 1) if seed not in (None, "") else None
    except (TypeError, ValueError):
        seed = None

    # An explicitly empty list means the model decides the shape (Kling
    # 2.5, Hailuo): the request records no ratio, and validation does
    # not hold the result to one. A missing key means "anything".
    ratios = caps.get("ratios")
    if ratios is None:
        ratios = RATIOS
    resolutions = caps.get("resolutions") or RESOLUTIONS
    model_ids = [m["id"] for m in caps.get("models") or []]
    image_url = payload.get("image_url")
    return Spec(
        prompt=str(payload.get("prompt") or "").strip()[:2000],
        negative=(str(payload.get("negative") or "").strip()[:500]
                  if caps.get("negative", True) else ""),
        seconds=seconds,
        ratio=pick(payload.get("ratio"), ratios, "16:9") if ratios else "",
        resolution=pick(payload.get("resolution") or payload.get("quality"),
                        resolutions, caps.get("default_resolution", "720p")),
        seed=seed if caps.get("seed", True) else None,
        motion=pick(payload.get("motion"), MOTIONS, "medium"),
        fps=int(caps.get("fps", 24)),
        image_url=(str(image_url)[:500]
                   if image_url and caps.get("image_to_video") else None),
        model=pick(payload.get("model"), model_ids, caps.get("default_model"))
        if model_ids else None,
        enhance=bool(payload.get("enhance", True)),
        camera=str(payload.get("camera") or "").strip()[:200],
        seeded=bool(caps.get("seed", True)),
    )


# ---------------------------------------------------- prompt engineering
# The "prompt and motion engineering" step. A person types "a fox in
# snow"; a video model does far better with a shot: subject, action,
# setting, camera path, lens, lighting, movement, mood, and what to
# avoid. The model writes that brief as JSON so each part can be checked
# and the composed prompt is deterministic given the brief.
ENHANCE_SYSTEM = (
    "You are a cinematographer writing a brief for a text-to-video model. "
    "Given a short request, return ONLY a JSON object with these string "
    "fields, no prose around it:\n"
    '  "subject": who or what, in concrete visual terms\n'
    '  "action": what happens over the clip, one continuous motion\n'
    '  "setting": place, time of day, weather, era\n'
    '  "camera": one camera move and lens, e.g. "slow dolly-in, 35mm, '
    'eye level" or "static wide shot"\n'
    '  "lighting": the light source, direction and quality\n'
    '  "style": film stock or look, colour palette, mood, 3-6 words\n'
    '  "negative": what to avoid, comma-separated (artifacts, text, '
    'extra limbs, flicker)\n'
    "Rules: keep the person's subject and intent exactly; add detail, "
    "never a different scene. No dialogue, no on-screen text. One shot, "
    "not a sequence. Each field under 30 words. If the request already "
    "names a camera move or lighting, keep it."
)

ENHANCE_FIELDS = ("subject", "action", "setting", "camera", "lighting",
                  "style", "negative")


def enhance(spec: Spec, turn: Callable | None) -> dict:
    """The brief, as a dict, with `prompt` composed from it.

    `turn(system, user) -> str | None` is whatever text model the caller
    has - injected, so this module never imports app.py. Every failure -
    no model, a refusal, invalid JSON, a field that is not a string, a
    brief that drops the subject - returns the person's own words
    unchanged: this improves a clip, it is not allowed to stop one.
    """
    base = {"prompt": spec.prompt, "negative": spec.negative, "enhanced": False}
    if not spec.enhance or not turn or not spec.prompt:
        return base
    # A long, careful request is already a brief. Rewriting it would be
    # this app overruling the person.
    if len(spec.prompt) > 400:
        return base
    try:
        raw = turn(ENHANCE_SYSTEM, spec.prompt + (
            ("\nCamera hint: " + spec.camera) if spec.camera else "")
            + ("\nMotion intensity: " + spec.motion))
    except Exception:                            # noqa: BLE001
        return base
    brief = _parse_brief(raw or "")
    if not brief:
        return base
    composed = compose_prompt(brief, spec)
    if not composed or len(composed) > 1500:
        return base
    negative = brief.get("negative") or ""
    if spec.negative:
        negative = ", ".join(x for x in (spec.negative, negative) if x)
    return {"prompt": composed, "negative": negative[:500], "enhanced": True,
            "brief": brief}


def _parse_brief(raw: str) -> dict | None:
    """The model's JSON, validated. Tolerates a fenced block around it
    and nothing else; anything malformed is None, never a guess."""
    text = raw.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key in ENHANCE_FIELDS:
        value = data.get(key, "")
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())[:300]
        out[key] = value
    if not out["subject"]:
        return None
    return out


def compose_prompt(brief: dict, spec: Spec) -> str:
    """Brief -> one prompt string, always in the same order, so the same
    brief produces the same prompt (determinism the model itself does
    not offer). The motion intensity the person chose is stated in
    words the video models respond to - none of them takes it as a
    number."""
    motion_words = {
        "low": "subtle, minimal motion, near-still",
        "medium": "natural, moderate motion",
        "high": "dynamic, energetic, pronounced motion",
    }[spec.motion]
    parts = [brief["subject"]]
    if brief.get("action"):
        parts.append(brief["action"])
    if brief.get("setting"):
        parts.append(brief["setting"])
    if brief.get("camera"):
        parts.append("Camera: " + brief["camera"])
    if brief.get("lighting"):
        parts.append("Lighting: " + brief["lighting"])
    if brief.get("style"):
        parts.append("Style: " + brief["style"])
    parts.append("Motion: " + motion_words)
    return ". ".join(p.rstrip(".") for p in parts if p) + "."


# ------------------------------------------------------------- the store
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def _after(seconds: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).replace(
                microsecond=0).isoformat()


def _log(job_id: str, text: str) -> None:
    db.video_job_log(job_id, _now(), text)


def _age(job: dict) -> float:
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.datetime.fromisoformat(job["created"])).total_seconds()


_providers: dict[str, Any] = {}
_on_failed: Callable[[dict], None] | None = None
_poller_started = False
_webhook_url: Callable[[str], str | None] | None = None


def register(name: str, module) -> None:
    """A provider module, checked for the shape the poller relies on."""
    missing = [a for a in PROVIDER_SHAPE if not hasattr(module, a)]
    if missing:
        raise TypeError("provider %s lacks %s" % (name, missing))
    _providers[name] = module


def providers() -> dict[str, Any]:
    return dict(_providers)


def submit(owner_id: str, spec: Spec, backend: str,
           turn: Callable | None = None, extra: dict | None = None) -> dict:
    """Record the job, engineer the prompt, hand it to the provider.
    -> the job row: "running" when accepted, "queued" when the provider
    is at its limit and the poller will resubmit, "failed" with `error`
    when it was refused outright (the caller refunds). `extra` is kept
    with the job's parameters for the caller's own bookkeeping (the
    quota period it was counted against)."""
    provider = _providers[backend]
    job_id = uuid.uuid4().hex[:12]
    now = _now()
    params = {
        "seconds": spec.seconds, "ratio": spec.ratio,
        "resolution": spec.resolution, "motion": spec.motion,
        "fps": spec.fps, "model": spec.model, "camera": spec.camera,
        "image_url": spec.image_url,
        "kind": provider.capabilities().get("kind", "video"),
    }
    params.update(extra or {})
    db.video_job_create({
        "id": job_id, "owner_id": owner_id, "backend": backend,
        "status": "queued", "prompt": spec.prompt, "negative": spec.negative,
        "params_json": json.dumps(params),
        "seed": spec.seed, "created": now, "updated": now,
    })
    _log(job_id, "Submitted to %s%s." % (
        backend, (" (%s)" % spec.model) if spec.model else ""))

    brief = enhance(spec, turn)
    if brief["enhanced"]:
        db.video_job_update(job_id, enhanced=brief["prompt"],
                            negative=brief["negative"])
        _log(job_id, "Prompt engineered: camera, lighting and motion added.")
    # A seed is chosen here, not left to the provider, so the person can
    # reuse it - unless the model takes none, in which case recording
    # one would promise a reproducibility that does not exist.
    if spec.seeded:
        seed = spec.seed if spec.seed is not None else random.randrange(1, SEED_RANGE)
        db.video_job_update(job_id, seed=seed)

    _try_start(db.video_job_get(job_id))
    return db.video_job_get(job_id)


def _try_start(job: dict) -> None:
    """Hand a queued job to its provider. Moves it to running, keeps it
    queued with a later `next_poll` when the provider is at its limit,
    or fails it."""
    provider = _providers.get(job["backend"])
    if provider is None:
        _fail(job, "The %s backend is no longer configured on this server."
              % job["backend"])
        return
    if _age(job) > JOB_TIMEOUT_SECONDS:
        _fail(job, "The provider would not take it within %d minutes."
              % (JOB_TIMEOUT_SECONDS // 60))
        return
    params = job.get("params") or {}
    attempts = int(job.get("polls") or 0)
    try:
        ref, err = provider.start(
            job.get("enhanced") or job["prompt"],
            seconds=params.get("seconds", 5), quality=params.get("resolution", "720p"),
            ratio=params.get("ratio") or "16:9", seed=job.get("seed"),
            negative=job.get("negative") or "", motion=params.get("motion", "medium"),
            image_url=params.get("image_url"), model=params.get("model"),
            webhook_url=_webhook_url(job["id"]) if _webhook_url else None)
    except RateLimited as e:
        if attempts >= len(RESUBMIT_INTERVALS):
            _fail(job, "The provider stayed busy for too long. Try again later.")
            return
        wait = RESUBMIT_INTERVALS[attempts]
        try:
            wait = max(wait, float(e.args[0]))
        except (IndexError, TypeError, ValueError):
            pass
        db.video_job_update(job["id"], status="queued", polls=attempts + 1,
                            updated=_now(), next_poll=_after(wait))
        _log(job["id"], "Provider is at its limit; retrying in %ds." % wait)
        return
    except Exception as e:                       # noqa: BLE001
        _fail(job, "Could not submit: %s" % str(e)[:160])
        return
    if err:
        _fail(job, err)
        return
    db.video_job_update(job["id"], status="running", provider_ref=str(ref),
                        polls=0, updated=_now(), next_poll=_after(POLL_INTERVALS[0]))
    _log(job["id"], "Accepted by %s as %s. Rendering…" % (job["backend"], ref))


# ----------------------------------------------------------- the poller
def start_poller(on_failed: Callable[[dict], None] | None = None,
                 webhook_url: Callable[[str], str | None] | None = None) -> None:
    """One daemon thread for the whole process. `on_failed` is called
    once per job that fails after being accepted - the route uses it to
    give the month's quota back. `webhook_url(job_id)` returns the
    public URL a provider may POST to when a job ends, or None."""
    global _poller_started, _on_failed, _webhook_url
    _on_failed = on_failed
    _webhook_url = webhook_url
    if _poller_started:
        return
    _poller_started = True
    threading.Thread(target=_poll_forever, daemon=True,
                     name="videogen-poller").start()


def _poll_forever() -> None:
    while True:
        try:
            poll_once()
        except Exception as e:                   # noqa: BLE001
            print("[videogen] poll failed: %s" % e)
        time.sleep(POLL_TICK_SECONDS)


def poll_once() -> int:
    """Ask about every job that is due. -> how many were touched.
    Public so a check can drive the poller without the thread."""
    due = db.video_jobs_due(_now())
    for job in due:
        if job["status"] == "queued":
            _try_start(job)
        else:
            _advance(job)
    return len(due)


def nudge(provider_ref: str) -> bool:
    """A webhook said this job ended: poll it now rather than at the
    next backoff step. The webhook's own content is not trusted - the
    provider is asked. -> whether a job with that ref exists."""
    job = db.video_job_by_ref(provider_ref)
    if not job:
        return False
    if job["status"] in ("running", "queued"):
        db.video_job_update(job["id"], next_poll=_now())
    return True


def _advance(job: dict) -> None:
    provider = _providers.get(job["backend"])
    if provider is None:
        _fail(job, "The %s backend is no longer configured on this server."
              % job["backend"])
        return
    age = _age(job)
    if age > JOB_TIMEOUT_SECONDS:
        _fail(job, "Gave up after %d minutes with no clip." % (JOB_TIMEOUT_SECONDS // 60))
        return

    polls = int(job.get("polls") or 0) + 1
    state, url, err = provider.result(job["provider_ref"])
    if state == "pending":
        interval = POLL_INTERVALS[min(polls, len(POLL_INTERVALS) - 1)]
        db.video_job_update(job["id"], polls=polls, updated=_now(),
                            next_poll=_after(interval))
        if polls == 1:
            _log(job["id"], "Queued at the provider.")
        elif polls % 4 == 0:
            _log(job["id"], "Still rendering (%ds)." % int(age))
        return
    if state == "failed":
        _fail(job, err or "The provider could not make that one.")
        return

    # Ready. The URL is the provider's; what the dashboard gets is a
    # copy on this server that has been checked.
    db.video_job_update(job["id"], status="validating", url=url,
                        polls=polls, updated=_now(), next_poll=None)
    _log(job["id"], "Ready at the provider. Fetching and checking it…")
    try:
        path, meta = fetch_and_validate(url, job)
    except ValidationError as e:
        _fail(job, "The result came back wrong: %s" % e)
        return
    db.video_job_update(
        job["id"], status="done", local_path=path, finished=_now(),
        updated=_now(), width=meta.get("width"), height=meta.get("height"),
        duration=meta.get("duration"), bytes=meta.get("bytes"))
    if meta.get("width"):
        _log(job["id"], "Done: %dx%d, %.1fs, %s." % (
            meta["width"], meta["height"], meta.get("duration") or 0,
            _human_bytes(meta.get("bytes") or 0)))
    else:
        _log(job["id"], "Done: %s." % _human_bytes(meta.get("bytes") or 0))


def _fail(job: dict, error: str) -> None:
    db.video_job_update(job["id"], status="failed", error=error[:400],
                        finished=_now(), updated=_now(), next_poll=None)
    _log(job["id"], "Failed: " + error)
    if _on_failed:
        try:
            _on_failed(job)
        except Exception as e:                   # noqa: BLE001
            print("[videogen] on_failed raised: %s" % e)


def cancel(job: dict) -> bool:
    """Stop a job that has not finished. Asks the provider when it can
    be asked; marks the row cancelled either way. -> whether the
    provider confirmed (a queued-here job needs no provider)."""
    provider = _providers.get(job["backend"])
    confirmed = job["status"] == "queued"
    if job["status"] == "running" and provider is not None and hasattr(provider, "cancel"):
        try:
            confirmed = bool(provider.cancel(job["provider_ref"]))
        except Exception:                        # noqa: BLE001
            confirmed = False
    db.video_job_update(job["id"], status="cancelled", finished=_now(),
                        updated=_now(), next_poll=None)
    _log(job["id"], "Cancelled." if confirmed else
         "Cancelled here; the provider had already started it.")
    return confirmed


# ------------------------------------------------------------ validation
class ValidationError(Exception):
    """The provider said done and handed back something that is not the
    clip that was asked for."""


# The first bytes of the containers a provider may return. A 200 with an
# HTML error page, or a zero-byte file, fails here rather than being
# handed to a <video> that shows nothing.
_MP4_BRAND = b"ftyp"
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
_GLB_MAGIC = b"glTF"

# Under this a "video" is a stub. A one-second 480p clip from any of
# these models is well over it; an error page is well under.
MIN_BYTES = 20_000


def fetch_and_validate(url: str, job: dict) -> tuple[str, dict]:
    """Bring the result to OUTPUT_DIR and check it. -> (web path,
    metadata). Raises ValidationError with a reason otherwise.

    Three tiers of checking, each as strict as the tools allow:
      1. it is a video (or, for a 3D backend, a GLB) - container
         signature in the first 64 bytes
      2. it is not trivially small
      3. with ffprobe on the machine: the real width, height, duration,
         and that they match the shape that was asked for - a 9:16
         request answered with a 16:9 file is a wrong render, not a
         detail
    """
    kind = (job.get("params") or {}).get("kind", "video")
    if url and url.startswith("/static/"):
        # A backend that rendered onto this disk (hfvideo). Adopted, not
        # downloaded.
        src = url.lstrip("/").replace("/", os.sep)
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise ValidationError("local file missing (%s)" % e)
        try:
            os.remove(src)
        except OSError:
            pass
    else:
        if not url or not re.match(r"^https?://", url):
            raise ValidationError("no usable URL (%r)" % (url or "")[:80])
        data = _download(url)

    if len(data) < MIN_BYTES:
        raise ValidationError("file is only %d bytes" % len(data))
    head = data[:64]
    if kind == "model":
        if not head.startswith(_GLB_MAGIC):
            raise ValidationError("not a GLB model (starts %r)" % head[:12])
        ext = ".glb"
    elif _MP4_BRAND in head:
        ext = ".mp4"
    elif head.startswith(_WEBM_MAGIC):
        ext = ".webm"
    else:
        raise ValidationError("not an MP4 or WebM (starts %r)" % head[:12])

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, job["id"] + ext)
    with open(path, "wb") as fh:
        fh.write(data)

    meta: dict[str, Any] = {"bytes": len(data)}
    if ext == ".glb":
        return "/" + path.replace(os.sep, "/"), meta
    probed = probe(path)
    if probed:
        meta.update(probed)
        params = job.get("params") or {}
        want = params.get("ratio")
        if want and probed.get("width") and probed.get("height"):
            if not ratio_matches(want, probed["width"], probed["height"]):
                raise ValidationError(
                    "asked for %s, got %dx%d" % (want, probed["width"],
                                                  probed["height"]))
        want_s = params.get("seconds")
        if want_s and probed.get("duration"):
            # Providers round to their own frame counts; a clip within
            # a second and a half of the request is the request.
            if abs(float(probed["duration"]) - float(want_s)) > 1.5:
                raise ValidationError(
                    "asked for %ss, got %.1fs" % (want_s, probed["duration"]))
    return "/" + path.replace(os.sep, "/"), meta


def _download(url: str, tries: int = 3) -> bytes:
    """The bytes at `url`, with retries on transport errors and 5xx -
    a provider's storage answering 503 for a moment is not a lost clip."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=120, stream=True)
            if r.status_code >= 500 or r.status_code == 429:
                last = "storage answered %d" % r.status_code
            elif r.status_code != 200:
                raise ValidationError("storage answered %d" % r.status_code)
            else:
                return b"".join(r.iter_content(1 << 16))
        except requests.exceptions.RequestException as e:
            last = str(e)[:120]
        if attempt + 1 < tries:
            _sleep(1.5 * (2 ** attempt))
    raise ValidationError("could not download it (%s)" % last)


def probe(path: str) -> dict | None:
    """Width, height, duration via ffprobe when it is installed.
    None when it is not - validation then stops at the container check,
    which is the honest amount of checking a box without ffprobe can do."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        return {
            "width": int(stream.get("width") or 0) or None,
            "height": int(stream.get("height") or 0) or None,
            "duration": round(float(fmt.get("duration") or 0), 2) or None,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ratio_matches(ratio: str, width: int, height: int) -> bool:
    """16:9 against 1280x720 is a match; against 1280x704 (a model that
    rounds to multiples of 32) is also a match. 3% either way."""
    try:
        a, b = (int(x) for x in ratio.split(":"))
    except ValueError:
        return True
    want = a / b
    got = width / height
    return abs(got - want) / want <= 0.03


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


# ---------------------------------------------------------- job views
def public_job(job: dict) -> dict:
    """What the browser gets: no owner id, and `url` is this server's
    copy when there is one - the provider's link expires."""
    view = {k: v for k, v in job.items() if k != "owner_id"}
    view["url"] = job.get("local_path") if job.get("status") == "done" else None
    return view


def remove_file(job: dict) -> None:
    """Delete the local copy, if any. Never raises."""
    path = job.get("local_path")
    if not path:
        return
    try:
        os.remove(path.lstrip("/").replace("/", os.sep))
    except OSError:
        pass


# ---------------------------------------------------------- test hook
def _reset_for_tests() -> None:
    global _poller_started, _on_failed, _webhook_url
    _providers.clear()
    _poller_started = False
    _on_failed = None
    _webhook_url = None
