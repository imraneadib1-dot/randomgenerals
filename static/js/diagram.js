import { state } from "./state.js";
import { loadCredits } from "./credits.js";
import { genForm, genPrompt, genRun, genUpgrade, modelOut, videoDownload, videoResult } from "./dom.js";
import { loadVideoLimits, submitVideo, videoSay } from "./video.js";

/* Mermaid is fetched the first time a diagram is drawn, not with the
   page. The bundle is about 2.7MB - bigger than everything else the
   page loads put together - and index.html used to include it for every
   visitor, most of whom never open the diagram bay. One script tag,
   inserted on demand; the promise is kept so a second diagram in the
   same session neither waits nor loads it twice. */
const MERMAID_URL =
  "https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js";
let mermaidLoading = null;

function loadMermaid() {
  if (window.mermaid) return Promise.resolve(true);
  if (!mermaidLoading) {
    mermaidLoading = new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = MERMAID_URL;
      s.onload = () => resolve(!!window.mermaid);
      // Cleared on failure so a later diagram tries again rather than
      // remembering one bad network moment for the whole session.
      s.onerror = () => { mermaidLoading = null; resolve(false); };
      document.head.appendChild(s);
    });
  }
  return mermaidLoading;
}

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
      // The glass theme's palette: near-black nodes, an indigo edge,
      // zinc lines. Was the old navy-and-tan, which no longer matched
      // anything else on the page.
      themeVariables: {
        background: "transparent",
        primaryColor: "#18181b",
        primaryTextColor: "#fafafa",
        primaryBorderColor: "#6366f1",
        secondaryColor: "#27272a",
        tertiaryColor: "#111114",
        lineColor: "#a1a1aa",
        textColor: "#e4e4e7",
        edgeLabelBackground: "#18181b",
        clusterBkg: "#111114",
        clusterBorder: "#3f3f46",
        noteBkgColor: "#27272a",
        noteTextColor: "#fafafa",
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

  await loadMermaid();
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

/* DIAGRAMS IN THE CONVERSATION.
   A reply that contains a ```mermaid block is drawn, in place, once it
   has finished arriving (markdown.js announces that with rg:rendered).
   The source stays one click away behind a Code toggle, and a block
   that does not parse is simply left as code - the reply is never
   worse for having tried. */
async function renderInlineDiagrams(bubble) {
  const blocks = /** @type {HTMLElement[]} */ (
    [...bubble.querySelectorAll('.code-block[data-lang="mermaid"]')]);
  if (!blocks.length) return;
  if (!(await loadMermaid()) || !initMermaid()) return;
  for (const block of blocks) {
    if (block.querySelector(".mermaid-view")) continue;
    const pre = block.querySelector("pre");
    const code = pre && pre.querySelector("code");
    if (!code) continue;
    const id = "mmd" + Date.now() + Math.random().toString(36).slice(2, 8);
    try {
      const { svg } = await window.mermaid.render(id, code.textContent || "");
      const view = document.createElement("div");
      view.className = "mermaid-view";
      view.innerHTML = svg;
      pre.hidden = true;
      block.insertBefore(view, pre);

      const header = block.querySelector(".code-block-header");
      if (header) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "copy-btn";
        toggle.textContent = "Code";
        toggle.addEventListener("click", () => {
          const showCode = pre.hidden;
          pre.hidden = !showCode;
          view.hidden = showCode;
          toggle.textContent = showCode ? "Diagram" : "Code";
        });
        header.insertBefore(toggle, header.children[1] || null);
      }
    } catch (_) {
      // Mermaid leaves its error drawing in the page on a failed parse.
      document.getElementById("d" + id)?.remove();
      document.getElementById(id)?.remove();
    }
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

  const log = document.getElementById("chatLog");
  if (log) {
    log.addEventListener("rg:rendered", (e) => {
      const bubble = /** @type {HTMLElement} */ (e.target);
      if (bubble instanceof HTMLElement) renderInlineDiagrams(bubble);
    });
  }
}
