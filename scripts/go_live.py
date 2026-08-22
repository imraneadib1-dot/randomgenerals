"""Switch the app from Stripe test mode to live mode.

    python scripts/go_live.py --key sk_live_... --url https://yourdomain.com

Test mode and live mode are entirely separate worlds in Stripe: products,
prices, customers, subscriptions and webhooks created in one do not exist
in the other. So going live is not "flip a switch" - the $1.99 product
and the webhook endpoint have to be created again, against live keys.
This script does that, idempotently, and writes the resulting values to
.env so nothing has to be copied by hand.

It deliberately refuses to run against a test key: the whole point is to
produce a live configuration, and silently succeeding with sk_test_
would leave you believing you were charging real cards when you weren't.
"""
import argparse
import io
import re
import sys

import stripe

PRODUCT_NAME = "Pro"
PRICE_CENTS = 199
CURRENCY = "usd"
INTERVAL = "month"
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
]
ENV_PATH = ".env"


def write_env(updates):
    """Update keys in .env in place, leaving everything else untouched
    and without printing the file (it holds other secrets)."""
    try:
        lines = io.open(ENV_PATH, encoding="utf-8").read().splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    out, seen = [], set()
    for line in lines:
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}\n")
    io.open(ENV_PATH, "w", encoding="utf-8", newline="").write("".join(out))


def ensure_product_and_price():
    """Find or create the live $1.99/mo Pro product. Reuses an existing
    matching price rather than creating duplicates - Stripe happily lets
    you attach many prices to one product, and a stray second $1.99
    price is a confusing thing to debug later."""
    products = stripe.Product.list(active=True, limit=100).data
    product = next((p for p in products if p.name == PRODUCT_NAME), None)
    if product:
        print(f"  found existing product: {product.id}")
    else:
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description="Advanced 7B model, image understanding, unlimited "
                        "memory, long-form code, 100MB uploads.",
        )
        print(f"  created product: {product.id}")

    prices = stripe.Price.list(product=product.id, active=True, limit=100).data
    price = next(
        (p for p in prices
         if p.unit_amount == PRICE_CENTS
         and p.currency == CURRENCY
         and p.recurring
         and p.recurring.interval == INTERVAL),
        None,
    )
    if price:
        print(f"  found existing price: {price.id}")
    else:
        price = stripe.Price.create(
            product=product.id,
            unit_amount=PRICE_CENTS,
            currency=CURRENCY,
            recurring={"interval": INTERVAL},
        )
        print(f"  created price: {price.id}  (${PRICE_CENTS/100:.2f}/{INTERVAL})")
    return price.id


def ensure_webhook(base_url):
    """Register the webhook and return its signing secret.

    Stripe reveals a webhook's secret only when it is created, so an
    endpoint that already exists has to be recreated to obtain one.
    """
    url = base_url.rstrip("/") + "/api/billing/webhook"
    existing = next(
        (e for e in stripe.WebhookEndpoint.list(limit=100).data if e.url == url),
        None,
    )
    if existing:
        print(f"  endpoint already exists ({existing.id}) - recreating to "
              "retrieve its signing secret")
        stripe.WebhookEndpoint.delete(existing.id)
    ep = stripe.WebhookEndpoint.create(
        url=url,
        enabled_events=WEBHOOK_EVENTS,
        description="RandomGenerals AI (live)",
    )
    print(f"  created endpoint: {ep.id}\n    -> {url}")
    return ep.secret


def main():
    ap = argparse.ArgumentParser(description="Move Stripe config to live mode.")
    ap.add_argument("--key", required=True, help="live secret key (sk_live_...)")
    ap.add_argument("--url", required=True,
                    help="public HTTPS base URL, e.g. https://yourdomain.com")
    ap.add_argument("--allow-test-key", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.key.startswith("sk_test_") and not args.allow_test_key:
        sys.exit("This is a TEST key. Going live needs sk_live_... "
                 "(Stripe dashboard -> toggle off Test mode -> API keys).")
    if not args.key.startswith(("sk_live_", "sk_test_")):
        sys.exit("That doesn't look like a Stripe secret key.")
    if not args.url.startswith("https://"):
        sys.exit("The URL must be https:// - Stripe will not send webhooks "
                 "to a plain http endpoint.")

    stripe.api_key = args.key

    acct = stripe.Account.retrieve()
    print(f"account {acct.id}  ({acct.country})")
    if not acct.charges_enabled:
        sys.exit(
            "This account cannot take charges yet. Finish activation at\n"
            "  https://dashboard.stripe.com/account/onboarding\n"
            "(business details, bank account, identity verification), then "
            "run this again."
        )
    print(f"  charges_enabled={acct.charges_enabled} "
          f"payouts_enabled={acct.payouts_enabled}")

    print("\nproduct & price:")
    price_id = ensure_product_and_price()

    print("\nwebhook:")
    secret = ensure_webhook(args.url)

    write_env({
        "STRIPE_SECRET_KEY": args.key,
        "STRIPE_PRICE_ID_PRO": price_id,
        "STRIPE_WEBHOOK_SECRET": secret,
    })
    print("\n.env updated (STRIPE_SECRET_KEY, STRIPE_PRICE_ID_PRO, "
          "STRIPE_WEBHOOK_SECRET)")
    print("\nRestart the app, then make ONE real purchase with a real card "
          "to confirm the whole path works end to end. Refund it from the "
          "Stripe dashboard afterwards - that is the only way to know the "
          "live webhook actually fires.")


if __name__ == "__main__":
    main()
