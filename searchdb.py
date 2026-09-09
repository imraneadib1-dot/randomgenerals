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
        _initialised = True


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
            "INSERT INTO pages (url, host, title, body, lang, fetched) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET title = excluded.title, "
            "body = excluded.body, lang = excluded.lang, "
            "fetched = excluded.fetched",
            (url, host_of(url), title[:300], body, lang, time.time()))


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

def _fts_query(raw):
    """Turn what someone typed into an FTS5 query.

    Every term is quoted. FTS5 treats characters like - : * ( ) as
    operators, so an unquoted apostrophe or hyphen from a real question
    ("qu'est-ce que") is a syntax error rather than a search, and the
    user sees a crash where they expected no results.
    """
    terms = [t for t in raw.replace('"', " ").split() if t.strip()]
    if not terms:
        return ""
    return " ".join('"%s"' % t.replace("'", "''") for t in terms[:12])


def _fold(s):
    """Lowercase and strip accents, so a title comparison agrees with
    what the FTS tokenizer already did with remove_diacritics."""
    decomposed = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def search(query, limit=20, offset=0):
    """Ranked results. Empty list when there is nothing, never an error."""
    init()
    match = _fts_query(query)
    if not match:
        return []

    try:
        rows = _connect().execute(
            """
            SELECT p.url, p.title, p.host, p.links_in, p.lang,
                   snippet(pages_fts, 1, char(2), char(3), ' … ', 14) AS snip,
                   bm25(pages_fts, 8.0, 1.0) AS score
            FROM pages_fts
            JOIN pages p ON p.id = pages_fts.rowid
            WHERE pages_fts MATCH ?
            ORDER BY score
            LIMIT ? OFFSET ?
            """, (match, limit * 4, offset)).fetchall()
    except sqlite3.OperationalError:
        # A malformed MATCH is a bad query, not a broken index.
        return []

    # bm25() is negative and smaller is better.
    #
    # The link nudge used to be 0.35 per log-link, which was enough to
    # push AlloSchool's "Primaire" and "Collège" pages above the page
    # actually titled "Classes Préparatoires (CPGE)" for the query
    # "cpge" - those category pages are linked from every other page on
    # the site, so popularity beat relevance. Popularity is the weakest
    # signal here and now scores like it.
    terms = [_fold(t) for t in query.split() if t.strip()]
    scored = []
    for r in rows:
        row = dict(r)
        boost = math.log1p(min(row["links_in"], 40)) * 0.12

        # Someone searching "cpge" wants the page called that. An exact
        # title hit is the strongest thing a small index knows.
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
        # The highlight markers are control characters, not "<mark>",
        # so the snippet can be escaped BEFORE they become tags. This
        # text came off a crawled page: asking a template to trust it
        # would be putting other people's HTML into our own.
        safe = (html.escape(raw.replace("\x02", "\x00").replace("\x03", "\x01"))
                .replace("\x00", "<mark>").replace("\x01", "</mark>"))
        out.append({"title": r["title"] or r["url"],
                    "url": r["url"],
                    "host": r["host"],
                    "snippet": raw.replace("\x02", "").replace("\x03", ""),
                    "snippet_html": safe,
                    "links_in": r["links_in"]})
    return out
