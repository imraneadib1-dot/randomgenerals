"""The workspace browser - web pages, read on the server, shown in the pane.

What the browser in the workspace pane (static/js/browser.js) displays,
and what the AI reads when it opens a page (the open_page tool) or is
asked about the page someone has open.

WHY A READER AND NOT AN IFRAME
Most sites refuse to be framed (X-Frame-Options / frame-ancestors), so an
iframe of the real page is a blank box for half the web. The page is
fetched here instead, and turned into a clean reading view: headings,
paragraphs, lists, links, images, tables, code - no scripts, no styles,
no forms, no trackers' code. It always renders, it is safe to show, and
it is the same text the model reads, so "what does this page say" is
answered about exactly what is on screen. The original is one click
away ("Open original").

SAFETY
Every hop - the URL and each redirect - goes through connectors'
_resolve_safe(), which refuses private, loopback, link-local and other
internal addresses: this server fetches on someone's behalf, so it must
never be a way into its own network. Bodies are capped, and the output
is built from an allow-list of tags and attributes; nothing from the
page is passed through as markup.
"""
import html
import re
import threading
import time
import urllib.parse
from html.parser import HTMLParser

import requests

import connectors
import crawler

TIMEOUT = 15
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_HTML_CHARS = 400_000
MAX_TEXT_CHARS = 12_000
CACHE_SECONDS = 600
CACHE_SIZE = 64
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 RandomGeneralsBrowser/1.0")


class BrowseError(Exception):
    """A sentence for a person: why the page could not be opened."""


# ------------------------------------------------------------ addresses
def normalise(raw):
    """What was typed -> ("url", url) or ("search", query).

    "example.com" is a site, "how do magnets work" is a search - the
    address bar is both, as in any browser."""
    text = (raw or "").strip()
    if not text:
        raise BrowseError("Type an address or something to search for.")
    if re.match(r"^https?://", text, re.I):
        return "url", text
    if " " not in text and re.match(r"^[\w.-]+\.[a-z]{2,}(?::\d+)?(/.*)?$", text, re.I):
        return "url", "https://" + text
    return "search", text


# ------------------------------------------------------------ fetching
def _charset(ctype, body):
    """The page's encoding: the header's, else its own <meta>, else UTF-8.
    requests would guess ISO-8859-1 for any text/* without a charset,
    which turns every accented letter of a French page into mojibake."""
    m = re.search(r"charset=([\w-]+)", ctype or "")
    if m:
        return m.group(1)
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    return m.group(1).decode("ascii", "ignore") if m else "utf-8"


def _fetch(url):
    """-> (final_url, content_type, bytes, encoding). Guarded at every hop."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            connectors._resolve_safe(current)
        except connectors.Unsafe as e:
            raise BrowseError(str(e)) from e
        try:
            r = requests.get(current, timeout=TIMEOUT, allow_redirects=False, stream=True,
                             headers={"User-Agent": USER_AGENT,
                                      "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
                                      "Accept-Language": "en,fr;q=0.8,ar;q=0.6"})
        except requests.RequestException as e:
            raise BrowseError("That site could not be reached (%s)." % e.__class__.__name__) from e
        with r:
            if r.status_code in (301, 302, 303, 307, 308):
                target = r.headers.get("Location") or ""
                if not target:
                    raise BrowseError("That page redirects nowhere.")
                current = urllib.parse.urljoin(current, target)
                continue
            if r.status_code >= 400:
                raise BrowseError("That site answered with an error (%d)." % r.status_code)
            ctype = (r.headers.get("content-type") or "").lower()
            buf, size = [], 0
            for chunk in r.iter_content(65536):
                buf.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    break
            body = b"".join(buf)[:MAX_BYTES]
            return current, ctype, body, _charset(ctype, body)
    raise BrowseError("That page redirects too many times.")


# ------------------------------------------------------------ the reader
_BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote",
          "pre", "table", "thead", "tbody", "tr", "th", "td", "figure",
          "figcaption", "hr", "dl", "dt", "dd"}
_INLINE = {"a", "strong", "b", "em", "i", "code", "br", "img", "sup", "sub", "mark", "small"}
_DROP = {"script", "style", "noscript", "svg", "canvas", "template", "iframe",
         "object", "embed", "form", "button", "select", "textarea", "input",
         "nav", "footer", "aside", "dialog", "video", "audio"}
_VOID = {"br", "img", "hr"}
# Tags HTML never closes. A dropped one (an <input>) must not open a
# "skipping" span, or everything after the first search box vanished.
_HTML_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "param", "source", "track", "wbr"}


# Page furniture marked by its class or id: menus, language lists,
# share bars, cookie banners, navboxes. Skipped with everything inside.
# Only on containers that are always closed, so a skip always ends.
_SKIPPABLE = {"div", "section", "ul", "ol", "table", "header", "span", "details"}
_NOISE = re.compile(
    r"^(?:[a-z0-9]+[-_])*(?:nav|navbar|navbox|navigation|menu|menubar|sidebar|toc|"
    r"breadcrumbs?|cookies?|consent|banner|share|sharing|social|related|advert|ads|"
    r"promo|newsletter|subscribe|popup|modal|skip|dropdown|portlet|interlanguage|"
    r"langlinks|editsection|catlinks|masthead|comments?|hatnote|noprint|printfooter|"
    r"jump-link|sitesub)(?:[-_][a-z0-9]+)*$", re.I)


def _is_noise(attrs):
    names = ((attrs.get("class") or "") + " " + (attrs.get("id") or "")).split()
    return any(_NOISE.match(n) for n in names)


def _safe_link(base, href):
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("javascript:", "data:", "vbscript:", "mailto:", "tel:")):
        return ""
    absolute = urllib.parse.urljoin(base, href)
    return absolute if absolute.startswith(("http://", "https://")) else ""


class Reader(HTMLParser):
    """The page, rebuilt from an allow-list. Nothing of the original
    markup survives except these tags and these attributes."""

    def __init__(self, base, tidy=True):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.tidy = tidy
        self.skip_tag = None
        self.skip_depth = 0
        self.out = []
        self.drop = 0
        self.open = []
        self.title = ""
        self._in_title = False
        self.chars = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in _DROP:
            if tag not in _HTML_VOID:
                self.drop += 1
            return
        if self.drop or self.chars > MAX_HTML_CHARS:
            return
        a = dict(attrs)
        if self.tidy and tag in _SKIPPABLE and _is_noise(a):
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag == "img":
            src = _safe_link(self.base, a.get("src") or a.get("data-src") or "")
            if src:
                alt = html.escape(a.get("alt") or "", quote=True)
                self._emit('<img src="%s" alt="%s" loading="lazy" referrerpolicy="no-referrer">'
                           % (html.escape(src, quote=True), alt))
            return
        if tag not in _BLOCK and tag not in _INLINE:
            return
        if tag == "a":
            href = _safe_link(self.base, a.get("href"))
            if not href:
                self.open.append(None)
                return
            self._emit('<a href="%s">' % html.escape(href, quote=True))
            self.open.append("a")
            return
        if tag in _VOID:
            self._emit("<%s>" % tag)
            return
        extra = ""
        if tag in ("td", "th") and (a.get("colspan") or "").isdigit():
            extra = ' colspan="%s"' % a["colspan"][:2]
        self._emit("<%s%s>" % (tag, extra))
        self.open.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip_tag = None
            return
        if tag in _DROP:
            if tag not in _HTML_VOID:
                self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag in _VOID:
            return
        if tag not in _BLOCK and tag not in _INLINE:
            return
        # Close up to the matching open tag, so broken markup on the page
        # cannot leave this output unbalanced.
        if tag in self.open or (tag == "a" and None in self.open):
            while self.open:
                t = self.open.pop()
                if t is not None:
                    self._emit("</%s>" % t)
                if t == tag or (tag == "a" and t in (None, "a")):
                    break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.drop or self.skip_tag or self.chars > MAX_HTML_CHARS:
            return
        if data.strip():
            self._emit(html.escape(data))
        elif data and self.out and not self.out[-1].endswith(" "):
            self._emit(" ")

    def _emit(self, s):
        self.out.append(s)
        self.chars += len(s)

    def result(self):
        while self.open:
            t = self.open.pop()
            if t is not None:
                self.out.append("</%s>" % t)
        body = "".join(self.out)
        # Empty blocks left behind by stripped widgets.
        body = re.sub(r"<(p|li|td|th|figure|blockquote)>\s*</\1>", "", body)
        return body


def _reader_html(raw_html, base):
    # Prefer the page's own main content when it marks it.
    m = re.search(r"<(main|article)\b[^>]*>(.*?)</\1>", raw_html, re.I | re.S)
    source = m.group(2) if m and len(m.group(2)) > 2000 else raw_html
    title = ""
    t = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
    if t:
        title = html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()

    def run(tidy):
        reader = Reader(base, tidy=tidy)
        try:
            reader.feed(source)
            reader.close()
        except Exception:                      # noqa: BLE001 - a broken page is still a page
            pass
        return reader

    def visible(markup):
        return len(re.sub(r"<[^>]+>", "", markup).strip())

    reader = run(True)
    body = reader.result()
    # The furniture filter guesses from class names; when it leaves
    # little, it may have caught the article's own wrapper (a
    # "layout-with-sidebar" div). Keep whichever reading has more.
    if visible(body) < 1500:
        plain = run(False).result()
        if visible(plain) > 3 * max(visible(body), 1):
            body = plain
    title = (title or html.unescape(reader.title).strip())[:300]
    if title and "<h1" not in body:
        body = "<h1>%s</h1>" % html.escape(title) + body
    return title, body


# ------------------------------------------------------------ cache
_cache = {}
_cache_lock = threading.Lock()


def _cached(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    return None


def _remember(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)
        if len(_cache) > CACHE_SIZE:
            for k, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:len(_cache) - CACHE_SIZE]:
                _cache.pop(k, None)


# ------------------------------------------------------------ public
def open_page(raw):
    """What the address bar, the AI's tool and the chat context all use.

    -> {"kind": "page"|"search", "url", "title", "site", "html", "text"}
    Raises BrowseError with a sentence when the page cannot be opened."""
    kind, value = normalise(raw)
    if kind == "search":
        return search_page(value)
    hit = _cached(value)
    if hit:
        return hit
    final, ctype, body, encoding = _fetch(value)
    site = urllib.parse.urlsplit(final).hostname or ""
    if "pdf" in ctype or final.lower().endswith(".pdf"):
        title, text = crawler.pdf_text(body, final)
        if not text:
            raise BrowseError("That PDF has no text to read (it is probably a scan).")
        page = {"kind": "page", "url": final, "site": site,
                "title": title or final.rsplit("/", 1)[-1],
                "html": "".join("<p>%s</p>" % html.escape(p) for p in text.split("\n\n") if p.strip()),
                "text": text[:MAX_TEXT_CHARS]}
    elif "html" in ctype or "xml" in ctype or not ctype:
        markup = body.decode(encoding or "utf-8", errors="replace")
        title, reader_html = _reader_html(markup, final)
        extract = crawler.Extract(final)
        try:
            extract.feed(markup)
        except Exception:                      # noqa: BLE001
            pass
        text = extract.text
        if not reader_html.strip() and text.strip():
            # The reader found nothing it trusts, the text extractor did:
            # show that rather than a blank pane.
            reader_html = "".join("<p>%s</p>" % html.escape(p)
                                  for p in text.split("\n") if p.strip())
        if not reader_html.strip() and not text.strip():
            raise BrowseError("That page has no readable text - it may need JavaScript. "
                              "Use Open original to see it.")
        page = {"kind": "page", "url": final, "site": site, "title": title or site,
                "html": reader_html, "text": text[:MAX_TEXT_CHARS]}
    elif ctype.startswith("text/"):
        text = body.decode(encoding or "utf-8", errors="replace")
        page = {"kind": "page", "url": final, "site": site, "title": final.rsplit("/", 1)[-1] or site,
                "html": "<pre>%s</pre>" % html.escape(text[:MAX_HTML_CHARS]),
                "text": text[:MAX_TEXT_CHARS]}
    else:
        raise BrowseError("That link is a file (%s), not a page - use Open original."
                          % (ctype.split(";")[0] or "unknown type"))
    _remember(value, page)
    if final != value:
        _remember(final, page)
    return page


def search_page(query):
    """A search typed in the address bar: the results as a page."""
    import websearch
    key = "search:" + query.lower()
    hit = _cached(key)
    if hit:
        return hit
    try:
        results = websearch.search(query, max_results=8) or []
    except Exception as e:                     # noqa: BLE001
        raise BrowseError("Search is not reachable right now.") from e
    items, lines = [], []
    for r in results:
        url = _safe_link("https://example.invalid/", r.get("url") or "")
        if not url:
            continue
        title = html.escape(r.get("title") or url)
        snippet = html.escape(r.get("snippet") or "")
        host = html.escape(urllib.parse.urlsplit(url).hostname or "")
        items.append('<li><a href="%s">%s</a><br><small>%s</small><p>%s</p></li>'
                     % (html.escape(url, quote=True), title, host, snippet))
        lines.append("%s - %s\n%s" % (r.get("title") or "", url, r.get("snippet") or ""))
    body = ("<h1>%s</h1><ul class=\"rg-results\">%s</ul>" % (html.escape(query), "".join(items))
            if items else "<h1>%s</h1><p>No results.</p>" % html.escape(query))
    page = {"kind": "search", "url": "", "site": "Search", "title": "Search: " + query,
            "html": body, "text": "\n\n".join(lines)}
    _remember(key, page)
    return page
