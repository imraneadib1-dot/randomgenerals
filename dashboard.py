"""The owner's dashboard: visitors, money, and requests.

    from dashboard import collect
    collect()          # one plain dict, everything the page shows

HOW THIS DIFFERS FROM stats.py

stats.py answers "what is in the database" and is careful to derive
every figure from rows the app wrote anyway. It says so in its own
docstring, and it names its blind spot: it cannot see anyone who did not
type. Someone who opened the site, read the landing page and left was
invisible.

That blind spot is exactly what was asked for here, so this module is
backed by two things stats.py did not have: a visit counter
(db.record_visit, called from app.py on every page load) and a payments
table (written by the Paddle webhook). Both are counters. Neither
stores a person - see the site_visitors comment in db.py for what a
visitor hash is and why it expires nightly.

WHY MONEY IS NOT ONE NUMBER

Three different numbers get called revenue and only one of them is
income:

    gross     what customers were charged, tax included
    fee       Paddle's cut, plus payment processing
    earnings  what actually reaches the balance

The dashboard leads with earnings. Leading with gross would overstate
what the site makes by roughly Paddle's margin, and it is the figure
people quote at themselves right up until the first payout arrives
smaller than expected.

And they are grouped by currency, never summed across them. Paddle
charges in the customer's own currency; adding EUR to USD produces a
number that is not an amount of money.
"""
import datetime

import db


def _money(minor, currency="USD"):
    """Minor units -> a string a person can read. 199 -> '$1.99'."""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return "%s%.2f%s" % (symbol, (minor or 0) / 100.0,
                         "" if symbol else " " + currency)


def _billing_health():
    """Can this site actually take money right now?

    On the dashboard because it is the question the numbers cannot
    answer. PADDLE_ENV was set to "pro" here for a while - every
    credential correct, every check passing, and checkout failing for
    everybody. A revenue chart reading zero looks identical whether
    nobody wanted to pay or nobody was able to.
    """
    try:
        import paddle_billing
    except ImportError:
        return {"ok": False, "detail": "Paddle support is not installed."}

    problem = paddle_billing.config_problem()
    if problem:
        return {"ok": False, "environment": paddle_billing.environment(),
                "detail": problem}
    if not paddle_billing.webhook_ready():
        return {"ok": False, "environment": paddle_billing.environment(),
                "detail": "No webhook secret, so payments would succeed "
                          "without anyone being upgraded."}
    if paddle_billing.environment() != "production":
        return {"ok": False, "environment": paddle_billing.environment(),
                "detail": "Running against Paddle's sandbox. Payments are "
                          "fake and no real money can arrive."}
    return {"ok": True, "environment": "production",
            "detail": "Configured for real payments."}


def collect(days=30):
    """Everything the dashboard shows, as one plain dict."""
    visits = db.visit_totals()
    requests_ = db.request_totals()
    payments = db.payment_totals()

    # Presentation is done here rather than in the template because the
    # same figures go out over /api/dashboard, and a caller reading the
    # JSON should not have to re-derive "what did this actually earn".
    money = []
    for currency, row in sorted(payments["by_currency"].items()):
        money.append({
            "currency": currency,
            "payments": row["count"],
            "gross": row["gross"],
            "fee": row["fee"],
            "earnings": row["earnings"],
            "gross_text": _money(row["gross"], currency),
            "fee_text": _money(row["fee"], currency),
            "earnings_text": _money(row["earnings"], currency),
        })

    # The headline figure. Only meaningful when there is one currency;
    # with several, the table below it is the honest answer and this
    # says so rather than adding them together.
    if len(money) == 1:
        headline = money[0]["earnings_text"]
    elif not money:
        headline = _money(0)
    else:
        headline = "%d currencies" % len(money)

    visit_days = db.visit_series(days)
    request_days = db.request_series(days)
    payment_days = db.payment_series(days)

    # Peak values, so the template can scale bars without doing
    # arithmetic in Jinja. A zero max would divide by zero in the
    # template, so it floors at 1 - a flat empty chart, which is the
    # truth when nothing has happened yet.
    def peak(rows, key):
        return max([r[key] for r in rows] or [0]) or 1

    subs = dict(db._connect().execute(
        "select coalesce(nullif(subscription_status,''),'none'), count(*) "
        "from users group by 1").fetchall())

    return {
        "generated": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "days": days,
        "visitors": {
            "today": visits["visitors_today"],
            "views_today": visits["views_today"],
            "views_total": visits["views_total"],
            "series": visit_days,
            "peak_views": peak(visit_days, "views"),
            "peak_visitors": peak(visit_days, "visitors"),
            "window_views": sum(r["views"] for r in visit_days),
            "top_pages": db.top_pages(days),
        },
        "money": {
            "headline": headline,
            "by_currency": money,
            "payments": payments["count"],
            "series": payment_days,
            "peak": peak(payment_days, "earnings"),
            "recent": [dict(p, gross_text=_money(p["gross"], p["currency"]),
                            earnings_text=_money(p["earnings"], p["currency"]))
                       for p in db.recent_payments()],
            "subscribers": subs.get("active", 0),
            "subscriptions": subs,
            "health": _billing_health(),
        },
        "requests": {
            "total": requests_["messages_total"],
            "today": requests_["messages_today"],
            "credits": requests_["credits_total"],
            "series": request_days,
            "peak": peak(request_days, "messages"),
            "window": sum(r["messages"] for r in request_days),
        },
    }
