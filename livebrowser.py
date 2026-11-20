"""The live workspace browser: a real Chromium on this server, one tab
per person, shown in the pane as a stream of screenshots.

Why a remote browser rather than an iframe or a proxy: most sites refuse
to be framed, and proxying a site's own scripts into our origin would
hand them the person's session. Here the page runs on the server, inside
Chromium's sandbox, and the person's browser only ever receives a JPEG.
Clicks, scrolling and typing are sent back as coordinates and keys.

Every request the page makes - the document, its scripts, fetch(),
images - passes the same private-address guard as the rest of the app
(connectors._resolve_safe), so a page cannot reach this machine, the
cloud metadata service or the private network behind it.

Playwright is async-only in spirit and not thread-safe; Flask serves on
several threads. So Playwright lives on one thread with its own event
loop, and every call from a request thread is a coroutine submitted to
it (_call). Tabs close after IDLE_SECONDS, and at most MAX_SESSIONS are
open at once - the oldest is closed to make room.

Optional: without Playwright installed (a dev machine) available() is
False and the pane falls back to the reading view in browser.py.
"""
import asyncio
import os
import threading
import time
import urllib.parse

import browser
import connectors

try:
    from playwright.async_api import async_playwright
except ImportError:                                   # pragma: no cover
    async_playwright = None

MAX_SESSIONS = int(os.environ.get("LIVE_BROWSER_MAX", "4") or 4)
IDLE_SECONDS = int(os.environ.get("LIVE_BROWSER_IDLE", "300") or 300)
NAV_TIMEOUT_MS = 20000
CALL_TIMEOUT = 30
DEFAULT_W, DEFAULT_H = 1024, 720
JPEG_QUALITY = 62
SEARCH_URL = os.environ.get("BROWSER_SEARCH_URL", "https://www.bing.com/search?q=%s")
EXECUTABLE = os.environ.get("LIVE_BROWSER_EXECUTABLE") or None   # e.g. local Edge for tests
MAX_TEXT_CHARS = browser.MAX_TEXT_CHARS

# Keys a person can press on the page. Anything else printable is typed.
KEYS = {"Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown",
        "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", "Space"}


class LiveError(Exception):
    """A sentence for a person."""


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ the guard
_verdicts = {}          # host -> (time, allowed)


def _host_allowed(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return True
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    hit = _verdicts.get(host)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        connectors._resolve_safe("%s://%s/" % (parts.scheme, host))
        ok = True
    except Exception:                                  # noqa: BLE001 - Unsafe, DNS failure...
        ok = False
    if len(_verdicts) > 2000:
        _verdicts.clear()
    _verdicts[host] = (time.time(), ok)
    return ok


# ------------------------------------------------------------ the engine
class _Session:
    def __init__(self, context, page, w, h):
        self.context = context
        self.page = page
        self.w, self.h = w, h
        self.used = time.time()
        self.lock = asyncio.Lock()


class _Engine:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.pw = None
        self.browser = None
        self.sessions = {}
        self.failed_at = 0.0
        self.start_lock = threading.Lock()

    # -- plumbing ---------------------------------------------------
    def _ensure_thread(self):
        with self.start_lock:
            if self.thread and self.thread.is_alive():
                return
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self._run, name="live-browser", daemon=True)
            self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._reaper())
        self.loop.run_forever()

    def call(self, coro_fn, *args, timeout=CALL_TIMEOUT):
        self._ensure_thread()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self.loop)
        try:
            return fut.result(timeout)
        except LiveError:
            raise
        except TimeoutError as e:
            fut.cancel()
            raise LiveError("The page took too long to respond.") from e
        except Exception as e:                         # noqa: BLE001
            raise LiveError("The browser hit a problem: %s" % (str(e).split("\n")[0][:160])) from e

    async def _browser(self):
        if self.browser and self.browser.is_connected():
            return self.browser
        if self.pw is None:
            self.pw = await async_playwright().start()
        try:
            self.browser = await self.pw.chromium.launch(
                executable_path=EXECUTABLE, headless=True,
                args=["--disable-dev-shm-usage", "--disable-gpu", "--mute-audio",
                      "--disable-background-networking", "--no-first-run"])
        except Exception:
            self.failed_at = time.time()
            raise
        self.sessions.clear()                          # contexts died with the old one
        return self.browser

    async def _reaper(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for owner, s in list(self.sessions.items()):
                if now - s.used > IDLE_SECONDS:
                    await self._close(owner)

    async def _close(self, owner):
        s = self.sessions.pop(owner, None)
        if s:
            try:
                await s.context.close()
            except Exception:                          # noqa: BLE001
                pass

    async def _guard(self, route):
        url = route.request.url
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        hit = _verdicts.get(host)
        if hit and time.time() - hit[0] < 60:
            ok = hit[1]
        else:                                          # DNS off the event loop
            ok = await asyncio.get_running_loop().run_in_executor(None, _host_allowed, url)
        if ok:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _session(self, owner, w=None, h=None, create=True):
        s = self.sessions.get(owner)
        if s and not s.page.is_closed():
            s.used = time.time()
            if w and h and (w, h) != (s.w, s.h):
                s.w, s.h = w, h
                await s.page.set_viewport_size({"width": w, "height": h})
            return s
        if not create:
            return None
        b = await self._browser()
        while len(self.sessions) >= MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda k: self.sessions[k].used)
            await self._close(oldest)
        w, h = w or DEFAULT_W, h or DEFAULT_H
        ctx = await b.new_context(
            viewport={"width": w, "height": h}, locale="en-US",
            service_workers="block", accept_downloads=False,
            user_agent=None)
        ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        ctx.set_default_timeout(8000)
        await ctx.route("**/*", self._guard)
        page = await ctx.new_page()
        s = _Session(ctx, page, w, h)

        # One tab: a link that opens a new window opens here instead.
        async def on_popup(p):
            url = p.url
            try:
                await p.close()
            except Exception:                          # noqa: BLE001
                pass
            if url and url != "about:blank" and _host_allowed(url):
                try:
                    await page.goto(url)
                except Exception:                      # noqa: BLE001
                    pass
        ctx.on("page", lambda p: asyncio.ensure_future(on_popup(p)) if p is not page else None)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        self.sessions[owner] = s
        return s

    @staticmethod
    async def _settle(page, ms=1500):
        try:
            await page.wait_for_load_state("load", timeout=ms)
        except Exception:                              # noqa: BLE001 - slow pages still show
            pass

    @staticmethod
    async def _state(s):
        try:
            title = await s.page.title()
        except Exception:                              # noqa: BLE001
            title = ""
        url = s.page.url if s.page.url != "about:blank" else ""
        return {"url": url, "title": title,
                "site": urllib.parse.urlsplit(url).hostname or "",
                "w": s.w, "h": s.h}

    # -- operations ---------------------------------------------------
    async def go(self, owner, raw, w, h):
        try:
            kind, value = browser.normalise(raw)
        except browser.BrowseError as e:
            raise LiveError(str(e)) from e
        url = SEARCH_URL % urllib.parse.quote_plus(value) if kind == "search" else value
        try:
            await asyncio.get_running_loop().run_in_executor(None, connectors._resolve_safe, url)
        except connectors.Unsafe as e:
            raise LiveError(str(e)) from e
        s = await self._session(owner, w, h)
        async with s.lock:
            try:
                await s.page.goto(url, wait_until="domcontentloaded")
            except Exception as e:                     # noqa: BLE001
                msg = str(e)
                if "ERR_BLOCKED_BY_CLIENT" in msg:
                    raise LiveError("That address is inside this server's own network, "
                                    "so it cannot be opened.") from e
                if "ERR_NAME_NOT_RESOLVED" in msg:
                    raise LiveError("No website answers at that address.") from e
                if "Timeout" not in msg:
                    raise LiveError("That page could not be opened (%s)."
                                    % msg.split("\n")[0][:120]) from e
            await self._settle(s.page)
            return await self._state(s)

    async def act(self, owner, a, w, h):
        s = await self._session(owner, w, h, create=False)
        if not s:
            raise LiveError("The browser was closed after a while unused. Open a page again.")
        kind = a.get("type")
        async with s.lock:
            p = s.page
            if kind == "click":
                x = _clamp(a.get("x"), 0, s.w - 1, 0)
                y = _clamp(a.get("y"), 0, s.h - 1, 0)
                await p.mouse.click(x, y)
                await asyncio.sleep(0.25)
                await self._settle(p, 2500)
            elif kind == "scroll":
                await p.mouse.move(_clamp(a.get("x"), 0, s.w - 1, s.w // 2),
                                   _clamp(a.get("y"), 0, s.h - 1, s.h // 2))
                await p.mouse.wheel(_clamp(a.get("dx"), -3000, 3000, 0),
                                    _clamp(a.get("dy"), -3000, 3000, 0))
                await asyncio.sleep(0.12)
            elif kind == "type":
                text = str(a.get("text") or "")[:500]
                if text:
                    await p.keyboard.type(text)
            elif kind == "key":
                key = str(a.get("key") or "")
                if key not in KEYS:
                    raise LiveError("That key is not supported.")
                await p.keyboard.press(" " if key == "Space" else key)
                if key == "Enter":
                    await asyncio.sleep(0.3)
                    await self._settle(p, 2500)
            elif kind == "back":
                await p.go_back(wait_until="domcontentloaded")
                await self._settle(p)
            elif kind == "forward":
                await p.go_forward(wait_until="domcontentloaded")
                await self._settle(p)
            elif kind == "reload":
                await p.reload(wait_until="domcontentloaded")
                await self._settle(p)
            elif kind == "resize":
                pass                                   # done by _session
            else:
                raise LiveError("Unknown action.")
            return await self._state(s)

    async def frame(self, owner):
        s = await self._session(owner, create=False)
        if not s:
            return None, None
        async with s.lock:
            img = await s.page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=8000)
            return img, await self._state(s)

    async def text(self, owner):
        s = await self._session(owner, create=False)
        if not s:
            return None
        async with s.lock:
            state = await self._state(s)
            try:
                body = await s.page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:                          # noqa: BLE001
                body = ""
        state["text"] = (body or "")[:MAX_TEXT_CHARS]
        return state

    async def close(self, owner):
        await self._close(owner)


_engine = _Engine()


# ------------------------------------------------------------ public
def available():
    """Can this server run the live browser? (Playwright installed, and the
    last launch did not fail in the past minute.)"""
    if async_playwright is None or os.environ.get("LIVE_BROWSER", "1") == "0":
        return False
    return time.time() - _engine.failed_at > 60


def _size(w, h):
    return (_clamp(w, 360, 1600, DEFAULT_W) if w else None,
            _clamp(h, 300, 1400, DEFAULT_H) if h else None)


def go(owner, raw, w=None, h=None):
    w, h = _size(w, h)
    return _engine.call(_engine.go, owner, raw, w, h)


def act(owner, action, w=None, h=None):
    w, h = _size(w, h)
    return _engine.call(_engine.act, owner, action, w, h)


def frame(owner):
    """-> (jpeg bytes, state) or (None, None) when this person has no tab."""
    return _engine.call(_engine.frame, owner, timeout=15)


def text(owner):
    """The open tab's url, title and visible text, or None."""
    if not available():
        return None
    try:
        return _engine.call(_engine.text, owner, timeout=15)
    except LiveError:
        return None


def close(owner):
    if available():
        try:
            _engine.call(_engine.close, owner, timeout=10)
        except LiveError:
            pass
