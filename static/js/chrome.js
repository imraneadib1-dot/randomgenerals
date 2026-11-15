import { state } from "./state.js";
import { PREF_THEME, applyAppearance, readPref, writePref } from "./appearance.js";
import { openSettings, switchModalTab } from "./modal.js";

/* ----------------------------------------------------------------
   Chrome - the pieces of the shell that are not a feature of their own:
   the centred empty state, the + menu, the profile menu at the foot of
   the sidebar, and the model picker at the top of the chat.

   THE EMPTY STATE IS ONE GROUP. With no conversation yet, the greeting,
   the suggestions, the input and the modes are one thing - "what do
   you want to do?" - and sit together in the middle of the screen. The
   moment a message exists, the input drops to the bottom where a chat
   keeps it. bays.js and sidebar.js show and hide #emptyState by its
   inline display; watching that attribute here means neither of them
   has to know the layout depends on it.
   ---------------------------------------------------------------- */

function mountEmptyLayout() {
  const main = document.querySelector(".main");
  const empty = document.getElementById("emptyState");
  const log = document.getElementById("chatLog");
  if (!main || !empty || !log) return;

  // The diagram/video bay hides the composer and has its own panel;
  // centring applies only to the chat bays.
  const composer = document.querySelector(".composer");
  const sync = () => {
    const isEmpty = empty.isConnected && empty.style.display !== "none"
      && !log.querySelector(".msg") && !(composer && composer.hidden);
    main.classList.toggle("is-empty", isEmpty);
    // The studio bay has its own controls; the model pill goes with
    // the composer.
    main.classList.toggle("is-studio", !!(composer && composer.hidden));
  };
  const obs = new MutationObserver(sync);
  obs.observe(empty, { attributes: true, attributeFilter: ["style"] });
  obs.observe(log, { childList: true });
  if (composer) obs.observe(composer, { attributes: true, attributeFilter: ["hidden"] });
  sync();
}

/* The + opens a small menu: upload files, upload a folder. The items
   are the buttons attachments.js already listens to, so choosing one
   does exactly what the two separate icons did. */
function mountPlusMenu() {
  const btn = document.getElementById("composerPlus");
  const menu = document.getElementById("composerMenu");
  if (!btn || !menu) return;
  const items = () => /** @type {HTMLElement[]} */ ([...menu.querySelectorAll('[role="menuitem"]')]);

  const setOpen = (open, focusFirst = false) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open && focusFirst) items()[0]?.focus();
  };

  btn.addEventListener("click", () => setOpen(menu.hidden, false));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true, true);
    } else if (e.key === "Escape" && !menu.hidden) {
      // Opened with the mouse, focus is still here, not in the menu.
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
    }
  });

  // Choosing an item closes the menu; attachments.js handles the rest.
  menu.addEventListener("click", (e) => {
    if (/** @type {HTMLElement} */ (e.target).closest('[role="menuitem"]')) setOpen(false);
  });

  menu.addEventListener("keydown", (e) => {
    const list = items();
    const i = list.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      list[(i + step + list.length) % list.length]?.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      btn.focus();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  });

  document.addEventListener("pointerdown", (e) => {
    if (menu.hidden) return;
    const t = /** @type {Node} */ (e.target);
    if (!menu.contains(t) && !btn.contains(t)) setOpen(false);
  });
}

/* A popover that closes on Escape, on an outside click, and on Tab out.
   Shared by the profile menu and the model panel.
   @param {HTMLElement} btn @param {HTMLElement} panel
   @param {{ onOpen?: () => void }} [opts] */
function popover(btn, panel, opts = {}) {
  const { onOpen } = opts;
  const set = (open) => {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open && onOpen) onOpen();
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
  panel.addEventListener("focusout", (e) => {
    const to = /** @type {Node|null} */ (e.relatedTarget);
    if (to && !panel.contains(to) && !btn.contains(to)) set(false);
  });
  return set;
}

/* ---- the profile menu ------------------------------------------------
   The quick look at your account before the full Settings panel: who
   you are, your plan, the theme, and the doors into Settings. */
function mountProfile() {
  const btn = document.getElementById("profileBtn");
  const menu = document.getElementById("profileMenu");
  if (!btn || !menu) return;
  const $ = (id) => document.getElementById(id);

  const render = () => {
    const u = state.currentUser;
    const pro = !!(u && u.plan === "pro");
    const name = u ? (u.nickname || u.name || u.email.split("@")[0]) : "Guest";
    const initial = u ? (name[0] || u.email[0] || "?").toUpperCase() : "?";
    const sub = u ? (pro ? "Pro plan" : "Free plan") : "Sign up to save your chats";
    $("profileName").textContent = name;
    $("profileSub").textContent = sub;
    $("profileAvatar").textContent = initial;
    $("profileMenuName").textContent = u ? u.email : "Guest";
    $("profileMenuSub").textContent = u ? (pro ? "Pro plan" : "Free plan") : "Not signed in";
    $("profileMenuAvatar").textContent = initial;
    $("profileUpgrade").hidden = pro;
    $("profileAuthLabel").textContent = u ? "Log out" : "Sign up or log in";
    btn.classList.toggle("is-pro", pro);

    const theme = readPref(PREF_THEME) || "system";
    menu.querySelectorAll("[data-theme-choice]").forEach((/** @type {HTMLElement} */ b) =>
      b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme)));
  };

  const setOpen = popover(btn, menu, {
    onOpen: () => {
      render();
      /** @type {HTMLElement|null} */ (menu.querySelector("[data-theme-choice][aria-pressed='true']"))?.focus();
    },
  });

  // auth.js rewrites this label whenever the account changes - signed
  // in, out, upgraded - so it is the one signal worth listening to.
  const label = $("settingsBtnLabel");
  if (label) new MutationObserver(render).observe(label, { childList: true, characterData: true, subtree: true });

  menu.querySelectorAll("[data-theme-choice]").forEach((/** @type {HTMLElement} */ b) =>
    b.addEventListener("click", () => {
      writePref(PREF_THEME, b.dataset.themeChoice);
      applyAppearance();
      const select = /** @type {HTMLSelectElement|null} */ ($("themeSelect"));
      if (select) select.value = b.dataset.themeChoice;
      render();
    }));

  const openAt = (tab) => {
    setOpen(false);
    openSettings();
    switchModalTab(tab);
  };
  $("profileUpgrade").addEventListener("click", () => openAt("plan"));
  $("profileAppearance").addEventListener("click", () => openAt("appearance"));
  // #settingsBtn opens Settings through modal.js; this only closes the menu.
  $("settingsBtn").addEventListener("click", () => setOpen(false));
  $("profileAuth").addEventListener("click", () => {
    if (state.currentUser) {
      setOpen(false);
      $("logoutBtn")?.click();
    } else {
      openAt("account");
    }
  });

  // Up and down move through the menu, as in any menu.
  menu.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const list = /** @type {HTMLElement[]} */ (
      [...menu.querySelectorAll('button:not([hidden]), a[role="menuitem"]')]);
    const i = list.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    list[(i + (e.key === "ArrowDown" ? 1 : -1) + list.length) % list.length]?.focus();
  });

  render();
}

/* ---- the model picker ------------------------------------------------
   A pill at the top of the chat naming the model that will answer, and
   a panel with the channels, the models as a list, and Deep search.

   The hidden <select id="modelSelect"> stays the source of truth -
   providers.js fills it, bays.js picks from it, chat.js reads it - so
   this module only draws it and writes back to it. */
function mountModelPicker() {
  const btn = document.getElementById("modelPickerBtn");
  const panel = document.getElementById("modelPanel");
  const select = /** @type {HTMLSelectElement|null} */ (document.getElementById("modelSelect"));
  const list = document.getElementById("modelList");
  const nameEl = document.getElementById("modelPickerName");
  const tag = document.getElementById("modelPickerTag");
  const chatControls = document.getElementById("chatModeControls");
  const strength = document.getElementById("strengthToggle");
  if (!btn || !panel || !select || !list || !nameEl) return;

  const CHECK = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  const cleanName = (opt) => opt.text.replace(/\s+—\s+Pro$/, "");

  const renderLabel = () => {
    if (chatControls && chatControls.hidden) {
      // The image bay: the pill names what will draw it.
      const size = /** @type {HTMLSelectElement|null} */ (document.getElementById("imageSizeSelect"));
      const shape = size && size.selectedOptions[0] ? size.selectedOptions[0].text.split("·")[0].trim() : "";
      nameEl.textContent = "Image" + (shape ? " · " + shape : "");
      if (tag) tag.hidden = true;
      return;
    }
    const opt = select.selectedOptions[0];
    nameEl.textContent = opt && !select.disabled ? cleanName(opt) : (opt ? opt.text : "Model");
    if (tag) tag.hidden = !(strength && strength.getAttribute("aria-checked") === "true");
  };

  const renderList = () => {
    list.replaceChildren();
    [...select.options].forEach((opt) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "model-option";
      item.setAttribute("role", "option");
      const on = opt.value === select.value;
      item.setAttribute("aria-selected", String(on));
      item.tabIndex = on ? 0 : -1;
      item.disabled = opt.disabled || select.disabled;

      const text = document.createElement("span");
      text.className = "model-option-text";
      const name = document.createElement("span");
      name.className = "model-option-name";
      name.textContent = cleanName(opt);
      text.appendChild(name);
      // providers.js puts "id — blurb" on the title; the blurb is the
      // useful half.
      const blurb = (opt.title || "").split(" — ").slice(1).join(" — ");
      if (blurb) {
        const b = document.createElement("span");
        b.className = "model-option-blurb";
        b.textContent = blurb;
        text.appendChild(b);
      }
      item.appendChild(text);
      if (opt.disabled && / — Pro$/.test(opt.text)) {
        const pro = document.createElement("span");
        pro.className = "model-option-pro";
        pro.textContent = "Pro";
        item.appendChild(pro);
      }
      const check = document.createElement("span");
      check.className = "model-option-check";
      check.innerHTML = on ? CHECK : "";
      item.appendChild(check);

      item.addEventListener("click", () => {
        select.value = opt.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        setOpen(false);
        btn.focus();
      });
      list.appendChild(item);
    });
  };

  const refresh = () => { renderLabel(); if (!panel.hidden) renderList(); };

  const setOpen = popover(btn, panel, {
    onOpen: () => {
      renderList();
      /** @type {HTMLElement|null} */ (list.querySelector('[aria-selected="true"]') || panel.querySelector("button:not([disabled])"))?.focus();
    },
  });

  // bays.js sets the value directly (modelSelect.value = preferred),
  // which fires no event. Hooking the setter on this one element is the
  // only way to hear it without editing every caller.
  const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  if (desc && desc.get && desc.set) {
    Object.defineProperty(select, "value", {
      configurable: true,
      get() { return desc.get.call(this); },
      set(v) { desc.set.call(this, v); queueMicrotask(refresh); },
    });
  }
  select.addEventListener("change", refresh);
  const obs = new MutationObserver(refresh);
  obs.observe(select, { childList: true, attributes: true, attributeFilter: ["disabled"] });
  if (chatControls) obs.observe(chatControls, { attributes: true, attributeFilter: ["hidden"] });
  if (strength) obs.observe(strength, { attributes: true, attributeFilter: ["aria-checked"] });
  ["imageSizeSelect", "imageStyleSelect"].forEach((id) =>
    document.getElementById(id)?.addEventListener("change", refresh));

  // Arrow keys through the list.
  list.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = /** @type {HTMLElement[]} */ ([...list.querySelectorAll(".model-option:not(:disabled)")]);
    const i = items.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
  });

  refresh();
}

export function mountChrome() {
  mountEmptyLayout();
  mountPlusMenu();
  mountProfile();
  mountModelPicker();
}
