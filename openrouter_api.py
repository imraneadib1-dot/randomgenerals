"""OpenRouter - the channel that runs DeepSeek for the code bay.

WHY THIS FILE EXISTS

pip 1.0 answers first. This is what answers when it cannot - the
engine is down, or busy - and what the code bay can be pointed at by
name: DeepSeek, hundreds of billions of parameters that no machine of
ours could hold.

OpenRouter serves those, and that is the whole reason for the channel.

DEEPSEEK COSTS MONEY, A LITTLE. THE FALLBACKS DO NOT.

The code bay's model is DeepSeek V3 (deepseek-v3.2, the current V3
line: about $0.27 in and $0.40 out per million tokens, so a long
answer is a fraction of a cent), and Deep mode switches to DeepSeek R1
(r1-0528, the reasoning model, about five times the output price).
Both are billed to this server's key and stopped by the daily ceiling
below.

What OpenRouter also has is genuinely free models - no card, just an
account, rate-limited to about 50 requests a day (1,000 if the account
ever holds $10 in credit). Several are large and current. They sit
under DeepSeek in PREFERRED below, so a key with no credit, or one that
has spent the day's ceiling, still gets a strong coding model at no
cost.

That ordering is a judgement, not a measurement. Ranking these against
each other needs a key to test with, and none of them has been run on
this deployment yet - so the order below is by what each model is FOR,
which is the honest basis available. Re-order it after measuring.

With no key at all this module reports itself unconfigured and app.py
skips past it in the routing table. Nothing here degrades the free path.

Exposes configured(), models(), chat_once() and stream_chat(), the same
interface app.py registers every provider through.
"""
import datetime
import json
import os
import time

import requests

import attachments  # the data-URL helper images travel in

import db
import pip_api
from providers import RateLimited, Unreachable

API_ROOT = "https://openrouter.ai/api/v1"

MODELS_TTL = 3600

# FREE FIRST. Everything with a :free suffix costs nothing beyond an
# account, and the first four are ordered by what they are built for:
#
#   laguna-s-2.1   poolside builds coding models and nothing else
#   north-mini-code  code is in the name
#   glm-5.2        strong generalist, 256k context
#   nemotron ultra 550B parameters and a million tokens of context - the
#                  largest thing on the free list by a wide margin
#
# DEEPSEEK FIRST NOW, by request. V3 is the code bay's model and R1 is
# what Deep mode switches to (see DEEP_MODEL and app._stream_reply). The
# older V3 (0324) is kept as the fallback for the day OpenRouter retires
# the id. The free models follow, so a key with no credit - or one that
# has spent the day's ceiling - still gets a coding model rather than a
# dead bay. Kimi is gone from the list: two paid coding models is two
# ways to be surprised by the bill, and one was asked for.
#
# Ids on this list get retired as new versions land, and models() keeps
# only the ones the live catalogue still has - so a retired id is
# skipped rather than 404ing the bay.
PREFERRED = [
    # paid: DeepSeek, the code bay's models
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-r1-0528",
    "deepseek/deepseek-chat-v3-0324",
    # free, from here down
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

# What Deep mode asks for on this channel. R1 thinks before it writes,
# which is what "deep" means, and costs about five times V3 per output
# token, which is why it is not the default. Only reached when the
# catalogue has it and the day's ceiling allows it.
DEEP_MODEL = "deepseek/deepseek-r1-0528"

# What the picker offers. OpenRouter lists several hundred models from
# every lab, which is not a menu anyone wants in a settings dropdown, so
# this is narrowed to the ones this app has a reason to route to.
EXPOSED_MODELS = tuple(PREFERRED)

FALLBACK_MODELS = list(PREFERRED[:2])

_models_cache = {"at": 0.0, "models": None, "error": "",
                 # Every id in the catalogue whose architecture lists
                 # "image" among its input modalities. Kept apart from
                 # `models` (the picker list, filtered to PREFERRED) so a
                 # vision model can be routed to without being offered
                 # in the dropdown.
                 "vision": []}


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
# reached. The bay then falls to the free models. The site gets
# slightly less good; it does not get an unexpected bill.
#
# Site-wide rather than per-user on purpose: the limit protects one bank
# account, and a per-user cap still lets a hundred signups spend a
# hundred times it.
# ----------------------------------------------------------------------
DEFAULT_DAILY_USD = 1.00


def daily_limit():
    """Dollars a day, from OPENROUTER_DAILY_USD. 0 disables the paid
    models entirely, which is a legitimate way to turn them off without
    removing the key."""
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


def is_free(model):
    """OpenRouter marks zero-cost models with a :free suffix."""
    return str(model or "").endswith(":free")


def budget_ok(model=None):
    """Whether another request is allowed today.

    A :free model is always allowed - it cannot add to the total, so
    stopping it would be a limit on nothing. The ceiling exists for the
    paid ids at the bottom of PREFERRED.
    """
    if model is not None and is_free(model):
        return True
    limit = daily_limit()
    if limit <= 0:
        # Two different questions, and answering both the same way is
        # what let a zero ceiling keep spending.
        #
        # A NAMED model here is paid (free ids returned above), and the
        # question is "may I spend on this one?". At a zero ceiling the
        # answer is no - that is what setting it to zero is for.
        if model is not None:
            return False
        # NO model named means "is this channel usable at all?", which it
        # is, as long as something free is on the list. Callers ask this
        # to decide whether to offer OpenRouter, not whether to spend.
        return any(is_free(m) for m in models())
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
        "X-Title": "pip 1.0",
    }


def models():
    """The models this key can actually see, live and cached.

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
        entries = r.json().get("data", [])
        found = {m.get("id", "") for m in entries}
        # Keep PREFERRED's order - it is a preference list, and sorting
        # it alphabetically would put k2 ahead of k2.7-code.
        out = [m for m in PREFERRED if m in found]
        seeing = [
            m.get("id", "") for m in entries
            if "image" in ((m.get("architecture") or {})
                           .get("input_modalities") or [])
        ]
        _models_cache.update({"at": now, "models": out or FALLBACK_MODELS,
                              "vision": seeing, "error": ""})
        return _models_cache["models"]
    except requests.exceptions.RequestException as e:
        out = cached["models"] or FALLBACK_MODELS
        _models_cache.update({"at": now, "models": out,
                              "error": "could not reach OpenRouter: %s" % e})
        return out


def vision_models():
    """Catalogue ids that accept an image, whether or not the picker
    offers them. app.py's vision route matches its patterns against
    this; matching them against models() - the PREFERRED-filtered
    picker list - could never succeed, because no vision model is in
    PREFERRED, and the route was dead.
    """
    models()                       # fills the cache when it is stale
    return list(_models_cache.get("vision") or [])


def _pick(model):
    """The requested model if this key has it, else the best it does."""
    available = models()
    if model in available:
        return model
    return next((m for m in PREFERRED if m in available),
                available[0] if available else PREFERRED[0])


def _to_messages(history):
    """Same wire format as the pip channel, so the same converter.

    This is deliberately not a second copy. That one already handles
    the two cases that are easy to get wrong - preserving the `tool` role
    and keeping assistant messages whose content is empty because their
    payload is tool_calls - and the tool loop broke once already when a
    converter dropped exactly those. One implementation, one place to fix.
    """
    return pip_api._to_messages(history)


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
    if r.status_code == 429:
        # Same as pip_api.chat_once: raised, so the tool loop can drop
        # the tools and still answer, rather than returned as an error
        # it would treat the same as a bad request.
        raise RateLimited("OpenRouter rate limit")
    if r.status_code == 402:
        return None, ("This OpenRouter key is out of credit - DeepSeek "
                      "is a paid model.")
    if r.status_code != 200:
        return None, "OpenRouter error %d" % r.status_code
    try:
        payload = r.json()
    except ValueError:
        return None, "OpenRouter returned an unreadable response."
    note_cost(payload.get("usage"))
    try:
        choice = payload["choices"][0]
        message = dict(choice["message"])
    except (KeyError, IndexError, TypeError):
        return None, "OpenRouter returned an unreadable response."
    # Same as pip_api.chat_once: the tool loop reads this.
    message["finish_reason"] = choice.get("finish_reason")
    return message, None


def _with_images(messages, images):
    """Attach base64 images to the last user turn, OpenAI-style.

    The text becomes one part and each image another, which is the
    shape every multimodal model on OpenRouter reads (and
    pip_api._with_images does the same there).
    Ollama takes images in its own field.
    """
    if not images:
        return messages
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        text = out[i].get("content") or ""
        parts = [{"type": "text", "text": text}] if text else []
        for b64 in images:
            parts.append({"type": "image_url",
                          "image_url": {"url": attachments.data_url(b64)}})
        out[i] = {"role": "user", "content": parts}
        break
    return out


def stream_chat(model, history, options=None, images=None, usage=None):
    """Stream a reply. Yields text pieces.

    `images` are sent when given. This channel used to accept and
    IGNORE them - but it serves models that can see, and app.py routes
    image turns here for exactly that reason. A
    caller that picked a vision model and sent the picture got a reply
    about a picture the model never received.
    """
    if not configured():
        yield "[OpenRouter is not configured - set OPENROUTER_API_KEY.]"
        return
    if not budget_ok(_pick(model)):
        spent, limit = budget_state()
        yield ("[DeepSeek has reached its spending limit for today "
               "($%.2f of $%.2f). Switch to the chat model, or raise "
               "OPENROUTER_DAILY_USD on the server.]" % (spent, limit))
        return

    opts = options or {}
    body = {
        "model": _pick(model),
        "messages": _with_images(_to_messages(history), images),
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
    # No reasoning_effort. It is the pip engine's knob, and sending
    # unknown fields to another lab's model has returned an empty reply
    # before.

    # Why the model stopped. Read for the same reason as on the pip
    # channel: "length" means it ran out of budget mid-answer, and
    # without checking it a truncated reply is indistinguishable from a
    # complete one that ended awkwardly.
    finish = None
    # Whether any content has reached the caller yet. Before the first
    # token a failure is raised and answered elsewhere; after it the
    # reply is on somebody's screen and the only honest option is to
    # say what happened at the end of it. See providers.py.
    sent_any = False

    try:
        # `with`, so the connection is returned to the pool however this
        # generator ends - an early return on 402, or the browser
        # closing the tab mid-reply, which abandons the generator
        # without ever reaching the end of the loop.
        with requests.post(API_ROOT + "/chat/completions", json=body,
                           headers=_headers(), stream=True,
                           timeout=120) as r:
            if r.status_code == 429:
                raise RateLimited(r.headers.get("retry-after")
                                  or "OpenRouter rate limit")
            if r.status_code == 402:
                # Worth its own message. "error 402" tells someone
                # nothing, and this is the one failure here with an
                # obvious remedy. Said, not raised: another provider
                # cannot top the key up.
                yield ("[This OpenRouter key is out of credit. DeepSeek "
                       "is a paid model - top the key up, or unset "
                       "OPENROUTER_API_KEY to go back to the free channel.]")
                return
            if r.status_code in (401, 403):
                yield "[OpenRouter rejected the key.]"
                return
            if r.status_code >= 500:
                # Their outage, not this request's fault, and nothing
                # has streamed. This used to be yielded as
                # "[OpenRouter error 502.]" - the outage became the reply.
                raise Unreachable("OpenRouter returned %d" % r.status_code)
            if r.status_code != 200:
                yield "[OpenRouter error %d.]" % r.status_code
                return

            # requests falls back to ISO-8859-1 for a text/* response
            # that carries no charset, and text/event-stream almost
            # never carries one - so decode_unicode was decoding UTF-8
            # bytes as Latin-1 and every accented character came out
            # mojibake: "medina" as "mÃ©dina", an em dash as "aEUR"", and
            # Arabic as noise. The stream is UTF-8; say so.
            r.encoding = "utf-8"
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
                    choice = data["choices"][0]
                except (KeyError, IndexError):
                    choice = None
                if choice is not None:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        sent_any = True
                        yield piece
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                if data.get("usage"):
                    note_cost(data["usage"])
                    if usage is not None:
                        usage.update(data["usage"])
                        # The name app.py charges by. The pip channel and
                        # Ollama both report generated tokens as eval_count;
                        # this channel reported completion_tokens only,
                        # so usage_based_cost() read None and billed
                        # every Kimi reply - the one paid channel - at
                        # the floor.
                        if "completion_tokens" in data["usage"]:
                            usage["eval_count"] = (
                                data["usage"]["completion_tokens"])
    except requests.exceptions.RequestException as e:
        if not sent_any:
            # Nothing has been streamed, so this can still be raised
            # and answered by another provider instead of becoming an
            # error message the person has to read.
            raise Unreachable(str(e))
        # Mid-stream. Text is already on screen, so this is said at the
        # end of it rather than raised - there is nowhere to fail over
        # to once the reader has started reading.
        yield ("\n\n_…the connection to the model dropped part-way "
               "through. The answer above is incomplete — ask again to "
               "get the rest._")
        return

    if not sent_any:
        # The stream ended without a single content delta.
        #
        # This is not hypothetical: nemotron returned zero characters
        # twice while the same prompt answered fine over a plain
        # non-streaming call, and the person saw an empty assistant
        # bubble with no error - which reads as the AI having nothing to
        # say rather than as a channel that did not answer.
        #
        # Raised rather than yielded as text, for the same reason the
        # RequestException branch above raises when nothing has streamed:
        # with no output on screen yet, another provider can still take
        # the question. A message here would end the turn instead.
        raise Unreachable("the model returned an empty reply")

    if usage is not None and finish:
        usage["finish_reason"] = finish
    if finish == "length" and not (usage or {}).get("auto_continue"):
        yield ("\n\n_…that hit the length limit for one reply. Say "
               "**continue** and I'll pick up where I left off._")
