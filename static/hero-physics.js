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
        hue: isMoon ? rand(188, 200) : rand(198, 222),
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

  /**
   * A cumulus silhouette as a list of circles.
   *
   * Real cumulus have a roughly flat base and a billowing cauliflower
   * top - that asymmetry is most of what makes them read as clouds
   * rather than as a row of circles. So: a wide base row, then
   * progressively smaller, higher billows clustered toward the centre.
   * Units are fractions of the body radius.
   */
  function makeCumulus() {
    const puffs = [];
    // Base row - flat bottoms, sitting on the same line.
    const baseCount = 3 + Math.floor(Math.random() * 2);
    for (let i = 0; i < baseCount; i++) {
      const t = baseCount === 1 ? 0.5 : i / (baseCount - 1);
      puffs.push({
        x: (t - 0.5) * 1.7,
        y: 0.34 + rand(-0.04, 0.04),
        r: rand(0.46, 0.62),
      });
    }
    // Billows - fewer and smaller the higher they go, pulled toward the
    // middle so the cloud peaks rather than being a flat slab.
    const tiers = [
      { count: 3, y: -0.02, spread: 1.15, r: [0.44, 0.60] },
      { count: 2, y: -0.34, spread: 0.72, r: [0.34, 0.48] },
      { count: 1, y: -0.58, spread: 0.34, r: [0.26, 0.36] },
    ];
    for (const tier of tiers) {
      for (let i = 0; i < tier.count; i++) {
        const t = tier.count === 1 ? 0.5 : i / (tier.count - 1);
        puffs.push({
          x: (t - 0.5) * tier.spread + rand(-0.07, 0.07),
          y: tier.y + rand(-0.05, 0.05),
          r: rand(tier.r[0], tier.r[1]),
        });
      }
    }
    return puffs;
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

  /* ---- drawing ---- */
  // One reusable offscreen buffer for moons. The crescent is cut with
  // destination-out, which erases from the WHOLE canvas it's applied to -
  // done directly on the main canvas, a moon drifting over a cloud
  // punches a hole straight through it. Compositing each moon in
  // isolation and blitting the result keeps the cut local to that moon.
  const buf = document.createElement("canvas");
  const bctx = buf.getContext("2d");

  function drawMoon(b) {
    const pad = b.r * 1.6;              // room for the glow
    const size = Math.ceil((b.r + pad) * 2);
    if (buf.width !== size || buf.height !== size) {
      buf.width = size;
      buf.height = size;
    } else {
      bctx.clearRect(0, 0, size, size);
    }
    const c = size / 2;                 // centre within the buffer

    const g = bctx.createRadialGradient(
      c - b.r * 0.3, c - b.r * 0.3, b.r * 0.1, c, c, b.r);
    g.addColorStop(0, `hsla(${b.hue}, 95%, 82%, 0.98)`);
    g.addColorStop(1, `hsla(${b.hue}, 90%, 58%, 0.75)`);

    bctx.save();
    bctx.shadowColor = `hsla(${b.hue}, 95%, 65%, 0.55)`;
    bctx.shadowBlur = b.r * 0.9;
    bctx.beginPath();
    bctx.arc(c, c, b.r, 0, Math.PI * 2);
    bctx.fillStyle = g;
    bctx.fill();
    bctx.restore();

    // Crescent cut - now confined to this buffer.
    bctx.save();
    bctx.globalCompositeOperation = "destination-out";
    bctx.beginPath();
    bctx.arc(c + b.r * 0.42, c - b.r * 0.3, b.r * 0.86, 0, Math.PI * 2);
    bctx.fillStyle = "#000";
    bctx.fill();
    bctx.restore();

    ctx.drawImage(buf, b.x - c, b.y - c);
  }

  // Second buffer, for clouds. Compositing the puffs opaquely here and
  // blitting the finished shape once is what stops the overlaps showing
  // as darker seams - drawing translucent circles straight onto the
  // canvas makes every intersection visibly denser, which is exactly
  // what made the previous version look like stacked discs.
  const cbuf = document.createElement("canvas");
  const cctx = cbuf.getContext("2d");

  function drawCloud(b) {
    const pad = b.r * 0.9;
    const w = Math.ceil(b.r * 2.6 + pad * 2);
    const h = Math.ceil(b.r * 2.2 + pad * 2);
    if (cbuf.width !== w || cbuf.height !== h) {
      cbuf.width = w;
      cbuf.height = h;
    } else {
      cctx.clearRect(0, 0, w, h);
    }
    const cx = w / 2;
    const cy = h / 2;

    // Each puff is lit from above: bright, almost-white crown fading to
    // a cooler, dimmer underside. That vertical light gradient is what
    // gives a flat circle the appearance of volume.
    for (const p of b.puffs) {
      const px = cx + p.x * b.r;
      const py = cy + p.y * b.r;
      const pr = p.r * b.r;
      const g = cctx.createRadialGradient(
        px, py - pr * 0.45, pr * 0.08,   // light source: up and slightly back
        px, py + pr * 0.15, pr,
      );
      // Brighter than feels right in isolation - against a near-black
      // page a "white" cloud at 70% reads as grey. Real cumulus are
      // brilliant white on top with only the undersides in shadow.
      g.addColorStop(0, "rgba(255, 255, 255, 1)");
      g.addColorStop(0.42, "rgba(244, 250, 255, 0.94)");
      g.addColorStop(0.78, `hsla(${b.hue}, 40%, 74%, 0.55)`);
      g.addColorStop(1, `hsla(${b.hue}, 45%, 58%, 0)`);
      cctx.fillStyle = g;
      cctx.beginPath();
      cctx.arc(px, py, pr, 0, Math.PI * 2);
      cctx.fill();
    }

    ctx.save();
    // Overall transparency applied to the finished cloud, not per puff.
    ctx.globalAlpha = 0.72;
    ctx.translate(b.x, b.y);
    ctx.rotate(Math.sin(b.angle) * 0.04);   // barely perceptible sway
    ctx.drawImage(cbuf, -cx, -cy);
    ctx.restore();
  }

  function draw() {
    ctx.clearRect(0, 0, width, height);
    for (const b of bodies) {
      if (b.kind === "moon") drawMoon(b);
      else drawCloud(b);
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
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting && !document.hidden) start();
        else stop();
      }
    },
    { threshold: 0.05 },
  );
  io.observe(canvas);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (canvas.getBoundingClientRect().bottom > 0) start();
  });

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
