from flask import (Flask, render_template, request, Response, jsonify,
                   session, stream_with_context, redirect, url_for)
from dotenv import load_dotenv
import json
import os
import re
import uuid
import threading
import datetime
import urllib.parse
from typing import Any, TYPE_CHECKING

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
# configured. GEMINI_API_KEY could never have worked either.
load_dotenv()

import db  # noqa: E402  SQLite persistence - see db.py for the schema and why
import websearch  # noqa: E402  keyless web search - see websearch.py
import attachments  # noqa: E402  upload handling/text extraction
import imagegen  # noqa: E402  local text-to-image generation
import codeexec  # noqa: E402  sandboxed Python execution - see codeexec.py
import moderation  # noqa: E402  content filtering - see moderation.py
import features  # noqa: E402  per-tier feature flags - see features.py
import gemini  # noqa: E402  cloud fallback for when this machine is off
import paddle_billing  # noqa: E402  subscriptions where Stripe can't reach

# The one AI this app talks to: Ollama, running locally (llama3.2 pulled
# already). Runs fully on this machine - no cloud call, no API key - but
# unlike the from-scratch model in brain/, it's a real large language
# model and can actually hold a conversation, answer questions, and write
# working code. brain/ is untouched on disk; this app just isn't wired to
# it any more.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Billing - real subscription charges via Stripe Checkout, when a Stripe
# account is configured. Without keys, upgrading stays the old mock
# instant-flip (no card touched) so the app still works before a Stripe
# account exists. See .env for what to set and where to get it.
# stripe is an optional dependency: the app runs fine without it, just
# with billing disabled. The import is split by TYPE_CHECKING because a
# plain `stripe = None` fallback makes a type checker infer the module
# as `Module | None`, which then flags every single stripe.* call as a
# possible attribute-of-None error. At type-check time it's the real
# module; at runtime the fallback still applies, and billing_live()
# below is what actually guards every use.
if TYPE_CHECKING:
    import stripe
else:
    try:
        import stripe
    except ImportError:
        stripe = None

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


def billing_live():
    """Whether real charges can actually be taken.

    Note the placeholder check: a key that only *looks* like a key is
    treated as absent, so the app reports "not configured" (true and
    actionable) rather than failing mid-checkout.
    """
    return bool(
        stripe
        and not _is_placeholder(STRIPE_SECRET_KEY)
        and not _is_placeholder(STRIPE_PRICE_ID_PRO)
    )


def billing_config_problem():
    """A specific description of what's missing, for the UI to show
    instead of a generic failure. -> None when billing is ready."""
    if stripe is None:
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


if billing_live():
    stripe.api_key = STRIPE_SECRET_KEY

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

# Session cookie hardening. HttpOnly keeps the login cookie away from
# any JS that might get injected into a page; SameSite=Lax stops another
# site silently issuing authenticated requests as this user. Secure is
# conditional: forcing it on a plain-http localhost dev run would stop
# the cookie being set at all and break sign-in locally.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS_COOKIES") == "1",
)

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
    "before presenting code, not after. Always use fenced markdown code "
    "blocks with the language name (e.g. ```python) for any real code, "
    "and make sure it's complete and runnable, not pseudocode, unless "
    "the user asked for an outline. Prefer concise explanations over "
    "long prose. Ask a clarifying question only if the request is "
    "genuinely ambiguous."
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
    "quick": {
        "options": {"num_predict": 320},
        "nudge": "Keep the answer brief and to the point.",
    },
    "deep": {
        "options": {"num_predict": 2048, "temperature": 0.15},
        "nudge": "Work through this thoroughly - consider multiple "
                 "angles, check your own reasoning for mistakes, use any "
                 "search results provided - then give a complete, "
                 "carefully-reasoned final answer.",
    },
}
DEFAULT_STRENGTH = "quick"

CHAT_SYSTEM_PROMPT = (
    "You are RandomGenerals AI, a friendly and knowledgeable general-purpose "
    "assistant. Answer clearly and conversationally. Match your length to "
    "the question - short answers for simple questions, more detail when "
    "it's actually warranted. Don't force markdown structure (headers, "
    "bullet lists) onto answers that read fine as plain prose. If code "
    "would genuinely help, use a fenced code block.\n\n"
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
#   pro  - $1.99/mo (mock checkout, no real payment processor wired up),
#          3000 credits, auto-refills every 30 minutes
# Guests (not signed in) get the free plan's allowance too, tracked in the
# shared credits.json so the app still works without an account.
# ----------------------------------------------------------------------
CREDIT_COST_CHAT = 1  # floor cost, and the pre-flight "any balance at all" gate
CREDIT_COST_IMAGE = 20  # flat - there's no output-length signal like token count here
# Gemini costs the app owner real money past their free API quota, unlike
# the local model which only costs CPU time - priced higher to reflect
# that, not arbitrarily.
CREDIT_COST_IMAGE_GEMINI = 40

# Real cost scales with how much the model actually had to generate -
# Ollama reports the real token count per reply, so a one-line answer and
# a 900-line refactor don't cost the same. 40 tokens/credit is a rough
# calibration, not a real compute-cost measurement; adjust freely.
CREDIT_TOKENS_PER_UNIT = 40


def usage_based_cost(eval_count):
    if not eval_count:
        return CREDIT_COST_CHAT
    return max(CREDIT_COST_CHAT, round(eval_count / CREDIT_TOKENS_PER_UNIT))


PLANS = {
    "free": {
        "label": "Free",
        "price": "$0",
        "cap": 4000,
        "refill_seconds": 60 * 60,  # 1 hour
        "blurb": "4,000 credits hourly. Fast chat model, coding model, "
                 "image generation, code execution, web search.",
    },
    "pro": {
        "label": "Pro",
        "price": "$1.99/mo",
        "cap": 10000,
        "refill_seconds": 15 * 60,  # 15 minutes
        "blurb": "Everything in Free, plus the advanced 7B model, image "
                 "understanding (vision), unlimited memory, long-form "
                 "code past 1,000 lines, and 10,000 credits every 15 min.",
    },
}
STARTING_CREDITS = PLANS["free"]["cap"]

DATA_LOCK = threading.Lock()
CREDITS_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_threads():
    data = db.load_threads()
    # Backfill "mode" on threads saved before workspaces existed. Old
    # image conversations didn't record a mode either, so infer it from
    # whether the thread actually contains an image message.
    for t in data.values():
        if "mode" not in t:
            has_image = any(m.get("type") == "image"
                            for m in t.get("messages", []))
            t["mode"] = "image" if has_image else DEFAULT_MODE
        elif t["mode"] not in VALID_MODES:
            t["mode"] = "chat"
    return data


def save_threads():
    with DATA_LOCK:
        db.save_threads(THREADS)


def load_users():
    return db.load_users()


def save_users():
    with USERS_LOCK:
        db.save_users(USERS)


THREADS = load_threads()
USERS = load_users()

# Warms up the image model in the background so the download/load time
# (first run only) doesn't land on whoever happens to ask for the first
# image - the request just waits on imagegen's own lock if it's still
# loading when they get there.
threading.Thread(target=imagegen.preload, daemon=True).start()


def public_user(user):
    """Strip the password hash before this ever reaches the frontend."""
    return {
        "id": user["id"],
        "email": user["email"],
        "plan": user["plan"],
        "created": user["created"],
    }


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
        return USERS[uid]["credits"], (lambda: save_users())

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


def spend_credits(amount):
    """Deduct credits from the current account (user or guest) if there's
    enough balance, refilling first if it's due. Returns True on success."""
    credits, save = current_account()
    with CREDITS_LOCK:
        apply_refill(credits)
        if credits["balance"] < amount:
            save()
            return False
        credits["balance"] -= amount
    save()
    return True


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


@app.route("/robots.txt")
def robots_txt():
    """What search engines may crawl.

    Only the landing page is worth indexing. /app is the application
    itself - it renders nothing useful without a session, so a crawler
    would file an empty shell under the site's name. /api/ is machine
    endpoints. The upload and generated directories hold users' own
    files and images, which must never turn up in search results.

    The sitemap line is built from the live host rather than hardcoded,
    so this stays correct on a .pythonanywhere.com or .onrender.com
    address as well as the real domain.
    """
    body = (
        "User-agent: *\n"
        "Allow: /$\n"
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
    """One entry, because there is one indexable page. A sitemap listing
    pages that shouldn't be indexed actively works against you."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{request.url_root}</loc>\n"
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@app.route("/")
def landing():
    """The public front door. The app itself lives at /app.

    Signed-in visitors used to be redirected straight to /app on the
    reasoning that they don't need the pitch. That was wrong: it means
    nobody who has ever logged in - the owner included - can reach the
    landing page at all without clearing cookies, which makes the
    pricing, FAQ and feature copy effectively invisible to the person
    most likely to want to check it. Everyone gets the landing page;
    the nav's "Launch app" button is one click away.
    """
    return render_template(
        "landing.html", signed_in=bool(session.get("user_id")))


@app.route("/app")
def index():
    return render_template("index.html")


# ----------------------------------------------------------------------
# Accounts - two ways in, one session cookie either way. Google sign-in
# needs the server owner's own Cloud Console setup (see GOOGLE_CLIENT_ID
# in .env), so local email/password signup exists as the path that
# always works with nothing to configure - not a second-class fallback,
# just the one that doesn't depend on an external service being wired up.
# ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _create_user(email, password_hash="", google_id=None):
    """Adds a new row to USERS and returns its id. Caller has already
    checked the email isn't taken."""
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
    return uid


def _migrate_guest_threads(uid):
    """Carries this browser's guest chats over to the now-signed-in
    account instead of orphaning them - credits don't carry over (a new
    account starts at the free-plan balance), just the conversations."""
    guest_id = session.pop("guest_id", None)
    if not guest_id:
        return
    moved = False
    for t in THREADS.values():
        if t.get("owner_id") == guest_id:
            t["owner_id"] = uid
            moved = True
    if moved:
        save_threads()


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    uid = session.get("user_id")
    if uid and uid in USERS:
        return jsonify({"user": public_user(USERS[uid])})
    return jsonify({"user": None, "google_configured": google_oauth_configured()})


@app.route("/api/auth/signup", methods=["POST"])
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

    uid = _create_user(email, password_hash=generate_password_hash(password))
    _migrate_guest_threads(uid)
    session["user_id"] = uid
    return jsonify({"user": public_user(USERS[uid])})


@app.route("/api/auth/login", methods=["POST"])
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
    if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
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

    user = next((u for u in USERS.values() if u["email"] == email), None)
    if user:
        uid = user["id"]
        # A local-signup account signing in with Google for the first
        # time - link it rather than silently ignoring the Google id.
        if not user.get("google_id"):
            user["google_id"] = info.get("sub")
            save_users()
    else:
        uid = _create_user(email, google_id=info.get("sub"))

    _migrate_guest_threads(uid)
    session["user_id"] = uid
    return redirect("/app")


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user_id", None)
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


def _apply_plan(user, plan):
    user["plan"] = plan
    user["credits"]["plan"] = plan
    user["credits"]["balance"] = PLANS[plan]["cap"]
    user["credits"]["starting"] = PLANS[plan]["cap"]
    user["credits"]["last_refill"] = now_iso()
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
        # Paddle first when it's configured. It is a merchant of record,
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
                # success before anyone had paid.
                return_url=request.host_url.rstrip("/") + "/app",
            )
            if err:
                return jsonify({"error": f"Could not start checkout: {err}"}), 502
            return jsonify({"checkout_url": url})

        if billing_live():
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
                return jsonify({"error": f"Could not start checkout: {e}"}), 502
            return jsonify({"checkout_url": checkout.url})

        if not ALLOW_MOCK_UPGRADE:
            # Say precisely what's wrong rather than a generic "not
            # configured" - "your key is a placeholder" and "you haven't
            # set a key" need different actions from whoever runs this.
            return jsonify({
                "error": "Payments aren't set up on this server yet, so Pro "
                         "can't be purchased.",
                "detail": billing_config_problem(),
                "setup_url": "https://dashboard.stripe.com/apikeys",
            }), 503

    _apply_plan(user, plan)
    save_users()
    return jsonify({"user": public_user(user), "credits": credits_view(user["credits"])})


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
    if not billing_live():
        return jsonify({"error": "Billing isn't configured on this server."}), 503

    customer_id = USERS[uid].get("stripe_customer_id")
    if not customer_id:
        return jsonify({"error": "No billing account yet - subscribe first."}), 400

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
    return jsonify({
        "plan": user["plan"],
        "plan_label": PLANS[user["plan"]]["label"],
        "price": PLANS[user["plan"]]["price"],
        "status": user.get("subscription_status"),
        "current_period_end": user.get("current_period_end"),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "billing_live": billing_live(),
        "has_billing_account": bool(user.get("stripe_customer_id")),
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
    ok, reason = paddle_billing.verify_webhook(
        request.get_data(), request.headers.get("Paddle-Signature", ""))
    if not ok:
        # 400 rather than 403: Paddle retries on 5xx, and a request that
        # failed verification will fail identically every time, so
        # asking them to retry it forever helps nobody.
        return jsonify({"error": f"Rejected: {reason}"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    event = paddle_billing.parse_event(payload)
    if not event:
        # Acknowledged, not acted on. Returning an error for events we
        # simply don't handle would make Paddle retry them and
        # eventually disable the destination.
        return jsonify({"received": True, "handled": False})

    # user_id comes from custom_data set at checkout, which is the only
    # reliable link back to a local account - matching on email breaks
    # as soon as someone pays with a different address than they signed
    # up with.
    user = USERS.get(str(event.get("user_id") or ""))
    if not user:
        return jsonify({"received": True, "handled": False,
                        "reason": "unknown user"})

    if event["active"]:
        if user["plan"] != "pro":
            _apply_plan(user, "pro")
    elif user["plan"] != "free":
        _apply_plan(user, "free")

    user["paddle_subscription_id"] = event.get("subscription_id")
    user["paddle_customer_id"] = event.get("customer_id")
    user["subscription_status"] = event.get("status")
    user["cancel_at_period_end"] = event.get("cancel_at_period_end", False)
    if event.get("current_period_end"):
        user["current_period_end"] = event["current_period_end"]
    save_users()

    return jsonify({"received": True, "handled": True})


@app.route("/api/health", methods=["GET"])
def health():
    """Liveness + capability probe.

    Deliberately cheap: no database write, no model call. It's polled by
    the deployment's health check and by the UI's status indicator, so
    it has to stay fast enough to call often.

    Two possible sources: Ollama on this machine, and Gemini as a
    fallback for when this machine is off. `mode` says which one a
    request would actually reach right now.
    """
    local = ollama_reachable()
    cloud = gemini.configured()
    return jsonify({
        "status": "ok",
        "time": now_iso(),
        "compute": {
            "local_models": local,
            "cloud_models": cloud,
        },
        "mode": "local" if local else ("cloud" if cloud else "unavailable"),
    })


@app.route("/api/plans", methods=["GET"])
def get_plans():
    return jsonify({
        "plans": PLANS,
        "billing_live": billing_live() or paddle_billing.configured(),
        "processor": "paddle" if paddle_billing.configured() else "stripe",
        # Safe to publish: Paddle client tokens are designed to ship
        # in the page. They can start a checkout and nothing else -
        # the API key, which can read and refund, never leaves here.
        "paddle_client_token": paddle_billing.client_token(),
        "paddle_environment": paddle_billing.environment(),
    })


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
    # keys absent (self-hosting, development).
    user = next((u for u in USERS.values()
                 if u.get("stripe_subscription_id") == key), None)
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
@app.route("/api/providers", methods=["GET"])
def list_providers():
    providers = [ollama_provider()]
    # Only advertised when a key exists, so the picker never offers a
    # channel that would immediately fail.
    if gemini.configured():
        providers.append({
            "id": "gemini",
            "label": "Cloud",
            "available": True,
            "models": gemini.models(),
            "model_info": [describe_model(m) for m in gemini.models()],
            "note": "Used automatically when this machine is off. Runs "
                    "off-device, so prompts leave it.",
        })
    return jsonify({
        "providers": providers,
        "gemini_configured": imagegen.gemini_configured(),
    })


# Product names for the models, so the picker reads like a product
# rather than a list of upstream vendor SKUs. Purely presentational -
# `id` is always the real model name the API is called with, and the
# About tab still states plainly which open models are underneath. The
# aim is a clear capability ladder, not hiding what this is built on.
MODEL_DISPLAY_NAMES = {
    # local
    "llama3.2": ("Swift", "Fastest replies, lighter reasoning"),
    "qwen2.5-coder": ("Coder", "Tuned for writing and debugging code"),
    "qwen2.5": ("Core", "Best local accuracy for general questions"),
    "llava": ("Vision", "Can see and describe attached images"),
    # cloud
    "openai/gpt-oss-120b": ("Max", "Strongest overall - large cloud model"),
    "openai/gpt-oss-20b": ("Swift Cloud", "Fast cloud replies"),
    "qwen/qwen3.6-27b": ("Core Cloud", "Balanced cloud model"),
    "allam-2-7b": ("Arabic", "Tuned for Arabic language"),
}


def describe_model(model_id):
    """-> {id, name, blurb}. Falls back to the raw id for anything not in
    the table, so a model the user pulls themselves still appears."""
    key = model_id.split(":")[0].strip().lower()
    name, blurb = MODEL_DISPLAY_NAMES.get(
        model_id, MODEL_DISPLAY_NAMES.get(key, (None, None)))
    return {
        "id": model_id,
        "name": name or model_id,
        "blurb": blurb or "",
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
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        ok = r.ok and bool(r.json().get("models"))
    except requests.exceptions.RequestException:
        ok = False
    _ollama_health.update({"at": now, "ok": ok})
    return ok


def _best_cloud_model(mode, available):
    """Which cloud model stands in for the local one this bay uses.

    First match wins, falling back to whatever the catalogue offers, so
    this can never return nothing and strand a request that has already
    decided to use the cloud.
    """
    preferred = {
        # Flash over Pro on purpose: the free tier's daily request count
        # is the scarce resource here, not quality, and Flash is fast
        # enough that a fallback does not feel like a downgrade.
        "code": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro"],
        "chat": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro"],
    }.get(mode, ["gemini-2.5-flash", "gemini-2.0-flash"])
    for m in preferred:
        if m in available:
            return m
    return available[0]


def ollama_provider():
    """The locally-hosted models. Kept as a function rather than a
    hardcoded dict so /api/providers reflects reality if Ollama isn't
    running or has no models pulled."""
    models, available = [], False
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        available = True
    except requests.exceptions.RequestException:
        pass

    return {
        "id": "ollama",
        "label": "On this machine",
        "available": available and bool(models),
        "models": models,
        "model_info": [describe_model(m) for m in models],
        "note": None if models else
        "The local models aren't running right now - cloud is being used "
        "instead.",
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
    summary = [
        {"id": tid, "title": t["title"], "updated": t["updated"],
         "mode": t.get("mode", DEFAULT_MODE)}
        for tid, t in sorted(items, key=lambda x: x[1]["updated"], reverse=True)
    ]
    return jsonify({"threads": summary})


@app.route("/api/threads", methods=["POST"])
def create_thread():
    payload = request.get_json(force=True, silent=True) or {}
    mode = payload.get("mode") if payload.get(
        "mode") in VALID_MODES else DEFAULT_MODE
    tid = str(uuid.uuid4())
    THREADS[tid] = {
        "title": "New chat",
        "messages": [],
        "updated": now_iso(),
        "mode": mode,
        "owner_id": current_owner_id(),
    }
    save_threads()
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


@app.route("/api/threads/<tid>", methods=["DELETE"])
def delete_thread(tid):
    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Not found"}), 404
    THREADS.pop(tid, None)
    save_threads()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# Streaming chat generator. One provider, Ollama - yields plain text
# chunks as the model produces them.
# ----------------------------------------------------------------------
_VISION_MODEL_HINTS = ("vision", "llava", "moondream", "minicpm-v", "bakllava")


def is_vision_model(model):
    name = (model or "").lower()
    return any(hint in name for hint in _VISION_MODEL_HINTS)


def stream_ollama(model, history, options=None, images=None, usage=None):
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
        "keep_alive": "30m",
    }
    if options:
        body["options"] = options
    with requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=body,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                yield f"[Ollama error: {chunk['error']}]"
                return
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
            if chunk.get("done"):
                if usage is not None:
                    usage["eval_count"] = chunk.get("eval_count")
                return


def tool_event(payload):
    """A structured event smuggled through the plain-text reply stream.

    The reply is streamed as text and the browser appends whatever
    arrives straight into the message bubble, so "searching the web..."
    can't just be yielded - it would become part of the answer. Wrapping
    events in U+001E (RECORD SEPARATOR) gives the frontend something it
    can split on and pull out: the character has no visual form, no
    meaning in markdown, and a language model will never emit one.
    """
    return "\x1e" + json.dumps(payload, separators=(",", ":")) + "\x1e"


PROVIDER_STREAMERS = {
    "ollama": stream_ollama,
    "gemini": gemini.stream_chat,
}


@app.route("/api/web-search", methods=["POST"])
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
    uploaded = request.files.getlist("files")
    results = [
        attachments.save_and_extract(f)
        for f in uploaded[:10] if f and f.filename
    ]
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


def _attachment_context_block(files, vision_available):
    text_files = [f for f in files if f["kind"] == "text" and f["text"]]
    image_files = [f for f in files if f["kind"] == "image"]
    bad_files = [f for f in files if f["kind"] == "unsupported"]

    blocks = []
    if text_files:
        parts = ["Files attached to this message:"]
        for f in text_files:
            parts.append(f"--- {f['filename']} ---\n{f['text']}")
        blocks.append("\n".join(parts))
    if image_files and not vision_available:
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
# Image generation - a small local diffusion model (see imagegen.py),
# not Ollama - Ollama only serves LLMs. Runs on CPU so it never fights
# the chat models for the GPU's limited VRAM; slower, but reliable.
# ----------------------------------------------------------------------
@app.route("/api/generate-image", methods=["POST"])
def generate_image_route():
    payload = request.get_json(force=True, silent=True) or {}
    tid = payload.get("thread_id")
    prompt = (payload.get("prompt") or "").strip()
    backend = "gemini" if payload.get("backend") == "gemini" else "local"
    if backend == "gemini" and not imagegen.gemini_configured():
        backend = "local"
    cost = CREDIT_COST_IMAGE_GEMINI if backend == "gemini" else CREDIT_COST_IMAGE

    thread = THREADS.get(tid)
    if not thread or thread.get("owner_id") != current_owner_id():
        return jsonify({"error": "Unknown thread"}), 400
    if not prompt:
        return jsonify({"error": "Describe what you want to see."}), 400

    blocked = moderation.check_message(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400

    account_credits, save_account = current_account()
    apply_refill(account_credits)
    save_account()
    if account_credits["balance"] < cost:
        return jsonify({
            "error": "Out of credits for image generation. They'll refill "
                     "automatically, or upgrade to Pro in Settings.",
            "credits": credits_view(account_credits),
        }), 402

    thread["messages"].append(
        {"role": "user", "content": prompt, "type": "text"})
    if thread["title"] == "New chat":
        thread["title"] = prompt[:40]

    url, error = imagegen.generate_image(prompt, backend=backend)

    if error:
        thread["updated"] = now_iso()
        save_threads()
        return jsonify({"error": error}), 502

    thread["messages"].append({
        "role": "assistant",
        "content": url,
        "type": "image",
        "provider": "imagegen",
        "model": imagegen.GEMINI_IMAGE_MODEL if backend == "gemini" else imagegen.MODEL_ID,
    })
    thread["updated"] = now_iso()
    save_threads()
    spend_credits(cost)

    # Not echoing a `credits` field here - spend_credits() re-fetches its
    # own account copy (a fresh dict per call for guests, see
    # current_account()), so account_credits above is stale by now. The
    # frontend re-fetches /api/credits itself right after, same as it
    # does following a normal chat reply.
    return jsonify({"url": url})


# ----------------------------------------------------------------------
# Code execution - runs AI-written Python in a locked-down subprocess
# (see codeexec.py for exactly what that does and doesn't protect
# against). Gated to signed-in accounts only, not guests: this app is
# reachable over a public tunnel right now and there's no real network
# isolation without a container, so requiring an account is the actual
# damage-control measure here, not a formality.
# ----------------------------------------------------------------------
@app.route("/api/run-code", methods=["POST"])
def run_code_route():
    if not session.get("user_id"):
        return jsonify({"error": "Sign in to run code."}), 401

    payload = request.get_json(force=True, silent=True) or {}
    code = payload.get("code") or ""
    if len(code) > 20000:
        return jsonify({"error": "That's too long to run here."}), 400

    result: dict[str, Any] = codeexec.run_python(code)
    if "error" in result and result["error"]:
        return jsonify(result), 502
    result["timeout"] = codeexec.TIMEOUT_SECONDS
    return jsonify(result)


def _canned_reply(thread, text):
    """Appends `text` as an assistant message and returns it through the
    same streaming Response shape /api/chat normally returns, just
    yielded in one piece instead of generated token by token - so the
    frontend's existing stream-reading code handles it with no special
    casing, and it persists/reloads like any other reply. Used for
    content-filter blocks, which don't touch the model at all."""
    thread["messages"].append({
        "role": "assistant", "content": text, "type": "text",
        "provider": "filter", "model": "content-filter",
    })
    thread["updated"] = now_iso()
    save_threads()

    def generate():
        yield text

    return streaming_response(generate())


def _stream_reply(thread, provider, model, web_results, files, strength):
    """Builds the system prompt + history from thread["messages"] as it
    currently stands, streams a reply, and persists + charges for it once
    done. Shared by /api/chat (appends the new user turn first) and
    /regenerate (replays on the existing history minus the old reply) so
    the two can't quietly drift apart."""
    mode = thread.get("mode", DEFAULT_MODE)
    vision_available = is_vision_model(model)
    images_b64 = [
        b64 for f in files if f["kind"] == "image" and vision_available
        for b64 in [attachments.encode_image_base64(f["url"])] if b64
    ]

    system_prompt = CODING_SYSTEM_PROMPT if mode == "code" else CHAT_SYSTEM_PROMPT
    owner_id = current_owner_id()
    memories = db.load_memories(owner_id)
    custom_instructions = db.load_custom_instructions(owner_id)
    if memories or custom_instructions:
        system_prompt = system_prompt + "\n\n" + _memory_context_block(
            memories, custom_instructions) + "\n\n" + MEMORY_ACK_NUDGE
    if web_results:
        system_prompt = system_prompt + "\n\n" + \
            _web_context_block(web_results)
    if files:
        system_prompt = system_prompt + "\n\n" + _attachment_context_block(
            files, vision_available)
    nudge = STRENGTH_LEVELS[strength]["nudge"]
    if nudge:
        system_prompt = system_prompt + "\n\n" + nudge

    history = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]}
        for m in thread["messages"] if m["type"] == "text"
    ]

    # Local first, always. Prompts stay on this machine whenever that is
    # possible at all, and Gemini is only reached for when it isn't -
    # Ollama not started, still loading, or the whole machine off and the
    # app running somewhere else.
    #
    # The reply is tagged so the UI can say which one answered. Falling
    # back silently would mean a user who chose local for privacy could
    # have a prompt leave the device without ever being told.
    fell_back_to_cloud = False
    if provider == "ollama" and not ollama_reachable():
        if gemini.configured():
            cloud_models = gemini.models()
            if cloud_models:
                provider = "gemini"
                model = _best_cloud_model(mode, cloud_models)
                fell_back_to_cloud = True

    if provider == "ollama" and not ollama_reachable():
        def unavailable():
            yield ("[The local AI isn't running and no cloud fallback is "
                   "configured, so there's nothing to answer with. Start "
                   "Ollama, or set GEMINI_API_KEY on the server.]")
        return streaming_response(unavailable())

    streamer = PROVIDER_STREAMERS[provider]
    # num_ctx matters more here than it looks. Without it, Ollama defaults
    # to a model's advertised max context - 131,072 for llama3.2 - which
    # alone needs an ~18GB KV cache. That doesn't fit in this machine's
    # 8GB of VRAM, so Ollama silently spilled most of the model onto CPU
    # (measured: 67% CPU / 33% GPU) to make room, which is what was
    # actually behind both the slow cold-loads and sluggish generation -
    # far more than the model-swap cost alone. 8192 tokens is generous for
    # a chat conversation and keeps the whole model comfortably inside
    # VRAM, so it runs on GPU only.
    combined_options = dict(CODE_MODEL_OPTIONS) if mode == "code" else {}
    combined_options["num_ctx"] = 8192
    combined_options.update(STRENGTH_LEVELS[strength]["options"])
    if mode == "code":
        # Quick's 320-token cap and Deep's 2048 are both prose-sized -
        # real code needs a much bigger generation budget, which is what
        # was actually behind "it doesn't know how to write 1000 lines
        # of code": it wasn't incapable, Quick mode was cutting it off
        # at roughly a page. The per-tier numbers live in features.py so
        # this isn't a second place where tier rules can drift.
        account_credits, _ = current_account()
        combined_options.update(
            features.limits_for(account_credits.get("plan")))
    stream_kwargs: dict[str, Any] = {"options": combined_options}
    if images_b64:
        stream_kwargs["images"] = images_b64
    usage = {}
    stream_kwargs["usage"] = usage

    # Model-driven tool calling is not wired up. It only ever ran on the
    # cloud provider, which has been removed - Ollama's tool support
    # varies by model, and a small local model that hallucinates a tool
    # call answers worse than one that just talks. tools.py is kept
    # because the hard parts (schemas, dispatch that never raises, size
    # caps) are done and tested, ready if a provider that supports it
    # comes back. Web search, the code sandbox and image generation all
    # still work; the user drives them from the UI as before.

    def generate():
        full_reply = ""
        try:
            for piece in streamer(model, history, **stream_kwargs):
                full_reply += piece
                yield piece
        except GeneratorExit:
            raise
        except Exception as e:
            # A backstop for anything unexpected from the streamer - a
            # broken reply still gets recorded (as a bracketed error)
            # instead of vanishing silently.
            full_reply = f"[Error talking to {provider}: {e}]"
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
                msg = {
                    "role": "assistant",
                    "content": full_reply,
                    "type": "text",
                    "provider": provider,
                    "model": model,
                }
                if fell_back_to_cloud:
                    # Recorded on the message so reloading the thread
                    # still shows it answered from the cloud - a reader
                    # shouldn't have to guess why one reply differs.
                    msg["fallback"] = True
                if web_results:
                    msg["sources"] = web_results
                thread["messages"].append(msg)
                thread["updated"] = now_iso()
                save_threads()
                # Only charge for replies that actually came back - bracketed
                # provider errors and flagged/blocked replies don't cost the
                # user anything. Cost otherwise scales with what Ollama
                # reports it actually generated, not a flat per-message fee.
                if not flagged and not full_reply.startswith("["):
                    spend_credits(usage_based_cost(usage.get("eval_count")))

    # stream_with_context keeps the request context alive for as long as the
    # generator is running. Without it Flask tears the context down as soon
    # as this function returns - which is *before* a single chunk has been
    # produced - and the `finally` block above blows up on `session` the
    # moment it tries to charge for the reply. The user still saw their
    # answer, so the failure is invisible from the browser; it just means
    # credits were silently never deducted.
    return streaming_response(generate())


@app.route("/api/chat", methods=["POST"])
def chat():
    payload = request.get_json(force=True, silent=True) or {}
    tid = payload.get("thread_id")
    provider = (payload.get("provider") or "ollama").strip().lower()
    model = (payload.get("model") or "").strip()
    user_message = (payload.get("message") or "").strip()
    web_results = _sanitize_web_results(payload.get("web_results"))
    files = _sanitize_attachments(payload.get("attachments"))
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
        return jsonify({"error": "No model selected"}), 400
    account_credits, save_account = current_account()
    # Premium models are enforced here, not in the browser. The frontend
    # hides them from free accounts, but the model name arrives in the
    # request body and a hand-crafted POST could name any of them.
    if not features.model_allowed(account_credits.get("plan"), model):
        return jsonify({
            "error": f"{model} is a Pro model. Upgrade in Settings, or "
                     "pick one of the free models.",
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
    memory_stored = maybe_capture_memory(current_owner_id(), user_message)

    user_msg = {"role": "user", "content": user_message, "type": "text"}
    if files:
        user_msg["attachments"] = files
    thread["messages"].append(user_msg)

    if thread["title"] == "New chat":
        thread["title"] = user_message[:40]

    # Content filter runs before the message ever reaches the model - see
    # moderation.py for what it actually catches and why. No credits
    # charged for a blocked request; nothing was generated.
    blocked = moderation.check_message(user_message)
    if blocked is not None:
        return _canned_reply(thread, blocked)

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
        )

    return _stream_reply(thread, provider, model, web_results, files, strength)


@app.route("/api/threads/<tid>/regenerate", methods=["POST"])
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
        return jsonify({"error": "No model selected"}), 400
    if not thread["messages"] or thread["messages"][-1]["role"] != "assistant":
        return jsonify({"error": "Nothing to regenerate yet."}), 400

    account_credits, save_account = current_account()
    apply_refill(account_credits)
    save_account()
    if account_credits["balance"] < CREDIT_COST_CHAT:
        return jsonify({
            "error": "Out of credits. They'll refill automatically, or "
                     "upgrade to Pro in Settings for a bigger, faster pool.",
            "credits": credits_view(account_credits),
        }), 402

    old_reply = thread["messages"].pop()
    web_results = old_reply.get("sources") or []
    last_user = thread["messages"][-1] if thread["messages"] else None
    files = (last_user.get("attachments") if last_user and
             last_user.get("role") == "user" else None) or []
    save_threads()

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
