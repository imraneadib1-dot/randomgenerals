const BAY_ORDER = ["code", "chat", "image"];

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
    sub: "Describe what you want to see — generated locally by default, or via Gemini if you've set that up.",
    placeholder: "A red apple on a wooden table…",
    hints:
      "<div><span>Enter</span> to generate · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎ ✺</span> switch bays above</div>",
  },
};

const PROVIDER_META = {
  ollama: { label: "RandomGenerals AI" },
  groq: { label: "Groq" },
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
let geminiConfigured = false;
let imageBackend = "local";

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

const bayPuck = document.getElementById("bayPuck");
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
  if (e.key === "Enter" && !e.shiftKey) {
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

function tickClock() {
  clockEl.textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
tickClock();
setInterval(tickClock, 1000);

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
function updateEmptyState() {
  const meta = BAY_META[currentBay];
  emptyEyebrow.textContent = meta.eyebrow;
  emptyTitle.textContent = meta.title;
  emptySub.textContent = meta.sub;
  emptyHints.innerHTML = meta.hints;
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
// OpenAI's open-weight models, served free through Groq. gpt-oss-120b
// is roughly seventeen times the size of the 7B coder that fits on this
// machine's GPU, and measured at half a second for a complete function -
// so for code it is both better and faster than running locally, which
// is not the tradeoff local-vs-cloud usually offers.
const isOpenAIModel = (m) => /gpt-oss/i.test(m);

function preferredModel(models, bay) {
  if (!models.length) return null;
  const isCoder = (m) => /coder|code/i.test(m);
  if (bay === "code") {
    // Biggest OpenAI model first, then the smaller one, then whatever
    // coding-tuned local model exists. The fallback chain matters
    // because this same function runs for the local provider too, where
    // no gpt-oss model is present.
    return (
      models.find((m) => /gpt-oss-120b/i.test(m)) ||
      models.find(isOpenAIModel) ||
      models.find(isCoder) ||
      models[0]
    );
  }

  const isVision = (m) => /llava|vision|moondream|minicpm-v|bakllava/i.test(m);
  const isGeneral = (m) => !isCoder(m) && !isVision(m);

  // The speed-over-accuracy default above is a VRAM argument, and it
  // only applies locally: one multi-GB model fits at a time, so picking
  // the bigger one costs a ~39s reload. Nothing is loaded on a cloud
  // channel, every model answers equally fast, and the biggest one is
  // simply better - so prefer it there. Without this the list order
  // decided it, which meant chat opened on allam-2-7b, a model
  // specialised for Arabic, for everyone.
  if (models.some(isOpenAIModel)) {
    return (
      models.find((m) => /gpt-oss-120b/i.test(m)) ||
      models.find(isOpenAIModel) ||
      models[0]
    );
  }

  return (
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

// Which channel a bay should open on. Only the Code bay expresses a
// preference, and only when Groq actually has an OpenAI model to offer -
// otherwise this returns null and whatever channel the user was on is
// left alone, including when they picked it deliberately.
function preferredProviderFor(bay) {
  if (bay !== "code") return null;
  const groq = providers.find(
    (p) => p.id === "groq" && p.available && p.models.some(isOpenAIModel),
  );
  return groq ? "groq" : null;
}

function selectBay(bay) {
  if (!BAY_ORDER.includes(bay)) return;
  currentBay = bay;

  // Set before selectProvider runs: it applies the preferred model for
  // whatever currentBay currently is, so switching the channel first
  // would pick a model for the bay being left behind.
  const wanted = preferredProviderFor(bay);
  if (wanted && wanted !== activeProvider) selectProvider(wanted);

  const idx = BAY_ORDER.indexOf(bay);
  bayPuck.style.transform = `translateX(${idx * 100}%)`;
  bayButtons.forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.bay === bay ? "true" : "false"),
  );

  root.style.setProperty("--bay", `var(--bay-${bay})`);
  root.style.setProperty("--bay-soft", `var(--bay-${bay}-soft)`);

  messageInput.placeholder = BAY_META[bay].placeholder;
  chatModeControls.hidden = bay === "image";
  imageModeControls.hidden = bay !== "image";
  if (bay !== "image") applyPreferredModel(bay);

  currentThreadId = null;
  updateEmptyState();
  showEmptyState();
  loadThreadList();
  updateComposerHint();
}

bayButtons.forEach((btn) =>
  btn.addEventListener("click", () => selectBay(btn.dataset.bay)),
);

imageQualityToggle.addEventListener("click", () => {
  imageBackend = imageBackend === "local" ? "gemini" : "local";
  imageQualityToggle.setAttribute(
    "aria-checked",
    String(imageBackend === "gemini"),
  );
  imageModeNote.textContent =
    imageBackend === "gemini"
      ? "Gemini - higher quality, via Google's API, costs more credits."
      : "Local generation - free, runs on this machine.";
});

/* ----------------------------------------------------------------
   Channel (provider) picker
   ---------------------------------------------------------------- */
function updateComposerHint() {
  if (currentBay === "image") {
    composerHintText.textContent = geminiConfigured
      ? "Local or Gemini · pick a quality above"
      : "Local generation · free, runs on this machine";
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
  modelSelect.replaceChildren(
    ...p.models.map((m) => new Option(m, m)),
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
  const showPicker = providers.length > 1;
  channelRow.style.display = showPicker ? "" : "none";
  patchBayLabel.style.display = showPicker ? "" : "none";
  if (!showPicker) return;

  channelRow.innerHTML = "";
  providers.forEach((p) => {
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
    renderChannelRow();

    geminiConfigured = !!data.gemini_configured;
    imageQualityToggle.hidden = !geminiConfigured;

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
      visible += buf.slice(0, start);
      try {
        handleToolEvent(msgEl, JSON.parse(buf.slice(start + 1, end)));
      } catch (_) {
        /* a malformed event is not worth breaking the reply over */
      }
      buf = buf.slice(end + 1);
    }

    const cut = buf.indexOf(SEP);
    renderContent(bubble, visible + (cut === -1 ? buf : buf.slice(0, cut)));
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
    imageBackend === "gemini" ? "Gemini" : "Local",
  );
  const msgEl = bubble.parentElement;
  msgEl.classList.add("streaming");
  sendBtn.classList.add("is-streaming");
  sendBtn.title = "Generating…";
  sendBtn.disabled = true; // one-shot request, nothing to stream/abort

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
  plan: document.getElementById("planPanel"),
  memory: document.getElementById("memoryPanel"),
  appearance: document.getElementById("appearancePanel"),
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

/* ---------------------------------------------------------------- */
updateEmptyState();
boot();
