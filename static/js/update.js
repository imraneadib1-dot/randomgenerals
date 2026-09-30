/* ----------------------------------------------------------------
   Picking up a new build in a page that is already open.

   Every other fix for stale code works at load time: versioned asset
   URLs, a page that is never stored. None of it reaches a page that
   never loads again - a pinned tab, the installed app, the phone app
   resumed from the background - and those run the build they started
   with for as long as they stay open. That is days, for the people
   who use this most, and to them a shipped feature is one that never
   arrived.

   So the page asks which build the server is running (/api/build) and
   compares it with the one it was rendered with (<meta name="rg-build">).
   When they differ it reloads by itself only if nobody could lose
   anything by it - the tab is out of sight and nothing is in progress -
   and otherwise says so and leaves the moment to the person.
   ---------------------------------------------------------------- */
import { state } from "./state.js";
import { toast } from "./toast.js";
import { messageInput, genPrompt, sendBtn, settingsBackdrop } from "./dom.js";

// A request this small is cheap, but a tab left open all week should
// not be asking every few seconds.
const CHECK_EVERY_MS = 5 * 60 * 1000;
const MIN_GAP_MS = 60 * 1000;

let lastCheck = 0;
let offered = false;

function pageBuild() {
  const meta = document.querySelector('meta[name="rg-build"]');
  return (meta && meta.getAttribute("content")) || "";
}

/** True when a reload would cost the person something. */
function busy() {
  return Boolean(
    state.activeStreamController ||
    document.querySelector(".msg.streaming") ||
    sendBtn.disabled ||                 // an image is being made
    state.uploadingCount > 0 ||
    state.pendingAttachments.length ||
    messageInput.value.trim() ||
    (genPrompt && genPrompt.value.trim()) ||
    !settingsBackdrop.hidden ||
    document.querySelector("dialog[open]") ||
    // A reload lands in the chat bay: the thread is remembered per
    // bay, the bay itself is not, so from anywhere else it would move
    // the person somewhere they did not go.
    state.currentBay !== "chat",
  );
}

function offer() {
  if (offered) return;
  offered = true;
  toast("RandomGenerals has been updated.", {
    id: "update",
    timeout: 0,
    action: { label: "Reload", run: () => location.reload() },
  });
}

async function check() {
  const now = Date.now();
  if (now - lastCheck < MIN_GAP_MS) return;
  lastCheck = now;
  const mine = pageBuild();
  if (!mine || mine === "unknown") return;
  let theirs = "";
  try {
    const res = await fetch("/api/build", { cache: "no-store" });
    if (!res.ok) return;
    theirs = (await res.json()).build || "";
  } catch (_) {
    return;                             // offline, or mid-restart
  }
  if (!theirs || theirs === "unknown" || theirs === mine) return;
  if (document.visibilityState === "hidden" && !busy()) {
    location.reload();
  } else {
    offer();
  }
}

export function mountUpdate() {
  document.addEventListener("visibilitychange", check);
  // Restored from the back-forward cache: the page is as old as it was
  // when it was left, whatever has been deployed since.
  window.addEventListener("pageshow", (e) => { if (e.persisted) check(); });
  window.addEventListener("online", check);
  window.setInterval(check, CHECK_EVERY_MS);
}
