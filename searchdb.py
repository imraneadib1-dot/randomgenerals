"""The search index: its own SQLite file, deliberately not app.db.

WHY A SEPARATE FILE

app.db holds accounts, threads and payments, and gunicorn's eight
threads are on it constantly. A crawler writing tens of thousands of
rows would hold write locks for long stretches, and the first symptom
would be the site going slow for real visitors with nothing in the logs
to explain it. Two files means the crawler cannot ever be the reason a
page is slow to load.

WHAT IT STORES

  pages      one row per fetched document, with its extracted text
  pages_fts  the inverted index over that text, FTS5
  frontier   URLs discovered but not yet fetched
  hosts      per-host politeness state: robots.txt and last fetch time

RANKING

FTS5's bm25() does the term weighting. Title matches count for more than
body matches, and a page other pages link to is nudged up. That is the
whole of it - no link graph eigenvectors, no learned ranker. On a
corpus this size, honest BM25 with a title boost beats anything
complicated, and it can be explained to whoever asks why a result is
where it is.
"""
import html
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse

DB_PATH = os.environ.get(
    "SEARCH_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "search.db"))

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

# States a frontier URL can be in.
QUEUED, DONE, FAILED, SKIPPED = "queued", "done", "failed", "skipped"


def _connect():
    """One connection per thread, like db.py - a shared connection with
    unlocked reads is what caused the InterfaceError in /api/usage."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    return conn


def init():
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        conn = _connect()
        with conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                id        INTEGER PRIMARY KEY,
                url       TEXT UNIQUE NOT NULL,
                host      TEXT NOT NULL,
                title     TEXT NOT NULL DEFAULT '',
                body      TEXT NOT NULL DEFAULT '',
                lang      TEXT NOT NULL DEFAULT '',
                kind      TEXT NOT NULL DEFAULT '',
                fetched   REAL NOT NULL DEFAULT 0,
                links_in  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS pages_host ON pages(host);

            -- remove_diacritics 2 so "eleve" finds "élève"; there is no
            -- stemmer here because the corpus is French, Arabic and
            -- English at once and a French stemmer would quietly mangle
            -- the other two.
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                title, body,
                content='pages', content_rowid='id',
                tokenize="unicode61 remove_diacritics 2"
            );

            CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
              INSERT INTO pages_fts(rowid, title, body)
              VALUES (new.id, new.title, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
              INSERT INTO pages_fts(pages_fts, rowid, title, body)
              VALUES ('delete', old.id, old.title, old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
              INSERT INTO pages_fts(pages_fts, rowid, title, body)
              VALUES ('delete', old.id, old.title, old.body);
              INSERT INTO pages_fts(rowid, title, body)
              VALUES (new.id, new.title, new.body);
            END;

            CREATE TABLE IF NOT EXISTS frontier (
                url    TEXT PRIMARY KEY,
                host   TEXT NOT NULL,
                depth  INTEGER NOT NULL DEFAULT 0,
                state  TEXT NOT NULL DEFAULT 'queued',
                added  REAL NOT NULL DEFAULT 0,
                note   TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS frontier_state
                ON frontier(state, depth, added);

            CREATE TABLE IF NOT EXISTS hosts (
                host           TEXT PRIMARY KEY,
                robots         TEXT NOT NULL DEFAULT '',
                robots_fetched REAL NOT NULL DEFAULT 0,
                last_fetch     REAL NOT NULL DEFAULT 0,
                pages          INTEGER NOT NULL DEFAULT 0
            );
            """)
            # Added after the first crawl, so an existing search.db does
            # not have to be thrown away to get result filters. This runs
            # BEFORE the index on that column - putting the CREATE INDEX
            # in the script above meant it ran against a table that had
            # not been altered yet, and every open of an older database
            # died with "no such column: kind".
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
            if "kind" not in cols:
                conn.execute(
                    "ALTER TABLE pages ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS pages_kind ON pages(kind)")
        _initialised = True


# What a document is, for the filter tabs. Derived from the address and
# the title rather than stored by the crawler, so the rules can be
# changed without recrawling 40,000 pages.
KINDS = ("examen", "exercice", "cours", "corrige")

_KIND_PATTERNS = (
    ("examen", re.compile(
        r"examen|bac(?:calaur)|concours|epreuve|épreuve|sujet|annale|"
        r"national|regional|régional|devoir|controle|contrôle|"
        r"الامتحان|امتحان|الوطني", re.I)),
    ("corrige", re.compile(r"corrig|solution|correction|réponse|reponse|تصحيح", re.I)),
    ("exercice", re.compile(r"exercice|exo|serie|série|td\b|travaux|تمارين", re.I)),
    ("cours", re.compile(
        r"cours|lecon|leçon|chapitre|resume|résumé|fiche|formulaire|"
        r"rappel|درس|ملخص", re.I)),
)


def classify(url, title):
    """One label per document. Checked most-specific first: a page
    called "corrigé de l'examen national" is more useful filed under
    corrections than under exams."""
    hay = "%s %s" % (url, title)
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(hay):
            return kind
    return ""


def reclassify_all():
    """Recompute every kind. Cheap - a few seconds over tens of
    thousands of rows - and it means changing the patterns above does
    not mean recrawling."""
    init()
    conn = _connect()
    rows = conn.execute("SELECT id, url, title FROM pages").fetchall()
    with conn:
        conn.executemany(
            "UPDATE pages SET kind = ? WHERE id = ?",
            [(classify(r["url"], r["title"]), r["id"]) for r in rows])
    return len(rows)


# ---------------------------------------------------------------- frontier

def host_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""


def enqueue(url, depth=0):
    """Add a URL to be crawled. Returns True if it was new."""
    init()
    host = host_of(url)
    if not host:
        return False
    conn = _connect()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO frontier (url, host, depth, state, added) "
            "VALUES (?, ?, ?, ?, ?)", (url, host, depth, QUEUED, time.time()))
    return (cur.rowcount or 0) > 0


def take_queued(limit=50, max_depth=3):
    """Shallowest first, so the crawl broadens before it deepens."""
    init()
    rows = _connect().execute(
        "SELECT url, host, depth FROM frontier "
        "WHERE state = ? AND depth <= ? "
        "ORDER BY depth ASC, added ASC LIMIT ?",
        (QUEUED, max_depth, limit)).fetchall()
    return [dict(r) for r in rows]


def mark(url, state, note=""):
    init()
    conn = _connect()
    with conn:
        conn.execute("UPDATE frontier SET state = ?, note = ? WHERE url = ?",
                     (state, note[:200], url))


def queue_size():
    init()
    row = _connect().execute(
        "SELECT COUNT(*) AS n FROM frontier WHERE state = ?",
        (QUEUED,)).fetchone()
    return row["n"] if row else 0


# ------------------------------------------------------------------- hosts

def host_state(host):
    init()
    row = _connect().execute("SELECT * FROM hosts WHERE host = ?",
                             (host,)).fetchone()
    return dict(row) if row else None


def save_robots(host, body):
    init()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO hosts (host, robots, robots_fetched) VALUES (?, ?, ?) "
            "ON CONFLICT(host) DO UPDATE SET robots = excluded.robots, "
            "robots_fetched = excluded.robots_fetched",
            (host, body, time.time()))


def touch_host(host):
    init()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO hosts (host, last_fetch, pages) VALUES (?, ?, 1) "
            "ON CONFLICT(host) DO UPDATE SET last_fetch = excluded.last_fetch, "
            "pages = hosts.pages + 1", (host, time.time()))


def host_page_count(host):
    st = host_state(host)
    return st["pages"] if st else 0


# ------------------------------------------------------------------- pages

def store_page(url, title, body, lang=""):
    init()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO pages (url, host, title, body, lang, kind, fetched) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET title = excluded.title, "
            "body = excluded.body, lang = excluded.lang, "
            "kind = excluded.kind, fetched = excluded.fetched",
            (url, host_of(url), title[:300], body, lang,
             classify(url, title), time.time()))


def note_link(url):
    """Somebody linked to this page. Counted even before it is fetched,
    which is what makes it a useful ranking signal rather than a
    by-product of crawl order."""
    init()
    conn = _connect()
    with conn:
        conn.execute("UPDATE pages SET links_in = links_in + 1 WHERE url = ?",
                     (url,))


def page_count():
    init()
    row = _connect().execute("SELECT COUNT(*) AS n FROM pages").fetchone()
    return row["n"] if row else 0


def stats():
    init()
    conn = _connect()
    pages = conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"]
    hosts = conn.execute("SELECT COUNT(*) AS n FROM hosts").fetchone()["n"]
    queued = conn.execute("SELECT COUNT(*) AS n FROM frontier WHERE state=?",
                          (QUEUED,)).fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) AS n FROM frontier WHERE state=?",
                        (DONE,)).fetchone()["n"]
    try:
        size = os.path.getsize(DB_PATH)
    except OSError:
        size = 0
    return {"pages": pages, "hosts": hosts, "queued": queued,
            "done": done, "bytes": size}


# ------------------------------------------------------------------ search

OPERATORS = ("site", "filetype", "lang", "kind")
_OPERATOR_RE = re.compile(r"\b(site|filetype|lang|kind):([^\s]+)", re.I)
_NEGATIVE_RE = re.compile(r"(?:^|\s)-([^\s-][^\s]*)")


def parse_query(raw):
    """Pull the operators out of what someone typed.

        site:alloschool.com   only that host
        filetype:pdf          only PDFs
        lang:ar               only pages declaring that language
        kind:examen           only past papers
        -forum                documents NOT containing that word

    -> (text, filters, negatives)
    """
    filters = {}

    def take(match):
        filters.setdefault(match.group(1).lower(), []).append(
            match.group(2).lower().strip('"\''))
        return " "

    text = _OPERATOR_RE.sub(take, raw or "")
    negatives = [m.group(1) for m in _NEGATIVE_RE.finditer(text)][:6]
    text = _NEGATIVE_RE.sub(" ", text)
    return text.strip(), filters, negatives


def _fts_query(raw, negatives=()):
    """Turn what someone typed into an FTS5 query.

    Every term is quoted. FTS5 treats - : * ( ) as operators, so an
    apostrophe or a hyphen out of a real question ("qu'est-ce que") is a
    syntax error rather than a search, and the reader gets a crash where
    they expected results.
    """
    terms = [t for t in raw.replace('"', " ").split() if t.strip()]
    if not terms:
        return ""
    q = " ".join('"%s"' % t.replace("'", "''") for t in terms[:12])
    if negatives:
        q += " NOT (%s)" % " OR ".join(
            '"%s"' % n.replace("'", "''") for n in negatives)
    return q


def _fold(s):
    """Lowercase and strip accents, so a title comparison agrees with
    what the FTS tokenizer already did with remove_diacritics."""
    decomposed = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _where(filters, kind=None):
    """-> (sql fragment, params).

    Built from a fixed set of columns. An operator someone invents
    cannot reach the query, and every value is a bound parameter.
    """
    clauses, params = [], []

    for host in filters.get("site", [])[:3]:
        clauses.append("(p.host = ? OR p.host LIKE ?)")
        params += [host, "%." + host]
    for ext in filters.get("filetype", [])[:2]:
        clauses.append("p.url LIKE ?")
        params.append("%." + re.sub(r"[^a-z0-9]", "", ext)[:8])
    for lang in filters.get("lang", [])[:2]:
        clauses.append("p.lang LIKE ?")
        params.append(re.sub(r"[^a-zA-Z-]", "", lang)[:8] + "%")

    wanted = kind or (filters.get("kind") or [None])[0]
    if wanted in KINDS:
        clauses.append("p.kind = ?")
        params.append(wanted)

    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def search(query, limit=20, offset=0, kind=None):
    """Ranked results, plus what it took to get them.

    -> {"results": [...], "total": int, "took_ms": float,
        "filters": {...}, "negatives": [...], "counts": {kind: n}}

    Never raises. A malformed query is a query with no results, not a
    500 on a page somebody reached from a link.
    """
    init()
    started = time.time()
    empty = {"results": [], "total": 0, "took_ms": 0.0,
             "filters": {}, "negatives": [], "counts": {}}

    text, filters, negatives = parse_query(query or "")
    match = _fts_query(text, negatives)
    if not match:
        return empty

    where, params = _where(filters, kind)
    conn = _connect()

    try:
        rows = conn.execute(
            """
            SELECT p.url, p.title, p.host, p.links_in, p.lang, p.kind,
                   snippet(pages_fts, 1, char(2), char(3), ' … ', 14) AS snip,
                   bm25(pages_fts, 8.0, 1.0) AS score
            FROM pages_fts
            JOIN pages p ON p.id = pages_fts.rowid
            WHERE pages_fts MATCH ?""" + where + """
            ORDER BY score
            LIMIT ? OFFSET ?
            """, [match] + params + [limit * 4, offset]).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) AS n FROM pages_fts JOIN pages p "
            "ON p.id = pages_fts.rowid WHERE pages_fts MATCH ?" + where,
            [match] + params).fetchone()["n"]

        # How many results each tab would show, so a tab can be labelled
        # with its count instead of leading somewhere empty.
        counts = {}
        base_where, base_params = _where(filters, None)
        for row in conn.execute(
                "SELECT p.kind, COUNT(*) AS n FROM pages_fts JOIN pages p "
                "ON p.id = pages_fts.rowid WHERE pages_fts MATCH ?"
                + base_where + " GROUP BY p.kind",
                [match] + base_params):
            counts[row["kind"] or "autre"] = row["n"]
    except sqlite3.OperationalError:
        return empty

    # bm25() is negative and smaller is better.
    #
    # The link nudge used to be 0.35 per log-link, which was enough to
    # push AlloSchool's "Primaire" and "Collège" pages above the page
    # actually titled "Classes Préparatoires (CPGE)" for the query
    # "cpge" - those pages are linked from everywhere on the site, so
    # popularity beat relevance. It is the weakest signal in an index
    # this size and now scores like it.
    terms = [_fold(t) for t in text.split() if t.strip()]
    scored = []
    for r in rows:
        row = dict(r)
        boost = math.log1p(min(row["links_in"], 40)) * 0.12

        title = _fold(row["title"] or "")
        if terms and all(t in title for t in terms):
            boost += 4.0
            if title.strip() == " ".join(terms):
                boost += 2.0

        scored.append((row["score"] - boost, row))
    scored.sort(key=lambda x: x[0])

    out = []
    for _, r in scored[:limit]:
        raw = r["snip"] or ""
        # The highlight markers are control characters, not "<mark>", so
        # the snippet can be escaped BEFORE they become tags. This text
        # came off a crawled page: asking a template to trust it would
        # be putting other people's HTML into our own.
        safe = (html.escape(raw.replace("\x02", "\x00").replace("\x03", "\x01"))
                .replace("\x00", "<mark>").replace("\x01", "</mark>"))
        out.append({"title": r["title"] or r["url"],
                    "url": r["url"],
                    "host": r["host"],
                    "kind": r["kind"] or "",
                    "lang": r["lang"] or "",
                    "is_pdf": r["url"].lower().endswith(".pdf"),
                    "snippet": raw.replace("\x02", "").replace("\x03", ""),
                    "snippet_html": safe,
                    "links_in": r["links_in"]})

    return {"results": out, "total": total,
            "took_ms": round((time.time() - started) * 1000, 1),
            "filters": filters, "negatives": negatives, "counts": counts}


def suggest(prefix, limit=8):
    """Titles matching what has been typed so far, for the search box.

    Reads only our own index. A suggestion service would mean sending
    every keystroke somebody types to a third party.
    """
    init()
    prefix = (prefix or "").strip()
    if len(prefix) < 2:
        return []
    terms = [t for t in prefix.replace('"', " ").split() if t]
    if not terms:
        return []

    # Earlier words are whole words; the last one is still being typed.
    head = " ".join('"%s"' % t.replace("'", "''") for t in terms[:-1])
    last = terms[-1].replace("'", "''")
    match = (head + " " if head else "") + '"%s"*' % last

    try:
        rows = _connect().execute(
            "SELECT p.title, p.url FROM pages_fts JOIN pages p "
            "ON p.id = pages_fts.rowid WHERE pages_fts MATCH ? "
            "ORDER BY bm25(pages_fts, 8.0, 1.0) LIMIT ?",
            (match, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        return []

    seen, out = set(), []
    for r in rows:
        title = (r["title"] or "").strip()
        key = _fold(title)
        if not title or key in seen:
            continue
        seen.add(key)
        out.append({"title": title[:90], "url": r["url"]})
        if len(out) >= limit:
            break
    return out
