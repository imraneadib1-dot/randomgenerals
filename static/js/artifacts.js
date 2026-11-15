import { readPref, writePref } from "./appearance.js";
import { renderMarkdown } from "./markdown.js";
import { downloadBlob } from "./preview.js";

/* ----------------------------------------------------------------
   The workspace pane - code and documents beside the conversation.

   A long code block in the middle of a chat is read once and then
   scrolled past forever. Anything substantial a reply produces - a
   script, a page, a document - is also an ARTIFACT: a thing with a
   name that the next reply is likely to revise. It opens in a pane to
   the right of the chat, where it can be read at full height, run as a
   page, copied or saved, while the conversation about it carries on.

   WHAT COUNTS. A block of ten lines or more, or anything that can be
   previewed (a page, an SVG, a markdown document). A one-line shell
   command is an answer, not an artifact, and stays in the chat only.

   VERSIONS. Artifacts are keyed by filename when the model put one on
   the first line, otherwise by language - so "now make the button
   blue" producing a second index.html becomes version 2 of the same
   artifact rather than a new tab. A second block of the same language
   in ONE reply is a different file, not a revision, and gets its own key.

   NO SECOND SOURCE OF TRUTH. The registry points at the code blocks in
   the chat log; it does not keep copies. Opening another conversation
   empties the log, the blocks disconnect, and the pruning below drops
   them - so this module never needs to be told a thread changed.
   ---------------------------------------------------------------- */

const MIN_LINES = 10;
const MIN_PANE = 360;
const MIN_CHAT = 380;
const WIDE = 1024;            // below this the pane is a full-screen sheet
const WIDTH_KEY = "artifactPaneWidth";

const LANG_LABEL = {
  js: "JavaScript", javascript: "JavaScript", jsx: "JSX", ts: "TypeScript",
  typescript: "TypeScript", tsx: "TSX", py: "Python", python: "Python",
  html: "HTML", htm: "HTML", css: "CSS", scss: "SCSS", json: "JSON",
  sql: "SQL", bash: "Bash", sh: "Shell", shell: "Shell", go: "Go",
  rust: "Rust", rs: "Rust", java: "Java", c: "C", cpp: "C++", "c++": "C++",
  csharp: "C#", cs: "C#", php: "PHP", ruby: "Ruby", rb: "Ruby",
  swift: "Swift", kotlin: "Kotlin", yaml: "YAML", yml: "YAML",
  markdown: "Markdown", md: "Markdown", svg: "SVG", xml: "XML",
  plaintext: "Text", text: "Text",
};

const EXT_BY_LANG = {
  js: "js", javascript: "js", jsx: "jsx", ts: "ts", typescript: "ts",
  tsx: "tsx", py: "py", python: "py", html: "html", htm: "html",
  css: "css", scss: "scss", json: "json", sql: "sql", bash: "sh", sh: "sh",
  shell: "sh", go: "go", rust: "rs", rs: "rs", java: "java", c: "c",
  cpp: "cpp", csharp: "cs", cs: "cs", php: "php", ruby: "rb", rb: "rb",
  swift: "swift", kotlin: "kt", yaml: "yml", yml: "yml", markdown: "md",
  md: "md", svg: "svg", xml: "xml",
};

/**
 * @typedef {{ content: string, block: HTMLElement }} Version
 * @typedef {{ key: string, title: string, lang: string, versions: Version[] }} Artifact
 */

/** @type {Map<string, Artifact>} insertion order is tab order */
const artifacts = new Map();
/** What the pane is showing. */
const view = { key: "", index: 0, mode: "code" };
let collapsedSidebarForUs = false;
let drawnSig = "";
/** The pane's width as last set - not as measured, which mid-transition
 *  is somewhere between the old value and the new one. */
let paneWidth = 0;

/* ---- elements ------------------------------------------------------ */
const app = /** @type {HTMLElement} */ (document.querySelector(".app"));
const pane = document.getElementById("artifactPane");
const resizer = document.getElementById("paneResizer");
const chatLog = document.getElementById("chatLog");
const titleEl = document.getElementById("artifactTitle");
const langEl = document.getElementById("artifactLang");
const tabsEl = document.getElementById("artifactTabs");
const viewsEl = document.getElementById("artifactViews");
const versionsEl = document.getElementById("artifactVersions");
const versionLabel = document.getElementById("artifactVersionLabel");
const prevBtn = /** @type {HTMLButtonElement} */ (document.getElementById("artifactPrev"));
const nextBtn = /** @type {HTMLButtonElement} */ (document.getElementById("artifactNext"));
const copyBtn = document.getElementById("artifactCopy");
const downloadBtn = document.getElementById("artifactDownload");
const popoutBtn = document.getElementById("artifactPopout");
const closeBtn = document.getElementById("artifactClose");
const codeView = document.getElementById("artifactCodeView");
const gutter = document.getElementById("artifactGutter");
const codeEl = document.getElementById("artifactCode");
const previewView = document.getElementById("artifactPreviewView");

/* ---- what a block is ------------------------------------------------ */
function langOf(block) {
  if (block.dataset.lang) return block.dataset.lang;
  const code = block.querySelector("pre code");
  const cls = code ? code.className : "";
  return (cls.match(/language-([\w+-]+)/) || [])[1] || "plaintext";
}

/** The kind of preview a piece of content supports, or "" for none. */
function previewKind(lang, content) {
  const l = lang.toLowerCase();
  if (l === "html" || l === "htm") return /<[a-z!][\s\S]*>/i.test(content) ? "html" : "";
  if (l === "svg" || ((l === "xml" || l === "plaintext") && /^\s*<svg[\s>]/i.test(content))) return "svg";
  if (l === "markdown" || l === "md") return "markdown";
  return "";
}

function describe(block) {
  const code = block.querySelector("pre code");
  if (!code) return null;
  const content = (code.textContent || "").replace(/\n$/, "");
  const lang = langOf(block);
  const lines = content.split("\n").length;
  if (lines < MIN_LINES && !(previewKind(lang, content) && lines >= 3)) return null;
  return { content, lang, filename: block.dataset.filename || "" };
}

function titleFor(filename, lang, n) {
  const base = filename || LANG_LABEL[lang.toLowerCase()] || lang;
  return n > 1 ? `${base} (${n})` : base;
}

/* ---- registry ------------------------------------------------------- */
function prune() {
  for (const [key, a] of artifacts) {
    a.versions = a.versions.filter((v) => v.block.isConnected);
    if (!a.versions.length) artifacts.delete(key);
  }
}

/**
 * Register every artifact-sized block in a finished reply. Returns the
 * versions this reply added, last one last.
 * @param {HTMLElement} bubble
 * @returns {{ key: string, index: number }[]}
 */
function collect(bubble) {
  prune();
  const added = [];
  const seen = new Map();
  bubble.querySelectorAll(".code-block").forEach((/** @type {HTMLElement} */ block) => {
    const info = describe(block);
    if (!info) return;
    const base = info.filename || info.lang.toLowerCase();
    const n = (seen.get(base) || 0) + 1;
    seen.set(base, n);
    const key = n > 1 ? `${base}#${n}` : base;

    let a = artifacts.get(key);
    if (!a) {
      a = { key, title: titleFor(info.filename, info.lang, n), lang: info.lang, versions: [] };
      artifacts.set(key, a);
    }
    const last = a.versions[a.versions.length - 1];
    if (last && last.content === info.content) {
      // The same file again - a reply that repeated it unchanged is not
      // a new version. Point at the newest copy and move on.
      last.block = block;
    } else {
      a.versions.push({ content: info.content, block });
      a.lang = info.lang;
    }
    block.dataset.artifactKey = key;
    addOpenButton(block);
    added.push({ key, index: a.versions.length - 1 });
  });
  return added;
}

function locate(block) {
  const key = block.dataset.artifactKey;
  const a = key && artifacts.get(key);
  if (!a) return null;
  const index = a.versions.findIndex((v) => v.block === block);
  return index === -1 ? null : { key, index };
}

/* ---- the Open buttons in the chat ------------------------------------ */
const OPEN_ICON =
  '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
  '<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2" stroke="currentColor" stroke-width="1.4"/>' +
  '<path d="M9.5 2.75v10.5" stroke="currentColor" stroke-width="1.4"/></svg>';

function addOpenButton(block) {
  const project = block.closest(".code-project");
  const host = project
    ? project.querySelector(".code-project-actions")
    : block.querySelector(".code-block-header");
  if (!host || host.querySelector(".open-artifact-btn")) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn open-artifact-btn";
  btn.innerHTML = OPEN_ICON + "<span>Open</span>";
  btn.title = "Open in the workspace pane";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const target = project
      ? /** @type {HTMLElement} */ (project.querySelector(".code-block:not([hidden])"))
      : block;
    const at = target && locate(target);
    if (at) openArtifact(at.key, at.index);
  });
  // First in the row: it is the main thing to do with a long block.
  const lang = project ? null : host.firstElementChild;
  if (lang && lang.nextSibling) host.insertBefore(btn, lang.nextSibling);
  else host.prepend(btn);
}

/* ---- drawing the pane ------------------------------------------------ */
function current() {
  const a = artifacts.get(view.key);
  if (!a) return null;
  const v = a.versions[Math.min(view.index, a.versions.length - 1)];
  return { a, v };
}

function render() {
  const cur = current();
  if (!cur) { closePane(); return; }
  const { a, v } = cur;
  const kind = previewKind(a.lang, v.content);
  if (!kind && view.mode === "preview") view.mode = "code";

  titleEl.textContent = a.title;
  langEl.textContent = LANG_LABEL[a.lang.toLowerCase()] || a.lang;

  // Views: Preview only exists for something that can be previewed.
  viewsEl.hidden = !kind;
  viewsEl.querySelectorAll("[data-view]").forEach((/** @type {HTMLElement} */ b) => {
    const on = b.dataset.view === view.mode;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  });
  popoutBtn.hidden = !(kind && view.mode === "preview" && kind !== "markdown");

  // Versions.
  const count = a.versions.length;
  const idx = a.versions.indexOf(v);
  versionsEl.hidden = count < 2;
  versionLabel.textContent = `v${idx + 1} of ${count}`;
  prevBtn.disabled = idx <= 0;
  nextBtn.disabled = idx >= count - 1;

  view.index = idx;

  renderTabs();

  codeView.hidden = view.mode !== "code";
  previewView.hidden = view.mode !== "preview";
  // Redraw the body only when what it shows changed. Every new message
  // in the chat calls render() for the tabs; rebuilding the iframe each
  // time would restart a previewed page under the person using it.
  const sig = `${a.key}|${idx}|${view.mode}|${v.content.length}`;
  if (sig !== drawnSig) {
    drawnSig = sig;
    if (view.mode === "code") drawCode(a.lang, v.content);
    else drawPreview(kind, v.content);
  }

  // Which block in the chat this is, so the eye can find it.
  chatLog.querySelectorAll(".code-block.is-in-pane")
    .forEach((el) => el.classList.remove("is-in-pane"));
  v.block.classList.add("is-in-pane");
}

function renderTabs() {
  tabsEl.innerHTML = "";
  tabsEl.hidden = artifacts.size < 2;
  if (tabsEl.hidden) return;
  for (const a of artifacts.values()) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "artifact-tab";
    tab.setAttribute("role", "tab");
    const on = a.key === view.key;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    tab.textContent = a.title;
    tab.addEventListener("click", () => openArtifact(a.key, a.versions.length - 1));
    tabsEl.appendChild(tab);
  }
}

function drawCode(lang, content) {
  codeEl.className = "language-" + lang;
  codeEl.textContent = content;
  // highlight.js refuses an element it has marked as done.
  delete codeEl.dataset.highlighted;
  if (window.hljs) {
    try { hljs.highlightElement(codeEl); } catch (_) { /* plain is fine */ }
  }
  // A separate gutter rather than one span per line: highlight.js
  // emits spans that cross line breaks (a block comment, a template
  // string), and cutting its output into lines would break them.
  const lines = content.split("\n").length;
  let nums = "";
  for (let i = 1; i <= lines; i++) nums += i + "\n";
  gutter.textContent = nums;
}

function drawPreview(kind, content) {
  previewView.innerHTML = "";
  if (kind === "markdown") {
    const doc = document.createElement("div");
    doc.className = "msg assistant artifact-doc";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    doc.appendChild(bubble);
    previewView.appendChild(doc);
    renderMarkdown(bubble, content, { final: true });
    return;
  }
  const frame = document.createElement("iframe");
  frame.className = "artifact-frame";
  frame.title = "Preview";
  // Scripts but never same-origin - see preview.js. The page runs, and
  // it cannot read this document, its cookies or the visitor's session.
  frame.setAttribute("sandbox", "allow-scripts allow-forms allow-popups allow-modals");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.srcdoc = pageFor(kind, content);
  previewView.appendChild(frame);
}

function pageFor(kind, content) {
  if (kind === "svg") {
    return '<!doctype html><meta charset="utf-8"><body style="margin:0;min-height:100vh;' +
      'display:grid;place-items:center;background:#fff">' + content + "</body>";
  }
  if (/<html[\s>]|<!doctype html/i.test(content)) return content;
  // A fragment - a component, a snippet with its own <style>. Give it a
  // document so it renders as written rather than in quirks mode.
  return '<!doctype html><html><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1"></head>' +
    "<body>" + content + "</body></html>";
}

/* ---- opening and closing ------------------------------------------- */
function isWide() {
  return window.innerWidth >= WIDE;
}

/** @param {string} key @param {number} index @param {string} [mode] */
export function openArtifact(key, index, mode) {
  const a = artifacts.get(key);
  if (!a) return;
  const switching = key !== view.key;
  view.key = key;
  view.index = Math.max(0, Math.min(index, a.versions.length - 1));
  if (mode) view.mode = mode;
  else if (switching) {
    view.mode = previewKind(a.lang, a.versions[view.index].content) ? "preview" : "code";
  }
  const wasOpen = isOpen();
  if (!wasOpen) openPane();
  render();
  if (!wasOpen && !isWide()) closeBtn.focus();
}

function isOpen() {
  return app.classList.contains("artifact-open");
}

function openPane() {
  applyWidth(storedWidth());
  app.classList.add("artifact-open");
  pane.inert = false;
  pane.setAttribute("aria-hidden", "false");
  resizer.tabIndex = 0;
  // Room for both panes: on a laptop-sized window the conversation list
  // is the thing to give up while working on a file. Put back on close,
  // and only if it was this module that took it away.
  if (isWide() && window.innerWidth < 1600 && !app.classList.contains("sidebar-collapsed")) {
    setSidebarCollapsed(true);
    collapsedSidebarForUs = true;
  }
}

export function closePane() {
  if (!isOpen()) return;
  app.classList.remove("artifact-open");
  pane.inert = true;
  pane.setAttribute("aria-hidden", "true");
  resizer.tabIndex = -1;
  chatLog.querySelectorAll(".code-block.is-in-pane")
    .forEach((el) => el.classList.remove("is-in-pane"));
  if (collapsedSidebarForUs) {
    setSidebarCollapsed(false);
    collapsedSidebarForUs = false;
  }
  // Drop the iframe so a previewed page stops running in the background.
  previewView.innerHTML = "";
  drawnSig = "";
}

/* Toggles the class the sidebar module uses, without its preference
   write - collapsing it for the pane is not the person choosing a
   collapsed sidebar, and should not survive a reload. */
function setSidebarCollapsed(collapsed) {
  app.classList.toggle("sidebar-collapsed", collapsed);
  const btn = document.getElementById("sidebarCollapse");
  if (btn) {
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }
}

/* ---- width ---------------------------------------------------------- */
function maxWidth() {
  const sidebar = document.getElementById("sidebar");
  const side = sidebar && isWide() ? sidebar.getBoundingClientRect().width : 0;
  return Math.max(MIN_PANE, app.clientWidth - side - MIN_CHAT);
}

function storedWidth() {
  const saved = Number(readPref(WIDTH_KEY));
  return saved > 0 ? saved : Math.round(app.clientWidth * 0.5);
}

function applyWidth(px) {
  const w = Math.round(Math.min(maxWidth(), Math.max(MIN_PANE, px)));
  app.style.setProperty("--pane-w", w + "px");
  paneWidth = w;
  resizer.setAttribute("aria-valuenow", String(Math.round((w / app.clientWidth) * 100)));
  return w;
}

function mountResizer() {
  let dragging = false;
  resizer.setAttribute("aria-valuemin", "0");
  resizer.setAttribute("aria-valuemax", "100");

  resizer.addEventListener("pointerdown", (e) => {
    if (!isOpen() || e.button !== 0) return;
    dragging = true;
    resizer.setPointerCapture(e.pointerId);
    app.classList.add("is-resizing");
    e.preventDefault();
  });
  resizer.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applyWidth(app.getBoundingClientRect().right - e.clientX);
  });
  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    if (resizer.hasPointerCapture(e.pointerId)) resizer.releasePointerCapture(e.pointerId);
    app.classList.remove("is-resizing");
    writePref(WIDTH_KEY, String(paneWidth));
  };
  resizer.addEventListener("pointerup", stop);
  resizer.addEventListener("pointercancel", stop);

  // Double-click puts it back to half.
  resizer.addEventListener("dblclick", () => {
    writePref(WIDTH_KEY, String(applyWidth(app.clientWidth * 0.5)));
  });

  // The separator is a control, so it moves from the keyboard too.
  resizer.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 96 : 24;
    const w = paneWidth;
    let next = null;
    if (e.key === "ArrowLeft") next = w + step;
    else if (e.key === "ArrowRight") next = w - step;
    else if (e.key === "Home") next = maxWidth();
    else if (e.key === "End") next = MIN_PANE;
    if (next === null) return;
    e.preventDefault();
    writePref(WIDTH_KEY, String(applyWidth(next)));
  });

  // Keep the pane inside the window when the window shrinks.
  window.addEventListener("resize", () => {
    if (isOpen()) applyWidth(paneWidth || storedWidth());
  });
}

/* ---- toolbar -------------------------------------------------------- */
function flash(btn, label) {
  const span = btn.querySelector("span");
  if (!span) return;
  const was = span.textContent;
  span.textContent = label;
  setTimeout(() => (span.textContent = was), 1400);
}

function mountToolbar() {
  closeBtn.addEventListener("click", closePane);

  copyBtn.addEventListener("click", async () => {
    const cur = current();
    if (!cur) return;
    try {
      await navigator.clipboard.writeText(cur.v.content);
      flash(copyBtn, "Copied");
    } catch (_) {
      flash(copyBtn, "Blocked");
    }
  });

  downloadBtn.addEventListener("click", () => {
    const cur = current();
    if (!cur) return;
    const { a, v } = cur;
    const ext = EXT_BY_LANG[a.lang.toLowerCase()] || "txt";
    const name = /\.[a-z0-9]{1,5}$/i.test(a.key.replace(/#\d+$/, ""))
      ? a.key.replace(/#\d+$/, "").split("/").pop()
      : "artifact." + ext;
    downloadBlob(v.content, name, ext === "html" ? "text/html" : "text/plain");
  });

  popoutBtn.addEventListener("click", () => {
    const cur = current();
    if (!cur) return;
    const kind = previewKind(cur.a.lang, cur.v.content);
    const blob = new Blob([pageFor(kind, cur.v.content)], { type: "text/html" });
    window.open(URL.createObjectURL(blob), "_blank", "noopener");
  });

  prevBtn.addEventListener("click", () => { view.index -= 1; render(); });
  nextBtn.addEventListener("click", () => { view.index += 1; render(); });

  viewsEl.addEventListener("click", (e) => {
    const b = /** @type {HTMLElement} */ (e.target).closest("[data-view]");
    if (!(b instanceof HTMLElement)) return;
    view.mode = b.dataset.view;
    render();
  });

  // Arrow keys move along a tab row, as tabs are expected to.
  [viewsEl, tabsEl].forEach((row) => row.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const tabs = /** @type {HTMLElement[]} */ ([...row.querySelectorAll('[role="tab"]')]);
    const i = tabs.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    if (i === -1) return;
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    next.click();
    const again = /** @type {HTMLElement[]} */ ([...row.querySelectorAll('[role="tab"]')]);
    again[tabs.indexOf(next)]?.focus();
  }));

  pane.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closePane(); }
  });
}

/* ---- wiring --------------------------------------------------------- */
export function mountArtifacts() {
  if (!app || !pane || !resizer || !chatLog) return;
  pane.inert = true;
  mountResizer();
  mountToolbar();

  // A finished reply (see renderMarkdown). Live ones - just streamed,
  // not replayed from history - open the newest artifact they made, the
  // way a file opens in an editor the moment it is created. Only on a
  // screen with room for it, or when the pane is already up; on a phone
  // a sheet sliding over the answer is an interruption.
  chatLog.addEventListener("rg:rendered", (e) => {
    const bubble = /** @type {HTMLElement} */ (e.target);
    if (!(bubble instanceof HTMLElement)) return;
    const added = collect(bubble);
    const live = /** @type {CustomEvent} */ (e).detail?.live;
    if (isOpen()) {
      if (live && added.length) {
        const last = added[added.length - 1];
        openArtifact(last.key, last.index);
      } else {
        render();
      }
    } else if (live && added.length && isWide()) {
      const last = added[added.length - 1];
      openArtifact(last.key, last.index);
    }
  });

  // Preview buttons on blocks in the chat. Handled here when the block
  // is an artifact; otherwise left alone, and the overlay opens instead.
  chatLog.addEventListener("rg:open-artifact", (e) => {
    const target = /** @type {HTMLElement} */ (e.target);
    const project = target.closest(".code-project");
    const block = /** @type {HTMLElement} */ (target.closest(".code-block")
      || (project && project.querySelector(".code-block:not([hidden])")));
    const at = block && locate(block);
    if (!at) return;
    e.preventDefault();
    openArtifact(at.key, at.index, /** @type {CustomEvent} */ (e).detail?.view || undefined);
  });

  // The conversation was replaced or cleared. Whatever the pane was
  // showing may no longer exist.
  new MutationObserver(() => {
    if (!isOpen()) return;
    // A message was added: nothing the pane shows went away.
    const cur = current();
    if (cur && cur.v.block.isConnected) return;
    prune();
    if (!artifacts.has(view.key)) closePane();
    else render();
  }).observe(chatLog, { childList: true });
}
