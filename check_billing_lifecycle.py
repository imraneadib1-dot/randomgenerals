# -*- coding: utf-8 -*-
"""Does a Paddle subscription survive contact with this app?

    python check_billing_lifecycle.py

Sends REAL, SIGNED webhook payloads through the real route - the
signature helper below does exactly what Paddle does, with the test
secret - and checks what the account looks like afterwards, including
after the kind of restart that reloads users from the database.

WHY THESE CHECKS

check_webhook_security.py proves a forged webhook is refused. Nothing
proved a genuine one did the right thing, and two of them did not:

  - the customer and subscription ids Paddle sends were written to the
    in-memory user dict and to nowhere else, so every restart forgot
    who was a customer - invoices came back empty and "manage plan"
    had nothing to manage
  - events were applied in the order they arrived, and Paddle does not
    promise an order: a delayed or redelivered "active" after a
    "canceled" re-granted Pro, with a full credit refill

Nothing here touches Paddle. Every request is local.
"""
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time

WORK = tempfile.mkdtemp(prefix="billingtest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["PADDLE_ENV"] = "production"
os.environ["PADDLE_API_KEY"] = "pdl_live_" + "x" * 40
os.environ["PADDLE_PRICE_ID_PRO"] = "pri_" + "x" * 20
os.environ["PADDLE_WEBHOOK_SECRET"] = "ntfset_" + "s" * 20
os.environ["PADDLE_CLIENT_TOKEN"] = "live_" + "x" * 20

sys.path.insert(0, os.path.abspath("."))

import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402
import paddle_billing as pb                               # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


# The IP allowlist is defence in depth behind the signature and needs
# the network to fetch; the signature is what this exercises.
pb.ip_allowed = lambda ip: (True, "")
client = appmod.app.test_client()


def sign(body: bytes, ts: int | None = None) -> str:
    """Exactly what Paddle puts in Paddle-Signature: ts=<unix>;h1=<hmac>
    over the bytes "<ts>:<body>"."""
    ts = ts or int(time.time())
    mac = hmac.new(os.environ["PADDLE_WEBHOOK_SECRET"].encode("utf-8"),
                   str(ts).encode("ascii") + b":" + body,
                   hashlib.sha256).hexdigest()
    return "ts=%d;h1=%s" % (ts, mac)


def deliver(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    return client.post("/api/billing/paddle/webhook", data=body,
                       headers={"Content-Type": "application/json",
                                "Paddle-Signature": sign(body)})


def subscription_event(event_type, uid, status, occurred_at,
                       sub_id="sub_01", customer_id="ctm_01",
                       cancel=False, period_end="2026-10-12T00:00:00Z"):
    data = {
        "id": sub_id, "customer_id": customer_id, "status": status,
        "custom_data": {"user_id": uid},
        "current_billing_period": {"ends_at": period_end},
    }
    if cancel:
        data["scheduled_change"] = {"action": "cancel"}
    return {"event_type": event_type, "occurred_at": occurred_at,
            "data": data}


def user():
    return appmod.USERS[uid]


def reloaded():
    """The account as a fresh process would see it."""
    return db.load_users()[uid]


uid = appmod._create_user("payer@example.com", password_hash="x")

print("== the route only listens to Paddle ==")
body = json.dumps(subscription_event(
    "subscription.activated", uid, "active", "2026-09-01T10:00:00Z")).encode()
r = client.post("/api/billing/paddle/webhook", data=body,
                headers={"Content-Type": "application/json",
                         "Paddle-Signature": "ts=1;h1=bogus"})
check("a bad signature is refused", r.status_code, 400)
check("and nothing changed", user()["plan"], "free")

print("\n== a subscription starts ==")
r = deliver(subscription_event(
    "subscription.activated", uid, "active", "2026-09-01T10:00:00Z"))
check("accepted", r.status_code, 200)
check("the account is Pro", user()["plan"], "pro")
check("with Pro's credits", user()["credits"]["balance"],
      appmod.PLANS["pro"]["cap"])
u = reloaded()
check("the Paddle subscription id survives a reload",
      u["paddle_subscription_id"], "sub_01")
check("and the customer id", u["paddle_customer_id"], "ctm_01")
check("and the status", u["subscription_status"], "active")
check("and the renewal date", u["current_period_end"], "2026-10-12T00:00:00Z")

print("\n== the subscription is a licence for the desktop build ==")
r = client.post("/api/license/verify", json={"key": "sub_01"})
check("a Paddle subscription id verifies", r.get_json().get("valid"), True)
check("as Pro", r.get_json().get("tier"), "pro")

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
