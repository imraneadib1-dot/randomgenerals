import { state } from "./state.js";
import { renderUserInfo } from "./appearance.js";
import { refreshAuthUI } from "./auth.js";
import { loadSubscription } from "./billing.js";
import { aboutModelName, aboutStatusDot, aboutStatusText, settingsBackdrop, settingsBtn, settingsClose } from "./dom.js";
import { loadMemoryPanel } from "./memory.js";

export const modalTabs = [...document.querySelectorAll(".modal-tab")];

export const modalPanels = {
  account: document.getElementById("accountPanel"),
  general: document.getElementById("generalPanel"),
  plan: document.getElementById("planPanel"),
  memory: document.getElementById("memoryPanel"),
  appearance: document.getElementById("appearancePanel"),
  ai: document.getElementById("aiPanel"),
  notify: document.getElementById("notifyPanel"),
  apps: document.getElementById("appsPanel"),
  data: document.getElementById("dataPanel"),
  usage: document.getElementById("usagePanel"),
  about: document.getElementById("aboutPanel"),
};

export function openSettings() {
  settingsBackdrop.hidden = false;
  refreshAuthUI();
  renderAbout();
  loadMemoryPanel();
  loadSubscription();
  renderUserInfo();
}

export function closeSettings() {
  settingsBackdrop.hidden = true;
}

export function switchModalTab(tab) {
  modalTabs.forEach((btn) =>
    btn.setAttribute(
      "aria-selected",
      btn.dataset.tab === tab ? "true" : "false",
    ),
  );
  Object.entries(modalPanels).forEach(([name, panel]) => {
    panel.hidden = name !== tab;
  });
}

/* ---- About tab ---- */
export function renderAbout() {
  // The pip 1.0 engine when it is up; otherwise whatever stands in for it.
  const engine = state.providers.find((x) => x.id === "pip");
  const p = engine && engine.available
    ? engine
    : state.providers.find((x) => x.id === "ollama") || engine;
  if (!p) {
    aboutStatusDot.className = "about-status-dot";
    aboutStatusText.textContent = "checking…";
    return;
  }
  aboutStatusDot.className =
    "about-status-dot" + (p.available ? " online" : "");
  aboutStatusText.textContent = p.available
    ? (p.id === "pip"
      ? "pip 1.0 is running, responding"
      : "answering from a backup model")
    : p.note || "not reachable";
  aboutModelName.textContent = p.available
    ? (p.id === "pip"
      ? "the engine is up right now"
      : `a backup model, ${p.models[0]}, is answering right now`)
    : "no model detected yet";
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountModal() {
  settingsBtn.addEventListener("click", openSettings);

  settingsClose.addEventListener("click", closeSettings);

  settingsBackdrop.addEventListener("click", (e) => {
    if (e.target === settingsBackdrop) closeSettings();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !settingsBackdrop.hidden) closeSettings();
  });

  modalTabs.forEach((btn) =>
    btn.addEventListener("click", () => switchModalTab(btn.dataset.tab)),
  );

}
