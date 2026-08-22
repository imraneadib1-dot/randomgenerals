/* Landing-page behaviour: scroll progress, back-to-top, mobile drawer,
   cookie notice, and UTM capture. All progressive enhancement - the page
   is fully readable and navigable with this file blocked. */

/* localStorage throws outright in some privacy modes rather than just
   returning null, so every access goes through these. */
function readPref(key) {
  try {
    return localStorage.getItem(key);
  } catch (e) {
    return null;
  }
}
function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* preference simply won't persist */
  }
}

/* ---- Scroll progress bar ---- */
const scrollProgress = document.getElementById("scrollProgress");
const scrollTopBtn = document.getElementById("scrollTop");

function onScroll() {
  const doc = document.documentElement;
  const max = doc.scrollHeight - doc.clientHeight;
  const pct = max > 0 ? (doc.scrollTop / max) * 100 : 0;
  scrollProgress.style.width = `${pct}%`;
  scrollTopBtn.hidden = doc.scrollTop < 300;
}

// rAF-throttled: scroll fires far more often than the screen repaints,
// and doing layout reads on every event is the classic way to make a
// page feel heavy while scrolling.
let ticking = false;
window.addEventListener(
  "scroll",
  () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      onScroll();
      ticking = false;
    });
  },
  { passive: true },
);
onScroll();

scrollTopBtn.addEventListener("click", () => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });
});

/* ---- Mobile drawer ---- */
const hamburger = document.getElementById("hamburger");
const drawer = document.getElementById("mobileDrawer");
const drawerBackdrop = document.getElementById("drawerBackdrop");

function setDrawer(open) {
  drawer.hidden = !open;
  drawerBackdrop.hidden = !open;
  hamburger.setAttribute("aria-expanded", String(open));
  hamburger.classList.toggle("is-open", open);
  hamburger.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  document.body.style.overflow = open ? "hidden" : "";
}

hamburger.addEventListener("click", () => setDrawer(drawer.hidden));
drawerBackdrop.addEventListener("click", () => setDrawer(false));
drawer.addEventListener("click", (e) => {
  // Any in-drawer link means navigation is happening - close behind it.
  if (e.target.tagName === "A") setDrawer(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !drawer.hidden) {
    setDrawer(false);
    hamburger.focus();
  }
});

/* ---- Cookie notice ---- */
const cookieBanner = document.getElementById("cookieBanner");

if (!readPref("cookieChoice")) {
  cookieBanner.hidden = false;
}
function closeCookies(choice) {
  writePref("cookieChoice", choice);
  cookieBanner.hidden = true;
}
document
  .getElementById("cookieAccept")
  .addEventListener("click", () => closeCookies("accepted"));
document
  .getElementById("cookieDecline")
  .addEventListener("click", () => closeCookies("essential"));

/* ---- UTM capture ----
   Stored, not transmitted: this app has no analytics backend, so the
   honest thing is to keep attribution parameters in this browser's own
   session storage where a future signup flow can read them, rather than
   implying they're being reported somewhere they aren't. First touch
   wins - overwriting on a later visit would credit the wrong source. */
(function captureUtm() {
  const params = new URLSearchParams(window.location.search);
  const keys = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
  ];
  const found = {};
  keys.forEach((k) => {
    const v = params.get(k);
    if (v) found[k] = v;
  });
  if (!Object.keys(found).length) return;
  try {
    if (!sessionStorage.getItem("utm")) {
      found.landing_path = window.location.pathname;
      found.captured_at = new Date().toISOString();
      sessionStorage.setItem("utm", JSON.stringify(found));
    }
  } catch (e) {
    /* nothing to do - attribution is best-effort */
  }
})();
