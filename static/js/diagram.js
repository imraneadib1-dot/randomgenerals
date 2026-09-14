import { state } from "./state.js";
import { loadCredits } from "./credits.js";
import { genForm, genPrompt, genRun, genUpgrade, modelOut, videoDownload, videoResult } from "./dom.js";
import { loadVideoLimits, submitVideo, videoSay } from "./video.js";

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
      // A clip or a mesh: the studio in video.js owns the request, the
      // polling and the grid. This handler only routes.
      await submitVideo(prompt);
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
