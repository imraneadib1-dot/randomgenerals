"""Connected apps: sign-in, stored tokens, and tool calls for a chat turn.

The glue between mcp_client.py (the protocol and OAuth), db.py (where a
connection is kept) and app.py (routes and the chat turn). Nothing here
knows about Flask: every function takes the owner id it acts for, and a
connection is only ever loaded by (owner, id), so one account can never
reach another's apps.

TOKENS

Access and refresh tokens are stored together as one JSON secret,
encrypted with keystore (AES-GCM under SETTINGS_ENCRYPTION_KEY) when the
server has a key, and as plain JSON otherwise - the same trade the
paste-a-link connectors already make, recorded per row so turning
encryption on later does not strand existing connections. An expired
access token is refreshed before use; a refused one is refreshed once
and, if that fails too, the connection is marked "needs sign-in" so the
Connectors page can say so.
"""
import datetime
import json
import re
import secrets
import time
import uuid

import db
import extensions
import keystore
import mcp_client


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ConnectError(Exception):
    """A sentence the person can act on."""


# ---------------------------------------------------------------- secrets
def _seal(owner_id, tokens):
    raw = json.dumps(tokens or {})
    if keystore.available():
        ct, nonce = keystore.encrypt(raw, owner_id, "app-connection")
        return ct, nonce, True
    return raw.encode("utf-8"), None, False


def _open(owner_id, item):
    blob = item.get("secret")
    if not blob:
        return {}
    try:
        if item.get("secret_enc"):
            return json.loads(keystore.decrypt(blob, item.get("nonce"), owner_id,
                                               "app-connection"))
        return json.loads(bytes(blob).decode("utf-8"))
    except Exception:                       # noqa: BLE001 - unreadable = signed out
        return {}


def _save(owner_id, app, url, auth, tokens, tools, status="ok"):
    ct, nonce, enc = _seal(owner_id, tokens)
    existing = next((c for c in db.connections_for(owner_id) if c["app_id"] == app["id"]), None)
    item = {"id": existing["id"] if existing else uuid.uuid4().hex[:16],
            "app_id": app["id"], "title": app["title"], "url": url,
            "auth": auth or {}, "secret": ct, "nonce": nonce, "secret_enc": enc,
            "tools": tools, "status": status}
    db.connection_save(owner_id, item, _now())
    mcp_client.forget_session(item["id"])
    return public(dict(item, created=_now(), updated=_now()))


def public(item):
    """What the browser may see of a connection."""
    return {"id": item["id"], "app_id": item["app_id"], "title": item["title"],
            "status": item.get("status", "ok"),
            "tools": [{"name": t["name"], "title": t.get("title") or "",
                       "description": (t.get("description") or "")[:200]}
                      for t in item.get("tools") or []],
            "updated": item.get("updated", "")}


# ---------------------------------------------------------------- connect
def begin(owner_id, app_id, redirect_uri):
    """Start connecting. -> ("connected", public item) when the app needs
    no sign-in, or ("redirect", url) to send the browser to."""
    app = extensions.APPS_BY_ID.get(app_id)
    if not app or app not in extensions.available_apps():
        raise ConnectError("That app is not available to connect.")
    try:
        auth = mcp_client.discover_auth(app["url"])
    except mcp_client.MCPError as e:
        raise ConnectError("%s could not be reached right now (%s). Try again in a minute."
                           % (app["title"], e)) from e
    if auth is None:
        try:
            tools = mcp_client.Session(app["url"]).list_tools()
        except mcp_client.MCPError as e:
            raise ConnectError("%s did not answer: %s" % (app["title"], e)) from e
        return "connected", _save(owner_id, app, app["url"], {}, {}, tools)

    if app["auth"] == "app":
        client_id, client_secret = extensions.app_client(app_id)
        if not client_id:
            raise ConnectError("%s is not set up on this server yet." % app["title"])
    else:
        client_id, client_secret = db.app_client_get(auth["issuer"], redirect_uri)
        if not client_id:
            try:
                client_id, client_secret = mcp_client.register_client(auth, redirect_uri)
            except mcp_client.MCPError as e:
                raise ConnectError("%s: %s" % (app["title"], e)) from e
            db.app_client_save(auth["issuer"], redirect_uri, client_id, client_secret, _now())
    auth = dict(auth, client_secret=client_secret or "")
    verifier, challenge = mcp_client.pkce_pair()
    state = secrets.token_urlsafe(24)
    db.signin_save(state, owner_id, app_id, verifier, auth, client_id, redirect_uri, time.time())
    return "redirect", mcp_client.authorize_url(auth, client_id, redirect_uri, state, challenge)


def finish(owner_id, state, code):
    """Complete a sign-in the app has sent back. -> public item."""
    pending = db.signin_take(state or "", time.time())
    if not pending or pending["owner_id"] != owner_id:
        # A state from another session, reused, or expired. Never
        # completes a connection for an account that did not start it.
        raise ConnectError("That sign-in link expired. Press Connect again.")
    app = extensions.APPS_BY_ID.get(pending["app_id"])
    if not app:
        raise ConnectError("That app is no longer available.")
    auth = pending["auth"]
    secret = auth.pop("client_secret", "")
    try:
        tokens = mcp_client.exchange_code(auth, pending["client_id"], secret, code,
                                          pending["redirect"], pending["verifier"])
    except mcp_client.MCPError as e:
        if app["auth"] != "app":
            # A registration the app has since forgotten fails here; the
            # next attempt registers afresh.
            db.app_client_forget(auth["issuer"], pending["redirect"])
        raise ConnectError("%s did not accept the sign-in. Press Connect to try again."
                           % app["title"]) from e
    try:
        tools = mcp_client.Session(app["url"], tokens["access_token"]).list_tools()
    except mcp_client.MCPError as e:
        raise ConnectError("Signed in to %s, but it did not list its tools: %s"
                           % (app["title"], e)) from e
    stored_auth = dict(auth, client_id=pending["client_id"], client_secret=secret)
    return _save(owner_id, app, app["url"], stored_auth, tokens, tools)


# ---------------------------------------------------------------- tokens
def _token(owner_id, item, force_refresh=False):
    tokens = _open(owner_id, item)
    auth = item.get("auth") or {}
    if not auth.get("token_endpoint"):
        return None                                    # an open app
    expired = tokens.get("expires_at") and tokens["expires_at"] < time.time()
    if (force_refresh or expired or not tokens.get("access_token")) and tokens.get("refresh_token"):
        try:
            fresh = mcp_client.refresh(auth, auth.get("client_id"), auth.get("client_secret"),
                                       tokens["refresh_token"])
        except mcp_client.MCPError:
            db.connection_update(owner_id, item["id"], _now(), status="reauth")
            raise
        fresh["refresh_token"] = fresh.get("refresh_token") or tokens["refresh_token"]
        ct, nonce, enc = _seal(owner_id, fresh)
        db.connection_update(owner_id, item["id"], _now(), secret=ct, nonce=nonce,
                             secret_enc=1 if enc else 0, status="ok")
        return fresh["access_token"]
    if force_refresh:
        db.connection_update(owner_id, item["id"], _now(), status="reauth")
        raise mcp_client.NeedsAuth("signed out")
    return tokens.get("access_token")


def _with_session(owner_id, item, fn):
    """Run fn(session), refreshing the token once if the app refuses it."""
    for attempt in (0, 1):
        try:
            token = _token(owner_id, item, force_refresh=bool(attempt))
        except mcp_client.MCPError:
            raise ConnectError("%s needs you to sign in again - open Customize > "
                               "Connectors and press Reconnect." % item["title"])
        s = mcp_client.session_for(item["id"], item["url"], token)
        try:
            return fn(s)
        except mcp_client.NeedsAuth:
            mcp_client.forget_session(item["id"])
            if attempt or not (item.get("auth") or {}).get("token_endpoint"):
                db.connection_update(owner_id, item["id"], _now(), status="reauth")
                raise ConnectError("%s needs you to sign in again - open Customize > "
                                   "Connectors and press Reconnect." % item["title"])


def refresh_tools(owner_id, conn_id):
    item = db.connection_get(owner_id, conn_id, with_secret=True)
    if not item:
        raise ConnectError("No such connection.")
    try:
        tools = _with_session(owner_id, item, lambda s: s.list_tools())
    except mcp_client.MCPError as e:
        raise ConnectError("%s did not answer: %s" % (item["title"], e)) from e
    db.connection_update(owner_id, conn_id, _now(), tools_json=json.dumps(tools), status="ok")
    item["tools"] = tools
    item["status"] = "ok"
    return public(item)


def call(owner_id, conn_id, tool, args):
    """One tool call. Always returns text for the model; never raises."""
    item = db.connection_get(owner_id, conn_id, with_secret=True)
    if not item:
        return "That app is no longer connected."
    try:
        return _with_session(owner_id, item, lambda s: s.call_tool(tool, args))
    except ConnectError as e:
        return str(e)
    except mcp_client.MCPError as e:
        return "%s could not run that: %s" % (item["title"], e)


# ---------------------------------------------------------------- a chat turn
# ONLY THE TOOLS THIS MESSAGE NEEDS, AND ONLY WHAT THE MODEL MUST KNOW.
#
# Notion alone lists 40 tools and 135,882 characters of descriptions and
# schemas - about 34,000 tokens, four times Groq's whole per-request
# allowance on the free tier. Sending all of them meant every request
# naming Notion was refused, the tool loop quietly gave up, and the
# answer came back as if nothing were connected.
#
# So each turn ranks an app's tools against what was asked and offers
# the best few, each compacted (descriptions shortened, deep schema
# detail collapsed - the server still validates the real arguments),
# within a character budget the caller sets for the model in use.
TOOLS_PER_APP = 6
DEFAULT_TURN_BUDGET = 11000         # characters of tool JSON, ~2,800 tokens
DESC_CHARS = 240
PROP_DESC_CHARS = 80
SCHEMA_DEPTH = 4

_STOP = set("the a an and or to of in on for with my me our your it this that is are be "
            "can could please would should what which who how do does about from".split())
# Reading comes before writing when nothing in the message says which:
# a vague "check my Notion" should search, not create.
_READ_WORDS = ("search", "fetch", "get", "list", "query", "find", "read", "retrieve")


# How people say it -> how tools name it.
_SYNONYMS = {
    "add": "create", "new": "create", "make": "create", "write": "create",
    "post": "create", "save": "create",
    "edit": "update", "change": "update", "rename": "update", "modify": "update", "set": "update",
    "move": "move", "mark": "update", "complete": "update", "close": "update", "assign": "update",
    "remove": "delete", "erase": "delete", "cancel": "delete", "trash": "delete",
    "show": "get", "see": "get", "view": "get", "look": "search", "check": "search",
    "find": "search", "lookup": "search", "where": "search", "which": "search", "any": "search",
    "pages": "page", "notes": "page", "note": "page", "doc": "page", "docs": "page",
    "issues": "issue", "tickets": "issue", "ticket": "issue", "bug": "issue", "bugs": "issue",
    "tasks": "task", "todo": "task", "todos": "task", "comments": "comment", "reply": "comment",
    "tables": ("database", "query"), "table": ("database", "query"), "databases": ("database", "query"),
    "rows": ("database", "query"), "records": ("database", "query"), "entries": ("database", "query"),
    "filter": "query", "sort": "query", "count": "query",
}


def _words(text, expand=False):
    """Content words. Plurals are folded both ways round; synonyms are
    added only for what the PERSON wrote (expand=True) - expanding tool
    names too made "delete databases" match a question about rows."""
    out = {w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower()) if w not in _STOP}
    out |= {w[:-1] for w in out if w.endswith("s") and len(w) > 4}
    if expand:
        for w in list(out):
            syn = _SYNONYMS.get(w)
            if syn:
                out |= set(syn) if isinstance(syn, tuple) else {syn}
    return out


def compact_schema(schema, depth=0):
    """A JSON schema with the shape kept and the prose cut."""
    if not isinstance(schema, dict):
        return schema
    if depth >= SCHEMA_DEPTH:
        kept = {k: schema[k] for k in ("type", "enum") if k in schema}
        return kept or {"type": "object"}
    out = {}
    for k, v in schema.items():
        if k in ("examples", "example", "$schema", "$comment", "title", "default", "$id",
                 "additionalProperties", "markdownDescription"):
            continue
        if k == "description":
            if isinstance(v, str) and v:
                out[k] = v if len(v) <= PROP_DESC_CHARS else v[:PROP_DESC_CHARS - 1] + "…"
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: compact_schema(pv, depth + 1) for pk, pv in v.items()}
        elif k in ("items", "additionalItems", "not") and isinstance(v, dict):
            out[k] = compact_schema(v, depth + 1)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [compact_schema(x, depth + 1) for x in v[:6]]
        elif k in ("$defs", "definitions"):
            continue                       # refs into them become loose objects
        elif k == "$ref":
            out.setdefault("type", "object")
        else:
            out[k] = v
    return out


def _rank(tools, text):
    asked = _words(text, expand=True)

    def score(item):
        i, t = item
        name_words = _words(t["name"].replace("-", " ").replace("_", " ") + " " + (t.get("title") or ""))
        desc_words = _words((t.get("description") or "")[:300])
        s = 3 * len(asked & name_words) + len(asked & desc_words)
        # Finding a thing and opening it are how almost every request
        # starts, so an app's search and fetch tools are always near
        # the front.
        if name_words & {"search", "fetch"}:
            s += 3
        elif any(w in name_words for w in _READ_WORDS):
            s += 1
        # Nothing destructive unless it was asked for.
        if name_words & {"delete", "remove", "archive", "trash"} and not asked & {"delete", "remove", "archive"}:
            s -= 6
        if t.get("read_only"):
            s += 0.5
        return (-s, i)                     # best first; the app's own order breaks ties
    return [t for _, t in sorted(enumerate(tools), key=score)]


def _compact_tool(t):
    desc = t.get("description") or t.get("title") or t["name"]
    if len(desc) > DESC_CHARS:
        desc = desc[:DESC_CHARS - 1] + "…"
    return dict(t, description=desc, input_schema=compact_schema(t.get("input_schema") or {}))


def tools_for_turn(owner_id, text, budget=DEFAULT_TURN_BUDGET):
    """-> (tool specs, {tool name: route}) for the apps this conversation
    is about. An app's tools are offered only when the recent messages
    mention it (by name, by one of its words, or as @app-id), so
    connecting many apps does not make every request enormous - and then
    only its few tools that fit what was asked (see above)."""
    specs, routes = [], {}
    try:
        conns = db.connections_for(owner_id)
    except Exception:                       # noqa: BLE001
        return [], {}
    used = 0
    for c in conns:
        app = extensions.APPS_BY_ID.get(c["app_id"])
        if not app or c.get("status") != "ok" or not extensions.app_mentioned(app, text):
            continue
        taken = 0
        for t in _rank(c.get("tools") or [], text):
            if taken >= TOOLS_PER_APP:
                break
            spec = mcp_client.tool_spec(app["id"], app["title"], _compact_tool(t))
            size = len(json.dumps(spec))
            if used + size > budget:
                continue                   # a smaller one further down may still fit
            name = spec["function"]["name"]
            if name in routes:
                continue
            specs.append(spec)
            used += size
            taken += 1
            routes[name] = {"kind": "mcp", "owner_id": owner_id, "conn_id": c["id"],
                            "tool": t["name"], "app_title": app["title"],
                            "tool_title": t.get("title") or t["name"]}
    return specs, routes
