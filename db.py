"""SQLite persistence layer.

Replaces the three flat JSON files (chat_data.json, credits.json,
users.json) with one real database: app.db, plain SQLite so there's
nothing to install or run - the whole "server" is a file on disk, same
as the JSON files were, but now with a schema, indices, and atomic
transactions instead of hand-rolled read-modify-write-the-whole-file.

app.py keeps working with the same in-memory dicts (THREADS/CREDITS/USERS)
it always has - only load_threads()/save_threads()/etc underneath change
where those dicts come from. That keeps this a storage-layer swap, not a
rewrite of every route that touches them.
"""
import json
import os
import sqlite3
import threading

# Overridable so a deployment can point the database at persistent
# storage. On a container host the working directory is usually
# ephemeral - it's recreated on every rebuild, taking accounts and
# conversations with it - so being able to move this to a mounted volume
# is the difference between a demo and something people can keep using.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db"),
)
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# Old JSON files - read once, on first launch, to carry existing chats,
# credits, and accounts into the new database. Never written again after.
_LEGACY_FILES = {
    "threads": "chat_data.json",
    "credits": "credits.json",
    "users": "users.json",
}

_GUEST_OWNER = "guest"  # sentinel row in `credits` for the shared, signed-out pool

_lock = threading.Lock()
_conn = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    email             TEXT UNIQUE NOT NULL,
    -- Left over from the old email/password signup flow, before this app
    -- switched to Google sign-in only. Always empty for accounts created
    -- since - kept (rather than dropped) so this migration doesn't have
    -- to touch every existing row.
    password_hash     TEXT NOT NULL DEFAULT '',
    google_id         TEXT,
    plan              TEXT NOT NULL DEFAULT 'free',
    created           TEXT NOT NULL,
    stripe_customer_id TEXT,
    -- Subscription state mirrored from Stripe via the webhook. Stripe is
    -- the source of truth; these are a local cache so the account UI can
    -- show a renewal date without an API round-trip on every page load.
    stripe_subscription_id TEXT,
    subscription_status    TEXT,
    current_period_end     TEXT,
    cancel_at_period_end   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS credits (
    owner_id     TEXT PRIMARY KEY,   -- a user's id, or 'guest' for the shared pool
    balance      INTEGER NOT NULL,
    starting     INTEGER NOT NULL,
    plan         TEXT NOT NULL DEFAULT 'free',
    last_refill  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_codes (
    email       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id       TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    content  TEXT NOT NULL,
    created  TEXT NOT NULL
);

-- Video generations used, per owner, per calendar month.
--
-- Deliberately NOT a column on `credits`. Credits refill on a timer and
-- are meant to be spent; this counts something that costs real money per
-- use and must not refill, roll over, or be reachable by waiting two
-- hours. Keeping them in separate tables makes that difference
-- structural rather than a rule someone has to remember.
--
-- `month` is 'YYYY-MM'. Storing the period rather than a reset timestamp
-- means there is no scheduled job to run and no clock to drift: the row
-- for a past month simply stops being the one that is read.
CREATE TABLE IF NOT EXISTS video_quota (
    owner_id TEXT NOT NULL,
    month    TEXT NOT NULL,
    used     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, month)
);

CREATE TABLE IF NOT EXISTS custom_instructions (
    owner_id TEXT PRIMARY KEY,
    text     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS threads (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    mode     TEXT NOT NULL,
    updated  TEXT NOT NULL,
    -- Whose thread this is: a user's id, or "guest:<random>" for a
    -- signed-out visitor (one per browser session, not one shared pool -
    -- see current_owner_id() in app.py). NULL means it predates this
    -- column and belongs to nobody currently signed in, so it just
    -- won't show up in anyone's list any more.
    owner_id TEXT,
    -- The message list stays one JSON blob per thread rather than a fully
    -- normalized table. Nothing in this app ever queries an individual
    -- message by role/provider/etc - threads are always read and written
    -- whole - so normalizing would add joins with no matching access
    -- pattern to justify them.
    messages_json TEXT NOT NULL DEFAULT '[]'
);
"""


def _migrate_columns(conn):
    """Additive column migrations for databases created before a column
    existed. CREATE TABLE IF NOT EXISTS only applies to brand-new tables,
    so a users table from before Stripe billing needs this to pick up
    stripe_customer_id, and a threads table from before per-owner
    isolation needs owner_id."""
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "stripe_customer_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        conn.commit()
    if "google_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
        conn.commit()
    for col, ddl in (
        ("stripe_subscription_id", "TEXT"),
        ("subscription_status", "TEXT"),
        ("current_period_end", "TEXT"),
        ("cancel_at_period_end", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()

    thread_cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
    if "owner_id" not in thread_cols:
        conn.execute("ALTER TABLE threads ADD COLUMN owner_id TEXT")
        conn.commit()


def _connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
        _migrate_columns(_conn)
        _migrate_legacy_json()
    return _conn


def _migrate_legacy_json():
    """One-time import from the old JSON files into empty tables.

    Runs at most once per table: if `users` already has rows, signup data
    plainly already lives in the database and re-importing would either
    duplicate it or silently overwrite newer accounts with a stale file.
    The check is per-table so a database that already has users but was
    never handed any legacy threads still picks those up.
    """
    conn = _conn
    base = os.path.dirname(DB_PATH)

    def _read_json(name):
        path = os.path.join(base, _LEGACY_FILES[name])
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    if conn.execute("SELECT 1 FROM threads LIMIT 1").fetchone() is None:
        data = _read_json("threads") or {}
        for tid, t in data.items():
            conn.execute(
                "INSERT OR IGNORE INTO threads (id, title, mode, updated, messages_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, t.get("title", "New chat"), t.get("mode", "chat"),
                 t.get("updated", ""), json.dumps(t.get("messages", []))),
            )

    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
        data = _read_json("users") or {}
        for uid, u in data.items():
            conn.execute(
                "INSERT OR IGNORE INTO users (id, email, password_hash, plan, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, u["email"], u["password_hash"], u.get("plan", "free"),
                 u.get("created", "")),
            )
            c = u.get("credits", {})
            conn.execute(
                "INSERT OR IGNORE INTO credits (owner_id, balance, starting, plan, last_refill) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, c.get("balance", 0), c.get("starting", 0),
                 c.get("plan", u.get("plan", "free")), c.get("last_refill", "")),
            )

    if conn.execute("SELECT 1 FROM credits WHERE owner_id = ?",
                    (_GUEST_OWNER,)).fetchone() is None:
        c = _read_json("credits")
        if c:
            conn.execute(
                "INSERT INTO credits (owner_id, balance, starting, plan, last_refill) "
                "VALUES (?, ?, ?, ?, ?)",
                (_GUEST_OWNER, c.get("balance", 0), c.get("starting", 0),
                 c.get("plan", "free"), c.get("last_refill", "")),
            )

    conn.commit()


# ----------------------------------------------------------------------
# Threads
# ----------------------------------------------------------------------
def load_threads():
    """-> {thread_id: {title, messages, updated, mode, owner_id}}, the
    same shape app.py has always kept in memory as THREADS."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, mode, updated, owner_id, messages_json FROM threads").fetchall()
    return {
        r["id"]: {
            "title": r["title"],
            "mode": r["mode"],
            "updated": r["updated"],
            "owner_id": r["owner_id"],
            "messages": json.loads(r["messages_json"]),
        }
        for r in rows
    }


def save_threads(threads):
    """Replace the whole `threads` table with the contents of `threads`.

    A full replace rather than a diff - THREADS in app.py is small (one
    person's conversation history) and every call site already has the
    complete, current dict in memory, so there's nothing to gain from
    tracking per-row deltas.
    """
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM threads")
        conn.executemany(
            "INSERT INTO threads (id, title, mode, updated, owner_id, messages_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(tid, t["title"], t.get("mode", "chat"), t["updated"],
              t.get("owner_id"), json.dumps(t["messages"]))
             for tid, t in threads.items()],
        )
        conn.commit()


# ----------------------------------------------------------------------
# Credits - one row per owner_id. A signed-in user's id, or a
# "guest:<random>" id minted per browser session (see current_owner_id()
# in app.py) so guests don't share one pool with every other visitor.
# ----------------------------------------------------------------------
def load_credits(owner_id):
    conn = _connect()
    row = conn.execute("SELECT balance, starting, plan, last_refill FROM credits "
                       "WHERE owner_id = ?", (owner_id,)).fetchone()
    return dict(row) if row else None


def save_credits(owner_id, credits):
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO credits (owner_id, balance, starting, plan, last_refill) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET "
            "balance=excluded.balance, starting=excluded.starting, "
            "plan=excluded.plan, last_refill=excluded.last_refill",
            (owner_id, credits["balance"], credits["starting"],
             credits["plan"], credits["last_refill"]),
        )
        conn.commit()


# ----------------------------------------------------------------------
# Users (+ their own credits row)
# ----------------------------------------------------------------------
def load_users():
    """-> {user_id: {id, email, password_hash, google_id, plan, created,
    credits, stripe_customer_id}}, the same shape app.py has always kept
    in memory as USERS. password_hash is '' for Google-only accounts."""
    conn = _connect()
    rows = conn.execute(
        "SELECT u.id, u.email, u.password_hash, u.google_id, u.plan, "
        "       u.created, u.stripe_customer_id, u.stripe_subscription_id, "
        "       u.subscription_status, u.current_period_end, "
        "       u.cancel_at_period_end, "
        "       c.balance, c.starting, c.plan AS credit_plan, c.last_refill "
        "FROM users u LEFT JOIN credits c ON c.owner_id = u.id"
    ).fetchall()
    return {
        r["id"]: {
            "id": r["id"],
            "email": r["email"],
            "password_hash": r["password_hash"] or "",
            "google_id": r["google_id"],
            "plan": r["plan"],
            "created": r["created"],
            "stripe_customer_id": r["stripe_customer_id"],
            "stripe_subscription_id": r["stripe_subscription_id"],
            "subscription_status": r["subscription_status"],
            "current_period_end": r["current_period_end"],
            "cancel_at_period_end": bool(r["cancel_at_period_end"]),
            "credits": {
                "balance": r["balance"],
                "starting": r["starting"],
                "plan": r["credit_plan"],
                "last_refill": r["last_refill"],
            },
        }
        for r in rows
    }


def save_users(users):
    """Replace `users` and their `credits` rows with the contents of
    `users`. Same full-replace reasoning as save_threads()."""
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM users")
        # Leave every guest's credits row alone - only clear rows that
        # belonged to a real (now-replaced) user id.
        conn.execute("DELETE FROM credits WHERE owner_id NOT LIKE 'guest:%' "
                     "AND owner_id != ?", (_GUEST_OWNER,))
        for uid, u in users.items():
            conn.execute(
                "INSERT INTO users (id, email, password_hash, google_id, "
                "plan, created, stripe_customer_id, stripe_subscription_id, "
                "subscription_status, current_period_end, "
                "cancel_at_period_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, u["email"], u.get("password_hash", ""),
                 u.get("google_id"), u["plan"], u["created"],
                 u.get("stripe_customer_id"), u.get("stripe_subscription_id"),
                 u.get("subscription_status"), u.get("current_period_end"),
                 1 if u.get("cancel_at_period_end") else 0),
            )
            c = u["credits"]
            conn.execute(
                "INSERT INTO credits (owner_id, balance, starting, plan, last_refill) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, c["balance"], c["starting"], c["plan"], c["last_refill"]),
            )
        conn.commit()


# ----------------------------------------------------------------------
# Email verification codes - one active code per email, single-use.
# ----------------------------------------------------------------------
def save_verification_code(email, code, expires_at):
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO verification_codes (email, code, expires_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET code=excluded.code, "
            "expires_at=excluded.expires_at",
            (email, code, expires_at),
        )
        conn.commit()


def get_verification_code(email):
    """-> {code, expires_at} or None."""
    conn = _connect()
    row = conn.execute(
        "SELECT code, expires_at FROM verification_codes WHERE email = ?",
        (email,),
    ).fetchone()
    return dict(row) if row else None


def delete_verification_code(email):
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM verification_codes WHERE email = ?", (email,))
        conn.commit()


# ----------------------------------------------------------------------
# Memory - short facts the AI has been told to remember about an owner
# (signed-in user or guest session), plus one free-text "custom
# instructions" block per owner. Both get folded into the system prompt
# on every chat (see app.py's _memory_context_block) so they carry across
# separate conversations, the same idea as ChatGPT's Memory / Custom
# Instructions.
# ----------------------------------------------------------------------
def video_used(owner_id, month):
    """How many generations this owner has spent in `month`."""
    conn = _connect()
    row = conn.execute(
        "SELECT used FROM video_quota WHERE owner_id=? AND month=?",
        (owner_id, month)).fetchone()
    return row["used"] if row else 0


def video_consume(owner_id, month):
    """Record one generation. -> the new total.

    An UPSERT rather than read-modify-write: two requests arriving
    together would otherwise both read the same count and both write
    count+1, handing out a free generation to whoever raced.
    """
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO video_quota (owner_id, month, used) VALUES (?,?,1) "
            "ON CONFLICT(owner_id, month) DO UPDATE SET used = used + 1",
            (owner_id, month))
    return video_used(owner_id, month)


def video_refund(owner_id, month):
    """Give one back when a generation failed on the provider's side.

    Charging for a clip that never arrived is the kind of thing people
    remember, and the failure is ours to absorb - PixVerse does not bill
    us for a rejected prompt either.
    """
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE video_quota SET used = MAX(0, used - 1) "
            "WHERE owner_id=? AND month=?", (owner_id, month))


def load_memories(owner_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content, created FROM memories WHERE owner_id = ? "
        "ORDER BY created ASC", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def add_memory(owner_id, memory_id, content, created):
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO memories (id, owner_id, content, created) "
            "VALUES (?, ?, ?, ?)", (memory_id, owner_id, content, created),
        )
        conn.commit()


def delete_memory(owner_id, memory_id):
    """-> True if a row belonging to owner_id was actually deleted."""
    conn = _connect()
    with _lock:
        cur = conn.execute(
            "DELETE FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, owner_id),
        )
        conn.commit()
        return cur.rowcount > 0


def load_custom_instructions(owner_id):
    conn = _connect()
    row = conn.execute(
        "SELECT text FROM custom_instructions WHERE owner_id = ?",
        (owner_id,)).fetchone()
    return row["text"] if row else ""


def save_custom_instructions(owner_id, text):
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO custom_instructions (owner_id, text) VALUES (?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET text=excluded.text",
            (owner_id, text),
        )
        conn.commit()
