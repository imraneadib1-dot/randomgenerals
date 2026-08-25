#!/usr/bin/env python3
"""Check the Paddle setup, and say precisely what is wrong if it isn't.

    python scripts/paddle_check.py

Every check here is one that otherwise fails silently or fails at the
worst moment. A wrong price id is invisible until someone clicks
Upgrade. A missing default payment link produces a transaction with no
checkout URL. A production key against the sandbox host is an auth error
that names neither. Finding those now costs a minute; finding them from
a customer costs the customer.

Nothing here writes anything or takes payment - it reads config and asks
Paddle to confirm it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests
except ImportError:
    sys.exit("requests isn't installed. Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[0;32m", "\033[0;31m", "\033[0;33m", "\033[2m", "\033[0m")

problems = []


def ok(msg):
    print(f"  {GREEN}ok{RESET}    {msg}")


def bad(msg, fix=""):
    print(f"  {RED}fail{RESET}  {msg}")
    if fix:
        print(f"        {DIM}{fix}{RESET}")
    problems.append(msg)


def warn(msg):
    print(f"  {YELLOW}!{RESET}     {msg}")


print(f"\n{DIM}Paddle configuration check{RESET}\n")

env = os.environ.get("PADDLE_ENV", "sandbox").strip().lower()
api_key = os.environ.get("PADDLE_API_KEY", "").strip()
price_id = os.environ.get("PADDLE_PRICE_ID_PRO", "").strip()
webhook_secret = os.environ.get("PADDLE_WEBHOOK_SECRET", "").strip()
client_token = os.environ.get("PADDLE_CLIENT_TOKEN", "").strip()

base = ("https://api.paddle.com" if env == "production"
        else "https://sandbox-api.paddle.com")

print("Environment")
if env not in ("sandbox", "production"):
    bad(f"PADDLE_ENV is {env!r}",
        "must be exactly 'sandbox' or 'production'")
else:
    ok(f"PADDLE_ENV={env}  ->  {base}")

# ------------------------------------------------------------------ keys
print("\nCredentials")
if not api_key:
    bad("PADDLE_API_KEY is not set",
        "Paddle dashboard > Developer tools > Authentication > New API key")
elif "..." in api_key:
    bad("PADDLE_API_KEY looks truncated (contains '...')",
        "Copy the whole key - a partial key fails only at checkout")
else:
    # Sandbox keys and live keys are different strings against different
    # hosts; using one with the other is a 403 that explains nothing.
    looks_sandbox = "sdbx" in api_key or api_key.startswith("pdl_sdbx")
    if env == "production" and looks_sandbox:
        bad("PADDLE_ENV=production but the API key looks like a sandbox key",
            "Production keys come from vendors.paddle.com, not "
            "sandbox-vendors.paddle.com")
    elif env == "sandbox" and not looks_sandbox:
        warn("PADDLE_ENV=sandbox but the key doesn't look like a sandbox "
             "key - continuing, the live check below will settle it")
        ok(f"PADDLE_API_KEY set ({len(api_key)} chars)")
    else:
        ok(f"PADDLE_API_KEY set ({len(api_key)} chars)")

if not price_id:
    bad("PADDLE_PRICE_ID_PRO is not set",
        "Catalog > Products > your Pro product > the price's id")
elif not price_id.startswith("pri_"):
    bad(f"PADDLE_PRICE_ID_PRO is {price_id!r}, which is not a price id",
        "Price ids start with 'pri_'. A product id ('pro_') is a common "
        "mix-up and will not work.")
else:
    ok(f"PADDLE_PRICE_ID_PRO={price_id}")

if not webhook_secret:
    bad("PADDLE_WEBHOOK_SECRET is not set",
        "Without it the webhook refuses every request, so payments "
        "succeed and nobody is ever upgraded. Developer tools > "
        "Notifications > your destination > secret key")
else:
    ok(f"PADDLE_WEBHOOK_SECRET set ({len(webhook_secret)} chars)")

if not client_token:
    bad("PADDLE_CLIENT_TOKEN is not set",
        "Paddle's checkout is an overlay drawn by Paddle.js on your own "
        "page, not a page to redirect to. Without this token Paddle.js "
        "never starts and Upgrade appears to do nothing at all. "
        "Developer tools > Authentication > Client-side tokens")
elif not client_token.startswith(("test_", "live_")):
    warn(f"PADDLE_CLIENT_TOKEN starts {client_token[:6]!r} - client-side "
         f"tokens start with test_ or live_. Check you did not paste the "
         f"API key here.")
else:
    expected = "test_" if env == "sandbox" else "live_"
    if not client_token.startswith(expected):
        bad(f"PADDLE_CLIENT_TOKEN starts {client_token[:5]!r} but "
            f"PADDLE_ENV={env} expects {expected!r}",
            "Sandbox and production have separate client tokens")
    else:
        ok(f"PADDLE_CLIENT_TOKEN set ({len(client_token)} chars)")

if problems:
    print(f"\n{RED}Fix the above before continuing.{RESET}\n")
    sys.exit(1)

# ------------------------------------------------------------ live checks
headers = {"Authorization": f"Bearer {api_key}", "Paddle-Version": "1"}


def call(path):
    try:
        return requests.get(f"{base}{path}", headers=headers, timeout=20)
    except requests.exceptions.RequestException as e:
        bad(f"could not reach {base}: {e}")
        return None


print("\nTalking to Paddle")
r = call("/event-types")
if r is None:
    sys.exit(1)
if r.status_code == 403:
    bad("Paddle rejected the API key (403)",
        "Wrong key, or a sandbox key against production (or vice versa)")
    sys.exit(1)
if r.status_code >= 400:
    bad(f"unexpected response {r.status_code}: {r.text[:160]}")
    sys.exit(1)
ok("API key works")

r = call(f"/prices/{price_id}")
if r is not None and r.status_code == 404:
    bad(f"price {price_id} does not exist in this environment",
        "Sandbox and production have separate catalogs - a sandbox price "
        "id is meaningless in production")
elif r is not None and r.status_code < 400:
    data = r.json().get("data", {})
    amount = (data.get("unit_price") or {}).get("amount")
    currency = (data.get("unit_price") or {}).get("currency_code")
    cycle = data.get("billing_cycle") or {}
    period = (f"every {cycle.get('frequency', '')} "
              f"{cycle.get('interval', '')}" if cycle else "one-time")
    if amount is not None:
        # Paddle stores money in the currency's smallest unit, so 199 is
        # $1.99 - printing it raw is how a price ends up 100x wrong.
        ok(f"price found: {int(amount) / 100:.2f} {currency}, {period}")
    else:
        ok(f"price found: {data.get('description', price_id)}")
    if not cycle:
        warn("this price is one-time, not recurring - subscriptions need "
             "a recurring price")
elif r is not None:
    warn(f"could not read the price ({r.status_code})")

# Notification destinations tell us the webhook is actually pointed here.
r = call("/notification-settings")
if r is not None and r.status_code < 400:
    dests = r.json().get("data", [])
    if not dests:
        bad("no notification destination configured",
            "Nothing will call your webhook, so nobody gets upgraded. "
            "Developer tools > Notifications > New destination")
    else:
        for d in dests:
            url = d.get("destination", "")
            active = d.get("active")
            subs = d.get("subscribed_events") or []
            names = {e.get("name") for e in subs}
            state = "active" if active else "INACTIVE"
            ok(f"destination: {url}  ({state}, {len(names)} events)")
            if not active:
                warn("  that destination is switched off")
            if "/api/billing/paddle/webhook" not in url:
                warn("  URL doesn't end in /api/billing/paddle/webhook")
            wanted = {"subscription.created", "subscription.updated",
                      "subscription.canceled"}
            missing = wanted - names
            if missing:
                warn(f"  not subscribed to: {', '.join(sorted(missing))}")

print()
if problems:
    print(f"{RED}{len(problems)} problem(s) above.{RESET}\n")
    sys.exit(1)
print(f"{GREEN}Paddle is configured correctly.{RESET}")
print(f"{DIM}Next: sign in to the app and click Upgrade to test a "
      f"checkout.{RESET}\n")
