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

/* ----------------------------------------------------------------
   Motion
   ----------------------------------------------------------------
   Two effects, both cheap: reveal-on-scroll, and a glow that follows
   the cursor across a card.

   Everything is skipped entirely under prefers-reduced-motion. The
   reveal is skipped by never adding the class that hides things, so
   the page is simply static rather than static-and-invisible.
   ---------------------------------------------------------------- */
(function motion() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- reveal on scroll ---- */
  //
  // A scroll listener rather than IntersectionObserver. IO is the usual
  // advice and is cheaper in principle, but it failed to fire at all
  // under test here, and its failure mode is the worst available: the
  // class that hides the element is applied, the callback that would
  // show it never runs, and eighteen sections stay permanently blank -
  // which looks like a page with no content rather than like a bug.
  //
  // A geometry check on scroll cannot fail that way. It is throttled to
  // one measurement per animation frame, reads a single bounding box
  // per not-yet-revealed element, and unregisters itself once they have
  // all been shown, so the steady-state cost is nothing.
  const targets = Array.from(
    document.querySelectorAll(
      ".section-head, .card, .price-card, .step, .faq-list details, .cta-band",
    ),
  );

  if (!reduced && targets.length) {
    targets.forEach((el) => {
      const siblings = Array.from(el.parentElement?.children || []);
      // Stagger by position within the row, so a grid arrives as a wave.
      // Capped - past a handful the delay stops reading as choreography
      // and starts reading as the page being slow.
      const i = Math.min(siblings.indexOf(el), 5);
      el.style.transitionDelay = `${i * 70}ms`;
      el.classList.add("reveal");
    });

    let pending = targets.slice();
    let queued = false;

    const check = () => {
      queued = false;
      const h = window.innerHeight;
      pending = pending.filter((el) => {
        const r = el.getBoundingClientRect();
        // Slightly inside the viewport, so an element finishes arriving
        // as it comes into view rather than starting once already there.
        if (r.top < h * 0.88 && r.bottom > 0) {
          el.classList.add("is-visible");
          return false;
        }
        return true;
      });
      if (!pending.length) {
        window.removeEventListener("scroll", request);
        window.removeEventListener("resize", request);
      }
    };

    // setTimeout rather than requestAnimationFrame. rAF is the usual
    // choice and pauses politely in background tabs, but it did not run
    // at all under test here, so every throttled check after the first
    // was silently dropped. A 60ms timer is coarser than a frame and
    // entirely sufficient: the CSS transition does the smoothing, this
    // only decides when to start it.
    const request = () => {
      if (queued) return;
      queued = true;
      window.setTimeout(check, 60);
    };

    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request, { passive: true });
    check();   // whatever is already on screen at load

    // A slow poll alongside the scroll listener, because .reveal sets
    // opacity:0 and anything the listener misses stays permanently
    // invisible - a failure that looks like an empty section rather
    // than like a bug, which is the hardest kind to notice.
    //
    // It also covers scrolling this listener cannot see: anchor jumps,
    // find-in-page, a container that scrolls instead of the window.
    // Three checks a second costs one bounding box per hidden element
    // and stops entirely once they are all shown.
    const poll = window.setInterval(() => {
      if (!pending.length) {
        window.clearInterval(poll);
        return;
      }
      check();
    }, 350);

    // Last resort. If nothing has revealed after fifteen seconds then
    // the geometry check is wrong about this page in some way I did not
    // anticipate, and showing the content unanimated is obviously
    // better than never showing it.
    window.setTimeout(() => {
      pending.forEach((el) => el.classList.add("is-visible"));
      pending = [];
      window.clearInterval(poll);
    }, 15000);
  }

  /* ---- cursor-following glow ---- */
  if (!reduced && window.matchMedia("(hover: hover)").matches) {
    const cards = document.querySelectorAll(".card, .price-card");
    cards.forEach((card) => {
      // Listener on the card, not the document: this only needs to run
      // while a pointer is actually over one, and a document-level
      // mousemove would fire for the entire page.
      card.addEventListener("pointermove", (e) => {
        const r = card.getBoundingClientRect();
        card.style.setProperty("--mx", `${e.clientX - r.left}px`);
        card.style.setProperty("--my", `${e.clientY - r.top}px`);
      });
    });
  }
})();

/* ----------------------------------------------------------------
   Hero video
   ----------------------------------------------------------------
   Only revealed once it is genuinely playing. The element starts at
   opacity 0 over the drawn dune scene, so a slow or failed load leaves
   the canvas showing rather than flashing a black rectangle over it.

   Autoplay is refused in more situations than people expect - data
   saver, battery saver, some corporate policies - and the promise
   rejects rather than throwing. Unhandled, that is a console error and
   a permanently invisible video.
   ---------------------------------------------------------------- */
function setupBackgroundVideo(id) {
  const v = document.getElementById(id);
  if (!v) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) return;   // CSS hides it; do not fetch the file either

  const reveal = () => v.classList.add("is-ready");

  // Check the state before subscribing, not only after.
  //
  // The autoplay attribute starts playback during parsing, so by the
  // time this runs the video is frequently already playing - which means
  // `playing` and `canplay` have already fired and one-shot listeners
  // wait forever for events that will not come again. The element then
  // sits at opacity 0 permanently while decoding happily underneath:
  // readyState 4, not paused, time advancing, and invisible.
  //
  // readyState >= 2 is HAVE_CURRENT_DATA - there is a frame to show.
  function revealIfReady() {
    if (v.readyState >= 2 || !v.paused || v.currentTime > 0) {
      reveal();
      return true;
    }
    return false;
  }

  if (!revealIfReady()) {
    v.addEventListener("playing", reveal, { once: true });
    v.addEventListener("canplay", reveal, { once: true });
    v.addEventListener("loadeddata", reveal, { once: true });
    // Last resort: if none of those arrive but the video is fine, a
    // permanently hidden video is worse than an unfaded one.
    window.setTimeout(revealIfReady, 2500);
  }

  const attempt = v.play();
  if (attempt && typeof attempt.catch === "function") {
    attempt.catch(() => {
      // Autoplay refused. The canvas scene is already behind it and
      // looks complete, so this needs no message - it just stays hidden.
    });
  }

  // Pause off-screen. A looping video decoding behind three screens of
  // other content is pure battery cost.
  let queued = false;
  function sync() {
    const r = v.getBoundingClientRect();
    const onScreen = r.bottom > 0 && r.top < window.innerHeight;
    if (onScreen && !document.hidden) {
      if (v.paused) v.play().catch(() => {});
    } else if (!v.paused) {
      v.pause();
    }
  }
  window.addEventListener("scroll", () => {
    if (queued) return;
    queued = true;
    window.setTimeout(() => { queued = false; sync(); }, 150);
  }, { passive: true });
  document.addEventListener("visibilitychange", sync);
}

// Both background videos, same handling. The scene one matters more for
// the off-screen pause: it sits three screens down, so without it the
// browser decodes a looping clip nobody is looking at for as long as the
// page is open.
setupBackgroundVideo("heroVideo");
setupBackgroundVideo("sceneVideo");
