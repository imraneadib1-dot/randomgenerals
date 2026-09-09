"""A polite, single-threaded crawler for one subject at a time.

Run it as its own process, never inside gunicorn:

    python crawler.py --seeds seeds.txt --max-pages 2000
    nice -n 19 python crawler.py --max-pages 20000      # on the VM

WHY SINGLE-THREADED

The VM has two cores and they are already serving the site. A crawler
that saturates them makes every page slow for real visitors, and the
symptom - "the site feels slow sometimes" - is nearly impossible to
trace back to its cause. One thread, one request at a time, a delay
between them. Politeness here is not only about other people's servers.

WHAT IT WILL NOT DO

  - fetch anything robots.txt disallows
  - hit the same host more often than once per DELAY seconds
  - follow a link off the allowed hosts
  - take more than HOST_CAP pages from any single host
  - download anything that is not HTML, or larger than MAX_BYTES

The scope list is the whole design. A crawler without one is how you end
up with 40 GB of forum pagination and no course notes.
"""
import argparse
import html
import io
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from html.parser import HTMLParser

import requests

import searchdb

USER_AGENT = os.environ.get(
    "CRAWLER_UA",
    "RandomGeneralsBot/0.1 (+https://randomgenerals.com/about; "
    "contact via the site)")

DELAY = float(os.environ.get("CRAWL_DELAY", "1.5"))     # seconds per host
MAX_BYTES = 2_000_000
HOST_CAP = int(os.environ.get("CRAWL_HOST_CAP", "4000"))
TIMEOUT = (8, 20)

# Query parameters that change nothing about the page. Dropping them
# stops the same document being indexed a dozen times.
JUNK_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
               "utm_content", "fbclid", "gclid", "ref", "s", "share",
               "sessionid", "phpsessid"}

SKIP_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|css|js|mp[34]|avi|mov|wmv|zip|rar|7z|"
    r"gz|tar|exe|dmg|apk|woff2?|ttf|eot)(\?|$)", re.I)

# Past papers live in PDFs. Skipping them meant the index had the page
# that lists "Examen National 2023" and not the exam.
PDF_EXT = re.compile(r"\.pdf(\?|$)", re.I)

# A scanned 200-page textbook is not worth the minute of CPU it takes to
# get nothing out of - scans have no text layer at all. Read the first
# pages, and if there is no prose by then, give up.
PDF_MAX_PAGES = 40
PDF_MAX_BYTES = 12_000_000

# Pages that exist on every site and teach nothing. The first crawl
# spent a third of its budget on AlloSchool's login, register, cgu, cgv,
# contact form and language switchers - all indexed, all useless to
# somebody looking for a maths lesson.
JUNK_PATH = re.compile(
    r"/(login|log-in|signin|sign-in|logout|register|signup|sign-up|"
    r"password|account|profil|profile|panier|cart|checkout|abonnement|"
    r"contact|contact-us|cgu|cgv|terms|conditions|privacy|confidentialite|"
    r"mentions-legales|legal|cookies|language|lang|sitemap|rss|feed|"
    r"print|share|report|signaler)(/|$|\?)", re.I)


class Extract(HTMLParser):
    """Title, visible text, language and links, using only the stdlib.

    No BeautifulSoup on purpose: this project has kept its dependency
    list short, and everything needed here is one parser and a set of
    tags to ignore.
    """

    IGNORE = {"script", "style", "noscript", "svg", "canvas", "template",
              "nav", "footer", "header", "form", "aside"}

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.title = ""
        self.lang = ""
        self.links = []
        self._chunks = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"][:8]
        if tag in self.IGNORE:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if href and not href.startswith(("#", "javascript:", "mailto:",
                                             "tel:", "data:")):
                self.links.append(urllib.parse.urljoin(self.base, href))
        elif tag == "meta":
            # A description is often the only decent summary a page has.
            if a.get("name", "").lower() in ("description", "og:description"):
                if a.get("content"):
                    self._chunks.append(a["content"])
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORE:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    @property
    def text(self):
        joined = " ".join(self._chunks)
        return re.sub(r"[ \t ]{2,}", " ",
                      re.sub(r"\n{2,}", "\n", joined)).strip()


def normalise(url):
    """One canonical spelling per document."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""

    keep = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)
            if k.lower() not in JUNK_PARAMS]
    query = urllib.parse.urlencode(keep)
    path = re.sub(r"/{2,}", "/", u.path) or "/"
    # index.html and / are the same page.
    path = re.sub(r"/index\.(html?|php)$", "/", path, flags=re.I)
    host = u.netloc.lower()
    if host.endswith(":80"):
        host = host[:-3]
    if host.endswith(":443"):
        host = host[:-4]
    return urllib.parse.urlunsplit((u.scheme, host, path, query, ""))


def in_scope(url, allow):
    """allow is a list of host suffixes. Empty means nothing is allowed -
    a crawler with no scope is not a default worth having."""
    host = searchdb.host_of(url)
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in allow)


def robots_for(host, session):
    """robots.txt, cached in the hosts table for a day."""
    st = searchdb.host_state(host)
    body = ""
    if st and st["robots_fetched"] and time.time() - st["robots_fetched"] < 86400:
        body = st["robots"]
    else:
        try:
            r = session.get("https://%s/robots.txt" % host, timeout=TIMEOUT)
            body = r.text[:200_000] if r.status_code == 200 else ""
        except requests.RequestException:
            body = ""
        searchdb.save_robots(host, body)

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.splitlines())
    return rp


def wait_for_host(host):
    st = searchdb.host_state(host)
    if not st:
        return
    gap = time.time() - (st["last_fetch"] or 0)
    if gap < DELAY:
        time.sleep(DELAY - gap)


def _first_heading(text):
    """The first line that reads like a title rather than a paragraph or
    a page number."""
    for line in text.split("\n")[:12]:
        line = line.strip()
        if 8 <= len(line) <= 120 and not line.isdigit():
            # A line ending in a full stop is prose, not a heading -
            # except numbered headings like "Chapitre 1." which end in
            # one legitimately.
            if line.endswith(".") and not re.search(r"\d\.$", line):
                continue
            return line
    return ""


def pdf_text(raw, url):
    """-> (title, text). Empty text means there was nothing to index."""
    try:
        import pypdf
    except ImportError:
        return "", ""

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:                    # noqa: BLE001
                return "", ""

        chunks = []
        for page in reader.pages[:PDF_MAX_PAGES]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:                    # noqa: BLE001
                continue                         # one bad page, not a bad file

        text = re.sub(r"[ \t]{2,}", " ",
                      re.sub(r"\n{3,}", "\n\n", "\n".join(chunks))).strip()

        title = ""
        try:
            title = (reader.metadata or {}).get("/Title") or ""
        except Exception:                        # noqa: BLE001
            title = ""
        title = str(title).strip()

        # A LaTeX-produced PDF usually carries its .dvi or .tex filename
        # as the metadata title - "01_complements_algebre_chapitre.dvi"
        # is worse than useless as a search result heading, when the
        # document's own first line reads "Chapitre 1. Compléments
        # d'algèbre".
        looks_like_filename = bool(re.search(
            r"\.(dvi|tex|pdf|docx?|odt|ps)$", title, re.I)) or (
            " " not in title and "_" in title)
        if not title or looks_like_filename:
            heading = _first_heading(text)
            if heading:
                title = heading
            elif not title:
                name = urllib.parse.unquote(
                    urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
                title = re.sub(r"[_-]+", " ",
                               name[:-4] if name.lower().endswith(".pdf")
                               else name).strip()
        return title[:300], text
    except Exception:                            # noqa: BLE001
        # A truncated or malformed PDF is a page we skip, not a crawl
        # that stops.
        return "", ""


def fetch(session, url):
    """-> (ok, title, text, lang, links, note)"""
    try:
        with session.get(url, timeout=TIMEOUT, stream=True,
                         allow_redirects=True) as r:
            if r.status_code != 200:
                return False, "", "", "", [], "http %d" % r.status_code

            ctype = (r.headers.get("content-type") or "").lower()
            is_pdf = "pdf" in ctype or PDF_EXT.search(url) is not None
            if not is_pdf and "html" not in ctype:
                return False, "", "", "", [], "not html (%s)" % ctype[:40]

            ceiling = PDF_MAX_BYTES if is_pdf else MAX_BYTES

            # Read with a ceiling rather than trusting content-length,
            # which plenty of servers get wrong or omit.
            buf = io.BytesIO()
            for chunk in r.iter_content(16384):
                buf.write(chunk)
                if buf.tell() > ceiling:
                    return False, "", "", "", [], "too big"
            raw = buf.getvalue()

        if is_pdf:
            title, text = pdf_text(raw, url)
            if not text:
                # Almost always a scan: an image of a page, with no text
                # layer for anyone to read.
                return False, "", "", "", [], "pdf with no text layer"
            return True, title, text, "", [], ""

        encoding = r.encoding or "utf-8"
        body = raw.decode(encoding, errors="replace")
    except requests.RequestException as e:
        return False, "", "", "", [], type(e).__name__

    parser = Extract(url)
    try:
        parser.feed(body)
    except Exception as e:                       # noqa: BLE001
        return False, "", "", "", [], "parse: %s" % e

    title = html.unescape(parser.title).strip()[:300]
    return True, title, parser.text, parser.lang, parser.links, ""


def crawl(allow, max_pages, max_depth, verbose=True):
    searchdb.init()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept-Language"] = "fr,ar,en;q=0.8"

    done = 0
    started = time.time()

    while done < max_pages:
        batch = searchdb.take_queued(limit=40, max_depth=max_depth)
        if not batch:
            if verbose:
                print("frontier empty")
            break

        progressed = False
        for item in batch:
            if done >= max_pages:
                break
            url, host, depth = item["url"], item["host"], item["depth"]

            if not in_scope(url, allow):
                searchdb.mark(url, searchdb.SKIPPED, "out of scope")
                continue
            if JUNK_PATH.search(urllib.parse.urlsplit(url).path):
                searchdb.mark(url, searchdb.SKIPPED, "boilerplate page")
                continue
            if searchdb.host_page_count(host) >= HOST_CAP:
                searchdb.mark(url, searchdb.SKIPPED, "host cap")
                continue

            rp = robots_for(host, session)
            if not rp.can_fetch(USER_AGENT, url):
                searchdb.mark(url, searchdb.SKIPPED, "robots.txt")
                continue

            wait_for_host(host)
            ok, title, text, lang, links, note = fetch(session, url)
            searchdb.touch_host(host)
            progressed = True

            if not ok:
                searchdb.mark(url, searchdb.FAILED, note)
                if verbose:
                    print("  x %-58s %s" % (url[:58], note))
                continue

            # A page with almost no prose is a menu, a login wall or a
            # gallery. Indexing it only pollutes results.
            if len(text) < 250:
                searchdb.mark(url, searchdb.SKIPPED, "too little text")
                continue

            searchdb.store_page(url, title, text, lang)
            searchdb.mark(url, searchdb.DONE)
            done += 1
            if verbose:
                print("  %5d %-58s %s" % (done, url[:58], title[:40]))

            if depth >= max_depth:
                continue
            seen = set()
            for raw_link in links:
                link = normalise(raw_link)
                if not link or link in seen:
                    continue
                seen.add(link)
                if SKIP_EXT.search(link):
                    continue
                if not in_scope(link, allow):
                    continue
                searchdb.enqueue(link, depth + 1)
                searchdb.note_link(link)

        if not progressed:
            if verbose:
                print("nothing fetchable in this batch")
            break

    took = time.time() - started
    return {"fetched": done, "seconds": round(took, 1),
            "stats": searchdb.stats()}


def read_seeds(path):
    seeds = []
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                seeds.append(line)
    return seeds


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="seeds.txt")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stats", action="store_true",
                    help="print index stats and exit")
    args = ap.parse_args()

    searchdb.init()

    if args.stats:
        for k, v in searchdb.stats().items():
            print("  %-8s %s" % (k, v))
        return

    allow = []
    if os.path.exists(args.seeds):
        added = 0
        for seed in read_seeds(args.seeds):
            url = normalise(seed)
            if not url:
                continue
            host = searchdb.host_of(url)
            if host and host not in allow:
                # The seed list IS the scope: every host you seed is a
                # host the crawler may follow links within, and no other.
                allow.append(host[4:] if host.startswith("www.") else host)
            if searchdb.enqueue(url, 0):
                added += 1
        print("seeds: %d hosts, %d new URLs queued" % (len(allow), added))
    else:
        print("no %s - crawling only what is already queued" % args.seeds)
        rows = searchdb.take_queued(limit=500, max_depth=args.max_depth)
        for r in rows:
            h = r["host"]
            h = h[4:] if h.startswith("www.") else h
            if h not in allow:
                allow.append(h)

    if not allow:
        print("nothing in scope - add seeds to %s first" % args.seeds)
        return 1

    print("scope: %s" % ", ".join(allow))
    result = crawl(allow, args.max_pages, args.max_depth, not args.quiet)
    print("\nfetched %(fetched)d pages in %(seconds)ss" % result)
    for k, v in result["stats"].items():
        print("  %-8s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
