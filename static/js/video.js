import { state } from "./state.js";
import { renderGenExamples } from "./bays.js";
import { updateGenBayLabel } from "./boot.js";
import { genExamples, genForm, genLocked, genLockedText, genPrompt, genQuotaEl, genSeconds, genSecondsOut, genSub, genTitle, videoBay, videoStatusEl } from "./dom.js";

export function videoSay(msg, isError) {
  if (!videoStatusEl) return;
  videoStatusEl.hidden = !msg;
  videoStatusEl.textContent = msg || "";
  videoStatusEl.classList.toggle("is-error", !!isError);
}

export function renderQuota(q) {
  if (!genQuotaEl || !q) return;
  if (!q.allowed) { genQuotaEl.textContent = ""; return; }
  genQuotaEl.textContent = `${q.remaining} of ${q.limit} left this month`;
}

/* Locking the form rather than hiding the bay. Someone on Free should be
   able to see what the feature is and what it costs before deciding to
   pay for it - a bay that simply is not there sells nothing. */
export function applyVideoAccess(d) {
  const q = d.quota || {};

  // WHAT THIS BAY IS DECIDED FIRST.
  //
  // Diagrams have no provider behind them - the model writes Mermaid and
  // the browser draws it - so none of the "is a key configured" checks
  // below apply. Running them first was a real bug: with no media key
  // set the function returned early with the bay locked, and the diagram
  // mode it should have fallen back to was never reached.
  // DIAGRAM IS THE FLOOR, not the last resort of a broken chain.
  //
  // A media backend only wins if it can actually run. A configured Tripo
  // key with a zero balance is configured and useless, and treating that
  // as "3D mode" locked the whole bay behind a credits notice - with the
  // diagram bay, which needs no key and no balance, sitting right there
  // unreachable. Anything that cannot generate falls through to drawing.
  // "Usable" has to mean usable BY THIS PERSON, not merely installed.
  //
  // This checked the key and the balance and ignored the plan, so a
  // deployment with a video backend put the bay into video mode for
  // everybody - and then a free account hit "Video generation is a Pro
  // feature". A whole bay spent on an advert, with the diagram mode
  // that needs no key, no balance and no plan sitting right behind it
  // unreachable.
  //
  // quota.allowed already carries the answer: it is false for a plan
  // without video and for a guest who would need an account first. If
  // this person cannot generate a video, the bay draws diagrams, which
  // they can.
  const entitled = !d.quota || d.quota.allowed !== false;
  const mediaUsable =
    d.configured
    && entitled
    && (d.credits === undefined || d.credits === null || d.credits > 0);
  const kind =
    !mediaUsable ? "diagram"
      : d.kind === "model" ? "model"
      : "video";

  updateGenBayLabel(kind);

  if (kind === "diagram") {
    state.genKind = "diagram";
    genLocked.hidden = true;
    genForm.hidden = false;
    if (genTitle) genTitle.textContent = "Draw a diagram";
    if (genSub) {
      genSub.textContent =
        "Describe a system, a flow or a schema and see it drawn.";
    }
    if (genPrompt) {
      genPrompt.placeholder =
        "How an OAuth 2.0 authorization code flow works, "
        + "including the token exchange";
    }
    if (genQuotaEl) genQuotaEl.textContent = "";
    renderGenExamples("diagram");
    // None of the media controls mean anything for a diagram.
    ["genSeconds", "genRatio", "genQuality"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.closest(".gen-ctl")) el.closest(".gen-ctl").hidden = true;
    });
    return;
  }

  if (!d.configured) {
    genLocked.hidden = false;
    // The server says which key is missing; show that rather than a
    // generic line, since the person reading this is usually the one
    // who can fix it.
    genLockedText.textContent =
      d.detail || "Video generation isn't switched on for this server yet.";
    genForm.hidden = true;
    return;
  }
  // The free backend has no quality or aspect controls behind it, so
  // hide the ones that would do nothing rather than let someone set a
  // value that is quietly ignored.
  // A key with no credits behind it is worse than no key: the bay looks
  // ready and fails on the first click. Say it before anyone types.
  // Unreachable while diagrams are the floor above - kept because the
  // moment another media bay exists that has no fallback, an empty
  // balance has to say so rather than looking broken.
  if (d.kind === "model" && d.credits === 0) {
    genLocked.hidden = false;
    genLockedText.textContent =
      "3D generation is set up but the account has no credits left.";
    genForm.hidden = true;
    return;
  }
  state.genKind = d.kind === "model" ? "model"
    : d.kind === "video" && d.configured ? "video"
    : "diagram";
  updateGenBayLabel(state.genKind);
  if (genTitle) {
    genTitle.textContent =
      state.genKind === "model" ? "Make a 3D model"
      : state.genKind === "video" ? "Make a video"
      : "Draw a diagram";
  }
  if (genSub) {
    genSub.textContent =
      state.genKind === "model"
        ? "Describe an object and get back a 3D model you can spin."
        : state.genKind === "video"
        ? "Describe a shot and get it back as a clip, up to "
          + d.max_seconds + "s."
        : "Describe a system, a flow or a schema and see it drawn.";
  }
  if (genPrompt) {
    if (state.genKind === "model") {
      genPrompt.placeholder =
        "A weathered brass diving helmet with a cracked glass port";
    } else if (state.genKind === "diagram") {
      genPrompt.placeholder =
        "How an OAuth 2.0 authorization code flow works, "
        + "including the token exchange";
    }
  }
  // The diagram bay has no provider behind it, so nothing is locked and
  // none of the media controls apply.
  if (state.genKind === "diagram") {
    genLocked.hidden = true;
    genForm.hidden = false;
    if (genQuotaEl) genQuotaEl.textContent = "";
    ["genSeconds", "genRatio", "genQuality"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.closest(".gen-ctl")) el.closest(".gen-ctl").hidden = true;
    });
    return;
  }
  // A mesh has no duration, so the length slider means nothing here.
  const lengthCtl = document.getElementById("genSeconds");
  if (lengthCtl && lengthCtl.closest(".gen-ctl")) {
    lengthCtl.closest(".gen-ctl").hidden = state.genKind === "model";
  }
  const freeTier = !!d.free_tier;
  const shapeCtl = document.getElementById("genRatio");
  const qualityCtl = document.getElementById("genQuality");
  if (shapeCtl) shapeCtl.closest(".gen-ctl").hidden = freeTier;
  if (qualityCtl) qualityCtl.closest(".gen-ctl").hidden = freeTier;
  if (!q.allowed) {
    genLocked.hidden = false;
    genLockedText.textContent =
      "Video generation is a Pro feature - 10 clips a month.";
    genForm.hidden = true;
    return;
  }
  genLocked.hidden = true;
  genForm.hidden = false;
  renderQuota(q);
  if (genSeconds && d.max_seconds) {
    genSeconds.max = String(d.max_seconds);
    genSeconds.min = String(d.min_seconds || 1);
    genSeconds.value = String(d.default_seconds || 5);
    genSecondsOut.textContent = genSeconds.value + "s";
  }
  if (genSub) {
    genSub.textContent =
      `Describe a shot and get it back as a clip, up to ${d.max_seconds}s.`;
  }
}

export async function loadVideoLimits() {
  if (!videoBay) return;
  try {
    const r = await fetch("/api/video/status");
    applyVideoAccess(await r.json());
  } catch (_) {
    /* Leave the form as the markup has it. A status call failing is not
       a reason to take the feature away - the generate call will report
       anything genuinely wrong, with a real message. */
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountVideo() {
  if (genSeconds) {
    genSeconds.addEventListener("input", () => {
      genSecondsOut.textContent = genSeconds.value + "s";
    });
  }

  if (genExamples) {
    genExamples.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-ex]");
      if (!b) return;
      genPrompt.value = b.dataset.ex;
      genPrompt.focus();
    });
  }

}
