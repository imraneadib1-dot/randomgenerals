"""Paddle Billing - subscriptions for countries Stripe doesn't reach.

WHY THIS EXISTS ALONGSIDE STRIPE
Stripe supports 56 countries and Morocco is not one of them - not as a
standard account, not in preview, and not through the Paystack
partnership that covers Ghana, Kenya, Nigeria and South Africa. A
Moroccan business therefore cannot activate a Stripe account at all, no
matter how complete the integration is. Paddle pays out worldwide except
to sanctioned countries (Russia, Belarus, Iran, North Korea), so it can.

Paddle is a merchant of record, which is a different arrangement from
Stripe rather than just a different API. Paddle is legally the seller:
they take the customer's money, charge and remit VAT/sales tax in every
jurisdiction themselves, and pay out on a schedule. That is why they cost
more per transaction, and why the tax side stops being your problem.

The Stripe code is deliberately left in place. If this ever runs from a
supported country, Stripe is cheaper, and the app picks whichever is
configured (see app.py).

CONFIGURATION
    PADDLE_API_KEY          server-side key from Paddle > Developer tools
    PADDLE_PRICE_ID_PRO     the price to subscribe to, "pri_..."
    PADDLE_WEBHOOK_SECRET   from the notification destination you create
    PADDLE_ENV              "sandbox" (default) or "production"

Sandbox and production are entirely separate systems with separate keys,
prices and dashboards, so a sandbox price id is meaningless in
production and vice versa.
"""
import hashlib
import hmac
import os
import time

import requests

API_KEY = os.environ.get("PADDLE_API_KEY", "")
PRICE_ID_PRO = os.environ.get("PADDLE_PRICE_ID_PRO", "")
WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET", "")
ENV = os.environ.get("PADDLE_ENV", "sandbox").strip().lower()

# Two completely separate hosts. Pointing production keys at the sandbox
# host fails as an auth error rather than anything that explains itself,
# so this is worth getting right up front.
API_BASE = ("https://api.paddle.com" if ENV == "production"
            else "https://sandbox-api.paddle.com")

# Signatures are rejected if the timestamp is too old, so a captured
# request cannot be replayed later. Paddle's own tolerance is 5 seconds
# for their maximum retry window; 5 minutes is lenient enough to survive
# ordinary clock skew without being a meaningful replay window.
MAX_SIGNATURE_AGE_SECONDS = 300


def _is_placeholder(value):
    """Catch keys that were pasted from documentation rather than a real
    dashboard - truncated examples, or the literal placeholder text.
    A truncated key is worse than an absent one: absent is detected and
    reported, truncated fails at the moment a customer tries to pay."""
    if not value:
        return True
    v = value.strip()
    if "..." in v or v.lower().startswith(("your_", "paste", "<", "xxx")):
        return True
    return False


def configured():
    """Can checkouts be created? Webhooks are checked separately, since
    checkout works without them - it just means nobody is ever upgraded,
    which is the failure that looks like the payment vanished."""
    return not (_is_placeholder(API_KEY) or _is_placeholder(PRICE_ID_PRO))


def webhook_ready():
    return configured() and not _is_placeholder(WEBHOOK_SECRET)


def config_problem():
    """A specific sentence about what is missing. 'Not configured' sends
    whoever is debugging to read the source; naming the variable does
    not."""
    if _is_placeholder(API_KEY):
        return ("PADDLE_API_KEY is missing or a placeholder. Create one in "
                "Paddle > Developer tools > Authentication.")
    if _is_placeholder(PRICE_ID_PRO):
        return ("PADDLE_PRICE_ID_PRO is missing or a placeholder. It is the "
                "price id from your Pro product, and starts with 'pri_'.")
    if _is_placeholder(WEBHOOK_SECRET):
        return ("PADDLE_WEBHOOK_SECRET is missing, so payments would "
                "succeed without anyone being upgraded. Create a "
                "notification destination in Paddle > Developer tools.")
    return ""


def _headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        # Pinning the API version means Paddle changing their default
        # cannot silently change the response shape this code parses.
        "Paddle-Version": "1",
    }


def create_checkout(user_id, email, return_url):
    """Start a subscription. -> (checkout_url, error); exactly one is set.

    Creates a transaction and hands back its hosted checkout link, which
    is the same shape as Stripe Checkout: the customer pays on Paddle's
    page, so no card details ever reach this server.

    user_id travels in custom_data and comes back on every webhook for
    this subscription. That is the only link between a Paddle customer
    and a local account - matching on email instead would break the
    moment someone pays with a different address than they signed up
    with, which is common.
    """
    if not configured():
        return None, config_problem()

    body = {
        "items": [{"price_id": PRICE_ID_PRO, "quantity": 1}],
        "custom_data": {"user_id": str(user_id)},
        "checkout": {"url": return_url},
    }
    if email:
        body["customer"] = {"email": email}

    try:
        r = requests.post(f"{API_BASE}/transactions", headers=_headers(),
                          json=body, timeout=20)
    except requests.exceptions.RequestException as e:
        return None, f"Could not reach Paddle: {e}"

    if r.status_code >= 400:
        detail = ""
        try:
            err = r.json().get("error", {})
            detail = err.get("detail") or err.get("code") or ""
        except ValueError:
            detail = r.text[:200]
        return None, f"Paddle rejected the checkout ({r.status_code}): {detail}"

    try:
        data = r.json()["data"]
    except (ValueError, KeyError):
        return None, "Unexpected response from Paddle when creating checkout."

    url = (data.get("checkout") or {}).get("url")
    if not url:
        # Almost always means no default payment link is set on the
        # account, which is a dashboard setting rather than a code bug -
        # so say that instead of "unexpected response".
        return None, ("Paddle created the transaction but returned no "
                      "checkout URL. Set a default payment link in "
                      "Paddle > Checkout > Checkout settings.")
    return url, None


def verify_webhook(raw_body, signature_header):
    """Is this really from Paddle? -> (ok, reason).

    Without this, anyone who finds the webhook URL can POST a fabricated
    'subscription created' event and grant themselves Pro. It is the only
    thing standing between a public URL and free accounts, so it fails
    closed on every unexpected input.

    Paddle sends: Paddle-Signature: ts=<unix>;h1=<hex hmac>
    where the HMAC is over the exact bytes "<ts>:<raw body>".
    """
    if _is_placeholder(WEBHOOK_SECRET):
        return False, "PADDLE_WEBHOOK_SECRET is not set"
    if not signature_header:
        return False, "no Paddle-Signature header"

    parts = {}
    for chunk in signature_header.split(";"):
        key, _, value = chunk.partition("=")
        if key and value:
            parts[key.strip()] = value.strip()

    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1:
        return False, "malformed Paddle-Signature header"

    try:
        age = abs(time.time() - int(ts))
    except ValueError:
        return False, "non-numeric timestamp in signature"
    if age > MAX_SIGNATURE_AGE_SECONDS:
        return False, f"signature is {int(age)}s old - possible replay"

    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    signed = ts.encode("ascii") + b":" + raw_body
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed,
                        hashlib.sha256).hexdigest()

    # compare_digest, not ==, so the comparison takes the same time
    # whatever the input and cannot be probed byte by byte.
    if not hmac.compare_digest(expected, h1):
        return False, "signature mismatch"
    return True, ""


# Paddle's status values, mapped to whether Pro should be active.
# "past_due" stays active on purpose: a renewal that has not settled yet
# is usually a card that needs updating, and cutting someone off the hour
# their payment retries is a worse outcome than a few days of grace.
ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def parse_event(payload):
    """Normalise a webhook into the fields app.py stores.

    -> dict with event_type, user_id, subscription_id, status, active,
       cancel_at_period_end, current_period_end - or None if this event
       is not about a subscription and should simply be acknowledged.
    """
    event_type = payload.get("event_type", "")
    if not event_type.startswith("subscription."):
        return None

    data = payload.get("data") or {}
    custom = data.get("custom_data") or {}
    status = data.get("status", "")

    period = data.get("current_billing_period") or {}

    return {
        "event_type": event_type,
        "user_id": custom.get("user_id"),
        "subscription_id": data.get("id"),
        "customer_id": data.get("customer_id"),
        "status": status,
        "active": status in ACTIVE_STATUSES,
        # scheduled_change of type "cancel" is how Paddle expresses
        # "cancels at the end of the period" - the subscription is still
        # active until then, which is what the UI needs to say.
        "cancel_at_period_end": bool(
            (data.get("scheduled_change") or {}).get("action") == "cancel"),
        "current_period_end": period.get("ends_at"),
    }
