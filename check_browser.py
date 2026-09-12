# -*- coding: utf-8 -*-
"""Does the front-end run - in a browser, against this server?

    python check_browser.py

Starts the real app on a spare port with a temp database and a FAKE
model channel (the local "ollama" streamer answers instantly, no
network), then drives it with check_browser.js in a headless Edge or
Chrome that is already on the machine - playwright-core drives the
browser, it does not download one.

WHY

Every other check runs Python. tsc proves the modules refer to names
that exist; nothing proved they RUN. A ReferenceError at load is a
blank app with one red line in a console nobody is watching, and a
module split can introduce one in a hundred quiet ways. This loads
the page, boots, switches bays, opens Settings, and sends a message
that streams back, and fails on any uncaught exception on the way.

Skips - with a message, exit 0 - when node, playwright-core or a
browser is missing, since those are dev-machine tools.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = tempfile.mkdtemp(prefix="browsertest-")
os.environ["DB_PATH"] = os.path.join(WORK, "t.db")
os.environ["SECRET_KEY"] = "test-only"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""

sys.path.insert(0, HERE)

if not os.path.exists(os.path.join(HERE, "node_modules", "playwright-core")):
    print("  (skipped: run `npm install` here first - dev-only, see package.json)")
    sys.exit(0)
if not shutil.which("node"):
    print("  (skipped: node is not on PATH)")
    sys.exit(0)

import app as appmod                                      # noqa: E402

# A channel that answers. The routing asks whether the local model is
# up before using it; it is not, so answer yes and stream a fake.
appmod.ollama_reachable = lambda: True
appmod.ollama_provider = lambda plan=None: {
    "id": "ollama", "label": "RandomGenerals", "available": True,
    "models": ["fake-model"], "model_info": [],
    "note": "a fake channel for the browser check"}
appmod._failover_chain = lambda provider, mode: []


def fake_stream(model, history, **kw):
    if kw.get("usage") is not None:
        kw["usage"]["eval_count"] = 12
    # Markdown on purpose: the check reads the rendered DOM for it.
    for piece in ("**Hello** from the fake model.\n\n",
                  "- one\n- two\n\n",
                  "The area is $x^2$.\n\n",
                  "```python\nprint(1)\n```\n"):
        yield piece


appmod.PROVIDER_STREAMERS["ollama"] = fake_stream

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


port = free_port()
server = threading.Thread(
    target=lambda: appmod.app.run(host="127.0.0.1", port=port, debug=False,
                                  use_reloader=False, threaded=True),
    daemon=True)
server.start()
url = "http://127.0.0.1:%d/app" % port
for _ in range(50):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.2)

print("== the browser ==")
code = subprocess.call(["node", os.path.join(HERE, "check_browser.js"), url],
                       cwd=HERE, shell=(os.name == "nt"))
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(code)
