/* Sahara scene drawing: dunes, sun, stars.
 *
 * Replaces cloud-render.js. The moons and clouds are gone entirely -
 * this is the whole art direction now, not a recolour of the old one.
 *
 * A dune is a curve, not a sprite. Summing a few sine waves at different
 * frequencies gives a ridgeline that reads as sand: one slow wave for
 * the overall rise and fall, a faster one for the secondary crests, and
 * a third barely-there one so the line never looks mathematically clean.
 * Filling from that curve down to the bottom of the canvas gives the
 * body of the dune, so a layer costs one path and one fill.
 *
 * Everything takes an explicit context, so the hero and the scroll scene
 * can share it. That was the mistake in the original hero code: the
 * drawing closed over one canvas and nothing else could call it.
 */
(function () {
  "use strict";

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  /* ---- dunes ------------------------------------------------------- */

  /**
   * A dune profile, in units independent of canvas size.
   *
   * `base` is where the ridge sits vertically, 0 at the top of the
   * canvas and 1 at the bottom. `waves` are the sine components summed
   * to make the ridgeline.
   */
  function makeDune(opts) {
    const o = opts || {};
    const base = o.base != null ? o.base : 0.7;
    const scale = o.scale != null ? o.scale : 1;
    return {
      base,
      // Three octaves. Two looks like a wave, four starts to look noisy
      // at the sizes these are drawn at.
      waves: [
        { amp: 0.055 * scale, freq: rand(0.7, 1.1), phase: rand(0, 6.283) },
        { amp: 0.026 * scale, freq: rand(1.8, 2.6), phase: rand(0, 6.283) },
        { amp: 0.011 * scale, freq: rand(3.6, 5.2), phase: rand(0, 6.283) },
      ],
      // Parallax factor: how far this layer shifts relative to the
      // nearest one. Distant dunes barely move, which is what sells
      // depth on a flat image.
      depth: o.depth != null ? o.depth : 1,
      hue: o.hue != null ? o.hue : 26,
      sat: o.sat != null ? o.sat : 42,
      light: o.light != null ? o.light : 22,
    };
  }

  function duneY(dune, t) {
    // t is 0..1 across the canvas width.
    let y = dune.base;
    for (const w of dune.waves) {
      y += w.amp * Math.sin(w.freq * t * Math.PI * 2 + w.phase);
    }
    return y;
  }

  /**
   * Fill one dune layer.
   *
   * `shift` moves the ridgeline horizontally - that is the whole
   * parallax mechanism, and it costs nothing because the curve is
   * evaluated per frame anyway.
   */
  function drawDune(ctx, dune, width, height, shift, lightMix) {
    const steps = Math.max(24, Math.min(120, Math.round(width / 12)));
    const mix = lightMix == null ? 0 : lightMix;

    ctx.beginPath();
    ctx.moveTo(0, height);
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const y = duneY(dune, t + (shift || 0)) * height;
      ctx.lineTo(t * width, y);
    }
    ctx.lineTo(width, height);
    ctx.closePath();

    // Vertical gradient per layer: lit along the ridge, falling into
    // shadow lower down. A flat fill makes dunes read as cut paper.
    const top = duneY(dune, 0.5) * height;
    const g = ctx.createLinearGradient(0, top, 0, height);
    const l = dune.light + mix * 10;
    g.addColorStop(0, `hsl(${dune.hue}, ${dune.sat}%, ${l + 9}%)`);
    g.addColorStop(1, `hsl(${dune.hue - 4}, ${dune.sat - 6}%, ${Math.max(4, l - 8)}%)`);
    ctx.fillStyle = g;
    ctx.fill();
  }

  /* ---- sun --------------------------------------------------------- */

  /**
   * The sun, drawn as a disc inside a much larger halo.
   *
   * The halo is the part that matters: a bare circle reads as a sticker,
   * where a wide soft falloff reads as light. Drawn additively so it
   * brightens the sky it sits on rather than covering it.
   */
  function drawSun(ctx, x, y, r, opts) {
    const o = opts || {};
    const hue = o.hue != null ? o.hue : 34;
    const alpha = o.alpha != null ? o.alpha : 1;
    if (alpha <= 0.01) return;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.globalCompositeOperation = "lighter";

    const halo = ctx.createRadialGradient(x, y, r * 0.5, x, y, r * 7);
    halo.addColorStop(0, `hsla(${hue}, 92%, 62%, 0.34)`);
    halo.addColorStop(0.35, `hsla(${hue - 6}, 88%, 52%, 0.12)`);
    halo.addColorStop(1, `hsla(${hue - 10}, 80%, 45%, 0)`);
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(x, y, r * 7, 0, Math.PI * 2);
    ctx.fill();

    const disc = ctx.createRadialGradient(x, y - r * 0.2, r * 0.1, x, y, r);
    disc.addColorStop(0, `hsla(${hue + 12}, 100%, 88%, 1)`);
    disc.addColorStop(0.6, `hsla(${hue}, 96%, 68%, 0.95)`);
    disc.addColorStop(1, `hsla(${hue - 8}, 92%, 56%, 0.7)`);
    ctx.fillStyle = disc;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
  }

  /* ---- stars ------------------------------------------------------- */

  function makeStars(count) {
    const stars = [];
    for (let i = 0; i < count; i++) {
      stars.push({
        // Kept in the upper band: stars sitting behind a dune would be
        // painted over anyway, so generating them there is wasted work.
        x: Math.random(),
        y: Math.random() * 0.55,
        r: rand(0.5, 1.5),
        // Each twinkles on its own phase and rate, or they pulse in
        // unison and the whole sky looks like it is breathing.
        phase: rand(0, 6.283),
        rate: rand(0.6, 1.8),
      });
    }
    return stars;
  }

  function drawStars(ctx, stars, width, height, alpha, t) {
    if (alpha <= 0.01) return;
    ctx.save();
    for (const s of stars) {
      const twinkle = 0.65 + 0.35 * Math.sin(t * s.rate + s.phase);
      ctx.globalAlpha = alpha * twinkle;
      ctx.fillStyle = "#fff6e2";
      ctx.beginPath();
      ctx.arc(s.x * width, s.y * height, s.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  /* ---- sky --------------------------------------------------------- */

  /**
   * The sky gradient. `warm` 0..1 runs night to full daylight.
   *
   * Three stops rather than two: a desert sky has a distinctly different
   * colour at the horizon than overhead, and interpolating straight
   * between the two loses the band of light that makes it read as a
   * desert rather than as a gradient.
   */
  function skyGradient(ctx, width, height, warm) {
    const g = ctx.createLinearGradient(0, 0, 0, height);
    g.addColorStop(0, `hsl(${248 - warm * 32}, ${42 - warm * 8}%, ${7 + warm * 12}%)`);
    g.addColorStop(0.55, `hsl(${32 - warm * 4}, ${46 + warm * 20}%, ${12 + warm * 24}%)`);
    g.addColorStop(1, `hsl(${26 + warm * 6}, ${58 + warm * 14}%, ${20 + warm * 30}%)`);
    return g;
  }

  window.RGDesert = {
    makeDune,
    duneY,
    drawDune,
    drawSun,
    makeStars,
    drawStars,
    skyGradient,
  };
})();
