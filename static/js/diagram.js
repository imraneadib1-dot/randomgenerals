import { state } from "./state.js";
import { loadCredits } from "./credits.js";
import { genForm, genPrompt, genQuality, genRatio, genRun, genSeconds, genUpgrade, modelOut, videoDownload, videoOut, videoResult } from "./dom.js";
import { loadVideoLimits, renderQuota, videoSay } from "./video.js";

export function initMermaid() {
  if (state.mermaidReady || !window.mermaid) return state.mermaidReady;
  try {
    window.mermaid.initialize({
      startOnLoad: false,
      // securityLevel strict: the source comes from a language model,
      // and mermaid can emit click handlers and inline HTML if asked.
      // Nothing here needs either.
      securityLevel: "strict",
      theme: "dark",
      themeVariables: {
        background: "transparent",
        primaryColor: "#2a4155",
        primaryTextColor: "#dfe2dc",
        primaryBorderColor: "#a08348",
        lineColor: "#7fa3bd",
        fontFamily: "Inter, system-ui, sans-serif",
      },
    });
    state.mermaidReady = true;
  } catch (_) {
    state.mermaidReady = false;
  }
  return state.mermaidReady;
}

export async function drawDiagram(source) {
  const out = document.getElementById("diagramOut");
  const srcBox = document.getElementById("diagramSrc");
  const code = document.getElementById("diagramCode");
  if (!out) return;

  code.textContent = source;
  srcBox.hidden = false;
  // #diagramOut lives INSIDE #videoResult, which sendDiagram hides on
  // the way in. Showing the SVG without showing its container left a
  // fully-rendered diagram - 13 nodes, measured - invisible on the page.
  videoResult.hidden = false;
  if (videoOut) videoOut.hidden = true;
  if (modelOut) modelOut.hidden = true;
  if (videoDownload) videoDownload.hidden = true;

  if (!initMermaid()) {
    // The CDN did not answer. The source is still the useful artefact,
    // so it is shown rather than an apology.
    out.className = "diagram-out is-error";
    out.textContent =
      "The diagram renderer could not load. The source is below.";
    out.hidden = false;
    srcBox.open = true;
    return;
  }

  try {
    const id = "mmd" + Date.now();
    const { svg } = await window.mermaid.render(id, source);
    out.className = "diagram-out";
    out.innerHTML = svg;
    out.hidden = false;
  } catch (err) {
    // A parse failure is the model's mistake, not the user's. Show what
    // it wrote so the line can be fixed or the prompt rephrased.
    out.className = "diagram-out is-error";
    out.textContent =
      "That diagram did not parse. The source is below - usually one "
      + "line needs quoting.";
    out.hidden = false;
    srcBox.open = true;
  }
}

export async function sendDiagram(prompt) {
  const out = document.getElementById("diagramOut");
  const srcBox = document.getElementById("diagramSrc");
  if (out) out.hidden = true;
  if (srcBox) srcBox.hidden = true;
  videoResult.hidden = true;
  genRun.disabled = true;
  videoSay("Drawing\u2026");

  let d;
  try {
    const r = await fetch("/api/diagram", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    d = await r.json();
    if (!r.ok) {
      videoSay(d.error || "Could not draw that.", true);
      genRun.disabled = false;
      return;
    }
  } catch (_) {
    videoSay("Could not reach the server.", true);
    genRun.disabled = false;
    return;
  }

  videoSay("");
  genRun.disabled = false;
  await drawDiagram(d.source);
  loadCredits();
}

export function pollVideoJob(id) {
  clearInterval(state.videoPoll);
  // Minutes, not seconds - so this says so rather than implying it is
  // nearly done. An invented percentage that sticks is worse than an
  // honest label.
  const started = Date.now();
  videoSay(
    state.genKind === "model"
      ? "Building the model… usually under a minute."
      : "Generating… this usually takes a minute or two.",
  );
  state.videoPoll = setInterval(async () => {
    let j, q;
    try {
      const r = await fetch(`/api/video/job/${id}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "gone");
      j = d.job;
      q = d.quota;
    } catch (_) {
      clearInterval(state.videoPoll);
      videoSay("Lost track of that one. Check back in a moment.", true);
      genRun.disabled = false;
      return;
    }

    if (q) renderQuota(q);

    if (j.status === "running") {
      const secs = Math.round((Date.now() - started) / 1000);
      videoSay(`Generating… ${secs}s so far.`);
      return;
    }

    clearInterval(state.videoPoll);
    genRun.disabled = false;

    if (j.status === "failed") {
      videoSay(j.error || "That one didn't work. Your quota wasn't used.",
               true);
      return;
    }

    videoSay("");
    if (state.genKind === "model") {
      // model-viewer takes the URL on `src` like an <img> and fetches it
      // itself. Cross-origin is fine - Tripo serves the GLB with
      // permissive CORS, which is why it is not proxied through here.
      modelOut.src = j.url;
      modelOut.setAttribute("alt", j.prompt || "Generated 3D model");
      modelOut.hidden = false;
      videoOut.hidden = true;
      videoDownload.textContent = "Download GLB";
    } else {
      videoOut.src = j.url;
      videoOut.hidden = false;
      modelOut.hidden = true;
      videoDownload.textContent = "Download MP4";
    }
    // The file lives on the provider's CDN, so this is a link out rather
    // than a served file. `download` is a hint the browser may ignore
    // cross-origin, which is why the label says what it is.
    videoDownload.href = j.url;
    videoDownload.setAttribute("target", "_blank");
    videoDownload.setAttribute("rel", "noopener");
    videoDownload.setAttribute("download", `video-${j.id}.mp4`);
    videoResult.hidden = false;
  }, 4000);
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountDiagram() {
  if (genForm) {
    genForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const prompt = (genPrompt.value || "").trim();
      if (!prompt) {
        videoSay("Describe what you want drawn.", true);
        genPrompt.focus();
        return;
      }
      if (state.genKind === "diagram") {
        await sendDiagram(prompt);
        return;
      }
      genRun.disabled = true;
      videoResult.hidden = true;
      videoSay("Sending it over…");
      let d;
      try {
        const r = await fetch("/api/video/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            prompt,
            seconds: parseInt(genSeconds.value, 10),
            ratio: genRatio.value,
            quality: genQuality.value,
          }),
        });
        d = await r.json();
        if (!r.ok) {
          videoSay(d.error || "Could not start that.", true);
          if (d.quota) renderQuota(d.quota);
          // No openUpgrade() exists; a bare `openUpgrade &&` would be a
          // ReferenceError, not a short-circuit. Point at the plan panel
          // that is actually in the page instead.
          if (d.upgrade_required) {
            const pro = document.getElementById("planProBtn");
            if (pro) pro.scrollIntoView({ behavior: "smooth", block: "center" });
          }
          genRun.disabled = false;
          return;
        }
      } catch (_) {
        videoSay("Could not reach the server.", true);
        genRun.disabled = false;
        return;
      }
      if (d.quota) renderQuota(d.quota);
      pollVideoJob(d.job.id);
    });
  }

  if (genUpgrade) {
    genUpgrade.addEventListener("click", (e) => {
      e.preventDefault();
      const pro = document.getElementById("planProBtn");
      if (pro) {
        const settings = document.getElementById("settingsBtn");
        if (settings) settings.click();
        setTimeout(() => pro.scrollIntoView(
          { behavior: "smooth", block: "center" }), 120);
      }
    });
  }

  loadVideoLimits();

}
