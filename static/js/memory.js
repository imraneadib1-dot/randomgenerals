import { customInstructionsInput, instructionsSavedNote, memoryAddForm, memoryAddInput, memoryList, saveInstructionsBtn } from "./dom.js";

export async function loadMemoryPanel() {
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

export function renderMemoryList(memories) {
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

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountMemory() {
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

}
