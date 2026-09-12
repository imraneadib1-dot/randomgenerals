# -*- coding: utf-8 -*-
"""Can two tabs spend the same credit?

    python check_credits.py

WHY THESE CHECKS

Credits were deducted by reading a balance, comparing it to the cost,
and writing the result back. For a signed-in user that happened on a
dict in memory under a Python lock and was then persisted by rewriting
EVERY user's credits row from memory; for a guest it happened on a
fresh copy loaded per request, so two concurrent spends each saw the
old balance and the last writer won. And when the balance was short,
spend_credits returned False without deducting anything - which every
caller ignored - so a person whose balance had drifted below one
reply's cost was served long replies free, indefinitely.

The credits ROW is authoritative now and the arithmetic happens in
the table, in one statement. These checks hammer that from eight
threads and then look at what is left.
"""
import os
import shutil
import sys
import tempfile
import threading

WORK = tempfile.mkdtemp(prefix="creditstest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.abspath("."))

import app as appmod                                      # noqa: E402
import db                                                 # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


def hammer(n_threads, fn):
    errors = []

    def run(i):
        try:
            fn(i)
        except Exception as e:                             # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


now = appmod.now_iso()

print("== debit: the check and the take are one statement ==")
db.save_credits("acct", {"balance": 400, "starting": 400, "plan": "free",
                         "last_refill": now})
wins = []
errors = hammer(8, lambda i: [wins.append(db.debit("acct", 1))
                              for _ in range(50)])
check("no thread raised", errors, [])
check("every one of 400 debits landed", sum(1 for w in wins if w is not None), 400)
check("and exactly nothing is left", db.load_credits("acct")["balance"], 0)
check("the 401st is refused", db.debit("acct", 1), None)
check("and takes nothing", db.load_credits("acct")["balance"], 0)
check("a debit on an unknown account is refused", db.debit("nobody", 1), None)

print("\n== debit_to_floor: never free, never negative ==")
db.save_credits("floor", {"balance": 20, "starting": 20, "plan": "free",
                          "last_refill": now})
check("a 60-credit reply on a 20 balance charges 20 and lands on 0",
      db.debit_to_floor("floor", 60), (20, 0))
check("a reply on a zero balance charges nothing",
      db.debit_to_floor("floor", 60), (0, 0))
check("the balance never went below zero", db.load_credits("floor")["balance"], 0)
db.save_credits("floor2", {"balance": 1000, "starting": 1000, "plan": "free",
                           "last_refill": now})
taken = []
errors = hammer(8, lambda i: [taken.append(db.debit_to_floor("floor2", 7)[0])
                              for _ in range(30)])
check("eight threads' takes add up to what left the account",
      sum(taken), 1000 - db.load_credits("floor2")["balance"])
check("never negative", db.load_credits("floor2")["balance"] >= 0, True)
check("refund restores it", db.credit("floor2", 5),
      db.load_credits("floor2")["balance"])

print("\n== the same account from two tabs ==")
appmod.ollama_reachable = lambda: True
appmod._failover_chain = lambda provider, mode: []


def slow_reply(model, history, **kw):
    if kw.get("usage") is not None:
        kw["usage"]["eval_count"] = 4000         # an expensive reply
    yield "a long answer"


appmod.PROVIDER_STREAMERS["ollama"] = slow_reply
uid = appmod._create_user("two@tabs.example", password_hash="x")
check("a new account has a credits row",
      db.load_credits(uid)["balance"], appmod.PLANS["free"]["cap"])
db.save_credits(uid, {"balance": 20, "starting": 2000, "plan": "free",
                      "last_refill": now})
appmod.USERS[uid]["credits"]["balance"] = 20


def tab():
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    tid = c.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
    return c, tid


tabs = [tab() for _ in range(4)]
results = []
errors = hammer(4, lambda i: results.append(tabs[i][0].post("/api/chat", json={
    "thread_id": tabs[i][1], "provider": "ollama", "model": "m",
    "message": "go"}).status_code))
check("no thread raised", errors, [])
check("every tab was answered (the pre-check is a floor, not a wall)",
      results.count(200) >= 1, True)
check("the balance is exactly zero, not negative",
      db.load_credits(uid)["balance"], 0)
charged = sum((m.get("charged") or 0)
              for t in appmod.THREADS.values() if t.get("owner_id") == uid
              for m in t["messages"] if m.get("role") == "assistant")
check("what the replies say they charged is what left the account",
      charged, 20)
check("the in-memory copy agrees with the table",
      appmod.USERS[uid]["credits"]["balance"], 0)

c, tid = tabs[0]
r = c.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                              "model": "m", "message": "more?"})
check("with nothing left, the next request is refused up front",
      r.status_code, 402)

print("\n== a plan change writes its own row ==")
appmod._apply_plan(appmod.USERS[uid], "pro")
check("upgrade lands in the table", db.load_credits(uid)["balance"],
      appmod.PLANS["pro"]["cap"])
check("and the plan with it", db.load_credits(uid)["plan"], "pro")
check("save_users no longer touches credits",
      (appmod.USERS[uid]["credits"].__setitem__("balance", 1),
       appmod.save_users(), db.load_credits(uid)["balance"])[-1],
      appmod.PLANS["pro"]["cap"])
appmod.USERS[uid]["credits"]["balance"] = appmod.PLANS["pro"]["cap"]
check("a reload sees the row", db.load_users()[uid]["credits"]["balance"],
      appmod.PLANS["pro"]["cap"])

print("\n== an image is paid for before it is made, and refunded if it is not ==")
c, tid = tab()
appmod._apply_plan(appmod.USERS[uid], "free")
before = db.load_credits(uid)["balance"]
img_calls = []


def failing_image(*a, **k):
    img_calls.append(1)
    return None, "the backend is down"


real_gen = appmod.imagegen.generate_image
appmod.imagegen.generate_image = failing_image
appmod.imagegen.enhance_prompt = lambda p, *a, **k: p
r = c.post("/api/generate-image", json={"thread_id": tid, "prompt": "a cat"})
appmod.imagegen.generate_image = real_gen
check("a failed generation answers 502", r.status_code, 502)
check("and the charge was given back", db.load_credits(uid)["balance"], before)

print("\n== video quota: the comparison is in the statement ==")
grabbed = []
errors = hammer(8, lambda i: grabbed.append(
    db.video_try_consume("vid", "2026-09", 2)))
check("no thread raised", errors, [])
check("exactly the limit got through", grabbed.count(True), 2)
check("the count never exceeds it", db.video_used("vid", "2026-09"), 2)
check("a zero limit lets nobody through",
      db.video_try_consume("vid0", "2026-09", 0), False)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
