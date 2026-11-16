"""Remote MCP servers: the protocol, and the one-click sign-in.

WHAT THIS IS

Most apps people use - Notion, Linear, Stripe, Jira, Figma - now run an
MCP server: one web address that lists what the app can do as tools and
runs them when asked. This module speaks that protocol (the Streamable
HTTP transport: JSON-RPC over POST, answered with JSON or a short SSE
stream) so a connected app's tools can be offered to the model like the
built-in ones in tools.py.

ONE CLICK, WITHOUT A DEVELOPER ACCOUNT PER APP

The sign-in is the MCP authorization flow, which is OAuth 2.1 with three
discovery steps in front of it:

  1. Ask the server for anything. It answers 401 and names its
     "protected resource metadata".
  2. That document names the authorization server; ITS metadata gives
     the authorize, token and - the part that makes this one click -
     registration endpoints.
  3. Dynamic client registration: this app registers itself with that
     app on the spot (once per app, cached in the database), so nobody
     has to create a developer account with each service first.

Then the ordinary authorization-code flow with PKCE: the person is sent
to the app's own sign-in page, approves, and comes back with a code that
is exchanged for tokens. The tokens never reach the browser.

Apps whose sign-in does not allow registration (GitHub, Asana, HubSpot,
Box when this was written) can still be one click for users once the
site owner registers the app with that service and sets its client id and
secret in the environment - see connector_catalog.

EVERY ADDRESS IS CHECKED

Each URL - the MCP endpoint, every metadata document, the token and
registration endpoints - goes through connectors._resolve_safe before it
is fetched, and redirects are not followed. Metadata is supplied by the
remote server, so without this a hostile server could name the cloud
metadata service as its "token endpoint" and have this machine POST to
it. See connectors.py for why that matters on this VM.

Pure functions and a small session cache: no Flask, no database. app.py
owns where tokens are stored and who may use them.
"""
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse

import requests

import connectors

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "RandomGenerals", "version": "1.0"}
CLIENT_NAME = "RandomGenerals"
TIMEOUT = 25
# What one tool result may add to the conversation. It is fed back into
# the next model call, so an uncapped result is a context overflow and a
# bill. Enough for a page of search results or a document excerpt.
MAX_RESULT_CHARS = 8000
MAX_BYTES = 2_000_000
# A server listing hundreds of tools would crowd everything else out of
# the prompt. The first ones in its own order are kept.
MAX_TOOLS = 40


class MCPError(Exception):
    """The server could not be reached or said something unusable."""


class NeedsAuth(MCPError):
    """401: sign in (again). `www` is the WWW-Authenticate header."""

    def __init__(self, message, www=""):
        super().__init__(message)
        self.www = www


def _safe(url):
    try:
        connectors._resolve_safe(url)
    except connectors.Unsafe as e:
        raise MCPError(str(e)) from e


# ----------------------------------------------------------------------
# The protocol
# ----------------------------------------------------------------------
def _read_response(r, want_id):
    """A JSON-RPC response from a JSON body or an SSE stream."""
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in ctype:
        r.encoding = "utf-8"
        data_lines = []
        size = 0
        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            size += len(raw)
            if size > MAX_BYTES:
                raise MCPError("The app sent more than this connection accepts.")
            if raw.startswith("data:"):
                data_lines.append(raw[5:].lstrip())
                continue
            if raw == "" and data_lines:
                try:
                    msg = json.loads("\n".join(data_lines))
                except ValueError:
                    msg = None
                data_lines = []
                if isinstance(msg, dict) and msg.get("id") == want_id and (
                        "result" in msg or "error" in msg):
                    return msg
        if data_lines:
            try:
                msg = json.loads("\n".join(data_lines))
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
            except ValueError:
                pass
        raise MCPError("The app ended its reply without an answer.")
    body = r.content[:MAX_BYTES]
    try:
        msg = json.loads(body or b"null")
    except ValueError as e:
        raise MCPError("The app's reply was not JSON.") from e
    if isinstance(msg, list):
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == want_id), None)
    if not isinstance(msg, dict):
        raise MCPError("The app's reply was empty.")
    return msg


class Session:
    """One initialised conversation with one MCP server."""

    def __init__(self, url, token=None):
        self.url = url
        self.token = token
        self.session_id = None
        self.protocol = PROTOCOL_VERSION
        self._next_id = 0
        self.ready = False
        self.server_name = ""
        self.instructions = ""

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": "RandomGenerals-MCP/1.0"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.ready:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _send(self, method, params=None, notify=False):
        _safe(self.url)
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._next_id += 1
            body["id"] = self._next_id
        try:
            r = requests.post(self.url, json=body, headers=self._headers(),
                              timeout=TIMEOUT, allow_redirects=False,
                              stream=True)
        except requests.exceptions.RequestException as e:
            raise MCPError("Could not reach the app: %s" % type(e).__name__) from e
        with r:
            if r.status_code == 401:
                raise NeedsAuth("The app needs you to sign in again.",
                                r.headers.get("WWW-Authenticate", ""))
            if r.status_code == 404 and self.session_id:
                # The server forgot this session (a restart, a timeout).
                self.session_id = None
                self.ready = False
                raise _SessionExpired()
            if notify:
                return None
            if r.status_code in (301, 302, 303, 307, 308):
                raise MCPError("The app redirected the request, which is not followed.")
            if r.status_code >= 400:
                raise MCPError("The app answered with error %d." % r.status_code)
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            msg = _read_response(r, body["id"])
        if msg.get("error"):
            err = msg["error"]
            raise MCPError("The app refused: %s" % (
                (err.get("message") if isinstance(err, dict) else err) or "unknown error"))
        return msg.get("result") or {}

    def initialize(self):
        result = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_name = ((result.get("serverInfo") or {}).get("name") or "")
        self.instructions = (result.get("instructions") or "")[:2000]
        self.ready = True
        try:
            self._send("notifications/initialized", notify=True)
        except _SessionExpired:
            pass
        return result

    def request(self, method, params=None):
        if not self.ready:
            self.initialize()
        try:
            return self._send(method, params)
        except _SessionExpired:
            self.initialize()
            return self._send(method, params)

    def list_tools(self):
        tools, cursor = [], None
        for _ in range(10):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in (result.get("tools") or []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return [_clean_tool(t) for t in tools[:MAX_TOOLS] if t.get("name")]

    def call_tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return result_text(result)


class _SessionExpired(MCPError):
    pass


def _clean_tool(t):
    schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return {"name": str(t["name"])[:120],
            "title": str(t.get("title") or (t.get("annotations") or {}).get("title") or "")[:120],
            "description": str(t.get("description") or "")[:1000],
            "input_schema": schema,
            "read_only": bool((t.get("annotations") or {}).get("readOnlyHint"))}


def result_text(result):
    """A tools/call result as text the model can read."""
    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(res.get("text") or "[resource %s]" % res.get("uri", ""))
        elif kind == "resource_link":
            parts.append("[%s](%s)" % (block.get("name") or "link", block.get("uri", "")))
        elif kind in ("image", "audio"):
            parts.append("[%s returned - not shown]" % kind)
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"])[:MAX_RESULT_CHARS])
    text = "\n".join(p for p in parts if p).strip() or "(the tool returned nothing)"
    if result.get("isError"):
        text = "The tool reported an error: " + text
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n…[cut - the result was longer than this]"
    return text


# Sessions are reused between tool calls of the same connection for a
# few minutes: initialising costs two round trips, and a tool loop can
# make several calls in one reply.
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 300


def session_for(key, url, token):
    now = time.time()
    with _SESSIONS_LOCK:
        hit = _SESSIONS.get(key)
        if hit and hit[1] > now and hit[0].token == token and hit[0].url == url:
            return hit[0]
        s = Session(url, token)
        _SESSIONS[key] = (s, now + SESSION_TTL)
        for k in [k for k, v in _SESSIONS.items() if v[1] <= now]:
            _SESSIONS.pop(k, None)
        return s


def forget_session(key):
    with _SESSIONS_LOCK:
        _SESSIONS.pop(key, None)


# ----------------------------------------------------------------------
# One-click sign-in (MCP authorization: OAuth 2.1 + discovery + DCR)
# ----------------------------------------------------------------------
def _get_json(url):
    _safe(url)
    try:
        r = requests.get(url, timeout=TIMEOUT, allow_redirects=False,
                         headers={"Accept": "application/json",
                                  "User-Agent": "RandomGenerals-MCP/1.0"})
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _well_known(base, name):
    """RFC 8414 / 9728 locations: path-inserted first, then root."""
    p = urllib.parse.urlsplit(base)
    root = "%s://%s" % (p.scheme, p.netloc)
    path = p.path.rstrip("/")
    urls = []
    if path:
        urls.append("%s/.well-known/%s%s" % (root, name, path))
    urls.append("%s/.well-known/%s" % (root, name))
    if path and name == "openid-configuration":
        urls.append("%s%s/.well-known/%s" % (root, path, name))
    return urls


def discover_auth(url):
    """-> None when the server needs no sign-in, else a dict with the
    authorization server's endpoints. Raises MCPError when it needs a
    sign-in this app cannot perform."""
    s = Session(url)
    try:
        s.initialize()
        return None                      # open: answered without a token
    except NeedsAuth as e:
        www = e.www
    m = re.search(r'resource_metadata="([^"]+)"', www or "")
    prm = None
    if m:
        prm = _get_json(urllib.parse.urljoin(url, m.group(1)))
    if prm is None:
        for candidate in _well_known(url, "oauth-protected-resource"):
            prm = _get_json(candidate)
            if prm:
                break
    p = urllib.parse.urlsplit(url)
    issuer = ((prm or {}).get("authorization_servers") or ["%s://%s" % (p.scheme, p.netloc)])[0]
    asm = None
    for candidate in (_well_known(issuer, "oauth-authorization-server")
                      + _well_known(issuer, "openid-configuration")):
        asm = _get_json(candidate)
        if asm and asm.get("authorization_endpoint") and asm.get("token_endpoint"):
            break
        asm = None
    if not asm:
        raise MCPError("This app's sign-in could not be found.")
    scope_m = re.search(r'scope="([^"]+)"', www or "")
    scopes = (scope_m.group(1) if scope_m else
              " ".join((prm or {}).get("scopes_supported") or []))
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if asm.get(key):
            _safe(asm[key])
    return {
        "issuer": issuer,
        "authorization_endpoint": asm["authorization_endpoint"],
        "token_endpoint": asm["token_endpoint"],
        "registration_endpoint": asm.get("registration_endpoint") or "",
        "scope": scopes.strip(),
        "resource": (prm or {}).get("resource") or url,
        "pkce": asm.get("code_challenge_methods_supported") or ["S256"],
        "auth_methods": asm.get("token_endpoint_auth_methods_supported") or [],
    }


def register_client(auth, redirect_uri):
    """Dynamic client registration (RFC 7591). -> (client_id, secret)."""
    if not auth.get("registration_endpoint"):
        raise MCPError("This app does not allow one-click sign-in yet.")
    _safe(auth["registration_endpoint"])
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_uri": redirect_uri.split("/api/")[0],
    }
    if auth.get("scope"):
        body["scope"] = auth["scope"]
    try:
        r = requests.post(auth["registration_endpoint"], json=body, timeout=TIMEOUT,
                          allow_redirects=False,
                          headers={"Accept": "application/json",
                                   "User-Agent": "RandomGenerals-MCP/1.0"})
    except requests.exceptions.RequestException as e:
        raise MCPError("Could not register with the app: %s" % type(e).__name__) from e
    if r.status_code not in (200, 201):
        raise MCPError("The app refused to register RandomGenerals (%d)." % r.status_code)
    try:
        data = r.json()
    except ValueError as e:
        raise MCPError("The app's registration reply was not JSON.") from e
    if not data.get("client_id"):
        raise MCPError("The app did not issue a client id.")
    return data["client_id"], data.get("client_secret") or ""


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(auth, client_id, redirect_uri, state, challenge):
    q = {"response_type": "code", "client_id": client_id,
         "redirect_uri": redirect_uri, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256",
         "resource": auth["resource"]}
    if auth.get("scope"):
        q["scope"] = auth["scope"]
    sep = "&" if "?" in auth["authorization_endpoint"] else "?"
    return auth["authorization_endpoint"] + sep + urllib.parse.urlencode(q)


def _token_request(auth, client_id, client_secret, form):
    _safe(auth["token_endpoint"])
    form = dict(form, client_id=client_id)
    if auth.get("resource"):
        form["resource"] = auth["resource"]
    basic = None
    if client_secret:
        if "client_secret_post" in (auth.get("auth_methods") or []) and \
                "client_secret_basic" not in (auth.get("auth_methods") or []):
            form["client_secret"] = client_secret
        else:
            basic = (client_id, client_secret)
    try:
        r = requests.post(auth["token_endpoint"], data=form, auth=basic,
                          timeout=TIMEOUT, allow_redirects=False,
                          headers={"Accept": "application/json",
                                   "User-Agent": "RandomGenerals-MCP/1.0"})
    except requests.exceptions.RequestException as e:
        raise MCPError("Could not reach the app's sign-in: %s" % type(e).__name__) from e
    try:
        data = r.json()
    except ValueError:
        data = dict(urllib.parse.parse_qsl(r.text or ""))
    if r.status_code != 200 or not data.get("access_token"):
        raise NeedsAuth("The app did not accept the sign-in (%s)."
                        % (data.get("error") or r.status_code))
    expires = data.get("expires_in")
    try:
        expires_at = time.time() + float(expires) - 60 if expires else 0
    except (TypeError, ValueError):
        expires_at = 0
    return {"access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or "",
            "expires_at": expires_at}


def exchange_code(auth, client_id, client_secret, code, redirect_uri, verifier):
    return _token_request(auth, client_id, client_secret, {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier})


def refresh(auth, client_id, client_secret, refresh_token):
    return _token_request(auth, client_id, client_secret, {
        "grant_type": "refresh_token", "refresh_token": refresh_token})


# ----------------------------------------------------------------------
# Tool names the model sees
# ----------------------------------------------------------------------
def tool_name(app_id, name):
    """mcp__<app>__<tool>, within the 64-character limit providers set."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    base = "mcp__%s__" % re.sub(r"[^A-Za-z0-9]", "", app_id)[:16]
    full = base + clean
    if len(full) > 64:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
        full = base + clean[:64 - len(base) - 7] + "_" + digest
    return full


def tool_spec(app_id, app_title, tool):
    desc = tool.get("description") or tool.get("title") or tool["name"]
    return {"type": "function", "function": {
        "name": tool_name(app_id, tool["name"]),
        "description": ("[%s] %s" % (app_title, desc))[:1024],
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }}
