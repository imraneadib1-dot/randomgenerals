# -*- coding: utf-8 -*-
"""Does the video engine do what it says, end to end, without a key?

    python check_video.py

WHY THESE CHECKS

A video job used to live in a dict: gone on restart, its URL the
provider's (which expires), its content never looked at. The engine
in videogen.py persists jobs, polls on a backoff, resubmits when the
provider is at its limit, downloads and validates every result, and
refunds the quota on every failure. Each of those is a promise about
behaviour under conditions a person cannot produce on demand - a 503
at the wrong moment, an NSFW verdict, a file that is not a video - so
each is produced here.

The Higgsfield API is played by a small HTTP server in this process
that enforces the request contract from their OpenAPI spec: every
field a model's schema declares, with its type and its enum, and
nothing else. A payload the real API would 422 is 422 here. That is
the "double-check the payload parameters" step, run on every commit.

Every job the engine makes here is driven by hand (videogen.poll_once)
rather than by the background thread, so the checks are deterministic.
"""
import base64
import json
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="videotest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_SITE_URL"] = "https://example.test"
os.environ["VIDEO_WEBHOOKS"] = "1"
os.environ["HIGGSFIELD_API_KEY_ID"] = "test-id"
os.environ["HIGGSFIELD_API_KEY_SECRET"] = "test-secret"
os.environ.pop("PIXVERSE_API_KEY", None)
os.environ.pop("HF_TOKEN", None)
os.environ.pop("TRIPO_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)
sys.path.insert(0, os.path.abspath("."))

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-60s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


# ------------------------------------------------------- a fake Higgsfield
from fake_higgsfield import Fake, CONTRACT, media, serve   # noqa: E402

serve()

# The poller thread would race the hand-driven polls below. Marking it
# started before app.py loads keeps it off; start_poller still records
# the callbacks the routes rely on.
import videogen                                           # noqa: E402
videogen._poller_started = True
videogen.OUTPUT_DIR = os.path.join(WORK, "generated")
videogen.POLL_INTERVALS = (0, 0, 0, 0, 0, 0)
videogen.RESUBMIT_INTERVALS = (0, 0, 0)
videogen._sleep = lambda s: None
import higgsfield_api                                     # noqa: E402
higgsfield_api._sleep = lambda s: None
higgsfield_api.MAX_CONCURRENT = 50
# This check submits far faster than a person could; the per-minute
# bucket is real and would (correctly) queue most of them.
higgsfield_api._bucket = higgsfield_api._Bucket(10 ** 6)
for _k in higgsfield_api.RETRY_AFTER:
    higgsfield_api.RETRY_AFTER[_k] = 0
import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402

for _limiter in (appmod.LIMIT_VIDEO, appmod.LIMIT_VIDEO_ENHANCE):
    _limiter.rate = _limiter.burst = 10 ** 6

HAVE_FFPROBE = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))
print("ffprobe on this machine: %s" % ("yes" if HAVE_FFPROBE else "no - resolution checks are skipped"))


def last_submit():
    return next(r for r in reversed(Fake.requests)
                if r["method"] == "POST" and r["path"] in CONTRACT)


def reset_quota():
    """The check makes far more clips than a day allows."""
    with db._connect() as conn:
        conn.execute("DELETE FROM video_quota WHERE owner_id = ?", (uid,))


def drive(job_id, max_rounds=12):
    """Poll until the job is out of queued/running. -> the row."""
    for _ in range(max_rounds):
        job = db.video_job_get(job_id)
        if job["status"] not in ("queued", "running", "validating"):
            return job
        videogen.poll_once()
    return db.video_job_get(job_id)


# ---------------------------------------------------------- the client
print("\n== every payload matches the provider's contract ==")
bad = []
for mid, m in higgsfield_api.MODELS.items():
    for direction in ("t2v", "i2v"):
        if not m[direction]:
            continue
        kw = {"image_url": "https://example.com/in.jpg"} if direction == "i2v" else {}
        est, err = higgsfield_api.estimate(
            mid, "A red kite over a wheat field at dusk, slow pan.",
            seconds=7, ratio="9:16", resolution="1080p", negative="text",
            motion="low", seed=42, **kw)
        if est is None:
            bad.append("%s %s: %s" % (mid, direction, err))
check("every model, both directions, accepted by the schema", bad, [])
path, body = higgsfield_api.build_body("veo-3.1", "x", seconds=7)
check("Veo's duration is a string and snapped to its set", body["duration"], "6")
check("and its required fields are present",
      all(k in body for k in ("resolution", "aspect_ratio", "generate_audio")), True)
path, body = higgsfield_api.build_body("kling-2.5-turbo", "x", seconds=7, ratio="9:16")
check("Kling's duration is an integer and 7 snaps down to 5", body["duration"], 5)
check("Kling 2.5 gets no aspect_ratio (its schema has none)", "aspect_ratio" in body, False)
path, body = higgsfield_api.build_body("kling-2.1-master", "x", ratio="9:16")
check("Kling 2.1 Master does", body.get("aspect_ratio"), "9:16")
path, body = higgsfield_api.build_body("seedance-lite", "x", seconds=99, resolution="1080p", motion="low")
check("Seedance clamps 99s to 12 and spells 1080p as '1080'", (body["duration"], body["resolution"]), (12, "1080"))
check("low motion becomes a fixed camera on Seedance", body["camera_fixed"], True)
path, body = higgsfield_api.build_body("wan-2.5", "x", seed=None)
check("Wan's unset seed is -1, as its schema says", body["seed"], -1)
path, body = higgsfield_api.build_body("hailuo-2.3", "x", negative="blur")
check("Hailuo never receives a negative prompt", "negative_prompt" in body, False)
check("nor its own rewriter, which would fight ours", body["prompt_optimizer"], False)
path, body = higgsfield_api.build_body("dop", "x", image_url="https://e/x.jpg", seed=0)
check("DoP is image-to-video at its own path", path, "/higgsfield-ai/dop/standard")
check("its seed floor is 1", body["seed"], 1)
try:
    higgsfield_api.build_body("dop", "x")
    check("DoP without an image is refused before any call", False, True)
except ValueError as e:
    check("DoP without an image is refused before any call", "text-to-video" in str(e), True)

print("\n== the spec is clamped to the model, never refused late ==")
caps = videogen.caps_for(higgsfield_api, "seedance-lite")
spec = videogen.parse_spec({"prompt": "x", "seconds": "99", "ratio": "junk", "seed": "abc",
                            "resolution": "4k", "motion": "wild", "model": "seedance-lite"}, caps)
check("seconds 99 -> the model's 12", spec.seconds, 12)
check("an unknown ratio -> 16:9", spec.ratio, "16:9")
check("a non-numeric seed -> random later", spec.seed, None)
check("an unknown resolution -> the default", spec.resolution, "720p")
check("an unknown motion -> medium", spec.motion, "medium")
caps = videogen.caps_for(higgsfield_api, "sora-2")
spec = videogen.parse_spec({"prompt": "x", "seconds": 7, "negative": "blur"}, caps)
check("7s on Sora (4/8/12) -> 8", spec.seconds, 8)
check("a negative prompt on a model without one is dropped", spec.negative, "")
caps = videogen.caps_for(higgsfield_api, "no-such-model")
check("an unknown model falls back to the default", caps["model"], higgsfield_api.DEFAULT_MODEL)

print("\n== the prompt engineer ==")
good = json.dumps({"subject": "a red fox", "action": "trots through fresh snow",
                   "setting": "birch forest at dawn", "camera": "slow dolly-in, 35mm",
                   "lighting": "low golden sun from camera left",
                   "style": "Kodak 250D, muted, quiet", "negative": "text, flicker"})
spec = videogen.Spec(prompt="a fox in snow", motion="low", negative="blur")
out = videogen.enhance(spec, lambda s, u: "```json\n" + good + "\n```")
check("a valid brief becomes a composed prompt", out["enhanced"], True)
check("with the camera path in it", "Camera: slow dolly-in, 35mm" in out["prompt"], True)
check("and the motion intensity in words", "minimal motion" in out["prompt"], True)
check("negatives are merged, the person's first", out["negative"].startswith("blur, "), True)
out2 = videogen.enhance(spec, lambda s, u: "```json\n" + good + "\n```")
check("the same brief composes the same prompt", out2["prompt"], out["prompt"])
check("prose instead of JSON -> the person's own words",
      videogen.enhance(spec, lambda s, u: "Sure! Here is a fox.")["prompt"], "a fox in snow")
check("a non-string field -> the person's own words",
      videogen.enhance(spec, lambda s, u: json.dumps({"subject": ["fox"]}))["enhanced"], False)
check("no subject -> the person's own words",
      videogen.enhance(spec, lambda s, u: json.dumps({"camera": "pan"}))["enhanced"], False)
check("a model that raises -> the person's own words",
      videogen.enhance(spec, lambda s, u: 1 / 0)["enhanced"], False)
check("no model at all -> the person's own words",
      videogen.enhance(spec, None)["enhanced"], False)
check("a long, careful prompt is left alone",
      videogen.enhance(videogen.Spec(prompt="x" * 500), lambda s, u: good)["enhanced"], False)

# ----------------------------------------------------------- the routes
uid = appmod._create_user("video@check.example", password_hash="x")
appmod._apply_plan(appmod.USERS[uid], "pro")
c = appmod.app.test_client()
with c.session_transaction() as s:
    s["user_id"] = uid
appmod._video_turn = lambda system, user: good

print("\n== status describes the live backend and its models ==")
st = c.get("/api/video/status").get_json()
check("configured", st["configured"], True)
check("Higgsfield is the backend", st["backend"], "higgsfield")
hf = st["backends"][0]
check("with every model listed", sorted(m["id"] for m in hf["models"]),
      sorted(higgsfield_api.MODELS))
check("and image-to-video offered", hf["image_to_video"], True)
check("the quota is the plan's", st["quota"]["limit"], 10)

print("\n== a generation: submit, poll, download, validate ==")
guest = appmod.app.test_client()
r = guest.post("/api/video/generate", json={"prompt": "a fox"})
check("a guest is asked to sign in", r.status_code, 401)
r = c.post("/api/video/generate", json={"prompt": ""})
check("an empty prompt is a 400", r.status_code, 400)
r = c.post("/api/video/generate", json={"prompt": "a fox", "model": "no-such"})
check("an unknown model is a 400", r.status_code, 400)
r = c.post("/api/video/generate", json={"prompt": "a fox in snow", "seconds": 7,
                                        "ratio": "9:16", "seed": 1234, "motion": "low",
                                        "negative": "blur"})
check("a good request is accepted", r.status_code, 200)
job = r.get_json()["job"]
check("the job is running", job["status"], "running")
check("the quota counted it", r.get_json()["quota"]["used"], 1)
sub = last_submit()
check("the provider saw the Key header", sub["headers"].get("Authorization"), "Key test-id:test-secret")
check("the exact Kling body, nothing extra", sorted(sub["body"]),
      ["cfg_scale", "duration", "negative_prompt", "prompt"])
check("7s snapped to 5 for Kling", sub["body"]["duration"], 5)
check("the engineered prompt went, not the raw one", "Camera:" in sub["body"]["prompt"], True)
check("with the negatives merged", sub["body"]["negative_prompt"].startswith("blur, "), True)
check("a webhook URL was attached, with this server's token",
      sub["query"].get("hf_webhook", [""])[0],
      "https://example.test/api/video/webhook/" + appmod._video_webhook_token())
check("owner id is not in the public view", "owner_id" in job, False)
check("Kling takes no seed, so none is recorded", job["seed"], None)
check("nor a ratio it cannot honour", job["params"]["ratio"], "")
check("the log has started", len(job["log"]) >= 2, True)
row = drive(job["id"])
check("three polls later it is done", row["status"], "done")
check("the file is on this server",
      os.path.exists(os.path.join(videogen.OUTPUT_DIR, job["id"] + ".mp4")), True)
check("bytes recorded", (row["bytes"] or 0) > 20000, True)
if HAVE_FFPROBE:
    check("ffprobe read the resolution", (row["width"], row["height"]), (1280, 720))
    check("and the duration", abs(row["duration"] - 5) < 1.5, True)
view = c.get("/api/video/job/%s" % job["id"]).get_json()["job"]
check("the public URL is the local copy", view["url"], row["local_path"])
check("the provider's URL is not what the browser plays",
      view["url"].startswith("http://127.0.0.1"), False)
check("the log ends with Done", view["log"][-1][1].startswith("Done:"), True)
lst = c.get("/api/video/jobs").get_json()
check("history lists it", [j["id"] for j in lst["jobs"]], [job["id"]])
r = c.get("/api/video/job/%s/download" % job["id"])
check("download is an attachment", "attachment" in r.headers.get("Content-Disposition", ""), True)
check("named after the job", "randomgenerals-%s.mp4" % job["id"] in r.headers.get("Content-Disposition", ""), True)
check("with the bytes", len(r.data), row["bytes"])
r = guest.get("/api/video/job/%s" % job["id"])
check("someone else cannot see it", r.status_code, 404)

print("\n== a seed is kept when the model takes one ==")
r = c.post("/api/video/generate", json={"prompt": "a fox", "model": "wan-2.5", "seed": 1234})
check("Wan accepted", r.status_code, 200)
check("the seed is recorded", r.get_json()["job"]["seed"], 1234)
check("and sent", last_submit()["body"]["seed"], 1234)
r2 = c.post("/api/video/generate", json={"prompt": "a fox", "model": "wan-2.5"})
check("with none given, one is chosen and recorded", isinstance(r2.get_json()["job"]["seed"], int), True)
check("and that is what was sent", last_submit()["body"]["seed"], r2.get_json()["job"]["seed"])
drive(r.get_json()["job"]["id"])
drive(r2.get_json()["job"]["id"])

print("\n== the wrong request never lands on the 9:16 job ==")
r = c.post("/api/video/generate", json={"prompt": "a fox", "model": "seedance-lite",
                                        "ratio": "9:16", "seconds": 4})
check("Seedance 9:16 accepted", r.status_code, 200)
sub = last_submit()
check("the body says 9:16 and 4s", (sub["body"]["aspect_ratio"], sub["body"]["duration"]), ("9:16", 4))
job2 = r.get_json()["job"]
if HAVE_FFPROBE:
    # The fake served a 16:9 file for a 9:16 request: the engine must
    # notice rather than hand a landscape clip to a portrait request.
    row = drive(job2["id"])
    check("a landscape file for a portrait request is a failure", row["status"], "failed")
    check("that says what came back", "asked for 9:16" in row["error"], True)
    check("and the quota is refunded", db.video_used(uid, row["params"]["period"]), 3)
    # The right shape passes.
    Fake.script["media"] = media(720, 1280, 4)
    r = c.post("/api/video/generate", json={"prompt": "a fox", "model": "seedance-lite",
                                            "ratio": "9:16", "seconds": 4})
    row = drive(r.get_json()["job"]["id"])
    check("a portrait file for a portrait request is done", row["status"], "done")
    check("at its real size", (row["width"], row["height"]), (720, 1280))
else:
    drive(job2["id"])

print("\n== the provider's bad days ==")
reset_quota()
used_before = db.video_used(uid, appmod._video_period_key("pro"))
Fake.script["submit_503"] = 2
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("a 503 on submit is retried inside the client", r.status_code, 200)
posts = [q for q in Fake.requests if q["method"] == "POST" and q["path"] in CONTRACT]
check("three attempts were made", len(posts) >= 3, True)
drive(r.get_json()["job"]["id"])

Fake.script["submit_503"] = 10
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("a provider that stays down leaves the job queued", r.get_json()["job"]["status"], "queued")
qid = r.get_json()["job"]["id"]
check("and the log says why", "at its limit" in db.video_job_get(qid)["log"][-1][1], True)
Fake.script["submit_503"] = 0
row = drive(qid)
check("the poller resubmits and it completes", row["status"], "done")

Fake.script["concurrency"] = 1
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("the concurrency 400 is a queue, not a failure", r.get_json()["job"]["status"], "queued")
row = drive(r.get_json()["job"]["id"])
check("and it goes through once there is room", row["status"], "done")

Fake.script["concurrency"] = 99
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"], max_rounds=10)
check("a provider that never has room fails the job in the end", row["status"], "failed")
check("saying so", "busy" in (row["error"] or ""), True)
Fake.script["concurrency"] = 0

Fake.script["submit_429"] = 1
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("a 429 is retried with backoff", r.get_json()["job"]["status"], "running")
drive(r.get_json()["job"]["id"])

Fake.script["reject_input"] = True
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("a moderation 400 is a 502 with the reason", r.status_code, 502)
check("naming it", "moderation" in r.get_json()["error"], True)

Fake.script["status_5xx"] = 2
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("5xx while polling is retried, not failed", row["status"], "done")

Fake.script["outcome"] = "nsfw"
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("an NSFW verdict fails the job", row["status"], "failed")
check("in plain words", "content grounds" in row["error"], True)

Fake.script["outcome"] = "failed"
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("a provider failure fails the job", row["status"], "failed")

Fake.script["media"] = b"<html><body>Not Found</body></html>" + b" " * 30_000
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("an HTML page where a clip should be is a failure", row["status"], "failed")
check("that says what it was", "not an MP4" in row["error"], True)

Fake.script["media"] = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("a stub file is a failure", row["status"], "failed")
check("naming its size", "bytes" in row["error"], True)

Fake.script["media_503_once"] = True
r = c.post("/api/video/generate", json={"prompt": "a fox"})
row = drive(r.get_json()["job"]["id"])
check("storage answering 503 once is retried", row["status"], "done")

used_after = db.video_used(uid, appmod._video_period_key("pro"))
done_since = 6
check("only completed clips count against the quota (every failure refunded)",
      used_after - used_before, done_since)

print("\n== timeouts and restarts ==")
reset_quota()
r = c.post("/api/video/generate", json={"prompt": "a fox"})
old = r.get_json()["job"]["id"]
db.video_job_update(old, created="2020-01-01T00:00:00+00:00")
row = drive(old)
check("a job older than the timeout is given up", row["status"], "failed")
check("saying so", "Gave up" in row["error"], True)

r = c.post("/api/video/generate", json={"prompt": "a fox"})
survivor = r.get_json()["job"]["id"]
videogen._reset_for_tests()                     # "restart": registry gone
videogen.register("higgsfield", higgsfield_api)
videogen._poller_started = True
videogen.start_poller(on_failed=appmod._video_refund_failed, webhook_url=appmod._video_webhook_url)
row = drive(survivor)
check("a job survives a restart and still completes", row["status"], "done")

print("\n== cancel and delete ==")
reset_quota()
r = c.post("/api/video/generate", json={"prompt": "a fox"})
cid = r.get_json()["job"]["id"]
used = db.video_used(uid, appmod._video_period_key("pro"))
r = c.delete("/api/video/job/%s" % cid)
check("deleting a running job answers ok", r.status_code, 200)
cancels = [q for q in Fake.requests if q["method"] == "POST" and q["path"].endswith("/cancel")]
check("the provider was asked to cancel", len(cancels), 1)
check("the row is gone", db.video_job_get(cid), None)
check("and the quota refunded, since it never started", db.video_used(uid, appmod._video_period_key("pro")), used - 1)
r = c.delete("/api/video/job/%s" % job["id"])
check("deleting a finished job removes its file",
      os.path.exists(os.path.join(videogen.OUTPUT_DIR, job["id"] + ".mp4")), False)
r = guest.delete("/api/video/job/%s" % survivor)
check("someone else cannot delete it", r.status_code, 404)

print("\n== the webhook is a nudge, not a verdict ==")
reset_quota()
r = c.post("/api/video/generate", json={"prompt": "a fox"})
wid = r.get_json()["job"]["id"]
ref = db.video_job_get(wid)["provider_ref"]
db.video_job_update(wid, next_poll="2999-01-01T00:00:00+00:00")
check("with next_poll far off, a poll does nothing", videogen.poll_once(), 0)
r = appmod.app.test_client().post("/api/video/webhook/wrong-token",
                                  json={"request_id": ref, "status": "completed"})
check("a wrong token is ignored, quietly", (r.status_code, r.get_json()["ok"]), (200, False))
r = appmod.app.test_client().post("/api/video/webhook/" + appmod._video_webhook_token(),
                                  json={"request_id": ref, "status": "completed",
                                        "payload": {"video": {"url": "http://evil/x.mp4"}}})
check("the right token is acknowledged", (r.status_code, r.get_json()["known"]), (200, True))
check("and the job is due now", db.video_job_get(wid)["next_poll"] <= videogen._now(), True)
row = drive(wid)
check("the result came from the provider's status, not the webhook body",
      row["url"].startswith("http://127.0.0.1"), True)
check("and is done", row["status"], "done")
r = appmod.app.test_client().post("/api/video/webhook/" + appmod._video_webhook_token(),
                                  json={"request_id": "unknown"})
check("an unknown id is still a 200 (a 4xx would stop their retries)", r.status_code, 200)

print("\n== image to video ==")
reset_quota()
png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
r = c.post("/api/video/generate", json={"prompt": "the fox looks up", "model": "dop",
                                        "image": "data:image/png;base64," + png})
check("an image request is accepted", r.status_code, 200)
uploads = [q for q in Fake.requests if q["method"] == "PUT"]
check("the image was PUT to the presigned URL", len(uploads), 1)
check("without our credentials", "Authorization" in uploads[0]["headers"], False)
check("with the storage headers they asked for", uploads[0]["headers"].get("x-amz-tagging"), "retention=temporary")
sub = last_submit()
check("DoP received the public URL", "/public/" in sub["body"]["image_url"], True)
check("and not a data URL", sub["body"]["image_url"].startswith("data:"), False)
drive(r.get_json()["job"]["id"])
r = c.post("/api/video/generate", json={"prompt": "x", "model": "dop"})
check("DoP without an image is refused up front", r.status_code, 400)
r = c.post("/api/video/generate", json={"prompt": "x", "model": "dop", "image": "not a data url"})
check("a non-data-URL image is a 400", r.status_code, 400)

print("\n== the enhance preview ==")
r = c.post("/api/video/enhance", json={"prompt": "a fox in snow", "motion": "high"})
check("the preview answers", r.status_code, 200)
check("with the engineered prompt", "Camera:" in r.get_json()["prompt"], True)
check("and the brief itself", r.get_json()["brief"]["camera"], "slow dolly-in, 35mm")
r = c.post("/api/video/enhance", json={"prompt": ""})
check("an empty prompt is a 400", r.status_code, 400)

print("\n== quota ==")
period = appmod._video_period_key("pro")
reset_quota()
for _ in range(20):
    db.video_try_consume(uid, period, 10)
r = c.post("/api/video/generate", json={"prompt": "a fox"})
check("past the quota is a 402", r.status_code, 402)
check("that says so", "used all 10" in r.get_json()["error"], True)

shutil.rmtree(WORK, ignore_errors=True)
print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
