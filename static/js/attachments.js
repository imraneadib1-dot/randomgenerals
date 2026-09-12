import { state } from "./state.js";
import { attachChips, attachFileBtn, attachFolderBtn, fileInput, folderInput } from "./dom.js";

export function renderAttachChips() {
  attachChips.innerHTML = "";
  state.pendingAttachments.forEach((att, i) => {
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
      state.pendingAttachments.splice(i, 1);
      renderAttachChips();
    });
    chip.appendChild(label);
    chip.appendChild(remove);
    attachChips.appendChild(chip);
  });
  for (let i = 0; i < state.uploadingCount; i++) {
    const chip = document.createElement("div");
    chip.className = "attach-chip uploading";
    chip.textContent = "uploading…";
    attachChips.appendChild(chip);
  }
}

export async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  state.uploadingCount = files.length;
  renderAttachChips();
  try {
    const formData = new FormData();
    files.forEach((f) => formData.append("files", f));
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    state.pendingAttachments.push(...(data.attachments || []));
  } catch (err) {
    /* upload failed - nothing gets added, chips just clear below */
  } finally {
    state.uploadingCount = 0;
    renderAttachChips();
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountAttachments() {
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

}
