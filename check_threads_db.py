# -*- coding: utf-8 -*-
"""Can eight people talk at once without losing a message?

    python check_threads_db.py

WHY THIS EXISTS

Every message used to persist by rewriting the whole threads table
from the in-memory dict: a DELETE and a reinsert of every user's every
conversation, per message, while iterating a dict that the other
seven request threads were appending to. That is a "dictionary changed
size during iteration" - or a half-appended list serialised - waiting
for enough traffic to happen, and nothing in the checks exercised it.

Now one thread is written at a time, its row snapshotted under a lock.
This hammers that from eight threads and then reads the table back
cold, the way a restart would.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

WORK = tempfile.mkdtemp(prefix="threadtest-")
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


WRITERS = 8
ROUNDS = 40

print("== eight writers, one reader, no lock of their own ==")
threads = {}
for i in range(WRITERS):
    tid = "t%d" % i
    threads[tid] = {"id": tid, "title": "thread %d" % i, "messages": [],
                    "updated": appmod.now_iso(), "mode": "chat",
                    "owner_id": "owner%d" % (i % 2)}
with appmod.DATA_LOCK:
    appmod.THREADS.update(threads)
errors = []


def writer(i):
    t = threads["t%d" % i]
    try:
        for n in range(ROUNDS):
            with appmod.DATA_LOCK:
                t["messages"].append({"role": "user", "type": "text",
                                      "kind": "text",
                                      "content": "w%d m%d" % (i, n)})
                t["updated"] = appmod.now_iso()
            appmod.save_thread(t)
    except Exception as e:                                 # noqa: BLE001
        errors.append("writer %d: %r" % (i, e))


def reader():
    # What a request listing threads does: walks the whole dict while
    # the writers are appending to its members.
    try:
        for _ in range(ROUNDS * 4):
            with appmod.DATA_LOCK:
                _ = [(tid, len(t["messages"]))
                     for tid, t in appmod.THREADS.items()]
            json.dumps(list(threads["t0"]["messages"]))
    except Exception as e:                                 # noqa: BLE001
        errors.append("reader: %r" % e)


ts = [threading.Thread(target=writer, args=(i,)) for i in range(WRITERS)]
ts.append(threading.Thread(target=reader))
for t in ts:
    t.start()
for t in ts:
    t.join()

check("no thread raised", errors, [])
cold = db.load_threads()
check("every thread is in the table", sorted(cold), sorted(threads))
check("and every message survived",
      [len(cold["t%d" % i]["messages"]) for i in range(WRITERS)],
      [ROUNDS] * WRITERS)
check("in order",
      cold["t3"]["messages"][-1]["content"], "w3 m%d" % (ROUNDS - 1))

print("\n== one row at a time ==")
before = db.load_threads()
with appmod.DATA_LOCK:
    threads["t1"]["title"] = "renamed"
appmod.save_thread(threads["t1"])
after = db.load_threads()
check("the changed row changed", after["t1"]["title"], "renamed")
check("no other row was touched",
      {k: v for k, v in after.items() if k != "t1"},
      {k: v for k, v in before.items() if k != "t1"})

db.delete_thread("t2")
check("a delete removes its row only",
      sorted(db.load_threads()), sorted(k for k in threads if k != "t2"))

moved = db.reassign_threads("owner1", "newowner")
check("a guest signing in keeps their threads",
      moved, sum(1 for k in threads if k != "t2"
                 and threads[k]["owner_id"] == "owner1"))
check("and only theirs",
      {t["owner_id"] for t in db.load_threads().values()},
      {"owner0", "newowner"})

gone = db.delete_threads_for("newowner")
check("clearing an owner's history removes theirs", gone, moved)
check("and nobody else's",
      all(t["owner_id"] == "owner0" for t in db.load_threads().values()), True)

print("\n== a save for a thread that was deleted meanwhile is a no-op ==")
orphan = {"id": "gone", "title": "x", "messages": [], "updated": "", "mode": "chat"}
appmod.save_thread(orphan)
check("nothing written for an unregistered thread",
      "gone" in db.load_threads(), False)

print("\n== the app's own routes use it ==")
appmod.ollama_reachable = lambda: True


def fake(model, history, **kw):
    yield "reply"


appmod.PROVIDER_STREAMERS["ollama"] = fake
client = appmod.app.test_client()
tid = client.post("/api/threads", json={"mode": "chat"}).get_json()["id"]
client.post("/api/chat", json={"thread_id": tid, "provider": "ollama",
                               "model": "m", "message": "hello"})
row = db.load_threads().get(tid)
check("a chat lands in the table", bool(row), True)
check("both turns", [m["role"] for m in row["messages"]], ["user", "assistant"])
check("the thread carries its id in the API",
      client.get("/api/threads/%s" % tid).get_json().get("id"), tid)
client.delete("/api/threads/%s" % tid)
check("and deleting it removes the row", tid in db.load_threads(), False)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
