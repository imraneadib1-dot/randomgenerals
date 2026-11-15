"""The engine's own database: conversations, a context cache, metadata.

    from pip_store import Store
    store = Store()                      # pip.db beside this file, or PIP_DB_PATH
    cid = store.conversation_create()
    store.message_add(cid, "user", "hello", tokens=2)
    store.messages(cid)

One SQLite file in WAL mode, one connection per thread - the same shape
as db.py, for the same reason: many readers beside one writer, and no
two threads ever sharing a connection. It is separate from app.db on
purpose. app.db is the product (accounts, billing, people's threads);
this is the engine's working state, and it can be deleted without
losing anything a person owns.

THREE TABLES

  conversations + messages   a conversation the engine keeps for a
                             caller that sends `conversation_id`
                             instead of resending its whole history.
  context_cache              the token count of text the engine has
                             already measured. Fitting a long
                             conversation into the window means
                             counting every turn on every request;
                             only the newest turn is ever new.
  meta                       what is being served: model, base,
                             adapter, schema version.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid

DEFAULT_PATH = os.environ.get(
    "PIP_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pip.db"))

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL
                     REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL
                     CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content          TEXT NOT NULL,
    tokens           INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation
    ON messages (conversation_id, id);

CREATE TABLE IF NOT EXISTS context_cache (
    key      TEXT PRIMARY KEY,
    tokens   INTEGER NOT NULL,
    hits     INTEGER NOT NULL DEFAULT 0,
    used_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS context_cache_by_use ON context_cache (used_at);
"""


def text_key(text: str) -> str:
    """The cache key for a piece of text: its SHA-256."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class Store:
    """One database file. Safe to share between threads."""

    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_PATH
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        self._local = threading.local()
        self._write = threading.Lock()
        with self._write:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.commit()
        self.meta_set("schema_version", SCHEMA_VERSION)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _run(self, sql: str, params=()) -> sqlite3.Cursor:
        """One write, committed."""
        with self._write:
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    # --- metadata ----------------------------------------------------

    def meta_set(self, key: str, value) -> None:
        self._run(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, str(value), time.time()))

    def meta_get(self, key: str, default=None):
        row = self._conn().execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_all(self) -> dict:
        return {r["key"]: r["value"] for r in
                self._conn().execute("SELECT key, value FROM meta")}

    # --- conversations -----------------------------------------------

    def conversation_create(self, conversation_id: str | None = None,
                            title: str = "") -> str:
        """-> the id. An id that already exists is left as it is."""
        cid = conversation_id or uuid.uuid4().hex
        now = time.time()
        self._run(
            "INSERT OR IGNORE INTO conversations "
            "(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, title[:200], now, now))
        return cid

    def conversation_get(self, conversation_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m "
            "             WHERE m.conversation_id = c.id) AS messages, "
            "       (SELECT COALESCE(SUM(tokens), 0) FROM messages m "
            "             WHERE m.conversation_id = c.id) AS tokens "
            "FROM conversations c WHERE c.id = ?",
            (conversation_id,)).fetchone()
        return dict(row) if row else None

    def conversations(self, limit: int = 50) -> list[dict]:
        """Newest first."""
        rows = self._conn().execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def conversation_delete(self, conversation_id: str) -> bool:
        """-> whether there was one. Its messages go with it."""
        cur = self._run("DELETE FROM conversations WHERE id = ?",
                        (conversation_id,))
        return cur.rowcount > 0

    def message_add(self, conversation_id: str, role: str, content: str,
                    tokens: int = 0) -> int:
        """Append one turn. -> its row id. Raises sqlite3.IntegrityError
        for an unknown conversation or role."""
        now = time.time()
        with self._write:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, "
                    "tokens, created_at) VALUES (?, ?, ?, ?, ?)",
                    (conversation_id, role, content, int(tokens), now))
                # The first user turn names the conversation.
                conn.execute(
                    "UPDATE conversations SET updated_at = ?, title = CASE "
                    "WHEN title = '' AND ? = 'user' THEN ? ELSE title END "
                    "WHERE id = ?",
                    (now, role, " ".join(content.split())[:80],
                     conversation_id))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise
            return cur.lastrowid

    def messages(self, conversation_id: str) -> list[dict]:
        """The conversation in order, as chat messages."""
        rows = self._conn().execute(
            "SELECT role, content, tokens FROM messages "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,))
        return [dict(r) for r in rows]

    # --- context cache -----------------------------------------------

    def cached_tokens(self, text: str) -> int | None:
        """How many tokens `text` is, if it has been counted before."""
        key = text_key(text)
        row = self._conn().execute(
            "SELECT tokens FROM context_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        self._run("UPDATE context_cache SET hits = hits + 1, used_at = ? "
                  "WHERE key = ?", (time.time(), key))
        return row["tokens"]

    def cache_tokens(self, text: str, tokens: int) -> None:
        self._run(
            "INSERT INTO context_cache (key, tokens, used_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET tokens = excluded.tokens, "
            "used_at = excluded.used_at",
            (text_key(text), int(tokens), time.time()))

    def cache_prune(self, keep: int = 50000) -> int:
        """Drop the least recently used rows beyond `keep`. -> how many."""
        cur = self._run(
            "DELETE FROM context_cache WHERE key IN ("
            "  SELECT key FROM context_cache ORDER BY used_at DESC "
            "  LIMIT -1 OFFSET ?)", (int(keep),))
        return cur.rowcount

    def stats(self) -> dict:
        conn = self._conn()
        one = lambda sql: conn.execute(sql).fetchone()[0]      # noqa: E731
        return {
            "conversations": one("SELECT COUNT(*) FROM conversations"),
            "messages": one("SELECT COUNT(*) FROM messages"),
            "cached_texts": one("SELECT COUNT(*) FROM context_cache"),
            "cache_hits": one("SELECT COALESCE(SUM(hits), 0) FROM context_cache"),
        }
