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

# ONE CONNECTION PER THREAD, not one per process.
#
# It was one shared connection with check_same_thread=False, and gunicorn
# runs eight threads. Writes took _lock; reads did not - load_credits and
# every other SELECT called conn.execute() straight off the shared
# object. Two threads using one sqlite3 connection at the same moment is
# undefined, and what it actually produced was:
#
#     sqlite3.InterfaceError: bad parameter or other API misuse
#
# on /api/usage and /api/credits, intermittently, for real visitors. The
# parameter was fine every time; the connection was busy.
#
# Locking the reads too would fix the crash and serialise every request
# in the process behind one mutex. A connection each is better and is
# what WAL was already enabled for: WAL allows many readers alongside
# one writer, across connections, which is exactly this workload.
#
# _lock stays, for the multi-statement full-replace saves. Those DELETE
# every row and reinsert, and two of them interleaving would be a mess
# regardless of how many connections are involved.
_lock = threading.RLock()
_local = threading.local()

# Schema creation and migration run once per process, not once per
# thread. Guarded by its own lock so the second thread to arrive waits
# for the first to finish rather than racing it through CREATE TABLE.
_init_lock = threading.Lock()
_initialised = False


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
    cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,
    -- Who the account belongs to. Collected at signup and editable on
    -- the profile page.
    name TEXT NOT NULL DEFAULT '',
    -- Stored as the birth year rather than an age, because an age is
    -- wrong within twelve months of being written and nothing ever
    -- updates it. The current age is derived when needed.
    birth_year INTEGER
);

CREATE TABLE IF NOT EXISTS credits (
    owner_id     TEXT PRIMARY KEY,   -- a user's id, or 'guest' for the shared pool
    balance      INTEGER NOT NULL,
    starting     INTEGER NOT NULL,
    plan         TEXT NOT NULL DEFAULT 'free',
    last_refill  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    -- One row per owner per UTC day, so a chart is a single indexed read
    -- rather than a scan over every message ever sent. Counters only -
    -- nothing here records WHAT was asked, just how much.
    owner_id TEXT NOT NULL,
    day      TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    credits  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, day)
);

CREATE TABLE IF NOT EXISTS notification_prefs (
    owner_id TEXT NOT NULL,
    event    TEXT NOT NULL,
    channel  TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (owner_id, event, channel)
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    body       TEXT NOT NULL,
    created    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    -- One row per owner. NOT columns on `users`: save_users() deletes and
    -- reinserts that whole table, so a column added there has to be
    -- threaded through the schema, the INSERT and the SELECT or it
    -- silently does nothing. That has already cost one bug.
    owner_id        TEXT PRIMARY KEY,
    theme           TEXT    NOT NULL DEFAULT 'system',
    language        TEXT    NOT NULL DEFAULT 'en',
    timezone        TEXT    NOT NULL DEFAULT 'UTC',
    -- NULL means "use the server default", which is different from a
    -- value that happens to equal it: clearing a field must not pin it.
    default_model   TEXT,
    temperature     REAL,
    top_p           REAL,
    max_tokens      INTEGER,
    system_prompt   TEXT    NOT NULL DEFAULT '',
    web_search      INTEGER NOT NULL DEFAULT 1,
    tools_enabled   INTEGER NOT NULL DEFAULT 1,
    retention_days  INTEGER,
    avatar_url      TEXT    NOT NULL DEFAULT '',
    bio             TEXT    NOT NULL DEFAULT '',
    updated         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    -- The id in the cookie. Server-side rows are what make "log out that
    -- other device" mean anything: a bare signed cookie carrying a user
    -- id cannot be revoked, because the server never learns it exists.
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    created    TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id);

CREATE TABLE IF NOT EXISTS provider_keys (
    -- RECOVERABLE, unlike users.api_key_hash. That one is hashed because
    -- it is only ever compared; this one has to be replayed upstream, so
    -- it is encrypted and the nonce is stored beside it.
    owner_id    TEXT NOT NULL,
    provider    TEXT NOT NULL,
    ciphertext  BLOB NOT NULL,
    nonce       BLOB NOT NULL,
    last_four   TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    last_used   TEXT,
    PRIMARY KEY (owner_id, provider)
);

CREATE TABLE IF NOT EXISTS mfa_enrollment (
    owner_id     TEXT PRIMARY KEY,
    secret_ct    BLOB NOT NULL,
    secret_nonce BLOB NOT NULL,
    -- Enrolment is two-step on purpose. Marking it confirmed before a
    -- code has been verified locks out anyone whose authenticator failed
    -- to take the secret.
    confirmed    INTEGER NOT NULL DEFAULT 0,
    created      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    owner_id  TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    used_at   TEXT,
    PRIMARY KEY (owner_id, code_hash)
);

CREATE TABLE IF NOT EXISTS password_resets (
    -- SEPARATE FROM verification_codes, DELIBERATELY.
    --
    -- That table is keyed by email alone, so sharing it would mean a
    -- code mailed to prove "this address is mine" could be replayed to
    -- set a new password - two very different levels of authority behind
    -- one six-digit number. It would also mean requesting one silently
    -- cancelled a pending other.
    email      TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    -- Guessing budget. Six digits is a million combinations, which is a
    -- lot for a person and nothing for a script, so the code dies after
    -- a handful of wrong tries rather than waiting out its clock.
    attempts   INTEGER NOT NULL DEFAULT 0,
    -- When the last code was sent, so a request cannot be repeated
    -- endlessly to flood somebody's inbox.
    sent_at    TEXT NOT NULL DEFAULT ''
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

CREATE TABLE IF NOT EXISTS openrouter_spend (
    -- One row per UTC day, site-wide. Not per user: the ceiling exists
    -- to protect one bank account, and a per-user cap would still let a
    -- hundred signups spend a hundred times the limit.
    day TEXT PRIMARY KEY,
    usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS site_visits (
    -- Page views, aggregated to (day, path) the moment they happen.
    -- There is no row per request and no row per person: by the time
    -- anything is written it is already a count, so there is nothing
    -- here to link back to anybody even in principle.
    day   TEXT NOT NULL,
    path  TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, path)
);

CREATE TABLE IF NOT EXISTS site_visitors (
    -- One row per distinct visitor per day, so "how many people" can be
    -- answered separately from "how many page loads".
    --
    -- `visitor` is a truncated hash of the caller's IP and user agent
    -- salted with a value that is thrown away at the end of the day
    -- (see visitor_salt). That has three consequences worth stating:
    -- the same person is one row rather than forty; the value cannot be
    -- turned back into an IP address; and the SAME PERSON GETS A NEW
    -- HASH TOMORROW, so this can count today's visitors and can never
    -- follow anyone from one day to the next. That last property is the
    -- reason for the design, not a limitation of it.
    day     TEXT NOT NULL,
    visitor TEXT NOT NULL,
    PRIMARY KEY (day, visitor)
);

CREATE TABLE IF NOT EXISTS visitors_seen (
    -- ALL-TIME distinct visitors, which site_visitors cannot answer.
    --
    -- That table keys on a hash whose salt is destroyed nightly, so the
    -- same person is a different row tomorrow: summing its days counts
    -- one regular reader as thirty people. That is a deliberate
    -- property, not a defect - it is what stops anybody, including this
    -- dashboard, following a visitor from one day to the next - so the
    -- answer has to come from somewhere else rather than by weakening
    -- it.
    --
    -- It comes from the session cookie's own id: the user id for
    -- somebody signed in, or the per-browser guest id the app already
    -- mints to scope credits and threads. Nothing new is stored about
    -- anybody, and no new cookie exists - this counts an identifier the
    -- app was already setting for its own bookkeeping.
    --
    -- WHAT THIS NUMBER IS: distinct browsers that have loaded a page.
    -- A person on a phone and a laptop is two. Someone who clears their
    -- cookies is two. A crawler that presents a browser user agent and
    -- keeps cookies is one, and one that discards them is counted anew
    -- each visit - which is why the bot filter in app.py matters to
    -- this figure and not only to the daily one.
    visitor_key TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visit_salt (
    -- Today's salt, kept only so a restart does not double-count
    -- everybody. Yesterday's is deleted the first time a new day is
    -- seen, which is what makes yesterday's hashes permanently
    -- unreadable rather than merely inconvenient.
    day  TEXT PRIMARY KEY,
    salt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    -- Real money received, one row per completed Paddle transaction.
    --
    -- Keyed on Paddle's own transaction id because webhooks are
    -- at-least-once: Paddle retries anything that does not return 2xx,
    -- and it retried into a table keyed on anything else would count
    -- the same payment twice. INSERT OR IGNORE plus this key makes a
    -- redelivery a no-op.
    --
    -- Amounts are in MINOR UNITS (cents), as integers, exactly as
    -- Paddle sends them. Storing money as a float is how totals end up
    -- at 1.9899999999999998.
    txn_id   TEXT PRIMARY KEY,
    day      TEXT NOT NULL,
    user_id  TEXT NOT NULL DEFAULT '',
    email    TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'USD',
    -- What the customer was charged, including tax.
    gross    INTEGER NOT NULL DEFAULT 0,
    -- Paddle's cut, and what is actually left to be paid out. These are
    -- the figures worth looking at: gross is not income.
    fee      INTEGER NOT NULL DEFAULT 0,
    earnings INTEGER NOT NULL DEFAULT 0,
    created  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS connectors (
    id       TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title    TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT 'link',
    url      TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    -- The discovered operation list, as JSON. Cached at connect time
    -- rather than re-fetched per message: a spec is a document that
    -- changes on deploys, not per request, and re-downloading it on
    -- every chat turn would add a round trip to someone else's server
    -- before this app could answer anything.
    operations TEXT NOT NULL DEFAULT '[]',
    -- Optional API token. Never leaves the server: load_connectors()
    -- strips it, the same way public_user() strips password_hash, so a
    -- token cannot come back out through the settings panel that put it
    -- in.
    token    TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL DEFAULT ''
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
        ("name", "TEXT NOT NULL DEFAULT ''"),
        ("birth_year", "INTEGER"),
        ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()

    # user_settings grew two columns after it shipped. CREATE TABLE IF
    # NOT EXISTS does nothing to a table that already exists, so without
    # this they would be present on a fresh database and absent on the
    # live one - which is the worst of both.
    settings_cols = {row["name"] for row in
                     conn.execute("PRAGMA table_info(user_settings)")}
    for col, ddl in (("avatar_url", "TEXT NOT NULL DEFAULT ''"),
                     ("bio", "TEXT NOT NULL DEFAULT ''")):
        if settings_cols and col not in settings_cols:
            conn.execute(
                f"ALTER TABLE user_settings ADD COLUMN {col} {ddl}")
            conn.commit()

    thread_cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
    if "owner_id" not in thread_cols:
        conn.execute("ALTER TABLE threads ADD COLUMN owner_id TEXT")
        conn.commit()


def _connect():
    """This thread's connection, opening and initialising it if needed."""
    global _initialised

    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
    conn.execute("PRAGMA foreign_keys=ON")
    # With several connections there is now a real writer lock to
    # contend for. Waiting five seconds for it beats "database is
    # locked" on a page load; WAL keeps writers rare enough that this
    # should never actually be reached.
    conn.execute("PRAGMA busy_timeout=5000")

    with _init_lock:
        if not _initialised:
            conn.executescript(SCHEMA)
            conn.commit()
            _migrate_columns(conn)
            _migrate_legacy_json(conn)
            _initialised = True

    _local.conn = conn
    return conn


def _migrate_legacy_json(conn):
    """One-time import from the old JSON files into empty tables.

    Runs at most once per table: if `users` already has rows, signup data
    plainly already lives in the database and re-importing would either
    duplicate it or silently overwrite newer accounts with a stale file.
    The check is per-table so a database that already has users but was
    never handed any legacy threads still picks those up.
    """
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
        "       u.cancel_at_period_end, u.name, u.birth_year, "
        "       u.email_verified, "
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
            "name": r["name"] or "",
            "birth_year": r["birth_year"],
            "email_verified": bool(r["email_verified"]),
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
    # Caught here rather than left to the UNIQUE constraint, because the
    # constraint's own message names neither account and arrives after
    # every row has already been deleted. Two entries sharing an address
    # meant this function threw on every call from then on, so nothing
    # about any account could be saved - and it surfaced as a 500 on
    # whichever endpoint next tried. _find_or_create_user() in app.py is
    # what stops them being created; this is what makes it obvious if
    # one ever gets in anyway.
    seen = {}
    for uid, u in users.items():
        email = (u.get("email") or "").strip()
        if not email:
            continue
        if email in seen:
            raise RuntimeError(
                "Refusing to save: %r is on two accounts (%s and %s). "
                "Nothing was written. Merge or remove one of them - see "
                "_find_or_create_user() in app.py for how this is meant "
                "to be prevented." % (email, seen[email], uid))
        seen[email] = uid

    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM users")
        # Leave every guest's credits row alone - only clear rows that
        # belonged to a real (now-replaced) user id.
        conn.execute("DELETE FROM credits WHERE owner_id NOT LIKE 'guest:%' "
                     "AND owner_id != ?", (_GUEST_OWNER,))
        for uid, u in users.items():
            # email_verified WAS MISSING HERE, and this function
            # deletes every row before reinserting it - so verifying an
            # address set the flag in memory and the next save threw it
            # away. Anything added to the users table has to be listed
            # in three places: the schema, this INSERT, and the SELECT in
            # load_users(). Miss one and the column silently does
            # nothing.
            conn.execute(
                "INSERT INTO users (id, email, password_hash, google_id, "
                "plan, created, stripe_customer_id, stripe_subscription_id, "
                "subscription_status, current_period_end, "
                "cancel_at_period_end, name, birth_year, email_verified) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, u["email"], u.get("password_hash", ""),
                 u.get("google_id"), u["plan"], u["created"],
                 u.get("stripe_customer_id"), u.get("stripe_subscription_id"),
                 u.get("subscription_status"), u.get("current_period_end"),
                 1 if u.get("cancel_at_period_end") else 0,
                 u.get("name") or "", u.get("birth_year"),
                 1 if u.get("email_verified") else 0),
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
def delete_user(user_id, email=None):
    """Remove an account and everything scoped to it. -> rows removed.

    Every table that keys on owner_id is cleared here explicitly rather
    than left to a foreign key: the schema does not declare one for
    threads or credits, so a DELETE on users alone would leave an
    orphaned conversation history keyed to an id nobody can log into -
    invisible, undeletable, and still on disk.

    One transaction, so a failure halfway cannot leave an account that
    is half gone: still able to log in, with its memories missing.
    """
    conn = _connect()
    removed = 0
    with conn:
        for table in ("threads", "credits", "memories",
                      "custom_instructions", "connectors",
                      "user_settings", "sessions", "provider_keys",
                      "mfa_enrollment", "mfa_backup_codes",
                      "usage_log", "notification_prefs",
                      "prompt_templates"):
            cur = conn.execute(
                "DELETE FROM %s WHERE owner_id = ?" % table, (user_id,))
            removed += cur.rowcount or 0
        cur = conn.execute("DELETE FROM video_quota WHERE owner_id = ?",
                           (user_id,))
        removed += cur.rowcount or 0
        if email:
            conn.execute("DELETE FROM verification_codes WHERE email = ?",
                         (email,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        removed += cur.rowcount or 0
    return removed


def openrouter_spend_today(day):
    """Dollars spent on OpenRouter today. 0.0 if nothing yet."""
    conn = _connect()
    row = conn.execute(
        "SELECT usd FROM openrouter_spend WHERE day = ?", (day,)).fetchone()
    return float(row["usd"]) if row else 0.0


def openrouter_add_spend(day, usd):
    """Add to today's total and return the new one.

    UPSERT rather than read-modify-write: two requests finishing at once
    would otherwise both read the old total and the cheaper one would be
    lost, which on a spend counter means undercounting exactly when
    traffic is highest.
    """
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO openrouter_spend (day, usd) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd",
            (day, float(usd)))
    return openrouter_spend_today(day)


def load_connectors(owner_id, with_tokens=False):
    """The apps this owner has connected.

    Tokens are stripped unless the caller explicitly asks. Only the tool
    dispatcher has a reason to see one, and everything else - the
    settings list especially - would otherwise hand a secret back to the
    browser that submitted it.
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, kind, url, base_url, operations, token, created "
        "FROM connectors WHERE owner_id = ? ORDER BY created",
        (owner_id,)).fetchall()
    out = []
    for r in rows:
        item = {
            "id": r["id"], "title": r["title"], "kind": r["kind"],
            "url": r["url"], "base_url": r["base_url"],
            "created": r["created"],
            "has_token": bool(r["token"]),
        }
        try:
            item["operations"] = json.loads(r["operations"] or "[]")
        except (ValueError, TypeError):
            item["operations"] = []
        if with_tokens:
            item["token"] = r["token"]
        out.append(item)
    return out


def save_connector(owner_id, connector):
    """Insert or replace one connected app."""
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO connectors "
            "(id, owner_id, title, kind, url, base_url, operations, token, "
            " created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (connector["id"], owner_id, connector.get("title", ""),
             connector.get("kind", "link"), connector.get("url", ""),
             connector.get("base_url", ""),
             json.dumps(connector.get("operations") or []),
             connector.get("token", ""), connector.get("created", "")))
    return connector["id"]


def delete_connector(owner_id, connector_id):
    """Scoped to the owner, so an id from another account is a no-op
    rather than someone else's connection being removed."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM connectors WHERE id = ? AND owner_id = ?",
            (connector_id, owner_id))
    return (cur.rowcount or 0) > 0


SETTINGS_DEFAULTS = {
    "theme": "system",
    "language": "en",
    "timezone": "UTC",
    "default_model": None,
    "temperature": None,
    "top_p": None,
    "max_tokens": None,
    "system_prompt": "",
    "web_search": 1,
    "tools_enabled": 1,
    "retention_days": None,
    "avatar_url": "",
    "bio": "",
}


def note_usage(owner_id, credits, messages=1):
    """Add to today's counters. Never raises.

    Wrapped because this runs on the reply path: a usage counter that
    could not be written must not cost somebody their answer.
    """
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO usage_log (owner_id, day, messages, credits) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_id, day) DO UPDATE SET "
                "  messages = messages + excluded.messages, "
                "  credits  = credits  + excluded.credits",
                (owner_id, day, messages, int(credits)))
    except Exception:                          # noqa: BLE001
        pass


def usage_series(owner_id, days=30):
    """-> [{day, messages, credits}], oldest first, with empty days
    filled in. A chart with holes in it reads as missing data rather
    than as a quiet day."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    start = today - _dt.timedelta(days=days - 1)
    conn = _connect()
    rows = {r["day"]: r for r in conn.execute(
        "SELECT day, messages, credits FROM usage_log "
        "WHERE owner_id = ? AND day >= ? ORDER BY day",
        (owner_id, start.isoformat())).fetchall()}
    out = []
    for offset in range(days):
        key = (start + _dt.timedelta(days=offset)).isoformat()
        row = rows.get(key)
        out.append({
            "day": key,
            "messages": row["messages"] if row else 0,
            "credits": row["credits"] if row else 0,
        })
    return out


# ------------------------------------------------------- visits and money
#
# Everything below serves the owner's dashboard. Two rules shaped it:
#
#   1. Nothing identifies a visitor. Counts are incremented in place, and
#      the only per-person value is a hash that expires nightly.
#   2. Nothing here may cost anybody a page. Recording a visit runs on
#      every request, so it swallows its own errors - a broken counter
#      must not become a broken site.

def _today():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def visitor_salt(day):
    """Today's salt, creating it once and dropping every older one.

    The delete is the point. While the salt exists, a given IP always
    hashes the same way, so the same person is counted once. Once it is
    gone the day's hashes are permanently unreadable - there is no key
    left to test a guessed IP address against.
    """
    conn = _connect()
    row = conn.execute("SELECT salt FROM visit_salt WHERE day = ?",
                       (day,)).fetchone()
    if row:
        return row["salt"]
    import secrets as _secrets
    salt = _secrets.token_hex(16)
    with conn:
        conn.execute("INSERT OR IGNORE INTO visit_salt (day, salt) "
                     "VALUES (?, ?)", (day, salt))
        conn.execute("DELETE FROM visit_salt WHERE day <> ?", (day,))
    # Re-read: another worker may have won the insert, and both must
    # agree on the salt or the same person is counted twice.
    return conn.execute("SELECT salt FROM visit_salt WHERE day = ?",
                        (day,)).fetchone()["salt"]


def record_visit(path, ip, user_agent):
    """Count one page view, and the visitor behind it. Never raises."""
    try:
        import hashlib as _hashlib
        day = _today()
        salt = visitor_salt(day)
        visitor = _hashlib.sha256(
            ("%s|%s|%s" % (salt, ip or "", user_agent or "")).encode("utf-8")
        ).hexdigest()[:32]
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT INTO site_visits (day, path, views) VALUES (?, ?, 1) "
                "ON CONFLICT(day, path) DO UPDATE SET views = views + 1",
                (day, path))
            conn.execute("INSERT OR IGNORE INTO site_visitors (day, visitor) "
                         "VALUES (?, ?)", (day, visitor))
    except Exception:                          # noqa: BLE001
        pass


def launch_day():
    """The first day this app has any record of. -> "YYYY-MM-DD" or None.

    Taken as the earliest date across the tables that were written from
    the very beginning - accounts, per-day usage counters, and threads.
    NOT from site_visits: counting page views started long after launch,
    so the first visit is the day instrumentation arrived, not the day
    the product did.

    Threads carry only `updated`, so an old thread that was touched
    yesterday reports yesterday. That can only ever make the answer
    LATER than the truth, never earlier, and the other two sources are
    append-only - so the minimum across all three is still the earliest
    day anything is known to have happened.
    """
    conn = _connect()
    candidates = []
    for sql in ("SELECT MIN(substr(created, 1, 10)) FROM users",
                "SELECT MIN(day) FROM usage_log",
                "SELECT MIN(substr(updated, 1, 10)) FROM threads",
                "SELECT MIN(day) FROM payments"):
        try:
            value = conn.execute(sql).fetchone()[0]
        except Exception:                      # noqa: BLE001 - a missing
            continue                           # table must not break this
        if value:
            candidates.append(value)
    return min(candidates) if candidates else None


def first_visit_day():
    """The first day page views were counted at all. -> day or None.

    Separate from launch_day() on purpose. The gap between them is a
    period the app existed and was not counting visitors, and drawing
    that as zeroes would read as "nobody came" when it means "nobody
    was looking".
    """
    conn = _connect()
    row = conn.execute("SELECT MIN(day) FROM site_visits").fetchone()
    return row[0] if row and row[0] else None


def _span(days=None, since=None):
    """-> (start_day, number_of_days), whichever way the caller asked.

    `since` wins when given, so a dashboard can ask for "everything from
    launch" without knowing today's date or doing the arithmetic.
    """
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if since:
        try:
            start = _dt.date.fromisoformat(since)
        except (ValueError, TypeError):
            start = today
        # A future or malformed start would give a negative range.
        if start > today:
            start = today
        return start.isoformat(), (today - start).days + 1
    n = max(1, int(days or 30))
    return (today - _dt.timedelta(days=n - 1)).isoformat(), n


def visit_series(days=30, since=None):
    """-> [{day, views, visitors, counted}], oldest first.

    `counted` says whether the app was recording visits that day. A day
    before instrumentation existed has views 0 like a genuinely quiet
    day, and the two mean completely different things - so the flag
    travels with the row rather than being inferred later from a zero.
    """
    import datetime as _dt
    start, n = _span(days, since)
    conn = _connect()
    views = dict(conn.execute(
        "SELECT day, SUM(views) FROM site_visits WHERE day >= ? "
        "GROUP BY day", (start,)).fetchall())
    people = dict(conn.execute(
        "SELECT day, COUNT(*) FROM site_visitors WHERE day >= ? "
        "GROUP BY day", (start,)).fetchall())
    began = first_visit_day()
    out = []
    for offset in range(n):
        key = (_dt.date.fromisoformat(start)
               + _dt.timedelta(days=offset)).isoformat()
        out.append({"day": key,
                    "views": views.get(key, 0),
                    "visitors": people.get(key, 0),
                    "counted": bool(began and key >= began)})
    return out


def note_visitor(visitor_key):
    """Record that this browser has been seen, ever. Never raises.

    INSERT OR IGNORE, so the row keeps the FIRST time rather than the
    latest - which is what makes the table a count of visitors rather
    than a log of visits.
    """
    if not visitor_key:
        return
    try:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO visitors_seen (visitor_key, first_seen)"
                " VALUES (?, ?)",
                (str(visitor_key)[:200],
                 _dt_now().isoformat()))
    except Exception:                          # noqa: BLE001
        pass


def _dt_now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def visitors_all_time():
    """-> {total, registered, guests, first_seen}.

    Split because the two halves mean different things: a registered
    count is people who chose to make an account, and the guest count is
    browsers that never did.
    """
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM visitors_seen").fetchone()[0]
    guests = conn.execute("SELECT COUNT(*) FROM visitors_seen "
                          "WHERE visitor_key LIKE 'guest:%'").fetchone()[0]
    first = conn.execute("SELECT MIN(first_seen) "
                         "FROM visitors_seen").fetchone()[0]
    return {"total": total, "guests": guests,
            "registered": total - guests, "first_seen": first}


def visit_totals():
    conn = _connect()
    total = conn.execute("SELECT COALESCE(SUM(views), 0) "
                         "FROM site_visits").fetchone()[0]
    today = conn.execute("SELECT COALESCE(SUM(views), 0) FROM site_visits "
                         "WHERE day = ?", (_today(),)).fetchone()[0]
    today_people = conn.execute("SELECT COUNT(*) FROM site_visitors "
                                "WHERE day = ?", (_today(),)).fetchone()[0]
    # Distinct visitors cannot be summed across days - the same person
    # has a different hash each day, so a sum counts one regular reader
    # as thirty people. All-time uniques are not knowable by design, and
    # saying so is better than printing a number that means nothing.
    return {"views_total": total, "views_today": today,
            "visitors_today": today_people}


def top_pages(days=30, limit=8, since=None):
    start, _n = _span(days, since)
    return [{"path": r[0], "views": r[1]} for r in _connect().execute(
        "SELECT path, SUM(views) FROM site_visits WHERE day >= ? "
        "GROUP BY path ORDER BY 2 DESC LIMIT ?", (start, limit)).fetchall()]


def record_payment(txn_id, user_id, email, currency, gross, fee, earnings,
                   created):
    """Record one completed payment. Idempotent, and never raises.

    INSERT OR IGNORE rather than upsert: Paddle redelivers webhooks, and
    a completed transaction's totals do not change afterwards, so the
    first write is the true one and a retry has nothing to add.
    """
    try:
        conn = _connect()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO payments (txn_id, day, user_id, "
                "email, currency, gross, fee, earnings, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (txn_id, (created or "")[:10] or _today(), user_id or "",
                 email or "", currency or "USD", int(gross or 0),
                 int(fee or 0), int(earnings or 0), created or ""))
        return True
    except Exception:                          # noqa: BLE001
        return False


def payment_totals():
    """-> {currency: {gross, fee, earnings, count}} plus a payment count.

    Grouped by currency because Paddle settles in the customer's
    currency: adding a EUR payment to a USD one gives a number that is
    not an amount of money in any currency.
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT currency, COUNT(*), COALESCE(SUM(gross), 0), "
        "COALESCE(SUM(fee), 0), COALESCE(SUM(earnings), 0) "
        "FROM payments GROUP BY currency").fetchall()
    by_currency = {r[0]: {"count": r[1], "gross": r[2], "fee": r[3],
                          "earnings": r[4]} for r in rows}
    return {"by_currency": by_currency,
            "count": sum(v["count"] for v in by_currency.values())}


def payment_series(days=30, since=None):
    import datetime as _dt
    start, n = _span(days, since)
    have = {r[0]: (r[1], r[2]) for r in _connect().execute(
        "SELECT day, COUNT(*), COALESCE(SUM(earnings), 0) FROM payments "
        "WHERE day >= ? GROUP BY day", (start,)).fetchall()}
    out = []
    for offset in range(n):
        key = (_dt.date.fromisoformat(start)
               + _dt.timedelta(days=offset)).isoformat()
        count, earnings = have.get(key, (0, 0))
        out.append({"day": key, "payments": count, "earnings": earnings})
    return out


def recent_payments(limit=10):
    return [dict(r) for r in _connect().execute(
        "SELECT txn_id, day, email, currency, gross, fee, earnings "
        "FROM payments ORDER BY created DESC LIMIT ?", (limit,)).fetchall()]


def request_totals():
    """AI requests, from the counters the app already keeps."""
    conn = _connect()
    total = conn.execute("SELECT COALESCE(SUM(messages), 0), "
                         "COALESCE(SUM(credits), 0) FROM usage_log").fetchone()
    today = conn.execute("SELECT COALESCE(SUM(messages), 0) FROM usage_log "
                         "WHERE day = ?", (_today(),)).fetchone()[0]
    return {"messages_total": total[0], "credits_total": total[1],
            "messages_today": today}


def request_series(days=30, since=None):
    """Site-wide AI requests per day - usage_series() without the owner
    filter, which is the shape the dashboard needs and the per-account
    settings panel does not."""
    import datetime as _dt
    start, n = _span(days, since)
    have = {r[0]: (r[1], r[2]) for r in _connect().execute(
        "SELECT day, COALESCE(SUM(messages), 0), COALESCE(SUM(credits), 0) "
        "FROM usage_log WHERE day >= ? GROUP BY day", (start,)).fetchall()}
    out = []
    for offset in range(n):
        key = (_dt.date.fromisoformat(start)
               + _dt.timedelta(days=offset)).isoformat()
        messages, credits = have.get(key, (0, 0))
        out.append({"day": key, "messages": messages, "credits": credits})
    return out


# --------------------------------------------------------- notifications
NOTIFICATION_EVENTS = ("usage_limit", "product_update", "security_alert")
NOTIFICATION_CHANNELS = ("email", "in_app")

# Security warnings cannot be switched off. Somebody who has just had
# their password changed by an intruder is exactly who must be told, and
# a preference that silences that is a preference against the person's
# own interest.
NOTIFICATION_LOCKED = {("security_alert", "email")}


def load_notification_prefs(owner_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT event, channel, enabled FROM notification_prefs "
        "WHERE owner_id = ?", (owner_id,)).fetchall()
    saved = {(r["event"], r["channel"]): bool(r["enabled"]) for r in rows}
    out = []
    for event in NOTIFICATION_EVENTS:
        for channel in NOTIFICATION_CHANNELS:
            locked = (event, channel) in NOTIFICATION_LOCKED
            out.append({
                "event": event,
                "channel": channel,
                "enabled": True if locked else saved.get((event, channel), True),
                "locked": locked,
            })
    return out


def save_notification_pref(owner_id, event, channel, enabled):
    if (event, channel) in NOTIFICATION_LOCKED:
        return False
    if event not in NOTIFICATION_EVENTS or channel not in NOTIFICATION_CHANNELS:
        return False
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO notification_prefs "
            "(owner_id, event, channel, enabled) VALUES (?, ?, ?, ?)",
            (owner_id, event, channel, 1 if enabled else 0))
    return True


# ------------------------------------------------------ prompt templates
def list_templates(owner_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, name, body, created FROM prompt_templates "
        "WHERE owner_id = ? ORDER BY created DESC", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def save_template(owner_id, tid, name, body, created):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO prompt_templates "
            "(id, owner_id, name, body, created) VALUES (?, ?, ?, ?, ?)",
            (tid, owner_id, name, body, created))


def delete_template(owner_id, tid):
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM prompt_templates WHERE id = ? AND owner_id = ?",
            (tid, owner_id))
    return (cur.rowcount or 0) > 0


def load_settings(owner_id):
    """This owner's settings, with defaults filled in for a new account."""
    conn = _connect()
    row = conn.execute(
        # Every key in SETTINGS_DEFAULTS must appear here. The dict
        # comprehension below reads the row by those names, so a default
        # added without a matching column in this SELECT raises
        # IndexError on the next read - which is exactly what adding
        # avatar_url and bio did.
        "SELECT theme, language, timezone, default_model, temperature, "
        "       top_p, max_tokens, system_prompt, web_search, "
        "       tools_enabled, retention_days, avatar_url, bio, updated "
        "FROM user_settings WHERE owner_id = ?", (owner_id,)).fetchone()
    if not row:
        out = dict(SETTINGS_DEFAULTS)
        out["updated"] = ""
        return out
    # .keys() rather than row[k] straight: a missing column is then a
    # named, findable error instead of a bare IndexError from sqlite3.
    have = set(row.keys())
    missing = [k for k in SETTINGS_DEFAULTS if k not in have]
    if missing:
        raise RuntimeError(
            "load_settings: %s in SETTINGS_DEFAULTS but not in the SELECT "
            "above. Add the column to all three places." % ", ".join(missing))
    out = {k: row[k] for k in SETTINGS_DEFAULTS}
    out["web_search"] = bool(row["web_search"])
    out["tools_enabled"] = bool(row["tools_enabled"])
    out["updated"] = row["updated"]
    return out


def save_settings(owner_id, changes, updated):
    """Sparse update: only the given keys are written.

    UPSERT rather than read-modify-write, so two tabs saving different
    fields at the same moment cannot have one silently overwrite the
    other with the values it loaded minutes ago.
    """
    fields = [k for k in changes if k in SETTINGS_DEFAULTS]
    if not fields:
        return load_settings(owner_id)
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO user_settings (owner_id) VALUES (?) "
            "ON CONFLICT(owner_id) DO NOTHING", (owner_id,))
        assignments = ", ".join("%s = ?" % f for f in fields)
        conn.execute(
            "UPDATE user_settings SET %s, updated = ? WHERE owner_id = ?"
            % assignments,
            [changes[f] for f in fields] + [updated, owner_id])
    return load_settings(owner_id)


# ------------------------------------------------------------ sessions
def create_session(sid, owner_id, now, ip="", user_agent=""):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(id, owner_id, created, last_seen, ip, user_agent, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (sid, owner_id, now, now, ip[:64], (user_agent or "")[:300]))
    return sid


def session_is_live(sid, owner_id):
    """False for a revoked, missing, or reassigned session.

    owner_id is checked too: a session id that survived into another
    account's cookie must not authenticate anybody.
    """
    conn = _connect()
    row = conn.execute(
        "SELECT revoked_at FROM sessions WHERE id = ? AND owner_id = ?",
        (sid, owner_id)).fetchone()
    return bool(row) and not row["revoked_at"]


def touch_session(sid, now):
    conn = _connect()
    with conn:
        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?",
                     (now, sid))


def list_sessions(owner_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, created, last_seen, ip, user_agent FROM sessions "
        "WHERE owner_id = ? AND revoked_at IS NULL "
        "ORDER BY last_seen DESC", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_session(owner_id, sid, now):
    conn = _connect()
    with conn:
        cur = conn.execute(
            "UPDATE sessions SET revoked_at = ? "
            "WHERE id = ? AND owner_id = ? AND revoked_at IS NULL",
            (now, sid, owner_id))
    return (cur.rowcount or 0) > 0


def revoke_other_sessions(owner_id, keep_sid, now):
    conn = _connect()
    with conn:
        cur = conn.execute(
            "UPDATE sessions SET revoked_at = ? "
            "WHERE owner_id = ? AND id != ? AND revoked_at IS NULL",
            (now, owner_id, keep_sid))
    return cur.rowcount or 0


# ------------------------------------------------------ provider keys
def save_provider_key(owner_id, provider, ciphertext, nonce, last_four, now):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO provider_keys "
            "(owner_id, provider, ciphertext, nonce, last_four, created, "
            " last_used) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (owner_id, provider, ciphertext, nonce, last_four, now))


def list_provider_keys(owner_id):
    """Metadata only. The ciphertext never leaves this module by accident
    - a caller that needs the secret asks for it by name."""
    conn = _connect()
    rows = conn.execute(
        "SELECT provider, last_four, created, last_used FROM provider_keys "
        "WHERE owner_id = ? ORDER BY provider", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def get_provider_key_row(owner_id, provider):
    conn = _connect()
    return conn.execute(
        "SELECT ciphertext, nonce FROM provider_keys "
        "WHERE owner_id = ? AND provider = ?", (owner_id, provider)).fetchone()


def touch_provider_key(owner_id, provider, now):
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE provider_keys SET last_used = ? "
            "WHERE owner_id = ? AND provider = ?", (now, owner_id, provider))


def delete_provider_key(owner_id, provider):
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM provider_keys WHERE owner_id = ? AND provider = ?",
            (owner_id, provider))
    return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------- MFA
def save_mfa_enrollment(owner_id, ct, nonce, now):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO mfa_enrollment "
            "(owner_id, secret_ct, secret_nonce, confirmed, created) "
            "VALUES (?, ?, ?, 0, ?)", (owner_id, ct, nonce, now))


def get_mfa_enrollment(owner_id):
    conn = _connect()
    return conn.execute(
        "SELECT secret_ct, secret_nonce, confirmed, created "
        "FROM mfa_enrollment WHERE owner_id = ?", (owner_id,)).fetchone()


def confirm_mfa(owner_id):
    conn = _connect()
    with conn:
        conn.execute("UPDATE mfa_enrollment SET confirmed = 1 "
                     "WHERE owner_id = ?", (owner_id,))


def disable_mfa(owner_id):
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM mfa_enrollment WHERE owner_id = ?",
                     (owner_id,))
        conn.execute("DELETE FROM mfa_backup_codes WHERE owner_id = ?",
                     (owner_id,))


def save_backup_codes(owner_id, hashes):
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM mfa_backup_codes WHERE owner_id = ?",
                     (owner_id,))
        conn.executemany(
            "INSERT INTO mfa_backup_codes (owner_id, code_hash, used_at) "
            "VALUES (?, ?, NULL)", [(owner_id, h) for h in hashes])


def unused_backup_codes(owner_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT code_hash FROM mfa_backup_codes "
        "WHERE owner_id = ? AND used_at IS NULL", (owner_id,)).fetchall()
    return [r["code_hash"] for r in rows]


def spend_backup_code(owner_id, code_hash, now):
    conn = _connect()
    with conn:
        cur = conn.execute(
            "UPDATE mfa_backup_codes SET used_at = ? "
            "WHERE owner_id = ? AND code_hash = ? AND used_at IS NULL",
            (now, owner_id, code_hash))
    return (cur.rowcount or 0) > 0


# ------------------------------------------------------------ history
def delete_threads_older_than(owner_id, cutoff_iso):
    """Retention policy. Returns how many threads went."""
    conn = _connect()
    with conn:
        cur = conn.execute(
            "DELETE FROM threads WHERE owner_id = ? AND updated < ?",
            (owner_id, cutoff_iso))
    return cur.rowcount or 0


def save_password_reset(email, code, expires_at, sent_at):
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO password_resets "
            "(email, code, expires_at, attempts, sent_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (email, code, expires_at, sent_at))


def get_password_reset(email):
    conn = _connect()
    return conn.execute(
        "SELECT email, code, expires_at, attempts, sent_at "
        "FROM password_resets WHERE email = ?", (email,)).fetchone()


def bump_password_reset_attempts(email):
    """-> the new attempt count, after this one."""
    conn = _connect()
    with conn:
        conn.execute(
            "UPDATE password_resets SET attempts = attempts + 1 "
            "WHERE email = ?", (email,))
    row = get_password_reset(email)
    return int(row["attempts"]) if row else 0


def delete_password_reset(email):
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM password_resets WHERE email = ?", (email,))


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
