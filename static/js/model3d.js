import { explain, getJSON, postJSON } from "./api.js";
import { showPaneMode } from "./artifacts.js";
import { loadModelViewer } from "./video.js";

/* ----------------------------------------------------------------
   The 3D studio - a picture or a description in, a textured GLB out
   (TRELLIS.2, see model3d.py). It lives in the workspace pane, like the
   browser, so a model can sit beside the conversation about it.

   Ways in: the 3D button by the modes, + -> Make a 3D model, and the
   card the chat shows when someone asks for a model in a message.

   A job is polled until it is done or failed; the chat cards and the
   studio share one poll per job (watch()).
   ---------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const POLL_MS = 2500;

let resolution = "512";
/** @type {File | null} */
let picked = null;
let currentJob = "";
let status = null;

/* ---- one poll per job, any number of listeners -------------------------- */
/** @type {Map<string, {subs: Set<Function>, timer: number, last: any}>} */
const watching = new Map();

/** Call `fn(job)` now and on every change until the job ends. */
function watch(jobId, fn) {
  let w = watching.get(jobId);
  if (!w) {
    w = { subs: new Set(), timer: 0, last: null };
    watching.set(jobId, w);
    tick(jobId);
  } else if (w.last) {
    fn(w.last);
  }
  w.subs.add(fn);
  return () => w.subs.delete(fn);
}

async function tick(jobId) {
  const w = watching.get(jobId);
  if (!w) return;
  let job = null;
  try {
    job = (await getJSON(`/api/3d/job/${encodeURIComponent(jobId)}`)).job;
  } catch (err) {
    job = { id: jobId, status: "failed", error: explain(err, "That model could not be found.") };
  }
  w.last = job;
  w.subs.forEach((fn) => { try { fn(job); } catch (_) { /* a listener's problem */ } });
  if (job.status === "queued" || job.status === "running") {
    w.timer = window.setTimeout(() => tick(jobId), POLL_MS);
  } else {
    watching.delete(jobId);
    if (jobId === currentJob || $("artifactPane")?.dataset.mode === "model3d") {
      loadHistory();
      loadStatus();
    }
  }
}

/* ---- helpers --------------------------------------------------------------- */
function elapsed(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function sizeText(bytes) {
  if (!bytes) return "";
  return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
}

function label(job) {
  return job.source === "text" ? job.prompt || "From a description" : "From a picture";
}

function note(text, isError = false) {
  const el = $("m3dNote");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("is-error", !!isError);
}

/* ---- the studio ------------------------------------------------------------ */
async function loadStatus() {
  try {
    status = await getJSON("/api/3d/status");
  } catch (_) {
    status = null;
  }
  const go = /** @type {HTMLButtonElement|null} */ ($("m3dGo"));
  if (!status || !status.available) {
    note("3D models aren't set up on this server yet.", true);
    if (go) go.disabled = true;
    return;
  }
  if (go) go.disabled = false;
  if (!status.signed_in) {
    note("Make a free account to create 3D models.");
    return;
  }
  const q = status.quota || {};
  note(q.limit ? `${q.left} of ${q.limit} models left today.` : "");
}

async function loadHistory() {
  const box = $("m3dHistory");
  if (!box) return;
  let jobs = [];
  try {
    jobs = (await getJSON("/api/3d/jobs")).jobs || [];
  } catch (_) { /* leave the list as it was */ return; }
  box.replaceChildren();
  if (!jobs.length) {
    const p = document.createElement("p");
    p.className = "m3d-empty";
    p.textContent = "Models you make appear here.";
    box.appendChild(p);
    return;
  }
  for (const job of jobs) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "m3d-item" + (job.id === currentJob ? " is-current" : "");
    const thumb = document.createElement("span");
    thumb.className = "m3d-thumb";
    if (job.image_url) {
      const img = document.createElement("img");
      img.src = job.image_url;
      img.alt = "";
      img.loading = "lazy";
      thumb.appendChild(img);
    }
    const text = document.createElement("span");
    text.className = "m3d-item-text";
    const t = document.createElement("span");
    t.className = "m3d-item-title";
    t.textContent = label(job);
    const sub = document.createElement("small");
    sub.textContent = job.status === "done" ? `${job.resolution} · ${sizeText(job.glb_bytes)}`
      : job.status === "failed" ? "Failed" : "Making…";
    text.append(t, sub);
    row.append(thumb, text);
    row.addEventListener("click", () => showJob(job.id));
    box.appendChild(row);
  }
}

/** Show one job in the studio's result area and follow it. */
function showJob(jobId) {
  currentJob = jobId;
  const result = $("m3dResult");
  if (result) result.hidden = false;
  $("m3dViewerWrap").hidden = true;
  $("m3dActions").hidden = true;
  $("m3dStage").hidden = false;
  $("m3dStageText").textContent = "Starting…";
  $("m3dElapsed").textContent = "";
  document.querySelectorAll("#m3dHistory .m3d-item").forEach((el) => el.classList.remove("is-current"));
  watch(jobId, (job) => {
    if (currentJob !== jobId) return;
    drawJob(job);
  });
}

function drawJob(job) {
  const stage = $("m3dStage");
  const stageText = $("m3dStageText");
  if (job.status === "done") {
    stage.hidden = true;
    stage.classList.remove("is-error");
    showModel(/** @type {any} */ ($("m3dViewer")), $("m3dViewerWrap"), job.glb_url);
    const dl = /** @type {HTMLAnchorElement} */ ($("m3dDownload"));
    dl.href = job.download_url;
    $("m3dMeta").textContent = `${job.resolution} · ${sizeText(job.glb_bytes)} · made in ${elapsed(job.elapsed)}`;
    $("m3dActions").hidden = false;
  } else if (job.status === "failed") {
    stage.hidden = false;
    stage.classList.add("is-error");
    stageText.textContent = job.error || "That model could not be made.";
    $("m3dElapsed").textContent = "";
  } else {
    stage.hidden = false;
    stage.classList.remove("is-error");
    stageText.textContent = job.stage || (job.status === "queued" ? "Waiting for a free slot" : "Working");
    $("m3dElapsed").textContent = elapsed(job.elapsed);
  }
}

async function showModel(viewer, wrap, url) {
  if (!viewer || !wrap || !url) return;
  wrap.hidden = false;
  await loadModelViewer();
  if (viewer.getAttribute("src") !== url) viewer.setAttribute("src", url);
}

function setPicked(file) {
  picked = file;
  const prev = /** @type {HTMLImageElement} */ ($("m3dPreview"));
  const text = $("m3dDropText");
  const clear = $("m3dClear");
  if (file) {
    if (prev.src) URL.revokeObjectURL(prev.src);
    prev.src = URL.createObjectURL(file);
    prev.hidden = false;
    text.hidden = true;
    clear.hidden = false;
  } else {
    prev.hidden = true;
    prev.removeAttribute("src");
    text.hidden = false;
    clear.hidden = true;
    const input = /** @type {HTMLInputElement} */ ($("m3dFile"));
    if (input) input.value = "";
  }
}

function checkFile(file) {
  if (!file) return "";
  if (!/^image\/(png|jpeg|webp)$/.test(file.type)) return "Use a PNG, JPEG or WebP image.";
  if (file.size > 10 * 1024 * 1024) return "Keep the image under 10 MB.";
  return "";
}

async function generate() {
  const prompt = /** @type {HTMLInputElement} */ ($("m3dPrompt")).value.trim();
  if (!picked && !prompt) {
    note("Add a picture, or describe the object.", true);
    return;
  }
  const go = /** @type {HTMLButtonElement} */ ($("m3dGo"));
  go.disabled = true;
  note("Starting…");
  try {
    let data;
    if (picked) {
      const form = new FormData();
      form.append("image", picked);
      form.append("resolution", resolution);
      const res = await fetch("/api/3d/generate", { method: "POST", body: form });
      data = await res.json().catch(() => ({}));
      if (!res.ok) throw Object.assign(new Error(data.error || "That could not be started."), { body: data });
    } else {
      data = await postJSON("/api/3d/generate", { prompt, resolution });
    }
    const q = data.quota || {};
    note(q.limit ? `${q.left} of ${q.limit} models left today.` : "");
    showJob(data.job.id);
    loadHistory();
  } catch (err) {
    note(explain(err, "That could not be started."), true);
  } finally {
    go.disabled = false;
  }
}

/** Open the studio, optionally on one job. @param {{jobId?: string}} [opts] */
export function open3D(opts = {}) {
  showPaneMode("model3d");
  const title = $("artifactTitle");
  const lang = $("artifactLang");
  if (title) title.textContent = "3D studio";
  if (lang) lang.textContent = "TRELLIS.2";
  loadStatus();
  loadHistory();
  if (opts.jobId) showJob(opts.jobId);
}

/* ---- the card in the chat ---------------------------------------------- */
/** A live card for a 3D job, under the message that asked for it. */
export function renderModel3dCard(msgEl, jobId) {
  if (!msgEl || !jobId || msgEl.querySelector(`.m3d-card[data-job="${CSS.escape(jobId)}"]`)) return;
  const card = document.createElement("div");
  card.className = "m3d-card";
  card.dataset.job = jobId;
  const head = document.createElement("div");
  head.className = "m3d-card-head";
  const icon = document.createElement("span");
  icon.className = "m3d-spinner";
  const text = document.createElement("span");
  text.className = "m3d-card-text";
  text.textContent = "Making a 3D model…";
  const time = document.createElement("span");
  time.className = "m3d-elapsed";
  head.append(icon, text, time);
  const wrap = document.createElement("div");
  wrap.className = "m3d-card-viewer";
  wrap.hidden = true;
  const viewer = document.createElement("model-viewer");
  ["camera-controls", "auto-rotate"].forEach((a) => viewer.setAttribute(a, ""));
  viewer.setAttribute("environment-image", "neutral");
  viewer.setAttribute("shadow-intensity", "0.8");
  viewer.setAttribute("touch-action", "pan-y");
  viewer.setAttribute("alt", "The generated 3D model");
  wrap.appendChild(viewer);
  const actions = document.createElement("div");
  actions.className = "m3d-card-actions";
  actions.hidden = true;
  const dl = document.createElement("a");
  dl.className = "m3d-download";
  dl.textContent = "Download GLB";
  dl.setAttribute("download", "");
  const openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = "m3d-open";
  openBtn.textContent = "Open in 3D studio";
  openBtn.addEventListener("click", () => open3D({ jobId }));
  actions.append(dl, openBtn);
  card.append(head, wrap, actions);
  const bubble = msgEl.querySelector(".msg-bubble");
  if (bubble) msgEl.insertBefore(card, bubble);
  else msgEl.appendChild(card);

  watch(jobId, (job) => {
    if (job.status === "done") {
      icon.hidden = true;
      card.classList.remove("is-error");
      text.textContent = job.source === "text" ? `3D model: ${job.prompt}` : "3D model from your picture";
      time.textContent = `${job.resolution} · ${sizeText(job.glb_bytes)}`;
      dl.href = job.download_url;
      actions.hidden = false;
      showModel(viewer, wrap, job.glb_url);
    } else if (job.status === "failed") {
      icon.hidden = true;
      card.classList.add("is-error");
      text.textContent = job.error || "The 3D model could not be made.";
      time.textContent = "";
    } else {
      text.textContent = job.stage || "Making a 3D model…";
      time.textContent = elapsed(job.elapsed);
    }
  });
}

/* ---- wiring ------------------------------------------------------------------ */
export function mountModel3d() {
  $("open3dBtn")?.addEventListener("click", () => open3D());
  $("make3dBtn")?.addEventListener("click", () => open3D());

  const form = $("m3dForm");
  if (!form) return;
  form.addEventListener("submit", (ev) => { ev.preventDefault(); generate(); });

  const input = /** @type {HTMLInputElement} */ ($("m3dFile"));
  input?.addEventListener("change", () => {
    const f = input.files && input.files[0];
    const bad = checkFile(f);
    if (bad) { note(bad, true); setPicked(null); return; }
    note("");
    setPicked(f || null);
  });
  $("m3dClear")?.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); setPicked(null); });

  const drop = $("m3dDrop");
  drop?.addEventListener("dragover", (ev) => { ev.preventDefault(); drop.classList.add("is-over"); });
  drop?.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop?.addEventListener("drop", (ev) => {
    ev.preventDefault();
    drop.classList.remove("is-over");
    const f = ev.dataTransfer?.files?.[0];
    const bad = checkFile(f);
    if (bad) { note(bad, true); return; }
    note("");
    setPicked(f || null);
  });

  $("m3dRes")?.querySelectorAll("[data-res]").forEach((/** @type {HTMLElement} */ b) => {
    b.addEventListener("click", () => {
      resolution = b.dataset.res || "512";
      $("m3dRes").querySelectorAll("[data-res]").forEach((x) =>
        x.setAttribute("aria-checked", String(x === b)));
    });
  });
}
