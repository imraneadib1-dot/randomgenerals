/* Scroll-driven scene below the hero.
 *
 * A tall section with a pinned viewport inside. How far you have
 * scrolled through the section becomes a 0..1 progress value, which
 * moves a cloudscape and decides which caption is lit.
 *
 * The art comes from cloud-render.js, the same module the hero uses, so
 * the two cannot drift apart.
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
  if (!section || !canvas || !window.RGCloudRender) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const { makeCumulus, drawCloud, drawMoon } = window.RGCloudRender;
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
  // Positions are fractions of the canvas, resolved in layout(), so a
  // resize rearranges the scene instead of leaving it in the old shape.
  const CLOUDS = [
    { fx: 0.16, fy: 0.80, r: 0.150, depth: 1.00, part: -0.16 },
    { fx: 0.40, fy: 0.90, r: 0.115, depth: 0.72, part: -0.08 },
    { fx: 0.62, fy: 0.84, r: 0.170, depth: 1.00, part: 0.14 },
    { fx: 0.86, fy: 0.92, r: 0.130, depth: 0.66, part: 0.22 },
    { fx: 0.28, fy: 1.02, r: 0.100, depth: 0.45, part: -0.05 },
    { fx: 0.74, fy: 1.05, r: 0.120, depth: 0.45, part: 0.10 },
  ];

  const MOONS = [
    // Rises through the whole scroll.
    { fx: 0.78, from: 1.06, to: 0.24, r: 0.075, hue: 36, at: 0.00 },
    // Second moon appears in the back half. Kept to the right of
    // centre and high: at fx 0.20 it rose directly through the
    // headline, which reads as a rendering bug rather than as art.
    { fx: 0.63, from: 1.10, to: 0.17, r: 0.048, hue: 42, at: 0.45 },
  ];

  const clouds = CLOUDS.map((c) => ({
    kind: "cloud",
    x: 0, y: 0, r: 10,
    hue: 8 + Math.random() * 18,
    angle: 0,
    puffs: makeCumulus(),
    spec: c,
  }));

  const moons = MOONS.map((m) => ({
    kind: "moon",
    x: 0, y: 0, r: 10,
    hue: m.hue,
    spec: m,
  }));

  function layout() {
    // Radius scales with the smaller axis so the scene keeps its
    // proportions on a tall phone as well as a wide monitor.
    const unit = Math.min(width, height);
    for (const c of clouds) {
      c.r = Math.max(26, c.spec.r * unit);
      c.ext = null;        // extents are radius-derived; force a re-measure
      c.sprite = null;     // and a re-render of the cached sprite
    }
    for (const m of moons) m.r = Math.max(14, m.spec.r * unit);
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
    // Night at the top, dawn at the bottom. Interpolating two stops is
    // enough; a gradient per frame is cheap, a new canvas is not.
    const warm = ease(p);
    const g = ctx.createLinearGradient(0, 0, 0, height);
    g.addColorStop(0, `hsl(${262 - warm * 24}, ${38 + warm * 6}%, ${5 + warm * 3}%)`);
    g.addColorStop(1, `hsl(${18 - warm * 4}, ${30 + warm * 34}%, ${7 + warm * 12}%)`);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, width, height);

    for (const m of moons) {
      const s = m.spec;
      // Each moon has its own start point in the scroll, so they do not
      // rise in lockstep.
      const local = clamp01((p - s.at) / (1 - s.at));
      m.x = s.fx * width;
      m.y = (s.from + (s.to - s.from) * ease(local)) * height;
      // Fades in as it clears the horizon rather than popping into view.
      const alpha = clamp01(local * 2.2);
      if (alpha <= 0.01) continue;
      ctx.save();
      ctx.globalAlpha = alpha;
      drawMoon(ctx, m);
      ctx.restore();
    }

    for (const c of clouds) {
      const s = c.spec;
      // Parting: clouds slide outward from centre as the scroll advances,
      // then ease back in at the end so the scene closes as it opened.
      const part = Math.sin(warm * Math.PI);
      // Parallax: nearer clouds (higher depth) travel further.
      const lift = warm * height * 0.16 * s.depth;
      c.x = (s.fx + s.part * part) * width;
      c.y = s.fy * height - lift;
      c.angle = 0;
      ctx.save();
      ctx.globalAlpha = 0.92;
      drawCloud(ctx, c);
      ctx.restore();
    }

    // Scrim behind the caption column. The clouds drift wherever the
    // physics of the layout put them, so at some scroll positions the
    // body text was landing on a bright white cloud and becoming
    // genuinely hard to read. A one-sided gradient fixes that without
    // dimming the half of the frame the scene is actually in.
    const scrim = ctx.createLinearGradient(0, 0, width * 0.62, 0);
    scrim.addColorStop(0, "rgba(10, 7, 9, 0.82)");
    scrim.addColorStop(0.55, "rgba(10, 7, 9, 0.45)");
    scrim.addColorStop(1, "rgba(10, 7, 9, 0)");
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
