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
      // Tightly packed and generously sized so the base row fuses into
      // one mass. Spread them out and each circle stays legible as a
      // circle, which is what made the underside look like a row of
      // beads rather than a cloud bottom.
      puffs.push({
        x: (t - 0.5) * 1.32,
        y: 0.30 + rand(-0.03, 0.03),
        r: rand(0.54, 0.68),
      });
    }
    // Billows - fewer and smaller the higher they go, pulled toward the
    // middle so the cloud peaks rather than being a flat slab.
    // Taller and narrower than a first guess suggests. Cumulus grow
    // upward - the reference photo is towers, not mounds - and a tier
    // list that widens as it rises produces the squat clipart mound this
    // originally looked like.
    const tiers = [
      { count: 3, y: -0.10, spread: 1.10, r: [0.44, 0.58] },
      { count: 3, y: -0.46, spread: 0.86, r: [0.36, 0.50] },
      { count: 2, y: -0.80, spread: 0.58, r: [0.30, 0.42] },
      { count: 1, y: -1.08, spread: 0.30, r: [0.24, 0.34] },
    ];
    // A per-cloud lean, applied in proportion to height so the base stays
    // put and the top drifts - which is what wind shear does to a real
    // cumulus. Without it every cloud is a symmetrical triangle, and a
    // skyful of identical triangles is the tell that they were generated.
    const lean = rand(-0.26, 0.26);
    for (const tier of tiers) {
      for (let i = 0; i < tier.count; i++) {
        const t = tier.count === 1 ? 0.5 : i / (tier.count - 1);
        puffs.push({
          x: (t - 0.5) * tier.spread + lean * -tier.y + rand(-0.07, 0.07),
          y: tier.y + rand(-0.05, 0.05),
          r: rand(tier.r[0], tier.r[1]),
        });
      }
    }
    // Rim detail. The tiers above give a lumpy outline, but every lump is
    // the same size, and a cumulus is lumpy at several scales at once -
    // big billows carrying smaller billows on their shoulders, which is
    // what the eye reads as "cauliflower" rather than "circles". So walk
    // the upper puffs and stud each one's top arc with smaller lobes.
    // Collected separately and appended afterwards, because pushing into
    // the array being iterated would grow lobes on lobes forever.
    // Two rounds, because cauliflower is self-similar: the first studs
    // the big billows with medium lobes, the second studs those with
    // small ones. One round alone still reads as smooth - it just moves
    // the smoothness down a size. Two is enough; a third is invisible at
    // this scale and only costs fill time.
    let parents = puffs.slice();
    for (const round of [
      { scale: [0.38, 0.58], ride: 0.72, count: [2, 4] },
      { scale: [0.30, 0.46], ride: 0.68, count: [1, 3] },
    ]) {
      const detail = [];
      for (const p of parents) {
        if (p.y > 0.2) continue;            // leave the flat base alone
        const n = round.count[0] +
          Math.floor(Math.random() * (round.count[1] - round.count[0] + 1));
        for (let i = 0; i < n; i++) {
          // Negative sine is upward in canvas coordinates, so this arc
          // spans the top of the puff and never the shadowed underside.
          const a = rand(-Math.PI * 0.95, -Math.PI * 0.05);
          detail.push({
            x: p.x + Math.cos(a) * p.r * round.ride,
            y: p.y + Math.sin(a) * p.r * round.ride,
            r: p.r * rand(round.scale[0], round.scale[1]),
          });
        }
      }
      for (const d of detail) puffs.push(d);
      parents = detail;
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

  // Clouds are drawn once into their own offscreen canvas and then
  // blitted every frame.
  //
  // Compositing the puffs opaquely offscreen is what stops the overlaps
  // showing as darker seams - drawing translucent circles straight onto
  // the canvas makes every intersection visibly denser, which is exactly
  // what made the previous version look like stacked discs.
  //
  // Caching per body matters now that a cloud is forty-odd puffs plus a
  // blur and three compositing passes: none of that depends on time, so
  // repeating it every frame was pure waste. It only has to be redone if
  // the cloud's size changes, which a viewport resize can do.
  function renderCloudSprite(b) {
    // Extents depend only on the puff layout, which never changes, so
    // measure once per cloud rather than every frame. Kept symmetrical
    // about the origin so the body's x/y stays the cloud's centre and
    // the drawImage below can simply centre the buffer.
    if (!b.ext) {
      let ex = 0;
      let ey = 0;
      for (const p of b.puffs) {
        ex = Math.max(ex, Math.abs(p.x) + p.r);
        ey = Math.max(ey, Math.abs(p.y) + p.r);
      }
      b.ext = { ex, ey };
    }
    const pad = b.r * 0.3;
    const w = Math.ceil(b.ext.ex * 2 * b.r + pad * 2);
    const h = Math.ceil(b.ext.ey * 2 * b.r + pad * 2);
    const cbuf = b.sprite || (b.sprite = document.createElement("canvas"));
    const cctx = cbuf.getContext("2d");
    cbuf.width = w;            // assigning size also clears the canvas
    cbuf.height = h;
    b.spriteR = b.r;
    const cx = w / 2;
    const cy = h / 2;
    const top = cy - b.ext.ey * b.r;
    const bottom = cy + b.ext.ey * b.r;

    // PASS 1 - silhouette. Flat opaque white, so overlapping puffs merge
    // into one shape instead of showing their seams. The previous version
    // shaded each puff individually and let it fade out at its own rim,
    // which is why the cloud had no outline and read as a pile of discs.
    //
    // Every arc goes into a SINGLE path and is filled once. Canvas fills
    // a path's subpaths as a union, so the result is identical - but the
    // blur runs once instead of once per puff, and there are forty-odd
    // puffs now. Each arc needs its own moveTo or the path would draw a
    // connecting line from the end of one circle to the start of the next.
    cctx.filter = `blur(${Math.max(1, b.r * 0.016)}px)`;
    cctx.fillStyle = "#ffffff";
    cctx.beginPath();
    for (const p of b.puffs) {
      const px = cx + p.x * b.r;
      const py = cy + p.y * b.r;
      const pr = p.r * b.r;
      cctx.moveTo(px + pr, py);
      cctx.arc(px, py, pr, 0, Math.PI * 2);
    }
    cctx.fill();
    cctx.filter = "none";

    // PASS 2 - light. One gradient across the whole cloud, not per puff:
    // sunlit white on top falling to a cool blue-grey base, the way the
    // reference photo has its shadowed underside. source-atop confines it
    // to pixels the silhouette already covers, so the shape is untouched.
    cctx.globalCompositeOperation = "source-atop";
    // The range top-to-bottom is wide on purpose. An almost-white cloud
    // with a faint tint at the base looks like a paper cut-out; what
    // sells the volume in the reference photo is how dark the shadowed
    // underside actually goes next to the blown-out sunlit top.
    const light = cctx.createLinearGradient(0, top, 0, bottom);
    light.addColorStop(0, "rgba(255, 255, 255, 1)");
    light.addColorStop(0.34, "rgba(250, 253, 255, 1)");
    light.addColorStop(0.62, `hsl(${b.hue}, 36%, 86%)`);
    light.addColorStop(0.84, `hsl(${b.hue}, 42%, 72%)`);
    light.addColorStop(1, `hsl(${b.hue}, 46%, 58%)`);
    cctx.fillStyle = light;
    cctx.fillRect(0, 0, w, h);

    // PASS 3 - crevices. A lobe sitting in front of another casts a soft
    // shadow into the gap between them, and it is that internal structure
    // the eye uses to read a cloud as a heap of volumes rather than one
    // flat silhouette. Darkening each puff's rim gets it almost for free:
    // where two lobes overlap, two rims fall in the same place and the
    // seam between them naturally deepens.
    //
    // The alpha is low deliberately. Push it and every lobe gains a
    // visible outline, which looks drawn rather than lit.
    // Blurred, because the shading has to arrive as a gradient and not as
    // an edge. An unblurred version of this drew a crisp ring around
    // every single puff and the cloud looked like a bag of bubbles. The
    // sprite is rendered once and cached, so the cost is paid on creation
    // rather than per frame.
    cctx.filter = `blur(${Math.max(1, b.r * 0.05)}px)`;
    for (const p of b.puffs) {
      const px = cx + p.x * b.r;
      const py = cy + p.y * b.r;
      const pr = p.r * b.r;
      // Starts darkening early and ends shallow. The total contrast is
      // what separates lobes; concentrating it near the rim is what turns
      // the separation into an outline.
      const s = cctx.createRadialGradient(px, py, pr * 0.2, px, py, pr);
      s.addColorStop(0, `hsla(${b.hue}, 40%, 46%, 0)`);
      s.addColorStop(0.55, `hsla(${b.hue}, 40%, 46%, 0.03)`);
      s.addColorStop(1, `hsla(${b.hue}, 42%, 44%, 0.10)`);
      cctx.fillStyle = s;
      // Fill the circle, not its bounding box. A radial gradient clamps
      // to its final colour stop everywhere beyond the outer radius, so
      // a fillRect here paints the square's corners with the full shadow
      // colour - which showed up as a grid of translucent boxes over the
      // whole cloud.
      cctx.beginPath();
      cctx.arc(px, py, pr, 0, Math.PI * 2);
      cctx.fill();
    }
    cctx.filter = "none";

    // PASS 4 - volume. The flat gradient alone reads as a cut-out, so put
    // a soft highlight back on each lobe's crown. Still source-atop, so
    // these only brighten the cloud and never spill past its edge.
    for (const p of b.puffs) {
      // Strength falls off toward the base and stops entirely at the
      // bottom row. Highlighting every puff equally lit the base circles
      // individually, and a row of separately-lit spheres is exactly
      // what a cloud must not look like - the underside of a cumulus is
      // one flat shadow, not a bank of lamps.
      const lift = (0.10 - p.y) / 1.1;
      if (lift <= 0) continue;
      const strength = Math.min(1, lift);
      const px = cx + p.x * b.r;
      const py = cy + p.y * b.r;
      const pr = p.r * b.r;
      const g = cctx.createRadialGradient(
        px, py - pr * 0.5, pr * 0.05,
        px, py - pr * 0.1, pr * 1.05,
      );
      g.addColorStop(0, `rgba(255, 255, 255, ${0.88 * strength})`);
      g.addColorStop(0.5, `rgba(255, 255, 255, ${0.24 * strength})`);
      g.addColorStop(1, "rgba(255, 255, 255, 0)");
      cctx.fillStyle = g;
      // Circle rather than bounding box, for the same clamping reason as
      // the shadow pass above. Harmless here only because this gradient
      // happens to end fully transparent - not worth relying on.
      cctx.beginPath();
      cctx.arc(px, py - pr * 0.1, pr * 1.05, 0, Math.PI * 2);
      cctx.fill();
    }
    cctx.globalCompositeOperation = "source-over";
  }

  function drawCloud(b) {
    // Re-render only when there is no sprite yet, or the body changed
    // size - a resize rescales the bodies, and blitting a stale sprite
    // would leave the cloud at its old dimensions.
    if (!b.sprite || b.spriteR !== b.r) renderCloudSprite(b);

    ctx.save();
    // Near-opaque. Real cumulus are solid, and the old 0.72 was most of
    // why these looked washed out against the dark page.
    ctx.globalAlpha = 0.93;
    ctx.translate(b.x, b.y);
    ctx.rotate(Math.sin(b.angle) * 0.04);   // barely perceptible sway
    ctx.drawImage(b.sprite, -b.sprite.width / 2, -b.sprite.height / 2);
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
