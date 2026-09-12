import { state } from "./state.js";
import { consumeStream, finishReply, markReplyKind, showThinking } from "./chat.js";
import { loadCredits, renderCredits } from "./credits.js";
import { modelSelect, sendBtn, topbarModelChip } from "./dom.js";
import { addMessage, addMessageActions, renderContent } from "./message.js";
import { loadThreadList } from "./sidebar.js";

/* ----------------------------------------------------------------
   Sending — Image bay
   ---------------------------------------------------------------- */
export async function sendImagePrompt(text) {
  addMessage("user", text);
  const bubble = addMessage(
    "assistant",
    "",
    "imagegen",
    state.imageBackend === "flux" ? "FLUX" : "Local",
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
        thread_id: state.currentThreadId,
        prompt: text,
        backend: state.imageBackend,
        size: state.imageSize,
        style: state.imageStyle,
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

export async function regenerateLast(oldMsgEl) {
  if (state.activeStreamController || !state.currentThreadId) return;
  const model = modelSelect.value;

  oldMsgEl.remove();
  const bubble = addMessage("assistant", "", state.activeProvider, model);
  const msgEl = bubble.parentElement;
  msgEl.classList.add("streaming");
  sendBtn.classList.add("is-streaming");
  sendBtn.title = "Stop generating";
  topbarModelChip.textContent = model;
  showThinking(bubble, "Thinking");

  const controller = new AbortController();
  state.activeStreamController = controller;

  try {
    const res = await fetch(`/api/threads/${state.currentThreadId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: state.activeProvider,
        model,
        strength: state.currentStrength,
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
    msgEl.classList.remove("streaming");
    loadThreadList();
    loadCredits();
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountImage() {
  // nothing ran at load in this section
}
