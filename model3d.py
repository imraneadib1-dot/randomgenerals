"""3D models from a picture or a description, with Microsoft's TRELLIS.2.

    import model3d
    model3d.start(on_failed=refund)                    # once, at boot
    job = model3d.submit(owner, source="image", image="static/uploads/x.png")
    job = model3d.submit(owner, source="text", prompt="a red ceramic teapot")
    model3d.get(job_id) -> the row; model3d.public(row) -> what the browser sees

WHAT TRELLIS.2 IS
An image-to-3D model (MIT licence, 4B parameters): one picture in, a
textured PBR mesh out as a GLB. It needs a 24 GB NVIDIA GPU, which this
server does not have, so it always runs somewhere else - see PROVIDERS.

TEXT TO 3D is two steps: the description is drawn first by imagegen.py
(as a single object on a plain background, which is what TRELLIS.2 reads
best), then that picture becomes the model. The picture is kept with
the job, so the person can see what the model was made from.

PROVIDERS - one shape, chosen by TRELLIS_PROVIDERS (tried in order):
  hf_space   the public Hugging Face Space microsoft/TRELLIS.2 over
             gradio_client. Free; runs on shared ZeroGPU with a daily
             quota (HF_TOKEN raises it). The default.
  fal        fal.ai's hosted TRELLIS.2 (fal-ai/trellis-2). Paid per
             model: $0.25 / $0.30 / $0.35 at 512 / 1024 / 1536. Needs
             FAL_KEY. Off unless listed.
  selfhost   your own GPU machine running TRELLIS.2's app.py, reached at
             TRELLIS_SELFHOST_URL. The same Gradio calls as the Space, so
             it shares that code; untested until such a machine exists.
A provider that is busy or out of quota hands the job to the next one.

JOBS run on a small thread pool (MODEL3D_PARALLEL, default 2) and are
rows in db.model_jobs, so the dashboard and history survive a reload. A
job still running when the server restarts cannot be collected again
(the Space session died with it), so it is failed and refunded at boot.

THE GLB is downloaded here, checked (glTF 2 binary, a mesh, the length
in the header matches the file), and served from static/models3d. The
exporter writes WebP textures (EXT_texture_webp); Blender 4.x and
<model-viewer> both read those. Scale is a unit cube centred on the
origin, Y up in the file - the Blender importer turns that to Z up.
"""
import base64
import json
import os
import random
import re
import shutil
import struct
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

import db

OUTPUT_DIR = os.path.join("static", "models3d")
os.makedirs(OUTPUT_DIR, exist_ok=True)

RESOLUTIONS = ("512", "1024", "1536")
DEFAULT_RESOLUTION = "512"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 64, 4096
MAX_GLB_BYTES = 200 * 1024 * 1024
JOB_TIMEOUT = int(os.environ.get("MODEL3D_TIMEOUT", "600") or 600)
PARALLEL = int(os.environ.get("MODEL3D_PARALLEL", "2") or 2)

# The export. TRELLIS.2's Space defaults to 300k faces; 200k with 2K
# textures keeps a model to roughly 5-20 MB - quick to show on a phone,
# and a clean import in Blender rather than a multi-million-face mesh.
DECIMATION = int(os.environ.get("MODEL3D_DECIMATION", "200000") or 200000)
TEXTURE_SIZE = int(os.environ.get("MODEL3D_TEXTURE", "2048") or 2048)

SPACE = os.environ.get("TRELLIS_SPACE", "microsoft/TRELLIS.2")
FAL_ENDPOINT = "fal-ai/trellis-2"
FAL_PRICES = {"512": 0.25, "1024": 0.30, "1536": 0.35}
# fal.ai is paid per model. When it is in the chain (usually after the
# free Space, as overflow), it stops for the day at this much spend.
FAL_DAILY_USD = float(os.environ.get("FAL_DAILY_USD", "2") or 2)

# What people see. The engine underneath is TRELLIS.2; the product
# calls it relo 1.0, the way the Pro chat model is "pip 2".
DISPLAY_NAME = "relo 1.0"
GENERATOR = "relo 1.0 - RandomGenerals"

# When a free GPU says its daily quota is spent, the provider RESTS:
# it is skipped (and the studio says so) until this long has passed,
# instead of every request waiting on a refusal.
REST_SECONDS = int(os.environ.get("MODEL3D_REST_SECONDS", "1800") or 1800)

# The sampler settings, as the Space and fal both default them.
SAMPLER = {
    "ss_guidance_strength": 7.5, "ss_guidance_rescale": 0.7,
    "ss_sampling_steps": 12, "ss_rescale_t": 5.0,
    "shape_slat_guidance_strength": 7.5, "shape_slat_guidance_rescale": 0.5,
    "shape_slat_sampling_steps": 12, "shape_slat_rescale_t": 3.0,
    "tex_slat_guidance_strength": 1.0, "tex_slat_guidance_rescale": 0.0,
    "tex_slat_sampling_steps": 12, "tex_slat_rescale_t": 3.0,
}


class Model3DError(Exception):
    """A sentence for a person."""


class Busy(Model3DError):
    """The provider is out of quota or full: try the next one."""


def _now():
    return time.time()


# ------------------------------------------------------------ input checks
def check_image(data):
    """-> (ext, width, height) for a PNG, JPEG or WebP the model can use.
    Raises Model3DError with the reason. Judged by the bytes, not the
    file name: a renamed PDF is not a picture."""
    if not data:
        raise Model3DError("That file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise Model3DError("Keep the image under %d MB." % (MAX_IMAGE_BYTES // (1024 * 1024)))
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise Model3DError("Use a PNG, JPEG or WebP image.")
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            im.verify()
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
    except Exception as e:                             # noqa: BLE001
        raise Model3DError("That image could not be read - it may be damaged.") from e
    if min(w, h) < MIN_SIDE:
        raise Model3DError("That image is too small (at least %d px a side)." % MIN_SIDE)
    if max(w, h) > MAX_SIDE:
        raise Model3DError("That image is too large (at most %d px a side)." % MAX_SIDE)
    return ext, w, h


def save_input(data, ext):
    """Keep an uploaded picture with the job. -> path under static/."""
    name = "%s.%s" % (uuid.uuid4().hex[:16], ext)
    path = os.path.join(OUTPUT_DIR, "inputs")
    os.makedirs(path, exist_ok=True)
    full = os.path.join(path, name)
    with open(full, "wb") as fh:
        fh.write(data)
    return full.replace(os.sep, "/")


def check_glb(path):
    """-> facts about a GLB, or raises Model3DError. Checks what a
    viewer or Blender would trip over: the container, the version, the
    declared length, and that there is a mesh in it."""
    size = os.path.getsize(path)
    if size < 1024:
        raise Model3DError("The model came back empty.")
    if size > MAX_GLB_BYTES:
        raise Model3DError("The model came back too large to keep.")
    with open(path, "rb") as fh:
        head = fh.read(20)
        if head[:4] != b"glTF":
            raise Model3DError("The model is not a GLB file.")
        version, length = struct.unpack("<II", head[4:12])
        if version != 2:
            raise Model3DError("The model is glTF %d, not 2." % version)
        if length != size:
            raise Model3DError("The model file is cut short.")
        chunk_len, chunk_type = struct.unpack("<II", head[12:20])
        if chunk_type != 0x4E4F534A:                   # "JSON"
            raise Model3DError("The model file is malformed.")
        doc = json.loads(fh.read(chunk_len).decode("utf-8"))
    meshes = doc.get("meshes") or []
    if not meshes:
        raise Model3DError("The model has no mesh in it.")
    return {
        "bytes": size,
        "meshes": len(meshes),
        "materials": len(doc.get("materials") or []),
        "textures": len(doc.get("images") or []),
        "extensions": sorted(set(doc.get("extensionsUsed") or [])),
        "required": sorted(set(doc.get("extensionsRequired") or [])),
    }


def _brand_glb(path):
    """Stamp the file as relo 1.0's: the generator field (trimesh, from
    the exporter) and the mesh and material names Blender shows. Only the
    JSON chunk changes; the binary chunk is copied as it is."""
    with open(path, "rb") as fh:
        data = fh.read()
    json_len = struct.unpack("<I", data[12:16])[0]
    doc = json.loads(data[20:20 + json_len].decode("utf-8"))
    rest_bytes = data[20 + json_len:]
    doc.setdefault("asset", {})["generator"] = GENERATOR
    for i, mesh in enumerate(doc.get("meshes") or []):
        mesh["name"] = "relo_model" if i == 0 else "relo_model_%d" % i
    for i, mat in enumerate(doc.get("materials") or []):
        mat["name"] = "relo_material" if i == 0 else "relo_material_%d" % i
    for node in doc.get("nodes") or []:
        if "mesh" in node:
            node["name"] = "relo_model"
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + len(rest_bytes)
    with open(path, "wb") as fh:
        fh.write(b"glTF" + struct.pack("<II", 2, total)
                 + struct.pack("<II", len(js), 0x4E4F534A) + js + rest_bytes)


# ------------------------------------------------------------ providers
class GradioTrellis:
    """TRELLIS.2's own web demo, driven over its Gradio API. The same
    four calls work against the Hugging Face Space and against app.py on
    your own GPU: start a session, cut the object out, generate (kept in
    the session), export the GLB."""

    def __init__(self, name, src, token=""):
        self.name = name
        self.src = src
        self.token = token

    def configured(self):
        if not self.src:
            return False
        try:
            import gradio_client  # noqa: F401
        except ImportError:
            return False
        return True

    def run(self, image_path, resolution, seed, deadline, stage):
        from gradio_client import Client, handle_file
        work = tempfile.mkdtemp(prefix="trellis-")
        try:
            def left():
                remaining = deadline - _now()
                if remaining <= 5:
                    raise Model3DError("It took too long - %s may be busy. Try again." % DISPLAY_NAME)
                return remaining

            def call(*args, api_name):
                job = client.submit(*args, api_name=api_name)
                try:
                    return job.result(timeout=left())
                except TimeoutError as e:
                    job.cancel()
                    raise Model3DError("It took too long - %s may be busy. "
                                       "Try again." % DISPLAY_NAME) from e

            stage("Starting %s" % DISPLAY_NAME)
            try:
                client = Client(self.src, token=self.token or None, verbose=False,
                                download_files=work)
            except Exception as e:                     # noqa: BLE001
                raise Busy("%s is not answering right now." % DISPLAY_NAME) from e
            try:
                call(api_name="/start_session")
                stage("Cutting the object out of the picture")
                cut = call(handle_file(image_path), api_name="/preprocess_image")
                cut_path = cut.get("path") if isinstance(cut, dict) else cut
                stage("Shaping the model (%s)" % resolution)
                params = dict(SAMPLER)
                call(handle_file(cut_path), seed, resolution,
                     params["ss_guidance_strength"], params["ss_guidance_rescale"],
                     params["ss_sampling_steps"], params["ss_rescale_t"],
                     params["shape_slat_guidance_strength"], params["shape_slat_guidance_rescale"],
                     params["shape_slat_sampling_steps"], params["shape_slat_rescale_t"],
                     params["tex_slat_guidance_strength"], params["tex_slat_guidance_rescale"],
                     params["tex_slat_sampling_steps"], params["tex_slat_rescale_t"],
                     api_name="/image_to_3d")
                stage("Painting and exporting the model")
                out = call(DECIMATION, TEXTURE_SIZE, api_name="/extract_glb")
            except Model3DError:
                raise
            except Exception as e:                     # noqa: BLE001
                raise _gradio_error(e) from e
            glb = out[0] if isinstance(out, (list, tuple)) else out
            glb = glb.get("path") if isinstance(glb, dict) else glb
            if not glb or not os.path.exists(glb):
                raise Model3DError("%s finished without a model." % DISPLAY_NAME)
            keep = tempfile.mktemp(suffix=".glb", prefix="trellis-out-")
            shutil.move(glb, keep)
            return keep
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _gradio_error(e):
    text = str(e)
    low = text.lower()
    if "quota" in low or "zerogpu" in low or "gpu duration" in low or "runs limit" in low:
        when = re.search(r"try again in (\d+):(\d+):(\d+)", text, re.I)
        seconds = (int(when.group(1)) * 3600 + int(when.group(2)) * 60 + int(when.group(3))
                   if when else REST_SECONDS)
        return Exhausted("%s has used today's free GPU time." % DISPLAY_NAME, seconds)
    if "queue" in low and ("full" in low or "too many" in low):
        return Busy("%s is full right now. Try again in a few minutes." % DISPLAY_NAME)
    if "429" in low or "rate" in low and "limit" in low:
        return Busy("%s is busy right now. Try again in a few minutes." % DISPLAY_NAME)
    if "sleeping" in low or "building" in low or "paused" in low or "503" in low:
        return Busy("%s is starting up. Try again shortly." % DISPLAY_NAME)
    return Model3DError("%s could not make this model. Try another picture, or try again."
                        % DISPLAY_NAME)


class Exhausted(Busy):
    """The provider's daily allowance is spent: rest it for `seconds`."""

    def __init__(self, message, seconds=REST_SECONDS):
        super().__init__(message)
        self.seconds = max(60, int(seconds or REST_SECONDS))


# fal.ai spend today, so the paid overflow cannot run away.
_fal_spend = {"day": "", "usd": 0.0}
_fal_lock = threading.Lock()


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _seconds_to_midnight():
    t = time.gmtime()
    return 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)


class FalTrellis:
    """fal.ai's hosted TRELLIS.2 over its queue API. Paid per model, so
    capped per day by FAL_DAILY_USD."""

    name = "fal"
    BASE = "https://queue.fal.run/" + FAL_ENDPOINT

    def configured(self):
        return bool(self.key())

    @staticmethod
    def key():
        return os.environ.get("FAL_KEY", "").strip()

    def _headers(self):
        return {"Authorization": "Key " + self.key(), "Content-Type": "application/json"}

    def run(self, image_path, resolution, seed, deadline, stage):
        cost = FAL_PRICES.get(resolution, 0.35)
        with _fal_lock:
            if _fal_spend["day"] != _today():
                _fal_spend.update(day=_today(), usd=0.0)
            if _fal_spend["usd"] + cost > FAL_DAILY_USD:
                raise Exhausted("%s has used today's GPU budget." % DISPLAY_NAME,
                                _seconds_to_midnight())
            _fal_spend["usd"] += cost             # reserved now, kept if it runs
        try:
            return self._run(image_path, resolution, seed, deadline, stage)
        except Busy:
            with _fal_lock:                        # refused before running: not charged
                _fal_spend["usd"] = max(0.0, _fal_spend["usd"] - cost)
            raise

    def _run(self, image_path, resolution, seed, deadline, stage):
        with open(image_path, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        body = dict(SAMPLER, image_url="data:%s;base64,%s" % (mime, base64.b64encode(data).decode()),
                    resolution=resolution, seed=seed, texture_size=str(TEXTURE_SIZE),
                    decimation_target=DECIMATION, remesh=True)
        stage("Sending the picture to %s" % DISPLAY_NAME)
        r = requests.post(self.BASE, headers=self._headers(), json=body, timeout=60)
        self._raise_for(r)
        sub = r.json()
        status_url = sub.get("status_url")
        response_url = sub.get("response_url")
        if not status_url or not response_url:
            raise Model3DError("%s did not accept the job." % DISPLAY_NAME)
        stage("Shaping the model (%s)" % resolution)
        wait = 2.0
        while True:
            if _now() > deadline:
                raise Model3DError("It took too long - %s may be busy. Try again." % DISPLAY_NAME)
            time.sleep(wait)
            wait = min(wait * 1.4, 8)
            s = requests.get(status_url, headers=self._headers(), timeout=30)
            self._raise_for(s)
            state = (s.json() or {}).get("status")
            if state == "COMPLETED":
                break
            if state not in ("IN_QUEUE", "IN_PROGRESS"):
                raise Model3DError("%s stopped the job." % DISPLAY_NAME)
        res = requests.get(response_url, headers=self._headers(), timeout=60)
        self._raise_for(res)
        url = ((res.json() or {}).get("model_glb") or {}).get("url")
        if not url:
            raise Model3DError("%s finished without a model." % DISPLAY_NAME)
        stage("Saving the model")
        return _download(url)

    @staticmethod
    def _raise_for(r):
        if r.status_code < 400:
            return
        if r.status_code in (401, 403, 402):
            # A refused key or an empty account: rest it until tomorrow
            # rather than ask again for every model.
            raise Exhausted("%s's paid GPU is unavailable." % DISPLAY_NAME, _seconds_to_midnight())
        if r.status_code == 429:
            raise Busy("%s is busy right now." % DISPLAY_NAME)
        raise Model3DError("%s answered with an error (%d)." % (DISPLAY_NAME, r.status_code))


def _download(url):
    out = tempfile.mktemp(suffix=".glb", prefix="trellis-out-")
    with requests.get(url, stream=True, timeout=120) as r:
        if r.status_code >= 400:
            raise Model3DError("The model could not be downloaded (%d)." % r.status_code)
        size = 0
        with open(out, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > MAX_GLB_BYTES:
                    raise Model3DError("The model came back too large to keep.")
                fh.write(chunk)
    return out


PROVIDERS = {
    "hf_space": GradioTrellis("hf_space", SPACE, os.environ.get("HF_TOKEN", "").strip()),
    "fal": FalTrellis(),
    "selfhost": GradioTrellis("selfhost", os.environ.get("TRELLIS_SELFHOST_URL", "").strip()),
}


_resting = {}            # provider name -> time it may be asked again


def configured_chain():
    """The providers in TRELLIS_PROVIDERS that are set up here, in order."""
    names = [n.strip() for n in os.environ.get("TRELLIS_PROVIDERS", "hf_space").split(",")
             if n.strip()]
    return [PROVIDERS[n] for n in names if n in PROVIDERS and PROVIDERS[n].configured()]


def chain():
    """The providers to try now: set up, and not resting."""
    now = _now()
    return [p for p in configured_chain() if _resting.get(p.name, 0) <= now]


def rest(name, seconds):
    _resting[name] = _now() + seconds


def available():
    """Is 3D generation set up here at all (resting or not)?"""
    return bool(configured_chain())


def resting_for():
    """Seconds until a provider can take a job, when every one is
    resting; 0 when one can take it now."""
    if chain() or not configured_chain():
        return 0
    return max(1, int(min(_resting.get(p.name, 0) for p in configured_chain()) - _now()))


def rest_message(seconds=None):
    seconds = resting_for() if seconds is None else seconds
    if seconds >= 3600:
        when = "in about %d hour%s" % (round(seconds / 3600), "" if round(seconds / 3600) == 1 else "s")
    else:
        when = "in about %d minutes" % max(1, round(seconds / 60))
    return ("%s has used today's free GPU time and is resting. It's back %s - "
            "nothing was counted against your models." % (DISPLAY_NAME, when))


def price(resolution):
    """What one model costs on the first provider in the chain, in USD."""
    c = chain()
    if c and c[0].name == "fal":
        return FAL_PRICES.get(resolution, 0.30)
    return 0.0


# ------------------------------------------------------------ text -> picture
_COMMAND = re.compile(
    r"^\s*(please\s+)?((can|could|would)\s+you\s+)?(please\s+)?"
    r"(make|create|generate|build|give|turn|convert|model|sculpt|design|produce|i\s+want|i\s+need|i'd\s+like)"
    r"(\s+me)?(\s+(a|an|the))?(\s+(3d|3-d|three[- ]?d|glb|gltf))?"
    r"(\s+(model|mesh|object|asset|version|figure|file))?(\s+(of|for|from))?\s*[:,-]?\s*",
    re.I)
_TRAIL = re.compile(r"\s*(,?\s*(as|in|into)\s+(a\s+)?(3d|3-d|glb)(\s+(model|mesh|file))?|"
                    r"\s+(3d|glb)\s+(model|mesh|file))\s*[.!?]*\s*$", re.I)


def subject_of(text):
    """'make me a 3d model of a red teapot please' -> 'a red teapot'."""
    s = (text or "").strip()
    s = _COMMAND.sub("", s, count=1)
    s = _TRAIL.sub("", s)
    s = re.sub(r"\s*,?\s*(in|at)\s+(high|higher|highest|max|maximum|best|good)\s+(quality|detail)\b",
               "", s, flags=re.I)
    s = re.sub(r"\s*,?\s*(in|at)\s+(512|1024|1536)(p|px)?\b", "", s, flags=re.I)
    s = re.sub(r"\s*\bplease\b[.!?]*\s*$", "", s, flags=re.I).strip(" .!?,")
    return s or (text or "").strip()


def picture_prompt(subject):
    return ("%s, a single object, centered, full view, isolated on a plain white "
            "background, soft even studio lighting, sharp detail, no text" % subject)


def _draw(prompt, stage):
    import imagegen
    stage("Drawing a picture of it first")
    try:
        url, error = imagegen.generate_image(picture_prompt(subject_of(prompt)),
                                             backend=imagegen.best_backend(), enhance=False)
    except Exception as e:                             # noqa: BLE001
        raise Model3DError("The picture to model from could not be made.") from e
    if error or not url:
        raise Model3DError("The picture to model from could not be made: %s" % (error or "no image"))
    path = url.lstrip("/")
    if not url.startswith("/static/generated/") or not os.path.exists(path):
        raise Model3DError("The picture to model from could not be made.")
    return path


# ------------------------------------------------------------ the jobs
_pool = None
_pool_lock = threading.Lock()
_on_failed = None


def start(on_failed=None):
    """At boot: fail what a restart orphaned, and get ready to run."""
    global _on_failed
    _on_failed = on_failed
    for job in db.model_jobs_unfinished():
        _fail(job, "The server restarted while this was being made. "
                   "It has not been counted - try again.")


def _executor():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="model3d")
        return _pool


def submit(owner_id, source, prompt="", image="", resolution=DEFAULT_RESOLUTION,
           quota_key=""):
    if resolution not in RESOLUTIONS:
        resolution = DEFAULT_RESOLUTION
    now = _now()
    job = {"id": uuid.uuid4().hex[:12], "owner_id": owner_id, "status": "queued",
           "source": source, "prompt": (prompt or "")[:600], "image": image or "",
           "resolution": resolution, "stage": "Waiting for a free slot",
           "quota_key": quota_key, "created": now, "updated": now}
    db.model_job_create(job)
    _executor().submit(_work, job["id"])
    return db.model_job_get(job["id"])


def _fail(job, error):
    db.model_job_update(job["id"], status="failed", error=error[:300], stage="",
                        updated=_now())
    if _on_failed:
        try:
            _on_failed(db.model_job_get(job["id"]) or job)
        except Exception:                              # noqa: BLE001
            pass


def _work(job_id):
    job = db.model_job_get(job_id)
    if not job or job["status"] != "queued":
        return
    deadline = job["created"] + JOB_TIMEOUT

    def stage(text):
        db.model_job_update(job_id, stage=text, updated=_now())

    db.model_job_update(job_id, status="running", updated=_now())
    try:
        image = job["image"]
        if job["source"] == "text":
            image = _draw(job["prompt"], stage)
            db.model_job_update(job_id, image=image)
        providers = chain()
        if not providers:
            if available():
                raise Model3DError(rest_message())
            raise Model3DError("3D generation is not set up on this server.")
        seed = random.randrange(0, 2**31 - 1)
        last = None
        out = None
        for p in providers:
            db.model_job_update(job_id, provider=p.name)
            try:
                out = p.run(image, job["resolution"], seed, deadline, stage)
                break
            except Exhausted as e:
                rest(p.name, e.seconds)                # skip it until its quota is back
                last = e
                continue
            except Busy as e:
                last = e                               # the next provider may have room
                continue
        if out is None:
            if isinstance(last, Exhausted) and not chain():
                raise Model3DError(rest_message())
            raise last or Model3DError("%s could not take this right now." % DISPLAY_NAME)
        stage("Checking the model")
        try:
            info = check_glb(out)
            _brand_glb(out)
            name = job_id + ".glb"
            shutil.move(out, os.path.join(OUTPUT_DIR, name))
        finally:
            if os.path.exists(out):
                os.remove(out)
        db.model_job_update(job_id, status="done", stage="", glb=name,
                            glb_bytes=info["bytes"], updated=_now())
    except Model3DError as e:
        _fail(job, str(e))
    except Exception as e:                             # noqa: BLE001 - never leave a job running
        _fail(job, "Something went wrong making the model (%s)." % e.__class__.__name__)


def get(job_id):
    return db.model_job_get(job_id)


def jobs_for(owner_id, limit=20):
    return db.model_jobs_for(owner_id, limit)


def public(job):
    """What the browser is told about a job."""
    if not job:
        return None
    done = job["status"] == "done" and job.get("glb")
    image = job.get("image") or ""
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job.get("stage") or "",
        "source": job["source"],
        "prompt": job.get("prompt") or "",
        "resolution": job.get("resolution"),
        "engine": DISPLAY_NAME,
        "error": job.get("error") or "",
        "created": job["created"],
        "elapsed": round((job["updated"] if job["status"] in ("done", "failed") else _now())
                         - job["created"]),
        "image_url": ("/" + image) if image.startswith("static/") else "",
        "glb_url": ("/api/3d/job/%s/model.glb" % job["id"]) if done else "",
        "download_url": ("/api/3d/job/%s/model.glb?download=1" % job["id"]) if done else "",
        "blend_url": ("/api/3d/job/%s/model.blend" % job["id"])
                     if done and blend_available() else "",
        "glb_bytes": job.get("glb_bytes") or 0,
    }


def glb_path(job):
    if not job or job["status"] != "done" or not job.get("glb"):
        return None
    path = os.path.join(OUTPUT_DIR, os.path.basename(job["glb"]))
    return path if os.path.exists(path) else None


# ------------------------------------------------------------ for Blender
# "Download for Blender": the same model as a .blend, made by a Blender on
# this server (scripts/glb_to_blend.py) the first time someone asks, and
# kept next to the GLB. It opens straight into a ready scene - the model
# named relo_model, textures packed inside the file, the alpha connected
# when the texture has any, a camera and lights - instead of an import.
# Only offered where Blender is installed (BLENDER_BIN, or `blender` on
# the PATH); one conversion at a time, since this server has two cores.
BLENDER = os.environ.get("BLENDER_BIN", "").strip() or shutil.which("blender") or ""
BLEND_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "glb_to_blend.py")
BLEND_TIMEOUT = 240
_blend_gate = threading.Semaphore(1)


def blend_available():
    return bool(BLENDER) and os.path.exists(BLENDER) and os.path.exists(BLEND_SCRIPT)


def _looks_like_blend(path):
    with open(path, "rb") as fh:
        head = fh.read(7)
    # Plain, gzip (old compression) or zstd (Blender 3.0+ compression).
    return head.startswith(b"BLENDER") or head[:2] == b"\x1f\x8b" or head[:4] == b"\x28\xb5\x2f\xfd"


def make_blend(job):
    """-> path of the job's .blend, converting on first request. Raises
    Model3DError with a sentence when it cannot."""
    glb = glb_path(job)
    if not glb:
        raise Model3DError("That model isn't ready yet.")
    if not blend_available():
        raise Model3DError("Blender downloads aren't available on this server yet.")
    out = glb[:-4] + ".blend"
    if os.path.exists(out):
        return out
    if not _blend_gate.acquire(timeout=BLEND_TIMEOUT):
        raise Model3DError("Blender is busy with another model. Try again in a minute.")
    try:
        if os.path.exists(out):                    # made while this one waited
            return out
        tmp = out + ".part.blend"
        import subprocess
        try:
            done = subprocess.run(
                [BLENDER, "-b", "--factory-startup", "--python", BLEND_SCRIPT, "--", glb, tmp],
                capture_output=True, text=True, timeout=BLEND_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise Model3DError("Making the Blender file took too long.") from e
        if done.returncode != 0 or not os.path.exists(tmp) or not _looks_like_blend(tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
            raise Model3DError("The Blender file could not be made.")
        os.replace(tmp, out)
        return out
    finally:
        _blend_gate.release()
