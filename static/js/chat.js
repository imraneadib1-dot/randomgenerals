import { state } from "./state.js";
import { renderAttachChips } from "./attachments.js";
import { loadCredits, renderCredits } from "./credits.js";
import { chatForm, chatLog, messageInput, modelSelect, sendBtn, topbarModelChip } from "./dom.js";
import { sendImagePrompt } from "./image.js";
import { StreamRenderer } from "./markdown.js";
import { addMessage, addMessageActions, renderContent, renderMsgAttachments, renderSourceChips } from "./message.js";
import { loadThreadList } from "./sidebar.js";
import { shouldAutoSearch } from "./voice.js";

/* ----------------------------------------------------------------
   Creating a thread in the current bay
   ---------------------------------------------------------------- */
export async function ensureThread() {
  if (state.currentThreadId) return state.currentThreadId;
  const res = await fetch("/api/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: state.currentBay }),
  });
  state.currentThreadId = (await res.json()).id;
  return state.currentThreadId;
}

// Human-readable labels for the tools the model can call. Anything not
// listed still shows, just under its raw name.
export const TOOL_LABELS = {
  web_search: ["Searching the web", "Searched the web"],
  run_python: ["Running code", "Ran code"],
  generate_image: ["Creating an image", "Created an image"],
};

export function toolStatusStrip(msgEl) {
  let strip = msgEl.querySelector(".tool-activity");
  if (!strip) {
    strip = document.createElement("div");
    strip.className = "tool-activity";
    msgEl.insertBefore(strip, msgEl.firstChild);
  }
  return strip;
}

export function handleToolEvent(msgEl, evt) {
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
  renderToolDisplay(msgEl, evt.display);
}

/* What a tool produced, drawn under the reply: the sources a search
   found, the picture that was generated, the output of code that ran.
   Called live from the stream and again from openThread(), which
   re-draws whatever the server kept on the message - so a reload shows
   the same picture the stream did rather than a reply describing an
   image that is no longer there. */
export function renderToolDisplay(msgEl, d) {
  if (!d || !d.kind) return;
  if (d.kind === "sources") {
    renderSourceChips(msgEl, d.sources);
  } else if (d.kind === "image" && d.url) {
    const img = document.createElement("img");
    img.className = "tool-image";
    img.src = d.url;
    img.alt = d.prompt || "Generated image";
    img.loading = "lazy";
    msgEl.appendChild(img);
  } else if (d.kind === "code") {
    const box = document.createElement("div");
    box.className = "tool-code";
    const out = (d.stdout || "") + (d.stderr ? "\n" + d.stderr : "");
    const pre = document.createElement("pre");
    pre.className = "tool-code-output";
    pre.textContent =
      (out.trim() || "(ran, printed nothing)") +
      (d.timed_out ? "\n[killed: took too long]" : "");
    box.appendChild(pre);
    msgEl.appendChild(box);
  }
}

/* Everything that arrives between U+001E separators lands here. Three
   shapes share the channel (see stream_event() in app.py): tool
   progress, "this reply is an error/refusal, not an answer", and "the
   fast channel is busy, retrying in N seconds". */
export function handleStreamEvent(msgEl, bubble, evt) {
  if (evt.event === "reply") {
    // Recorded on the element so the code that runs after the stream
    // can withhold Copy / Save / Regenerate from a sentence the server
    // wrote in place of an answer, and reloading shows it the same way.
    markReplyKind(msgEl, evt.kind);
    return;
  }
  if (evt.event === "wait") {
    if (bubble.classList.contains("is-thinking")) {
      const label = bubble.querySelector(".thinking-label");
      if (label) {
        label.textContent = `Fast channel busy — retrying in ${evt.seconds}s`;
      }
    }
    return;
  }
  if (evt.tool) handleToolEvent(msgEl, evt);
}

export function markReplyKind(msgEl, kind) {
  if (!kind || kind === "text") return;
  msgEl.dataset.kind = kind;
  msgEl.classList.add("is-" + kind);
}

export function replyKindOf(msgEl) {
  return msgEl.dataset.kind || "text";
}

/* What happens once a reply has fully arrived: either the actions row,
   or nothing - an error and a refusal are not things to copy, save or
   regenerate. Shared by send, regenerate and the lost-thread retry so
   the three cannot disagree about it. */
export function finishReply(msgEl, bubble, fullText, { allowRegenerate }) {
  if (!fullText) {
    renderContent(bubble, "[No response received. Check the channel setup.]");
    return;
  }
  if (replyKindOf(msgEl) !== "text") return;
  addMessageActions(msgEl, bubble, { allowRegenerate });
}

/* The waiting state. Built as real elements rather than a CSS pseudo so
   the three dots can carry independent animation delays, and so the
   label can say what is being waited for. Removed by clearThinking() the
   moment the first character of the reply arrives. */
export function showThinking(bubble, label) {
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

export function clearThinking(bubble) {
  if (!bubble || !bubble.classList.contains("is-thinking")) return;
  bubble.classList.remove("is-thinking");
  bubble.replaceChildren();
}

export async function consumeStream(res, bubble) {
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
  // One render per frame, not one per chunk - see StreamRenderer. The
  // old loop re-rendered the whole bubble, re-highlighted every code
  // block and re-ran KaTeX on every network chunk, so a long reply cost
  // more to draw with every token it gained.
  const renderer = new StreamRenderer(bubble);
  const scroller = chatLog.parentElement;
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
      let evt = null;
      try {
        evt = JSON.parse(buf.slice(start + 1, end));
      } catch (_) {
        /* a malformed event is not worth breaking the reply over */
      }
      // A "wait" event is the one kind that should leave the thinking
      // indicator up - it is what the indicator is now reporting on.
      if (evt && evt.event !== "wait") clearThinking(bubble);
      if (evt) handleStreamEvent(msgEl, bubble, evt);
      buf = buf.slice(end + 1);
    }

    const cut = buf.indexOf(SEP);
    const sofar = visible + (cut === -1 ? buf : buf.slice(0, cut));
    // Only once there is something to show. renderContent() would blow
    // the indicator away by itself, but the is-thinking CLASS would
    // survive - leaving the shimmer running underneath the reply and the
    // caret suppressed for the rest of the stream.
    if (sofar) clearThinking(bubble);
    // Follow the reply only if the reader was already at the bottom.
    // Forcing the scroll on every chunk yanked the page away from
    // anyone who had scrolled up to re-read something.
    const nearBottom =
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    renderer.update(sofar);
    if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  }
  const cut = buf.indexOf(SEP);
  visible += cut === -1 ? buf : buf.slice(0, cut);
  renderer.finish(visible);
  return visible;
}

export async function sendChatMessage(text) {
  const model = modelSelect.value;
  // Deep mode always searches first; Quick still searches for messages
  // that obviously need current info, so switching to Quick isn't a
  // privacy/accuracy cliff, just less automatic about it.
  const useWebSearch = state.currentStrength === "deep" || shouldAutoSearch(text);
  const files = state.pendingAttachments;
  state.pendingAttachments = [];
  renderAttachChips();

  const userBubble = addMessage("user", text);
  renderMsgAttachments(userBubble.parentElement, files);
  const bubble = addMessage("assistant", "", state.activeProvider, model);
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
  state.activeStreamController = controller;

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
        thread_id: state.currentThreadId,
        provider: state.activeProvider,
        model,
        message: text,
        web_results: webResults,
        attachments: files,
        strength: state.currentStrength,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));

      // "Unknown thread" means the server no longer recognises this
      // conversation as ours - almost always a dropped session cookie
      // rather than a real fault. In-app browsers (the one Instagram
      // opens for a link in a bio) tear their storage down between
      // navigations, so the thread was created as one guest and the
      // message arrived as another.
      //
      // The cookie is persistent now, which should stop this at source.
      // This is the second line of defence: rather than show a stranger
      // an error they can do nothing about on their first ever message,
      // start a fresh thread and send it again. Once only - a second
      // failure is something else, and hiding it would be worse.
      if (
        res.status === 400 &&
        /unknown thread/i.test(data.error || "") &&
        !state.retriedAfterLostThread
      ) {
        state.retriedAfterLostThread = true;
        state.currentThreadId = null;
        await ensureThread();
        const retry = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: state.currentThreadId,
            provider: state.activeProvider,
            model,
            message: text,
            web_results: webResults,
            attachments: files,
            strength: state.currentStrength,
          }),
          signal: controller.signal,
        });
        if (retry.ok) {
          const retried = await consumeStream(retry, bubble);
          finishReply(msgEl, bubble, retried, { allowRegenerate: true });
          return;
        }
      }

      renderContent(bubble, data.error || "Something went wrong.");
      if (data.credits) renderCredits(data.credits);
      return;
    }

    const fullText = await consumeStream(res, bubble);
    finishReply(msgEl, bubble, fullText, { allowRegenerate: true });
  } catch (err) {
    if (err.name === "AbortError") {
      if (!bubble.textContent) renderContent(bubble, "[Stopped]");
      else addMessageActions(msgEl, bubble, { allowRegenerate: true });
    } else {
      markReplyKind(msgEl, "error");
      renderContent(bubble, "Something went wrong reaching the server.");
    }
  } finally {
    state.activeStreamController = null;
    sendBtn.classList.remove("is-streaming");
    sendBtn.title = "Send";
    bubble.parentElement.classList.remove("streaming");
    sendBtn.disabled = false;
    loadThreadList();
    loadCredits();
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountChat() {
  sendBtn.addEventListener("click", (e) => {
    if (state.activeStreamController) {
      e.preventDefault();
      state.activeStreamController.abort();
    }
  });

  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (state.activeStreamController) return; // send button is in stop mode right now
    const text = messageInput.value.trim();
    if (!text || !state.activeProvider) return;
    await ensureThread();
    messageInput.value = "";
    messageInput.style.height = "auto";
    if (state.currentBay === "image") await sendImagePrompt(text);
    else await sendChatMessage(text);
  });

}
