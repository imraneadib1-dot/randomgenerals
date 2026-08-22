"""WSGI entry point for PythonAnywhere.

PythonAnywhere doesn't run `python app.py` or gunicorn - it imports a
WSGI callable from a file it owns at

    /var/www/<username>_pythonanywhere_com_wsgi.py

That file is created for you when you add the web app, prefilled with a
Django example. Replace its entire contents with this, changing USERNAME
below, then hit Reload on the Web tab.

Keep this copy in the repo as the source of truth: the /var/www one
isn't in git, so without this there'd be no record of how the site is
wired up.
"""
import os
import sys

USERNAME = "USERNAME"                      # <-- your PythonAnywhere username
PROJECT = f"/home/{USERNAME}/randomgenerals"

# The app imports its own modules (db, features, moderation...) as
# top-level names, so the project directory has to be importable.
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# app.py calls load_dotenv() at import, but python-dotenv looks in the
# *current working directory*, and under uWSGI that is /var/www - not the
# project. Pointing at the file explicitly is what makes GROQ_API_KEY and
# SECRET_KEY actually load.
from dotenv import load_dotenv                    # noqa: E402
load_dotenv(os.path.join(PROJECT, ".env"))

# Keep the database on the persistent home disk. PythonAnywhere gives
# free accounts 512MB there that survives reloads, which is the one
# thing Render's free tier cannot do.
os.environ.setdefault("DB_PATH", os.path.join(PROJECT, "app.db"))

# Behind PythonAnywhere's nginx, so TLS always terminates in front of us
# and session cookies can safely be marked Secure.
os.environ.setdefault("FORCE_HTTPS_COOKIES", "1")

# PythonAnywhere always serves through their nginx, so the
# X-Forwarded-* headers are set by them and safe to trust. Without
# this, Google sign-in builds its redirect_uri from the internal
# address and Google rejects it as a mismatch.
os.environ.setdefault("TRUST_PROXY", "1")
os.environ.setdefault("APP_DEBUG", "0")

# Never set ALLOW_MOCK_UPGRADE here. It grants Pro without paying - fine
# on a laptop, a free-Pro button on a public site.
os.environ.pop("ALLOW_MOCK_UPGRADE", None)

from app import app as application            # noqa: E402,F401
