/**
 * Interactive hero: draggable, throwable moons and clouds.
 *
 * Three constraints shaped this:
 *
 * 1. It must never block the UI. The canvas sits *behind* the hero
 *    content with pointer-events:none, and input is read from window
 *    listeners instead. A canvas overlaying the page with
 *    pointer-events:auto would swallow clicks on "Start free" - the
 *    single most important button on the site.
 *
 * 2. It must not cost anything when off-screen. The loop stops when the
 *    hero scrolls away or the tab is hidden, so a background tab isn't
 *    burning a core animating something nobody can see.
 *
 * 3. It must respect prefers-reduced-motion. Physics-driven motion is
 *    exactly the kind that triggers vestibular symptoms, so that setting
 *    disables the simulation and draws a single static frame.
 *
 * The physics is deliberately simple - Euler integration with damping
 * and impulse-based circle collisions. A real engine (matter.js) would
 * be ~80KB for behaviour nobody would perceive as better on eight
 * floating shapes.
 */
(function () {
  const canvas = document.getElementById("heroCanvas");
  if (!canvas) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ctx = canvas.getContext("2d", { alpha: true });

  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2); // cap: 3x on a
  // phone quadruples the fill cost for no visible gain

  const bodies = [];
  const GRAVITY = 0.012;     // barely-there drift, not a falling ball
  const DAMPING = 0.994;     // air resistance
  const WALL_BOUNCE = 0.7;
  const MAX_SPEED = 22;      // stops a hard fling going hypersonic

  function resize() {
    const rect = canvas.getBoundingClientRect();
    width = rect.width;
    height = rect.height;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  /**
   * The central column where the headline, subtitle and buttons live.
   * Shapes are kept out of it: drifting across "Your own AI assistant"
   * makes the one thing a visitor must read harder to read, which is a
   * bad trade for some ambience. They frame the content instead.
   * Dragging a shape in is still allowed - that's the user's choice.
   */
  function safeZone() {
    const w = Math.min(width * (width < 700 ? 0.92 : 0.66), 780);
    const h = Math.min(height * 0.78, 460);
    return {
      x: width / 2,
      y: height * 0.52,
      hw: w / 2,
      hh: h / 2,
    };
  }

  function makeBodies() {
    bodies.length = 0;
    // Fewer and smaller on a phone: the same eight shapes that look
    // sparse on a desktop crowd a narrow screen.
    const narrow = width < 700;
    const count = narrow ? 4 : 7;
    const z = safeZone();

    for (let i = 0; i < count; i++) {
      const isMoon = i % 3 === 0;
      const scale = narrow ? 0.7 : 1;
      const r = (isMoon ? rand(22, 34) : rand(40, 66)) * scale;

      // Spawn in the margins - alternate left/right so they're balanced
      // rather than clumping on one side.
      const leftSide = i % 2 === 0;
      const marginOuter = 8;
      const marginInner = Math.max(marginOuter, z.x - z.hw - r);
      let x = leftSide
        ? rand(marginOuter + r, Math.max(marginOuter + r, marginInner))
        : rand(Math.min(width - marginOuter - r, z.x + z.hw + r), width - marginOuter - r);
      // On a narrow screen the safe zone leaves no side margin, so put
      // them above and below the text instead.
      if (narrow) {
        x = rand(r, width - r);
      }
      const y = narrow
        ? (i % 2 === 0 ? rand(r, height * 0.16) : rand(height * 0.86, height - r))
        : rand(r, height - r);

      bodies.push({
        kind: isMoon ? "moon" : "cloud",
        x, y,
        vx: rand(-0.28, 0.28),
        vy: rand(-0.15, 0.15),
        r,
        // Clouds are fluffier than they are heavy; moons are dense. This
        // only affects how much they shove each other.
        mass: isMoon ? r * 1.6 : r * 0.7,
        // Warm hues, to match the red palette. These drive the shadowed
        // side of a cloud and the body of a moon, so leaving them on the
        // old cyan ramp put blue-grey undersides under a red sky - the
        // one detail that makes a recoloured theme look recoloured.
        // Clouds sit redder than the moons, which stay amber, so the two
        // still read as different objects rather than one warm smear.
        hue: isMoon ? rand(38, 50) : rand(14, 32),
        spin: rand(-0.003, 0.003),
        angle: rand(0, Math.PI * 2),
        drift: rand(0, Math.PI * 2),   // phase, for a gentle bob
        grabbed: false,
        // Generated once and kept: regenerating per frame would make the
        // cloud boil and shimmer instead of drifting.
        puffs: isMoon ? null : makeCumulus(),
      });
    }
  }


  /* ---- pointer handling ---- */
  const pointer = { x: -9999, y: -9999, down: false, prevX: 0, prevY: 0 };
  let held = null;

  function toLocal(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function withinCanvas(p) {
    return p.x >= 0 && p.x <= width && p.y >= 0 && p.y <= height;
  }

  function onDown(e) {
    const p = toLocal(e);
    if (!withinCanvas(p)) return;
    pointer.x = pointer.prevX = p.x;
    pointer.y = pointer.prevY = p.y;
    pointer.down = true;

    // Grab the topmost body under the pointer. If none, the click falls
    // through untouched - which is what keeps buttons clickable.
    for (let i = bodies.length - 1; i >= 0; i--) {
      const b = bodies[i];
      if (Math.hypot(b.x - p.x, b.y - p.y) <= b.r) {
        held = b;
        b.grabbed = true;
        canvas.style.cursor = "grabbing";
        break;
      }
    }
  }

  function onMove(e) {
    const p = toLocal(e);
    pointer.prevX = pointer.x;
    pointer.prevY = pointer.y;
    pointer.x = p.x;
    pointer.y = p.y;

    if (held) {
      // Dragging a body is a text-selection gesture as far as the
      // browser is concerned; suppress that so it doesn't highlight the
      // headline while you play.
      e.preventDefault();
    } else if (withinCanvas(p)) {
      let over = false;
      for (const b of bodies) {
        if (Math.hypot(b.x - p.x, b.y - p.y) <= b.r) { over = true; break; }
      }
      canvas.style.cursor = over ? "grab" : "";
    }
  }

  function onUp() {
    if (held) {
      // Throw: carry the pointer's last-frame velocity into the body.
      held.vx = clamp(pointer.x - pointer.prevX, -MAX_SPEED, MAX_SPEED);
      held.vy = clamp(pointer.y - pointer.prevY, -MAX_SPEED, MAX_SPEED);
      held.grabbed = false;
      held = null;
      canvas.style.cursor = "";
    }
    pointer.down = false;
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  /* ---- simulation ---- */
  function step() {
    for (const b of bodies) {
      if (b.grabbed) {
        // Follow the pointer directly rather than applying a force -
        // spring-following feels laggy when you're dragging something.
        b.x = pointer.x;
        b.y = pointer.y;
        b.vx = pointer.x - pointer.prevX;
        b.vy = pointer.y - pointer.prevY;
        continue;
      }

      // Gentle repulsion from the cursor, so moving through the field
      // pushes things aside even without clicking.
      if (!pointer.down && withinCanvas(pointer)) {
        const dx = b.x - pointer.x;
        const dy = b.y - pointer.y;
        const d = Math.hypot(dx, dy);
        const reach = b.r + 60;
        if (d < reach && d > 0.01) {
          const push = (1 - d / reach) * 0.55;
          b.vx += (dx / d) * push;
          b.vy += (dy / d) * push;
        }
      }

      // Ease out of the text column. A soft, capped nudge rather than a
      // hard wall - a shape you threw in there drifts back out on its
      // own instead of being snapped away, which would feel broken.
      const z = safeZone();
      const dxz = b.x - z.x;
      const dyz = b.y - z.y;
      const ox = z.hw + b.r - Math.abs(dxz);   // overlap on each axis
      const oy = z.hh + b.r - Math.abs(dyz);
      if (ox > 0 && oy > 0) {
        // Push along whichever axis needs the least movement to clear.
        if (ox < oy) {
          b.vx += Math.sign(dxz || 1) * Math.min(ox * 0.0022, 0.12);
        } else {
          b.vy += Math.sign(dyz || 1) * Math.min(oy * 0.0022, 0.12);
        }
      }

      b.vy += GRAVITY;
      // A slow bob so idle shapes still feel alive once they've settled
      // against the floor, instead of sitting in a dead row.
      b.drift += 0.0065;
      b.vy += Math.sin(b.drift) * 0.010;
      b.vx *= DAMPING;
      b.vy *= DAMPING;
      b.x += b.vx;
      b.y += b.vy;
      b.angle += b.spin;

      // Walls
      if (b.x - b.r < 0) { b.x = b.r; b.vx = Math.abs(b.vx) * WALL_BOUNCE; }
      if (b.x + b.r > width) { b.x = width - b.r; b.vx = -Math.abs(b.vx) * WALL_BOUNCE; }
      if (b.y - b.r < 0) { b.y = b.r; b.vy = Math.abs(b.vy) * WALL_BOUNCE; }
      if (b.y + b.r > height) {
        b.y = height - b.r;
        b.vy = -Math.abs(b.vy) * WALL_BOUNCE;
        b.vx *= 0.985; // a little ground friction so they settle
      }
    }

    // Pairwise collisions. O(n²), but n is 8 - a spatial grid here would
    // be more code than the whole simulation.
    for (let i = 0; i < bodies.length; i++) {
      for (let j = i + 1; j < bodies.length; j++) {
        const a = bodies[i];
        const b = bodies[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.hypot(dx, dy);
        const minD = a.r + b.r;
        if (d >= minD || d === 0) continue;

        const nx = dx / d;
        const ny = dy / d;
        const overlap = minD - d;

        // Separate proportionally to mass, but never move a grabbed body
        // - the pointer owns its position.
        const total = a.mass + b.mass;
        if (!a.grabbed) { a.x -= nx * overlap * (b.mass / total); a.y -= ny * overlap * (b.mass / total); }
        if (!b.grabbed) { b.x += nx * overlap * (a.mass / total); b.y += ny * overlap * (a.mass / total); }

        // Elastic-ish impulse along the collision normal
        const rvx = b.vx - a.vx;
        const rvy = b.vy - a.vy;
        const sep = rvx * nx + rvy * ny;
        if (sep > 0) continue; // already moving apart
        const imp = (-(1 + 0.65) * sep) / (1 / a.mass + 1 / b.mass);
        if (!a.grabbed) { a.vx -= (imp / a.mass) * nx; a.vy -= (imp / a.mass) * ny; }
        if (!b.grabbed) { b.vx += (imp / b.mass) * nx; b.vy += (imp / b.mass) * ny; }
      }
    }
  }

  /* ---- drawing ----
     The cloud and moon art lives in cloud-render.js so this file and the
     scroll scene draw from one source. Pulled into locals here so the
     call sites below read the same as they did when the functions were
     defined in this file. */
  const { makeCumulus, drawCloud, drawMoon } = window.RGCloudRender;


  function draw() {
    ctx.clearRect(0, 0, width, height);
    for (const b of bodies) {
      if (b.kind === "moon") drawMoon(ctx, b);
      else drawCloud(ctx, b);
    }
  }

  /* ---- loop, paused when not visible ---- */
  let raf = null;
  let running = false;

  function frame() {
    step();
    draw();
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

  // Only animate while the hero is actually on screen.
  //
  // A geometry check rather than IntersectionObserver. IO is the usual
  // advice and is cheaper in principle, but it did not fire at all under
  // test on this page - and its failure mode here is that the hero
  // animation silently never starts, which looks like the canvas being
  // broken rather than like an observer problem.
  function syncRunning() {
    const r = canvas.getBoundingClientRect();
    const onScreen = r.bottom > 0 && r.top < window.innerHeight;
    if (onScreen && !document.hidden) start();
    else stop();
  }

  let visQueued = false;
  function requestSync() {
    if (visQueued) return;
    visQueued = true;
    window.setTimeout(() => {
      visQueued = false;
      syncRunning();
    }, 120);
  }

  window.addEventListener("scroll", requestSync, { passive: true });
  document.addEventListener("visibilitychange", syncRunning);

  window.addEventListener("resize", () => {
    const prev = bodies.map((b) => ({ fx: b.x / (width || 1), fy: b.y / (height || 1) }));
    resize();
    // Keep bodies proportionally where they were instead of teleporting
    // them to a corner when the window changes size.
    bodies.forEach((b, i) => {
      if (prev[i]) {
        b.x = clamp(prev[i].fx * width, b.r, width - b.r);
        b.y = clamp(prev[i].fy * height, b.r, height - b.r);
      }
    });
  });

  window.addEventListener("pointerdown", onDown);
  window.addEventListener("pointermove", onMove, { passive: false });
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onUp);

  resize();
  makeBodies();
  if (reduced) {
    draw();          // one static frame, no motion
  } else {
    start();
  }
})();
