"""Kelviq - the payment provider, as merchant of record.

Kelviq is legally the seller: it takes the payment, charges and remits
VAT / GST / US sales tax, handles chargebacks, and pays out. Same
arrangement as Paddle (paddle_billing.py), which stays in the code as
the fallback; app.py uses Kelviq first whenever it is configured.

HOW AN ACCOUNT IS LINKED
Kelviq lets the seller choose the customer's id (`customerId` on
checkout). This app passes its own user id, so every webhook carries
`customer.customer_id` == the local user id and nothing has to be
matched on email - which breaks the moment someone pays with another
address. `metadata.user_id` is sent as well, as a second key.

Docs: https://docs.kelviq.com (API reference, guides/webhooks).

CONFIGURATION
    KELVIQ_API_KEY          server key (Authorization: Bearer ...)
    KELVIQ_PLAN_PRO         the plan identifier for Pro in Kelviq
    KELVIQ_WEBHOOK_SECRET   signing secret of the webhook endpoint
    KELVIQ_ENV              "production" (default) or "sandbox"
    KELVIQ_CHARGE_PERIOD    "MONTHLY" (default), "YEARLY", ...
    KELVIQ_API_BASE         optional override of the API base URL

Sandbox and production are separate systems with separate keys and
plans. The webhook endpoint to register in Kelviq is
    https://<your domain>/api/billing/kelviq/webhook
"""
import hashlib
import hmac
import os
import time

import requests

# Read at call time, never at import - see paddle_billing.py for the bug
# that taught this.
def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def api_key():
    return _env("KELVIQ_API_KEY")


def plan_identifier():
    return _env("KELVIQ_PLAN_PRO")


def webhook_secret():
    return _env("KELVIQ_WEBHOOK_SECRET")


def charge_period():
    return (_env("KELVIQ_CHARGE_PERIOD") or "MONTHLY").upper()


def environment():
    """Production unless sandbox is asked for by name: this site goes
    straight to live payments, with no sandbox stage."""
    return ("sandbox" if _env("KELVIQ_ENV").lower()
            in ("sandbox", "test") else "production")


def api_base():
    override = _env("KELVIQ_API_BASE")
    if override:
        return override.rstrip("/")
    return ("https://api.kelviq.com/api/v1" if environment() == "production"
            else "https://sandboxapi.kelviq.com/api/v1")


def configured():
    """Can a checkout be started? A key and a plan to sell."""
    return bool(api_key() and plan_identifier())


def webhook_ready():
    return bool(webhook_secret())


def config_problem():
    """One sentence for whoever runs the server, or "" when all is set."""
    missing = [name for name, value in (
        ("KELVIQ_API_KEY", api_key()),
        ("KELVIQ_PLAN_PRO", plan_identifier()),
        ("KELVIQ_WEBHOOK_SECRET", webhook_secret()),
    ) if not value]
    if missing:
        return "Kelviq is missing " + ", ".join(missing) + "."
    return ""


# Statuses in which the customer has Pro. Kelviq sends them lower-case.
ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def _headers():
    return {"Authorization": "Bearer " + api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _post(path, body):
    """-> (json, error). error is a sentence, never an exception."""
    try:
        r = requests.post(api_base() + path, json=body, headers=_headers(),
                          timeout=20)
    except requests.RequestException as e:
        return None, "Kelviq could not be reached (%s)." % e.__class__.__name__
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = (data.get("detail") or data.get("message")
                      or data.get("error") or "")
            if isinstance(detail, (list, dict)):
                detail = str(detail)
        return None, "Kelviq answered %s%s" % (
            r.status_code, (": " + str(detail)) if detail else ".")
    return data if isinstance(data, dict) else {}, None


def create_checkout(user_id, email, success_url, cancel_url):
    """-> (checkout_url, error)."""
    body = {
        "planIdentifier": plan_identifier(),
        "chargePeriod": charge_period(),
        "successUrl": success_url,
        "cancelUrl": cancel_url,
        "customerId": str(user_id),
        "metadata": {"user_id": str(user_id)},
    }
    if email:
        body["email"] = email
    data, err = _post("/checkout/", body)
    if err:
        return None, err
    url = data.get("checkoutUrl")
    if not url:
        return None, "Kelviq did not return a checkout link."
    return url, None


def cancel_subscription(sub_id, immediately=False):
    """-> (state, error). Kelviq answers with a message only, so the
    state is what was asked for; the subscription.* webhook that follows
    is the confirmation, and it goes through the same ordering rule."""
    _data, err = _post("/subscriptions/%s/cancel/" % sub_id, {
        "cancellationType": "IMMEDIATE" if immediately else "CURRENT_PERIOD_ENDS",
    })
    if err:
        return None, err
    return {
        "subscription_id": sub_id,
        "status": "canceled" if immediately else "active",
        "active": not immediately,
        "cancel_at_period_end": not immediately,
        "current_period_end": None,
        "occurred_at": "",
    }, None


def portal_session(customer_id):
    """-> (url, error). The URL needs its token to be signed in."""
    data, err = _post("/portal/session/", {"customerId": str(customer_id)})
    if err:
        return None, err
    url = data.get("customerPortalUrl") or data.get("url")
    if not url:
        return None, "Kelviq did not return a portal link."
    token = data.get("token")
    if token:
        url += ("&" if "?" in url else "?") + "token=" + requests.utils.quote(str(token))
    return url, None


TOLERANCE_SECONDS = 300


def verify_webhook(raw_body, headers, now=None):
    """Kelviq's scheme: HMAC-SHA256 over "{webhook-id}.{webhook-timestamp}.
    {raw body}" with the endpoint's signing secret, hex, sent as
    "v1,<hex>" in webhook-signature. -> (ok, reason). Fails closed."""
    # Several secrets may be configured, separated by commas or spaces:
    # one per webhook endpoint pointing here (two endpoints to the same
    # URL each sign with their own), and an old and a new one while a
    # secret is being rotated. A delivery signed with any of them passes.
    secrets = [s for s in webhook_secret().replace(",", " ").split() if s]
    if not secrets:
        return False, "no webhook secret configured"
    wid = headers.get("webhook-id", "")
    ts = headers.get("webhook-timestamp", "")
    sig_header = headers.get("webhook-signature", "")
    if not (wid and ts and sig_header):
        return False, "missing signature headers"
    try:
        sent_at = int(ts)
    except ValueError:
        return False, "bad timestamp"
    if abs((now or time.time()) - sent_at) > TOLERANCE_SECONDS:
        return False, "timestamp outside tolerance"
    body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else str(raw_body)
    signed = ("%s.%s.%s" % (wid, ts, body)).encode()
    expected = [hmac.new(s.encode(), signed, hashlib.sha256).hexdigest() for s in secrets]
    # The header may carry several space-separated signatures (a secret
    # being rotated); any one matching any configured secret is enough.
    for part in sig_header.split():
        version, _, value = part.partition(",")
        if version == "v1" and any(hmac.compare_digest(value.strip(), e) for e in expected):
            return True, ""
    return False, "signature mismatch"


def _customer_ref(obj):
    """The local user id: customer.customer_id (ours, set at checkout),
    else customer_id, else metadata.user_id."""
    customer = obj.get("customer") if isinstance(obj.get("customer"), dict) else {}
    return str(customer.get("customer_id") or obj.get("customer_id")
               or (obj.get("metadata") or {}).get("user_id") or "")


def _minor_units(obj, units_key, amount_key):
    """Money as integer minor units. Kelviq sends both "amount" ("21.24")
    and "amount_units" (2124); the integer is preferred."""
    units = obj.get(units_key)
    if isinstance(units, (int, float)):
        return int(units)
    try:
        return int(round(float(obj.get(amount_key) or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _iso(value):
    """A timestamp as ISO 8601, whatever shape it came in. app.py orders
    events by it, and an epoch number read as text would sort as
    ancient and get every later event ignored."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).isdigit():
        from datetime import datetime, timezone
        n = float(value)
        if n > 1e12:          # milliseconds
            n /= 1000.0
        return datetime.fromtimestamp(n, timezone.utc).isoformat()
    return str(value)


def subscription_state(obj, occurred_at=""):
    """A Kelviq subscription object -> the fields app.py stores."""
    obj = obj or {}
    status = str(obj.get("status") or "").lower()
    end_date = obj.get("end_date")
    future_end = False
    if end_date:
        try:
            from datetime import datetime, timezone
            future_end = datetime.fromisoformat(
                str(end_date).replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except ValueError:
            future_end = False
    # A cancellation scheduled for the end of the period keeps status
    # "active" and sets end_date. A "canceled" one whose end_date is
    # still ahead has also been paid up to then, so Pro stays until it.
    active = status in ACTIVE_STATUSES or (status == "canceled" and future_end)
    return {
        "user_id": _customer_ref(obj),
        "subscription_id": obj.get("id"),
        "customer_id": _customer_ref(obj),
        "status": status,
        "active": active,
        "cancel_at_period_end": bool(active and end_date),
        "current_period_end": (end_date if end_date
                               else obj.get("billing_period_end_time")),
        "occurred_at": _iso(occurred_at or obj.get("updated_at")),
    }


def parse_event(payload):
    """subscription.* -> subscription_state() + event_type; else None."""
    event_type = str(payload.get("type") or "")
    if not event_type.startswith("subscription."):
        return None
    obj = ((payload.get("data") or {}).get("object")) or {}
    state = subscription_state(obj, payload.get("created_at") or "")
    state["event_type"] = event_type
    return state


def parse_payment(payload):
    """invoice.paid -> the dashboard's payment record, or None."""
    if payload.get("type") != "invoice.paid":
        return None
    obj = ((payload.get("data") or {}).get("object")) or {}
    customer = obj.get("customer") if isinstance(obj.get("customer"), dict) else {}
    return {
        "txn_id": str(obj.get("id") or payload.get("id") or ""),
        "user_id": _customer_ref(obj),
        "customer_id": _customer_ref(obj),
        "subscription_id": obj.get("subscription_id") or "",
        "occurred_at": _iso(payload.get("created_at")),
        "email": customer.get("email") or "",
        "currency": (obj.get("currency") or obj.get("currency_code") or "USD").upper(),
        "gross": _minor_units(obj, "amount_total_units", "amount_total"),
        # A merchant of record's fee is not on the invoice; it shows in
        # the payout. Fee is recorded as unknown (0) and earnings as the
        # gross, i.e. before Kelviq's cut - not guessed.
        "fee": 0,
        "earnings": _minor_units(obj, "amount_total_units", "amount_total"),
        "created": obj.get("paid_at") or payload.get("created_at") or "",
    }
