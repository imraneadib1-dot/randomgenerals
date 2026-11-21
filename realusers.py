"""Real people: everyone who actually TYPED something.

    import realusers
    realusers.collect(start_day, days)   # one plain dict

Page views and visitor hashes count anyone who loaded a page - crawlers
that run JavaScript, someone who bounced off the landing page, a link
preview. The question the owner asked is narrower and more useful: how
many PEOPLE used the thing. A person counts here once they sent at least
one message, and is one of two kinds:

    accounts   signed in (their id is a row in `users`)
    visitors   never signed in - a guest session that still typed

Two sources, because neither covers the whole life of the app:

    usage_log  one row per owner per day with a message count. Exact per
               day, but it only began on the day it was added.
    threads    every conversation, with its messages. Goes back to the
               beginning; has no per-message dates, so it only feeds the
               ALL-TIME count (someone who typed before usage_log existed
               is still a person who typed).

The owner's own account (ADMIN_EMAIL) is left out of every figure: the
point is other people.
"""
import datetime
import json
import os

import db


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _owner_id():
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    if not email:
        return None
    row = db._connect().execute("select id from users where lower(email) = ?", (email,)).fetchone()
    return row[0] if row else None


def collect(start=None, days=30):
    """-> totals and a per-day series of people who typed.

    `start` is an ISO day (the first day of the series); `days` its length."""
    conn = db._connect()
    me = _owner_id()
    accounts = {r[0] for r in conn.execute("select id from users").fetchall()}

    def kind(owner):
        return "account" if owner in accounts else "visitor"

    # --- per day, from usage_log -------------------------------------------
    by_day = {}
    for owner, day in conn.execute(
            "select owner_id, day from usage_log where messages > 0").fetchall():
        if owner == me:
            continue
        by_day.setdefault(day, {})[owner] = kind(owner)
    log_began = min(by_day) if by_day else None

    # --- all time: usage_log plus everyone with a message in a thread ------
    everyone = {}
    for people in by_day.values():
        everyone.update(people)
    for owner, blob in conn.execute("select owner_id, messages_json from threads").fetchall():
        if owner == me or owner in everyone or not owner:
            continue
        try:
            msgs = json.loads(blob or "[]")
        except (ValueError, TypeError):
            continue
        if any(m.get("role") == "user" and (m.get("content") or "").strip() for m in msgs):
            everyone[owner] = kind(owner)

    today = _today()

    def people_between(first, last):
        seen = {}
        for d, people in by_day.items():
            if first.isoformat() <= d <= last.isoformat():
                seen.update(people)
        return seen

    def split(people):
        acc = sum(1 for k in people.values() if k == "account")
        return {"total": len(people), "accounts": acc, "visitors": len(people) - acc}

    if start is None:
        start = (today - datetime.timedelta(days=days - 1)).isoformat()
    first = datetime.date.fromisoformat(start)
    series = []
    for i in range(days):
        d = (first + datetime.timedelta(days=i)).isoformat()
        people = by_day.get(d, {})
        s = split(people)
        s["day"] = d
        s["counted"] = bool(log_began) and d >= log_began
        series.append(s)
    if days > 120:
        # A bar per day stops being readable; a week is counted as
        # distinct people in that week (not its days added up - someone
        # here every day is one person a week, not seven).
        weeks = []
        for i in range(0, days, 7):
            chunk_first = first + datetime.timedelta(days=i)
            chunk_last = min(first + datetime.timedelta(days=days - 1),
                             chunk_first + datetime.timedelta(days=6))
            s = split(people_between(chunk_first, chunk_last))
            s["day"] = chunk_first.isoformat()
            s["counted"] = bool(log_began) and chunk_last.isoformat() >= log_began
            weeks.append(s)
        series = weeks
    counted = [r for r in series if r["counted"]]

    return {
        "bucket": "week" if days > 120 else "day",
        "all_time": split(everyone),
        "today": split(by_day.get(today.isoformat(), {})),
        "yesterday": split(by_day.get((today - datetime.timedelta(days=1)).isoformat(), {})),
        "last_7": split(people_between(today - datetime.timedelta(days=6), today)),
        "last_30": split(people_between(today - datetime.timedelta(days=29), today)),
        "window": split(people_between(first, first + datetime.timedelta(days=days - 1))),
        "average": round(sum(r["total"] for r in counted) / len(counted), 1) if counted else 0,
        "peak": max([r["total"] for r in series] or [0]) or 1,
        "series": series,
        "began": log_began,
        "excludes_owner": bool(me),
    }
