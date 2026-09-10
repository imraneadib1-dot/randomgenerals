# -*- coding: utf-8 -*-
"""Are the dashboard's numbers actually right?

    python check_dashboard.py

Builds a throwaway database, drives real requests through the real app,
and checks what came out. Nothing here touches the live site.

Not a smoke test. It checks the things that would otherwise be found by
the owner looking at a wrong number and believing it:

  - visits are counted, and counted ONCE per person per day
  - daily users are people rather than page loads: one browser is one
    user however many pages it opens, a cookie-less crawler is not one
    person per page, and a week is distinct people rather than its own
    days added together
  - bots and assets are not counted
  - a Paddle webhook writes a payment, and a redelivery does not
    write it twice
  - the page renders with data and with none of it
  - the gate actually holds for someone who is not the owner

WHY THESE CHECKS AND NOT OTHERS

Each one stands in for a way the dashboard could lie quietly. A double
count makes a hobby project look like a business; a missed webhook
retry makes one sale look like two; an ungated /api/dashboard hands the
revenue figures to anyone who guesses the path. None of those raise an
error - they just print a wrong number that gets believed.
"""
import json
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="dashtest-")
os.environ["DB_PATH"] = os.path.join(WORK, "test.db")
os.environ["ADMIN_EMAIL"] = "owner@example.com"
os.environ["SECRET_KEY"] = "test-only"
os.environ["PADDLE_ENV"] = "production"
os.environ["PADDLE_API_KEY"] = "pdl_live_" + "x" * 40
os.environ["PADDLE_PRICE_ID_PRO"] = "pri_" + "x" * 20
os.environ["PADDLE_WEBHOOK_SECRET"] = "ntfset_" + "x" * 20
os.environ["PADDLE_CLIENT_TOKEN"] = "live_" + "x" * 20

sys.path.insert(0, os.path.abspath("."))

import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402
import dashboard                                          # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-52s %s   (got %r)" % (label, "ok" if ok else "FAIL", got))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


client = appmod.app.test_client()

print("== visits ==")
# Same person, three page loads.
HUMAN = {"CF-Connecting-IP": "203.0.113.9",
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130"}
for _ in range(3):
    client.get("/", headers=HUMAN)
client.get("/privacy", headers=HUMAN)

# A different person.
client.get("/", headers={"CF-Connecting-IP": "198.51.100.4",
                         "User-Agent": "Mozilla/5.0 (iPhone) Safari/17"})

# Things that must NOT count.
client.get("/", headers={"CF-Connecting-IP": "8.8.8.8",
                         "User-Agent": "Googlebot/2.1"})
client.get("/static/style.css", headers=HUMAN)
client.get("/api/plans", headers=HUMAN)
client.get("/sw.js", headers=HUMAN)

totals = db.visit_totals()
check("page views counted", totals["views_total"], 5)
check("distinct visitors today", totals["visitors_today"], 2)
check("top page is /", db.top_pages()[0]["path"], "/")
check("/ has 4 views (3 + 1)", db.top_pages()[0]["views"], 4)
paths = [p["path"] for p in db.top_pages()]
check("no /static path counted", any(p.startswith("/static") for p in paths),
      False)
check("no /api path counted", any(p.startswith("/api") for p in paths), False)

print("\n== the visitor hash really is unreadable ==")
salt = db.visitor_salt(db._today())
row = db._connect().execute("SELECT visitor FROM site_visitors "
                            "LIMIT 1").fetchone()
check("stored value is not an IP", "203.0.113" in row["visitor"], False)
check("stored value is a truncated hash", len(row["visitor"]), 32)
# Tomorrow's salt evicts today's, which is what makes today's
# permanently unreadable rather than merely inconvenient.
db.visitor_salt("2099-01-01")
left = db._connect().execute("SELECT COUNT(*) FROM visit_salt").fetchone()[0]
check("only one salt is ever kept", left, 1)
gone = db._connect().execute("SELECT COUNT(*) FROM visit_salt WHERE salt = ?",
                             (salt,)).fetchone()[0]
check("yesterday's salt is destroyed", gone, 0)

print("\n== payments ==")
PAYLOAD = {
    "event_type": "transaction.completed",
    "occurred_at": "2026-09-06T10:00:00Z",
    "data": {
        "id": "txn_01abc", "status": "completed",
        "customer_id": "ctm_1", "custom_data": {"user_id": "u1"},
        "billed_at": "2026-09-06T09:59:00Z",
        "details": {"totals": {"currency_code": "USD", "grand_total": "199",
                               "fee": "35", "earnings": "164"}},
    },
}
parsed = appmod.paddle_billing.parse_payment(PAYLOAD)
check("gross parsed as integer cents", parsed["gross"], 199)
check("earnings parsed", parsed["earnings"], 164)
check("fee parsed", parsed["fee"], 35)
check("non-payment event ignored",
      appmod.paddle_billing.parse_payment({"event_type": "subscription.created"}),
      None)

db.record_payment(parsed["txn_id"], "u1", "payer@example.com",
                  parsed["currency"], parsed["gross"], parsed["fee"],
                  parsed["earnings"], parsed["created"])
# Paddle retries. The same transaction must not be counted twice.
db.record_payment(parsed["txn_id"], "u1", "payer@example.com",
                  parsed["currency"], parsed["gross"], parsed["fee"],
                  parsed["earnings"], parsed["created"])
t = db.payment_totals()
check("one payment after a redelivery", t["count"], 1)
check("earnings total", t["by_currency"]["USD"]["earnings"], 164)

# A second payment, and a different currency, to prove they never merge.
db.record_payment("txn_02", "u2", "b@example.com", "EUR", 199, 40, 159,
                  "2026-09-06T11:00:00Z")
t = db.payment_totals()
check("currencies stay separate", sorted(t["by_currency"]), ["EUR", "USD"])

print("\n== collect() ==")
d = dashboard.collect()
check("visitors today", d["visitors"]["today"], 2)
check("views total", d["visitors"]["views_total"], 5)
check("payments", d["money"]["payments"], 2)
check("two currencies -> no single headline", d["money"]["headline"],
      "2 currencies")
check("peaks never zero", d["requests"]["peak"] >= 1, True)
# The default is no longer a rolling 30-day window: collect() reports
# everything since launch. Every row here was written today, so launch
# is today and the span is one day.
check("span defaults to since-launch, not 30 days",
      len(d["visitors"]["series"]), d["days"])
check("and launch is reported", bool(d["launch"]), True)
json.dumps(d)                      # must be serialisable for /api/dashboard
print("  %-52s ok" % "collect() is JSON-serialisable")


print("")
print("== since launch, and the gap before counting existed ==")
import datetime as _dt

# usage_log dates the launch here because save_users() deletes and
# reinserts every user row - a backdated account inserted directly
# would be destroyed by the next save, which is exactly how the first
# version of this test fooled itself.
_launch = (_dt.date.today() - _dt.timedelta(days=20)).isoformat()
db._connect().execute(
    "INSERT INTO usage_log (owner_id, day, messages, credits) "
    "VALUES (?, ?, 1, 1)", ("seed", _launch))
# A path the earlier section did not use: site_visits is keyed on
# (day, path), and "/" today already has a row from the visit checks
# above.
db._connect().execute(
    "INSERT INTO site_visits (day, path, views) VALUES (?, '/since-test', 4)",
    (_dt.date.today().isoformat(),))
db._connect().commit()

check("launch_day finds the earliest record", db.launch_day(), _launch)
d2 = dashboard.collect()
check("span reaches back to launch", d2["days"], 21)
check("series covers every day of it",
      len(d2["visitors"]["series"]), 21)
check("first row IS launch day", d2["visitors"]["series"][0]["day"], _launch)
check("only days after counting began are counted",
      d2["visitors"]["counted_days"], 1)
check("uncounted days are flagged, not silently zero",
      d2["visitors"]["series"][0]["counted"], False)
check("a counted day says so", d2["visitors"]["series"][-1]["counted"], True)

print("")
print("== weekly buckets once a range outgrows a bar per day ==")
_old = (_dt.date.today() - _dt.timedelta(days=400)).isoformat()
db._connect().execute(
    "INSERT INTO usage_log (owner_id, day, messages, credits) "
    "VALUES (?, ?, 1, 1)", ("seed2", _old))
db._connect().commit()
before = sum(r["views"] for r in db.visit_series(since=db.launch_day()))
d3 = dashboard.collect()
check("long range switches to weeks", d3["bucket"], "week")
check("fewer buckets than days",
      len(d3["visitors"]["series"]) < d3["days"], True)
check("every series buckets the same way",
      len(d3["requests"]["series"]) == len(d3["visitors"]["series"])
      == len(d3["money"]["series"]), True)
check("bucketing loses no views",
      sum(r["views"] for r in d3["visitors"]["series"]), before)
check("an explicit short range stays daily",
      dashboard.collect(days=10)["bucket"], "day")
check("and honours the days it was given",
      dashboard.collect(days=10)["days"], 10)
json.dumps(d3)
print("  %-52s ok" % "still JSON-serialisable when bucketed")


print("")
print("== all-time visitors: browsers, not page loads ==")
# A fresh client is a fresh browser: no cookie, so the app mints a new
# session id for it. Reusing one client is the same browser returning.
_b1 = appmod.app.test_client()
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131"}
_before = db.visitors_all_time()["total"]
for _ in range(4):
    _b1.get("/", headers=_UA)
_b1.get("/privacy", headers=_UA)
check("four page loads from one browser count once",
      db.visitors_all_time()["total"] - _before, 1)

_b2 = appmod.app.test_client()
_b2.get("/", headers=_UA)
_b2.get("/", headers=_UA)
check("a second browser counts again",
      db.visitors_all_time()["total"] - _before, 2)

# The reason the count above needs two requests, and the reason it is
# not simply "every request with a session id". A scraper that keeps no
# cookies is handed a brand new guest id every single time, so counting
# the first request would file one crawler as a fresh person per page.
_after_two = db.visitors_all_time()["total"]
for _path in ("/", "/privacy", "/", "/privacy", "/"):
    appmod.app.test_client().get(_path, headers=_UA)
check("a cookie-less crawler is not five browsers",
      db.visitors_all_time()["total"], _after_two)
appmod.app.test_client().get("/", headers={"User-Agent": "Googlebot/2.1"})
check("a crawler does not count", db.visitors_all_time()["total"], _after_two)

_assets = appmod.app.test_client()
_assets.get("/static/style.css", headers=_UA)
_assets.get("/api/plans", headers=_UA)
check("assets and API polling do not count",
      db.visitors_all_time()["total"], _after_two)

# The whole reason this table exists: summing site_visitors would count
# one returning reader once per day, because that hash is re-salted
# nightly and cannot be summed.
check("it is a count of browsers, not of visitor-days",
      db.visitors_all_time()["total"]
      <= db._connect().execute(
          "SELECT COUNT(*) FROM site_visitors").fetchone()[0] + 2, True)
check("registered and guests add up to the total",
      db.visitors_all_time()["registered"] + db.visitors_all_time()["guests"],
      db.visitors_all_time()["total"])
check("the dashboard carries it",
      dashboard.collect()["visitors"]["all_time"]["total"],
      db.visitors_all_time()["total"])

print("")
print("== daily users: how many real people, today ==")
_TODAY = _dt.date.fromisoformat(db._today())


def _users_today():
    return db.daily_user_totals()["today"]


_users_before = _users_today()

# One browser, five page loads. One person.
_p1 = appmod.app.test_client()
for _ in range(5):
    _p1.get("/", headers=_UA)
check("five page loads from one browser are one user",
      _users_today() - _users_before, 1)

_p2 = appmod.app.test_client()
_p2.get("/", headers=_UA)
_p2.get("/privacy", headers=_UA)
check("a second browser is a second user",
      _users_today() - _users_before, 2)

# The same trap as the all-time count, and worse here: a per-day figure
# is the one the owner reads every morning.
for _path in ("/", "/privacy", "/", "/privacy", "/"):
    appmod.app.test_client().get(_path, headers=_UA)
check("a cookie-less crawler is not five people",
      _users_today() - _users_before, 2)

appmod.app.test_client().get("/", headers={"User-Agent": "SemrushBot/7"})
check("a declared bot is not a person",
      _users_today() - _users_before, 2)

# Somebody the site met before, back today. Backdating first_seen is
# exactly what the real table holds for anyone returning.
_key = db._connect().execute(
    "SELECT visitor_key FROM visitor_days WHERE day = ? LIMIT 1",
    (db._today(),)).fetchone()[0]
db._connect().execute("UPDATE visitors_seen SET first_seen = ? "
                      "WHERE visitor_key = ?",
                      ((_TODAY - _dt.timedelta(days=3)).isoformat()
                       + "T09:00:00", _key))
db._connect().commit()
_t = db.daily_user_totals()
check("someone first seen days ago counts as returning", _t["returning"], 1)
check("and the rest are new", _t["new"], _t["today"] - 1)
check("new and returning account for everybody",
      _t["new"] + _t["returning"], _t["today"])

# A signed-in person is not a guest id, and the split says so.
db.note_visitor("owner-account-id")
_t = db.daily_user_totals()
check("signed-in people are counted apart from guests", _t["signed_in"], 1)
check("guests and accounts add up",
      _t["guests"] + _t["signed_in"], _t["today"])

# THE POINT OF THE WHOLE TABLE: one person on three days is one active
# user. Summing daily figures would call them three.
_reader = "guest:regular-reader"
for _back in (0, 1, 2):
    db._connect().execute(
        "INSERT OR IGNORE INTO visitor_days (day, visitor_key) VALUES (?, ?)",
        ((_TODAY - _dt.timedelta(days=_back)).isoformat(), _reader))
db._connect().commit()
_others = db._connect().execute(
    "SELECT COUNT(DISTINCT visitor_key) FROM visitor_days "
    "WHERE day >= ? AND visitor_key <> ?",
    ((_TODAY - _dt.timedelta(days=6)).isoformat(), _reader)).fetchone()[0]
check("three days from one reader is one weekly active",
      db.daily_user_totals()["active_7"] - _others, 1)
check("a rolling window is never bigger than all time",
      db.daily_user_totals()["active_30"]
      <= db.visitors_all_time()["total"] + 3, True)

print("")
print("== weeks count people, not visits ==")
# A quiet stretch far enough back that nothing else here touches it.
_quiet = _TODAY - _dt.timedelta(days=140)
_quiet -= _dt.timedelta(days=_quiet.weekday())          # back to its Monday
for _off in (0, 3):
    db._connect().execute(
        "INSERT OR IGNORE INTO visitor_days (day, visitor_key) VALUES (?, ?)",
        ((_quiet + _dt.timedelta(days=_off)).isoformat(),
         "guest:twice-that-week"))
db._connect().commit()

_weekly = db.daily_user_series(since=_quiet.isoformat(), bucket="week")
check("a week bucket is labelled with its Monday",
      _weekly[0]["day"], _quiet.isoformat())
check("two visits in one week are ONE weekly user", _weekly[0]["users"], 1)
_daily = db.daily_user_series(since=_quiet.isoformat(), bucket="day")
check("the same person is two daily users",
      sum(r["users"] for r in _daily[:4]), 2)
check("which is the sum a weekly bucket must not report",
      sum(r["users"] for r in _daily[:7]) != _weekly[0]["users"], True)

_du = dashboard.collect()
check("the dashboard carries daily users",
      _du["users"]["today"], db.daily_user_totals()["today"])
check("its series buckets with the rest of the page",
      len(_du["users"]["series"]), len(_du["visitors"]["series"]))
check("peak is never zero", _du["users"]["peak"] >= 1, True)
json.dumps(_du)
print("  %-52s ok" % "still JSON-serialisable with users")


print("\n== the gate ==")
check("/dashboard 404s for a stranger", client.get("/dashboard").status_code,
      404)
check("/api/dashboard 404s for a stranger",
      client.get("/api/dashboard").status_code, 404)

# Sign in as the owner.
owner = appmod._create_user("owner@example.com", password_hash="x")
with client.session_transaction() as s:
    s["user_id"] = owner
r = client.get("/dashboard")
check("/dashboard renders for the owner", r.status_code, 200)
html = r.get_data(as_text=True)
check("page shows the visitor count", "Visitors today" in html, True)
check("page shows daily users", "Users today" in html, True)
check("page shows the weekly actives", "Active this week" in html, True)
check("page shows money", "You earned" in html, True)
check("no unrendered Jinja left", "{{" in html, False)
r = client.get("/api/dashboard")
check("/api/dashboard serves the owner", r.status_code, 200)

# Someone signed in, but not the owner.
other = appmod._create_user("someone@example.com", password_hash="x")
with client.session_transaction() as s:
    s["user_id"] = other
check("/dashboard 404s for a non-owner user",
      client.get("/dashboard").status_code, 404)

print("\n== the empty case ==")
# The first day, before anything has happened. This is what the owner
# actually sees first, and it is the render most likely to divide by
# zero.
db._connect().executescript(
    "DELETE FROM site_visits; DELETE FROM site_visitors; "
    "DELETE FROM visitor_days; DELETE FROM visitors_seen; "
    "DELETE FROM payments; DELETE FROM usage_log;")

# Checked before the request rather than in its HTML, because asking
# for /dashboard is itself a page view: by the time the page renders,
# the owner loading it is today's first user and the empty state is
# already gone. The template is rendered directly instead.
_empty = dashboard.collect()
check("no daily users on an empty database", _empty["users"]["today"], 0)
check("nothing to average", _empty["users"]["average"], 0)
check("and counting has not begun", _empty["users"]["began"], None)
with appmod.app.test_request_context("/"):
    _blank = appmod.render_template("dashboard.html", d=_empty)
check("the empty page says so instead of drawing zeroes",
      "Nothing counted yet" in _blank, True)
check("and renders without dividing by zero", "{{" in _blank, False)

with client.session_transaction() as s:
    s["user_id"] = owner
r = client.get("/dashboard")
check("renders with no data at all", r.status_code, 200)
html = r.get_data(as_text=True)
check("says there are no payments yet", "No payments yet" in html, True)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
