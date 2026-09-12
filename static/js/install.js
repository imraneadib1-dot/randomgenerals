import { toast } from "./toast.js";
/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountInstall() {
  /* ----------------------------------------------------------------
     Installing the app
     Two different worlds. Chrome, Edge and Android fire
     beforeinstallprompt and let the page ask. iOS Safari does not - there
     the only route is Share -> Add to Home Screen, so all the page can do
     is say so.
     ---------------------------------------------------------------- */
  (function setUpInstall() {
    const btn = document.getElementById("installBtn");
    if (!btn) return;
    const standalone =
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true;
    // Already installed - offering to install again would be nonsense.
    if (standalone) return;
    let deferred = null;
    window.addEventListener("beforeinstallprompt", (e) => {
      // Chrome would otherwise show its own mini-infobar; taking the event
      // lets the offer sit in the top bar where it reads as part of the app.
      e.preventDefault();
      deferred = e;
      btn.hidden = false;
    });
    const isIOS =
      /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const isSafari = /^((?!chrome|android|crios|fxios).)*safari/i.test(
      navigator.userAgent,
    );
    if (isIOS && isSafari) btn.hidden = false;
    btn.addEventListener("click", async () => {
      if (deferred) {
        deferred.prompt();
        const { outcome } = await deferred.userChoice;
        deferred = null;
        if (outcome === "accepted") btn.hidden = true;
        return;
      }
      if (isIOS) {
        toast(
          "To install:\n\n" +
            "1. Tap the Share button at the bottom of Safari\n" +
            "2. Scroll down and tap \u201cAdd to Home Screen\u201d\n\n" +
            "It will then open like an app, without the browser bars.",
        );
      }
    });
    window.addEventListener("appinstalled", () => {
      btn.hidden = true;
    });
  })();

  /* The worker is registered last and failures are swallowed: offline
     support is a bonus, and a browser that refuses it (private mode, an
     old iOS) must still get a working app. */
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

}
