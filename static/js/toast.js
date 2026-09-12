/* ----------------------------------------------------------------
   Toasts - the one way the app says something in passing.

   Before this, feedback was a native alert() (two sites), a native
   confirm() (four), and a dozen little status <p>s inside whichever
   panel happened to have one - so a failed save in Settings was a
   line of grey text under the field, and a failed save anywhere else
   was nothing. There was no aria-live region at all: streamed replies,
   "Copied!", "Saved." were silent to a screen reader.

   One container, two regions: polite for news, assertive for errors,
   so assistive tech hears an error the moment it happens and a
   confirmation when it gets to it. Toasts stack, time out, pause while
   hovered, and can carry one action ("Retry", "Undo").
   ---------------------------------------------------------------- */

const REGIONS = { polite: null, assertive: null };
const DEFAULT_TIMEOUT = { info: 4500, success: 3500, error: 8000 };

function region(kind) {
  const level = kind === "error" ? "assertive" : "polite";
  if (REGIONS[level]) return REGIONS[level];
  let host = document.getElementById("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    host.className = "toasts";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "toast-region";
  el.setAttribute("role", level === "assertive" ? "alert" : "status");
  el.setAttribute("aria-live", level);
  el.setAttribute("aria-relevant", "additions");
  host.appendChild(el);
  REGIONS[level] = el;
  return el;
}

/**
 * Show a message in passing.
 * @param {string} message
 * @param {{kind?: "info"|"success"|"error", action?: {label: string, run: () => void},
 *          timeout?: number, id?: string}} [opts]
 *   `id` de-duplicates: a second toast with the same id replaces the
 *   first instead of stacking under it.
 * @returns {() => void} dismiss
 */
export function toast(message, opts = {}) {
  const kind = opts.kind || "info";
  const host = region(kind);
  if (opts.id) {
    const old = host.querySelector(`[data-toast-id="${CSS.escape(opts.id)}"]`);
    if (old) old.remove();
  }
  const el = document.createElement("div");
  el.className = "toast toast-" + kind;
  if (opts.id) el.dataset.toastId = opts.id;

  const text = document.createElement("div");
  text.className = "toast-text";
  text.textContent = message;
  el.appendChild(text);

  let timer = 0;
  const dismiss = () => {
    if (timer) clearTimeout(timer);
    el.classList.add("is-leaving");
    // The leave animation is 160ms; a listener would fire once per
    // property, and a page with animations off fires none.
    setTimeout(() => el.remove(), 180);
  };

  if (opts.action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = opts.action.label;
    btn.addEventListener("click", () => { dismiss(); opts.action.run(); });
    el.appendChild(btn);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.addEventListener("click", dismiss);
  el.appendChild(close);

  const ms = opts.timeout ?? DEFAULT_TIMEOUT[kind] ?? DEFAULT_TIMEOUT.info;
  const arm = () => { if (ms > 0) timer = window.setTimeout(dismiss, ms); };
  el.addEventListener("mouseenter", () => { if (timer) clearTimeout(timer); timer = 0; });
  el.addEventListener("mouseleave", arm);
  host.appendChild(el);
  arm();
  return dismiss;
}

/* ---- errors nobody caught ---------------------------------------------
   An uncaught exception or a rejected promise used to be a line in a
   console nobody was watching, and the visible symptom was whatever
   stopped happening. Now it is a toast - one, not one per tick: a
   runaway loop must not become a wall of them. */
let lastUncaught = 0;
function reportUncaught(what) {
  const now = Date.now();
  if (now - lastUncaught < 5000) return;
  lastUncaught = now;
  const msg = (what && (what.message || what.reason?.message || String(what.reason || what))) || "Something went wrong.";
  toast("Something went wrong in the page: " + String(msg).slice(0, 140), {
    kind: "error", id: "uncaught",
  });
}

export function installErrorReporting() {
  window.addEventListener("error", (e) => {
    // Resource errors (a blocked script) arrive here too, with no
    // message; those are handled by whoever loads the resource.
    if (e.message) reportUncaught(e);
  });
  window.addEventListener("unhandledrejection", (e) => reportUncaught(e));
}
