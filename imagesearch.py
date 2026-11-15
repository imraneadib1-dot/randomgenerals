"""Real photos from the internet, for the pages the model builds.

Asked for a website, a model with no pictures does one of three things:
leaves the page bare, invents image URLs that 404, or points at a
placeholder service. Measured on the live site before this existed:
three generated sites, nine image tags, every one a placeholder.

This finds real, openly licensed photos for a topic and hands back
URLs that load, with the credit each one needs. Three sources, best
first, each optional:

    Pexels          stock photography, the best fit for a business site.
                    Used only when PEXELS_API_KEY is set (free key).
    Openverse       WordPress's index of openly licensed images, filtered
                    to licences that allow commercial use. No key; the
                    anonymous allowance is about 200 searches a day.
    Wikimedia       Wikimedia Commons. No key, no practical limit, and a
                    CDN that serves a resized copy - so it is both the
                    floor and the fallback.

Results are cached per query for a day: the same "coffee shop" costs one
lookup however many people ask for a café site. Nothing here raises; a
source that fails is skipped and the next one tried.
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from urllib.parse import quote, unquote

import requests

# The app identifying itself, as Wikimedia asks every API client to.
# Nothing about the person asking is sent - the query is the topic of
# the site, not anything about them.
UA = {"User-Agent": "RandomGeneralsBot/1.0 (+https://randomgenerals.com)"}
TIMEOUT = 5
WIDTH = 1280                 # big enough for a hero, small enough to load
CACHE_TTL = 24 * 3600
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()

# Not photographs of the thing: maps, logos, plans, scans of documents.
_NOT_A_PHOTO = re.compile(
    r"\b(map|logo|diagram|chart|plan|flag|coat of arms|seal|icon|svg|"
    r"poster|stamp|document|letter|page|scan|graph|sign(?:age)?|screenshot|"
    r"annotated|painting|engraving|illustration|drawing|lithograph|"
    r"portrait of|museum|catalog(?:ue)?)\b",
    re.IGNORECASE)


def _commons_thumb(url: str, width: int = WIDTH) -> str:
    """upload.wikimedia.org/.../a/ab/Name.jpg -> the WIDTH-px thumbnail.
    The originals are often 4000px and several megabytes."""
    m = re.match(r"(https://upload\.wikimedia\.org/wikipedia/commons)/([0-9a-f])/([0-9a-f]{2})/([^/?#]+)$", url)
    if not m:
        return url
    root, a, ab, name = m.groups()
    return f"{root}/thumb/{a}/{ab}/{name}/{width}px-{name}"


def _clean_title(title: str) -> str:
    title = unquote(title or "")
    title = re.sub(r"^File:", "", title)
    title = re.sub(r"\.(jpe?g|png|webp|tiff?)$", "", title, flags=re.I)
    title = re.sub(r"[_]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()[:90]


def _pexels(query: str, n: int) -> list[dict]:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    r = requests.get("https://api.pexels.com/v1/search",
                     params={"query": query, "per_page": n, "orientation": "landscape"},
                     headers={"Authorization": key, **UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        out.append({
            "url": src.get("large2x") or src.get("large") or src.get("original"),
            "width": p.get("width"), "height": p.get("height"),
            "title": (p.get("alt") or query)[:90],
            "credit": "%s on Pexels" % (p.get("photographer") or "Photographer"),
            "license": "Pexels License",
            "page": p.get("url"),
        })
    return out


def _openverse(query: str, n: int) -> list[dict]:
    r = requests.get("https://api.openverse.org/v1/images/", params={
        "q": query, "page_size": min(20, n * 2), "license_type": "commercial",
        "mature": "false", "aspect_ratio": "wide", "size": "large",
    }, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for it in r.json().get("results", []):
        url = it.get("url") or ""
        title = _clean_title(it.get("title") or "")
        if not url.startswith("https://") or _NOT_A_PHOTO.search(title):
            continue
        # Only hosts that are fine being linked to directly. Wikimedia
        # and Flickr are; a museum's image server may not be.
        if "upload.wikimedia.org" in url:
            url = _commons_thumb(url)
        elif not re.search(r"(staticflickr\.com|live\.staticflickr)", url):
            continue
        out.append({
            "url": url, "width": it.get("width"), "height": it.get("height"),
            "title": title or query,
            "credit": it.get("creator") or "Unknown",
            "license": ("CC " + (it.get("license") or "").upper()).replace("CC CC0", "CC0"),
            "page": it.get("foreign_landing_url"),
        })
    return out


def _commons(query: str, n: int) -> list[dict]:
    r = requests.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query + " filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": min(30, n * 3), "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": WIDTH,
    }, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        return []
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 0))
    out = []
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        title = _clean_title(pg.get("title", ""))
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if ii.get("mime") not in ("image/jpeg", "image/webp"):
            continue
        if w < 1000 or h < 600 or w < h or _NOT_A_PHOTO.search(title):
            continue
        meta = ii.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
        out.append({
            "url": ii.get("thumburl") or _commons_thumb(ii.get("url", "")),
            "width": WIDTH, "height": round(WIDTH * h / w) if w else None,
            "title": title,
            "credit": (artist[:60] or "Wikimedia Commons contributor"),
            "license": (meta.get("LicenseShortName") or {}).get("value", "CC"),
            "page": ii.get("descriptionurl"),
        })
    return out


def search(query: str, n: int = 8) -> list[dict]:
    """Up to `n` photos for `query`, best source first. Never raises.

    -> [{url, width, height, title, credit, license, page}, ...]
    """
    query = re.sub(r"\s+", " ", (query or "").strip())[:80]
    if not query:
        return []
    key = "%s|%d" % (query.lower(), n)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return list(hit[1])
    found: list[dict] = []
    seen: set[str] = set()
    for source in (_pexels, _openverse, _commons):
        if len(found) >= n:
            break
        try:
            batch = source(query, n)
        except (requests.exceptions.RequestException, ValueError):
            continue
        for item in batch:
            if item.get("url") and item["url"] not in seen:
                seen.add(item["url"])
                found.append(item)
    found = found[:n]
    with _lock:
        _cache[key] = (time.time(), found)
    return list(found)


# Words that say what is being asked for rather than what it is about.
_STOP = set("""
a an the and or of for to in on at with by from about my our your their me us
i we you it its this that these those is are be please can could would will
make build create design code write generate develop give need want get do
website web site sites page pages landing homepage home webpage html css js
javascript single one simple basic modern beautiful nice cool clean minimal
responsive professional full complete file files template layout called named
small local new little best great business company brand online store shop's
""".split())


def topic_of(message: str) -> str:
    """The subject of a website request, as an image search query.
    "Build a landing page for a yoga studio in Lisbon" -> "yoga studio lisbon".
    Capitalised words after "called"/"named" are a brand name, not a
    subject, and are dropped."""
    text = re.sub(r"\b(?:called|named)\s+(?:[A-Z][\w'&-]*\s*(?:&\s*)?)+", " ", message or "")
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text)
    keep = [w.lower() for w in words if w.lower() not in _STOP and len(w) > 2]
    return " ".join(keep[:4])


_GENERIC = set("""
shop shops store stores studio studios business businesses company service
services center centre house place agency firm group club co ltd inc
""".split())

# A pre-1900 date in a title is an artwork or an archive scan, not a photo.
_OLD_ART = re.compile(r"\((1[0-8]\d\d)\)|\b1[0-8]\d\d\b.*\bby\b")

# Someone else's shop. A street address or a shopfront survey in the
# title means the picture shows another business's name and phone
# number across its front - the hero of the first yoga site generated
# with photos was "Myogi Yoga Studio, 63 Newport Road", sign and all.
_STOREFRONT = re.compile(
    r"geograph|shop ?front|store ?front|facade|façade|exterior|"
    r"\b\d+[a-z]?,?\s+[a-z][\w'.-]*(?:\s+[a-z][\w'.-]*)?\s+"
    r"(?:road|rd|street|st|avenue|ave|lane|ln|place|pl|boulevard|blvd|"
    r"way|square|sq|drive|dr|parade|terrace|row)\b|"
    # Streets elsewhere, named before the number or without one: the live
    # Lyon bakery build used "Boulangerie Chez Jules, rue Victor Hugo".
    r"\b(?:rue|quai|chemin|cours|calle|carrer|avenida|rua|via|viale|corso|"
    r"piazza|plaza|platz|stra(?:ss|ß)e|gasse|laan|straat)\s+\w|"
    r"\bchez\s+\w",
    re.IGNORECASE)


def _queries(topic: str) -> list[str]:
    """The full topic, then each adjacent pair, then each word: the
    narrow search first, widening only while it comes back thin.
    "yoga studio lisbon" -> yoga studio lisbon, yoga studio,
    studio lisbon, yoga, studio, lisbon."""
    words = topic.split()
    out = [topic]
    out += [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    out += words
    seen, ordered = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            ordered.append(q)
    return ordered


def photos_for(message: str, n: int = 8, deadline: float = 4.0) -> list[dict]:
    """Photos for what a website request is about. Never raises.

    Relevance is checked, not assumed: a photo is kept only when its
    title names something in the topic. A loose search for "specialty
    coffee shop" otherwise returns muffins and office towers. Stops at
    `deadline` seconds with whatever it has, because these are fetched
    before the reply starts and the person is waiting.
    """
    topic = topic_of(message)
    if not topic:
        return []
    words = topic.split()
    # "shop" matches every shopfront in the world; the word that says
    # WHAT is sold is the one a photo has to be about.
    specific = [w for w in words if w not in _GENERIC and len(w) > 2] or words
    stems = [w[:-1] if w.endswith("s") and len(w) > 4 else w for w in specific]

    # Every query at once: each is a network round trip, and the reply
    # is waiting on all of them. Merged afterwards in priority order, so
    # the narrowest search still wins.
    queries = _queries(topic)[:5]
    results: dict[str, list[dict]] = {}
    pool = ThreadPoolExecutor(max_workers=len(queries))
    futures = {pool.submit(search, q, n): q for q in queries}
    try:
        for fut in as_completed(futures, timeout=deadline):
            try:
                results[futures[fut]] = fut.result()
            except Exception:  # noqa: BLE001 - a source failing is not an error here
                pass
    except FuturesTimeout:
        pass
    pool.shutdown(wait=False, cancel_futures=True)

    scored: list[tuple[int, int, dict]] = []
    seen: set[str] = set()
    order = 0
    for q in queries:
        for p in results.get(q, []):
            title = (p.get("title") or "").lower()
            score = sum(1 for s in stems if s in title)
            # A numbered series ("... shop 01", "... shop 02") is one
            # picture three times over; a page wants different ones.
            series = re.sub(r"[\d\W_]+", " ", title).strip()[:40]
            if (p["url"] in seen or series in seen or score == 0
                    or _OLD_ART.search(title) or _STOREFRONT.search(title)
                    or _STOREFRONT.search(p.get("page") or "")):
                continue
            seen.add(p["url"])
            seen.add(series)
            scored.append((-score, order, p))
            order += 1
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in scored[:n]]


def credit_line(photos: list[dict]) -> str:
    """One line crediting every photo, for a page footer."""
    names = []
    for p in photos:
        bit = "%s (%s)" % (p.get("credit") or "Unknown", p.get("license") or "CC")
        if bit not in names:
            names.append(bit)
    return "Photos: " + "; ".join(names[:8])
