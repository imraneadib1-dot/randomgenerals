/* Timeline editor for the video bay.
 *
 * WHY THIS EXISTS ALONGSIDE THE PROMPT BOX
 *
 * The prompt was the only control, and for compound edits it is still
 * the better one - "vertical, moody grade, caption over the first three
 * seconds" is one sentence and about nine clicks. But it is the wrong
 * tool for the single most common operation in video, which is finding
 * an exact frame. Nobody knows the timestamp they want; they know the
 * frame when they see it. Describing it is a worse way of doing
 * something people already know how to do by dragging.
 *
 * So this adds direct manipulation and keeps the sentence. They meet at
 * the same place: both end as an ops list that videoedit.validate()
 * checks, which is why the timeline needed no new render path.
 *
 * HOW THE PREVIEW WORKS
 *
 * Colour and frame changes are previewed with CSS filters on the
 * <video> element, live, as the slider moves. That is an approximation
 * of what ffmpeg will do and is deliberately not exact - the point is to
 * answer "roughly this?" in a frame rather than after a 40-second
 * render. The exported file is always ffmpeg's work, never the browser's.
 *
 * State lives in one object and every control writes into it. Nothing
 * reads the DOM back to work out what is applied, because two sources of
 * truth is how a UI starts disagreeing with the file it produces.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tl = $("tl");
  if (!tl) return;

  const video = $("videoPreview");
  const track = $("tlTrack");

  // The looks table is duplicated from videoedit.py's LOOKS. It is a list
  // of names, not behaviour, and the server rejects anything it does not
  // recognise - so the cost of drift here is a chip that turns into a
  // "skipped" note, not a broken render.
  const LOOKS = [
    "cinematic", "vintage", "noir", "warm", "cool", "vivid", "dream",
    "bleach", "faded", "moody", "sunset", "neon", "film",
  ];

  const state = {
    duration: 0,
    in: 0,
    out: 0,
    speed: 1,
    motion: null,          // reverse | boomerang
    ratio: null,
    blurfill: false,
    rotate: 0,
    flip: false,
    zoom: 0,
    look: null,
    bright: 0, contrast: 1, sat: 1, temp: 0,
    fx: new Set(),         // vignette | grain | sharpen | denoise
    text: "", textPos: "bottom", textSize: "medium",
    textColor: "white", textBox: true, textFrom: null, textTo: null,
    mute: false, volume: 1, normalize: false,
    quality: "", scale: "", fadeIn: 0, fadeOut: 0, gif: false,
  };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const fmt = (t) => {
    if (!isFinite(t) || t < 0) t = 0;
    const m = Math.floor(t / 60);
    const s = (t % 60).toFixed(1).padStart(4, "0");
    return `${m}:${s}`;
  };

  /* ---------------------------------------------------------------- strip
   * Thumbnails are drawn from the same <video> the user is watching, by
   * seeking it and painting to a canvas. That is why they appear a beat
   * after load rather than instantly: each frame costs a seek. Doing it
   * client-side keeps it free - the alternative is an ffmpeg call per
   * upload, on a server whose CPU is the scarcest thing it has.
   */
  function buildStrip() {
    const strip = $("tlStrip");
    strip.textContent = "";
    const count = Math.max(6, Math.min(14,
      Math.round(track.clientWidth / 90))) || 8;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");

    // A second, detached element. Seeking the visible one would yank the
    // playhead around under the person while the strip builds.
    const probe = document.createElement("video");
    probe.src = video.currentSrc || video.src;
    probe.muted = true;
    probe.preload = "auto";
    probe.crossOrigin = "anonymous";

    let i = 0;
    const shots = [];
    probe.addEventListener("loadeddata", () => {
      canvas.width = 160;
      canvas.height = Math.max(
        40, Math.round(160 * (probe.videoHeight / probe.videoWidth || 0.56)));
      seek();
    });
    function seek() {
      if (i >= count) return done();
      probe.currentTime = (state.duration * (i + 0.5)) / count;
    }
    probe.addEventListener("seeked", () => {
      try {
        ctx.drawImage(probe, 0, 0, canvas.width, canvas.height);
        shots.push(canvas.toDataURL("image/jpeg", 0.55));
      } catch (e) {
        // A tainted canvas (cross-origin source) throws here. The strip
        // is decoration; losing it must not take the editor with it.
        return done();
      }
      i += 1;
      seek();
    });
    probe.addEventListener("error", done);
    function done() {
      if (!shots.length) {
        strip.style.background =
          "repeating-linear-gradient(90deg,#2a4155 0 60px,#243a4d 60px 120px)";
        return;
      }
      shots.forEach((src) => {
        const img = new Image();
        img.src = src;
        strip.appendChild(img);
      });
      probe.src = "";
    }
  }

  /* --------------------------------------------------------------- paint */
  function paint() {
    const d = state.duration || 1;
    const l = (state.in / d) * 100;
    const r = (state.out / d) * 100;
    $("tlSel").style.left = l + "%";
    $("tlSel").style.width = Math.max(0, r - l) + "%";
    $("tlShadeL").style.width = l + "%";
    $("tlShadeR").style.width = Math.max(0, 100 - r) + "%";
    $("tlIn").textContent = fmt(state.in);
    $("tlOut").textContent = fmt(state.out);

    const len = state.out - state.in;
    const whole = state.in <= 0.05 && state.out >= d - 0.05;
    $("tlLen").textContent = whole ? "whole clip" : fmt(len) + " selected";

    $("tlHead").style.left =
      ((video.currentTime || 0) / d) * 100 + "%";
    $("tlNow").textContent = fmt(video.currentTime || 0);

    applyPreviewFilter();
    renderApplied();
  }

  /* CSS approximation of the colour ops, so a slider shows something
   * immediately. Not a promise about the render - ffmpeg's curves-based
   * looks have no CSS equivalent, so a named look only dims/saturates
   * here as a hint that something is on. */
  function applyPreviewFilter() {
    const f = [];
    if (state.bright) f.push(`brightness(${1 + state.bright})`);
    if (state.contrast !== 1) f.push(`contrast(${state.contrast})`);
    if (state.sat !== 1) f.push(`saturate(${state.sat})`);
    if (state.temp) f.push(`sepia(${Math.abs(state.temp) * 0.35})` +
      ` hue-rotate(${state.temp > 0 ? 0 : 180}deg)`);
    if (state.look === "noir") f.push("grayscale(1) contrast(1.35)");
    else if (state.look) f.push("saturate(1.1) contrast(1.05)");
    video.style.filter = f.join(" ");

    const t = [];
    if (state.rotate) t.push(`rotate(${state.rotate}deg)`);
    if (state.flip) t.push("scaleX(-1)");
    video.style.transform = t.join(" ");
  }

  /* ------------------------------------------------------------- the ops
   * One place that turns state into the list the server renders. Order
   * matters and follows videoedit.py's own pipeline: cut, then motion,
   * then frame, then colour, then overlay, then export.
   */
  function buildOps() {
    const ops = [];
    const d = state.duration;
    const push = (op, args) => ops.push(args ? { op, args } : { op });

    if (state.in > 0.05 || state.out < d - 0.05) {
      push("trim", { start: +state.in.toFixed(2), end: +state.out.toFixed(2) });
    }
    if (state.speed !== 1) push("speed", { factor: +state.speed.toFixed(2) });
    if (state.motion) push(state.motion);

    if (state.ratio) {
      push(state.blurfill ? "blurfill" : "aspect", { ratio: state.ratio });
    }
    if (state.rotate) push("rotate", { degrees: state.rotate });
    if (state.flip) push("flip", { axis: "h" });
    if (state.zoom > 0) {
      push("zoom", { direction: "in", amount: +state.zoom.toFixed(2) });
    }

    if (state.look) push("look", { name: state.look });
    if (state.bright) push("brightness", { amount: +state.bright.toFixed(2) });
    if (state.contrast !== 1) push("contrast", { amount: +state.contrast.toFixed(2) });
    if (state.sat !== 1) push("saturation", { amount: +state.sat.toFixed(2) });
    if (state.temp) push("temperature", { amount: +state.temp.toFixed(2) });
    if (state.fx.has("vignette")) push("vignette");
    if (state.fx.has("grain")) push("grain", { amount: 12 });
    if (state.fx.has("sharpen")) push("sharpen", { amount: 0.8 });
    if (state.fx.has("denoise")) push("denoise");

    if (state.text.trim()) {
      const args = {
        content: state.text.trim(),
        position: state.textPos,
        size: state.textSize,
        color: state.textColor,
        box: state.textBox ? "on" : "off",
      };
      // Caption times are relative to the TRIMMED clip, which is what
      // the person sees. The playhead is in source time, so the trim
      // start has to come off - without this a caption set at 0:08 on a
      // clip trimmed from 0:05 lands three seconds late.
      if (state.textFrom != null || state.textTo != null) {
        args.start = +Math.max(0, (state.textFrom ?? state.in) - state.in)
          .toFixed(2);
        args.end = +Math.max(0, (state.textTo ?? state.out) - state.in)
          .toFixed(2);
      }
      push("text", args);
    }

    if (state.mute) push("mute");
    else if (state.volume !== 1) push("volume", { amount: +state.volume.toFixed(2) });
    if (state.normalize) push("normalize");

    if (state.fadeIn > 0) push("fadein", { seconds: +state.fadeIn.toFixed(1) });
    if (state.fadeOut > 0) push("fadeout", { seconds: +state.fadeOut.toFixed(1) });
    if (state.scale) push("scale", { height: parseInt(state.scale, 10) });
    if (state.gif) push("gif");
    if (state.quality) push("quality", { level: state.quality });
    return ops;
  }

  /* A plain-words summary of what will be exported, each removable.
   * Without it the only record of a stray setting is the control itself,
   * three tabs away - which is how you end up with a mystery slow-motion
   * export and no idea which panel did it. */
  function renderApplied() {
    const box = $("tlApplied");
    const ops = buildOps();
    if (!ops.length) { box.hidden = true; box.textContent = ""; return; }
    box.hidden = false;
    box.textContent = "";
    ops.forEach((o) => {
      const chip = document.createElement("span");
      chip.textContent = describe(o);
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "×";
      x.title = "Remove";
      x.addEventListener("click", () => { undo(o.op); sync(); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }

  function describe(o) {
    const a = o.args || {};
    switch (o.op) {
      case "trim": return `trim ${fmt(a.start)}–${fmt(a.end)}`;
      case "speed": return `${a.factor}× speed`;
      case "aspect": return `${a.ratio}`;
      case "blurfill": return `${a.ratio} blurred bars`;
      case "zoom": return "slow zoom";
      case "look": return a.name;
      case "brightness": return `brightness ${a.amount > 0 ? "+" : ""}${a.amount}`;
      case "contrast": return `contrast ${a.amount}`;
      case "saturation": return `saturation ${a.amount}`;
      case "temperature": return a.amount > 0 ? "warmer" : "cooler";
      case "grain": return "grain";
      case "text": return `caption “${a.content.slice(0, 18)}”`;
      case "volume": return `volume ${a.amount}×`;
      case "fadein": return `fade in ${a.seconds}s`;
      case "fadeout": return `fade out ${a.seconds}s`;
      case "scale": return `${a.height}p`;
      case "quality": return `${a.level} quality`;
      case "rotate": return "rotated";
      case "flip": return "flipped";
      default: return o.op;
    }
  }

  function undo(op) {
    switch (op) {
      case "trim": state.in = 0; state.out = state.duration; break;
      case "speed": state.speed = 1; $("tlSpeed").value = 1; break;
      case "reverse": case "boomerang": state.motion = null; break;
      case "aspect": case "blurfill":
        state.ratio = null; state.blurfill = false;
        $("tlBlurfill").checked = false; break;
      case "rotate": state.rotate = 0; break;
      case "flip": state.flip = false; break;
      case "zoom": state.zoom = 0; $("tlZoom").value = 0; break;
      case "look": state.look = null; break;
      case "brightness": state.bright = 0; $("tlBright").value = 0; break;
      case "contrast": state.contrast = 1; $("tlContrast").value = 1; break;
      case "saturation": state.sat = 1; $("tlSat").value = 1; break;
      case "temperature": state.temp = 0; $("tlTemp").value = 0; break;
      case "vignette": case "grain": case "sharpen": case "denoise":
        state.fx.delete(op); break;
      case "text":
        state.text = ""; $("tlText").value = "";
        state.textFrom = state.textTo = null; break;
      case "mute": state.mute = false; $("tlMute").checked = false; break;
      case "volume": state.volume = 1; $("tlVol").value = 1; break;
      case "normalize": state.normalize = false; $("tlNorm").checked = false; break;
      case "fadein": state.fadeIn = 0; $("tlFadeIn").value = 0; break;
      case "fadeout": state.fadeOut = 0; $("tlFadeOut").value = 0; break;
      case "scale": state.scale = ""; $("tlScale2").value = ""; break;
      case "gif": state.gif = false; $("tlGif").checked = false; break;
      case "quality": state.quality = ""; $("tlQuality").value = ""; break;
      default: break;
    }
  }

  /* Redraw every control from state. Called after anything that can
   * change several at once, so the chips, the toggles and the track can
   * never disagree about what is on. */
  function sync() {
    document.querySelectorAll("#tlMotion button").forEach((b) =>
      b.classList.toggle("is-on", state.motion === b.dataset.op));
    document.querySelectorAll("#tlRatio button").forEach((b) =>
      b.classList.toggle("is-on", state.ratio === b.dataset.ratio));
    document.querySelectorAll("#tlLooks button").forEach((b) =>
      b.classList.toggle("is-on", state.look === b.dataset.look));
    document.querySelectorAll("#tlColourFx button").forEach((b) =>
      b.classList.toggle("is-on", state.fx.has(b.dataset.op)));
    document.querySelectorAll("#tlOrient button").forEach((b) =>
      b.classList.toggle("is-on",
        b.dataset.op === "rotate" ? !!state.rotate : state.flip));
    $("tlTextWhen").textContent =
      state.textFrom == null && state.textTo == null
        ? "shown for the whole clip"
        : `${fmt(state.textFrom ?? state.in)} – ${fmt(state.textTo ?? state.out)}`;
    paint();
  }

  /* ------------------------------------------------------------ dragging */
  let drag = null;
  const posToTime = (clientX) => {
    const r = track.getBoundingClientRect();
    return clamp(((clientX - r.left) / r.width) * state.duration,
      0, state.duration);
  };

  track.addEventListener("pointerdown", (e) => {
    if (!state.duration) return;
    const t = posToTime(e.clientX);
    if (e.target.id === "tlHandleL") drag = "in";
    else if (e.target.id === "tlHandleR") drag = "out";
    else { drag = "head"; video.currentTime = t; }
    track.setPointerCapture(e.pointerId);
    move(e);
  });
  track.addEventListener("pointermove", move);
  track.addEventListener("pointerup", () => { drag = null; });
  track.addEventListener("pointercancel", () => { drag = null; });

  function move(e) {
    if (!drag || !state.duration) return;
    const t = posToTime(e.clientX);
    // A quarter second of clearance. Handles that can cross produce a
    // negative-length trim, which ffmpeg accepts and renders as nothing.
    if (drag === "in") state.in = Math.min(t, state.out - 0.25);
    else if (drag === "out") state.out = Math.max(t, state.in + 0.25);
    else video.currentTime = t;
    paint();
  }

  /* --------------------------------------------------------------- wiring */
  function bindRange(id, key, outId, format) {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => {
      state[key] = parseFloat(el.value);
      if (outId) $(outId).textContent = format(state[key]);
      paint();
    });
  }

  function init() {
    state.duration = video.duration || 0;
    state.in = 0;
    state.out = state.duration;
    $("tlDur").textContent = fmt(state.duration);
    buildStrip();
    sync();
  }

  video.addEventListener("loadedmetadata", init);
  video.addEventListener("timeupdate", () => {
    $("tlHead").style.left =
      ((video.currentTime || 0) / (state.duration || 1)) * 100 + "%";
    $("tlNow").textContent = fmt(video.currentTime || 0);
    // Loop inside the selection while playing, so what you hear is the
    // cut you are making rather than the footage around it.
    if (!video.paused && video.currentTime > state.out) {
      video.currentTime = state.in;
    }
  });
  video.addEventListener("play", () => { $("tlPlay").innerHTML = "&#10073;&#10073;"; });
  video.addEventListener("pause", () => { $("tlPlay").innerHTML = "&#9654;"; });

  $("tlPlay").addEventListener("click", () => {
    if (video.paused) {
      if (video.currentTime < state.in || video.currentTime > state.out) {
        video.currentTime = state.in;
      }
      video.play();
    } else video.pause();
  });

  $("tlSetIn").addEventListener("click", () => {
    state.in = Math.min(video.currentTime, state.out - 0.25); paint();
  });
  $("tlSetOut").addEventListener("click", () => {
    state.out = Math.max(video.currentTime, state.in + 0.25); paint();
  });
  $("tlReset").addEventListener("click", () => {
    const d = state.duration;
    Object.assign(state, {
      in: 0, out: d, speed: 1, motion: null, ratio: null, blurfill: false,
      rotate: 0, flip: false, zoom: 0, look: null, bright: 0, contrast: 1,
      sat: 1, temp: 0, text: "", textFrom: null, textTo: null,
      mute: false, volume: 1, normalize: false, quality: "", scale: "",
      fadeIn: 0, fadeOut: 0, gif: false,
    });
    state.fx.clear();
    tl.querySelectorAll("input[type=range]").forEach((r) => {
      r.value = r.id === "tlSpeed" || r.id === "tlContrast" ||
        r.id === "tlSat" || r.id === "tlVol" ? 1 : 0;
    });
    tl.querySelectorAll("input[type=checkbox]").forEach((c) => {
      c.checked = c.id === "tlTextBox";
    });
    tl.querySelectorAll("select").forEach((sel) => { sel.selectedIndex = 0; });
    $("tlText").value = "";
    ["tlSpeedOut", "tlZoomOut", "tlBrightOut", "tlContrastOut", "tlSatOut",
      "tlTempOut", "tlVolOut", "tlFadeInOut", "tlFadeOutOut"]
      .forEach((id) => { const o = $(id); if (o) o.textContent = ""; });
    $("tlSpeedOut").textContent = "1.00×";
    $("tlVolOut").textContent = "1.00×";
    $("tlZoomOut").textContent = "off";
    $("tlFadeInOut").textContent = "off";
    $("tlFadeOutOut").textContent = "off";
    $("tlBrightOut").textContent = "0";
    $("tlContrastOut").textContent = "1.00";
    $("tlSatOut").textContent = "1.00";
    $("tlTempOut").textContent = "0";
    sync();
  });

  // tabs
  $("tlTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    $("tlTabs").querySelectorAll("button").forEach((x) =>
      x.classList.toggle("is-on", x === b));
    tl.querySelectorAll(".tl-panel").forEach((p) =>
      p.classList.toggle("is-on", p.dataset.panel === b.dataset.tab));
  });

  bindRange("tlSpeed", "speed", "tlSpeedOut", (v) => v.toFixed(2) + "×");
  bindRange("tlZoom", "zoom", "tlZoomOut",
    (v) => (v ? Math.round(v * 100) + "%" : "off"));
  bindRange("tlBright", "bright", "tlBrightOut",
    (v) => (v > 0 ? "+" : "") + v.toFixed(2));
  bindRange("tlContrast", "contrast", "tlContrastOut", (v) => v.toFixed(2));
  bindRange("tlSat", "sat", "tlSatOut", (v) => v.toFixed(2));
  bindRange("tlTemp", "temp", "tlTempOut",
    (v) => (v > 0 ? "+" : "") + v.toFixed(2));
  bindRange("tlVol", "volume", "tlVolOut", (v) => v.toFixed(2) + "×");
  bindRange("tlFadeIn", "fadeIn", "tlFadeInOut",
    (v) => (v ? v.toFixed(1) + "s" : "off"));
  bindRange("tlFadeOut", "fadeOut", "tlFadeOutOut",
    (v) => (v ? v.toFixed(1) + "s" : "off"));

  $("tlMotion").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.motion = state.motion === b.dataset.op ? null : b.dataset.op;
    sync();
  });
  $("tlRatio").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.ratio = state.ratio === b.dataset.ratio ? null : b.dataset.ratio;
    sync();
  });
  $("tlOrient").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.op === "rotate") state.rotate = (state.rotate + 90) % 360;
    else state.flip = !state.flip;
    sync();
  });
  $("tlColourFx").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const op = b.dataset.op;
    if (state.fx.has(op)) state.fx.delete(op); else state.fx.add(op);
    sync();
  });

  const looks = $("tlLooks");
  LOOKS.forEach((name) => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.look = name;
    b.textContent = name;
    looks.appendChild(b);
  });
  looks.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.look = state.look === b.dataset.look ? null : b.dataset.look;
    sync();
  });

  $("tlBlurfill").addEventListener("change", (e) => {
    state.blurfill = e.target.checked; paint();
  });
  $("tlText").addEventListener("input", (e) => {
    state.text = e.target.value; paint();
  });
  ["tlTextPos:textPos", "tlTextSize:textSize", "tlTextColor:textColor",
    "tlQuality:quality", "tlScale2:scale"].forEach((pair) => {
      const [id, key] = pair.split(":");
      $(id).addEventListener("change", (e) => {
        state[key] = e.target.value; paint();
      });
    });
  $("tlTextBox").addEventListener("change", (e) => {
    state.textBox = e.target.checked; paint();
  });
  $("tlTextFrom").addEventListener("click", () => {
    state.textFrom = video.currentTime; sync();
  });
  $("tlTextTo").addEventListener("click", () => {
    state.textTo = video.currentTime; sync();
  });
  $("tlMute").addEventListener("change", (e) => {
    state.mute = e.target.checked; paint();
  });
  $("tlNorm").addEventListener("change", (e) => {
    state.normalize = e.target.checked; paint();
  });
  $("tlGif").addEventListener("change", (e) => {
    state.gif = e.target.checked; paint();
  });

  // Export. Hands the ops to whatever the page uses to run a render -
  // window.runVideoOps is defined in script.js, which owns the upload
  // name, the job polling and the result player. Duplicating that here
  // would be a second place for the same bugs.
  $("tlApply").addEventListener("click", () => {
    const ops = buildOps();
    if (!ops.length) return;
    if (typeof window.runVideoOps === "function") window.runVideoOps(ops);
  });

  window.addEventListener("resize", () => {
    // Only the strip depends on width; the rest is percentage-based.
    clearTimeout(window.__tlResize);
    window.__tlResize = setTimeout(() => {
      if (state.duration) buildStrip();
    }, 300);
  });
})();
