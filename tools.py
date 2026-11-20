"""Tools the model can call on its own, mid-answer.

Until now web search, code execution and image generation each had their
own route, and the *user* decided when to use them by flipping a switch
before sending. That puts the burden on someone who doesn't necessarily
know which switch helps: asking "what happened in the news today" with
web search off gets a confident answer from stale training data.

Here the model decides instead. It is handed a list of tools with their
schemas, calls the ones it needs, sees the results, and continues. This
is the OpenAI function-calling format, which the pip engine implements
too, so the same definitions work on every channel.

WHAT THIS MODULE IS RESPONSIBLE FOR
Turning a tool name plus a blob of model-supplied JSON into a string the
model can read, without ever raising. A tool that throws would abort the
whole reply; a tool that returns "that failed because X" lets the model
recover, apologise, or try something else. So every failure path here
comes back as text.

Sizes are capped hard. Tool output is fed straight back into the next
request, and an uncapped 200KB page of search results would either blow
the context window or cost a fortune in tokens for content the model
skims once.
"""
import json

import browser
import codeexec
import imagegen
import imagesearch
import websearch

# Roughly 4 chars per token, so 6000 chars is ~1500 tokens per tool
# result. Enough for five search snippets or a page of program output,
# small enough that several tool calls in one turn stay affordable.
MAX_RESULT_CHARS = 6000

# A model that keeps calling tools without ever answering is usually
# stuck in a loop - searching, not liking the result, searching again.
# Cutting it off costs one bad answer; not cutting it off costs the
# user's whole quota.


def _truncate(text, limit=MAX_RESULT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated at {limit} characters]"


# ---------------------------------------------------------------- specs

WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the live web. Use this for anything that happened "
            "recently, for prices, versions, release dates, or any fact "
            "that may have changed since training. Do not use it for "
            "general knowledge you already have."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms, as you would type "
                                   "them into a search engine.",
                },
            },
            "required": ["query"],
        },
    },
}

FIND_IMAGES = {
    "type": "function",
    "function": {
        "name": "find_images",
        "description": (
            "Find real photos on the internet: openly licensed images with "
            "URLs that load. Use it whenever the person wants pictures of "
            "something, or a page you are building needs photos. Put the "
            "returned URLs straight into <img src> or markdown images; "
            "never invent an image URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the photos should show, in 1-4 "
                                   "words, e.g. 'coffee shop interior'.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many photos, 1-8. Default 6.",
                },
            },
            "required": ["query"],
        },
    },
}

RUN_PYTHON = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "Execute Python in a sandbox and get back whatever it "
            "prints. Use it for arithmetic you would otherwise do in "
            "your head, data wrangling, or checking that code you just "
            "wrote actually runs. The sandbox has NO internet access "
            "and NO third-party packages - only the standard library. "
            "Nothing persists between calls. You must print() anything "
            "you want to see; expression values are not echoed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to run. Use print() "
                                   "for any result you need back.",
                },
            },
            "required": ["code"],
        },
    },
}

GENERATE_IMAGE = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Create an image from a text description and show it to the "
            "user. Use it when they ask for a picture, drawing, logo or "
            "illustration. Takes several seconds, so do not call it "
            "speculatively."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to draw. Be visually specific: "
                                   "subject, style, lighting, colours.",
                },
            },
            "required": ["prompt"],
        },
    },
}

ALL_SPECS = [WEB_SEARCH, RUN_PYTHON, GENERATE_IMAGE]


OPEN_PAGE = {
    "type": "function",
    "function": {
        "name": "open_page",
        "description": (
            "Open a web page in the workspace browser and read it. Use this "
            "when the user gives a link, asks what a page or site says, or "
            "after web_search when a result needs reading in full rather "
            "than from its snippet. The page also appears in the user's "
            "browser pane, so you can refer to it as on screen."),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The full address, e.g. https://example.com/page"},
            },
            "required": ["url"],
        },
    },
}


def available_specs(allow_images=True, allow_code=True, allow_web=True):
    """The tool list to advertise for this request.

    Offering a tool that then fails is worse than not offering it - the
    model spends a round trip discovering it doesn't work, and the user
    waits through it. So image generation is only advertised when a
    backend is actually configured.
    """
    specs = []
    if allow_web:
        specs.append(WEB_SEARCH)
        specs.append(OPEN_PAGE)
        specs.append(FIND_IMAGES)
    if allow_code:
        specs.append(RUN_PYTHON)
    # imagegen.available(), which is unconditionally true: the default
    # image backend needs no key at all, so there is nothing to gate on.
    if allow_images and imagegen.available():
        specs.append(GENERATE_IMAGE)
    return specs


# ------------------------------------------------------------ execution

def _do_web_search(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": "No query given.", "display": None}
    try:
        results = websearch.search(query, max_results=5)
    except Exception as e:                    # noqa: BLE001 - see module docstring
        return {"text": f"The search failed: {e}", "display": None}
    if not results:
        return {"text": f"No results for {query!r}.", "display": None}

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n"
                     f"    {r.get('url', '')}\n"
                     f"    {r.get('snippet', '')}")
    return {
        "text": _truncate("\n".join(lines)),
        # Handed to the UI so the same results render as source chips,
        # rather than the user having to take the model's word for it.
        "display": {"kind": "sources", "sources": results},
    }


def _do_find_images(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": "No query given.", "display": None}
    try:
        n = max(1, min(8, int(args.get("count") or 6)))
    except (TypeError, ValueError):
        n = 6
    photos = imagesearch.search(query, n)
    if not photos:
        return {"text": f"No photos found for {query!r}. Try simpler words.",
                "display": None}
    lines = []
    for i, p in enumerate(photos, 1):
        lines.append(f"[{i}] {p['url']}\n    {p['title']} - "
                     f"{p['credit']} ({p['license']})")
    lines.append(imagesearch.credit_line(photos))
    return {"text": _truncate("\n".join(lines)), "display": None}


def _do_run_python(args):
    code = args.get("code") or ""
    if not code.strip():
        return {"text": "No code given.", "display": None}
    try:
        out = codeexec.run_python(code)
    except Exception as e:                    # noqa: BLE001
        return {"text": f"The sandbox failed to start: {e}", "display": None}

    if out.get("error"):
        return {"text": f"Could not run it: {out['error']}", "display": None}

    parts = []
    if out.get("stdout"):
        parts.append("stdout:\n" + out["stdout"])
    if out.get("stderr"):
        parts.append("stderr:\n" + out["stderr"])
    if out.get("timed_out"):
        parts.append(f"[killed after {codeexec.TIMEOUT_SECONDS}s]")
    if not parts:
        parts.append("[ran fine, but printed nothing - "
                     "remember results are only visible via print()]")

    return {
        "text": _truncate("\n\n".join(parts)),
        "display": {"kind": "code", "code": code,
                    "stdout": out.get("stdout", ""),
                    "stderr": out.get("stderr", ""),
                    "timed_out": bool(out.get("timed_out"))},
    }


def _do_generate_image(args):
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"text": "No prompt given.", "display": None}
    # imagegen.best_backend() picks the best hosted backend that is
    # actually usable here. The old call was imagegen.gemini_configured(),
    # which stopped existing when Gemini was removed - a latent
    # AttributeError that only never fired because model-driven tool
    # calling is not currently wired up (see the note in app.py).
    backend = imagegen.best_backend()
    try:
        url, error = imagegen.generate_image(prompt, backend=backend)
    except Exception as e:                    # noqa: BLE001
        return {"text": f"Image generation failed: {e}", "display": None}
    if error or not url:
        return {"text": f"Image generation failed: {error}", "display": None}
    # The model is told it succeeded but not given the URL to repeat -
    # the UI renders the image from `display`, and a model that also
    # pastes the raw URL into its prose just makes a mess.
    return {
        "text": "The image was created and is already shown to the user. "
                "Describe it briefly; do not paste any URL.",
        "display": {"kind": "image", "url": url, "prompt": prompt},
    }


def _do_open_page(args):
    url = (args.get("url") or "").strip()
    if not url:
        return {"text": "No url given.", "display": None}
    try:
        page = browser.open_page(url)
    except browser.BrowseError as e:
        return {"text": "Could not open %s: %s" % (url, e), "display": None}
    head = "%s\n%s\n\n" % (page["title"], page["url"] or url)
    return {
        "text": _truncate(head + (page["text"] or "(no readable text)"), 9000),
        # The pane opens on the same page, so the reader sees what the
        # model read (static/js/browser.js).
        "display": {"kind": "page", "url": page["url"] or url, "title": page["title"]},
    }


HANDLERS = {
    "web_search": _do_web_search,
    "open_page": _do_open_page,
    "find_images": _do_find_images,
    "run_python": _do_run_python,
    "generate_image": _do_generate_image,
}


def dispatch(name, raw_arguments):
    """Run one tool call. Never raises.

    `raw_arguments` is whatever the model emitted, which is a JSON
    *string* and not necessarily valid - models do sometimes produce
    truncated or malformed argument blobs, and that has to read as a
    tool failure the model can retry, not a 500.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return {"text": f"There is no tool called {name!r}.",
                "display": None}

    if isinstance(raw_arguments, dict):
        args = raw_arguments
    else:
        try:
            args = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"text": "Your arguments were not valid JSON. "
                            "Call the tool again with correct JSON.",
                    "display": None}
        if not isinstance(args, dict):
            return {"text": "Arguments must be a JSON object.",
                    "display": None}

    try:
        return handler(args)
    except Exception as e:                    # noqa: BLE001
        return {"text": f"The tool errored: {e}", "display": None}
