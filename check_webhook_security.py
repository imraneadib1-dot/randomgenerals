# -*- coding: utf-8 -*-
"""Is the Paddle webhook actually protected?

    python check_webhook_security.py

Covers the source-IP allowlist and the pwCustomer value Retain needs.
Uses a throwaway database and never touches the live site, but it does
fetch Paddle's real published IP list, so it needs network.

The allowlist is the risky one: get it wrong in the closed direction and
every real payment stops being applied - the customer is charged and
nobody is upgraded. So the fail-open paths are tested as carefully as
the blocking one.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="iptest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["PADDLE_ENV"] = "production"
os.environ["PADDLE_API_KEY"] = "pdl_live_" + "x" * 40
os.environ["PADDLE_PRICE_ID_PRO"] = "pri_" + "x" * 20
os.environ["PADDLE_WEBHOOK_SECRET"] = "ntfset_" + "x" * 20
os.environ["PADDLE_CLIENT_TOKEN"] = "live_" + "x" * 20

sys.path.insert(0, os.path.abspath("."))
import app as appmod                                       # noqa: E402
import paddle_billing as pb                                # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s   (got %r)" % (label, "ok" if ok else "FAIL", got))
    if not ok:
        FAILED.append(label)


print("== the real list, fetched live ==")
cidrs = pb.allowed_ip_cidrs(force=True)
print("  Paddle publishes %s" % cidrs)
check("fetched a non-empty list", bool(cidrs), True)
check("all are /32", all(c.endswith("/32") for c in cidrs or []), True)

print("\n== matching ==")
real = (cidrs or ["34.237.3.244/32"])[0].split("/")[0]
check("a published address is allowed", pb.ip_allowed(real)[0], True)
check("an unpublished address is refused",
      pb.ip_allowed("203.0.113.7")[0], False)
check("the refusal says why",
      "not one of Paddle" in pb.ip_allowed("203.0.113.7")[1], True)
check("loopback is refused too", pb.ip_allowed("127.0.0.1")[0], False)

print("\n== fail-open paths (the dangerous direction) ==")
check("empty address allowed", pb.ip_allowed("")[0], True)
check("garbage address allowed", pb.ip_allowed("not-an-ip")[0], True)
saved = dict(pb._IP_CACHE)
pb._IP_CACHE.update({"cidrs": None, "fetched": 0, "env": None})
_orig = pb.requests.get


def boom(*a, **k):
    raise IOError("Paddle unreachable")


pb.requests.get = boom
allowed, why = pb.ip_allowed("203.0.113.7")
pb.requests.get = _orig
check("list unavailable -> ALLOWED, not blocked", allowed, True)
check("and says so", "unavailable" in why, True)
pb._IP_CACHE.update(saved)

print("\n== the webhook route ==")
client = appmod.app.test_client()
r = client.post("/api/billing/paddle/webhook",
                json={"event_type": "transaction.completed"},
                headers={"CF-Connecting-IP": "203.0.113.7"})
check("bad IP -> 403", r.status_code, 403)
check("and no detail leaked", "Paddle-Signature" in r.get_data(as_text=True),
      False)

r = client.post("/api/billing/paddle/webhook",
                json={"event_type": "transaction.completed"},
                headers={"CF-Connecting-IP": real})
# Right IP, no valid signature: must still be refused. The allowlist is
# a layer in front of the signature, never a replacement for it.
check("good IP but no signature -> still refused", r.status_code, 400)

print("\n== pwCustomer ==")
uid = appmod._create_user("retain@example.com", password_hash="x")
with client.session_transaction() as s:
    s["user_id"] = uid
d = client.get("/api/plans").get_json()
check("no customer id before paying", d["paddle_customer_id"], "")

appmod.USERS[uid]["paddle_customer_id"] = "ctm_01abc"
d = client.get("/api/plans").get_json()
check("customer id served once known", d["paddle_customer_id"], "ctm_01abc")

anon = appmod.app.test_client()
d = anon.get("/api/plans").get_json()
check("signed-out sees no customer id", d["paddle_customer_id"], "")
check("client token still public", d["paddle_client_token"].startswith("live_"),
      True)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
