"""Product statistics, computed from data the app already stores.

WHAT THIS IS AND IS NOT

This is not analytics in the tracking sense. Nothing here is instrumented,
no page view is recorded, no visitor is identified, and no new column
exists to serve it. Every figure below is derived from rows the app wrote
anyway in order to function - accounts, credit pools, threads - by asking
questions of them after the fact.

That distinction is the whole design. LOCAL_AI.md promises no telemetry
and the privacy page repeats it, so a stats feature that logged visits
would have made both of those false. Reading what is already there costs
nothing and keeps them true.

The cost of that choice is a real blind spot, stated here rather than
hidden: THIS CANNOT SEE ANYONE WHO DID NOT TYPE. A visitor who opened the
site, read the landing page and left is invisible, because the app never
wrote anything about them. Cloudflare's dashboard sees exactly those
people and almost nothing about what they did next, which is why the two
answer different questions and why neither replaces the other.

One subtlety worth knowing when reading the numbers: a row in `credits`
is created for every distinct owner, guest sessions included, at the
moment they first do something that costs credits. So the credits table
is the closest thing here to a count of people who actually used the app,
and it will always exceed the user count, because signing up is rarer
than trying it.
"""
import collections
import datetime
import json

import db


def _rows(sql, args=()):
    """Read-only query against the live connection. Never writes."""
    return db._connect().execute(sql, args).fetchall()


def _one(sql, args=()):
    row = _rows(sql, args)
    return row[0][0] if row else 0


def _day_series(pairs, days=30):
    """[(iso_day, count)] -> a dense series with the gaps filled in.

    Sparse data plots a lie: three signups on three separate days drawn
    without the empty days between them looks like steady growth. The
    zeroes have to be real points.
    """
    have = {d: n for d, n in pairs if d}
    today = datetime.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        out.append((d, have.get(d, 0)))
    return out


def collect():
    """Everything the stats page shows, as one plain dict."""
    users_total = _one("select count(*) from users")
    by_plan = dict(_rows("select plan, count(*) from users group by plan"))
    owners_total = _one("select count(*) from credits")
    guests = _one("select count(*) from credits "
                  "where owner_id like 'guest:%'")

    # Subscriptions. Counted from subscription_status rather than plan,
    # because plan is what the account currently gets and status is what
    # Paddle last said - they disagree exactly when something has gone
    # wrong with billing, which is the case worth being able to see.
    subs = dict(_rows(
        "select coalesce(nullif(subscription_status,''),'none'), count(*) "
        "from users group by 1"))

    threads_total = _one("select count(*) from threads")
    by_mode = dict(_rows("select mode, count(*) from threads group by mode"))

    # Messages and who answered them. Held in a JSON blob per thread
    # rather than a table, so this is a scan - fine at this size, and the
    # alternative is a schema change to serve a page nobody loads often.
    messages = 0
    by_provider = collections.Counter()
    by_model = collections.Counter()
    images = 0
    for (blob,) in _rows("select messages_json from threads"):
        try:
            msgs = json.loads(blob or "[]")
        except (ValueError, TypeError):
            continue
        messages += len(msgs)
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            by_provider[m.get("provider") or "unknown"] += 1
            if m.get("model"):
                by_model[m["model"]] += 1
            if m.get("type") == "image":
                images += 1

    # Credits actually consumed, as a share of what was handed out. The
    # useful reading is not the raw number but whether people are running
    # into the cap - a high proportion means the free tier is binding.
    spent = _one("select coalesce(sum(max(0, starting - balance)), 0) "
                 "from credits")
    granted = _one("select coalesce(sum(starting), 0) from credits")

    # What the one paid model has cost today, and the last week of it.
    # This is the only figure on the page denominated in money leaving an
    # account rather than in rows, so it is worth being able to see
    # without opening OpenRouter.
    try:
        import openrouter_api          # noqa: PLC0415 - avoids a cycle
        spent, limit = openrouter_api.budget_state()
        recent = _rows("select day, usd from openrouter_spend "
                       "order by day desc limit 7")
        kimi = {
            "configured": openrouter_api.configured(),
            "today": round(spent, 4) if spent != float("inf") else None,
            "daily_limit": round(limit, 2),
            "recent": [{"day": d, "usd": round(u, 4)} for d, u in recent],
            "week": round(sum(u for _, u in recent), 4),
        }
    except Exception:                  # noqa: BLE001 - a stats page must
        kimi = {"configured": False}   # not fail over an optional figure

    return {
        "generated": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "kimi": kimi,
        "accounts": {
            "users": users_total,
            "free": by_plan.get("free", 0),
            "pro": by_plan.get("pro", 0),
            "google": _one("select count(*) from users where google_id "
                           "is not null and google_id <> ''"),
            "subscriptions": subs,
            # Of everyone who used the app, how many made an account.
            "signup_rate": (round(100.0 * users_total / owners_total, 1)
                            if owners_total else 0.0),
        },
        "reach": {
            "owners": owners_total,
            "guests": guests,
            "registered_active": owners_total - guests,
        },
        "usage": {
            "threads": threads_total,
            "by_mode": by_mode,
            "messages": messages,
            "images": images,
            "by_provider": dict(by_provider.most_common()),
            "by_model": dict(by_model.most_common(8)),
        },
        "credits": {
            "spent": spent,
            "granted": granted,
            "used_pct": (round(100.0 * spent / granted, 1)
                         if granted else 0.0),
        },
        "signups": _day_series(_rows(
            "select substr(created,1,10), count(*) from users "
            "group by 1 order by 1")),
        "activity": _day_series(_rows(
            "select substr(updated,1,10), count(*) from threads "
            "group by 1 order by 1")),
    }
