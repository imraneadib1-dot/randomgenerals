"""Video trimming and export, via ffmpeg.

SCOPE
Trim and export, and nothing else yet. That is deliberate: it exercises
every part the rest of a video bay would need - upload, probe, a queued
job, encode, download - while being useful on its own. Style presets and
reference-matching sit on top of this and can be judged separately.

WHY THE LIMITS ARE LOW
Encoding saturates a CPU core for as long as it runs. This app is served
from one laptop through a tunnel, so a single unbounded job makes the
whole site unresponsive - including for people only reading the landing
page. Hence a short maximum duration, a small maximum file, one job at a
time per account, and a hard timeout on the process itself.

ON UNTRUSTED INPUT
ffmpeg parses attacker-controlled files here. It is the right tool and
the standard one, but that is still a large parser reached from a public
URL, so: the process gets a timeout, the output path is generated rather
than taken from the request, and nothing from the user reaches a shell -
arguments are passed as a list, never a string.
"""
import io
import os
import json
import re
import shutil
import subprocess
import threading
import time
import uuid

# Where finished renders live. Served as static files, and pruned by age.
OUTPUT_DIR = os.path.join("static", "video", "renders")
os.makedirs(OUTPUT_DIR, exist_ok=True)

UPLOAD_DIR = os.path.join("static", "uploads", "video")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = 120 * 1024 * 1024      # 120MB
MAX_INPUT_SECONDS = 600                   # 10 minutes
MAX_OUTPUT_SECONDS = 180                  # 3 minutes of output
ENCODE_TIMEOUT = 240                      # kill a job that will not finish
RENDER_TTL_SECONDS = 6 * 3600             # prune finished renders after 6h

ALLOWED_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}

_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _winget_bin(name):
    """Find a binary installed by winget, which does not put it on PATH for
    an already-running process. Without this the feature works from a fresh
    shell and mysteriously does not from the running server."""
    root = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if not os.path.isdir(root):
        return None
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def _find(tool):
    exe = tool + ".exe" if _IS_WINDOWS else tool
    found = shutil.which(tool) or shutil.which(exe)
    if found:
        return found
    return _winget_bin(exe) if _IS_WINDOWS else None


_paths = {"ffmpeg": None, "ffprobe": None, "checked": False}


def _tools():
    if not _paths["checked"]:
        _paths["ffmpeg"] = _find("ffmpeg")
        _paths["ffprobe"] = _find("ffprobe")
        _paths["checked"] = True
    return _paths["ffmpeg"], _paths["ffprobe"]


def available():
    ff, fp = _tools()
    return bool(ff and fp)


def unavailable_reason():
    if available():
        return ""
    return ("Video editing needs ffmpeg, which isn't installed on this "
            "server. Install it and restart the app.")


# ---------------------------------------------------------------- quality
# The original encode was a single setting, veryfast at CRF 23, chosen
# when the only operation was a trim and the only thing that mattered was
# turnaround. That is the wrong default once people are grading and
# reframing: veryfast at 23 puts visible blocking into exactly the smooth
# gradients a colour grade creates, so someone who asked for a cinematic
# look gets back something that looks worse than what they put in.
#
# `time` is a multiplier on the render timeout. A flat cap set for
# veryfast turns "render this at high quality" into "render this until it
# is killed", and a timeout reads as a failure rather than as a limit.
# `cap` is a bitrate ceiling in kbit/s - see the note on _codec_args.
QUALITY = {
    "draft":    {"crf": 26, "preset": "veryfast", "cq": 30,
                 "audio": "128k", "time": 1.0, "cap": 4000},
    "standard": {"crf": 21, "preset": "medium", "cq": 25,
                 "audio": "160k", "time": 1.4, "cap": 8000},
    "high":     {"crf": 18, "preset": "slow", "cq": 21,
                 "audio": "192k", "time": 2.2, "cap": 14000},
    "max":      {"crf": 15, "preset": "slower", "cq": 18,
                 "audio": "256k", "time": 3.2, "cap": 24000},
}

_encoder_cache = {"checked": False, "name": "libx264"}


def encoder():
    """Which H.264 encoder to use. -> "h264_nvenc" or "libx264".

    NVENC where the machine really has it: roughly an order of magnitude
    faster than libx264 at comparable quality, which is what makes a
    higher-quality default affordable at all on a laptop that is also
    serving the site.

    Being in the build is not evidence it works. The ffmpeg binaries
    people install are compiled with NVENC whether or not there is an
    NVIDIA card in the machine, and the mismatch only shows up when a
    real encode dies part-way - by which point it looks like the render
    failed rather than like the encoder was never there. So this encodes
    one black frame and believes the result rather than the feature list.
    """
    if _encoder_cache["checked"]:
        return _encoder_cache["name"]
    _encoder_cache["checked"] = True
    ff, _fp = _tools()
    if ff:
        try:
            r = _run([ff, "-hide_banner", "-loglevel", "error",
                      "-f", "lavfi", "-i", "color=black:s=128x128:d=0.1",
                      "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=25)
            if r.returncode == 0:
                _encoder_cache["name"] = "h264_nvenc"
        except (subprocess.TimeoutExpired, OSError):
            pass
    return _encoder_cache["name"]


def default_quality():
    """"high" where the encode is hardware-accelerated, "standard" where
    it is not.

    A fixed default either wastes a GPU or times out a laptop. This one
    tracks the encoder, so a request that says nothing about quality
    gets the best this machine can actually finish inside the timeout.
    """
    return "high" if encoder() == "h264_nvenc" else "standard"


def _codec_args(level, has_audio=True):
    """The encoding half of an ffmpeg call, for one quality level."""
    q = QUALITY.get(level) or QUALITY[default_quality()]
    if encoder() == "h264_nvenc":
        # p6 is NVENC's slow-ish preset. vbr with -cq and no bitrate
        # target is its constant-quality mode - the closest thing it has
        # to CRF, and the only mode worth using for a one-off render.
        args = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                "-rc", "vbr", "-cq", str(q["cq"]), "-b:v", "0"]
    else:
        args = ["-c:v", "libx264", "-preset", q["preset"],
                "-crf", str(q["crf"])]
    # A ceiling on top of the quality target. CRF alone will spend
    # whatever it takes, and on genuinely incompressible content - film
    # grain, heavy noise, confetti - what it takes is tens of megabytes
    # a second. bufsize at twice the cap leaves enough slack that the
    # limit smooths the peaks rather than flattening the whole render.
    args += ["-maxrate", "%dk" % q["cap"], "-bufsize", "%dk" % (q["cap"] * 2)]
    args += [
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",   # without this, some players show nothing
        "-movflags", "+faststart",
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", q["audio"]]
    else:
        args += ["-an"]
    return args


def _timeout_for(level):
    q = QUALITY.get(level) or QUALITY[default_quality()]
    return int(ENCODE_TIMEOUT * q["time"])


def _run(args, timeout):
    """Run a tool with no shell, no window, and a hard timeout."""
    kwargs = {
        "capture_output": True,
        "timeout": timeout,
        "text": True,
        "errors": "replace",
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def probe(path):
    """-> (info, error). info has duration, width, height, fps, has_audio."""
    _ff, fp = _tools()
    if not fp:
        return None, unavailable_reason()
    try:
        r = _run([fp, "-v", "error", "-print_format", "json",
                  "-show_format", "-show_streams", path], timeout=30)
    except subprocess.TimeoutExpired:
        return None, "Reading that file took too long - it may be corrupt."
    if r.returncode != 0:
        return None, "That file isn't a video this server can read."
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None, "Could not read that file's details."

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return None, "That file has no video track."

    # Frame rate arrives as a rational string like "30000/1001".
    fps = 0.0
    raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        num, _, den = raw.partition("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "duration": round(duration, 2),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 2),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "size_bytes": int(os.path.getsize(path)) if os.path.exists(path) else 0,
    }, None


# ---------------------------------------------------------------- jobs
# In-memory, because a render is worthless after a restart anyway - the
# output file is gone with it. Keyed by job id; each entry carries enough
# for the UI to show progress without the client holding any state.
_jobs = {}
_jobs_lock = threading.Lock()


def job(job_id):
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def active_job_for(owner):
    """One render at a time per account. Without this, a handful of
    simultaneous encodes make every other request on the server crawl."""
    with _jobs_lock:
        for j in _jobs.values():
            if j["owner"] == owner and j["status"] in ("queued", "running"):
                return dict(j)
    return None


def _set(job_id, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def start_trim(owner, src_path, start, end, scale=None):
    """Queue a trim. -> (job_id, error).

    `scale` is an optional output height (720, 1080); None keeps the
    source size.
    """
    if not available():
        return None, unavailable_reason()

    info, err = probe(src_path)
    if err or not info:
        return None, err or "Could not read that video."
    if info["duration"] > MAX_INPUT_SECONDS:
        return None, (f"That video is {int(info['duration'] // 60)} minutes "
                      f"long. The limit is "
                      f"{MAX_INPUT_SECONDS // 60} minutes.")

    try:
        start = max(0.0, float(start))
        end = float(end)
    except (TypeError, ValueError):
        return None, "Start and end must be numbers."
    if end <= start:
        return None, "The end point has to come after the start."
    end = min(end, info["duration"])
    if end - start > MAX_OUTPUT_SECONDS:
        return None, (f"That selection is {int(end - start)} seconds. "
                      f"The limit is {MAX_OUTPUT_SECONDS} seconds.")

    busy = active_job_for(owner)
    if busy:
        return None, "You already have a render going. Wait for it to finish."

    job_id = uuid.uuid4().hex[:12]
    out_name = f"{job_id}.mp4"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "owner": owner,
            "status": "queued",
            "stage": "waiting",
            "error": "",
            "url": None,
            "started": time.time(),
            "duration": round(end - start, 2),
        }

    t = threading.Thread(
        target=_encode,
        args=(job_id, src_path, out_path, out_name, start, end, scale),
        daemon=True,
    )
    t.start()
    return job_id, None


def _encode(job_id, src, dst, out_name, start, end, scale):
    ff, _fp = _tools()
    _set(job_id, status="running", stage="trimming")

    args = [
        ff, "-y",
        # -ss before -i seeks by keyframe and is far faster; -accurate_seek
        # keeps the cut where the user asked rather than at the nearest
        # keyframe, which can be seconds away.
        "-accurate_seek", "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", src,
    ]
    if scale:
        # -2 keeps the width even, which H.264 requires; an odd dimension
        # fails the encode outright.
        args += ["-vf", f"scale=-2:{int(scale)}"]
    level = default_quality()
    args += _codec_args(level) + [dst]

    try:
        r = _run(args, timeout=_timeout_for(level))
    except subprocess.TimeoutExpired:
        _set(job_id, status="failed", stage="",
             error=f"That render took longer than {_timeout_for(level)}s and "
                   f"was stopped. Try a shorter selection.")
        _cleanup(dst)
        return
    except OSError as e:
        _set(job_id, status="failed", stage="", error=f"Could not run ffmpeg: {e}")
        return

    if r.returncode != 0 or not os.path.exists(dst):
        # ffmpeg's last stderr line is usually the actual reason.
        tail = (r.stderr or "").strip().splitlines()
        detail = tail[-1][:200] if tail else "unknown error"
        _set(job_id, status="failed", stage="", error=f"Render failed: {detail}")
        _cleanup(dst)
        return

    _set(job_id, status="done", stage="",
         url=f"/static/video/renders/{out_name}",
         size_bytes=os.path.getsize(dst))


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def prune(now=None):
    """Delete renders and their job records once they are stale.

    Without this every render is kept until the disk fills. Called on
    upload rather than on a timer, so it needs no scheduler.
    """
    now = now or time.time()
    removed = 0
    with _jobs_lock:
        stale = [k for k, j in _jobs.items()
                 if now - j["started"] > RENDER_TTL_SECONDS]
        for k in stale:
            _jobs.pop(k, None)
    for name in os.listdir(OUTPUT_DIR) if os.path.isdir(OUTPUT_DIR) else []:
        p = os.path.join(OUTPUT_DIR, name)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > RENDER_TTL_SECONDS:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


# ==================================================================
# Prompt-driven editing
#
# A person types "cut the first 3 seconds, add a caption, make it
# vertical with a blurred background". That has to become an ffmpeg
# command without any of their words reaching ffmpeg.
#
# The pivot is the OPS table below. A prompt becomes a list of
# (op, args) drawn only from these names, every argument is coerced to a
# number or to one of a fixed set of strings, and every number is
# clamped. Only then does anything build a filter string. Free text
# never becomes an argument - not from the person, and not from the
# model that reads their sentence, which chooses op names and numbers
# and nothing else.
#
# The one exception is caption text, which is by definition the person's
# own words. It never reaches the filter string either: it is written to
# a file and ffmpeg is pointed at that file with drawtext's textfile=
# option. Nothing to escape means nothing to escape wrong.
#
# So the worst a bad prompt or a confused model can produce is a valid
# edit nobody wanted, which is visible and costs one render.
# ==================================================================

# Named colour grades. Data rather than code because this is the part
# most likely to be tuned by eye, and a table can be adjusted without
# anyone reading the chain builder.
LOOKS = {
    "cinematic": ("curves=r='0/0.03 0.5/0.48 1/0.97':"
                  "b='0/0.06 0.5/0.52 1/0.94',eq=contrast=1.12:saturation=1.05"),
    "vintage": ("curves=r='0/0.10 0.5/0.55 1/0.95':"
                "g='0/0.06 0.5/0.50 1/0.92':b='0/0.14 0.5/0.48 1/0.84',"
                "eq=saturation=0.82:contrast=0.95"),
    "noir": "hue=s=0,eq=contrast=1.35:brightness=-0.03",
    "warm": "colorbalance=rs=0.12:gs=0.02:bs=-0.10,eq=saturation=1.08",
    "cool": "colorbalance=rs=-0.10:gs=0.00:bs=0.14,eq=saturation=1.02",
    "vivid": "eq=saturation=1.5:contrast=1.15",
    "dream": "eq=brightness=0.05:saturation=1.15,gblur=sigma=1.2",
    # Warm highlights against cool shadows - the grade almost every
    # trailer uses, and what people mean by "make it look like a film"
    # more often than they mean the "cinematic" one above.
    "teal-orange": ("curves=r='0/0.00 0.5/0.55 1/1.00':"
                    "b='0/0.10 0.5/0.48 1/0.90',"
                    "eq=contrast=1.10:saturation=1.12"),
    # Blown highlights, crushed blacks, most of the colour pulled out.
    "bleach": "eq=contrast=1.45:saturation=0.35:brightness=0.04",
    # The opposite move: lifted blacks and low contrast, so it reads as
    # printed rather than backlit.
    "faded": ("curves=all='0/0.14 0.5/0.52 1/0.92',"
              "eq=saturation=0.88:contrast=0.92"),
    "moody": ("curves=all='0/0.02 0.5/0.44 1/0.92',"
              "eq=saturation=0.80:contrast=1.18"),
    "sunset": ("colorbalance=rs=0.20:gs=0.04:bs=-0.16,"
               "eq=saturation=1.22:contrast=1.06"),
    "neon": "eq=saturation=1.75:contrast=1.22,colorbalance=rs=-0.08:bs=0.20",
    # Grain and a slight desaturation, for footage that should look shot
    # rather than rendered.
    "film": ("curves=all='0/0.05 0.5/0.50 1/0.95',eq=saturation=0.94,"
             "noise=alls=8:allf=t+u"),
}

# Caption colours. A fixed table rather than free text for the same
# reason every other argument is one: whatever is in here gets pasted
# into a filter string, so it has to be something this file chose.
TEXT_COLORS = {
    "white": "white",
    "black": "black",
    "yellow": "#ffd83d",
    "red": "#ff4d4d",
    "orange": "#ff9a3d",
    "green": "#5fd38d",
    "blue": "#5fb2ff",
    "pink": "#ff7fc4",
}

# Letterbox colours, same reasoning.
PAD_COLORS = ("black", "white", "gray")

# Every ratio offered anywhere a shape is chosen, named once so aspect,
# blurfill and pad cannot drift apart.
RATIOS = ("1:1", "9:16", "16:9", "4:5", "4:3", "21:9")

# Where a caption sits, as a drawtext y expression.
TEXT_POS = {
    "top": "h*0.08",
    "middle": "(h-text_h)/2",
    "bottom": "h-text_h-h*0.10",
}

# Font size as a divisor of frame height, so a caption is the same
# relative size whether the clip is 480p or 1080p.
TEXT_SIZE = {"small": 26.0, "medium": 19.0, "large": 13.0}

# name -> {param: (kind, ...)}
OPS = {
    # ---- cutting
    "trim": {"start": ("num", 0.0, None), "end": ("num", 0.0, None)},
    "cutout": {"start": ("num", 0.0, None), "end": ("num", 0.0, None)},
    "speed": {"factor": ("num", 0.25, 4.0)},
    "reverse": {},
    "boomerang": {},
    "loop": {"times": ("int", 2, 10)},
    "freeze": {"seconds": ("num", 0.2, 5.0)},

    # ---- audio
    "mute": {},
    "volume": {"amount": ("num", 0.0, 3.0)},
    "normalize": {},

    # ---- frame
    "scale": {"height": ("int", 144, 2160)},
    "aspect": {"ratio": ("pick", RATIOS)},
    "blurfill": {"ratio": ("pick", RATIOS)},
    "pad": {"ratio": ("pick", RATIOS), "color": ("pick", PAD_COLORS)},
    # Fractions of the frame, not pixels. Whatever is reading the
    # sentence knows what "the left half" and "the top third" mean; it
    # does not know how many pixels wide this particular clip is.
    "crop": {"x": ("num", 0.0, 0.95), "y": ("num", 0.0, 0.95),
             "width": ("num", 0.05, 1.0), "height": ("num", 0.05, 1.0)},
    "rotate": {"degrees": ("pick", (90, 180, 270))},
    "flip": {"axis": ("pick", ("h", "v"))},
    "fps": {"value": ("int", 8, 60)},
    "zoom": {"direction": ("pick", ("in", "out")),
             "amount": ("num", 0.05, 0.6)},

    # ---- colour
    "look": {"name": ("pick", tuple(LOOKS))},
    "grayscale": {},
    "sepia": {},
    "brightness": {"amount": ("num", -0.4, 0.4)},
    "contrast": {"amount": ("num", 0.5, 2.0)},
    "saturation": {"amount": ("num", 0.0, 3.0)},
    # One axis rather than a full white balance: negative is cooler,
    # positive warmer, because "warmer" is what people actually ask for.
    "temperature": {"amount": ("num", -1.0, 1.0)},
    "hue": {"degrees": ("num", -180.0, 180.0)},
    "vignette": {},
    "grain": {"amount": ("num", 2.0, 40.0)},

    # ---- clean-up
    "sharpen": {"amount": ("num", 0.2, 2.0)},
    "denoise": {},
    "stabilize": {},
    "pixelate": {"amount": ("int", 4, 64)},

    # ---- overlay
    # start/end are what make this a caption rather than a watermark. A
    # line held on screen for the whole clip is almost never what
    # someone means by "put a caption on it". 0/0 means the whole clip.
    "text": {"content": ("text", 120),
             "position": ("pick", ("top", "middle", "bottom")),
             "size": ("pick", ("small", "medium", "large")),
             "color": ("pick", tuple(TEXT_COLORS)),
             "box": ("pick", ("on", "off")),
             "start": ("num", 0.0, None),
             "end": ("num", 0.0, None)},

    # ---- transitions
    "fadein": {"seconds": ("num", 0.1, 5.0)},
    "fadeout": {"seconds": ("num", 0.1, 5.0)},

    # ---- export
    "gif": {},
    "quality": {"level": ("pick", ("draft", "standard", "high", "max"))},
}

# Arguments that need not be stated. Every entry here is one more
# sentence that works: "add a caption saying hi" should not have to also
# settle a colour, a position, a size and two timestamps before it can
# run. Anything missing and NOT listed here leaves the op unusable, and
# it is dropped with a note rather than guessed at.
DEFAULTS = {
    ("text", "position"): "bottom",
    ("text", "size"): "medium",
    ("text", "color"): "white",
    ("text", "box"): "on",
    ("text", "start"): 0.0,
    ("text", "end"): 0.0,
    ("zoom", "amount"): 0.18,
    ("zoom", "direction"): "in",
    ("sharpen", "amount"): 0.8,
    ("crop", "x"): 0.0,
    ("crop", "y"): 0.0,
    ("crop", "width"): 1.0,
    ("crop", "height"): 1.0,
    ("pad", "color"): "black",
    ("pad", "ratio"): "16:9",
    ("grain", "amount"): 12.0,
    ("pixelate", "amount"): 16,
    ("temperature", "amount"): 0.35,
    ("hue", "degrees"): 30.0,
    ("quality", "level"): "high",
    ("volume", "amount"): 1.4,
    ("speed", "factor"): 1.5,
    ("loop", "times"): 2,
    ("freeze", "seconds"): 1.0,
    ("fadein", "seconds"): 0.8,
    ("fadeout", "seconds"): 0.8,
}

# Ops that reshape the filter graph rather than adding a link to the
# chain. Only one can apply per render: each consumes the whole stream
# and hands back a different one, so composing two would need a graph
# the prompt has no way to describe.
STRUCTURAL = ("blurfill", "boomerang")


def describe(op):
    """A human-readable line, shown back before the render finishes so a
    misread prompt is caught by reading rather than by watching the
    wrong video come out."""
    name, a = op["op"], op.get("args", {})
    if name == "trim":
        return "keep %gs to %gs" % (a["start"], a["end"])
    if name == "cutout":
        return "remove %gs to %gs" % (a["start"], a["end"])
    if name == "speed":
        f = a["factor"]
        return ("speed up %g×" % f) if f > 1 else ("slow down to %g×" % f)
    if name == "reverse":
        return "play backwards"
    if name == "boomerang":
        return "boomerang (forward, then back)"
    if name == "loop":
        return "repeat %d×" % a["times"]
    if name == "freeze":
        return "hold the last frame %gs" % a["seconds"]
    if name == "mute":
        return "remove the audio"
    if name == "volume":
        v = a["amount"]
        return "silence the audio" if v == 0 else "volume ×%g" % v
    if name == "scale":
        return "resize to %dp" % a["height"]
    if name == "aspect":
        return "crop to %s" % a["ratio"]
    if name == "blurfill":
        return "fit to %s on a blurred background" % a["ratio"]
    if name == "pad":
        return "fit to %s on %s bars" % (a["ratio"], a["color"])
    if name == "crop":
        return ("crop to %d%% × %d%% of the frame"
                % (round(a["width"] * 100), round(a["height"] * 100)))
    if name == "rotate":
        return "rotate %d°" % a["degrees"]
    if name == "flip":
        return "mirror horizontally" if a["axis"] == "h" else "flip vertically"
    if name == "fps":
        return "%d fps" % a["value"]
    if name == "zoom":
        return "slow zoom in" if a["direction"] == "in" else "slow zoom out"
    if name == "look":
        return "%s look" % a["name"]
    if name == "grayscale":
        return "black and white"
    if name == "sepia":
        return "sepia tone"
    if name == "brightness":
        v = a["amount"]
        return ("brighten" if v > 0 else "darken") + " (%+.2f)" % v
    if name == "contrast":
        return "contrast ×%g" % a["amount"]
    if name == "saturation":
        v = a["amount"]
        return ("saturate" if v > 1 else "desaturate") + " (×%g)" % v
    if name == "vignette":
        return "vignette"
    if name == "sharpen":
        return "sharpen"
    if name == "denoise":
        return "reduce noise"
    if name == "stabilize":
        return "steady the shake"
    if name == "temperature":
        v = a["amount"]
        return ("warmer" if v > 0 else "cooler") + " (%+.2f)" % v
    if name == "hue":
        return "shift the hue %+.0f°" % a["degrees"]
    if name == "grain":
        return "film grain"
    if name == "pixelate":
        return "pixelate (%dx blocks)" % a["amount"]
    if name == "normalize":
        return "even out the loudness"
    if name == "quality":
        return "%s-quality export" % a["level"]
    if name == "text":
        # The timing is the part worth reading back. A caption in the
        # wrong place is obvious in the result; one that never appears
        # because it was timed past the end of the clip is not.
        when = ""
        if a.get("start") or a.get("end"):
            when = " from %gs" % a["start"]
            when += " to %gs" % a["end"] if a["end"] else " on"
        return "caption “%s” at the %s%s" % (
            a["content"].replace("\n", " "), a["position"], when)
    if name == "fadein":
        return "fade in over %gs" % a["seconds"]
    if name == "fadeout":
        return "fade out over %gs" % a["seconds"]
    if name == "gif":
        return "export as a GIF"
    return name


def _clean_text(v, limit):
    """Caption text: the person's own words, so it is kept as typed apart
    from what would break a frame or a file.

    Control characters go because drawtext draws them as boxes. Two lines
    is the cap because a third pushes the caption out of the safe area on
    a vertical crop. Length is capped because past a point the line is
    wider than the frame and disappears off both sides."""
    t = str(v).replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(c for c in t if c == "\n" or ord(c) >= 32)
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()][:2]
    return "\n".join(lines).strip()[:limit]


def validate(raw, duration):
    """Coerce a proposed op list into a safe one. -> (ops, skipped, error).

    Anything unrecognised is dropped rather than rejected: one invented
    op should not cost someone their whole edit.

    `skipped` is the list of things that did not survive, in plain words.
    Dropping silently is what makes a tool feel arbitrarily limited -
    you ask for five changes, four happen, and nothing anywhere says the
    fifth was not understood. Saying so is the difference between a
    limitation and a mystery.
    """
    if not isinstance(raw, list):
        return None, [], "Could not read that as a list of edits."

    ops = []
    skipped = []
    for item in raw[:16]:                       # a cap, not a judgement
        if not isinstance(item, dict):
            continue
        name = str(item.get("op", "")).strip().lower()
        given_raw = item.get("args") or {}

        # Not an op - the model's way of saying "this clause was in the
        # request and nothing in the vocabulary covers it". It is the
        # only participant that can notice that, because it is the only
        # one that sees the sentence and the vocabulary at the same time.
        if name == "unsupported":
            what = _clean_text(
                (given_raw or {}).get("what")
                if isinstance(given_raw, dict) else "", 80)
            if what:
                skipped.append("%s - not something I can do yet" % what)
            continue

        spec = OPS.get(name)
        if spec is None:
            if name:
                skipped.append("%s - not something I can do yet" % name)
            continue
        given = item.get("args") or {}
        if not isinstance(given, dict):
            given = {}

        args = {}
        bad = ""
        for param, rule in spec.items():
            kind = rule[0]
            if param not in given:
                if (name, param) in DEFAULTS:
                    args[param] = DEFAULTS[(name, param)]
                    continue
                bad = "needs a %s" % param
                break

            v = given[param]
            if kind == "text":
                cleaned = _clean_text(v, rule[1])
                if not cleaned:
                    bad = "the text was empty"
                    break
                args[param] = cleaned
            elif kind == "pick":
                match = next(
                    (c for c in rule[1]
                     if str(c).lower() == str(v).strip().lower()), None)
                if match is None:
                    # Falling back to the default beats dropping the op:
                    # "make it vertical in 2:3" is a ratio this cannot do,
                    # and reframing it to the nearest one it can is much
                    # closer to the request than doing nothing.
                    if (name, param) in DEFAULTS:
                        args[param] = DEFAULTS[(name, param)]
                        skipped.append(
                            "%s %s=%s - using %s instead"
                            % (name, param, str(v)[:24], args[param]))
                        continue
                    bad = "%s can't be %s" % (param, str(v)[:24])
                    break
                args[param] = match
            else:
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    bad = "%s has to be a number" % param
                    break
                low, high = rule[1], rule[2]
                if low is not None:
                    num = max(low, num)
                if high is not None:
                    num = min(high, num)
                args[param] = int(round(num)) if kind == "int" else num
        if bad:
            skipped.append("%s - %s" % (name, bad))
            continue

        if name in ("trim", "cutout"):
            # The generic clamp cannot know the clip's length, so the
            # bounds land here instead.
            args["start"] = min(args["start"], max(0.0, duration - 0.1))
            end = args["end"] if args["end"] > 0 else duration
            args["end"] = min(end, duration)
            if args["end"] - args["start"] < 0.1:
                skipped.append("%s - that range is empty" % name)
                continue
        if name == "text" and args["end"] and args["end"] <= args["start"]:
            # An end before its start would render nothing at all. Read
            # it as "from here on", which is the likelier intent.
            args["end"] = 0.0
        ops.append({"op": name, "args": args})

    # One of each: two trims or two speeds cannot both be honoured, and
    # the later one is what the sentence meant. Captions are the
    # exception - two captions in different places is a real request.
    seen, unique = set(), []
    for o in reversed(ops):
        if o["op"] == "text":
            unique.append(o)
            continue
        if o["op"] in seen:
            skipped.append("a second %s - keeping the last one" % o["op"])
            continue
        seen.add(o["op"])
        unique.append(o)
    unique.reverse()

    # Only one structural op can apply.
    found = [o for o in unique if o["op"] in STRUCTURAL]
    if len(found) > 1:
        drop = {id(o) for o in found[:-1]}
        skipped += ["%s - only one of these fits in a render" % o["op"]
                    for o in found[:-1]]
        unique = [o for o in unique if id(o) not in drop]

    # blurfill, pad and aspect all decide the output shape, and only one
    # of them can. blurfill wins over pad wins over aspect: each is a
    # more specific request than the next, so the most specific one is
    # the one the sentence went out of its way to say.
    for winner, losers in (("blurfill", ("pad", "aspect")), ("pad", ("aspect",))):
        if any(o["op"] == winner for o in unique):
            skipped += ["%s - %s already sets the shape" % (o["op"], winner)
                        for o in unique if o["op"] in losers]
            unique = [o for o in unique if o["op"] not in losers]

    if not unique:
        return None, skipped, ""
    return unique, skipped, ""


# ---- ops -> ffmpeg ---------------------------------------------------

def _atempo_chain(factor):
    """atempo accepts 0.5-2.0 only, so anything past that is a chain of
    stages multiplying out to the requested factor."""
    stages, f = [], factor
    while f > 2.0:
        stages.append(2.0)
        f /= 2.0
    while f < 0.5:
        stages.append(0.5)
        f /= 0.5
    stages.append(f)
    return ["atempo=%.4f" % x for x in stages]


def _ff_path(path):
    """A filesystem path as a filter argument.

    Inside a filter graph ffmpeg reads ':' as the separator between
    options and '\\' as an escape, so a Windows path breaks the graph
    unless both are neutralised. Forward slashes are accepted on Windows,
    which leaves only the drive colon to escape."""
    p = os.path.abspath(path).replace("\\", "/")
    return p.replace(":", "\\:")


def _fit(w, h, ratio):
    """Target pixel size for a ratio, keeping the long edge and rounding
    to even numbers - H.264 rejects odd dimensions outright."""
    rw, rh = (int(x) for x in ratio.split(":"))
    if rw / rh >= w / h:
        out_w, out_h = w, int(round(w * rh / rw))
    else:
        out_h, out_w = h, int(round(h * rw / rh))
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def _box(w, h, ratio):
    """The smallest box of `ratio` that fully CONTAINS a w x h frame.

    The mirror of _fit: that one finds the largest box the frame can be
    cut down to, this one the smallest it can be padded out to. Cropping
    and letterboxing are the two ways to change shape and they need
    opposite arithmetic, which is easy to get subtly wrong if one
    function tries to serve both.
    """
    rw, rh = (int(x) for x in ratio.split(":"))
    if rw / rh >= w / h:
        out_h, out_w = h, int(round(h * rw / rh))
    else:
        out_w, out_h = w, int(round(w * rh / rw))
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def build_filters(ops, info, workdir=None):
    """-> dict describing the whole command.

    Order is not free. Cutting and speed change how long the result is,
    so they precede the fades that are placed against that length;
    reframing before scaling means the scale acts on the frame that
    survives; colour before text keeps a caption from being graded along
    with the picture; and fades last so they fade the finished frame.
    """
    by = {}
    for o in ops:
        by.setdefault(o["op"], []).append(o["args"])
    first = {k: v[0] for k, v in by.items()}

    vf, af = [], []
    drop_audio = "mute" in by
    input_args = []
    temp_files = []

    src_w = int(info.get("width") or 1280)
    src_h = int(info.get("height") or 720)
    out = float(info.get("duration") or 0)

    # ---- timeline ----------------------------------------------------
    if "trim" in first:
        out = first["trim"]["end"] - first["trim"]["start"]

    if "cutout" in first:
        a, b = first["cutout"]["start"], first["cutout"]["end"]
        if "trim" in first:
            # Expressed against the original clip, but select runs after
            # the input-level trim, so shift it into the trimmed clip's
            # own timeline.
            a -= first["trim"]["start"]
            b -= first["trim"]["start"]
        a, b = max(0.0, a), max(0.0, b)
        if b > a:
            # Drop the frames in the gap, then restamp: without setpts
            # the removed span survives as a freeze the length of the cut.
            vf.append("select='not(between(t,%.3f,%.3f))'" % (a, b))
            vf.append("setpts=N/FRAME_RATE/TB")
            af.append("aselect='not(between(t,%.3f,%.3f))'" % (a, b))
            af.append("asetpts=N/SR/TB")
            out = max(0.1, out - (b - a))

    if "loop" in first:
        # An input option, not a filter: -stream_loop repeats the decode
        # where the loop filter would hold every frame in memory first.
        n = first["loop"]["times"]
        input_args += ["-stream_loop", str(n - 1)]
        out *= n

    if "reverse" in by:
        vf.append("reverse")
        af.append("areverse")

    if "speed" in first:
        f = first["speed"]["factor"]
        vf.append("setpts=%.6f*PTS" % (1.0 / f))
        af.extend(_atempo_chain(f))
        out /= f

    if "freeze" in first:
        d = first["freeze"]["seconds"]
        vf.append("tpad=stop_mode=clone:stop_duration=%.3f" % d)
        af.append("apad=pad_dur=%.3f" % d)
        out += d

    # ---- frame -------------------------------------------------------
    # Before the ratio ops: "crop to the left half, then make it
    # vertical" has to cut first and shape what is left, or the shaping
    # is done against a frame that is about to be thrown away.
    if "crop" in first:
        c = first["crop"]
        cw = max(0.05, min(1.0, c["width"]))
        ch = max(0.05, min(1.0, c["height"]))
        # Clamped so the window cannot run off the edge of the frame,
        # which ffmpeg rejects rather than clipping.
        cx = max(0.0, min(1.0 - cw, c["x"]))
        cy = max(0.0, min(1.0 - ch, c["y"]))
        vf.append("crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f" % (cw, ch, cx, cy))
        src_w = max(2, int(src_w * cw) - int(src_w * cw) % 2)
        src_h = max(2, int(src_h * ch) - int(src_h * ch) % 2)

    if "pad" in first:
        w, h = _box(src_w, src_h, first["pad"]["ratio"])
        vf.append("scale=%d:%d:force_original_aspect_ratio=decrease,"
                  "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:%s"
                  % (w, h, w, h, first["pad"]["color"]))
        src_w, src_h = w, h

    if "aspect" in first:
        # Centre crop to shape. force_original_aspect_ratio would
        # letterbox, which is not what "make it vertical" means - that is
        # what blurfill is for.
        rw, rh = (int(x) for x in first["aspect"]["ratio"].split(":"))
        vf.append("crop='min(iw,ih*%d/%d)':'min(ih,iw*%d/%d)'" % (rw, rh, rh, rw))
        src_w, src_h = _fit(src_w, src_h, first["aspect"]["ratio"])

    if "scale" in first:
        h = first["scale"]["height"]
        src_w = max(2, int(round(src_w * h / max(1, src_h))))
        src_w -= src_w % 2
        src_h = h
        vf.append("scale=-2:%d" % h)

    if "rotate" in first:
        deg = first["rotate"]["degrees"]
        vf.append({90: "transpose=1",
                   180: "transpose=1,transpose=1",
                   270: "transpose=2"}[deg])
        if deg in (90, 270):
            src_w, src_h = src_h, src_w

    if "flip" in first:
        vf.append("hflip" if first["flip"]["axis"] == "h" else "vflip")

    if "zoom" in first:
        z = first["zoom"]
        amt = z["amount"]
        dur = max(0.1, out)
        fps = float(info.get("fps") or 30) or 30.0
        frames = max(1.0, dur * fps)
        # zoompan, because crop cannot do this. crop evaluates its width
        # and height once when the filter is configured - there is no
        # per-frame mode for the size, only for x and y - so a crop whose
        # dimensions depend on t simply holds the first value.
        #
        # d=1 makes zoompan emit one frame per input frame rather than
        # holding each one for a stack of output frames, and s= pins the
        # output size, which zoompan otherwise resets to its own default.
        step = amt / frames
        if z["direction"] == "in":
            zexpr = "min(1+%.6f*on,%.4f)" % (step, 1 + amt)
        else:
            zexpr = "max(%.4f-%.6f*on,1)" % (1 + amt, step)
        vf.append(
            "zoompan=z='%s':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            ":s=%dx%d:fps=%.4f" % (zexpr, src_w, src_h, fps))

    if "fps" in first:
        vf.append("fps=%d" % first["fps"]["value"])

    # ---- colour ------------------------------------------------------
    if "stabilize" in by:
        # deshake rather than vidstab: one pass, where vidstab needs a
        # detect pass written to disk before it can correct anything.
        vf.append("deshake")
    if "denoise" in by:
        vf.append("hqdn3d=3:3:6:6")
    if "look" in first:
        vf.append(LOOKS[first["look"]["name"]])
    if "grayscale" in by:
        vf.append("hue=s=0")
    if "sepia" in by:
        vf.append("colorchannelmixer="
                  ".393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")

    eq = []
    if "brightness" in first:
        eq.append("brightness=%.3f" % first["brightness"]["amount"])
    if "contrast" in first:
        eq.append("contrast=%.3f" % first["contrast"]["amount"])
    if "saturation" in first:
        eq.append("saturation=%.3f" % first["saturation"]["amount"])
    if eq:
        vf.append("eq=" + ":".join(eq))

    if "temperature" in first:
        # One axis: red up and blue down is warmer, the reverse cooler.
        t = first["temperature"]["amount"]
        vf.append("colorbalance=rs=%.3f:gs=%.3f:bs=%.3f"
                  % (0.30 * t, 0.05 * t, -0.28 * t))
    if "hue" in first:
        vf.append("hue=h=%.2f" % first["hue"]["degrees"])

    if "sharpen" in first:
        vf.append("unsharp=5:5:%.3f" % first["sharpen"]["amount"])
    if "pixelate" in first:
        # Down and back up with nearest-neighbour on both legs. Any
        # smooth scaler on the way back interpolates the blocks into
        # mush, which is a blur, not a pixelation.
        n = first["pixelate"]["amount"]
        vf.append("scale=iw/%d:ih/%d:flags=neighbor,"
                  "scale=iw*%d:ih*%d:flags=neighbor" % (n, n, n, n))
    if "vignette" in by:
        vf.append("vignette=PI/5")
    if "grain" in first:
        # Last of the colour work, so the grain sits on top of the grade
        # rather than being graded itself. t+u re-rolls the pattern every
        # frame; without it the noise is a fixed dirty overlay.
        vf.append("noise=alls=%d:allf=t+u" % int(first["grain"]["amount"]))

    # ---- captions ----------------------------------------------------
    # The text goes to a file and drawtext is pointed at the file. That
    # is what keeps someone's caption - which can contain quotes, colons,
    # commas, backslashes, anything - out of the filter string entirely.
    for i, a in enumerate(by.get("text", [])):
        if workdir is None:
            break
        tf = os.path.join(workdir, "caption_%d_%s.txt" % (i, uuid.uuid4().hex[:8]))
        with io.open(tf, "w", encoding="utf-8") as fh:
            fh.write(a["content"])
        temp_files.append(tf)

        # With the box off the caption still has to survive landing on a
        # bright frame, so it gets an outline instead. Text with neither
        # is legible right up until the one shot where it is not.
        backing = ("box=1:boxcolor=black@0.45:boxborderw=18"
                   if a["box"] == "on"
                   else "borderw=3:bordercolor=black@0.65")

        # 0/0 means the whole clip, which needs no enable expression at
        # all - and leaving it off matters, because `enable` forces
        # drawtext to evaluate per frame.
        window = ""
        if a["start"] > 0 or a["end"] > 0:
            end = a["end"] if a["end"] > 0 else max(out, a["start"] + 0.1)
            window = ":enable='between(t,%.3f,%.3f)'" % (a["start"], end)

        vf.append(
            "drawtext=fontfile='%s':textfile='%s':fontcolor=%s"
            ":fontsize=h/%.1f:line_spacing=6:x=(w-text_w)/2:y=%s:%s%s"
            % (_ff_path(_font()), _ff_path(tf), TEXT_COLORS[a["color"]],
               TEXT_SIZE[a["size"]], TEXT_POS[a["position"]], backing, window))

    # ---- audio level -------------------------------------------------
    if "volume" in first and not drop_audio:
        v = first["volume"]["amount"]
        if v == 0:
            drop_audio = True
        else:
            af.append("volume=%.3f" % v)
    if "normalize" in by and not drop_audio:
        # Single-pass loudnorm. The two-pass form measures first and is
        # more accurate, but it means decoding the whole file twice for
        # a correction most people are asking for because their clip is
        # simply too quiet.
        af.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    # ---- fades -------------------------------------------------------
    if "fadein" in first:
        d = first["fadein"]["seconds"]
        vf.append("fade=t=in:st=0:d=%.3f" % d)
        af.append("afade=t=in:st=0:d=%.3f" % d)
    if "fadeout" in first:
        d = min(first["fadeout"]["seconds"], max(0.1, out))
        st = max(0.0, out - d)
        vf.append("fade=t=out:st=%.3f:d=%.3f" % (st, d))
        af.append("afade=t=out:st=%.3f:d=%.3f" % (st, d))

    # ---- structural --------------------------------------------------
    # These reshape the graph, so they are expressed as a filter_complex
    # wrapped around the linear chain rather than another link in it.
    complex_graph = None
    if "blurfill" in first:
        w, h = _fit(src_w, src_h, first["blurfill"]["ratio"])
        chain = ",".join(vf) if vf else "null"
        complex_graph = (
            "[0:v]%s[base];"
            "[base]split[bg][fg];"
            # The background is the same frame blown up to fill the new
            # shape and blurred past recognition - which is the point: it
            # reads as ambient colour, not as a second copy of the video.
            "[bg]scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,gblur=sigma=%d[bgb];"
            "[fg]scale=%d:%d:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vout]"
            % (chain, w, h, w, h, max(8, h // 28), w, h))
        src_w, src_h = w, h
    elif "boomerang" in by:
        chain = ",".join(vf) if vf else "null"
        complex_graph = (
            "[0:v]%s,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[vout]"
            % chain)
        out *= 2
        drop_audio = True          # a reversed soundtrack is never wanted

    return {
        "vf": vf,
        "af": af,
        "drop_audio": drop_audio,
        "out_seconds": out,
        "complex": complex_graph,
        "input_args": input_args,
        "temp_files": temp_files,
        "gif": "gif" in by,
        "width": src_w,
        "height": src_h,
        # Not a filter - it decides how the result is encoded. It rides
        # in the plan so there is one place that turns a request into
        # everything the ffmpeg call needs.
        "quality": first.get("quality", {}).get("level", default_quality()),
    }


_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_font_cache = []


def _font():
    """A font file for drawtext. There is no default: without fontfile=
    the filter fails outright on most builds."""
    if _font_cache:
        return _font_cache[0]
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            _font_cache.append(p)
            return p
    _font_cache.append(_FONT_CANDIDATES[0])     # fail with a clear error
    return _font_cache[0]


def captions_available():
    return os.path.isfile(_font())


# ---- reading the sentence -------------------------------------------
# Rules first, model second. Most edit requests are a handful of stock
# phrases, and matching those directly means the common case needs no
# model at all: instant, works with Ollama stopped and no Groq key, and
# it cannot drift. The model handles the sentences these patterns miss.

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes)"


def _seconds(text):
    """Parse '1:30', '90s', '1m30', '2 minutes' -> seconds."""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", text)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.fullmatch(r"(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*"
                     r"(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?)?", text)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    try:
        return float(text)
    except ValueError:
        return None


def parse_rules(prompt, duration):
    """Best-effort structured read of a prompt. -> list of ops."""
    t = " " + prompt.lower().strip() + " "
    ops = []
    TIME = r"([\d:.]+(?:\s*(?:m|min|mins|minutes|s|sec|secs|seconds))?)"

    # -- captions come first, and the original casing is used ----------
    # Matched against the raw prompt, not the lowered copy: a caption is
    # the one thing here where what the person typed is the value.
    m = re.search(
        r"(?:caption|text|title|subtitle|label|says?|saying|write)"
        r"[^\"'“]{0,24}[\"'“]([^\"'”]{1,120})[\"'”]",
        prompt, re.I)
    if m:
        pos = ("top" if re.search(r"\btop\b|\babove\b", t)
               else "middle" if re.search(r"\bmiddle\b|\bcent(er|re)\b", t)
               else "bottom")
        size = ("large" if re.search(r"\bbig\b|\blarge\b|\bhuge\b", t)
                else "small" if re.search(r"\bsmall\b|\btiny\b", t)
                else "medium")
        ops.append({"op": "text", "args": {"content": m.group(1),
                                           "position": pos, "size": size}})

    # -- freeze and loop, ahead of the rules that would misread them --
    # "hold the last frame 2 seconds" contains "last ... 2 seconds",
    # which the trim rule below reads as "keep the last 2 seconds".
    # "loop 3 times" contains "3 times", which the speed rule reads as
    # 3x. Both are settled here first and the later rules stand down.
    froze = looped = False
    m = re.search(r"(?:freeze|hold)\D{0,16}?" + _NUM, t)
    if m:
        ops.append({"op": "freeze", "args": {"seconds": float(m.group(1))}})
        froze = True
    elif re.search(r"freeze frame|freeze at the end|hold the last frame", t):
        ops.append({"op": "freeze", "args": {"seconds": 1.5}})
        froze = True

    m = re.search(r"(?:loop|repeat)\D{0,10}?(\d{1,2})\s*(?:x|times)?", t)
    if m:
        ops.append({"op": "loop", "args": {"times": int(m.group(1))}})
        looped = True
    elif re.search(r"\bloop\b|\brepeat\b", t):
        ops.append({"op": "loop", "args": {"times": 2}})

    # -- trim ----------------------------------------------------------
    m = (re.search(r"(?:from|between)\s+" + TIME +
                   r"\s*(?:to|-|until|till|through|and)\s*" + TIME, t)
         or re.search(r"(?:cut out|cut|remove|delete|drop|get rid of)\s+"
                      + TIME + r"\s*(?:to|-|until|till|through)\s*"
                      + TIME, t)
         or re.search(r"(?:trim|clip|keep)\s+" + TIME +
                      r"\s*(?:to|-|until|till)\s*" + TIME, t))
    if m:
        a, b = _seconds(m.group(1)), _seconds(m.group(2))
        if a is not None and b is not None and b > a:
            # "cut out 5 to 10" removes the middle; "from 5 to 10" keeps it.
            op = "cutout" if re.search(r"cut out|remove|delete|get rid of", t) \
                else "trim"
            ops.append({"op": op, "args": {"start": a, "end": b}})
    else:
        m = (None if froze else
             re.search(r"(cut|remove|drop|trim|delete|skip|keep|first|last)"
                       r"\D{0,18}?" + _NUM + r"\s*" + _UNIT + r"\b", t))
        if m:
            n = float(m.group(2))
            if m.group(3).startswith("m"):
                n *= 60
            removing = m.group(1) in ("cut", "remove", "drop", "delete", "skip")
            last = bool(re.search(r"\blast\b|\bend\b", t))
            if removing and last:
                ops.append({"op": "trim",
                            "args": {"start": 0.0, "end": max(0.1, duration - n)}})
            elif removing:
                ops.append({"op": "trim", "args": {"start": n, "end": duration}})
            elif last:
                ops.append({"op": "trim",
                            "args": {"start": max(0.0, duration - n),
                                     "end": duration}})
            else:
                ops.append({"op": "trim", "args": {"start": 0.0, "end": n}})

    # -- speed ---------------------------------------------------------
    m = (None if looped else
         re.search(_NUM + r"\s*(?:x|times)\s*(?:faster|speed|slower)?", t))
    if m and re.search(r"speed|fast|slow|\bx\b|times", t):
        f = float(m.group(1))
        if re.search(r"slow", t) and f > 1:
            f = 1.0 / f
        ops.append({"op": "speed", "args": {"factor": f}})
    elif re.search(r"speed (?:it |this |the video )?up|faster|sped up", t):
        ops.append({"op": "speed", "args": {"factor": 2.0}})
    elif re.search(r"slow (?:it |this |the video )?down|slower|slow ?motion|slo-?mo", t):
        ops.append({"op": "speed", "args": {"factor": 0.5}})

    # -- audio ---------------------------------------------------------
    if re.search(r"\bmute\b|no (?:audio|sound)|remove (?:the )?(?:audio|sound)"
                 r"|silent|strip (?:the )?audio", t):
        ops.append({"op": "mute", "args": {}})
    else:
        m = re.search(r"volume\D{0,12}?" + _NUM, t)
        if m:
            ops.append({"op": "volume", "args": {"amount": float(m.group(1))}})
        elif re.search(r"\blouder\b|turn (?:it )?up", t):
            ops.append({"op": "volume", "args": {"amount": 1.6}})
        elif re.search(r"\bquieter\b|turn (?:it )?down", t):
            ops.append({"op": "volume", "args": {"amount": 0.5}})

    # -- shape ---------------------------------------------------------
    ratio = None
    for pat, r in (
        (r"\bsquare\b|\b1:1\b|\b1x1\b", "1:1"),
        (r"\b9:16\b|vertical|portrait|tiktok|reels?\b|shorts?\b|story|stories", "9:16"),
        (r"\b16:9\b|widescreen|landscape|youtube", "16:9"),
        (r"\b4:5\b|instagram post", "4:5"),
    ):
        if re.search(pat, t):
            ratio = r
            break
    if ratio:
        # Blurred bars keep the whole frame; a crop throws the edges away.
        blurred = re.search(r"blur(?:red)?\s*(?:back|bg|background|bars?|fill)"
                            r"|dont crop|don't crop|without cropping|fit\b", t)
        ops.append({"op": "blurfill" if blurred else "aspect",
                    "args": {"ratio": ratio}})

    m = re.search(r"\b(144|240|360|480|720|1080)\s*p\b", t)
    if m:
        ops.append({"op": "scale", "args": {"height": int(m.group(1))}})
    elif re.search(r"\bhd\b|full hd", t):
        ops.append({"op": "scale", "args": {"height": 1080}})

    m = re.search(r"rotate\D{0,12}?(90|180|270)", t)
    if m:
        ops.append({"op": "rotate", "args": {"degrees": int(m.group(1))}})
    elif re.search(r"\brotate\b|\bturn\b.{0,10}\bsideways\b", t):
        ops.append({"op": "rotate", "args": {"degrees": 90}})

    if re.search(r"\bmirror\b|flip horizontal|flip it horizontally", t):
        ops.append({"op": "flip", "args": {"axis": "h"}})
    elif re.search(r"flip (?:it )?(?:upside down|vertical)", t):
        ops.append({"op": "flip", "args": {"axis": "v"}})

    m = re.search(r"(\d{1,2})\s*fps", t)
    if m:
        ops.append({"op": "fps", "args": {"value": int(m.group(1))}})

    # -- motion --------------------------------------------------------
    if re.search(r"zoom (?:slowly )?out|pull back|pull out", t):
        ops.append({"op": "zoom", "args": {"direction": "out", "amount": 0.18}})
    elif re.search(r"\bzoom\b|push in|ken burns", t):
        ops.append({"op": "zoom", "args": {"direction": "in", "amount": 0.18}})
    if re.search(r"boomerang|back and forth|ping.?pong", t):
        ops.append({"op": "boomerang", "args": {}})
    elif re.search(r"\breverse\b|backwards|back to front", t):
        ops.append({"op": "reverse", "args": {}})

    # -- looks ---------------------------------------------------------
    for pat, name in (
        (r"cinematic|film look|movie look|teal and orange", "cinematic"),
        (r"vintage|old film|retro|\b70s\b|\b80s\b", "vintage"),
        (r"\bnoir\b|high contrast black", "noir"),
        (r"warmer|warm tone|golden", "warm"),
        (r"cooler|cold tone|blue tone", "cool"),
        (r"\bvivid\b|punchy colou?rs?", "vivid"),
        (r"dreamy|soft glow|hazy", "dream"),
    ):
        if re.search(pat, t):
            ops.append({"op": "look", "args": {"name": name}})
            break

    if re.search(r"black and white|b&w|\bbw\b|greyscale|grayscale|monochrome", t):
        ops.append({"op": "grayscale", "args": {}})
    if re.search(r"\bsepia\b", t):
        ops.append({"op": "sepia", "args": {}})
    if re.search(r"\bbrighter\b|brighten|too dark", t):
        ops.append({"op": "brightness", "args": {"amount": 0.12}})
    elif re.search(r"\bdarker\b|darken|too bright", t):
        ops.append({"op": "brightness", "args": {"amount": -0.12}})
    if re.search(r"more contrast|contrasty", t):
        ops.append({"op": "contrast", "args": {"amount": 1.3}})
    if re.search(r"more colou?r|saturate|vibrant|make it pop", t):
        ops.append({"op": "saturation", "args": {"amount": 1.4}})
    elif re.search(r"less colou?r|desaturate|washed out|muted colou?rs", t):
        ops.append({"op": "saturation", "args": {"amount": 0.5}})
    if re.search(r"vignette|dark edges|dark corners", t):
        ops.append({"op": "vignette", "args": {}})

    # -- clean-up ------------------------------------------------------
    if re.search(r"sharpen|sharper|crisp", t):
        ops.append({"op": "sharpen", "args": {"amount": 0.9}})
    if re.search(r"denoise|noisy|grainy|clean it up", t):
        ops.append({"op": "denoise", "args": {}})
    if re.search(r"stabili[sz]e|shaky|steady", t):
        ops.append({"op": "stabilize", "args": {}})

    # -- fades / export ------------------------------------------------
    m = re.search(r"fade in\D{0,14}?" + _NUM, t)
    if m:
        ops.append({"op": "fadein", "args": {"seconds": float(m.group(1))}})
    elif re.search(r"fade in|fade from black", t):
        ops.append({"op": "fadein", "args": {"seconds": 0.8}})
    m = re.search(r"fade out\D{0,14}?" + _NUM, t)
    if m:
        ops.append({"op": "fadeout", "args": {"seconds": float(m.group(1))}})
    elif re.search(r"fade out|fade to black", t):
        ops.append({"op": "fadeout", "args": {"seconds": 0.8}})

    if re.search(r"\bgif\b|as a gif|make a gif", t):
        ops.append({"op": "gif", "args": {}})

    return ops


_SCHEMA = """You convert a video editing request into JSON.

Reply with ONLY a JSON array. No prose, no markdown fence.
Each element: {"op": "<name>", "args": {...}}

CUTTING
  trim        start, end (seconds)      - keep only this range
  cutout      start, end (seconds)      - remove this range
  speed       factor (0.25-4; >1 faster)
  reverse     -
  boomerang   -                         - forward then backwards
  loop        times (2-10)
  freeze      seconds (0.2-5)           - hold the last frame

AUDIO
  mute        -
  volume      amount (0-3; 1 unchanged)
  normalize   -                         - even out quiet/loud audio

FRAME
  crop        x, y, width, height       - FRACTIONS of the frame, 0-1.
                                          left half = x 0, width 0.5.
                                          top third = y 0, height 0.33.
  aspect      ratio                     - crops to fit the shape
  blurfill    ratio                     - fits, blurred backdrop
  pad         ratio, color ("black"|"white"|"gray")  - fits, solid bars
  scale       height (144-2160)
  rotate      degrees (90|180|270)
  flip        axis ("h"|"v")
  fps         value (8-60)
  zoom        direction ("in"|"out"), amount (0.05-0.6)
  ratio is one of "1:1" "9:16" "16:9" "4:5" "4:3" "21:9"

COLOUR
  look        name ("cinematic"|"vintage"|"noir"|"warm"|"cool"|"vivid"
                    |"dream"|"teal-orange"|"bleach"|"faded"|"moody"
                    |"sunset"|"neon"|"film")
  grayscale   -
  sepia       -
  brightness  amount (-0.4 to 0.4)
  contrast    amount (0.5 to 2.0)
  saturation  amount (0.0 to 3.0)
  temperature amount (-1 to 1; negative cooler, positive warmer)
  hue         degrees (-180 to 180)
  vignette    -
  grain       amount (2-40)             - film grain

CLEAN-UP
  sharpen     amount (0.2-2.0)
  denoise     -
  stabilize   -                         - steady shaky footage
  pixelate    amount (4-64)             - block size, for censoring

CAPTIONS
  text        content (max 120 chars),
              position ("top"|"middle"|"bottom"),
              size ("small"|"medium"|"large"),
              color ("white"|"black"|"yellow"|"red"|"orange"|"green"
                     |"blue"|"pink"),
              box ("on"|"off"),
              start, end (seconds; omit both for the whole clip)
              Several text ops are fine - that is how you caption
              different moments with different lines.

TRANSITIONS
  fadein      seconds
  fadeout     seconds

EXPORT
  gif         -
  quality     level ("draft"|"standard"|"high"|"max")

PHRASES
These come up constantly and the right op is not always the obvious one.
  "vertical"/"for tiktok"/"for reels"/"for shorts"  -> ratio "9:16"
  "square"/"for instagram"                          -> ratio "1:1"
  "widescreen"/"cinematic bars"                     -> ratio "16:9" or "21:9"
  "...with a blurred background"                    -> blurfill, NOT aspect
  "...with black bars"/"don't crop it"/"fit it in"  -> pad, NOT aspect
  "...cropped to fill"/no backdrop mentioned        -> aspect
  "the first N seconds"     -> trim start 0 end N
  "cut the first N seconds" -> trim start N end 0
  "the last N seconds"      -> trim start (duration-N) end 0
  "cut out the middle bit"  -> cutout over the middle of the duration
  "slow motion"             -> speed factor below 1
  "timelapse"/"speed it up" -> speed factor above 1
  "make it pop"             -> saturation and contrast slightly up
  "fix the shaky footage"   -> stabilize
  "too quiet"/"audio is uneven" -> normalize
  "blur the face"/"censor"  -> pixelate
  "best quality"/"export in high quality" -> quality "max" or "high"

RULES
- Ops with no args: {"op": "mute", "args": {}}.
- Omit any arg you have no reason to set; sensible defaults are filled in.
- At most one of each op, except text.
- Break a request into as many ops as it takes. Every clause someone
  wrote is usually its own op. "make it a moody vertical tiktok with a
  blurred background and the title on the first 3 seconds" is three ops
  (blurfill, look, text), not one.
- Only leave something out if no combination of the ops above comes
  close to it. Reply [] only if none of it maps at all.
- If part of the request maps to nothing above, do NOT silently drop it
  and do NOT substitute a different op for it. Add
  {"op": "unsupported", "args": {"what": "<that part, in a few words>"}}
  so it can be reported back. One of these per unmet part.
"""


def plan(prompt, duration, complete=None):
    """Turn a sentence into a validated op list.
    -> (ops, source, skipped, error).

    `source` is "rules" or "model", so the UI can say which read the
    sentence. `complete` is passed in rather than imported so this module
    holds no opinion about which provider is running, and so the parsing
    can be tested with no model at all.

    WHY THE MODEL GOES FIRST
    This used to run the regex rules first and return the moment they
    matched anything. That is what made the feature feel arbitrarily
    limited, and the mechanism is worth spelling out because it is not
    obvious from either piece on its own: write "make it vertical for
    tiktok, cinematic, caption it 'day one', and fade out", the rules
    recognise the word vertical, and the function returns one crop. The
    other three requests were never read by anything. Nothing failed,
    nothing was reported, and from outside it looks exactly like a tool
    that can only do one thing at a time.

    So the model now sees every sentence it can, and the rules are what
    they should always have been: the answer when there is no model to
    ask, not a gate in front of one.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "", [], "Say what you want changed."
    if len(prompt) > 600:
        return None, "", [], "That request is too long. Try a shorter one."

    # Read the sentence with the rules regardless, as the fallback for
    # every way the model below can fail to produce something usable.
    rule_ops, _rule_skipped, _ = validate(parse_rules(prompt, duration), duration)

    def fallback(reason):
        if rule_ops:
            return rule_ops, "rules", [], ""
        return None, "", [], reason

    if complete is None:
        return fallback(
            "No model is running to read that, and the built-in patterns "
            "did not match it. Start the local models, or phrase it like "
            "\"trim from 5s to 20s\" or \"vertical with a blurred "
            "background\".")

    try:
        reply = complete(
            _SCHEMA
            + "\nThe video is %.1f seconds long." % duration
            + "\n\nRequest: %s\n\nJSON:" % prompt)
    except Exception as e:                       # noqa: BLE001 - reported
        return fallback("Could not reach the model to read that: %s" % e)

    raw = _extract_json(reply or "")
    if raw is None:
        return fallback("I couldn't work out what to change from that. Try "
                        "naming the changes one per clause.")

    ops, skipped, err = validate(raw, duration)
    if err:
        return None, "", [], err
    if not ops:
        # The rules are still worth a try here: a model can return an
        # empty array for a sentence the patterns read perfectly well.
        if rule_ops:
            return rule_ops, "rules", skipped, ""
        return None, "", skipped, (
            "None of that maps to an edit I can do. I can cut, re-time, "
            "reframe, crop, grade colour, caption, stabilise, clean up "
            "audio, fade, and export GIFs - try naming one of those.")
    return ops, "model", skipped, ""


def _extract_json(text):
    """Pull the first JSON array out of a reply. Models wrap them in
    fences and commentary however firmly the prompt says not to."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


# ---- running it ------------------------------------------------------

def start_edit(owner, src_path, ops):
    """Queue an edit built from a validated op list. -> (job_id, error)."""
    if not available():
        return None, unavailable_reason()

    info, err = probe(src_path)
    if err or not info:
        return None, err or "Could not read that video."
    if info["duration"] > MAX_INPUT_SECONDS:
        return None, ("That video is %d minutes long. The limit is %d."
                      % (info["duration"] // 60, MAX_INPUT_SECONDS // 60))
    if not ops:
        return None, "No edits to apply."
    if any(o["op"] == "text" for o in ops) and not captions_available():
        return None, ("Captions need a font file, and none was found on this "
                      "server.")

    job_id = uuid.uuid4().hex[:12]
    plan_ = build_filters(ops, info, workdir=OUTPUT_DIR)

    if plan_["out_seconds"] > MAX_OUTPUT_SECONDS:
        for f in plan_["temp_files"]:
            _cleanup(f)
        return None, ("That would produce %d seconds of video. The limit is %d."
                      % (plan_["out_seconds"], MAX_OUTPUT_SECONDS))

    # reverse and boomerang buffer every decoded frame. At 1080p that is
    # roughly 6MB a frame, so a long clip is an out-of-memory kill rather
    # than a slow render - hence a much tighter cap for those two.
    if (any(o["op"] in ("reverse", "boomerang") for o in ops)
            and plan_["out_seconds"] > 30):
        for f in plan_["temp_files"]:
            _cleanup(f)
        return None, ("Reversing is limited to 30 seconds - every frame has "
                      "to be held in memory at once.")

    busy = active_job_for(owner)
    if busy:
        for f in plan_["temp_files"]:
            _cleanup(f)
        return None, "You already have a render going. Wait for it to finish."

    ext = ".gif" if plan_["gif"] else ".mp4"
    out_name = job_id + ext
    out_path = os.path.join(OUTPUT_DIR, out_name)

    trim = next((o["args"] for o in ops if o["op"] == "trim"), None)

    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "owner": owner,
            "status": "queued",
            "stage": "waiting",
            "error": "",
            "url": None,
            "started": time.time(),
            "duration": round(plan_["out_seconds"], 2),
            "steps": [describe(o) for o in ops],
        }

    t = threading.Thread(
        target=_encode_edit,
        args=(job_id, src_path, out_path, out_name, trim, plan_, info),
        daemon=True,
    )
    t.start()
    return job_id, None


def _encode_edit(job_id, src, dst, out_name, trim, plan_, info):
    ff, _fp = _tools()
    _set(job_id, status="running", stage="rendering")

    args = [ff, "-y"]
    args += plan_["input_args"]
    if trim:
        # Before -i, so the decoder skips rather than decodes-and-drops.
        # -accurate_seek keeps the cut where it was asked for instead of
        # at the nearest keyframe.
        args += ["-accurate_seek", "-ss", "%.3f" % trim["start"],
                 "-to", "%.3f" % trim["end"]]
    args += ["-i", src]

    has_audio = bool(info.get("has_audio")) and not plan_["drop_audio"]

    if plan_["gif"]:
        # A GIF is 256 colours, so the palette has to be built from this
        # clip's own frames - the default web palette turns a desert into
        # posterised bands. Two passes in one graph: generate, then apply.
        chain = ",".join(plan_["vf"]) if plan_["vf"] else "null"
        w = min(720, plan_["width"])
        args += ["-filter_complex",
                 "[0:v]%s,fps=16,scale=%d:-2:flags=lanczos,split[x][y];"
                 "[x]palettegen=stats_mode=diff[p];"
                 "[y][p]paletteuse=dither=bayer:bayer_scale=5[vout]"
                 % (chain, w),
                 "-map", "[vout]", "-an", "-loop", "0", dst]
    else:
        if plan_["complex"]:
            args += ["-filter_complex", plan_["complex"], "-map", "[vout]"]
            if has_audio:
                args += ["-map", "0:a?"]
        elif plan_["vf"]:
            args += ["-vf", ",".join(plan_["vf"])]

        if has_audio and plan_["af"] and not plan_["complex"]:
            args += ["-af", ",".join(plan_["af"])]

        # -an, when there is no audio, comes from here too - one place
        # decides everything about how the result is written.
        args += _codec_args(plan_["quality"], has_audio) + [dst]

    limit = _timeout_for(plan_["quality"])
    try:
        r = _run(args, timeout=limit)
    except subprocess.TimeoutExpired:
        _set(job_id, status="failed", stage="",
             error=("That render took longer than %ds and was stopped. Try a "
                    "shorter clip, fewer changes, or ask for draft quality."
                    % limit))
        _cleanup(dst)
        return
    except OSError as e:
        _set(job_id, status="failed", stage="",
             error="Could not run ffmpeg: %s" % e)
        return
    finally:
        # The caption files exist only for the length of the render.
        for f in plan_["temp_files"]:
            _cleanup(f)

    if r.returncode != 0 or not os.path.exists(dst):
        tail = (r.stderr or "").strip().splitlines()
        detail = tail[-1][:200] if tail else "unknown error"
        _set(job_id, status="failed", stage="", error="Render failed: %s" % detail)
        _cleanup(dst)
        return

    _set(job_id, status="done", stage="",
         url="/static/video/renders/%s" % out_name,
         size_bytes=os.path.getsize(dst))
