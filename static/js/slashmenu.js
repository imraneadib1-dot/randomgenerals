// THE "/" MENU. Typing "/" at the start of the message lists the skills
// that are on for this person (built-in, their own, and their plugins'),
// filtered as they type; Enter or Tab puts "/name " in the box. The
// server does the rest - extensions.match_skills reads the /name.

/** @type {{id: string, title: string, icon: string, description: string, handled_by?: string}[] | null} */
let skills = null;
let fetchedAt = 0;

async function loadSkills() {
  if (skills && Date.now() - fetchedAt < 60_000) return skills;
  try {
    const r = await fetch("/api/extensions");
    const d = await r.json();
    skills = (d.active || []).filter((s) => !s.handled_by);
    fetchedAt = Date.now();
  } catch (e) {
    skills = skills || [];
  }
  return skills;
}

export function mountSlashMenu() {
  const input = /** @type {HTMLTextAreaElement | null} */ (document.getElementById("messageInput"));
  const menu = document.getElementById("slashMenu");
  if (!input || !menu) return;
  /** @type {{id: string, title: string, icon: string, description: string}[]} */
  let shown = [];
  let active = 0;

  function close() {
    menu.hidden = true;
    shown = [];
  }

  /** @param {number} i */
  function pick(i) {
    const s = shown[i];
    if (!s) return;
    input.value = "/" + s.id + " " + input.value.replace(/^\/[a-z0-9-]*\s?/, "");
    close();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function render() {
    menu.replaceChildren();
    shown.forEach((s, i) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "slash-item" + (i === active ? " is-active" : "");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(i === active));
      const icon = document.createElement("span");
      icon.className = "slash-icon";
      icon.textContent = s.icon || "✨";
      const text = document.createElement("span");
      text.className = "slash-text";
      const name = document.createElement("span");
      name.className = "slash-name";
      name.textContent = "/" + s.id;
      const title = document.createElement("small");
      title.textContent = s.title + (s.description ? " — " + s.description : "");
      text.append(name, title);
      item.append(icon, text);
      item.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); });
      menu.append(item);
    });
    const more = document.createElement("a");
    more.className = "slash-more";
    more.href = "/customize";
    more.textContent = "Manage skills, apps & plugins";
    menu.append(more);
    menu.hidden = false;
  }

  input.addEventListener("input", async () => {
    const m = /^\/([a-z0-9-]*)$/i.exec(input.value);
    if (!m) { if (!menu.hidden) close(); return; }
    const list = await loadSkills();
    const q = m[1].toLowerCase();
    /** @param {{id: string, title: string}} s */
    const rank = (s) => (s.id.startsWith(q) ? 0 : s.title.toLowerCase().startsWith(q) ? 1 : 2);
    shown = list.filter((s) => s.id.includes(q) || s.title.toLowerCase().includes(q))
      .sort((a, b) => rank(a) - rank(b)).slice(0, 8);
    active = 0;
    if (!shown.length) { close(); return; }
    render();
  });

  // Capture phase on the document, so Enter picks a skill instead of
  // reaching the composer's own Enter-to-send handler first.
  document.addEventListener("keydown", (e) => {
    if (menu.hidden || e.target !== input) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
      render();
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      e.stopImmediatePropagation();
      pick(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }, true);

  input.addEventListener("blur", () => setTimeout(close, 120));
}
