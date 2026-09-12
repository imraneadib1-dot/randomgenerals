import { state } from "./state.js";
import { del as deleteJSON, explain, getJSON } from "./api.js";
import { confirmDialog } from "./confirm.js";
import { toast } from "./toast.js";
import { readPref, writePref } from "./appearance.js";
import { showEmptyState } from "./bays.js";
import { markReplyKind, renderToolDisplay, replyKindOf } from "./chat.js";
import { appShell, chatLog, clearThreadsBtn, deleteBtn, emptyState, newChatBtn, sidebar, sidebarCollapseBtn, threadList, topbarTitle } from "./dom.js";
import { addMessage, addMessageActions, renderContent, renderMsgAttachments, renderSourceChips } from "./message.js";
import { relativeTime } from "./strength.js";

/* ----------------------------------------------------------------
   Thread list — scoped to the active bay
   ---------------------------------------------------------------- */
/**
 * Delete every conversation, in every bay.
 *
 * One function for both buttons - the sidebar and Settings > Data - so
 * the two cannot drift into deleting different things. It goes through
 * the per-thread endpoint that already checks ownership rather than a
 * bulk route, which would need its own authorisation logic to get
 * wrong.
 *
 * `/api/threads` with no mode returns every bay, so this really does
 * clear chat, code and image history and not just whichever tab
 * happens to be open.
 */
export async function deleteAllConversations(say) {
  const report = say || (() => {});
  report("Deleting…");
  const list = await (await fetch("/api/threads")).json();
  let n = 0;
  for (const t of list.threads || []) {
    const r = await fetch("/api/threads/" + t.id, { method: "DELETE" });
    if (r.ok) n += 1;
  }
  report(`Deleted ${n} conversation${n === 1 ? "" : "s"}.`);
  state.currentThreadId = null;
  await loadThreadList();
  showEmptyState();
  return n;
}

export async function loadThreadList() {
  let data;
  try {
    data = await getJSON(`/api/threads?mode=${state.currentBay}`);
  } catch (err) {
    // Fired from eight places without an await, so a failure here was
    // an unhandled rejection and a list that silently stopped updating.
    toast(explain(err, "Could not load your conversations."),
          { kind: "error", id: "threads" });
    return;
  }
  threadList.innerHTML = "";

  // Nothing to clear, nothing to offer. A destructive button that does
  // nothing is still a button people have to think about.
  const clearBtn = document.getElementById("clearThreads");
  if (clearBtn) clearBtn.hidden = !data.threads.length;

  data.threads.forEach((t) => {
    const item = document.createElement("div");
    item.className =
      "thread-item" + (t.id === state.currentThreadId ? " active" : "");

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
      // A confirmation, because there is no undo. "Clear all" always
      // asked; deleting one did not, and the button sat a few pixels
      // from the row it belonged to.
      const ok = await confirmDialog({
        title: "Delete this conversation?",
        body: t.title ? `"${t.title}" will be gone for good.` : "It will be gone for good.",
        confirmLabel: "Delete", danger: true,
      });
      if (!ok) return;
      try {
        await deleteJSON(`/api/threads/${t.id}`);
      } catch (err) {
        toast(explain(err, "Could not delete it."), { kind: "error" });
        return;
      }
      if (t.id === state.currentThreadId) {
        state.currentThreadId = null;
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

export async function openThread(tid) {
  state.currentThreadId = tid;
  sidebar.classList.remove("open");
  let thread;
  try {
    thread = await getJSON(`/api/threads/${tid}`);
  } catch (err) {
    state.currentThreadId = null;
    showEmptyState();
    toast(explain(err, "Could not open that conversation."), { kind: "error" });
    return;
  }

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
      // A question the filter blocked is kept so the person can see
      // what they asked above the refusal - marked, so it can be
      // styled as such, and never sent to the model (see
      // _model_history in app.py).
      markReplyKind(bubble.parentElement, m.kind);
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
      markReplyKind(bubble.parentElement, m.kind);
      renderContent(bubble, m.content);
      renderSourceChips(bubble.parentElement, m.sources);
      (m.tool_displays || []).forEach((d) =>
        renderToolDisplay(bubble.parentElement, d),
      );
      if (replyKindOf(bubble.parentElement) === "text") {
        addMessageActions(bubble.parentElement, bubble, {
          allowRegenerate: i === lastAssistantIdx,
        });
      }
    }
  });

  loadThreadList();
}

export const SIDEBAR_KEY = "sidebarCollapsed";

export function applySidebarCollapsed(collapsed) {
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

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountSidebar() {
  newChatBtn.addEventListener("click", () => {
    state.currentThreadId = null;
    showEmptyState();
    loadThreadList();
  });

  if (clearThreadsBtn) {
    clearThreadsBtn.addEventListener("click", async () => {
      // The count is in the question. "Delete every conversation?" reads
      // the same whether it is going to remove one or forty.
      const list = await (await fetch("/api/threads")).json();
      const n = (list.threads || []).length;
      if (!n) return;
      const sure = await confirmDialog({
        title: `Delete ${n} conversation${n === 1 ? "" : "s"}?`,
        body: "In every channel. This cannot be undone.",
        confirmLabel: "Delete all", danger: true,
      });
      if (!sure) return;
      clearThreadsBtn.disabled = true;
      try {
        await deleteAllConversations();
      } catch (_) {
        // Nothing to say here that the empty list will not say better.
      } finally {
        clearThreadsBtn.disabled = false;
      }
    });
  }

  deleteBtn.addEventListener("click", async () => {
    if (!state.currentThreadId) return;
    await fetch(`/api/threads/${state.currentThreadId}`, { method: "DELETE" });
    state.currentThreadId = null;
    showEmptyState();
    loadThreadList();
  });

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

}
