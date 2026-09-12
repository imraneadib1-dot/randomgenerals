/* ----------------------------------------------------------------
   A confirmation you can style, on a <dialog>.

   window.confirm() blocks the whole tab, cannot be themed, and on a
   phone is a system sheet that says "localhost says". A <dialog>
   opened with showModal() gives the two things a hand-rolled modal
   has to fake: focus is trapped inside it, and Escape closes it. The
   promise resolves true only for the confirming button, so callers
   read like the confirm() they replaced:

       if (!(await confirmDialog({ title: "Delete?" }))) return;
   ---------------------------------------------------------------- */

let dialog = null;

function ensureDialog() {
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.className = "confirm";
  dialog.innerHTML =
    '<form method="dialog" class="confirm-form">' +
    '<h2 class="confirm-title" id="confirmTitle"></h2>' +
    '<p class="confirm-body" id="confirmBody"></p>' +
    '<div class="confirm-actions">' +
    '<button type="button" class="confirm-cancel" value="cancel"></button>' +
    '<button type="submit" class="confirm-ok" value="ok"></button>' +
    "</div></form>";
  dialog.setAttribute("aria-labelledby", "confirmTitle");
  dialog.setAttribute("aria-describedby", "confirmBody");
  document.body.appendChild(dialog);
  return dialog;
}

/**
 * @param {{title: string, body?: string, confirmLabel?: string,
 *          cancelLabel?: string, danger?: boolean}} opts
 * @returns {Promise<boolean>}
 */
export function confirmDialog(opts) {
  const d = ensureDialog();
  const title = d.querySelector(".confirm-title");
  const body = d.querySelector(".confirm-body");
  const ok = d.querySelector(".confirm-ok");
  const cancel = d.querySelector(".confirm-cancel");
  title.textContent = opts.title || "Are you sure?";
  body.textContent = opts.body || "";
  body.hidden = !opts.body;
  ok.textContent = opts.confirmLabel || "Confirm";
  cancel.textContent = opts.cancelLabel || "Cancel";
  ok.classList.toggle("is-danger", !!opts.danger);

  const opener = /** @type {HTMLElement|null} */ (document.activeElement);
  return new Promise((resolve) => {
    const finish = (value) => {
      d.removeEventListener("close", onClose);
      cancel.removeEventListener("click", onCancel);
      if (d.open) d.close();
      // Back to where the person was: a dialog that leaves focus on
      // <body> strands a keyboard user at the top of the page.
      if (opener && typeof opener.focus === "function") opener.focus();
      resolve(value);
    };
    const onClose = () => finish(d.returnValue === "ok");
    const onCancel = () => { d.returnValue = "cancel"; d.close(); };
    d.addEventListener("close", onClose);
    cancel.addEventListener("click", onCancel);
    d.returnValue = "";
    d.showModal();
    // Cancel is the safe default to land on; Enter still submits ok
    // only when it is the focused button.
    cancel.focus();
  });
}
