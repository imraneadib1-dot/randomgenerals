import { state } from "./state.js";
import { accentSwatches, customAccent, skyThemeSelect, userInfoGrid } from "./dom.js";
import { root, updateTimeOfDay } from "./shell.js";

export const ACCENT_PRESETS = [
  { name: "Cyan", value: "#22d3ee" },
  { name: "Blue", value: "#38bdf8" },
  { name: "Violet", value: "#a78bfa" },
  { name: "Green", value: "#34d399" },
  { name: "Amber", value: "#fbbf24" },
  { name: "Rose", value: "#fb7185" },
];

// localStorage can throw outright (Safari private mode, blocked site
// data), not just return null - so every access is guarded and falls
// back to the stylesheet's own defaults rather than breaking boot.
export function readPref(key) {
  try {
    return localStorage.getItem(key);
  } catch (e) {
    return null;
  }
}

export function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* preference just won't persist; the app still works */
  }
}

export function hexToSoft(hex, alpha) {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function applyAccent(hex) {
  // Override every bay's accent, so the choice holds whichever
  // workspace is open rather than being reset by the next selectBay().
  ["code", "chat", "image"].forEach((bay) => {
    root.style.setProperty(`--bay-${bay}`, hex);
    root.style.setProperty(`--bay-${bay}-soft`, hexToSoft(hex, 0.16));
  });
  root.style.setProperty("--bay", hex);
  root.style.setProperty("--bay-soft", hexToSoft(hex, 0.16));
  if (customAccent) customAccent.value = hex;
  [...accentSwatches.querySelectorAll(".swatch")].forEach((b) =>
    b.setAttribute(
      "aria-pressed",
      String(b.dataset.value.toLowerCase() === hex.toLowerCase()),
    ),
  );
}

export function renderSwatches() {
  accentSwatches.innerHTML = "";
  ACCENT_PRESETS.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch";
    b.dataset.value = p.value;
    b.style.background = p.value;
    b.title = p.name;
    b.setAttribute("aria-label", `${p.name} accent`);
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => {
      applyAccent(p.value);
      writePref("accent", p.value);
    });
    accentSwatches.appendChild(b);
  });
}

export function renderUserInfo() {
  const rows = state.currentUser
    ? [
        ["Email", state.currentUser.email],
        ["Plan", state.currentUser.plan === "pro" ? "Pro" : "Free"],
        [
          "Member since",
          new Date(state.currentUser.created).toLocaleDateString(undefined, {
            year: "numeric",
            month: "long",
            day: "numeric",
          }),
        ],
        ["Account ID", state.currentUser.id],
      ]
    : [["Signed in", "Not signed in — using a guest session"]];

  userInfoGrid.innerHTML = "";
  rows.forEach(([label, value]) => {
    const l = document.createElement("span");
    l.className = "userinfo-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "userinfo-value";
    v.textContent = value;
    userInfoGrid.appendChild(l);
    userInfoGrid.appendChild(v);
  });
}

export function initAppearance() {
  renderSwatches();
  const savedAccent = readPref("accent");
  if (savedAccent) applyAccent(savedAccent);
  const savedSky = readPref("skyTheme");
  if (savedSky) skyThemeSelect.value = savedSky;
}

/* ----------------------------------------------------------------
   Appearance

   The theme is applied inline in <head> before first paint - see the
   note there. This handles the controls and keeps them in step.

   "System" is a live subscription, not a one-off read: someone whose
   machine flips to dark at sunset should see this follow, and only the
   media query knows when that happens.
   ---------------------------------------------------------------- */
export const PREF_THEME = "theme";

export const PREF_CONTRAST = "contrast";

export const PREF_TEXTSIZE = "textSize";

export function resolvedTheme() {
  const choice = readPref(PREF_THEME) || "system";
  if (choice !== "system") return choice;
  return window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

export function applyAppearance() {
  const root = document.documentElement;
  if (resolvedTheme() === "light") root.setAttribute("data-theme", "light");
  else root.removeAttribute("data-theme");

  const contrast = readPref(PREF_CONTRAST) || "normal";
  if (contrast === "high") root.setAttribute("data-contrast", "high");
  else root.removeAttribute("data-contrast");

  const size = readPref(PREF_TEXTSIZE) || "medium";
  if (size !== "medium") root.setAttribute("data-textsize", size);
  else root.removeAttribute("data-textsize");
}

export function initAppearanceControls() {
  const theme = document.getElementById("themeSelect");
  const contrast = document.getElementById("contrastSelect");
  const size = document.getElementById("textSizeSelect");

  if (theme) {
    theme.value = readPref(PREF_THEME) || "system";
    theme.addEventListener("change", () => {
      writePref(PREF_THEME, theme.value);
      applyAppearance();
    });
  }
  if (contrast) {
    contrast.value = readPref(PREF_CONTRAST) || "normal";
    contrast.addEventListener("change", () => {
      writePref(PREF_CONTRAST, contrast.value);
      applyAppearance();
    });
  }
  if (size) {
    size.value = readPref(PREF_TEXTSIZE) || "medium";
    size.addEventListener("change", () => {
      writePref(PREF_TEXTSIZE, size.value);
      applyAppearance();
    });
  }

  // Follow the system while "System" is selected. Without this, choosing
  // System means "whatever it was when the tab opened", which is not
  // what the word promises.
  if (window.matchMedia) {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if ((readPref(PREF_THEME) || "system") === "system") applyAppearance();
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountAppearance() {
  customAccent.addEventListener("input", () => {
    applyAccent(customAccent.value);
    writePref("accent", customAccent.value);
  });

  skyThemeSelect.addEventListener("change", () => {
    writePref("skyTheme", skyThemeSelect.value);
    updateTimeOfDay();
  });

  applyAppearance();

  initAppearanceControls();

}
