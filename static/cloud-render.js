/* Shared cloud and moon drawing.
 *
 * Extracted from hero-physics.js so the hero and the scroll scene draw
 * the same art from one place. Two copies would drift, and this is also
 * the code the social preview image is rendered from - a divergence
 * would show up as the branding not matching itself.
 *
 * drawCloud and drawMoon take an explicit target context. Inside
 * hero-physics they closed over one module-scoped canvas, which is
 * precisely what stopped anything else from calling them.
 *
 * Attached to window rather than exported as an ES module: both pages
 * load this with a plain <script> tag, and switching to modules would
 * change load order and defer semantics for no benefit here.
 */
(function () {
  "use strict";

  function rand(min, max) {
    return min + Math.random() * (max - min);
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

  // One reusable offscreen buffer for moons. The crescent is cut with
  // destination-out, which erases from the WHOLE canvas it's applied to -
  // done directly on the main canvas, a moon drifting over a cloud
  // punches a hole straight through it. Compositing each moon in
  // isolation and blitting the result keeps the cut local to that moon.
  const buf = document.createElement("canvas");
  const bctx = buf.getContext("2d");

  function drawMoon(ctx, b) {
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

  function drawCloud(ctx, b) {
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

  window.RGCloudRender = { makeCumulus, drawCloud, drawMoon };
})();
