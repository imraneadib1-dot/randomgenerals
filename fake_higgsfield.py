# -*- coding: utf-8 -*-
"""A Higgsfield Cloud that runs in this process, for the checks.

    from fake_higgsfield import serve, Fake, CONTRACT, media
    port = serve()           # -> set HIGGSFIELD_API_BASE=http://127.0.0.1:<port>

It enforces the request contract from docs.higgsfield.ai's OpenAPI
spec - every field each model's schema declares, with its type and
enum, and nothing else - so a payload the real API would 422 is 422
here. Jobs go queued -> in_progress -> a scripted outcome over three
status polls; Fake.script holds the knobs (a 503 on submit, the
concurrency 400, an NSFW verdict, a stub file for a clip) and
Fake.requests records every call for the checks to read.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WORK = tempfile.mkdtemp(prefix="fakehf-")
PORT = 0

# ------------------------------------------------------- a fake Higgsfield
# The contract, transcribed from docs.higgsfield.ai/docs/openapi.json:
# path -> {field: (type, enum-or-None, required)}. "int" and "str" are
# exact: Veo's duration is a string "6", Kling's an integer 5.
INT, STR, NUM, BOOL = "int", "str", "num", "bool"
KLING = {"prompt": (STR, None, True), "duration": (INT, [5, 10], False),
         "cfg_scale": (NUM, None, False), "negative_prompt": (STR, None, False)}
KLING_MASTER_T2V = dict(KLING, aspect_ratio=(STR, ["1:1", "16:9", "9:16"], False))
KLING_I2V = dict(KLING, image_url=(STR, None, True))
VEO = {"prompt": (STR, None, True), "duration": (STR, ["4", "6", "8"], False),
       "resolution": (STR, ["720", "1080"], True),
       "aspect_ratio": (STR, ["16:9", "9:16"], True),
       "generate_audio": (BOOL, None, True)}
VEO_I2V = dict(VEO, image_url=(STR, None, True),
               resolution=(STR, ["720", "1080"], False),
               aspect_ratio=(STR, ["16:9", "9:16"], False),
               generate_audio=(BOOL, None, False))
SEEDANCE = {"prompt": (STR, None, True), "duration": (INT, list(range(2, 13)), False),
            "resolution": (STR, ["480", "720", "1080"], False),
            "aspect_ratio": (STR, ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"], False),
            "camera_fixed": (BOOL, None, False)}
SEEDANCE_I2V = dict(SEEDANCE, image_url=(STR, None, True))
SORA = {"prompt": (STR, None, True), "duration": (INT, [4, 8, 12], False),
        "resolution": (STR, ["720p"], False), "aspect_ratio": (STR, ["16:9", "9:16"], False)}
SORA_PRO = dict(SORA, resolution=(STR, ["720p", "1080p"], False))
SORA_I2V = dict(SORA, image_url=(STR, None, False))
SORA_PRO_I2V = dict(SORA_PRO, image_url=(STR, None, True))
HAILUO = {"prompt": (STR, None, True), "duration": (INT, [6, 10], False),
          "prompt_optimizer": (BOOL, None, False)}
HAILUO_I2V = dict(HAILUO, image_url=(STR, None, True))
WAN = {"prompt": (STR, None, True), "seed": (INT, None, False),
       "duration": (INT, [5, 10], False), "audio_url": (STR, None, False),
       "resolution": (STR, ["480p", "720p", "1080p"], False),
       "negative_prompt": (STR, None, False)}
WAN_I2V = dict(WAN, image_url=(STR, None, True))
DOP = {"prompt": (STR, None, True), "seed": (INT, None, False),
       "motions": ("list", None, False), "image_url": (STR, None, True),
       "end_image_url": (STR, None, False), "enhance_prompt": (BOOL, None, False)}
CONTRACT = {
    "/kling-video/v2.5-turbo/pro/text-to-video": KLING,
    "/kling-video/v2.5-turbo/pro/image-to-video": KLING_I2V,
    "/kling-video/v2.1/master/text-to-video": KLING_MASTER_T2V,
    "/kling-video/v2.1/master/image-to-video": KLING_I2V,
    "/veo3.1": VEO, "/veo3.1/fast": VEO,
    "/veo3.1/image-to-video": VEO_I2V, "/veo3.1/fast/image-to-video": VEO_I2V,
    "/bytedance/seedance/v1/lite/text-to-video": SEEDANCE,
    "/bytedance/seedance/v1/pro/fast/text-to-video": SEEDANCE,
    "/bytedance/seedance/v1/lite/image-to-video": SEEDANCE_I2V,
    "/bytedance/seedance/v1/pro/fast/image-to-video": SEEDANCE_I2V,
    "/sora-2/text-to-video": SORA, "/sora-2/text-to-video/pro": SORA_PRO,
    "/sora-2/image-to-video": SORA_I2V, "/sora-2/image-to-video/pro": SORA_PRO_I2V,
    "/minimax/hailuo-2.3/standard/text-to-video": HAILUO,
    "/minimax/hailuo-2.3/standard/image-to-video": HAILUO_I2V,
    "/wan-25-preview/text-to-video": WAN, "/wan-25-preview/image-to-video": WAN_I2V,
    "/higgsfield-ai/dop/standard": DOP,
}


def validate(schema, body):
    """-> list of problems, the way FastAPI would report them."""
    problems = []
    for key, (typ, enum, required) in schema.items():
        if key not in body:
            if required:
                problems.append("%s: field required" % key)
            continue
        v = body[key]
        ok = {INT: lambda x: isinstance(x, int) and not isinstance(x, bool),
              STR: lambda x: isinstance(x, str),
              NUM: lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
              BOOL: lambda x: isinstance(x, bool),
              "list": lambda x: isinstance(x, list)}[typ](v)
        if not ok:
            problems.append("%s: wrong type %s" % (key, type(v).__name__))
        elif enum is not None and v not in enum:
            problems.append("%s: %r not in %s" % (key, v, enum))
    for key in body:
        if key not in schema:
            problems.append("%s: extra field" % key)
    return problems


class Fake:
    """State for the fake provider: what it has been asked, and what it
    has been told to do next."""
    lock = threading.Lock()
    requests = []                # every (method, path, query, headers, body)
    jobs = {}                    # request_id -> {"polls": n, "outcome": …}
    script = {}                  # knobs the checks set
    media = {}                   # request_id -> bytes


def media(width=1280, height=720, seconds=5):
    """A real MP4 when ffmpeg is here, else bytes that pass the
    container check and nothing more."""
    exe = shutil.which("ffmpeg")
    if exe:
        out = os.path.join(WORK, "m_%dx%d_%d.mp4" % (width, height, seconds))
        if not os.path.exists(out):
            subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=%dx%d:rate=24:duration=%d" % (width, height, seconds),
                            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", out],
                           check=True, timeout=60)
        with open(out, "rb") as fh:
            return fh.read()
    return b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 40_000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=None, raw=None, ctype="application/json"):
        self.send_response(code)
        data = raw if raw is not None else json.dumps(body or {}).encode()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Correlation-ID", uuid.uuid4().hex)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _auth_ok(self):
        return self.headers.get("Authorization") == "Key test-id:test-secret"

    def _record(self, method, body):
        u = urlparse(self.path)
        with Fake.lock:
            Fake.requests.append({"method": method, "path": u.path,
                                  "query": parse_qs(u.query),
                                  "headers": dict(self.headers), "body": body})

    def do_PUT(self):
        data = self._body()
        self._record("PUT", data)
        if self.path.startswith("/upload/"):
            if "Authorization" in self.headers:
                return self._send(403, {"detail": "credentials sent to storage"})
            return self._send(200, raw=b"", ctype="text/plain")
        self._send(404, {"detail": "Not Found"})

    def do_GET(self):
        u = urlparse(self.path)
        self._record("GET", None)
        if u.path.startswith("/media/"):
            rid = u.path.split("/")[-1].split(".")[0]
            blob = Fake.media.get(rid)
            if blob is None:
                return self._send(404, {"detail": "gone"})
            if Fake.script.get("media_503_once"):
                Fake.script["media_503_once"] = False
                return self._send(503, {"detail": "storage hiccup"})
            return self._send(200, raw=blob, ctype="video/mp4")
        if not self._auth_ok():
            return self._send(401, {"detail": "Invalid credentials"})
        m = re.match(r"^/requests/([^/]+)/status$", u.path)
        if not m:
            return self._send(404, {"detail": "Not Found"})
        rid = m.group(1)
        with Fake.lock:
            job = Fake.jobs.get(rid)
            if not job:
                return self._send(404, {"detail": "Request not found"})
            if job.get("status_5xx", 0) > 0:
                job["status_5xx"] -= 1
                return self._send(502, {"detail": "bad gateway"})
            job["polls"] += 1
            base = "http://127.0.0.1:%d" % PORT
            view = {"request_id": rid, "status_url": base + "/requests/%s/status" % rid,
                    "cancel_url": base + "/requests/%s/cancel" % rid, "error": None}
            if job["polls"] == 1:
                view["status"] = "queued"
            elif job["polls"] == 2:
                view["status"] = "in_progress"
            else:
                outcome = job["outcome"]
                if outcome == "completed":
                    view["status"] = "completed"
                    view["video"] = {"url": base + "/media/%s.mp4" % rid}
                elif outcome == "nsfw":
                    view["status"] = "nsfw"
                elif outcome == "canceled":
                    view["status"] = "canceled"
                else:
                    view["status"] = "failed"
                    view["error"] = "Generation failed"
            return self._send(200, view)

    def do_POST(self):
        u = urlparse(self.path)
        raw = self._body()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        self._record("POST", body)
        if not self._auth_ok():
            return self._send(401, {"detail": "Invalid credentials"})
        if u.path == "/files/generate-upload-url":
            if (body or {}).get("content_type") not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/jpg"):
                return self._send(422, {"detail": [{"loc": ["body", "content_type"], "msg": "unsupported"}]})
            fid = uuid.uuid4().hex
            base = "http://127.0.0.1:%d" % PORT
            return self._send(200, {"public_url": base + "/public/%s" % fid,
                                    "upload_url": base + "/upload/%s" % fid,
                                    "content_type": body["content_type"],
                                    "upload_headers": {"Content-Type": body["content_type"],
                                                       "x-amz-tagging": "retention=temporary"}})
        m = re.match(r"^/requests/([^/]+)/cancel$", u.path)
        if m:
            with Fake.lock:
                job = Fake.jobs.get(m.group(1))
                if not job:
                    return self._send(404, {"detail": "Request not found"})
                if job["polls"] >= 2:
                    return self._send(400, {"detail": "Request already started"})
                job["outcome"] = "canceled"
            return self._send(202, raw=b"", ctype="text/plain")
        path = u.path
        estimating = path.startswith("/estimate/")
        if estimating:
            path = path[len("/estimate"):]
        schema = CONTRACT.get(path)
        if schema is None:
            return self._send(404, {"detail": "Model not found"})
        if body is None:
            return self._send(422, {"detail": [{"loc": ["body"], "msg": "invalid JSON"}]})
        problems = validate(schema, body)
        if problems:
            return self._send(422, {"detail": [{"loc": ["body", p.split(":")[0]], "msg": p}
                                               for p in problems]})
        if estimating:
            return self._send(200, {"credits": "1.500", "usd": "0.094"})
        # Scripted trouble, consumed once each.
        if Fake.script.get("submit_503", 0) > 0:
            Fake.script["submit_503"] -= 1
            return self._send(503, {"detail": "Model is not ready"})
        if Fake.script.get("submit_429", 0) > 0:
            Fake.script["submit_429"] -= 1
            return self._send(429, {"detail": "slow down"})
        if Fake.script.get("concurrency", 0) > 0:
            Fake.script["concurrency"] -= 1
            return self._send(400, {"detail": "Maximum number of concurrent requests (4) has been reached"})
        if Fake.script.get("reject_input"):
            Fake.script["reject_input"] = False
            return self._send(400, {"detail": "Input rejected by moderation"})
        rid = str(uuid.uuid4())
        with Fake.lock:
            Fake.jobs[rid] = {"polls": 0, "outcome": Fake.script.pop("outcome", "completed"),
                              "status_5xx": Fake.script.pop("status_5xx", 0), "body": body}
            Fake.media[rid] = Fake.script.pop("media", None) or media()
        base = "http://127.0.0.1:%d" % PORT
        return self._send(200, {"status": "queued", "request_id": rid,
                                "status_url": base + "/requests/%s/status" % rid,
                                "cancel_url": base + "/requests/%s/cancel" % rid})




def serve() -> int:
    """Start on a free port, in a daemon thread. -> the port."""
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["HIGGSFIELD_API_BASE"] = "http://127.0.0.1:%d" % PORT
    return PORT
