/* Scroll-driven scene below the hero.
 *
 * A tall section with a pinned viewport inside. How far you have
 * scrolled through the section becomes a 0..1 progress value, which
 * moves a cloudscape and decides which caption is lit.
 *
 * The art comes from desert-render.js, the same module the hero uses,
 * so the two cannot drift apart.
 *
 * WHY THE LOOP IS SHAPED LIKE THIS
 * IntersectionObserver never reported at all when this page was tested,
 * and rAF callbacks were seen being dropped. Both failures are silent
 * and both leave content stuck, so this file used to run on a timer
 * alone - which was safe and also capped the whole scene at 25 frames a
 * second, on a canvas repaint, a caption swap and a video seek that all
 * look exactly as coarse as whatever drives them.
 *
 * So rAF drives and the timer watches it. If a frame callback has not
 * painted recently the timer paints instead, which makes the fast path a
 * real 60fps and the failure mode the old behaviour rather than a frozen
 * page.
 *
 * The section is readable without any of this: the beats are visible in
 * the markup by default and only become scroll-driven once .scene-live
 * is added below.
 */
(function () {
  "use strict";

  const section = document.getElementById("scene");
  const canvas = document.getElementById("sceneCanvas");
  // The strip is hero + scene sharing one pinned shot. The captions are
  // still timed against the scene alone, but the footage is scrubbed
  // against the whole strip, so the walk starts on the first scroll from
  // the top of the page rather than waiting for the hero to pass.
  const strip = document.getElementById("filmstrip");
  if (!section || !canvas || !window.RGDesert) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const D = window.RGDesert;
  const ctx = canvas.getContext("2d", { alpha: true });
  if (!ctx) return;

  const beats = Array.from(section.querySelectorAll(".scene-beat"));

  // Split each caption heading into one span per word so they can be
  // staggered. Done here rather than in the template because it is
  // presentational: the markup stays a plain heading for anything that
  // reads the page instead of rendering it, and if this file never runs
  // the heading is simply a heading.
  beats.forEach((b) => {
    const h = b.querySelector(".beat-card h2");
    if (!h) return;
    const words = (h.textContent || "").trim().split(/\s+/);
    h.textContent = "";
    words.forEach((word, i) => {
      const outer = document.createElement("span");
      outer.className = "w";
      outer.style.setProperty("--i", String(i));
      const inner = document.createElement("i");
      inner.textContent = word;
      outer.appendChild(inner);
      h.appendChild(outer);
      // A real space between the spans, so the line still wraps and
      // still reads as words rather than as one run.
      if (i < words.length - 1) h.appendChild(document.createTextNode(" "));
    });
  });
  // The box pin coordinates are measured in.
  //
  // It must be the pins' own offset parent, not merely something the
  // same size. Measuring against .filmstrip-media and positioning inside
  // .scene-beats put every dot hundreds of pixels off, because
  // .scene-beats is a centred max-width column with padding and the
  // media layer is the full viewport. CSS gives .scene-beats the same
  // geometry as the media layer while the scene is live, so the two
  // agree - but the offset parent is the one to ask.
  const stage = section.querySelector(".scene-beats");
  // The dots are their own list now. They are the only thing that moves
  // with the footage; the captions below hold one position.
  const pins = Array.from(section.querySelectorAll(".beat-pin"));

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
    // Sand at dusk. Distant dunes are the palest - haze washes out
    // contrast with distance, and reproducing that is most of what makes
    // a layer read as far away rather than merely higher up. The values
    // are low because the sky behind them is, not because the dunes are
    // in shadow: the nearest ridge is still the darkest thing on screen.
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

  // How far through the scene, for choosing which caption is showing.
  function progress() {
    const r = section.getBoundingClientRect();
    const travel = r.height - window.innerHeight;
    if (travel <= 0) return 0;
    return clamp01(-r.top / travel);
  }

  // How far through the whole strip, for the footage. Separate from
  // progress() because the two measure different things: the captions
  // belong to the scene, the shot spans the hero as well.
  function stripProgress() {
    const el = strip || section;
    const r = el.getBoundingClientRect();
    const travel = r.height - window.innerHeight;
    if (travel <= 0) return 0;
    return clamp01(-r.top / travel);
  }

  // Measured against the whole strip, not the scene.
  //
  // This was #scene, which starts one full screen down - so at the top
  // of the page it reported "off screen", render() never ran, and the
  // entire first screen was inert: no camera move, no scrub, and no
  // call into readyToScrub(). The shot is pinned across the hero as
  // well as the scene, so the strip is what "is any of this visible"
  // actually means.
  function onScreen() {
    const r = (strip || section).getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight;
  }

  /* ---- pins on the vine -------------------------------------------- */
  //
  // Where each caption attaches to the plant.
  //
  // These are measured, not placed by eye: each frame a caption is
  // active over was sampled for pixels that are strongly green (the only
  // such thing in a desert at dusk), and the anchor is the densest lit
  // cluster that is far enough from the pins already chosen. So the four
  // dots sit on four different parts of the vine rather than piling up
  // on its centre of mass, which is where a naive centroid puts them.
  //
  // Coordinates are normalised to the VIDEO, not to the element. The
  // element is object-fit:cover, so how much of the frame is on screen
  // depends on the window's shape - mapping these through the cover
  // maths below is what keeps a dot on the same leaf at every size.
  //
  // `t` is the moment in the clip the vine actually reaches that point -
  // the same measurement the coordinates came from. It is what decides
  // when the dot and its card appear, so the information arrives as the
  // plant arrives at it rather than on a separate schedule of its own.
  // Re-measured with the copy column excluded. The first pass picked the
  // densest lit vine anywhere in frame, which put one dot at x=0.203 -
  // squarely behind the caption panel, so it was placed correctly and
  // never seen. The search now only considers the right of the frame,
  // where three of the four best clusters already were.
  const PINS = [
    { x: 0.828, y: 0.750, t: 2.67 },
    { x: 0.641, y: 0.750, t: 4.10 },
    { x: 0.672, y: 0.528, t: 5.59 },
    { x: 0.828, y: 0.583, t: 7.12 },
  ];

  // A beat lights slightly before its anchor time, so the dot is already
  // there as the vine grows into it instead of catching up afterwards.
  const PIN_LEAD = 0.35;

  // The source's own aspect. Read off the element once it has metadata,
  // because a hardcoded 16:9 would silently misplace every pin the day
  // the clip is replaced with something else.
  //
  // Only the declaration lives here. The wiring that reads it is down in
  // the video section, because `video` is a const declared there - and
  // touching it from up here throws a ReferenceError in the temporal
  // dead zone, which kills this whole IIFE on load. That failure is
  // invisible in exactly the way that matters: the markup is fine, the
  // video element is fine, and the page just quietly never animates.
  let mediaAspect = 16 / 9;

  /**
   * Map a point in video space to a point in the pinned area.
   *
   * object-fit: cover scales the frame to fill the box and crops the
   * overflow, so this reproduces that: whichever axis overflows is the
   * one that gets a negative offset, and a normalised coordinate is
   * measured against the DISPLAYED size rather than the box.
   */
  function toStage(nx, ny) {
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    const boxAspect = w / h;
    let dw, dh;
    if (mediaAspect > boxAspect) {
      dh = h;
      dw = h * mediaAspect;
    } else {
      dw = w;
      dh = w / mediaAspect;
    }
    return {
      x: (w - dw) / 2 + nx * dw,
      y: (h - dh) / 2 + ny * dh,
      w: w,
      h: h,
    };
  }

  function placePins() {
    if (!stage) return;
    pins.forEach((dot, i) => {
      const pin = PINS[i];
      if (!pin) return;
      const p = toStage(pin.x, pin.y);
      // Off the visible frame entirely - a very tall window crops most
      // of the width away. Better to drop the dot than to park it on the
      // edge pointing at nothing.
      const off = p.x < 16 || p.x > p.w - 16 || p.y < 16 || p.y > p.h - 16;
      dot.classList.toggle("is-offstage", off);
      dot.style.left = p.x + "px";
      dot.style.top = p.y + "px";
    });
  }

  /* ---- the dune pass ----------------------------------------------- */
  //
  // The two dunes draw back as the strip ends. Driven from the same
  // scroll position as everything else, over the last quarter of it, so
  // the pass finishes opening exactly as the footage runs out.
  const pass = document.getElementById("dunePass");
  const duneEls = pass ? Array.from(pass.querySelectorAll(".dune")) : [];
  let lastOpen = -1;

  function openPass(p) {
    if (!duneEls.length) return;
    // 0 until three-quarters of the way down, then 0..1 over the rest.
    const t = Math.max(0, Math.min(1, (p - 0.75) / 0.25));
    // Eased, so they part slowly and then clear quickly rather than
    // sliding at a constant rate.
    const open = t * t * (3 - 2 * t);
    // Writing a custom property every frame is cheap, but writing the
    // same one is not free either - style writes invalidate regardless
    // of whether the value changed.
    if (Math.abs(open - lastOpen) < 0.002) return;
    lastOpen = open;
    duneEls.forEach((d) => d.style.setProperty("--open", open.toFixed(4)));
  }

  /* ---- drawing ----------------------------------------------------- */
  function paint(p) {
    // 0 at the top of the scene, 1 at the bottom - and, since the clip
    // this stands in for opens on a sunset and ends in the dark, that is
    // also how far into the night the drawing is.
    const night = ease(p);
    const t = performance.now() / 1000;

    ctx.fillStyle = D.skyGradient(ctx, width, height, night);
    ctx.fillRect(0, 0, width, height);

    // Stars arrive as the light goes. Squared so they hold off through
    // the bright part of the sunset and then come quickly, which is how
    // it actually looks - dusk has almost no stars right up until it has
    // all of them.
    D.drawStars(ctx, stars, width, height, Math.pow(night, 2) * 0.85, t);

    // The sun sets: it starts just clear of the ridgeline and sinks
    // behind it, drifting right as it goes so the scene has a direction
    // rather than a disc dropping straight down the middle. The dunes
    // are filled after this, which is what occludes it - no masking
    // needed, the sun simply goes behind the sand.
    const sunX = width * (0.30 + 0.30 * p);
    const sunY = height * (0.54 + 0.24 * ease(p));
    D.drawSun(ctx, sunX, sunY, Math.max(18, Math.min(width, height) * 0.062), {
      // Reddening as it drops, and fading out rather than being cut off
      // at the horizon - the glow outlives the disc.
      hue: 34 - night * 20,
      alpha: Math.max(0, 1 - Math.pow(night, 1.6) * 1.1),
    });

    for (const d of dunes) {
      // Parallax across the scroll. Near dunes travel several times as
      // far as distant ones - the whole depth illusion is this one line.
      const shift = p * 0.09 * d.depth;
      // Lit at the top of the scene and unlit at the bottom. Passing
      // `night` straight through here would brighten the sand as the sun
      // went down.
      D.drawDune(ctx, d, width, height, shift, 1 - night);
    }

    // Scrim behind the caption column. Lit dune crests get bright enough
    // that body text on top of them stops being readable; a one-sided
    // gradient fixes it without dimming the half of the frame the scene
    // actually lives in.
    //
    // The colour is the page ground (--ink-0), not the old warm dark it
    // was left on through two repalettes. A scrim in a colour the page
    // does not use is a tinted rectangle over the artwork, which is what
    // it had become.
    const scrim = ctx.createLinearGradient(0, 0, width * 0.62, 0);
    scrim.addColorStop(0, "rgba(8, 25, 36, 0.84)");
    scrim.addColorStop(0.55, "rgba(8, 25, 36, 0.46)");
    scrim.addColorStop(1, "rgba(8, 25, 36, 0)");
    ctx.fillStyle = scrim;
    ctx.fillRect(0, 0, width * 0.62, height);
  }

  /* ---- captions ---------------------------------------------------- */
  let activeBeat = -1;

  /**
   * Which caption is showing, decided by how far the vine has grown.
   *
   * This used to divide the scene's scroll into four equal blocks, which
   * put the captions on a schedule unrelated to the footage: the vine
   * spends the first quarter of the strip growing with nothing to say
   * about it, and then a caption changes in the middle of a stretch
   * where nothing on screen has moved. Reading the clip's own clock
   * instead means the dot appears on the leaf as the leaf appears.
   *
   * `vt` is the current time in the clip. Before the first anchor no
   * beat is active at all, which is correct - there is no vine there yet.
   */
  function setBeat(vt) {
    if (!beats.length) return;
    let i = -1;
    for (let n = 0; n < PINS.length && n < beats.length; n++) {
      if (vt >= PINS[n].t - PIN_LEAD) i = n;
    }
    if (i === activeBeat) return;
    activeBeat = i;
    // i === -1 leaves everything dark, which is the state before the vine
    // has reached the first anchor.
    beats.forEach((b, n) => b.classList.toggle("is-active", n === i));

    // The dots keep their own state. Once one has appeared it stays, so
    // scrolling leaves a trail down the vine rather than a single dot
    // teleporting around the frame - the plant and the markers growing
    // together is the whole effect.
    pins.forEach((dot, n) => {
      dot.classList.toggle("is-active", n === i);
      dot.classList.toggle("has-appeared", n <= i);
    });
  }

  /* ---- video scrubbing --------------------------------------------- */
  //
  // The figure walks as you scroll: currentTime is driven straight from
  // scroll progress rather than the video playing itself.
  //
  // Three things this has to get right.
  //
  // Seeking into an unbuffered range stalls, and a stalled seek is a
  // frozen frame that looks like the effect is broken. So nothing is
  // scrubbed until enough of the file has arrived, and the video reveals
  // itself only at that point.
  //
  // Assigning currentTime on every scroll event queues seeks faster than
  // the decoder retires them, which stutters. A small threshold skips
  // seeks too fine to see - below about a frame at 30fps there is nothing
  // to show for the work.
  //
  // And the last frames are not always seekable: browsers clamp to just
  // under duration, so mapping progress 1.0 to exactly duration can leave
  // the seek permanently pending. The range is trimmed slightly short.
  const video = document.getElementById("sceneVideo");

  // Now that `video` exists, let the pins learn the clip's real shape.
  if (video) {
    const readAspect = () => {
      if (video.videoWidth && video.videoHeight) {
        mediaAspect = video.videoWidth / video.videoHeight;
        placePins();
      }
    };
    video.addEventListener("loadedmetadata", readAspect);
    readAspect();
  }
  let scrubbable = false;
  // Set once the video has finished fading up and is genuinely covering
  // the canvas, which is not the same moment as it becoming scrubbable -
  // the fade is 0.9s long and the drawn scene shows through all of it.
  let covered = false;
  const FRAME = 1 / 30;
  const END_TRIM = 0.05;

  function readyToScrub() {
    if (!video || scrubbable) return scrubbable;
    // HAVE_FUTURE_DATA or better, and a buffered range that starts at
    // the beginning - a mid-file range is no use for scrubbing from 0.
    if (video.readyState >= 3 && video.buffered.length > 0) {
      scrubbable = true;
      video.classList.add("is-ready");
      // Slightly longer than the CSS fade, so the handover happens under
      // an opaque frame rather than one frame before it.
      window.setTimeout(() => { covered = true; }, 1100);
    }
    return scrubbable;
  }

  // Where the scroll says the playhead should be, and where it actually
  // is. Kept apart so the video eases toward the target instead of
  // jumping to it.
  //
  // Setting currentTime straight from the scroll position ties playback
  // to the wheel's own granularity: a mouse wheel moves in coarse steps,
  // so the figure advanced in visible jerks. Easing decouples the two -
  // the scroll sets a destination and the video slides toward it, which
  // is what makes it read as motion rather than as scrubbing.
  let targetTime = 0;
  let smoothing = false;
  let lastStep = 0;

  function scrubTo(p) {
    if (!readyToScrub()) return;
    const dur = video.duration;
    if (!isFinite(dur) || dur <= 0) return;
    targetTime = Math.max(0, Math.min(dur - END_TRIM, p * (dur - END_TRIM)));
    if (!smoothing) {
      smoothing = true;
      lastStep = performance.now();
      requestAnimationFrame(smoothStep);
    }
  }

  function smoothStep(now) {
    if (!video || !scrubbable) {
      smoothing = false;
      return;
    }
    const delta = targetTime - video.currentTime;
    if (Math.abs(delta) < FRAME) {
      // Close enough that another seek would show nothing. Stop the loop
      // rather than spin a frame callback forever.
      smoothing = false;
      return;
    }
    // Elapsed time, not frame count. A fixed fraction per callback means
    // the footage catches up twice as fast on a 120Hz screen as on a
    // 60Hz one - the same scroll gesture producing a different amount of
    // glide depending on the monitor. Converting the rate to a
    // per-millisecond one makes the feel a property of the page.
    const dt = Math.min(64, Math.max(1, (now || performance.now()) - lastStep));
    lastStep = now || performance.now();
    const k = 1 - Math.pow(1 - 0.18, dt / 16.67);
    try {
      // A large jump - a scrollbar drag, an anchor link - snaps instead
      // of crawling through seconds of footage to catch up.
      const stepped = video.currentTime + delta * k;
      video.currentTime = Math.abs(delta) > 1.2 ? targetTime : stepped;
    } catch (e) {
      // A seek can throw while the element is still settling; the next
      // frame tries again.
    }
    requestAnimationFrame(smoothStep);
  }

  // Skipped entirely under reduced motion: CSS hides the video there,
  // and fetching several megabytes to decode frames nobody will see is
  // waste on top of ignoring the preference.
  if (video && !reduced) {
    // Check the state, do not only subscribe to it.
    //
    // This is the bug that made the video invisible. preload="auto"
    // starts fetching during parse, and this file runs at the end of
    // the body - so on any reload with the clip in cache, readyState is
    // already 4 and canplaythrough fired before there was a listener to
    // hear it. Both handlers then waited forever for events that were
    // never coming again, the element kept its opacity:0, and the page
    // showed the drawn fallback with no indication anything was wrong.
    //
    // The same trap is documented in landing.js for the old hero video.
    // It was fixed there and not here.
    const tryReveal = () => {
      if (readyToScrub()) {
        scrubTo(stripProgress());
        return true;
      }
      return false;
    };

    if (!tryReveal()) {
      // Every event that can mean "there are frames now". readyState can
      // reach 3 without any of them firing again, which is why the poll
      // below exists as well.
      ["loadeddata", "canplay", "canplaythrough", "progress", "suspend"]
        .forEach((ev) => video.addEventListener(ev, tryReveal));

      // Last resort, and the one that actually catches the cached case
      // if an event somehow still does not arrive. Stops as soon as it
      // succeeds, so it costs nothing once the video is up.
      const watch = window.setInterval(() => {
        if (tryReveal() || video.error) window.clearInterval(watch);
      }, 120);
      window.setTimeout(() => window.clearInterval(watch), 20000);
    }
    // Some browsers will not decode a frame until playback has been
    // kicked once. Playing and immediately pausing gets a frame on
    // screen without the video actually running.
    const kick = video.play();
    if (kick && typeof kick.then === "function") {
      kick.then(() => video.pause()).catch(() => {});
    }
  }

  /* ---- loop -------------------------------------------------------- */
  function render() {
    const p = progress();
    // The canvas is the fallback, and once the footage is up it is an
    // invisible one: same box, object-fit:cover, fully opaque over the
    // top of it. Redrawing four gradient-filled dune paths, a sun halo
    // and ninety stars underneath that is a few hundred fills a frame
    // that nobody can see, and on a laptop it is the difference between
    // the scrub keeping up with the scroll and lagging behind it.
    if (!covered) paint(p);
    // The clip's own clock, derived from the same scroll position that
    // drives the playhead - not read off video.currentTime, which lags
    // behind the target while the eased scrub catches up and would make
    // the captions stutter.
    const dur = (video && isFinite(video.duration) && video.duration > 0)
      ? video.duration
      : 8;
    setBeat(stripProgress() * (dur - END_TRIM));
    // The footage is measured against the strip, the captions against
    // the scene - passing the caption progress to the scrub would leave
    // the shot frozen on frame one for the whole hero.
    placePins();
    openPass(stripProgress());
    scrubTo(stripProgress());
  }

  if (reduced) {
    // One static frame, mid-scene, and every caption left visible by CSS.
    // The camera is parked mid-push rather than at 1x, so the still is
    // the composition the moving version passes through instead of the
    // wide shot it starts on.
    resize();
    paint(0.5);
    beats.forEach((b) => b.classList.add("is-active"));
    return;
  }

  let queued = false;
  let lastPaint = 0;

  function paintFrame() {
    queued = false;
    lastPaint = performance.now();
    if (onScreen()) render();
  }

  function request() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(paintFrame);
  }

  window.addEventListener("scroll", request, { passive: true });
  window.addEventListener("resize", () => {
    resize();
    if (onScreen()) render();
  });

  // Two jobs, both of them safety nets rather than the engine.
  //
  // It covers scrolling the listener cannot see - anchor jumps,
  // find-in-page, a container that scrolls instead of the window - and
  // it covers rAF not arriving at all, which is the failure this file
  // was originally written around: `queued` would be stuck true and
  // every later request would return early against a callback that is
  // never coming. Clearing it here is what makes that recoverable.
  window.setInterval(() => {
    if (!onScreen()) return;
    if (performance.now() - lastPaint < 200) return;
    queued = false;
    paintFrame();
  }, 200);

  resize();
  render();
})();
