import { state } from "./state.js";
import { initAppearance, readPref, writePref } from "./appearance.js";
import { updateEmptyState } from "./bays.js";
import { handleAuthReturn, handleCheckoutReturn, handleSearchHandoff, loadAuthState, loadPlansMeta } from "./billing.js";
import { loadCredits } from "./credits.js";
import { bootLabel, bootScreen, emptyState } from "./dom.js";
import { loadProviders } from "./providers.js";
import { patchSettingsDebounced } from "./settings.js";
import { loadThreadList } from "./sidebar.js";
import { BAY_ORDER } from "./shell.js";
import { selectBay } from "./bays.js";

/** Name the fourth tab after what that bay is currently doing.
 *
 * It is not a video bay with a diagram fallback - diagram is the FLOOR,
 * and on a deployment with no usable media backend it is the only thing
 * that bay ever does. A fixed label is therefore wrong most of the time
 * whichever word is chosen: "Video" on a machine that only draws
 * diagrams, or "Diagram" above a panel headed "Make a video".
 *
 * So the tab says what the panel underneath it says.
 */
export function updateGenBayLabel(kind) {
  const label = document.getElementById("genBayLabel");
  const icon = document.getElementById("genBayIcon");
  if (label) {
    label.textContent =
      kind === "model" ? "3D" : kind === "video" ? "Video" : "Diagram";
  }
  if (icon) {
    icon.textContent = kind === "model" ? "◆" : kind === "video" ? "▶" : "◇";
  }
}

/** Take the splash down. Safe to call twice; the second call is a no-op. */
export function dismissBoot() {
  if (!bootScreen || bootScreen.hidden) return;
  bootScreen.classList.add("boot-done");
  setTimeout(() => (bootScreen.hidden = true), 200);
}

export async function boot() {
  // A splash screen that can outlive its own script is a locked door.
  // Whatever happens below - a loader rejecting, a network that never
  // answers, an exception in code that has not been written yet - this
  // timer takes the screen down and lets the person use the app.
  const deadLetter = setTimeout(dismissBoot, 12000);

  try {
    // NO MINIMUM HOLD. The splash used to stay up for at least 650ms
    // and then fade for 500ms, so every arrival spent over a second
    // looking at a logo whatever the network did. The site is the
    // chat; the splash exists only to cover the moment before the
    // first data lands, and it comes down the moment it has.
    const minHold = Promise.resolve();
    if (bootLabel) bootLabel.textContent = "reaching the local model…";
    initAppearance();
    // ?bay=code from the installed app's shortcuts (manifest.webmanifest)
    // was never read; the shortcut opened the default bay. Honoured here,
    // before anything loads for the wrong one, and then taken off the
    // URL so a reload does not force it again.
    const params = new URLSearchParams(window.location.search);
    const askedBay = params.get("bay");
    if (askedBay && BAY_ORDER.includes(askedBay) && askedBay !== state.currentBay) {
      selectBay(askedBay);
      params.delete("bay");
      window.history.replaceState({}, "", window.location.pathname
        + (params.toString() ? "?" + params : ""));
    }
    handleCheckoutReturn();
    handleAuthReturn();
    handleSearchHandoff();

    // allSettled, not all: one endpoint being down degrades the app,
    // it does not justify refusing to show it. Promise.all rejects on
    // the first failure and skipped the two lines that hide the splash.
    await Promise.allSettled([
      loadProviders(),
      loadThreadList(),
      loadCredits(),
      loadAuthState(),
      loadPlansMeta(),
      minHold,
    ]);
  } catch (e) {
    console.error("boot failed", e);
  } finally {
    clearTimeout(deadLetter);
    dismissBoot();
  }
}

/* ----------------------------------------------------------------
   Greeting
   ---------------------------------------------------------------- */
export const NAME_KEY = "displayName";

export function greetingFor(hour) {
  if (hour < 5) return "Still up";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** The three-ring mark, drawn rather than loaded.
 *
 *  Inline SVG rather than <img src="logo.svg">: the file strokes with
 *  currentColor so it themes with everything around it, and an <img>
 *  gets its own document where currentColor resolves to black.
 *
 *  Geometry is the same as static/logo.svg and tools/make_icons.py -
 *  three internally tangent circles, each touching the one containing
 *  it on the OPPOSITE side, which is what makes the gap sweep round
 *  into a coil instead of stacking into a crescent.
 */
export function brandMark() {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 200 200");
  svg.setAttribute("class", "empty-mark");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "6.5");
  for (const [cy, r] of [
    [100, 88],
    [69, 57],
    [93, 33],
  ]) {
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("cx", "100");
    c.setAttribute("cy", String(cy));
    c.setAttribute("r", String(r));
    svg.appendChild(c);
  }
  return svg;
}

/** Ask for a name, only once somebody has asked to be asked. */
export function promptForName(host) {
  host.replaceChildren();
  host.append(brandMark());
  const row = document.createElement("div");
  row.className = "name-prompt";
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 40;
  input.placeholder = "Your name";
  input.setAttribute("aria-label", "Your name");
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save";
  const commit = () => {
    const v = input.value.trim();
    if (v) {
      writePref(NAME_KEY, v);
      // And to the account, so it is there on the next device rather
      // than only in this browser.
      state.accountNickname = v;
      if (typeof patchSettingsDebounced === "function") {
        patchSettingsDebounced({ nickname: v });
      }
      const field = document.getElementById("profileNickname");
      if (field) field.value = v;
    }
    renderGreeting();
  };
  save.addEventListener("click", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commit();
    // Escape backs out without setting anything, so the offer is not a
    // trap once taken up.
    if (e.key === "Escape") renderGreeting();
  });
  row.append(input, save);
  host.append(row);
  input.focus();
}

export function renderGreeting() {
  if (!emptyState) return;
  let host = document.getElementById("greetingHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "greetingHost";
    // Above the eyebrow, so the greeting is the first thing read rather
    // than an afterthought under the bay title.
    emptyState.insertBefore(host, emptyState.firstChild);
  }
  host.replaceChildren();

  host.append(brandMark());

  // THE ACCOUNT FIRST, the browser second. This used to read only
  // localStorage, so somebody who set their name here was a stranger
  // again on their phone - and the desktop app, being a different
  // browser profile entirely, never knew it at all. The local value is
  // kept as a fallback for signed-out visitors, who have nowhere else
  // to put it.
  const name = (state.accountNickname || readPref(NAME_KEY) || "").trim();
  if (!name) {
    // NO LONGER A QUESTION YOU HAVE TO ANSWER.
    //
    // This used to be "What should I call you?" with a text field and a
    // Save button - a form standing between somebody and the thing they
    // opened the app to do, asking them to give something up before it
    // had done anything for them. The name is a nicety; it was being
    // collected like a requirement.
    //
    // Now it greets and gets out of the way. Anyone who wants to be
    // called something can say so, and the offer sits under the
    // greeting at the weight of an aside rather than a gate.
    const h = document.createElement("h2");
    h.className = "greeting";
    h.textContent = greetingFor(new Date().getHours());
    const ask = document.createElement("button");
    ask.type = "button";
    ask.className = "greeting-edit";
    ask.textContent = "add your name";
    ask.addEventListener("click", () => promptForName(host));
    host.append(h, ask);
    return;
  }

  const h = document.createElement("h2");
  h.className = "greeting";
  // textContent, never innerHTML - this string is whatever the user
  // typed, and it is re-rendered on every empty state.
  h.textContent = `${greetingFor(new Date().getHours())}, ${name}`;
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "greeting-edit";
  edit.textContent = "not you?";
  edit.addEventListener("click", () => {
    writePref(NAME_KEY, "");
    renderGreeting();
  });
  host.append(h, edit);
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountBoot() {
  /* ---------------------------------------------------------------- */
  // Guarded because it runs on the line before boot(). An exception here
  // used to mean boot() was never reached at all - the app sat on its
  // splash screen and nothing said why.
  try {
    updateEmptyState();
  } catch (e) {
    console.error("initial empty state failed", e);
  }

}
