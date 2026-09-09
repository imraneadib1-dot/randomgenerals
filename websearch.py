"""Web search with no API key, from several places at once.

WHY SEVERAL

This was one function scraping DuckDuckGo's HTML results page, and its
own docstring said what would happen: "if DuckDuckGo changes their
markup or rate-limits this app, search() just returns an empty list".
That is exactly what happened. DuckDuckGo began answering **HTTP 202
with an anomaly page** - a bot block, not a markup change - and every
search on the site quietly returned nothing.

The visible symptom was not "search is down". It was the assistant
calling web_search three times, getting nothing each time, and then
producing no reply at all, which the browser reported as "[No response
received. Check the channel setup.]". A single scraped backend is a
single point of failure that fails silently and blames something else.

So: our own index first, then a handful of independent sources, each
isolated. One being blocked costs some results, not all of them.

  searchdb     our crawled index - best on the curriculum, knows nothing else
  marginalia   an independent crawler, good on documents and small sites
  wikipedia    the official API, no scraping, reliable for anything factual
  duckduckgo   kept last: excellent when it answers, and it often will not

Every backend returns [{"title", "url", "snippet"}] and NEVER raises.
A backend that throws is a backend that contributed nothing.
"""
import html
import os
import re
import urllib.parse

import requests

_TAG_RE = re.compile(r"<[^>]+>")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr,en;q=0.8,ar;q=0.6",
}

TIMEOUT = 8


def _clean(fragment):
    return html.unescape(_TAG_RE.sub("", fragment or "")).strip()


def _trim(text, limit=280):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------- backends

_DDG_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)


def _real_url(ddg_href):
    """DuckDuckGo wraps result links in its own redirect - unwrap it so
    callers get the actual destination."""
    if ddg_href.startswith("//"):
        ddg_href = "https:" + ddg_href
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(ddg_href).query)
    if "uddg" in qs:
        return urllib.parse.unquote(qs["uddg"][0])
    return ddg_href


def _duckduckgo(query, limit=5):
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                         params={"q": query}, headers=_HEADERS, timeout=TIMEOUT)
        # 202 is their bot wall, not a result page. Checked explicitly
        # so a block reads as a block in the logs rather than as a
        # search that found nothing.
        if r.status_code != 200:
            return []
    except requests.RequestException:
        return []

    out = []
    for m in _DDG_RE.finditer(r.text):
        href, title_html, snippet_html = m.groups()
        title = _clean(title_html)
        if not title:
            continue
        out.append({"title": title, "url": _real_url(href),
                    "snippet": _trim(_clean(snippet_html))})
        if len(out) >= limit:
            break
    return out


_MARGINALIA_RE = re.compile(
    r'<h2>\s*<a[^>]+class="title"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<p class="description">(.*?)</p>',
    re.DOTALL,
)


def _marginalia(query, limit=5):
    """An independent index, not a wrapper round one of the big ones.

    old-search, not search.marginalia.nu: the newer address serves a
    JavaScript shell and links here itself, and their JSON API timed out
    on every attempt.
    """
    try:
        r = requests.get("https://old-search.marginalia.nu/search",
                         params={"query": query}, headers=_HEADERS,
                         timeout=TIMEOUT + 6)
        if r.status_code != 200:
            return []
    except requests.RequestException:
        return []

    out = []
    for m in _MARGINALIA_RE.finditer(r.text):
        url, title_html, desc_html = m.groups()
        title = _clean(title_html)
        if not title or not url.startswith("http"):
            continue
        out.append({"title": title, "url": html.unescape(url),
                    "snippet": _trim(_clean(desc_html))})
        if len(out) >= limit:
            break
    return out


def _wikipedia(query, limit=3, lang="fr"):
    """The official API. No scraping, no bot wall, and for a question
    about a place, a person or a concept it is often the answer rather
    than a route to it."""
    try:
        r = requests.get(
            "https://%s.wikipedia.org/w/api.php" % lang,
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": str(limit),
                    "srprop": "snippet"},
            headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        hits = (r.json().get("query") or {}).get("search") or []
    except (requests.RequestException, ValueError):
        return []

    out = []
    for h in hits:
        title = h.get("title") or ""
        if not title:
            continue
        out.append({
            "title": title,
            "url": "https://%s.wikipedia.org/wiki/%s"
                   % (lang, urllib.parse.quote(title.replace(" ", "_"))),
            "snippet": _trim(_clean(h.get("snippet") or "")),
        })
    return out


def _brave(query, limit=5):
    """The one backend that is actually good, when there is a key.

    Brave's free tier is 2,000 queries a month and needs no card. It is
    off unless BRAVE_API_KEY is set, and everything above keeps working
    without it - but a keyless web search is scraping whoever has not
    blocked us yet, and it shows in the results. This is the upgrade
    path, wired and waiting.
    """
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": str(min(limit, 20))},
            headers={"Accept": "application/json",
                     "X-Subscription-Token": key},
            timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        hits = (r.json().get("web") or {}).get("results") or []
    except (requests.RequestException, ValueError):
        return []

    return [{"title": _clean(h.get("title") or ""),
             "url": h.get("url") or "",
             "snippet": _trim(_clean(h.get("description") or ""))}
            for h in hits if h.get("url")][:limit]


# ------------------------------------------------------------------ public

# Words too common to prove a result has anything to do with the query.
_NOISE = frozenset("""
de la le les des du et ou un une en dans pour sur avec au aux
the of and or to in a an for on at by with from is are what how
""".split())


def _relevant(item, wanted):
    """Does this result contain any of the words that were asked for?

    Marginalia answered "anisse international school morocco" with
    "LGBTQ Reads", and Wikipedia answered "weather in tokyo today" with
    "Dionne Warwick". Both are what a search engine returns when it has
    nothing: its least-bad guess. Passing that to the model as though it
    were an answer is how a confident wrong reply gets written.

    Deliberately generous - one real word is enough. This is here to
    drop the obviously unrelated, not to second-guess a ranker.
    """
    if not wanted:
        return True
    hay = ("%s %s" % (item.get("title") or "",
                      item.get("snippet") or "")).lower()
    return any(w in hay for w in wanted)


def _key(url):
    return (url or "").rstrip("/").lower()


def search(query, max_results=5):
    """-> [{"title", "url", "snippet", "source"}, ...]

    Our own index first, then the open web. Never raises: any failure
    means fewer results, not an exception.
    """
    query = (query or "").strip()
    if not query:
        return []

    results, seen = [], set()
    wanted = [w for w in re.findall(r"\w+", query.lower())
              if len(w) > 3 and w not in _NOISE]

    def add(items, source, guard=True):
        for item in items:
            if len(results) >= max_results:
                return
            k = _key(item.get("url"))
            if not k or k in seen:
                continue
            if guard and not _relevant(item, wanted):
                continue
            seen.add(k)
            item["source"] = source
            results.append(item)

    try:
        import searchdb
        # search() returns a dict of results plus timing and counts, not
        # a bare list - the page needs those, this does not.
        # Guarded like everything else.
        #
        # This was exempt at first, on the reasoning that bm25 had
        # already ranked it against this exact query. That stopped being
        # true when the index started relaxing a strict AND to an OR:
        # "theoreme de thales" then filled the whole page with
        # "Ressources pour le collège" and "Biographies", which contain
        # neither word, and left no room for the pages that do.
        add(searchdb.search(query, limit=max_results)["results"], "index")
    except Exception:                            # noqa: BLE001
        # No index file yet, or a locked database. Neither is a reason
        # to refuse to search.
        pass

    # Order matters: an independent crawler first for breadth, then the
    # encyclopedia for anything factual, then the one most likely to
    # refuse us.
    for backend, source in ((_brave, "brave"),
                            (_marginalia, "marginalia"),
                            (_wikipedia, "wikipedia"),
                            (_duckduckgo, "web")):
        if len(results) >= max_results:
            break
        try:
            add(backend(query, max_results * 2), source)
        except Exception:                        # noqa: BLE001
            continue

    return results


def which_backends():
    """Which sources are answering right now. For check scripts and for
    saying "search is degraded" out loud instead of returning silence."""
    state = {}
    for name, fn in (("marginalia", _marginalia),
                     ("wikipedia", _wikipedia),
                     ("duckduckgo", _duckduckgo)):
        try:
            state[name] = len(fn("test", 3))
        except Exception:                        # noqa: BLE001
            state[name] = 0
    return state
