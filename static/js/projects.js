import { state } from "./state.js";
import { del, explain, getJSON, patchJSON, postJSON } from "./api.js";
import { opensDown } from "./chrome.js";
import { confirmDialog } from "./confirm.js";
import { toast } from "./toast.js";
import { loadThreadList } from "./sidebar.js";

/* ----------------------------------------------------------------
   Projects - names that conversations are filed under.

   Three places use them, and all three read state.projects:

     the Project menu under the input   files the open conversation (or
                                        the next one, if none is open)
     Projects in the sidebar            narrows Recents to one project,
                                        makes and deletes projects
     the filter chip above Recents      shows the narrowing, and undoes it

   state.currentProjectId is where the conversation being written goes;
   state.projectFilter is which project Recents is showing. They are
   separate on purpose: looking at one project's list and filing the
   open chat somewhere else are different things to want.
   ---------------------------------------------------------------- */

const FOLDER = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>';
const CHECK = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const PLUS = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
const TRASH = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const $ = (id) => document.getElementById(id);

export function projectName(id) {
  const p = state.projects.find((x) => x.id === id);
  return p ? p.name : "";
}

export async function loadProjects() {
  try {
    const data = await getJSON("/api/projects");
    state.projects = data.projects || [];
  } catch (_) {
    // Projects are an organiser, not the conversation. A failure here
    // leaves the menus empty rather than stopping anything.
    state.projects = [];
  }
  if (state.projectFilter && !projectName(state.projectFilter)) setProjectFilter(null);
  if (state.currentProjectId && !projectName(state.currentProjectId)) state.currentProjectId = null;
  renderProjectPicker();
  renderProjectFilter();
}

async function createProject(name) {
  const p = await postJSON("/api/projects", { name });
  if (!state.projects.some((x) => x.id === p.id)) state.projects.push(p);
  return p;
}

/* ---- the Project menu under the input ------------------------------ */

/** Called when a conversation opens (sidebar.js) or a new one starts. */
export function setCurrentProject(id) {
  state.currentProjectId = id || null;
  renderProjectPicker();
}

export function renderProjectPicker() {
  const label = $("projectPickerName");
  const btn = $("projectPickerBtn");
  if (!label || !btn) return;
  const name = projectName(state.currentProjectId);
  label.textContent = name || "Project";
  btn.classList.toggle("has-value", !!name);
}

async function fileCurrent(id) {
  const before = state.currentProjectId;
  setCurrentProject(id);
  // No conversation yet: the choice is held and goes with the first
  // message (chat.js ensureThread). One open: it is filed now.
  if (!state.currentThreadId) return;
  try {
    await patchJSON(`/api/threads/${state.currentThreadId}`, { project_id: id || null });
    await loadProjects();
    loadThreadList();
  } catch (err) {
    setCurrentProject(before);
    toast(explain(err, "Could not move it."), { kind: "error" });
  }
}

/** A menu item. @returns {HTMLButtonElement} */
function item(html, text, onClick, extra = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pmenu-item " + extra;
  b.setAttribute("role", "menuitem");
  b.innerHTML = html;
  const span = document.createElement("span");
  span.className = "pmenu-text";
  span.textContent = text;
  b.insertBefore(span, b.children[1] || null);
  b.addEventListener("click", onClick);
  return b;
}

/** "New project…" turned into a field in place. Enter makes it. */
function newProjectRow(menu, onMade) {
  const row = document.createElement("div");
  row.className = "pmenu-new";
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 60;
  input.placeholder = "Project name";
  input.setAttribute("aria-label", "New project name");
  const go = document.createElement("button");
  go.type = "button";
  go.className = "pmenu-new-go";
  go.textContent = "Create";
  const submit = async () => {
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    go.disabled = true;
    try {
      const p = await createProject(name);
      await onMade(p);
    } catch (err) {
      toast(explain(err, "Could not make that project."), { kind: "error" });
      go.disabled = false;
    }
  };
  go.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  });
  row.append(input, go);
  return { row, input };
}

function arrowKeys(menu) {
  menu.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if (/** @type {HTMLElement} */ (e.target).tagName === "INPUT") return;
    e.preventDefault();
    const list = /** @type {HTMLElement[]} */ ([...menu.querySelectorAll(".pmenu-item")]);
    const i = list.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    list[(i + (e.key === "ArrowDown" ? 1 : -1) + list.length) % list.length]?.focus();
  });
}

/* A popover that closes on Escape, an outside click, and focus leaving.
   @param {HTMLElement} btn @param {HTMLElement} panel @param {() => void} render */
function popover(btn, panel, render) {
  const set = (open) => {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) {
      render();
      /** @type {HTMLElement|null} */ (panel.querySelector(".pmenu-item.is-on") || panel.querySelector(".pmenu-item"))?.focus();
    }
  };
  btn.addEventListener("click", () => set(panel.hidden));
  const onKey = (e) => {
    if (e.key === "Escape" && !panel.hidden) {
      e.preventDefault();
      e.stopPropagation();
      set(false);
      btn.focus();
    }
  };
  btn.addEventListener("keydown", onKey);
  panel.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", (e) => {
    if (panel.hidden) return;
    const t = /** @type {Node} */ (e.target);
    if (!panel.contains(t) && !btn.contains(t)) set(false);
  });
  arrowKeys(panel);
  return set;
}

function mountPicker() {
  const btn = $("projectPickerBtn");
  const menu = $("projectMenu");
  if (!btn || !menu) return;

  const render = () => {
    opensDown(btn, menu);
    menu.replaceChildren();
    const head = document.createElement("div");
    head.className = "pmenu-label";
    head.textContent = state.currentThreadId ? "Move this chat to" : "Start this chat in";
    menu.appendChild(head);

    const none = item(FOLDER + (state.currentProjectId ? "" : CHECK), "No project",
      () => { setOpen(false); fileCurrent(null); },
      state.currentProjectId ? "" : "is-on");
    menu.appendChild(none);
    state.projects.forEach((p) => {
      const on = p.id === state.currentProjectId;
      menu.appendChild(item(FOLDER + (on ? CHECK : ""), p.name,
        () => { setOpen(false); fileCurrent(p.id); }, on ? "is-on" : ""));
    });
    const sep = document.createElement("div");
    sep.className = "pmenu-sep";
    sep.setAttribute("role", "separator");
    menu.appendChild(sep);
    const add = item(PLUS, "New project…", () => {
      const { row, input } = newProjectRow(menu, async (p) => {
        setOpen(false);
        await fileCurrent(p.id);
        loadProjects();
        btn.focus();
      });
      add.replaceWith(row);
      input.focus();
    });
    menu.appendChild(add);
  };
  const setOpen = popover(btn, menu, render);
}

/* ---- Projects in the sidebar, and the filter it sets --------------- */

export function setProjectFilter(id) {
  state.projectFilter = id || null;
  renderProjectFilter();
}

function renderProjectFilter() {
  const chip = $("projectFilter");
  const name = $("projectFilterName");
  if (!chip || !name) return;
  const n = projectName(state.projectFilter);
  chip.hidden = !n;
  name.textContent = n;
  $("navProjects")?.classList.toggle("is-on", !!n);
}

function mountPanel() {
  const btn = $("navProjects");
  const panel = $("projectsPanel");
  if (!btn || !panel) return;

  const choose = (id) => {
    setOpen(false);
    setProjectFilter(id);
    // A new chat from here starts inside the project being looked at.
    if (!state.currentThreadId) setCurrentProject(id);
    loadThreadList();
  };

  const remove = async (p) => {
    const ok = await confirmDialog({
      title: `Delete the project "${p.name}"?`,
      body: p.threads
        ? `Its ${p.threads} conversation${p.threads === 1 ? "" : "s"} will be kept, just no longer filed in it.`
        : "It has no conversations in it.",
      confirmLabel: "Delete project", danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/projects/${p.id}`);
    } catch (err) {
      toast(explain(err, "Could not delete it."), { kind: "error" });
      return;
    }
    if (state.projectFilter === p.id) setProjectFilter(null);
    if (state.currentProjectId === p.id) setCurrentProject(null);
    await loadProjects();
    loadThreadList();
  };

  const render = () => {
    panel.replaceChildren();
    const head = document.createElement("div");
    head.className = "pmenu-label";
    head.textContent = "Projects";
    panel.appendChild(head);

    if (state.projectFilter) {
      panel.appendChild(item(FOLDER, "All conversations", () => choose(null)));
    }
    if (!state.projects.length) {
      const empty = document.createElement("p");
      empty.className = "pmenu-empty";
      empty.textContent = "File conversations together - by course, client or anything else.";
      panel.appendChild(empty);
    }
    state.projects.forEach((p) => {
      const on = p.id === state.projectFilter;
      const row = document.createElement("div");
      row.className = "pmenu-row";
      const open = item(FOLDER, p.name, () => choose(p.id), on ? "is-on" : "");
      const count = document.createElement("span");
      count.className = "pmenu-count";
      count.textContent = String(p.threads || 0);
      open.appendChild(count);
      const trash = document.createElement("button");
      trash.type = "button";
      trash.className = "pmenu-trash";
      trash.innerHTML = TRASH;
      trash.title = "Delete project";
      trash.setAttribute("aria-label", `Delete project ${p.name}`);
      trash.addEventListener("click", (e) => { e.stopPropagation(); setOpen(false); remove(p); });
      row.append(open, trash);
      panel.appendChild(row);
    });
    const sep = document.createElement("div");
    sep.className = "pmenu-sep";
    sep.setAttribute("role", "separator");
    panel.appendChild(sep);
    const add = item(PLUS, "New project…", () => {
      const { row, input } = newProjectRow(panel, async (p) => {
        await loadProjects();
        choose(p.id);
      });
      add.replaceWith(row);
      input.focus();
    });
    panel.appendChild(add);
  };
  const setOpen = popover(btn, panel, () => {
    render();
    // The counts move as chats are filed; refresh them under the open
    // panel, keeping focus on the same row.
    loadProjects().then(() => {
      if (panel.hidden) return;
      const rows = [...panel.querySelectorAll(".pmenu-item")];
      const at = rows.indexOf(/** @type {Element} */ (document.activeElement));
      if (panel.querySelector(".pmenu-new")) return;   // mid-typing a name
      render();
      if (at >= 0) /** @type {HTMLElement|undefined} */ (panel.querySelectorAll(".pmenu-item")[at])?.focus();
    });
  });

  $("projectFilterClear")?.addEventListener("click", () => {
    setProjectFilter(null);
    loadThreadList();
  });
}

/** Wiring, called from app.js once every module has been evaluated. */
export function mountProjects() {
  mountPicker();
  mountPanel();
}
