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

print("\n== a cancellation, then a stale 'active' that arrives late ==")
r = deliver(subscription_event(
    "subscription.canceled", uid, "canceled", "2026-09-05T10:00:00Z"))
check("the cancellation is applied", user()["plan"], "free")
check("with free credits", user()["credits"]["balance"],
      appmod.PLANS["free"]["cap"])
balance_after_cancel = user()["credits"]["balance"]
r = deliver(subscription_event(
    "subscription.updated", uid, "active", "2026-09-03T10:00:00Z"))
check("an older event is acknowledged", r.status_code, 200)
check("but not applied", r.get_json().get("applied"), False)
check("the account stays free", user()["plan"], "free")
check("and is not refilled", user()["credits"]["balance"], balance_after_cancel)
check("the applied stamp is the cancellation's",
      reloaded()["subscription_updated_at"], "2026-09-05T10:00:00Z")

print("\n== the same event twice ==")
deliver(subscription_event(
    "subscription.activated", uid, "active", "2026-09-06T10:00:00Z"))
check("re-activated by a newer event", user()["plan"], "pro")
pro_balance = user()["credits"]["balance"]
user()["credits"]["balance"] -= 500          # they used some
r = deliver(subscription_event(
    "subscription.activated", uid, "active", "2026-09-06T10:00:00Z"))
check("a redelivery of the same event is applied harmlessly",
      r.get_json().get("applied"), True)
check("without a second refill", user()["credits"]["balance"],
      pro_balance - 500)

print("\n== an event without custom_data, for a known subscription ==")
ev = subscription_event("subscription.updated", uid, "active",
                        "2026-09-07T10:00:00Z", cancel=True)
del ev["data"]["custom_data"]
r = deliver(ev)
check("matched by subscription id", r.get_json().get("applied"), True)
check("cancel-at-period-end mirrored", user()["cancel_at_period_end"], True)
check("still Pro until then", user()["plan"], "pro")

print("\n== a payment alone upgrades ==")
uid2 = appmod._create_user("payer2@example.com", password_hash="x")
r = deliver({
    "event_type": "transaction.completed",
    "occurred_at": "2026-09-08T10:00:00Z",
    "data": {"id": "txn_09", "status": "completed",
             "customer_id": "ctm_02", "subscription_id": "sub_02",
             "custom_data": {"user_id": uid2},
             "billed_at": "2026-09-08T09:59:00Z",
             "details": {"totals": {"currency_code": "USD",
                                    "grand_total": "199", "fee": "35",
                                    "earnings": "164"}}},
})
u2 = appmod.USERS[uid2]
check("recorded as a payment", r.get_json().get("recorded"), "payment")
check("and the payer is Pro without waiting for a subscription event",
      u2["plan"], "pro")
check("with the subscription id", u2["paddle_subscription_id"], "sub_02")
check("a later subscription event still applies on top",
      deliver(subscription_event(
          "subscription.updated", uid2, "active", "2026-09-08T10:00:05Z",
          sub_id="sub_02", customer_id="ctm_02",
          period_end="2026-10-08T00:00:00Z")).get_json().get("applied"), True)
check("bringing the renewal date", u2["current_period_end"],
      "2026-10-08T00:00:00Z")

print("\n== reconciliation: asking Paddle when the webhook did not come ==")
answers = {}
pb_real_get = pb.get_subscription
pb.get_subscription = lambda sub_id: answers.get(sub_id, (None, "not found"))
answers["sub_02"] = (pb.subscription_state({
    "id": "sub_02", "customer_id": "ctm_02", "status": "canceled",
    "updated_at": "2026-09-20T00:00:00Z"}), None)
result = appmod.reconcile_subscriptions()
check("every known subscription is asked about", result["checked"] >= 2, True)
check("a cancellation the webhook never delivered is applied",
      u2["plan"], "free")
check("counted as a change", result["changed"], 1)
answers["sub_02"] = (pb.subscription_state({
    "id": "sub_02", "customer_id": "ctm_02", "status": "active",
    "updated_at": "2026-09-01T00:00:00Z"}), None)
appmod.reconcile_subscriptions()
check("but an OLDER state from the API cannot undo it", u2["plan"], "free")
answers["sub_01"] = (None, "Could not reach Paddle: timeout")
result = appmod.reconcile_subscriptions()
check("an unreachable Paddle is reported, not fatal",
      any("sub_01" in e for e in result["errors"]), True)
pb.get_subscription = pb_real_get

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
