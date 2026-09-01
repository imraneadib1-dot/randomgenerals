const BAY_ORDER = ["code", "chat", "image", "video"];

const BAY_META = {
  code: {
    eyebrow: "randomgenerals --ready",
    title: "Ask for code",
    // Deliberately says nothing about where it runs. The Code bay now
    // opens on the cloud channel when a stronger model is available
    // there, so a fixed "no cloud call" line was simply untrue - and
    // the composer hint under the input already names the live
    // channel, which is the honest place for it.
    sub: "Ask for code or debug an error — you get working code back, not a lecture.",
    placeholder: "Ask for code, debug an error…",
    hints:
      "<div><span>Enter</span> to send · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎</span> switch bays above</div>",
  },
  chat: {
    eyebrow: "randomgenerals --chat",
    title: "What's on your mind?",
    sub: "Ask anything. RandomGenerals AI keeps it conversational — no forced structure.",
    placeholder: "Ask me anything…",
    hints:
      "<div><span>Enter</span> to send · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎</span> switch bays above</div>",
  },
  image: {
    eyebrow: "randomgenerals --image",
    title: "Describe an image",
    sub: "Describe what you want to see — your words get expanded into a full prompt first, so a few words still make a real picture.",
    placeholder: "A red apple on a wooden table…",
    hints:
      "<div><span>Enter</span> to generate · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎ ✺</span> switch bays above</div>",
  },
  video: {
    eyebrow: "randomgenerals --video",
    title: "Trim a video",
    // Says what it does and, just as importantly, what it does not. A bay
    // called Video that turns out to only trim is worse than one that
    // said so before the upload.
    sub: "Drop a clip, choose the part you want, and get an MP4 back. Trimming only for now.",
    placeholder: "",
    hints:
      "<div><span>Drop a file</span> or click to choose one</div>" +
      "<div><span>&lt;/&gt; ◎ ✺ ▶</span> switch bays above</div>",
  },
};

// Labels come from the server now - it is the only side that knows
// whether the local channel is on this hardware or on Ollama Cloud, and
// the label has to follow that. Kept as a fallback for a provider the
// server names but does not label.
const PROVIDER_META = {
  ollama: { label: "RandomGenerals AI" },
  groq: { label: "RandomGenerals AI Turbo" },
  imagegen: { label: "Image" },
};

let currentBay = "code";
let currentThreadId = null;
let providers = [];
let activeProvider = null;

const root = document.documentElement;

// Time-of-day sky: drifting clouds in the morning, a glowing moon at
// night, plain sky the rest of the day - purely decorative, driven by
// the visitor's own local clock (not the server's), rechecked
// periodically so it comes back if a tab is left open across a boundary.
function updateTimeOfDay() {
  // A saved Appearance override wins over the clock. Read defensively -
  // this runs at boot, before initAppearance(), and localStorage can
  // throw outright in a private window.
  let override = null;
  try {
    override = localStorage.getItem("skyTheme");
  } catch (e) {
    /* fall through to the clock */
  }
  const hour = new Date().getHours();
  const phase =
    override && override !== "auto"
      ? override
      : hour >= 6 && hour < 12
        ? "morning"
        : hour >= 20 || hour < 6
          ? "night"
          : "day";
  root.classList.remove("time-morning", "time-night", "time-day");
  root.classList.add(`time-${phase}`);
}
updateTimeOfDay();
setInterval(updateTimeOfDay, 5 * 60 * 1000);

const chatLog = document.getElementById("chatLog");
const emptyState = document.getElementById("emptyState");
const emptyEyebrow = document.getElementById("emptyEyebrow");
const emptyTitle = document.getElementById("emptyTitle");
const emptySub = document.getElementById("emptySub");
const emptyHints = document.getElementById("emptyHints");

const chatForm = document.getElementById("chatForm");
const messageInput = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");
const fileInput = document.getElementById("fileInput");
const folderInput = document.getElementById("folderInput");
const attachFileBtn = document.getElementById("attachFileBtn");
const attachFolderBtn = document.getElementById("attachFolderBtn");
const attachChips = document.getElementById("attachChips");
const strengthToggle = document.getElementById("strengthToggle");
const micBtn = document.getElementById("micBtn");

const chatModeControls = document.getElementById("chatModeControls");
const imageModeControls = document.getElementById("imageModeControls");
const imageQualityToggle = document.getElementById("imageQualityToggle");
const imageModeNote = document.getElementById("imageModeNote");
// Whether Stable Diffusion is installed alongside the hosted image
// backend, i.e. whether there is a quality choice to offer at all.
let localImageAvailable = false;
// FLUX by default: it needs no key, so it is the one backend guaranteed
// to work everywhere this app runs. Local is opt-in and only offered
// where torch is actually installed.
let imageBackend = "flux";
let imageSize = "square";
let imageStyle = "none";

const imageSizeSelect = document.getElementById("imageSizeSelect");
const imageStyleSelect = document.getElementById("imageStyleSelect");
if (imageSizeSelect) {
  imageSizeSelect.addEventListener("change", () => {
    imageSize = imageSizeSelect.value;
  });
}
if (imageStyleSelect) {
  imageStyleSelect.addEventListener("change", () => {
    imageStyle = imageStyleSelect.value;
  });
}

const modelSelect = document.getElementById("modelSelect");
const channelRow = document.getElementById("channelRow");
const patchBayLabel = document.getElementById("patchBayLabel");
const channelNote = document.getElementById("channelNote");
const sidebarBottom = document.getElementById("sidebarBottom");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const threadList = document.getElementById("threadList");
const newChatBtn = document.getElementById("newChatBtn");
const deleteBtn = document.getElementById("deleteBtn");
const topbarTitle = document.getElementById("topbarTitle");
const topbarModelChip = document.getElementById("topbarModelChip");
const sidebar = document.getElementById("sidebar");
const mobileToggle = document.getElementById("mobileToggle");
const clockEl = document.getElementById("clock");
const composerHintText = document.getElementById("composerHintText");

// The puck is one bay wide, and CSS cannot count its siblings. Set the
// count once here so adding a bay to BAY_ORDER is the only change
// needed - the sizing follows.

const bayButtons = [...document.querySelectorAll(".bay-btn")];

const creditBarFill = document.getElementById("creditBarFill");
const creditCount = document.getElementById("creditCount");

/* ----------------------------------------------------------------
   Composer input behaviour
   ---------------------------------------------------------------- */
function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

messageInput.addEventListener("input", () => autoGrow(messageInput));
messageInput.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  // Which key sends is a real preference, not decoration: people
  // pasting multi-line code want Enter to make a newline, and people
  // holding a conversation want it to send. Settings > General.
  //
  // readPref is consulted per keystroke rather than cached, so changing
  // the switch takes effect in the composer already on screen.
  const enterSends = readPref("enterToSend") !== "0";
  if (enterSends ? !e.shiftKey : (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

mobileToggle.addEventListener("click", () => sidebar.classList.toggle("open"));

/* ----------------------------------------------------------------
   Voice input - browser-native speech-to-text (Chrome, Edge, Safari).
   Transcribes into the message box rather than auto-sending, so a
   misheard word can still be fixed before it goes out. Runs entirely
   in the browser - no server round-trip, no credits, no added latency.
   Firefox has no SpeechRecognition implementation, so the mic button
   just stays hidden there instead of showing something that fails
   silently.
   ---------------------------------------------------------------- */
const SpeechRecognitionAPI =
  window.SpeechRecognition || window.webkitSpeechRecognition;

if (SpeechRecognitionAPI) {
  micBtn.hidden = false;
  const recognizer = new SpeechRecognitionAPI();
  recognizer.continuous = false;
  recognizer.interimResults = true;
  recognizer.lang = navigator.language || "en-US";

  let isListening = false;
  let baseText = "";

  recognizer.onstart = () => {
    isListening = true;
    // Keep whatever was already typed and append to it, rather than
    // clobbering a partially-typed message.
    baseText = messageInput.value && !messageInput.value.endsWith(" ")
      ? messageInput.value + " "
      : messageInput.value;
    micBtn.classList.add("mic-listening");
  };

  recognizer.onresult = (e) => {
    let transcript = "";
    for (let i = 0; i < e.results.length; i++) {
      transcript += e.results[i][0].transcript;
    }
    messageInput.value = baseText + transcript;
    messageInput.dispatchEvent(new Event("input"));
  };

  const stopListeningUI = () => {
    isListening = false;
    micBtn.classList.remove("mic-listening");
  };
  recognizer.onerror = stopListeningUI;
  recognizer.onend = () => {
    stopListeningUI();
    messageInput.focus();
  };

  micBtn.addEventListener("click", () => {
    if (isListening) {
      recognizer.stop();
      return;
    }
    try {
      recognizer.start();
    } catch (e) {
      // start() throws if a session is already active - the existing
      // session's onend handles cleanup, nothing to do here.
    }
  });
}

// Auto web search - no toggle to think about. If the message looks
// time-sensitive or current-events-shaped, search first; otherwise just
// answer from the model directly. A heuristic, not a real classifier -
// it'll miss things and occasionally search when it didn't need to, but
// that's the same tradeoff a manual toggle has (except nobody has to
// remember to flip it).
const SEARCH_TRIGGER_RE = new RegExp(
  "\\b(" +
    [
      "today",
      "tonight",
      "right now",
      "currently",
      "this (week|month|year)",
      "latest",
      "newest",
      "recently",
      "up[- ]to[- ]date",
      "breaking",
      "news",
      "headline",
      "weather",
      "forecast",
      "score",
      "stock price",
      "exchange rate",
      "who (is|are) the current",
      "what(?:'s| is) the (current|latest)",
      "release date",
      "when (is|does|will)",
      "how much (does|is)",
      "price of",
      "20(2[4-9]|3\\d)",
    ].join("|") +
    ")\\b",
  "i",
);

function shouldAutoSearch(text) {
  return SEARCH_TRIGGER_RE.test(text);
}

/* ----------------------------------------------------------------
   Attachments — documents/images uploaded alongside a message
   ---------------------------------------------------------------- */
let pendingAttachments = [];
let uploadingCount = 0;

function renderAttachChips() {
  attachChips.innerHTML = "";
  pendingAttachments.forEach((att, i) => {
    const chip = document.createElement("div");
    chip.className = "attach-chip";
    const label = document.createElement("span");
    label.textContent = att.error
      ? `${att.filename} (${att.error})`
      : att.filename;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attach-chip-remove";
    remove.textContent = "✕";
    remove.title = "Remove";
    remove.addEventListener("click", () => {
      pendingAttachments.splice(i, 1);
      renderAttachChips();
    });
    chip.appendChild(label);
    chip.appendChild(remove);
    attachChips.appendChild(chip);
  });
  for (let i = 0; i < uploadingCount; i++) {
    const chip = document.createElement("div");
    chip.className = "attach-chip uploading";
    chip.textContent = "uploading…";
    attachChips.appendChild(chip);
  }
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  uploadingCount = files.length;
  renderAttachChips();
  try {
    const formData = new FormData();
    files.forEach((f) => formData.append("files", f));
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    pendingAttachments.push(...(data.attachments || []));
  } catch (err) {
    /* upload failed - nothing gets added, chips just clear below */
  } finally {
    uploadingCount = 0;
    renderAttachChips();
  }
}

attachFileBtn.addEventListener("click", () => fileInput.click());
attachFolderBtn.addEventListener("click", () => folderInput.click());
fileInput.addEventListener("change", () => {
  uploadFiles(fileInput.files);
  fileInput.value = "";
});
folderInput.addEventListener("change", () => {
  uploadFiles(folderInput.files);
  folderInput.value = "";
});

/* ----------------------------------------------------------------
   Response mode — Quick (fast, brief, no forced search) or Deep
   (always searches the web first, thinks it through more thoroughly)
   ---------------------------------------------------------------- */
let currentStrength = "quick";
strengthToggle.addEventListener("click", () => {
  currentStrength = currentStrength === "quick" ? "deep" : "quick";
  strengthToggle.setAttribute(
    "aria-checked",
    String(currentStrength === "deep"),
  );
  // Deliberately NOT switching models here any more. This app's ~8GB of
  // VRAM can only hold one multi-GB model at a time, so swapping models
  // means evicting one and cold-loading another - measured at ~39s,
  // against ~1.4s once a model is already warm. Tying model choice to
  // this toggle meant flipping Quick/Deep mid-session paid that cost
  // every time, which made the "too slow" complaint this was supposed
  // to fix worse instead of better. Quick/Deep now only changes the
  // prompt/decoding options; the model stays whatever it already was.
});

// The clock was removed from the sidebar - the operating system already
// has one, and a second-by-second ticker was the busiest thing on an
// otherwise idle screen. The guard stays rather than the function being
// deleted, because the element is optional now: a layout that wants a
// clock back only has to add the span.
function tickClock() {
  if (!clockEl) return;
  clockEl.textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
if (clockEl) {
  tickClock();
  setInterval(tickClock, 1000);
}

function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return "now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d`;
  return `${Math.round(d / 30)}mo`;
}

/* ----------------------------------------------------------------
   Bay switcher — Code / Chat / Image
   ---------------------------------------------------------------- */
// One line each, per bay. Deliberately concrete rather than clever: a
// starter that reads "Explain quantum computing" teaches nothing about
// what this app is good at, whereas one that names a real task shows
// both the capability and the phrasing that gets the best out of it.
const STARTERS = {
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
const GEN_EXAMPLES = {
  diagram: [
    ["OAuth flow", "How an OAuth 2.0 authorization code flow works, including the token exchange"],
    ["DB schema", "A database schema for a blog with users, posts, comments and tags"],
    ["Request path", "How a browser request reaches a Flask app behind a Cloudflare tunnel"],
    ["State machine", "The states of an online order from placed to delivered or refunded"],
  ],
};

function renderGenExamples(kind) {
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

function renderStarters() {
  if (!emptyState) return;
  const old = emptyState.querySelector(".starters");
  if (old) old.remove();
  const list = STARTERS[currentBay] || [];
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

function updateEmptyState() {
  // The eyebrow/title/sub/hints elements are kept in the DOM and left
  // empty rather than deleted: BAY_META still carries the copy, the
  // placeholder text is read from the same table, and a future bay that
  // genuinely needs an explanation can unhide one of these without the
  // markup having to be rebuilt.
  if (typeof renderGreeting === "function") renderGreeting();
  renderStarters();
}

function showEmptyState() {
  chatLog.innerHTML = "";
  chatLog.appendChild(emptyState);
  emptyState.style.display = "";
  topbarTitle.textContent = "New chat";
  topbarModelChip.textContent = "";
}

// Same provider, but a coding-tuned model answers noticeably better in
// the Code bay than a general chat model, and vice versa - so the model
// dropdown's default follows whichever bay is active instead of staying
// wherever it was left.
//
// Chat bay always defaults to the smallest general model (llama3.2),
// not the more accurate qwen2.5 - speed over accuracy as the default,
// deliberately. This app's ~8GB VRAM can only hold one multi-GB model
// at a time, so switching to a different model means a ~39s cold
// reload versus ~1.4s once warm (measured), and defaulting to the
// slower model made every-day use feel broken. qwen2.5 is still there
// in the dropdown for anyone who wants to pick it on purpose and knows
// they're paying a one-time load cost for it.
// What the server says should answer this bay, resolved against what is
// actually live - see BAY_ROUTES in app.py. Populated by loadProviders().
let recommended = {};

// Which models this plan may actually run, from /api/providers. The
// browser cannot work this out - it does not know the plan rules - so it
// is told, and every automatic choice below filters through it.
function unlockedModels(models) {
  const p = providers.find((x) => x.id === activeProvider);
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

function preferredModel(all, bay) {
  if (!all.length) return null;
  // THE BUG THIS CLOSES: the fallbacks below match on name, and a name
  // cannot tell you a model is paid. gemma3:4b reads as a perfectly
  // ordinary general model, and being first in the list it was what a
  // free session landed on - then every message was refused as Pro.
  const models = unlockedModels(all);

  // The server's choice wins when it applies to the channel in use. It
  // is the only party that can rank across channels, because it is the
  // only one that knows a local 7B answers at 3.5 tokens a second here
  // while a hosted model answers in one - a name-matching heuristic in
  // the browser cannot see any of that.
  const route = recommended[bay];
  if (route && route.provider === activeProvider
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

function applyPreferredModel(bay) {
  const p = providers.find((x) => x.id === activeProvider);
  if (!p || !p.models.length) return;
  const preferred = preferredModel(p.models, bay);
  if (preferred) modelSelect.value = preferred;
}

// Which channel should answer this bay. The server ranks them against
// what is live (BAY_ROUTES in app.py); this just reads the answer.
function preferredProviderFor(bay) {
  const route = recommended[bay];
  return route ? route.provider : null;
}

function selectBay(bay) {
  if (!BAY_ORDER.includes(bay)) return;
  currentBay = bay;

  // Set before selectProvider runs: it applies the preferred model for
  // whatever currentBay currently is, so switching the channel first
  // would pick a model for the bay being left behind.
  const wanted = preferredProviderFor(bay);
  if (wanted && wanted !== activeProvider) selectProvider(wanted);

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

  currentThreadId = null;
  if (!isVideo) {
    updateEmptyState();
    showEmptyState();
    loadThreadList();
    updateComposerHint();
  }
}

bayButtons.forEach((btn) =>
  btn.addEventListener("click", () => selectBay(btn.dataset.bay)),
);

// The toggle picks where the picture is made. It only appears at all if
// a local model exists (see loadProviders) - on a host without torch
// there is nothing to toggle between, so offering the choice would be
// offering a broken option.
imageQualityToggle.addEventListener("click", () => {
  imageBackend = imageBackend === "flux" ? "local" : "flux";
  imageQualityToggle.setAttribute(
    "aria-checked",
    String(imageBackend === "flux"),
  );
  imageModeNote.textContent =
    imageBackend === "flux"
      ? "FLUX - hosted, higher quality, no key needed."
      : "Local Stable Diffusion - private, runs on this machine.";
});

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
function renderMath(el) {
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

/* ----------------------------------------------------------------
   HTML preview
   ----------------------------------------------------------------
   Renders a generated page in a sandboxed iframe.

   srcdoc plus a sandbox that grants scripts but NOT same-origin. That
   combination is what makes this safe to offer: the page runs, so a
   generated site with tabs or a menu actually works, but it is in an
   opaque origin - it cannot read this document, cannot touch cookies or
   localStorage, and cannot call the API with the visitor's session.

   allow-scripts together with allow-same-origin would undo all of that,
   which is why they are never both listed.
   ---------------------------------------------------------------- */
function isPreviewable(lang, content) {
  if (!content) return false;
  const l = (lang || "").toLowerCase();
  if (l !== "html" && l !== "htm") return false;
  // A fragment is not a page. Previewing one shows a bare line of text
  // on white and looks broken, so the button only appears for something
  // that is actually a document.
  return /<html[\s>]|<!doctype html/i.test(content);
}

function downloadHtml(content) {
  const blob = new Blob([content], { type: "text/html" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "page.html";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on a timer rather than immediately: Chrome cancels an
  // in-flight download if the blob URL is released too early.
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

function openPreview(content) {
  const existing = document.getElementById("htmlPreview");
  if (existing) existing.remove();

  const wrap = document.createElement("div");
  wrap.className = "html-preview";
  wrap.id = "htmlPreview";

  const bar = document.createElement("div");
  bar.className = "html-preview-bar";

  const title = document.createElement("span");
  title.textContent = "Preview";
  bar.appendChild(title);

  const spacer = document.createElement("span");
  spacer.style.flex = "1";
  bar.appendChild(spacer);

  const openBtn = document.createElement("button");
  openBtn.className = "copy-btn";
  openBtn.textContent = "Open in tab";
  openBtn.onclick = () => {
    const blob = new Blob([content], { type: "text/html" });
    window.open(URL.createObjectURL(blob), "_blank", "noopener");
  };
  bar.appendChild(openBtn);

  const closeBtn = document.createElement("button");
  closeBtn.className = "copy-btn";
  closeBtn.textContent = "Close";
  closeBtn.onclick = () => wrap.remove();
  bar.appendChild(closeBtn);

  const frame = document.createElement("iframe");
  frame.className = "html-preview-frame";
  frame.setAttribute("sandbox", "allow-scripts allow-forms allow-popups");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.srcdoc = content;

  wrap.appendChild(bar);
  wrap.appendChild(frame);
  document.body.appendChild(wrap);

  const onKey = (e) => {
    if (e.key === "Escape") {
      wrap.remove();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);
}

/* ----------------------------------------------------------------
   Channel (provider) picker
   ---------------------------------------------------------------- */
function updateComposerHint() {
  if (currentBay === "image") {
    composerHintText.textContent = localImageAvailable
      ? "Hosted or on-device · pick a quality above"
      : "Image generation · free, no account needed";
    return;
  }
  const p = providers.find((x) => x.id === activeProvider);
  if (!p) {
    composerHintText.textContent = "no channel available — check your setup";
    return;
  }
  composerHintText.textContent = p.available
    ? `${p.label} · responses stream in real time`
    : p.note || `${p.label} unavailable`;
}

function selectProvider(id) {
  const p = providers.find((x) => x.id === id);
  if (!p || !p.available) return;
  activeProvider = id;

  [...channelRow.children].forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.provider === id),
  );

  // Built with DOM APIs rather than an innerHTML template: model names
  // come from Ollama's /api/tags, and interpolating them into
  // `value="${m}"` unescaped lets a name containing a double quote break
  // out of the attribute. new Option() assigns them as data, so there is
  // no markup context to escape from in the first place.
  // Locked models stay VISIBLE and disabled rather than being hidden.
  // Hiding them would make Pro invisible to the people it is sold to;
  // disabling them makes the ceiling legible without letting anyone walk
  // into it. The label carries the reason, since a greyed row with no
  // explanation reads as a bug.
  const info = new Map((p.model_info || []).map((m) => [m.id, m]));
  modelSelect.replaceChildren(
    ...p.models.map((m) => {
      const meta = info.get(m);
      // The friendly name, not the routing string. "Max" says more to
      // the person choosing than "openai/gpt-oss-120b" does, and the
      // full id is still on the option's title for anyone who wants it.
      let label = (meta && meta.name) || m;
      if (meta && meta.locked) label += " — Pro";
      const opt = new Option(label, m);
      opt.title = (meta && meta.blurb) ? `${m} — ${meta.blurb}` : m;
      if (meta && meta.locked) opt.disabled = true;
      return opt;
    }),
  );
  modelSelect.disabled = p.models.length === 0;
  channelNote.textContent = p.models.length
    ? ""
    : "no models found for this channel";
  applyPreferredModel(currentBay);
  updateComposerHint();
}

function renderChannelRow() {
  // One AI, one provider - a picker with a single button to click isn't a
  // choice, it's just an extra step. Auto-select it and hide the row
  // entirely; the model dropdown below still shows which model answers.
  //
  // The server already drops channels that cannot answer while any other
  // one can (see /api/providers), so a single entry here usually means
  // this deployment has exactly one working provider rather than that
  // the others are broken.
  // A provider marked hidden is plumbing, not a choice: the local
  // channel stays configured so it can answer an attached image and
  // catch a rate-limited request, but nobody should be picking it from
  // a menu. See ollama_provider() in app.py for why it survives at all.
  const shown = providers.filter((p) => !p.hidden);
  const showPicker = shown.length > 1;
  channelRow.style.display = showPicker ? "" : "none";
  patchBayLabel.style.display = showPicker ? "" : "none";
  if (!showPicker) return;

  channelRow.innerHTML = "";
  shown.forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "channel-btn" + (p.available ? " online" : "");
    btn.dataset.provider = p.id;
    btn.disabled = !p.available;
    btn.title = p.available ? p.label : p.note || `${p.label} unavailable`;
    btn.style.setProperty("--btn-color", `var(--c-${p.id})`);
    btn.style.setProperty("--btn-soft", `var(--c-${p.id}-soft)`);
    // Same reasoning as the model list above - the label is server-
    // supplied, so it goes in as text rather than as markup.
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.textContent = PROVIDER_META[p.id]?.label || p.label;
    btn.replaceChildren(dot, label);
    btn.addEventListener("click", () => selectProvider(p.id));
    channelRow.appendChild(btn);
  });
}

async function loadProviders() {
  try {
    const res = await fetch("/api/providers");
    const data = await res.json();
    providers = data.providers || [];
    recommended = data.recommended || {};
    renderChannelRow();

    localImageAvailable = !!data.local_image;
    imageQualityToggle.hidden = !localImageAvailable;

    const firstAvailable = providers.find((p) => p.available);
    statusDot.classList.toggle("online", !!firstAvailable);
    statusText.textContent = firstAvailable ? "Online" : "Offline";

    if (firstAvailable) {
      // The bay's own preference wins over "first available in the
      // list". Without this the Code bay only moved to the OpenAI model
      // once the user clicked a bay tab - on a fresh load it opened on
      // whichever channel happened to come first, which is the local
      // one, so the default nobody changes was the weaker model.
      const wanted = preferredProviderFor(currentBay);
      selectProvider(wanted || firstAvailable.id);
    } else {
      modelSelect.innerHTML = "<option>no channel available</option>";
      modelSelect.disabled = true;
      channelNote.textContent =
        "nothing trained yet — run: python brain/train.py";
      updateComposerHint();
    }
  } catch (err) {
    statusDot.classList.remove("online");
    statusText.textContent = "Offline";
    channelNote.textContent = "could not reach the server";
  }
}

/* ----------------------------------------------------------------
   Credit meter
   ---------------------------------------------------------------- */
function renderCredits(data) {
  if (!data || typeof data.balance !== "number") return;
  const pct = data.starting
    ? Math.max(0, Math.min(100, (data.balance / data.starting) * 100))
    : 0;
  creditBarFill.style.width = pct + "%";
  creditBarFill.classList.remove("mid", "low");
  if (pct <= 15) creditBarFill.classList.add("low");
  else if (pct <= 45) creditBarFill.classList.add("mid");
  creditCount.textContent = `${data.balance} / ${data.starting}`;
}

async function loadCredits() {
  try {
    renderCredits(await (await fetch("/api/credits")).json());
  } catch (err) {
    creditCount.textContent = "—";
  }
}

/* ----------------------------------------------------------------
   Thread list — scoped to the active bay
   ---------------------------------------------------------------- */
async function loadThreadList() {
  const res = await fetch(`/api/threads?mode=${currentBay}`);
  const data = await res.json();
  threadList.innerHTML = "";

  data.threads.forEach((t) => {
    const item = document.createElement("div");
    item.className =
      "thread-item" + (t.id === currentThreadId ? " active" : "");

    const main = document.createElement("div");
    main.className = "thread-main";

    const title = document.createElement("span");
    title.className = "thread-title";
    title.textContent = t.title;

    const meta = document.createElement("span");
    meta.className = "thread-meta";
    meta.textContent = relativeTime(t.updated);

    main.appendChild(title);
    main.appendChild(meta);

    const del = document.createElement("button");
    del.className = "thread-delete";
    del.textContent = "✕";
    del.title = "Delete";
    del.onclick = async (e) => {
      e.stopPropagation();
      await fetch(`/api/threads/${t.id}`, { method: "DELETE" });
      if (t.id === currentThreadId) {
        currentThreadId = null;
        showEmptyState();
      }
      loadThreadList();
    };

    item.appendChild(main);
    item.appendChild(del);
    item.onclick = () => openThread(t.id);
    threadList.appendChild(item);
  });

  if (data.threads.length === 0) {
    threadList.innerHTML =
      '<div class="thread-empty">No conversations yet</div>';
  }
}

async function openThread(tid) {
  currentThreadId = tid;
  sidebar.classList.remove("open");
  const res = await fetch(`/api/threads/${tid}`);
  if (!res.ok) {
    currentThreadId = null;
    showEmptyState();
    return;
  }
  const thread = await res.json();

  topbarTitle.textContent = thread.title;
  chatLog.innerHTML = "";
  chatLog.appendChild(emptyState);
  emptyState.style.display = "none";

  const lastAssistantIdx = thread.messages.reduce(
    (acc, m, i) => (m.role === "assistant" ? i : acc),
    -1,
  );
  thread.messages.forEach((m, i) => {
    if (m.role === "user") {
      const bubble = addMessage("user", m.content);
      renderMsgAttachments(bubble.parentElement, m.attachments);
    } else if (m.type === "image") {
      const bubble = addMessage("assistant", "", m.provider, m.model);
      const img = document.createElement("img");
      img.className = "generated-image";
      img.src = m.content;
      bubble.appendChild(img);
      addMessageActions(bubble.parentElement, bubble, { allowRegenerate: false });
    } else {
      const bubble = addMessage("assistant", "", m.provider, m.model);
      renderContent(bubble, m.content);
      renderSourceChips(bubble.parentElement, m.sources);
      addMessageActions(bubble.parentElement, bubble, {
        allowRegenerate: i === lastAssistantIdx,
      });
    }
  });

  loadThreadList();
}

newChatBtn.addEventListener("click", () => {
  currentThreadId = null;
  showEmptyState();
  loadThreadList();
});

deleteBtn.addEventListener("click", async () => {
  if (!currentThreadId) return;
  await fetch(`/api/threads/${currentThreadId}`, { method: "DELETE" });
  currentThreadId = null;
  showEmptyState();
  loadThreadList();
});

/* ----------------------------------------------------------------
   Message rendering
   ---------------------------------------------------------------- */
function addMessage(role, text, provider, model) {
  emptyState.style.display = "none";
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;

  const roleLabel = document.createElement("div");
  roleLabel.className = "msg-role";

  if (role === "user") {
    roleLabel.textContent = "you";
  } else {
    const dot = document.createElement("span");
    dot.className = "src-dot";
    const label = document.createElement("span");
    if (provider) {
      msg.style.setProperty("--msg-color", `var(--c-${provider})`);
      dot.style.color = `var(--c-${provider})`;
      label.textContent = `${PROVIDER_META[provider]?.label || provider}${
        model ? " · " + model : ""
      }`;
    } else {
      dot.style.color = "var(--bay)";
      label.textContent = "randomgenerals ai";
    }
    roleLabel.appendChild(dot);
    roleLabel.appendChild(label);
  }

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  if (text) bubble.textContent = text;

  msg.appendChild(roleLabel);
  msg.appendChild(bubble);
  chatLog.appendChild(msg);
  chatLog.parentElement.scrollTop = chatLog.parentElement.scrollHeight;
  return bubble;
}

// Copy always works on any assistant reply; Regenerate only makes sense
// on the most recent one, since the backend only ever replays the
// thread's last message - showing it elsewhere would just confuse which
// reply is actually getting redone.
function addMessageActions(msg, bubble, { allowRegenerate }) {
  // Only one message can ever be "the last reply" - drop any stale
  // Regenerate button before (maybe) adding a fresh one.
  document
    .querySelectorAll(".msg-action-regenerate")
    .forEach((btn) => btn.remove());

  const row = document.createElement("div");
  row.className = "msg-actions";

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "msg-action-btn";
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(bubble.textContent);
    copyBtn.textContent = "Copied!";
    setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
  });
  row.appendChild(copyBtn);

  if (allowRegenerate) {
    const regenBtn = document.createElement("button");
    regenBtn.type = "button";
    regenBtn.className = "msg-action-btn msg-action-regenerate";
    regenBtn.textContent = "Regenerate";
    regenBtn.addEventListener("click", () => regenerateLast(msg));
    row.appendChild(regenBtn);
  }

  if (window.speechSynthesis) {
    const speakBtn = document.createElement("button");
    speakBtn.type = "button";
    speakBtn.className = "msg-action-btn";
    speakBtn.textContent = "Read aloud";
    speakBtn.addEventListener("click", () => {
      if (speechSynthesis.speaking) {
        speechSynthesis.cancel();
        speakBtn.textContent = "Read aloud";
        return;
      }
      const utter = new SpeechSynthesisUtterance(bubble.textContent);
      utter.onend = () => (speakBtn.textContent = "Read aloud");
      utter.onerror = () => (speakBtn.textContent = "Read aloud");
      speakBtn.textContent = "Stop";
      speechSynthesis.speak(utter);
    });
    row.appendChild(speakBtn);
  }

  msg.appendChild(row);
}

function renderSourceChips(container, sources) {
  if (!sources || !sources.length) return;
  const wrap = document.createElement("div");
  wrap.className = "source-chips";
  sources.forEach((s, i) => {
    const a = document.createElement("a");
    a.className = "source-chip";
    a.href = s.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = s.title;
    const idx = document.createElement("span");
    idx.className = "source-index";
    idx.textContent = i + 1;
    const label = document.createElement("span");
    try {
      label.textContent = new URL(s.url).hostname.replace(/^www\./, "");
    } catch (err) {
      label.textContent = s.title;
    }
    a.appendChild(idx);
    a.appendChild(label);
    wrap.appendChild(a);
  });
  container.appendChild(wrap);
}

function renderMsgAttachments(container, atts) {
  if (!atts || !atts.length) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-attachments";
  atts.forEach((a) => {
    if (a.kind === "image" && a.url) {
      const img = document.createElement("img");
      img.className = "msg-attachment-image";
      img.src = a.url;
      img.alt = a.filename;
      img.loading = "lazy";
      wrap.appendChild(img);
    } else {
      const chip = document.createElement("span");
      chip.className = "msg-attachment-file";
      chip.textContent = a.filename;
      wrap.appendChild(chip);
    }
  });
  container.appendChild(wrap);
}

function escapeHtml(str) {
  // Quotes are escaped too. The one current caller inserts the result as
  // element *content*, where quotes are harmless - but an escape helper
  // that is only safe in some contexts is a trap for the next use, and
  // over-escaping costs nothing here.
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderContent(bubble, text) {
  const parts = [];
  const fenceRegex = /```(\w*)\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match;

  while ((match = fenceRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }
    parts.push({
      type: "code",
      lang: match[1] || "plaintext",
      content: match[2],
    });
    lastIndex = fenceRegex.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push({ type: "text", content: text.slice(lastIndex) });
  }

  bubble.innerHTML = "";

  parts.forEach((part) => {
    if (part.type === "code") {
      const block = document.createElement("div");
      block.className = "code-block";

      const header = document.createElement("div");
      header.className = "code-block-header";
      const langSpan = document.createElement("span");
      langSpan.textContent = part.lang;
      header.appendChild(langSpan);

      const copyBtn = document.createElement("button");
      copyBtn.className = "copy-btn";
      copyBtn.textContent = "Copy";
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(part.content);
        copyBtn.textContent = "Copied!";
        setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
      };
      header.appendChild(copyBtn);

      // A page you can look at, not just read the source of.
      //
      // This is the difference between "here is some HTML" and "here is
      // your website". The model is asked for one self-contained file
      // precisely so this works: no missing stylesheet, no broken script
      // path, nothing to assemble before it renders.
      if (isPreviewable(part.lang, part.content)) {
        const previewBtn = document.createElement("button");
        previewBtn.className = "copy-btn";
        previewBtn.textContent = "Preview";
        previewBtn.onclick = () => openPreview(part.content);
        header.appendChild(previewBtn);

        const saveBtn = document.createElement("button");
        saveBtn.className = "copy-btn";
        saveBtn.textContent = "Save .html";
        saveBtn.onclick = () => downloadHtml(part.content);
        header.appendChild(saveBtn);
      }

      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (part.lang && part.lang !== "plaintext") {
        code.className = `language-${part.lang}`;
      }
      code.textContent = part.content;
      pre.appendChild(code);

      block.appendChild(header);
      block.appendChild(pre);
      bubble.appendChild(block);

      if (window.hljs) hljs.highlightElement(code);
    } else {
      const span = document.createElement("span");
      span.style.whiteSpace = "pre-wrap";
      span.innerHTML = escapeHtml(part.content).replace(
        /`([^`]+)`/g,
        '<span class="inline-code">$1</span>',
      );
      bubble.appendChild(span);
    }
  });

  // Last, once every part is in the DOM. renderContent is also called
  // repeatedly while a reply streams, and KaTeX is happy to render a
  // half-arrived expression as an error - but each call rebuilds the
  // bubble from scratch, so the final one always renders the finished
  // text and overwrites anything the earlier passes got wrong.
  renderMath(bubble);
}

/* ----------------------------------------------------------------
   Creating a thread in the current bay
   ---------------------------------------------------------------- */
async function ensureThread() {
  if (currentThreadId) return currentThreadId;
  const res = await fetch("/api/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: currentBay }),
  });
  currentThreadId = (await res.json()).id;
  return currentThreadId;
}

/* ----------------------------------------------------------------
   Sending — Code / Chat bays
   ---------------------------------------------------------------- */
let activeStreamController = null;

// Human-readable labels for the tools the model can call. Anything not
// listed still shows, just under its raw name.
const TOOL_LABELS = {
  web_search: ["Searching the web", "Searched the web"],
  run_python: ["Running code", "Ran code"],
  generate_image: ["Creating an image", "Created an image"],
};

function toolStatusStrip(msgEl) {
  let strip = msgEl.querySelector(".tool-activity");
  if (!strip) {
    strip = document.createElement("div");
    strip.className = "tool-activity";
    msgEl.insertBefore(strip, msgEl.firstChild);
  }
  return strip;
}

function handleToolEvent(msgEl, evt) {
  const [running, finished] = TOOL_LABELS[evt.tool] || [evt.tool, evt.tool];
  const strip = toolStatusStrip(msgEl);
  const id = "tool-" + evt.tool;
  let row = strip.querySelector(`[data-tool="${CSS.escape(id)}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "tool-row";
    row.dataset.tool = id;
    strip.appendChild(row);
  }
  row.classList.toggle("is-running", evt.status === "start");
  row.textContent = evt.status === "start" ? running + "…" : finished;

  if (evt.status !== "done" || !evt.display) return;
  const d = evt.display;
  if (d.kind === "sources") {
    renderSourceChips(msgEl, d.sources);
  } else if (d.kind === "image" && d.url) {
    const img = document.createElement("img");
    img.className = "tool-image";
    img.src = d.url;
    img.alt = d.prompt || "Generated image";
    img.loading = "lazy";
    msgEl.appendChild(img);
  }
}

/* The waiting state. Built as real elements rather than a CSS pseudo so
   the three dots can carry independent animation delays, and so the
   label can say what is being waited for. Removed by clearThinking() the
   moment the first character of the reply arrives. */
function showThinking(bubble, label) {
  if (!bubble) return;
  bubble.classList.add("is-thinking");
  const wrap = document.createElement("span");
  wrap.className = "thinking";
  const dots = document.createElement("span");
  dots.className = "thinking-dots";
  dots.append(document.createElement("i"), document.createElement("i"),
              document.createElement("i"));
  const text = document.createElement("span");
  text.className = "thinking-label";
  text.textContent = label || "Thinking";
  wrap.append(dots, text);
  bubble.replaceChildren(wrap);
}

function clearThinking(bubble) {
  if (!bubble || !bubble.classList.contains("is-thinking")) return;
  bubble.classList.remove("is-thinking");
  bubble.replaceChildren();
}

async function consumeStream(res, bubble) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const msgEl = bubble.parentElement;
  // Tool events travel inside the text stream wrapped in U+001E pairs
  // (see tool_event() in app.py). They have to come out before anything
  // is rendered, and a pair can straddle two network chunks - so text
  // after an unmatched separator is held back rather than shown, or the
  // user would see a flash of raw JSON before it gets stripped.
  const SEP = "";   // U+001E RECORD SEPARATOR
  let buf = "";
  let visible = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    for (;;) {
      const start = buf.indexOf(SEP);
      if (start === -1) break;
      const end = buf.indexOf(SEP, start + 1);
      if (end === -1) break;              // incomplete - wait for more
      clearThinking(bubble);
      visible += buf.slice(0, start);
      try {
        handleToolEvent(msgEl, JSON.parse(buf.slice(start + 1, end)));
      } catch (_) {
        /* a malformed event is not worth breaking the reply over */
      }
      buf = buf.slice(end + 1);
    }

    const cut = buf.indexOf(SEP);
    const sofar = visible + (cut === -1 ? buf : buf.slice(0, cut));
    // Only once there is something to show. renderContent() would blow
    // the indicator away by itself, but the is-thinking CLASS would
    // survive - leaving the shimmer running underneath the reply and the
    // caret suppressed for the rest of the stream.
    if (sofar) clearThinking(bubble);
    renderContent(bubble, sofar);
    chatLog.parentElement.scrollTop = chatLog.parentElement.scrollHeight;
  }
  const cut = buf.indexOf(SEP);
  visible += cut === -1 ? buf : buf.slice(0, cut);
  renderContent(bubble, visible);
  return visible;
}

async function sendChatMessage(text) {
  const model = modelSelect.value;
  // Deep mode always searches first; Quick still searches for messages
  // that obviously need current info, so switching to Quick isn't a
  // privacy/accuracy cliff, just less automatic about it.
  const useWebSearch = currentStrength === "deep" || shouldAutoSearch(text);
  const files = pendingAttachments;
  pendingAttachments = [];
  renderAttachChips();

  const userBubble = addMessage("user", text);
  renderMsgAttachments(userBubble.parentElement, files);
  const bubble = addMessage("assistant", "", activeProvider, model);
  const msgEl = bubble.parentElement;
  msgEl.classList.add("streaming");
  sendBtn.classList.add("is-streaming");
  sendBtn.disabled = false;
  sendBtn.title = "Stop generating";
  topbarModelChip.textContent = model;
  // Named for what is actually happening: Deep mode searches the web
  // before it writes anything, and "Thinking" during a search is the
  // kind of small lie that makes a wait feel longer than it is.
  showThinking(bubble, useWebSearch ? "Searching the web" : "Thinking");

  const controller = new AbortController();
  activeStreamController = controller;

  let webResults = [];

  try {
    if (useWebSearch) {
      const status = document.createElement("div");
      status.className = "search-status";
      status.innerHTML =
        '<span class="spin-dot"></span><span>searching the web…</span>';
      msgEl.appendChild(status);
      try {
        const sres = await fetch("/api/web-search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: text }),
          signal: controller.signal,
        });
        const sdata = await sres.json().catch(() => ({}));
        webResults = sdata.results || [];
      } catch (err) {
        if (err.name === "AbortError") throw err;
        webResults = [];
      }
      status.remove();
      renderSourceChips(msgEl, webResults);
    }

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: currentThreadId,
        provider: activeProvider,
        model,
        message: text,
        web_results: webResults,
        attachments: files,
        strength: currentStrength,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      renderContent(bubble, data.error || "Something went wrong.");
      if (data.credits) renderCredits(data.credits);
      return;
    }

    const fullText = await consumeStream(res, bubble);

    if (!fullText) {
      renderContent(bubble, "[No response received. Check the channel setup.]");
    } else {
      addMessageActions(msgEl, bubble, { allowRegenerate: true });
    }
  } catch (err) {
    if (err.name === "AbortError") {
      if (!bubble.textContent) renderContent(bubble, "[Stopped]");
      else addMessageActions(msgEl, bubble, { allowRegenerate: true });
    } else {
      renderContent(bubble, "Something went wrong reaching the server.");
    }
  } finally {
    activeStreamController = null;
    sendBtn.classList.remove("is-streaming");
    sendBtn.title = "Send";
    bubble.parentElement.classList.remove("streaming");
    sendBtn.disabled = false;
    loadThreadList();
    loadCredits();
  }
}

sendBtn.addEventListener("click", (e) => {
  if (activeStreamController) {
    e.preventDefault();
    activeStreamController.abort();
  }
});

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (activeStreamController) return; // send button is in stop mode right now
  const text = messageInput.value.trim();
  if (!text || !activeProvider) return;

  await ensureThread();
  messageInput.value = "";
  messageInput.style.height = "auto";
  if (currentBay === "image") await sendImagePrompt(text);
  else await sendChatMessage(text);
});

/* ----------------------------------------------------------------
   Sending — Image bay
   ---------------------------------------------------------------- */
async function sendImagePrompt(text) {
  addMessage("user", text);
  const bubble = addMessage(
    "assistant",
    "",
    "imagegen",
    imageBackend === "flux" ? "FLUX" : "Local",
  );
  const msgEl = bubble.parentElement;
  msgEl.classList.add("streaming");
  sendBtn.classList.add("is-streaming");
  sendBtn.title = "Generating…";
  sendBtn.disabled = true; // one-shot request, nothing to stream/abort
  showThinking(bubble, "Drawing");

  const status = document.createElement("div");
  status.className = "search-status";
  status.innerHTML =
    '<span class="spin-dot"></span><span>generating an image… can take a minute</span>';
  msgEl.appendChild(status);

  try {
    const res = await fetch("/api/generate-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: currentThreadId,
        prompt: text,
        backend: imageBackend,
        size: imageSize,
        style: imageStyle,
      }),
    });
    const data = await res.json().catch(() => ({}));
    status.remove();

    if (!res.ok || data.error) {
      renderContent(bubble, data.error || "Image generation failed.");
      if (data.credits) renderCredits(data.credits);
      return;
    }

    const img = document.createElement("img");
    img.className = "generated-image";
    img.src = data.url;
    img.alt = text;
    bubble.appendChild(img);
    addMessageActions(msgEl, bubble, { allowRegenerate: false });
  } catch (err) {
    status.remove();
    renderContent(bubble, "Something went wrong reaching the server.");
  } finally {
    sendBtn.classList.remove("is-streaming");
    sendBtn.title = "Send";
    sendBtn.disabled = false;
    msgEl.classList.remove("streaming");
    loadThreadList();
    loadCredits();
  }
}

async function regenerateLast(oldMsgEl) {
  if (activeStreamController || !currentThreadId) return;
  const model = modelSelect.value;

  oldMsgEl.remove();
  const bubble = addMessage("assistant", "", activeProvider, model);
  const msgEl = bubble.parentElement;
  msgEl.classList.add("streaming");
  sendBtn.classList.add("is-streaming");
  sendBtn.title = "Stop generating";
  topbarModelChip.textContent = model;
  showThinking(bubble, "Thinking");

  const controller = new AbortController();
  activeStreamController = controller;

  try {
    const res = await fetch(`/api/threads/${currentThreadId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: activeProvider,
        model,
        strength: currentStrength,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      renderContent(bubble, data.error || "Something went wrong.");
      if (data.credits) renderCredits(data.credits);
      return;
    }

    const fullText = await consumeStream(res, bubble);
    if (!fullText) {
      renderContent(bubble, "[No response received. Check the channel setup.]");
    } else {
      addMessageActions(msgEl, bubble, { allowRegenerate: true });
    }
  } catch (err) {
    if (err.name === "AbortError") {
      if (!bubble.textContent) renderContent(bubble, "[Stopped]");
      else addMessageActions(msgEl, bubble, { allowRegenerate: true });
    } else {
      renderContent(bubble, "Something went wrong reaching the server.");
    }
  } finally {
    activeStreamController = null;
    sendBtn.classList.remove("is-streaming");
    sendBtn.title = "Send";
    msgEl.classList.remove("streaming");
    loadThreadList();
    loadCredits();
  }
}

/* ----------------------------------------------------------------
   Settings modal — tabs, account, plan, about
   ---------------------------------------------------------------- */
const settingsBtn = document.getElementById("settingsBtn");
const settingsBtnLabel = document.getElementById("settingsBtnLabel");
const settingsBackdrop = document.getElementById("settingsBackdrop");
const settingsClose = document.getElementById("settingsClose");
const modalTabs = [...document.querySelectorAll(".modal-tab")];
const modalPanels = {
  account: document.getElementById("accountPanel"),
  general: document.getElementById("generalPanel"),
  plan: document.getElementById("planPanel"),
  memory: document.getElementById("memoryPanel"),
  appearance: document.getElementById("appearancePanel"),
  data: document.getElementById("dataPanel"),
  about: document.getElementById("aboutPanel"),
};

const authSignedOut = document.getElementById("authSignedOut");
const authSignedIn = document.getElementById("authSignedIn");

const googleSignInBtn = document.getElementById("googleSignInBtn");
const googleAuthError = document.getElementById("googleAuthError");
const googleNotConfiguredNote = document.getElementById(
  "googleNotConfiguredNote",
);

const accountAvatar = document.getElementById("accountAvatar");
const accountEmail = document.getElementById("accountEmail");
const accountPlanBadge = document.getElementById("accountPlanBadge");
const logoutBtn = document.getElementById("logoutBtn");

const planNote = document.getElementById("planNote");
const planError = document.getElementById("planError");
const planFineprint = document.getElementById("planFineprint");
const planFreeBtn = document.getElementById("planFreeBtn");
const planProBtn = document.getElementById("planProBtn");

const aboutStatusDot = document.getElementById("aboutStatusDot");
const aboutStatusText = document.getElementById("aboutStatusText");
const aboutModelName = document.getElementById("aboutModelName");

let currentUser = null;

function openSettings() {
  settingsBackdrop.hidden = false;
  refreshAuthUI();
  renderAbout();
  loadMemoryPanel();
  loadSubscription();
  renderUserInfo();
}
function closeSettings() {
  settingsBackdrop.hidden = true;
}
settingsBtn.addEventListener("click", openSettings);
settingsClose.addEventListener("click", closeSettings);
settingsBackdrop.addEventListener("click", (e) => {
  if (e.target === settingsBackdrop) closeSettings();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !settingsBackdrop.hidden) closeSettings();
});

function switchModalTab(tab) {
  modalTabs.forEach((btn) =>
    btn.setAttribute(
      "aria-selected",
      btn.dataset.tab === tab ? "true" : "false",
    ),
  );
  Object.entries(modalPanels).forEach(([name, panel]) => {
    panel.hidden = name !== tab;
  });
}
modalTabs.forEach((btn) =>
  btn.addEventListener("click", () => switchModalTab(btn.dataset.tab)),
);

/* ---- Memory: custom instructions + remembered facts ---- */
const customInstructionsInput = document.getElementById(
  "customInstructionsInput",
);
const saveInstructionsBtn = document.getElementById("saveInstructionsBtn");
const instructionsSavedNote = document.getElementById(
  "instructionsSavedNote",
);
const memoryList = document.getElementById("memoryList");
const memoryAddForm = document.getElementById("memoryAddForm");
const memoryAddInput = document.getElementById("memoryAddInput");

async function loadMemoryPanel() {
  try {
    const res = await fetch("/api/memory");
    const data = await res.json();
    customInstructionsInput.value = data.custom_instructions || "";
    renderMemoryList(data.memories || []);
  } catch (e) {
    memoryList.innerHTML =
      '<div class="memory-empty">Couldn\'t load memory right now.</div>';
  }
}

function renderMemoryList(memories) {
  memoryList.innerHTML = "";
  if (memories.length === 0) {
    memoryList.innerHTML =
      '<div class="memory-empty">Nothing remembered yet.</div>';
    return;
  }
  memories.forEach((m) => {
    const row = document.createElement("div");
    row.className = "memory-item";
    const text = document.createElement("span");
    text.textContent = m.content;
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "memory-del-btn";
    delBtn.textContent = "Remove";
    delBtn.onclick = async () => {
      delBtn.disabled = true;
      const res = await fetch(`/api/memory/${m.id}`, { method: "DELETE" });
      if (res.ok) row.remove();
      else delBtn.disabled = false;
    };
    row.appendChild(text);
    row.appendChild(delBtn);
    memoryList.appendChild(row);
  });
}

saveInstructionsBtn.addEventListener("click", async () => {
  saveInstructionsBtn.disabled = true;
  try {
    await fetch("/api/memory/instructions", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: customInstructionsInput.value }),
    });
    instructionsSavedNote.hidden = false;
    setTimeout(() => (instructionsSavedNote.hidden = true), 1800);
  } finally {
    saveInstructionsBtn.disabled = false;
  }
});

memoryAddForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = memoryAddInput.value.trim();
  if (!content) return;
  const res = await fetch("/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (res.ok) {
    memoryAddInput.value = "";
    loadMemoryPanel();
  }
});

/* ---- Sign-in: Google only ---- */

function setError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

googleSignInBtn.addEventListener("click", () => {
  // Full navigation, not fetch() - OAuth needs an actual page redirect to
  // Google's consent screen, not an API call.
  window.location.href = "/api/auth/google/login";
});

/* ---- Local email/password signup + login ---- */
const localAuthForm = document.getElementById("localAuthForm");
const localAuthEmail = document.getElementById("localAuthEmail");
const localAuthPassword = document.getElementById("localAuthPassword");
const localAuthSubmit = document.getElementById("localAuthSubmit");
const localAuthError = document.getElementById("localAuthError");
const localAuthSwitch = document.getElementById("localAuthSwitch");

let localAuthMode = "signup";

localAuthSwitch.addEventListener("click", () => {
  localAuthMode = localAuthMode === "signup" ? "login" : "signup";
  const signingUp = localAuthMode === "signup";
  localAuthSubmit.textContent = signingUp ? "Sign up" : "Log in";
  localAuthSwitch.textContent = signingUp
    ? "Already have an account? Log in"
    : "Need an account? Sign up";
  localAuthPassword.placeholder = signingUp
    ? "Password (8+ characters)"
    : "Password";
  localAuthPassword.autocomplete = signingUp
    ? "new-password"
    : "current-password";
  setError(localAuthError, "");
});

localAuthForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  setError(localAuthError, "");
  localAuthSubmit.disabled = true;
  try {
    const res = await fetch(`/api/auth/${localAuthMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: localAuthEmail.value.trim(),
        password: localAuthPassword.value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(localAuthError, data.error || "Something went wrong.");
      return;
    }
    currentUser = data.user;
    localAuthForm.reset();
    refreshAuthUI();
    loadCredits();
    // Threads move to the new account server-side on signup/login, so
    // the sidebar has to re-read them rather than keep the guest list.
    loadThreadList();
  } catch (err) {
    setError(localAuthError, "Could not reach the server.");
  } finally {
    localAuthSubmit.disabled = false;
  }
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  refreshAuthUI();
  loadCredits();
  currentThreadId = null;
  showEmptyState();
  loadThreadList();
});

function refreshAuthUI() {
  setError(planError, "");
  const signedIn = !!currentUser;
  authSignedOut.hidden = signedIn;
  authSignedIn.hidden = !signedIn;
  settingsBtnLabel.textContent = signedIn
    ? currentUser.email
    : "Sign up / Settings";

  if (signedIn) {
    accountAvatar.textContent = currentUser.email[0].toUpperCase();
    accountEmail.textContent = currentUser.email;
    accountPlanBadge.textContent =
      currentUser.plan === "pro" ? "Pro plan" : "Free plan";
    planNote.textContent = `Signed in as ${currentUser.email}.`;
    planFreeBtn.disabled = currentUser.plan !== "pro";
    planFreeBtn.textContent =
      currentUser.plan === "pro" ? "Downgrade to Free" : "Current plan";
    planProBtn.hidden = currentUser.plan === "pro";
  } else {
    planNote.textContent = "Sign in to manage your plan.";
    planFreeBtn.disabled = true;
    planFreeBtn.textContent = "Current plan";
    planProBtn.hidden = false;
  }
}

async function changePlan(plan) {
  setError(planError, "");
  if (!currentUser) {
    setError(
      planError,
      "Sign in first to upgrade — switch to the Account tab.",
    );
    return;
  }
  planProBtn.disabled = true;
  try {
    const res = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan }),
    });
    const data = await res.json();
    if (res.ok && data.checkout_url) {
      // Paddle hands back our own URL with ?_ptxn=<transaction id> on it,
      // because its checkout is an overlay rather than a page. Pulling the
      // id out and opening the overlay in place is the same flow without
      // the full page reload - and it still works if Paddle.js is slow,
      // because the navigation below is kept as the fallback.
      const txn = new URL(data.checkout_url, window.location.origin)
        .searchParams.get("_ptxn");
      if (txn && paddleReady) {
        try {
          const paddle = await paddleReady;
          paddle.Checkout.open({ transactionId: txn });
          planProBtn.disabled = false;
          return;
        } catch (e) {
          console.error("Paddle overlay failed:", e);
          setError(
            planError,
            "The payment window could not open. Reload the page and try " +
              "again — if it keeps happening, check that the browser or an " +
              "ad blocker isn't blocking cdn.paddle.com.",
          );
          planProBtn.disabled = false;
          return;
        }
      }
      // No transaction id means this isn't a Paddle URL - a Stripe
      // checkout link, which really is a page to send the browser to.
      if (!txn) {
        window.location.href = data.checkout_url;
        return;
      }
      // Paddle URL, but Paddle.js never initialised. Navigating there
      // does eventually work - the reloaded page starts Paddle.js, which
      // opens the overlay from ?_ptxn - but it looks exactly like the app
      // resetting itself for no reason, so say what is happening instead
      // of doing it silently.
      setError(
        planError,
        "The payment window isn't ready yet. Reload the page and try again.",
      );
      planProBtn.disabled = false;
      return;
    } else if (res.ok) {
      currentUser = data.user;
      refreshAuthUI();
      renderCredits(data.credits);
    } else {
      // `detail` names the exact missing piece of Stripe config when the
      // server has one - far more actionable than "could not change plan".
      setError(
        planError,
        [data.error, data.detail].filter(Boolean).join(" ") ||
          "Could not change plan.",
      );
    }
  } catch (err) {
    setError(planError, "Could not reach the server.");
  } finally {
    // Only re-enable if buying is actually possible. An unconditional
    // `= false` here would undo the disabled state set in
    // loadPlansMeta() and put the dead-end click straight back.
    planProBtn.disabled = !billingLive;
  }
}
planProBtn.addEventListener("click", () => changePlan("pro"));
planFreeBtn.addEventListener("click", () => {
  if (currentUser && currentUser.plan === "pro") changePlan("free");
});

let billingLive = false;

// Paddle's checkout is an overlay drawn on this page by Paddle.js, not a
// hosted page to redirect to like Stripe's. So the script has to be
// present and initialised before any Upgrade click, and the transaction
// id arrives as ?_ptxn= on our own URL. Without this the whole flow
// looks broken in the most confusing way possible: the transaction is
// created successfully, the browser navigates, and nothing appears.
let paddleReady = null;

function loadPaddle(token, environment) {
  if (paddleReady) return paddleReady;
  paddleReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.paddle.com/paddle/v2/paddle.js";
    s.onload = () => {
      try {
        // Must be set before Initialize, and only for sandbox - calling
        // it with "production" is not valid.
        if (environment === "sandbox") window.Paddle.Environment.set("sandbox");
        window.Paddle.Initialize({ token });
        resolve(window.Paddle);
      } catch (e) {
        reject(e);
      }
    };
    s.onerror = () => reject(new Error("Paddle.js failed to load"));
    document.head.appendChild(s);
  });
  return paddleReady;
}

async function loadPlansMeta() {
  try {
    const res = await fetch("/api/plans");
    const data = await res.json();
    billingLive = !!data.billing_live;

    if (data.processor === "paddle" && data.paddle_client_token) {
      // Initialised on page load, not on click: Paddle.js opens the
      // overlay by itself when it sees ?_ptxn= in the URL, and it can
      // only do that if it is already running when the page loads.
      loadPaddle(data.paddle_client_token, data.paddle_environment).catch(
        (e) => console.error("Paddle failed to initialise:", e),
      );
    }

    if (billingLive) {
      planProBtn.disabled = false;
      planProBtn.textContent = "Upgrade to Pro";
      planProBtn.removeAttribute("title");
      planFineprint.textContent =
        data.processor === "paddle"
          ? "Card details are entered in Paddle's own checkout and never " +
            "touch this server. Paddle is the seller of record and handles " +
            "VAT. Cancel any time from the button above."
          : "Real card payments via Stripe. Card details are entered on " +
            "Stripe's own checkout page and never touch this server. Manage " +
            "or cancel any time from the button above.";
    } else {
      // Disable rather than let the click fail. Previously the button
      // stayed active, the click returned 503, and the resulting red
      // error said the same thing as the fineprint directly beneath it -
      // the same sentence twice, the second one clipped by the modal.
      // One statement, in one place, and no dead-end click.
      planProBtn.disabled = true;
      planProBtn.textContent = "Pro not available yet";
      planProBtn.title =
        "This server hasn't been connected to a payment provider.";
      planFineprint.textContent =
        "Pro isn't purchasable yet — this server hasn't been connected " +
        "to Stripe. Everything in Free works normally.";
    }
    setError(planError, "");
  } catch (err) {
    /* fineprint keeps its default text */
  }
}

/* ----------------------------------------------------------------
   Appearance — accent colour, sky-theme override, account info.
   All of it is per-browser (localStorage), not per-account: it's a
   display preference, not data worth a server round-trip, and it needs
   to apply before first paint rather than after a fetch resolves.
   ---------------------------------------------------------------- */
const accentSwatches = document.getElementById("accentSwatches");
const customAccent = document.getElementById("customAccent");
const skyThemeSelect = document.getElementById("skyThemeSelect");
const userInfoGrid = document.getElementById("userInfoGrid");

const ACCENT_PRESETS = [
  { name: "Cyan", value: "#22d3ee" },
  { name: "Blue", value: "#38bdf8" },
  { name: "Violet", value: "#a78bfa" },
  { name: "Green", value: "#34d399" },
  { name: "Amber", value: "#fbbf24" },
  { name: "Rose", value: "#fb7185" },
];

// localStorage can throw outright (Safari private mode, blocked site
// data), not just return null - so every access is guarded and falls
// back to the stylesheet's own defaults rather than breaking boot.
function readPref(key) {
  try {
    return localStorage.getItem(key);
  } catch (e) {
    return null;
  }
}
function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* preference just won't persist; the app still works */
  }
}

function hexToSoft(hex, alpha) {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function applyAccent(hex) {
  // Override every bay's accent, so the choice holds whichever
  // workspace is open rather than being reset by the next selectBay().
  ["code", "chat", "image"].forEach((bay) => {
    root.style.setProperty(`--bay-${bay}`, hex);
    root.style.setProperty(`--bay-${bay}-soft`, hexToSoft(hex, 0.16));
  });
  root.style.setProperty("--bay", hex);
  root.style.setProperty("--bay-soft", hexToSoft(hex, 0.16));
  if (customAccent) customAccent.value = hex;
  [...accentSwatches.querySelectorAll(".swatch")].forEach((b) =>
    b.setAttribute(
      "aria-pressed",
      String(b.dataset.value.toLowerCase() === hex.toLowerCase()),
    ),
  );
}

function renderSwatches() {
  accentSwatches.innerHTML = "";
  ACCENT_PRESETS.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch";
    b.dataset.value = p.value;
    b.style.background = p.value;
    b.title = p.name;
    b.setAttribute("aria-label", `${p.name} accent`);
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => {
      applyAccent(p.value);
      writePref("accent", p.value);
    });
    accentSwatches.appendChild(b);
  });
}

customAccent.addEventListener("input", () => {
  applyAccent(customAccent.value);
  writePref("accent", customAccent.value);
});

skyThemeSelect.addEventListener("change", () => {
  writePref("skyTheme", skyThemeSelect.value);
  updateTimeOfDay();
});

function renderUserInfo() {
  const rows = currentUser
    ? [
        ["Email", currentUser.email],
        ["Plan", currentUser.plan === "pro" ? "Pro" : "Free"],
        [
          "Member since",
          new Date(currentUser.created).toLocaleDateString(undefined, {
            year: "numeric",
            month: "long",
            day: "numeric",
          }),
        ],
        ["Account ID", currentUser.id],
      ]
    : [["Signed in", "Not signed in — using a guest session"]];

  userInfoGrid.innerHTML = "";
  rows.forEach(([label, value]) => {
    const l = document.createElement("span");
    l.className = "userinfo-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "userinfo-value";
    v.textContent = value;
    userInfoGrid.appendChild(l);
    userInfoGrid.appendChild(v);
  });
}

function initAppearance() {
  renderSwatches();
  const savedAccent = readPref("accent");
  if (savedAccent) applyAccent(savedAccent);
  const savedSky = readPref("skyTheme");
  if (savedSky) skyThemeSelect.value = savedSky;
}

/* ---- Subscription dashboard: real plan status from Stripe ---- */
const subDashboard = document.getElementById("subDashboard");
const subPlanValue = document.getElementById("subPlanValue");
const subStatusRow = document.getElementById("subStatusRow");
const subStatusValue = document.getElementById("subStatusValue");
const subRenewalRow = document.getElementById("subRenewalRow");
const subRenewalLabel = document.getElementById("subRenewalLabel");
const subRenewalValue = document.getElementById("subRenewalValue");
const managePlanBtn = document.getElementById("managePlanBtn");

async function loadSubscription() {
  if (!currentUser) {
    subDashboard.hidden = true;
    return;
  }
  try {
    const res = await fetch("/api/billing/subscription");
    if (!res.ok) {
      subDashboard.hidden = true;
      return;
    }
    const s = await res.json();
    subDashboard.hidden = false;
    subPlanValue.textContent = `${s.plan_label} · ${s.price}`;

    subStatusRow.hidden = !s.status;
    if (s.status) subStatusValue.textContent = s.status;

    if (s.current_period_end) {
      subRenewalRow.hidden = false;
      // "Renews" vs "Cancels" is a materially different message - a
      // subscription set to lapse shouldn't imply it's about to charge.
      subRenewalLabel.textContent = s.cancel_at_period_end
        ? "Cancels"
        : "Renews";
      subRenewalValue.textContent = new Date(
        s.current_period_end,
      ).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
      subRenewalValue.classList.toggle(
        "is-cancelling",
        !!s.cancel_at_period_end,
      );
    } else {
      subRenewalRow.hidden = true;
    }

    managePlanBtn.hidden = !(s.billing_live && s.has_billing_account);
  } catch (err) {
    subDashboard.hidden = true;
  }
}

managePlanBtn.addEventListener("click", async () => {
  managePlanBtn.disabled = true;
  try {
    const res = await fetch("/api/billing/portal", { method: "POST" });
    const data = await res.json();
    if (res.ok && data.portal_url) {
      window.location.href = data.portal_url;
      return;
    }
    setError(planError, data.error || "Could not open the billing portal.");
  } catch (err) {
    setError(planError, "Could not reach the server.");
  } finally {
    managePlanBtn.disabled = false;
  }
});

function handleCheckoutReturn() {
  const params = new URLSearchParams(window.location.search);
  const checkout = params.get("checkout");
  if (!checkout) return;
  if (checkout === "success") {
    setError(planError, "");
    planNote.textContent = "Payment received — syncing your plan…";
  } else if (checkout === "cancel") {
    setError(planError, "Checkout cancelled — no charge was made.");
  }
  params.delete("checkout");
  const clean =
    window.location.pathname + (params.toString() ? `?${params}` : "");
  window.history.replaceState({}, "", clean);
}

async function loadAuthState() {
  try {
    const res = await fetch("/api/auth/me");
    const data = await res.json();
    currentUser = data.user || null;
    const configured = data.google_configured !== false;
    googleSignInBtn.hidden = !configured;
    googleNotConfiguredNote.hidden = configured;
  } catch (err) {
    currentUser = null;
  }
  refreshAuthUI();
}

const AUTH_ERROR_MESSAGES = {
  state_mismatch: "That sign-in attempt expired — try again.",
  denied: "Google sign-in was cancelled.",
  google_unreachable: "Could not reach Google — try again in a moment.",
  unverified_email: "That Google account's email isn't verified.",
  not_configured: "Google sign-in isn't configured on this server yet.",
};

function handleAuthReturn() {
  const params = new URLSearchParams(window.location.search);
  const err = params.get("auth_error");
  if (!err) return;
  setError(googleAuthError, AUTH_ERROR_MESSAGES[err] || "Sign-in failed.");
  params.delete("auth_error");
  const clean =
    window.location.pathname + (params.toString() ? `?${params}` : "");
  window.history.replaceState({}, "", clean);
  openSettings();
}

/* ---- About tab ---- */
function renderAbout() {
  const p = providers.find((x) => x.id === "ollama");
  if (!p) {
    aboutStatusDot.className = "about-status-dot";
    aboutStatusText.textContent = "checking…";
    return;
  }
  aboutStatusDot.className =
    "about-status-dot" + (p.available ? " online" : "");
  aboutStatusText.textContent = p.available
    ? "running locally, responding"
    : p.note || "not reachable";
  aboutModelName.textContent = p.available
    ? `currently running ${p.models[0]}`
    : "no model detected yet";
}

/* ----------------------------------------------------------------
   Boot screen — shown until the first real data load resolves, with
   a minimum hold so it never just flashes on a fast connection.
   ---------------------------------------------------------------- */
const bootScreen = document.getElementById("bootScreen");
const bootLabel = document.getElementById("bootLabel");

async function boot() {
  const minHold = new Promise((resolve) => setTimeout(resolve, 650));
  bootLabel.textContent = "reaching the local model…";
  initAppearance();
  handleCheckoutReturn();
  handleAuthReturn();

  await Promise.all([
    loadProviders(),
    loadThreadList(),
    loadCredits(),
    loadAuthState(),
    loadPlansMeta(),
    minHold,
  ]);

  bootScreen.classList.add("boot-done");
  setTimeout(() => (bootScreen.hidden = true), 500);
}

/* ----------------------------------------------------------------
   Collapsible sidebar
   ---------------------------------------------------------------- */
const sidebarCollapseBtn = document.getElementById("sidebarCollapse");
const appShell = document.querySelector(".app");
const SIDEBAR_KEY = "sidebarCollapsed";

function applySidebarCollapsed(collapsed) {
  if (!appShell || !sidebarCollapseBtn) return;
  appShell.classList.toggle("sidebar-collapsed", collapsed);
  // The accessible name stays constant and the state is carried by
  // aria-expanded, which is what a screen reader announces. A label
  // that flips between "Collapse" and "Expand" reads as two different
  // controls appearing in the same place.
  sidebarCollapseBtn.setAttribute("aria-expanded", String(!collapsed));
  sidebarCollapseBtn.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  writePref(SIDEBAR_KEY, collapsed ? "1" : "0");
}

if (sidebarCollapseBtn) {
  // Restored before first paint would be better, but the preference
  // lives in localStorage and this script is deferred, so the width
  // transition is suppressed for the initial application to avoid the
  // sidebar visibly sliding shut on every load.
  const saved = readPref(SIDEBAR_KEY) === "1";
  if (saved) {
    appShell.style.transition = "none";
    applySidebarCollapsed(true);
    // Two frames: one for the class to apply, one for the browser to
    // finish laying out before transitions are allowed back.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        appShell.style.transition = "";
      }),
    );
  }
  sidebarCollapseBtn.addEventListener("click", () => {
    applySidebarCollapsed(!appShell.classList.contains("sidebar-collapsed"));
  });
}

/* ----------------------------------------------------------------
   Greeting
   ---------------------------------------------------------------- */
const NAME_KEY = "displayName";

function greetingFor(hour) {
  if (hour < 5) return "Still up";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function renderGreeting() {
  if (!emptyState) return;
  let host = document.getElementById("greetingHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "greetingHost";
    // Above the eyebrow, so the greeting is the first thing read rather
    // than an afterthought under the bay title.
    emptyState.insertBefore(host, emptyState.firstChild);
  }
  host.replaceChildren();

  const name = (readPref(NAME_KEY) || "").trim();
  if (!name) {
    const h = document.createElement("h2");
    h.className = "greeting";
    h.textContent = "What should I call you?";
    const row = document.createElement("div");
    row.className = "name-prompt";
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 40;
    input.placeholder = "Your name";
    input.setAttribute("aria-label", "Your name");
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "Save";
    const commit = () => {
      const v = input.value.trim();
      if (!v) return;
      writePref(NAME_KEY, v);
      renderGreeting();
    };
    save.addEventListener("click", commit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commit();
    });
    row.append(input, save);
    host.append(h, row);
    return;
  }

  const h = document.createElement("h2");
  h.className = "greeting";
  // textContent, never innerHTML - this string is whatever the user
  // typed, and it is re-rendered on every empty state.
  h.textContent = `${greetingFor(new Date().getHours())}, ${name}`;
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "greeting-edit";
  edit.textContent = "not you?";
  edit.addEventListener("click", () => {
    writePref(NAME_KEY, "");
    renderGreeting();
  });
  host.append(h, edit);
}

/* ---------------------------------------------------------------- */
updateEmptyState();
boot();

/* ----------------------------------------------------------------
   Video bay - generation

   A panel rather than a thread: a clip takes minutes and has settings,
   which is not a shape the chat log has a message type for.

   This replaced an ffmpeg editor. The important difference for anyone
   reading this later is that there is no upload and no source file -
   the only input is a sentence, and the only output is a URL on the
   provider's CDN. Nothing is stored on our server at all.

   Generation is slow (minutes, not seconds) and costs real money per
   clip, so two things matter more here than elsewhere: the remaining
   quota is always on screen, and the wait says what is happening rather
   than showing an unexplained spinner.
   ---------------------------------------------------------------- */
const videoBay = document.getElementById("videoBay");
const genForm = document.getElementById("genForm");
const genPrompt = document.getElementById("genPrompt");
const genRun = document.getElementById("genRun");
const genSeconds = document.getElementById("genSeconds");
const genSecondsOut = document.getElementById("genSecondsOut");
const genRatio = document.getElementById("genRatio");
const genQuality = document.getElementById("genQuality");
const genQuotaEl = document.getElementById("genQuota");
const genLocked = document.getElementById("genLocked");
const genLockedText = document.getElementById("genLockedText");
const genSub = document.getElementById("genSub");
const videoStatusEl = document.getElementById("videoStatus");
const videoResult = document.getElementById("videoResult");
const videoOut = document.getElementById("videoOut");
const modelOut = document.getElementById("modelOut");
const genTitle = document.getElementById("genTitle");
// "video" or "model" - what this bay currently produces, from
// /api/video/status. The two share a route, a quota and a job table;
// only the output element and the wording differ.
let genKind = "video";
const videoDownload = document.getElementById("videoDownload");

let videoPoll = null;

function videoSay(msg, isError) {
  if (!videoStatusEl) return;
  videoStatusEl.hidden = !msg;
  videoStatusEl.textContent = msg || "";
  videoStatusEl.classList.toggle("is-error", !!isError);
}

function renderQuota(q) {
  if (!genQuotaEl || !q) return;
  if (!q.allowed) { genQuotaEl.textContent = ""; return; }
  genQuotaEl.textContent = `${q.remaining} of ${q.limit} left this month`;
}

/* Locking the form rather than hiding the bay. Someone on Free should be
   able to see what the feature is and what it costs before deciding to
   pay for it - a bay that simply is not there sells nothing. */
function applyVideoAccess(d) {
  const q = d.quota || {};

  // WHAT THIS BAY IS DECIDED FIRST.
  //
  // Diagrams have no provider behind them - the model writes Mermaid and
  // the browser draws it - so none of the "is a key configured" checks
  // below apply. Running them first was a real bug: with no media key
  // set the function returned early with the bay locked, and the diagram
  // mode it should have fallen back to was never reached.
  // DIAGRAM IS THE FLOOR, not the last resort of a broken chain.
  //
  // A media backend only wins if it can actually run. A configured Tripo
  // key with a zero balance is configured and useless, and treating that
  // as "3D mode" locked the whole bay behind a credits notice - with the
  // diagram bay, which needs no key and no balance, sitting right there
  // unreachable. Anything that cannot generate falls through to drawing.
  const mediaUsable =
    d.configured && (d.credits === undefined || d.credits === null
                     || d.credits > 0);
  const kind =
    !mediaUsable ? "diagram"
      : d.kind === "model" ? "model"
      : "video";

  if (kind === "diagram") {
    genKind = "diagram";
    genLocked.hidden = true;
    genForm.hidden = false;
    if (genTitle) genTitle.textContent = "Draw a diagram";
    if (genSub) {
      genSub.textContent =
        "Describe a system, a flow or a schema and see it drawn.";
    }
    if (genPrompt) {
      genPrompt.placeholder =
        "How an OAuth 2.0 authorization code flow works, "
        + "including the token exchange";
    }
    if (genQuotaEl) genQuotaEl.textContent = "";
    renderGenExamples("diagram");
    // None of the media controls mean anything for a diagram.
    ["genSeconds", "genRatio", "genQuality"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.closest(".gen-ctl")) el.closest(".gen-ctl").hidden = true;
    });
    return;
  }

  if (!d.configured) {
    genLocked.hidden = false;
    // The server says which key is missing; show that rather than a
    // generic line, since the person reading this is usually the one
    // who can fix it.
    genLockedText.textContent =
      d.detail || "Video generation isn't switched on for this server yet.";
    genForm.hidden = true;
    return;
  }
  // The free backend has no quality or aspect controls behind it, so
  // hide the ones that would do nothing rather than let someone set a
  // value that is quietly ignored.
  // A key with no credits behind it is worse than no key: the bay looks
  // ready and fails on the first click. Say it before anyone types.
  // Unreachable while diagrams are the floor above - kept because the
  // moment another media bay exists that has no fallback, an empty
  // balance has to say so rather than looking broken.
  if (d.kind === "model" && d.credits === 0) {
    genLocked.hidden = false;
    genLockedText.textContent =
      "3D generation is set up but the account has no credits left.";
    genForm.hidden = true;
    return;
  }
  genKind = d.kind === "model" ? "model"
    : d.kind === "video" && d.configured ? "video"
    : "diagram";
  if (genTitle) {
    genTitle.textContent =
      genKind === "model" ? "Make a 3D model"
      : genKind === "video" ? "Make a video"
      : "Draw a diagram";
  }
  if (genSub) {
    genSub.textContent =
      genKind === "model"
        ? "Describe an object and get back a 3D model you can spin."
        : genKind === "video"
        ? "Describe a shot and get it back as a clip, up to "
          + d.max_seconds + "s."
        : "Describe a system, a flow or a schema and see it drawn.";
  }
  if (genPrompt) {
    if (genKind === "model") {
      genPrompt.placeholder =
        "A weathered brass diving helmet with a cracked glass port";
    } else if (genKind === "diagram") {
      genPrompt.placeholder =
        "How an OAuth 2.0 authorization code flow works, "
        + "including the token exchange";
    }
  }
  // The diagram bay has no provider behind it, so nothing is locked and
  // none of the media controls apply.
  if (genKind === "diagram") {
    genLocked.hidden = true;
    genForm.hidden = false;
    if (genQuotaEl) genQuotaEl.textContent = "";
    ["genSeconds", "genRatio", "genQuality"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.closest(".gen-ctl")) el.closest(".gen-ctl").hidden = true;
    });
    return;
  }
  // A mesh has no duration, so the length slider means nothing here.
  const lengthCtl = document.getElementById("genSeconds");
  if (lengthCtl && lengthCtl.closest(".gen-ctl")) {
    lengthCtl.closest(".gen-ctl").hidden = genKind === "model";
  }
  const freeTier = !!d.free_tier;
  const shapeCtl = document.getElementById("genRatio");
  const qualityCtl = document.getElementById("genQuality");
  if (shapeCtl) shapeCtl.closest(".gen-ctl").hidden = freeTier;
  if (qualityCtl) qualityCtl.closest(".gen-ctl").hidden = freeTier;
  if (!q.allowed) {
    genLocked.hidden = false;
    genLockedText.textContent =
      "Video generation is a Pro feature - 10 clips a month.";
    genForm.hidden = true;
    return;
  }
  genLocked.hidden = true;
  genForm.hidden = false;
  renderQuota(q);
  if (genSeconds && d.max_seconds) {
    genSeconds.max = String(d.max_seconds);
    genSeconds.min = String(d.min_seconds || 1);
    genSeconds.value = String(d.default_seconds || 5);
    genSecondsOut.textContent = genSeconds.value + "s";
  }
  if (genSub) {
    genSub.textContent =
      `Describe a shot and get it back as a clip, up to ${d.max_seconds}s.`;
  }
}

async function loadVideoLimits() {
  if (!videoBay) return;
  try {
    const r = await fetch("/api/video/status");
    applyVideoAccess(await r.json());
  } catch (_) {
    /* Leave the form as the markup has it. A status call failing is not
       a reason to take the feature away - the generate call will report
       anything genuinely wrong, with a real message. */
  }
}

if (genSeconds) {
  genSeconds.addEventListener("input", () => {
    genSecondsOut.textContent = genSeconds.value + "s";
  });
}

const genExamples = document.getElementById("genExamples");
if (genExamples) {
  genExamples.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-ex]");
    if (!b) return;
    genPrompt.value = b.dataset.ex;
    genPrompt.focus();
  });
}

/* ----------------------------------------------------------------
   Diagram bay

   The only generative bay with no provider bill behind it: the server
   returns Mermaid source and the browser draws it. That is why it can
   be free at any volume on hardware that could not afford video - the
   expensive step happens on the viewer's machine, not the server's.

   Synchronous, so it skips the job/polling machinery the video and 3D
   backends need. One request, one diagram.
   ---------------------------------------------------------------- */
let mermaidReady = false;

function initMermaid() {
  if (mermaidReady || !window.mermaid) return mermaidReady;
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
    mermaidReady = true;
  } catch (_) {
    mermaidReady = false;
  }
  return mermaidReady;
}

async function drawDiagram(source) {
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

async function sendDiagram(prompt) {
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

if (genForm) {
  genForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = (genPrompt.value || "").trim();
    if (!prompt) {
      videoSay("Describe what you want drawn.", true);
      genPrompt.focus();
      return;
    }
    if (genKind === "diagram") {
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

function pollVideoJob(id) {
  clearInterval(videoPoll);
  // Minutes, not seconds - so this says so rather than implying it is
  // nearly done. An invented percentage that sticks is worse than an
  // honest label.
  const started = Date.now();
  videoSay(
    genKind === "model"
      ? "Building the model… usually under a minute."
      : "Generating… this usually takes a minute or two.",
  );
  videoPoll = setInterval(async () => {
    let j, q;
    try {
      const r = await fetch(`/api/video/job/${id}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "gone");
      j = d.job;
      q = d.quota;
    } catch (_) {
      clearInterval(videoPoll);
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

    clearInterval(videoPoll);
    genRun.disabled = false;

    if (j.status === "failed") {
      videoSay(j.error || "That one didn't work. Your quota wasn't used.",
               true);
      return;
    }

    videoSay("");
    if (genKind === "model") {
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

// The "See Pro" button in the locked state. Opens the settings panel at
// the plan section rather than a separate modal, so there is one place
// in this app where a plan is chosen.
const genUpgrade = document.getElementById("genUpgrade");
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


/* ----------------------------------------------------------------
   Settings that do something

   Every control here changes behaviour on the next action - none is a
   placeholder for a feature that does not exist, which is the failure
   mode a settings screen invites. Preferences live in localStorage
   because they are per-browser choices, not account state: signing in
   on a different machine should not drag your font size across.
   ---------------------------------------------------------------- */
const PREF_STRENGTH = "defaultStrength";
const PREF_BAY = "defaultBay";
const PREF_ENTER = "enterToSend";
const PREF_MOTION = "reduceMotion";

function applyMotionPref() {
  const off = readPref(PREF_MOTION) === "1";
  document.documentElement.setAttribute(
    "data-motion", off ? "reduced" : "full");
}

function initSettingsControls() {
  const strength = document.getElementById("defaultStrength");
  const bay = document.getElementById("defaultBay");
  const enter = document.getElementById("enterToSend");
  const motion = document.getElementById("reduceMotion");

  if (strength) {
    strength.value = readPref(PREF_STRENGTH) || "quick";
    strength.addEventListener("change", () => {
      writePref(PREF_STRENGTH, strength.value);
      // Applies now as well as next time: changing a default in front of
      // someone and having it not take effect reads as a broken switch.
      // Apply it now as well as next time: changing a default in front
      // of someone and having nothing happen reads as a broken switch.
      currentStrength = strength.value;
      strengthToggle.setAttribute(
        "aria-checked", String(currentStrength === "deep"));
    });
  }
  if (bay) {
    bay.value = readPref(PREF_BAY) || "code";
    bay.addEventListener("change", () => writePref(PREF_BAY, bay.value));
  }
  if (enter) {
    enter.checked = readPref(PREF_ENTER) !== "0";
    enter.addEventListener("change", () => {
      writePref(PREF_ENTER, enter.checked ? "1" : "0");
    });
  }
  if (motion) {
    motion.checked = readPref(PREF_MOTION) === "1";
    motion.addEventListener("change", () => {
      writePref(PREF_MOTION, motion.checked ? "1" : "0");
      applyMotionPref();
    });
  }
}

/* Filtering the rail, not the whole page. Searching settings is how you
   find a category you cannot name - so this matches the label AND a few
   keywords per tab, or "dark" would find nothing. */
const SETTINGS_KEYWORDS = {
  account: "sign in out email password google login",
  general: "default mode quick deep bay enter send motion animation",
  appearance: "colour color accent theme sky dark light background",
  memory: "custom instructions remember personalization name",
  data: "export delete download privacy conversations egress",
  plan: "billing upgrade pro credits subscription payment",
  about: "version model provider status licence",
};

function initSettingsSearch() {
  const box = document.getElementById("settingsSearch");
  if (!box) return;
  box.addEventListener("input", () => {
    const q = box.value.trim().toLowerCase();
    modalTabs.forEach((t) => {
      if (!q) { t.hidden = false; return; }
      const key = t.dataset.tab;
      const hay = (t.textContent + " " + (SETTINGS_KEYWORDS[key] || ""))
        .toLowerCase();
      t.hidden = !hay.includes(q);
    });
    // Jump to the first surviving tab so the pane matches the list.
    const first = modalTabs.find((t) => !t.hidden);
    if (q && first && first.getAttribute("aria-selected") !== "true") {
      first.click();
    }
  });
}

/* Data controls. Export is a real download of what the server holds for
   you; delete really deletes, one thread at a time through the endpoint
   that already checks ownership - rather than a bulk route that would
   need its own authorisation logic. */
function initDataControls() {
  const exportBtn = document.getElementById("exportData");
  const deleteBtn = document.getElementById("deleteAllThreads");
  const status = document.getElementById("dataStatus");
  const egress = document.getElementById("dataEgress");

  if (exportBtn) {
    exportBtn.addEventListener("click", async () => {
      status.textContent = "Collecting…";
      try {
        const list = await (await fetch("/api/threads")).json();
        const full = [];
        for (const t of list.threads || []) {
          const one = await (await fetch("/api/threads/" + t.id)).json();
          full.push(one);
        }
        const blob = new Blob([JSON.stringify(full, null, 2)],
                              { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "randomgenerals-conversations.json";
        a.click();
        URL.revokeObjectURL(a.href);
        status.textContent = `Exported ${full.length} conversations.`;
      } catch (_) {
        status.textContent = "Could not export just now.";
      }
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(
        "Delete every conversation? This cannot be undone.")) return;
      status.textContent = "Deleting…";
      try {
        const list = await (await fetch("/api/threads")).json();
        let n = 0;
        for (const t of list.threads || []) {
          const r = await fetch("/api/threads/" + t.id, { method: "DELETE" });
          if (r.ok) n += 1;
        }
        status.textContent = `Deleted ${n} conversations.`;
        if (typeof loadThreadList === "function") loadThreadList();
        showEmptyState();
      } catch (_) {
        status.textContent = "Could not delete just now.";
      }
    });
  }

  if (egress) {
    // Named from what the server actually reports, not from a fixed
    // sentence - this app has been wrong about where prompts go before.
    fetch("/api/health").then((r) => r.json()).then((h) => {
      const fast = h.fast_channel && h.fast_channel.configured;
      egress.textContent = fast
        ? "Chat and code are answered by Groq, so those messages leave "
          + "this server. Images go to Pollinations. Web search goes to "
          + "DuckDuckGo. Nothing else is sent anywhere."
        : "Everything is answered on this server right now. Images go to "
          + "Pollinations and web search to DuckDuckGo; nothing else "
          + "leaves.";
    }).catch(() => {
      egress.textContent = "Could not check right now.";
    });
  }
}

// Apply the saved defaults before the first render, so the app opens in
// the state the person chose rather than snapping to it a moment later.
(function applySavedDefaults() {
  const st = readPref(PREF_STRENGTH);
  if (st === "deep" || st === "quick") {
    currentStrength = st;
    strengthToggle.setAttribute("aria-checked", String(st === "deep"));
  }
  const bay = readPref(PREF_BAY);
  if (bay && bay !== currentBay && typeof selectBay === "function") {
    selectBay(bay);
  }
})();

applyMotionPref();
initSettingsControls();
initSettingsSearch();
initDataControls();
