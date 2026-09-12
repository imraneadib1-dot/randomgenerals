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

# Config is read at call time, never captured at import.
#
# Reading it at import binds whatever the environment happened to be at
# the moment this module was first imported, which makes correctness
# depend on load_dotenv() having already run - a dependency that is
# invisible, easy to break by reordering imports, and fails by looking
# exactly like "no keys configured" while the keys sit correctly in
# .env. That bug already happened here once. Functions cost nothing and
# cannot be got wrong.
def _env(name, default=""):
    return os.environ.get(name, default)


def api_key():
    return _env("PADDLE_API_KEY").strip()


def price_id_pro():
    return _env("PADDLE_PRICE_ID_PRO").strip()


def webhook_secret():
    return _env("PADDLE_WEBHOOK_SECRET").strip()


def client_token():
    """The browser-side token, which is a different credential from the
    API key and is *meant* to be public - it ships inside the page.

    Paddle Billing has no hosted checkout page to redirect to. Its
    checkout is an overlay that Paddle.js opens on your own site, and
    Paddle.js cannot start without this token. Without it the flow gets
    as far as a transaction being created and then simply does nothing
    visible, which is exactly how it failed here.

    Paddle > Developer tools > Authentication > Client-side tokens.
    """
    return _env("PADDLE_CLIENT_TOKEN").strip()


def client_ready():
    return configured() and not _is_placeholder(client_token())


# Paddle recognises exactly two environments, spelled exactly these two
# ways. Paddle.js refuses to initialise on anything else, and api_base()
# would quietly pick the wrong host.
VALID_ENVIRONMENTS = ("production", "sandbox")

# The near-misses that are unambiguous. This deliberately does NOT try to
# guess at anything outside the list: mapping an unrecognised value to
# "production" would point real keys at real money on a typo, and that is
# the one mistake that must never be made silently.
_ENV_ALIASES = {"prod": "production", "pro": "production",
                "live": "production", "production": "production",
                "sandbox": "sandbox", "sbx": "sandbox", "test": "sandbox",
                "dev": "sandbox", "development": "sandbox"}

_env_warned = set()


def environment():
    """Which Paddle system to talk to. Always one of VALID_ENVIRONMENTS.

    PADDLE_ENV=pro was set on the live server for a while, and everything
    downstream took the else-branch: real production keys were sent to
    sandbox-api.paddle.com, and Paddle.js was handed "pro" as its
    environment. Nothing said so. Checkout simply failed, and the browser
    reported it as the server being unreachable.

    So an unrecognised value is no longer read as "not production". It is
    normalised where the intent is obvious, and complained about loudly
    where it is not.
    """
    # An unset variable and a variable set to "" mean the same thing -
    # nobody chose - so neither is worth complaining about.
    raw = (_env("PADDLE_ENV", "sandbox").strip().lower() or "sandbox")
    if raw in _ENV_ALIASES:
        return _ENV_ALIASES[raw]
    # Unknown. Fall back to sandbox - the safe direction, since it cannot
    # charge anyone - but say so once per distinct value rather than
    # letting it pass as a normal configuration.
    if raw not in _env_warned:
        _env_warned.add(raw)
        print("[paddle] PADDLE_ENV=%r is not a Paddle environment. "
              "Expected 'production' or 'sandbox'. Falling back to "
              "sandbox, so no real payment can be taken." % raw)
    return "sandbox"


def environment_problem():
    """-> a sentence if PADDLE_ENV is not spelled the way Paddle wants.

    Reported even when environment() managed to normalise it, because the
    variable should be corrected at the source rather than relied on to
    be guessed correctly.
    """
    # An unset variable and a variable set to "" mean the same thing -
    # nobody chose - so neither is worth complaining about.
    raw = (_env("PADDLE_ENV", "sandbox").strip().lower() or "sandbox")
    if raw in VALID_ENVIRONMENTS:
        return ""
    if raw in _ENV_ALIASES:
        return ("PADDLE_ENV is %r, which is being read as %r. Set it to "
                "exactly %r." % (raw, _ENV_ALIASES[raw], _ENV_ALIASES[raw]))
    return ("PADDLE_ENV is %r, which is not a Paddle environment. Set it "
            "to 'production' to take real payments, or 'sandbox' to test. "
            "Until then no real payment can be taken." % raw)


def api_base():
    """Sandbox and production are two entirely separate systems, with
    separate keys, prices and dashboards. Pointing one's keys at the
    other's host is an auth error that names neither."""
    return ("https://api.paddle.com" if environment() == "production"
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
    return not (_is_placeholder(api_key()) or _is_placeholder(price_id_pro()))


def webhook_ready():
    return configured() and not _is_placeholder(webhook_secret())


def config_problem():
    """A specific sentence about what is missing. 'Not configured' sends
    whoever is debugging to read the source; naming the variable does
    not."""
    if _is_placeholder(api_key()):
        return ("PADDLE_API_KEY is missing or a placeholder. Create one in "
                "Paddle > Developer tools > Authentication.")
    if _is_placeholder(price_id_pro()):
        return ("PADDLE_PRICE_ID_PRO is missing or a placeholder. It is the "
                "price id from your Pro product, and starts with 'pri_'.")
    if _is_placeholder(webhook_secret()):
        return ("PADDLE_WEBHOOK_SECRET is missing, so payments would "
                "succeed without anyone being upgraded. Create a "
                "notification destination in Paddle > Developer tools.")
    if _is_placeholder(client_token()):
        return ("PADDLE_CLIENT_TOKEN is missing. Paddle's checkout is an "
                "overlay opened by Paddle.js in the browser, not a page to "
                "redirect to, and Paddle.js cannot start without this "
                "token - so Upgrade appears to do nothing. Paddle > "
                "Developer tools > Authentication > Client-side tokens.")
    # Last, because a misspelled environment with every credential present
    # is the subtlest of these: nothing is missing, so every other check
    # passes, and the only symptom is that payment fails.
    return environment_problem()


def _headers():
    return {
        "Authorization": f"Bearer {api_key()}",
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
        "items": [{"price_id": price_id_pro(), "quantity": 1}],
        "custom_data": {"user_id": str(user_id)},
        "checkout": {"url": return_url},
    }
    if email:
        body["customer"] = {"email": email}

    try:
        r = requests.post(f"{api_base()}/transactions", headers=_headers(),
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
    if _is_placeholder(webhook_secret()):
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
    expected = hmac.new(webhook_secret().encode("utf-8"), signed,
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


# --------------------------------------------------------------- source IPs
#
# Paddle publishes the addresses its webhooks come from. Checking them is
# defence in depth BEHIND the signature, never instead of it: an address
# can be spoofed at the packet level and a signature cannot, so the
# signature stays the thing that actually decides.
#
# The list is fetched rather than hard-coded because Paddle changes it,
# and a hard-coded copy fails silently months later - webhooks start
# being rejected, subscriptions stop being applied, and the cause is a
# constant nobody has looked at since.
_IP_CACHE = {"fetched": 0.0, "cidrs": None, "env": None}
_IP_TTL_SECONDS = 12 * 3600


def allowed_ip_cidrs(force=False):
    """-> [cidr] from Paddle, or None if the list could not be fetched.

    None is meaningfully different from []: an empty list would mean
    "Paddle sends from nowhere" and reject everything, where None means
    "we do not know", which the caller must treat as "do not block".
    """
    import time as _time
    env = environment()
    fresh = (_IP_CACHE["cidrs"] is not None
             and _IP_CACHE["env"] == env
             and (_time.time() - _IP_CACHE["fetched"]) < _IP_TTL_SECONDS)
    if fresh and not force:
        return _IP_CACHE["cidrs"]
    try:
        r = requests.get("%s/ips" % api_base(), timeout=10,
                         headers={"Paddle-Version": "1"})
        r.raise_for_status()
        cidrs = (r.json().get("data") or {}).get("ipv4_cidrs") or []
        if not cidrs:
            raise ValueError("no ipv4_cidrs in response")
        _IP_CACHE.update({"fetched": _time.time(), "cidrs": cidrs,
                          "env": env})
        return cidrs
    except Exception as e:                          # noqa: BLE001
        print("[paddle] could not fetch the webhook IP list: %s" % e)
        # Keep serving a stale list if there is one. An outage at
        # Paddle's end must not silently widen the check.
        return _IP_CACHE["cidrs"]


def ip_allowed(ip):
    """-> (allowed, reason). Unknown addresses are allowed, not blocked.

    FAILS OPEN, deliberately. If the list cannot be fetched, rejecting
    everything would stop real payments being applied - the customer is
    charged, the webhook is refused, and nobody is upgraded. The
    signature check is unaffected by any of this and still has to pass,
    so failing open costs a layer, while failing closed costs money.
    """
    cidrs = allowed_ip_cidrs()
    if not cidrs:
        return True, "Paddle's IP list is unavailable; signature only"
    if not ip:
        return True, "no client address available"
    try:
        import ipaddress as _ipaddress
        addr = _ipaddress.ip_address(ip.strip())
    except ValueError:
        return True, "unparseable client address %r" % ip
    for cidr in cidrs:
        try:
            if addr in _ipaddress.ip_network(cidr, strict=False):
                return True, "in %s" % cidr
        except ValueError:
            continue
    return False, "%s is not one of Paddle's %d published addresses" % (
        ip, len(cidrs))


def _minor(value):
    """Paddle sends money as a STRING in minor units - "199" is $1.99.

    A string because JSON numbers are doubles and Paddle will not put
    somebody's money through a float. Neither will this: it stays an
    integer number of cents all the way to the dashboard, and is divided
    by 100 only for display.
    """
    try:
        return int(str(value or "0").strip() or "0")
    except (TypeError, ValueError):
        return 0


def parse_payment(payload):
    """Pull the money out of a transaction.completed webhook.

    -> dict, or None if this event is not a completed payment.

    parse_event() below returns None for anything that is not a
    subscription event, which meant transaction.completed - the one
    event that says money actually arrived - was acknowledged and then
    thrown away. The dashboard needs it, so it is parsed here rather
    than by widening parse_event: the two produce different shapes and
    the caller does different things with them.
    """
    if payload.get("event_type") != "transaction.completed":
        return None

    data = payload.get("data") or {}
    details = data.get("details") or {}
    totals = details.get("totals") or {}
    # payout_totals is the same money expressed in the currency Paddle
    # will actually pay out in, and it carries fee/earnings when totals
    # does not. Falling back to it is the difference between knowing
    # what was earned and showing zero.
    payout = details.get("payout_totals") or {}

    return {
        "txn_id": data.get("id") or "",
        "user_id": str((data.get("custom_data") or {}).get("user_id") or ""),
        "customer_id": data.get("customer_id") or "",
        # A completed payment on a subscription means that subscription
        # is active, whether or not the subscription.* event that says
        # so ever arrives. app.py applies it as one.
        "subscription_id": data.get("subscription_id") or "",
        "occurred_at": payload.get("occurred_at") or "",
        "email": ((data.get("customer") or {}).get("email") or ""),
        "currency": (totals.get("currency_code")
                     or data.get("currency_code") or "USD"),
        "gross": _minor(totals.get("grand_total") or totals.get("total")),
        "fee": _minor(totals.get("fee") or payout.get("fee")),
        "earnings": _minor(totals.get("earnings") or payout.get("earnings")),
        # billed_at is when the money moved; occurred_at is when Paddle
        # got round to telling us, and they differ on a retry.
        "created": (data.get("billed_at") or payload.get("occurred_at")
                    or ""),
    }


def subscription_state(data, occurred_at=""):
    """One subscription object -> the fields app.py stores.

    The same object arrives three ways - inside a subscription.* webhook,
    from GET /subscriptions/{id} when reconciling, and in the response to
    a cancel - and all three are read here so they cannot disagree about
    what "active" or "cancels at period end" means.

    `occurred_at` is when this state was true. For a webhook that is the
    event's own timestamp; for an API read it is the subscription's
    updated_at. app.py compares it to the last one applied and ignores
    anything older, which is what makes delivery order not matter.
    """
    data = data or {}
    custom = data.get("custom_data") or {}
    status = data.get("status", "")
    period = data.get("current_billing_period") or {}
    return {
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
        "occurred_at": occurred_at or data.get("updated_at") or "",
    }


def parse_event(payload):
    """Normalise a webhook into the fields app.py stores.

    -> subscription_state() plus event_type - or None if this event is
       not about a subscription and should simply be acknowledged.
    """
    event_type = payload.get("event_type", "")
    if not event_type.startswith("subscription."):
        return None
    state = subscription_state(payload.get("data"),
                               payload.get("occurred_at") or "")
    state["event_type"] = event_type
    return state


def _subscription_call(method, path, body=None):
    """One authenticated call that returns a subscription. -> (state, error)."""
    if not configured():
        return None, config_problem()
    try:
        r = requests.request(method, f"{api_base()}{path}", headers=_headers(),
                             json=body, timeout=20)
    except requests.exceptions.RequestException as e:
        return None, f"Could not reach Paddle: {e}"
    if r.status_code == 404:
        return None, "not found"
    if r.status_code >= 400:
        detail = ""
        try:
            err = r.json().get("error", {})
            detail = err.get("detail") or err.get("code") or ""
        except ValueError:
            detail = r.text[:200]
        return None, f"Paddle refused ({r.status_code}): {detail}"
    try:
        return subscription_state(r.json()["data"]), None
    except (ValueError, KeyError, TypeError):
        return None, "Unexpected response from Paddle."


def cancel_subscription(sub_id, immediately=False):
    """Cancel at Paddle. -> (state, error).

    THE APP HAD NO WAY TO DO THIS. "Downgrade to Free" set the local
    plan to free and stopped there, so the customer lost Pro that
    minute and Paddle carried on charging them every month for a
    subscription this app no longer showed. Cancelling has to happen
    where the billing happens.

    `next_billing_period` (the default) keeps Pro until the period the
    customer already paid for runs out - the honest reading of
    "cancel". `immediately` is for deleting the account, where there is
    nobody left to keep it for.
    """
    if not sub_id:
        return None, "no subscription id"
    return _subscription_call(
        "POST", f"/subscriptions/{sub_id}/cancel",
        {"effective_from": "immediately" if immediately
         else "next_billing_period"})


def resume_subscription(sub_id):
    """Undo a scheduled cancellation. -> (state, error). Clearing
    scheduled_change is how Paddle expresses 'never mind'."""
    if not sub_id:
        return None, "no subscription id"
    return _subscription_call("PATCH", f"/subscriptions/{sub_id}",
                              {"scheduled_change": None})


def portal_session(customer_id):
    """A link to Paddle's own customer portal. -> (url, error).

    Card updates, invoices, and the receipts a customer may need for
    their own accounts, on Paddle's page - the same idea as Stripe's
    billing portal, which was the only one this app knew how to open.
    """
    if not configured():
        return None, config_problem()
    if not customer_id:
        return None, "no customer id"
    try:
        r = requests.post(f"{api_base()}/customers/{customer_id}/portal-sessions",
                          headers=_headers(), json={}, timeout=20)
    except requests.exceptions.RequestException as e:
        return None, f"Could not reach Paddle: {e}"
    if r.status_code >= 400:
        return None, f"Paddle refused ({r.status_code})"
    try:
        urls = r.json()["data"]["urls"]
        return urls["general"]["overview"], None
    except (ValueError, KeyError, TypeError):
        return None, "Unexpected response from Paddle."


def get_subscription(sub_id):
    """What Paddle says a subscription is right now. -> (state, error).

    The webhook is the normal way to learn about changes, and it is
    at-least-once, not exactly-once: a destination that failed too many
    times is disabled, a rotated secret refuses everything, a tunnel
    outage drops the lot. Any of those leaves a cancelled subscription
    marked active here for ever. This is the other direction - asking
    - and app.py's reconcile_subscriptions() runs it on a schedule.
    """
    if not configured():
        return None, config_problem()
    if not sub_id:
        return None, "no subscription id"
    try:
        r = requests.get(f"{api_base()}/subscriptions/{sub_id}",
                         headers=_headers(), timeout=20)
    except requests.exceptions.RequestException as e:
        return None, f"Could not reach Paddle: {e}"
    if r.status_code == 404:
        return None, "not found"
    if r.status_code >= 400:
        return None, f"Paddle refused ({r.status_code})"
    try:
        return subscription_state(r.json()["data"]), None
    except (ValueError, KeyError, TypeError):
        return None, "Unexpected response from Paddle."
