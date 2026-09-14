/**
 * The video studio: controls, live log, and the grid of clips.
 *
 * WHAT THIS IS BUILT ON
 * The server reports, per backend and per model, exactly which
 * parameters the provider honours (/api/video/status). Every control
 * here is shown or hidden from that: a duration select lists only the
 * lengths the model makes, the seed field is gone for a model that
 * takes none, and the aspect picker disappears when the model decides
 * the shape. A control that would be silently ignored is not offered.
 *
 * Jobs are the server's (they live in its database and survive a
 * restart); this module polls the ones still running, streams their
 * log lines into the panel, and turns finished ones into cards. Several
 * jobs can run at once - the grid is the batch view.
 *
 * Diagrams still live in this bay when no video backend is configured;
 * applyVideoAccess() decides which the bay is, and diagram.js owns the
 * drawing.
 */
import { state } from "./state.js";
import { renderGenExamples } from "./bays.js";
import { updateGenBayLabel } from "./boot.js";
import { getJSON, postJSON, del, explain } from "./api.js";
import { toast } from "./toast.js";
import { confirmDialog } from "./confirm.js";
import {
  genExamples, genForm, genLocked, genLockedText, genPrompt, genQuotaEl,
  genRun, genSeconds, genRatio, genQuality, genSub, genTitle, videoBay,
  videoStatusEl, videoResult, modelOut, videoDownload,
} from "./dom.js";

const $ = (id) => document.getElementById(id);

/** @type {any} the last /api/video/status payload */
let status = null;
/** @type {Map<string, any>} job id -> the latest job row we have */
const jobs = new Map();
/** @type {Map<string, number>} job id -> how many log lines are shown */
const shownLog = new Map();
/** @type {number|null} */
let pollTimer = null;
/** @type {string|null} a data URL for image-to-video, when one is set */
let startImage = null;

const POLL_MS = 3000;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

/* ------------------------------------------------------------ helpers */
export function videoSay(msg, isError) {
  if (!videoStatusEl) return;
  videoStatusEl.hidden = !msg;
  videoStatusEl.textContent = msg || "";
  videoStatusEl.classList.toggle("is-error", !!isError);
}

export function renderQuota(q) {
  if (!genQuotaEl || !q) return;
  if (!q.allowed) { genQuotaEl.textContent = ""; return; }
  const period = q.period === "day" ? "today" : "this month";
  genQuotaEl.textContent = `${q.remaining} of ${q.limit} left ${period}`;
}

function fillSelect(select, options, value) {
  select.textContent = "";
  options.forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = String(v);
    o.textContent = label;
    select.appendChild(o);
  });
  if (value != null && [...select.options].some((o) => o.value === String(value))) {
    select.value = String(value);
  }
}

function show(id, on) {
  const el = $(id);
  if (el) el.hidden = !on;
}

function backendById(id) {
  return (status && status.backends || []).find((b) => b.id === id) || null;
}

function currentBackend() {
  const sel = /** @type {HTMLSelectElement|null} */ ($("genBackend"));
  return backendById(sel && sel.value) || (status && status.backends && status.backends[0]) || null;
}

function currentModel() {
  const b = currentBackend();
  const sel = /** @type {HTMLSelectElement|null} */ ($("genModel"));
  if (!b || !b.models || !b.models.length) return null;
  return b.models.find((m) => m.id === (sel && sel.value)) || b.models[0];
}

/** The backend's controls with the model's own limits laid over them -
 *  the same merge the server does in videogen.caps_for(). */
function caps() {
  const b = currentBackend();
  if (!b) return null;
  const m = currentModel();
  const c = { ...b };
  if (m) {
    for (const k of ["seconds", "seconds_discrete", "ratios", "resolutions",
                     "negative", "seed", "image_to_video", "text_to_video"]) {
      if (k in m) c[k] = m[k];
    }
  }
  return c;
}

function motionValue() {
  const on = $("genMotion") && $("genMotion").querySelector('[aria-checked="true"]');
  return on ? on.getAttribute("data-v") : "medium";
}

function setMotion(v) {
  const box = $("genMotion");
  if (!box) return;
  box.querySelectorAll("[role=radio]").forEach((b) => {
    b.setAttribute("aria-checked", b.getAttribute("data-v") === v ? "true" : "false");
  });
}

/* ----------------------------------------------------- the controls */
function renderBackends() {
  const sel = /** @type {HTMLSelectElement} */ ($("genBackend"));
  const list = (status.backends || []).map((b) => [b.id, b.label]);
  fillSelect(sel, list, sel.value || status.backend);
  show("ctlBackend", list.length > 1);
}

/** The model list for the chosen backend. A re-render keeps whatever
 *  is selected; only a backend change falls back to the default. */
function renderModels() {
  const b = currentBackend();
  const sel = /** @type {HTMLSelectElement} */ ($("genModel"));
  const models = (b && b.models) || [];
  const keep = models.some((m) => m.id === sel.value) ? sel.value : (b && b.default_model);
  fillSelect(sel, models.map((m) => [m.id, m.label]), keep);
  show("ctlModel", models.length > 1);
  // A model that can only start from an image, or only from text, is
  // said so in the list rather than discovered by a 400.
  [...sel.options].forEach((o) => {
    const m = models.find((x) => x.id === o.value);
    if (!m) return;
    const i2vOnly = m.image_to_video && m.text_to_video === false;
    const t2vOnly = m.text_to_video !== false && m.image_to_video === false;
    o.textContent = m.label + (i2vOnly ? " · image only" : t2vOnly ? " · text only" : "");
    o.disabled = startImage ? m.image_to_video === false : m.text_to_video === false;
  });
  if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled) {
    const ok = [...sel.options].find((o) => !o.disabled);
    if (ok) sel.value = ok.value;
  }
}

/** Every dependent control, from what the chosen model accepts. */
function renderControls() {
  const c = caps();
  if (!c) return;
  const kind = c.kind || "video";

  // Length: the model's own set (Kling 5/10, Veo 4/6/8) or its range.
  const secs = c.seconds || [1, 8];
  const options = c.seconds_discrete
    ? secs.map((n) => [n, n + "s"])
    : Array.from({ length: secs[secs.length - 1] - secs[0] + 1 }, (_, i) => [secs[0] + i, (secs[0] + i) + "s"]);
  const want = genSeconds.value || c.default_seconds;
  fillSelect(genSeconds, options, want);
  if (!genSeconds.value) genSeconds.value = String(options[0][0]);
  show("ctlSeconds", kind === "video" && options.length > 1);

  const ratios = c.ratios || [];
  const ratioLabel = { "16:9": "16:9 wide", "9:16": "9:16 vertical", "1:1": "1:1 square",
                       "4:3": "4:3", "3:4": "3:4", "21:9": "21:9 cinema" };
  fillSelect(genRatio, ratios.map((r) => [r, ratioLabel[r] || r]), genRatio.value || "16:9");
  show("ctlRatio", kind === "video" && ratios.length > 1);

  const res = c.resolutions || [];
  fillSelect(genQuality, res.map((r) => [r, r]), genQuality.value || c.default_resolution);
  show("ctlResolution", kind === "video" && res.length > 1);

  show("ctlMotion", kind === "video" && !!c.motion);
  show("ctlSeed", !!c.seed);
  show("ctlNegative", !!c.negative);
  show("ctlImage", !!c.image_to_video);
  show("ctlCount", kind === "video");
  show("ctlEnhance", !!status.enhancer);
  show("genPreview", !!status.enhancer);
}

/* ---------------------------------------------------- access + kind */
/* Locking the form rather than hiding the bay. Someone on Free should be
   able to see what the feature is and what it costs before deciding to
   pay for it - a bay that simply is not there sells nothing.

   DIAGRAM IS THE FLOOR, not the last resort of a broken chain: with no
   usable media backend - none configured, a plan without video, a guest
   who needs an account, a 3D key with an empty balance - the bay draws
   diagrams, which need no key and no plan. */
export function applyVideoAccess(d) {
  status = d;
  const q = d.quota || {};
  const entitled = !d.quota || d.quota.allowed !== false;
  const first = (d.backends || [])[0];
  const mediaUsable = d.configured && entitled
    && (!first || first.credits === undefined || first.credits === null || first.credits > 0);
  const kind = !mediaUsable ? "diagram" : d.kind === "model" ? "model" : "video";

  state.genKind = kind;
  updateGenBayLabel(kind);
  const studio = $("videoStudio");

  if (kind === "diagram") {
    genLocked.hidden = true;
    genForm.hidden = false;
    if (studio) studio.hidden = true;
    show("videoGrid", false);
    show("videoLog", false);
    if (genTitle) genTitle.textContent = "Draw a diagram";
    if (genSub) genSub.textContent = "Describe a system, a flow or a schema and see it drawn.";
    if (genPrompt) {
      genPrompt.placeholder = "How an OAuth 2.0 authorization code flow works, including the token exchange";
    }
    if (genQuotaEl) genQuotaEl.textContent = "";
    renderGenExamples("diagram");
    if (!d.configured && d.detail) {
      // The server says which key is missing; the person reading this
      // is usually the one who can set it. Said once, quietly.
      videoSay("");
    }
    return;
  }

  genLocked.hidden = true;
  genForm.hidden = false;
  if (studio) studio.hidden = false;
  if (genTitle) genTitle.textContent = kind === "model" ? "Make a 3D model" : "Make a video";
  if (genSub) {
    genSub.textContent = kind === "model"
      ? "Describe an object and get back a 3D model you can spin."
      : "Describe a shot. The brief is engineered - camera, lighting, motion - then rendered.";
  }
  if (genPrompt) {
    genPrompt.placeholder = kind === "model"
      ? "A weathered brass diving helmet with a cracked glass port"
      : "A red fox trotting through fresh snow in a birch forest at dawn";
  }
  renderQuota(q);
  renderBackends();
  renderModels();
  renderControls();
  show("videoGrid", true);
  loadJobs();
}

export async function loadVideoLimits() {
  if (!videoBay) return;
  try {
    applyVideoAccess(await getJSON("/api/video/status"));
  } catch (_) {
    /* Leave the form as the markup has it. A status call failing is not
       a reason to take the feature away - the generate call will report
       anything genuinely wrong, with a real message. */
  }
}

/* ------------------------------------------------------- the brief */
async function previewBrief() {
  const prompt = (genPrompt.value || "").trim();
  const box = $("genBrief");
  if (!prompt || !box) { genPrompt.focus(); return; }
  const btn = /** @type {HTMLButtonElement} */ ($("genPreview"));
  btn.disabled = true;
  box.hidden = false;
  box.textContent = "Writing the brief…";
  try {
    const b = currentBackend();
    const m = currentModel();
    const d = await postJSON("/api/video/enhance", {
      prompt, backend: b && b.id, model: m && m.id, motion: motionValue(),
      negative: ($("genNegative") || {}).value || "",
    });
    renderBrief(box, d);
  } catch (err) {
    box.textContent = explain(err, "Could not write the brief.");
  } finally {
    btn.disabled = false;
  }
}

function renderBrief(box, d) {
  box.textContent = "";
  if (!d.enhanced) {
    box.textContent = "No model was available to expand this; your own words will be sent as they are.";
    return;
  }
  const dl = document.createElement("dl");
  for (const key of ["subject", "action", "setting", "camera", "lighting", "style", "negative"]) {
    const v = d.brief && d.brief[key];
    if (!v) continue;
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  box.appendChild(dl);
  const use = document.createElement("button");
  use.type = "button";
  use.className = "studio-link";
  use.textContent = "Use this as the prompt";
  use.addEventListener("click", () => {
    genPrompt.value = d.prompt;
    if ($("genNegative")) $("genNegative").value = d.negative || "";
    if ($("genEnhance")) $("genEnhance").checked = false;   // already engineered
    box.hidden = true;
    genPrompt.focus();
  });
  box.appendChild(use);
}

/* ----------------------------------------------------- submitting */
function requestBody(prompt, seedOffset) {
  const b = currentBackend();
  const m = currentModel();
  const seedRaw = ($("genSeed") || {}).value;
  const seed = seedRaw ? Math.max(1, (parseInt(seedRaw, 10) + seedOffset) % 1000000) : null;
  const body = {
    prompt,
    backend: b && b.id,
    model: m && m.id,
    seconds: parseInt(genSeconds.value, 10) || undefined,
    ratio: genRatio.value || undefined,
    resolution: genQuality.value || undefined,
    motion: motionValue(),
    seed,
    negative: ($("genNegative") || {}).value || "",
    enhance: !$("genEnhance") || $("genEnhance").checked,
  };
  if (startImage) body.image = startImage;
  return body;
}

/** Called by diagram.js's submit handler when the bay is not drawing. */
export async function submitVideo(prompt) {
  const count = Math.max(1, Math.min(4, parseInt(($("genCount") || {}).value || "1", 10)));
  genRun.disabled = true;
  videoSay(count > 1 ? `Sending ${count} over…` : "Sending it over…");
  let started = 0;
  for (let i = 0; i < count; i++) {
    let d;
    try {
      d = await postJSON("/api/video/generate", requestBody(prompt, i));
    } catch (err) {
      const body = (err && err.body) || {};
      videoSay(body.error || explain(err, "Could not start that."), true);
      if (body.quota) renderQuota(body.quota);
      if (body.upgrade_required || body.needs_account) {
        const pro = $("planProBtn");
        if (pro) pro.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      break;
    }
    started++;
    if (d.quota) renderQuota(d.quota);
    track(d.job);
  }
  genRun.disabled = false;
  if (started) {
    videoSay("");
    show("videoLog", true);
    show("videoGrid", true);
  }
}

/* --------------------------------------------------- job tracking */
function track(job) {
  jobs.set(job.id, job);
  renderCard(job);
  appendLog(job);
  if (isActive(job) && pollTimer == null) pollTimer = window.setInterval(pollActive, POLL_MS);
}

function isActive(job) {
  return job.status === "queued" || job.status === "running" || job.status === "validating";
}

async function pollActive() {
  const active = [...jobs.values()].filter(isActive);
  if (!active.length) {
    window.clearInterval(pollTimer);
    pollTimer = null;
    return;
  }
  for (const j of active) {
    let d;
    try {
      d = await getJSON(`/api/video/job/${j.id}`);
    } catch (err) {
      if (err && err.status === 404) {
        jobs.delete(j.id);
        removeCard(j.id);
      }
      continue;
    }
    jobs.set(j.id, d.job);
    renderCard(d.job);
    appendLog(d.job);
    if (d.quota) renderQuota(d.quota);
    if (!isActive(d.job)) finished(d.job);
  }
}

function finished(job) {
  if (job.status === "done") {
    toast(job.params && job.params.kind === "model" ? "Your model is ready." : "Your clip is ready.",
          { kind: "success", id: "video-" + job.id });
    if (job.params && job.params.kind === "model" && modelOut) {
      modelOut.src = job.url;
      modelOut.setAttribute("alt", job.prompt || "Generated 3D model");
      modelOut.hidden = false;
      videoResult.hidden = false;
      if (videoDownload) {
        videoDownload.hidden = false;
        videoDownload.href = `/api/video/job/${job.id}/download`;
        videoDownload.textContent = "Download GLB";
      }
    }
  } else if (job.status === "failed") {
    toast(job.error || "That one didn't work. Your allowance wasn't used.",
          { kind: "error", id: "video-" + job.id, timeout: 9000 });
  }
}

async function loadJobs() {
  let d;
  try {
    d = await getJSON("/api/video/jobs");
  } catch (_) {
    return;
  }
  const cards = $("videoCards");
  if (cards) cards.textContent = "";
  jobs.clear();
  // Oldest first into the grid so the newest ends up on top.
  for (const j of [...d.jobs].reverse()) {
    jobs.set(j.id, j);
    renderCard(j);
    if (isActive(j)) appendLog(j);
  }
  if (d.quota) renderQuota(d.quota);
  if ([...jobs.values()].some(isActive)) {
    show("videoLog", true);
    if (pollTimer == null) pollTimer = window.setInterval(pollActive, POLL_MS);
  }
  updateCount();
}

/* ------------------------------------------------------ the log */
function appendLog(job) {
  const list = $("videoLogList");
  if (!list || !job.log) return;
  const seen = shownLog.get(job.id) || 0;
  const tag = job.id.slice(0, 6);
  job.log.slice(seen).forEach(([when, text]) => {
    const li = document.createElement("li");
    li.className = /^Failed/.test(text) ? "is-error" : /^Done/.test(text) ? "is-done" : "";
    const t = document.createElement("time");
    t.dateTime = when;
    t.textContent = new Date(when).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const id = document.createElement("code");
    id.textContent = tag;
    const msg = document.createElement("span");
    msg.textContent = text;
    li.append(t, id, msg);
    list.appendChild(li);
  });
  shownLog.set(job.id, job.log.length);
  while (list.children.length > 80) list.removeChild(list.firstChild);
  list.lastElementChild && list.lastElementChild.scrollIntoView({ block: "nearest" });
}

/* ------------------------------------------------------ the grid */
function updateCount() {
  const n = jobs.size;
  const el = $("videoGridCount");
  if (el) el.textContent = n ? `${n} ${n === 1 ? "clip" : "clips"}` : "";
  const grid = $("videoGrid");
  if (grid && state.genKind !== "diagram") grid.hidden = false;
  const empty = $("videoCards");
  if (empty) empty.classList.toggle("is-empty", n === 0);
}

function removeCard(id) {
  const card = document.querySelector(`.clip-card[data-id="${CSS.escape(id)}"]`);
  if (card) card.remove();
  updateCount();
}

function metaLine(job) {
  const p = job.params || {};
  const bits = [];
  if (p.model) bits.push(p.model);
  else if (job.backend) bits.push(job.backend);
  if (job.width && job.height) bits.push(`${job.width}×${job.height}`);
  else if (p.resolution && p.kind !== "model") bits.push(p.resolution);
  if (job.duration) bits.push(`${Number(job.duration).toFixed(1)}s`);
  else if (p.seconds && p.kind !== "model") bits.push(`${p.seconds}s`);
  if (p.ratio) bits.push(p.ratio);
  if (job.seed != null) bits.push(`seed ${job.seed}`);
  if (p.motion && p.kind !== "model") bits.push(`${p.motion} motion`);
  return bits.join(" · ");
}

function renderCard(job) {
  const cards = $("videoCards");
  if (!cards) return;
  let card = cards.querySelector(`.clip-card[data-id="${CSS.escape(job.id)}"]`);
  const fresh = !card;
  if (fresh) {
    card = document.createElement("article");
    card.className = "clip-card";
    card.dataset.id = job.id;
  }
  card.dataset.status = job.status;
  card.textContent = "";

  const media = document.createElement("div");
  media.className = "clip-media";
  if (job.status === "done" && job.url && (job.params || {}).kind !== "model") {
    const v = document.createElement("video");
    v.src = job.url;
    v.preload = "metadata";
    v.muted = true;
    v.loop = true;
    v.playsInline = true;
    v.setAttribute("controlslist", "nodownload noremoteplayback");
    v.addEventListener("mouseenter", () => { v.play().catch(() => {}); });
    v.addEventListener("mouseleave", () => { v.pause(); });
    v.addEventListener("click", () => {
      v.controls = true;
      v.muted = false;
      v.play().catch(() => {});
    });
    media.appendChild(v);
  } else if (job.status === "done") {
    const m = document.createElement("div");
    m.className = "clip-placeholder";
    m.textContent = "◆ 3D model";
    media.appendChild(m);
  } else {
    const m = document.createElement("div");
    m.className = "clip-placeholder";
    const last = job.log && job.log.length ? job.log[job.log.length - 1][1] : "";
    if (job.status === "failed") {
      m.classList.add("is-error");
      m.textContent = job.error || "Failed.";
    } else if (job.status === "cancelled") {
      m.textContent = "Cancelled.";
    } else {
      const spin = document.createElement("span");
      spin.className = "clip-spinner";
      spin.setAttribute("aria-hidden", "true");
      const t = document.createElement("span");
      t.textContent = job.status === "queued" ? "Queued…" : last || "Rendering…";
      m.append(spin, t);
    }
    media.appendChild(m);
  }
  card.appendChild(media);

  const body = document.createElement("div");
  body.className = "clip-body";
  const prompt = document.createElement("p");
  prompt.className = "clip-prompt";
  prompt.textContent = job.prompt;
  prompt.title = job.enhanced ? "Engineered brief:\n" + job.enhanced : job.prompt;
  const meta = document.createElement("p");
  meta.className = "clip-meta";
  meta.textContent = metaLine(job);
  body.append(prompt, meta);
  card.appendChild(body);

  const actions = document.createElement("div");
  actions.className = "clip-actions";
  const btn = (label, fn, cls) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (cls) b.className = cls;
    b.addEventListener("click", fn);
    actions.appendChild(b);
    return b;
  };
  if (job.status === "done") {
    const a = document.createElement("a");
    a.href = `/api/video/job/${job.id}/download`;
    a.textContent = "Download";
    a.className = "clip-download";
    actions.appendChild(a);
  }
  btn("Reuse prompt", () => {
    genPrompt.value = job.prompt;
    if ($("genNegative")) $("genNegative").value = job.negative || "";
    if (job.params) {
      if (job.params.motion) setMotion(job.params.motion);
      if (job.params.seconds && genSeconds) genSeconds.value = String(job.params.seconds);
      if (job.params.ratio && genRatio) genRatio.value = job.params.ratio;
      if (job.params.resolution && genQuality) genQuality.value = job.params.resolution;
    }
    genPrompt.focus();
    genPrompt.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  if (job.seed != null) {
    btn("Reuse seed", () => {
      if ($("genSeed")) $("genSeed").value = String(job.seed);
      toast(`Seed ${job.seed} set.`, { id: "seed" });
    });
  }
  if (isActive(job)) {
    btn("Cancel", () => deleteJob(job, true), "is-danger");
  } else {
    btn("Delete", () => deleteJob(job, false), "is-danger");
  }
  card.appendChild(actions);

  if (fresh) cards.prepend(card);
  updateCount();
}

async function deleteJob(job, cancelling) {
  const ok = await confirmDialog({
    title: cancelling ? "Cancel this render?" : "Delete this clip?",
    body: cancelling
      ? "If the provider has already started it, the allowance is spent."
      : "The file is removed from the server. This cannot be undone.",
    confirmLabel: cancelling ? "Cancel render" : "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await del(`/api/video/job/${job.id}`);
  } catch (err) {
    toast(explain(err, "Could not remove it."), { kind: "error" });
    return;
  }
  jobs.delete(job.id);
  removeCard(job.id);
  const q = await getJSON("/api/video/jobs").catch(() => null);
  if (q && q.quota) renderQuota(q.quota);
}

/* -------------------------------------------------- the start image */
function setStartImage(file) {
  const thumb = /** @type {HTMLImageElement} */ ($("genImageThumb"));
  const clear = $("genImageClear");
  if (!file) {
    startImage = null;
    if (thumb) { thumb.hidden = true; thumb.removeAttribute("src"); }
    if (clear) clear.hidden = true;
    if ($("genImage")) $("genImage").value = "";
    renderModels();
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    toast("Keep the start image under 8 MB.", { kind: "error" });
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    startImage = String(reader.result);
    if (thumb) { thumb.src = startImage; thumb.hidden = false; }
    if (clear) clear.hidden = false;
    renderModels();
  };
  reader.readAsDataURL(file);
}

/* --------------------------------------------------------- mount */
/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountVideo() {
  if (genExamples) {
    genExamples.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-ex]");
      if (!b) return;
      genPrompt.value = b.dataset.ex;
      genPrompt.focus();
    });
  }

  const backend = $("genBackend");
  if (backend) backend.addEventListener("change", () => { renderModels(); renderControls(); });
  const model = $("genModel");
  if (model) model.addEventListener("change", renderControls);

  const motion = $("genMotion");
  if (motion) {
    motion.addEventListener("click", (e) => {
      const b = e.target.closest("[role=radio]");
      if (b) setMotion(b.getAttribute("data-v"));
    });
    motion.addEventListener("keydown", (e) => {
      const order = ["low", "medium", "high"];
      const i = order.indexOf(motionValue());
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); setMotion(order[(i + 1) % 3]); }
      if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); setMotion(order[(i + 2) % 3]); }
    });
  }

  const dice = $("genSeedDice");
  if (dice) {
    dice.addEventListener("click", () => {
      $("genSeed").value = String(1 + Math.floor(Math.random() * 999999));
    });
  }

  const preview = $("genPreview");
  if (preview) preview.addEventListener("click", previewBrief);

  const image = $("genImage");
  if (image) image.addEventListener("change", () => setStartImage(image.files && image.files[0]));
  const clear = $("genImageClear");
  if (clear) clear.addEventListener("click", () => setStartImage(null));

  const logClear = $("videoLogClear");
  if (logClear) {
    logClear.addEventListener("click", () => {
      const list = $("videoLogList");
      if (list) list.textContent = "";
    });
  }
}
