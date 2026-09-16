import { chatLog, emptyState } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { regenerateLast } from "./image.js";
import { downloadBlob, downloadHtml, isPreviewable, noteFilename, openPreview } from "./preview.js";
import { PROVIDER_META } from "./shell.js";
import { state } from "./state.js";
import { postJSON } from "./api.js";

/* ----------------------------------------------------------------
   Message rendering
   ---------------------------------------------------------------- */
export function addMessage(role, text, provider, model) {
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
export function addMessageActions(msg, bubble, { allowRegenerate }) {
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

  // Notes are the point of a tutor answer, and a note you cannot keep
  // is a note you have to screenshot. Saves the markdown source, so the
  // headings, the tables and the LaTeX survive into the file.
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "msg-action-btn";
  saveBtn.textContent = "Save";
  saveBtn.title = "Download this reply as a Markdown file";
  saveBtn.addEventListener("click", () => {
    const md = bubble.dataset.md || bubble.textContent || "";
    if (!md.trim()) return;
    downloadBlob(md, noteFilename(md), "text/markdown;charset=utf-8");
    saveBtn.textContent = "Saved!";
    setTimeout(() => (saveBtn.textContent = "Save"), 1500);
  });
  row.appendChild(saveBtn);

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

  // A thumb either way. Two buttons and no dialog: the moment a reply
  // is judged is the moment it is read, and a form would lose it.
  // The reply's place in the thread is counted from the log, which is
  // rendered in thread order; the server checks that it is a reply.
  const vote = document.createElement("span");
  vote.className = "msg-vote";
  vote.setAttribute("role", "group");
  vote.setAttribute("aria-label", "Rate this reply");
  /** @type {[string, number, string][]} */
  const choices = [["👍", 1, "Good answer"], ["👎", -1, "Wrong or unhelpful"]];
  choices.forEach(([glyph, value, title]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "msg-action-btn msg-vote-btn";
    b.textContent = glyph;
    b.title = title;
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", async () => {
      const index = Array.from(chatLog.querySelectorAll(".msg")).indexOf(msg);
      const was = b.getAttribute("aria-pressed") === "true";
      const next = was ? 0 : value;
      try {
        await postJSON("/api/feedback", { thread_id: state.currentThreadId, index, vote: next });
      } catch (_) {
        return;                    // a thumb is not worth an error toast
      }
      vote.querySelectorAll(".msg-vote-btn").forEach((x) => x.setAttribute("aria-pressed", "false"));
      if (!was) b.setAttribute("aria-pressed", "true");
    });
    vote.appendChild(b);
  });
  row.appendChild(vote);

  msg.appendChild(row);
}

export function renderSourceChips(container, sources) {
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

export function renderMsgAttachments(container, atts) {
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

export function escapeHtml(str) {
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

/**
 * Draw a finished reply. The markdown renderer does the work; this is
 * the name every caller already used, kept so a reload (openThread)
 * and a stream's last frame render the same way.
 */
export function renderContent(bubble, text) {
  renderMarkdown(bubble, text, { final: true });
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountMessage() {
  // nothing ran at load in this section
}
