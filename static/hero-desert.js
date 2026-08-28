/* Hero: a Sahara horizon behind the headline.
 *
 * Replaces hero-physics.js, which threw draggable moons and clouds
 * around. Those are gone with the rest of that theme.
 *
 * WHAT MOVES
 * The dunes drift very slowly on their own, and lean toward the pointer.
 * Both are parallax on layered ridgelines: the near dunes move several
 * times as far as the distant ones, which is what reads as depth on a
 * flat image. Nothing is thrown, dragged or simulated - sand does not
 * behave like a ball, and pretending otherwise looked like a toy.
 *
 * THE SAFE ZONE
 * The old version had to keep shapes away from the headline, because a
 * moon drifting across the text made it unreadable. A horizon cannot
 * wander into the text, so that whole problem disappears - the dunes sit
 * along the bottom by construction.
 */
(function () {
  const canvas = document.getElementById("heroCanvas");
  if (!canvas || !window.RGDesert) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  const D = window.RGDesert;

  let width = 0;
  let height = 0;
  // Capped at 2: past that the extra pixels are invisible and the fill
  // cost rises with their square.
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  // Back to front. Distant dunes are lighter and hazier - atmosphere
  // washes out contrast with distance, and reproducing that is most of
  // what makes layers read as far away rather than merely higher up.
  const dunes = [
    D.makeDune({ base: 0.64, scale: 0.75, depth: 0.10, hue: 28, sat: 30, light: 26 }),
    D.makeDune({ base: 0.73, scale: 0.95, depth: 0.26, hue: 26, sat: 36, light: 21 }),
    D.makeDune({ base: 0.83, scale: 1.15, depth: 0.52, hue: 24, sat: 42, light: 16 }),
    D.makeDune({ base: 0.95, scale: 1.35, depth: 1.00, hue: 22, sat: 46, light: 11 }),
  ];

  const stars = D.makeStars(70);

  function resize() {
    const r = canvas.getBoundingClientRect();
    width = Math.max(1, Math.round(r.width));
    height = Math.max(1, Math.round(r.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /* ---- pointer ----------------------------------------------------- */
  // Target and current are separate so the scene eases toward the
  // pointer instead of snapping. Snapping to raw mouse position is the
  // single thing that makes a parallax effect feel cheap.
  let targetLean = 0;
  let lean = 0;

  window.addEventListener("pointermove", (e) => {
    const r = canvas.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) return;
    // -1 .. 1 across the canvas.
    targetLean = ((e.clientX - r.left) / (r.width || 1)) * 2 - 1;
  }, { passive: true });

  /* ---- draw -------------------------------------------------------- */
  // Dusk. Fixed rather than tied to the clock: the palette around it is
  // a fixed dusk too, and a hero that is pale blue at 2pm would not
  // match the page it sits in.
  const WARM = 0.34;

  function draw(t) {
    ctx.clearRect(0, 0, width, height);

    ctx.fillStyle = D.skyGradient(ctx, width, height, WARM);
    ctx.fillRect(0, 0, width, height);

    D.drawStars(ctx, stars, width, height, 0.5, t);

    // Down at the horizon, not up in the copy. At 0.52 it landed on the
    // sub-headline and blotted out a word. Sitting just above the back
    // dune also lets the ridgelines cut across it, since the dunes are
    // drawn after this - which is both more convincing and the reason
    // the collision cannot come back if the text reflows.
    const sunX = width * (0.78 + lean * 0.012);
    const sunY = height * 0.63;
    D.drawSun(ctx, sunX, sunY, Math.max(16, Math.min(width, height) * 0.052), {
      hue: 34,
      alpha: 0.95,
    });

    for (const d of dunes) {
      // Two inputs to the same shift: a slow constant drift so the scene
      // is never entirely still, and the pointer lean.
      const drift = t * 0.004 * d.depth;
      const shift = drift + lean * 0.045 * d.depth;
      D.drawDune(ctx, d, width, height, shift, 0);
    }
  }

  /* ---- loop -------------------------------------------------------- */
  let raf = null;
  let running = false;
  let t0 = performance.now();

  function frame() {
    const t = (performance.now() - t0) / 1000;
    // Ease toward the pointer. 0.06 is slow enough to feel weighted and
    // fast enough not to lag behind a deliberate movement.
    lean += (targetLean - lean) * 0.06;
    draw(t);
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (running || reduced) return;
    running = true;
    raf = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  // A geometry check rather than IntersectionObserver, which did not
  // fire at all under test on this page - and its failure mode here is
  // the hero silently never animating.
  function syncRunning() {
    const r = canvas.getBoundingClientRect();
    const onScreen = r.bottom > 0 && r.top < window.innerHeight;
    if (onScreen && !document.hidden) start();
    else stop();
  }

  let queued = false;
  function requestSync() {
    if (queued) return;
    queued = true;
    window.setTimeout(() => {
      queued = false;
      syncRunning();
    }, 120);
  }

  window.addEventListener("scroll", requestSync, { passive: true });
  window.addEventListener("resize", () => {
    resize();
    draw((performance.now() - t0) / 1000);
  });
  document.addEventListener("visibilitychange", syncRunning);

  resize();
  if (reduced) draw(0);   // one static frame, no motion
  else start();
})();
