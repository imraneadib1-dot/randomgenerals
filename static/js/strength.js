import { state } from "./state.js";
import { clockEl, strengthToggle } from "./dom.js";

// The clock was removed from the sidebar - the operating system already
// has one, and a second-by-second ticker was the busiest thing on an
// otherwise idle screen. The guard stays rather than the function being
// deleted, because the element is optional now: a layout that wants a
// clock back only has to add the span.
export function tickClock() {
  if (!clockEl) return;
  clockEl.textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return "now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d`;
  return `${Math.round(d / 30)}mo`;
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountStrength() {
  strengthToggle.addEventListener("click", () => {
    state.currentStrength = state.currentStrength === "quick" ? "deep" : "quick";
    strengthToggle.setAttribute(
      "aria-checked",
      String(state.currentStrength === "deep"),
    );
    // Deliberately NOT switching models here any more. This app's ~8GB of
    // VRAM can only hold one multi-GB model at a time, so swapping models
    // means evicting one and cold-loading another - measured at ~39s,
    // against ~1.4s once a model is already warm. Tying model choice to
    // this toggle meant flipping Quick/Deep mid-session paid that cost
    // every time, which made the "too slow" complaint this was supposed
    // to fix worse instead of better. Quick/Deep now only changes the
    // prompt/decoding options; the model stays whatever it already was.
  });

  if (clockEl) {
    tickClock();
    setInterval(tickClock, 1000);
  }

}
