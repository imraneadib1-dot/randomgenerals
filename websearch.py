"""Lightweight web search - no API key required.

Scrapes DuckDuckGo's HTML results page. This is not an official API (DDG
doesn't publish a free one), so it's inherently more fragile than a paid
search API - if DuckDuckGo changes their markup or rate-limits this app,
search() just returns an empty list instead of raising. Callers should
treat an empty list as "no results right now", not "search is broken".

For anything beyond casual, low-volume use, swap this out for a real
search API (Tavily, Serper, Bing) - keep the same
search(query) -> [{"title", "url", "snippet"}, ...] shape and only this
file needs to change.
"""
import html
import re
import urllib.parse

import requests

_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _clean(fragment):
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _real_url(ddg_href):
    """DuckDuckGo wraps result links in its own redirect - unwrap it so
    callers get the actual destination, not a duckduckgo.com URL."""
    if ddg_href.startswith("//"):
        ddg_href = "https:" + ddg_href
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(ddg_href).query)
    if "uddg" in qs:
        return urllib.parse.unquote(qs["uddg"][0])
    return ddg_href


def search(query, max_results=5):
    """-> [{"title", "url", "snippet", "source"}, ...]

    Our own index first, DuckDuckGo for whatever it cannot answer.

    The index is small and narrow - a few sites of Moroccan and French
    curriculum material - but on those subjects it is better than a
    scrape of somebody else's results page: the crawler read the whole
    document, so the snippet comes from the text rather than from a
    search engine's summary of it. Off that subject it knows nothing,
    which is what the fallback is for.

    Never raises. Any failure means fewer results, not an exception.
    """
    query = (query or "").strip()
    if not query:
        return []

    results = []
    seen = set()
    try:
        import searchdb
        for r in searchdb.search(query, limit=max_results):
            results.append({"title": r["title"], "url": r["url"],
                            "snippet": r["snippet"], "source": "index"})
            seen.add(r["url"].rstrip("/"))
    except Exception:                            # noqa: BLE001
        # No index file yet, or a locked database. Neither is a reason
        # to refuse to search.
        pass

    if len(results) >= max_results:
        return results

    for r in _duckduckgo(query, max_results * 2):
        if r["url"].rstrip("/") in seen:
            continue
        r["source"] = "web"
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def _duckduckgo(query, max_results=5):
    """The keyless fallback. Same contract: never raises, empty on any
    failure."""
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException:
        return []

    results = []
    for m in _RESULT_RE.finditer(r.text):
        href, title_html, snippet_html = m.groups()
        title = _clean(title_html)
        if not title:
            continue
        results.append({
            "title": title,
            "url": _real_url(href),
            "snippet": _clean(snippet_html),
        })
        if len(results) >= max_results:
            break
    return results
