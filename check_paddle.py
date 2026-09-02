# -*- coding: utf-8 -*-
"""Is Paddle actually able to take real money?

    python check_paddle.py

Reads .env the way the app does, says which environment each credential
belongs to, catches the mismatches that fail confusingly, and asks
Paddle whether the price is real.

WHY THIS EXISTS

Sandbox and production are two entirely separate systems: separate
dashboards, separate keys, separate products, separate price ids.
Nothing stops you setting PADDLE_ENV=production while every credential
is still a sandbox one. The result is an auth error at the moment a
customer tries to pay, which is the worst possible time to discover it
and the hardest place to see it.

Secrets are never printed - only their prefix, which is what says which
environment they came from.
"""
import io
import os
import sys

import requests


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def env_of_key(value):
    """Which Paddle system a credential belongs to, from its prefix."""
    v = (value or "").strip()
    if not v:
        return "missing"
    if v.startswith("pdl_sdbx") or v.startswith("test_"):
        return "sandbox"
    if v.startswith("pdl_live") or v.startswith("live_"):
        return "production"
    return "unknown"


def main():
    load_env()
    declared = os.environ.get("PADDLE_ENV", "sandbox").strip().lower()
    api = os.environ.get("PADDLE_API_KEY", "").strip()
    price = os.environ.get("PADDLE_PRICE_ID_PRO", "").strip()
    secret = os.environ.get("PADDLE_WEBHOOK_SECRET", "").strip()
    token = os.environ.get("PADDLE_CLIENT_TOKEN", "").strip()

    print("PADDLE_ENV is %r - so this app will talk to %s"
          % (declared,
             "api.paddle.com (REAL MONEY)" if declared == "production"
             else "sandbox-api.paddle.com (fake money)"))
    print("")

    rows = [("PADDLE_API_KEY", api, env_of_key(api)),
            ("PADDLE_CLIENT_TOKEN", token, env_of_key(token)),
            ("PADDLE_WEBHOOK_SECRET", secret, "n/a"),
            ("PADDLE_PRICE_ID_PRO", price, "n/a")]
    problems = []
    for name, value, belongs in rows:
        if not value:
            print("  %-22s MISSING" % name)
            problems.append("%s is not set" % name)
            continue
        shown = value[:9] + "..."
        extra = "" if belongs == "n/a" else "  [%s credential]" % belongs
        print("  %-22s %s (%d chars)%s" % (name, shown, len(value), extra))
        if belongs not in ("n/a", "unknown") and belongs != declared:
            problems.append(
                "%s is a %s credential but PADDLE_ENV says %s"
                % (name, belongs, declared))

    print("")
    if problems:
        print("PROBLEMS")
        for p in problems:
            print("  - %s" % p)
        print("")

    if not api or not price:
        print("Cannot check the price without both a key and a price id.")
        return 1

    base = ("https://api.paddle.com" if declared == "production"
            else "https://sandbox-api.paddle.com")
    print("Asking %s about the price..." % base)
    try:
        r = requests.get(base + "/prices/" + price, timeout=25,
                         headers={"Authorization": "Bearer " + api})
    except requests.exceptions.RequestException as e:
        print("  could not reach Paddle: %s" % e)
        return 1

    if r.status_code in (401, 403):
        print("  Paddle rejected the key (%d)." % r.status_code)
        print("  Usually this is a key from the OTHER environment.")
        return 1
    if r.status_code == 404:
        print("  No such price in this environment.")
        print("  Price ids are NOT shared between sandbox and production -")
        print("  a live account needs its own product and its own price id.")
        return 1
    if r.status_code != 200:
        print("  Paddle answered %d: %s" % (r.status_code, r.text[:200]))
        return 1

    data = r.json().get("data") or {}
    unit = data.get("unit_price") or {}
    cycle = data.get("billing_cycle") or {}
    amount = unit.get("amount")
    currency = unit.get("currency_code")
    print("  name     : %s" % (data.get("description") or "(unnamed)"))
    print("  price    : %s %s" % (amount, currency))
    print("  billing  : every %s %s" % (cycle.get("frequency"),
                                        cycle.get("interval")))
    print("  status   : %s" % data.get("status"))
    print("")

    # The site advertises a price of its own. If the two ever disagree,
    # customers are quoted one number and charged another - which is a
    # trust problem before it is a bug.
    try:
        sys.path.insert(0, os.getcwd())
        import app                          # noqa: PLC0415
        advertised = app.PLANS["pro"]["price"]
        print("The site advertises: %s" % advertised)
        digits = "".join(c for c in advertised if c.isdigit())
        if amount is not None and digits and str(amount) != digits:
            print("  MISMATCH - Paddle would charge %s %s. Customers would "
                  "be quoted one price and billed another." % (amount,
                                                               currency))
            problems.append("advertised price does not match Paddle")
        else:
            print("  matches Paddle.")
    except Exception as e:                   # noqa: BLE001
        print("  (could not read PLANS to compare: %s)" % e)

    print("")
    if data.get("status") != "active":
        print("The price is not active, so checkout will fail.")
        return 1
    if problems:
        print("Checkout would work, but fix the problems above first.")
        return 1
    if declared != "production":
        print("Everything checks out - in SANDBOX. No real money can be")
        print("taken until PADDLE_ENV=production and every credential is")
        print("replaced with its production equivalent.")
        return 0
    print("Ready to take real payments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
