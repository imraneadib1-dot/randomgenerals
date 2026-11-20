import { explain, getJSON, postJSON } from "./api.js";
import { showBrowserPane } from "./artifacts.js";

/* ----------------------------------------------------------------
   The workspace browser - real web pages in the pane beside the chat.

   LIVE (the normal case): a real Chromium tab runs on the server
   (livebrowser.py) and is shown here as a stream of frames. Clicks,
   the wheel and the keyboard are sent back to it, so the page behaves
   like a page: menus open, forms take text, sites log in. Nothing of
   the site runs in this document - it only ever receives a JPEG.

   READING VIEW (fallback): where the server cannot run Chromium (a dev
   machine), pages are fetched and cleaned by browser.py instead.

   Three ways in: the globe at the top, + -> Open the browser, and the
   AI opening a page (open_page), which loads here live.

   While a page is open it goes with the next message ("Asking about"
   above the input). The x on that tag leaves it out; opening another
   page brings it back.
   ---------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const nav = { stack: /** @type {string[]} */ ([]), at: -1 };       // reading view only
/** @type {{url: string, title: string, site: string, kind: string} | null} */
let page = null;
let attached = true;
let loadSeq = 0;

let live = false;
/** @type {Promise<void> | null} */
let ready = null;
let viewW = 0;
let viewH = 0;
let frameTimer = 0;
let frameBusy = false;
let lastInput = 0;
let frameUrl = "";

function els() {
  return {
    pane: $("artifactPane"),
    address: /** @type {HTMLInputElement|null} */ ($("browserAddress")),
    view: $("browserPage"),
    box: $("artifactBrowserView"),
    liveBox: $("browserLive"),
    frame: /** @type {HTMLImageElement|null} */ ($("browserFrame")),
    status: $("browserStatus"),
    back: /** @type {HTMLButtonElement|null} */ ($("browserBack")),
    forward: /** @type {HTMLButtonElement|null} */ ($("browserForward")),
    original: /** @type {HTMLButtonElement|null} */ ($("browserOriginal")),
    title: $("artifactTitle"),
    lang: $("artifactLang"),
    chip: $("pageChip"),
    chipTitle: $("pageChipTitle"),
  };
}

function showing() {
  const pane = $("artifactPane");
  return !!pane && pane.dataset.mode === "browser"
    && !!document.querySelector(".app")?.classList.contains("artifact-open");
}

/** The page to send with the next message, or "". */
export function currentPageUrl() {
  return showing() && attached && page && page.url ? page.url : "";
}

function setStatus(text) {
  const e = els();
  if (e.status) e.status.textContent = text || "";
}

function syncChrome() {
  const e = els();
  if (live) {
    if (e.back) e.back.disabled = !page;
    if (e.forward) e.forward.disabled = !page;
  } else {
    if (e.back) e.back.disabled = nav.at <= 0;
    if (e.forward) e.forward.disabled = nav.at >= nav.stack.length - 1;
  }
  if (e.original) e.original.disabled = !(page && page.url);
  if (e.title) e.title.textContent = page ? page.title || page.site || "Browser" : "Browser";
  if (e.lang) e.lang.textContent = page ? page.site || "" : "";
  const showLive = live && !!page;
  if (e.liveBox) e.liveBox.hidden = !showLive;
  if (e.view) e.view.hidden = showLive;
  syncChip();
}

export function syncChip() {
  const e = els();
  if (!e.chip || !e.chipTitle) return;
  const on = !!currentPageUrl();
  e.chip.hidden = !on;
  if (on) e.chipTitle.textContent = page.title || page.url;
}

function startPage() {
  const e = els();
  page = null;
  if (e.view) {
    e.view.replaceChildren();
    const box = document.createElement("div");
    box.className = "browser-start";
    const h = document.createElement("p");
    h.className = "browser-start-title";
    h.textContent = "Browse with the AI beside you";
    const p = document.createElement("p");
    p.textContent = "Type an address or a search above. Whatever page is open "
      + "here goes with your next message, so you can ask about it.";
    box.append(h, p);
    e.view.appendChild(box);
  }
  setStatus("");
  if (e.address) e.address.value = "";
  syncChrome();
}

/* ---- live: state and frames ------------------------------------------ */
function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return ""; }
}

/** @param {{url?: string, title?: string, w?: number, h?: number}} st */
function applyState(st) {
  if (!st) return;
  const e = els();
  const url = st.url || "";
  if (!url) return;
  if (!page || page.url !== url) attached = true;      // a new page is sent again
  page = { url, title: st.title || "", site: hostOf(url), kind: "page" };
  if (st.w) viewW = st.w;
  if (st.h) viewH = st.h;
  if (e.address && document.activeElement !== e.address) e.address.value = url;
  syncChrome();
}

function viewSize() {
  const box = els().box;
  const w = Math.round(box ? box.clientWidth : 1024);
  const h = Math.round(box ? box.clientHeight : 720);
  return { w: Math.max(360, w), h: Math.max(300, h) };
}

function scheduleFrame(ms) {
  clearTimeout(frameTimer);
  frameTimer = window.setTimeout(pullFrame, ms);
}

async function pullFrame() {
  frameTimer = 0;
  if (!live || !page || !showing() || document.hidden) return;
  if (frameBusy) { scheduleFrame(250); return; }
  frameBusy = true;
  try {
    const r = await fetch("/api/browser/frame", { cache: "no-store" });
    if (r.status === 200) {
      const blob = await r.blob();
      const url = decodeURIComponent(r.headers.get("X-Page-Url") || "");
      const title = decodeURIComponent(r.headers.get("X-Page-Title") || "");
      const [w, h] = (r.headers.get("X-Page-Size") || "").split("x").map(Number);
      showFrame(blob);
      applyState({ url, title, w, h });
    } else if (r.status === 204) {
      setStatus("This page was closed after a while unused. Press reload to open it again.");
      return;
    }
  } catch (_) {
    /* a missed frame; the next one will do */
  } finally {
    frameBusy = false;
  }
  // Quick while someone is using the page, slow while they read.
  scheduleFrame(Date.now() - lastInput < 8000 ? 600 : 1800);
}

function showFrame(blob) {
  const img = els().frame;
  if (!img) return;
  const next = URL.createObjectURL(blob);
  const prev = frameUrl;
  img.onload = () => { if (prev) URL.revokeObjectURL(prev); };
  img.src = next;
  frameUrl = next;
}

async function liveGo(input) {
  const seq = ++loadSeq;
  const e = els();
  setStatus("Loading…");
  e.liveBox?.classList.add("is-loading");
  try {
    const st = await postJSON("/api/browser/go", { url: input, ...viewSize() });
    if (seq !== loadSeq) return;
    attached = true;
    applyState(st);
    setStatus("");
  } catch (err) {
    if (seq !== loadSeq) return;
    setStatus(explain(err, "That page could not be opened."));
  } finally {
    if (seq === loadSeq) e.liveBox?.classList.remove("is-loading");
  }
  scheduleFrame(0);
}

/** @param {Record<string, any>} a */
async function act(a) {
  lastInput = Date.now();
  try {
    const st = await postJSON("/api/browser/act", { ...a, ...viewSize() });
    applyState(st);
    if (a.type !== "type") setStatus("");
  } catch (err) {
    setStatus(explain(err, "The page did not respond."));
  }
  scheduleFrame(0);
}

/* ---- reading view (no Chromium on this server) -------------------------- */
async function readerLoad(input, push) {
  const e = els();
  const seq = ++loadSeq;
  setStatus("Loading…");
  e.view?.classList.add("is-loading");
  let data;
  try {
    data = await getJSON("/api/browse?url=" + encodeURIComponent(input));
  } catch (err) {
    if (seq !== loadSeq) return;
    e.view?.classList.remove("is-loading");
    setStatus(explain(err, "That page could not be opened."));
    return;
  }
  if (seq !== loadSeq) return;
  e.view?.classList.remove("is-loading");
  page = { url: data.url || "", title: data.title || "", site: data.site || "", kind: data.kind };
  attached = true;
  const key = data.url || input;
  if (push) {
    nav.stack = nav.stack.slice(0, nav.at + 1);
    nav.stack.push(key);
    nav.at = nav.stack.length - 1;
  }
  if (e.address) e.address.value = data.kind === "search" ? input : (data.url || input);
  setStatus("");
  if (e.view) {
    // Built server-side from an allow-list (browser.py Reader): no
    // scripts, styles, handlers or forms survive, links are absolute
    // http(s), so it is safe to insert as markup.
    e.view.innerHTML = data.html || "<p>(This page has no readable text.)</p>";
    e.view.parentElement?.scrollTo?.(0, 0);
  }
  syncChrome();
}

function load(input, push = true) {
  return live ? liveGo(input) : readerLoad(input, push);
}

/** Open the browser, optionally on an address or search.
 *  @param {string} [input] @param {{fromAI?: boolean, focus?: boolean}} [opts] */
export async function openBrowser(input, opts = {}) {
  // The AI opening a page does not take over a phone screen; the chip
  // under its reply opens it on demand.
  if (opts.fromAI && window.innerWidth < 1024 && !showing()) return;
  showBrowserPane();
  if (ready) await ready;
  syncChrome();
  if (input) {
    if (page && page.url === input && !opts.fromAI) { scheduleFrame(0); return; }
    load(input);
  } else if (!page) {
    startPage();
    els().address?.focus();
  } else {
    scheduleFrame(0);
    if (opts.focus) {
      const a = els().address;
      a?.focus();
      a?.select();
    }
  }
}

/* ---- wiring -------------------------------------------------------------- */
const PAGE_KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp",
  "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End"]);

function mountLive() {
  const e = els();
  const liveBox = e.liveBox;
  const img = e.frame;
  if (!liveBox || !img) return;

  /** Pane pixels -> page pixels. The frame is drawn at the pane's width. */
  const toPage = (ev) => {
    const r = img.getBoundingClientRect();
    const sx = (viewW || r.width) / (r.width || 1);
    const sy = (viewH || r.height) / (r.height || 1);
    return { x: Math.round((ev.clientX - r.left) * sx), y: Math.round((ev.clientY - r.top) * sy) };
  };

  img.addEventListener("click", (ev) => {
    if (!page) return;
    liveBox.focus({ preventScroll: true });
    act({ type: "click", ...toPage(ev) });
  });

  let wheelDy = 0;
  let wheelDx = 0;
  let wheelTimer = 0;
  /** @type {{x: number, y: number}} */
  let wheelAt = { x: 0, y: 0 };
  liveBox.addEventListener("wheel", (ev) => {
    if (!page) return;
    ev.preventDefault();
    wheelDy += ev.deltaMode === 1 ? ev.deltaY * 40 : ev.deltaY;
    wheelDx += ev.deltaMode === 1 ? ev.deltaX * 40 : ev.deltaX;
    wheelAt = toPage(ev);
    if (!wheelTimer) {
      wheelTimer = window.setTimeout(() => {
        const dy = Math.round(wheelDy);
        const dx = Math.round(wheelDx);
        wheelDy = 0; wheelDx = 0; wheelTimer = 0;
        if (dy || dx) act({ type: "scroll", dy, dx, ...wheelAt });
      }, 140);
    }
  }, { passive: false });

  // Keys: printable ones are batched into one "type", the rest pressed.
  let typed = "";
  let typeTimer = 0;
  const flush = () => {
    clearTimeout(typeTimer);
    typeTimer = 0;
    if (typed) { const t = typed; typed = ""; act({ type: "type", text: t }); }
  };
  liveBox.addEventListener("keydown", (ev) => {
    if (!page || ev.isComposing) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;   // let paste and shortcuts be
    if (ev.key.length === 1) {
      ev.preventDefault();
      typed += ev.key;
      clearTimeout(typeTimer);
      typeTimer = window.setTimeout(flush, 120);
    } else if (PAGE_KEYS.has(ev.key)) {
      ev.preventDefault();
      ev.stopPropagation();                             // Escape must not close the pane
      flush();
      act({ type: "key", key: ev.key });
    }
  });
  liveBox.addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") || "";
    if (!page || !text) return;
    ev.preventDefault();
    flush();
    act({ type: "type", text: text.slice(0, 500) });
  });

  // Keep the page the size of the pane.
  let sizeTimer = 0;
  new ResizeObserver(() => {
    if (!live || !page || !showing()) return;
    clearTimeout(sizeTimer);
    sizeTimer = window.setTimeout(() => {
      const s = viewSize();
      if (Math.abs(s.w - viewW) > 8 || Math.abs(s.h - viewH) > 8) act({ type: "resize" });
    }, 350);
  }).observe(e.box || liveBox);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && showing()) scheduleFrame(0);
  });
}

export function mountBrowser() {
  const e = els();
  if (!e.view || !e.address) return;

  ready = getJSON("/api/browser/status")
    .then((d) => { live = !!(d && d.live); })
    .catch(() => { live = false; });

  $("browserBar")?.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = e.address.value.trim();
    if (v) { e.address.blur(); load(v); }
  });
  e.back?.addEventListener("click", () => {
    if (live) { if (page) act({ type: "back" }); return; }
    if (nav.at > 0) { nav.at -= 1; readerLoad(nav.stack[nav.at], false); }
  });
  e.forward?.addEventListener("click", () => {
    if (live) { if (page) act({ type: "forward" }); return; }
    if (nav.at < nav.stack.length - 1) { nav.at += 1; readerLoad(nav.stack[nav.at], false); }
  });
  $("browserReload")?.addEventListener("click", () => {
    if (live) {
      if (!page) return;
      // A tab closed while unused is opened again on the same page.
      const url = page.url;
      postJSON("/api/browser/act", { type: "reload", ...viewSize() })
        .then((st) => { applyState(st); setStatus(""); scheduleFrame(0); })
        .catch(() => liveGo(url));
      return;
    }
    if (nav.at >= 0) readerLoad(nav.stack[nav.at], false);
  });
  e.original?.addEventListener("click", () => {
    if (page && page.url) window.open(page.url, "_blank", "noopener");
  });

  // Reading view: links open in this browser; with Ctrl/Cmd, in a real tab.
  e.view.addEventListener("click", (ev) => {
    const a = /** @type {HTMLElement} */ (ev.target).closest?.("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!/^https?:\/\//.test(href)) return;
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey) window.open(href, "_blank", "noopener");
    else load(href);
  });

  mountLive();

  $("pageChipClear")?.addEventListener("click", () => {
    attached = false;
    syncChip();
  });

  $("openBrowserBtn")?.addEventListener("click", () => openBrowser());
  // The globe at the top: straight to the address bar, ready to search.
  $("globeBtn")?.addEventListener("click", () => openBrowser(undefined, { focus: true }));

  // The tag above the input follows the pane: closing it, or switching
  // the pane to a code file, stops sending the page. Reopening resumes
  // the frames.
  const pane = $("artifactPane");
  const app = document.querySelector(".app");
  const obs = new MutationObserver(() => {
    syncChip();
    if (showing() && live && page && !frameTimer) scheduleFrame(0);
  });
  if (pane) obs.observe(pane, { attributes: true, attributeFilter: ["data-mode"] });
  if (app) obs.observe(app, { attributes: true, attributeFilter: ["class"] });
}
