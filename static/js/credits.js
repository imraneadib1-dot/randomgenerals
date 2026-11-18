import { creditBarFill, creditCount } from "./dom.js";

/* ----------------------------------------------------------------
   Credit meter
   ---------------------------------------------------------------- */
export function renderCredits(data) {
  if (!data || typeof data.balance !== "number") return;
  const pct = data.starting
    ? Math.max(0, Math.min(100, (data.balance / data.starting) * 100))
    : 0;
  creditBarFill.style.width = pct + "%";
  creditBarFill.classList.remove("mid", "low");
  if (pct <= 15) creditBarFill.classList.add("low");
  else if (pct <= 45) creditBarFill.classList.add("mid");
  creditCount.textContent =
    `${data.balance.toLocaleString()} / ${(data.starting || 0).toLocaleString()}`;
  // Settings > Usage says when it fills back up.
  const refill = document.getElementById("usageRefill");
  if (refill) {
    const s = Number(data.next_refill_in);
    refill.textContent = Number.isFinite(s) && data.balance < data.starting
      ? `Refills to ${(data.starting || 0).toLocaleString()} in ${duration(s)}.`
      : "Full.";
  }
}

/** 7260 -> "2h 1m", 300 -> "5m". */
function duration(seconds) {
  const m = Math.max(1, Math.round(seconds / 60));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.round(h / 24)} days`;
}

export async function loadCredits() {
  try {
    renderCredits(await (await fetch("/api/credits")).json());
  } catch (err) {
    creditCount.textContent = "—";
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountCredits() {
  // nothing ran at load in this section
}
