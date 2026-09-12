# -*- coding: utf-8 -*-
"""Is the front-end served the way the page says it is?

    python check_frontend.py

Three things, in the order they would break:

  1. every file under static/js parses as an ES module (node --check),
     because a syntax error in a module is a blank app with one line in
     the console
  2. tsc --checkJs passes - an undeclared name, a wrong call, a
     property that exists on nothing (dev-only; skipped with a warning
     if `npm install` has not been run here)
  3. the rendered /app loads the module the way the deploy expects:
     type="module", a versioned URL, no reference to the file the
     script used to be

Nothing here needs a browser or a network.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = tempfile.mkdtemp(prefix="fronttest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, HERE)

FAILED = []


def check(label, got, want):
    ok = got == want
    print("  %-56s %s" % (label, "ok" if ok else "FAIL   (got %r)" % (got,)))
    if not ok:
        FAILED.append("%s: got %r, wanted %r" % (label, got, want))


def run(cmd, **kw):
    """-> (exit code, combined output). Never raises on a missing tool."""
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           shell=(os.name == "nt"), **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except OSError as e:
        return 127, str(e)


JS_DIR = os.path.join(HERE, "static", "js")
modules = sorted(
    os.path.relpath(os.path.join(root, f), HERE)
    for root, _dirs, files in os.walk(JS_DIR)
    for f in files if f.endswith(".js"))

print("== every module parses ==")
check("there are modules to check", len(modules) >= 1, True)
for rel in modules:
    # node --check parses a .js file as CommonJS, which is looser than a
    # module (no strict mode). A copy with an .mjs suffix is parsed as
    # the module the browser will see.
    tmp = os.path.join(WORK, os.path.basename(rel) + ".mjs")
    shutil.copyfile(os.path.join(HERE, rel), tmp)
    code, out = run(["node", "--check", tmp])
    check("%s parses as a module" % rel, code, 0)
    if code:
        print("    " + out.strip().replace("\n", "\n    ")[:600])

print("\n== the type check ==")
tsc = os.path.join(HERE, "node_modules", ".bin",
                   "tsc.cmd" if os.name == "nt" else "tsc")
if not os.path.exists(tsc):
    print("  (skipped: run `npm install` here first - dev-only, see package.json)")
else:
    code, out = run([tsc, "-p", "jsconfig.json"])
    check("tsc --checkJs is clean", code, 0)
    if code:
        print("    " + out.strip().replace("\n", "\n    ")[:1500])

print("\n== every import points at a file that exists ==")
import re                                                 # noqa: E402
IMPORT_RE = re.compile(r'^\s*import\s[^;]*?from\s+"\./([^"]+)"', re.M)
missing = []
for rel in modules:
    text = open(os.path.join(HERE, rel), encoding="utf-8").read()
    for target in IMPORT_RE.findall(text):
        if not os.path.exists(os.path.join(JS_DIR, target)):
            missing.append("%s -> %s" % (rel, target))
check("no module imports a file that is not there", missing, [])

print("\n== the page loads it as a module ==")
import json                                               # noqa: E402
import app as appmod                                      # noqa: E402
client = appmod.app.test_client()
html = client.get("/app").get_data(as_text=True)
check("the app script is a module",
      'type="module" src="/static/js/app.js?v=' in html, True)
m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
check("an import map is in the page", bool(m), True)
imports = json.loads(m.group(1))["imports"] if m else {}
expected = {"/static/js/" + os.path.basename(r) for r in modules}
check("it covers every module", sorted(expected - set(imports)), [])
check("with versioned targets",
      all(v.startswith(k + "?v=") for k, v in imports.items()), True)
check("and precedes the module script",
      html.find('type="importmap"') < html.find('type="module" src="/static/js/'),
      True)
r = client.get("/static/js/app.js")
check("modules answer", r.status_code, 200)
check("and ask browsers without import maps to revalidate",
      r.headers.get("Cache-Control"), "no-cache")
check("the old path is gone from the page",
      "static/script.js" in html, False)
check("and from disk",
      os.path.exists(os.path.join(HERE, "static", "script.js")), False)
check("the build marker is present", 'name="rg-build"' in html, True)
head = html.split("</head>")[0]
check("the theme bootstrap runs in the head, before the stylesheet",
      head.find('localStorage.getItem("theme")') != -1
      and head.find('localStorage.getItem("theme")') < head.find("style.css"),
      True)

print("")
if FAILED:
    print("%d FAILED:" % len(FAILED))
    for f in FAILED:
        print("  - %s" % f)
else:
    print("All checks passed.")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAILED else 0)
