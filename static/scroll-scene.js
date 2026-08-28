/* Scroll-driven scene below the hero.
 *
 * A tall section with a pinned viewport inside. How far you have
 * scrolled through the section becomes a 0..1 progress value, which
 * moves a cloudscape and decides which caption is lit.
 *
 * The art comes from desert-render.js, the same module the hero uses,
 * so the two cannot drift apart.
 *
 * WHY NOT IntersectionObserver OR requestAnimationFrame
 * Neither fired reliably when this page was tested. IO never reported at
 * all, and rAF callbacks were dropped, which took the reveal animations
 * with them. Both failures are silent and both leave content invisible,
 * so this file uses a scroll listener with a timer throttle plus a slow
 * poll - the combination that was actually verified to work here.
 *
 * The section is readable without any of this: the beats are visible in
 * the markup by default and only become scroll-driven once .scene-live
 * is added below.
 */
(function () {
  "use strict";

  const section = document.getElementById("scene");
  const canvas = document.getElementById("sceneCanvas");
  if (!section || !canvas || !window.RGDesert) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const D = window.RGDesert;
  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  const beats = Array.from(section.querySelectorAll(".scene-beat"));

  // Taking over only once everything needed is present means a failure
  // above this line leaves the plain stacked list rather than an empty
  // pinned box.
  section.classList.add("scene-live");

  let width = 0;
  let height = 0;
  // Capped at 2: beyond that the extra pixels are invisible and the
  // fill cost rises with their square.
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resize() {
    const r = canvas.getBoundingClientRect();
    width = Math.max(1, Math.round(r.width));
    height = Math.max(1, Math.round(r.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layout();
  }

  /* ---- the cast ---------------------------------------------------- */
  // Back to front. Distant dunes are lighter and less saturated because
  // atmosphere washes out contrast with distance; reproducing that is
  // most of what makes a layer read as far away rather than just higher.
  const dunes = [
    D.makeDune({ base: 0.60, scale: 0.75, depth: 0.10, hue: 28, sat: 30, light: 26 }),
    D.makeDune({ base: 0.70, scale: 0.95, depth: 0.26, hue: 26, sat: 36, light: 21 }),
    D.makeDune({ base: 0.81, scale: 1.15, depth: 0.52, hue: 24, sat: 42, light: 16 }),
    D.makeDune({ base: 0.94, scale: 1.35, depth: 1.00, hue: 22, sat: 46, light: 11 }),
  ];

  const stars = D.makeStars(90);

  function layout() {
    // Nothing size-dependent to precompute: dune curves are evaluated in
    // canvas units at draw time, so a resize needs no rebuild. Kept as a
    // function because resize() calls it and a later layer may need it.
  }

  /* ---- progress ---------------------------------------------------- */
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  // Ease so the beats hold at their marks and move quickly between them,
  // rather than drifting at a constant rate the whole way down.
  const ease = (t) => t * t * (3 - 2 * t);

  function progress() {
    const r = section.getBoundingClientRect();
    const travel = r.height - window.innerHeight;
    if (travel <= 0) return 0;
    return clamp01(-r.top / travel);
  }

  function onScreen() {
    const r = section.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight;
  }

  /* ---- drawing ----------------------------------------------------- */
  function paint(p) {
    const warm = ease(p);
    const t = performance.now() / 1000;

    ctx.fillStyle = D.skyGradient(ctx, width, height, warm);
    ctx.fillRect(0, 0, width, height);

    // Stars fade as the sun comes up. Squared so they linger through
    // early dawn and then go quickly, which is how it actually looks.
    D.drawStars(ctx, stars, width, height, Math.pow(1 - warm, 2) * 0.75, t);

    // The sun crosses and climbs: it enters low on the left and ends
    // high on the right, so the scene has direction rather than a disc
    // rising straight up out of the middle.
    const sunX = width * (0.18 + 0.62 * p);
    const sunY = height * (1.02 - 0.72 * ease(p));
    D.drawSun(ctx, sunX, sunY, Math.max(18, Math.min(width, height) * 0.062), {
      hue: 30 + warm * 8,
      alpha: 1,
    });

    for (const d of dunes) {
      // Parallax across the scroll. Near dunes travel several times as
      // far as distant ones - the whole depth illusion is this one line.
      const shift = p * 0.09 * d.depth;
      D.drawDune(ctx, d, width, height, shift, warm);
    }

    // Scrim behind the caption column. Sunlit dune crests get bright
    // enough that body text on top of them stops being readable; a
    // one-sided gradient fixes it without dimming the half of the frame
    // the scene actually lives in.
    const scrim = ctx.createLinearGradient(0, 0, width * 0.62, 0);
    scrim.addColorStop(0, "rgba(18, 13, 10, 0.84)");
    scrim.addColorStop(0.55, "rgba(18, 13, 10, 0.46)");
    scrim.addColorStop(1, "rgba(18, 13, 10, 0)");
    ctx.fillStyle = scrim;
    ctx.fillRect(0, 0, width * 0.62, height);
  }

  /* ---- captions ---------------------------------------------------- */
  let activeBeat = -1;

  function setBeat(p) {
    if (!beats.length) return;
    // Bias slightly forward so a beat lights as its scene arrives rather
    // than a moment after.
    const i = Math.min(beats.length - 1, Math.floor(p * beats.length + 0.08));
    if (i === activeBeat) return;
    activeBeat = i;
    beats.forEach((b, n) => b.classList.toggle("is-active", n === i));
  }

  /* ---- loop -------------------------------------------------------- */
  function render() {
    const p = progress();
    paint(p);
    setBeat(p);
  }

  if (reduced) {
    // One static frame, mid-scene, and every caption left visible by CSS.
    resize();
    paint(0.5);
    beats.forEach((b) => b.classList.add("is-active"));
    return;
  }

  let queued = false;
  function request() {
    if (queued) return;
    queued = true;
    window.setTimeout(() => {
      queued = false;
      if (onScreen()) render();
    }, 40);
  }

  window.addEventListener("scroll", request, { passive: true });
  window.addEventListener("resize", () => {
    resize();
    if (onScreen()) render();
  });

  // The poll covers scrolling the listener cannot see - anchor jumps,
  // find-in-page, and the environments where scroll events simply do not
  // arrive. It only paints while the section is on screen, so it costs
  // one bounding-box read a second otherwise.
  window.setInterval(() => {
    if (onScreen()) render();
  }, 250);

  resize();
  render();
})();
