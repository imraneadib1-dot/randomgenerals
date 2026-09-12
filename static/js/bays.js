import { state } from "./state.js";
import { renderGreeting } from "./boot.js";
import { chatLog, chatModeControls, emptyState, genPrompt, imageModeControls, imageModeNote, imageQualityToggle, messageInput, modelSelect, topbarModelChip, topbarTitle } from "./dom.js";
import { selectProvider, updateComposerHint } from "./providers.js";
import { BAY_META, BAY_ORDER, bayButtons, root } from "./shell.js";
import { loadThreadList } from "./sidebar.js";

/* ----------------------------------------------------------------
   Bay switcher — Code / Chat / Image
   ---------------------------------------------------------------- */
// One line each, per bay. Deliberately concrete rather than clever: a
// starter that reads "Explain quantum computing" teaches nothing about
// what this app is good at, whereas one that names a real task shows
// both the capability and the phrasing that gets the best out of it.
export const STARTERS = {
  code: [
    "Debug this stack trace",
    "Write a Python script to rename files by date",
    "Explain this regex",
    "Refactor a function to be testable",
  ],
  chat: [
    "Why is the sky blue?",
    "A ball is thrown up at 20 m/s — how high?",
    "Explain entropy without the word disorder",
    "Plan a week of meals for two",
  ],
  image: [
    "A lighthouse in a storm, oil painting",
    "Neon Tokyo alley in the rain",
    "Macro shot of frost on a leaf",
    "A lone tree on a salt flat at dusk",
  ],
  video: [],
};

// The chips inside the generation bay, which are separate from the chat
// starters: this bay has its own examples element in the markup.
export const GEN_EXAMPLES = {
  diagram: [
    ["OAuth flow", "How an OAuth 2.0 authorization code flow works, including the token exchange"],
    ["DB schema", "A database schema for a blog with users, posts, comments and tags"],
    ["Request path", "How a browser request reaches a Flask app behind a Cloudflare tunnel"],
    ["State machine", "The states of an online order from placed to delivered or refunded"],
  ],
};

export function renderGenExamples(kind) {
  const box = document.getElementById("genExamples");
  if (!box) return;
  const list = GEN_EXAMPLES[kind];
  if (!list) return;               // media bays keep their own markup
  box.textContent = "";
  const lead = document.createElement("span");
  lead.className = "gen-examples-lead";
  lead.textContent = "Try";
  box.appendChild(lead);
  list.forEach(([label, prompt]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", () => {
      genPrompt.value = prompt;
      genPrompt.focus();
    });
    box.appendChild(b);
  });
}

export function renderStarters() {
  if (!emptyState) return;
  const old = emptyState.querySelector(".starters");
  if (old) old.remove();
  const list = STARTERS[state.currentBay] || [];
  if (!list.length) return;

  const wrap = document.createElement("div");
  wrap.className = "starters";
  list.forEach((text) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.addEventListener("click", () => {
      // Fills the box rather than sending. A starter is a suggestion,
      // and sending it outright takes the edit away from someone who
      // wanted to change two words of it first.
      messageInput.value = text;
      messageInput.focus();
      messageInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    wrap.appendChild(b);
  });
  emptyState.appendChild(wrap);
}

export function updateEmptyState() {
  // The eyebrow/title/sub/hints elements are kept in the DOM and left
  // empty rather than deleted: BAY_META still carries the copy, the
  // placeholder text is read from the same table, and a future bay that
  // genuinely needs an explanation can unhide one of these without the
  // markup having to be rebuilt.
  if (typeof renderGreeting === "function") renderGreeting();
  renderStarters();
}

export function showEmptyState() {
  chatLog.innerHTML = "";
  chatLog.appendChild(emptyState);
  emptyState.style.display = "";
  topbarTitle.textContent = "New chat";
  topbarModelChip.textContent = "";
}

// Which models this plan may actually run, from /api/providers. The
// browser cannot work this out - it does not know the plan rules - so it
// is told, and every automatic choice below filters through it.
export function unlockedModels(models) {
  const p = state.providers.find((x) => x.id === state.activeProvider);
  const info = (p && p.model_info) || [];
  const locked = new Set(
    info.filter((m) => m.locked).map((m) => m.id),
  );
  const open = models.filter((m) => !locked.has(m));
  // If every model is locked, return the full list rather than nothing:
  // an empty picker is a worse failure than one that shows a model the
  // server will explain is Pro.
  return open.length ? open : models;
}

export function preferredModel(all, bay) {
  if (!all.length) return null;
  // THE BUG THIS CLOSES: the fallbacks below match on name, and a name
  // cannot tell you a model is paid. gemma3:4b reads as a perfectly
  // ordinary general model, and being first in the list it was what a
  // free session landed on - then every message was refused as Pro.
  const models = unlockedModels(all);

  // The person's own choice first. Settings > Model > "Default model"
  // was stored and read by nothing - the picker landed on the server's
  // recommendation every time regardless. It applies only when that
  // model is actually offered on the channel in use; a name from
  // another channel or a Pro model on a free account falls through.
  const chosen = state.settingsDoc?.settings?.default_model;
  if (chosen && models.includes(chosen)) return chosen;

  // The server's choice wins when it applies to the channel in use. It
  // is the only party that can rank across channels, because it is the
  // only one that knows a local 7B answers at 3.5 tokens a second here
  // while a hosted model answers in one - a name-matching heuristic in
  // the browser cannot see any of that.
  const route = state.recommended[bay];
  if (route && route.provider === state.activeProvider
      && models.includes(route.model)) {
    return route.model;
  }

  // Fallback for a channel the table does not cover: match on the name.
  const isCoder = (m) => /coder|code/i.test(m);
  if (bay === "code") return models.find(isCoder) || models[0];

  const isVision = (m) => /llava|vision|moondream|minicpm-v|bakllava/i.test(m);
  const isGeneral = (m) => !isCoder(m) && !isVision(m);

  return (
    models.find((m) => /gemma3/i.test(m)) ||
    models.find((m) => /llama3\.2/i.test(m)) ||
    models.find(isGeneral) ||
    models[0]
  );
}

export function applyPreferredModel(bay) {
  const p = state.providers.find((x) => x.id === state.activeProvider);
  if (!p || !p.models.length) return;
  const preferred = preferredModel(p.models, bay);
  if (preferred) modelSelect.value = preferred;
}

// Which channel should answer this bay. The server ranks them against
// what is live (BAY_ROUTES in app.py); this just reads the answer.
export function preferredProviderFor(bay) {
  const route = state.recommended[bay];
  return route ? route.provider : null;
}

export function selectBay(bay) {
  if (!BAY_ORDER.includes(bay)) return;
  state.currentBay = bay;

  // Set before selectProvider runs: it applies the preferred model for
  // whatever currentBay currently is, so switching the channel first
  // would pick a model for the bay being left behind.
  const wanted = preferredProviderFor(bay);
  if (wanted && wanted !== state.activeProvider) selectProvider(wanted);

  // aria-selected is the whole highlight: the stylesheet paints the
  // selected tab from it, so the state the screen reader announces and
  // the state you can see cannot disagree.
  bayButtons.forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.bay === bay ? "true" : "false"),
  );

  root.style.setProperty("--bay", `var(--bay-${bay})`);
  root.style.setProperty("--bay-soft", `var(--bay-${bay}-soft)`);

  messageInput.placeholder = BAY_META[bay].placeholder;

  // Video swaps the whole workspace rather than just the sidebar
  // controls: there is no prompt to type and no thread to show, so the
  // chat shell and the composer go away entirely instead of sitting
  // there inert.
  const isVideo = bay === "video";
  const bayEl = document.getElementById("videoBay");
  // The composer and the panel header are siblings of .chat-shell, not
  // children, so hiding the shell alone left a message box and a
  // "New chat / delete" header sitting above and below a video tool
  // that has neither messages nor a conversation to delete.
  const shell = document.querySelector(".chat-shell");
  const composer = document.querySelector(".composer");
  const panelHeader = document.querySelector(".panel-header");
  if (bayEl) bayEl.hidden = !isVideo;
  if (shell) shell.hidden = isVideo;
  if (composer) composer.hidden = isVideo;
  if (panelHeader) panelHeader.hidden = isVideo;

  chatModeControls.hidden = bay === "image" || isVideo;
  imageModeControls.hidden = bay !== "image";
  if (!isVideo && bay !== "image") applyPreferredModel(bay);

  state.currentThreadId = null;
  if (!isVideo) {
    updateEmptyState();
    showEmptyState();
    loadThreadList();
    updateComposerHint();
  }
}

/**
 * Typeset any maths in a finished message.
 *
 * Runs over the bubble after its parts are in the DOM, never over
 * streaming text: re-parsing a half-written formula on every token both
 * costs real time and renders garbage from an expression that is not
 * finished arriving yet.
 *
 * ignoredTags keeps it away from code. A shell command containing $PATH
 * or a regex with $ is not maths, and letting KaTeX loose on a code
 * block turns working code into a rendering error.
 */
export function renderMath(el) {
  if (!el || !window.renderMathInElement) return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      // A malformed expression shows as the original text in red rather
      // than throwing and taking the rest of the message with it.
      throwOnError: false,
    });
  } catch (e) {
    /* maths is a nicety; a failure here must not lose the reply */
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountBays() {
  bayButtons.forEach((btn) =>
    btn.addEventListener("click", () => selectBay(btn.dataset.bay)),
  );

  // The toggle picks where the picture is made. It only appears at all if
  // a local model exists (see loadProviders) - on a host without torch
  // there is nothing to toggle between, so offering the choice would be
  // offering a broken option.
  imageQualityToggle.addEventListener("click", () => {
    state.imageBackend = state.imageBackend === "flux" ? "local" : "flux";
    imageQualityToggle.setAttribute(
      "aria-checked",
      String(state.imageBackend === "flux"),
    );
    imageModeNote.textContent =
      state.imageBackend === "flux"
        ? "FLUX - hosted, higher quality, no key needed."
        : "Local Stable Diffusion - private, runs on this machine.";
  });

}
