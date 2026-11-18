import { state } from "./state.js";
import { explain, getJSON } from "./api.js";
import { selectBay } from "./bays.js";
import { sidebar } from "./dom.js";
import { openThread } from "./sidebar.js";
import { relativeTime } from "./strength.js";
import { projectName } from "./projects.js";

/* ----------------------------------------------------------------
   Search - Ctrl+K (Cmd+K on a Mac), or Search in the sidebar.

   Every conversation in every mode, by title and by what was said in
   it (/api/threads?q=). With nothing typed it lists the most recent
   ones, so it doubles as a jump-to. A <dialog> opened with showModal()
   gives the focus trap, Escape and the backdrop for free.
   ---------------------------------------------------------------- */

const MODE_LABEL = { chat: "Chat", code: "Code", image: "Image", video: "Diagram" };

/** @type {HTMLDialogElement|null} */
let dialog = null;
/** @type {HTMLInputElement|null} */
let input = null;
/** @type {HTMLElement|null} */
let results = null;
let seq = 0;
let timer = 0;

export function openSearch() {
  if (!dialog || !input) return;
  sidebar.classList.remove("open");
  if (!dialog.open) dialog.showModal();
  input.value = "";
  input.focus();
  run("");
}

function rows() {
  return /** @type {HTMLElement[]} */ ([...results.querySelectorAll(".search-row")]);
}

function setActive(i) {
  const list = rows();
  if (!list.length) return;
  const n = (i + list.length) % list.length;
  list.forEach((r, k) => r.setAttribute("aria-selected", String(k === n)));
  list[n].scrollIntoView({ block: "nearest" });
  input.setAttribute("aria-activedescendant", list[n].id);
}

function activeIndex() {
  return rows().findIndex((r) => r.getAttribute("aria-selected") === "true");
}

async function go(t) {
  dialog.close();
  // The list in the sidebar is per mode; open the conversation in its own.
  if (t.mode && t.mode !== state.currentBay) selectBay(t.mode);
  await openThread(t.id);
}

/** Marks the query inside a piece of text, safely (textContent only). */
function highlighted(text, q) {
  const span = document.createElement("span");
  const at = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (at < 0) { span.textContent = text; return span; }
  const mark = document.createElement("mark");
  mark.textContent = text.slice(at, at + q.length);
  span.append(text.slice(0, at), mark, text.slice(at + q.length));
  return span;
}

function render(threads, q) {
  results.replaceChildren();
  if (!threads.length) {
    const empty = document.createElement("p");
    empty.className = "search-empty";
    empty.textContent = q ? `Nothing matches "${q}".` : "No conversations yet.";
    results.appendChild(empty);
    input.removeAttribute("aria-activedescendant");
    return;
  }
  const head = document.createElement("div");
  head.className = "search-section";
  head.textContent = q ? "Results" : "Recent";
  results.appendChild(head);
  threads.forEach((t, i) => {
    const row = document.createElement("div");
    row.className = "search-row";
    row.id = "searchRow" + i;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(i === 0));

    const main = document.createElement("div");
    main.className = "search-row-main";
    const title = highlighted(t.title || "Untitled", q);
    title.className = "search-row-title";
    main.appendChild(title);
    if (t.snippet) {
      const snip = highlighted(t.snippet, q);
      snip.className = "search-row-snippet";
      main.appendChild(snip);
    }
    const meta = document.createElement("span");
    meta.className = "search-row-meta";
    const bits = [MODE_LABEL[t.mode] || "Chat"];
    const pn = projectName(t.project_id);
    if (pn) bits.push(pn);
    bits.push(relativeTime(t.updated));
    meta.textContent = bits.filter(Boolean).join(" · ");

    row.append(main, meta);
    row.addEventListener("click", () => go(t));
    row.addEventListener("pointermove", () => {
      if (row.getAttribute("aria-selected") !== "true") setActive(rows().indexOf(row));
    });
    results.appendChild(row);
  });
  input.setAttribute("aria-activedescendant", "searchRow0");
  results.scrollTop = 0;
}

async function run(q) {
  const mine = ++seq;
  let data;
  try {
    data = await getJSON(q ? `/api/threads?q=${encodeURIComponent(q)}` : "/api/threads");
  } catch (err) {
    if (mine !== seq) return;
    results.replaceChildren();
    const p = document.createElement("p");
    p.className = "search-empty";
    p.textContent = explain(err, "Search is not reachable right now.");
    results.appendChild(p);
    return;
  }
  if (mine !== seq) return;          // a later keystroke already answered
  render((data.threads || []).slice(0, q ? 50 : 20), q);
}

/** Wiring, called from app.js once every module has been evaluated. */
export function mountSearch() {
  dialog = /** @type {HTMLDialogElement|null} */ (document.getElementById("searchDialog"));
  input = /** @type {HTMLInputElement|null} */ (document.getElementById("searchInput"));
  results = document.getElementById("searchResults");
  if (!dialog || !input || !results) return;

  // Cmd on a Mac, Ctrl elsewhere - and the hint in the sidebar says which.
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const kbd = document.getElementById("navSearchKbd");
  if (kbd) kbd.textContent = mac ? "⌘K" : "Ctrl K";

  document.getElementById("navSearch")?.addEventListener("click", openSearch);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (dialog.open) dialog.close();
      else openSearch();
    }
  });

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    timer = window.setTimeout(() => run(q), q ? 140 : 0);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(activeIndex() + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(activeIndex() - 1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const row = rows()[Math.max(0, activeIndex())];
      row?.click();
    }
  });
  // A click on the backdrop (the dialog element itself, outside the box)
  // closes it, as every other popover here does.
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close();
  });
}
