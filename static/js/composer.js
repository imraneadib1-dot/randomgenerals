import { readPref } from "./appearance.js";
import { chatForm, messageInput, mobileToggle, sidebar } from "./dom.js";

/* ----------------------------------------------------------------
   Composer input behaviour
   ---------------------------------------------------------------- */
export function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountComposer() {
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

}
