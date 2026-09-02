"""OpenRouter - the channel that can actually run Kimi.

WHY THIS FILE EXISTS

Kimi was asked for by name. Groq does not serve it: the live catalogue
on this key is 14 models and not one of them is from Moonshot, so
pointing the code bay at "kimi" there would have produced a 404 on the
first coding question. It cannot run locally either - K2 is a trillion
-parameter mixture-of-experts, against an Always Free VM with 11.9GB of
RAM and no GPU.

OpenRouter serves it, and that is the whole reason for a third provider.

THIS ONE COSTS MONEY, WHICH THE OTHER TWO DO NOT

Groq and Ollama are free at the point of use. OpenRouter is not: Kimi
runs about $0.66 per million tokens in and $3.40 out, so a typical
coding turn is roughly half a cent and a dollar buys a couple of hundred
of them. That is cheap, but it is not nothing, and it is the first thing
in this app that spends real money on an ordinary chat message.

So it is off unless OPENROUTER_API_KEY is set. With no key this module
reports itself unconfigured, app.py skips straight past it in the
routing table, and the app behaves exactly as it did before - gpt-oss on
Groq. Nothing here degrades the free path; it only adds a better one
when someone has paid for it.

Exposes configured(), models(), chat_once() and stream_chat(), the same
interface app.py registers every provider through.
"""
import datetime
import json
import os
import time

import requests

import db
import groq_api

API_ROOT = "https://openrouter.ai/api/v1"

MODELS_TTL = 3600

# Coding first, because that is the bay this provider was added for.
#
# k2.7-code is Moonshot's coding-tuned build and the one the code bay
# asks for by name. k2.5 is the cheaper general model and stands in when
# the catalogue no longer lists the coding one - model ids here get
# retired as new versions land, and a bay that 404s because an id moved
# is worse than one answering on last month's model.
PREFERRED = [
    "moonshotai/kimi-k2.7-code",
    "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2-0905",
    "moonshotai/kimi-k2",
]

# What the picker offers. OpenRouter lists several hundred models from
# every lab, which is not a menu anyone wants in a settings dropdown, so
# this is narrowed to the ones this app has a reason to route to.
EXPOSED_MODELS = tuple(PREFERRED)

FALLBACK_MODELS = list(PREFERRED[:2])

_models_cache = {"at": 0.0, "models": None, "error": ""}


# ----------------------------------------------------------------------
# THE SPEND CEILING
#
# This is the only thing in the app that costs money per message, and it
# sits behind a public website. Without a ceiling, one stranger looping
# the code bay spends the owner's balance and the first anyone knows is
# an empty account.
#
# So every response's real cost - OpenRouter returns it in usage.cost,
# including on the last chunk of a stream - is added to a site-wide daily
# total, and the router stops choosing Kimi once the day's limit is
# reached. The bay then falls to gpt-oss, which is free. The site gets
# slightly less good; it does not get an unexpected bill.
#
# Site-wide rather than per-user on purpose: the limit protects one bank
# account, and a per-user cap still lets a hundred signups spend a
# hundred times it.
# ----------------------------------------------------------------------
DEFAULT_DAILY_USD = 1.00


def daily_limit():
    """Dollars a day, from OPENROUTER_DAILY_USD. 0 disables Kimi
    entirely, which is a legitimate way to turn it off without removing
    the key."""
    raw = os.environ.get("OPENROUTER_DAILY_USD", "").strip()
    if not raw:
        return DEFAULT_DAILY_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DAILY_USD


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def note_cost(usage):
    """Record what a response actually cost. Never raises.

    A failure to write the spend counter must not fail the reply the user
    is already reading - but it does mean the ceiling is now blind, so it
    is logged rather than swallowed silently.
    """
    if not isinstance(usage, dict):
        return
    cost = usage.get("cost")
    if cost in (None, 0):
        return
    try:
        db.openrouter_add_spend(_today(), float(cost))
    except Exception as e:                       # noqa: BLE001
        print("[openrouter] could not record spend (%s): %s" % (cost, e))


def spend_today():
    try:
        return db.openrouter_spend_today(_today())
    except Exception:                            # noqa: BLE001
        # Failing CLOSED: if the counter cannot be read, the safe
        # assumption is that the budget is gone, not that it is intact.
        return float("inf")


def budget_ok():
    """Whether another paid request is allowed today."""
    limit = daily_limit()
    if limit <= 0:
        return False
    return spend_today() < limit


def budget_state():
    """-> (spent, limit), for the owner's stats page."""
    return spend_today(), daily_limit()


def api_key():
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def configured():
    return bool(api_key())


def last_error():
    return _models_cache.get("error", "")


def _headers():
    return {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
        # OpenRouter attributes requests to a site when these are sent.
        # Neither is required and neither carries anything about the
        # person asking - it is the app identifying itself, not them.
        "HTTP-Referer": "https://randomgenerals.com",
        "X-Title": "RandomGenerals AI",
    }


def models():
    """The Kimi models this key can actually see, live and cached.

    Filtered to EXPOSED_MODELS rather than returned whole: the raw list
    is enormous, and the picker is not the place to discover that
    OpenRouter also resells sixty models this app never routes to.
    """
    if not configured():
        return []
    now = time.time()
    cached = _models_cache
    if cached["models"] is not None and now - cached["at"] < MODELS_TTL:
        return cached["models"]
    try:
        r = requests.get(API_ROOT + "/models", headers=_headers(), timeout=10)
        if r.status_code in (401, 403):
            _models_cache.update({
                "at": now, "models": [],
                "error": "OpenRouter rejected the key (%d)." % r.status_code,
            })
            return []
        r.raise_for_status()
        found = {m.get("id", "") for m in r.json().get("data", [])}
        # Keep PREFERRED's order - it is a preference list, and sorting
        # it alphabetically would put k2 ahead of k2.7-code.
        out = [m for m in PREFERRED if m in found]
        _models_cache.update({"at": now, "models": out or FALLBACK_MODELS,
                              "error": ""})
        return _models_cache["models"]
    except requests.exceptions.RequestException as e:
        out = cached["models"] or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out,
                              "error": "could not reach OpenRouter: %s" % e})
        return out


def _pick(model):
    """The requested model if this key has it, else the best it does."""
    available = models()
    if model in available:
        return model
    return next((m for m in PREFERRED if m in available),
                available[0] if available else PREFERRED[0])


def _to_messages(history):
    """Same wire format as Groq, so the same converter.

    This is deliberately not a second copy. The Groq one already handles
    the two cases that are easy to get wrong - preserving the `tool` role
    and keeping assistant messages whose content is empty because their
    payload is tool_calls - and the tool loop broke once already when a
    converter dropped exactly those. One implementation, one place to fix.
    """
    return groq_api._to_messages(history)


def chat_once(model, history, tools=None, options=None, timeout=120):
    """One non-streamed turn, for the tool loop. -> (message, error)."""
    if not configured():
        return None, "OpenRouter is not configured."
    body = {
        "model": _pick(model),
        "messages": _to_messages(history),
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    opts = options or {}
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    try:
        r = requests.post(API_ROOT + "/chat/completions", json=body,
                          headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, "Could not reach OpenRouter: %s" % e
    if r.status_code == 402:
        return None, ("This OpenRouter key is out of credit - Kimi is a "
                      "paid model.")
    if r.status_code != 200:
        return None, "OpenRouter error %d" % r.status_code
    try:
        payload = r.json()
    except ValueError:
        return None, "OpenRouter returned an unreadable response."
    note_cost(payload.get("usage"))
    try:
        return payload["choices"][0]["message"], None
    except (KeyError, IndexError):
        return None, "OpenRouter returned an unreadable response."


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces.

    `images` is accepted and ignored, as on the Groq channel: the caller
    has already decided vision is not on this route, and dropping the
    attachment beats refusing the message.
    """
    if not configured():
        yield "[OpenRouter is not configured - set OPENROUTER_API_KEY.]"
        return
    if not budget_ok():
        spent, limit = budget_state()
        yield ("[Kimi has reached its spending limit for today "
               "($%.2f of $%.2f). Switch to the chat model, or raise "
               "OPENROUTER_DAILY_USD on the server.]" % (spent, limit))
        return

    opts = options or {}
    body = {
        "model": _pick(model),
        "messages": _to_messages(history),
        "stream": True,
    }
    if opts.get("tools"):
        body["tools"] = opts["tools"]
        body["tool_choice"] = "auto"
    if "temperature" in opts:
        body["temperature"] = opts["temperature"]
    if "top_p" in opts:
        body["top_p"] = opts["top_p"]
    if opts.get("num_predict"):
        body["max_tokens"] = int(opts["num_predict"])
    # No reasoning_effort. It is a gpt-oss-specific knob on Groq, and
    # sending unknown fields to a different lab's model is how the qwen
    # empty-reply bug happened - see groq_api._supports_effort.

    try:
        r = requests.post(API_ROOT + "/chat/completions", json=body,
                          headers=_headers(), stream=True, timeout=120)
    except requests.exceptions.RequestException as e:
        yield "[Could not reach OpenRouter: %s]" % e
        return

    if r.status_code == 402:
        # Worth its own message. "error 402" tells someone nothing, and
        # this is the one failure here with an obvious remedy.
        yield ("[This OpenRouter key is out of credit. Kimi is a paid "
               "model - top the key up, or unset OPENROUTER_API_KEY to go "
               "back to the free channel.]")
        return
    if r.status_code in (401, 403):
        yield "[OpenRouter rejected the key.]"
        return
    if r.status_code != 200:
        yield "[OpenRouter error %d.]" % r.status_code
        return

    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        chunk = raw[6:].strip()
        if chunk == "[DONE]":
            break
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        try:
            delta = data["choices"][0].get("delta") or {}
        except (KeyError, IndexError):
            continue
        piece = delta.get("content")
        if piece:
            yield piece
        if data.get("usage"):
            note_cost(data["usage"])
            if usage is not None:
                usage.update(data["usage"])
