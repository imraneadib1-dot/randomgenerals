#!/usr/bin/env python3
"""One-shot PythonAnywhere deployment. Run this INSIDE a PA Bash console:

    cd ~/randomgenerals && git pull && python deploy/pa_setup.py

It does everything the Web tab does - creates the web app, points it at
the virtualenv, installs the WSGI file, maps static files and reloads -
by calling PythonAnywhere's API.

The API token comes from $API_TOKEN, which PythonAnywhere pre-populates
in every console. Nothing secret has to be typed, pasted or shared.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://www.pythonanywhere.com"
PROJECT = os.path.expanduser("~/randomgenerals")
VENV = os.path.expanduser("~/.virtualenvs/randomgenerals")

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
DIM = "\033[2m"
RESET = "\033[0m"


def ok(msg):
    print("  " + GREEN + "ok" + RESET + "   " + msg)


def warn(msg):
    print("  " + YELLOW + "!" + RESET + "    " + msg)


def die(msg):
    print("  " + RED + "fail" + RESET + " " + msg + "\n")
    sys.exit(1)


def step(msg):
    print("\n" + msg)


def api(method, path, data=None, token=None):
    """Call the PythonAnywhere API. Returns (status, parsed_body)."""
    url = HOST + "/api/v0" + path
    body = None
    headers = {"Authorization": "Token " + token}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:400]}
    except urllib.error.URLError as e:
        die("could not reach " + HOST + ": " + str(e.reason))


def load_env(path):
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    values[k.strip()] = v.strip()
    return values


def main():
    print("\n" + DIM + "RandomGenerals -> PythonAnywhere" + RESET)

    # ------------------------------------------------------- token / user
    step("Credentials")
    token = os.environ.get("API_TOKEN")
    if not token:
        die("$API_TOKEN is not set.\n"
            "       This script must run inside a PythonAnywhere Bash\n"
            "       console, where the token is provided automatically.\n"
            "       If you are in one, go to the Account page -> API Token\n"
            "       tab, click 'Create a new API token', then open a NEW\n"
            "       console and run this again.")
    ok("API token found in environment")

    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        die("cannot determine your username from $USER")
    ok("user: " + username)

    domain = username.lower() + ".pythonanywhere.com"
    wsgi_path = "/var/www/" + domain.replace(".", "_") + "_wsgi.py"

    status, _ = api("GET", "/user/" + username + "/cpu/", token=token)
    if status == 401:
        die("the API token was rejected. Create a fresh one on the Account\n"
            "       page -> API Token tab, then open a NEW console.")
    if status == 200:
        ok("token works")
    else:
        warn("unexpected response checking token: HTTP " + str(status))

    # ------------------------------------------------------- project files
    step("Checking the project")
    if not os.path.isdir(PROJECT):
        die(PROJECT + " does not exist - did the git clone run?")
    ok("project at " + PROJECT)

    if not os.path.isdir(VENV):
        die("virtualenv missing at " + VENV + "\n"
            "       Create it with:\n"
            "         mkvirtualenv --python=/usr/local/bin/python3.11 "
            "randomgenerals\n"
            "         pip install -r ~/randomgenerals/requirements-cloud.txt")
    ok("virtualenv at " + VENV)

    # ------------------------------------------------------- secrets
    step("Secrets")
    env_file = os.path.join(PROJECT, ".env")
    values = load_env(env_file)

    if not values.get("SECRET_KEY"):
        import secrets as _secrets
        values["SECRET_KEY"] = _secrets.token_hex(32)
        ok("generated a SECRET_KEY")
    else:
        ok("SECRET_KEY present (" + str(len(values["SECRET_KEY"])) + " chars)")

    # No AI provider key is requested any more. The cloud provider was
    # removed, so this app answers only from Ollama on the machine it
    # runs on - and PythonAnywhere has no Ollama. Every page will serve
    # correctly and prompts will report the local AI as unavailable.
    # Said plainly at the end of this script rather than left to be
    # discovered.
    values.pop("GROQ_API_KEY", None)

    # ALLOW_MOCK_UPGRADE grants Pro without paying. Never on a public site.
    values.pop("ALLOW_MOCK_UPGRADE", None)

    with open(env_file, "w", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(k + "=" + v + "\n")
    os.chmod(env_file, 0o600)
    ok("wrote " + env_file + " (chmod 600, " + str(len(values)) + " values)")

    # ------------------------------------------------------- web app
    step("Web app")
    status, _ = api("GET", "/user/" + username + "/webapps/" + domain + "/",
                    token=token)
    if status == 200:
        ok(domain + " already exists - updating it")
    else:
        status, resp = api("POST", "/user/" + username + "/webapps/",
                           {"domain_name": domain, "python_version": "3.11"},
                           token=token)
        if status in (200, 201):
            ok("created " + domain)
        else:
            die("could not create the web app (HTTP " + str(status) + "): "
                + json.dumps(resp)[:300])

    status, resp = api("PATCH",
                       "/user/" + username + "/webapps/" + domain + "/",
                       {"virtualenv_path": VENV}, token=token)
    if status in (200, 201):
        ok("virtualenv linked")
    else:
        warn("could not set virtualenv (HTTP " + str(status) + "): "
             + json.dumps(resp)[:200])

    # ------------------------------------------------------- wsgi file
    step("WSGI configuration")
    template = os.path.join(PROJECT, "deploy", "pythonanywhere_wsgi.py")
    if not os.path.exists(template):
        die("missing " + template)
    with open(template, encoding="utf-8") as f:
        content = f.read()
    content = content.replace('USERNAME = "USERNAME"',
                              'USERNAME = "' + username + '"')
    try:
        with open(wsgi_path, "w", encoding="utf-8") as f:
            f.write(content)
        ok("wrote " + wsgi_path)
    except (PermissionError, OSError) as e:
        die("cannot write " + wsgi_path + " (" + str(e) + ")\n"
            "       Paste deploy/pythonanywhere_wsgi.py in by hand via the\n"
            "       Web tab, changing USERNAME to " + username + ".")

    # ------------------------------------------------------- static files
    step("Static files")
    status, resp = api("POST",
                       "/user/" + username + "/webapps/" + domain
                       + "/static_files/",
                       {"url": "/static/",
                        "path": os.path.join(PROJECT, "static") + "/"},
                       token=token)
    if status in (200, 201):
        ok("/static/ mapped")
    elif status == 400 and "already" in json.dumps(resp).lower():
        ok("/static/ already mapped")
    else:
        warn("static mapping returned HTTP " + str(status) + ": "
             + json.dumps(resp)[:200])

    # ------------------------------------------------------- reload
    step("Starting it up")
    status, resp = api("POST",
                       "/user/" + username + "/webapps/" + domain + "/reload/",
                       token=token)
    if status in (200, 201):
        ok("reloaded")
    else:
        warn("reload returned HTTP " + str(status) + ": "
             + json.dumps(resp)[:200])

    # ------------------------------------------------------- verify
    step("Checking it works")
    url = "https://" + domain
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=45) as r:
            health = json.loads(r.read().decode())
            code = r.status
        ok("/api/health -> " + str(code))
        mode = health.get("mode")
        print("       mode=" + str(mode))
        if not health.get("compute", {}).get("local_models"):
            warn("no local models here - expected, since PythonAnywhere has")
            warn("no GPU and no Ollama. Pages will serve, but a prompt will")
            warn("answer 'the local AI isn't running'. Adding a cloud")
            warn("provider back is what would make it answer.")
    except Exception as e:
        warn("could not reach " + url + "/api/health yet: " + str(e))
        warn("Give it 30 seconds and open the URL in your browser.")
        warn("If it errors, the Web tab's error log has the traceback.")

    print("\n" + GREEN + "  Your site: " + url + RESET)
    print(DIM + "  Free web apps expire every 3 months - PythonAnywhere")
    print("  emails a link to press to keep it alive." + RESET + "\n")


if __name__ == "__main__":
    main()
