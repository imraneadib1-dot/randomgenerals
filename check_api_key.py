# -*- coding: utf-8 -*-
"""Can a signed-in account make a key for other apps, and does it work?

    python check_api_key.py           against a throwaway database
    python check_api_key.py --live    also ask randomgenerals.com whether
                                      it serves the button

The Blender add-on and MCP clients sign in with this key. The route to
issue one had shipped, but the "Create a key" button that calls it had
not: the live page had no way to reach the route, so nobody could get a
key and the add-on answered every Ask with "key rejected". The
last section here reads the page the app actually serves, so a button
that is missing from the template fails a check instead of a person.
"""
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="apikeytest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.abspath("."))
import app as appmod                                        # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL -> %r" % (got,)))
    if not ok:
        FAILED.append(label)


uid = appmod._create_user("keys@blender.example", password_hash="x")
signed_in = appmod.app.test_client()
with signed_in.session_transaction() as s:
    s["user_id"] = uid
anon = appmod.app.test_client()


def bearer(key):
    return {"Authorization": "Bearer " + key}


print("== issuing a key ==")
r = anon.post("/api/account/api-key")
check("signed out, refused", r.status_code, 401)
r = signed_in.post("/api/account/api-key")
check("signed in, issued", r.status_code, 200)
first = (r.get_json() or {}).get("api_key", "")
check("with the rg- prefix the add-on expects", first.startswith("rg-"), True)
check("and the /v1 URL to paste beside it",
      (r.get_json() or {}).get("base_url", "").endswith("/v1"), True)

print("\n== the key signs in to the API ==")
check("/v1/models accepts it", anon.get("/v1/models", headers=bearer(first)).status_code, 200)
check("a made-up key is refused",
      anon.get("/v1/models", headers=bearer("rg-made-up")).status_code, 401)

print("\n== a new key replaces the old one ==")
second = signed_in.post("/api/account/api-key").get_json()["api_key"]
check("the new one works", anon.get("/v1/models", headers=bearer(second)).status_code, 200)
check("the old one stops", anon.get("/v1/models", headers=bearer(first)).status_code, 401)

print("\n== revoking ==")
check("revoke answers", signed_in.delete("/api/account/api-key").status_code, 200)
check("and the key stops", anon.get("/v1/models", headers=bearer(second)).status_code, 401)

print("\n== the page the app serves has the button ==")
page = signed_in.get("/app").get_data(as_text=True)
check("Settings has the Create a key button", 'id="issueApiKey"' in page, True)
check("and the box the key appears in", 'id="apiKeyOut"' in page, True)
app_js = signed_in.get("/static/js/app.js").get_data(as_text=True)
check("app.js mounts the handler", "mountApiKey()" in app_js, True)
conn_js = signed_in.get("/static/js/connectors.js").get_data(as_text=True)
check("connectors.js posts to the route",
      'fetch("/api/account/api-key", { method: "POST" })' in conn_js, True)

if "--live" in sys.argv:
    import urllib.request
    print("\n== randomgenerals.com ==")

    req = urllib.request.Request("https://randomgenerals.com/app",
                                 headers={"User-Agent": "pip-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            live_page = resp.read().decode("utf-8", "replace")
    except OSError as e:
        live_page = ""
        print("  could not fetch the live page: %s" % e)
    # The module frontend replaced static/script.js on 2026-09-12. A live
    # page still loading script.js is running code from before then, and
    # no change made since has reached it.
    check("the live site runs the current frontend (js/app.js)",
          "js/app.js" in live_page, True)
    check("the live page has the Create a key button",
          'id="issueApiKey"' in live_page, True)

print("")
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
