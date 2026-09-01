"""Paste a link, and the model can use whatever is on the other end.

WHAT "CONNECT" MEANS HERE

Given a URL this module works out what it is pointing at and turns it
into something the model can call mid-answer:

  * An OpenAPI/Swagger document - fetched, parsed, and every operation
    in it becomes callable. This is the real "it connected to the app"
    case: paste https://example.com/openapi.json and the model can now
    list, create and search whatever that service exposes.

  * A bare domain - the well-known spec locations are tried before
    giving up, because most people paste the site, not the spec.

  * Anything else - registered as a plain readable source. The model can
    GET it and read what comes back. Less capable, always works.

THE PART THAT MATTERS MORE THAN THE FEATURE

"Fetch any URL the user pastes" on a cloud VM is a server-side request
forgery hole, and this particular VM has a metadata service at
169.254.169.254 that hands out credentials to anything that asks. A
naive version of this file would let someone paste that address and have
the model read the instance's keys back to them.

So every request - the discovery fetch, every redirect hop, and every
later tool call - goes through _resolve_safe(), which resolves the
hostname and refuses loopback, private, link-local, multicast and
reserved addresses. Redirects are followed by hand rather than by
requests, because allow_redirects=True would check the first address and
then happily follow a 302 to the metadata endpoint.

That leaves one gap worth naming: between the check and the connection
the DNS answer could change (rebinding). Closing it properly means
pinning the connection to the validated IP, which requires a custom
transport adapter. The window is small and the payoff for an attacker is
reading their own server, so it is documented rather than closed.
"""
import ipaddress
import json
import re
import socket
import urllib.parse

import requests

# Tool results are fed back into the next request, so an uncapped
# response is a context-window overflow and a token bill.
MAX_RESULT_CHARS = 6000
MAX_BYTES = 400_000
TIMEOUT = 12
MAX_REDIRECTS = 3

# Where specs usually live, tried in order when someone pastes a domain.
WELL_KNOWN = (
    "/openapi.json",
    "/.well-known/openapi.json",
    "/swagger.json",
    "/api/openapi.json",
    "/v1/openapi.json",
    "/openapi.yaml",
    # Framework defaults, which is where most real specs actually sit -
    # almost nobody moves these. springdoc, ASP.NET and the Swagger UI
    # default respectively.
    "/v3/api-docs",
    "/swagger/v1/swagger.json",
    "/api-docs",
    "/docs/api-docs.json",
)

# Enough operations to be useful, few enough to fit in a tool
# description without dominating the prompt.
MAX_OPERATIONS = 40


class Unsafe(Exception):
    """The URL points somewhere this server must not fetch."""


def _resolve_safe(url):
    """Check a URL is safe to fetch. -> (host, [ips]). Raises Unsafe.

    Refuses anything that is not plain http(s), and anything resolving
    into a range that only exists inside this network. The metadata
    service is the one that matters most and it is link-local, so it is
    covered by is_link_local rather than by a special case.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Unsafe("Only http:// and https:// links can be connected.")
    host = parts.hostname
    if not host:
        raise Unsafe("That does not look like a web address.")

    try:
        infos = socket.getaddrinfo(host, parts.port or
                                   (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Unsafe("That address does not resolve.")

    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise Unsafe(
                "That address is inside this server's own network, so it "
                "cannot be connected. Paste a public URL.")
        ips.append(str(ip))
    return host, ips


def _get(url, token=None, accept="application/json"):
    """A guarded GET. Follows redirects by hand, re-checking each hop."""
    seen = 0
    current = url
    while True:
        _resolve_safe(current)
        headers = {"Accept": accept,
                   "User-Agent": "RandomGenerals-Connector/1.0"}
        if token:
            headers["Authorization"] = _auth_value(token)
        r = requests.get(current, headers=headers, timeout=TIMEOUT,
                         allow_redirects=False, stream=True)
        if r.status_code in (301, 302, 303, 307, 308):
            target = r.headers.get("Location") or ""
            r.close()
            seen += 1
            if seen > MAX_REDIRECTS or not target:
                raise Unsafe("That link redirects too many times.")
            # A relative Location is normal and must be resolved against
            # the hop it came from, not the original URL.
            current = urllib.parse.urljoin(current, target)
            continue
        body = r.raw.read(MAX_BYTES + 1, decode_content=True) or b""
        r.close()
        if len(body) > MAX_BYTES:
            body = body[:MAX_BYTES]
        return r.status_code, r.headers.get("Content-Type", ""), body, current


def _auth_value(token):
    """Tokens get pasted in whatever form the service documents.

    Someone copying from an API's docs pastes "Bearer abc", "Token abc"
    or just "abc" depending on whose docs they read. Guessing wrong
    produces a 401 that looks like a bad key, so an already-qualified
    token is passed through and a bare one is assumed to be a bearer.
    """
    t = (token or "").strip()
    if re.match(r"^(bearer|token|basic|apikey)\s", t, re.I):
        return t
    return "Bearer " + t


def _parse_spec(body, content_type):
    """An OpenAPI document, or None. YAML is only handled if PyYAML is
    installed - it is not a dependency of this app, and a missing
    optional parser should mean "this looks like a plain link", not a
    crash."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    doc = None
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError:
            return None
    else:
        try:
            import yaml            # noqa: PLC0415 - optional, see docstring
            doc = yaml.safe_load(text)
        except Exception:           # noqa: BLE001
            return None
    if not isinstance(doc, dict):
        return None
    if not (doc.get("openapi") or doc.get("swagger")):
        return None
    return doc


def _operations(doc):
    """Flatten a spec's paths into a callable list."""
    ops = []
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return ops
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(op, dict):
                continue
            ops.append({
                "method": method.upper(),
                "path": path,
                "summary": (op.get("summary") or op.get("description")
                            or "")[:160].strip(),
            })
            if len(ops) >= MAX_OPERATIONS:
                return ops
    return ops


def _base_url(doc, spec_url):
    """Where the operations actually live.

    A spec's servers[] is frequently a relative path, or absent, or a
    template with variables nobody filled in. Falling back to the spec's
    own origin is right far more often than it is wrong.
    """
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(spec_url))
    servers = doc.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            url = (first.get("url") or "").strip()
            if url and "{" not in url:
                return urllib.parse.urljoin(origin + "/", url).rstrip("/")
    if doc.get("basePath"):                     # Swagger 2
        return origin + str(doc["basePath"]).rstrip("/")
    return origin


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (slug or "app")[:28]


def discover(url, token=None):
    """Work out what a pasted link is. -> (connector dict, error).

    Never raises for an ordinary bad link: the caller is a settings
    dialog and every failure here needs to become a sentence someone can
    act on.
    """
    url = (url or "").strip()
    if not url:
        return None, "Paste a link first."
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    try:
        _resolve_safe(url)
    except Unsafe as e:
        return None, str(e)

    candidates = [url]
    # A bare domain, or a path with no file at the end, is probably the
    # site rather than the spec - so try the usual spec locations too.
    parts = urllib.parse.urlsplit(url)
    if not re.search(r"\.(json|ya?ml)$", parts.path or "", re.I):
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        candidates += [origin + p for p in WELL_KNOWN]

    last_status = None
    for candidate in candidates:
        try:
            status, ctype, body, final = _get(candidate, token)
        except Unsafe as e:
            return None, str(e)
        except requests.exceptions.RequestException:
            continue
        last_status = status
        if status != 200:
            continue
        doc = _parse_spec(body, ctype)
        if doc is None:
            continue
        ops = _operations(doc)
        if not ops:
            continue
        title = ((doc.get("info") or {}).get("title") or
                 parts.netloc or "Connected app")
        return {
            "kind": "openapi",
            "url": final,
            "base_url": _base_url(doc, final),
            "title": str(title)[:80],
            "operations": ops,
        }, None

    # No spec anywhere. Register the link itself as readable - which is
    # the honest outcome for the majority of URLs people will paste, and
    # still lets the model go and look at it.
    if last_status is None:
        return None, ("Could not reach that link. Check the address, or "
                      "that the service is up.")
    return {
        "kind": "link",
        "url": url,
        "base_url": "%s://%s" % (parts.scheme, parts.netloc),
        "title": parts.netloc[:80] or "Link",
        "operations": [],
    }, None


# ------------------------------------------------------- tool synthesis

def tool_spec(connector):
    """One function schema per connector.

    ONE, not one per operation. A spec with forty endpoints would push
    forty tool definitions into every request, which costs more prompt
    than the conversation and buys nothing - the model picks an operation
    from the description just as well as from forty schemas.
    """
    name = "connector_" + slugify(connector.get("title"))
    ops = connector.get("operations") or []
    if connector.get("kind") == "openapi" and ops:
        listing = "; ".join(
            "%s %s%s" % (o["method"], o["path"],
                         " - " + o["summary"] if o["summary"] else "")
            for o in ops[:MAX_OPERATIONS])
        description = (
            "Call the %s API, which the user connected. Available "
            "operations: %s" % (connector.get("title"), listing))[:4000]
        params = {
            "type": "object",
            "properties": {
                "method": {"type": "string",
                           "description": "HTTP method, e.g. GET"},
                "path": {"type": "string",
                         "description": "Operation path, exactly as listed"},
                "path_params": {
                    "type": "object",
                    "description": "Values for {placeholders} in the path, "
                                   "e.g. {\"id\": 42} for /pet/{id}"},
                "query": {"type": "object",
                          "description": "Query string parameters"},
                "body": {"type": "object",
                         "description": "JSON body for POST/PUT/PATCH"},
            },
            "required": ["method", "path"],
        }
    else:
        description = ("Read %s, which the user connected. Returns the "
                       "page or data at that address."
                       % connector.get("title"))
        params = {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Optional path under the site root"},
            },
        }
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": params}}


def call(connector, args, token=None):
    """Run one connector call. -> text for the model. Never raises.

    Same contract as tools.py: a failure has to come back as something
    the model can read and react to, because an exception here would
    abort the whole reply.
    """
    kind = connector.get("kind")
    base = (connector.get("base_url") or "").rstrip("/")
    path = str(args.get("path") or "").strip()

    if kind == "openapi":
        method = str(args.get("method") or "GET").upper()
        allowed = connector.get("operations") or []
        # The model must pick something the spec actually declares.
        # Without this it can invent a path, and an invented path on a
        # connected service is a request the user never authorised.
        if not any(o["method"] == method and o["path"] == path
                   for o in allowed):
            return ("No operation %s %s in this API. Choose one of the "
                    "listed operations exactly." % (method, path))
    else:
        method = "GET"
        if path and not path.startswith("/"):
            path = "/" + path

    # An OpenAPI path is a template - /pet/{petId}, not a URL. The
    # allow-list above deliberately matches the TEMPLATE, so an invented
    # path is still refused; substitution happens only after that check
    # passes, and a leftover placeholder is caught rather than fetched
    # literally (which is a 404 the model cannot diagnose).
    if kind == "openapi" and "{" in path:
        for key, value in (args.get("path_params") or {}).items():
            path = path.replace(
                "{%s}" % key, urllib.parse.quote(str(value), safe=""))
        missing = re.findall(r"\{([^}]+)\}", path)
        if missing:
            return ("This operation needs %s. Call it again with "
                    "path_params filled in." % ", ".join(missing))

    target = base + path if path else (connector.get("url") or base)
    query = args.get("query")
    if isinstance(query, dict) and query:
        target += ("&" if "?" in target else "?") + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None})

    try:
        _resolve_safe(target)
    except Unsafe as e:
        return str(e)

    headers = {"Accept": "application/json, text/plain, text/html",
               "User-Agent": "RandomGenerals-Connector/1.0"}
    if token:
        headers["Authorization"] = _auth_value(token)

    try:
        if method == "GET":
            status, _ctype, body, _final = _get(target, token)
        else:
            payload = args.get("body")
            r = requests.request(
                method, target, headers=headers,
                json=payload if isinstance(payload, dict) else None,
                timeout=TIMEOUT, allow_redirects=False, stream=True)
            body = r.raw.read(MAX_BYTES + 1, decode_content=True) or b""
            status = r.status_code
            r.close()
    except Unsafe as e:
        return str(e)
    except requests.exceptions.RequestException as e:
        return "That request failed: %s" % e

    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = text.strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n\n[... truncated]"
    if status >= 400:
        hint = ""
        if status in (401, 403):
            hint = (" The connection may need an access token - the user "
                    "can add one where they pasted the link.")
        return "The service returned %d.%s\n%s" % (status, hint, text[:1000])
    return text or "(empty response)"
