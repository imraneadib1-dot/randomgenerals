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

_EXT_LANG = {".html": "html", ".css": "css", ".js": "javascript", ".py": "python",
             ".json": "json", ".rs": "rust", ".toml": "toml", ".md": "markdown",
             ".yml": "yaml", ".yaml": "yaml", ".ts": "typescript", ".txt": "text"}


def lang_of(name: str) -> str:
    """The fence language for a file, by name or extension."""
    if name in LANG:
        return LANG[name]
    if name == "Dockerfile":
        return "dockerfile"
    for ext, lang in _EXT_LANG.items():
        if name.endswith(ext):
            return lang
    return "text"

CONTRACT = """PROJECT CONTRACT - every file must follow it exactly.
- Files: index.html, styles.css, script.js, app.py. index.html links styles.css in <head> (<link rel="stylesheet" href="styles.css">) and loads script.js with <script src="script.js" defer></script> right before </body>. No inline <style>, no style= attributes, no inline event handlers, no other local files.
- Header: <header class="site-header"> with a brand link and <nav id="site-nav" class="site-nav"> linking to each section id. A <button class="nav-toggle" aria-controls="site-nav" aria-expanded="false" aria-label="Menu"> containing a visible hamburger icon (an inline SVG of three lines) is shown only below 768px. script.js toggles the class "nav-open" on <body> and keeps aria-expanded in step; styles.css hides the nav links below 768px unless body.nav-open, and closes nothing above 768px.
- Sections: one <section id="..."> per planned section, in plan order. The hero holds the page's only <h1>.
- Reveal: elements with class "reveal" start hidden and fade up when script.js adds "is-visible" (IntersectionObserver); with prefers-reduced-motion they are simply visible.
- Forms: <form class="rg-form" data-rg-form="FORM_NAME" novalidate> with a <label> and a named field for every planned field, a submit button, and <p class="form-status" role="status" aria-live="polite"></p> inside the form. script.js checks required fields and email format, POSTs JSON {"field": "value", ...} to "/api/forms/FORM_NAME" with Content-Type application/json, shows "Sending…" in .form-status, then the server's "message" on success (and resets the form) or its "error". The server answers {"ok": true, "message": "..."} or, with status 400, {"ok": false, "error": "..."}.
- Footer: <footer class="site-footer"> with the photo credit line inside <small class="credits"> and the year in <span data-year></span>, filled by script.js.
- Images: only the photo URLs provided, each <img> with width, height and real alt text; loading="lazy" on all but the hero."""

DESIGN = """You are a senior web designer and front-end engineer at a top studio, building a real, production-quality website for a client. Take your time and get every detail right: this is not a demo.
Design standards: a distinctive Google Fonts pairing (a characterful display face for headings, a clean sans for body); a palette in :root custom properties built around one bold accent; fluid type with clamp(); an 8px spacing rhythm and generous whitespace; containers around 1200px; CSS grid and flexbox; cards with soft shadows and rounded corners; buttons with hover, active and :focus-visible states; 200ms transitions; a sticky translucent header; smooth scrolling. Mobile-first and fully responsive - nothing scrolls sideways at 360px, grids stack, images keep an aspect-ratio with object-fit: cover. Text contrast at least 4.5:1.
Content standards: real, specific copy for this business - names, services or menu items with prices, opening hours, testimonials with first names and a detail that sounds lived. No lorem ipsum, no "Your text here". Use example.com emails and 555 phone numbers for invented contact details."""


SAFETY_MARK = "/* RandomGenerals safety net */"
SAFETY_CSS = (SAFETY_MARK + "\n"
              "/* Nothing may widen the page on a phone: an off-canvas menu parked at\n"
              "   right: -100% made a live build scroll sideways. */\n"
              "html, body { max-width: 100%; overflow-x: clip; }\n"
              "img, video, iframe { max-width: 100%; }\n"
              "img { height: auto; }\n")


MENU_ICON = ('<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
             '<path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" fill="none"/></svg>')


def add_safety(files: dict) -> None:
    """The guarantees every build ships with, whatever the model wrote:
    the stylesheet's safety net, once, and a menu button you can see.
    A live build wrote <button class="nav-toggle"></button> - empty, no
    icon, no CSS to draw one - so on a phone the menu could not be
    opened at all. An empty button gets an icon that needs no CSS."""
    for name in ("styles.css", "css/main.css"):
        if name in files and SAFETY_MARK not in files[name]:
            files[name] = files[name].rstrip() + "\n\n" + SAFETY_CSS
    html = files.get("index.html")
    if html:
        files["index.html"] = re.sub(
            r'(<button\b[^>]*class="[^"]*\bnav-toggle\b[^"]*"[^>]*>)\s*(</button>)',
            lambda m: m.group(1) + MENU_ICON + m.group(2), html, count=1)


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
    lines = ["Photos you may use (exact URLs). They come from open photo libraries and are not "
             "all good: use only those whose description clearly fits this business, and skip "
             "any that are off-topic, show another named business or its sign, or look like "
             "archive, museum or diagram images. Fewer photos - or a rich gradient hero - are "
             "better than a wrong one. Never invent an image URL."]
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
        ctx = "Plan:\n" + plan_json + "\n\nPage outline (every tag, class and id of index.html):\n" + _html_outline(files.get("index.html", ""))
    elif name == "script.js":
        task = ("Write script.js: plain modern JavaScript (no libraries), wrapped so nothing leaks into globals. "
                "Do exactly what the contract says for the menu, reveal, year and forms, with clear messages, "
                "disabled submit buttons while sending, and graceful failure if the network is down.")
        ctx = ("Forms (fields and success messages):\n" + json.dumps(plan.get("forms"), indent=1)
               + "\n\nPage outline:\n" + _html_outline(files.get("index.html", "")))
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
    lang = lang_of(name)
    user = ("Brief: " + brief + "\n\n" + ctx + "\n\n" + task
            + ("\n\nREQUESTED CHANGE - apply it fully, keep everything else that works:\n" + instruction if instruction else "")
            + ("\n\nCurrent " + name + " (rewrite it completely with the change applied):\n" + files[name]
               if instruction and files.get(name) else "")
            + "\n\nReply with the complete file in a single ```" + lang + " block and nothing else. Never stop part-way.")
    return [{"role": "system", "content": head}, {"role": "user", "content": user}]


def _fix_messages(name: str, plan: dict, files: dict, problems: list[str]) -> list[dict]:
    ctx = ""
    if name in ("styles.css", "script.js"):
        ctx = "Page outline:\n" + _html_outline(files.get("index.html", "")) + "\n\n"
    return [
        {"role": "system", "content": DESIGN + "\n\n" + CONTRACT},
        {"role": "user", "content": (
            ctx + "Current " + name + ":\n" + files.get(name, "") + "\n\n"
            "A review found these problems:\n- " + "\n- ".join(problems) + "\n\n"
            "Fix every one and return the complete corrected " + name + " in a single ```"
            + lang_of(name) + " block and nothing else. Keep everything else as it is.")},
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
MIN_FILE_CHARS = {"index.html": 600, "styles.css": 400, "script.js": 150, "app.py": 200,
                  "css/tokens.css": 150, "css/main.css": 600, "js/core.js": 200,
                  "js/ui.js": 200, "js/forms.js": 150, "js/app.js": 300,
                  "backend/app.py": 300, "backend/storage.py": 150, "wasm/src/lib.rs": 150}


class EmptyFileError(RuntimeError):
    """The model would not produce a usable file, even when asked again."""


def _write(llm, name, label, messages, max_tokens, effort, small=False, min_chars=None):
    """One file, continued if the model ran out of room. Generator; returns text.
    `min_chars` overrides the size below which a reply counts as empty -
    a handful of CSS rules added in review is short, and still real."""
    floor = MIN_FILE_CHARS.get(name, 1) if min_chars is None else min_chars
    text, finish = yield from llm(name, label, messages, max_tokens, effort, small)
    body = _fenced(text)
    if len(body.strip()) < floor:
        # Almost always the thinking ate the allowance. Ask again with
        # less of it, and say plainly that the file itself is wanted.
        log("%s came back with %d chars; asking again" % (name, len(body.strip())))
        # The instruction is added to the SAME request, not sent as a new
        # message: a third message lets the budget fitter drop the task
        # itself to make room, and the model then writes a file from
        # nothing - which is how a live build replaced a 22 KB stylesheet
        # with a generic 8 KB one that matched none of the page.
        retry = messages[:-1] + [{"role": "user", "content": messages[-1]["content"]
                                  + "\n\nIMPORTANT: write the file itself now, complete, in a single ```"
                                  + lang_of(name) + " block. Keep your reasoning brief."}]
        text, finish = yield from llm(name, label + " (second attempt)", retry,
                                      max_tokens, "low", small)
        body = _fenced(text)
        if len(body.strip()) < floor:
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
                            {"role": "assistant", "content": "```" + lang_of(name) + "\n"
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
    add_safety(files)
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
    add_safety(files)
    files["requirements.txt"] = files.get("requirements.txt") or "flask>=3.0\n"
    files["README.md"] = readme(plan)
    return {"name": plan["name"], "slug": plan["slug"], "plan": plan,
            "files": {n: files[n] for n in FILE_ORDER if n in files}, "fixed": fixed,
            "touched": touched}


def _review(llm, plan, files):
    yield ("step", "review", "start", "Reviewing the files against each other")
    fixed = yield from _fix_all(llm, plan, files, premium=False)
    left = check_files(files, plan)
    n = sum(len(v) for v in left.values())
    yield ("step", "review", "done", "Review passed" if not n else
           "Reviewed (%d minor issue%s left)" % (n, "" if n == 1 else "s"))
    return fixed


# ---- fixing what the review found ---------------------------------------
# PATCH, DON'T REWRITE. The first live build with this review asked the
# model to rewrite a complete 22 KB stylesheet to fix one missing rule.
# Page plus stylesheet did not fit in one request's budget, the reply
# allowance was squeezed to nothing, and the retry lost the task: the
# project shipped a generic 8 KB stylesheet that styled none of its own
# page. So the review now repairs what it can by itself (a missing
# <link> or <script> is a line, not a rewrite), ADDS the CSS rules that
# are missing instead of regenerating the sheet, rewrites only files
# small enough to fit, and refuses any rewrite that comes back much
# smaller than what it replaces. A fix that fails leaves the file as it
# was; it never stops the build.
REWRITE_LIMIT = 14000


def _repair_html(html: str, premium: bool) -> str:
    """Insert missing stylesheet links and script tags - no model needed."""
    css = ["css/tokens.css", "css/main.css"] if premium else ["styles.css"]
    js = PREMIUM_JS if premium else ["script.js"]
    for href in css:
        if 'href="%s"' % href not in html and "</head>" in html:
            html = html.replace("</head>", '<link rel="stylesheet" href="%s">\n</head>' % href, 1)
    for src in js:
        if 'src="%s"' % src not in html and "</body>" in html:
            html = html.replace("</body>", '<script src="%s" defer></script>\n</body>' % src, 1)
    return html


def _css_additions_messages(name, plan, files, problems, premium):
    tokens = files.get("css/tokens.css", "") if premium else ""
    current = files.get(name, "")
    root = (re.search(r":root\s*\{[^}]*\}", tokens or current) or [""])[0] if (tokens or current) else ""
    return [
        {"role": "system", "content": (PREMIUM_DESIGN if premium else DESIGN) + "\n\n"
         + (PREMIUM_CONTRACT if premium else CONTRACT)},
        {"role": "user", "content": (
            "The stylesheet " + name + " already exists and is good; do NOT rewrite it. "
            "A review found it is missing rules:\n- " + "\n- ".join(problems) + "\n\n"
            "Design tokens in use:\n" + root[:2500] + "\n\n"
            "Page outline:\n" + _html_outline(files.get("index.html", ""), 8000) + "\n\n"
            "Write ONLY the additional CSS rules that fix these problems, in the same visual style, "
            "using the existing custom properties. They will be appended to the file. "
            "Reply with a single ```css block and nothing else.")},
    ]


def _fix_one(llm, name, plan, files, problems, premium):
    """Fix one file. Generator; returns True when the file changed."""
    before = files.get(name, "")
    try:
        if name == "index.html":
            repaired = _repair_html(before, premium)
            if repaired != before:
                files[name] = before = repaired
                left = (check_premium if premium else check_files)(files, plan).get(name)
                if not left:
                    return True
                problems = left
        if name in ("styles.css", "css/main.css"):
            more = yield from _write(llm, name, "Adding the missing styles",
                                     _css_additions_messages(name, plan, files, problems, premium),
                                     4000, "low", False, min_chars=15)
            files[name] = before.rstrip() + "\n\n/* Added in review */\n" + more.strip() + "\n"
            return True
        if len(before) > REWRITE_LIMIT:
            log("%s is %d chars - too big to rewrite safely; left as written" % (name, len(before)))
            return False
        messages = (_premium_fix_messages if premium else _fix_messages)(name, plan, files, problems)
        after = yield from _write(llm, name, "Fixing " + name, messages, 6500, "low",
                                  name.startswith(("js/core", "js/forms", "backend/"))
                                  or name in ("script.js", "app.py"))
        if before and len(after) < 0.6 * len(before):
            log("rewrite of %s shrank %d -> %d chars; kept the original" % (name, len(before), len(after)))
            return False
        files[name] = after
        return True
    except EmptyFileError as e:
        log("could not fix %s: %s" % (name, e))
        files[name] = before
        return False


def _fix_all(llm, plan, files, premium):
    issues = (check_premium if premium else check_files)(files, plan)
    fixed = []
    for name in list(issues):
        if name not in files:
            continue
        problems = issues[name]
        yield ("step", "fix-" + name, "start", "Fixing %s (%d issue%s)" % (
            name, len(problems), "" if len(problems) == 1 else "s"))
        changed = yield from _fix_one(llm, name, plan, files, problems, premium)
        if changed:
            fixed.append(name)
        yield ("step", "fix-" + name, "done", ("Fixed " if changed else "Kept ") + name)
    return fixed

# =====================================================================
# PREMIUM BUILDS
#
# Asked for something "premium", "sick", "high-end" - or in Deep mode -
# the builder does the slow, thorough version: research first, then an
# architecture, then a real workspace rather than four files.
#
#   research     web results on the domain, condensed into a design
#                direction and real facts (the model reads them)
#   plan         features, datasets, API, and whether heavy computation
#                earns a Rust/WASM module
#   served       index.html, css/tokens.css + css/main.css,
#                js/core.js ui.js forms.js app.js, data/*.json
#   backend      backend/app.py + backend/storage.py (self-hosting)
#   wasm         wasm/ crate, only when the plan says it is needed
#   scaffolding  Dockerfile, docker-compose.yml, .env.example,
#                .dockerignore, package.json, tsconfig.json, README -
#                written here from templates, so they are always right
#
# Everything a browser loads runs as-is, so the site publishes exactly
# like a standard one; the rest is the workspace you download. The JS is
# type-checked JavaScript (// @ts-check + JSDoc, tsc --checkJs) rather
# than .ts files - one codebase, TypeScript's checking, no build step
# between writing it and publishing it.
#
# THE BUDGET SHAPES THE CONTEXT. Groq's free tier allows about 8,000
# tokens a minute per model, prompt included, and the page alone can be
# most of that. So later steps see the page as an OUTLINE - its tags,
# classes, ids and data attributes without the copy - and the shared JS
# as a list of what it defines, not its source.
# =====================================================================

PREMIUM_JS = ["js/core.js", "js/ui.js", "js/forms.js", "js/app.js"]

PREMIUM_CONTRACT = """PREMIUM PROJECT CONTRACT - every file must follow it exactly.
Served to visitors (no build step - browsers run these files as written):
- index.html links css/tokens.css then css/main.css in <head>, and loads js/core.js, js/ui.js, js/forms.js, js/app.js in that order, each with <script src="..." defer>, right before </body>. No inline <style>, style= attributes, inline <script> or inline event handlers. No frameworks and no CDNs except Google Fonts.
- css/tokens.css holds ONLY design tokens as :root custom properties - colours, type scale (clamp), spacing scale, radii, shadows, motion durations and easings - plus a dark variant under [data-theme="dark"]. css/main.css styles everything using only those tokens.
- JavaScript: plain modern JS. Every file starts with // @ts-check, documents functions and data shapes with JSDoc, and is one IIFE that adds to a single namespace: window.Site = window.Site || {}.
  core.js defines Site.$(sel, root), Site.$$(sel, root) returning an array, Site.on(root, event, selector, handler) for delegation, Site.store.get(key, fallback) / Site.store.set(key, value) (localStorage, JSON, try/catch), Site.money(amount, currency), and Site.bus.on(name, fn) / Site.bus.emit(name, detail).
  ui.js: the menu (toggles body.nav-open, keeps aria-expanded in step), reveal (.reveal gets .is-visible via IntersectionObserver; visible at once under prefers-reduced-motion), Site.modal.open(id) / Site.modal.close() for <dialog class="modal" id="..."> elements with focus return and Escape, Site.toast(message), and [data-year].
  forms.js: every form[data-rg-form] - required fields and email checked, JSON POSTed to "/api/forms/" + its name, "Sending…" then the server's message (resetting the form) or its error, shown in the form's .form-status; the submit button disabled while sending. The server answers {"ok": true, "message": "..."} or, with status 400, {"ok": false, "error": "..."}.
  app.js: the site's own features from the plan (cart, quick view, filters, bookings...). Each planned dataset is fetched from data/NAME.json and rendered into every [data-list="NAME"] container, with a loading state and a friendly error state. State that should survive a reload uses Site.store.
- Header: <header class="site-header"> with a brand link, <nav id="site-nav" class="site-nav">, and <button class="nav-toggle" aria-controls="site-nav" aria-expanded="false" aria-label="Menu"> containing a visible hamburger icon (an inline SVG of three lines), shown only below 768px.
- Sections: one <section id="..."> per planned section in plan order; the hero holds the only <h1>. Sections and cards carry class "reveal".
- Forms: <form class="rg-form" data-rg-form="NAME" novalidate> with a <label> and named field per planned field, a submit button and <p class="form-status" role="status" aria-live="polite"></p>.
- Footer: <footer class="site-footer"> with the photo credit line in <small class="credits"> and <span data-year></span>.
- Images: only provided photo URLs (or URLs inside the datasets, which come from the same list), each <img> with width, height and real alt text; loading="lazy" except the hero.
For self-hosting (downloaded, never run by the publishing service):
- backend/app.py: a Flask app object named `app`, importable as backend.app and runnable with `python -m backend.app` from the project root. It serves the static site from the project root (index.html, css/, js/, data/ only), implements POST /api/forms/<form> exactly as above for the planned forms, GET /api/forms/<form>/submissions behind Authorization: Bearer <ADMIN_TOKEN>, and any planned API endpoints. It imports persistence with `from backend import storage`. Configuration only from environment variables (ADMIN_TOKEN, PORT default 8000, STORAGE_DIR default ./storage). Rate-limit form posts per IP.
- backend/storage.py: SQLite in STORAGE_DIR, created on first use, with the functions backend/app.py needs; parameterised queries only."""

PREMIUM_DESIGN = DESIGN + """
This is a PREMIUM build. Work like a principal engineer and a design director: deliberate typography, a confident layout with real visual hierarchy, purposeful micro-interactions (hover states, transitions, a subtle reveal), and polish in every state - empty, loading, error, success. Every file is complete and production-ready: no TODOs, no placeholder comments, no "add styles here", no stubbed functions."""


def _html_outline(html: str, limit: int = 12000) -> str:
    """The page without its words: tags, classes, ids and data
    attributes in order. What a stylesheet or a script needs to know
    about the markup, at a fraction of the tokens."""
    s = re.sub(r"<!--.*?-->", "", html or "", flags=re.S)
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"\s(alt|title|placeholder|content|aria-label|href|src|srcset|width|height|loading|rel|lang|charset|name=\"viewport\")=\"[^\"]*\"", "", s)
    s = re.sub(r">[^<]+<", "><", s)
    s = re.sub(r"\s+", " ", s)
    s = s.replace("><", ">\n<")
    return s[:limit]


def _js_api(files: dict) -> str:
    """What the shared scripts define - Site.x = ... and JSDoc @typedef
    lines - so app.js can use them without their source."""
    lines = []
    for name in PREMIUM_JS[:-1]:
        src = files.get(name, "")
        defs = re.findall(r"^\s*(Site\.[\w.]+\s*=\s*(?:function\s*)?\(?[^)\n]{0,80}\)?)", src, re.M)
        types = re.findall(r"@typedef\s+\{[^}]+\}\s+\w+", src)
        if defs or types:
            lines.append(name + ": " + "; ".join(dict.fromkeys(d.strip() for d in defs + types)))
    return "\n".join(lines)[:3000]


def normalise_premium_plan(plan: dict | None, brief: str) -> dict:
    p = normalise_plan(plan, brief)
    raw = plan or {}
    p["premium"] = True
    p["design_direction"] = str(raw.get("design_direction") or "")[:800]
    p["features"] = [str(f)[:200] for f in (raw.get("features") or []) if f][:10]
    datasets = []
    for d in (raw.get("datasets") or []):
        if isinstance(d, dict) and d.get("name"):
            datasets.append({"name": _field_name(d["name"]).replace("_", "-"),
                             "purpose": str(d.get("purpose") or "")[:200],
                             "fields": d.get("fields") if isinstance(d.get("fields"), dict) else {},
                             "count": max(1, min(24, int(d.get("count") or 6)))})
    p["datasets"] = datasets[:3]
    p["api"] = [a for a in (raw.get("api") or []) if isinstance(a, dict) and a.get("path")][:6]
    p["needs_compute"] = bool(raw.get("needs_compute"))
    p["compute"] = str(raw.get("compute") or "")[:400] if p["needs_compute"] else ""
    return p


def _premium_plan_messages(brief, photos, research):
    return [
        {"role": "system", "content": PREMIUM_DESIGN + "\n\nYou are the principal architect planning a premium website before anyone writes code. Use the research. Reply with ONE JSON object and nothing else."},
        {"role": "user", "content": (
            "Brief: " + brief + "\n\nResearch notes from the web:\n" + (research or "(none)") + "\n\n"
            + _photo_lines(photos) + "\n\n"
            "Plan it as JSON with these keys:\n"
            '{"name": "...", "slug": "...", "tagline": "...", "audience": "...", "tone": "...",\n'
            ' "design_direction": "3-5 sentences: the layout idea, typography, colour use, imagery and motion",\n'
            ' "palette": {"background": "#hex", "surface": "#hex", "text": "#hex", "muted": "#hex", "accent": "#hex", "accent_text": "#hex"},\n'
            ' "fonts": {"display": "Google Font", "body": "Google Font"},\n'
            ' "sections": [{"id": "kebab-id", "title": "...", "content": "exactly what goes in it"}],\n'
            ' "features": ["each interactive feature, specifically - e.g. slide-out cart with quantities and subtotal"],\n'
            ' "datasets": [{"name": "products", "purpose": "...", "count": 6, "fields": {"id": "string", "name": "string", "price": "number", "image": "photo URL", "...": "..."}}],\n'
            ' "forms": [{"name": "...", "purpose": "...", "success": "...", "fields": [{"name": "...", "type": "email|text|tel|textarea|date|time|number", "label": "...", "required": true}]}],\n'
            ' "api": [{"method": "GET", "path": "/api/...", "purpose": "only if the self-hosted backend genuinely needs it"}],\n'
            ' "needs_compute": false, "compute": "only if true: the heavy computation a Rust/WASM module would do"}\n'
            "5-8 sections. Content the visitor browses (products, menu items, rooms, classes...) belongs in a dataset, not hard-coded. "
            "Set needs_compute true ONLY for genuinely heavy computation (simulation, image processing, large calculations) - never for ordinary sites.")},
    ]


def _research_messages(brief, notes):
    return [
        {"role": "system", "content": "You are a design researcher. Condense web research into guidance for a design team. Be concrete and brief."},
        {"role": "user", "content": (
            "Project: " + brief + "\n\nSearch results:\n" + notes + "\n\n"
            "Write at most 12 bullet points: what visitors to this kind of site expect, layout and interaction patterns that work in this domain right now, typography and colour directions that fit, and any real facts worth using in the copy. No preamble.")},
    ]


def _premium_file_messages(name, brief, plan, photos, files, instruction=""):
    plan_json = json.dumps({k: v for k, v in plan.items() if k not in ("credit",)}, indent=1)
    outline = _html_outline(files.get("index.html", ""))
    ds = json.dumps(plan.get("datasets") or [], indent=1)
    if name.startswith("data/") and name.endswith(".json"):
        dname = name[5:-5]
        spec = next((d for d in plan.get("datasets", []) if d["name"] == dname), {})
        task = ("Write data/%s.json: a JSON array of %d realistic, specific items for this business (%s), each with exactly these fields: %s. "
                "Use the provided photo URLs for any image field (spread them across items; reuse is fine), real-sounding names and copy, sensible prices. Output valid JSON only."
                % (dname, spec.get("count", 6), spec.get("purpose", ""), json.dumps(spec.get("fields", {}))))
        ctx = "Plan:\n" + plan_json + "\n\n" + _photo_lines(photos)
    elif name == "index.html":
        task = ("Write index.html following the plan section by section, with real, specific copy. Load the two Google Fonts. "
                "Every planned dataset is rendered by app.js into an empty container <div data-list=\"NAME\" class=\"...\"></div> - do not hard-code those items. "
                "Include the <dialog class=\"modal\"> elements the features need (e.g. quick view, cart drawer) with their inner structure and ids, and the buttons that open them (data-open=\"dialog-id\").")
        ctx = ("Plan:\n" + plan_json + "\n\n" + _photo_lines(photos)
               + ("\n\nFooter photo credit line: " + plan["credit"] if plan.get("credit") else ""))
    elif name == "css/tokens.css":
        task = "Write css/tokens.css: only :root design tokens (and the [data-theme=\"dark\"] variant) built from the plan's palette, fonts and design direction - a complete, considered token system."
        ctx = "Plan:\n" + plan_json
    elif name == "css/main.css":
        task = ("Write css/main.css: style every element and class in this page outline - including dialogs, cart, product cards, hover states, loading and error states, and the items app.js renders into [data-list] containers (style them by the classes the plan's features imply: .card, .product-card etc.) - using only the tokens in css/tokens.css. Responsive from 360px to wide desktop.")
        ctx = "Plan:\n" + plan_json + "\n\ncss/tokens.css:\n" + files.get("css/tokens.css", "")[:5000] + "\n\nPage outline:\n" + outline
    elif name == "js/core.js":
        task = "Write js/core.js exactly as the contract defines it, with JSDoc on every function."
        ctx = "Features:\n" + json.dumps(plan.get("features"), indent=1)
    elif name == "js/ui.js":
        task = "Write js/ui.js exactly as the contract defines it: menu, reveal, modal (also open any dialog from a [data-open] button and close from [data-close]), toast, year."
        ctx = "Page outline:\n" + outline + "\n\nShared API:\n" + _js_api(files)
    elif name == "js/forms.js":
        task = "Write js/forms.js exactly as the contract defines it."
        ctx = "Forms:\n" + json.dumps(plan.get("forms"), indent=1) + "\n\nShared API:\n" + _js_api(files)
    elif name == "js/app.js":
        task = ("Write js/app.js: every planned feature, completely - rendering each dataset into its [data-list] containers with markup that matches the classes styles use, "
                "and all the interactions (e.g. cart: add, change quantity, remove, subtotal, item count badge, persisted with Site.store; quick view: fill and open the dialog). Use Site.$, Site.on, Site.modal, Site.toast, Site.money.")
        ctx = ("Plan features:\n" + json.dumps(plan.get("features"), indent=1) + "\n\nDatasets:\n" + ds
               + "\n\nPage outline:\n" + outline + "\n\nShared API:\n" + _js_api(files))
    elif name == "backend/storage.py":
        task = "Write backend/storage.py: SQLite persistence in STORAGE_DIR for form submissions (and anything the planned API needs), created on first use, parameterised queries, type hints and docstrings."
        ctx = "Forms:\n" + json.dumps(plan.get("forms"), indent=1) + "\n\nAPI:\n" + json.dumps(plan.get("api"), indent=1)
    elif name == "backend/app.py":
        task = ("Write backend/app.py exactly as the contract defines it, using backend/storage.py's functions, with validation per the planned forms, JSON errors, "
                "a per-IP rate limit, security headers on responses, type hints and docstrings. Serve index.html at / and only files under css/, js/, data/.")
        ctx = ("Forms:\n" + json.dumps(plan.get("forms"), indent=1) + "\n\nAPI:\n" + json.dumps(plan.get("api"), indent=1)
               + "\n\nbackend/storage.py:\n" + files.get("backend/storage.py", "")[:6000])
    elif name == "wasm/src/lib.rs":
        task = ("Write wasm/src/lib.rs: a Rust crate using wasm-bindgen that implements this computation with #[wasm_bindgen] exported functions, doc comments, input validation and unit tests: "
                + plan.get("compute", "") + ". app.js ships a JavaScript fallback with the same function names, so the site works before the module is built.")
        ctx = "Plan:\n" + plan_json
    else:
        raise ValueError(name)
    lang = lang_of(name)
    user = ("Brief: " + brief + "\n\n" + ctx + "\n\n" + task
            + ("\n\nREQUESTED CHANGE - apply it fully, keep everything else that works:\n" + instruction if instruction else "")
            + ("\n\nCurrent " + name + " (rewrite it completely with the change applied):\n" + files[name][:12000]
               if instruction and files.get(name) else "")
            + "\n\nReply with the complete file in a single ```" + lang + " block and nothing else.")
    return [{"role": "system", "content": PREMIUM_DESIGN + "\n\n" + PREMIUM_CONTRACT}, {"role": "user", "content": user}]


def check_premium(files: dict, plan: dict) -> dict[str, list[str]]:
    issues: dict[str, list[str]] = {}
    html = files.get("index.html", "")
    low = html.lower()
    h = []
    if "<!doctype html" not in low or "</html>" not in low:
        h.append("It must be a complete document from <!doctype html> to </html>.")
    for css in ("css/tokens.css", "css/main.css"):
        if 'href="%s"' % css not in html:
            h.append('Link %s in <head>.' % css)
    for js in PREMIUM_JS:
        if 'src="%s"' % js not in html:
            h.append('Load %s with <script src="%s" defer></script> before </body>, in contract order.' % (js, js))
    if "nav-toggle" not in html or 'id="site-nav"' not in html:
        h.append('Include <nav id="site-nav" class="site-nav"> and the <button class="nav-toggle"> menu button.')
    if len(re.findall(r"<h1[\s>]", low)) != 1:
        h.append("Use exactly one <h1>, in the hero.")
    for d in plan.get("datasets", []):
        if 'data-list="%s"' % d["name"] not in html:
            h.append('Add an empty <div data-list="%s"> container for the %s dataset.' % (d["name"], d["name"]))
    for f in plan.get("forms", []):
        if 'data-rg-form="%s"' % f["name"] not in html:
            h.append('Add the %s form with data-rg-form="%s".' % (f["name"], f["name"]))
    if re.search(r"<style[\s>]|\sstyle=\"|<script>(?!\s*</script>)", html):
        h.append("No inline styles or scripts: everything goes in css/ and js/.")
    if h:
        issues["index.html"] = h

    css = files.get("css/tokens.css", "") + files.get("css/main.css", "")
    c = []
    used = set()
    for attr in re.findall(r'class="([^"]+)"', html):
        used.update(attr.split())
    used -= {"reveal", "is-visible", "nav-open"}
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    missing = sorted(u for u in used if u not in defined)
    if used and len(missing) > max(3, len(used) // 5):
        c.append("These classes in index.html have no styles: %s." % ", ".join(missing[:40]))
    if "nav-open" not in css or "@media" not in css or ".reveal" not in css:
        c.append("Add the responsive menu rules (body.nav-open), media queries and the .reveal / .reveal.is-visible states.")
    if ":root" not in files.get("css/tokens.css", ""):
        c.append("css/tokens.css must define the tokens on :root.")
    if c:
        issues["css/main.css"] = c

    for name, needles in (("js/core.js", ["Site.$", "Site.store", "Site.bus"]),
                          ("js/ui.js", ["nav-open", "IntersectionObserver", "Site.modal", "data-year"]),
                          ("js/forms.js", ["/api/forms/"]),
                          ("js/app.js", ["data/"] if plan.get("datasets") else [])):
        src = files.get(name, "")
        j = [("It must define/use %s as the contract says." % n) for n in needles if n not in src]
        if not src.lstrip().startswith("// @ts-check"):
            j.append("Start the file with // @ts-check.")
        if re.search(r"\bTODO\b|add .* here|implement (this|later)", src, re.I):
            j.append("Remove placeholders and write the real implementation.")
        if j:
            issues[name] = j

    for name in ("backend/storage.py", "backend/app.py"):
        src = files.get(name, "")
        p = []
        try:
            compile(src, name, "exec")
        except SyntaxError as e:
            p.append("Python syntax error on line %s: %s" % (e.lineno, e.msg))
        if name == "backend/app.py" and "/api/forms/" not in src:
            p.append("Implement POST /api/forms/<form> as the contract says.")
        if p:
            issues[name] = p

    for d in plan.get("datasets", []):
        name = "data/%s.json" % d["name"]
        try:
            items = json.loads(files.get(name, ""))
            if not isinstance(items, list) or not items:
                issues[name] = ["It must be a non-empty JSON array."]
        except ValueError as e:
            issues[name] = ["Invalid JSON: %s" % e]
    return issues


def _premium_fix_messages(name, plan, files, problems):
    ctx = ""
    if name in ("css/main.css",) or name.startswith("js/"):
        ctx = "Page outline:\n" + _html_outline(files.get("index.html", "")) + "\n\n"
    return [
        {"role": "system", "content": PREMIUM_DESIGN + "\n\n" + PREMIUM_CONTRACT},
        {"role": "user", "content": (
            ctx + "Current " + name + ":\n" + files.get(name, "")[:14000] + "\n\n"
            "A review found these problems:\n- " + "\n- ".join(problems) + "\n\n"
            "Fix every one and return the complete corrected " + name + " in a single ```"
            + lang_of(name) + " block and nothing else. Keep everything else as it is.")},
    ]


# ---- scaffolding, from templates ----------------------------------------
def scaffolding(plan: dict) -> dict[str, str]:
    slug = plan["slug"]
    out = {
        "backend/__init__.py": "",
        "backend/requirements.txt": "flask>=3.0\ngunicorn>=22.0\n",
        "Dockerfile": (
            "# Production image: the static site and its API, served by gunicorn.\n"
            "FROM python:3.12-slim\n\n"
            "ENV PYTHONDONTWRITEBYTECODE=1 \\\n    PYTHONUNBUFFERED=1 \\\n    PORT=8000 \\\n    STORAGE_DIR=/storage\n\n"
            "WORKDIR /app\n"
            "COPY backend/requirements.txt backend/requirements.txt\n"
            "RUN pip install --no-cache-dir -r backend/requirements.txt\n\n"
            "COPY . .\n"
            "RUN useradd --create-home --uid 10001 app \\\n    && mkdir -p /storage && chown -R app /app /storage\n"
            "USER app\n\n"
            "EXPOSE 8000\n"
            "HEALTHCHECK --interval=30s --timeout=3s CMD python -c \"import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT','8000'))\" || exit 1\n"
            "CMD [\"sh\", \"-c\", \"gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 backend.app:app\"]\n"),
        "docker-compose.yml": (
            "services:\n  web:\n    build: .\n    ports:\n      - \"${PORT:-8000}:8000\"\n"
            "    env_file: .env\n    environment:\n      STORAGE_DIR: /storage\n"
            "    volumes:\n      - storage:/storage\n    restart: unless-stopped\n\nvolumes:\n  storage:\n"),
        ".env.example": (
            "# Copy to .env and fill in.\n"
            "# Secret used to read form submissions: Authorization: Bearer <ADMIN_TOKEN>\n"
            "ADMIN_TOKEN=change-me-to-a-long-random-string\n"
            "# Port the app listens on\nPORT=8000\n"
            "# Where the SQLite database lives\nSTORAGE_DIR=./storage\n"),
        ".dockerignore": ".git\n.env\nnode_modules\n__pycache__\n*.pyc\nstorage/\nwasm/target/\nwasm/pkg/\n",
        "package.json": json.dumps({
            "name": slug, "private": True, "version": "1.0.0",
            "description": plan.get("tagline") or plan["name"],
            "scripts": {"typecheck": "tsc -p tsconfig.json",
                        "serve": "python -m backend.app",
                        "docker": "docker compose up --build"},
            "devDependencies": {"typescript": "^5.6.0"}}, indent=2) + "\n",
        "tsconfig.json": json.dumps({
            "compilerOptions": {"allowJs": True, "checkJs": True, "noEmit": True,
                                "target": "ES2022", "module": "ES2022",
                                "lib": ["ES2022", "DOM", "DOM.Iterable"], "strict": False,
                                "skipLibCheck": True},
            "include": ["js/**/*.js", "types/**/*.d.ts"]}, indent=2) + "\n",
        "types/globals.d.ts": ("// The one global the site's scripts share. See js/core.js.\n"
                               "export {};\ndeclare global {\n  interface Window {\n    Site: any;\n  }\n}\n"),
    }
    if plan.get("needs_compute"):
        crate = slug.replace("-", "_") + "_core"
        out["wasm/Cargo.toml"] = (
            "[package]\nname = \"%s\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n"
            "[lib]\ncrate-type = [\"cdylib\", \"rlib\"]\n\n"
            "[dependencies]\nwasm-bindgen = \"0.2\"\n\n"
            "[profile.release]\nopt-level = \"s\"\nlto = true\n" % crate)
    return out


def premium_readme(plan: dict, files: dict) -> str:
    tree = "\n".join("    " + n for n in sorted(files) + ["README.md"])
    forms = "\n".join("- `POST /api/forms/%s` - %s" % (f["name"], f.get("purpose") or f["name"])
                      for f in plan.get("forms", []))
    wasm = ""
    if plan.get("needs_compute"):
        wasm = f"""
## The Rust/WASM module

`wasm/` computes: {plan.get('compute')}

`js/app.js` ships a JavaScript version of the same functions, so the site works
without it. To use the compiled module:

```bash
cargo install wasm-pack
cd wasm && wasm-pack build --target web --release
```

then load `wasm/pkg/` from `js/app.js` in place of the fallback.
"""
    return f"""# {plan['name']}

{plan.get('tagline') or ''}

{plan.get('design_direction') or ''}

Built with RandomGenerals (premium build).

## Structure

{tree}

- **index.html, css/, js/, data/** - the site. Browsers run these as written; no build step.
- **css/tokens.css** - the design system (colour, type, spacing, motion); **css/main.css** uses only these tokens.
- **js/** - `core.js` (shared helpers on `window.Site`), `ui.js` (menu, reveal, dialogs, toasts), `forms.js` (form submission), `app.js` (the site's features).
- **data/** - the content the site renders (edit these to change products, menus and so on).
- **backend/** - a Flask service for self-hosting: serves the site, stores form submissions in SQLite.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\\Scripts\\activate
pip install -r backend/requirements.txt
cp .env.example .env                               # then set ADMIN_TOKEN
python -m backend.app                              # http://localhost:8000
```

### With Docker

```bash
cp .env.example .env
docker compose up --build                          # http://localhost:8000
```

### Type checking

The JavaScript is type-checked by TypeScript (`// @ts-check` + JSDoc):

```bash
npm install && npm run typecheck
```

## Forms

{forms}

Read submissions: `curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/api/forms/<form>/submissions`
{wasm}
## Publishing

Published on RandomGenerals, the site is served at `https://{plan['slug']}.randomgenerals.com`,
and form entries appear under **Your sites** in your account - no server to run.
"""


def build_premium(brief: str, photos: list[dict], credit: str, research_notes: str, llm):
    """The premium pipeline. Same events and return shape as build()."""
    research = ""
    if research_notes:
        yield ("step", "research", "start", "Reading the research")
        text, _ = yield from llm("research", "Reading the research",
                                 _research_messages(brief, research_notes), 2500, "medium", False)
        research = (text or "").strip()[:4000]
        yield ("step", "research", "done", "Distilled the research into a design direction")

    yield ("step", "plan", "start", "Architecting the project")
    messages = _premium_plan_messages(brief, photos, research)
    text, _ = yield from llm("plan", "Architecting the project", messages, 4000, "medium", False)
    obj = _json_object(text)
    if obj is None:
        log("premium plan did not parse; first 300 chars: %r" % (text or "")[:300])
        text, _ = yield from llm("plan", "Architecting the project", messages + [
            {"role": "assistant", "content": (text or "")[:3000]},
            {"role": "user", "content": "That was not a valid JSON object. Reply with ONLY the JSON object."},
        ], 4000, "low", False)
        obj = _json_object(text)
    plan = normalise_premium_plan(obj, brief)
    plan["credit"] = credit
    plan["research"] = research[:2000]
    yield ("step", "plan", "done", "Architected %s: %d sections, %d features, %s%s" % (
        plan["name"], len(plan["sections"]), len(plan["features"]),
        ", ".join("%s data" % d["name"] for d in plan["datasets"]) or "no datasets",
        ", plus a Rust/WASM module" if plan["needs_compute"] else ""))

    files: dict[str, str] = {}
    steps = [("data/%s.json" % d["name"], "low", False, 4000) for d in plan["datasets"]]
    steps += [("index.html", "medium", False, 7000), ("css/tokens.css", "low", False, 3000),
              ("css/main.css", "low", False, 7000), ("js/core.js", "low", True, 3500),
              ("js/ui.js", "low", True, 4500), ("js/forms.js", "low", True, 3500),
              ("js/app.js", "medium", False, 7000), ("backend/storage.py", "low", True, 4000),
              ("backend/app.py", "low", True, 5000)]
    if plan["needs_compute"]:
        steps.append(("wasm/src/lib.rs", "medium", False, 5000))
    for name, effort, small, size in steps:
        yield ("step", name, "start", "Writing " + name)
        body = yield from _write(llm, name, "Writing " + name,
                                 _premium_file_messages(name, brief, plan, photos, files),
                                 size, effort, small)
        if name.endswith(".json"):
            try:
                body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
            except ValueError:
                pass                    # left as written; the review says why
        files[name] = body
        yield ("step", name, "done", "Wrote %s (%.1f KB)" % (name, len(body) / 1024))

    fixed = yield from _review_premium(llm, plan, files)
    add_safety(files)
    files.update(scaffolding(plan))
    files["README.md"] = premium_readme(plan, files)
    return {"name": plan["name"], "slug": plan["slug"], "plan": plan,
            "files": dict(sorted(files.items(), key=lambda kv: _premium_order(kv[0]))),
            "fixed": fixed}


def _premium_order(name: str):
    order = ["index.html", "css/", "js/", "data/", "backend/", "wasm/", "types/",
             "Dockerfile", "docker-compose.yml", ".env.example", ".dockerignore",
             "package.json", "tsconfig.json", "README.md"]
    for i, prefix in enumerate(order):
        if name == prefix or name.startswith(prefix):
            sub = PREMIUM_JS.index(name) if name in PREMIUM_JS else 0
            return (i, sub, name)
    return (len(order), 0, name)


def _review_premium(llm, plan, files):
    yield ("step", "review", "start", "Reviewing every file against the others")
    fixed = yield from _fix_all(llm, plan, files, premium=True)
    left = check_premium(files, plan)
    n = sum(len(v) for v in left.values())
    yield ("step", "review", "done", "Review passed" if not n else
           "Reviewed (%d minor issue%s left)" % (n, "" if n == 1 else "s"))
    return fixed

def revise_premium(instruction: str, project: dict, photos: list[dict], llm):
    """A change to a premium project: only the touched files are rewritten."""
    plan = project["plan"]
    files = dict(project["files"])
    writable = [n for n in files if n == "index.html" or n.startswith(("css/", "js/", "data/", "backend/"))
                and n not in ("backend/__init__.py", "backend/requirements.txt")]
    if plan.get("needs_compute"):
        writable.append("wasm/src/lib.rs")
    yield ("step", "plan", "start", "Working out the change")
    text, _ = yield from llm("plan", "Working out the change", [
        {"role": "system", "content": "You maintain a premium website project. Decide which files a requested change touches. Reply with ONE JSON object only."},
        {"role": "user", "content": (
            "Files: " + ", ".join(writable) + "\n\nFeatures: " + json.dumps(plan.get("features"))
            + "\n\nPage outline:\n" + _html_outline(files.get("index.html", ""), 6000)
            + "\n\nRequested change: " + instruction + "\n\n"
            'Reply as {"summary": "one sentence", "files": ["index.html", "css/main.css", ...]}. List only files that must change.')},
    ], 2500, "medium", False)
    decision = _json_object(text) or {}
    touched = [n for n in writable if n in (decision.get("files") or [])] or ["index.html", "css/main.css"]
    yield ("step", "plan", "done", decision.get("summary") or "Changing " + ", ".join(touched))
    for name in sorted(touched, key=_premium_order):
        yield ("step", name, "start", "Updating " + name)
        files[name] = yield from _write(llm, name, "Updating " + name,
                                        _premium_file_messages(name, instruction, plan, photos, files, instruction),
                                        7000, "low", name.startswith(("js/core", "js/forms", "backend/")))
        yield ("step", name, "done", "Updated %s (%.1f KB)" % (name, len(files[name]) / 1024))
    fixed = yield from _review_premium(llm, plan, files)
    add_safety(files)
    files["README.md"] = premium_readme(plan, {k: v for k, v in files.items() if k != "README.md"})
    return {"name": plan["name"], "slug": plan["slug"], "plan": plan,
            "files": dict(sorted(files.items(), key=lambda kv: _premium_order(kv[0]))),
            "fixed": fixed, "touched": touched}
