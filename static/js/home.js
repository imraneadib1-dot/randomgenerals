import { state } from "./state.js";
import { getJSON } from "./api.js";
import { readPref, writePref } from "./appearance.js";
import { selectBay } from "./bays.js";
import { sidebar } from "./dom.js";
import { openThread } from "./sidebar.js";

/* ----------------------------------------------------------------
   The home screen's extras, and the sidebar's small behaviours.

   RECENT ARTIFACTS. The three sites the builder made most recently,
   under the input on the empty state only (the CSS shows the section
   for .main.is-empty). A card opens the conversation that built it;
   one whose conversation is gone opens the sites page instead. Hidden
   outright when there are none - an empty grid with a heading is a
   promise of something that is not there.

   RECENTS can be folded away, and stays folded (per browser).
   ---------------------------------------------------------------- */

const RECENTS_KEY = "recentsCollapsed";
const CARDS = 3;
let lastLoad = 0;

/** "9m ago", "3h ago", then a date: "Nov 14". */
export function shortAgo(iso) {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  const d = new Date(t);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, sameYear
    ? { month: "short", day: "numeric" }
    : { month: "short", day: "numeric", year: "numeric" });
}

/** A stable hue per name, so each card's thumbnail is its own colour. */
function hueFor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

/** A drawn stand-in for the page: a window, a hero line, a few blocks.
 *  Built from the site's own name and files, never fetched - the home
 *  screen should not be running three websites to show three cards. */
function thumbnail(site) {
  const box = document.createElement("div");
  box.className = "artifact-thumb";
  box.style.setProperty("--thumb-hue", String(hueFor(site.name || "")));
  box.setAttribute("aria-hidden", "true");
  const bar = document.createElement("div");
  bar.className = "artifact-thumb-bar";
  bar.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  const page = document.createElement("div");
  page.className = "artifact-thumb-page";
  const hero = document.createElement("div");
  hero.className = "artifact-thumb-hero";
  hero.textContent = site.name || "Untitled";
  const lines = document.createElement("div");
  lines.className = "artifact-thumb-lines";
  lines.append(document.createElement("i"), document.createElement("i"));
  const tiles = document.createElement("div");
  tiles.className = "artifact-thumb-tiles";
  tiles.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  page.append(hero, lines, tiles);
  box.append(bar, page);
  if (site.url) {
    const live = document.createElement("span");
    live.className = "artifact-thumb-live";
    live.textContent = "Live";
    box.appendChild(live);
  }
  return box;
}

function card(site) {
  const a = document.createElement("a");
  a.className = "artifact-card";
  a.href = "/sites";
  a.title = site.name || "Untitled";
  a.appendChild(thumbnail(site));
  const foot = document.createElement("div");
  foot.className = "artifact-card-foot";
  const name = document.createElement("span");
  name.className = "artifact-card-title";
  name.textContent = site.name || "Untitled";
  const when = document.createElement("time");
  when.className = "artifact-card-time";
  when.dateTime = site.updated || "";
  when.textContent = shortAgo(site.updated);
  foot.append(name, when);
  a.appendChild(foot);
  // Opens the conversation in the app when it still exists; the link
  // to /sites stays the fallback (and the middle-click target).
  if (site.thread_id) {
    a.addEventListener("click", async (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      if (site.thread_mode && site.thread_mode !== state.currentBay) selectBay(site.thread_mode);
      await openThread(site.thread_id);
    });
  }
  return a;
}

export async function loadHomeArtifacts(force = false) {
  const section = document.getElementById("homeArtifacts");
  const grid = document.getElementById("homeArtifactsGrid");
  if (!section || !grid) return;
  if (!force && Date.now() - lastLoad < 15000) return;
  lastLoad = Date.now();
  let sites = [];
  try {
    sites = (await getJSON("/api/sites")).sites || [];
  } catch (_) {
    sites = [];         // decoration, not function: say nothing, show nothing
  }
  grid.replaceChildren(...sites.slice(0, CARDS).map(card));
  section.hidden = !sites.length;
}

function mountRecents() {
  const btn = document.getElementById("recentsToggle");
  const body = document.getElementById("recentsBody");
  if (!btn || !body) return;
  const apply = (collapsed) => {
    btn.setAttribute("aria-expanded", String(!collapsed));
    body.hidden = collapsed;
  };
  apply(readPref(RECENTS_KEY) === "1");
  btn.addEventListener("click", () => {
    const collapsed = btn.getAttribute("aria-expanded") === "true";
    apply(collapsed);
    writePref(RECENTS_KEY, collapsed ? "1" : "0");
  });
}

/* On a phone the sidebar is a drawer over the page. Anything chosen in
   it - a conversation, New chat, Search - closes it, and so does a tap
   on the page beside it. */
function mountDrawer() {
  document.getElementById("newChatBtn")?.addEventListener("click", () => sidebar.classList.remove("open"));
  // Design is the Image mode, opened on a fresh page.
  document.getElementById("navDesign")?.addEventListener("click", () => {
    sidebar.classList.remove("open");
    selectBay("image");
    document.getElementById("messageInput")?.focus();
  });
  document.addEventListener("pointerdown", (e) => {
    if (!sidebar.classList.contains("open")) return;
    const t = /** @type {Element} */ (e.target);
    if (sidebar.contains(t) || t.closest?.("#mobileToggle")) return;
    sidebar.classList.remove("open");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sidebar.classList.contains("open")) sidebar.classList.remove("open");
  });
}

/* The connection light and Install sit in the corner of the
   conversation on a desktop, and in the top bar on a phone - the bar is
   only on screen at that width. Moved, not copied: the ids (and the
   listeners on Install) stay on the one element. */
function mountStatusPlacement() {
  const bar = document.querySelector("body > .topbar");
  const corner = document.getElementById("chatToolbar");
  const pill = document.getElementById("statusPill");
  const install = document.getElementById("installBtn");
  const globe = document.getElementById("globeBtn");
  if (!bar || !corner || !pill || !install) return;
  const slot = document.createElement("div");
  slot.className = "topbar-right";
  bar.appendChild(slot);
  const mq = window.matchMedia("(max-width: 720px)");
  const place = () => {
    const to = mq.matches ? slot : corner;
    if (globe) to.append(globe);
    to.append(pill, install);
  };
  mq.addEventListener("change", place);
  place();
}

/** Wiring, called from app.js once every module has been evaluated. */
export function mountHome() {
  mountRecents();
  mountDrawer();
  mountStatusPlacement();
  // Refresh the cards whenever the home screen comes back: a site built
  // in the conversation just left should be on it.
  const main = document.querySelector(".main");
  if (main) {
    let was = main.classList.contains("is-empty");
    new MutationObserver(() => {
      const now = main.classList.contains("is-empty");
      if (now && !was) loadHomeArtifacts(true);
      was = now;
    }).observe(main, { attributes: true, attributeFilter: ["class"] });
  }
  loadHomeArtifacts(true);
}
