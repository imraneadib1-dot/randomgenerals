# -*- coding: utf-8 -*-
"""Can a free account connect an app, and only as many as it should?

    python check_connectors.py

Connected apps moved from Pro-only to a per-plan cap: one on Free,
eight on Pro. That is two things to get right and one to not get wrong.

The one to not get wrong is the reason this file exists. The limit is
enforced server-side in the POST route, and it is ALSO advertised on the
plan cards and in the settings panel. Those are three separate readings
of the same number, and if they drift, the app promises one thing and
refuses another. Every check below reads features.py rather than a
literal, so the test cannot drift either.

Runs against a throwaway database and never makes an outbound request:
connector discovery is stubbed, because what is under test is the
gating, not connectors.py's HTTP handling.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="conntest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"

sys.path.insert(0, os.path.abspath("."))
import app as appmod                                        # noqa: E402
import connectors                                           # noqa: E402
import db                                                   # noqa: E402
import features                                             # noqa: E402

FREE_MAX = features.FEATURES[features.FREE]["max_connectors"]
PRO_MAX = features.FEATURES[features.PRO]["max_connectors"]
FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


# Discovery would fetch the URL. Stubbed: the gating is what is on test,
# and a test that reached the network would fail for unrelated reasons.
_n = {"i": 0}


def fake_discover(url, token=None):
    _n["i"] += 1
    return {"title": "App %d" % _n["i"], "kind": "openapi", "url": url,
            "base_url": url, "operations": [{"name": "get_thing"}]}, None


connectors.discover = fake_discover

client = appmod.app.test_client()


def sign_in(plan):
    """A FRESH client per identity, signed in as a new account.

    Not one client re-pointed at a second user. Doing that swaps
    user_id while the cookie still carries the first session's `sid`,
    and the app's own session-revocation check correctly throws the
    request out to a guest - so the second half of this file silently
    tested a signed-out visitor and read "free" for the wrong reason.
    That is the revocation feature working, not a bug to route around.
    """
    global client
    uid = appmod._create_user("%s@example.com" % plan, password_hash="x")
    # The app's own promotion path, rather than setting user["plan"] by
    # hand. current_account() gates on the CREDITS row, not the user
    # row, so setting only the latter leaves every route reading "free".
    appmod._apply_plan(appmod.USERS[uid], plan)
    client = appmod.app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = uid
    return uid


def add(url):
    return client.post("/api/connectors", json={"url": url})


print("== the free tier can connect an app at all ==")
check("free is no longer locked out",
      features.FEATURES[features.FREE]["external_connectors"], True)
check("free cap is a number, not False", isinstance(FREE_MAX, int), True)
check("pro cap is larger than free", PRO_MAX > FREE_MAX, True)

sign_in("free")
r = add("https://example.com/openapi.json")
check("first connection is accepted", r.status_code, 200)
listing = client.get("/api/connectors").get_json()
check("it is listed", len(listing["connectors"]), 1)
check("the cap is sent to the browser", listing["max"], FREE_MAX)
check("the plan is sent too", listing["plan"], "free")

print("\n== and is held to its cap ==")
r = add("https://other.example.com/openapi.json")
check("the one over the line is refused", r.status_code, 400)
body = r.get_json()
check("still only one stored",
      len(client.get("/api/connectors").get_json()["connectors"]), 1)
check("the refusal offers a way forward", body.get("upgrade"), True)
check("and does not read as a bug",
      "Free connects one app" in body.get("error", ""), True)
check("it names Pro's number",
      str(PRO_MAX) in body.get("error", ""), True)

print("\n== pro gets the larger allowance ==")
sign_in("pro")
codes = [add("https://p%d.example.com/openapi.json" % i).status_code
         for i in range(PRO_MAX)]
check("pro can add up to its cap", codes.count(200), PRO_MAX)
r = add("https://one-too-many.example.com/openapi.json")
check("and is refused past it", r.status_code, 400)
check("pro sees its own cap",
      client.get("/api/connectors").get_json()["max"], PRO_MAX)

print("\n== the plan cards say the same thing the API enforces ==")
perks = appmod.plan_perks()
free_text = " ".join(t for t, _ in perks["free"])
pro_text = " ".join(t for t, _ in perks["pro"])
check("free card mentions connecting an app",
      "onnect" in free_text, True)
check("free card states its real cap",
      str(FREE_MAX) in free_text, True)
check("pro card states its real cap", str(PRO_MAX) in pro_text, True)
check("no card still calls it Pro-only",
      "Pro feature" in free_text or "Pro feature" in pro_text, False)

print("\n== the model is actually offered the tools ==")
check("free tier passes the flag the tool loop checks",
      features.FEATURES[features.FREE]["external_connectors"], True)
check("public_flags carries the cap",
      features.public_flags("free")["max_connectors"], FREE_MAX)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
