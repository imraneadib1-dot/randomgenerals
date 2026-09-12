# -*- coding: utf-8 -*-
"""Can one caller hit a route as fast as they like?

    python check_ratelimit.py

WHY THESE CHECKS

Nothing limited how often one caller could hit the generation routes,
sign-in or sign-up. The only "rate limit" was Groq's per-minute token
budget, shared by everyone, so one script looping /api/chat drained it
for every other visitor - and a six-digit verification code could be
guessed at full speed, with no counter on wrong tries.

The bucket's clock is injectable, so "ten minutes later" is a variable
assignment rather than a wait.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="ratetest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.abspath("."))

import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402
from ratelimit import Limiter                             # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


print("== the bucket itself ==")
clock = {"t": 1000.0}
lim = Limiter(rate=6, per=60, burst=3, clock=lambda: clock["t"])
check("a burst of three goes through",
      [lim.allow("k")[0] for _ in range(3)], [True] * 3)
ok, wait = lim.allow("k")
check("the fourth is refused", ok, False)
check("with a wait of about ten seconds (one token at 6/min)",
      9 <= wait <= 11, True)
clock["t"] += 10
check("ten seconds later one more is allowed", lim.allow("k")[0], True)
check("another key is unaffected", lim.allow("other")[0], True)
clock["t"] += 3600
check("a long idle refills to the burst, not beyond",
      [lim.allow("k")[0] for _ in range(4)], [True, True, True, False])

print("\n== sign-in: per address, and per account ==")
IP = {"CF-Connecting-IP": "203.0.113.9"}
for limiter in (appmod.LIMIT_LOGIN_IP, appmod.LIMIT_LOGIN_EMAIL,
                appmod.LIMIT_SIGNUP_IP, appmod.LIMIT_VERIFY_SEND,
                appmod.LIMIT_CHAT):
    limiter.clock = lambda: clock["t"]
client = appmod.app.test_client()
codes = [client.post("/api/auth/login", headers=IP,
                     json={"email": "x%d@e.com" % i, "password": "nope"}
                     ).status_code for i in range(11)]
check("ten sign-in attempts from one address are answered",
      all(c != 429 for c in codes[:10]), True)
check("the eleventh is refused", codes[10], 429)
r = client.post("/api/auth/login", headers=IP,
                json={"email": "y@e.com", "password": "nope"})
check("with a Retry-After header", "Retry-After" in r.headers, True)
check("and a body that says how long",
      isinstance(r.get_json().get("retry_after"), int), True)
codes = [client.post("/api/auth/login", headers={"CF-Connecting-IP": "198.51.100.%d" % i},
                     json={"email": "victim@e.com", "password": "nope"}
                     ).status_code for i in range(6)]
check("five tries at ONE account from different addresses are answered",
      all(c != 429 for c in codes[:5]), True)
check("the sixth is refused whatever the address", codes[5], 429)
clock["t"] += 700
r = client.post("/api/auth/login", headers=IP,
                json={"email": "z@e.com", "password": "nope"})
check("ten minutes later the address may try again", r.status_code != 429, True)

print("\n== sign-up ==")
codes = [client.post("/api/auth/signup", headers=IP,
                     json={"email": "", "password": ""}).status_code
         for _ in range(6)]
check("five sign-ups an hour per address, the sixth refused", codes[5], 429)

print("\n== verification: one code a minute, five guesses ==")
uid = appmod._create_user("verify@e.com", password_hash="x")
with client.session_transaction() as s:
    s["user_id"] = uid
appmod.mailer.send_verification_code = lambda *a, **k: (True, "")
r1 = client.post("/api/auth/verify/send")
r2 = client.post("/api/auth/verify/send")
check("a code is sent", r1.status_code, 200)
check("a second request inside a minute is refused", r2.status_code, 429)
clock["t"] += 61
check("a minute later it is not", client.post("/api/auth/verify/send").status_code, 200)
codes = [client.post("/api/auth/verify/confirm", json={"code": "000000"})
         for _ in range(5)]
check("wrong guesses are refused", [c.status_code for c in codes], [400] * 5)
check("and the fifth wrong guess destroys the code",
      "Too many" in codes[4].get_json().get("error", ""), True)
check("so the real one no longer works",
      "No code outstanding" in client.post(
          "/api/auth/verify/confirm", json={"code": "123456"}
      ).get_json().get("error", ""), True)
clock["t"] += 61
client.post("/api/auth/verify/send")
check("a fresh code starts with a clean count",
      db.get_verification_code("verify@e.com") is not None
      and client.post("/api/auth/verify/confirm",
                      json={"code": "000000"}).status_code == 400
      and db.get_verification_code("verify@e.com") is not None, True)

print("\n== chat: a burst is fine, a loop is not ==")
appmod.ollama_reachable = lambda: True
appmod._failover_chain = lambda provider, mode: []


def fake(model, history, **kw):
    yield "ok"


appmod.PROVIDER_STREAMERS["ollama"] = fake
guest = appmod.app.test_client()
tid = guest.post("/api/threads", json={"mode": "chat"},
                 headers=IP).get_json()["id"]
codes = [guest.post("/api/chat", headers=IP, json={
    "thread_id": tid, "provider": "ollama", "model": "m", "message": "hi"}
).status_code for _ in range(6)]
check("five quick messages are answered", codes[:5], [200] * 5)
check("the sixth in the same instant is refused", codes[5], 429)
other = appmod.app.test_client()
tid2 = other.post("/api/threads", json={"mode": "chat"},
                  headers={"CF-Connecting-IP": "203.0.113.77"}).get_json()["id"]
check("somebody else is unaffected", other.post(
    "/api/chat", headers={"CF-Connecting-IP": "203.0.113.77"}, json={
        "thread_id": tid2, "provider": "ollama", "model": "m", "message": "hi"}
).status_code, 200)
check("regenerate shares the chat budget", guest.post(
    "/api/threads/%s/regenerate" % tid, headers=IP,
    json={"provider": "ollama", "model": "m"}).status_code, 429)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
