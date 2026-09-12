import { state } from "./state.js";
import { imageSizeSelect, imageStyleSelect } from "./dom.js";

export const BAY_ORDER = ["chat", "code", "image", "video"];

export const BAY_META = {
  code: {
    eyebrow: "randomgenerals --ready",
    title: "Ask for code",
    // Deliberately says nothing about where it runs. The Code bay now
    // opens on the cloud channel when a stronger model is available
    // there, so a fixed "no cloud call" line was simply untrue - and
    // the composer hint under the input already names the live
    // channel, which is the honest place for it.
    sub: "Ask for code or debug an error — you get working code back, not a lecture.",
    placeholder: "Ask for code, debug an error…",
    hints:
      "<div><span>Enter</span> to send · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎</span> switch bays above</div>",
  },
  chat: {
    eyebrow: "randomgenerals --chat",
    title: "What's on your mind?",
    sub: "Ask anything. RandomGenerals keeps it conversational — no forced structure.",
    placeholder: "Ask me anything…",
    hints:
      "<div><span>Enter</span> to send · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎</span> switch bays above</div>",
  },
  image: {
    eyebrow: "randomgenerals --image",
    title: "Describe an image",
    sub: "Describe what you want to see — your words get expanded into a full prompt first, so a few words still make a real picture.",
    placeholder: "A red apple on a wooden table…",
    hints:
      "<div><span>Enter</span> to generate · <span>Shift+Enter</span> for a new line</div>" +
      "<div><span>&lt;/&gt; ◎ ✺</span> switch bays above</div>",
  },
  video: {
    eyebrow: "randomgenerals --video",
    title: "Trim a video",
    // Says what it does and, just as importantly, what it does not. A bay
    // called Video that turns out to only trim is worse than one that
    // said so before the upload.
    sub: "Drop a clip, choose the part you want, and get an MP4 back. Trimming only for now.",
    placeholder: "",
    hints:
      "<div><span>Drop a file</span> or click to choose one</div>" +
      "<div><span>&lt;/&gt; ◎ ✺ ▶</span> switch bays above</div>",
  },
};

// Labels come from the server now - it is the only side that knows
// whether the local channel is on this hardware or on Ollama Cloud, and
// the label has to follow that. Kept as a fallback for a provider the
// server names but does not label.
export const PROVIDER_META = {
  ollama: { label: "RandomGenerals" },
  groq: { label: "RandomGenerals Turbo" },
  imagegen: { label: "Image" },
};

export const root = document.documentElement;

// Time-of-day sky: drifting clouds in the morning, a glowing moon at
// night, plain sky the rest of the day - purely decorative, driven by
// the visitor's own local clock (not the server's), rechecked
// periodically so it comes back if a tab is left open across a boundary.
export function updateTimeOfDay() {
  // A saved Appearance override wins over the clock. Read defensively -
  // this runs at boot, before initAppearance(), and localStorage can
  // throw outright in a private window.
  let override = null;
  try {
    override = localStorage.getItem("skyTheme");
  } catch (e) {
    /* fall through to the clock */
  }
  const hour = new Date().getHours();
  const phase =
    override && override !== "auto"
      ? override
      : hour >= 6 && hour < 12
        ? "morning"
        : hour >= 20 || hour < 6
          ? "night"
          : "day";
  root.classList.remove("time-morning", "time-night", "time-day");
  root.classList.add(`time-${phase}`);
}

// The puck is one bay wide, and CSS cannot count its siblings. Set the
// count once here so adding a bay to BAY_ORDER is the only change
// needed - the sizing follows.

export const bayButtons = [...document.querySelectorAll(".bay-btn")];

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountShell() {
  updateTimeOfDay();

  setInterval(updateTimeOfDay, 5 * 60 * 1000);

  if (imageSizeSelect) {
    imageSizeSelect.addEventListener("change", () => {
      state.imageSize = imageSizeSelect.value;
    });
  }

  if (imageStyleSelect) {
    imageStyleSelect.addEventListener("change", () => {
      state.imageStyle = imageStyleSelect.value;
    });
  }

}
