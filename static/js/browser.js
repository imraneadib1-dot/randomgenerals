import { explain, getJSON } from "./api.js";
import { showBrowserPane } from "./artifacts.js";

/* ----------------------------------------------------------------
   The workspace browser - web pages in the pane beside the chat.

   Pages are fetched and cleaned on the server (browser.py, /api/browse)
   and shown as a reading view: most sites refuse to be framed, so an
   iframe of the real page would be blank half the time, and this way
   what is on screen is exactly what the AI reads. "Open original" is
   always one click away.

   Three ways in:
     - + menu -> Open the browser, then type an address or a search
     - the AI opens a page (the open_page tool); it appears here live
     - an "Opened ..." chip under a reply, to go back to that page

   While a page is open, it goes with the next message ("Asking about"
   above the input), so "summarise this" means the page on screen. The
   x on that tag leaves it out; opening another page brings it back.
   ---------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const nav = { stack: /** @type {string[]} */ ([]), at: -1 };
/** @type {{url: string, title: string, site: string, kind: string} | null} */
let page = null;
let attached = true;
let loadSeq = 0;

function els() {
  return {
    pane: $("artifactPane"),
    address: /** @type {HTMLInputElement|null} */ ($("browserAddress")),
    view: $("browserPage"),
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
    && document.querySelector(".app")?.classList.contains("artifact-open");
}

/** The page to send with the next message, or "". */
export function currentPageUrl() {
  return showing() && attached && page && page.url ? page.url : "";
}

function syncChrome() {
  const e = els();
  if (e.back) e.back.disabled = nav.at <= 0;
  if (e.forward) e.forward.disabled = nav.at >= nav.stack.length - 1;
  if (e.original) e.original.disabled = !(page && page.url);
  if (e.title) e.title.textContent = page ? page.title || page.site || "Browser" : "Browser";
  if (e.lang) e.lang.textContent = page ? page.site || "" : "";
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
  if (e.status) e.status.textContent = "";
  if (e.address) e.address.value = "";
  syncChrome();
}

async function load(input, push) {
  const e = els();
  const seq = ++loadSeq;
  if (e.status) e.status.textContent = "Loading…";
  e.view?.classList.add("is-loading");
  let data;
  try {
    data = await getJSON("/api/browse?url=" + encodeURIComponent(input));
  } catch (err) {
    if (seq !== loadSeq) return;
    e.view?.classList.remove("is-loading");
    if (e.status) e.status.textContent = explain(err, "That page could not be opened.");
    return;
  }
  if (seq !== loadSeq) return;           // a newer navigation won
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
  if (e.status) e.status.textContent = "";
  if (e.view) {
    // Built server-side from an allow-list (browser.py Reader): no
    // scripts, styles, handlers or forms survive, links are absolute
    // http(s), so it is safe to insert as markup.
    e.view.innerHTML = data.html || "<p>(This page has no readable text.)</p>";
    e.view.scrollTop = 0;
    e.view.parentElement?.scrollTo?.(0, 0);
  }
  syncChrome();
}

/** Open the browser, optionally on an address or search.
 *  @param {string} [input] @param {{fromAI?: boolean}} [opts] */
export function openBrowser(input, opts = {}) {
  // The AI opening a page does not take over a phone screen; the chip
  // under its reply opens it on demand.
  if (opts.fromAI && window.innerWidth < 1024 && !showing()) return;
  showBrowserPane();
  syncChrome();
  if (input) {
    if (page && page.url === input && !opts.fromAI) return;
    load(input, true);
  } else if (!page) {
    startPage();
    els().address?.focus();
  }
}

export function mountBrowser() {
  const e = els();
  if (!e.view || !e.address) return;

  $("browserBar")?.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = e.address.value.trim();
    if (v) load(v, true);
  });
  e.back?.addEventListener("click", () => {
    if (nav.at > 0) { nav.at -= 1; load(nav.stack[nav.at], false); }
  });
  e.forward?.addEventListener("click", () => {
    if (nav.at < nav.stack.length - 1) { nav.at += 1; load(nav.stack[nav.at], false); }
  });
  $("browserReload")?.addEventListener("click", () => {
    if (nav.at >= 0) load(nav.stack[nav.at], false);
  });
  e.original?.addEventListener("click", () => {
    if (page && page.url) window.open(page.url, "_blank", "noopener");
  });

  // Links in a page open in this browser; with Ctrl/Cmd, in a real tab.
  e.view.addEventListener("click", (ev) => {
    const a = /** @type {HTMLElement} */ (ev.target).closest?.("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!/^https?:\/\//.test(href)) return;
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey) window.open(href, "_blank", "noopener");
    else load(href, true);
  });

  $("pageChipClear")?.addEventListener("click", () => {
    attached = false;
    syncChip();
  });

  $("openBrowserBtn")?.addEventListener("click", () => openBrowser());

  // The tag above the input follows the pane: closing it, or switching
  // the pane to a code file, stops sending the page.
  const pane = $("artifactPane");
  const app = document.querySelector(".app");
  const obs = new MutationObserver(syncChip);
  if (pane) obs.observe(pane, { attributes: true, attributeFilter: ["data-mode"] });
  if (app) obs.observe(app, { attributes: true, attributeFilter: ["class"] });
}
