import { messageInput, micBtn } from "./dom.js";

/* ----------------------------------------------------------------
   Voice input - browser-native speech-to-text (Chrome, Edge, Safari).
   Transcribes into the message box rather than auto-sending, so a
   misheard word can still be fixed before it goes out. Runs entirely
   in the browser - no server round-trip, no credits, no added latency.
   Firefox has no SpeechRecognition implementation, so the mic button
   just stays hidden there instead of showing something that fails
   silently.
   ---------------------------------------------------------------- */
export const SpeechRecognitionAPI =
  window.SpeechRecognition || window.webkitSpeechRecognition;

// Auto web search - no toggle to think about. If the message looks
// time-sensitive or current-events-shaped, search first; otherwise just
// answer from the model directly. A heuristic, not a real classifier -
// it'll miss things and occasionally search when it didn't need to, but
// that's the same tradeoff a manual toggle has (except nobody has to
// remember to flip it).
export const SEARCH_TRIGGER_RE = new RegExp(
  "\\b(" +
    [
      "today",
      "tonight",
      "right now",
      "currently",
      "this (week|month|year)",
      "latest",
      "newest",
      "recently",
      "up[- ]to[- ]date",
      "breaking",
      "news",
      "headline",
      "weather",
      "forecast",
      "score",
      "stock price",
      "exchange rate",
      "who (is|are) the current",
      "what(?:'s| is) the (current|latest)",
      "release date",
      "when (is|does|will)",
      "how much (does|is)",
      "price of",
      "20(2[4-9]|3\\d)",
    ].join("|") +
    ")\\b",
  "i",
);

export function shouldAutoSearch(text) {
  return SEARCH_TRIGGER_RE.test(text);
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountVoice() {
  if (SpeechRecognitionAPI) {
    micBtn.hidden = false;
    const recognizer = new SpeechRecognitionAPI();
    recognizer.continuous = false;
    recognizer.interimResults = true;
    recognizer.lang = navigator.language || "en-US";
    let isListening = false;
    let baseText = "";
    recognizer.onstart = () => {
      isListening = true;
      // Keep whatever was already typed and append to it, rather than
      // clobbering a partially-typed message.
      baseText = messageInput.value && !messageInput.value.endsWith(" ")
        ? messageInput.value + " "
        : messageInput.value;
      micBtn.classList.add("mic-listening");
    };
    recognizer.onresult = (e) => {
      let transcript = "";
      for (let i = 0; i < e.results.length; i++) {
        transcript += e.results[i][0].transcript;
      }
      messageInput.value = baseText + transcript;
      messageInput.dispatchEvent(new Event("input"));
    };
    const stopListeningUI = () => {
      isListening = false;
      micBtn.classList.remove("mic-listening");
    };
    recognizer.onerror = stopListeningUI;
    recognizer.onend = () => {
      stopListeningUI();
      messageInput.focus();
    };
    micBtn.addEventListener("click", () => {
      if (isListening) {
        recognizer.stop();
        return;
      }
      try {
        recognizer.start();
      } catch (e) {
        // start() throws if a session is already active - the existing
        // session's onend handles cleanup, nothing to do here.
      }
    });
  }

}
