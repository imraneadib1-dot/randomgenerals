# -*- coding: utf-8 -*-
"""Why is there no "Daily users" section on the dashboard?

    python3 check_daily_users.py          # run this ON THE SERVER

WHY THIS EXISTS

"I don't see it" has four completely different causes and they look
identical from a browser:

  1. this checkout does not have the feature - the deploy never ran
  2. the checkout has it but the running service is older, because a
     pull without a restart leaves the old process serving from memory
  3. the service is new but the database has no visitor_days table
  4. everything is correct and the table is simply empty, because a
     person is counted on their SECOND page view and nobody has made
     one yet today

Only the last of those is nothing to fix, and it is indistinguishable
from the other three by looking at the page. So this looks at the
checkout, the process, and the database in that order and says which
one it is.

deploy.sh already verifies the running site by grepping /app for
strings that only exist in new code. It cannot do that here: the
dashboard 404s for anybody who is not the owner, including curl.
"""
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROBLEMS = []


def say(label, detail=""):
    print("  %-34s %s" % (label, detail))


def bad(label, detail):
    say(label, detail)
    PROBLEMS.append("%s: %s" % (label.strip(), detail))


def newest_source():
    """-> mtime of the newest file the running service would have read."""
    times = []
    for name in ("app.py", "db.py", "dashboard.py",
                 os.path.join("templates", "dashboard.html")):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            times.append(os.path.getmtime(path))
    return max(times) if times else 0


print("== 1. does this checkout have the feature ==")
have_code = have_page = False
try:
    with open(os.path.join(HERE, "db.py"), encoding="utf-8") as fh:
        have_code = "visitor_days" in fh.read()
    with open(os.path.join(HERE, "templates", "dashboard.html"),
              encoding="utf-8") as fh:
        have_page = "Daily users" in fh.read()
except OSError as exc:
    bad("could not read the source", str(exc))

say("db.py knows visitor_days", "yes" if have_code else "NO")
say("dashboard.html has the section", "yes" if have_page else "NO")

try:
    commit = subprocess.check_output(
        ["git", "log", "--oneline", "-1"], cwd=HERE,
        stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=HERE,
        stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()
    say("checkout", "%s  (%s)" % (commit, branch))
except Exception:                              # noqa: BLE001 - not a
    say("checkout", "not a git checkout")      # git tree, still usable

if not (have_code and have_page):
    print("")
    print("THE CODE IS NOT HERE. The deploy has not run in this")
    print("directory. On the server:")
    print("")
    print("    bash %s/deploy.sh" % HERE)
    sys.exit(1)


print("")
print("== 2. is the RUNNING service this code, or older ==")
service = os.environ.get("SERVICE", "randomgenerals")
started = None
try:
    out = subprocess.check_output(
        ["systemctl", "show", service, "--property=ActiveEnterTimestampMonotonic",
         "--value"], stderr=subprocess.DEVNULL).decode().strip()
    if out and out != "0":
        # Monotonic microseconds since boot, against this machine's
        # uptime - which avoids parsing systemd's localised timestamps.
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        started = __import__("time").time() - (uptime - int(out) / 1e6)
except Exception:                              # noqa: BLE001 - no
    pass                                       # systemd, or not Linux

if started is None:
    say("service start time", "unknown (no systemd here)")
    say("", "if this is the server, restart it anyway:")
    say("", "sudo systemctl restart %s" % service)
else:
    import datetime
    fmt = "%Y-%m-%d %H:%M"
    source = newest_source()
    say("%s started" % service,
        datetime.datetime.fromtimestamp(started).strftime(fmt))
    say("newest source file",
        datetime.datetime.fromtimestamp(source).strftime(fmt))
    if source > started:
        bad("  the process is OLDER than the code",
            "it is still serving the previous version")
        print("")
        print("    sudo systemctl restart %s" % service)
    else:
        say("verdict", "the running process has this code")


print("")
print("== 3. does the database have the table ==")
sys.path.insert(0, HERE)
import db                                                   # noqa: E402

say("database file", db.DB_PATH)
if not os.path.exists(db.DB_PATH):
    bad("the file does not exist", "nothing has ever written to it")
    print("")
    print("  The service writes to whatever DB_PATH it was started")
    print("  with. If it is set in the unit file or a .env, this script")
    print("  has to see the same value:")
    print("")
    print("    DB_PATH=/path/to/app.db python3 check_daily_users.py")
    sys.exit(1)

conn = sqlite3.connect("file:%s?mode=ro" % db.DB_PATH, uri=True)
tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table'")}
for name in ("site_visits", "site_visitors", "visitors_seen", "visitor_days"):
    say(name, "present" if name in tables else "MISSING")

if "visitor_days" not in tables:
    print("")
    print("  The table is created at startup by the schema in db.py, so")
    print("  a restart makes it. Reading the dashboard once also makes")
    print("  it - the read path re-applies the schema when it finds a")
    print("  table missing rather than returning a 500.")


print("")
print("== 4. is anything in it ==")
today = 0
if "visitor_days" in tables:
    rows = conn.execute("SELECT COUNT(*) FROM visitor_days").fetchone()[0]
    days = conn.execute(
        "SELECT COUNT(DISTINCT day) FROM visitor_days").fetchone()[0]
    first = conn.execute("SELECT MIN(day) FROM visitor_days").fetchone()[0]
    today = conn.execute("SELECT COUNT(*) FROM visitor_days WHERE day = ?",
                         (db._today(),)).fetchone()[0]
    say("rows", "%d across %d day(s)" % (rows, days))
    say("counting began", first or "never")
    say("people today", str(today))
else:
    say("rows", "none - the table is not there yet")

# Always, even with no table: views today with no users is the single
# most likely reason for an empty chart on a day the deploy landed, and
# it is the one thing here that is not a fault.
views = conn.execute("SELECT COALESCE(SUM(views), 0) FROM site_visits "
                     "WHERE day = ?", (db._today(),)).fetchone()[0]
say("page views today", str(views))
if views and not today:
    print("")
    print("  Views but no users is the EXPECTED first day. Somebody is")
    print("  counted once they come back with the session cookie they")
    print("  were given, so a single-page visit is a view and a visitor")
    print("  but not yet a user. Open the site, then click through to a")
    print("  second page, and this becomes 1.")


print("")
print("== 5. what the page would print right now ==")
try:
    import dashboard                                        # noqa: E402
    users = dashboard.collect()["users"]
    for key in ("today", "yesterday", "new_today", "returning_today",
                "signed_in_today", "active_7", "active_30", "average",
                "began"):
        say(key, str(users[key]))
except Exception as exc:                       # noqa: BLE001
    bad("the dashboard could not build", "%s: %s" % (type(exc).__name__, exc))


# Section 5 does a real read through db.py, and that read re-applies
# the schema when it finds a table missing - so a table reported gone
# above may exist by now. Saying so beats leaving the two sections
# contradicting each other.
if "visitor_days" not in tables:
    conn.close()
    back = sqlite3.connect("file:%s?mode=ro" % db.DB_PATH, uri=True)
    if back.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'visitor_days'").fetchone():
        print("")
        print("  The table was missing and has just been created by this")
        print("  run. It starts empty: counting begins from now, and")
        print("  there is no way to fill in the days before it existed.")
    else:
        bad("visitor_days is still missing",
            "the schema could not create it - check the database is "
            "writable by the service user")
    back.close()

print("")
if PROBLEMS:
    print("%d thing(s) to fix:" % len(PROBLEMS))
    for p in PROBLEMS:
        print("  - %s" % p)
    sys.exit(1)

print("Nothing is wrong with the plumbing. The section is on")
print("/dashboard between \"Distinct visitors\" and \"Most visited")
print("pages\" - it is a section on that page, not a separate tab.")
sys.exit(0)
