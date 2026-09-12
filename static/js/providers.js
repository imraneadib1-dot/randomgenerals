import { state } from "./state.js";
import { applyPreferredModel, preferredProviderFor } from "./bays.js";
import { channelNote, channelRow, composerHintText, imageQualityToggle, modelSelect, patchBayLabel, statusDot, statusText } from "./dom.js";
import { PROVIDER_META } from "./shell.js";

/* ----------------------------------------------------------------
   Channel (provider) picker
   ---------------------------------------------------------------- */
export function updateComposerHint() {
  if (state.currentBay === "image") {
    composerHintText.textContent = state.localImageAvailable
      ? "Hosted or on-device · pick a quality above"
      : "Image generation · free, no account needed";
    return;
  }
  const p = state.providers.find((x) => x.id === state.activeProvider);
  if (!p) {
    composerHintText.textContent = "no channel available — check your setup";
    return;
  }
  composerHintText.textContent = p.available
    ? `${p.label} · responses stream in real time`
    : p.note || `${p.label} unavailable`;
}

export function selectProvider(id) {
  const p = state.providers.find((x) => x.id === id);
  if (!p || !p.available) return;
  state.activeProvider = id;

  [...channelRow.children].forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.provider === id),
  );

  // Built with DOM APIs rather than an innerHTML template: model names
  // come from Ollama's /api/tags, and interpolating them into
  // `value="${m}"` unescaped lets a name containing a double quote break
  // out of the attribute. new Option() assigns them as data, so there is
  // no markup context to escape from in the first place.
  // Locked models stay VISIBLE and disabled rather than being hidden.
  // Hiding them would make Pro invisible to the people it is sold to;
  // disabling them makes the ceiling legible without letting anyone walk
  // into it. The label carries the reason, since a greyed row with no
  // explanation reads as a bug.
  const info = new Map((p.model_info || []).map((m) => [m.id, m]));
  modelSelect.replaceChildren(
    ...p.models.map((m) => {
      const meta = info.get(m);
      // The friendly name, not the routing string. "Max" says more to
      // the person choosing than "openai/gpt-oss-120b" does, and the
      // full id is still on the option's title for anyone who wants it.
      let label = (meta && meta.name) || m;
      if (meta && meta.locked) label += " — Pro";
      const opt = new Option(label, m);
      opt.title = (meta && meta.blurb) ? `${m} — ${meta.blurb}` : m;
      if (meta && meta.locked) opt.disabled = true;
      return opt;
    }),
  );
  modelSelect.disabled = p.models.length === 0;
  channelNote.textContent = p.models.length
    ? ""
    : "no models found for this channel";
  applyPreferredModel(state.currentBay);
  updateComposerHint();
}

export function renderChannelRow() {
  // One AI, one provider - a picker with a single button to click isn't a
  // choice, it's just an extra step. Auto-select it and hide the row
  // entirely; the model dropdown below still shows which model answers.
  //
  // The server already drops channels that cannot answer while any other
  // one can (see /api/providers), so a single entry here usually means
  // this deployment has exactly one working provider rather than that
  // the others are broken.
  // A provider marked hidden is plumbing, not a choice: the local
  // channel stays configured so it can answer an attached image and
  // catch a rate-limited request, but nobody should be picking it from
  // a menu. See ollama_provider() in app.py for why it survives at all.
  const shown = state.providers.filter((p) => !p.hidden);
  const showPicker = shown.length > 1;
  channelRow.style.display = showPicker ? "" : "none";
  patchBayLabel.style.display = showPicker ? "" : "none";
  if (!showPicker) return;

  channelRow.innerHTML = "";
  shown.forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "channel-btn" + (p.available ? " online" : "");
    btn.dataset.provider = p.id;
    btn.disabled = !p.available;
    btn.title = p.available ? p.label : p.note || `${p.label} unavailable`;
    btn.style.setProperty("--btn-color", `var(--c-${p.id})`);
    btn.style.setProperty("--btn-soft", `var(--c-${p.id}-soft)`);
    // Same reasoning as the model list above - the label is server-
    // supplied, so it goes in as text rather than as markup.
    const dot = document.createElement("span");
    dot.className = "dot";
    const label = document.createElement("span");
    label.textContent = PROVIDER_META[p.id]?.label || p.label;
    btn.replaceChildren(dot, label);
    btn.addEventListener("click", () => selectProvider(p.id));
    channelRow.appendChild(btn);
  });
}

export async function loadProviders() {
  try {
    const res = await fetch("/api/providers");
    const data = await res.json();
    state.providers = data.providers || [];
    state.recommended = data.recommended || {};
    renderChannelRow();

    state.localImageAvailable = !!data.local_image;
    imageQualityToggle.hidden = !state.localImageAvailable;

    const firstAvailable = state.providers.find((p) => p.available);
    statusDot.classList.toggle("online", !!firstAvailable);
    statusText.textContent = firstAvailable ? "Online" : "Offline";

    if (firstAvailable) {
      // The bay's own preference wins over "first available in the
      // list". Without this the Code bay only moved to the OpenAI model
      // once the user clicked a bay tab - on a fresh load it opened on
      // whichever channel happened to come first, which is the local
      // one, so the default nobody changes was the weaker model.
      const wanted = preferredProviderFor(state.currentBay);
      selectProvider(wanted || firstAvailable.id);
    } else {
      modelSelect.innerHTML = "<option>no channel available</option>";
      modelSelect.disabled = true;
      channelNote.textContent =
        "nothing trained yet — run: python brain/train.py";
      updateComposerHint();
    }
  } catch (err) {
    statusDot.classList.remove("online");
    statusText.textContent = "Offline";
    channelNote.textContent = "could not reach the server";
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountProviders() {
  // nothing ran at load in this section
}
