# -*- coding: utf-8 -*-
"""The two 500s that hit real visitors, and the races behind them.

    python check_concurrency.py

Both were intermittent, which is why neither showed up in ordinary use
and both showed up in the log:

    sqlite3.InterfaceError: bad parameter or other API misuse
        on /api/usage, from db.load_credits

    sqlite3.IntegrityError: UNIQUE constraint failed: users.email
        on /api/credits, from db.save_users

The first was one sqlite3 connection shared by eight gunicorn threads
with only the WRITES holding a lock. The second was two requests both
passing a "is this email taken" check before either had inserted.

Racy bugs do not fail reliably, so these checks hammer the paths with
real threads rather than asserting on a single call. A pass is not proof
there is no race; a failure is proof there is one.
"""
import os
import shutil
import sys
import tempfile
import threading

WORK = tempfile.mkdtemp(prefix="conc-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
sys.path.insert(0, os.path.abspath("."))

import app as appmod                                        # noqa: E402
import db                                                   # noqa: E402

THREADS = 8
ROUNDS = 40
FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-58s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


print("== a connection per thread ==")
seen = {}


def grab(i):
    seen[i] = id(db._connect())


ts = [threading.Thread(target=grab, args=(i,)) for i in range(THREADS)]
[t.start() for t in ts]
[t.join() for t in ts]
check("each thread gets its own connection",
      len(set(seen.values())), THREADS)
check("and the same one twice in a thread", db._connect() is db._connect(),
      True)

print("\n== reads racing writes (the /api/usage crash) ==")
errors = []


def reader():
    for _ in range(ROUNDS):
        try:
            db.load_credits("guest:reader")
            db.visit_totals()
            db.request_totals()
        except Exception as e:                              # noqa: BLE001
            errors.append("read: %r" % e)


def writer(n):
    for i in range(ROUNDS):
        try:
            db.save_credits("guest:w%d-%d" % (n, i),
                            {"balance": 10, "starting": 10, "plan": "free",
                             "last_refill": "2026-01-01T00:00:00+00:00"})
            db.record_visit("/", "203.0.113.%d" % (i % 250), "Mozilla/5.0")
        except Exception as e:                              # noqa: BLE001
            errors.append("write: %r" % e)


ts = ([threading.Thread(target=reader) for _ in range(4)]
      + [threading.Thread(target=writer, args=(n,)) for n in range(4)])
[t.start() for t in ts]
[t.join() for t in ts]
check("no InterfaceError under %d threads" % len(ts),
      [e for e in errors if "InterfaceError" in e], [])
check("no errors of any kind", errors, [])

print("\n== two signups for one address (the /api/credits crash) ==")
results = []
barrier = threading.Barrier(THREADS)


def racer():
    # Every thread waits, then goes at once - the whole point is to have
    # them inside the check-then-create window together.
    barrier.wait()
    try:
        results.append(appmod._find_or_create_user("race@example.com",
                                                   password_hash="x"))
    except Exception as e:                                  # noqa: BLE001
        results.append(("ERROR", repr(e)))


ts = [threading.Thread(target=racer) for _ in range(THREADS)]
[t.start() for t in ts]
[t.join() for t in ts]
uids = {r[0] for r in results}
created = [r for r in results if r[1] is True]
check("all %d threads agree on one account" % THREADS, len(uids), 1)
check("exactly one of them created it", len(created), 1)
check("the rest were told it already existed",
      len([r for r in results if r[1] is False]), THREADS - 1)
check("only one account exists for that address",
      len([u for u in appmod.USERS.values()
           if u.get("email") == "race@example.com"]), 1)

print("\n== and the save that used to break forever ==")
appmod.save_users()
check("saving works after the race", True, True)

# Force the state the race used to leave behind, and confirm it is now
# reported rather than thrown by the constraint after the DELETE.
appmod.USERS["forced-dup"] = dict(
    next(u for u in appmod.USERS.values()
         if u.get("email") == "race@example.com"),
    id="forced-dup")
try:
    db.save_users(appmod.USERS)
    check("a duplicate is refused", "no error", "an error")
except RuntimeError as e:
    check("a duplicate raises a named, readable error",
          "is on two accounts" in str(e), True)
    check("and names the address", "race@example.com" in str(e), True)
except Exception as e:                                      # noqa: BLE001
    check("raises RuntimeError, not the raw constraint", repr(e), "RuntimeError")
del appmod.USERS["forced-dup"]

check("data survived the refusal",
      len([r for r in db._connect().execute("SELECT email FROM users")]) > 0,
      True)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
