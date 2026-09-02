from flask import (Flask, render_template, request, Response, jsonify,
                   session, stream_with_context, redirect, url_for, abort,
                   send_from_directory)
from dotenv import load_dotenv
import hmac
import secrets
import time
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
# configured. GROQ_API_KEY could never have worked either.
load_dotenv()

import db  # noqa: E402  SQLite persistence - see db.py for the schema and why
import websearch  # noqa: E402  keyless web search - see websearch.py
import attachments  # noqa: E402  upload handling/text extraction
import imagegen  # noqa: E402  local text-to-image generation
import codeexec  # noqa: E402  sandboxed Python execution - see codeexec.py
import moderation  # noqa: E402  content filtering - see moderation.py
import features  # noqa: E402  per-tier feature flags - see features.py
import stats  # noqa: E402  owner-only usage figures - see stats.py
import groq_api  # noqa: E402  fast open-weight models - see groq_api.py
import openai_api  # noqa: E402  OpenAI-compatible API - see openai_api.py
import connectors  # noqa: E402  pasted links, turned into tools
import keystore  # noqa: E402  provider-key encryption + TOTP
import openrouter_api  # noqa: E402  the only channel serving Kimi
import paddle_billing  # noqa: E402  subscriptions where Stripe can't reach
import pixverse  # noqa: E402  paid text-to-video - see pixverse.py
import hfvideo  # noqa: E402  free-tier text-to-video - see hfvideo.py
import tripo3d  # noqa: E402  text-to-3D generation - see tripo3d.py
import tools  # noqa: E402  model-callable tools - see tools.py
import mailer  # noqa: E402  verification email - see mailer.py

# The one AI this app talks to: Ollama, running locally (llama3.2 pulled
# already). Runs fully on this machine - no cloud call, no API key - but
# unlike the from-scratch model in brain/, it's a real large language
# model and can actually hold a conversation, answer questions, and write
# working code. brain/ is untouched on disk; this app just isn't wired to
# it any more.
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
    "genuinely ambiguous.\n\n"

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
        "options": {"num_predict": 1400, "temperature": 0.2,
                    "top_p": 0.9},
        "nudge": "Keep the answer brief and to the point - but never "
                 "stop mid-working on a calculation. If it needs steps, "
                 "take them.",
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
    # tool, because it hands the reader a reason to trust it.
    "Never say you ran code, looked something up, or checked a result "
    "unless you actually called a tool and read what it returned. If "
    "you cannot run it, do the arithmetic openly and say it was done "
    "by hand. Inventing an output and attributing it to Python is "
    "worse than an ordinary mistake: it tells the reader the number "
    "was verified when it was not.\n"

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
    "Notation: this chat renders plain text, not LaTeX. Write x^2, "
    "sqrt(x), <=, >=, !=, integral from 0 to 1, sum over i, and use a "
    "fenced block for anything that needs alignment. Emitting "
    "\\\\frac{a}{b} here produces literal backslashes on screen, not a "
    "fraction.\n"
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
#   pro  - $1.99/mo (mock checkout, no real payment processor wired up),
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
# Not simply "more credits". The thing that genuinely runs out on this
# deployment is the fast channel: Groq's ceiling is 8,000 tokens a minute
# shared across everyone on the site at once, so at a busy moment the
# scarce resource is a fast reply, not an allowance. Pro is therefore
# first in the queue for it (see _groq_has_room), and keeps answering
# quickly at exactly the times a free session drops to the local model.
#
# The credit gap widened as well - 2,000 against 25,000 - but that is the
# smaller half of the offer, and deliberately so. An allowance can be
# waited out; being the one who still gets the fast channel cannot.
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
                 "coding models, image generation, diagrams, the "
                 "sandboxed code runner, and web search.",
    },
    "pro": {
        "label": "Pro",
        "price": "$1.99/mo",
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
                 "channel when the site is busy, so your replies stay "
                 "quick while free sessions fall back to the local "
                 "model. Plus tools the model can use on its own, "
                 "image understanding, unlimited memory, long-form "
                 "code, a larger context window, 100MB uploads and "
                 "every image shape.",
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

    free_list = [
        (credits("free"), True),
        # Name the model. "Chat and coding models" told nobody what they
        # were actually getting, and the honest headline here is that the
        # free tier runs a 120-billion-parameter model on dedicated
        # accelerators - which is the single most impressive true thing
        # about this plan.
        ("<strong>gpt-oss-120b</strong> - a 120-billion-parameter model, "
         "on dedicated accelerators rather than a CPU", True),
        ("An 8,192-token context window, and replies up to %s tokens"
         % "{:,}".format(free["max_output_tokens_code"]), True),
        ("Image generation, diagrams, the code runner, web search "
         "and voice", True),
        ("Runs Python and searches the web on its own, so arithmetic "
         "and current facts are checked rather than recalled", True),
        ("Remembers up to %d things" % free["max_memories"], True),
        ("%dMB uploads" % free["max_upload_mb"], True),
    ]
    pro_list = [
        (credits("pro"), True),
        ("<strong>Priority on the fast channel</strong> - your replies "
         "stay quick while free sessions fall back to the local model",
         True),
        ("<strong>Image understanding</strong> - it sees what you attach",
         True),
        ("<strong>Connect your own apps</strong> - paste a link and it "
         "can use whatever is behind it", True),
        ("Unlimited memory - no %d-item cap" % free["max_memories"], True),
        ("Code replies up to {:,} tokens, against {:,}".format(
            pro["max_output_tokens_code"],
            free["max_output_tokens_code"]), True),
        ("{:,}-token context window, twice the free one - a long file "
         "plus its error output stops being truncated".format(
             pro["max_context_tokens"]), True),
        # Said plainly, because the alternative is implying a smarter
        # model that does not exist. Chat runs on the same gpt-oss-120b
        # either way; what Pro buys is room, sight, priority and tools.
        ("<em>The chat model is the same gpt-oss-120b on both plans - "
         "Pro buys room, sight, priority and tools, not a smarter "
         "model.</em>", True),
        ("%dMB uploads and %d image shapes, not %d" % (
            pro["max_upload_mb"], len(pro["image_sizes"]),
            len(free["image_sizes"])), True),
    ]

    # The video bay has no funded backend on this deployment, so neither
    # card mentions clips until one exists.
    if video_live:
        free_list.append(("%d video clips a day" % free["video_clips"], True))
        pro_list.append(("%d video clips a day, up to %ds at %s" % (
            pro["video_clips"], pro["video_max_seconds"],
            pro["video_max_quality"]), True))

    # Shown struck through on the free card: what upgrading buys.
    free_list += [
        ("Image understanding", False),
        ("Priority when the site is busy", False),
        ("Connecting your own apps", False),
    ]
    return {"free": free_list, "pro": pro_list}

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
                    "messages": [
                        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                        {"role": "user", "content": "hi"},
                    ],
                    # Must match what _stream_reply() sends for chat, or
                    # this warms a runner that the first request discards.
                    "options": {"num_predict": 1, "num_ctx": 8192},
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
    # Counters for the usage chart. Written here rather than at the call
    # sites so no future spender can forget to record itself.
    db.note_usage(current_owner_id(), amount)
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


@app.route("/stats")
def stats_page():
    if not ADMIN_EMAIL:
        abort(404)
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user or (user.get("email") or "").strip().lower() != ADMIN_EMAIL:
        abort(404)
    return render_template("stats.html", s=stats.collect())


@app.route("/api/stats")
def stats_json():
    """The same figures as JSON, for anything that wants to graph them
    elsewhere. Same gate - it would be a strange kind of protection that
    covered the page and not the data behind it."""
    if not ADMIN_EMAIL:
        abort(404)
    uid = session.get("user_id")
    user = USERS.get(uid) if uid else None
    if not user or (user.get("email") or "").strip().lower() != ADMIN_EMAIL:
        abort(404)
    return jsonify(stats.collect())


@app.route("/terms")
def terms_page():
    return _legal("terms", "Terms of Service", [
        {"heading": "What this is", "body": [
            "RandomGenerals AI is a hosted assistant for chat, code, image "
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
             "generate sexual content involving minors, or content that "
             "sexualises real people without consent",
             "harass, threaten or defame anyone",
             "produce malware, or material intended to attack systems you "
             "do not own",
             "impersonate a real person or organisation",
             "resell access, or run automated traffic through the service "
             "beyond ordinary personal use"],
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
             "signed session cookie that keeps you logged in."],
        ]},
        {"heading": "Where prompts actually go", "body": [
            "This matters more than the rest of the page, so it is stated "
            "plainly rather than buried.",
            "Chat and code replies come from one of two places. Where "
            "a model is running on our own server, what you type stays "
            "on it. Otherwise it is sent to <strong>Groq</strong> to "
            "produce a reply, and Groq processes it under their own "
            "terms. Which one answered is shown on each reply.",
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
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /refunds\n"
        "Allow: /contact\n"
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
    """The home page and the four legal pages - everything that is meant
    to be indexed, and nothing that is not. A sitemap listing pages that
    should not be indexed actively works against you."""
    pages = [
        ("", "weekly", "1.0"),
        ("terms", "yearly", "0.3"),
        ("privacy", "yearly", "0.3"),
        ("refunds", "yearly", "0.3"),
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
    return render_template("index.html", plan_perks=plan_perks())


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

    name, birth_year, err = _validate_profile(payload.get("name"),
                                              payload.get("age"))
    if err:
        return jsonify({"error": err}), 400

    uid = _create_user(email, password_hash=generate_password_hash(password))
    USERS[uid]["name"] = name
    USERS[uid]["birth_year"] = birth_year
    save_users()
    _start_session(uid)
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


# ----------------------------------------------------------------------
# Account management: verify, change password, delete.
#
# mailer.py and the verification_codes table were both written and
# neither was ever called - the code to send a six-digit code existed,
# and nothing asked it to. These are the routes that use them.
# ----------------------------------------------------------------------
VERIFY_TTL_MINUTES = 15


def _issue_code(email):
    """Mint a code, store it, mail it. -> (sent, detail)."""
    code = "%06d" % secrets.randbelow(1000000)
    expires = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(minutes=VERIFY_TTL_MINUTES))
    db.save_verification_code(email, code, expires.isoformat())
    return mailer.send_verification_code(email, code, VERIFY_TTL_MINUTES)


@app.route("/api/auth/verify/send", methods=["POST"])
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

    sent, detail = _issue_code(email)
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

    # A live subscription is the caller's to cancel first: deleting the
    # account here would leave Paddle billing a customer this app can
    # no longer recognise or refund.
    if (user.get("subscription_status") or "") in ("active", "trialing"):
        return jsonify({
            "error": "Cancel your Pro subscription first, so you are "
                     "not billed for an account that no longer exists.",
        }), 400

    removed = db.delete_user(uid, email)
    USERS.pop(uid, None)
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
    credits = credits_view(user["credits"])
    return jsonify({
        "plan": user["plan"],
        "plan_label": PLANS[user["plan"]]["label"],
        "price": PLANS[user["plan"]]["price"],
        "status": user.get("subscription_status"),
        "current_period_end": user.get("current_period_end"),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "billing_live": billing_live() or paddle_billing.configured(),
        # Which processor holds the card, so the panel can name it rather
        # than saying "your payment provider".
        "processor": "Paddle" if paddle_billing.configured() else "Stripe",
        # Paddle was missing here. This checked only for a Stripe customer
        # id, so on a Paddle deployment - which this one is - a paying
        # subscriber never saw the button that manages their own
        # subscription.
        "has_billing_account": bool(user.get("stripe_customer_id")
                                    or user.get("paddle_customer_id")),
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
    relay on a public URL: a stranger could spend the owner's per-minute
    Groq budget, and on a machine with Ollama running, occupy their GPU.
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
    return jsonify(openai_api.model_list(ids or ["randomgenerals"]))


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
    local = list(ollama_provider().get("models") or [])

    if requested in local:
        provider, model = "ollama", requested
    elif local and ollama_reachable():
        provider, model = "ollama", (
            next((m for m in local if "coder" in m), local[0]))
    else:
        return _api_error(
            "No model is available right now - Ollama is not answering.",
            503, "server_error")

    opts = openai_api.options_from(payload)
    opts["num_ctx"] = 16384
    streamer = PROVIDER_STREAMERS[provider]
    pieces = streamer(model, history, options=opts)

    if payload.get("stream"):
        return Response(
            stream_with_context(openai_api.stream_sse(pieces, model)),
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
    # Rough, but present: clients divide by these and some render NaN if
    # the block is missing. ~4 chars per token is close enough to be
    # useful and is clearly documented as an estimate.
    prompt_chars = sum(len(m.get("content") or "") for m in history)
    return jsonify(openai_api.completion(
        text, model,
        prompt_tokens=prompt_chars // 4,
        completion_tokens=len(text) // 4))


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

    Two possible sources: Ollama on this machine, and Groq when that is
    faster or when there is no local model at all. `mode` says which one
    a request would actually reach right now.
    """
    local = ollama_reachable()
    # A key being present is not the same as a key that works, so this
    # asks whether any model actually came back.
    cloud = bool(groq_api.configured() and groq_api.models())
    # Whether the fast channel has budget right now, which is a different
    # question from whether it is configured - and the one that explains
    # a sudden change in reply latency to anyone watching this endpoint.
    remaining, resets_in = groq_api.budget_state()
    return jsonify({
        "status": "ok",
        "time": now_iso(),
        "compute": {
            "local_models": local,
            "cloud_models": cloud,
        },
        "fast_channel": {
            "configured": cloud,
            "tokens_remaining": remaining,
            "resets_in_seconds": round(resets_in, 1),
        },
        # Which one a request would actually reach right now. "cloud"
        # only when it is both configured and has budget left, because
        # otherwise the honest answer is that local is answering.
        "mode": ("cloud" if cloud and groq_api.budget_ok()
                 else "local" if local
                 else "cloud" if cloud else "unavailable"),
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
    mailer.send_reset_code(email, code, RESET_TTL_MINUTES)
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


@app.route("/api/settings/templates", methods=["GET"])
def get_templates():
    return jsonify({"templates": db.list_templates(current_owner_id())})


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
        filename = "randomgenerals-history-%s.md" % stamp
    else:
        body = json.dumps({
            "exported": now_iso(),
            "thread_count": len(mine),
            "threads": mine,
        }, indent=2, ensure_ascii=False)
        mimetype = "application/json; charset=utf-8"
        filename = "randomgenerals-history-%s.json" % stamp

    return Response(body, mimetype=mimetype, headers={
        "Content-Disposition": 'attachment; filename="%s"' % filename,
    })


@app.route("/api/account/history", methods=["DELETE"])
def clear_history():
    """Delete every thread for this owner, now."""
    owner_id = current_owner_id()
    doomed = [tid for tid, t in THREADS.items()
              if t.get("owner_id") == owner_id]
    for tid in doomed:
        THREADS.pop(tid, None)
    save_threads()
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
    doomed = [tid for tid, t in THREADS.items()
              if t.get("owner_id") == owner_id
              and (t.get("updated") or "") < cutoff]
    for tid in doomed:
        THREADS.pop(tid, None)
    if doomed:
        save_threads()
        db.delete_threads_older_than(owner_id, cutoff)
    return len(doomed)


@app.route("/api/settings/apply-retention", methods=["POST"])
def run_retention():
    return jsonify({"ok": True, "deleted": apply_retention(current_owner_id())})


@app.route("/api/connectors", methods=["GET"])
def list_connectors():
    """What this account has connected. Tokens are never included."""
    items = db.load_connectors(current_owner_id())
    return jsonify({"connectors": [
        {"id": i["id"], "title": i["title"], "kind": i["kind"],
         "url": i["url"], "operations": len(i["operations"]),
         "has_token": i["has_token"], "created": i["created"]}
        for i in items]})


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
            "error": "Connecting apps is a Pro feature.",
        }), 403

    owner_id = current_owner_id()
    existing = db.load_connectors(owner_id)
    if len(existing) >= MAX_CONNECTORS:
        return jsonify({
            "error": "You can connect up to %d apps. Remove one first."
                     % MAX_CONNECTORS,
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
# WHICH MODEL ANSWERS WHICH BAY
#
# Ranked preferences, best first. Every entry is (provider, name-pattern)
# and the first one that is actually present and working wins, so the
# same table serves a laptop with four local models and a GPU-less VM
# with none - it simply falls further down the list.
#
# The order is set by measurement on the deployment this runs on, not by
# which model is nominally strongest. On the Oracle VM (two ARM cores, no
# GPU) a 7B answers at about 3.5 tokens a second - a 417-token code reply
# took 194 seconds - while Groq returns the same answer in about one. So
# hosted comes first for the bays where waiting is the thing you notice,
# and local sits underneath as a deliberate choice rather than a default.
#
# Vision is the exception that decides its own order: Groq's catalogue
# has no model that can see an image, so vision is local-only: llava is
# the one thing here the fast channel cannot do at all.
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
# DIFFERENT MODELS FOR DIFFERENT BAYS, CHOSEN BY MEASUREMENT
#
# Both hosted models were scored on six maths and physics problems with
# verifiable answers - rolling-body dynamics, an improper integral, dice
# probability, modular exponentiation, photon energy, SHM. Both got 6/6,
# so accuracy did not separate them and the split is made on how they
# WRITE rather than what they know:
#
#   gpt-oss-120b answers in LaTeX and \boxed{}.
#   qwen3.8-27b states results in plain prose.
#
# That measurement is why qwen was on the chat bay. The split is now made
# on ROLE instead, which is a product decision rather than a measured
# one: Kimi is the coding model and gpt-oss-120b is the chat model. Two
# models, one job each, which is easier to reason about than a third
# model that differs only in how it formats an answer - and gpt-oss is
# the one that stays free, so the free chat bay is not the paid one
# degraded.
#
# (A warning for whoever tunes this next: the first two versions of that
# benchmark scored gpt-oss-120b 3/6 twice, and both times the model was
# right and the GRADER was wrong - it could not see a bare "9" or a
# \frac{1}{8}. Check what the model actually wrote before believing a
# score.)
BAY_ROUTES = {
    "code": [
        # Kimi first, asked for by name. It is coding-tuned and it is the
        # reason openrouter_api exists - Groq serves no Moonshot model at
        # all, so this line is unreachable without OPENROUTER_API_KEY and
        # gpt-oss below carries the bay exactly as before until one is
        # set. That is deliberate: this is the one model in the table
        # that costs money per message.
        ("openrouter", "laguna-s-2.1:free"),
        ("groq", "gpt-oss-120b"),
        ("ollama", "gemma3:1b"),
        ("ollama", "gemma3"),
        ("ollama", "qwen3.5"),          # Ollama Cloud
        ("ollama", "gpt-oss"),          # Ollama Cloud
    ],
    "chat": [
        # gpt-oss-120b is the chat model. It is free on Groq, so the bay
        # most people land in costs nothing to serve and does not depend
        # on a paid key being present.
        ("groq", "gpt-oss-120b"),
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
        # Hosted first here, unlike the other bays. A local vision model
        # needs a GPU to be bearable and this deployment has none, so on
        # the server these are the only entries that can ever match -
        # while a laptop running Ollama still falls through to gemma3:4b
        # below and keeps working offline.
        ("openrouter", "gemma-4-31b-it:free"),
        ("openrouter", "minimax-m3:free"),
        ("ollama", "gemma3:4b"),
        ("ollama", "gemma3"),
        ("ollama", "llava"),
    ],
}


def _vision_route():
    """A provider/model pair that can read an image. -> (provider, model).

    (None, None) when nothing available can see one, which is the honest
    answer on a server with no GPU and no OpenRouter key - and the
    caller then leaves the model as it was, so the reply explains itself
    rather than pretending the attachment was never there.
    """
    for provider_id, pattern in BAY_ROUTES["vision"]:
        if provider_id == "openrouter":
            if not (openrouter_api.configured()
                    and openrouter_api.budget_ok()):
                continue
            match = next((m for m in openrouter_api.models()
                          if pattern in m.lower()), None)
            if match:
                return "openrouter", match
        elif provider_id == "ollama":
            if not ollama_reachable():
                continue
            names = ollama_provider().get("models") or []
            match = next((n for n in names if pattern in n.lower()), None)
            if match:
                return "ollama", match
    return None, None


def _local_alternative(mode):
    """The local model that should answer instead of the fast channel.

    Reads the same BAY_ROUTES table rather than hardcoding a name, so
    the fallback and the primary can never drift apart, and returns None
    when Ollama has nothing to offer - which the caller must handle,
    since falling back to nothing is not falling back.
    """
    if not ollama_reachable():
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


def _groq_has_room(plan):
    """Whether this plan may spend the shared per-minute budget now.

    Pro skips the reserve. That is a real benefit rather than a cosmetic
    one: when the site is busy the fast channel is precisely what runs
    out, so being the one who still gets it is worth more than any
    number of extra credits.
    """
    return groq_api.budget_ok(priority=(plan == features.PRO))


# Safe here: BAY_ROUTES above is what this reads.
threading.Thread(target=_warm_providers, daemon=True).start()


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
        # Vision is the case that matters: Groq has no model that can see
        # an image, so llava is not merely preferred there, it is the only
        # option - and skipping it left the bay with no route at all.
        has_alternative = any(
            pid != "ollama" and pid in by_id for pid, _ in ranked)
        for provider_id, pattern in ranked:
            if skip_local and has_alternative and provider_id == "ollama":
                continue
            provider = by_id.get(provider_id)
            if not provider:
                continue
            match = next(
                (m for m in provider["models"]
                 if pattern in m.lower()
                 and features.model_allowed(plan, m)), None)
            if match:
                out[bay] = {"provider": provider_id, "model": match}
                break
    return out


@app.route("/api/providers", methods=["GET"])
def list_providers():
    # TWO CHANNELS: the fast one, and the one that is always there.
    #
    # Gemini is gone entirely: the import is removed, every call site is
    # unwired, and gemini.py has been deleted. It was the fallback for
    # "this machine is off", a job Groq now does faster and Ollama does
    # locally, so it was a third way to answer that nothing chose.
    #
    # Groq stays because it is genuinely the fastest thing here, and its
    # 8,000-tokens-per-minute ceiling is now steered around rather than
    # walked into: see groq_api.budget_ok() and the failover in
    # _stream_reply(). It no longer has to be reliable on its own,
    # because Ollama catches every request it cannot take.
    plan = features.normalize_plan(current_account()[0].get("plan"))
    providers = [ollama_provider(plan)]
    if groq_api.configured():
        groq_models = groq_api.models()
        remaining, resets_in = groq_api.budget_state()
        if remaining is not None and remaining < 500:
            note = ("At its per-minute limit - replies are coming from "
                    "the local model for about %ds." % int(resets_in))
        else:
            note = ("Open-weight models on dedicated accelerators. Runs "
                    "off-device, so prompts leave this machine.")
        providers.append({
            "id": "groq",
            "label": "RandomGenerals AI Turbo",
            "available": bool(groq_models),
            "models": groq_models,
            "model_info": [describe_model(m, plan) for m in groq_models],
            "note": note,
        })

    if openrouter_api.configured() and openrouter_api.budget_ok(
            (openrouter_api.models() or [None])[0]):
        or_models = openrouter_api.models()
        providers.append({
            "id": "openrouter",
            "label": "OpenRouter",
            "available": bool(or_models),
            "models": or_models,
            "model_info": [describe_model(m, plan) for m in or_models],
            "note": ("Large coding models over OpenRouter. The :free "
                     "ones cost nothing beyond an account and are capped "
                     "at about 50 requests a day; Kimi is the paid option "
                     "and is billed per message to this server's key."),
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
    # local
    "gemma3:1b": ("Swift", "Fastest replies - the default here"),
    "gemma3:4b": ("Sight", "Sees attached images, thinks a little harder"),
    "gemma3": ("Gemma", "Google's open model, running on this server"),
    "llama3.2": ("Swift", "Fastest replies, lighter reasoning"),
    "qwen2.5-coder": ("Coder", "Tuned for writing and debugging code"),
    "qwen2.5": ("Core", "Best local accuracy for general questions"),
    "llava": ("Vision", "Can see and describe attached images"),
    # groq - open weights on dedicated accelerators
    "llama-3.3-70b-versatile": ("Turbo", "Large open model, answers fast"),
    "llama-3.1-8b-instant": ("Turbo Lite", "Smaller and faster still"),
    # cloud
    "openai/gpt-oss-120b": ("Max", "Strongest for code, and 6/6 on maths and physics"),
    "openai/gpt-oss-20b": ("Swift Cloud", "Fast cloud replies"),
    "poolside/laguna-s-2.1:free": (
        "Laguna", "Free coding model - no cost beyond an account"),
    "cohere/north-mini-code:free": ("North", "Free, code-specialised"),
    "z-ai/glm-5.2:free": ("GLM", "Free generalist, 256k context"),
    "nvidia/nemotron-3-ultra-550b-a55b:free": (
        "Nemotron", "Free, 550B parameters, 1M context"),
    "moonshotai/kimi-k2.7-code": (
        "Kimi", "Coding model - PAID, billed per message"),
    "moonshotai/kimi-k2.5": ("Kimi", "Moonshot's general model - PAID"),
    "qwen/qwen3.8-27b": ("Reason", "Answers in plain prose"),
    "qwen/qwen3.6-27b": ("Core Cloud", "Balanced cloud model"),
    "allam-2-7b": ("Arabic", "Tuned for Arabic language"),
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

    Both channels are called RandomGenerals AI, because that is what this
    is - a visitor did not come here to choose between vendors, and a
    picker offering "Cloud" and "Fast cloud" was advertising suppliers
    instead of the thing they came for.

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
        "label": "RandomGenerals AI",
        # HIDDEN FROM THE PICKER, NOT REMOVED.
        #
        # Two jobs remain that the hosted channel cannot do, so the
        # models stay installed and reachable even though nobody picks
        # them:
        #
        #   1. VISION. Groq's catalogue has no model that can see an
        #      image, so an attached picture is answered by gemma3:4b or
        #      it is not answered at all.
        #   2. THE RATE LIMIT. Groq's free tier is 8,000 tokens a minute
        #      shared across every visitor. At the current output ceiling
        #      that is a handful of replies before the window drains, and
        #      without something underneath it the site simply stops
        #      answering - which is exactly the failure this deployment
        #      already had once.
        #
        # The frontend hides any provider carrying this flag, so the
        # picker shows the two chosen models and the fallback stays
        # invisible until it is needed.
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
    # Free, and reachable from a server with no GPU - which is the only
    # way this deployment can see an image at all. Groq serves no
    # multimodal model: its whole catalogue here is 14 text models plus
    # speech, so on Groq alone an attached picture can never be looked
    # at by anything. Checked against OpenRouter's own
    # architecture.input_modalities rather than assumed.
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "minimax/minimax-m3:free",
    "dots-studio/dots-3-note-preview:free",
)


def is_vision_model(model):
    name = (model or "").lower()
    if any(name.startswith(m) for m in _VISION_MODEL_EXACT):
        return True
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
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if options:
        body["options"] = options
    with requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=body,
        headers=ollama_headers(),
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
    "groq": groq_api.stream_chat,
    "openrouter": openrouter_api.stream_chat,
}

# The non-streamed turn each hosted provider uses inside the tool loop.
# Ollama is absent on purpose: tool calling is offered on the hosted
# channels only - see the note above tool_specs in _stream_reply.
PROVIDER_TURNS = {
    "groq": groq_api.chat_once,
    "openrouter": openrouter_api.chat_once,
}

# Per account. Each connection adds its operation list to every request
# on that channel, so this is a prompt-size ceiling as much as a
# tidiness one.
MAX_CONNECTORS = 8


# ----------------------------------------------------------------------
# Connected apps
#
# A pasted link becomes a tool the model can call. Discovery, the SSRF
# guard and the actual calling all live in connectors.py; what is here is
# the plumbing between that and a chat turn.
# ----------------------------------------------------------------------
def _connector_tools(owner_id):
    """-> (tool specs, {tool name: connector}).

    The map is what turns a tool name back into the connection it came
    from, including its token, which never leaves the server.
    """
    specs, by_name = [], {}
    try:
        saved = db.load_connectors(owner_id, with_tokens=True)
    except Exception:                       # noqa: BLE001
        return [], {}
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

    # A language model expands the request before it reaches the image
    # model - see enhance_prompt() in imagegen.py for why that is the
    # biggest quality lever available without paying for better pixels.
    # Opt-out rather than opt-in: the gain is large, the round trip is
    # under a second on the fast channel, and a failure silently leaves
    # the person's own words in place.
    url, error = imagegen.generate_image(
        prompt, backend=backend, size=size, style=style,
        enhance=payload.get("enhance") is not False)

    if error:
        thread["updated"] = now_iso()
        save_threads()
        return jsonify({"error": error}), 502

    thread["messages"].append({
        "role": "assistant",
        "content": url,
        "type": "image",
        "provider": "imagegen",
        "model": _image_model_name(backend),
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
VIDEO_JOBS = {}
VIDEO_JOBS_LOCK = threading.Lock()


# TWO BACKENDS, ONE BAY
#
# PixVerse is better and costs about $0.45 a clip with no free tier at
# any volume. Hugging Face's free allowance is the only genuinely free
# path that survived checking - Pollinations is images only, and
# Cloudflare Workers AI, which already serves this app's images, has no
# video model in its catalogue.
#
# Paid first when a key exists, because someone who has paid should get
# what they paid for; free otherwise, so the bay works at all rather
# than showing "not switched on" to everyone. Both expose the same
# start()/result() pair, so nothing below this line knows which answered.
def _video_backend():
    """Whichever generator this bay can actually run.

    3D FIRST, and the reason is arithmetic rather than preference. The
    video backends were measured on this deployment: PixVerse has no free
    tier at any volume, and Hugging Face's free allowance turned out to be
    about three clips a MONTH site-wide - three succeeded and the fourth
    was refused. A bay promising two a day on top of that fails on its
    first visitor.

    Tripo is first of the three because a mesh is one asset where a
    clip is a hundred rendered frames, so the same money goes further.
    Its free credits were checked rather than assumed, though, and a new
    account's balance came back 0 - so this returns None here too until
    someone funds a key, and the bay falls through to diagrams.
    """
    if tripo3d.configured():
        return tripo3d, "tripo"
    if pixverse.configured():
        return pixverse, "pixverse"
    if hfvideo.configured():
        return hfvideo, "huggingface"
    return None, None


def video_configured():
    return _video_backend()[0] is not None


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
    plan = features.normalize_plan(account_credits.get("plan"))
    providers = [ollama_provider(plan)]
    if groq_api.configured():
        groq_models = groq_api.models()
        providers.append({"id": "groq", "available": bool(groq_models),
                          "models": groq_models,
                          "model_info": []})
    route = _recommended_routes(providers, plan).get("code") or {}
    provider = route.get("provider") or "ollama"
    model = route.get("model")
    if not model:
        return jsonify({"error": "No model is available right now."}), 503

    streamer = PROVIDER_STREAMERS.get(provider)
    if streamer is None:
        return jsonify({
            "error": "No model is available right now.",
        }), 503
    history = [{"role": "system", "content": DIAGRAM_SYSTEM_PROMPT},
               {"role": "user", "content": prompt}]
    opts = {"num_predict": 900, "temperature": 0.2, "num_ctx": 8192}
    try:
        text = "".join(streamer(model, history, options=opts))
    except groq_api.RateLimited:
        # Same failover the chat bay uses: a drained per-minute budget
        # should degrade to the local model, not surface as an error.
        local = _local_alternative("code")
        if not local:
            return jsonify({
                "error": "The fast channel is busy and no local model "
                         "is running. Try again in a moment.",
            }), 503
        text = "".join(
            PROVIDER_STREAMERS["ollama"](local, history, options=opts))

    source = _clean_mermaid(text)
    if not source:
        return jsonify({
            "error": "The model did not return a diagram. Try "
                     "describing it differently.",
        }), 502

    spend_credits(usage_based_cost(len(source) // 4, mode="code"))
    return jsonify({"source": source, "model": model,
                    "provider": provider})


@app.route("/api/video/status", methods=["GET"])
def video_status():
    """What the bay should render before anyone types anything."""
    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    signed_in = bool(session.get("user_id"))
    backend, backend_name = _video_backend()
    return jsonify({
        "configured": backend is not None,
        "backend": backend_name,
        # Say WHICH thing is missing. "Not switched on" is true and
        # useless - it gives whoever runs the server nothing to do, and
        # this bay has two possible backends, so "no key" is ambiguous
        # without naming which key.
        # Names the cheapest way to switch the bay on, which is the
        # 3D key - a free Tripo account buys ~100 models against Hugging
        # Face's ~3 clips.
        "detail": "" if backend else tripo3d.unavailable_reason(),
        # Named so the bay can say what it is using, and so "free tier,
        # may run out" is something the page can explain rather than a
        # surprise at the moment it happens.
        "free_tier": backend_name == "huggingface",
        # "video" or "model" - the bay renders a <video> or a GLB viewer,
        # and the copy differs, so the client is told rather than
        # guessing from the backend name.
        "kind": "model" if backend_name == "tripo" else "video",
        # Credits left, when the backend can report them. A configured
        # key with an empty balance is indistinguishable from a working
        # one until someone waits on a job that cannot start, so the bay
        # asks up front and says so instead.
        "credits": (tripo3d.balance() if backend_name == "tripo" else None),
        "quota": _video_quota_view(current_owner_id(), plan, signed_in),
        "max_seconds": pixverse.MAX_SECONDS,
        "min_seconds": pixverse.MIN_SECONDS,
        "default_seconds": pixverse.DEFAULT_SECONDS,
        "qualities": list(pixverse.QUALITIES),
        "ratios": list(pixverse.RATIOS),
    })


@app.route("/api/video/generate", methods=["POST"])
def video_generate():
    payload = request.get_json(force=True, silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Describe the video you want."}), 400

    backend, backend_name = _video_backend()
    if backend is None:
        return jsonify({
            "error": "Generation isn't set up on this server yet.",
            "detail": tripo3d.unavailable_reason(),
        }), 503

    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    signed_in = bool(session.get("user_id"))
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

    # The image bar, not the chat one: a generated clip is a published
    # artefact in exactly the way a generated picture is, and the same
    # reasoning applies - there is no context in which the output is a
    # discussion of the thing rather than the thing itself.
    blocked = moderation.check_image_prompt(prompt)
    if blocked is not None:
        return jsonify({"error": blocked}), 400

    owner = current_owner_id()
    month = _video_period_key(plan)
    limit = features.video_quota_for(plan)
    if db.video_used(owner, month) >= limit:
        period = features.video_period_for(plan)
        return jsonify({
            "error": "You've used all %d video generations for this %s." % (
                limit, period),
            "quota": _video_quota_view(owner, plan, signed_in),
        }), 402

    # Counted BEFORE the call, not after. Two requests arriving together
    # would otherwise both pass the check above and both generate, and
    # the overage is money rather than a rate limit. Refunded below if
    # PixVerse never accepted the job.
    db.video_consume(owner, month)

    seconds = pixverse.clamp_seconds(payload.get("seconds"))
    if backend_name == "pixverse":
        video_id, err = backend.start(
            prompt,
            seconds=seconds,
            quality=payload.get("quality") or "720p",
            ratio=payload.get("ratio") or "16:9",
        )
    else:
        # Neither the free video backend nor the 3D one has quality or
        # aspect controls - a mesh has no aspect ratio and LTX produces a
        # fixed shape - so offering settings that do nothing is worse
        # than not offering them.
        video_id, err = backend.start(prompt, seconds=seconds)
    if err:
        db.video_refund(owner, month)
        return jsonify({"error": err,
                        "quota": _video_quota_view(owner, plan,
                                                   signed_in)}), 502

    job_id = uuid.uuid4().hex[:12]
    with VIDEO_JOBS_LOCK:
        VIDEO_JOBS[job_id] = {
            "id": job_id, "owner": owner, "video_id": video_id,
            "backend": backend_name,
            "status": "running", "url": None, "error": "",
            "prompt": prompt, "started": now_iso(),
        }
    return jsonify({
        "job": {"id": job_id, "status": "running"},
        "quota": _video_quota_view(owner, plan, signed_in),
    })


@app.route("/api/video/job/<job_id>", methods=["GET"])
def video_job(job_id):
    with VIDEO_JOBS_LOCK:
        job = VIDEO_JOBS.get(job_id)
        if not job or job["owner"] != current_owner_id():
            return jsonify({"error": "Unknown job"}), 404
        job = dict(job)

    plan_now = features.normalize_plan(current_account()[0].get("plan"))
    if job["status"] == "running":
        # Asked of whichever backend started it, not of whichever is
        # configured now - a key added mid-render must not orphan a job.
        started_with = {
            "pixverse": pixverse,
            "tripo": tripo3d,
        }.get(job.get("backend") or "", hfvideo)
        state, url, err = started_with.result(job["video_id"])
        if state == "done":
            job.update({"status": "done", "url": url})
        elif state == "failed":
            job.update({"status": "failed", "error": err or "Failed."})
            # The provider never delivered, so the month's count should
            # not carry it. Refunded once, at the moment the failure is
            # first observed - polling again finds status "failed" and
            # does not reach here a second time.
            db.video_refund(job["owner"], _video_period_key(plan_now))
        with VIDEO_JOBS_LOCK:
            if job_id in VIDEO_JOBS:
                VIDEO_JOBS[job_id].update(job)

    account, _ = current_account()
    plan = features.normalize_plan(account.get("plan"))
    view = {k: v for k, v in job.items() if k != "owner"}
    return jsonify({"job": view,
                    "quota": _video_quota_view(job["owner"], plan)})


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


# How many times the model may reach for a tool before it has to
# answer. Three covers search-then-refine-then-answer, which is the
# deepest useful pattern here; beyond that a model is usually looping
# on a question the tools cannot settle, and every round costs a
# request against a per-minute budget shared by everyone on the site.
MAX_TOOL_ROUNDS = 3


def _run_tool_loop(model, history, specs, provider="groq",
                   connector_map=None):
    """Let the model call tools until it is ready to answer.

    -> (history with the tool exchange appended, notes).

    Never raises: a tool that fails returns its error as the tool
    result, so the model can say what went wrong instead of the whole
    reply dying on a failed search.
    """
    notes = []
    convo = list(history)
    for _ in range(MAX_TOOL_ROUNDS):
        turn = PROVIDER_TURNS.get(provider, groq_api.chat_once)
        try:
            msg, err = turn(
                model, convo, tools=specs,
                options={"num_predict": 900, "temperature": 0.2})
        except groq_api.RateLimited:
            # The budget can drain between _groq_has_room() and this call,
            # and the loop's own turns are what drain it. chat_once raises
            # rather than returning an error, so without this the whole
            # request dies on a rate limit that the streaming path handles
            # gracefully two functions later. Tools are an enhancement;
            # losing them costs a less-checked answer, not the answer.
            return history, notes
        if err or not msg:
            # The loop is an enhancement, not a requirement - fall
            # through to a normal answer rather than failing.
            return history, notes
        calls = msg.get("tool_calls") or []
        if not calls:
            return convo, notes

        convo.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })
        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            result = _dispatch_tool(name, fn.get("arguments"),
                                    connector_map)
            notes.append(name)
            convo.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result.get("text") or "",
            })
    return convo, notes


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
    # Since the chat bay routes to gpt-oss-120b, which is text-only, that
    # was every image on this deployment.
    #
    # So: if there are images and this model is blind, look for one that
    # is not, among providers that are actually up, and use it for this
    # message only. The thread's own model is unchanged.
    if any(f["kind"] == "image" for f in files) and not is_vision_model(model):
        seer_provider, seer_model = _vision_route()
        # Both, so the pair is either fully replaced or left alone - a
        # provider without a model is not a route.
        if seer_provider and seer_model:
            provider, model = seer_provider, seer_model

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

    # THE FAST CHANNEL NO LONGER HAS TO BE RELIABLE ON ITS OWN
    #
    # Groq's ceiling is 8,000 tokens a minute shared by everybody using
    # the site, which is what made it "work for a few seconds a minute":
    # a few requests drained the window and every one after that failed
    # until it rolled over. Two changes, and neither asks Groq for more.
    #
    # First, the budget is checked BEFORE the request. Every Groq
    # response reports what is left, so when there is not enough for this
    # one it goes straight to the local model - no wasted round trip, no
    # error, and the person never learns there was a limit.
    #
    # Second, a 429 that happens anyway is caught below and retried
    # locally rather than surfaced. Between them the channel degrades
    # instead of failing, which is the actual fix: it is not that Groq
    # stops being rate-limited, it is that being rate-limited stops
    # mattering.
    fell_back_to_cloud = False
    plan = features.normalize_plan(current_account()[0].get("plan"))

    # Kimi is first in the code bay but it is the only route here that
    # needs a paid key, so a request naming it on a server without one -
    # a stale tab, a saved preference, an older client - must not become
    # a dead reply. It degrades to the free fast channel, and to the
    # local model after that, the same way every other channel does.
    if provider == "openrouter" and not openrouter_api.budget_ok(model):
        # Out of money for today rather than out of key. Same failover.
        local = None
        if groq_api.configured() and groq_api.models():
            provider, model = "groq", groq_api.PREFERRED[0]
        else:
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local

    if provider == "openrouter" and not openrouter_api.configured():
        if groq_api.configured() and groq_api.models():
            provider = "groq"
            model = groq_api.PREFERRED[0]
        else:
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local

    if provider == "groq":
        if not _groq_has_room(plan):
            local = _local_alternative(mode)
            if local:
                provider, model = "ollama", local
                fell_back_to_cloud = True

    if provider == "ollama" and not ollama_reachable():
        # Local is unavailable too. Try the fast channel rather than
        # giving up - the direction people usually need is the other way,
        # but a deployment with no local model at all is exactly the
        # hosted case, and refusing there would be gratuitous.
        if groq_api.configured() and groq_api.models():
            provider = "groq"
            model = next((m for _, p in BAY_ROUTES.get(mode, [])
                          for m in groq_api.models() if p in m.lower()),
                         groq_api.PREFERRED[0])
        else:
            def unavailable():
                yield ("[No model is answering. If this is running on your "
                       "own machine, start Ollama, or check OLLAMA_URL and "
                       "OLLAMA_API_KEY on the server.]")
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
    combined_options: dict[str, Any] = (
        dict(CODE_MODEL_OPTIONS) if mode == "code" else {})
    # 8192 for chat, 16384 for code. Measured on this GPU: 16384 stays
    # entirely in VRAM, 32768 spills to CPU and collapses throughput.
    # Code is where the extra context actually pays - a long file plus
    # its error output does not fit in 8192, and what gets dropped is
    # the top of the file, which is usually where the imports and the
    # definitions being asked about live.
    combined_options["num_ctx"] = 16384 if mode == "code" else 8192
    combined_options.update(STRENGTH_LEVELS[strength]["options"])
    # Only Groq reads this, and for it the setting is a budget decision
    # as much as a quality one: the same maths question cost 297
    # completion tokens at "low" effort and 683 at "high", for the same
    # correct answer. Against an 8,000/minute ceiling that is the
    # difference between roughly 25 replies a minute and 11.
    combined_options["reasoning_effort"] = groq_api.effort_for(
        mode, strength)
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

    # MODEL-DRIVEN TOOLS
    #
    # This was switched off when the cloud provider was removed, on the
    # grounds that a small local model which hallucinates a tool call
    # answers worse than one that just talks. That reasoning still holds
    # for Ollama - and no longer applies to the hosted channel, which is
    # back. Both models it serves were checked before this was wired:
    # asked to look something up, gpt-oss-120b and qwen3.8-27b each
    # returned a well-formed web_search call with a sensible query.
    #
    # So tools are offered on the hosted channel only. The loop runs
    # BEFORE the stream opens: a tool call arrives as a name and a JSON
    # blob, and half of either is useless, so those turns are fetched
    # whole and unseen, and only the final answer is streamed.
    tool_specs = None
    tool_notes = []
    connector_map = {}
    # The loop spends an extra non-streamed turn before the answer, so it
    # is offered only while the shared per-minute budget can carry one.
    # Under pressure the reply still happens, just without tools - which
    # is the same degradation every other part of this channel makes.
    if (provider in PROVIDER_TURNS
            and features.FEATURES[plan]["builtin_tools"]
            and _groq_has_room(plan)):
        tool_specs = tools.available_specs(
            allow_images=(mode != "image"),
            allow_code=True,
            allow_web=True,
        )
        # Connected apps are the part that stays paid, so they join the
        # list only for a plan that has them.
        if features.FEATURES[plan]["external_connectors"]:
            extra, connector_map = _connector_tools(current_owner_id())
            tool_specs = tool_specs + extra
        # Say the tools exist, in the same breath as offering them.
        # Promising a calculator to a session that has none is worse
        # than silence, so this rides with tool_specs rather than being
        # baked into the base prompt.
        if history and history[0].get("role") == "system":
            history = ([{"role": "system",
                         "content": history[0]["content"] + TOOL_USE_PROMPT}]
                       + history[1:])
        history, tool_notes = _run_tool_loop(
            model, history, tool_specs, provider=provider,
            connector_map=connector_map)

    def generate():
        nonlocal provider, model, streamer, fell_back_to_cloud
        full_reply = ""
        # Announce the tools that ran BEFORE the prose starts. They have
        # already finished by this point - the loop resolves them before
        # a stream is opened - so each is emitted as done rather than
        # started. An answer that quietly consulted the web is harder to
        # trust than one that says it did.
        for name in tool_notes:
            yield tool_event({"tool": name, "status": "done"})
        try:
            try:
                for piece in streamer(model, history, **stream_kwargs):
                    full_reply += piece
                    yield piece
            except groq_api.RateLimited:
                # Safe to retry: RateLimited is raised before the first
                # chunk, so nothing has reached the browser yet and the
                # same conversation can be answered locally without the
                # reader seeing a seam.
                local = _local_alternative(mode)
                if not local:
                    full_reply = ("[The fast channel is at its per-minute "
                                  "limit and no local model is running to "
                                  "take over. Try again in a moment.]")
                    yield full_reply
                    return
                provider, model = "ollama", local
                streamer = PROVIDER_STREAMERS[provider]
                fell_back_to_cloud = True
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
                    spend_credits(
                        usage_based_cost(usage.get("eval_count"), mode))

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
