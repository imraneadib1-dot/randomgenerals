from flask import (Flask, render_template, request, Response, jsonify,
                   session, stream_with_context, redirect, url_for, abort,
                   send_from_directory, send_file)
from flask.sessions import SecureCookieSessionInterface
from dotenv import load_dotenv
import hashlib
import hmac
import importlib.util
import secrets
import subprocess
import time
import json
import os
import re
import uuid
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
import threading
import datetime
import urllib.parse
from typing import Any

import requests
from werkzeug.security import generate_password_hash, check_password_hash

# Reads .env in the project root. This has to happen before the local
# imports below, not merely before the os.environ.get() calls further
# down this file: imagegen and paddle_billing both read their keys at
# module level, so importing them first meant they ran against an
# environment .env had not been applied to yet.
#
# The symptom is quiet and confusing - the key is correct, a standalone
# check confirms it, and the app still behaves as though nothing is
# configured.
load_dotenv()

import db  # noqa: E402  SQLite persistence - see db.py for the schema and why
import websearch  # noqa: E402  keyless web search - see websearch.py
import searchdb  # noqa: E402  our own crawled index - see searchdb.py
from markupsafe import escape  # noqa: E402
import attachments  # noqa: E402  upload handling/text extraction
import imagegen  # noqa: E402  local text-to-image generation
import codeexec  # noqa: E402  sandboxed Python execution - see codeexec.py
import moderation  # noqa: E402  content filtering - see moderation.py
import features  # noqa: E402  per-tier feature flags - see features.py
import stats  # noqa: E402  owner-only usage figures - see stats.py
import dashboard  # noqa: E402  visitors, money, requests - see dashboard.py
import providers  # noqa: E402  how every channel fails - see providers.py
from ratelimit import Limiter, limited  # noqa: E402  per-caller limits
import pip_api  # noqa: E402  pip 1.0, this app's own model - see pip_api.py
import pip_identity  # noqa: E402  who pip 1.0 is - see pip_identity.py
import openai_api  # noqa: E402  OpenAI-compatible API - see openai_api.py
import connectors  # noqa: E402  pasted links, turned into tools
import keystore  # noqa: E402  provider-key encryption + TOTP
import openrouter_api  # noqa: E402  DeepSeek for the code bay - see openrouter_api.py
import google_api  # noqa: E402  Gemma 4 31B on Google AI Studio - see google_api.py
import groq_api  # noqa: E402  the fast channel - see groq_api.py
import imagesearch  # noqa: E402  real photos for generated sites
import sitebuilder  # noqa: E402  plans and writes whole website projects
import paddle_billing  # noqa: E402  subscriptions where Stripe can't reach
import kelviq_billing  # noqa: E402  the payment provider, used first when set
import anthropic_api  # noqa: E402  pip 2, the Pro model
import browser  # noqa: E402  the workspace browser (pages read on the server)
import livebrowser  # noqa: E402  the live workspace browser (real Chromium)
import model3d  # noqa: E402  image/text to 3D with TRELLIS.2 - see model3d.py
import pixverse  # noqa: E402  paid text-to-video - see pixverse.py
import hfvideo  # noqa: E402  free-tier text-to-video - see hfvideo.py
import tripo3d  # noqa: E402  text-to-3D generation - see tripo3d.py
import higgsfield_api  # noqa: E402  Higgsfield Cloud video - see higgsfield_api.py
import videogen  # noqa: E402  the video job engine - see videogen.py
import tools  # noqa: E402  model-callable tools - see tools.py
import extensions  # noqa: E402  skills, one-click apps and plugins - see extensions.py
import connections  # noqa: E402  connected apps (MCP) - see connections.py
from agents import router as agent_router  # noqa: E402  who is answering well right now
try:
    from brain import intent as brain_intent  # noqa: E402  the triage model
except Exception as _e:                       # noqa: BLE001
    # NumPy missing, or a checkpoint from a different feature set. The
    # site works exactly as it did without it; it just stops guessing.
    print("[intent] brain unavailable: %s" % _e)
    brain_intent = None
from agents import verifier as agent_verifier  # noqa: E402  code answers, run before trusted
import mailer  # noqa: E402  verification email - see mailer.py

# The local channel: Ollama, on this machine by default. No cloud call
# and no key, and the floor every hosted channel falls back to.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# HOW LONG A LOADED MODEL STAYS IN MEMORY
#
# "-1" is Ollama's value for never unload, and it is the right default
# here rather than an aggressive one. The measurements that matter: a 7B
# answers in 0.09s once resident and takes 7-8 SECONDS to load from disk,
# so an unload is not a small cost paid occasionally, it is the whole
# difference between the app feeling instant and feeling broken. Ollama's
# own default is five minutes, which means a site that is quiet for six
# greets its next visitor with an eight-second wait.
#
# This machine is running in order to serve this app, so holding ~5GB for
# the model is not a sacrifice, it is the job. Override it on a machine
# that has other work to do:  OLLAMA_KEEP_ALIVE=30m
#
# Sent as an INTEGER when it is one. Ollama parses a string keep_alive as
# a Go duration, so "-1" is rejected outright - `time: missing unit in
# duration "-1"` - and a 400 on every single chat request. The bare
# number -1 is the documented way to say never unload; only named
# durations like "30m" may travel as strings.
def _keep_alive(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


OLLAMA_KEEP_ALIVE = _keep_alive(os.environ.get("OLLAMA_KEEP_ALIVE", "-1"))

# Ollama Cloud serves the same native API as a local install - /api/tags,
# /api/chat and /api/generate all behave identically - at
# https://ollama.com, authenticated with a bearer token. So running this
# app with no machine of its own is a matter of pointing OLLAMA_URL there
# and setting a key; not one line of the streaming or provider code below
# knows the difference.
#
#   OLLAMA_URL=https://ollama.com
#   OLLAMA_API_KEY=<from https://ollama.com/settings/keys>
#
# Cloud model names carry a -cloud suffix (gpt-oss:120b-cloud and the
# like); /api/tags returns whatever the key has access to, so the picker
# populates itself and nothing here needs a hardcoded list.
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()


def ollama_headers():
    """Auth for the Ollama endpoint, or nothing at all.

    A local Ollama takes no credentials and rejects nothing, so sending
    an empty Authorization header would be noise at best. Returning {}
    keeps the local path byte-for-byte what it was.
    """
    if not OLLAMA_API_KEY:
        return {}
    return {"Authorization": f"Bearer {OLLAMA_API_KEY}"}


def ollama_is_remote():
    """Is the configured endpoint somebody else's machine?

    This decides what the UI is allowed to claim. The whole product
    promise is that a prompt does not leave the device, and that promise
    is false the moment OLLAMA_URL points at a host - so the label has to
    follow the configuration rather than the other way round.
    """
    url = OLLAMA_URL.lower()
    return not (url.startswith("http://localhost")
                or url.startswith("http://127.0.0.1")
                or url.startswith("http://[::1]"))


# Billing - real subscription charges via Stripe Checkout, when a Stripe
# account is configured. Without keys, upgrading stays the old mock
# instant-flip (no card touched) so the app still works before a Stripe
# account exists. See .env for what to set and where to get it.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")


def _is_placeholder(value):
    """True for a value that looks like a copied-from-docs placeholder
    rather than a real credential.

    This exists because of a genuinely nasty failure mode: a truncated
    key like "sk_test_51..." is a non-empty string, so a plain
    truthiness check treats billing as configured. The app then hides
    its honest "payments aren't set up" message and the customer instead
    hits `AuthenticationError: Invalid API Key` at the checkout button -
    a much worse place to discover the problem. Fail at startup on an
    obviously-fake key instead of at the point of sale.
    """
    if not value:
        return True
    v = value.strip()
    if "..." in v or v.endswith("…"):
        return True
    # Real Stripe secret/publishable keys are ~100+ chars; live/test
    # prefixes plus a short tail means someone pasted an example.
    if v.startswith(("sk_", "pk_", "rk_")) and len(v) < 40:
        return True
    if v.lower() in {"changeme", "your_key_here", "todo", "xxx"}:
        return True
    return False


# The stripe package is imported on the first billing call, not at boot.
# It used to be imported at the top of this file, and that one line was
# 2.6 seconds of a 5-second start, measured with -X importtime: the
# package pulls in several hundred modules, including its whole
# test-helpers tree. The live site sells through Paddle, so on the
# two-core VM that was the longest single step of every deploy restart,
# spent on a provider nothing there uses. Whether it is INSTALLED is
# answered from the import machinery, which costs a directory lookup
# rather than an import.
STRIPE_INSTALLED = importlib.util.find_spec("stripe") is not None
_stripe_module: Any = None


def _stripe():
    """The stripe module, imported and given its key on first use.

    Every stripe.* call below goes through this, so a request that never
    touches billing - which is nearly all of them - never loads it.
    """
    global _stripe_module
    if _stripe_module is None:
        import stripe as module
        module.api_key = STRIPE_SECRET_KEY
        _stripe_module = module
    return _stripe_module


def billing_live():
    """Whether real charges can actually be taken.

    Note the placeholder check: a key that only *looks* like a key is
    treated as absent, so the app reports "not configured" (true and
    actionable) rather than failing mid-checkout.
    """
    return bool(
        STRIPE_INSTALLED
        and not _is_placeholder(STRIPE_SECRET_KEY)
        and not _is_placeholder(STRIPE_PRICE_ID_PRO)
    )


def billing_config_problem():
    """A specific description of what's missing, for the UI to show
    instead of a generic failure. -> None when billing is ready."""
    if not STRIPE_INSTALLED:
        return "The stripe package isn't installed (pip install stripe)."
    if not STRIPE_SECRET_KEY:
        return "STRIPE_SECRET_KEY isn't set in .env."
    if _is_placeholder(STRIPE_SECRET_KEY):
        return ("STRIPE_SECRET_KEY looks like a placeholder, not a real key. "
                "Copy the full key from dashboard.stripe.com/apikeys - it's "
                "about 100 characters, with no '...' in it.")
    if not STRIPE_PRICE_ID_PRO:
        return "STRIPE_PRICE_ID_PRO isn't set in .env."
    if _is_placeholder(STRIPE_PRICE_ID_PRO):
        return "STRIPE_PRICE_ID_PRO looks like a placeholder."
    return None


def webhook_ready():
    """Webhooks additionally need the signing secret - without it there's
    no way to prove a request came from Stripe. billing_live() alone
    isn't enough here: it doesn't check STRIPE_WEBHOOK_SECRET, so the
    webhook route could otherwise reach construct_event() with secret
    set to None."""
    return billing_live() and bool(STRIPE_WEBHOOK_SECRET)


# Sign-in - Google OAuth only. No passwords for this app to store or leak,
# no email/OTP flow to maintain - Google already proved the user controls
# that inbox before we ever see them. See .env for how to get these two
# values from Google Cloud Console.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_oauth_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


app = Flask(__name__)
# Needed for signed session cookies (login state). Set a real SECRET_KEY in
# .env before deploying - this fallback is fine for local dev only.
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-prod")

# Reject oversized request bodies before Werkzeug writes them anywhere.
# attachments.py also enforces a 20MB per-file limit, but it does so
# *after* file_storage.save() - by which point a multi-gigabyte upload
# has already been written to disk. This is the guard that actually
# prevents that: Werkzeug aborts with 413 once the declared or streamed
# body exceeds the cap, so nothing large ever lands. 10 files x 20MB is
# the documented ceiling for one request, plus headroom for encoding
# overhead.
app.config["MAX_CONTENT_LENGTH"] = 220 * 1024 * 1024

# Re-read a template when its file changes, instead of compiling it once
# and holding that copy for the life of the process.
#
# Jinja ties this to app.debug by default, and this app deliberately runs
# with the Werkzeug reloader off (see the bottom of this file - torch and
# diffusers import several hundred files lazily, and every one of them
# triggered a restart). Those two defaults together mean an edit to a
# template is invisible until someone restarts the server by hand, while
# edits to CSS and JS appear immediately because they are static files.
#
# That combination produces the worst kind of bug report: the page is
# served with markup from one point in time and styles from another, so
# it looks broken in ways that match neither version of the code and
# cannot be reproduced from a fresh start. It costs one stat() per
# render to not have that.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Session cookie hardening. HttpOnly keeps the login cookie away from
# any JS that might get injected into a page; SameSite=Lax stops another
# site silently issuing authenticated requests as this user. Secure is
# conditional: forcing it on a plain-http localhost dev run would stop
# the cookie being set at all and break sign-in locally.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS_COOKIES") == "1",
    # THE COOKIE HAS TO OUTLIVE THE TAB.
    #
    # Without a lifetime Flask issues a session cookie with no Expires,
    # which a browser is free to drop the moment its window goes away.
    # In-app browsers - the one Instagram opens for a link in a bio -
    # tear down and recreate that context between navigations, so the
    # cookie vanished mid-visit and the guest id was re-minted.
    #
    # The visible symptom was "Unknown thread": the page created a thread
    # owned by guest A, the cookie was lost, the send arrived as guest B,
    # and the ownership check refused it. Every visitor arriving from
    # that link hit it on their first message.
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    # Re-sends the cookie on every response rather than only when the
    # session changes, which gives a flaky in-app browser repeated
    # chances to persist it.
    SESSION_REFRESH_EACH_REQUEST=True,
)


# A COOKIE A SUBDOMAIN CANNOT OVERWRITE.
#
# Published sites live at <slug>.randomgenerals.com and run whatever
# their author wrote. A page there may set a cookie for the whole
# domain - document.cookie = "session=...; domain=randomgenerals.com" -
# and the browser would then send it here, logging a visitor into
# someone else's session or out of their own. A cookie named with the
# __Host- prefix can only ever be set by this exact host, over HTTPS,
# for path /, so no subdomain can forge or shadow it.
#
# Only where cookies are Secure (production); a plain-http local run
# keeps the old name, because the prefix requires Secure. The old
# cookie is read once and re-issued under the new name, so nobody is
# signed out by the change.
class _HostOnlySessionInterface(SecureCookieSessionInterface):
    LEGACY = "session"

    def open_session(self, app, request):
        sess = super().open_session(app, request)
        name = self.get_cookie_name(app)
        legacy = request.cookies.get(self.LEGACY)
        if name != self.LEGACY and not request.cookies.get(name) and legacy:
            try:
                data = self.get_signing_serializer(app).loads(
                    legacy, max_age=int(app.permanent_session_lifetime.total_seconds()))
                sess = self.session_class(data)
                sess.modified = True
            except Exception:                    # noqa: BLE001 - a stale
                pass                             # cookie is just no session
        return sess

    def save_session(self, app, session, response):
        super().save_session(app, session, response)
        if (self.get_cookie_name(app) != self.LEGACY
                and request.cookies.get(self.LEGACY)):
            response.delete_cookie(self.LEGACY, path="/")


if app.config["SESSION_COOKIE_SECURE"]:
    app.config["SESSION_COOKIE_NAME"] = "__Host-session"
app.session_interface = _HostOnlySessionInterface()


# Paths that are never a person arriving. Assets, polling and the
# service worker would otherwise drown the real numbers - a single chat
# session makes dozens of /api calls, and counting those as visits turns
# "how many people came" into "how chatty was the front end".
_NOT_A_VISIT = ("/static/", "/api/", "/v1/", "/sw.js", "/favicon",
                "/manifest.webmanifest", "/robots.txt", "/sitemap.xml",
                "/healthz")

# Crawlers are most of the traffic to any small site. Counted separately
# would be better; counted as visitors is simply wrong, and it is the
# mistake that makes a dashboard say 400 people came on a day nobody did.
_BOT_HINTS = ("bot", "crawler", "spider", "slurp", "curl/", "wget",
              "python-requests", "headlesschrome", "lighthouse",
              "monitoring", "uptime", "preview", "facebookexternalhit",
              "embedly", "pingdom", "semrush", "ahrefs")


def _client_ip():
    """The visitor's address, from the only header that can be trusted.

    gunicorn binds to 127.0.0.1 and the Cloudflare tunnel is the only
    thing that reaches it, so request.remote_addr is 127.0.0.1 for
    everybody on earth and CF-Connecting-IP is what Cloudflare puts the
    real one in. Nothing outside the tunnel can set that header, because
    nothing outside the tunnel can open the socket.
    """
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For", "").split(",")[0]
                .strip())
            or request.remote_addr or "")


# PER-CALLER LIMITS
#
# Nothing limited how often one caller could hit a generation route,
# sign-in, or sign-up, so one script looping /api/chat could occupy the
# model for every other visitor. See ratelimit.py for
# the bucket, and for why in-process state is correct at --workers 1.
#
# Generation is keyed by account for signed-in users and by address for
# guests: a guest id comes from a cookie, and a cookie can be discarded
# per request. Sign-in and sign-up are keyed by address AND by the
# email being tried, so neither a single attacker nor a spread-out one
# gets unlimited guesses at one account.
LIMIT_CHAT = Limiter(rate=20, per=60, burst=5)
LIMIT_IMAGE = Limiter(rate=6, per=60, burst=3)
LIMIT_VIDEO = Limiter(rate=3, per=60, burst=2)
LIMIT_FEEDBACK = Limiter(rate=30, per=60, burst=10)
LIMIT_VIDEO_ENHANCE = Limiter(rate=10, per=60, burst=4)
LIMIT_SEARCH = Limiter(rate=20, per=60, burst=10)
LIMIT_RUN = Limiter(rate=12, per=60, burst=4)
LIMIT_DIAGRAM = Limiter(rate=12, per=60, burst=4)
LIMIT_LOGIN_IP = Limiter(rate=10, per=600, burst=10)
LIMIT_LOGIN_EMAIL = Limiter(rate=5, per=600, burst=5)
LIMIT_SIGNUP_IP = Limiter(rate=5, per=3600, burst=5)
LIMIT_VERIFY_SEND = Limiter(rate=1, per=60, burst=1)
LIMIT_RESET_IP = Limiter(rate=10, per=3600, burst=10)


def _limit_key_account() -> str:
    uid = session.get("user_id")
    return ("user:%s" % uid) if uid else ("ip:%s" % _client_ip())


def _limit_key_ip() -> str:
    return "ip:%s" % _client_ip()


def _limit_key_email() -> str:
    payload = request.get_json(force=True, silent=True) or {}
    return "email:%s" % (payload.get("email") or "").strip().lower()


@app.before_request
def _count_visit():
    """Count page views for the owner's dashboard.

    Deliberately narrow. It runs before the session hook below so it
    still counts people who are not signed in - who are most of them -
    and it records a count, never a person: see db.record_visit and the
    site_visitors table for what is and is not stored.
    """
    try:
        if request.method != "GET":
            return
        path = request.path or "/"
        if path.startswith(_NOT_A_VISIT):
            return
        agent = (request.headers.get("User-Agent") or "").lower()
        if not agent or any(hint in agent for hint in _BOT_HINTS):
            return
        db.record_visit(path[:120], _client_ip(), agent)
        # Distinct people - all time, and per day - which the daily hash
        # cannot answer because its salt is destroyed nightly. This uses
        # the session's own id, already minted to scope credits and
        # threads, so it counts an identifier the app was setting anyway
        # rather than introducing one. See visitors_seen and
        # visitor_days in db.py.
        #
        # ONLY for a browser that sent the cookie back. current_owner_id
        # mints a fresh guest id for any request without a session, so
        # counting unconditionally counts one cookie-less crawler once
        # per page it walks: the all-time figure was inflated by exactly
        # the traffic the user-agent filter above fails to catch. The
        # cost is that somebody's very first page load is not a "user"
        # until their second one - they are still a visitor and still a
        # page view, and the dashboard says so under the chart.
        if request.cookies.get(app.config.get("SESSION_COOKIE_NAME")
                               or "session"):
            db.note_visitor(current_owner_id())
    except Exception:                            # noqa: BLE001 - a
        pass                                     # counter must never
                                                 # cost somebody a page


@app.before_request
def _persist_session():
    """Opt every request into the lifetime above, and check the session.

    Flask only writes an Expires date for sessions marked permanent, and
    the flag lives on the session rather than the config - so setting
    PERMANENT_SESSION_LIFETIME alone changes nothing without this.
    """
    session.permanent = True

    uid = session.get("user_id")
    if not uid:
        return
    sid = session.get("sid")
    if not sid:
        # A cookie from before sessions were recorded. Adopt it rather
        # than reject it: this change should not log anybody out.
        try:
            _start_session(uid)
        except Exception:                    # noqa: BLE001
            pass
        return
    try:
        if not db.session_is_live(sid, uid):
            # Revoked from another device, so this cookie stops working
            # here - which is the entire point of the feature.
            session.clear()
            return
        db.touch_session(sid, now_iso())
    except Exception:                        # noqa: BLE001
        # A database hiccup must not sign everybody out.
        pass

# Behind a reverse proxy - the Cloudflare tunnel, PythonAnywhere's nginx,
# Render's router - Flask only sees the connection from the proxy, so it
# thinks it is serving http://localhost:5001. Anything built with
# url_for(_external=True) then comes out with that address.
#
# Google sign-in is where this stops being cosmetic. The redirect_uri
# sent to Google has to match the one registered in Cloud Console
# exactly; send http://localhost:5001/... from randomgenerals.com and
# Google refuses the whole flow with redirect_uri_mismatch.
#
# ProxyFix makes Flask trust X-Forwarded-Proto and X-Forwarded-Host, so
# url_for produces the address the user actually typed. It is opt-in
# because those headers are just headers: with no proxy in front to
# overwrite them, any visitor could set them by hand and choose what
# this app believes its own hostname is. Only ever set TRUST_PROXY=1
# when something really is terminating connections in front of this.
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


@app.errorhandler(413)
def too_large(_err):
    return jsonify({"error": "That upload is too large (220MB per request)."}), 413


GENERATED_DIR = os.path.join("static", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)


def streaming_response(generator):
    """Wrap a token generator in a Response that actually streams.

    Behind a reverse proxy - PythonAnywhere and most shared hosts put
    nginx in front of the app - the default is to buffer the whole
    response and deliver it in one piece. The reply still arrives, but
    the token-by-token effect is lost: the user stares at nothing for
    several seconds and then the full answer appears at once.

    X-Accel-Buffering: no is nginx's opt-out. Cache-Control stops any
    intermediate cache from holding a partial stream and replaying it to
    someone else. Both are ignored by servers that don't proxy, so this
    is safe locally too.
    """
    return Response(
        stream_with_context(generator),
        mimetype="text/plain",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )


CODING_SYSTEM_PROMPT = (
    pip_identity.SYSTEM + "\n\n"
    "If the user's message is just casual conversation - a greeting, "
    "small talk, a one-word check-in like 'wsp' or 'yo' - answer that "
    "briefly and naturally like a normal chat, don't force a coding "
    "framework onto it or start talking about programming unprompted. "
    "Save the following for when they actually ask for code:\n\n"
    "You are an expert coding assistant, fluent across mainstream "
    "languages (Python, JavaScript/TypeScript, Java, C, C++, C#, Go, "
    "Rust, Ruby, PHP, Swift, Kotlin, SQL, Bash, HTML/CSS, and more) as "
    "well as robotics/education platforms: Pybricks (the Python API for "
    "LEGO SPIKE Prime and MINDSTORMS EV3 hardware), the SPIKE Prime app's "
    "word-block coding, LEGO EV3 Classroom, and Scratch. For a "
    "text-based language, write real, complete, runnable code. For a "
    "block-based platform (Scratch, EV3 Classroom, or SPIKE Prime's "
    "block mode), you cannot output an actual .sb3/project file - say so, "
    "then describe the exact block sequence as a numbered list using that "
    "platform's real block and category names (e.g. 'Motion > move 10 "
    "steps', 'Control > repeat 10', 'Sensors > if touching color') so it "
    "can be rebuilt by hand block-for-block. "
    "Correctness comes first: only state something works if you're "
    "actually confident it does, and if you're not sure about an API, "
    "syntax detail, or behavior - especially for a smaller/niche "
    "platform - say so explicitly instead of guessing. Think through "
    "edge cases (empty input, off-by-one errors, wrong types, null/None) "
    "before presenting code, not after. The chat renders Markdown: "
    "headings for long structured answers, lists for steps, tables for "
    "comparisons, **bold** for the one thing to notice. Always use fenced "
    "code blocks with the language name (e.g. ```python) for any real code, "
    "and make sure it's complete and runnable, not pseudocode, unless "
    "the user asked for an outline. Prefer concise explanations over "
    "long prose. Ask a clarifying question only if the request is "
    "genuinely ambiguous.\n\n"

    # The same contract the chat prompt carries, in the terms that
    # matter for code: an invented method name compiles in the reply
    # and fails on the reader's machine, which is the worst place for
    # it to fail.
    "HONESTY\n"
    "Do not invent APIs, method names, flags, package names or version "
    "numbers. If you are not certain a library call exists with that "
    "signature, say so and show the closest thing you are sure of, or "
    "tell the reader what to check. Never say you ran or tested code "
    "unless you actually called a tool and read its output; say 'this "
    "should work' rather than 'this works' when you have not run it. "
    "When search results or files are provided, take facts about the "
    "reader's own code from them, not from memory. When asked for a "
    "specific form - only the diff, only the function, a JSON object, "
    "one file - give exactly that and nothing around it.\n\n"

    # Websites get their own brief, because a general "write good code"
    # instruction produces a page that is valid and looks like 2003:
    # unstyled headings, Times New Roman, everything flush left. Nothing
    # in a correctness-focused prompt says anything about design, so the
    # model optimises for the thing it was asked about. Every line below
    # names one specific difference between a page that works and a page
    # someone would actually ship.
    "WHEN ASKED FOR A WEBSITE, PAGE, OR LANDING PAGE\n"
    "Return ONE complete ```html block containing the whole thing - "
    "<!doctype html>, a <style> block, and a <script> block if it needs "
    "one. One file, so it can be previewed and saved directly. Do not "
    "split it across separate html/css/js blocks unless asked, and never "
    "link to a stylesheet or script that does not exist.\n"
    "Make it finished, not a skeleton:\n"
    "- Real content. Write actual headlines and copy about their subject. "
    "Never ship lorem ipsum, 'Your text here', or empty placeholder "
    "sections - a page full of placeholders is not a draft, it is homework "
    "for the person who asked.\n"
    "- A deliberate palette of 3-4 colours as CSS custom properties at the "
    "top, so the page can be re-themed by editing one block. Never leave "
    "it on browser defaults.\n"
    "- Web fonts from Google Fonts via <link>, with a real fallback stack. "
    "Type is most of the reason a page reads as designed or doesn\'t.\n"
    "- Responsive: grid or flex, relative units, and at least one "
    "breakpoint so it does not fall apart on a phone.\n"
    "- Generous whitespace, a clear type scale, and states on everything "
    "interactive - including :focus-visible, not only :hover.\n"
    "- Semantic HTML (header, nav, main, section, footer) and alt text on "
    "every image.\n"
    "- For photos use https://picsum.photos/seed/<word>/<width>/<height>, "
    "which returns a real image. Never invent an image URL.\n"
    "- Motion only where it earns its place: a hover transition, a quiet "
    "reveal. No carousels nobody asked for.\n"
    "WHAT SEPARATES A PREMIUM PAGE FROM A CORRECT ONE\n"
    "The rules above get a page that works. These get one that looks "
    "paid for, and they are mostly about scale and restraint:\n"
    "- TYPE CARRIES IT. Pair a display face with a body face, and make "
    "the jump obvious: a hero at clamp(2.5rem, 6vw, 4.5rem) against "
    "1rem body text. Tighten display type (letter-spacing: -0.02em), "
    "give headings text-wrap: balance, and keep running text near 65 "
    "characters wide. Type is the single biggest difference between a "
    "page that reads as designed and one that does not.\n"
    "- AIR IS NOT WASTE. Sections want clamp(96px, 12vw, 180px) of "
    "vertical padding. Cramped sections read as a stack of panels "
    "however well each one is made.\n"
    "- ONE ACCENT, SPENT ONCE. Everything interactive in the accent, "
    "everything else quiet. Two competing accents read as a template.\n"
    "- HAIRLINES OVER BOXES. A 1px rule and space group content as well "
    "as a bordered card does, at a fraction of the weight. Reach for a "
    "bordered box only when something must be visually contained.\n"
    "- NEUTRALS ARE CHOSEN. Bias greys slightly toward the accent hue. "
    "Pure #000 on pure #fff reads as unstyled.\n"
    "- BOTH THEMES. Define colours as custom properties on :root, "
    "redefine them under @media (prefers-color-scheme: dark), and set an "
    "explicit background on body. Never let one theme inherit the "
    "other\'s text colour.\n"
    "AVOID THE DEFAULT AI LOOK. These are what a model reaches for when "
    "it has no direction, and they are instantly recognisable: a purple "
    "or blue gradient hero; Inter or Space Grotesk chosen as the safe "
    "option; emoji as section markers; every element centred; a row of "
    "identical rounded cards each with an accent bar; cream #F4F1EA with "
    "terracotta. If the user asks for one of these, give it to them - "
    "their words win. Unprompted, choose something with a reason behind "
    "it, and let the subject suggest the palette and the faces.\n\n"

    "IF THE PAGE NEEDS PYTHON\n"
    "A form that submits, data that persists, an API the page calls - "
    "those need a backend, and a single HTML file with fetch() pointed "
    "at an endpoint nobody wrote is worse than saying so. When the "
    "request needs one, give Flask:\n"
    "- One ```python block with the complete app.py: routes, the data "
    "layer (sqlite3 from the standard library unless told otherwise), "
    "and the if __name__ == \'__main__\' block.\n"
    "- One ```html block for templates/index.html, using Jinja "
    "({{ }}, {% for %}) against the data the route actually passes.\n"
    "- Say the two commands to run it: pip install flask, python app.py, "
    "and which URL to open.\n"
    "- Parameterise every SQL query (cursor.execute(sql, (a, b))). Never "
    "build SQL with an f-string or +.\n"
    "- Escape is on by default in Jinja; do not reach for |safe on "
    "anything a user typed.\n"
    "NAME EVERY FILE. When an answer contains more than one file, make "
    "the FIRST LINE of each block a comment holding its path - \"# app.py\", "
    "\"<!-- templates/index.html -->\", \"/* static/style.css */\". The chat "
    "folds consecutive blocks into one panel with a tab per file, and the "
    "tab is labelled from that line, so a set of files reads as one "
    "project instead of a stack of snippets.\n"
    "Keep the front end to the same standard as above - a Flask template "
    "is still a page someone looks at.\n\n"

    "Then say in one line what you built and which custom properties to "
    "edit to re-theme it. Do not narrate the code."
    # No CONTENT_POLICY_NUDGE here on purpose - adding it caused
    # qwen2.5-coder to false-positive refuse completely benign requests
    # (verified: a Flask CRUD API for a library system got refused with
    # the nudge in the system prompt, generated fine without it). A
    # smaller code-tuned model reads explicit refusal instructions as
    # license to refuse more broadly, not more precisely. The
    # deterministic pre-filter in moderation.check_message() still runs
    # on every message regardless of bay, so the actual hard-stop
    # protection is unaffected - this only drops the *secondary*,
    # softer reinforcement, which matters far less for code requests
    # than it does for open-ended chat.
)

# Lower temperature = less random token choice = more consistent, less
# "creative" output. Code has right and wrong answers in a way prose
# doesn't, so it's worth trading away some variety for reliability here;
# chat keeps Ollama's normal default instead.
CODE_MODEL_OPTIONS = {"temperature": 0.2, "top_p": 0.9}

# "Strength" - a user-facing toggle over how much effort the model puts
# in, via generation length and a nudge toward more careful reasoning.
# This is NOT a smarter model underneath (same weights either way) - a
# person asked to "just give me the gist" vs "really think about this
# and check other sources" is the same person, working differently.
# Quick skips web search unless the message obviously needs it; Deep
# always searches first, on top of the longer, more careful generation -
# see shouldAutoSearch()/currentStrength in script.js for the search half.
STRENGTH_LEVELS = {
    # 320 was set when a "quick" answer meant a paragraph. The chat
    # prompt now asks for worked steps AND a check on anything numeric,
    # and a rolling-body problem ran out of room mid-derivation - the
    # reply simply stopped after "Initial energy (at rest) =", which
    # reads as the app breaking rather than as a length limit.
    #
    # 1400, measured rather than guessed. At 900 the more verbose of the
    # two chat models still ran out mid-derivation on the same problem;
    # at 1400 both finish and both get it right. The cap does not make
    # answers long - "what is 17% of 340" comes back in 100-371
    # characters at this setting - so it is the brevity nudge below that
    # keeps ordinary replies short, and this only stops a genuinely
    # long calculation being guillotined.
    "quick": {
        # TEMPERATURE WAS MISSING HERE, AND THIS IS THE DEFAULT MODE.
        #
        # Deep sets 0.15 and the code options set 0.2, but quick set
        # none - so the OpenAI-compatible default applied, which is 1.0.
        # Every ordinary chat message, which is most messages, was
        # generated at maximum sampling randomness.
        #
        # For "write me a poem" that is fine and arguably right. For
        # "what is 17% of 4,250" it is a way to get a different answer
        # each time and occasionally a wrong one, on questions that have
        # exactly one right answer. 0.2 matches what the code bay
        # already uses.
        # 1400 was too low, and it is the DEFAULT mode - so the ceiling
        # most replies were written against was one most long answers
        # could reach. A truncated reply was then indistinguishable from
        # a finished one, because nothing read finish_reason; that is
        # fixed in pip_api.stream_chat, but the better fix is not
        # hitting the ceiling in the first place.
        #
        # Raising it costs nothing on a short answer - max_tokens is a
        # ceiling, not a target, and a two-line reply still spends two
        # lines. It only changes what happens to the long ones.
        "options": {"num_predict": 2600, "temperature": 0.2,
                    "top_p": 0.9},
        "nudge": "Keep the answer brief and to the point - but never "
                 "stop mid-working on a calculation. If it needs steps, "
                 "take them.",
    },
    "deep": {
        # Deep is chosen when someone wants the thorough version, and
        # 2048 was barely above quick's old ceiling.
        #
        # top_p was missing here while quick set 0.9, so the thorough
        # mode ran at the provider default (1.0) - the one mode that
        # asks the model to check its own reasoning was the one sampling
        # from the whole tail. 0.95: a little wider than quick, since
        # deep answers are longer and benefit from some variety, but
        # not the whole distribution.
        "options": {"num_predict": 4096, "temperature": 0.15,
                    "top_p": 0.95},
        "nudge": "Work through this thoroughly - consider multiple "
                 "angles, check your own reasoning for mistakes, use any "
                 "search results provided - then give a complete, "
                 "carefully-reasoned final answer.",
    },
}
DEFAULT_STRENGTH = "quick"

CHAT_SYSTEM_PROMPT = (
    pip_identity.SYSTEM + "\n\n"
    "You are friendly and knowledgeable, a general-purpose assistant. "
    "Answer clearly and conversationally. Match your length to "
    "the question - short answers for simple questions, more detail when "
    "it's actually warranted. Don't force markdown structure (headers, "
    "bullet lists) onto answers that read fine as plain prose. If code "
    "would genuinely help, use a fenced code block.\n\n"

    # One contract, stated once, before the subject sections. Each line
    # is a failure that was seen, not imagined: a confident date for an
    # event the model had never heard of; a "source" that was not among
    # the search results it was given; a list of seven when five were
    # asked for; and the fabricated Python run that used to live in the
    # MATHS section and applies to everything, not only arithmetic.
    "HONESTY\n"
    "If you do not know, say so - a plain 'I don't know' or 'I'm not "
    "sure' is a better answer than a confident guess, and a guess must "
    "be labelled as one. Do not invent names, dates, numbers, quotes, "
    "citations or URLs. If the question depends on something after "
    "your training data or on live information you were not given, say "
    "that is the case rather than answering as if you had it.\n"
    "When search results are provided in this conversation, answer "
    "from them: name which result each fact came from, and do not cite "
    "anything that is not in them. If they do not contain the answer, "
    "say what they do contain and that it was not enough.\n"
    "Never say you ran code, looked something up, or checked a result "
    "unless you actually called a tool and read what it returned. If "
    "you cannot run it, do the work openly and say it was done by hand. "
    "Inventing an output and attributing it to a tool is worse than an "
    "ordinary mistake: it tells the reader the answer was verified when "
    "it was not.\n"
    "When asked for a specific form - a number, a yes or no, a list of "
    "N, a table, JSON, one sentence - give exactly that form, and give "
    "it first. Explanation, if any, comes after the thing that was "
    "asked for.\n"
    "The same question should get the same answer. Do not vary a fact "
    "for variety's sake; vary wording, not substance.\n\n"

    # Maths gets its own section because the default failure mode is
    # specific and fixable. A language model asked for an answer produces
    # the shape of one immediately - fluent, confident, and wrong about a
    # sign or a carry - because nothing asked it to do the work first.
    # Every instruction below targets one identified way that goes wrong.
    "MATHS\n"
    "Work it out before answering. Do the algebra or arithmetic step by "
    "step in the reply, then state the result - never open with the "
    "answer and reconstruct the reasoning after it, which is how a wrong "
    "one gets dressed up as a right one.\n"
    "Then check it, and show the check. Substitute the solution back "
    "into the original equation, differentiate the antiderivative, "
    "confirm the count sums to the total. If the check fails, say so and "
    "redo it rather than shipping the first attempt.\n"
    "Sanity-check magnitude and sign before you commit: a probability "
    "outside 0-1, a negative length, an answer that is a factor of a "
    "thousand off, all mean something went wrong earlier.\n"
    "Keep exact forms - 1/3, sqrt(2), pi/4, 22/7 - and only give a "
    "decimal if it was asked for or the context is genuinely numeric. "
    "Never round mid-working; round once, at the end, and say to what.\n"
    "State assumptions when a question is underspecified (radians or "
    "degrees, real or complex, inclusive or exclusive bounds) instead of "
    "silently picking one.\n"
    # Observed, not hypothetical. Asked for 4839 * 2718 with no tools
    # available, the model wrote "let us run this in Python", then
    # "running this gives us 13287002" - a number it had invented,
    # and the wrong one. A fabricated tool result is worse than no
    # tool, because it hands the reader a reason to trust it. The rule
    # itself is in HONESTY above, because it is not only about maths.
    "If you cannot run the arithmetic, do it openly, by hand, in the "
    "reply - see HONESTY: never attribute a number to a tool you did "
    "not call.\n"

    # Physics fails differently from maths. The arithmetic is usually
    # fine; what goes wrong is picking the wrong relation, dropping a
    # factor that belongs to the geometry, or quietly changing units
    # halfway. Each line below names one of those.
    "\nPHYSICS\n"
    "Name the principle before using it - conservation of energy, "
    "Newton's second law, the work-energy theorem - so a wrong choice "
    "is visible as a wrong choice rather than hidden inside algebra.\n"
    "Carry units through every step and check the final ones match what "
    "was asked. A number whose units come out as m/s^2 when the question "
    "wanted m/s is wrong no matter how clean the working looked.\n"
    "Watch the terms that depend on the shape of the object. A rolling "
    "body has rotational kinetic energy as well as translational, and "
    "its moment of inertia depends on whether it is a sphere, a "
    "cylinder or a hoop - dropping that term is the single most common "
    "way an otherwise correct energy argument gives the wrong speed.\n"
    "State the constants you used (g = 9.81 m/s^2, c = 3.00e8 m/s) and "
    "say when you have idealised something away - no friction, no air "
    "resistance, a point mass.\n"

    # Added after watching the chat model fail a dice-probability
    # question by enumerating every combination by hand: it filled the
    # whole token budget with cases and never reached an answer. Nothing
    # was wrong with its maths - it took the longest route and ran out
    # of road.
    #
    # Measured on that question alone, five runs each at temperature
    # 0.3: 3/5 correct before this section, 5/5 after. The full 14-item
    # eval moved by a single question in each direction, which is what
    # noise looks like - it took repeating the one failing question to
    # tell the fix from the sampling.
    "\nCHOOSING A METHOD\n"
    "Pick the shortest correct route, not the most exhaustive one. "
    "Counting cases one by one is a last resort: reach for symmetry, a "
    "formula, a generating function or a complement first, and only "
    "enumerate when the set is genuinely small and irregular.\n"
    "If you do enumerate, group the cases and count the groups - list "
    "the distinct combinations and multiply by their arrangements "
    "rather than writing out every ordering.\n"
    "Budget your space. If a derivation is getting long, state the "
    "method, do the decisive step, and give the answer - an explanation "
    "that stops before the result is worth less than a short one that "
    "reaches it.\n"
    "For word problems, define what each variable means before using it. "
    "Most wrong answers to word problems are right answers to a "
    "different question.\n"
    "Arithmetic is the weakest link, not the reasoning. Break multi-digit "
    "multiplication, long division and big sums into steps you can see "
    "rather than doing them in one jump.\n"
    "For anything heavier than that - a non-obvious integral, an ODE, "
    "eigenvalues, a factorisation, high-precision arithmetic, a sum over "
    "many terms - do not attempt it in your head. Write the sympy for it "
    "in a ```python block and say it can be run with the Run button. The "
    "sandbox has sympy available specifically for this, so the answer "
    "gets computed rather than guessed. Give the setup and what the "
    "result will mean; let the run produce the number.\n"
    # The renderer draws Markdown and KaTeX now - for a long time it
    # drew fences and nothing else, and this line asked for plain text
    # to match. Both halves changed in the same commit, so there was
    # never a build that asked for markup the page could not show.
    "Notation: this chat renders Markdown and KaTeX. Write maths as "
    "$...$ inline and $$...$$ on its own line. Use headings only for "
    "long, structured answers; lists for enumerations; a table for a "
    "comparison; a fenced block with a language name for any code, "
    "commands or aligned working. Plain prose for everything else - "
    "do not bullet a two-sentence answer.\n"
    "If a problem is beyond what you can reliably do - a hard integral, "
    "a large prime factorisation, anything needing real computation - say "
    "so plainly and offer the method, rather than producing a confident "
    "wrong number.\n\n"

    + moderation.CONTENT_POLICY_NUDGE
)

VALID_MODES = {"code", "chat", "image"}
DEFAULT_MODE = "code"

# ----------------------------------------------------------------------
# Credits - a lightweight in-app usage meter. Not tied to real billing;
# it just gives the UI something to show and gate on. Adjust the costs
# or the starting balance below.
#
# Two plans:
#   free - 1000 credits, auto-refills to full every 2 hours
#   pro  - $3.99/mo (mock checkout, no real payment processor wired up),
#          3000 credits, auto-refills every 30 minutes
# Guests (not signed in) get the free plan's allowance too, tracked in the
# shared credits.json so the app still works without an account.
# ----------------------------------------------------------------------
# WHAT A REQUEST COSTS
#
# Every request lands between CREDIT_MIN and CREDIT_MAX. The band is the
# point: a floor means the counter always moves, so nothing is ever
# mysteriously free, and a ceiling means no single request can empty an
# allowance - which is what makes the number on screen predictable
# enough to plan around. Before this a reply cost anywhere from 1 to
# several hundred, and nobody could form any intuition from that.
#
# Within the band the four bays are still priced by roughly what they
# cost the machine serving them:
#
#   chat   metered on tokens actually generated. Cheapest per token,
#          because a short reply on a small context is the least work
#          anything here does.
#   code   the same metering, weighted heavier - the code bay runs a
#          larger context window and a much higher output ceiling.
#   image  a flat base scaled by pixel count, since no token count
#          exists and pixels are the only honest proxy.
#   video  base plus per-second, scaled by encoder preset. The only bay
#          whose cost is real CPU on this box, and a slow preset pins a
#          core several times longer than a fast one for the same
#          footage.
CREDIT_MIN = 15
CREDIT_MAX = 100

# Kept under its old name: it is also the pre-flight "is there any
# balance at all" gate, and that gate should be the price of the
# cheapest possible request, which is now the floor.
CREDIT_COST_CHAT = CREDIT_MIN
# One sandbox run. The floor: it is a few seconds of CPU, not a model
# call, and the point is that it is not nothing.
CREDIT_COST_RUN = CREDIT_MIN


def _band(value):
    """Anything chargeable, clamped into the published band."""
    return max(CREDIT_MIN, min(CREDIT_MAX, int(round(value))))


# Tokens per credit ABOVE the floor. Higher number = cheaper. Calibrated
# so an ordinary reply sits near the bottom of the band and only a very
# long one approaches the ceiling: at 40, a 300-token answer costs 23 and
# it takes ~3,400 tokens to reach 100.
CREDIT_TOKENS_PER_UNIT = 40          # chat
CREDIT_TOKENS_PER_UNIT_CODE = 30     # code - bigger context, longer output

CREDIT_COST_IMAGE = 25               # base, before the size multiplier
CREDIT_IMAGE_SIZE_MULTIPLIER = {
    "square": 1.0,
    "portrait": 1.15,
    "landscape": 1.15,
    "wide": 1.25,
    "tall": 1.25,
}

CREDIT_COST_VIDEO = 20               # base, before duration and quality
CREDIT_VIDEO_PER_SECOND = 1.2
CREDIT_VIDEO_QUALITY_MULTIPLIER = {
    "draft": 0.7,
    "standard": 1.0,
    "high": 1.6,
    "max": 2.4,
}


def usage_based_cost(eval_count, mode="chat"):
    """What a completed reply costs, from the tokens it actually produced.

    Metered after the fact rather than estimated up front, because the
    only honest signal for "how much work was that" is how much the model
    actually generated - and that is not knowable until it has.
    """
    per_unit = (CREDIT_TOKENS_PER_UNIT_CODE if mode == "code"
                else CREDIT_TOKENS_PER_UNIT)
    return _band(CREDIT_MIN + (eval_count or 0) / per_unit)


def image_cost(size):
    """Flat base times a size multiplier. No token count exists here, so
    pixels are the only honest proxy for work done."""
    mult = CREDIT_IMAGE_SIZE_MULTIPLIER.get(size, 1.0)
    return _band(CREDIT_COST_IMAGE * mult)


def video_cost(out_seconds, quality="standard"):
    """Base + per-second, scaled by encoder preset.

    A slower preset is not a surcharge for its own sake: -preset slow
    genuinely occupies a core several times longer than veryfast for the
    same footage, and on a two-core VM that is the scarcest thing here.
    """
    mult = CREDIT_VIDEO_QUALITY_MULTIPLIER.get(quality, 1.0)
    seconds = max(0.0, float(out_seconds or 0))
    return _band((CREDIT_COST_VIDEO + seconds * CREDIT_VIDEO_PER_SECOND)
                 * mult)


# Both tiers refill on the same two-hour clock; the difference is how
# much is in the tank. Pro is a little over three times the throughput,
# which is a narrower gap than a capability difference would give - so
# what Pro actually sells is the model access, vision, unlimited memory
# and the longer, higher-quality video renders, with the extra credits on
# top rather than instead.
#
# Refill is a reset to the cap, not a trickle. That is the behaviour
# people can reason about: "full again at half past" beats working out an
# accrual rate.
# WHAT PRO ACTUALLY SELLS
#
# A larger allowance - 2,000 credits against 25,000 - and the features
# in features.py that cost real compute per use: vision, video, the
# deep strength.
PLANS = {
    "free": {
        "label": "Free",
        "price": "$0",
        "cap": 2000,
        "refill_seconds": 2 * 60 * 60,  # 2 hours
        # Kept in step with what the code actually does. It has said
        # "the video bay" since that bay became a diagram bay, and a
        # plan description is the last place anyone thinks to update.
        "blurb": "2,000 credits, refilling every 2 hours. Chat and "
                 "coding models, up to 10 images read a day, image "
                 "generation, diagrams, the sandboxed code runner, "
                 "and web search.",
    },
    "pro": {
        "label": "Pro",
        "price": "$3.99/mo",
        "cap": 25000,
        # Hourly, not two-hourly. Halving the wait is the part people
        # feel: a free session that runs dry is out for up to two hours,
        # a Pro one for at most one - on an allowance twelve times larger
        # that it should rarely reach in the first place.
        "refill_seconds": 60 * 60,      # 1 hour
        # Every clause here was checked against features.py. It used
        # to promise "the 7B model", which was removed when this server
        # moved to Gemma, and "3-minute max-quality video renders",
        # which no funded backend can produce.
        "blurb": "25,000 credits, refilling every hour - 12x the free "
                 "allowance, twice as often. Priority on the fast "
                 "model when the site is busy, unlimited image "
                 "reading, unlimited memory, longer answers, twice "
                 "the context window, 100MB uploads and every image "
                 "shape.",
    },
}
STARTING_CREDITS = PLANS["free"]["cap"]


def _every(seconds):
    """3600 -> "every hour", 7200 -> "every 2 hours"."""
    hours = seconds / 3600.0
    if abs(hours - 1.0) < 0.01:
        return "every hour"
    if hours == int(hours):
        return "every %d hours" % int(hours)
    return "every %d minutes" % int(round(seconds / 60.0))


def plan_perks():
    """The bullet lists for the pricing cards, derived, not typed.

    These used to be hand-written HTML sitting next to the values they
    described, and every number in them had drifted: the free card
    advertised 4,000 credits on an hourly refill when it is 2,000 every
    two hours, Pro advertised "10,000 credits every 15 min" when it is
    25,000 hourly, and both cards sold an "Advanced 7B model" that this
    server stopped running when it moved to Gemma. A list maintained by
    hand beside its own source of truth will always end up describing an
    older build, so it is computed from PLANS and features.FEATURES.

    Anything that needs a backend is listed only when that backend is
    actually configured. Selling a clip allowance on a server with no
    video key is how a paid plan becomes a complaint.
    """
    free = features.FEATURES[features.FREE]
    pro = features.FEATURES[features.PRO]
    video_live = _video_backend()[0] is not None

    def credits(plan):
        return "{:,} credits, refilling {}".format(
            PLANS[plan]["cap"], _every(PLANS[plan]["refill_seconds"]))

    def apps(f):
        return "%d connected app%s, %d one-click apps and %d skills" % (
            f["max_connectors"], "" if f["max_connectors"] == 1 else "s",
            f["max_apps"], f["max_skills"])

    # REWRITTEN FROM THE LIVE LIMITS, AGAIN. The last version sold "pip
    # 1.0, our own model, on our own hardware" (this server answers with
    # hosted models), struck image reading off the free card (Free reads
    # ten a day now) and struck "connecting your own apps" off it too
    # (Free connects one). The Pro card is "everything in Free, plus" -
    # each line a real difference, no crossed-out list on Free.
    free_list = [
        ("Chat, homework help, writing and coding", True),
        (credits("free"), True),
        ("Reads up to %d images a day" % free["vision_images_per_day"], True),
        ("Image generation, diagrams, a code runner, web search and voice",
         True),
        ("Remembers up to %d things about you" % free["max_memories"], True),
        (apps(free), True),
        ("%d MB file uploads" % free["max_upload_mb"], True),
    ]
    multiple = PLANS["pro"]["cap"] // max(1, PLANS["free"]["cap"])
    pro_list = []
    # pip 2 leads the card only where it can actually answer.
    if anthropic_api.configured():
        pro_list.append(("<strong>pip 2</strong> - our most capable model, "
                         "for sharper answers, writing and code", True))
    pro_list += [
        ("<strong>%s</strong> - %dx the Free allowance"
         % (credits("pro"), multiple), True),
        ("<strong>Unlimited image reading</strong>", True),
        ("Priority on the fast model when the site is busy", True),
        ("Longer replies in one go - up to {:,} tokens, against {:,}".format(
            pro["max_output_tokens_code"], free["max_output_tokens_code"]),
         True),
        ("Twice the context ({:,} tokens) for long files and long chats"
         .format(pro["max_context_tokens"]), True),
        ("Unlimited memory", True),
        (apps(pro), True),
        ("%d MB uploads and all %d image shapes" % (
            pro["max_upload_mb"], len(pro["image_sizes"])), True),
    ]

    # The video bay has no funded backend on this deployment, so neither
    # card mentions clips until one exists.
    if video_live:
        free_list.append(("%d video clips a day" % free["video_clips"], True))
        pro_list.append(("%d video clips a day, up to %ds at %s" % (
            pro["video_clips"], pro["video_max_seconds"],
            pro["video_max_quality"]), True))
    # 3D models, from a picture or a description - only where a 3D
    # service is actually set up (model3d.py).
    if model3d.available():
        free_list.append(("%d 3D models a day, from a picture or a description"
                          % free["models_3d_per_day"], True))
        pro_list.append(("<strong>%d 3D models a day</strong>, Blender-ready GLB"
                         % pro["models_3d_per_day"], True))

    return {"free": free_list, "pro": pro_list}

DATA_LOCK = threading.Lock()
# An RLock: the guest save callback from current_account() takes this
# lock itself, and spend_credits calls that callback while holding it.
# With a plain Lock that is a deadlock on the one path nobody tested -
# a guest whose balance had run short.
CREDITS_LOCK = threading.RLock()
# RLock, not Lock. _find_or_create_user() has to hold this across the
# "is this email taken" check AND the save that follows, and save_users()
# takes the same lock - which a plain Lock would deadlock on.
USERS_LOCK = threading.RLock()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# WHAT KIND OF THING A MESSAGE IS
#
# Every assistant turn used to be saved the same way, whether the model
# wrote it, the content filter wrote it, or a provider fell over and the
# error text was recorded so it would not vanish. All three then went
# back to the model as prior assistant turns on the next message - so a
# conversation that had once hit a timeout carried
# "[Error talking to ollama: HTTPConnectionPool ... Read timed out]"
# in its context for ever after, and the model would sometimes
# apologise for it.
#
# `kind` says which it is. Only "text" is something the model said and
# only "text" is charged for; the rest are shown to the person and kept
# out of the model's sight. A user turn the filter blocked is stored as
# "blocked" for the same reason: visible on reload, never replayed.
MESSAGE_KINDS = ("text", "error", "refusal", "notice", "blocked")

# The prefixes fabricated replies had before `kind` existed. Used once,
# at load, to label rows saved by earlier builds - see load_threads().
_LEGACY_ERROR_RE = re.compile(
    r"^\[(Error talking to|Something went wrong talking to|"
    r"The fast channel|The model returned nothing|No model is answering|"
    r"No model could answer|pip 1\.0 error|OpenRouter error|Ollama error|"
    r"Could not reach)")

# The model never sees more than this much history from the app. The
# providers trim further to their own windows (fit_to_context for pip,
# _trim_for_local for Ollama) - this is the ceiling on what is handed to
# them, so a very long thread stops being re-serialised in full on every
# turn. Oldest turns go first; the system prompt never does.
HISTORY_MAX_MESSAGES = 40
HISTORY_MAX_CHARS = 60_000


def load_threads():
    data = db.load_threads()
    # Backfill "mode" on threads saved before workspaces existed. Old
    # image conversations didn't record a mode either, so infer it from
    # whether the thread actually contains an image message.
    for tid, t in data.items():
        # The thread knows its own id, so the code that holds a thread
        # dict - _stream_reply, _canned_reply - can save just that one
        # without a reverse lookup through the whole table.
        t["id"] = tid
        if "mode" not in t:
            has_image = any(m.get("type") == "image"
                            for m in t.get("messages", []))
            t["mode"] = "image" if has_image else DEFAULT_MODE
        elif t["mode"] not in VALID_MODES:
            t["mode"] = "chat"
        # Rows from before `kind` existed. The filter's replies carried
        # provider "filter"; provider errors are recognisable by their
        # opening words. Everything else was the model talking. A guess,
        # made once per load rather than on every turn, and never
        # written back in bulk - the row is corrected the next time its
        # thread is saved for its own reasons.
        for m in t.get("messages", []):
            if "kind" in m:
                continue
            if m.get("role") == "assistant" and m.get("provider") == "filter":
                m["kind"] = "refusal"
            elif (m.get("role") == "assistant" and m.get("type") == "text"
                    and _LEGACY_ERROR_RE.match(m.get("content") or "")):
                m["kind"] = "error"
            else:
                m["kind"] = "text"
    return data


def save_threads():
    """The whole table, in one go. Not called by the app any more - see
    save_thread - but kept because the checks seed threads through it."""
    with DATA_LOCK:
        db.save_threads(THREADS)


def save_thread(thread: dict) -> None:
    """Persist ONE thread, the one being changed.

    THREADS is read and appended to by eight request threads at once,
    and every message used to rewrite the whole table from it - a
    DELETE and a reinsert of every user's every conversation, per
    message, while iterating a dict other requests were mutating. The
    row is snapshotted under DATA_LOCK here, so the message list cannot
    change between being read and being written, and only that row is
    touched. Mutation sites take the same lock around their append.
    """
    with DATA_LOCK:
        tid = thread.get("id")
        if not tid or THREADS.get(tid) is not thread:
            # Deleted from under us, or never registered. Nothing to
            # write; a delete has its own db call at its own site.
            return
        row = (tid, thread["title"], thread.get("mode", "chat"),
               thread["updated"], thread.get("owner_id"),
               json.dumps(thread["messages"]), thread.get("project_id"))
    db.save_thread(*row)


def load_users():
    return db.load_users()


def save_users():
    with USERS_LOCK:
        db.save_users(USERS)


THREADS = load_threads()
USERS = load_users()

# The local Stable Diffusion pipeline used to be warmed here at boot. It
# is not any more: "local" is only ever used when a request names it
# (see generate_image_route - the default is a hosted backend), and on
# any machine with torch installed the warm-up imported torch, diffusers
# and a 2GB model into RAM on every start for a button almost nobody
# presses. imagegen loads the pipeline under its own lock on the first
# local request, which is where that cost belongs.


# IS THIS MACHINE ACTUALLY ABLE TO HOST THE MODEL?
#
# The answer differs so completely between deployments that hardcoding
# either one is wrong. On the development laptop - an RTX 5050 - the
# local model answers in 0.09s once resident, comfortably faster than the
# hosted channel. On the Oracle VM, two ARM cores and no GPU, the same
# question took 99.6 seconds and a 7B did not finish inside two minutes.
#
# So the preference order in BAY_ROUTES puts the app's own model first,
# and this decides whether that preference is honoured. The warm-up at
# boot already issues a real request; timing it costs nothing and answers
# the question directly, rather than inferring it from a CPU count or a
# GPU probe that would be wrong on the next host.
_local_speed: dict[str, float | None] = {"warm_seconds": None}

# A warm-up slower than this means the model loads and generates too
# slowly for anyone to sit through. Generous on purpose: it has to clear
# a cold model load, which is seconds even on good hardware, and the cost
# of being wrong in the strict direction is routing away from a machine
# that was fine.
LOCAL_WARM_BUDGET_SECONDS = 25.0

# What the warm-up asks, and the point is that it is not "hi".
#
# About 700 characters - a short question with a little context, which
# is the smallest thing a real request looks like. The old probe sent
# two characters and asked for one token; this asks the model to do the
# kind of work it will actually be given, so the number it produces
# means something.
WARMUP_PROBE_PROMPT = (
    "Here is a short function:\n\n"
    "def total(items):\n"
    "    n = 0\n"
    "    for i in items:\n"
    "        n = n + i['price'] * i['qty']\n"
    "    return n\n\n"
    "It is called on every request and the list can hold a few thousand "
    "rows. Briefly: is there anything worth changing about it, and why? "
    "Two or three sentences is plenty - this is a warm-up, not a "
    "review, and the answer is discarded."
)


def _local_is_fast():
    """Whether the local model should be the default anyone lands on.

    Unknown means yes. The warm-up may not have finished on the first
    request after boot, and assuming the worst there would send the very
    first visitor to the hosted channel on a machine that runs the model
    perfectly well.
    """
    seconds = _local_speed["warm_seconds"]
    if seconds is None:
        return True
    return seconds <= LOCAL_WARM_BUDGET_SECONDS


def _warm_providers():
    """Load the workhorse model at boot instead of in someone's request.

    This is the single largest latency win available here. A 7B loads
    from disk in 7-8 seconds and answers in 0.09 once it is resident -
    so whether the app feels instant or broken comes down entirely to
    whether the model happens to be in memory when someone types. Left
    alone, the first person after every restart pays that 8 seconds and
    reads it as the site being slow.

    Only the chat/code workhorse is warmed. llava is deliberately left
    cold: it is 5GB, only one 7B fits in 8GB of VRAM, and loading it here
    would evict the model almost every request actually needs.

    It replays a REAL request rather than merely loading the model, and
    that distinction turned out to be the whole thing. A bare load leaves
    Ollama holding a runner configured with default options; the first
    real request then asks for num_ctx 8192, which is a different runner,
    and it reconfigures - measured at 11.4s, of which only 0.5s was
    actually evaluating the prompt. The model was resident the whole time
    and the request was still slow. So the warm-up sends the same system
    prompt and the same num_ctx the chat bay sends, and the first real
    request finds a runner it can reuse.

    Deliberately silent - Ollama not being up at boot is not a startup
    failure, and the next real request will load it the slow way.
    """
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags",
                         headers=ollama_headers(), timeout=10)
        names = [m["name"] for m in r.json().get("models", [])]
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return

    for _, pattern in BAY_ROUTES["chat"]:
        match = next((n for n in names if pattern in n.lower()), None)
        if not match:
            continue
        try:
            started = time.monotonic()
            requests.post(
                f"{OLLAMA_URL}/api/chat", headers=ollama_headers(),
                json={
                    "model": match,
                    "stream": False,
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                    # The real system prompt, so the shared prefix is
                    # already in the KV cache when someone actually types.
                    #
                    # AND A PROMPT WITH SOMETHING IN IT. This asked "hi"
                    # and requested ONE token, which on the deployment VM
                    # returned in 0.7 seconds - so _local_is_fast() said
                    # yes, the router sent real work to Ollama, and real
                    # work took 55 to 90 seconds to produce its FIRST
                    # token. Measured, at 2k, 6k and 12k characters of
                    # prompt; the machine was never fast, the probe was
                    # measuring the one request shape that is.
                    #
                    # Reading the prompt is most of the cost for a small
                    # model on shared cores, so a probe with no prompt
                    # measures almost nothing about serving one.
                    "messages": [
                        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                        {"role": "user", "content": WARMUP_PROBE_PROMPT},
                    ],
                    # Must match what _stream_reply() sends for chat, or
                    # this warms a runner that the first request discards.
                    # 24 tokens rather than 1: enough to include the cost
                    # of actually starting to generate.
                    "options": {"num_predict": 24, "num_ctx": 8192},
                },
                timeout=300)
            # The warm-up is already a real request, so timing it is free
            # and gives the one number that decides whether this hardware
            # can host the product - see _local_is_fast().
            _local_speed["warm_seconds"] = time.monotonic() - started
        except requests.exceptions.RequestException:
            _local_speed["warm_seconds"] = None
        return


# Started at the FOOT of this module, not here. _warm_providers() reads
# BAY_ROUTES, which is defined ~1,200 lines below this point, and a
# thread launched here races module execution and loses: it raised
# NameError inside a daemon thread, where nothing reports it, and the
# only visible symptom was that the first request still paid the full
# 8-second model load the warm-up exists to remove.


def public_user(user):
    """Strip the password hash before this ever reaches the frontend."""
    return {
        "id": user["id"],
        "email": user["email"],
        "plan": user["plan"],
        "created": user["created"],
        # The settings panel needs both of these to decide what to show:
        # whether to offer verification, and whether a password form can
        # work at all. A Google account has no password to change, and
        # offering the form would only earn a refusal from the route.
        "email_verified": bool(user.get("email_verified")),
        "google_id": bool(user.get("google_id")),
    }


# ----------------------------------------------------------------------
# Sessions with a server-side record
#
# A signed cookie carrying only a user id cannot be revoked: the server
# never learns that it exists, so there is nothing to list and nothing to
# switch off. Every login now also writes a row, and the cookie carries
# that row's id.
#
# Existing cookies keep working. One is adopted on its next request
# rather than rejected - nobody is logged out by this change, and their
# session simply appears in the list from then on.
# ----------------------------------------------------------------------
_DEVICE_HINTS = (
    ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"), ("Safari/", "Safari"),
)
_PLATFORM_HINTS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
    ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux"),
)


def _describe_agent(user_agent):
    """"Chrome on Windows" from a user-agent string.

    Deliberately coarse. The point is for somebody to recognise their own
    devices in a list, and a full UA string is both unreadable and more
    fingerprint than the feature needs.
    """
    ua = user_agent or ""
    browser = next((name for token, name in _DEVICE_HINTS if token in ua), "")
    platform = next((name for token, name in _PLATFORM_HINTS
                     if token in ua), "")
    if browser and platform:
        return "%s on %s" % (browser, platform)
    return browser or platform or "Unknown device"


def _start_session(uid):
    """Mint a session row and put its id in the cookie."""
    sid = uuid.uuid4().hex
    db.create_session(sid, uid, now_iso(),
                      ip=request.remote_addr or "",
                      user_agent=request.headers.get("User-Agent", ""))
    session["sid"] = sid
    return sid


def current_owner_id():
    """Whoever is making this request, for scoping both threads and
    credits: a signed-in user's id, or a per-browser-session guest id
    minted on first visit and kept in their signed session cookie - never
    one pool shared by every signed-out visitor."""
    uid = session.get("user_id")
    if uid and uid in USERS:
        return uid
    gid = session.get("guest_id")
    if not gid:
        gid = f"guest:{uuid.uuid4().hex}"
        session["guest_id"] = gid
    return gid


def current_account():
    """Returns the credits dict this request should read/write - a signed
    in user's own pool, or this browser session's own guest pool - plus a
    save callback."""
    uid = session.get("user_id")
    if uid and uid in USERS:
        # The credits ROW is what the atomic debits act on, so the
        # in-memory copy is a cache of it, saved on its own. save_users
        # used to rewrite every credits row from memory on every call,
        # which overwrote whatever the table's own arithmetic had done.
        user_credits = USERS[uid]["credits"]
        return user_credits, (lambda: db.save_credits(uid, user_credits))

    gid = current_owner_id()
    credits = db.load_credits(gid)
    if not credits or credits.get("balance") is None:
        credits = {
            "balance": STARTING_CREDITS,
            "starting": STARTING_CREDITS,
            "plan": "free",
            "last_refill": now_iso(),
        }

    def save():
        with CREDITS_LOCK:
            db.save_credits(gid, credits)

    return credits, save


def apply_refill(credits):
    """Top a credits dict back up to its plan cap once the refill interval
    has elapsed. Mutates in place; caller is responsible for saving."""
    plan = PLANS.get(credits.get("plan", "free"), PLANS["free"])
    credits["starting"] = plan["cap"]
    last = credits.get("last_refill")
    try:
        last_dt = datetime.datetime.fromisoformat(last) if last else None
    except ValueError:
        last_dt = None
    now = datetime.datetime.now(datetime.timezone.utc)
    if last_dt is None:
        credits["last_refill"] = now_iso()
        return
    elapsed = (now - last_dt).total_seconds()
    if elapsed >= plan["refill_seconds"] or credits["balance"] > plan["cap"]:
        credits["balance"] = plan["cap"]
        credits["last_refill"] = now_iso()


def credits_view(credits):
    """Adds a countdown so the frontend can show 'next refill in Xm'."""
    plan = PLANS.get(credits.get("plan", "free"), PLANS["free"])
    last_dt = datetime.datetime.fromisoformat(credits["last_refill"])
    now = datetime.datetime.now(datetime.timezone.utc)
    remaining = plan["refill_seconds"] - (now - last_dt).total_seconds()
    view = dict(credits)
    view["plan_label"] = plan["label"]
    view["cap"] = plan["cap"]
    view["next_refill_in"] = max(0, int(remaining))
    return view


def spend_credits(amount: int, tokens: int = 0) -> int:
    """Charge the current account for work already done. -> what was
    actually charged, which may be less than asked.

    Metered after the fact - a reply's cost is known only once it has
    streamed - so this takes what it can down to zero (db.debit_to_floor)
    and never refuses. It USED to refuse: with a balance below the
    cost it returned False without deducting anything, and every
    caller ignored the return, so a person whose balance had drifted
    into the teens was served long replies free, indefinitely.

    The debit happens in the table, in one statement, so two tabs
    finishing at once cannot both read the old balance and both write.
    The in-memory copy is refreshed from what the table says.
    """
    owner = current_owner_id()
    credits, save = current_account()
    with CREDITS_LOCK:
        apply_refill(credits)
    save()              # the refill, and for a new guest the row itself
    charged, new_balance = db.debit_to_floor(owner, int(amount))
    credits["balance"] = new_balance
    if charged or tokens:
        # Counters for the usage chart. Written here rather than at the
        # call sites so no future spender can forget to record itself.
        # Tokens count even when the balance was already empty and
        # nothing could be charged: the model still wrote them.
        db.note_usage(owner, charged, tokens=tokens)
    return charged


def charge_up_front(cost: int):
    """For work whose price is known before it starts - an image, a code
    run. -> (ok, credits) where `credits` is the account after refill,
    for the 402 body when `ok` is False.

    The debit IS the check. The old shape read the balance, compared,
    generated, and charged afterwards, so two requests arriving together
    both passed the comparison; and a generation that failed after the
    charge was not always refunded. Callers refund with refund_charge()
    when the work does not happen.
    """
    owner = current_owner_id()
    credits, save = current_account()
    with CREDITS_LOCK:
        apply_refill(credits)
    save()
    new_balance = db.debit(owner, int(cost))
    if new_balance is None:
        return False, credits
    credits["balance"] = new_balance
    db.note_usage(owner, int(cost))
    return True, credits


def refund_charge(cost: int) -> None:
    """Give back what charge_up_front took, when the work did not happen."""
    owner = current_owner_id()
    new_balance = db.credit(owner, int(cost))
    credits, _save = current_account()
    if new_balance is not None:
        credits["balance"] = new_balance
    db.note_usage(owner, -int(cost), messages=0)


@app.template_global()
def asset(filename):
    """A URL for a static file that changes whenever the file does.

    Cloudflare serves this site's static assets with Cache-Control:
    max-age=14400, so a browser keeps script.js for four hours and does
    not even ask whether a newer one exists. Every fix shipped inside
    that window is invisible: the code on the server is right, the code
    running in the browser is whatever it downloaded hours ago, and the
    bug being reported was fixed long ago.

    That is not a caching bug to switch off - four hours of caching is
    exactly what you want for speed. The fix is to make the URL itself
    change: appending the file's modification time means a new build is
    a new URL, which no cache anywhere can answer with an old copy,
    while an unchanged file keeps its URL and stays cached.
    """
    path = os.path.join(app.static_folder or "static", filename)
    try:
        version = int(os.path.getmtime(path))
    except OSError:
        # A missing file is a template bug, not a reason to 500 the page
        # - fall back to an unversioned URL and let it 404 visibly.
        return url_for("static", filename=filename)
    return url_for("static", filename=filename, v=version)


@app.template_global()
def module_map() -> str:
    """An import map giving every front-end module a versioned URL.

    asset() versions the entry script's URL with its mtime, and that is
    enough for a single file. It is not enough for modules: app.js
    imports "./state.js", the browser resolves that to /static/js/
    state.js with no ?v=, and Cloudflare serves that URL from cache for
    four hours - so after a deploy the NEW app.js would run against the
    OLD state.js until the cache turned over, which is a class of bug
    nobody could reproduce locally.

    An import map is consulted with the resolved URL, so mapping each
    module's plain URL to its versioned one makes every relative import
    cache-busted without touching the import statements. Browsers
    without import maps ignore it and still run the app; for them the
    no-cache header on /static/js/ (see _module_cache_headers) means a
    revalidation per module instead of a stale one.
    """
    js_dir = os.path.join(app.static_folder or "static", "js")
    entries = {}
    try:
        names = sorted(n for n in os.listdir(js_dir) if n.endswith(".js"))
    except OSError:
        names = []
    for name in names:
        plain = url_for("static", filename="js/" + name)
        entries[plain] = asset("js/" + name)
    return json.dumps({"imports": entries}, separators=(",", ":"))


@app.after_request
def _module_cache_headers(response):
    """Modules revalidate rather than cache. The import map above is
    what actually keeps them fresh; this is for the browsers that do
    not read it, and it costs a 304 per module for them, not a
    download - Flask sends an ETag with every static file."""
    if request.path.startswith("/static/js/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _read_build_id() -> str:
    """The short git commit this process was started from.

    deploy.sh used to prove a deploy by grepping the running site for
    four phrases that happened to be new in one particular build - so
    every later change either updated the list or shipped unverified.
    A build id in the page is the general answer: the script compares
    what the site says it is running with what the checkout says it
    should be, and the two either match or they do not.

    Read once at import. A running process IS one build; re-reading it
    per request would report the checkout's commit, which after a pull
    without a restart is exactly the wrong one.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=here,
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("ascii", "replace").strip() or "unknown"
    except Exception:                          # noqa: BLE001 - not a git
        pass                                   # checkout, or no git
    try:
        # A tarball deploy has no git. The file's own mtime still
        # changes with every build, which is all a marker needs.
        return "t%d" % int(os.path.getmtime(__file__))
    except OSError:
        return "unknown"


BUILD_ID: str = _read_build_id()


@app.template_global()
def build_id() -> str:
    return BUILD_ID


# ----------------------------------------------------------------------
# Legal pages.
#
# Paddle is a merchant of record: it takes on the legal liability for
# every sale, so it reviews the site before approving an account, and a
# site charging subscriptions with no terms, privacy or refund policy is
# the most common reason a submission goes to manual review or is
# refused outright.
#
# They are also just necessary. This app holds accounts, stores what
# people write, and sends prompts to Google - none of which anyone has
# agreed to if it is written down nowhere.
#
# The copy below is deliberately specific about where data actually
# goes, because a privacy policy that describes a different product than
# the one running is worse than none: it is a promise nobody kept.
# ----------------------------------------------------------------------
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@randomgenerals.com")
LEGAL_UPDATED = "30 August 2026"


def _legal(slug, title, sections):
    return render_template(
        "legal.html", slug=slug, title=title, sections=sections,
        updated=LEGAL_UPDATED, support_email=SUPPORT_EMAIL)


# OWNER-ONLY STATISTICS
#
# Gated on an email in the environment rather than a role column, because
# there is exactly one owner and a schema change to express that would be
# ceremony. Set ADMIN_EMAIL to the address you sign in with.
#
# FAILS CLOSED, and deliberately with 404 rather than 403. With
# ADMIN_EMAIL unset the page does not exist for anybody - including the
# owner - because the alternative default is a public page listing
# revenue, signups and usage. A 403 would also confirm the route is real
# and worth attacking; a 404 says nothing.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()


def _owner_only():
    """404 unless the caller is the owner. Call at the top of a route.

    Factored out because there are now four of these and the gate must
    be identical in all of them - a dashboard that is protected and an
    /api endpoint behind it that is not is the same as neither being
    protected.
    """
    if not ADMIN_EMAIL:
        abort(404)
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user or (user.get("email") or "").strip().lower() != ADMIN_EMAIL:
        abort(404)


@app.route("/favicon.ico")
def favicon_ico():
    """Every browser asks for this path whether or not the page links an
    icon, and this returned 404 - so the tab was blank anywhere the
    <link rel=icon> was not read, which includes a lot of feed readers,
    link previews and the address bar of our own browser build.

    static/favicon.svg is the real icon; the PNG is here because .ico
    consumers are the ones least likely to accept SVG.
    """
    return send_from_directory(
        os.path.join(app.root_path, "static", "icons"),
        "icon-180.png", mimetype="image/png",
        max_age=60 * 60 * 24 * 7)


RESULTS_PER_PAGE = 10


def _page_number(raw):
    """A page number from a query string, which is whatever someone
    typed. ?p=abc and ?p=-4 are not errors worth a 500."""
    try:
        return max(1, min(int(raw or 1), 50))
    except (TypeError, ValueError):
        return 1


@app.route("/search")
def search_page():
    """Our own index. Public, unauthenticated, no tracking.

    Deliberately not behind a login: a search engine nobody can use
    without an account is not a search engine.
    """
    query = (request.args.get("q") or "").strip()[:200]
    page = _page_number(request.args.get("p"))
    kind = request.args.get("kind") or ""
    if kind not in searchdb.KINDS:
        kind = ""

    results, meta = [], {"total": 0, "took_ms": 0.0, "counts": {},
                         "filters": {}, "negatives": []}

    if query:
        meta = searchdb.search(query, limit=RESULTS_PER_PAGE,
                               offset=(page - 1) * RESULTS_PER_PAGE,
                               kind=kind or None)
        results = meta["results"]
        for r in results:
            r["source"] = "index"

        # The index covers fourteen sites. Answering "rien pour X" for
        # everything else would make this unusable as anyone's search
        # engine, so the open web fills the rest of the first page -
        # labelled, so nobody is misled about which is which.
        #
        # Not done when a tab or an operator is active: someone who
        # asked for kind:examen or site:x does not want the web's
        # opinion mixed in.
        narrowed = bool(kind or meta["filters"])
        if page == 1 and not narrowed and len(results) < RESULTS_PER_PAGE:
            seen = {r["url"].rstrip("/") for r in results}
            for r in websearch._duckduckgo(query, RESULTS_PER_PAGE * 2):
                if r["url"].rstrip("/") in seen:
                    continue
                results.append({
                    "title": r["title"], "url": r["url"],
                    "host": urllib.parse.urlparse(r["url"]).netloc,
                    "kind": "", "lang": "", "is_pdf":
                        r["url"].lower().endswith(".pdf"),
                    "snippet": r["snippet"],
                    # Escaped here: this text came from someone else's
                    # results page and the template renders it as HTML.
                    "snippet_html": escape(r["snippet"]),
                    "links_in": 0, "source": "web"})
                if len(results) >= RESULTS_PER_PAGE:
                    break

    return render_template("search.html", q=query, results=results,
                           page=page, kind=kind, meta=meta,
                           kinds=searchdb.KINDS, stats=searchdb.stats())


@app.route("/api/suggest")
def api_suggest():
    """Autocomplete from our own index.

    A suggestion service would mean forwarding every keystroke somebody
    types to a third party; this reads titles we crawled ourselves.
    """
    return jsonify({
        "suggestions": searchdb.suggest(
            (request.args.get("q") or "")[:120])})


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()[:200]
    if not query:
        return jsonify({"query": "", "results": [], "index": searchdb.stats()})
    limit = _page_number(request.args.get("limit")) if request.args.get(
        "limit") else RESULTS_PER_PAGE
    kind = request.args.get("kind") or ""
    found = searchdb.search(query, limit=min(limit, 25),
                            kind=kind if kind in searchdb.KINDS else None)
    return jsonify({
        "query": query,
        "results": found["results"],
        "total": found["total"],
        "took_ms": found["took_ms"],
        "counts": found["counts"],
        "filters": found["filters"],
        "index": searchdb.stats(),
    })


@app.route("/stats")
def stats_page():
    _owner_only()
    return render_template("stats.html", s=stats.collect())


@app.route("/dashboard")
def dashboard_page():
    """Visitors, money and requests, on one page."""
    _owner_only()
    return render_template("dashboard.html", d=dashboard.collect())


@app.route("/api/dashboard")
def dashboard_json():
    _owner_only()
    return jsonify(dashboard.collect())


@app.route("/api/stats")
def stats_json():
    """The same figures as JSON, for anything that wants to graph them
    elsewhere. Same gate - it would be a strange kind of protection that
    covered the page and not the data behind it."""
    _owner_only()
    return jsonify(stats.collect())


@app.route("/terms")
def terms_page():
    return _legal("terms", "Terms of Service", [
        {"heading": "What this is", "body": [
            "RandomGenerals is a hosted assistant for chat, code, image "
            "generation and video editing. Using it means accepting these "
            "terms.",
            "You must be at least 16, or old enough to enter a contract "
            "where you live, whichever is higher.",
        ]},
        {"heading": "Your account", "body": [
            "You are responsible for what happens under your account and "
            "for keeping your password to yourself. Tell us promptly if "
            "you think someone else has access.",
            "You can use the service without an account, in which case "
            "your work is tied to your browser session and is lost when "
            "it ends.",
        ]},
        {"heading": "Acceptable use", "body": [
            "Do not use the service to:",
            ["break the law, or help anyone else to",
             # Broadened from "involving minors, or real people without
             # consent" to all sexually explicit generation. The narrow
             # version left ordinary pornography permitted by omission,
             # which is both not what this service is for and the
             # opposite of what a payment processor reviewing an AI
             # product needs the terms to say.
             "generate sexually explicit or pornographic material of any "
             "kind, including sexual roleplay or companion "
             "conversations. Sexual content involving a minor, or "
             "sexual imagery of a real identifiable person, is "
             "absolutely prohibited",
             "swap faces, produce deepfakes, or otherwise manipulate the "
             "likeness of a real person",
             "harass, threaten or defame anyone",
             "produce malware, or material intended to attack systems you "
             "do not own",
             "impersonate a real person or organisation",
             "resell access, or run automated traffic through the service "
             "beyond ordinary personal use"],
            "The full rules, including how they are enforced, are on the "
            "<a href=\"/acceptable-use\">Acceptable Use Policy</a>.",
            "We may suspend an account that does these things, without "
            "refunding time already used.",
        ]},
        {"heading": "What you make", "body": [
            "You keep whatever rights you have in what you type and in "
            "what the service produces for you. We claim no ownership of "
            "your prompts or outputs.",
            "Generated output is not guaranteed to be original, accurate "
            "or free of third-party rights. Check anything you intend to "
            "publish or rely on.",
        ]},
        {"heading": "Credits and limits", "body": [
            "Both tiers run on credits that reset on a fixed schedule - "
            "Free every two hours, Pro every hour. Different tasks cost "
            "different amounts, roughly in proportion to the work they "
            "take. Current amounts are shown in the app and on the "
            "pricing section of the home page.",
            "We may change the credit amounts and prices. If a change "
            "makes Pro worse for you, it takes effect at your next "
            "renewal, not mid-term, and you can cancel before it applies.",
        ]},
        {"heading": "Availability", "body": [
            "This is a small service. It runs on modest hardware and "
            "depends on third parties for the models. There is no uptime "
            "guarantee, and features can change or be withdrawn.",
            "Nothing here is a professional service. Do not rely on it "
            "for medical, legal, financial or safety-critical decisions.",
        ]},
        {"heading": "Liability", "body": [
            "The service is provided as is. To the extent the law allows, "
            "we are not liable for indirect or consequential loss, and "
            "our total liability is limited to what you paid us in the "
            "twelve months before the claim.",
            "Nothing here limits liability that cannot lawfully be "
            "limited, including for death or personal injury caused by "
            "negligence, or for fraud.",
        ]},
        {"heading": "Payments", "body": [
            "Subscriptions are sold and processed by Paddle, which acts "
            "as the merchant of record. Your payment contract for the "
            "transaction is with Paddle, and their terms apply to it "
            "alongside these. Paddle handles tax and invoicing.",
        ]},
        {"heading": "Ending it", "body": [
            "You can stop using the service or cancel a subscription at "
            "any time. We can suspend or close an account that breaches "
            "these terms, or if we stop running the service.",
        ]},
    ])


@app.route("/privacy")
def privacy_page():
    return _legal("privacy", "Privacy Policy", [
        {"heading": "The short version", "body": [
            "We keep what is needed to run your account and no more. We "
            "do not sell your data, run advertising, or use your "
            "conversations to train models.",
        ]},
        {"heading": "What we hold", "body": [
            ["<strong>Account</strong> - your name, your age, your "
             "email address and a hashed password, or a Google account "
             "identifier if you sign in that way. Passwords are never "
             "stored in a readable form. Your age is kept as a year of "
             "birth rather than a number that would silently go stale, "
             "and it is held for one reason: to enforce the age limit "
             "below.",
             "<strong>Email verification and password resets</strong> - "
             "a six-digit code and its expiry, deleted as soon as it is "
             "used or runs out.",
             "<strong>Connected apps</strong> - if you connect one, its "
             "address and what it can do. If you give it an access "
             "token, that token is stored on our server so the "
             "assistant can use it, is sent only to the service it "
             "belongs to, and is never shown back to you or to anyone "
             "else. Removing the connection deletes it.",
             "<strong>Your work</strong> - conversation threads, saved "
             "memories, custom instructions, uploaded files and generated "
             "images, stored so they are there when you come back.",
             "<strong>Billing</strong> - a subscription status and "
             "identifier from Paddle. Card details never reach our "
             "servers; Paddle handles them.",
             "<strong>Technical</strong> - ordinary server logs, and a "
             "signed session cookie that keeps you logged in.",
             "<strong>Visit counts</strong> - we count page views so we "
             "can see whether anyone is using the site. This is done on "
             "our own server: there is no analytics provider, no "
             "advertising script and no tracking cookie. To count the "
             "same person once a day rather than once a click, your IP "
             "address and browser are combined with a secret that is "
             "destroyed every night, and only the resulting hash is "
             "stored. It cannot be turned back into your address, and "
             "because the secret changes daily it cannot be used to "
             "recognise you tomorrow.",
             "<strong>A first-visit marker</strong> - so we can tell how "
             "many people have used the service in total rather than "
             "only today, we record that the identifier already in your "
             "session cookie has been seen, once, with the date. That "
             "identifier is the same one used to keep your "
             "conversations and credits yours; no additional cookie is "
             "set for this, and nothing about what you did is attached "
             "to it."],
        ]},
        {"heading": "Where prompts actually go", "body": [
            "This matters more than the rest of the page, so it is stated "
            "plainly rather than buried.",
            "Chat and code replies come from <strong>pip 1.0</strong>, "
            "our own model, running on hardware we operate: what you "
            "type is processed there and is not sent to a model "
            "company. When pip 1.0 cannot answer - it is restarting, "
            "or busy - a reply may instead come from a model reached "
            "through <strong>OpenRouter</strong>, as described below, "
            "which processes it under its own terms. Which one "
            "answered is shown on each reply.",
            ["Image generation sends your description to "
             "<strong>Pollinations</strong>, which serves the SANA "
             "model. Your description is also rewritten by the same "
             "chat model first, to get a better picture.",
             "Web search sends your query to <strong>DuckDuckGo</strong>.",
             "Some coding replies are produced by models reached through "
             "<strong>OpenRouter</strong>, which passes the request to "
             "the lab that runs the model. As above, which channel "
             "answered is shown on each reply.",
             "Verification and password-reset emails are sent through "
             "our mail provider, which sees your address and the code.",
             "If you connect an app, the assistant sends requests to it "
             "on your behalf while answering you - that is the point of "
             "connecting it - and what it sends is whatever the task "
             "needs.",
             "Uploaded files and videos are processed on our own server "
             "and are not sent to a third party."],
            "The self-hosted version of this software can run models "
            "entirely on your own machine, in which case none of the "
            "above applies. The channel label in the app tells you which "
            "is in use - it reads \"On this machine\" only when that is "
            "literally true.",
        ]},
        {"heading": "Cookies", "body": [
            "One essential cookie keeps you signed in. A little browser "
            "storage remembers display preferences. No advertising or "
            "third-party tracking cookies are set.",
        ]},
        {"heading": "How long we keep it", "body": [
            "Your threads and memories stay until you delete them or "
            "close your account. Generated images and uploads are pruned "
            "on a timer. Deleting your account removes your data from the "
            "live database; backups age out.",
        ]},
        {"heading": "Your rights", "body": [
            "You can ask for a copy of your data, ask us to correct or "
            "delete it, or object to how we use it. Email "
            "<a href=\"mailto:%s\">%s</a> and we will act within 30 days. "
            "If you are in the UK or EU you can also complain to your "
            "data protection authority." % (SUPPORT_EMAIL, SUPPORT_EMAIL),
        ]},
        {"heading": "Children", "body": [
            "The service is not for under-16s. Signing up asks for "
            "your age and refuses anyone below that, which is why we ask "
            "for it at all. We do not knowingly collect data from "
            "children - tell us if you believe a child has an account "
            "and we will remove it.",
        ]},
    ])


# ----------------------------------------------------------------------
# Downloads
#
# The desktop build, served from the site rather than from a release
# host. One less account to depend on, and the page can tell the truth
# about what is actually on disk instead of linking at a tag that may
# not have an artefact attached.
#
# Files live OUTSIDE the repository - dropping a 124MB zip into static/
# would put it in git, in every clone, and in the Docker build context.
# The directory is created by the deploy, not by this file.
# ----------------------------------------------------------------------
DOWNLOADS_DIR = os.environ.get(
    "DOWNLOADS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))

# Named explicitly rather than globbed, so a stray file in that
# directory cannot become a download link, and so the label and the
# filename cannot drift apart.
DOWNLOAD_BUILDS = [
    {
        "id": "windows",
        "file": "RandomGenerals-windows-x64.zip",
        "label": "Windows",
        "detail": "Windows 10 or 11, 64-bit",
        "note": "Unzip anywhere and run RandomGenerals.exe. "
                "Windows will warn that the app is unsigned - "
                "More info, then Run anyway.",
    },
]


def _download_view():
    """What is actually on disk, with sizes. Never invents a link."""
    out = []
    for build in DOWNLOAD_BUILDS:
        path = os.path.join(DOWNLOADS_DIR, build["file"])
        exists = os.path.isfile(path)
        row = dict(build, available=exists, size="", updated="")
        if exists:
            size = os.path.getsize(path)
            row["size"] = "%.0f MB" % (size / (1024 * 1024))
            row["updated"] = datetime.datetime.fromtimestamp(
                os.path.getmtime(path),
                datetime.timezone.utc).strftime("%d %B %Y")
        out.append(row)
    return out


@app.route("/download")
def download_page():
    return render_template("download.html", builds=_download_view())


@app.route("/download/<build_id>")
def download_build(build_id):
    """Send the file. 404 if it is not one of ours, or not there."""
    build = next((b for b in DOWNLOAD_BUILDS if b["id"] == build_id), None)
    if not build:
        abort(404)
    path = os.path.join(DOWNLOADS_DIR, build["file"])
    if not os.path.isfile(path):
        abort(404)
    # send_from_directory rather than send_file with a joined path: it
    # refuses to serve anything outside the directory, which matters
    # because build_id came off the URL. The allow-list above already
    # makes traversal impossible; this is the belt to its braces.
    return send_from_directory(DOWNLOADS_DIR, build["file"],
                               as_attachment=True)


@app.route("/acceptable-use")
def acceptable_use_page():
    """The rules for what may be generated here, as its own page.

    These rules already existed inside /terms. They are repeated here as
    a standalone document because payment processors reviewing an AI
    product look for a named Acceptable Use Policy and do not go hunting
    through a terms page for a section - Paddle declined this site over
    product category, and the next processor's published checklist asks
    for this document by name.

    It also states plainly that this is an interface over third-party
    models rather than a model of its own, which is the other thing
    those reviews ask for.
    """
    return _legal("acceptable-use", "Acceptable Use Policy", [
        {"heading": "The short version", "body": [
            "This is a general-purpose assistant for work, study and "
            "building things. It is not an adult service, not a "
            "companion or roleplay service, and not a tool for making "
            "images of real people.",
        ]},
        {"heading": "What this product is", "body": [
            "RandomGenerals is an assistant owned and operated by "
            "Imrane Adib. Its chat model, pip 1.0, is our own: an "
            "openly licensed "
            "base model, fine-tuned by us and run on hardware we "
            "operate. The base model's licence and notice are kept "
            "with the weights.",
            "When pip 1.0 cannot answer, a reply may come from a "
            "third-party model instead, and image generation is "
            "served by a third-party model. Where another model's "
            "name is shown in the app, it is there so you know which "
            "one answered - it is not a claim of affiliation with, "
            "endorsement by, or partnership with whoever makes that "
            "model. Which one answered is labelled on every reply.",
        ]},
        {"heading": "Sexual content is not permitted", "body": [
            "You may not use this service to generate, request or store "
            "sexually explicit or pornographic material of any kind.",
            ["This includes explicit sexual imagery, written "
             "pornography, and sexual roleplay or companion "
             "conversations.",
             "It includes any sexual content involving a minor, or "
             "anyone depicted as a minor. There is no context in which "
             "this is acceptable, and we report it.",
             "It includes sexual or intimate imagery of a real, "
             "identifiable person, whether or not they are famous, and "
             "whether or not you say it is fictional."],
            "The service is filtered to refuse this material. Attempting "
            "to defeat that filter is itself a breach of these rules.",
        ]},
        {"heading": "Real people and likenesses", "body": [
            "You may not use this service to swap faces, produce "
            "deepfakes, or manipulate the face or body of a real person. "
            "You may not generate images intended to pass as genuine "
            "photographs of a real, identifiable person.",
            "You may not impersonate a real person or organisation, or "
            "produce material presented as coming from them.",
        ]},
        {"heading": "Other things you may not do", "body": [
            ["Break the law, or help anyone else to.",
             "Harass, threaten, bully or defame anyone.",
             "Produce material designed to attack, break into or "
             "disrupt systems you do not own.",
             "Produce malware, ransomware, or tools whose purpose is to "
             "cause damage.",
             "Generate content promoting self-harm, suicide, eating "
             "disorders, or violence against a person or group.",
             "Produce deliberate disinformation, or content designed to "
             "manipulate an election.",
             "Sell or give away access to your account, or resell "
             "output as though it were a service of your own.",
             "Run automated traffic through the service beyond ordinary "
             "personal use."],
        ]},
        {"heading": "How this is enforced", "body": [
            "Prompts are filtered before they reach a model, and "
            "generated images are checked before they are shown. "
            "Accounts used for anything on this page can be suspended "
            "without a refund of time already used, and material "
            "involving the sexual abuse of children is reported to the "
            "appropriate authorities.",
            "If you think something was blocked wrongly, or you want to "
            "report misuse, email "
            "<a href=\"mailto:%s\">%s</a>." % (SUPPORT_EMAIL,
                                               SUPPORT_EMAIL),
        ]},
    ])


@app.route("/refunds")
def refunds_page():
    return _legal("refunds", "Refund & Cancellation Policy", [
        {"heading": "Cancelling", "body": [
            "Cancel any time from Settings in the app, or through the "
            "link in any Paddle receipt. Cancellation stops the next "
            "renewal.",
            "Pro stays active until the end of the period you have "
            "already paid for. You are not cut off the moment you cancel.",
        ]},
        {"heading": "Refunds", "body": [
            "If Pro is not what you expected, email us within "
            "<strong>14 days</strong> of a payment and we will refund it "
            "in full. No explanation required.",
            "After 14 days we do not normally refund a period already "
            "under way, because the credits for it were available to use. "
            "Ask anyway if something went wrong - a service failure, a "
            "duplicate charge, a subscription you thought you had "
            "cancelled - and we will sort it out.",
        ]},
        {"heading": "Things we always refund", "body": [
            ["a charge after you cancelled",
             "a duplicate or accidental charge",
             "a period where the service was substantially unavailable"],
        ]},
        {"heading": "How to ask", "body": [
            "Email <a href=\"mailto:%s\">%s</a> from the address on the "
            "account, with the date of the charge. Refunds are issued by "
            "Paddle to the original payment method and usually appear "
            "within 5-10 working days." % (SUPPORT_EMAIL, SUPPORT_EMAIL),
        ]},
        {"heading": "The free tier", "body": [
            "Free costs nothing and involves no payment, so nothing to "
            "refund. You can stop using it at any time.",
        ]},
    ])


@app.route("/contact")
def contact_page():
    return _legal("contact", "Contact", [
        {"heading": "Getting in touch", "body": [
            "One address for everything - support, billing, privacy "
            "requests, bug reports and security issues: "
            "<a href=\"mailto:%s\">%s</a>." % (SUPPORT_EMAIL, SUPPORT_EMAIL),
            "We aim to reply within two working days.",
        ]},
        {"heading": "What to include", "body": [
            "For anything account-related, write from the email address "
            "on the account and say what you were doing when it went "
            "wrong. For a billing question, the date and amount of the "
            "charge is usually enough to find it.",
        ]},
        {"heading": "Security", "body": [
            "If you have found a vulnerability, please report it to the "
            "same address before disclosing it publicly, and give us a "
            "reasonable window to fix it. We will not pursue anyone who "
            "reports a genuine issue in good faith.",
        ]},
        {"heading": "Payments", "body": [
            "Subscriptions are handled by Paddle as merchant of record. "
            "Charges appear on statements under Paddle's name rather than "
            "ours, which is normal and not a fraudulent charge.",
        ]},
    ])


@app.route("/robots.txt")
def robots_txt():
    """What search engines may crawl.

    The legal pages and the download page are worth indexing. The app -
    which / now serves as well - renders nothing useful without a
    session, so a crawler would file an empty shell under the site's
    name. /api/ is machine endpoints. The upload and generated
    directories hold users' own files and images, which must never turn
    up in search results.

    The sitemap line is built from the live host rather than hardcoded,
    so this stays correct on a .pythonanywhere.com or .onrender.com
    address as well as the real domain.
    """
    body = (
        "User-agent: *\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /refunds\n"
        "Allow: /acceptable-use\n"
        "Allow: /download\n"
        "Allow: /contact\n"
        "Disallow: /$\n"
        "Disallow: /app\n"
        "Disallow: /api/\n"
        "Disallow: /static/generated/\n"
        "Disallow: /static/uploads/\n"
        "\n"
        f"Sitemap: {request.url_root}sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """The legal pages, the download page and contact - everything that
    is meant to be indexed, and nothing that is not. A sitemap listing
    pages that should not be indexed actively works against you. The
    root is the app now, and the app is not for crawlers."""
    pages = [
        ("terms", "yearly", "0.3"),
        ("privacy", "yearly", "0.3"),
        ("refunds", "yearly", "0.3"),
        ("acceptable-use", "yearly", "0.3"),
        # Monthly and higher priority than the legal pages: this one
        # changes whenever a build ships, and it is a page people are
        # meant to arrive at rather than consult.
        ("download", "monthly", "0.7"),
        ("contact", "yearly", "0.4"),
    ]
    entries = "".join(
        "  <url>\n"
        f"    <loc>{request.url_root}{path}</loc>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        "  </url>\n"
        for path, freq, prio in pages
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + entries +
        "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


# THE APP IS THE FRONT DOOR.
#
# There was a marketing page at / - hero, feature cards, pricing, FAQ,
# over a desert shot - with the app one click away at /app. It is gone
# by request: the product is the four bays, and a visitor should land
# in them. Both paths serve the same page rather than / redirecting,
# so a person arriving at the root is one visit on the dashboard, not
# two, and every existing link to /app keeps working. Pricing lives in
# Settings > Plan, where it always also was.
@app.route("/")
@app.route("/app")
def index():
    return render_template("index.html", plan_perks=plan_perks(),
                           plans=PLANS,
                           accept_types=attachments.accept_attribute())


# ----------------------------------------------------------------------
# Accounts - two ways in, one session cookie either way. Google sign-in
# needs the server owner's own Cloud Console setup (see GOOGLE_CLIENT_ID
# in .env), so local email/password signup exists as the path that
# always works with nothing to configure - not a second-class fallback,
# just the one that doesn't depend on an external service being wired up.
# ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _find_or_create_user(email, password_hash="", google_id=None):
    """Look up an account by email, or make one. -> (uid, created).

    ATOMIC, and that is the point. Both callers used to do this as two
    steps - check USERS for the email, then create if absent - with
    nothing between them. Two requests arriving together (a double-
    clicked Google button is enough) both saw no account and both made
    one, leaving two entries with the same email in USERS.

    db.save_users() then deletes every row and reinserts them, so the
    duplicate hit the UNIQUE constraint on users.email and threw. Not
    once: from then on EVERY save failed, so nothing about any account
    could be persisted at all. It surfaced as a 500 on /api/credits,
    which is merely where the next save happened to be attempted.

    USERS_LOCK is an RLock so this can hold it across the check, the
    insert and save_users() - which takes the same lock - without the
    deadlock that a plain Lock would give.
    """
    with USERS_LOCK:
        existing = next((u for u in USERS.values()
                         if (u.get("email") or "") == email), None)
        if existing:
            return existing["id"], False
        return _create_user(email, password_hash=password_hash,
                            google_id=google_id), True


def _create_user(email, password_hash="", google_id=None):
    """Adds a new row to USERS and returns its id.

    Prefer _find_or_create_user() from request paths: this one assumes
    the caller has already established the email is free, and doing that
    outside the lock is what produced duplicate accounts.
    """
    uid = str(uuid.uuid4())
    USERS[uid] = {
        "id": uid,
        "email": email,
        "password_hash": password_hash,
        "google_id": google_id,
        "plan": "free",
        "created": now_iso(),
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "subscription_status": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
        "credits": {
            "balance": PLANS["free"]["cap"],
            "starting": PLANS["free"]["cap"],
            "plan": "free",
            "last_refill": now_iso(),
        },
    }
    save_users()
    db.save_credits(uid, USERS[uid]["credits"])
    return uid


def _migrate_guest_threads(uid):
    """Carries this browser's guest chats over to the now-signed-in
    account instead of orphaning them - credits don't carry over (a new
    account starts at the free-plan balance), just the conversations."""
    guest_id = session.pop("guest_id", None)
    if not guest_id:
        return
    with DATA_LOCK:
        moved = [t for t in THREADS.values() if t.get("owner_id") == guest_id]
        for t in moved:
            t["owner_id"] = uid
    if moved:
        db.reassign_threads(guest_id, uid)
        db.reassign_sites(guest_id, uid)
    # Projects are moved whether or not any conversation was: a guest
    # can make a project before writing anything in it.
    db.reassign_projects(guest_id, uid)


# WHERE ACCOUNTS AND BILLING ACTUALLY LIVE.
#
# The desktop build spawns this same server on a loopback port, so
# everything below works there EXCEPT the two things that need a fixed,
# registered public address:
#
#   Google sign-in. The redirect_uri has to match one registered in
#   Cloud Console exactly, and the desktop app takes a FRESH RANDOM PORT
#   on every launch (findFreePort in desktop/src/backend.js). There is
#   no port to register, so no OAuth flow can complete there - copying
#   the client id and secret onto the machine would not change that.
#
#   Paddle checkout. It only opens on a domain Paddle has approved.
#   127.0.0.1 is not one and cannot become one.
#
# So neither is a missing key on the desktop; both are things that
# belong to the website. RG_DESKTOP is set by backend.js, and the two
# endpoints below hand it to the browser so the UI can offer a way
# through instead of a disabled button and a "not configured" note.
IS_DESKTOP = os.environ.get("RG_DESKTOP") == "1"
PUBLIC_SITE_URL = os.environ.get(
    "PUBLIC_SITE_URL", "https://randomgenerals.com").rstrip("/")


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    uid = session.get("user_id")
    if uid and uid in USERS:
        return jsonify({"user": public_user(USERS[uid])})
    return jsonify({
        "user": None,
        "google_configured": google_oauth_configured(),
        "desktop": IS_DESKTOP,
        "site_url": PUBLIC_SITE_URL,
    })


@app.route("/api/auth/signup", methods=["POST"])
@limited("signup", LIMIT_SIGNUP_IP, key=_limit_key_ip)
def auth_signup():
    payload = request.get_json(force=True, silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password needs at least 8 characters."}), 400
    if any(u["email"] == email for u in USERS.values()):
        return jsonify({"error": "An account with that email already exists."}), 409

    name, birth_year, err = _validate_profile(payload.get("name"),
                                              payload.get("age"))
    if err:
        return jsonify({"error": err}), 400

    # Checked again, atomically. The check above is kept because it
    # answers with the right status code for the ordinary case; this one
    # closes the gap between that check and the insert, where two
    # simultaneous signups for the same address both used to get through.
    uid, created = _find_or_create_user(
        email, password_hash=generate_password_hash(password))
    if not created:
        return jsonify({"error": "An account with that email already exists."}), 409
    USERS[uid]["name"] = name
    USERS[uid]["birth_year"] = birth_year
    save_users()
    _start_session(uid)
    _migrate_guest_threads(uid)
    session["user_id"] = uid
    return jsonify({"user": public_user(USERS[uid])})


@app.route("/api/auth/login", methods=["POST"])
@limited("login-ip", LIMIT_LOGIN_IP, key=_limit_key_ip)
@limited("login-email", LIMIT_LOGIN_EMAIL, key=_limit_key_email)
def auth_login():
    payload = request.get_json(force=True, silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    user = next((u for u in USERS.values() if u["email"] == email), None)
    if not user or not user.get("password_hash"):
        # Same message whether the email doesn't exist or it's a
        # Google-only account with no password set - confirming which
        # one is true would leak which emails have accounts here.
        return jsonify({"error": "Wrong email or password."}), 401
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Wrong email or password."}), 401

    _migrate_guest_threads(user["id"])
    session["user_id"] = user["id"]
    _start_session(user["id"])
    return jsonify({"user": public_user(user)})


@app.route("/api/auth/google/login")
def google_login():
    if not google_oauth_configured():
        return ("Google sign-in isn't configured yet - see GOOGLE_CLIENT_ID "
                "/ GOOGLE_CLIENT_SECRET in .env."), 503
    state = uuid.uuid4().hex
    session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": url_for("google_callback", _external=True),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/api/auth/google/callback")
def google_callback():
    if not google_oauth_configured():
        return redirect("/app?auth_error=not_configured")
    expected = session.pop("oauth_state", None)
    if not request.args.get("state") or request.args.get("state") != expected:
        return redirect("/app?auth_error=state_mismatch")
    code = request.args.get("code")
    if not code:
        return redirect("/app?auth_error=denied")

    try:
        token_res = requests.post(GOOGLE_TOKEN_URL, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": url_for("google_callback", _external=True),
        }, timeout=10)
        token_res.raise_for_status()
        access_token = token_res.json()["access_token"]

        info_res = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        info_res.raise_for_status()
        info = info_res.json()
    except requests.exceptions.RequestException:
        return redirect("/app?auth_error=google_unreachable")

    email = (info.get("email") or "").strip().lower()
    if not email or not info.get("email_verified"):
        return redirect("/app?auth_error=unverified_email")

    # One atomic step. A double-clicked sign-in button fires this
    # callback twice, and as a look-then-create it made two accounts
    # with the same address - which then broke every subsequent save.
    uid, created = _find_or_create_user(email, google_id=info.get("sub"))
    if not created:
        user = USERS[uid]
        # A local-signup account signing in with Google for the first
        # time - link it rather than silently ignoring the Google id.
        if not user.get("google_id"):
            user["google_id"] = info.get("sub")
            save_users()

    _migrate_guest_threads(uid)
    session["user_id"] = uid
    return redirect("/app")


# ----------------------------------------------------------------------
# Account management: verify, change password, delete.
#
# mailer.py and the verification_codes table were both written and
# neither was ever called - the code to send a six-digit code existed,
# and nothing asked it to. These are the routes that use them.
# ----------------------------------------------------------------------
VERIFY_TTL_MINUTES = 15


def _issue_code(email, name=""):
    """Mint a code, store it, mail it. -> (sent, detail)."""
    code = "%06d" % secrets.randbelow(1000000)
    expires = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(minutes=VERIFY_TTL_MINUTES))
    db.save_verification_code(email, code, expires.isoformat())
    return mailer.send_verification_code(email, code, VERIFY_TTL_MINUTES,
                                         name=name)


@app.route("/api/auth/verify/send", methods=["POST"])
@limited("verify-send", LIMIT_VERIFY_SEND, key=_limit_key_account)
def verify_send():
    """Send a fresh code to the signed-in account's address."""
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    if user.get("email_verified"):
        return jsonify({"ok": True, "already": True})
    email = (user.get("email") or "").strip()
    if not email:
        return jsonify({
            "error": "This account has no email address - it was "
                     "created through Google sign-in.",
        }), 400

    sent, detail = _issue_code(email, name=user.get("name") or "")
    # sent=False is the console fallback, which is a working state on
    # a server with no SMTP - not an error to report as one.
    return jsonify({
        "ok": True,
        "delivered": bool(sent),
        "detail": ("Code sent - check your inbox." if sent else
                   "No mail server is configured here, so the code was "
                   "printed to the server log."),
    })


@app.route("/api/auth/verify/confirm", methods=["POST"])
def verify_confirm():
    payload = request.get_json(force=True, silent=True) or {}
    code = (payload.get("code") or "").strip()
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    email = (user.get("email") or "").strip()

    row = db.get_verification_code(email)
    if not row:
        return jsonify({
            "error": "No code outstanding. Send a new one.",
        }), 400
    try:
        expires = datetime.datetime.fromisoformat(row["expires_at"])
    except (ValueError, KeyError, TypeError):
        expires = None
    if expires and datetime.datetime.now(datetime.timezone.utc) > expires:
        db.delete_verification_code(email)
        return jsonify({
            "error": "That code has expired. Send a new one.",
        }), 400

    # Constant-time compare. A six-digit code is small enough that a
    # timing signal on the first differing character is worth denying.
    if not hmac.compare_digest(str(row["code"]), code):
        # A guessing budget, the same one password resets have. Six
        # digits is a million combinations - a lot for a person, an
        # afternoon for a script - and this route had no counter at
        # all, so the code lived out its full fifteen minutes however
        # many wrong tries arrived.
        if db.bump_verification_attempts(email) >= RESET_MAX_ATTEMPTS:
            db.delete_verification_code(email)
            return jsonify({
                "error": "Too many wrong tries. Send a new code.",
            }), 400
        return jsonify({"error": "That code is not right."}), 400

    db.delete_verification_code(email)
    user["email_verified"] = True
    save_users()
    return jsonify({"ok": True, "user": public_user(user)})


@app.route("/api/account/password", methods=["POST"])
def change_password():
    payload = request.get_json(force=True, silent=True) or {}
    current = payload.get("current") or ""
    new = payload.get("new") or ""
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401

    if not user.get("password_hash"):
        return jsonify({
            "error": "This account signs in with Google, so it has no "
                     "password to change.",
        }), 400
    if not check_password_hash(user["password_hash"], current):
        return jsonify({"error": "Current password is not right."}), 400
    if len(new) < 8:
        return jsonify({
            "error": "Use at least 8 characters.",
        }), 400
    if new == current:
        return jsonify({
            "error": "That is the password you already have.",
        }), 400

    user["password_hash"] = generate_password_hash(new)
    save_users()
    # The session deliberately survives: signing someone out of the
    # tab where they just changed their password is a punishment for
    # doing the right thing. Other sessions are not tracked here, so
    # there is nothing else to invalidate.
    return jsonify({"ok": True})


@app.route("/api/account/delete", methods=["POST"])
def delete_account():
    """Permanent, and it means it - see db.delete_user()."""
    payload = request.get_json(force=True, silent=True) or {}
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401

    # Typing the address is the confirmation. A yes/no dialog is too
    # easy to click through for something with no undo, and this is
    # the one action in the app that destroys data on purpose.
    typed = (payload.get("confirm_email") or "").strip().lower()
    email = (user.get("email") or "").strip().lower()
    if not email or typed != email:
        return jsonify({
            "error": "Type your email address exactly to confirm.",
        }), 400

    # A live Paddle subscription is cancelled NOW, at Paddle, before the
    # row goes: deleting first would leave Paddle billing a customer
    # this app could no longer recognise or refund. If Paddle will not
    # cancel it, the account stays - and says why - because the other
    # order is exactly the outcome this exists to prevent. (The old
    # guard merely refused while the status was active, and "Downgrade
    # to Free" nulled the status locally without cancelling anything,
    # so the two together deleted a still-billing customer.)
    if _paddle_subscription_live(user):
        _state, err = paddle_billing.cancel_subscription(
            user["paddle_subscription_id"], immediately=True)
        if err:
            return jsonify({
                "error": "Your Pro subscription could not be cancelled at "
                         "Paddle just now, so the account was not deleted. "
                         "Try again in a moment.",
                "detail": err,
            }), 502
    elif (user.get("subscription_status") or "") in ("active", "trialing"):
        # A live subscription this app cannot cancel itself (Stripe).
        return jsonify({
            "error": "Cancel your Pro subscription first, so you are "
                     "not billed for an account that no longer exists.",
        }), 400

    removed = db.delete_user(uid, email)
    USERS.pop(uid, None)
    with DATA_LOCK:
        for tid in [t for t, v in THREADS.items()
                    if v.get("owner_id") == uid]:
            THREADS.pop(tid, None)
    session.clear()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    # Revoke the row as well as dropping the cookie. Without this the
    # session would keep showing in the device list of every other
    # signed-in device, as one that never ends.
    sid = session.get("sid")
    uid = session.get("user_id")
    if sid and uid:
        try:
            db.revoke_session(uid, sid, now_iso())
        except Exception:                    # noqa: BLE001
            pass
    session.pop("user_id", None)
    session.pop("sid", None)
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# Subscription. Downgrading to free is always instant and free of charge.
# Upgrading to pro goes through real Stripe Checkout - card details are
# entered on Stripe's own hosted page and never touch this server, which
# is what keeps this out of PCI-DSS scope entirely. Stripe is the source
# of truth for subscription state; the webhook below mirrors it locally.
#
# ALLOW_MOCK_UPGRADE exists so the plan UI can be exercised before a
# Stripe account exists, and is deliberately gated to a debug instance
# on an explicit opt-in env var. It used to be the silent fallback
# whenever billing wasn't configured, which meant any signed-in user on
# a public deployment could POST /api/subscribe and grant themselves Pro
# for free - a real hole, since this app is reachable over a tunnel.
# ----------------------------------------------------------------------
ALLOW_MOCK_UPGRADE = os.environ.get("ALLOW_MOCK_UPGRADE") == "1"


def _may_mock_upgrade(user):
    """Whether THIS user may grant themselves Pro without paying.

    The env var alone was never safe enough for a deployment that is
    live on a public tunnel: switching it on to give the owner Pro also
    gives it to every visitor for as long as it stays on, and the whole
    reason it gets switched on is that somebody is in a hurry.

    So it is now two conditions, not one. The flag says the escape hatch
    exists on this instance; ADMIN_EMAIL says whose it is. With
    ADMIN_EMAIL unset there is no owner and the hatch stays shut for
    everybody, which is the same fail-closed rule /stats uses.
    """
    if not ALLOW_MOCK_UPGRADE:
        return False
    if not ADMIN_EMAIL:
        return False
    return (user.get("email") or "").strip().lower() == ADMIN_EMAIL


def _apply_plan(user, plan):
    user["plan"] = plan
    user["credits"]["plan"] = plan
    user["credits"]["balance"] = PLANS[plan]["cap"]
    user["credits"]["starting"] = PLANS[plan]["cap"]
    user["credits"]["last_refill"] = now_iso()
    # The credits row is written here, by name, because save_users() no
    # longer carries it - see db.save_users.
    db.save_credits(user["id"], user["credits"])
    if plan == "free":
        user["stripe_subscription_id"] = None
        user["subscription_status"] = None
        user["current_period_end"] = None
        user["cancel_at_period_end"] = False


def _sv(obj, key, default=None):
    """Read a field from a Stripe object OR a plain dict.

    stripe-python 15's StripeObject is NOT a dict subclass and has no
    .get() - calling it raises AttributeError. It does support
    obj["key"] and `key in obj`. Since the same helpers here receive
    both real StripeObjects (from .retrieve()) and plain dicts (from
    tests and from json payloads), every read goes through this rather
    than assuming one shape.
    """
    try:
        if key in obj:
            value = obj[key]
            return default if value is None else value
    except (TypeError, KeyError, AttributeError):
        pass
    return getattr(obj, key, default)


def _period_end_timestamp(sub):
    """Unix timestamp when the current billing period ends, or None.

    Stripe moved `current_period_end` off the subscription and onto each
    subscription *item* - it is not a declared attribute of Subscription
    in the current library. Reading it directly off the subscription
    silently yields nothing on modern API versions, so this checks the
    subscription first (older API versions still return it) and then
    falls back to the item, taking the latest period end if a
    subscription somehow has several items.
    """
    direct = _sv(sub, "current_period_end")
    if direct:
        return direct
    items = _sv(sub, "items")
    data = _sv(items, "data", []) if items is not None else []
    ends = [_sv(i, "current_period_end") for i in (data or [])]
    ends = [e for e in ends if e]
    return max(ends) if ends else None


def _iso_from_timestamp(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat()


def _sync_subscription(user, sub):
    """Mirror a Stripe subscription object onto the local user record."""
    user["stripe_subscription_id"] = _sv(sub, "id")
    user["subscription_status"] = _sv(sub, "status")
    user["cancel_at_period_end"] = bool(_sv(sub, "cancel_at_period_end"))
    user["current_period_end"] = _iso_from_timestamp(
        _period_end_timestamp(sub))


@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first to manage your plan."}), 401

    payload = request.get_json(force=True, silent=True) or {}
    plan = payload.get("plan")
    if plan not in PLANS:
        return jsonify({"error": "Unknown plan"}), 400

    user = USERS[uid]

    if plan == "pro":
        # KELVIQ FIRST. It is the payment provider this site uses: a
        # merchant of record, like Paddle, so VAT and sales tax are its
        # problem. The account's own id is Kelviq's customer id, which
        # is how the webhook finds the account again.
        if kelviq_billing.configured():
            base = request.host_url.rstrip("/")
            url, err = kelviq_billing.create_checkout(
                user_id=uid, email=user["email"],
                # checkout=kelviq means "a checkout was in progress":
                # the page asks the server whether the upgrade has
                # landed, rather than announcing a payment it has not
                # seen confirmed.
                success_url=base + "/app?checkout=kelviq",
                cancel_url=base + "/app?checkout=cancel")
            if err:
                # 400, not 5xx - see the Paddle branch below for why.
                return jsonify({"error": "Could not start checkout. Try again "
                                         "in a moment.", "detail": err}), 400
            return jsonify({"checkout_url": url})

        # Paddle when it's configured. It is a merchant of record,
        # so it can sell from countries Stripe has no account type for -
        # Morocco among them - which is the only reason to prefer it.
        # Where both work, Stripe is cheaper, so this order only matters
        # on a deployment that has deliberately set Paddle up.
        if paddle_billing.configured():
            url, err = paddle_billing.create_checkout(
                user_id=uid,
                email=user["email"],
                # Paddle appends ?_ptxn=<id> to this. It is where the
                # overlay opens, not a "payment finished" landing page -
                # appending checkout=success here made the app announce
                # success before anyone had paid. checkout=paddle says
                # only that a Paddle checkout was in progress, so a
                # browser that comes back this way (the overlay could
                # not open, so it navigated) starts polling for the
                # upgrade rather than announcing it.
                return_url=request.host_url.rstrip("/") + "/app?checkout=paddle",
            )
            if err:
                # 400, NOT 502. A payment provider declining is not a
                # gateway failure, and the status matters for more than
                # correctness: Cloudflare substitutes its own error page
                # for a 5xx from the origin, so this JSON never reached
                # the browser. res.json() then threw, the catch in
                # changePlan() fired, and the person was told "Could not
                # reach the server" about a server that had answered
                # them in full.
                #
                # Paddle's own sentence is passed through. When it is the
                # account-level refusal, it is the only thing that
                # explains why a correctly configured Upgrade button
                # cannot work.
                friendly = err
                if "aren't enabled for this account" in err:
                    friendly = (
                        "Payments are not active on this site yet - the "
                        "payment provider has not finished approving the "
                        "account. Nothing is wrong with your card or your "
                        "sign-in, and Free carries on working normally.")
                return jsonify({"error": friendly, "detail": err}), 400
            return jsonify({"checkout_url": url})

        if billing_live():
            stripe = _stripe()
            try:
                checkout_kwargs = {
                    "mode": "subscription",
                    "line_items": [{"price": STRIPE_PRICE_ID_PRO, "quantity": 1}],
                    "client_reference_id": uid,
                    "success_url": request.host_url.rstrip("/") + "/app?checkout=success",
                    "cancel_url": request.host_url.rstrip("/") + "/app?checkout=cancel",
                }
                # Reuse the existing Stripe customer if this account has
                # subscribed before, so their saved payment methods and
                # billing history stay on one customer record instead of
                # a new one being created per checkout.
                if user.get("stripe_customer_id"):
                    checkout_kwargs["customer"] = user["stripe_customer_id"]
                else:
                    checkout_kwargs["customer_email"] = user["email"]
                checkout = stripe.checkout.Session.create(**checkout_kwargs)
            except stripe.StripeError as e:
                # 400 for the same reason as the Paddle branch above.
                return jsonify({"error": f"Could not start checkout: {e}"}), 400
            return jsonify({"checkout_url": checkout.url})

        if not _may_mock_upgrade(user):
            # Say precisely what's wrong rather than a generic "not
            # configured" - "your key is a placeholder" and "you haven't
            # set a key" need different actions from whoever runs this.
            return jsonify({
                "error": "Payments aren't set up on this server yet, so Pro "
                         "can't be purchased.",
                "detail": billing_config_problem(),
                "setup_url": "https://dashboard.stripe.com/apikeys",
            }), 503

    if plan == "free" and _kelviq_subscription_live(user):
        return _cancel_kelviq(user)

    if plan == "free" and _paddle_subscription_live(user):
        # A PAYING CUSTOMER LEAVING IS A CANCELLATION AT PADDLE, not a
        # local plan change. This used to set plan=free and stop: they
        # lost Pro that minute and Paddle kept charging them monthly
        # for a subscription the app no longer showed them.
        return _cancel_paddle(user)

    _apply_plan(user, plan)
    save_users()
    return jsonify({"user": public_user(user), "credits": credits_view(user["credits"])})


def _paddle_subscription_live(user: dict) -> bool:
    """Is there a Paddle subscription that is still billing, or would
    be? Includes one scheduled to cancel: it is still theirs to keep."""
    return bool(paddle_billing.configured()
                and user.get("paddle_subscription_id")
                and (user.get("subscription_status") or "")
                in paddle_billing.ACTIVE_STATUSES)


def _kelviq_subscription_live(user: dict) -> bool:
    """A Kelviq subscription that is billing, or scheduled to stop."""
    return bool(kelviq_billing.configured()
                and user.get("kelviq_subscription_id")
                and (user.get("subscription_status") or "")
                in kelviq_billing.ACTIVE_STATUSES)


def _cancel_kelviq(user: dict, immediately: bool = False):
    """Cancel at Kelviq (at the end of the paid period) and apply it."""
    state, err = kelviq_billing.cancel_subscription(
        user["kelviq_subscription_id"], immediately=immediately)
    if err:
        return jsonify({"error": "Kelviq could not cancel the subscription "
                                 "just now. Try again in a moment.",
                        "detail": err}), 502
    state["occurred_at"] = now_iso()
    # The period end is what it already was; Kelviq's reply carries none.
    state["current_period_end"] = user.get("current_period_end")
    _apply_subscription_state(user, state, force=True, provider="kelviq")
    return jsonify({"user": public_user(user),
                    "credits": credits_view(user["credits"]),
                    "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
                    "current_period_end": user.get("current_period_end"),
                    "status": user.get("subscription_status")})


def _cancel_paddle(user: dict, immediately: bool = False):
    """Cancel at Paddle and apply what Paddle says back. -> a response."""
    state, err = paddle_billing.cancel_subscription(
        user["paddle_subscription_id"], immediately=immediately)
    if err:
        # 502: the request was right and the provider could not act on
        # it. The person keeps what they have; nothing is half-done.
        return jsonify({"error": "Paddle could not cancel the subscription "
                                 "just now. Try again in a moment.",
                        "detail": err}), 502
    state["occurred_at"] = state.get("occurred_at") or now_iso()
    _apply_subscription_state(user, state, force=True)
    return jsonify({"user": public_user(user),
                    "credits": credits_view(user["credits"]),
                    "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
                    "current_period_end": user.get("current_period_end"),
                    "status": user.get("subscription_status")})


@app.route("/api/billing/cancel", methods=["POST"])
def billing_cancel():
    """Cancel at the end of the paid period. Pro stays until then."""
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    if _kelviq_subscription_live(user):
        return _cancel_kelviq(user)
    if _paddle_subscription_live(user):
        return _cancel_paddle(user)
    if user["plan"] == "pro":
        # Pro without a Paddle subscription - a mock upgrade or a plan
        # granted by hand. Nothing to cancel anywhere; just step down.
        _apply_plan(user, "free")
        save_users()
        return jsonify({"user": public_user(user),
                        "credits": credits_view(user["credits"])})
    return jsonify({"error": "There is no subscription to cancel."}), 400


@app.route("/api/billing/resume", methods=["POST"])
def billing_resume():
    """Keep a subscription that was scheduled to cancel."""
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    if _kelviq_subscription_live(user) and user.get("cancel_at_period_end"):
        # Kelviq's API has no "undo cancel"; its customer portal does.
        return jsonify({"error": "To keep your subscription, open Manage "
                                 "payment and resume it there.",
                        "portal": True}), 400
    if not (_paddle_subscription_live(user) and user.get("cancel_at_period_end")):
        return jsonify({"error": "There is no cancellation to undo."}), 400
    state, err = paddle_billing.resume_subscription(user["paddle_subscription_id"])
    if err:
        return jsonify({"error": "Paddle could not resume the subscription "
                                 "just now. Try again in a moment.",
                        "detail": err}), 502
    state["occurred_at"] = state.get("occurred_at") or now_iso()
    _apply_subscription_state(user, state, force=True)
    return jsonify({"user": public_user(user),
                    "credits": credits_view(user["credits"]),
                    "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
                    "current_period_end": user.get("current_period_end"),
                    "status": user.get("subscription_status")})


@app.route("/api/billing/portal", methods=["POST"])
def billing_portal():
    """Hands the user to Stripe's own hosted Billing Portal to update a
    card, view invoices, or cancel - all of which are things Stripe
    should own rather than this app reimplementing them against the API
    (and re-inheriting the PCI scope that comes with touching card data).
    """
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401

    # Kelviq's portal for a Kelviq customer - whose customer id is the
    # account's own id.
    if kelviq_billing.configured() and USERS[uid].get("kelviq_subscription_id"):
        url, err = kelviq_billing.portal_session(uid)
        if err:
            return jsonify({"error": "Could not open the billing portal.",
                            "detail": err}), 502
        return jsonify({"portal_url": url})

    # Paddle's portal for a Paddle customer. This route knew only
    # Stripe's, so on the deployment that actually takes money the
    # "Manage payment" button answered 503 to every paying customer.
    if paddle_billing.configured() and USERS[uid].get("paddle_customer_id"):
        url, err = paddle_billing.portal_session(USERS[uid]["paddle_customer_id"])
        if err:
            return jsonify({"error": "Could not open the billing portal.",
                            "detail": err}), 502
        return jsonify({"portal_url": url})

    if not billing_live():
        return jsonify({"error": "Billing isn't configured on this server."}), 503

    customer_id = USERS[uid].get("stripe_customer_id")
    if not customer_id:
        return jsonify({"error": "No billing account yet - subscribe first."}), 400

    stripe = _stripe()
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=request.host_url.rstrip("/") + "/app",
        )
    except stripe.StripeError as e:
        return jsonify({"error": f"Could not open billing portal: {e}"}), 502
    return jsonify({"portal_url": portal.url})


@app.route("/api/billing/subscription", methods=["GET"])
def billing_subscription():
    """Current plan + renewal state for the account dashboard."""
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    user = USERS[uid]
    credits = credits_view(user["credits"])
    return jsonify({
        "plan": user["plan"],
        "plan_label": PLANS[user["plan"]]["label"],
        "price": PLANS[user["plan"]]["price"],
        "status": user.get("subscription_status"),
        "current_period_end": user.get("current_period_end"),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "billing_live": (billing_live() or paddle_billing.configured()
                         or kelviq_billing.configured()),
        # Which processor holds the card, so the panel can name it rather
        # than saying "your payment provider".
        "processor": ("Kelviq" if kelviq_billing.configured()
                      else "Paddle" if paddle_billing.configured() else "Stripe"),
        # Paddle was missing here. This checked only for a Stripe customer
        # id, so on a Paddle deployment - which this one is - a paying
        # subscriber never saw the button that manages their own
        # subscription.
        "has_billing_account": bool(user.get("stripe_customer_id")
                                    or user.get("paddle_customer_id")
                                    or user.get("kelviq_subscription_id")),
        # The account itself. The panel is where someone goes to check
        # what they are being charged for, and "which account is this?"
        # is part of that question.
        "email": user.get("email") or "",
        "email_verified": bool(user.get("email_verified")),
        "created": user.get("created") or "",
        "credits": {
            "balance": credits.get("balance"),
            "cap": credits.get("cap"),
            "next_refill_in": credits.get("next_refill_in"),
        },
    })


@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    """Stripe calls this directly (no session cookie) when a checkout
    completes or a subscription's status changes. Signature verification
    is what proves a request actually came from Stripe and not anyone who
    found this URL - see STRIPE_WEBHOOK_SECRET in .env."""
    if not webhook_ready() or STRIPE_WEBHOOK_SECRET is None:
        # Refusing outright when the signing secret is missing is the
        # safe failure: without it there is no way to distinguish a real
        # Stripe callback from anyone who found this URL, and a webhook
        # that can't verify must never act on what it's told.
        return jsonify({"error": "Billing webhook not configured"}), 503

    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(
            request.get_data(), request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.SignatureVerificationError):
        return jsonify({"error": "Invalid signature"}), 400

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        user = USERS.get(_sv(obj, "client_reference_id"))
        if user:
            _apply_plan(user, "pro")
            if _sv(obj, "customer"):
                user["stripe_customer_id"] = obj["customer"]
            # Pull the full subscription so the account UI has a real
            # renewal date immediately, rather than waiting for the first
            # customer.subscription.updated event to arrive.
            sub_id = _sv(obj, "subscription")
            if sub_id:
                try:
                    _sync_subscription(
                        user, stripe.Subscription.retrieve(sub_id))
                except stripe.StripeError:
                    user["stripe_subscription_id"] = sub_id
            save_users()

    elif etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        user = next((u for u in USERS.values()
                     if u.get("stripe_customer_id") == _sv(obj, "customer")), None)
        if user:
            cancelled = (
                etype == "customer.subscription.deleted"
                or _sv(obj, "status") in ("canceled", "unpaid", "incomplete_expired")
            )
            if cancelled:
                if user["plan"] != "free":
                    _apply_plan(user, "free")
            else:
                # Still an active/trialing subscription - keep Pro, but
                # record whether it's set to lapse at period end so the
                # dashboard can say "cancels on <date>" rather than
                # implying it renews.
                if user["plan"] != "pro":
                    _apply_plan(user, "pro")
                _sync_subscription(user, obj)
            save_users()

    return jsonify({"received": True})


@app.route("/api/billing/kelviq/webhook", methods=["POST"])
def kelviq_webhook():
    """Kelviq calls this when a subscription changes or an invoice is paid.

    Public and unauthenticated, so the signature is the only thing that
    separates Kelviq from anyone POSTing themselves a Pro plan. It fails
    closed: no secret configured, nothing is trusted.
    """
    ok, reason = kelviq_billing.verify_webhook(request.get_data(), request.headers)
    if not ok:
        # 400, not 5xx: a delivery that fails verification will fail the
        # same way every retry.
        return jsonify({"error": f"Rejected: {reason}"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    event_id = str(payload.get("id") or request.headers.get("webhook-id") or "")
    if event_id and db.webhook_seen("kelviq", event_id, str(payload.get("type") or "")):
        return jsonify({"received": True, "duplicate": True})

    payment = kelviq_billing.parse_payment(payload)
    if payment:
        payer = USERS.get(payment["user_id"])
        if payment["txn_id"]:
            db.record_payment(
                payment["txn_id"], payment["user_id"],
                payment["email"] or (payer or {}).get("email", ""),
                payment["currency"], payment["gross"], payment["fee"],
                payment["earnings"], payment["created"])
        # A paid invoice on a subscription is an active subscription,
        # whether or not the subscription.* event arrives first.
        if payer and payment["subscription_id"]:
            _apply_subscription_state(payer, {
                "subscription_id": payment["subscription_id"],
                "status": "active", "active": True,
                "cancel_at_period_end": bool(payer.get("cancel_at_period_end")),
                "current_period_end": None,
                "occurred_at": payment["occurred_at"],
            }, provider="kelviq")
        return jsonify({"received": True, "handled": True, "recorded": "payment"})

    event = kelviq_billing.parse_event(payload)
    if not event:
        return jsonify({"received": True, "handled": False})
    user = USERS.get(str(event.get("user_id") or ""))
    if not user and event.get("subscription_id"):
        user = next((u for u in USERS.values()
                     if u.get("kelviq_subscription_id") == event["subscription_id"]),
                    None)
    if not user:
        return jsonify({"received": True, "handled": False, "reason": "unknown user"})
    applied = _apply_subscription_state(user, event, provider="kelviq")
    return jsonify({"received": True, "handled": True, "applied": applied})


@app.route("/api/billing/paddle/webhook", methods=["POST"])
def paddle_webhook():
    """Paddle calls this when a subscription is created or changes.

    Same trust model as the Stripe webhook above: this URL is public and
    unauthenticated, so the signature is the only thing distinguishing a
    real Paddle callback from anyone who guesses the path and POSTs
    themselves a Pro subscription. It therefore fails closed - no secret
    configured means the endpoint refuses outright rather than trusting
    the body.
    """
    # Layer one: is this even coming from Paddle's network? Cheap, and it
    # turns away path-guessers before any crypto runs. It is NOT the
    # protection - an address can be spoofed and a signature cannot - so
    # it fails open when Paddle's published list cannot be fetched, and
    # the signature below still has to pass either way. See
    # paddle_billing.ip_allowed for why open rather than closed.
    #
    # _client_ip() rather than remote_addr: gunicorn only listens on
    # 127.0.0.1 and the Cloudflare tunnel is what carries the request in,
    # so remote_addr is 127.0.0.1 for Paddle exactly as it is for
    # everybody else, and matching that against the allowlist would
    # reject every real webhook.
    allowed, why = paddle_billing.ip_allowed(_client_ip())
    if not allowed:
        print("[paddle] webhook refused: %s" % why)
        return jsonify({"error": "Not from Paddle."}), 403

    ok, reason = paddle_billing.verify_webhook(
        request.get_data(), request.headers.get("Paddle-Signature", ""))
    if not ok:
        # 400 rather than 403: Paddle retries on 5xx, and a request that
        # failed verification will fail identically every time, so
        # asking them to retry it forever helps nobody.
        return jsonify({"error": f"Rejected: {reason}"}), 400

    payload = request.get_json(force=True, silent=True) or {}

    # Money first, and independently of everything below. A payment is
    # recorded even when the account it belongs to cannot be found -
    # "somebody paid and we do not know who" is a fact worth having on
    # the dashboard, and the alternative was losing the record entirely.
    payment = paddle_billing.parse_payment(payload)
    if payment and payment["txn_id"]:
        payer = USERS.get(payment["user_id"])
        db.record_payment(
            payment["txn_id"], payment["user_id"],
            payment["email"] or (payer or {}).get("email", ""),
            payment["currency"], payment["gross"], payment["fee"],
            payment["earnings"], payment["created"])
        # A COMPLETED PAYMENT IS AN ACTIVE SUBSCRIPTION. The upgrade
        # used to wait for a separate subscription.* event; if that one
        # was delayed, or the destination was not subscribed to it, the
        # customer was charged and never upgraded. The payment is proof
        # enough, and the ordering rule below means a later, fuller
        # subscription event still applies on top.
        if payer and payment["subscription_id"]:
            _apply_subscription_state(payer, {
                "subscription_id": payment["subscription_id"],
                "customer_id": payment["customer_id"],
                "status": "active", "active": True,
                "cancel_at_period_end": False,
                "current_period_end": None,
                "occurred_at": payment["occurred_at"],
            })
        return jsonify({"received": True, "handled": True,
                        "recorded": "payment"})

    event = paddle_billing.parse_event(payload)
    if not event:
        # Acknowledged, not acted on. Returning an error for events we
        # simply don't handle would make Paddle retry them and
        # eventually disable the destination.
        return jsonify({"received": True, "handled": False})

    # user_id comes from custom_data set at checkout, which is the only
    # reliable link back to a local account - matching on email breaks
    # as soon as someone pays with a different address than they signed
    # up with. A subscription this app already knows the id of is
    # matched on that too, for events that arrive without custom_data.
    user = USERS.get(str(event.get("user_id") or ""))
    if not user and event.get("subscription_id"):
        user = next((u for u in USERS.values()
                     if u.get("paddle_subscription_id")
                     == event["subscription_id"]), None)
    if not user:
        return jsonify({"received": True, "handled": False,
                        "reason": "unknown user"})

    applied = _apply_subscription_state(user, event)
    return jsonify({"received": True, "handled": True,
                    "applied": applied})


def _apply_subscription_state(user: dict, state: dict,
                              force: bool = False,
                              provider: str = "paddle") -> bool:
    """The one place a subscription's state reaches an account.
    -> True if applied, False if it was older than what is stored.

    `force` is for a state this app just asked Paddle for - the
    response to its own cancel or resume. That is the truth as of now
    by definition, and a stamp comparison against it would only ever
    lose to clock skew between Paddle and this server.

    Paddle delivers at least once and in no promised order. Before
    this, events were applied as they arrived, so a redelivered or
    delayed "active" landing after a "canceled" re-granted Pro - with
    a full credit refill from _apply_plan. Each account remembers the
    occurred_at of the last state it applied; anything older is
    acknowledged and ignored. Equal is applied, so a redelivery of the
    same event is harmless (_apply_plan only refills on a real change).
    """
    newer = state.get("occurred_at") or ""
    known = user.get("subscription_updated_at") or ""
    if (not force and newer and known
            and _paddle_time(newer) < _paddle_time(known)):
        return False

    if state.get("active"):
        if user["plan"] != "pro":
            _apply_plan(user, "pro")
    elif user["plan"] != "free":
        _apply_plan(user, "free")

    if provider == "kelviq":
        # Kelviq's customer id is the account's own id, so only the
        # subscription needs remembering.
        if state.get("subscription_id"):
            user["kelviq_subscription_id"] = state["subscription_id"]
    else:
        if state.get("subscription_id"):
            user["paddle_subscription_id"] = state["subscription_id"]
        if state.get("customer_id"):
            user["paddle_customer_id"] = state["customer_id"]
    user["subscription_status"] = state.get("status")
    user["cancel_at_period_end"] = bool(state.get("cancel_at_period_end"))
    if state.get("current_period_end"):
        user["current_period_end"] = state["current_period_end"]
    if newer:
        user["subscription_updated_at"] = newer
    save_users()
    return True


def _paddle_time(value: str) -> datetime.datetime:
    """Paddle's RFC 3339 timestamps ("2026-09-12T10:00:00.123456Z") as
    something comparable. Unparseable means "epoch", so a malformed
    stamp is treated as old rather than crashing the webhook."""
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.datetime.fromtimestamp(0, datetime.timezone.utc)


# How far past its renewal date a Pro account may be before the
# reconciler asks Paddle about it even without a subscription id to go
# on. Paddle retries a failed renewal for a few days; three is inside
# that window, so a genuinely late payment is not mistaken for a lapse.
RECONCILE_GRACE_DAYS = 3

# Every six hours. Webhooks carry the news within seconds when they
# work; this is for when they do not, and a lapsed subscription being
# noticed a few hours late costs nothing anyone would notice.
RECONCILE_EVERY_SECONDS = 6 * 3600


def reconcile_subscriptions() -> dict:
    """Ask Paddle about every subscription this app thinks it has.
    -> {"checked": n, "changed": n, "errors": [...]}

    The webhook is how changes normally arrive, and it is at-least-once,
    not guaranteed: a destination disabled after repeated failures, a
    rotated secret, a tunnel outage, all leave a cancelled subscription
    marked active here indefinitely, because nothing compared
    current_period_end to the calendar. This is the second source of
    truth. It goes through _apply_subscription_state like a webhook, so
    the ordering rule holds and it can never apply something older
    than what a webhook already delivered.
    """
    out: dict[str, Any] = {"checked": 0, "changed": 0, "errors": []}
    if not paddle_billing.configured():
        out["errors"].append("Paddle is not configured")
        return out
    now = datetime.datetime.now(datetime.timezone.utc)
    grace = datetime.timedelta(days=RECONCILE_GRACE_DAYS)
    for user in list(USERS.values()):
        sub_id = user.get("paddle_subscription_id")
        lapsed = (user.get("plan") == "pro"
                  and user.get("current_period_end")
                  and _paddle_time(user["current_period_end"]) + grace < now)
        if not sub_id and not lapsed:
            continue
        if not sub_id:
            # Pro, past its renewal by more than the grace, and no
            # subscription to ask about - a Stripe-era or hand-granted
            # plan. Recorded rather than revoked: revoking on a guess
            # is worse than a dashboard line saying it needs a look.
            out["errors"].append("%s is Pro past %s with no Paddle "
                                 "subscription" % (user.get("email"),
                                                   user["current_period_end"]))
            continue
        out["checked"] += 1
        state, err = paddle_billing.get_subscription(sub_id)
        if err:
            out["errors"].append("%s: %s" % (sub_id, err))
            continue
        before = (user.get("plan"), user.get("subscription_status"),
                  user.get("cancel_at_period_end"))
        _apply_subscription_state(user, state)
        after = (user.get("plan"), user.get("subscription_status"),
                 user.get("cancel_at_period_end"))
        if before != after:
            out["changed"] += 1
            app.logger.info("reconcile: %s %s -> %s", user.get("email"),
                            before, after)
    return out


def _reconcile_forever() -> None:
    """The scheduled run. A daemon thread, like the boot probes."""
    while True:
        time.sleep(RECONCILE_EVERY_SECONDS)
        try:
            result = reconcile_subscriptions()
            if result["changed"] or result["errors"]:
                app.logger.info("reconcile: %s", result)
        except Exception as e:                          # noqa: BLE001
            app.logger.warning("reconcile failed: %s", e)
        try:
            # The router keeps a week of evidence; the same clock trims it.
            agent_router.prune()
        except Exception as e:                          # noqa: BLE001
            app.logger.warning("channel prune failed: %s", e)


threading.Thread(target=_reconcile_forever, daemon=True).start()


@app.route("/api/admin/reconcile", methods=["POST"])
def admin_reconcile():
    """Run the reconciler now. Owner only, like /stats."""
    _owner_only()
    return jsonify(reconcile_subscriptions())


# ---------------------------------------------------------------- #
# OpenAI-compatible API
#
# Lets Cursor, VS Code extensions and anything else that speaks the
# OpenAI protocol use this app as their model provider. See
# openai_api.py for why this shape rather than a per-editor plugin.
# ---------------------------------------------------------------- #

def _api_error(message, code=400, err_type="invalid_request_error"):
    """Flask wants (body, status). openai_api.error returns that pair, and
    jsonify(*pair) silently serialises the status code into the body and
    answers 200 - so a rejected key looked like a successful response
    containing an error object, which every client would then fail to
    parse as a completion."""
    body, status = openai_api.error(message, code, err_type)
    return jsonify(body), status


def _api_user():
    """The account behind the bearer key, or None.

    Every request is authenticated. Without this the endpoint is an open
    relay on a public URL: a stranger could occupy the owner's GPU and
    spend the owner's paid fallback budget.
    """
    raw = openai_api.key_from_request(request.headers)
    if not raw:
        return None
    digest = openai_api.hash_key(raw)
    for u in USERS.values():
        # Constant-time compare: a plain == leaks how much of the hash
        # matched through timing, one byte at a time.
        if u.get("api_key_hash") and hmac.compare_digest(
                u["api_key_hash"], digest):
            return u
    return None


@app.route("/v1/models", methods=["GET"])
def openai_models():
    if not _api_user():
        return _api_error(
            "Invalid API key. Create one in the app under Settings.",
            401, "invalid_request_error")
    ids = list(ollama_provider().get("models") or [])
    ids = [m for m in pip_api.models() if m not in ids] + ids
    return jsonify(openai_api.model_list(ids or [pip_identity.MODEL_ID]))


@app.route("/v1/chat/completions", methods=["POST"])
def openai_chat_completions():
    user = _api_user()
    if not user:
        return _api_error(
            "Invalid API key. Create one in the app under Settings.",
            401, "invalid_request_error")

    payload = request.get_json(force=True, silent=True) or {}
    history = openai_api.to_history(payload.get("messages"))
    if not history:
        return _api_error("messages must not be empty")

    # Pick a provider from the requested model name. An unknown name
    # falls back rather than erroring - editors send their own defaults
    # ("gpt-4o", "claude-3-5-sonnet") and refusing those would make the
    # integration look broken when it is only mislabelled.
    requested = (payload.get("model") or "").strip()
    provider, model = _v1_route(requested)
    if not model:
        return _api_error(
            "No model is available right now - nothing is answering.",
            503, "server_error")
    # The same gate the chat bay applies. This route skipped it, so a
    # free account's API key ran Pro models.
    if not features.model_allowed(user.get("plan"), model):
        return _api_error(
            "%s is a Pro model. Upgrade in the app, or ask for a free "
            "one." % model, 403, "permission_error")

    # Metered like the chat bay. This route charged nothing, so an API
    # key on a free account was unmetered generation.
    with CREDITS_LOCK:
        apply_refill(user["credits"])
    db.save_credits(user["id"], user["credits"])
    if user["credits"]["balance"] < CREDIT_MIN:
        return _api_error("Out of credits. They refill automatically, or "
                          "upgrade to Pro in the app.", 402,
                          "insufficient_quota")

    opts = openai_api.options_from(payload)
    opts["num_ctx"] = 16384
    usage: dict[str, Any] = {}
    failure: dict[str, str] = {}
    pieces = _v1_charged(
        _v1_pieces([(provider, model)] + _failover_chain(provider, "code"),
                   history, opts, usage, failure),
        user, usage)

    if payload.get("stream"):
        return Response(
            stream_with_context(
                openai_api.stream_sse(pieces, model, usage, failure)),
            mimetype="text/event-stream",
            headers={
                # Same reason as the chat stream: without this an nginx
                # in front buffers the whole reply and the editor shows
                # nothing until it is finished.
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache, no-transform",
            },
        )

    text = "".join(pieces)
    if failure and not text:
        return _api_error(failure["message"], 503, "server_error")
    # Rough where the provider did not say: clients divide by these and
    # some render NaN if the block is missing. ~4 chars per token is
    # close enough to be useful and is clearly documented as an
    # estimate; the real count is used whenever the channel reported it.
    prompt_chars = sum(len(m.get("content") or "") for m in history)
    return jsonify(openai_api.completion(
        text, model,
        prompt_tokens=prompt_chars // 4,
        completion_tokens=usage.get("eval_count") or len(text) // 4,
        finish_reason=usage.get("finish_reason") or "stop"))


def _v1_route(requested: str) -> tuple[str, str | None]:
    """Which channel answers an API request. -> (provider, model).

    A name the local model list has is used as asked. Otherwise the
    local model is preferred only when it is up AND measured fast
    enough - the chat bay's own rule, which this route skipped, so on
    the deployment where Ollama takes a minute to say its first word
    the API went there anyway. Then pip 1.0, then nothing.
    """
    local = list(ollama_provider().get("models") or [])
    if requested and requested in local:
        return "ollama", requested
    if requested and requested in (pip_api.models() or []):
        return "pip", requested
    if local and ollama_reachable() and _local_is_fast():
        return "ollama", next((m for m in local if "coder" in m), local[0])
    engine_model = _pip_model_for("code")
    if engine_model:
        return "pip", engine_model
    if local and ollama_reachable():
        # Slow is still better than nothing when it is all there is.
        return "ollama", next((m for m in local if "coder" in m), local[0])
    return "ollama", None


def _v1_charged(pieces, user: dict, usage: dict):
    """Charge the API account once the reply has fully streamed. A
    generator around the generator, so the charge happens in a finally
    whether the caller joined the pieces or streamed them - and only
    for text that was actually produced."""
    produced = False
    try:
        for piece in pieces:
            produced = produced or bool(piece)
            yield piece
    finally:
        if produced:
            cost = usage_based_cost(usage.get("eval_count"), "code")
            charged, new_balance = db.debit_to_floor(user["id"], cost)
            user["credits"]["balance"] = new_balance
            tokens = usage.get("eval_count") or 0
            if charged or tokens:
                db.note_usage(user["id"], charged, tokens=tokens)


def _v1_pieces(candidates, history, opts, usage, failure):
    """Text from the first channel that answers. A generator, so the
    same failover the chat bay does works whether the caller streams
    or joins.

    Never raises to the caller: a channel refusing before its first
    chunk moves to the next; every channel refusing, or anything
    unexpected, records a message in `failure` and ends the stream.
    Without this a requests exception from the streamer became a 500
    on the non-streaming path and a stream that simply stopped, with no
    [DONE], on the streaming one.
    """
    failures: list[tuple[str, BaseException]] = []
    for p, m in candidates:
        try:
            for piece in PROVIDER_STREAMERS[p](m, history, options=opts,
                                               usage=usage):
                yield piece
            return
        except pip_api.ProviderUnavailable as e:
            failures.append((p, e))
            continue
        except Exception as e:                            # noqa: BLE001
            app.logger.warning("/v1 reply failed on %s: %s", p, e)
            failure["message"] = ("Something went wrong talking to %s."
                                  % _channel_name(p))
            return
    failure["message"] = _nobody_answered(failures).strip("[]")


@app.route("/api/account/api-key", methods=["POST"])
def create_api_key():
    """Issue a key for the signed-in account, replacing any existing one.

    Returned exactly once. Only the hash is kept, so it cannot be shown
    again - which is the point: a key that can be re-read from the server
    is one a server compromise hands out.
    """
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    raw, digest = openai_api.new_key()
    USERS[uid]["api_key_hash"] = digest
    USERS[uid]["api_key_created"] = now_iso()
    save_users()
    return jsonify({
        "api_key": raw,
        "created": USERS[uid]["api_key_created"],
        "base_url": request.host_url.rstrip("/") + "/v1",
        "note": "Copy this now - it is not shown again.",
    })


@app.route("/api/account/api-key", methods=["DELETE"])
def revoke_api_key():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    USERS[uid]["api_key_hash"] = None
    USERS[uid]["api_key_created"] = None
    save_users()
    return jsonify({"revoked": True})


@app.route("/api/health", methods=["GET"])
def health():
    """Liveness + capability probe.

    Deliberately cheap: no database write, no model call. It's polled by
    the deployment's health check and by the UI's status indicator, so
    it has to stay fast enough to call often.

    Two possible sources: the pip 1.0 engine, and Ollama on this machine
    underneath it. `mode` says which one a request would actually reach
    right now.
    """
    local = ollama_reachable()
    # An engine URL being set is not the same as an engine that is up,
    # so this asks whether a model actually came back.
    engine = bool(pip_api.configured() and pip_api.models())
    return jsonify({
        "status": "ok",
        "time": now_iso(),
        "model": pip_identity.FULL_NAME,
        "compute": {
            "local_models": local,
            "engine": engine,
        },
        "engine": {
            "running": engine,
            # Non-zero while the engine has asked callers to wait, which
            # is what explains a sudden change in reply latency to
            # anyone watching this endpoint.
            "busy_for_seconds": round(pip_api.busy_for(), 1),
        },
        # "pip" only when the engine is both up and taking requests,
        # because otherwise the honest answer is that local is answering.
        "mode": ("pip" if engine and pip_api.has_room()
                 else "local" if local
                 else "pip" if engine else "unavailable"),
    })


def _paddle_customer_id():
    """The signed-in account's Paddle customer id, or "".

    Written by the webhook when a subscription event arrives, so it is
    empty for everyone who has never paid - which is the normal state,
    not an error.
    """
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    return (user or {}).get("paddle_customer_id") or ""


@app.route("/api/plans", methods=["GET"])
def get_plans():
    return jsonify({
        "plans": PLANS,
        "billing_live": (billing_live() or paddle_billing.configured()
                         or kelviq_billing.configured()),
        "processor": ("kelviq" if kelviq_billing.configured()
                      else "paddle" if paddle_billing.configured() else "stripe"),
        # Safe to publish: Paddle client tokens are designed to ship
        # in the page. They can start a checkout and nothing else -
        # the API key, which can read and refund, never leaves here.
        "paddle_client_token": paddle_billing.client_token(),
        "paddle_environment": paddle_billing.environment(),
        # For Paddle Retain, which needs to know WHICH customer is
        # looking at the page in order to show them anything.
        #
        # Empty until the person has paid at least once: a Paddle
        # customer id is minted by Paddle at first checkout, not by us at
        # signup. It must be that id - passing our own user id or an
        # email address gives Retain nothing it can match, and it fails
        # quietly rather than complaining.
        "paddle_customer_id": _paddle_customer_id(),
        "desktop": IS_DESKTOP,
        "site_url": PUBLIC_SITE_URL,
    })


# ----------------------------------------------------------------------
# Who the account belongs to
# ----------------------------------------------------------------------
# The age floor, and it is 16 because the privacy policy already says so
# - "the service is not for under-16s" has been published on /privacy for
# longer than this check has existed, and code that quietly admitted
# 13-year-olds would have made that a false statement rather than a
# strict one.
#
# 16 is also the GDPR default for digital consent without a parent. Some
# member states lower it to 13, as does COPPA in the US, so this can be
# dropped to 13 - but only by changing /privacy in the same commit. The
# two must not disagree.
MIN_AGE = 16
MAX_AGE = 120

# Codes are short-lived and rate-limited. A six-digit code is a million
# combinations, which is a lot for a person typing and nothing for a
# script, so it also dies after a few wrong guesses.
RESET_TTL_MINUTES = 15
RESET_MAX_ATTEMPTS = 5
RESET_COOLDOWN_SECONDS = 60


def _age_from_birth_year(birth_year):
    if not birth_year:
        return None
    return datetime.datetime.now(datetime.timezone.utc).year - int(birth_year)


def _validate_profile(name, age):
    """-> (name, birth_year, error). Shared by signup and the profile
    page, so the rules cannot drift between where they are set and where
    they are changed."""
    name = (name or "").strip()
    if len(name) < 2:
        return None, None, "Enter your name."
    if len(name) > 60:
        return None, None, "That name is too long."
    try:
        age = int(str(age).strip())
    except (TypeError, ValueError):
        return None, None, "Enter your age as a number."
    if age < MIN_AGE:
        return None, None, ("You need to be at least %d to have an account "
                            "here." % MIN_AGE)
    if age > MAX_AGE:
        return None, None, "Enter a real age."
    # Stored as a birth year: an age written to a database is wrong
    # within twelve months and nothing ever comes back to update it.
    year = datetime.datetime.now(datetime.timezone.utc).year - age
    return name, year, None


@app.route("/api/account/profile", methods=["GET", "POST"])
def account_profile():
    """Read or change the name and age on this account."""
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401

    if request.method == "GET":
        return jsonify({
            "name": user.get("name") or "",
            "age": _age_from_birth_year(user.get("birth_year")),
            "email": user.get("email") or "",
            "email_verified": bool(user.get("email_verified")),
            "created": user.get("created") or "",
            "plan": user.get("plan"),
            "has_password": bool(user.get("password_hash")),
        })

    payload = request.get_json(force=True, silent=True) or {}
    name, year, err = _validate_profile(payload.get("name"),
                                        payload.get("age"))
    if err:
        return jsonify({"error": err}), 400
    user["name"] = name
    user["birth_year"] = year
    save_users()
    return jsonify({"ok": True, "name": name,
                    "age": _age_from_birth_year(year)})


# ----------------------------------------------------------------------
# Forgotten password
#
# Distinct from /api/account/password, which changes a password you can
# already prove you know. This one is for someone locked out, so the
# proof is a code sent to the address on the account and nothing else -
# which is exactly why it gets its own table, its own attempt budget and
# its own cooldown.
# ----------------------------------------------------------------------
@app.route("/api/auth/reset/request", methods=["POST"])
@limited("reset-ip", LIMIT_RESET_IP, key=_limit_key_ip)
def reset_request():
    """Mail a reset code. Always answers the same way.

    Answering "no such account" would turn this endpoint into a way to
    ask whether an address has one here, so the response does not depend
    on whether it does.
    """
    payload = request.get_json(force=True, silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    same = jsonify({
        "ok": True,
        "detail": ("If there is an account for that address, a code is on "
                   "its way. It expires in %d minutes."
                   % RESET_TTL_MINUTES),
    })

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    user = next((u for u in USERS.values()
                 if (u.get("email") or "").lower() == email), None)
    if not user or not user.get("password_hash"):
        # No account, or a Google account with no password to reset.
        # Neither is said out loud.
        return same

    now = datetime.datetime.now(datetime.timezone.utc)
    existing = db.get_password_reset(email)
    if existing and existing["sent_at"]:
        try:
            last = datetime.datetime.fromisoformat(existing["sent_at"])
            if (now - last).total_seconds() < RESET_COOLDOWN_SECONDS:
                # Silently the same answer: a cooldown that announces
                # itself is another way to probe for an account.
                return same
        except (ValueError, TypeError):
            pass

    code = "%06d" % secrets.randbelow(1000000)
    expires = now + datetime.timedelta(minutes=RESET_TTL_MINUTES)
    db.save_password_reset(email, code, expires.isoformat(), now.isoformat())
    mailer.send_reset_code(email, code, RESET_TTL_MINUTES,
                           name=user.get("name") or "")
    return same


@app.route("/api/auth/reset/confirm", methods=["POST"])
def reset_confirm():
    """Trade a valid code for a new password."""
    payload = request.get_json(force=True, silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    code = (payload.get("code") or "").strip()
    new = payload.get("new") or ""

    row = db.get_password_reset(email)
    if not row:
        return jsonify({"error": "That code is not valid. Ask for a new "
                                 "one."}), 400

    try:
        expires = datetime.datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError):
        expires = None
    if expires and datetime.datetime.now(datetime.timezone.utc) > expires:
        db.delete_password_reset(email)
        return jsonify({"error": "That code has expired. Ask for a new "
                                 "one."}), 400

    if len(new) < 8:
        return jsonify({"error": "Use at least 8 characters."}), 400

    if not hmac.compare_digest(str(row["code"]), code):
        used = db.bump_password_reset_attempts(email)
        if used >= RESET_MAX_ATTEMPTS:
            db.delete_password_reset(email)
            return jsonify({
                "error": "Too many wrong tries. Ask for a new code.",
            }), 400
        return jsonify({"error": "That code is not right."}), 400

    user = next((u for u in USERS.values()
                 if (u.get("email") or "").lower() == email), None)
    if not user:
        db.delete_password_reset(email)
        return jsonify({"error": "That code is not valid."}), 400

    user["password_hash"] = generate_password_hash(new)
    # Reaching a code at that address proves the address works, which is
    # the same thing email verification asks for.
    user["email_verified"] = True
    save_users()
    db.delete_password_reset(email)
    # Deliberately NOT signing them in. A reset proves control of the
    # inbox; letting it also open a session means an intercepted email is
    # a session rather than a password they still have to type.
    return jsonify({"ok": True})


@app.route("/profile")
def profile_page():
    """A page for the account details, separate from the settings modal
    so it can be linked to and bookmarked."""
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return redirect("/app")
    return render_template("profile.html")


# ----------------------------------------------------------------------
# Settings
#
# One document per owner, read on load and PATCHed in pieces. PATCH not
# PUT: two open tabs sending a whole document would race, and the one
# that saved last would silently revert the other.
# ----------------------------------------------------------------------
# name -> (coercer, validator). A settings endpoint that writes whatever
# it is handed is a way to set a temperature to a string or a retention
# to -1, so nothing is written that is not on this list.
SETTING_FIELDS = {
    "theme":          (str, lambda v: v in ("system", "light", "dark")),
    "language":       (str, lambda v: 0 < len(v) <= 8),
    "timezone":       (str, lambda v: 0 < len(v) <= 64),
    "default_model":  (str, lambda v: len(v) <= 120),
    "system_prompt":  (str, lambda v: len(v) <= 4000),
    "temperature":    (float, lambda v: 0.0 <= v <= 2.0),
    "top_p":          (float, lambda v: 0.0 <= v <= 1.0),
    "max_tokens":     (int, lambda v: 1 <= v <= 32000),
    "web_search":     (bool, lambda v: True),
    "tools_enabled":  (bool, lambda v: True),
    # A fixed set, not any integer: these are the only options the UI
    # offers, and accepting 1 would let somebody delete their history
    # nightly by accident.
    "retention_days": (int, lambda v: v in (0, 7, 30, 90, 365)),
    "bio": (str, lambda v: len(v) <= 400),
    # What to call you. Was browser-local, so it did not follow the
    # account anywhere - a preference about how somebody is addressed
    # belongs to them, not to one copy of one browser.
    "nickname": (str, lambda v: len(v) <= 60),
    # Free text rather than a fixed enum, because every list of jobs is
    # wrong for somebody. The UI offers common ones and accepts anything.
    "work_role": (str, lambda v: len(v) <= 60),
    # A fixed set: these map to real font stacks in the stylesheet, so an
    # arbitrary string would be either ignored or an injection point.
    "chat_font": (str, lambda v: v in ("sans", "serif", "mono")),
}

# Fields that may be cleared back to "use the server default". Sending
# null for these is meaningful and different from sending 0.
NULLABLE_SETTINGS = ("default_model", "temperature", "top_p", "max_tokens",
                     "retention_days")

PROVIDER_KEY_HOSTS = {
    "openai": ("https://api.openai.com/v1/models", "sk-"),
    "anthropic": ("https://api.anthropic.com/v1/models", "sk-ant-"),
    "openrouter": ("https://openrouter.ai/api/v1/key", "sk-or-"),
}


AVATAR_DIR = os.path.join("static", "uploads", "avatars")
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_TYPES = {"image/png": ".png", "image/jpeg": ".jpg",
                "image/webp": ".webp", "image/gif": ".gif"}


@app.route("/api/account/avatar", methods=["POST"])
def upload_avatar():
    """A picture for the account.

    The extension comes from the DECLARED content type mapped through a
    fixed table, never from the uploaded filename - a file called
    x.png.html saved under its own name would otherwise be served back as
    HTML from this origin, which is a stored cross-site scripting hole
    rather than an avatar.
    """
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file was sent."}), 400
    ext = AVATAR_TYPES.get((upload.mimetype or "").lower())
    if not ext:
        return jsonify({
            "error": "Use a PNG, JPEG, WebP or GIF image.",
        }), 400

    os.makedirs(AVATAR_DIR, exist_ok=True)
    name = "%s%s" % (uuid.uuid4().hex[:16], ext)
    path = os.path.join(AVATAR_DIR, name)
    upload.save(path)
    if os.path.getsize(path) > AVATAR_MAX_BYTES:
        os.remove(path)
        return jsonify({"error": "Images must be under 2MB."}), 400

    url = "/static/uploads/avatars/" + name
    db.save_settings(current_owner_id(), {"avatar_url": url}, now_iso())
    return jsonify({"ok": True, "avatar_url": url})


@app.route("/api/account/avatar", methods=["DELETE"])
def remove_avatar():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    db.save_settings(current_owner_id(), {"avatar_url": ""}, now_iso())
    return jsonify({"ok": True})


@app.route("/api/usage", methods=["GET"])
def usage_dashboard():
    """Daily counters for the chart, plus the totals under it."""
    try:
        days = max(7, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    series = db.usage_series(current_owner_id(), days)
    credits, _ = current_account()
    plan = features.normalize_plan(credits.get("plan"))
    return jsonify({
        "days": days,
        "series": series,
        "totals": {
            "messages": sum(d["messages"] for d in series),
            "credits": sum(d["credits"] for d in series),
            "tokens": sum(d["tokens"] for d in series),
            # None when nothing happened. max() over all-zero days
            # returns the first one, which would label an idle month's
            # opening day as its busiest.
            "busiest": (max(series, key=lambda d: d["messages"])["day"]
                        if any(d["messages"] for d in series) else None),
        },
        "balance": credits.get("balance"),
        "cap": PLANS[plan]["cap"],
    })


@app.route("/api/billing/invoices", methods=["GET"])
def billing_invoices():
    """Past payments, read from Paddle.

    Read-only and passed through rather than mirrored into a table:
    Paddle is the merchant of record, so their record is the true one and
    a local copy could only ever disagree with it.
    """
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    customer = user.get("paddle_customer_id")
    if not (paddle_billing.configured() and customer):
        return jsonify({"invoices": [], "portal": None,
                        "detail": "No payments yet."})
    try:
        r = requests.get(
            paddle_billing.api_base() + "/transactions",
            params={"customer_id": customer, "per_page": 20},
            headers={"Authorization": "Bearer " + paddle_billing.api_key()},
            timeout=15)
        if r.status_code != 200:
            return jsonify({"invoices": [],
                            "detail": "Paddle returned %d." % r.status_code})
        rows = []
        for item in (r.json().get("data") or []):
            totals = ((item.get("details") or {}).get("totals") or {})
            rows.append({
                "id": item.get("id"),
                "status": item.get("status"),
                "billed_at": item.get("billed_at"),
                "total": totals.get("total"),
                "currency": totals.get("currency_code"),
                "invoice_url": (item.get("invoice_url")
                                or item.get("receipt_url")),
            })
        return jsonify({"invoices": rows})
    except requests.exceptions.RequestException as e:
        return jsonify({"invoices": [], "detail": "Could not reach Paddle: %s"
                        % e})


@app.route("/api/notifications/prefs", methods=["GET"])
def get_notification_prefs():
    return jsonify({"prefs": db.load_notification_prefs(current_owner_id())})


@app.route("/api/notifications/prefs", methods=["PATCH"])
def patch_notification_prefs():
    payload = request.get_json(force=True, silent=True) or {}
    event = (payload.get("event") or "").strip()
    channel = (payload.get("channel") or "").strip()
    enabled = bool(payload.get("enabled"))
    if not db.save_notification_pref(current_owner_id(), event, channel,
                                     enabled):
        return jsonify({
            "error": "Security alerts by email cannot be switched off.",
        }), 400
    return jsonify({"ok": True,
                    "prefs": db.load_notification_prefs(current_owner_id())})


# Templates every account has without having written them. The list
# users see is these plus their own; the ids are namespaced so a
# built-in can never collide with a uuid from create_template().
TUTOR_TEMPLATE = """You are an academic tutor for students in Morocco - Baccalaureat (SM, SVT, PC), CPGE, and international tracks (Cambridge, SAT, AP).

Explaining
- Give the reasoning, not only the result. Show why a formula holds and where it comes from, one step at a time.
- Concrete example first, then the general case, then the edge cases.
- Pair an intuitive analogy with the rigorous statement. Neither on its own is enough.
- Do not trade away precision for simplicity unless asked for a quick answer.

Notation
- Match the notation of the student's curriculum. For the Moroccan and French systems: intervals as ]a, b[, sequences as (u_n), limits and proofs written out with every step justified.
- Use English and international terminology when the student writes in English or names an international exam.

Mathematics and diagrams
- Write mathematics in LaTeX: $...$ inline, $$...$$ for display. It is rendered.
- Draw anything with structure: tables of signs and variations, geometric setups, cycles in biology or chemistry, decision trees. A mermaid code block is rendered; plain ASCII is fine for variation tables.

QCM (multiple choice)
- Give exactly the number of questions asked for; default to 5.
- Number every question. Label the options A) B) C) D). Exactly one is correct unless the student asks for several.
- Wrong options must be plausible - the mistakes a student actually makes: a sign error, a forgotten domain condition, necessary confused with sufficient. Never filler options.
- No two options may be the same value, or the same value written differently. 2 and 2/1 are one option, not two.
- Solve each question yourself before writing the answer key, then check the letter you wrote against the option that actually holds that value. A key that points at the wrong letter is worse than no QCM at all.
- Put every answer at the end under a "## Corrige" heading, as "1. B" followed by one line saying why it is right and, where it helps, why the tempting wrong option is wrong.
- Never put the answer next to its question.

Notes
- When asked for notes, a summary, a fiche or a resume, write a document rather than a chat reply: one "# " title, "## " sections, definitions in bold, formulas displayed, and a short "## A retenir" list of the three or four things that actually get examined.
- Notes must stand alone. Someone opening the file a month later has no memory of this conversation, so no "as we said above".
- Every reply can be saved as a .md file from the Save button underneath it. Structure the answer as if it will be read as a file.

Length
- Finish what you start. If a proof is long, number the sections and complete every one of them.
- No preamble and no filler. Begin with the content."""


BUILTIN_TEMPLATES = [
    {
        "id": "builtin:tutor",
        "name": "Academic tutor (Morocco / CPGE / international)",
        "body": TUTOR_TEMPLATE,
        "created": "",
        "builtin": True,
    },
]


@app.route("/api/settings/templates", methods=["GET"])
def get_templates():
    mine = db.list_templates(current_owner_id())
    for t in mine:
        t["builtin"] = False
    return jsonify({"templates": BUILTIN_TEMPLATES + mine})


@app.route("/api/settings/templates", methods=["POST"])
def create_template():
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    if not name or not body:
        return jsonify({"error": "A template needs a name and some text."}), 400
    if len(name) > 60 or len(body) > 4000:
        return jsonify({"error": "That is too long."}), 400
    tid = uuid.uuid4().hex[:16]
    db.save_template(current_owner_id(), tid, name, body, now_iso())
    return jsonify({"ok": True,
                    "templates": db.list_templates(current_owner_id())})


@app.route("/api/settings/templates/<tid>", methods=["DELETE"])
def remove_template(tid):
    if tid.startswith("builtin:"):
        return jsonify({"error": "Built-in templates cannot be deleted."}), 400
    if not db.delete_template(current_owner_id(), tid):
        return jsonify({"error": "No such template."}), 404
    return jsonify({"ok": True,
                    "templates": db.list_templates(current_owner_id())})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = db.load_settings(current_owner_id())
    plan = features.normalize_plan(current_account()[0].get("plan"))
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    return jsonify({
        "settings": settings,
        # What the UI needs to render the panels without a second call.
        "account": {
            "signed_in": bool(user),
            "email": (user or {}).get("email", ""),
            "name": (user or {}).get("name", ""),
            "email_verified": bool((user or {}).get("email_verified")),
            "has_password": bool((user or {}).get("password_hash")),
            "plan": plan,
        },
        "limits": {
            "max_context": features.get(plan, "max_context_tokens"),
            "max_output": features.get(plan, "max_output_tokens_code"),
        },
        "mfa": {"enabled": _mfa_enabled(uid) if uid else False},
        "secrets_ready": keystore.available(),
        "secrets_problem": keystore.unavailable_reason(),
    })


@app.route("/api/settings", methods=["PATCH"])
def patch_settings():
    payload = request.get_json(force=True, silent=True) or {}
    clean, errors = {}, {}
    for field, (cast, ok) in SETTING_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if value is None:
            if field in NULLABLE_SETTINGS:
                clean[field] = None
                continue
            errors[field] = "cannot be empty"
            continue
        try:
            value = cast(value)
        except (TypeError, ValueError):
            errors[field] = "expected %s" % cast.__name__
            continue
        if not ok(value):
            errors[field] = "out of range"
            continue
        clean[field] = int(value) if cast is bool else value

    if errors:
        return jsonify({"error": "Some settings could not be saved.",
                        "fields": errors}), 400
    if not clean:
        return jsonify({"error": "Nothing to update."}), 400

    saved = db.save_settings(current_owner_id(), clean, now_iso())
    return jsonify({"ok": True, "settings": saved})


# ----------------------------------------------------------- sessions
@app.route("/api/account/sessions", methods=["GET"])
def list_account_sessions():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    here = session.get("sid")
    rows = []
    for row in db.list_sessions(uid):
        rows.append({
            "id": row["id"],
            "created": row["created"],
            "last_seen": row["last_seen"],
            "ip": row["ip"],
            "device": _describe_agent(row["user_agent"]),
            # Marked so the UI can label it and refuse to revoke it -
            # "log out this device" is what the logout button is for.
            "current": row["id"] == here,
        })
    return jsonify({"sessions": rows})


@app.route("/api/account/sessions/<sid>", methods=["DELETE"])
def revoke_account_session(sid):
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    if sid == session.get("sid"):
        return jsonify({
            "error": "That is this device. Use Log out instead.",
        }), 400
    if not db.revoke_session(uid, sid, now_iso()):
        return jsonify({"error": "No such session."}), 404
    return jsonify({"ok": True})


@app.route("/api/account/sessions/revoke-others", methods=["POST"])
def revoke_other_account_sessions():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    count = db.revoke_other_sessions(uid, session.get("sid") or "", now_iso())
    return jsonify({"ok": True, "revoked": count})


# ----------------------------------------------------- provider keys
@app.route("/api/account/provider-keys", methods=["GET"])
def list_account_provider_keys():
    """Metadata only. The secret is never returned, by anyone, ever.

    NOT to be confused with /api/account/api-key, which issues a key for
    calling THIS server and keeps only a hash. These are keys belonging
    to someone else's account that this server spends on their behalf,
    so they have to be recoverable - a different problem entirely.
    """
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    return jsonify({
        "keys": db.list_provider_keys(uid),
        "providers": sorted(PROVIDER_KEY_HOSTS),
        "ready": keystore.available(),
        "problem": keystore.unavailable_reason(),
    })


@app.route("/api/account/provider-keys/<provider>", methods=["PUT"])
def put_account_provider_key(provider):
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    provider = (provider or "").strip().lower()
    if provider not in PROVIDER_KEY_HOSTS:
        return jsonify({"error": "Unknown provider."}), 400

    problem = keystore.unavailable_reason()
    if problem:
        # Refuse rather than store it in the clear. A key saved
        # unencrypted because the library was missing is worse than one
        # that was never saved.
        return jsonify({"error": problem}), 503

    key = ((request.get_json(force=True, silent=True) or {})
           .get("key") or "").strip()
    if len(key) < 20:
        return jsonify({"error": "That does not look like an API key."}), 400
    url, prefix = PROVIDER_KEY_HOSTS[provider]
    if not key.startswith(prefix):
        return jsonify({
            "error": "An %s key normally starts with %s." % (provider, prefix),
        }), 400

    # Checked against the provider before it is stored. A key that is
    # wrong on the day it is pasted otherwise becomes a mysterious
    # failure weeks later, with nothing pointing back to here.
    headers = {"Authorization": "Bearer " + key}
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        probe = requests.get(url, headers=headers, timeout=12)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Could not reach %s: %s" % (provider, e)}), 502
    if probe.status_code in (401, 403):
        return jsonify({"error": "%s rejected that key." % provider}), 400

    ct, nonce = keystore.encrypt(key, uid, "provider:" + provider)
    db.save_provider_key(uid, provider, ct, nonce, key[-4:], now_iso())
    return jsonify({"ok": True, "provider": provider, "last_four": key[-4:]})


@app.route("/api/account/provider-keys/<provider>", methods=["DELETE"])
def delete_account_provider_key(provider):
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    if not db.delete_provider_key(uid, (provider or "").strip().lower()):
        return jsonify({"error": "No key stored for that provider."}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------- MFA
def _mfa_enabled(owner_id):
    row = db.get_mfa_enrollment(owner_id)
    return bool(row and row["confirmed"])


@app.route("/api/account/mfa/enroll", methods=["POST"])
def mfa_enroll():
    """Begin enrolment. Nothing is enforced until a code is confirmed.

    Two steps on purpose: marking this confirmed here would lock out
    anyone whose authenticator failed to take the secret, and locking
    someone out of their own account while adding security is the worst
    outcome available.
    """
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    problem = keystore.unavailable_reason()
    if problem:
        return jsonify({"error": problem}), 503
    if _mfa_enabled(uid):
        return jsonify({"error": "Two-factor is already on."}), 400

    secret = keystore.new_totp_secret()
    ct, nonce = keystore.encrypt(secret, uid, "mfa")
    db.save_mfa_enrollment(uid, ct, nonce, now_iso())
    return jsonify({
        "ok": True,
        "secret": secret,
        "otpauth_uri": keystore.provisioning_uri(secret,
                                                 user.get("email") or ""),
    })


@app.route("/api/account/mfa/confirm", methods=["POST"])
def mfa_confirm():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    row = db.get_mfa_enrollment(uid)
    if not row:
        return jsonify({"error": "Start setup again."}), 400
    code = ((request.get_json(force=True, silent=True) or {})
            .get("code") or "")
    secret = keystore.decrypt(row["secret_ct"], row["secret_nonce"],
                              uid, "mfa")
    if not keystore.verify_totp(secret, code):
        return jsonify({"error": "That code is not right. Check your "
                                 "phone's clock if it keeps failing."}), 400

    db.confirm_mfa(uid)
    # Shown once, stored only as hashes - the same contract as a
    # password, because that is exactly what they are.
    codes = ["%s-%s" % (secrets.token_hex(2), secrets.token_hex(2))
             for _ in range(10)]
    db.save_backup_codes(uid, [generate_password_hash(c) for c in codes])
    return jsonify({"ok": True, "backup_codes": codes})


@app.route("/api/account/mfa/disable", methods=["POST"])
def mfa_disable():
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user:
        return jsonify({"error": "Sign in first."}), 401
    password = ((request.get_json(force=True, silent=True) or {})
                .get("password") or "")
    # Turning off a second factor is exactly when a stolen session would
    # be used, so it costs the password even though you are signed in.
    if user.get("password_hash") and not check_password_hash(
            user["password_hash"], password):
        return jsonify({"error": "That password is not right."}), 400
    db.disable_mfa(uid)
    return jsonify({"ok": True})


@app.route("/api/account/mfa/backup-codes", methods=["POST"])
def mfa_regenerate_backup_codes():
    uid = session.get("user_id")
    if not uid or uid not in USERS:
        return jsonify({"error": "Sign in first."}), 401
    if not _mfa_enabled(uid):
        return jsonify({"error": "Two-factor is not on."}), 400
    codes = ["%s-%s" % (secrets.token_hex(2), secrets.token_hex(2))
             for _ in range(10)]
    db.save_backup_codes(uid, [generate_password_hash(c) for c in codes])
    return jsonify({"ok": True, "backup_codes": codes})


# ------------------------------------------------------ data controls
@app.route("/api/account/export", methods=["GET"])
def export_history():
    """Every thread this owner has, as JSON or Markdown.

    Served as a download rather than a job: the whole history is a few
    hundred kilobytes of text at the sizes this app deals in, and a job
    queue for that would be machinery with nothing to carry.
    """
    owner_id = current_owner_id()
    fmt = (request.args.get("format") or "json").lower()
    mine = [t for t in THREADS.values() if t.get("owner_id") == owner_id]
    mine.sort(key=lambda t: t.get("updated") or "")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if fmt == "markdown":
        parts = ["# Chat history", "", "Exported %s" % stamp, ""]
        for thread in mine:
            parts.append("## %s" % (thread.get("title") or "Untitled"))
            parts.append("")
            for message in thread.get("messages") or []:
                who = "You" if message.get("role") == "user" else "Assistant"
                parts.append("**%s:** %s" % (who, message.get("content") or ""))
                parts.append("")
            parts.append("---")
            parts.append("")
        body = "\n".join(parts)
        mimetype = "text/markdown; charset=utf-8"
        filename = "pip-history-%s.md" % stamp
    else:
        body = json.dumps({
            "exported": now_iso(),
            "thread_count": len(mine),
            "threads": mine,
        }, indent=2, ensure_ascii=False)
        mimetype = "application/json; charset=utf-8"
        filename = "pip-history-%s.json" % stamp

    return Response(body, mimetype=mimetype, headers={
        "Content-Disposition": 'attachment; filename="%s"' % filename,
    })


@app.route("/api/account/history", methods=["DELETE"])
def clear_history():
    """Delete every thread for this owner, now."""
    owner_id = current_owner_id()
    with DATA_LOCK:
        doomed = [tid for tid, t in THREADS.items()
                  if t.get("owner_id") == owner_id]
        for tid in doomed:
            THREADS.pop(tid, None)
    db.delete_threads_for(owner_id)
    return jsonify({"ok": True, "deleted": len(doomed)})


def apply_retention(owner_id):
    """Drop threads older than this owner's retention window.

    Called on load rather than by a scheduler: this app has no cron, and
    a retention policy that only runs when a machine happens to be up is
    worse than one that runs whenever its owner appears.
    """
    days = db.load_settings(owner_id).get("retention_days")
    if not days:
        return 0
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=int(days))).isoformat()
    with DATA_LOCK:
        doomed = [tid for tid, t in THREADS.items()
                  if t.get("owner_id") == owner_id
                  and (t.get("updated") or "") < cutoff]
        for tid in doomed:
            THREADS.pop(tid, None)
    if doomed:
        db.delete_threads_older_than(owner_id, cutoff)
    return len(doomed)


@app.route("/api/settings/apply-retention", methods=["POST"])
def run_retention():
    return jsonify({"ok": True, "deleted": apply_retention(current_owner_id())})


@app.route("/api/connectors", methods=["GET"])
def list_connectors():
    """What this account has connected. Tokens are never included."""
    items = db.load_connectors(current_owner_id())
    plan = features.normalize_plan(current_account()[0].get("plan"))
    return jsonify({
        "connectors": [
            {"id": i["id"], "title": i["title"], "kind": i["kind"],
             "url": i["url"], "operations": len(i["operations"]),
             "has_token": i["has_token"], "created": i["created"]}
            for i in items],
        # Sent with the list rather than fetched from /api/features,
        # which the browser never calls. The panel needs the ceiling to
        # say "1 of 1" instead of letting somebody paste a URL and read
        # a refusal.
        "max": max_connectors(plan),
        "plan": plan,
    })


@app.route("/api/connectors", methods=["POST"])
def add_connector():
    """Paste a link; work out what it is; keep it.

    The discovery fetch happens here rather than at chat time so the
    person pasting finds out immediately whether it worked, and gets a
    sentence they can act on if it did not.
    """
    payload = request.get_json(force=True, silent=True) or {}
    url = (payload.get("url") or "").strip()
    token = (payload.get("token") or "").strip()

    plan = features.normalize_plan(current_account()[0].get("plan"))
    if not features.FEATURES[plan]["external_connectors"]:
        return jsonify({
            "error": "Connecting apps is not available on this plan.",
        }), 403

    owner_id = current_owner_id()
    existing = db.load_connectors(owner_id)
    limit = max_connectors(plan)
    if len(existing) >= limit:
        # Free hits this at one, so the message has to say what to do
        # about it rather than only stating the rule - and it must not
        # read as an error, because connecting one app is the intended
        # free experience rather than a thwarted Pro one.
        if plan == features.FREE:
            return jsonify({
                "error": "Free connects one app at a time. Remove the "
                         "current one to connect a different app, or "
                         "upgrade to Pro to connect up to %d at once."
                         % features.FEATURES[features.PRO]["max_connectors"],
                "upgrade": True,
            }), 400
        return jsonify({
            "error": "You can connect up to %d apps. Remove one first."
                     % limit,
        }), 400

    found, err = connectors.discover(url, token or None)
    if err or not found:
        return jsonify({
            "error": err or "Nothing usable was found at that link.",
        }), 400

    found["id"] = uuid.uuid4().hex[:16]
    found["token"] = token
    found["created"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    db.save_connector(owner_id, found)
    return jsonify({
        "ok": True,
        "connector": {
            "id": found["id"], "title": found["title"],
            "kind": found["kind"], "url": found["url"],
            "operations": len(found.get("operations") or []),
            "has_token": bool(token), "created": found["created"],
        },
    })


@app.route("/api/connectors/<connector_id>", methods=["DELETE"])
def remove_connector(connector_id):
    removed = db.delete_connector(current_owner_id(), connector_id)
    if not removed:
        return jsonify({"error": "No such connection."}), 404
    return jsonify({"ok": True})


@app.route("/sw.js")
def service_worker():
    """Served from the ROOT, not from /static, and that is not cosmetic.

    A service worker may only control URLs at or below its own path. At
    /static/sw.js it would control /static/* and nothing else - so the app
    at /app would never be intercepted, the install prompt would never
    appear, and nothing would work offline. The file lives in static/ for
    tidiness and is published here for scope.

    Cache-Control is no-cache so a browser revalidates the worker itself on
    every load. A stale worker is the one bug in this area nobody can
    clear from their end.
    """
    # app.static_folder is Optional; root_path is not, and this is the
    # same directory by construction.
    static_dir = os.path.join(app.root_path, "static")
    response = send_from_directory(static_dir, "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/manifest.webmanifest")
def web_manifest():
    static_dir = os.path.join(app.root_path, "static")
    response = send_from_directory(static_dir, "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.route("/api/features", methods=["GET"])
def get_features():
    """What this account is allowed to do, for the UI to gate on.

    Advisory only - every gated action is re-checked server-side when it
    is actually invoked. Sending this does not weaken anything: it just
    stops the UI offering a button that would only return an error.
    """
    account_credits, _ = current_account()
    return jsonify(features.public_flags(account_credits.get("plan")))


# ----------------------------------------------------------------------
# Licence verification for the desktop build.
#
# The desktop app can't use the session cookie the web app relies on -
# it's a separate client with no browser login, and a cookie wouldn't
# survive a reinstall. So Pro is proven by a licence key instead, and
# THIS endpoint is the authority: the desktop client's local check
# (desktop/src/entitlements.js) only decides what UI to show.
#
# The payment provider's secret key stays here, server-side, and is never
# shipped inside the desktop binary - anything embedded in a distributed
# .exe can be extracted from it in minutes.
# ----------------------------------------------------------------------
def _licence_from_stripe(key):
    """Treat the key as a Stripe subscription id and ask Stripe whether
    it's currently paid up. Returns the same shape as the route."""
    stripe = _stripe()
    try:
        sub = stripe.Subscription.retrieve(key)
    except stripe.StripeError:
        return {"valid": False, "error": "No subscription matches that key."}

    active = _sv(sub, "status") in ("active", "trialing")
    return {
        "valid": active,
        "tier": "pro" if active else "free",
        "expiresAt": _iso_from_timestamp(_period_end_timestamp(sub)),
        "error": None if active else f"Subscription is {_sv(sub, 'status')}.",
    }


@app.route("/api/license/verify", methods=["POST"])
def license_verify():
    payload = request.get_json(force=True, silent=True) or {}
    key = (payload.get("key") or "").strip()
    if not key:
        return jsonify({"valid": False, "error": "No key supplied."}), 400

    # A locally-issued key, for accounts that upgraded through the web UI
    # on this same server. Checked first so it works even with billing
    # keys absent (self-hosting, development). A Paddle subscription id
    # is a licence too - it was not, so a Paddle customer could pay and
    # still be unable to activate the desktop build.
    user = next((u for u in USERS.values()
                 if key and key in (u.get("stripe_subscription_id"),
                                    u.get("paddle_subscription_id"))),
                None)
    if user:
        return jsonify({
            "valid": user["plan"] == "pro",
            "tier": user["plan"],
            "email": user["email"],
            "expiresAt": user.get("current_period_end"),
        })

    if not billing_live():
        return jsonify({
            "valid": False,
            "error": "Licence checking isn't configured on this server.",
        }), 503

    return jsonify(_licence_from_stripe(key))


# ----------------------------------------------------------------------
# Credits
# ----------------------------------------------------------------------
@app.route("/api/credits", methods=["GET"])
def get_credits():
    credits, save = current_account()
    apply_refill(credits)
    save()
    return jsonify(credits_view(credits))


# ----------------------------------------------------------------------
# Provider / model discovery
# ----------------------------------------------------------------------
# WHICH MODEL ANSWERS WHICH BAY
#
# Ranked preferences, best first. Every entry is (provider, name-pattern)
# and the first one that is actually present and working wins, so the
# same table serves a laptop with four local models and a GPU-less VM
# with none - it simply falls further down the list.
#
# pip 1.0 is first in every bay: it is this app's own model, and the
# other channels exist for the moments it cannot answer - the engine is
# restarting, or its queue is full. Below it the order is set by
# measurement on the deployment this runs on. On a machine with no GPU
# a local 7B answers at about 3.5 tokens a second - a 417-token code
# reply took 194 seconds - so the hosted fallbacks come before the
# local ones for the bays where waiting is the thing you notice.
#
# Vision follows what can actually see. pip 1.0 is listed there too,
# and matches only when the engine reports a build that reads images
# (pip_api.vision_models); until then an attached picture goes to the
# next entry that can.
#
# ONE MODEL ANSWERS BOTH CHAT AND CODE, AND THAT IS THE WHOLE POINT
#
# A 7B at Q4 occupies ~5GB of VRAM. This machine has 8GB, so exactly one
# fits, and /api/ps confirms Ollama keeps exactly one resident. Every
# time a bay asks for a different model the current one is evicted and
# the new one loaded from disk, which was measured at 7-8 SECONDS before
# the first token - against 0.09s when the model is already warm.
#
# Chat and code are the two bays people move between constantly, so
# giving them different models means paying that 7-8s on most switches.
# qwen2.5-coder was checked on general chat before being given the job -
# it explained Rayleigh scattering perfectly well - so one model covers
# both bays and stays resident permanently. That is where the speed
# comes from: not a faster model, an un-evicted one.
#
# llava is the deliberate exception. Vision is rare and always follows an
# upload, so its load is paid by someone who has just chosen to wait, and
# it buys the one capability no text model has.
#
# Each entry is matched as a SUBSTRING against whatever Ollama actually
# reports, which is what lets one table serve two very different
# endpoints. Locally that resolves to qwen2.5-coder:7b and llava:7b; with
# OLLAMA_URL pointed at Ollama Cloud the same patterns fall through to
# qwen3.5 and gemma4, because the cloud catalogue carries neither
# qwen2.5 nor llava (checked against ollama.com/api/tags, which lists 19
# models and none of them llava).
# (A warning for whoever tunes this next: an early benchmark scored a
# model 3/6 twice, and both times the model was right and the GRADER was
# wrong - it could not see a bare "9" or a \frac{1}{8}. Check what the
# model actually wrote before believing a score.)
BAY_ROUTES = {
    "code": [
        # pip 2 first, for Pro - _recommended_routes skips it for Free,
        # where model_allowed() says no.
        ("anthropic", "claude-haiku"),
        # pip 1.0 first. Underneath it DeepSeek, asked for by name: V3
        # for the bay, and Deep mode swaps in R1
        # (openrouter_api.DEEP_MODEL) at _stream_reply. Both are paid
        # and bounded by OPENROUTER_DAILY_USD, so the free coding model
        # and then the local ones carry the bay when the ceiling is
        # reached or no OpenRouter key is set.
        ("pip", "pip-1.0"),
        # DeepSeek V4 Pro, the owner's choice for code, while it has
        # credit (openrouter_api.deepseek_ready).
        ("openrouter", "deepseek-v4-pro"),
        # Groq next: gpt-oss-120b on dedicated accelerators streams at
        # hundreds of tokens a second, and the key costs nothing. It is
        # what answers "make me a website" in seconds while the engine
        # is down - the free OpenRouter models took minutes for that.
        ("groq", "gpt-oss-120b"),
        ("openrouter", "deepseek-v3.2"),
        ("openrouter", "deepseek-chat-v3"),
        ("openrouter", "laguna-s-2.1:free"),
        ("ollama", "gemma3:1b"),
        ("ollama", "gemma3"),
        ("ollama", "qwen3.5"),          # Ollama Cloud
        ("ollama", "gpt-oss"),          # Ollama Cloud
    ],
    "chat": [
        ("anthropic", "claude-haiku"),      # pip 2, Pro accounts
        # ORDER IS THE DEFAULT. _recommended_routes walks this list and
        # lands a visitor on the first entry that is live, so whatever is
        # first here is what the chat bay actually runs: pip 1.0,
        # wherever its engine is up.
        #
        # Nemotron and GLM behind it. Both are free on OpenRouter, where
        # free means metered by the DAY: 50 requests until an account has
        # bought about $10 of credit once, 1,000 after. They are what
        # answers while the engine is down, not a second default.
        ("pip", "pip-1.0"),
        ("openrouter", "deepseek-v4.1-flash"),
        ("groq", "gpt-oss-120b"),
        # Gemma 4 31B, offered in the picker after Turbo (see _gemma_model).
        ("google", "gemma-4"),
        ("openrouter", "nemotron-3-ultra-550b-a55b:free"),
        ("openrouter", "glm-5.2:free"),
        ("ollama", "gemma3:1b"),
        ("ollama", "gemma3"),
        ("ollama", "qwen3.5"),          # Ollama Cloud
    ],
    # Reading an attached image, not generating one - the Image bay's
    # pictures come from imagegen.py and never touch a chat model.
    #
    # gemma3:4b replaced llava:7b here. It is multimodal, it answered a
    # test image correctly, and it does so at 6.98 tok/s against llava's
    # ~3.7 - so the smaller model is both faster AND the one that lets
    # this box hold only Gemma.
    "vision": [
        # pip 1.0 first, and it matches only when the engine reports a
        # build that can see. Then hosted before local: a local vision
        # model needs a GPU to be bearable, so on a server without one
        # the OpenRouter entries are the only ones that can match -
        # while a laptop running Ollama still falls through to gemma3:4b
        # below and keeps working offline. The OpenRouter entries are
        # :free models, the first thing to be rate-limited on a busy
        # afternoon.
        ("pip", "pip-1.0"),
        ("groq", "qwen3.8-27b"),
        ("groq", "qwen3.6-27b"),
        # Gemma 4 on Google AI Studio sees too - the second route, so a
        # picture is not stuck behind Groq's shared per-minute budget.
        ("google", "gemma"),
        ("openrouter", "gemma-4-31b-it:free"),
        ("openrouter", "minimax-m3:free"),
        ("ollama", "gemma3:4b"),
        ("ollama", "gemma3"),
        ("ollama", "llava"),
    ],
}


# HAVING a local vision model is not the same as being able to USE one.
#
# Measured on the deployment VM: gemma3:4b answered "what colour is
# this" about a 200x200 solid red square in 98 seconds, 91 of which were
# prompt_eval - the time spent looking at the picture before writing a
# single token. A real screenshot is bigger and blew straight through
# the 120-second HTTP timeout, so an attached image produced a two
# minute wait and then a ReadTimeout.
#
# That is worse than having no vision at all, because "I can't see
# images on this deployment" is instant and true, while a two-minute
# hang followed by an error looks like the site is broken.
#
# The number is generous. It is not "fast", it is "a person might
# plausibly wait": image understanding is a considered request, not a
# chat turn, so it gets more room than LOCAL_WARM_BUDGET_SECONDS.
LOCAL_VISION_BUDGET_SECONDS = 30.0

# A 1x1 PNG. Size does not matter for the measurement: gemma3 scales
# every image to a fixed patch count, so a tiny image costs the same
# prompt_eval as a large one, which is exactly what makes it a fair and
# cheap probe.
_PROBE_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
              "P8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

_vision_speed: dict[str, float | None] = {"seconds": None}


def _local_vision_is_fast():
    """Whether the local vision model is worth routing to.

    UNMEASURED MEANS NO, which is the opposite of _local_is_fast() and
    deliberately so. The two have opposite failure costs: guessing wrong
    about chat sends one reply to a hosted model, while guessing wrong
    about vision hangs somebody for two minutes and then errors. The
    probe runs at boot, so on hardware that can actually do this the
    answer is available long before anyone attaches anything.
    """
    seconds = _vision_speed["seconds"]
    return seconds is not None and seconds <= LOCAL_VISION_BUDGET_SECONDS


def _probe_vision_speed():
    """Time the local vision model once, on a 1x1 image. Never raises."""
    if not ollama_reachable():
        return
    names = ollama_provider().get("models") or []
    for provider_id, pattern in BAY_ROUTES["vision"]:
        if provider_id != "ollama":
            continue
        match = next((n for n in names if pattern in n.lower()), None)
        if not match:
            continue
        try:
            started = time.monotonic()
            requests.post(
                f"{OLLAMA_URL}/api/chat", headers=ollama_headers(),
                json={"model": match, "stream": False,
                      "keep_alive": OLLAMA_KEEP_ALIVE,
                      "messages": [{"role": "user", "content": "colour?",
                                    "images": [_PROBE_PNG]}],
                      "options": {"num_predict": 1}},
                timeout=LOCAL_VISION_BUDGET_SECONDS * 4)
            _vision_speed["seconds"] = time.monotonic() - started
        except (requests.exceptions.RequestException, ValueError):
            # A timeout here IS the answer: too slow to use.
            _vision_speed["seconds"] = float("inf")
        return


def _vision_route():
    """A provider/model pair that can read an image. -> (provider, model).

    (None, None) when nothing available can see one, which is the honest
    answer on a server with no GPU and no OpenRouter key - and the
    caller then leaves the model as it was, so the reply explains itself
    rather than pretending the attachment was never there.
    """
    for provider_id, pattern in BAY_ROUTES["vision"]:
        if provider_id == "pip":
            if not (pip_api.configured() and pip_api.has_room()):
                continue
            match = next((m for m in pip_api.vision_models()
                          if pattern in m.lower()), None)
            if match:
                return "pip", match
        elif provider_id == "groq":
            if not (groq_api.configured() and groq_api.budget_ok()):
                continue
            match = next((m for m in groq_api.vision_models()
                          if pattern in m.lower()), None)
            if match:
                return "groq", match
        elif provider_id == "google":
            if not google_api.ready():
                continue
            match = next((m for m in google_api.vision_models()
                          if pattern in m.lower()), None)
            if match:
                return "google", match
        elif provider_id == "openrouter":
            if not (openrouter_api.configured()
                    and openrouter_api.budget_ok()):
                continue
            # Matched against the models that can SEE, not the picker
            # list. models() is filtered to PREFERRED, which holds no
            # vision model, so this branch could never match and every
            # image on the hosted deployment was answered blind.
            match = next((m for m in openrouter_api.vision_models()
                          if pattern in m.lower()), None)
            if match:
                return "openrouter", match
        elif provider_id == "ollama":
            if not ollama_reachable():
                continue
            # Measured, not assumed. See LOCAL_VISION_BUDGET_SECONDS.
            if not _local_vision_is_fast():
                continue
            names = ollama_provider().get("models") or []
            match = next((n for n in names if pattern in n.lower()), None)
            if match:
                return "ollama", match
    return None, None


def _local_alternative(mode):
    """The local model that should answer instead of pip 1.0.

    Reads the same BAY_ROUTES table rather than hardcoding a name, so
    the fallback and the primary can never drift apart, and returns None
    when Ollama has nothing to offer - which the caller must handle,
    since falling back to nothing is not falling back.
    """
    if not ollama_reachable():
        return None

    # REACHABLE IS NOT THE SAME AS USABLE, and this is the second place
    # that distinction has cost a working reply.
    #
    # On the deployment VM Ollama answers, eventually. The engine is
    # busy, the request falls back here, and the
    # visitor waits out the full 120-second HTTP timeout before being
    # shown:
    #
    #   [Error talking to ollama: HTTPConnectionPool(host='localhost',
    #    port=11434): Read timed out. (read timeout=120)]
    #
    # Two minutes of waiting to be told it did not work is worse than
    # being told immediately that the channel is busy and when it frees
    # up - which is what the caller does when this returns None.
    #
    # _local_is_fast() is measured, not guessed: the boot warm-up times
    # a real request. Unlike the vision probe this one treats UNKNOWN as
    # usable, because the measurement may not have finished on the first
    # request after a restart and refusing then would strand the very
    # first visitor.
    if not _local_is_fast():
        return None

    # Fetched once. ollama_provider() is an HTTP round trip, and this
    # runs on the failover path, where latency is already the reason we
    # are here.
    names = ollama_provider().get("models") or []
    for provider_id, pattern in BAY_ROUTES.get(mode, BAY_ROUTES["chat"]):
        if provider_id != "ollama":
            continue
        match = next((n for n in names if pattern in n.lower()), None)
        if match:
            return match
    return names[0] if names else None


def _pip_model_for(mode: str) -> str | None:
    """The pip model this bay would pick, for a request that arrives on
    the engine by failover rather than by choice. None when the engine
    cannot answer at all."""
    if not (pip_api.configured() and pip_api.models()):
        return None
    names = pip_api.models()
    for provider_id, pattern in BAY_ROUTES.get(mode, BAY_ROUTES["chat"]):
        if provider_id != "pip":
            continue
        match = next((n for n in names if pattern in n.lower()), None)
        if match:
            return match
    return pip_api.PREFERRED[0]


def _groq_model_for(mode: str) -> str | None:
    """The Groq model this bay would pick when it arrives on the fast
    channel by failover rather than by choice. None when Groq cannot
    answer at all."""
    if not (groq_api.configured() and groq_api.models()):
        return None
    names = groq_api.models()
    for provider_id, pattern in BAY_ROUTES.get(mode, BAY_ROUTES["chat"]):
        if provider_id != "groq":
            continue
        match = next((n for n in names if pattern in n.lower()), None)
        if match:
            return match
    return next((m for m in groq_api.PREFERRED if m in names), names[0])


def _groq_only() -> bool:
    """GROQ, AND NOTHING ELSE, wherever Groq is set up.

    Decided by the owner: the slower channels - the free OpenRouter
    models, the local model on two CPU cores, pip 1.0 while its engine
    is not running - answered "make me a website" in minutes or not at
    all. With a Groq key present every chat and code reply goes to
    Groq, and the picker offers nothing else. A server without a key
    keeps the old routing, so it is never left with no model at all.
    """
    return groq_api.configured() and bool(groq_api.models())


def _deepseek_model_for(mode: str, strength: str = "") -> str | None:
    """DEEPSEEK FIRST, wherever it can answer - the owner's choice.

    V4 Pro for the code bay, Deep mode and website builds; V4.1 Flash
    for chat. None when neither can answer right now (no key, no credit,
    today's ceiling reached, or not switched on with DEEPSEEK_PRIMARY=1),
    and the caller falls through to Groq exactly as before.
    """
    pro, flash = openrouter_api.PRO_MODEL, openrouter_api.FLASH_MODEL
    wanted = pro if (mode == "code" or strength == "deep") else flash
    for m in (wanted, pro if wanted == flash else flash):
        if openrouter_api.deepseek_ready(m):
            return m
    return None


def _gemma_model() -> str | None:
    """GEMMA 4 31B FIRST, the owner's choice of main model, wherever Google
    AI Studio is configured and not cooling down after a rate limit. None
    otherwise, and the caller falls through to Groq exactly as before."""
    return google_api.model_id() if google_api.ready() else None


def _is_deepseek(provider: str, model: str) -> bool:
    return provider == "openrouter" and model in (
        openrouter_api.PRO_MODEL, openrouter_api.FLASH_MODEL)


def _groq_fallbacks(model: str) -> list[tuple[str, str]]:
    """Other Groq text models to try when `model` is rate-limited.
    Groq meters each model separately, so the smaller one usually still
    has room when the big one is at its per-minute ceiling."""
    have = set(groq_api.catalogue() or [])
    return [("groq", m) for m in groq_api.PREFERRED
            if m != model and m in have and m not in groq_api.VISION_MODELS]


def _groq_has_room(plan) -> bool:
    """Whether this plan may spend the shared per-minute budget now.
    Pro skips the reserve: when the site is busy the fast channel is
    what runs out, so being the one who still gets it is the benefit."""
    return groq_api.budget_ok(priority=(plan == features.PRO))


def _openrouter_model_for(mode: str) -> str | None:
    """The OpenRouter model this bay would pick when it arrives there by
    failover rather than by choice. None when OpenRouter cannot answer.

    Free ids only, and deliberately. The paid DeepSeek models at the top
    of PREFERRED are chosen by someone opening the Code bay; arriving
    here means another channel just refused, and spending money as a
    silent consequence of that is not a decision this code should make
    on someone's behalf.
    """
    if not openrouter_api.configured():
        return None
    names = [n for n in openrouter_api.models() if openrouter_api.is_free(n)]
    if not names:
        return None
    # The bay's own preference first, if it names a free id.
    for provider_id, pattern in BAY_ROUTES.get(mode, BAY_ROUTES["chat"]):
        if provider_id != "openrouter":
            continue
        match = next((n for n in names if pattern in n.lower()), None)
        if match:
            return match
    # Otherwise the first free id in PREFERRED that the catalogue still
    # has - PREFERRED is ordered, so this is the best free one available.
    for candidate in openrouter_api.PREFERRED:
        if openrouter_api.is_free(candidate) and candidate in names:
            return candidate
    return names[0]


def _failover_chain(provider: str, mode: str) -> list[tuple[str, str]]:
    """Who to ask next when `provider` refuses before its first chunk.

    -> [(provider, model), ...] in the order to try them. Every channel
    fails the same way (providers.py), so the fallback is a table rather
    than a special case per channel:

        ollama      -> pip -> openrouter
        pip         -> ollama -> openrouter
        openrouter  -> pip -> ollama

    OpenRouter sits last on the two chains that reach it because its
    free tier is metered by the day: it is the channel that still works
    when the engine is down AND no local model is running, and spending
    one of 50 daily requests before both are true would waste it.

    Each candidate is checked for being able to answer at all - a key,
    a model list, a local model that is up and measured fast enough -
    but NOT for having budget: a candidate that turns out to be
    rate-limited raises the same exception and the loop moves on, and
    the caller decides afterwards whether waiting for a refill is worth
    it. Checking budget here would only refuse a channel a second
    before it refused itself.
    """
    if provider in ("openrouter", "google", "anthropic") and _groq_only():
        # DeepSeek or Gemma refused (no credit, a rate limit, an outage,
        # an empty reply): Groq's main model takes it, then Groq's others.
        main = _groq_model_for(mode) or ""
        return ([("groq", main)] if main else []) + _groq_fallbacks(main)
    if _groq_only():
        return _groq_fallbacks(_groq_model_for(mode) or "")
    chain: list[tuple[str, str]] = []
    if provider in ("ollama", "openrouter", "groq"):
        engine_model = _pip_model_for(mode)
        if engine_model:
            chain.append(("pip", engine_model))
    # Groq before the free OpenRouter models and the local one: it is
    # the fastest thing here by a wide margin.
    if provider != "groq":
        fast = _groq_model_for(mode)
        if fast:
            chain.append(("groq", fast))
    if provider in ("pip", "ollama", "groq"):
        free = _openrouter_model_for(mode)
        if free:
            chain.append(("openrouter", free))
    if provider in ("pip", "openrouter", "groq"):
        local = _local_alternative(mode)
        if local:
            chain.append(("ollama", local))
    # The table says what to prefer; the router says what has actually
    # been answering in the last ten minutes. A channel that is failing
    # or crawling right now goes to the back of the line rather than
    # being tried first because it is listed first. See agents/router.py.
    return agent_router.rank(chain)


_CHANNEL_NAMES = {
    "pip": pip_identity.FULL_NAME,
    "ollama": "the local model",
    "openrouter": "the backup channel",
    "groq": "the fast channel",
}


def _channel_name(provider: str) -> str:
    """What a channel is called in a sentence shown to a person."""
    return _CHANNEL_NAMES.get(provider, provider)


def _retry_after(provider: str,
                 failures: list[tuple[str, BaseException]]) -> float | None:
    """Seconds until a busy channel will take a request, if that is known.

    The engine says so in Retry-After, which pip_api.busy_for() keeps.
    Other channels may put a number there too, which their RateLimited
    carries as its message; anything else is unknown, and unknown means
    do not wait.
    """
    if provider == "pip":
        return pip_api.busy_for() or None
    for p, exc in failures:
        if p == provider and isinstance(exc, providers.RateLimited):
            try:
                return float(str(exc))
            except ValueError:
                return None
    return None


def _nobody_answered(failures: list[tuple[str, BaseException]]) -> str:
    """The sentence shown when every channel refused before its first
    chunk. Names each one and why, in a person's words - never the
    exception's own text, which is a host and a port and a pool state.
    """
    tried = "; ".join("%s is %s" % (_channel_name(p), providers.describe(e))
                      for p, e in failures)
    if any(isinstance(e, providers.RateLimited) for _, e in failures):
        return ("[%s is answering as many people as it can right now "
                "and nothing else could take over (%s). Send that again "
                "in about %d seconds.]"
                % (pip_identity.FULL_NAME, tried,
                   max(1, int(pip_api.busy_for() or 5))))
    if not tried:
        return ("[No model is answering. If this is running on your own "
                "machine, start Ollama, or check OLLAMA_URL and "
                "OLLAMA_API_KEY on the server.]")
    return ("[No model could answer just now (%s). Try again in a "
            "moment.]" % tried)


# How long a request may wait for a busy channel to free up before
# giving up and saying so.
#
# The engine asks for a few seconds at a time, so the wait is usually
# short. Sitting on the request is only better than failing while
# it stays shorter than somebody's patience - past about half a minute
# a spinner is worse than a sentence telling them when to come back.
RATE_LIMIT_WAIT_SECONDS = 25

# The wait above is taken in slices, and the browser is told at every
# slice how long is left - so "pip 1.0 is busy" is a countdown
# in the thinking indicator rather than an unexplained pause. See the
# "wait" event in stream_event.
RATE_LIMIT_WAIT_SLICE = 2

# The longest reply asked of a local model on the failover path.
#
# Measured on the deployment VM: gemma3:1b writes about 180 tokens in
# 12 seconds. 900 is therefore roughly a minute of generation - long
# enough for a real answer, short enough that somebody waiting for it
# does not conclude the app has hung. The code bay's own ceiling is
# 3,500, which on this hardware is four minutes.
LOCAL_FALLBACK_MAX_TOKENS = 900

# How much conversation a local model is handed on the failover path.
#
# Reading the prompt is most of the work for a small model on shared
# cores, so a long thread costs more before the first token than the
# whole reply does after it. 12,000 characters is roughly 3,000 tokens -
# enough for the question and a couple of turns of context.
LOCAL_FALLBACK_MAX_CHARS = 12000


def _trim_for_local(history):
    """Cut a conversation down to what a small local model can read.

    Keeps the system prompt and the newest message - the question being
    asked is the one thing that must survive - and drops whole turns
    from the middle. A single oversized message is truncated with a
    marker, so the model says it only saw part rather than answering as
    though it saw everything.
    """
    kept = list(history or [])

    def size(msgs):
        return sum(len(m.get("content") or "") for m in msgs
                   if isinstance(m.get("content"), str))

    while size(kept) > LOCAL_FALLBACK_MAX_CHARS and len(kept) > 2:
        kept.pop(1 if kept[0].get("role") == "system" else 0)

    if size(kept) > LOCAL_FALLBACK_MAX_CHARS:
        for i in range(len(kept) - 1, -1, -1):
            content = kept[i].get("content")
            if isinstance(content, str) and len(content) > 2000:
                others = size([m for j, m in enumerate(kept) if j != i])
                room = max(1500, LOCAL_FALLBACK_MAX_CHARS - others)
                kept[i] = dict(kept[i], content=(
                    content[:room]
                    + "\n\n[... truncated to fit a smaller model. Say so "
                      "rather than answering as though you saw all of it.]"))
                break
    return kept


def _boot_probes():
    """Warm the chat model, then find out if local vision is usable.

    In that order, and in one thread. The vision probe can take a minute
    and a half on hardware that cannot do it, and running it first would
    delay the warm-up that every ordinary request depends on.
    """
    _warm_providers()
    _probe_vision_speed()


# Safe here: BAY_ROUTES above is what this reads.
threading.Thread(target=_boot_probes, daemon=True).start()


def _recommended_routes(providers, plan=None):
    """-> {bay: {"provider": id, "model": name}} for what is live now.

    Resolved from the live provider list rather than assumed, so a
    channel that is rate-limited, unconfigured or simply absent is
    skipped instead of being recommended and then failing.

    Models this plan may not run are skipped for the same reason. A
    recommendation the recipient cannot act on is worse than none: it is
    what put a free session on gemma3:4b and then refused the message.
    """
    by_id = {p["id"]: p for p in providers if p.get("available")}
    # On hardware that cannot run the model at a usable speed, the app's
    # own channel is skipped for the DEFAULT only. It stays in the picker
    # and still answers if someone chooses it deliberately - the judgement
    # here is about what a visitor should land on without asking, not
    # about what they are allowed to have.
    skip_local = not _local_is_fast()
    out = {}
    for bay, ranked in BAY_ROUTES.items():
        # Only skip local where something else in this bay could answer.
        # Vision is the case that matters: where nothing hosted can see
        # an image, the local vision model is not merely preferred, it
        # is the only option - and skipping it left the bay with no
        # route at all.
        has_alternative = any(
            pid != "ollama" and pid in by_id for pid, _ in ranked)
        first_choice = None
        for provider_id, pattern in ranked:
            if skip_local and has_alternative and provider_id == "ollama":
                continue
            provider = by_id.get(provider_id)
            if not provider:
                continue
            match = next(
                (m for m in provider["models"]
                 if pattern in m.lower()
                 and features.model_allowed(plan, m)
                 # The vision bay is for models that can see. pip 1.0
                 # is listed there for a build that reads images; a
                 # text-only engine must not be recommended for it.
                 and (bay != "vision" or provider_id != "pip"
                      or m in pip_api.vision_models())
                 # A paid id at a zero ceiling will be refused the moment
                 # it is used, so recommending it puts a name in the
                 # picker that cannot answer. Ask the same guard the
                 # request will ask.
                 and (provider_id != "openrouter"
                      or (openrouter_api.budget_ok(m)
                          and (openrouter_api.is_free(m)
                               or openrouter_api.has_credit())))
                 and (provider_id != "groq" or _groq_only()
                      or _groq_has_room(plan))), None)
            if not match:
                continue
            # A channel the router has watched fail for the last ten
            # minutes is not recommended to the next person while
            # another one is answering - but it is remembered, so a bay
            # whose every channel is struggling still gets its first
            # choice rather than nothing.
            if first_choice is None:
                first_choice = {"provider": provider_id, "model": match}
            if agent_router.health(provider_id, match)["failing"]:
                continue
            out[bay] = {"provider": provider_id, "model": match}
            break
        if bay not in out and first_choice is not None:
            out[bay] = first_choice
    return out


@app.route("/api/providers", methods=["GET"])
def list_providers():
    # pip 1.0, AND WHAT STANDS IN FOR IT.
    #
    # pip 1.0 is the model the picker shows. Ollama and OpenRouter sit
    # underneath and take a request only when the engine cannot - see
    # the failover in _stream_reply().
    plan = features.normalize_plan(current_account()[0].get("plan"))
    providers = [ollama_provider(plan)]
    if pip_api.configured():
        pip_models = pip_api.models()
        if pip_models and not pip_api.has_room():
            note = ("Busy right now - replies are coming from a backup "
                    "model for about %ds."
                    % max(1, int(pip_api.busy_for())))
        else:
            note = "Our own model, running on our own hardware."
        providers.append({
            "id": "pip",
            "label": pip_identity.FULL_NAME,
            "available": bool(pip_models),
            "models": pip_models,
            "model_info": [describe_model(m, plan) for m in pip_models],
            "note": note,
        })

    if groq_api.configured():
        groq_models = groq_api.models()
        remaining, resets_in = groq_api.budget_state()
        if remaining is not None and remaining < 500:
            note = ("At its per-minute limit - replies are coming from a "
                    "backup model for about %ds." % max(1, int(resets_in)))
        else:
            note = ("Open-weight models on dedicated accelerators - the "
                    "fastest replies here.")
        providers.append({
            "id": "groq",
            "label": "RandomGenerals Turbo",
            "available": bool(groq_models),
            "models": groq_models,
            "model_info": [describe_model(m, plan) for m in groq_models],
            "note": note,
        })

    # No model named on purpose: this is "should OpenRouter be offered at
    # all", not "may I spend on some particular id". It used to pass the
    # first entry in the catalogue, which is a PAID DeepSeek model, so
    # with paid spending switched off the entire channel - free models
    # included - disappeared from the picker.
    if openrouter_api.configured() and openrouter_api.budget_ok():
        or_models = openrouter_api.models()
        providers.append({
            "id": "openrouter",
            "label": "OpenRouter",
            "available": bool(or_models),
            "models": or_models,
            "model_info": [describe_model(m, plan) for m in or_models],
            "note": ("DeepSeek for code, over OpenRouter: V3 answers, "
                     "and Deep mode thinks with R1. Both are billed per "
                     "message to this server's key, within a daily "
                     "ceiling; the :free models cost nothing beyond an "
                     "account and are capped at about 50 requests a day."),
        })

    v4 = [m for m in (openrouter_api.PRO_MODEL, openrouter_api.FLASH_MODEL)
          if openrouter_api.deepseek_ready(m)]
    if v4:
        # DeepSeek answers every text message while it has credit
        # (_stream_reply), so the picker shows what is answering: the
        # two V4 models and nothing else. Groq stays behind them as the
        # backup and returns to the picker when the credit runs out.
        providers = [{
            "id": "openrouter",
            "label": "DeepSeek",
            "available": True,
            "models": v4,
            "model_info": [describe_model(m, plan) for m in v4],
            "note": ("DeepSeek V4: Pro writes code and websites, Flash "
                     "answers chat. RandomGenerals Turbo takes over if "
                     "it is unavailable."),
        }]
    elif _groq_only():
        providers = [p for p in providers if p["id"] == "groq"]
        # Both models, the person's choice: gpt-oss-120b (fast, the
        # default) and Gemma 4 31B (slower - it thinks first - and
        # metered by Google's free tier; Groq answers when it is busy).
        if _gemma_model():
            gm = [_gemma_model()]
            providers.append({
                "id": "google",
                "label": "Gemma",
                "available": True,
                "models": gm,
                "model_info": [describe_model(m, plan) for m in gm],
                "note": ("Gemma 4 31B by Google - more polished writing and "
                         "design, but slower. Turbo answers if it is busy."),
            })
    # pip 2, first in the list. Locked (a Pro tag) for Free accounts;
    # unavailable for the rest of the UTC day once the spending ceiling
    # is reached, when Turbo answers instead.
    if anthropic_api.configured():
        pm = anthropic_api.models()
        providers.insert(0, {
            "id": "anthropic",
            "label": anthropic_api.DISPLAY_NAME,
            "available": anthropic_api.ready(),
            "models": pm,
            "model_info": [describe_model(m, plan) for m in pm],
            "note": ("pip 2 - our most capable model: sharper answers, "
                     "better writing and code, and it reads images. Pro."),
        })
    live = [p for p in providers if p["available"]]
    if live:
        providers = live

    return jsonify({
        "recommended": _recommended_routes(providers, plan),
        "providers": providers,
        # Whether there is a SECOND image backend (Stable Diffusion on
        # this machine) to choose between, the hosted one being always
        # Renamed off "gemini_configured": Gemini never made an image
        # here after Imagen was dropped, and the frontend was still
        # printing "Local or Gemini" underneath the composer on the
        # strength of that name.
        "local_image": imagegen.local_available(),
        "image_sizes": list(imagegen.SIZES),
        "image_styles": list(imagegen.STYLES),
    })


# Product names for the models, so the picker reads like a product
# rather than a list of upstream vendor SKUs. Purely presentational -
# `id` is always the real model name the API is called with, and the
# About tab still states plainly which open models are underneath. The
# aim is a clear capability ladder, not hiding what this is built on.
MODEL_DISPLAY_NAMES = {
    # the main model (Google AI Studio)
    "gemma-4-31b-it": ("Gemma 4", "Google's open model - chat, code and websites"),
    # pip 2 - the Pro model (anthropic_api.py)
    anthropic_api.DEFAULT_MODEL: (
        anthropic_api.DISPLAY_NAME,
        "Our most capable model - sharper answers, reads images. Pro"),
    # local
    "gemma3:1b": ("Swift", "Fastest replies - the default here"),
    "gemma3:4b": ("Sight", "Sees attached images, thinks a little harder"),
    "gemma3": ("Gemma", "Google's open model, running on this server"),
    "llama3.2": ("Swift", "Fastest replies, lighter reasoning"),
    "qwen2.5-coder": ("Coder", "Tuned for writing and debugging code"),
    "qwen2.5": ("Core", "Best local accuracy for general questions"),
    "llava": ("Vision", "Can see and describe attached images"),
    # ours
    pip_identity.MODEL_ID: (pip_identity.FULL_NAME,
                            "Our own model - chat, code and diagrams"),
    # the fast channel (Groq)
    "openai/gpt-oss-120b": ("GPT-OSS 120B", "Fastest strong model - code, websites, maths"),
    "openai/gpt-oss-20b": ("GPT-OSS 20B", "Quickest cloud replies"),
    "qwen/qwen3.8-27b": ("Qwen 3.8", "Sees images, answers in plain prose"),
    "qwen/qwen3.6-27b": ("Qwen 3.6", "Sees images, balanced"),
    # backup channels
    "poolside/laguna-s-2.1:free": (
        "Laguna", "Free coding model - no cost beyond an account"),
    "cohere/north-mini-code:free": ("North", "Free, code-specialised"),
    "z-ai/glm-5.2:free": ("GLM", "Free generalist, 256k context"),
    "nvidia/nemotron-3-ultra-550b-a55b:free": (
        "Nemotron", "Free, 550B parameters, 1M context"),
    "deepseek/deepseek-v3.2": (
        "DeepSeek V3", "The coding model - PAID, a fraction of a cent a reply"),
    "deepseek/deepseek-r1-0528": (
        "DeepSeek R1", "Reasons before it writes; Deep mode's model - PAID"),
    "deepseek/deepseek-chat-v3-0324": (
        "DeepSeek V3 (0324)", "The earlier V3 - PAID"),
    "moonshotai/kimi-k2.7-code": (
        "Kimi", "Coding model - PAID, billed per message"),
    "moonshotai/kimi-k2.5": ("Kimi", "Moonshot's general model - PAID"),
}


def describe_model(model_id, plan=None):
    """-> {id, name, blurb, locked}. Falls back to the raw id for anything
    not in the table, so a model the user pulls themselves still appears.

    `locked` is the fix for a specific bad moment: the picker used to
    list every model the server had, the frontend would auto-select one,
    and the server would then refuse it with "that is a Pro model". The
    app offered something and then told you off for taking it. Whether a
    model is usable is the server's knowledge, so the server says it.
    """
    key = model_id.split(":")[0].strip().lower()
    name, blurb = MODEL_DISPLAY_NAMES.get(
        model_id, MODEL_DISPLAY_NAMES.get(key, (None, None)))
    return {
        "id": model_id,
        "name": name or model_id,
        "blurb": blurb or "",
        "locked": not features.model_allowed(plan, model_id),
    }


_ollama_health = {"at": 0.0, "ok": False}
OLLAMA_HEALTH_TTL = 20  # seconds


def ollama_reachable():
    """Is the local model server answering?

    Cached briefly: this is checked on every message, and a fresh TCP
    connect per request would add latency to the common case where it's
    obviously fine. 20s is short enough that recovery is noticed quickly.
    """
    now = datetime.datetime.now().timestamp()
    if now - _ollama_health["at"] < OLLAMA_HEALTH_TTL:
        return _ollama_health["ok"]
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags",
                         headers=ollama_headers(), timeout=2)
        ok = r.ok and bool(r.json().get("models"))
    except requests.exceptions.RequestException:
        ok = False
    _ollama_health.update({"at": now, "ok": ok})
    return ok


_TAGS_TTL = 15
_tags_cache = {"at": 0.0, "models": [], "ok": False}


def _ollama_tags():
    """-> (model names, reachable). Cached for a few seconds.

    Failures are cached too, and deliberately: when Ollama is down every
    request would otherwise pay the full connect timeout, turning one
    outage into a slow site rather than a degraded one.
    """
    now = time.time()
    if now - _tags_cache["at"] < _TAGS_TTL:
        return list(_tags_cache["models"]), _tags_cache["ok"]
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags",
                         headers=ollama_headers(), timeout=3)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        ok = True
    except (requests.exceptions.RequestException, ValueError, KeyError):
        models, ok = [], False
    _tags_cache.update({"at": now, "models": models, "ok": ok})
    return list(models), ok


def ollama_provider(plan=None):
    """The Ollama-served models. Kept as a function rather than a
    hardcoded dict so /api/providers reflects reality if Ollama isn't
    running or has no models pulled.

    THE LABEL IS THE PRODUCT NAME, THE NOTE IS THE TRUTH

    Every channel is called pip 1.0, because that is what this is - a
    visitor did not come here to choose between vendors, and a picker
    offering "Cloud" and "Fast cloud" was advertising suppliers instead
    of the thing they came for.

    What must not be lost in that is where a prompt actually goes, so
    every label carries a note that says it plainly, and the note is
    computed rather than fixed: "on our own hardware" stops being true
    the moment OLLAMA_URL points at Ollama Cloud, which is a supported
    way to run this. Branding decides the name; the endpoint decides the
    sentence underneath it.
    """
    # The installed model list changes when someone runs `ollama pull`,
    # which is approximately never on a running server - but it was being
    # fetched over HTTP on every /api/providers call, inside
    # _local_alternative() on every failover, and twice more per
    # /v1/chat/completions. A short cache turns all of those into one
    # request every few seconds without making a genuinely new model wait
    # noticeably to appear.
    models, available = _ollama_tags()

    remote = ollama_is_remote()
    if models:
        note = ("Runs off-device on Ollama Cloud, so prompts leave this "
                "machine." if remote else
                "Runs on this server's own hardware - prompts do not "
                "leave it.")
    else:
        note = ("Ollama Cloud isn't answering - check OLLAMA_API_KEY."
                if remote else
                "The on-device model isn't running right now.")

    return {
        "id": "ollama",
        "label": pip_identity.FULL_NAME,
        # HIDDEN FROM THE PICKER, NOT REMOVED.
        #
        # The picker shows pip 1.0. The local models stay installed and
        # reachable for the two jobs it may not be able to do: read an
        # image, while the engine is a text-only build, and answer at
        # all, while the engine is down or its queue is full. Without
        # something underneath it the site simply stops answering.
        #
        # The frontend hides any provider carrying this flag, so the
        # fallback stays invisible until it is needed.
        "hidden": True,
        "available": available and bool(models),
        "models": models,
        "model_info": [describe_model(m, plan) for m in models],
        "note": note,
    }


# ----------------------------------------------------------------------
# Thread management - threads belong to a bay (code / chat / image) and
# to whoever created them (current_owner_id()). Every route here checks
# ownership before reading or touching a thread, so one visitor's chats
# are never visible to another's.
# ----------------------------------------------------------------------
@app.route("/api/threads", methods=["GET"])
def get_threads():
    mode = request.args.get("mode")
    owner = current_owner_id()
    items = [(tid, t)
             for tid, t in THREADS.items() if t.get("owner_id") == owner]
    if mode in VALID_MODES:
        items = [(tid, t)
                 for tid, t in items if t.get("mode", DEFAULT_MODE) == mode]
    # ?q= is the sidebar's Search: titles and message text, every bay.
    # The threads are already in memory, so a substring scan over one
    # owner's conversations is cheaper than any index would be to keep.
    query = (request.args.get("q") or "").strip().lower()[:200]
    snippets = {}
    if query:
        hits = []
        for tid, t in items:
            if query in (t.get("title") or "").lower():
                hits.append((tid, t))
                continue
            for m in t.get("messages", []):
                text = m.get("content") if m.get("type") != "image" else ""
                if not isinstance(text, str):
                    continue
                at = text.lower().find(query)
                if at >= 0:
                    start = max(0, at - 40)
                    snippets[tid] = (("…" if start else "")
                                     + " ".join(text[start:at + len(query) + 80].split()))
                    hits.append((tid, t))
                    break
        items = hits
    summary = [
        {"id": tid, "title": t["title"], "updated": t["updated"],
         "mode": t.get("mode", DEFAULT_MODE),
         "project_id": t.get("project_id"),
         **({"snippet": snippets[tid]} if tid in snippets else {})}
        for tid, t in sorted(items, key=lambda x: x[1]["updated"], reverse=True)
    ]
    if query:
        summary = summary[:50]
    return jsonify({"threads": summary})


def _own_project_id(value):
    """A project id from a request, if it names one of this owner's
    projects. -> (project_id or None, error or None)."""
    if value in (None, ""):
        return None, None
    project = db.project_get(str(value))
    if not project or project["owner_id"] != current_owner_id():
        return None, "No such project."
    return project["id"], None


@app.route("/api/threads", methods=["POST"])
def create_thread():
    payload = request.get_json(force=True, silent=True) or {}
    mode = payload.get("mode") if payload.get(
        "mode") in VALID_MODES else DEFAULT_MODE
    project_id, err = _own_project_id(payload.get("project_id"))
    if err:
        return jsonify({"error": err}), 404
    tid = str(uuid.uuid4())
    thread = {
        "id": tid,
        "title": "New chat",
        "messages": [],
        "updated": now_iso(),
        "mode": mode,
        "owner_id": current_owner_id(),
        "project_id": project_id,
    }
    with DATA_LOCK:
        THREADS[tid] = thread
    save_thread(thread)
    return jsonify({"id": tid, "mode": mode})


@app.route("/api/threads/<tid>", methods=["GET"])
def get_thread(tid):
    thread = THREADS.get(tid)
    # 404 either way (not just for a genuinely missing id) so a guessed
    # thread id can't be used to tell "not yours" apart from "doesn't
    # exist" - same reasoning as any other object-ownership check.
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Not found"}), 404
    return jsonify(thread)


@app.route("/api/threads/<tid>", methods=["PATCH"])
def rename_thread(tid):
    """A title of the person's choosing, and/or the project the
    conversation is filed under ({"project_id": null} un-files it).
    Titles were the first forty characters of the first message and
    could never be changed."""
    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Not found"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    changes = {}
    if "project_id" in payload:
        project_id, err = _own_project_id(payload.get("project_id"))
        if err:
            return jsonify({"error": err}), 404
        changes["project_id"] = project_id
    if "title" in payload or not changes:
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 80:
            return jsonify({"error": "A title is 1 to 80 characters."}), 400
        changes["title"] = title
    with DATA_LOCK:
        thread.update(changes)
    save_thread(thread)
    return jsonify({"ok": True, "title": thread["title"],
                    "project_id": thread.get("project_id")})


# ----------------------------------------------------------------------
# Projects - names to file conversations under. The sidebar filters by
# them and the composer's Project menu files the open conversation.
# ----------------------------------------------------------------------
MAX_PROJECTS = 100


def _project_view(p, counts):
    return {"id": p["id"], "name": p["name"], "created": p["created"],
            "threads": counts.get(p["id"], 0)}


@app.route("/api/projects", methods=["GET"])
def list_projects():
    owner = current_owner_id()
    counts = {}
    for t in list(THREADS.values()):
        if t.get("owner_id") == owner and t.get("project_id"):
            counts[t["project_id"]] = counts.get(t["project_id"], 0) + 1
    return jsonify({"projects": [_project_view(p, counts)
                                 for p in db.projects_for(owner)]})


def _project_name(payload):
    name = " ".join(str(payload.get("name") or "").split())
    if not name or len(name) > 60:
        return None
    return name


@app.route("/api/projects", methods=["POST"])
def create_project():
    payload = request.get_json(force=True, silent=True) or {}
    name = _project_name(payload)
    if not name:
        return jsonify({"error": "A project name is 1 to 60 characters."}), 400
    owner = current_owner_id()
    existing = db.projects_for(owner)
    if len(existing) >= MAX_PROJECTS:
        return jsonify({"error": "That is the most projects one account can have."}), 400
    same = next((p for p in existing if p["name"].lower() == name.lower()), None)
    if same:
        # Creating "Biology" twice files into the one that exists.
        return jsonify(_project_view(same, {}))
    pid = str(uuid.uuid4())
    db.project_create(pid, owner, name, now_iso())
    return jsonify(_project_view(db.project_get(pid), {})), 201


def _own_project(project_id):
    project = db.project_get(project_id)
    if not project or project["owner_id"] != current_owner_id():
        return None
    return project


@app.route("/api/projects/<project_id>", methods=["PATCH"])
def rename_project(project_id):
    project = _own_project(project_id)
    if not project:
        return jsonify({"error": "Not found"}), 404
    name = _project_name(request.get_json(force=True, silent=True) or {})
    if not name:
        return jsonify({"error": "A project name is 1 to 60 characters."}), 400
    db.project_rename(project_id, name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    """The project goes; its conversations stay, un-filed."""
    project = _own_project(project_id)
    if not project:
        return jsonify({"error": "Not found"}), 404
    with DATA_LOCK:
        for t in THREADS.values():
            if t.get("project_id") == project_id:
                t["project_id"] = None
    db.project_delete(project_id)
    return jsonify({"ok": True})


@app.route("/api/threads/<tid>", methods=["DELETE"])
def delete_thread(tid):
    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Not found"}), 404
    with DATA_LOCK:
        THREADS.pop(tid, None)
    db.delete_thread(tid)
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# Streaming chat generator. One provider, Ollama - yields plain text
# chunks as the model produces them.
# ----------------------------------------------------------------------
_VISION_MODEL_HINTS = ("vision", "llava", "moondream", "minicpm-v", "bakllava")

# Gemma 3 is multimodal FROM 4B UPWARDS ONLY. 1b and 270m are text-only,
# so a substring match on "gemma3" would claim the default chat model can
# see pictures when it cannot.
#
# This list is why attachments silently stopped working: vision moved
# from llava to gemma3:4b and this function was not told. It returned
# False for the new model, _stream_reply() builds images_b64 only when
# it returns True, and so the picture was dropped on the floor before
# the request was ever made - no error anywhere, just a model answering
# as though nothing had been attached.
_VISION_MODEL_EXACT = (
    "gemma3:4b", "gemma3:12b", "gemma3:27b",
    # Free, and reachable from a server with no GPU. Checked against
    # OpenRouter's own architecture.input_modalities rather than assumed.
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "minimax/minimax-m3:free",
    "dots-studio/dots-3-note-preview:free",
)


def is_vision_model(model):
    name = (model or "").lower()
    if any(name.startswith(m) for m in _VISION_MODEL_EXACT):
        return True
    if any(hint in name for hint in _VISION_MODEL_HINTS):
        return True
    if name in {m.lower() for m in groq_api.VISION_MODELS}:
        return True
    if name in {m.lower() for m in google_api.vision_models()}:
        return True
    if name in {m.lower() for m in anthropic_api.vision_models()}:
        return True
    # pip 1.0 can see when the engine says its build can.
    if name in {m.lower() for m in pip_api.vision_models()}:
        return True
    # Whatever OpenRouter's own catalogue says can see. The lists above
    # are a snapshot; the catalogue is live, and a model the vision
    # route picked from it must be recognised here or its images are
    # never encoded (images_b64 is built only when this is True).
    return name in {m.lower() for m in openrouter_api.vision_models()}


def stream_ollama(model, history, options=None, images=None, usage=None):
    # TRIMMED HERE, not at each call site. There are three ways a
    # request reaches Ollama - chosen as the default, pre-empted because
    # pip 1.0 had no room, and failed over to after an error -
    # and only the last one was cutting the prompt down. The other two
    # handed a 1B model on shared cores an entire conversation, which it
    # spends longer reading than answering, and the request went silent
    # past the read timeout.
    #
    # Doing it inside the streamer means every path is covered and none
    # can be forgotten when a fourth is added.
    history = _trim_for_local(history)
    if images:
        # Ollama expects images on the specific message they belong to -
        # these came from the user's current turn, so attach them there
        # rather than mutating the caller's history list in place.
        history = history[:-1] + [{**history[-1], "images": images}]
    body = {
        "model": model,
        "messages": history,
        "stream": True,
        # Ollama unloads a model from memory after 5 minutes idle by
        # default - fine for one message, brutal for a conversation,
        # since the next reply then has to wait for a multi-GB model to
        # load off disk again before it can say a word. Keeping it warm
        # for 30 minutes trades some idle RAM for not paying that reload
        # tax on every message.
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if options:
        # Ollama's options are sampling parameters. The hosted channels
        # share this dict and read keys of their own from it - tools,
        # reasoning_effort - which Ollama would log as unknown.
        body["options"] = {k: v for k, v in options.items()
                           if k not in ("tools", "reasoning_effort")}
    # The same contract as the hosted channels (providers.py): before
    # the first chunk a failure is RAISED so the caller can answer from
    # elsewhere; after it, it is said at the end of the reply. Before
    # this, a refused connection escaped as a requests exception and was
    # written into the conversation as
    # "[Error talking to ollama: HTTPConnectionPool(...)]".
    sent_any = False
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=body,
            headers=ollama_headers(),
            stream=True,
            # (connect, read). The read timeout applies BETWEEN CHUNKS
            # on a streamed response, not to the whole reply - so this
            # is "how long may it go silent", not "how long may it
            # take".
            #
            # It was a single 120, which meant a stall - a model still
            # loading, or a request queued behind another one - showed
            # nothing at all for two minutes and then an
            # HTTPConnectionPool traceback in the middle of the
            # conversation. Two minutes of nothing is worse than a
            # quick, honest failure: once tokens are flowing they
            # arrive every fraction of a second, so 45 seconds of
            # silence already means something is wrong.
            timeout=(10, 45),
        ) as r:
            if r.status_code != 200:
                # A missing model, an overloaded server - either way
                # nothing has streamed and another channel may answer.
                raise providers.Unreachable(
                    "Ollama returned %d" % r.status_code)
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    if not sent_any:
                        raise providers.Unreachable(str(chunk["error"])[:200])
                    yield f"\n\n_…Ollama stopped: {chunk['error']}_"
                    return
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    sent_any = True
                    yield piece
                if chunk.get("done"):
                    if usage is not None:
                        usage["eval_count"] = chunk.get("eval_count")
                        if chunk.get("done_reason"):
                            usage["finish_reason"] = chunk["done_reason"]
                    # Why it stopped, said in the reply for the same
                    # reason the hosted channels say it: a reply cut
                    # off at num_predict looks exactly like one that
                    # ended awkwardly, and the failover path caps this
                    # channel at 900 tokens, where it happens often.
                    if (chunk.get("done_reason") == "length"
                            and not (usage or {}).get("auto_continue")):
                        yield ("\n\n_…that hit the length limit for one "
                               "reply. Say **continue** and I'll pick up "
                               "where I left off._")
                    return
    except requests.exceptions.RequestException as e:
        if not sent_any:
            raise providers.Unreachable(str(e))
        yield ("\n\n_…the connection to the model dropped part-way "
               "through. The answer above is incomplete — ask again to "
               "get the rest._")


def stream_event(payload: dict) -> str:
    """A structured event smuggled through the plain-text reply stream.

    The reply is streamed as text and the browser appends whatever
    arrives straight into the message bubble, so "searching the web..."
    can't just be yielded - it would become part of the answer. Wrapping
    events in U+001E (RECORD SEPARATOR) gives the frontend something it
    can split on and pull out: the character has no visual form, no
    meaning in markdown, and a language model will never emit one.

    Three shapes travel this way, told apart by their keys:

        {"tool": name, "status": "start"|"done", "display": ...}
        {"event": "reply", "kind": "error"|"refusal"|"notice"}
        {"event": "wait", "seconds": n}

    The second is what lets the browser style a fabricated sentence -
    a provider error, a refusal - as something other than an answer,
    and withhold Copy / Regenerate from it. Before it existed the only
    signal was a leading "[", which a reply beginning with a markdown
    link also has.
    """
    return "\x1e" + json.dumps(payload, separators=(",", ":")) + "\x1e"


def tool_event(payload: dict) -> str:
    """The original name, kept for the tool loop's call sites."""
    return stream_event(payload)


def _assistant_message(content: str, provider: str, model: str,
                       kind: str = "text", **extra: Any) -> dict:
    """The one place an assistant turn is built, so every site that
    fabricates one - the filter, the fallback sentence, the backstop -
    records what it is rather than passing as an answer."""
    if kind not in MESSAGE_KINDS:
        kind = "text"
    msg: dict[str, Any] = {
        "role": "assistant", "content": content, "type": "text",
        "provider": provider, "model": model, "kind": kind,
    }
    for key, value in extra.items():
        if value is not None and value is not False:
            msg[key] = value
    return msg


def _fit_to_plan_context(history: list[dict], plan) -> list[dict]:
    """The conversation, cut to the plan's context window (8,192 tokens
    on Free, 16,384 on Pro), newest turns kept.

    THE PRO CARD'S "TWICE THE CONTEXT" HAD NOTHING BEHIND IT. The window
    (features.py max_context_tokens) reached only a local model's num_ctx;
    the hosted models every reply actually comes from were sent the
    whole thread on either plan. It is applied here, to every channel.

    Estimated at four characters a token. The system prompt is kept
    whole, and the conversation always gets at least half the window, so
    a long system prompt (the website playbook, many memories) cannot
    crowd the conversation out. The latest message is always kept.
    """
    budget = int(features.get(plan, "max_context_tokens") or 8192)

    def size(m):
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content or "")
        return len(text) // 4 + 4

    system = [m for m in history if m.get("role") == "system"]
    rest = [m for m in history if m.get("role") != "system"]
    room = max(budget - sum(size(m) for m in system), budget // 2)
    kept, used = [], 0
    for m in reversed(rest):
        s = size(m)
        if kept and used + s > room:
            break
        kept.append(m)
        used += s
    return system + kept[::-1]


def _model_history(thread: dict) -> list[dict]:
    """The turns the model is allowed to see, oldest first.

    Text only - image turns are pictures, not prose - and only turns
    that were actually said: a blocked question, a refusal, a provider
    error and a "your memory is full" notice are all things the PERSON
    needed to see and the model has no use for. Replaying them taught it
    to apologise for outages it never had.
    """
    said = [m for m in thread["messages"]
            if m.get("type") == "text" and m.get("kind", "text") == "text"]

    # WHAT WAS ATTACHED STAYS IN THE CONVERSATION. A file's text used
    # to live only in the system prompt of the turn it arrived with, so
    # two messages later "what did that file say?" had no file text in
    # context and the model guessed. The newest user turn's files are
    # still in the system prompt (see _attachment_context_block), so it
    # is skipped here; the three before it carry theirs inline. Sources
    # an answer used travel the same way, as one line, so a follow-up
    # can refer back to them. Bounded by HISTORY_MAX_CHARS below.
    user_turns = [i for i, m in enumerate(said) if m.get("role") == "user"]
    carry_files = set(user_turns[-4:-1])

    turns = []
    for i, m in enumerate(said):
        content = m["content"]
        if i in carry_files:
            for f in (m.get("attachments") or []):
                if f.get("kind") == "text" and f.get("text"):
                    content += "\n\n[Attached: %s]\n%s" % (
                        f.get("filename") or "file", str(f["text"])[:6000])
        if m.get("role") == "assistant" and m.get("sources"):
            cited = "; ".join(
                "%s - %s" % (s.get("title") or "", s.get("url") or "")
                for s in m["sources"][:6] if isinstance(s, dict))
            if cited:
                content += "\n\n(Sources used: %s)" % cited
        turns.append({"role": m["role"], "content": content})

    if len(turns) > HISTORY_MAX_MESSAGES:
        turns = turns[-HISTORY_MAX_MESSAGES:]
    total = sum(len(t["content"]) for t in turns)
    while len(turns) > 2 and total > HISTORY_MAX_CHARS:
        total -= len(turns.pop(0)["content"])
    return turns


PROVIDER_STREAMERS = {
    "ollama": stream_ollama,
    "pip": pip_api.stream_chat,
    "openrouter": openrouter_api.stream_chat,
    "groq": groq_api.stream_chat,
    "google": google_api.stream_chat,
    "anthropic": anthropic_api.stream_chat,
}

# The non-streamed turn each hosted provider uses inside the tool loop.
# Ollama is absent on purpose: tool calling is offered on the hosted
# channels only - see the note above tool_specs in _stream_reply.
PROVIDER_TURNS = {
    "pip": pip_api.chat_once,
    "openrouter": openrouter_api.chat_once,
    "groq": groq_api.chat_once,
    "google": google_api.chat_once,
    "anthropic": anthropic_api.chat_once,
}

# Per account, and per PLAN - free connects one, Pro connects eight.
# Each connection adds its operation list to every request on that
# channel, so this is a prompt-size ceiling as much as a tidiness one.
#
# The number lives in features.py with every other tier difference; this
# is only the lookup, so there is one place to change it and no way for
# the limit the API enforces to drift from the limit the plan card
# advertises.
def max_connectors(plan):
    return features.FEATURES[features.normalize_plan(plan)]["max_connectors"]


# ----------------------------------------------------------------------
# Connected apps
#
# A pasted link becomes a tool the model can call. Discovery, the SSRF
# guard and the actual calling all live in connectors.py; what is here is
# the plumbing between that and a chat turn.
# ----------------------------------------------------------------------
def _recent_user_text(thread, turns=3):
    """The last few things the person said, for deciding which connected
    apps this conversation is about."""
    said = [m.get("content") or "" for m in (thread.get("messages") or [])
            if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return "\n".join(said[-turns:])


def _connected_apps_note(owner_id, text):
    """Tell the model which apps this person connected.

    Handed Notion's tools with nothing else said, the model answered "do
    you have access to Notion?" with "I don't have access to Notion" -
    a model is cautious about access nobody told it it has. So it is
    told, by name, and told to use them.
    """
    try:
        conns = [c for c in db.connections_for(owner_id) if c.get("status") == "ok"]
    except Exception:                       # noqa: BLE001
        return ""
    if not conns:
        return ""
    names, here = [], []
    for c in conns:
        app = extensions.APPS_BY_ID.get(c["app_id"])
        if not app:
            continue
        names.append(app["title"])
        if extensions.app_mentioned(app, text):
            here.append(app["title"])
    if not names:
        return ""
    note = ("CONNECTED APPS\nThis person has connected: %s. You DO have access to these "
            "through tools whose names start with mcp__ - never say you cannot reach an app "
            "listed here." % ", ".join(names))
    if here:
        note += (" Tools for %s are available in this reply: when the message is about it, "
                 "call them (search or fetch first to find things) and answer from what they "
                 "return. Asked whether you can access it, say yes and offer to look something "
                 "up." % ", ".join(here))
    else:
        note += (" Their tools load when a message names the app - if the person asks about "
                 "one without naming it, ask them to mention it by name.")
    return note


def _skills_for_turn(owner_id, text):
    """The skills this message switches on (extensions.match_skills)."""
    try:
        mine = db.skills_for(owner_id)
        installed = db.plugins_for(owner_id)
    except Exception:                       # noqa: BLE001 - skills are an extra
        mine, installed = [], []
    return extensions.match_skills(text, extensions.active_skills(mine, installed))


def _connector_tools(owner_id, text=""):
    """-> (tool specs, {tool name: connector}).

    The map is what turns a tool name back into the connection it came
    from, including its token, which never leaves the server. One-click
    apps (connections.py) are included when `text` - the recent
    messages - mentions them.
    """
    specs, by_name = connections.tools_for_turn(owner_id, text) if text else ([], {})
    try:
        saved = db.load_connectors(owner_id, with_tokens=True)
    except Exception:                       # noqa: BLE001
        return specs, by_name
    for item in saved:
        spec = connectors.tool_spec(item)
        name = spec["function"]["name"]
        # Two connections whose titles slugify the same would otherwise
        # collide and the second would silently shadow the first.
        if name in by_name:
            name = "%s_%s" % (name, item["id"][:6])
            spec["function"]["name"] = name
        specs.append(spec)
        by_name[name] = item
    return specs, by_name


def _dispatch_tool(name, raw_args, connector_map):
    """One tool call, whether it is built in or a connected app."""
    item = (connector_map or {}).get(name)
    if item is None:
        return tools.dispatch(name, raw_args)
    if item.get("kind") == "mcp":
        try:
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
        except (ValueError, TypeError):
            return {"text": "Your arguments were not valid JSON. Call the "
                            "tool again with correct JSON.", "display": None}
        if not isinstance(args, dict):
            return {"text": "Arguments must be a JSON object.", "display": None}
        return {"text": connections.call(item["owner_id"], item["conn_id"],
                                         item["tool"], args), "display": None}
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args or "{}")
        except (ValueError, TypeError):
            return {"text": "Your arguments were not valid JSON. Call the "
                            "tool again with correct JSON.", "display": None}
        if not isinstance(args, dict):
            return {"text": "Arguments must be a JSON object.",
                    "display": None}
    try:
        text = connectors.call(item, args, token=item.get("token"))
    except Exception as e:                  # noqa: BLE001 - same contract as
        text = "That connection failed: %s" % e   # tools.dispatch: never raise
    return {"text": text, "display": None}


@app.route("/api/web-search", methods=["POST"])
@limited("search", LIMIT_SEARCH, key=_limit_key_account)
def web_search_route():
    """Runs a live web search and hands the results back as plain JSON -
    the frontend renders them as source chips *and* sends the same list
    back with the /api/chat call so what's shown as "sources" is exactly
    what the model actually saw, not a second, possibly different search."""
    payload = request.get_json(force=True, silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"results": []})
    return jsonify({"results": websearch.search(query, max_results=5)})


def _sanitize_web_results(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if title and url:
            out.append({
                "title": title[:200],
                "url": url[:500],
                "snippet": str(item.get("snippet") or "").strip()[:500],
            })
    return out


def _page_context_block(thread):
    """The page the person has open in the workspace browser, for the
    message they just sent about it. Read through the same cache the
    pane used, so it is the page on their screen."""
    last = next((m for m in reversed(thread.get("messages") or [])
                 if m.get("role") == "user"), None)
    url = ((last or {}).get("page") or {}).get("url")
    if not url:
        return ""
    # The live tab first: it is exactly what is on their screen,
    # scrolled, clicked and logged-in state included.
    page = None
    owner = thread.get("owner_id")
    live = livebrowser.text(owner) if owner else None
    if live and live.get("url") and live["url"].rstrip("/") == url.rstrip("/"):
        page = live
    if page is None:
        try:
            page = browser.open_page(url)
        except browser.BrowseError as e:
            return ("The user has %s open in their workspace browser, but it "
                    "could not be read (%s). Say so if they ask about it." % (url, e))
    return ("The user has this page open in their workspace browser and is "
            "probably asking about it. Answer from it when relevant, and say "
            "so when it does not cover the question.\n"
            "Title: %s\nURL: %s\n--- page text ---\n%s\n--- end of page ---"
            % (page["title"], page["url"], page["text"][:browser.MAX_TEXT_CHARS]))


def _web_context_block(results):
    lines = [
        "Web search results for the user's question below. Use them if "
        "they're actually relevant, and mention which source you used "
        "(by title or URL) when you do. If they don't help, ignore them "
        "and answer from what you already know - don't force it."
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# File uploads - documents get their text extracted and fed to the model
# as context (same pattern as web search results above); images get
# saved and shown either way, but are only actually seen by the model
# if a vision-capable model is selected - see is_vision_model().
# ----------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
def upload_files():
    uploaded = [f for f in request.files.getlist("files")[:10]
                if f and f.filename]
    # The plan's ceiling, which features.py has always stated (20 MB
    # free, 100 MB Pro) and nothing enforced - the only cap was the
    # app-wide MAX_CONTENT_LENGTH. Checked per file and as a total,
    # since ten files just under the limit is not under the limit.
    plan = features.normalize_plan(current_account()[0].get("plan"))
    limit_mb = int(features.get(plan, "max_upload_mb") or 20)
    limit = limit_mb * 1024 * 1024
    total = 0
    for f in uploaded:
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        total += size
        if size > limit or total > limit:
            return jsonify({
                "error": "%s is over the %d MB upload limit for your plan."
                         % (f.filename, limit_mb),
                "limit_mb": limit_mb,
                "upgrade_required": plan != features.PRO,
            }), 413
    results = [attachments.save_and_extract(f) for f in uploaded]
    return jsonify({"attachments": results})


def _sanitize_attachments(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        kind = item.get("kind")
        if kind not in ("text", "image", "unsupported"):
            kind = "unsupported"
        text = item.get("text")
        out.append({
            "filename": filename[:200],
            "url": (str(item.get("url"))[:500] if item.get("url") else None),
            "kind": kind,
            "text": (str(text)[:6000] if text else None),
        })
    return out


def _attachment_context_block(files, vision_available, capped_at=None):
    text_files = [f for f in files if f["kind"] == "text" and f["text"]]
    image_files = [f for f in files if f["kind"] == "image"]
    bad_files = [f for f in files if f["kind"] == "unsupported"]

    blocks = []
    if text_files:
        parts = ["Files attached to this message:"]
        for f in text_files:
            parts.append(f"--- {f['filename']} ---\n{f['text']}")
        blocks.append("\n".join(parts))
    if image_files and not vision_available and capped_at is not None:
        names = ", ".join(f["filename"] for f in image_files)
        blocks.append(
            f"The user attached {len(image_files)} image(s) ({names}), but "
            f"they have used today's {capped_at} free image reads, so the "
            "images were not shown to you. Tell them plainly: image reading "
            "resets tomorrow, and Pro has no daily limit. Do not guess at "
            "what the images contain."
        )
    elif image_files and not vision_available:
        names = ", ".join(f["filename"] for f in image_files)
        blocks.append(
            f"The user also attached {len(image_files)} image(s) ({names}), "
            "but the active model can't see images - acknowledge them by "
            "name if relevant, but don't guess at what's in them."
        )
    if bad_files:
        names = ", ".join(f["filename"] for f in bad_files)
        blocks.append(f"Could not read (unsupported file type): {names}")
    return "\n\n".join(blocks)


# ----------------------------------------------------------------------
# Memory - short facts + a standing "custom instructions" block, saved
# per owner (see db.py) and folded into every chat's system prompt so
# they carry across separate conversations, not just within one thread.
#
# Facts are captured two ways: explicitly through Settings, or by typing
# a message starting with "remember ..." - deliberately a fixed trigger
# phrase rather than having the model itself decide what's worth
# remembering. A 3-7B local model is not a reliable judge of that, and a
# fixed phrase means the user always knows exactly when something gets
# saved instead of being surprised by hidden extraction later.
# ----------------------------------------------------------------------
REMEMBER_RE = re.compile(
    r"^(?:please\s+|also\s+)*remember(?:\s+that)?[\s:,-]+(.+)", re.IGNORECASE | re.DOTALL)

MEMORY_ACK_NUDGE = (
    "If the user's message starts with \"remember\", the fact has already "
    "been saved by the app itself - just acknowledge it briefly and "
    "naturally, don't repeat the whole thing back."
)


def maybe_capture_memory(owner_id, user_message):
    """Stores a fact if `user_message` matches the remember-trigger
    pattern. Silent no-op otherwise - never blocks or alters the chat.

    -> True if a fact was stored, False if it was skipped, None if the
    message wasn't a remember request at all. The caller uses that to
    tell the user their memory is full rather than silently dropping
    something they asked to be remembered.
    """
    match = REMEMBER_RE.match(user_message.strip())
    if not match:
        return None
    fact = match.group(1).strip()
    if not fact:
        return None

    account_credits, _ = current_account()
    limit = features.get(account_credits.get("plan"), "max_memories")
    if limit is not None and len(db.load_memories(owner_id)) >= limit:
        return False

    db.add_memory(owner_id, uuid.uuid4().hex, fact[:500], now_iso())
    return True


def _memory_context_block(memories, custom_instructions):
    blocks = []
    if custom_instructions:
        blocks.append(
            "Standing instructions from the user for how you should "
            f"respond, in every conversation:\n{custom_instructions}")
    if memories:
        facts = "\n".join(f"- {m['content']}" for m in memories)
        blocks.append(
            "Things the user has asked you to remember about them, from "
            f"past conversations:\n{facts}")
    return "\n\n".join(blocks)


@app.route("/api/memory", methods=["GET"])
def get_memory():
    owner_id = current_owner_id()
    return jsonify({
        "memories": db.load_memories(owner_id),
        "custom_instructions": db.load_custom_instructions(owner_id),
    })


@app.route("/api/memory", methods=["POST"])
def add_memory_route():
    payload = request.get_json(force=True, silent=True) or {}
    content = (payload.get("content") or "").strip()[:500]
    if not content:
        return jsonify({"error": "Nothing to remember."}), 400
    owner_id = current_owner_id()

    account_credits, _ = current_account()
    limit = features.get(account_credits.get("plan"), "max_memories")
    if limit is not None and len(db.load_memories(owner_id)) >= limit:
        return jsonify({
            "error": f"Free accounts remember up to {limit} things. "
            "Remove one, or upgrade to Pro for unlimited memory.",
            "upgrade_required": True,
        }), 402

    memory_id = uuid.uuid4().hex
    db.add_memory(owner_id, memory_id, content, now_iso())
    return jsonify({"id": memory_id, "content": content})


@app.route("/api/memory/<memory_id>", methods=["DELETE"])
def delete_memory_route(memory_id):
    deleted = db.delete_memory(current_owner_id(), memory_id)
    if not deleted:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/memory/instructions", methods=["PUT"])
def save_instructions_route():
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()[:2000]
    db.save_custom_instructions(current_owner_id(), text)
    return jsonify({"text": text})


# ----------------------------------------------------------------------
# Image generation - see imagegen.py, not Ollama, which only serves LLMs.
#
# Three backends, and this route does not choose between the hosted ones:
# imagegen.best_backend() does, so setting a Cloudflare key upgrades every
# picture without anyone editing a setting or finding a menu. Local Stable
# Diffusion stays an explicit request, because it is the only one whose
# availability depends on the machine rather than on a credential.
#
# Google's Imagen used to be the hosted option; it was dropped because it
# needed the same key as chat, so revoking one key took out two features.
# ----------------------------------------------------------------------
def _image_model_name(backend):
    """What to record as having drawn it.

    Recorded per message rather than derived at read time, so a thread
    opened months later still says which model made a given picture even
    though the default has moved on since.
    """
    if backend == "local":
        return imagegen.MODEL_ID
    if backend == "cloudflare":
        return imagegen.CF_MODEL
    # Not FLUX_MODEL: that constant is the string sent to Pollinations,
    # which ignores it. See the note at the top of imagegen.py.
    return "sana"


@app.route("/api/generate-image", methods=["POST"])
@limited("image", LIMIT_IMAGE, key=_limit_key_account)
def generate_image_route():
    payload = request.get_json(force=True, silent=True) or {}
    tid = payload.get("thread_id")
    prompt = (payload.get("prompt") or "").strip()
    # Anything that is not an explicit request for local hardware takes
    # whichever hosted backend is best here - see imagegen.best_backend().
    backend = ("local" if payload.get("backend") == "local"
               else imagegen.best_backend())
    # Asking for local on a host without torch is a request that cannot be
    # served; falling back beats an error for something the person did not
    # choose and probably cannot see.
    if backend == "local" and not imagegen.local_available():
        backend = imagegen.best_backend()
    size = payload.get("size") or imagegen.DEFAULT_SIZE
    style = payload.get("style") or imagegen.DEFAULT_STYLE
    # The widest shapes are ~25% more pixels and cost ~25% more to make,
    # so they are a Pro shape. Falling back to square beats an error for
    # a setting most people will not have chosen deliberately.
    if not features.image_size_allowed(
            current_account()[0].get("plan"), size):
        size = imagegen.DEFAULT_SIZE
    # The hosted model is the better one and costs this app nothing, so it
    # is no longer the pricier option it was when hosted meant Imagen.
    cost = image_cost(size)

    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Unknown thread"}), 400
    if not prompt:
        return jsonify({"error": "Describe what you want to see."}), 400

    # The stricter image bar, not the conversational one - see the note
    # in moderation.py. Checked before any credits are touched, so a
    # refused prompt costs nothing.
    blocked = moderation.check_image_prompt(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400

    ok, account_credits = charge_up_front(cost)
    if not ok:
        return jsonify({
            "error": "Out of credits for image generation. They'll refill "
                     "automatically, or upgrade to Pro in Settings.",
            "credits": credits_view(account_credits),
        }), 402

    with DATA_LOCK:
        thread["messages"].append(
            {"role": "user", "content": prompt, "type": "text",
             "kind": "text"})
        if thread["title"] == "New chat":
            thread["title"] = prompt[:40]

    # A language model expands the request before it reaches the image
    # model - see enhance_prompt() in imagegen.py for why that is the
    # biggest quality lever available without paying for better pixels.
    # Opt-out rather than opt-in: the gain is large, the round trip is
    # short, and a failure silently leaves
    # the person's own words in place.
    url, error = imagegen.generate_image(
        prompt, backend=backend, size=size, style=style,
        enhance=payload.get("enhance") is not False)

    if error:
        # Paid for up front; nothing was made. Give it back.
        refund_charge(cost)
        thread["updated"] = now_iso()
        save_thread(thread)
        return jsonify({"error": error}), 502

    with DATA_LOCK:
        thread["messages"].append({
            "role": "assistant",
            "content": url,
            "type": "image",
            "provider": "imagegen",
            "model": _image_model_name(backend),
            "kind": "text",
        })
        thread["updated"] = now_iso()
    save_thread(thread)

    # Charged up front (charge_up_front above), not here. Not echoing a
    # `credits` field either: the frontend re-fetches /api/credits
    # itself right after, same as it
    # does following a normal chat reply.
    return jsonify({"url": url})


# ----------------------------------------------------------------------
# Video generation - PixVerse, see pixverse.py.
#
# This replaced a video EDITOR that ran ffmpeg on the server. The editor
# was removed rather than kept alongside: on a two-core VM with no GPU a
# re-encode of anything longer than a short clip took tens of minutes,
# which is not a slow feature, it is a broken one.
#
# What replaces it is not the same product. Generation makes short clips
# from a description; it cannot trim, cut or caption footage somebody
# already has. That capability is gone, deliberately.
#
# Unlike every other feature here, each call spends real money, so this
# is Pro-only and metered by a hard monthly count rather than credits -
# see the note at the top of pixverse.py for why those must not be the
# same pool.
# ----------------------------------------------------------------------
# THE BACKENDS, IN THE ORDER THE BAY PREFERS THEM
#
# Higgsfield first: it is the one this engine was built around, with a
# choice of models, image-to-video, and per-model controls. PixVerse
# next (paid, one model). Hugging Face's free allowance was measured at
# about three clips a MONTH site-wide, so it is a last resort for video.
# Tripo makes 3D meshes rather than clips and sits at the end so that a
# server with only that key still has a working bay.
#
# Every backend has the same shape (see videogen's docstring); the
# engine, not the routes, knows which one answered.
VIDEO_BACKENDS = (
    ("higgsfield", higgsfield_api),
    ("pixverse", pixverse),
    ("huggingface", hfvideo),
    ("tripo", tripo3d),
)
for _name, _module in VIDEO_BACKENDS:
    videogen.register(_name, _module)


def _video_backends() -> list[tuple[str, Any]]:
    """(name, module) for every backend with a key on this server."""
    return [(n, m) for n, m in VIDEO_BACKENDS if m.configured()]


def _video_backend(name: str | None = None):
    """The backend a request should use: the one it named if that is
    live, else the first live one. -> (module, name) or (None, None)."""
    live = _video_backends()
    if name:
        for n, m in live:
            if n == name:
                return m, n
    if live:
        return live[0][1], live[0][0]
    return None, None


def video_configured():
    return _video_backend()[0] is not None


def _video_turn(system: str, user: str) -> str | None:
    """The text model the prompt engineer uses: pip 1.0 when it has room,
    else whatever Ollama has loaded. None when neither can answer, and
    the engine then keeps the person's own words."""
    if pip_api.configured() and pip_api.models() and pip_api.has_room():
        model = _pip_model_for("chat")
        if model:
            msg, err = pip_api.chat_once(
                model, [{"role": "system", "content": system},
                        {"role": "user", "content": user}],
                options={"temperature": 0.4, "num_predict": 400}, timeout=30)
            if msg and not err and msg.get("content"):
                return msg["content"]
    text = imagegen._complete(system, user, max_tokens=400, timeout=30)
    return text or None


def _video_refund_failed(job: dict) -> None:
    """A job the provider accepted and then could not finish: give the
    period's count back. Called by the engine exactly once per job."""
    period = (job.get("params") or {}).get("period")
    if period:
        db.video_refund(job["owner_id"], period)


def _video_webhook_token() -> str:
    """A server-specific path segment for the provider's callback. The
    callback is only a nudge to poll, so a guessed token buys nothing
    but a poll; the token keeps it from being a public poll button."""
    return hmac.new(app.secret_key.encode(), b"video-webhook",
                    hashlib.sha256).hexdigest()[:32]


def _video_webhook_url(job_id: str) -> str | None:
    """Where a provider may POST when a job ends - only when this server
    is reachable over public HTTPS, which the providers require."""
    base = PUBLIC_SITE_URL
    if not base.startswith("https://") or os.environ.get("VIDEO_WEBHOOKS", "1") != "1":
        return None
    return "%s/api/video/webhook/%s" % (base, _video_webhook_token())


videogen.start_poller(on_failed=_video_refund_failed,
                      webhook_url=_video_webhook_url)


def _video_period_key(plan):
    """The bucket this generation counts against.

    'YYYY-MM-DD' for a daily plan, 'YYYY-MM' for a monthly one. Storing
    the period rather than a reset timestamp means there is no scheduled
    job and no clock to drift: yesterday's row simply stops being the
    one that gets read.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if features.video_period_for(plan) == "day":
        return now.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m")


def _video_quota_view(owner_id, plan, signed_in=True):
    limit = features.video_quota_for(plan)
    period = _video_period_key(plan)
    used = db.video_used(owner_id, period) if limit else 0
    return {
        "allowed": features.video_allowed(plan) and signed_in,
        "needs_account": (features.video_needs_account(plan)
                          and not signed_in),
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "period": features.video_period_for(plan),
        "resets": period,
    }


# ----------------------------------------------------------------------
# Diagrams. The fourth bay, and the only generative one that is free
# at any volume - because the drawing happens in the visitor's
# browser.
#
# The server's whole job is to get a language model to emit valid
# Mermaid; mermaid.js turns that into an SVG client-side. There is no
# render, no GPU, no per-call cost, and nothing to meter beyond the
# chat credits the model call already spends. That is the difference
# between this and the video bay it replaced: a clip cost real money
# per second on hardware this deployment does not have.
# ----------------------------------------------------------------------
DIAGRAM_SYSTEM_PROMPT = (
    "You draw diagrams as Mermaid source. Output ONLY the Mermaid "
    "code - no prose, no explanation, and no markdown fences.\n\n"
    # Each line below is a way the output has actually been seen to
    # break a render, not a style preference.
    "Pick the diagram type that fits: flowchart for processes and "
    "architecture, sequenceDiagram for messages between parties, "
    "erDiagram for schemas, stateDiagram-v2 for state machines, "
    "classDiagram for types, gantt for schedules.\n"
    "Quote every label containing a space, bracket, slash or "
    "punctuation. Node[Send request] parses; Node[Send request (v2)] "
    "does not unless quoted.\n"
    "Keep node ids short and alphanumeric, and never reuse an id for "
    "two different nodes.\n"
    "Aim for 6-14 nodes. A diagram that does not fit on a screen is a "
    "worse answer than one that leaves out a detail.\n"
    "Do not set styles, themes or colours - the page supplies those, "
    "and a hardcoded colour is unreadable in one of the two themes.\n"
    # Measured: this was the single parse failure in a 12-diagram run,
    # graded by the real renderer. The model wrote `class Queen {}` for
    # pieces with no listed members, and mermaid rejects an empty body.
    "In classDiagram, a class with no members is declared as "
    "`class Queen` on its own line. Never `class Queen {}` - an empty "
    "body does not parse."
)

# TOOLS, AND THE PROMPT THAT WAS ARGUING WITH THEM
#
# The tool loop was wired and correct - exercised directly it called
# run_python and returned 13152402 - and it still never fired through
# the chat route. The MATHS section above tells the model to work
# things out step by step in the reply, and it obeyed: it wrote the
# multiplication out, then stated 13153242, which is wrong.
#
# Having a calculator and being told to do it in your head is not a
# wiring bug, it is a contradiction in the instructions. This says
# which one wins and when.
TOOL_USE_PROMPT = (
    "\nTOOLS\n"
    "You can run Python, search the web, and generate images. Use "
    "them - a tool that returns the answer beats reasoning that "
    "might.\n"
    "Run the code for arithmetic with more than a couple of digits, "
    "for anything with many steps, and for any date, unit or "
    "financial calculation. Working it out in prose is exactly where "
    "a confident wrong number comes from - and the working looks "
    "just as convincing when the total is wrong.\n"
    "Search the web when the answer depends on anything current: "
    "prices, versions, releases, who holds a post, what happened "
    "recently. Do not guess and do not apologise for not knowing - "
    "look it up.\n"
    "Do not use a tool for something you already know. Looking up "
    "the boiling point of water wastes a second of somebody time "
    "and tells them you cannot tell the difference.\n"
    "When a tool answers, use its result. Do not restate a number "
    "you calculated yourself over one the tool returned.\n"
    "When someone wants pictures of something, call find_images and "
    "show the results as markdown images (![description](url)) with "
    "the credit line underneath. Never invent an image URL.\n"
)


def _clean_mermaid(text):
    """Strip the wrapping a model adds even when told not to.

    Fences are the common one; a leading "Here is the diagram:" is
    frequent enough to be worth removing rather than failing on.
    """
    body = (text or "").strip()
    if "```" in body:
        parts = body.split("```")
        # The longest fenced block is the diagram; prose around it
        # is not.
        blocks = [p for i, p in enumerate(parts) if i % 2 == 1]
        if blocks:
            body = max(blocks, key=len)
            first, _, rest = body.partition(chr(10))
            if first.strip().lower() in ("mermaid", "mmd"):
                body = rest
    lines = [l for l in body.splitlines() if l.strip()]
    # Drop any preamble before the first line that opens a diagram.
    starts = ("flowchart", "graph", "sequencediagram", "erdiagram",
              "statediagram", "classdiagram", "gantt", "pie",
              "mindmap", "journey", "timeline", "gitgraph")
    for i, l in enumerate(lines):
        if l.strip().lower().startswith(starts):
            return chr(10).join(lines[i:]).strip()
    return chr(10).join(lines).strip()


@app.route("/api/diagram", methods=["POST"])
@limited("diagram", LIMIT_DIAGRAM, key=_limit_key_account)
def diagram_route():
    payload = request.get_json(force=True, silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Describe what you want drawn."}), 400

    blocked = moderation.check_message(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400

    account_credits, save_account = current_account()
    apply_refill(account_credits)
    save_account()
    if account_credits["balance"] < CREDIT_COST_CHAT:
        return jsonify({
            "error": "Out of credits. They will refill automatically.",
            "credits": credits_view(account_credits),
        }), 402

    # Routed like the code bay: a diagram is source, and the model
    # that writes the best code writes the best Mermaid.
    #
    # THIS ONLY EVER KNEW ABOUT pip 1.0 AND THE LOCAL MODEL. When Groq
    # came back as the chat and code channel, the bay kept asking for
    # an engine that is not running and a 1B model on two CPU cores, and
    # every diagram on the live site failed with "pip 1.0 is busy and no
    # backup model is running". It now walks the same candidates as a
    # code reply: Groq (and Groq's smaller model when the big one is at
    # its limit), then whatever the old route picked.
    plan = features.normalize_plan(account_credits.get("plan"))
    candidates: list[tuple[str, str]] = []
    groq_model = _groq_model_for("code")
    if groq_model and _groq_has_room(plan):
        candidates.append(("groq", groq_model))
        candidates += _groq_fallbacks(groq_model)
    if not _groq_only():
        providers = [ollama_provider(plan)]
        if pip_api.configured():
            pip_models = pip_api.models()
            providers.append({"id": "pip", "available": bool(pip_models),
                              "models": pip_models,
                              "model_info": []})
        route = _recommended_routes(providers, plan).get("code") or {}
        if route.get("model"):
            candidates.append((route.get("provider") or "ollama", route["model"]))
        free = _openrouter_model_for("code")
        if free:
            candidates.append(("openrouter", free))
        local = _local_alternative("code")
        if local:
            candidates.append(("ollama", local))
    if not candidates:
        return jsonify({"error": "No model is available right now."}), 503

    history = [{"role": "system", "content": DIAGRAM_SYSTEM_PROMPT},
               {"role": "user", "content": prompt}]
    # 1,500, up from 900: gpt-oss thinks before it writes, and that
    # thinking comes out of the same allowance as the diagram.
    opts = {"num_predict": 1500, "temperature": 0.2, "num_ctx": 8192}
    text = ""
    provider = model = None
    tried: list[str] = []
    for provider, model in candidates:
        streamer = PROVIDER_STREAMERS.get(provider)
        if streamer is None:
            continue
        try:
            text = "".join(streamer(model, history, options=opts))
        except pip_api.ProviderUnavailable:
            tried.append(_channel_name(provider))
            continue
        if _clean_mermaid(text):
            break
    if not text:
        return jsonify({
            "error": "No model could draw that just now (%s). Try again "
                     "in a moment." % (", ".join(tried) or "none answered"),
        }), 503

    source = _clean_mermaid(text)
    if not source:
        return jsonify({
            "error": "The model did not return a diagram. Try "
                     "describing it differently.",
        }), 502

    spend_credits(usage_based_cost(len(source) // 4, mode="code"))
    return jsonify({"source": source, "model": model,
                    "provider": provider})


def _video_status_payload(account, signed_in):
    plan = features.normalize_plan(account.get("plan"))
    live = _video_backends()
    default = live[0][0] if live else None
    backends = []
    for name, module in live:
        caps = videogen.caps_for(module, None)
        backends.append({
            "id": name, "label": caps.get("label", name),
            "kind": caps.get("kind", "video"),
            "models": caps.get("models") or [],
            "default_model": caps.get("default_model"),
            "seconds": list(caps.get("seconds") or (1, 8)),
            "seconds_discrete": bool(caps.get("seconds_discrete")),
            "default_seconds": caps.get("default_seconds", 5),
            "ratios": caps.get("ratios") or [],
            "resolutions": caps.get("resolutions") or [],
            "default_resolution": caps.get("default_resolution", "720p"),
            "negative": bool(caps.get("negative")),
            "seed": bool(caps.get("seed")),
            "motion": bool(caps.get("motion")),
            "image_to_video": bool(caps.get("image_to_video")),
            "free_tier": bool(caps.get("free_tier")),
            # Credits left, when the backend can report them. A key with
            # an empty balance is indistinguishable from a working one
            # until someone waits on a job that cannot start.
            "credits": tripo3d.balance() if name == "tripo" else None,
        })
    return {
        "configured": bool(live),
        "backend": default,
        "backends": backends,
        # Say WHICH thing is missing. "Not switched on" is true and
        # useless - it gives whoever runs the server nothing to do.
        "detail": "" if live else higgsfield_api.unavailable_reason(),
        "kind": "model" if default == "tripo" else "video",
        "free_tier": default == "huggingface",
        "enhancer": bool((pip_api.configured() and pip_api.models())
                         or ollama_reachable()),
        "quota": _video_quota_view(current_owner_id(), plan, signed_in),
    }


@app.route("/api/video/status", methods=["GET"])
def video_status():
    """What the bay should render before anyone types anything: every
    live backend with the controls it honours, and the quota."""
    account, _ = current_account()
    return jsonify(_video_status_payload(account, bool(session.get("user_id"))))


def _video_gate(plan, signed_in):
    """The plan and account checks every generation route shares.
    -> (json, status) to return, or None to proceed."""
    if not features.video_allowed(plan):
        return jsonify({
            "error": "Video generation isn't available on your plan.",
            "upgrade_required": True,
        }), 402
    # Not an upsell - a cost control. The quota is keyed on the owner id,
    # and a signed-out owner id is just a cookie: clearing it mints a new
    # identity with a fresh allowance. For a feature billed per call that
    # is an open tap, so free generation needs a real account.
    if features.video_needs_account(plan) and not signed_in:
        return jsonify({
            "error": "Make a free account to generate videos - it takes a "
                     "moment, and it's how the daily limit is kept fair.",
            "needs_account": True,
        }), 401
    return None


@app.route("/api/video/enhance", methods=["POST"])
@limited("video_enhance", LIMIT_VIDEO_ENHANCE, key=_limit_key_account)
def video_enhance():
    """The engineered prompt, before anything is spent - so the person
    can read the brief, edit it, and generate from that instead."""
    payload = request.get_json(force=True, silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Describe the video you want."}), 400
    blocked = moderation.check_image_prompt(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400
    module, name = _video_backend(payload.get("backend"))
    caps = videogen.caps_for(module, payload.get("model")) if module else {}
    spec = videogen.parse_spec(dict(payload, prompt=prompt, enhance=True), caps)
    brief = videogen.enhance(spec, _video_turn)
    return jsonify({"prompt": brief["prompt"], "negative": brief["negative"],
                    "enhanced": brief["enhanced"], "brief": brief.get("brief")})


@app.route("/api/video/generate", methods=["POST"])
@limited("video", LIMIT_VIDEO, key=_limit_key_account)
def video_generate():
    payload = request.get_json(force=True, silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Describe the video you want."}), 400

    module, backend_name = _video_backend(payload.get("backend"))
    if module is None:
        return jsonify({
            "error": "Generation isn't set up on this server yet.",
            "detail": higgsfield_api.unavailable_reason(),
        }), 503

    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    signed_in = bool(session.get("user_id"))
    gate = _video_gate(plan, signed_in)
    if gate:
        return gate

    # The image bar, not the chat one: a generated clip is a published
    # artefact in exactly the way a generated picture is, and the same
    # reasoning applies - there is no context in which the output is a
    # discussion of the thing rather than the thing itself.
    blocked = moderation.check_image_prompt(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400

    caps = videogen.caps_for(module, payload.get("model"))
    spec = videogen.parse_spec(dict(payload, prompt=prompt), caps)
    if payload.get("model") and spec.model != payload.get("model"):
        return jsonify({"error": "That model isn't offered by %s." % backend_name}), 400

    # Image-to-video: the picture must be somewhere the provider can
    # fetch it, so it is uploaded to the provider's own storage first.
    # Only a backend that can do that offers the control at all.
    image = payload.get("image")
    if image and caps.get("image_to_video") and hasattr(module, "upload_image"):
        decoded = higgsfield_api.decode_data_url(str(image)) if isinstance(image, str) else None
        if not decoded:
            return jsonify({"error": "Send the image as a data URL."}), 400
        data, ctype = decoded
        if len(data) > 8 * 1024 * 1024:
            return jsonify({"error": "Keep the image under 8 MB."}), 413
        public_url, err = module.upload_image(data, ctype)
        if err:
            return jsonify({"error": err}), 502
        spec.image_url = public_url
        if spec.model and not any(
                m.get("id") == spec.model and m.get("image_to_video", True)
                for m in caps.get("models") or []):
            return jsonify({"error": "%s can't start from an image." % spec.model}), 400
    elif spec.model and not any(
            m.get("id") == spec.model and m.get("text_to_video", True)
            for m in caps.get("models") or []):
        return jsonify({"error": "%s needs a starting image." % spec.model}), 400

    owner = current_owner_id()
    period = _video_period_key(plan)
    limit = features.video_quota_for(plan)
    # Counted BEFORE the call, and the comparison to the limit is inside
    # the same statement as the count (db.video_try_consume). It used to
    # be a read, a compare, and then a consume - so two requests arriving
    # together both read 1, both passed, and both generated, and the
    # overage is money rather than a rate limit. Refunded below if the
    # provider never accepted the job, and by the engine if it accepted
    # and then failed.
    if not db.video_try_consume(owner, period, limit):
        return jsonify({
            "error": "You've used all %d video generations for this %s." % (
                limit, features.video_period_for(plan)),
            "quota": _video_quota_view(owner, plan, signed_in),
        }), 402

    job = videogen.submit(owner, spec, backend_name, turn=_video_turn,
                          extra={"period": period})
    quota = _video_quota_view(owner, plan, signed_in)
    if job["status"] == "failed":
        # Refused before it was ever accepted. The engine has already
        # given the count back (on_failed runs for every failure), so
        # this only reports it.
        return jsonify({"error": job.get("error") or "The provider refused it.",
                        "job": videogen.public_job(job),
                        "quota": _video_quota_view(owner, plan, signed_in)}), 502
    return jsonify({"job": videogen.public_job(job), "quota": quota})


@app.route("/api/video/jobs", methods=["GET"])
def video_jobs():
    """This owner's history, newest first - the dashboard's grid."""
    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    owner = current_owner_id()
    jobs = [videogen.public_job(j) for j in db.video_jobs_for(owner, limit=40)]
    return jsonify({"jobs": jobs,
                    "quota": _video_quota_view(owner, plan, bool(session.get("user_id")))})


def _own_video_job(job_id):
    job = db.video_job_get(job_id)
    if not job or job["owner_id"] != current_owner_id():
        return None
    return job


@app.route("/api/video/job/<job_id>", methods=["GET"])
def video_job(job_id):
    job = _own_video_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    return jsonify({"job": videogen.public_job(job),
                    "quota": _video_quota_view(job["owner_id"], plan,
                                               bool(session.get("user_id")))})


@app.route("/api/video/job/<job_id>", methods=["DELETE"])
def video_job_delete(job_id):
    """Cancel if it is still going, then forget it and its file."""
    job = _own_video_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    if job["status"] in ("queued", "running"):
        confirmed = videogen.cancel(job)
        # A provider that dropped it before starting does not bill for
        # it; a queued-here job never reached one. Either way the count
        # comes back. A render already in progress is spent.
        if confirmed:
            period = (job.get("params") or {}).get("period")
            if period:
                db.video_refund(job["owner_id"], period)
    gone = db.video_job_delete(job_id, job["owner_id"])
    if gone:
        videogen.remove_file(gone)
    return jsonify({"ok": True})


@app.route("/api/video/job/<job_id>/download", methods=["GET"])
def video_job_download(job_id):
    """The finished file as an attachment, named after the job."""
    job = _own_video_job(job_id)
    if not job or job["status"] != "done" or not job.get("local_path"):
        return jsonify({"error": "Nothing to download yet."}), 404
    rel = job["local_path"].lstrip("/")
    directory, name = os.path.split(os.path.join(app.root_path, rel))
    return send_from_directory(
        directory, name, as_attachment=True,
        download_name="pip-%s%s" % (job_id, os.path.splitext(name)[1]))


# ======================================================================
# 3D MODELS (model3d.py): a picture or a description in, a textured GLB
# out, made by TRELLIS.2. Free to run (the Hugging Face Space), so the
# limit is per day and exists to share the Space's GPU quota fairly.
# ======================================================================
LIMIT_3D = Limiter(rate=6, per=60, burst=3)


def _model3d_refund(job):
    """A job that failed gives its day's count back."""
    if job and job.get("quota_key"):
        db.video_refund(job["owner_id"], job["quota_key"])


model3d.start(on_failed=_model3d_refund)


def _model3d_quota(owner, plan):
    key = "3d:" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    limit = int(features.get(plan, "models_3d_per_day") or 0)
    used = db.video_used(owner, key)
    return key, limit, used


def _model3d_start(owner, plan, signed_in, source, prompt="", image="", resolution="512"):
    """Every way in (the studio, the chat) starts a job here.
    -> (job, None) or (None, (sentence, status, extra))."""
    if not model3d.available():
        return None, ("3D models aren't set up on this server yet.", 503, {})
    if not signed_in:
        return None, ("Make a free account to create 3D models - it's how the "
                      "daily limit is kept fair.", 401, {"needs_account": True})
    if source == "text":
        blocked = moderation.check_image_prompt(prompt)
        if blocked is not None:
            return None, (blocked, 400, {})
    key, limit, used = _model3d_quota(owner, plan)
    if not db.video_try_consume(owner, key, limit):
        return None, ("You've made all %d of today's 3D models. More tomorrow%s."
                      % (limit, "" if plan == features.PRO else ", or more with Pro"),
                      402, {"upgrade_required": plan != features.PRO})
    job = model3d.submit(owner, source, prompt=prompt, image=image,
                         resolution=resolution, quota_key=key)
    return job, None


def _model3d_view(owner, plan):
    key, limit, used = _model3d_quota(owner, plan)
    return {"limit": limit, "used": min(used, limit), "left": max(0, limit - used)}


def _owned_model_job(job_id):
    job = model3d.get(job_id)
    if not job or job["owner_id"] != current_owner_id():
        return None
    return job


@app.route("/api/3d/status", methods=["GET"])
def model3d_status():
    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    return jsonify({
        "available": model3d.available(),
        "providers": [p.name for p in model3d.chain()],
        "resolutions": list(model3d.RESOLUTIONS),
        "signed_in": bool(session.get("user_id")),
        "quota": _model3d_view(current_owner_id(), plan),
        "price_usd": {r: model3d.price(r) for r in model3d.RESOLUTIONS},
    })


@app.route("/api/3d/generate", methods=["POST"])
@limited("3d", LIMIT_3D, key=_limit_key_account)
def model3d_generate():
    """A picture (multipart `image`) or a description (`prompt`), and a
    `resolution` of 512, 1024 or 1536. -> {"job": ...}; poll
    /api/3d/job/<id> until it is done or failed."""
    if request.files:
        f = request.files.get("image")
        prompt = ""
        resolution = request.form.get("resolution") or model3d.DEFAULT_RESOLUTION
    else:
        payload = request.get_json(silent=True) or {}
        f = None
        prompt = str(payload.get("prompt") or "").strip()
        resolution = str(payload.get("resolution") or model3d.DEFAULT_RESOLUTION)
    if resolution not in model3d.RESOLUTIONS:
        return jsonify({"error": "Pick a resolution of 512, 1024 or 1536."}), 400
    image = ""
    if f is not None:
        data = f.read(model3d.MAX_IMAGE_BYTES + 1)
        try:
            ext, _w, _h = model3d.check_image(data)
        except model3d.Model3DError as e:
            return jsonify({"error": str(e)}), 400
        image = model3d.save_input(data, ext)
        source = "image"
    elif prompt:
        if len(prompt) > 600:
            return jsonify({"error": "Keep the description under 600 characters."}), 400
        source = "text"
    else:
        return jsonify({"error": "Add a picture, or describe the object."}), 400
    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    owner = current_owner_id()
    job, err = _model3d_start(owner, plan, bool(session.get("user_id")), source,
                              prompt=prompt, image=image, resolution=resolution)
    if err:
        text, status, extra = err
        return jsonify(dict(extra, error=text, quota=_model3d_view(owner, plan))), status
    return jsonify({"job": model3d.public(job), "quota": _model3d_view(owner, plan)})


@app.route("/api/3d/jobs", methods=["GET"])
def model3d_jobs():
    return jsonify({"jobs": [model3d.public(j) for j in model3d.jobs_for(current_owner_id())]})


@app.route("/api/3d/job/<job_id>", methods=["GET"])
def model3d_job(job_id):
    job = _owned_model_job(job_id)
    if not job:
        return jsonify({"error": "No such model."}), 404
    return jsonify({"job": model3d.public(job)})


@app.route("/api/3d/job/<job_id>", methods=["DELETE"])
def model3d_job_delete(job_id):
    job = _owned_model_job(job_id)
    if not job:
        return jsonify({"error": "No such model."}), 404
    if job["status"] in ("queued", "running"):
        return jsonify({"error": "Wait for it to finish first."}), 409
    path = model3d.glb_path(job)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    db.model_job_delete(job_id)
    return jsonify({"ok": True})


@app.route("/api/3d/job/<job_id>/model.glb", methods=["GET"])
def model3d_glb(job_id):
    job = _owned_model_job(job_id)
    path = model3d.glb_path(job)
    if not path:
        return jsonify({"error": "No such model."}), 404
    resp = send_file(os.path.abspath(path), mimetype="model/gltf-binary",
                     as_attachment=request.args.get("download") == "1",
                     download_name="randomgenerals-3d-%s.glb" % job_id, max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/api/video/webhook/<token>", methods=["POST"])
def video_webhook(token):
    """A provider saying a job ended. Unsigned (Higgsfield documents no
    signature), so it is treated as a hint: the engine polls the job
    now and believes the provider's status endpoint, not this body.
    Always 200 - a 4xx tells the provider to stop retrying, and a 5xx
    makes it retry for two hours, neither of which is wanted here."""
    if not hmac.compare_digest(token, _video_webhook_token()):
        return jsonify({"ok": False}), 200
    payload = request.get_json(force=True, silent=True) or {}
    ref = str(payload.get("request_id") or "")
    known = videogen.nudge(ref) if ref else False
    return jsonify({"ok": True, "known": known})


# ----------------------------------------------------------------------
@app.route("/api/run-code", methods=["POST"])
@limited("run", LIMIT_RUN, key=_limit_key_account)
def run_code_route():
    if not session.get("user_id"):
        return jsonify({"error": "Sign in to run code."}), 401

    payload = request.get_json(force=True, silent=True) or {}
    code = payload.get("code") or ""
    if len(code) > 20000:
        return jsonify({"error": "That's too long to run here."}), 400

    # A sandbox run is a slice of the same two cores that serve the
    # site, and it was free: the one generation route that checked
    # nothing. Charged up front at the floor; given back if the sandbox
    # itself failed to start, because the person's code never ran.
    ok, credits = charge_up_front(CREDIT_COST_RUN)
    if not ok:
        return jsonify({
            "error": "Out of credits. They'll refill automatically.",
            "credits": credits_view(credits),
        }), 402

    result: dict[str, Any] = codeexec.run_python(code)
    if "error" in result and result["error"]:
        refund_charge(CREDIT_COST_RUN)
        return jsonify(result), 502
    result["timeout"] = codeexec.TIMEOUT_SECONDS
    return jsonify(result)


def _canned_reply(thread: dict, text: str, kind: str = "refusal") -> Response:
    """Appends `text` as an assistant message and returns it through the
    same streaming Response shape /api/chat normally returns, just
    yielded in one piece instead of generated token by token - so the
    frontend's existing stream-reading code handles it with no special
    casing, and it persists/reloads like any other reply. Used for
    content-filter blocks and "your memory is full" notices, neither of
    which touches the model.

    `kind` travels first as a stream event, so the browser knows before
    the first character that this is not an answer - see stream_event.
    """
    with DATA_LOCK:
        thread["messages"].append(_assistant_message(
            text, "filter", "content-filter", kind=kind))
        thread["updated"] = now_iso()
    save_thread(thread)

    def generate():
        yield stream_event({"event": "reply", "kind": kind})
        yield text

    return streaming_response(generate())


# How many times the model may reach for a tool before it has to
# answer. Three covers search-then-refine-then-answer, which is the
# deepest useful pattern here; beyond that a model is usually looping
# on a question the tools cannot settle, and every round is a whole
# generation on a GPU shared by everyone on the site.
MAX_TOOL_ROUNDS = 3

# What one round of the loop costs the person, on top of the reply. Each
# round is a whole non-streamed model turn against the shared budget; a
# chat that consulted the web twice did more work than one that did not,
# and used to cost the same.
CREDIT_TOOL_ROUND = 5

# The loop's own turn, when the caller does not say otherwise: a short
# ceiling because these turns are usually a tool call and a sentence,
# and a low temperature because a tool call is a name and a JSON blob
# and neither benefits from creativity. _stream_reply passes the
# request's real options instead, so a first turn that turns out to be
# the whole answer was generated the way the reader asked for.
TOOL_TURN_OPTIONS: dict[str, Any] = {"num_predict": 900, "temperature": 0.2}


class ToolLoopResult:
    """What the loop hands back. A class rather than a tuple because it
    grew a fifth field, and positional unpacking of five things is where
    bugs live."""

    def __init__(self, convo: list[dict]) -> None:
        self.convo: list[dict] = convo         # history + tool exchange
        self.notes: list[str] = []             # tool names, in call order
        self.displays: list[dict] = []         # what the browser was shown
        self.final_text: str | None = None     # a complete no-tool answer
        self.rounds: int = 0                   # model turns spent
        self.extra_cost: int = 0               # image generations etc.

    @property
    def cost(self) -> int:
        return self.rounds * CREDIT_TOOL_ROUND + self.extra_cost


def _run_tool_loop(model: str, history: list[dict], specs: list[dict],
                   provider: str = "pip", connector_map: dict | None = None,
                   options: dict | None = None):
    """Let the model call tools until it is ready to answer.

    A generator: it YIELDS stream events as each tool starts and
    finishes - so the browser shows "Searching the web…" while the
    search is actually running, and the sources the moment it is done -
    and RETURNS a ToolLoopResult, which the caller picks up with
    `result = yield from _run_tool_loop(...)`.

    Never raises: a tool that fails returns its error as the tool
    result, so the model can say what went wrong instead of the whole
    reply dying on a failed search.

    THE FIRST TURN IS NOT THROWN AWAY. When the model decides it needs
    no tool and answers outright, that answer used to be discarded and
    the same question streamed again - two generations on a GPU
    shared by everyone on the site, for one reply. If
    the answer is complete (finish_reason "stop") it is returned as
    `final_text` and the caller hands it straight to the reader. If it
    hit the loop's own ceiling it is discarded as before, because a
    cut-off answer regenerated at full length is the right outcome.
    """
    result = ToolLoopResult(convo=list(history))
    for _ in range(MAX_TOOL_ROUNDS):
        turn = PROVIDER_TURNS.get(provider, pip_api.chat_once)
        try:
            msg, err = turn(model, result.convo, tools=specs,
                            options=dict(options or TOOL_TURN_OPTIONS))
        except pip_api.ProviderUnavailable:
            # The engine can fill up between has_room() and this call.
            # chat_once raises rather than returning an error, so without
            # this the whole request dies on a refusal that the streaming
            # path handles
            # gracefully two functions later. Tools are an enhancement;
            # losing them costs a less-checked answer, not the answer.
            result.convo = list(history)
            return result
        if err or not msg:
            # The loop is an enhancement, not a requirement - fall
            # through to a normal answer rather than failing.
            result.convo = list(history)
            return result
        calls = msg.get("tool_calls") or []
        if not calls:
            text = (msg.get("content") or "").strip()
            if text and msg.get("finish_reason") == "stop":
                result.final_text = text
            return result

        # Counted only when the turn actually called something: a first
        # turn that simply answers is the reply, and is charged as one.
        result.rounds += 1
        turn_msg = {
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        }
        # pip 2 replays its own blocks next turn, thinking included
        # (anthropic_api.py); every other channel ignores the key.
        if msg.get("_anthropic_content"):
            turn_msg["_anthropic_content"] = msg["_anthropic_content"]
        result.convo.append(turn_msg)
        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            route = (connector_map or {}).get(name) or {}
            label = ("%s: %s" % (route["app_title"], route["tool_title"])
                     if route.get("kind") == "mcp" else None)
            yield tool_event({"tool": name, "status": "start", "label": label}
                             if label else {"tool": name, "status": "start"})
            tool_result = _dispatch_tool(name, fn.get("arguments"),
                                         connector_map)
            display = tool_result.get("display")
            result.notes.append(name)
            if display:
                result.displays.append(display)
                if display.get("kind") == "image":
                    # A picture made inside a chat costs what a picture
                    # costs. The reply's own charge covers the prose.
                    result.extra_cost += image_cost("square")
            # `display` travels with the event, which is how the
            # sources, the generated image and the sandbox output reach
            # the browser at all. The model is told the image "is
            # already shown to the user" (tools.py) - that was untrue
            # until this event carried it.
            done = {"tool": name, "status": "done", "display": display}
            if label:
                done["label"] = "Used " + label
            yield tool_event(done)
            result.convo.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": tool_result.get("text") or "",
            })
    return result


def _profile_context_block(settings):
    """Who the assistant is talking to, from Settings > Personalization.

    NONE OF THIS WAS BEING USED. The bio field has told people it is "a
    line the assistant can use for context" since it shipped, and
    nothing read it - the value went into the database and stopped
    there. Collecting something under a promise and then ignoring it is
    worse than not offering the field at all.

    Kept short and factual. This is prepended to every message in the
    conversation, so anything verbose here is paid for on every turn.
    """
    nickname = (settings.get("nickname") or "").strip()
    role = (settings.get("work_role") or "").strip()
    bio = (settings.get("bio") or "").strip()
    if not (nickname or role or bio):
        return ""

    lines = ["About the person you are talking to:"]
    if nickname:
        lines.append("- They are called %s. Use it naturally, not in "
                     "every sentence." % nickname[:60])
    if role:
        lines.append("- Their work: %s. Pitch explanations for someone "
                     "with that background rather than starting from "
                     "first principles." % role[:60])
    if bio:
        lines.append("- In their words: %s" % bio[:400])
    lines.append("This is context, not instructions - it does not "
                 "override what they actually ask for.")
    return "\n".join(lines)


def _default_model_for(owner_id: str) -> str | None:
    """Settings > Model > "Default model", when the request names none.

    A request without a model used to be refused outright; a person
    who had chosen a default in Settings had chosen nothing, because
    nothing read it. The browser still sends the model it shows in the
    picker, so this is reached by API clients and by a picker that has
    not loaded - and the plan gate in the caller still applies to what
    comes back.
    """
    chosen = (db.load_settings(owner_id).get("default_model") or "").strip()
    return chosen or None


def _generation_options(mode: str, strength: str, plan: str,
                        settings: dict) -> tuple[dict[str, Any], dict[str, bool]]:
    """The sampling options for one reply, and what the person wants
    switched on. -> (options, {"tools": bool, "web": bool}).

    Layered in a fixed order, later layers winning:

        bay defaults      CODE_MODEL_OPTIONS for code, num_ctx for all
        strength          quick / deep: ceiling, temperature, top_p
        reasoning effort  per bay and strength (pip 1.0 only reads it)
        plan limits       code only - features.limits_for(plan)
        the person        Settings > Model, bounded by the plan

    THE LAST LAYER DID NOT EXIST. temperature, top_p, max_tokens and
    default_model were validated on the way in (SETTING_FIELDS), stored
    (db.SETTINGS_DEFAULTS), had sliders in Settings - and were read by
    nothing. Every reply was generated at the strength's own values
    however the sliders were set. Same class of bug as system_prompt
    before it: a control that changes a stored number and nothing else.

    Bounded, not obeyed: max_tokens cannot exceed what the plan allows
    for the bay, because the plan limit is the plan limit. A person
    asking for less than the ceiling gets less; asking for more gets
    the ceiling.

    num_ctx: 8192 keeps a local model inside this GPU's VRAM - without
    it Ollama defaults to the model's advertised maximum (131,072 for
    llama3.2, an ~18GB KV cache) and silently spills most of the model
    to CPU, which was behind both the slow cold loads and sluggish
    generation. Code gets the plan's larger window from limits_for,
    because a long file plus its error output does not fit in 8192 and
    what gets dropped is the top of the file - the imports and the
    definitions being asked about.
    """
    options: dict[str, Any] = dict(CODE_MODEL_OPTIONS) if mode == "code" else {}
    options["num_ctx"] = 8192
    options.update(STRENGTH_LEVELS[strength]["options"])
    # Only pip 1.0 reads this: "high" lets it think before it answers,
    # which Deep asks for and nothing else does - on one GPU thinking is
    # paid for in everybody's waiting time.
    options["reasoning_effort"] = pip_api.effort_for(mode, strength)
    limits = features.limits_for(plan)
    if mode != "code":
        # THE PLAN'S ANSWER CEILING APPLIES IN CHAT TOO. It used to reach
        # the code bay only, so the Pro card's "longer answers" was true
        # in one bay and not the other: chat stopped at the strength's
        # 2,600 tokens whatever the plan. (Past the ceiling a reply still
        # continues automatically - see _auto_continue.)
        options["num_predict"] = limits["num_predict"]
    if mode == "code":
        # Quick's and Deep's ceilings are both prose-sized - real code
        # needs a much bigger generation budget, which is what was
        # behind "it doesn't know how to write 1000 lines of code": it
        # wasn't incapable, Quick mode was cutting it off at roughly a
        # page. The per-tier numbers live in features.py so this isn't
        # a second place where tier rules can drift.
        options.update(limits)
    ceiling = int(options.get("num_predict") or limits["num_predict"])

    settings = settings or {}
    if settings.get("temperature") is not None:
        options["temperature"] = max(0.0, min(2.0, float(settings["temperature"])))
    if settings.get("top_p") is not None:
        options["top_p"] = max(0.0, min(1.0, float(settings["top_p"])))
    if settings.get("max_tokens") is not None:
        # At least 64: a ceiling below a sentence is a way to get
        # nothing but truncation trailers, and nobody asks for that
        # on purpose.
        options["num_predict"] = max(64, min(ceiling, int(settings["max_tokens"])))

    wants = {
        "tools": bool(settings.get("tools_enabled", True)),
        "web": bool(settings.get("web_search", True)),
    }
    return options, wants


# ---------------------------------------------------------------------
# WEBSITE MODE
#
# Building websites is what most people come here for, and the hosted
# models cannot be fine-tuned - so the "training" is this playbook, sent
# with every website request, plus real photos fetched for the topic.
# Measured against three briefs on the live site before it existed:
# every image a placeholder URL, no mobile menu on any page, and one of
# the three scrolling sideways on a phone.
# ---------------------------------------------------------------------
_WEBSITE_HINT = re.compile(
    r"\b(web ?sites?|web ?pages?|landing ?pages?|home ?pages?|one[- ]pager|"
    r"portfolio (?:site|page)|html (?:page|site|file)|(?:site|page) for|"
    r"web ?app|web ?shop|store ?front|e-?commerce|"
    r"online (?:store|shop|boutique|menu|portfolio)|"
    # "build a modern, minimalist ... store/shop/site/blog" - the thing
    # being built is named a few words after the verb.
    r"(?:build|make|create|design|code|develop)\s+(?:me\s+)?(?:a|an|the|my|our)\s+"
    r"(?:[\w,'&-]+\s+){0,5}(?:site|store|shop|portfolio|blog|dashboard))\b",
    re.IGNORECASE)

# A follow-up that asks for pictures, in a thread that is already a site.
_WANTS_PHOTOS = re.compile(
    r"\b(photos?|pictures?|images?|imagery|gallery|hero shot)\b", re.IGNORECASE)

WEBSITE_PROMPT = """WEBSITE MODE. You are a senior web designer and front-end engineer. The person wants a website: build one that looks like it came from a top design studio, not a tutorial.

Deliver:
- One complete, self-contained HTML file in a single ```html block: <!doctype html>, inline <style> and <script>, no build step, no frameworks. Google Fonts via <link> is fine.
- One or two sentences before the code; after it, 2-4 short bullets on what to customise. Nothing else.
- Build everything that was asked for, completely - every section, feature and interaction. Length is not a problem: if you run out of room you will be asked to continue, so never cut a feature to save space. Close every tag.
- Asked for a change, return the whole updated file, never a fragment.

Structure:
- <header> with the name and a <nav>. Always include a menu button that shows only below 768px wide (aria-expanded, aria-label="Menu", toggled by a few lines of JS) and hides the links behind it on phones.
- A hero with the page's only <h1>: a specific headline, a one-line subhead, a primary call-to-action button and a secondary link, with a photo or a rich gradient behind or beside it. Text over a photo gets a dark overlay so it stays readable.
- 4-6 sections that fit this business (services or menu with prices, about, gallery, testimonials, pricing, FAQ, opening hours, contact form), then a <footer>.

Design:
- Palette and type as :root custom properties: one bold accent, neutrals, a dark text colour. Text contrast at least 4.5:1.
- A distinctive Google Fonts pairing (display face for headings, clean sans for body), fluid sizes with clamp(), generous whitespace on an 8px rhythm, containers about 1200px wide.
- CSS grid and flexbox; cards with soft shadows and rounded corners; buttons with hover and :focus-visible states; 200ms transitions.
- Mobile-first and fully responsive: no horizontal scrolling at 360px wide, images width:100% with object-fit:cover and an aspect-ratio, grids that stack, long words that wrap.
- Polish: smooth scrolling, a sticky translucent header, reveal-on-scroll with IntersectionObserver that respects prefers-reduced-motion.

Content:
- Real, specific copy for this business: names, services or menu items with prices, hours, testimonials with first names. No lorem ipsum, no "Your text here". Invented details must be plausible; use example.com emails and 555 phone numbers.
- Semantic HTML, alt text on every image, a label on every form field, lang on <html>."""

# Diagrams in the conversation. The page draws a ```mermaid block in
# place (static/js/diagram.js); the model only has to be told that it
# can, and how to write Mermaid that parses. Same rules as the Diagram
# bay's own prompt, which were each learned from a render that broke.
_DIAGRAM_HINT = re.compile(
    r"\b(diagram|flow ?chart|sequence chart|mind ?map|org(?:anisation|anization)? chart|"
    r"architecture|state machine|er ?d|entity.relationship|schema|"
    r"visuali[sz]e|draw (?:me )?(?:a|the) (?:flow|process|system))\b",
    re.IGNORECASE)

CHAT_DIAGRAM_PROMPT = (
    "DIAGRAMS: when a diagram helps, draw it as a ```mermaid code block - "
    "the page renders it. Use flowchart for processes and architecture, "
    "sequenceDiagram for messages between parties, erDiagram for "
    "schemas, stateDiagram-v2 for states. Quote every label that "
    "contains a space, bracket, slash or punctuation; keep node ids "
    "short and alphanumeric; aim for 6-14 nodes; set no styles or "
    "colours. Add a sentence or two of explanation around it."
)

NO_PHOTOS_NOTE = ("No photos were found for this topic. Use rich CSS "
                  "gradients, shapes and inline SVG instead of pictures. "
                  "Never invent an image URL or use a placeholder service.")


def _is_website_request(text: str) -> bool:
    return bool(_WEBSITE_HINT.search(text or ""))


def _website_origin(thread) -> str | None:
    """The message that started the site in this thread, if one did -
    so a follow-up ("make the header blue") still gets the playbook, and
    a request for more photos searches for the site's subject rather
    than for the words "more photos"."""
    for m in thread.get("messages", []):
        if m.get("role") == "user" and _is_website_request(m.get("content")):
            return m.get("content")
    for m in thread.get("messages", []):
        if (m.get("role") == "assistant"
                and "<!doctype html" in (m.get("content") or "").lower()):
            return ""
    return None


def _photo_block(photos: list[dict]) -> str:
    lines = ["REAL PHOTOS FOR THIS SITE, fetched just now. They load, and "
             "their licences allow use on a website:"]
    for i, p in enumerate(photos, 1):
        size = ("%sx%s" % (p.get("width"), p.get("height"))
                if p.get("width") and p.get("height") else "")
        lines.append("%d. %s - %s %s" % (i, p["url"], p.get("title", ""), size))
    lines.append(
        "Use these exact URLs for the hero, the gallery and section images "
        "(<img src> with descriptive alt text, or a CSS background-image). "
        "Choose the ones that fit; the first is usually the strongest hero. "
        "A photo whose title names a different business is fine in a "
        "gallery but never as the hero. "
        "Never invent other image URLs and never use placeholder services. "
        "Put this credit line in the footer in small text: "
        + imagesearch.credit_line(photos))
    return "\n".join(lines)


def _website_block(thread, asked: str) -> str:
    """The playbook, plus photos when a site is being started or asks
    for pictures. Empty when this is not about a website."""
    new = _is_website_request(asked)
    origin = asked if new else _website_origin(thread)
    if origin is None:
        return ""
    parts = [WEBSITE_PROMPT]
    if new or _WANTS_PHOTOS.search(asked or ""):
        photos = imagesearch.photos_for(origin or asked, 8)
        parts.append(_photo_block(photos) if photos else NO_PHOTOS_NOTE)
    return "\n\n".join(parts)



# What a message that wants a tool tends to say: something current or
# looked up (web_search), something to compute or run (run_python), or a
# picture made (generate_image). A miss costs little - the model answers
# from what it knows, and Deep mode always offers the tools - while a
# false hit only costs the old, slower path. See the tool turn in
# _stream_reply for why skipping it matters.
_TOOL_HINT = re.compile(
    r"\b(search|look(?:ing)? up|google|latest|news|today|tonight|"
    r"yesterday|this (?:week|month|year)|right now|currently|current|"
    r"price|prices|stock|weather|forecast|score|who won|release[ds]?|"
    r"20[2-9]\d|https?://|www\.|"
    r"run (?:this|it|the code)|execute|calculate|compute|evaluate|"
    r"simulate|plot|"
    r"draw|paint|sketch|"
    r"(?:find|fetch|get|grab|search for|show me|look for)(?: me)?"
    r"(?: some| a few| a)? (?:pictures?|photos?|images?)|"
    r"(?:pictures?|photos?|images?) of|"
    r"(?:generate|make|create) (?:me )?(?:an? )?(?:image|picture|photo|"
    r"illustration|drawing))\b",
    re.IGNORECASE)


def _owner_checked(question: str, pieces):
    """`pieces`, with the reply to an owner question checked before any
    of it is sent (pip_identity.enforce).

    The engine does this itself for pip 1.0; this is the same guarantee
    for a reply that came from a backup channel. An owner question's
    reply is held back whole - it is a sentence or two, and a sentence
    cannot be unsent - and everything else streams straight through.
    A channel that refuses still raises before anything is yielded, so
    the failover contract in providers.py is untouched.
    """
    if not pip_identity.is_question(question):
        yield from pieces
        return
    yield pip_identity.enforce(question, "".join(pieces))


# LONG ANSWERS DO NOT STOP AT ONE REPLY'S CEILING.
#
# Every model call has a ceiling - 3,500 tokens of code on the free plan,
# and Groq's per-minute budget caps any single request near 8,000 tokens
# prompt included. A storefront with a cart and a modal is longer than
# that, and the reply used to stop mid-file with "Say continue". People
# asked for it to have no limit.
#
# A reply has no ceiling now: when the model stops because it ran out of
# room, the server asks it to carry on from exactly where it stopped and
# streams the rest into the same reply, part after part, waiting out a
# rate limit when it meets one. Each part sees the original request and
# the END of what was written (not all of it - that would outgrow the
# budget by the third part), which is all it needs to continue. The note
# asking the person to say "continue" only appears if a reply runs past
# MAX_CONTINUATIONS parts, which is tens of thousands of tokens.
MAX_CONTINUATIONS = 12
CONTINUE_TAIL_CHARS = 9000
CONTINUE_PROMPT = (
    "You ran out of room part-way through your reply. Continue EXACTLY "
    "where you stopped - mid-line, mid-word if that is where it ended. "
    "Do not repeat anything, do not summarise, do not apologise, and do "
    "not add an introduction. If you were inside a code block, keep "
    "writing the code: do not open a new ``` fence. Finish the reply "
    "completely.")


def _seam(so_far: str, first: str) -> str:
    """Tidy the join between two parts. A model told not to reopen the
    code fence sometimes does anyway; inside an open fence that line
    would end the code block and print the rest as prose."""
    if so_far.count("```") % 2 == 1:
        first = re.sub(r"^\s*```[\w+-]*[ \t]*\n", "", first, count=1)
    return first


def _auto_continue(provider, model, history, so_far, usage, kwargs):
    """Stream the rest of a reply that hit its ceiling. Generator;
    returns the whole reply."""
    system = [m for m in history[:1] if m.get("role") == "system"]
    asked = next((m for m in reversed(history) if m.get("role") == "user"), None)
    kwargs = {k: v for k, v in kwargs.items() if k != "images"}
    total_tokens = usage.get("eval_count") or 0
    part = 1
    while usage.get("finish_reason") == "length" and part <= MAX_CONTINUATIONS:
        part += 1
        usage["finish_reason"] = None
        tail = so_far[-CONTINUE_TAIL_CHARS:]
        messages = system + ([asked] if asked else []) + [
            {"role": "assistant",
             "content": ("[...the earlier part of your reply is above...]\n"
                         if len(so_far) > CONTINUE_TAIL_CHARS else "") + tail},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]
        row = "continue-%d" % part
        if (provider == "openrouter" and not openrouter_api.has_credit()) or (
                provider == "google" and not google_api.ready()):
            # DeepSeek ran out of credit part-way through a long reply:
            # Groq writes the rest rather than the reply waiting on a
            # balance nobody is about to top up.
            fast = _groq_model_for("code")
            if fast:
                provider, model = "groq", fast
        yield stream_event({"tool": row, "status": "start",
                            "label": "Writing part %d of the reply" % part})
        attempts = 0
        while True:
            head = ""
            started = False
            try:
                for piece in PROVIDER_STREAMERS[provider](model, messages, **kwargs):
                    if not started:
                        head += piece
                        if len(head) < 40:
                            continue
                        piece = _seam(so_far, head)
                        started = True
                    so_far += piece
                    yield piece
                if not started and head:
                    piece = _seam(so_far, head)
                    so_far += piece
                    yield piece
                break
            except providers.ProviderUnavailable as e:
                attempts += 1
                if attempts > 5:
                    note = ("\n\n_…the model stayed busy, so the reply stops "
                            "here. Say **continue** and I'll pick up where "
                            "I left off._")
                    so_far += note
                    yield note
                    usage["eval_count"] = total_tokens
                    yield stream_event({"tool": row, "status": "done",
                                        "label": "Stopped at part %d - the model was busy" % part})
                    return so_far
                wait = None
                if provider == "groq":
                    wait = groq_api.budget_state()[1]
                if wait is None and isinstance(e, providers.RateLimited):
                    try:
                        wait = float(str(e))
                    except ValueError:
                        wait = None
                wait = int(min(60, max(3, (wait or 10) + 1)))
                for remaining in range(wait, 0, -1):
                    yield stream_event({"tool": row, "status": "start",
                                        "label": "Writing part %d - waiting %ds for the model"
                                        % (part, remaining)})
                    time.sleep(1)
        total_tokens += usage.get("eval_count") or 0
        yield stream_event({"tool": row, "status": "done",
                            "label": "Wrote part %d" % part})
    usage["eval_count"] = total_tokens
    if usage.get("finish_reason") == "length":
        note = ("\n\n_…this reply is very long. Say **continue** and I'll "
                "pick up where I left off._")
        so_far += note
        yield note
    return so_far


# "MAKE THIS INTO A 3D MODEL", said in the chat. Detected here rather
# than left to the model: a message with a picture skips the tool loop
# (see _stream_reply), and that is exactly the message that wants this.
_3D_TARGET = re.compile(r"\b(3d|3-d|three[- ]?d|glb|gltf|mesh)\b", re.I)
_3D_VERB = re.compile(r"\b(make|create|generate|build|turn|convert|transform|give|"
                      r"produce|sculpt|model|want|need|render|into)\b", re.I)
_3D_QUESTION = re.compile(r"^\s*(what|why|how|when|who|where|explain|is|are|does|do|"
                          r"should|which)\b", re.I)
_3D_VAGUE = {"", "this", "it", "that", "these", "them", "this image", "this picture",
             "this photo", "the image", "the picture", "the photo", "my image"}


def _wants_3d(text):
    t = text or ""
    return bool(_3D_TARGET.search(t) and _3D_VERB.search(t) and not _3D_QUESTION.search(t))


def _model3d_resolution(text):
    t = (text or "").lower()
    if "1536" in t or "highest" in t or "max quality" in t:
        return "1536"
    if "1024" in t or "high quality" in t or "detailed" in t:
        return "1024"
    return "512"


def _model3d_picture(thread, files):
    """The picture to model: one attached now, else the latest one in the
    last few messages ("make that 3D"). -> path under static/ or ""."""
    def from_list(items):
        for f in items or []:
            url = str(f.get("url") or "")
            if f.get("kind") == "image" and url.startswith("/static/uploads/"):
                path = url.lstrip("/")
                if "/.." not in path and os.path.exists(path):
                    return path
        return ""
    found = from_list(files)
    if found:
        return found
    users = [m for m in (thread.get("messages") or []) if m.get("role") == "user"]
    for m in reversed(users[-4:]):
        found = from_list(m.get("attachments"))
        if found:
            return found
    return ""


def _model3d_usable(path):
    """An upload as TRELLIS.2 input: PNG/JPEG/WebP kept as they are,
    anything else Pillow can read re-saved as PNG. -> path or ""."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(model3d.MAX_IMAGE_BYTES + 1)
        ext, _w, _h = model3d.check_image(data)
        return model3d.save_input(data, ext)
    except model3d.Model3DError:
        pass
    except OSError:
        return ""
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(path) as im:
            buf = BytesIO()
            im.convert("RGBA").save(buf, "PNG")
        data = buf.getvalue()
        model3d.check_image(data)
        return model3d.save_input(data, "png")
    except Exception:                                  # noqa: BLE001
        return ""


def _stream_reply(thread, provider, model, web_results, files, strength):
    """Builds the system prompt + history from thread["messages"] as it
    currently stands, streams a reply, and persists + charges for it once
    done. Shared by /api/chat (appends the new user turn first) and
    /regenerate (replays on the existing history minus the old reply) so
    the two can't quietly drift apart."""
    mode = thread.get("mode", DEFAULT_MODE)

    # AN ATTACHED IMAGE SHOULD PICK ITS OWN MODEL.
    #
    # Before this, the picture was simply dropped whenever the chosen
    # model could not see - silently, on the server - and the reply came
    # back as though nothing had been attached. Nobody was told why.
    # Where the chat bay's model is text-only, that was every image.
    #
    # So: if there are images and this model is blind, look for one that
    # is not, among providers that are actually up, and use it for this
    # message only. The thread's own model is unchanged.
    # Pro only. features.py has said FREE: vision False since the tiers
    # were written, and nothing read it - every image was routed to a
    # vision model for everyone. A free account's image is now
    # acknowledged by name and not looked at, with a nudge to upgrade,
    # which is what the tier table always described.
    # DEEP MODE ON THE CODE BAY IS R1. The strength toggle has always
    # changed how much the model is asked to think (STRENGTH_LEVELS);
    # on the DeepSeek channel it can also change WHICH model, because
    # R1 exists to do exactly that. Only when the bay is already on V3 -
    # a person who picked something else in the picker keeps it - and
    # only while the day's ceiling has room for the pricier model.
    if (mode == "code" and strength == "deep" and provider == "openrouter"
            and model.startswith("deepseek/deepseek-v3")
            and openrouter_api.DEEP_MODEL in (openrouter_api.models() or [])
            and openrouter_api.budget_ok(openrouter_api.DEEP_MODEL)):
        model = openrouter_api.DEEP_MODEL

    plan_now = features.normalize_plan(current_account()[0].get("plan"))
    may_see = features.enabled(plan_now, "vision")
    # The Free plan's daily image allowance. Over it, the images are
    # named and not looked at, and the reply says why (see
    # _attachment_context_block) rather than pretending to be blind.
    image_count = sum(1 for f in files if f["kind"] == "image")
    image_cap = features.get(plan_now, "vision_images_per_day")
    images_capped = False
    if may_see and image_count and image_cap is not None:
        if db.images_today(current_owner_id()) + image_count > image_cap:
            may_see, images_capped = False, True
    if (may_see and any(f["kind"] == "image" for f in files)
            and not is_vision_model(model)):
        seer_provider, seer_model = _vision_route()
        # Both, so the pair is either fully replaced or left alone - a
        # provider without a model is not a route.
        if seer_provider and seer_model:
            provider, model = seer_provider, seer_model

    wants_sight = may_see and any(f["kind"] == "image" for f in files)
    # DeepSeek answers every text message while it has credit (see
    # _deepseek_model_for). It cannot see, so a message with a picture
    # keeps the vision route below.
    # pip 2, for a Pro account that has it selected - the Pro default.
    # A Free account cannot reach here with it (model_allowed in the
    # route); past the day's spending ceiling the channel is not ready
    # and Groq answers, through the branches below.
    pip2 = (provider == "anthropic" and plan_now == features.PRO
            and anthropic_api.ready())
    deepseek = (None if (wants_sight or pip2)
                else _deepseek_model_for(mode, strength))
    # Gemma 4 31B answers when the person PICKED it in the model picker
    # (gpt-oss-120b on Groq stays the fast default). A picture keeps the
    # vision route below; Gemma cooling down after a rate limit falls
    # through to Groq.
    gemma = (_gemma_model() if provider == "google" and not deepseek
             and (not wants_sight or google_api.vision_models())
             else None)
    if pip2:
        provider, model = "anthropic", anthropic_api.model_id()
    elif deepseek:
        provider, model = "openrouter", deepseek
    elif gemma:
        provider, model = "google", gemma
    elif _groq_only() and (provider != "groq" or not (
            model in groq_api.models() or model in groq_api.vision_models())):
        seer = (_vision_route() if wants_sight else (None, None))
        if seer[0] == "groq":
            provider, model = seer
        else:
            provider = "groq"
            model = _groq_model_for(mode) or groq_api.PREFERRED[0]
    # Only asked when there is a picture: is_vision_model() reads the
    # providers' catalogues, and with vision on for Free that would
    # otherwise run on every message anyone sends.
    vision_available = bool(image_count and may_see and is_vision_model(model))
    images_b64 = [
        b64 for f in files if f["kind"] == "image" and vision_available
        for b64 in [attachments.encode_image_base64(f["url"])] if b64
    ]
    if images_b64:
        # Counted toward the daily allowance when they are sent, not
        # when attached: a picture no model looked at costs nothing.
        db.note_usage(current_owner_id(), 0, messages=0, images=len(images_b64))

    system_prompt = CODING_SYSTEM_PROMPT if mode == "code" else CHAT_SYSTEM_PROMPT
    # What was actually asked, before anything is attached to it - the
    # owner check reads the person's words, not search results.
    asked = pip_identity.last_user_text(thread.get("messages"))

    # THE TRIAGE MODEL, ON THE CHAT BAY ONLY (brain/intent.py).
    #
    # Most people never switch bays. A coding question asked in Chat got
    # the general prompt - which is the difference between an answer
    # with a runnable block in it and an answer ABOUT programming. The
    # model reads the message in about a fifth of a millisecond and says
    # which bay it belongs to; when it is confident the answer is code,
    # this message alone gets the coding prompt.
    #
    # Deliberately the smallest possible action. The thread's mode, its
    # model and what it costs are untouched, so a wrong guess costs a
    # differently-worded answer and nothing else - which is the only
    # kind of mistake a model measured at 95% on the calls it is
    # confident about should be allowed to make. Its web and depth
    # heads are read and ignored: +4 and +7 points over guessing is not
    # enough to change what the site does.
    if mode == "chat" and brain_intent is not None:
        asked = next((m.get("content") or "" for m in reversed(thread["messages"])
                      if m.get("role") == "user"), "")
        guess = brain_intent.predict(asked)
        if guess and guess["confident"] and guess["bay"] == "code":
            system_prompt = CODING_SYSTEM_PROMPT
    owner_id = current_owner_id()
    memories = db.load_memories(owner_id)
    custom_instructions = db.load_custom_instructions(owner_id)
    if memories or custom_instructions:
        system_prompt = system_prompt + "\n\n" + _memory_context_block(
            memories, custom_instructions) + "\n\n" + MEMORY_ACK_NUDGE
    owner_settings = db.load_settings(owner_id)
    profile_block = _profile_context_block(owner_settings)
    if profile_block:
        system_prompt = system_prompt + "\n\n" + profile_block
    # settings["system_prompt"] existed in the validator and in the
    # defaults and NOWHERE ELSE. Settings > Templates wrote to it, the
    # "Use" button said "Applied", and not one character of it ever
    # reached the model. The templates feature was decorative.
    own_prompt = (owner_settings.get("system_prompt") or "").strip()
    if own_prompt:
        system_prompt = system_prompt + "\n\n" + (
            "Standing instructions from the person you are talking "
            "to. Follow them unless they conflict with the rules "
            "above.\n\n" + own_prompt[:4000])
    if web_results:
        system_prompt = system_prompt + "\n\n" + \
            _web_context_block(web_results)
    page_block = _page_context_block(thread)
    if page_block:
        system_prompt = system_prompt + "\n\n" + page_block
    if files:
        system_prompt = system_prompt + "\n\n" + _attachment_context_block(
            files, vision_available,
            capped_at=image_cap if images_capped else None)
    site_job = _site_job_for(thread, asked, bool(files and any(
        f.get("kind") == "image" for f in files)), strength)
    if site_job is not None:
        site_job["model"] = provider      # a site is built by the model picked
    site_block = "" if site_job else _website_block(thread, asked)
    if site_block:
        system_prompt = system_prompt + "\n\n" + site_block
    elif _DIAGRAM_HINT.search(asked or ""):
        system_prompt = system_prompt + "\n\n" + CHAT_DIAGRAM_PROMPT
    nudge = STRENGTH_LEVELS[strength]["nudge"]
    if nudge:
        system_prompt = system_prompt + "\n\n" + nudge
    # SKILLS (extensions.py): expert instructions for the job this
    # message is - picked by /name or by what it asks for. A website
    # build has its own pipeline and takes none.
    skills_on = [] if site_job else _skills_for_turn(owner_id, asked)
    for skill in skills_on:
        system_prompt = system_prompt + "\n\n" + extensions.skill_block(skill)
    apps_note = _connected_apps_note(owner_id, asked)
    if apps_note:
        system_prompt = system_prompt + "\n\n" + apps_note

    history = ([{"role": "system", "content": system_prompt}]
               + _model_history(thread))

    # pip 1.0 DOES NOT HAVE TO BE RELIABLE ON ITS OWN
    #
    # The engine is one GPU. It answers a few requests at a time and
    # refuses the rest, and it is sometimes simply restarting. Two
    # things keep that from reaching a person.
    #
    # First, it is asked BEFORE the request: when the engine has said it
    # is busy, this one goes straight to a backup model - no wasted
    # round trip, no error.
    #
    # Second, a refusal that happens anyway is caught below and retried
    # elsewhere rather than surfaced. Between them the channel degrades
    # instead of failing.
    fell_back_to_cloud = False
    plan = features.normalize_plan(current_account()[0].get("plan"))
    history = _fit_to_plan_context(history, plan)

    # DeepSeek is first in the code bay but it is the only route here
    # that needs a paid key, so a request naming it on a server without
    # one - a stale tab, a saved preference, an older client - must not
    # become a dead reply. It degrades to pip 1.0, and to the local
    # model after that, the same way every other channel does.
    if provider == "openrouter" and not openrouter_api.budget_ok(model):
        # Out of money for today rather than out of key.
        #
        # The free ids on this channel are tried BEFORE leaving it. The
        # code bay's list carries free coding models (laguna,
        # north-mini-code), and stepping off OpenRouter entirely traded a
        # model built for code for a general one when a better answer was
        # one line further down the same list.
        free = _openrouter_model_for(mode)
        if free:
            model = free
        elif pip_api.configured() and pip_api.models():
            provider, model = "pip", pip_api.PREFERRED[0]
        else:
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local

    if provider == "openrouter" and not openrouter_api.configured():
        if pip_api.configured() and pip_api.models():
            provider = "pip"
            model = pip_api.PREFERRED[0]
        else:
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local

    if provider == "pip":
        if not pip_api.has_room():
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local
                fell_back_to_cloud = True

    if provider == "ollama" and not ollama_reachable():
        # Local is unavailable too. Try pip 1.0 rather than
        # giving up - the direction people usually need is the other way,
        # but a deployment with no local model at all is exactly the
        # hosted case, and refusing there would be gratuitous.
        if pip_api.configured() and pip_api.models():
            provider = "pip"
            model = next((m for _, p in BAY_ROUTES.get(mode, [])
                          for m in pip_api.models() if p in m.lower()),
                         pip_api.PREFERRED[0])
        else:
            def unavailable():
                yield stream_event({"event": "reply", "kind": "error"})
                yield ("[No model is answering. If this is running on your "
                       "own machine, start Ollama, or check OLLAMA_URL and "
                       "OLLAMA_API_KEY on the server.]")
            return streaming_response(unavailable())

    combined_options, wants = _generation_options(
        mode, strength, plan, owner_settings)
    stream_kwargs: dict[str, Any] = {"options": combined_options}
    if images_b64:
        stream_kwargs["images"] = images_b64
    usage = {"auto_continue": True}
    stream_kwargs["usage"] = usage

    # MODEL-DRIVEN TOOLS
    #
    # This was switched off when the cloud provider was removed, on the
    # grounds that a small local model which hallucinates a tool call
    # answers worse than one that just talks. That reasoning still holds
    # for the small models Ollama runs here - and does not apply to
    # pip 1.0 or the hosted backups, whose models return a well-formed
    # web_search call with a sensible query when asked to look
    # something up.
    #
    # So tools are offered on those channels only. The loop runs
    # inside generate() below, AFTER the response has started: a tool
    # call arrives as a name and a JSON blob, and half of either is
    # useless, so those turns are fetched whole and unseen - but the
    # headers go out first, so the browser sees a live "searching the
    # web…" instead of nothing at all for as long as the loop takes.
    tool_specs = None
    connector_map = {}
    # The loop spends an extra non-streamed turn before the answer, so on
    # pip 1.0 it is offered only while the engine has room for one.
    # Under pressure the reply still happens, just without tools.
    # A MESSAGE WITH A PICTURE SKIPS THE TOOL LOOP.
    #
    # The loop's first turn is chat_once, which carries text only, and
    # when that turn comes back as a complete answer it IS the reply -
    # the streamed path, the one that attaches the images, never runs.
    # So on every plan with tools, every attached image was answered by
    # a model that had not been shown it, whichever vision route had
    # been chosen with such care just above. A picture goes straight to
    # the model that can see it; the tools are back on the next turn.
    if (site_job is None and provider in PROVIDER_TURNS
            and features.FEATURES[plan]["builtin_tools"]
            and wants["tools"]
            and not images_b64
            and (provider != "pip"
                 or (pip_api.has_room() and pip_api.supports_tools()))):
        tool_specs = tools.available_specs(
            allow_images=(mode != "image"),
            allow_code=True,
            # Settings > Model > "Web search" was a stored boolean and
            # nothing else; it now decides whether the model is offered
            # the search tool at all.
            allow_web=wants["web"],
        )
        # Both plans have connected apps now; how MANY differs, and that
        # is enforced when one is added rather than here. This stays a
        # flag check so a tier could still switch them off entirely.
        if features.FEATURES[plan]["external_connectors"]:
            extra, connector_map = _connector_tools(
                current_owner_id(), _recent_user_text(thread))
            tool_specs = tool_specs + extra
        # NO TOOL TURN FOR A MESSAGE THAT NEEDS NO TOOL.
        #
        # The tool turn is fetched whole, so when the model answers
        # without calling anything - which is most messages - the entire
        # reply arrives in one block at the end. "Make a website" waited
        # 150 seconds for its first character on the free OpenRouter
        # model that way, then appeared all at once. Offered only when
        # the message reads like it wants a search, a calculation run or
        # a picture, in Deep mode, or when the person has connected
        # apps; everything else streams from the first token.
        if not (connector_map or strength == "deep"
                or (_TOOL_HINT.search(asked or "")
                    and not _is_website_request(asked))):
            tool_specs = None
            connector_map = {}
    # A 3D MODEL, ASKED FOR IN THE CHAT: start the job now, show its
    # card at the top of the reply, and have the model say what it is
    # making rather than pretend to make it.
    model3d_display = None
    if (site_job is None and mode in ("chat", "code") and _wants_3d(asked)
            and model3d.available()):
        picture = _model3d_picture(thread, files)
        subject = model3d.subject_of(asked or "")
        note = ""
        if picture or subject.strip(" .!?").lower() not in _3D_VAGUE:
            usable = _model3d_usable(picture) if picture else ""
            if picture and not usable:
                note = ("The user asked for a 3D model from their picture, but that "
                        "picture could not be read as an image. Say so briefly and "
                        "ask for a PNG, JPEG or WebP.")
            else:
                job, err = _model3d_start(
                    current_owner_id(), plan, bool(session.get("user_id")),
                    "image" if usable else "text", prompt=subject,
                    image=usable, resolution=_model3d_resolution(asked))
                if job:
                    model3d_display = {"kind": "model3d", "job_id": job["id"]}
                    note = ("A 3D model is being made right now with TRELLIS.2, from %s. "
                            "It appears in a card above your reply with a live 3D preview "
                            "and a GLB download (it usually takes one to three minutes). "
                            "Reply in one or two short sentences saying what you are "
                            "modelling. Do not say it is finished, do not paste links, "
                            "and do not write code or Blender scripts for it."
                            % ("the picture they attached" if usable
                               else "a picture of '%s' drawn first" % subject[:120]))
                else:
                    note = ("The user asked for a 3D model, but it could not be started: "
                            "%s Tell them this plainly in one or two sentences." % err[0])
        else:
            note = ("The user wants a 3D model but gave neither a picture nor a "
                    "description of the object. Ask them to attach a picture or say "
                    "what the object is, in one sentence.")
        tool_specs = None
        connector_map = {}
        if note and history and history[0].get("role") == "system":
            history = ([{"role": "system", "content": history[0]["content"] + "\n\n" + note}]
                       + history[1:])
    if tool_specs is not None:
        # Say the tools exist, in the same breath as offering them.
        # Promising a calculator to a session that has none is worse
        # than silence, so this rides with tool_specs rather than being
        # baked into the base prompt.
        if history and history[0].get("role") == "system":
            history = ([{"role": "system",
                         "content": history[0]["content"] + TOOL_USE_PROMPT}]
                       + history[1:])

    def kwargs_for(candidate: str) -> dict[str, Any]:
        """The stream arguments for whichever channel ends up answering.

        A CEILING THE LOCAL MODEL CAN ACTUALLY REACH. The code bay asks
        for up to 3,500 tokens, which is a fair request of a hosted 120B
        and a very long wait from a 1B on two shared cores. The fallback
        exists to get an answer out, not to attempt the same answer
        slowly, so it asks for a shorter one - which is also honest
        about what a model this size is good for. The prompt is trimmed
        too, inside stream_ollama, for every path into it.
        """
        if candidate != "ollama" or provider == "ollama":
            return stream_kwargs
        local_kwargs = dict(stream_kwargs)
        opts = dict(local_kwargs.get("options") or {})
        if opts.get("num_predict", 0) > LOCAL_FALLBACK_MAX_TOKENS:
            opts["num_predict"] = LOCAL_FALLBACK_MAX_TOKENS
            local_kwargs["options"] = opts
        return local_kwargs

    def generate():
        nonlocal provider, model, history, fell_back_to_cloud
        full_reply = ""
        # What the saved turn will be. Flips to "error" at every site
        # below that fabricates a sentence in place of an answer, and
        # the browser is told in the same breath so it can show it as
        # one. The finally block charges only when this is still "text".
        reply_kind = "text"
        tools_used = ToolLoopResult(convo=history)
        try:
            for skill in skills_on:
                yield stream_event({"tool": "skill-" + skill["id"], "status": "done",
                                    "label": "Using the %s skill" % skill["title"]})
            if model3d_display is not None:
                tools_used.displays.append(model3d_display)
                yield stream_event({"tool": "make_3d_model", "status": "done",
                                    "label": "Making a 3D model",
                                    "display": model3d_display})
            if site_job is not None:
                # A website: planned, written file by file and reviewed
                # by sitebuilder.py, then saved as a project with a card.
                full_reply, display = yield from _run_site_job(
                    site_job, thread, asked, usage)
                tools_used.displays.append(display)
                usage["eval_count"] = usage.get("tokens", 0)
                yield full_reply
                yield stream_event({"tool": "build-saved", "status": "done",
                                    "label": "Saved the project",
                                    "display": display})
                return
            if tool_specs is not None:
                # The loop's own turns use the reply's real options, not
                # a short generic ceiling: when the first turn IS the
                # answer it should be the answer the reader asked for.
                tools_used = yield from _run_tool_loop(
                    model, history, tool_specs, provider=provider,
                    connector_map=connector_map,
                    options=dict(stream_kwargs.get("options") or {}))
                history = tools_used.convo
                if tools_used.final_text is not None:
                    # The model answered in full without reaching for a
                    # tool. Handing that over beats generating it twice.
                    full_reply = pip_identity.enforce(
                        asked, tools_used.final_text)
                    yield full_reply
                    return

            # ASK DOWN THE CHAIN UNTIL SOMEBODY ANSWERS.
            #
            # Every channel raises ProviderUnavailable only before its
            # first chunk (providers.py), so a refusal here is always
            # safe to hand to the next candidate - nothing has reached
            # the browser and the reader never sees a seam. A failure
            # AFTER the first chunk is yielded by the channel as a
            # sentence at the end of the reply, because by then there
            # is nowhere to fail over to.
            candidates = [(provider, model)] + [
                c for c in _failover_chain(provider, mode)
                # A picture is not handed to a channel that would answer
                # it blind; better the honest "busy, try again in 20s"
                # below than a confident description of nothing.
                if not images_b64 or is_vision_model(c[1])]
            failures: list[tuple[str, BaseException]] = []
            limited: tuple[str, str] | None = None
            answered = False
            for i, (p, m) in enumerate(candidates):
                # What the router learns from: when the first token
                # arrived, when the last did, and whether it worked.
                t0 = time.monotonic()
                t_first = None
                try:
                    for piece in _owner_checked(asked, PROVIDER_STREAMERS[p](
                            m, history, **kwargs_for(p))):
                        if t_first is None and piece:
                            t_first = time.monotonic()
                        full_reply += piece
                        yield piece
                except pip_api.ProviderUnavailable as e:
                    failures.append((p, e))
                    agent_router.record(
                        p, m, ok=False,
                        reason=("rate_limited" if isinstance(e, providers.RateLimited)
                                else "unreachable"))
                    if limited is None and isinstance(e, providers.RateLimited):
                        limited = (p, m)
                    continue
                except Exception:
                    agent_router.record(p, m, ok=False, total=time.monotonic() - t0,
                                        reason="dropped")
                    raise
                agent_router.record(
                    p, m, ok=True,
                    ttft=round((t_first or time.monotonic()) - t0, 3),
                    total=round(time.monotonic() - t0, 3),
                    tokens=usage.get("eval_count"))
                answered = True
                if i > 0:
                    # Recorded on the message so a reload still shows
                    # which channel actually answered.
                    provider, model = p, m
                    fell_back_to_cloud = True
                break

            if not answered and limited is not None:
                # NOBODY COULD ANSWER, but one of them is only busy, and
                # said for how long: "try again in a moment" was asking
                # somebody to do by hand, without knowing the number,
                # the one thing the server could do for them.
                #
                # Waited in slices, and the browser is told at each one
                # how long is left, so the pause is a countdown in the
                # thinking indicator rather than a hang.
                p, m = limited
                wait = _retry_after(p, failures)
                if wait is not None and wait <= RATE_LIMIT_WAIT_SECONDS:
                    # +1 so the window has genuinely rolled over rather
                    # than landing on the boundary.
                    remaining = wait + 1
                    while remaining > 0:
                        yield stream_event({"event": "wait",
                                            "seconds": int(remaining + 0.999)})
                        step = min(RATE_LIMIT_WAIT_SLICE, remaining)
                        time.sleep(step)
                        remaining -= step
                    try:
                        for piece in PROVIDER_STREAMERS[p](
                                m, history, **kwargs_for(p)):
                            full_reply += piece
                            yield piece
                        answered = True
                        if (p, m) != candidates[0]:
                            provider, model = p, m
                            fell_back_to_cloud = True
                    except pip_api.ProviderUnavailable as e:
                        # Still busy after the window rolled - the site
                        # is under sustained load rather than briefly
                        # spiky. Fall through and say so.
                        failures.append((p, e))

            if not answered:
                reply_kind = "error"
                full_reply = _nobody_answered(failures)
                yield stream_event({"event": "reply", "kind": reply_kind})
                yield full_reply
                return

            # Ran out of room? Keep going - see _auto_continue.
            if usage.get("finish_reason") == "length":
                full_reply = yield from _auto_continue(
                    provider, model, history, full_reply, usage,
                    kwargs_for(provider))

            # AN EMPTY STREAM IS A FAILURE, NOT AN ANSWER.
            #
            # "anisse international school" came back as "Searched the
            # web" and then nothing: POST /api/chat logged 200 with 117
            # bytes, no exception, no traceback. The model had used its
            # tool call and then produced no prose, so the loop above
            # yielded zero pieces and the browser fell back to "[No
            # response received. Check the channel setup.]" - which
            # blames the setup for a model that simply said nothing.
            #
            # One retry, with an instruction rather than the same
            # request again. The streamer is never handed `tools` - the
            # tool loop runs before this and leaves its results in the
            # history - so repeating the call unchanged would just ask
            # the same question that already produced silence. Telling
            # it to answer from what it has is the part that differs.
            if not full_reply.strip():
                nudged = list(history) + [{
                    "role": "system",
                    "content": "Answer now, in plain prose, using the "
                               "information already gathered above. Do "
                               "not request any more tools. If the "
                               "information is not enough, say what is "
                               "missing.",
                }]
                try:
                    for piece in PROVIDER_STREAMERS[provider](
                            model, nudged, **kwargs_for(provider)):
                        full_reply += piece
                        yield piece
                except Exception as e:                      # noqa: BLE001
                    app.logger.warning("empty-stream retry failed: %s", e)

            if not full_reply.strip():
                reply_kind = "error"
                full_reply = (
                    "[The model returned nothing for that. It usually "
                    "means the question needs rewording - try asking it "
                    "differently, or more specifically.]")
                yield stream_event({"event": "reply", "kind": reply_kind})
                yield full_reply

            # THE CODE IS RUN BEFORE IT IS TRUSTED. In the code bay, a
            # finished reply's Python goes through the sandbox; a block
            # that fails is sent back to the same model with the
            # traceback for one repair, and both are appended under a
            # "Self-check" heading. Signed-in only, for the reason the
            # Run button is: the sandbox is a timeout and a memory cap,
            # not a container. See agents/verifier.py.
            if (mode == "code" and reply_kind == "text" and full_reply.strip()
                    and session.get("user_id")):
                def ask(prompt: str) -> str:
                    repair = list(history) + [
                        {"role": "assistant", "content": full_reply},
                        {"role": "user", "content": prompt}]
                    opts = dict(stream_kwargs.get("options") or {})
                    opts["num_predict"] = min(900, int(opts.get("num_predict") or 900))
                    return "".join(PROVIDER_STREAMERS[provider](
                        model, repair, options=opts, usage={}))
                # A slow local model is not asked for a repair: the
                # answer is already on screen, and a forty-second wait
                # for a footnote is worse than the footnote saying it
                # could not ask. The failure itself is still reported.
                can_ask = provider != "ollama" or _local_is_fast()
                extra = agent_verifier.self_check(full_reply, ask if can_ask else None)
                if extra:
                    full_reply += extra
                    yield extra
        except GeneratorExit:
            raise
        except Exception as e:
            # A backstop for anything unexpected from the streamer - a
            # broken reply still gets recorded (as an error, so it is
            # shown as one and never replayed) instead of vanishing.
            # The exception's own text goes to the log, not the person:
            # a requests error carries a host, a port and a pool state.
            app.logger.warning("reply failed on %s: %s", provider, e)
            reply_kind = "error"
            if site_job is not None:
                # A build that stopped says so, and nothing half-made is
                # saved: a blank site is worse than no site.
                full_reply = ("[The website build stopped: %s Nothing was "
                              "saved - send the request again to retry.]"
                              % (str(e) if isinstance(e, (sitebuilder.EmptyFileError, RuntimeError))
                                 else "the model stopped answering."))
            else:
                full_reply = ("[Something went wrong talking to %s. Try "
                              "sending that again.]" % _channel_name(provider))
            yield stream_event({"event": "reply", "kind": reply_kind})
            yield full_reply
        finally:
            if full_reply:
                # A backstop, not the primary defense - moderation.py's
                # check_message() on the *input* is what actually stops a
                # hard-stop request before it reaches the model. This just
                # catches the rarer case where the model produced
                # something in a hard-stop category anyway (e.g. talked
                # into it by phrasing the filter didn't catch). Because
                # streaming already sent earlier chunks to the browser as
                # they were generated, it can't unsend those - it can only
                # make sure the *saved* record and any reload show the
                # refusal instead, and that the reply isn't charged for.
                flagged = moderation.check_reply(full_reply)
                if flagged:
                    full_reply = moderation.REFUSAL_RESPONSE
                    kind = "refusal"
                else:
                    kind = reply_kind
                # Only an answer costs anything. A provider error, a
                # refusal and an empty-reply sentence are recorded so
                # the person can see what happened, and charged nothing
                # because nothing was generated for them. This used to
                # key on the reply starting with "[" - which a reply
                # opening with a markdown link also does.
                charged = 0
                # The reply's receipt (tokens and credits, shown under it):
                # the model's own count when the channel reports one,
                # otherwise an estimate at ~4 characters a token, marked
                # as such so the page can say "about".
                reply_tokens = 0
                tokens_estimated = False
                if kind == "text":
                    reply_tokens = int(usage.get("eval_count") or 0)
                    if not reply_tokens and full_reply:
                        reply_tokens = max(1, len(full_reply) // 4)
                        tokens_estimated = True
                    # The prose, plus whatever the tools did on the way
                    # to it - each round of the loop and any image made.
                    cost = (usage_based_cost(usage.get("eval_count"), mode)
                            + tools_used.cost)
                    try:
                        charged = spend_credits(cost, tokens=reply_tokens)
                    except Exception as e:              # noqa: BLE001
                        # Charging runs before the save below so the
                        # amount can be recorded on the turn. A failure
                        # here must not cost the person their answer.
                        app.logger.warning("charge failed: %s", e)
                # `fallback` is recorded on the message so reloading the
                # thread still shows it answered from the cloud - a
                # reader shouldn't have to guess why one reply differs.
                # Tool output is kept on the turn too, so a reload shows
                # the sources and the picture where the stream did.
                with DATA_LOCK:
                    thread["messages"].append(_assistant_message(
                        full_reply, provider, model, kind=kind,
                        fallback=fell_back_to_cloud,
                        sources=web_results or None,
                        charged=charged or None,
                        tokens=reply_tokens or None,
                        tokens_estimated=tokens_estimated or None,
                        tool_displays=tools_used.displays or None))
                    thread["updated"] = now_iso()
                save_thread(thread)

    # stream_with_context keeps the request context alive for as long as the
    # generator is running. Without it Flask tears the context down as soon
    # as this function returns - which is *before* a single chunk has been
    # produced - and the `finally` block above blows up on `session` the
    # moment it tries to charge for the reply. The user still saw their
    # answer, so the failure is invisible from the browser; it just means
    # credits were silently never deducted.
    return streaming_response(generate())


# =====================================================================
# WEBSITES: BUILD, STORE, PUBLISH, SERVE
#
# A website request is built by sitebuilder.py - planned, written file
# by file, reviewed - and saved as a project. Its owner can download it
# or publish it at <slug>.<PUBLISH_DOMAIN>, where this app serves the
# files and answers the forms itself (the "built-in backend"): the
# project's own app.py is for self-hosting, and no AI-written Python
# ever runs on this server.
# =====================================================================
PUBLISH_DOMAIN = (os.environ.get("PUBLISH_DOMAIN") or "randomgenerals.com").strip().lower()
sitebuilder.log = lambda message: app.logger.warning("[sitebuilder] %s", message)

# Names that would look like part of this service, or that it may need.
RESERVED_SLUGS = set("""
www api app apps admin administrator root mail email smtp imap pop ftp ns ns1 ns2
static assets cdn media img images files download downloads dashboard status stats
help support docs blog news about legal terms privacy security secure login logout
signin signup auth account accounts billing pay payment payments checkout store shop
randomgenerals pip dev staging test beta demo preview sites site my me you official
""".split())
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])$")
SITE_LIMITS = {"free": 3, "pro": 20}

_BUILD_VERB = re.compile(r"\b(build|make|create|design|code|develop|generate|write|set up)\b",
                         re.IGNORECASE)

# The words that ask for the premium build (sitebuilder.build_premium).
_PREMIUM_HINT = re.compile(
    r"\b(premium|sick|amazing|high[- ]end|luxury|luxurious|world[- ]class|"
    r"production[- ](?:grade|ready)|state[- ]of[- ]the[- ]art|insane|stunning|"
    r"top[- ]tier|best possible|award[- ]winning|elite)\b",
    re.IGNORECASE)

# A follow-up in a thread that already has a site, asking for a change.
_REVISION_HINT = re.compile(
    r"\b(make|change|add|remove|delete|replace|use|turn|update|fix|put|move|"
    r"swap|increase|decrease|bigger|smaller|darker|lighter|redesign|rewrite|"
    r"rename|translate|instead|more|less|another|new section|new page)\b",
    re.IGNORECASE)


def _signed_in() -> bool:
    """A real account, not just a user_id left in the cookie - the same
    test current_owner_id() applies, so "may publish" and "owns this
    site" can never disagree."""
    uid = session.get("user_id")
    return bool(uid and uid in USERS)


def _site_slug_for_host(host: str) -> str | None:
    """The site a request is for, from its Host. None for the app itself."""
    host = (host or "").split(":")[0].strip().lower().rstrip(".")
    for base in (PUBLISH_DOMAIN, "localhost"):
        if host.endswith("." + base):
            sub = host[: -len(base) - 1]
            if sub and sub != "www" and "." not in sub:
                return sub
    return None


def site_url(slug: str) -> str:
    """Where a published site lives. A local run points at
    <slug>.localhost, which every browser resolves to this machine."""
    host = (request.host or "") if request else ""
    if host.split(":")[0] in ("localhost", "127.0.0.1"):
        port = host.split(":")[1] if ":" in host else "80"
        return "http://%s.localhost:%s" % (slug, port)
    return "https://%s.%s" % (slug, PUBLISH_DOMAIN)


def _thread_site_id(thread) -> str | None:
    """The newest site built in this thread, from the cards on its replies."""
    for m in reversed(thread.get("messages") or []):
        for d in (m.get("tool_displays") or []):
            if isinstance(d, dict) and d.get("kind") == "site" and d.get("id"):
                return d["id"]
    return None


def _site_job_for(thread, asked: str, has_images: bool, strength: str = "quick"):
    """Whether this message builds a site, revises one, or neither."""
    if has_images or not asked:
        return None
    site_id = _thread_site_id(thread)
    if site_id and _REVISION_HINT.search(asked) and not asked.rstrip().endswith("?"):
        site = db.site_get(site_id)
        if site and site["owner_id"] == current_owner_id():
            return {"kind": "revise", "site": site}
    # "What is a storefront?" names a website without asking for one. A
    # question only starts a build when it also asks for something made.
    if asked.rstrip().endswith("?") and not _BUILD_VERB.search(asked):
        return None
    if _is_website_request(asked):
        return {"kind": "build",
                "premium": bool(_PREMIUM_HINT.search(asked)) or strength == "deep"}
    return None


def _builder_llm(usage: dict, use_gemma: bool = False):
    """The model call the builder makes, as a generator: yields progress
    while it waits, returns (text, finish_reason).

    Groq when it is set up - the big model for planning, markup and
    styling, the smaller one (metered separately) for the script and the
    backend - and the free OpenRouter model otherwise. A rate limit is
    waited out, visibly, rather than failed: the builder is allowed to
    take its time, and the next minute's budget is usually seconds away.
    """
    daily_out: set[str] = set()      # models whose daily allowance is spent

    def llm(key, label, messages, max_tokens, effort, small):
        if _groq_only() or (groq_api.configured() and groq_api.models()):
            big = _groq_model_for("code") or groq_api.PREFERRED[0]
            smaller = next((m for _, m in _groq_fallbacks(big)), None)
            order = [("groq", smaller), ("groq", big)] if small and smaller else \
                    [("groq", big)] + ([("groq", smaller)] if smaller else [])
        else:
            free = _openrouter_model_for("code")
            order = [("openrouter", free)] if free else []
        # DeepSeek V4 Pro writes the site while the key has credit; the
        # Groq models above stay behind it, so running out of credit
        # mid-build finishes the build there instead of failing it.
        deepseek = _deepseek_model_for("code")
        if deepseek and not any(m == deepseek for _, m in order):
            order = [("openrouter", deepseek)] + order
        # Gemma 4 31B writes the site when the person picked it; Groq stays
        # behind it, so a rate limit mid-build finishes the build there.
        gemma = _gemma_model() if (use_gemma and not deepseek) else None
        if gemma:
            order = [("google", gemma)] + order
        order = [o for o in order if o[1] not in daily_out
                 and not (o[0] == "openrouter" and not openrouter_api.is_free(o[1])
                          and not openrouter_api.has_credit())
                 and not (o[0] == "google" and not google_api.ready())] or order
        if not order:
            raise RuntimeError("No model is available to build with.")
        opts = {"num_predict": max_tokens, "temperature": 0.4,
                "reasoning_effort": effort}
        attempts = 0
        idx = 0
        while True:
            provider, model = order[idx % len(order)]
            pool = ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(PROVIDER_TURNS[provider], model, messages,
                              None, opts, 240)
            # A model call is a quiet half-minute. Something has to cross
            # the wire meanwhile, or the tunnel decides the reply died.
            while True:
                try:
                    msg, err = fut.result(timeout=8)
                    break
                except FuturesTimeout:
                    yield ("tick",)
                except providers.ProviderUnavailable as e:
                    msg, err = None, e
                    break
            pool.shutdown(wait=False)
            if isinstance(err, providers.ProviderUnavailable) and provider == "google" \
                    and len(order) > 1:
                # Gemma's free tier is out for now: hand the step to Groq
                # at once instead of waiting on Google's cool-down.
                order = [o for o in order if o[0] != "google"] or order
                idx = 0
                continue
            if isinstance(err, providers.ProviderUnavailable) or (
                    err and "429" in str(err)):
                attempts += 1
                # How long Groq says to wait. Seconds means the per-minute
                # budget, which is worth waiting out; many minutes means a
                # model's DAILY allowance is spent, and waiting cannot fix
                # that - the first live premium build spun for minutes on
                # one before giving up.
                try:
                    told = float(str(err))
                except (TypeError, ValueError):
                    told = None
                if told is not None and told > 90:
                    daily_out.add(model)
                    if all(m in daily_out for _, m in order):
                        mins = max(1, int(told // 60))
                        raise RuntimeError(
                            "Groq's free daily limit is used up for now (it resets in "
                            "about %s). A paid Groq plan removes this limit."
                            % ("%d minutes" % mins if mins < 120 else "%d hours" % (mins // 60)))
                    idx += 1          # the other model has its own allowance
                    continue
                if attempts > 8:
                    raise RuntimeError("The model stayed busy for too long.")
                _left, resets_in = (groq_api.budget_state() if provider == "groq"
                                    else (None, None))
                wait = int(min(60, max(5, (told or resets_in or 12) + 1)))
                if attempts % 2 == 0 and len(order) > 1:
                    idx += 1          # try the other model next time
                for remaining in range(wait, 0, -1):
                    yield ("wait", key, label, remaining)
                    time.sleep(1)
                continue
            if err:
                attempts += 1
                if attempts > 3:
                    raise RuntimeError(str(err))
                idx += 1
                continue
            text = (msg or {}).get("content") or ""
            usage["tokens"] = usage.get("tokens", 0) + len(text) // 4
            return text, (msg or {}).get("finish_reason")
    return llm


def _drive(gen):
    """Run a builder generator, turning its events into stream events.
    Returns what the builder returns."""
    try:
        ev = next(gen)
        while True:
            kind = ev[0]
            if kind == "step":
                _, key, status, label = ev
                yield stream_event({"tool": "build-" + key, "status": status,
                                    "label": label})
            elif kind == "wait":
                _, key, label, seconds = ev
                yield stream_event({"tool": "build-" + key, "status": "start",
                                    "label": "%s - waiting %ds for the model" % (label, seconds)})
            else:
                yield stream_event({"event": "tick"})
            ev = next(gen)
    except StopIteration as stop:
        return stop.value


def _site_reply_text(project: dict, revised: list[str] | None) -> str:
    plan = project["plan"]
    files = project["files"]
    if revised is None and plan.get("premium"):
        lines = ["Built **%s** as a premium project - researched, architected, then %d files "
                 "written one at a time and reviewed against each other%s." % (
                     plan["name"], len(files), " (%s fixed in review)" % ", ".join(project["fixed"])
                     if project.get("fixed") else "")]
        if plan.get("design_direction"):
            lines.append("")
            lines.append("_%s_" % plan["design_direction"])
        lines.append("")
        lines.append("- **The site** - index.html, a token-based design system (css/tokens.css + css/main.css), "
                     "and type-checked JavaScript modules: %s" % ", ".join(plan.get("features") or ["menu, reveal, forms"]))
        if plan.get("datasets"):
            lines.append("- **Content as data** - %s, rendered by js/app.js"
                         % ", ".join("data/%s.json" % d["name"] for d in plan["datasets"]))
        lines.append("- **Backend** - a Flask service (backend/) that serves the site and stores form entries in SQLite")
        if plan.get("needs_compute"):
            lines.append("- **Rust/WASM** - wasm/ computes %s; app.js has a JavaScript fallback until you build it"
                         % plan.get("compute"))
        lines.append("- **Ready to ship** - Dockerfile, docker-compose.yml, .env.example, package.json + tsconfig.json for `npm run typecheck`, and a README")
        lines.append("")
        lines.append("Preview it in the panel, publish it from the card below, or download the whole workspace as a .zip.")
    elif revised is None:
        lines = ["Built **%s** - planned first, then each file written and "
                 "reviewed against the others%s." % (
                     plan["name"], " (and %s fixed in review)" % ", ".join(project["fixed"])
                     if project.get("fixed") else "")]
        if plan.get("tagline"):
            lines.append("_%s_" % plan["tagline"])
        lines.append("")
        lines.append("- **index.html** - %s" % ", ".join(s["title"] for s in plan["sections"]))
        lines.append("- **styles.css** - %s with %s, one accent (%s), responsive down to phones"
                     % (plan["fonts"]["display"], plan["fonts"]["body"], plan["palette"]["accent"]))
        lines.append("- **script.js** - mobile menu, scroll reveal, and working forms (%s)"
                     % ", ".join(f["name"] for f in plan["forms"]))
        lines.append("- **app.py** - a Flask backend that stores the form submissions, to run it yourself")
        lines.append("")
        lines.append("Preview it in the panel, then publish it from the card below - "
                     "or download the whole project as a .zip.")
    else:
        lines = ["Updated **%s**: %s. Publish again from the card to put the "
                 "new version live." % (plan["name"], ", ".join(revised))]
    lines.append("")
    for name, body in files.items():
        lang = sitebuilder.lang_of(name)
        fence = "````" if name.endswith(".md") else "```"
        ext = os.path.splitext(name)[1].lower()
        if ext in (".html", ".md"):
            label = "<!-- %s -->" % name
        elif ext in (".css",):
            label = "/* %s */" % name
        elif ext in (".js", ".ts", ".rs"):
            label = "// %s" % name
        elif ext == ".json":
            # JSON has no comments; the file's name goes on a line of its
            # own that the page reads and strips (see readFilename).
            label = "// %s" % name
        else:
            label = "# %s" % name
        lines.append("%s%s\n%s\n%s\n%s" % (fence, lang, label, body.rstrip("\n"), fence))
    return "\n".join(lines)


def _site_research(brief: str) -> tuple[str, int]:
    """Web results on what a site like this should be, for the premium
    planner. Several searches at once, eight seconds at most - the
    person is watching the build. -> (notes, how many results)."""
    topic = imagesearch.topic_of(brief) or brief[:60]
    queries = ["best %s website design" % topic,
               "%s what customers expect" % topic,
               topic]
    found: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=len(queries))
    futures = [pool.submit(websearch.search, q, 4) for q in queries]
    try:
        for fut in futures:
            try:
                found += fut.result(timeout=8) or []
            except Exception:                       # noqa: BLE001
                continue
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    seen, lines = set(), []
    for r in found:
        url = r.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        lines.append("- %s (%s): %s" % ((r.get("title") or "")[:100], url[:100],
                                         (r.get("snippet") or "")[:300]))
    return "\n".join(lines[:12]), len(lines[:12])


def _run_site_job(job, thread, asked, usage):
    """The whole build or revision, streamed. Returns (reply, display)."""
    owner = current_owner_id()
    llm = _builder_llm(usage, use_gemma=job.get("model") == "google")
    if job["kind"] == "build":
        premium = job.get("premium")
        notes = ""
        if premium:
            yield stream_event({"tool": "build-research", "status": "start",
                                "label": "Researching the domain on the web"})
            notes, count = _site_research(asked)
            yield stream_event({"tool": "build-research", "status": "done",
                                "label": "Read %d sources" % count if count
                                else "No research found - designing from the brief"})
        yield stream_event({"tool": "build-photos", "status": "start",
                            "label": "Finding real photos"})
        photos = imagesearch.photos_for(asked, 12 if premium else 8)
        yield stream_event({"tool": "build-photos", "status": "done",
                            "label": "Found %d photos" % len(photos) if photos
                            else "No photos found - using illustration instead"})
        credit = imagesearch.credit_line(photos) if photos else ""
        if premium:
            project = yield from _drive(sitebuilder.build_premium(asked, photos, credit, notes, llm))
        else:
            project = yield from _drive(sitebuilder.build(asked, photos, credit, llm))
        site_id = uuid.uuid4().hex[:16]
        revised = None
    else:
        site = job["site"]
        photos = []
        if _WANTS_PHOTOS.search(asked):
            photos = imagesearch.photos_for(
                (site["plan"].get("name") or "") + " " + asked, 8)
        reviser = (sitebuilder.revise_premium if site["plan"].get("premium")
                   else sitebuilder.revise)
        project = yield from _drive(reviser(
            asked, {"plan": site["plan"], "files": site["files"]}, photos, llm))
        site_id = site["id"]
        revised = project.get("touched") or []
    db.site_save(site_id, owner, thread.get("id"), project["name"],
                 project["plan"], project["files"], now_iso())
    saved = db.site_get(site_id)
    display = {"kind": "site", "id": site_id, "name": project["name"],
               "slug": project["plan"]["slug"], "version": saved["version"],
               "files": list(project["files"].keys())}
    return _site_reply_text(project, revised), display


def _own_site(site_id):
    site = db.site_get(site_id)
    if not site or site["owner_id"] != current_owner_id():
        return None
    return site


def _site_view(site) -> dict:
    # The conversation that built it, while that still exists and is the
    # same owner's - the home screen's artifact cards open it.
    thread = THREADS.get(site.get("thread_id") or "")
    if thread and thread.get("owner_id") != site.get("owner_id"):
        thread = None
    return {
        "thread_id": site.get("thread_id") if thread else None,
        "thread_mode": thread.get("mode", DEFAULT_MODE) if thread else None,
        "id": site["id"], "name": site["name"], "version": site["version"],
        "slug": site["slug"], "suggested_slug": site["plan"].get("slug") or "",
        "url": site_url(site["slug"]) if site["slug"] else None,
        "published_version": site.get("published_version"),
        "published_at": site.get("published_at"), "updated": site["updated"],
        "files": list(site["files"].keys()),
        "forms": [f["name"] for f in site["plan"].get("forms", [])],
        "submissions": db.submission_count(site["id"]),
        "domain": PUBLISH_DOMAIN,
    }


@app.route("/api/sites", methods=["GET"])
def list_sites():
    return jsonify({"sites": [_site_view(s) for s in db.sites_for(current_owner_id())],
                    "signed_in": _signed_in()})


@app.route("/api/sites/<site_id>", methods=["GET"])
def get_site(site_id):
    site = _own_site(site_id)
    if not site:
        return jsonify({"error": "No such site."}), 404
    view = _site_view(site)
    view["signed_in"] = _signed_in()
    return jsonify(view)


@app.route("/api/sites/<site_id>/publish", methods=["POST"])
@limited("chat", LIMIT_CHAT, key=_limit_key_account)
def publish_site(site_id):
    site = _own_site(site_id)
    if not site:
        return jsonify({"error": "No such site."}), 404
    if not _signed_in():
        return jsonify({"error": "Sign in to publish - your site stays here "
                                 "until you do.", "signin": True}), 401
    payload = request.get_json(force=True, silent=True) or {}
    slug = str(payload.get("slug") or site["slug"] or site["plan"].get("slug") or "").strip().lower()
    if not SLUG_RE.match(slug):
        return jsonify({"error": "Use 3-32 letters, numbers and dashes, "
                                 "starting and ending with a letter or number."}), 400
    if slug in RESERVED_SLUGS:
        return jsonify({"error": "That name is reserved. Try another."}), 400
    plan = features.normalize_plan(current_account()[0].get("plan"))
    limit = SITE_LIMITS.get(plan, SITE_LIMITS["free"])
    if not site["slug"] and db.published_count(site["owner_id"]) >= limit:
        return jsonify({"error": "Your plan can keep %d sites live. Unpublish "
                                 "one first%s." % (limit, "" if plan == "pro"
                                                   else ", or upgrade to Pro")}), 402
    blocked = moderation.check_message(
        " ".join(re.sub(r"<[^>]+>", " ", site["files"].get("index.html", ""))
                 .split())[:6000])
    if blocked is not None:
        return jsonify({"error": "This site can't be published: " + blocked}), 400
    if not db.site_publish(site_id, slug, now_iso()):
        return jsonify({"error": "%s.%s is taken. Try another name."
                                 % (slug, PUBLISH_DOMAIN)}), 409
    return jsonify(_site_view(db.site_get(site_id)))


@app.route("/api/sites/<site_id>/unpublish", methods=["POST"])
def unpublish_site(site_id):
    site = _own_site(site_id)
    if not site:
        return jsonify({"error": "No such site."}), 404
    db.site_unpublish(site_id)
    return jsonify(_site_view(db.site_get(site_id)))


@app.route("/api/sites/<site_id>/zip", methods=["GET"])
def download_site(site_id):
    site = _own_site(site_id)
    if not site:
        abort(404)
    buf = io.BytesIO()
    folder = site["plan"].get("slug") or "site"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in site["files"].items():
            z.writestr(folder + "/" + name, body)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=folder + ".zip")


@app.route("/api/sites/<site_id>/submissions", methods=["GET"])
def site_submissions(site_id):
    site = _own_site(site_id)
    if not site:
        return jsonify({"error": "No such site."}), 404
    return jsonify({"submissions": db.submissions_for(site_id)})


@app.route("/sites")
def sites_page():
    sites = []
    for s in db.sites_for(current_owner_id()):
        view = _site_view(s)
        view["recent"] = db.submissions_for(s["id"], limit=50)
        sites.append(view)
    return render_template("sites.html", sites=sites,
                           signed_in=_signed_in(),
                           domain=PUBLISH_DOMAIN)


# ---- skills, connectors and plugins (extensions.py) ----------------
#
# One page, /customize, with three tabs; these routes are what it and the
# composer's "/" menu read and write. Built-in skills work for everyone,
# signed in or not. Anything that is SAVED - a skill, an installed plugin,
# a connected app - needs an account, because a guest's identity lives in
# a session cookie and a connected Notion workspace should not vanish
# with it.
def _need_account():
    if not _signed_in():
        return jsonify({"error": "Sign in to save skills, plugins and connected apps.",
                        "signin": True}), 401
    return None


def _plan_limit(key):
    plan = features.normalize_plan(current_account()[0].get("plan"))
    return features.FEATURES[plan][key], plan


def _skill_view(s):
    return {"id": s["id"], "title": s["title"], "icon": s.get("icon") or "✨",
            "description": s.get("description") or "", "source": s.get("source", "builtin"),
            "handled_by": s.get("handled_by")}


@app.route("/customize")
def customize_page():
    return render_template("customize.html", signed_in=_signed_in())


@app.route("/api/extensions", methods=["GET"])
def extensions_overview():
    owner_id = current_owner_id()
    mine = db.skills_for(owner_id) if _signed_in() else []
    installed = db.plugins_for(owner_id) if _signed_in() else []
    active = extensions.active_skills(mine, installed)
    conns = {c["app_id"]: c for c in (db.connections_for(owner_id) if _signed_in() else [])}
    plugin_skill_ids = {sid for p in installed for sid in
                        extensions.PLUGINS_BY_ID.get(p, {}).get("skills", [])}
    apps = []
    for a in extensions.available_apps():
        c = conns.get(a["id"])
        apps.append({"id": a["id"], "title": a["title"], "category": a["category"],
                     "description": a["description"], "color": a["color"],
                     # SVG logos are one-colour shapes (Simple Icons), drawn
                     # on the brand colour; PNG/ICO ones are the app's own
                     # full-colour icon.
                     "logo": (url_for("static", filename="apps/" + a["logo"])
                              if a.get("logo") else None),
                     "logo_mono": (a.get("logo") or "").endswith(".svg"),
                     "signin": a["auth"] != "none",
                     "connection": connections.public(c) if c else None})
    max_apps, plan = _plan_limit("max_apps")
    max_skills, _ = _plan_limit("max_skills")
    return jsonify({
        "signed_in": _signed_in(),
        "plan": plan,
        "limits": {"apps": max_apps, "skills": max_skills},
        # Everything that is on for this person, for the "/" menu.
        "active": [_skill_view(s) for s in active],
        "builtin": [_skill_view(dict(s, source="builtin")) for s in extensions.BUILTIN_SKILLS],
        "mine": mine,
        "plugins": [dict(p, installed=p["id"] in installed,
                         skill_list=[{"id": sid, "title": (extensions.BUILTIN_BY_ID.get(sid)
                                                           or extensions.PLUGIN_SKILLS.get(sid, {})).get("title", sid)}
                                     for sid in p["skills"]],
                         app_list=[{"id": aid, "title": extensions.APPS_BY_ID[aid]["title"]}
                                   for aid in p["apps"] if aid in extensions.APPS_BY_ID
                                   and extensions.APPS_BY_ID[aid] in extensions.available_apps()])
                    for p in extensions.PLUGINS],
        "plugin_skills": sorted(plugin_skill_ids),
        "apps": apps,
    })


def _clean_skill(payload, existing=None):
    """-> (skill dict, error)."""
    slug = (payload.get("slug") or (existing or {}).get("slug") or "").strip().lower().lstrip("/")
    title = (payload.get("title") or "").strip()[:80]
    instructions = (payload.get("instructions") or "").strip()
    if not extensions.SLUG_RE.match(slug):
        return None, "The name must be 2-32 lowercase letters, numbers or dashes, like meal-planner."
    if extensions.reserved_slug(slug):
        return None, "/%s is already a built-in skill. Pick another name." % slug
    if not title:
        return None, "Give the skill a title."
    if len(instructions) < 10:
        return None, "Write the instructions the AI should follow (at least a sentence)."
    if len(instructions) > extensions.MAX_USER_INSTRUCTIONS:
        return None, "Instructions can be up to %d characters." % extensions.MAX_USER_INSTRUCTIONS
    triggers = ",".join(w.strip()[:40] for w in (payload.get("triggers") or "").split(",")
                        if w.strip())[:400]
    return {"id": (existing or {}).get("id") or uuid.uuid4().hex[:16], "slug": slug,
            "title": title, "description": (payload.get("description") or "").strip()[:240],
            "instructions": instructions, "triggers": triggers,
            "enabled": bool(payload.get("enabled", True))}, None


@app.route("/api/skills", methods=["POST"])
def create_skill():
    refused = _need_account()
    if refused:
        return refused
    owner_id = current_owner_id()
    limit, plan = _plan_limit("max_skills")
    if len(db.skills_for(owner_id)) >= limit:
        return jsonify({"error": "You can have up to %d skills on this plan%s." % (
            limit, "" if plan == features.PRO else " - Pro allows %d"
            % features.FEATURES[features.PRO]["max_skills"]), "upgrade": plan != features.PRO}), 400
    skill, err = _clean_skill(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    if any(s["slug"] == skill["slug"] for s in db.skills_for(owner_id)):
        return jsonify({"error": "You already have a skill called /%s." % skill["slug"]}), 400
    db.skill_save(owner_id, skill, datetime.datetime.now(datetime.timezone.utc).isoformat())
    return jsonify({"ok": True, "skill": db.skill_get(owner_id, skill["id"])})


@app.route("/api/skills/<skill_id>", methods=["PUT", "DELETE"])
def change_skill(skill_id):
    refused = _need_account()
    if refused:
        return refused
    owner_id = current_owner_id()
    existing = db.skill_get(owner_id, skill_id)
    if not existing:
        return jsonify({"error": "No such skill."}), 404
    if request.method == "DELETE":
        db.skill_delete(owner_id, skill_id)
        return jsonify({"ok": True})
    payload = request.get_json(force=True, silent=True) or {}
    merged = dict(existing, **{k: v for k, v in payload.items()
                               if k in ("slug", "title", "description", "instructions",
                                        "triggers", "enabled")})
    skill, err = _clean_skill(merged, existing)
    if err:
        return jsonify({"error": err}), 400
    if any(s["slug"] == skill["slug"] and s["id"] != skill_id for s in db.skills_for(owner_id)):
        return jsonify({"error": "You already have a skill called /%s." % skill["slug"]}), 400
    db.skill_save(owner_id, skill, datetime.datetime.now(datetime.timezone.utc).isoformat())
    return jsonify({"ok": True, "skill": db.skill_get(owner_id, skill_id)})


@app.route("/api/plugins/<plugin_id>", methods=["POST", "DELETE"])
def toggle_plugin(plugin_id):
    refused = _need_account()
    if refused:
        return refused
    if plugin_id not in extensions.PLUGINS_BY_ID:
        return jsonify({"error": "No such plugin."}), 404
    owner_id = current_owner_id()
    if request.method == "DELETE":
        db.plugin_uninstall(owner_id, plugin_id)
    else:
        db.plugin_install(owner_id, plugin_id,
                          datetime.datetime.now(datetime.timezone.utc).isoformat())
    return jsonify({"ok": True, "installed": request.method == "POST"})


LIMIT_CONNECT = Limiter(rate=20, per=600, burst=10)


@app.route("/api/apps/<app_id>/connect", methods=["POST"])
@limited("connect", LIMIT_CONNECT, key=_limit_key_account)
def connect_app(app_id):
    """One click: connected at once (apps that need no sign-in), or the
    address of the app's own sign-in page to send the browser to."""
    refused = _need_account()
    if refused:
        return refused
    owner_id = current_owner_id()
    limit, plan = _plan_limit("max_apps")
    current = db.connections_for(owner_id)
    if len(current) >= limit and not any(c["app_id"] == app_id for c in current):
        return jsonify({"error": "You can connect up to %d apps on this plan%s." % (
            limit, "" if plan == features.PRO else " - Pro allows %d"
            % features.FEATURES[features.PRO]["max_apps"]), "upgrade": plan != features.PRO}), 400
    try:
        kind, value = connections.begin(owner_id, app_id,
                                        url_for("apps_callback", _external=True))
    except connections.ConnectError as e:
        return jsonify({"error": str(e)}), 400
    if kind == "redirect":
        return jsonify({"redirect": value})
    return jsonify({"connected": value})


@app.route("/api/apps/callback")
def apps_callback():
    """Where an app's sign-in page sends the browser back."""
    if request.args.get("error"):
        return redirect("/customize?tab=connectors&error=" + urllib.parse.quote(
            "The sign-in was cancelled or refused (%s)." % request.args.get("error")[:60]))
    if not _signed_in():
        return redirect("/customize?tab=connectors&error=" + urllib.parse.quote(
            "Sign in to RandomGenerals first, then press Connect again."))
    try:
        item = connections.finish(current_owner_id(), request.args.get("state", ""),
                                  request.args.get("code", ""))
    except connections.ConnectError as e:
        return redirect("/customize?tab=connectors&error=" + urllib.parse.quote(str(e)))
    return redirect("/customize?tab=connectors&connected=" + urllib.parse.quote(item["app_id"]))


@app.route("/api/apps/connections/<conn_id>/refresh", methods=["POST"])
def refresh_app(conn_id):
    refused = _need_account()
    if refused:
        return refused
    try:
        return jsonify({"connection": connections.refresh_tools(current_owner_id(), conn_id)})
    except connections.ConnectError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/apps/connections/<conn_id>", methods=["DELETE"])
def disconnect_app(conn_id):
    refused = _need_account()
    if refused:
        return refused
    if not db.connection_delete(current_owner_id(), conn_id):
        return jsonify({"error": "No such connection."}), 404
    return jsonify({"ok": True})


# ---- serving a published site --------------------------------------
_SITE_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".js": "text/javascript; charset=utf-8", ".txt": "text/plain; charset=utf-8",
               ".json": "application/json", ".svg": "image/svg+xml"}
_SITE_HIDDEN = {"app.py", "requirements.txt", "README.md", "package.json",
                "tsconfig.json", "docker-compose.yml", "Dockerfile", ".env.example",
                ".dockerignore"}


def _site_public(name: str) -> bool:
    """Whether a project file is part of the site a visitor loads. The
    backend, the Rust crate and the tooling are the downloadable
    workspace, not the website."""
    if name in _SITE_HIDDEN or name.startswith(("backend/", "wasm/", "types/", ".")):
        return False
    return os.path.splitext(name)[1].lower() in (".html", ".css", ".js", ".json",
                                                 ".svg", ".txt", ".webmanifest")
_form_hits: dict[str, list[float]] = {}
_form_lock = threading.Lock()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# On every published page: who made it, and where to report it. A site
# on this domain carries this domain's name, so it also carries the way
# to say it should not.
_SITE_BADGE = (
    '<a href="https://%s/?ref=site" target="_blank" rel="noopener" '
    'style="position:fixed;right:12px;bottom:12px;z-index:2147483000;'
    'font:500 12px/1 system-ui,sans-serif;color:#fafafa;background:rgba(9,9,11,.82);'
    'padding:8px 11px;border-radius:999px;text-decoration:none;'
    'box-shadow:0 4px 16px rgba(0,0,0,.25)">Made with RandomGenerals</a>'
    '<a href="https://%s/contact?report=%s" target="_blank" rel="noopener" '
    'style="position:fixed;left:12px;bottom:12px;z-index:2147483000;'
    'font:500 11px/1 system-ui,sans-serif;color:#a1a1aa;text-decoration:none">Report</a>')


def _site_response(body, ctype, status=200):
    resp = Response(body, status=status, content_type=ctype)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def _site_not_found(slug):
    page = ("<!doctype html><meta charset=utf-8><title>Nothing here yet</title>"
            "<body style=\"font:16px system-ui;display:grid;place-items:center;min-height:90vh;"
            "background:#09090b;color:#fafafa;text-align:center\"><div><h1>Nothing here yet</h1>"
            "<p style=\"color:#a1a1aa\">%s.%s isn't a published site.</p>"
            "<p><a style=\"color:#a5b4fc\" href=\"https://%s\">Build one with RandomGenerals</a></p>"
            "</div></body>" % (escape(slug), PUBLISH_DOMAIN, PUBLISH_DOMAIN))
    return _site_response(page, _SITE_TYPES[".html"], 404)


def _site_form(site, form_name):
    form = next((f for f in site["plan"].get("forms", []) if f["name"] == form_name), None)
    if not form:
        return jsonify({"ok": False, "error": "There is no such form."}), 404
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"
    key = "%s|%s" % (site["id"], ip)
    now = time.time()
    with _form_lock:
        hits = [t for t in _form_hits.get(key, []) if now - t < 600]
        if len(hits) >= 10:
            return jsonify({"ok": False, "error": "Too many messages - try again "
                                                  "in a few minutes."}), 429
        hits.append(now)
        _form_hits[key] = hits
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or len(data) > 30:
        return jsonify({"ok": False, "error": "That didn't arrive as a form."}), 400
    clean = {}
    for fld in form["fields"]:
        value = str(data.get(fld["name"]) or "").strip()[:2000]
        if fld.get("required") and not value:
            return jsonify({"ok": False, "error": "Please fill in %s." % fld["label"]}), 400
        if value and fld["type"] == "email" and not _EMAIL_RE.match(value):
            return jsonify({"ok": False, "error": "That email address doesn't look right."}), 400
        clean[fld["name"]] = value
    for k, v in data.items():                 # extra fields the page added
        k = str(k)[:40]
        if k not in clean and isinstance(v, (str, int, float, bool)):
            clean[k] = str(v)[:2000]
    db.submission_add(site["id"], form_name, clean, now_iso())
    return jsonify({"ok": True, "message": form.get("success") or "Thanks - it's on its way."})


def _serve_published_site():
    """Answer requests for <slug>.<PUBLISH_DOMAIN> before anything else
    runs: no session, no visit counting, and none of the app's own routes
    - a published page shares nothing with the app but the server."""
    slug = _site_slug_for_host(request.host)
    if slug is None:
        return None
    site = db.site_by_slug(slug)
    if not site:
        return _site_not_found(slug)
    path = request.path
    if path.startswith("/api/forms/"):
        if request.method != "POST":
            return jsonify({"ok": False, "error": "Send the form with POST."}), 405
        return _site_form(site, path[len("/api/forms/"):].strip("/"))
    if request.method not in ("GET", "HEAD"):
        return _site_response("Method not allowed", "text/plain", 405)
    files = site["published_files"] or {}
    name = path.lstrip("/") or "index.html"
    if name.endswith("/"):
        name += "index.html"
    if name not in files and name + ".html" in files:
        name += ".html"
    if name not in files or not _site_public(name):
        return _site_not_found(slug) if name == "index.html" else \
            _site_response("Not found", "text/plain; charset=utf-8", 404)
    ext = os.path.splitext(name)[1].lower()
    body = files[name]
    if ext == ".html":
        badge = _SITE_BADGE % (PUBLISH_DOMAIN, PUBLISH_DOMAIN, escape(slug))
        body = (body.replace("</body>", badge + "</body>", 1)
                if "</body>" in body else body + badge)
    return _site_response(body, _SITE_TYPES.get(ext, "application/octet-stream"))


# First, ahead of every other before_request hook.
app.before_request_funcs.setdefault(None, []).insert(0, _serve_published_site)


@app.before_request
def _refuse_published_site_origins():
    """A published site shares this app's registrable domain, so the
    browser treats its requests here as same-site and sends the login
    cookie with them. Any state-changing request whose Origin is a
    published site is refused: those pages may run anything their
    author wrote, and none of it should act as the visitor here."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin") or ""
    if origin and _site_slug_for_host(urllib.parse.urlparse(origin).hostname or ""):
        return jsonify({"error": "Cross-site request refused."}), 403
    return None


@app.route("/api/feedback", methods=["POST"])
@limited("feedback", LIMIT_FEEDBACK, key=_limit_key_account)
def reply_feedback():
    """A thumb on a reply. Stored with the question and the answer,
    truncated, so agents/evaluate.py --from-feedback can turn a dislike
    into a case with a right answer attached later. vote 0 withdraws."""
    payload = request.get_json(force=True, silent=True) or {}
    tid = str(payload.get("thread_id") or "")
    try:
        index = int(payload.get("index"))
        vote = int(payload.get("vote"))
    except (TypeError, ValueError):
        return jsonify({"error": "index and vote are needed"}), 400
    if vote not in (-1, 0, 1):
        return jsonify({"error": "vote is 1, -1 or 0"}), 400
    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Unknown thread"}), 404
    messages = thread.get("messages") or []
    if not (0 <= index < len(messages)) or messages[index].get("role") != "assistant":
        return jsonify({"error": "That is not a reply"}), 400
    reply = messages[index]
    question = next((m.get("content") or "" for m in reversed(messages[:index])
                     if m.get("role") == "user"), "")
    db.feedback_set(
        current_owner_id(), tid, index, vote,
        note=str(payload.get("note") or "")[:500],
        provider=str(reply.get("provider") or ""),
        model=str(reply.get("model") or ""),
        mode=str(thread.get("mode") or "chat"),
        question=question, answer=str(reply.get("content") or ""))
    return jsonify({"ok": True, "vote": vote})


# The workspace browser's page loads. Rate-limited on its own: each one is
# an outbound fetch this server makes on someone's behalf.
LIMIT_BROWSE = Limiter(rate=40, per=60, burst=10)


@app.route("/api/browse", methods=["GET"])
@limited("browse", LIMIT_BROWSE, key=_limit_key_account)
def browse():
    """A page (or an address-bar search) for the workspace browser, as
    the reader view built in browser.py."""
    try:
        page = browser.open_page(request.args.get("url") or "")
    except browser.BrowseError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({k: page[k] for k in ("kind", "url", "title", "site", "html")})


# THE LIVE BROWSER (livebrowser.py): a real Chromium tab per person,
# streamed to the pane as JPEG frames. Frames are polled about once a
# second while the pane is open, hence their own, looser limit.
LIMIT_LIVE_FRAME = Limiter(rate=240, per=60, burst=30)
LIMIT_LIVE_ACT = Limiter(rate=150, per=60, burst=30)


@app.route("/api/browser/status", methods=["GET"])
def live_browser_status():
    return jsonify({"live": livebrowser.available()})


@app.route("/api/browser/go", methods=["POST"])
@limited("browse", LIMIT_BROWSE, key=_limit_key_account)
def live_browser_go():
    if not livebrowser.available():
        return jsonify({"error": "The live browser is not running here.", "live": False}), 503
    p = request.get_json(silent=True) or {}
    try:
        state = livebrowser.go(current_owner_id(), str(p.get("url") or "")[:2000],
                               p.get("w"), p.get("h"))
    except livebrowser.LiveError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(state)


@app.route("/api/browser/act", methods=["POST"])
@limited("browser-act", LIMIT_LIVE_ACT, key=_limit_key_account)
def live_browser_act():
    if not livebrowser.available():
        return jsonify({"error": "The live browser is not running here.", "live": False}), 503
    p = request.get_json(silent=True) or {}
    action = {k: p.get(k) for k in ("type", "x", "y", "dx", "dy", "text", "key")}
    try:
        state = livebrowser.act(current_owner_id(), action, p.get("w"), p.get("h"))
    except livebrowser.LiveError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(state)


@app.route("/api/browser/frame", methods=["GET"])
@limited("browser-frame", LIMIT_LIVE_FRAME, key=_limit_key_account)
def live_browser_frame():
    if not livebrowser.available():
        return ("", 204)
    try:
        img, state = livebrowser.frame(current_owner_id())
    except livebrowser.LiveError as e:
        return jsonify({"error": str(e)}), 400
    if img is None:
        return ("", 204)
    resp = Response(img, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Page-Url"] = urllib.parse.quote(state.get("url") or "", safe=":/?&=#%")
    resp.headers["X-Page-Title"] = urllib.parse.quote(state.get("title") or "")
    resp.headers["X-Page-Size"] = "%dx%d" % (state.get("w") or 0, state.get("h") or 0)
    return resp


_SEARCH_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>%(title)s</title><style>
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
color:#1f1f1f;background:#fff}header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ececec;
padding:14px 20px;display:flex;gap:14px;align-items:center}.brand{font-weight:700;letter-spacing:-.02em;
white-space:nowrap}form{flex:1;max-width:620px;display:flex}input{flex:1;font:inherit;padding:9px 16px;
border:1px solid #dcdcdc;border-radius:999px;outline:none}input:focus{border-color:#999}
main{max-width:680px;padding:12px 20px 48px}.r{padding:14px 0}.u{font-size:12.5px;color:#4d5156;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.t{display:block;font-size:18px;color:#1a0dab;
text-decoration:none;margin:2px 0}.t:hover{text-decoration:underline}.s{color:#474747;font-size:14px}
.none{color:#666;padding:24px 0}</style></head><body>
<header><span class="brand">RandomGenerals</span><form action="/browser/search" method="get">
<input name="q" value="%(q)s" placeholder="Search the web" autocomplete="off"></form></header>
<main>%(body)s</main></body></html>"""


# Every live browser's searches arrive from this server's own address,
# so this limit is shared by everyone browsing: generous.
LIMIT_BROWSER_SEARCH = Limiter(rate=120, per=60, burst=30)


@app.route("/browser/search", methods=["GET"])
@limited("browser-search", LIMIT_BROWSER_SEARCH, key=_limit_key_ip)
def browser_search_page():
    """The results page the live browser opens for a typed search.

    Search engines refuse a browser running in a data centre (Bing shows
    an empty page, DuckDuckGo an error), so searches go through the same
    search the AI uses, and each result opens as the real site."""
    import html as _html
    q = (request.args.get("q") or "").strip()[:300]
    items = []
    if q:
        try:
            results = websearch.search(q, max_results=10) or []
        except Exception:                              # noqa: BLE001
            results = []
        for r in results:
            url = browser._safe_link("https://example.invalid/", r.get("url") or "")
            if not url:
                continue
            items.append(
                '<div class="r"><div class="u">%s</div><a class="t" href="%s">%s</a>'
                '<div class="s">%s</div></div>' % (
                    _html.escape(url), _html.escape(url, quote=True),
                    _html.escape(r.get("title") or url), _html.escape(r.get("snippet") or "")))
    body = "".join(items) if items else (
        '<p class="none">%s</p>' % ("No results for that search." if q else "Type a search above."))
    page = _SEARCH_PAGE % {"title": _html.escape((q + " - Search") if q else "Search"),
                           "q": _html.escape(q, quote=True), "body": body}
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# Searches typed in the live browser open the page above, on the public
# address the browser can reach (a local dev server keeps the default).
if PUBLIC_SITE_URL.startswith("https://"):
    livebrowser.SEARCH_URL = PUBLIC_SITE_URL + "/browser/search?q=%s"


@app.route("/api/browser/close", methods=["POST"])
def live_browser_close():
    livebrowser.close(current_owner_id())
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
@limited("chat", LIMIT_CHAT, key=_limit_key_account)
def chat():
    payload = request.get_json(force=True, silent=True) or {}
    tid = payload.get("thread_id")
    provider = (payload.get("provider") or "ollama").strip().lower()
    model = (payload.get("model") or "").strip()
    user_message = (payload.get("message") or "").strip()
    web_results = _sanitize_web_results(payload.get("web_results"))
    files = _sanitize_attachments(payload.get("attachments"))
    # The page open in the workspace browser, if the person kept it
    # attached (static/js/browser.js). Saved on their turn, so a
    # regenerate reads the same page.
    page_url = str(payload.get("page_url") or "").strip()[:2000]
    if not re.match(r"^https?://", page_url):
        page_url = ""
    strength = payload.get("strength") or DEFAULT_STRENGTH
    if strength not in STRENGTH_LEVELS:
        strength = DEFAULT_STRENGTH

    if tid not in THREADS or THREADS[tid].get("owner_id") != current_owner_id():
        return jsonify({"error": "Unknown thread"}), 400
    if not user_message:
        return jsonify({"error": "Empty message"}), 400
    if provider not in PROVIDER_STREAMERS:
        return jsonify({"error": f"Unknown provider '{provider}'"}), 400
    if not model:
        model = _default_model_for(current_owner_id())
    if not model and _groq_only():
        # The only channel is always the answer, even in the minute its
        # budget is spent - the reply waits it out rather than failing.
        provider, model = "groq", _groq_model_for("code") or groq_api.PREFERRED[0]
    if not model:
        return jsonify({"error": "No model selected"}), 400
    account_credits, save_account = current_account()
    # Premium models are enforced here, not in the browser. The frontend
    # hides them from free accounts, but the model name arrives in the
    # request body and a hand-crafted POST could name any of them.
    if not features.model_allowed(account_credits.get("plan"), model):
        return jsonify({
            "error": f"{describe_model(model)['name']} is a Pro model. "
            "Upgrade in Settings, or pick one of the free models.",
            "upgrade_required": True,
        }), 402
    apply_refill(account_credits)
    save_account()
    if account_credits["balance"] < CREDIT_COST_CHAT:
        return jsonify({
            "error": "Out of credits. They'll refill automatically, or "
                     "upgrade to Pro in Settings for a bigger, faster pool.",
            "credits": credits_view(account_credits),
        }), 402

    thread = THREADS[tid]

    user_msg: dict[str, Any] = {
        "role": "user", "content": user_message, "type": "text",
        "kind": "text",
    }
    if files:
        user_msg["attachments"] = files
    if page_url:
        user_msg["page"] = {"url": page_url}

    # Content filter FIRST - before the turn is stored, before it can be
    # captured as a memory. moderation.py's docstring always said the
    # check ran before the message was added to the thread; for a while
    # the order was the other way round, so a blocked question was kept
    # as an ordinary turn and sent to the model on the very next
    # message. It is still kept - the person should see what they
    # asked, above the refusal, when they come back - but as "blocked",
    # which _model_history leaves out. No credits: nothing was generated.
    blocked = moderation.check_message(user_message)
    if blocked is not None:
        user_msg["kind"] = "blocked"
        with DATA_LOCK:
            thread["messages"].append(user_msg)
            if thread["title"] == "New chat":
                thread["title"] = user_message[:40]
        return _canned_reply(thread, blocked, kind="refusal")

    memory_stored = maybe_capture_memory(current_owner_id(), user_message)
    with DATA_LOCK:
        thread["messages"].append(user_msg)
        if thread["title"] == "New chat":
            thread["title"] = user_message[:40]

    # Someone asked to remember something and their memory is full.
    # Saying so beats silently dropping it - they'd otherwise believe it
    # was stored and only find out when it wasn't recalled.
    if memory_stored is False:
        limit = features.get(
            current_account()[0].get("plan"), "max_memories")
        return _canned_reply(
            thread,
            f"I couldn't save that - free accounts remember up to {limit} "
            "things and yours is full. Remove one in Settings > Memory, "
            "or upgrade to Pro for unlimited memory.",
            kind="notice",
        )

    return _stream_reply(thread, provider, model, web_results, files, strength)


@app.route("/api/threads/<tid>/regenerate", methods=["POST"])
@limited("chat", LIMIT_CHAT, key=_limit_key_account)
def regenerate(tid):
    """Drops the last reply and asks the same question again - same
    history, same attachments/sources it originally had, fresh output."""
    payload = request.get_json(force=True, silent=True) or {}
    provider = (payload.get("provider") or "ollama").strip().lower()
    model = (payload.get("model") or "").strip()
    strength = payload.get("strength") or DEFAULT_STRENGTH
    if strength not in STRENGTH_LEVELS:
        strength = DEFAULT_STRENGTH

    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Unknown thread"}), 404
    if provider not in PROVIDER_STREAMERS:
        return jsonify({"error": f"Unknown provider '{provider}'"}), 400
    if not model:
        model = _default_model_for(current_owner_id())
    if not model and _groq_only():
        # The only channel is always the answer, even in the minute its
        # budget is spent - the reply waits it out rather than failing.
        provider, model = "groq", _groq_model_for("code") or groq_api.PREFERRED[0]
    if not model:
        return jsonify({"error": "No model selected"}), 400
    if not thread["messages"] or thread["messages"][-1]["role"] != "assistant":
        return jsonify({"error": "Nothing to regenerate yet."}), 400
    # The same gate /api/chat applies. It was missing here, so a
    # hand-made POST to /regenerate naming a Pro model ran it on a free
    # account - the one route that skipped the check was the one that
    # replays exactly the same request.
    if not features.model_allowed(current_account()[0].get("plan"), model):
        return jsonify({
            "error": f"{model} is a Pro model. Upgrade in Settings, or "
            "pick one of the free models.",
            "upgrade_required": True,
        }), 402
    # A refusal to a blocked question is not a reply to try again:
    # the question itself is left out of what the model sees, so
    # "regenerating" would ask it to answer nothing.
    if (len(thread["messages"]) > 1
            and thread["messages"][-2].get("kind") == "blocked"):
        return jsonify({"error": "That message was blocked. Edit it and "
                                 "send it again instead."}), 400

    account_credits, save_account = current_account()
    apply_refill(account_credits)
    save_account()
    if account_credits["balance"] < CREDIT_COST_CHAT:
        return jsonify({
            "error": "Out of credits. They'll refill automatically, or "
                     "upgrade to Pro in Settings for a bigger, faster pool.",
            "credits": credits_view(account_credits),
        }), 402

    with DATA_LOCK:
        old_reply = thread["messages"].pop()
        last_user = thread["messages"][-1] if thread["messages"] else None
    web_results = old_reply.get("sources") or []
    files = (last_user.get("attachments") if last_user and
             last_user.get("role") == "user" else None) or []
    save_thread(thread)

    return _stream_reply(thread, provider, model, web_results, files, strength)


if __name__ == "__main__":
    # APP_DEBUG lets a second, public-facing instance (behind a tunnel)
    # run with Werkzeug's debugger OFF - it's an interactive Python
    # console on error pages, fine on localhost, a remote-code-execution
    # hole if anyone outside can reach it. Normal local dev is untouched
    # (defaults to debug on, port 5000); a tunnel instance sets
    # APP_DEBUG=0 and a different PORT instead.
    debug = os.environ.get("APP_DEBUG", "1") != "0"
    port = int(os.environ.get("PORT", "5000"))
    # threaded=True matters more now than it used to: a slow request
    # (CPU image generation can take a minute) would otherwise block
    # every other visitor's chat until it finished - Werkzeug's dev
    # server defaults to handling one request at a time.
    #
    # use_reloader is explicitly off, in both modes. Auto-restart on file
    # save sounds convenient, but Werkzeug's reloader watches every
    # *imported* module's file, not just this app's own - and
    # imagegen.py pulls in torch/diffusers/transformers, which lazily
    # import several hundred submodule files while the image pipeline
    # loads. Each one gets treated as a newly-discovered watched file and
    # triggers a full restart, so any image-generation activity on a
    # debug=True instance caused a restart storm that killed unrelated
    # in-flight chat requests. Restarting manually after an edit is more
    # reliable than that.
    run_kwargs = {"debug": debug, "port": port, "threaded": True,
                  "use_reloader": False}
    app.run(**run_kwargs)
