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
  creditCount.textContent = `${data.balance} / ${data.starting}`;
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
