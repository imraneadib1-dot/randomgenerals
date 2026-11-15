"""The website builder: a whole project, one careful step at a time.

A single reply that has to produce a complete website in one breath
gets one shot at everything - the plan, the markup, the styling, the
behaviour - and whatever it forgets stays forgotten. Measured on the
live site: no mobile menu on two of three pages, forms that went
nowhere, and one page that scrolled sideways on a phone.

This does what a person would. It plans first, then writes each file
in its own pass with the files before it in view, then checks the
files against each other and fixes what does not line up:

    plan          name, palette, type, sections, forms   (JSON)
    index.html    the page, against the plan
    styles.css    every class the page actually uses
    script.js     menu, reveal, year, and real form submission
    app.py        a Flask backend for the same forms, to self-host
    review        cross-file checks; a fix pass for each file that fails
    requirements.txt, README.md   written here, not by the model

THE CONTRACT is what lets the files agree without seeing each other
whole. It fixes the names that cross file boundaries - the menu button,
the body class it toggles, the form attribute, the form endpoint and
its JSON - so styles.css can style a menu index.html built and script.js
opens, and a form posts to an endpoint that both app.py and the hosted
site answer.

Nothing here talks to a model directly. The caller passes `llm`, a
generator that yields progress events while it waits (rate limits,
retries) and returns (text, finish_reason). That keeps the provider,
its budget and its failover in app.py, and lets this be tested with a
stand-in.
"""
from __future__ import annotations

import json
import re

FILE_ORDER = ["index.html", "styles.css", "script.js", "app.py",
              "requirements.txt", "README.md"]
LANG = {"index.html": "html", "styles.css": "css", "script.js": "javascript",
        "app.py": "python", "requirements.txt": "text", "README.md": "markdown"}
MAX_FILE_CHARS = 60000

CONTRACT = """PROJECT CONTRACT - every file must follow it exactly.
- Files: index.html, styles.css, script.js, app.py. index.html links styles.css in <head> (<link rel="stylesheet" href="styles.css">) and loads script.js with <script src="script.js" defer></script> right before </body>. No inline <style>, no style= attributes, no inline event handlers, no other local files.
- Header: <header class="site-header"> with a brand link and <nav id="site-nav" class="site-nav"> linking to each section id. A <button class="nav-toggle" aria-controls="site-nav" aria-expanded="false" aria-label="Menu"> is visible only below 768px. script.js toggles the class "nav-open" on <body> and keeps aria-expanded in step; styles.css hides the nav links below 768px unless body.nav-open, and closes nothing above 768px.
- Sections: one <section id="..."> per planned section, in plan order. The hero holds the page's only <h1>.
- Reveal: elements with class "reveal" start hidden and fade up when script.js adds "is-visible" (IntersectionObserver); with prefers-reduced-motion they are simply visible.
- Forms: <form class="rg-form" data-rg-form="FORM_NAME" novalidate> with a <label> and a named field for every planned field, a submit button, and <p class="form-status" role="status" aria-live="polite"></p> inside the form. script.js checks required fields and email format, POSTs JSON {"field": "value", ...} to "/api/forms/FORM_NAME" with Content-Type application/json, shows "Sending…" in .form-status, then the server's "message" on success (and resets the form) or its "error". The server answers {"ok": true, "message": "..."} or, with status 400, {"ok": false, "error": "..."}.
- Footer: <footer class="site-footer"> with the photo credit line inside <small class="credits"> and the year in <span data-year></span>, filled by script.js.
- Images: only the photo URLs provided, each <img> with width, height and real alt text; loading="lazy" on all but the hero."""

DESIGN = """You are a senior web designer and front-end engineer at a top studio, building a real, production-quality website for a client. Take your time and get every detail right: this is not a demo.
Design standards: a distinctive Google Fonts pairing (a characterful display face for headings, a clean sans for body); a palette in :root custom properties built around one bold accent; fluid type with clamp(); an 8px spacing rhythm and generous whitespace; containers around 1200px; CSS grid and flexbox; cards with soft shadows and rounded corners; buttons with hover, active and :focus-visible states; 200ms transitions; a sticky translucent header; smooth scrolling. Mobile-first and fully responsive - nothing scrolls sideways at 360px, grids stack, images keep an aspect-ratio with object-fit: cover. Text contrast at least 4.5:1.
Content standards: real, specific copy for this business - names, services or menu items with prices, opening hours, testimonials with first names and a detail that sounds lived. No lorem ipsum, no "Your text here". Use example.com emails and 555 phone numbers for invented contact details."""


# ---------------------------------------------------------------- helpers
def _fenced(text: str, lang_hint: str = "") -> str:
    """The body of the first fenced block, or the text itself when the
    model skipped the fence. An unterminated fence (a cut-off reply) is
    taken to the end."""
    text = text or ""
    m = re.search(r"(`{3,}|~{3,})[ \t]*([\w+-]*)[^\n]*\n", text)
    if not m:
        return text.strip()
    fence = m.group(1)
    body = text[m.end():]
    close = re.search(r"\n" + re.escape(fence[0]) + "{%d,}[ \t]*(\n|$)" % len(fence), body)
    return (body[:close.start()] if close else body).strip("\n")


def _unfence_continuation(text: str) -> str:
    text = re.sub(r"^\s*(`{3,}|~{3,})[\w+-]*[^\n]*\n", "", text or "")
    return re.sub(r"\n(`{3,}|~{3,})\s*$", "", text)


def _json_object(text: str) -> dict | None:
    text = _fenced(text) if "```" in (text or "") else (text or "")
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s[:32].strip("-") or "my-site")


def _field_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    return s[:32] or "field"


def log(message: str) -> None:
    """Where the builder says what went wrong. app.py points it at the
    app's logger; on its own it prints."""
    print("[sitebuilder] " + message, flush=True)


_NAME_STOP = set("""a an the for my our your website site page landing web html build make create
design me please with and that in of to called named small local new modern simple""".split())


def _name_from_brief(brief: str) -> str:
    """A name when the planner gave none: the one the brief states
    ("...called Ember & Oak"), else its subject ("Bakery Lyon")."""
    m = re.search(r"\b(?:called|named)\s+[\"']?([A-Z][\w'&.-]*(?:\s+(?:&\s+)?[A-Z][\w'&.-]*){0,4})", brief or "")
    if m:
        return m.group(1).strip().rstrip(".,;:!?")[:60]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", brief or "")
             if w.lower() not in _NAME_STOP]
    return " ".join(w.capitalize() for w in words[:2]) or "My Site"


def normalise_plan(plan: dict | None, brief: str) -> dict:
    """Fill in whatever the planner left out, so every later step can
    rely on the shape. A plan that did not parse at all becomes a
    sensible default rather than a failed build."""
    p = dict(plan or {})
    name = str(p.get("name") or "").strip()[:60] or _name_from_brief(brief)
    p["name"] = name
    p["slug"] = slugify(p.get("slug") or name)
    p["tagline"] = str(p.get("tagline") or "")[:140]
    pal = p.get("palette") if isinstance(p.get("palette"), dict) else {}
    p["palette"] = {k: str(pal.get(k) or v) for k, v in {
        "background": "#fbfaf7", "surface": "#ffffff", "text": "#1d1b19",
        "muted": "#6b645c", "accent": "#c4572a", "accent_text": "#ffffff",
    }.items()}
    fonts = p.get("fonts") if isinstance(p.get("fonts"), dict) else {}
    p["fonts"] = {"display": str(fonts.get("display") or "Fraunces"),
                  "body": str(fonts.get("body") or "Inter")}
    sections = [s for s in (p.get("sections") or []) if isinstance(s, dict) and s.get("id")]
    if len(sections) < 3:
        sections = [
            {"id": "home", "title": name, "content": "Hero: headline, subhead, call to action"},
            {"id": "about", "title": "About", "content": "Who they are and why it matters"},
            {"id": "services", "title": "What we offer", "content": "Services or menu with prices"},
            {"id": "testimonials", "title": "Kind words", "content": "Three testimonials"},
            {"id": "contact", "title": "Get in touch", "content": "Contact form, hours, address"},
        ]
    for s in sections:
        s["id"] = re.sub(r"[^a-z0-9-]+", "-", str(s["id"]).lower()).strip("-") or "section"
        s["title"] = str(s.get("title") or s["id"].title())[:80]
        s["content"] = str(s.get("content") or "")[:400]
    p["sections"] = sections[:8]
    forms = []
    for f in (p.get("forms") or []):
        if not isinstance(f, dict) or not f.get("name"):
            continue
        fields = []
        for fld in (f.get("fields") or []):
            if isinstance(fld, dict) and fld.get("name"):
                ftype = str(fld.get("type") or "text").lower()
                if ftype not in ("text", "email", "tel", "textarea", "date",
                                 "time", "number", "select"):
                    ftype = "text"
                fields.append({"name": _field_name(fld["name"]), "type": ftype,
                               "label": str(fld.get("label") or fld["name"])[:60],
                               "required": bool(fld.get("required", True))})
        if fields:
            forms.append({"name": _field_name(f["name"]),
                          "purpose": str(f.get("purpose") or "")[:200],
                          "success": str(f.get("success") or "Thanks - we'll be in touch soon.")[:200],
                          "fields": fields[:10]})
    if not forms:
        forms = [{"name": "contact", "purpose": "General enquiries",
                  "success": "Thanks - we'll get back to you within a day.",
                  "fields": [
                      {"name": "name", "type": "text", "label": "Your name", "required": True},
                      {"name": "email", "type": "email", "label": "Email", "required": True},
                      {"name": "message", "type": "textarea", "label": "Message", "required": True},
                  ]}]
    p["forms"] = forms[:3]
    return p


def _photo_lines(photos: list[dict]) -> str:
    if not photos:
        return ("No photos are available. Use rich CSS gradients, shapes and "
                "inline SVG instead of pictures. Never invent an image URL.")
    lines = ["Photos you may use (exact URLs; the first is usually the best hero):"]
    for i, p in enumerate(photos, 1):
        lines.append("%d. %s - %s (%sx%s)" % (i, p["url"], p.get("title", ""),
                                             p.get("width") or "?", p.get("height") or "?"))
    return "\n".join(lines)


# ---------------------------------------------------------------- checks
def check_files(files: dict, plan: dict) -> dict[str, list[str]]:
    """Cross-file problems, per file. Deterministic, so the review step
    costs nothing when everything already lines up."""
    issues: dict[str, list[str]] = {}
    html = files.get("index.html", "")
    css = files.get("styles.css", "")
    js = files.get("script.js", "")
    py = files.get("app.py", "")

    h = []
    low = html.lower()
    if "<!doctype html" not in low or "</html>" not in low:
        h.append("It must be a complete document from <!doctype html> to </html>.")
    if 'href="styles.css"' not in html:
        h.append('Link the stylesheet with <link rel="stylesheet" href="styles.css">.')
    if 'src="script.js"' not in html:
        h.append('Load the script with <script src="script.js" defer></script>.')
    if "nav-toggle" not in html or 'id="site-nav"' not in html:
        h.append('Include <nav id="site-nav" class="site-nav"> and the <button class="nav-toggle"> menu button.')
    if len(re.findall(r"<h1[\s>]", low)) != 1:
        h.append("Use exactly one <h1>, in the hero.")
    for f in plan.get("forms", []):
        if 'data-rg-form="%s"' % f["name"] not in html:
            h.append('Add the %s form: <form class="rg-form" data-rg-form="%s" novalidate> with fields %s and a <p class="form-status">.'
                     % (f["name"], f["name"], ", ".join(x["name"] for x in f["fields"])))
    if re.search(r"<style[\s>]|\sstyle=\"", html):
        h.append("Move all styling into styles.css: no <style> tags or style= attributes.")
    if re.search(r"placeholder\.com|picsum|dummyimage|source\.unsplash|via\.placeholder", low):
        h.append("Replace placeholder image services with the provided photo URLs.")
    if h:
        issues["index.html"] = h

    c = []
    used = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        used.update(attr.split())
    used -= {"reveal", "is-visible", "nav-open"}
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    missing = sorted(u for u in used if u not in defined)
    if used and len(missing) > max(3, len(used) // 5):
        c.append("These classes in index.html have no styles: %s." % ", ".join(missing[:40]))
    if "nav-open" not in css or "@media" not in css:
        c.append("Add the responsive rules: hide .site-nav links below 768px unless body.nav-open, and show .nav-toggle only there.")
    if ".reveal" not in css:
        c.append("Style .reveal (hidden, shifted down) and .reveal.is-visible (shown), with a prefers-reduced-motion override.")
    if c:
        issues["styles.css"] = c

    j = []
    if "/api/forms/" not in js:
        j.append('Submit forms with fetch to "/api/forms/" + form.dataset.rgForm as JSON.')
    if "nav-open" not in js:
        j.append('Toggle "nav-open" on document.body from the .nav-toggle button and update aria-expanded.')
    if "IntersectionObserver" not in js:
        j.append("Reveal .reveal elements with an IntersectionObserver.")
    if "data-year" not in js:
        j.append("Fill [data-year] with the current year.")
    if j:
        issues["script.js"] = j

    p = []
    try:
        compile(py, "app.py", "exec")
    except SyntaxError as e:
        p.append("Python syntax error on line %s: %s" % (e.lineno, e.msg))
    if "/api/forms/" not in py:
        p.append("Implement POST /api/forms/<form> as the contract says.")
    if "send_from_directory" not in py and "send_file" not in py:
        p.append("Serve index.html and the static files with send_from_directory.")
    if p:
        issues["app.py"] = p
    return issues


# ---------------------------------------------------------------- prompts
def _plan_messages(brief: str, photos: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": DESIGN + "\n\nYou are planning the site before anyone writes code. Think about who it is for and what they need to do on it. Reply with ONE JSON object and nothing else."},
        {"role": "user", "content": (
            "Brief: " + brief + "\n\n" + _photo_lines(photos) + "\n\n"
            "Plan the website as JSON with exactly these keys:\n"
            '{"name": "business name (invent a fitting one if none is given)",\n'
            ' "slug": "short-url-friendly-name",\n'
            ' "tagline": "one line",\n'
            ' "audience": "who visits and what they want",\n'
            ' "tone": "three adjectives",\n'
            ' "palette": {"background": "#hex", "surface": "#hex", "text": "#hex", "muted": "#hex", "accent": "#hex", "accent_text": "#hex"},\n'
            ' "fonts": {"display": "Google Font name", "body": "Google Font name"},\n'
            ' "sections": [{"id": "kebab-id", "title": "heading", "content": "exactly what goes in it - specific items, prices, names"}],\n'
            ' "forms": [{"name": "contact", "purpose": "...", "success": "message shown after sending", "fields": [{"name": "email", "type": "email|text|tel|textarea|date|time|number", "label": "Email", "required": true}]}],\n'
            ' "images": {"hero": 1, "others": [2, 3]}}\n'
            "5 to 7 sections, the first being the hero. 1 to 3 forms that make the site genuinely useful for this business (bookings, orders, enquiries, newsletter). Pick colours with strong contrast.")},
    ]


def _file_messages(name: str, brief: str, plan: dict, photos: list[dict],
                   files: dict, instruction: str = "") -> list[dict]:
    plan_json = json.dumps(plan, indent=1)
    head = DESIGN + "\n\n" + CONTRACT
    if name == "index.html":
        task = ("Write index.html: the complete page, following the plan section by section with real, specific content. "
                "Load the two planned Google Fonts with <link>. Give sections and cards the class \"reveal\". "
                "Use semantic HTML and descriptive class names (they will be styled in styles.css from this file alone).")
        ctx = ("Plan:\n" + plan_json + "\n\n" + _photo_lines(photos)
               + ("\n\nFooter photo credit line (put it in <small class=\"credits\">): "
                  + plan["credit"] if plan.get("credit") else ""))
    elif name == "styles.css":
        task = ("Write styles.css for this exact index.html: style every class and element it uses. "
                "Define the plan's palette and fonts as :root custom properties and use them everywhere. "
                "Make it look like a top studio built it, and make it work from 360px to wide desktop.")
        ctx = "Plan:\n" + plan_json + "\n\nindex.html:\n" + files.get("index.html", "")
    elif name == "script.js":
        task = ("Write script.js: plain modern JavaScript (no libraries), wrapped so nothing leaks into globals. "
                "Do exactly what the contract says for the menu, reveal, year and forms, with clear messages, "
                "disabled submit buttons while sending, and graceful failure if the network is down.")
        ctx = ("Forms (fields and success messages):\n" + json.dumps(plan.get("forms"), indent=1)
               + "\n\nindex.html:\n" + files.get("index.html", ""))
    elif name == "app.py":
        task = ("Write app.py: a small, production-sensible Flask backend for this site, to run it yourself. "
                "Serve index.html at / and the other files by name with send_from_directory (only files in this folder; 404 otherwise). "
                "Implement POST /api/forms/<form> for exactly these forms: validate required fields and email format, trim values to 2000 characters, "
                "store each submission in SQLite (submissions.db, table submissions: id, form, data JSON, created_at UTC ISO), "
                "and answer as the contract says with the form's success message. Unknown form -> 404 JSON. "
                "Add GET /api/forms/<form>/submissions that requires Authorization: Bearer <ADMIN_TOKEN from the environment> and returns the newest first. "
                "Rate-limit submissions to 10 per IP per 10 minutes in memory. Run on PORT from the environment (default 5000) with debug off. "
                "Comment the code where it helps someone extend it.")
        ctx = "Forms:\n" + json.dumps(plan.get("forms"), indent=1)
    else:
        raise ValueError(name)
    lang = LANG[name]
    user = ("Brief: " + brief + "\n\n" + ctx + "\n\n" + task
            + ("\n\nREQUESTED CHANGE - apply it fully, keep everything else that works:\n" + instruction if instruction else "")
            + ("\n\nCurrent " + name + " (rewrite it completely with the change applied):\n" + files[name]
               if instruction and files.get(name) else "")
            + "\n\nReply with the complete file in a single ```" + lang + " block and nothing else. Never stop part-way.")
    return [{"role": "system", "content": head}, {"role": "user", "content": user}]


def _fix_messages(name: str, plan: dict, files: dict, problems: list[str]) -> list[dict]:
    ctx = ""
    if name in ("styles.css", "script.js"):
        ctx = "index.html:\n" + files.get("index.html", "") + "\n\n"
    return [
        {"role": "system", "content": DESIGN + "\n\n" + CONTRACT},
        {"role": "user", "content": (
            ctx + "Current " + name + ":\n" + files.get(name, "") + "\n\n"
            "A review found these problems:\n- " + "\n- ".join(problems) + "\n\n"
            "Fix every one and return the complete corrected " + name + " in a single ```"
            + LANG[name] + " block and nothing else. Keep everything else as it is.")},
    ]


def _change_messages(instruction: str, plan: dict, files: dict) -> list[dict]:
    summary = {n: len(c) for n, c in files.items()}
    return [
        {"role": "system", "content": "You maintain a website project with files index.html, styles.css, script.js and app.py. Decide which files a requested change touches. Reply with ONE JSON object only."},
        {"role": "user", "content": (
            "Plan:\n" + json.dumps(plan, indent=1) + "\n\nFile sizes: " + json.dumps(summary)
            + "\n\nindex.html:\n" + files.get("index.html", "")[:12000]
            + "\n\nRequested change: " + instruction + "\n\n"
            'Reply as {"summary": "what you will change, one sentence", "files": ["index.html", ...], '
            '"plan": <the full plan with any updates, e.g. a new form or section, or null if unchanged>}. '
            "List only the files that must change. New or changed forms mean script.js and app.py may need to change too.")},
    ]


def readme(plan: dict) -> str:
    forms = "\n".join("- `POST /api/forms/%s` - %s (fields: %s)" % (
        f["name"], f.get("purpose") or f["name"], ", ".join(x["name"] for x in f["fields"]))
        for f in plan.get("forms", []))
    return f"""# {plan['name']}

{plan.get('tagline') or ''}

Built with RandomGenerals.

## Files

- `index.html` - the page
- `styles.css` - all styling ({plan['fonts']['display']} and {plan['fonts']['body']}, palette in `:root`)
- `script.js` - mobile menu, scroll reveal, and form submission
- `app.py` - a small Flask backend that serves the site and stores form submissions
- `requirements.txt` - Python dependencies

## Run it yourself

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
ADMIN_TOKEN=change-me python app.py
```

Open http://localhost:5000.

## Forms

{forms}

Submissions are saved in `submissions.db`. Read them with:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:5000/api/forms/<form>/submissions
```

## Publishing

Published on RandomGenerals, the site is served at `https://{plan['slug']}.randomgenerals.com`
and form submissions appear under **Your sites** in your account - no server to run.
"""


# ---------------------------------------------------------------- the build
# Below this, a file is not a file. Measured: a free model spent its
# whole allowance thinking and returned nothing, and the first real build
# saved a 0 KB styles.css, script.js and app.py as if all was well.
MIN_FILE_CHARS = {"index.html": 600, "styles.css": 400, "script.js": 150, "app.py": 200}


class EmptyFileError(RuntimeError):
    """The model would not produce a usable file, even when asked again."""


def _write(llm, name, label, messages, max_tokens, effort, small=False):
    """One file, continued if the model ran out of room. Generator; returns text."""
    text, finish = yield from llm(name, label, messages, max_tokens, effort, small)
    body = _fenced(text)
    if len(body.strip()) < MIN_FILE_CHARS.get(name, 1):
        # Almost always the thinking ate the allowance. Ask again with
        # less of it, and say plainly that the file itself is wanted.
        log("%s came back with %d chars; asking again" % (name, len(body.strip())))
        text, finish = yield from llm(
            name, label + " (second attempt)",
            messages + [{"role": "user", "content": "Your reply contained no file. Write the complete " + name + " now, in a single ```" + LANG[name] + " block. Keep your reasoning brief."}],
            max_tokens, "low", small)
        body = _fenced(text)
        if len(body.strip()) < MIN_FILE_CHARS.get(name, 1):
            raise EmptyFileError("The model did not write %s - try again in a minute." % name)
    rounds = 0
    # No practical ceiling on a file: continue until it is finished. Each
    # round sends only the END of the file so far - the whole of it would
    # outgrow the per-minute budget within a couple of rounds.
    while finish == "length" and rounds < 8:
        rounds += 1
        tail = body[-9000:]
        more, finish = yield from llm(
            name, label + " (part %d)" % (rounds + 1),
            messages[:1] + [messages[-1],
                            {"role": "assistant", "content": "```" + LANG[name] + "\n"
                             + ("/* ...earlier part above... */\n" if len(body) > 9000 else "") + tail},
                            {"role": "user", "content": "You stopped part-way. Continue EXACTLY where you stopped - no repetition, no fences, no commentary."}],
            max_tokens, "low", small)
        body += _unfence_continuation(more)
    return body[:MAX_FILE_CHARS]


def build(brief: str, photos: list[dict], credit: str, llm):
    """Plan and write a whole project. Generator: yields
    ("step", key, "start"|"done", label) events; returns the project
    {"name", "slug", "plan", "files", "fixed"}."""
    yield ("step", "plan", "start", "Planning the site")
    messages = _plan_messages(brief, photos)
    text, _ = yield from llm("plan", "Planning the site", messages, 3000, "medium", False)
    obj = _json_object(text)
    if obj is None:
        # One more go, asked plainly. A model that thought out loud or
        # wrapped the plan in prose usually gets it right when told so.
        log("plan did not parse; first 300 chars: %r" % (text or "")[:300])
        yield ("step", "plan", "start", "Planning the site (tidying the plan)")
        text, _ = yield from llm("plan", "Planning the site", messages + [
            {"role": "assistant", "content": (text or "")[:3000]},
            {"role": "user", "content": "That was not a valid JSON object. Reply with ONLY the JSON object, starting with { and ending with }."},
        ], 3000, "low", False)
        obj = _json_object(text)
        if obj is None:
            log("plan did not parse twice; building from the default plan")
    plan = normalise_plan(obj, brief)
    plan["credit"] = credit
    yield ("step", "plan", "done", "Planned %s: %d sections, %s" % (
        plan["name"], len(plan["sections"]), ", ".join(f["name"] + " form" for f in plan["forms"])))

    files: dict[str, str] = {}
    for name, effort, small, size in (("index.html", "medium", False, 7000),
                                      ("styles.css", "low", False, 6500),
                                      ("script.js", "low", True, 3500),
                                      ("app.py", "low", True, 4000)):
        yield ("step", name, "start", "Writing " + name)
        files[name] = yield from _write(llm, name, "Writing " + name,
                                        _file_messages(name, brief, plan, photos, files),
                                        size, effort, small)
        yield ("step", name, "done", "Wrote %s (%.1f KB)" % (name, len(files[name]) / 1024))

    fixed = yield from _review(llm, plan, files)
    files["requirements.txt"] = "flask>=3.0\n"
    files["README.md"] = readme(plan)
    return {"name": plan["name"], "slug": plan["slug"], "plan": plan,
            "files": {n: files[n] for n in FILE_ORDER if n in files}, "fixed": fixed}


def revise(instruction: str, project: dict, photos: list[dict], llm):
    """Apply a change to an existing project, rewriting only the files it
    touches. Same events and return shape as build()."""
    plan = project["plan"]
    files = dict(project["files"])
    yield ("step", "plan", "start", "Working out the change")
    text, _ = yield from llm("plan", "Working out the change",
                             _change_messages(instruction, plan, files), 2500, "medium", False)
    decision = _json_object(text) or {}
    if isinstance(decision.get("plan"), dict):
        plan = normalise_plan(decision["plan"], instruction)
        plan["slug"] = project["plan"].get("slug") or plan["slug"]
    plan["credit"] = plan.get("credit") or project["plan"].get("credit", "")
    touched = [n for n in ["index.html", "styles.css", "script.js", "app.py"]
               if n in (decision.get("files") or [])] or ["index.html", "styles.css"]
    yield ("step", "plan", "done", decision.get("summary") or "Changing " + ", ".join(touched))

    for name in ["index.html", "styles.css", "script.js", "app.py"]:
        if name not in touched:
            continue
        yield ("step", name, "start", "Updating " + name)
        files[name] = yield from _write(llm, name, "Updating " + name,
                                        _file_messages(name, instruction, plan, photos, files, instruction),
                                        6500 if name in ("index.html", "styles.css") else 4000,
                                        "low", name in ("script.js", "app.py"))
        yield ("step", name, "done", "Updated %s (%.1f KB)" % (name, len(files[name]) / 1024))

    fixed = yield from _review(llm, plan, files)
    files["requirements.txt"] = files.get("requirements.txt") or "flask>=3.0\n"
    files["README.md"] = readme(plan)
    return {"name": plan["name"], "slug": plan["slug"], "plan": plan,
            "files": {n: files[n] for n in FILE_ORDER if n in files}, "fixed": fixed,
            "touched": touched}


def _review(llm, plan, files):
    yield ("step", "review", "start", "Reviewing the files against each other")
    issues = check_files(files, plan)
    fixed = []
    for name in ["index.html", "styles.css", "script.js", "app.py"]:
        problems = issues.get(name)
        if not problems:
            continue
        yield ("step", "fix-" + name, "start", "Fixing %s (%d issue%s)" % (
            name, len(problems), "" if len(problems) == 1 else "s"))
        files[name] = yield from _write(llm, name, "Fixing " + name,
                                        _fix_messages(name, plan, files, problems),
                                        6500, "low", name in ("script.js", "app.py"))
        fixed.append(name)
        yield ("step", "fix-" + name, "done", "Fixed " + name)
    left = check_files(files, plan)
    yield ("step", "review", "done",
           "Review passed" if not left else "Reviewed (%d minor issue%s left)" % (
               sum(len(v) for v in left.values()), "" if sum(len(v) for v in left.values()) == 1 else "s"))
    return fixed
