import { state } from "./state.js";
import { readPref, writePref } from "./appearance.js";
import { selectBay } from "./bays.js";
import { strengthToggle } from "./dom.js";
import { modalTabs } from "./modal.js";
import { applyChatFont, renderAvatar } from "./profile.js";
import { deleteAllConversations, loadThreadList } from "./sidebar.js";
import { confirmDialog } from "./confirm.js";
import { explain, patchJSON } from "./api.js";
import { toast } from "./toast.js";

/* ----------------------------------------------------------------
   Settings that do something

   Every control here changes behaviour on the next action - none is a
   placeholder for a feature that does not exist, which is the failure
   mode a settings screen invites. Preferences live in localStorage
   because they are per-browser choices, not account state: signing in
   on a different machine should not drag your font size across.
   ---------------------------------------------------------------- */
export const PREF_STRENGTH = "defaultStrength";

export const PREF_BAY = "defaultBay";

export const PREF_ENTER = "enterToSend";

export const PREF_MOTION = "reduceMotion";

export function applyMotionPref() {
  const off = readPref(PREF_MOTION) === "1";
  document.documentElement.setAttribute(
    "data-motion", off ? "reduced" : "full");
}

export function initSettingsControls() {
  const strength = document.getElementById("defaultStrength");
  const bay = document.getElementById("defaultBay");
  const enter = document.getElementById("enterToSend");
  const motion = document.getElementById("reduceMotion");

  if (strength) {
    strength.value = readPref(PREF_STRENGTH) || "quick";
    strength.addEventListener("change", () => {
      writePref(PREF_STRENGTH, strength.value);
      // Applies now as well as next time: changing a default in front of
      // someone and having it not take effect reads as a broken switch.
      // Apply it now as well as next time: changing a default in front
      // of someone and having nothing happen reads as a broken switch.
      state.currentStrength = strength.value;
      strengthToggle.setAttribute(
        "aria-checked", String(state.currentStrength === "deep"));
    });
  }
  if (bay) {
    bay.value = readPref(PREF_BAY) || "chat";
    bay.addEventListener("change", () => writePref(PREF_BAY, bay.value));
  }
  if (enter) {
    enter.checked = readPref(PREF_ENTER) !== "0";
    enter.addEventListener("change", () => {
      writePref(PREF_ENTER, enter.checked ? "1" : "0");
    });
  }
  if (motion) {
    motion.checked = readPref(PREF_MOTION) === "1";
    motion.addEventListener("change", () => {
      writePref(PREF_MOTION, motion.checked ? "1" : "0");
      applyMotionPref();
    });
  }
}

/* Filtering the rail, not the whole page. Searching settings is how you
   find a category you cannot name - so this matches the label AND a few
   keywords per tab, or "dark" would find nothing. */
export const SETTINGS_KEYWORDS = {
  account: "sign in out email password google login",
  general: "default mode quick deep bay enter send motion animation",
  appearance: "colour color accent theme sky dark light background",
  memory: "custom instructions remember personalization name",
  data: "export delete download privacy conversations egress",
  plan: "billing upgrade pro credits subscription payment",
  about: "version model provider status licence",
};

export function initSettingsSearch() {
  const box = document.getElementById("settingsSearch");
  if (!box) return;
  box.addEventListener("input", () => {
    const q = box.value.trim().toLowerCase();
    modalTabs.forEach((t) => {
      if (!q) { t.hidden = false; return; }
      const key = t.dataset.tab;
      const hay = (t.textContent + " " + (SETTINGS_KEYWORDS[key] || ""))
        .toLowerCase();
      t.hidden = !hay.includes(q);
    });
    // Jump to the first surviving tab so the pane matches the list.
    const first = modalTabs.find((t) => !t.hidden);
    if (q && first && first.getAttribute("aria-selected") !== "true") {
      first.click();
    }
  });
}

/* Data controls. Export is a real download of what the server holds for
   you; delete really deletes, one thread at a time through the endpoint
   that already checks ownership - rather than a bulk route that would
   need its own authorisation logic. */
export function initDataControls() {
  const exportBtn = document.getElementById("exportData");
  const deleteBtn = document.getElementById("deleteAllThreads");
  const status = document.getElementById("dataStatus");
  const egress = document.getElementById("dataEgress");

  if (exportBtn) {
    exportBtn.addEventListener("click", async () => {
      status.textContent = "Collecting…";
      try {
        const list = await (await fetch("/api/threads")).json();
        const full = [];
        for (const t of list.threads || []) {
          const one = await (await fetch("/api/threads/" + t.id)).json();
          full.push(one);
        }
        const blob = new Blob([JSON.stringify(full, null, 2)],
                              { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "randomgenerals-conversations.json";
        a.click();
        URL.revokeObjectURL(a.href);
        status.textContent = `Exported ${full.length} conversations.`;
      } catch (_) {
        status.textContent = "Could not export just now.";
      }
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      const sure = await confirmDialog({
        title: "Delete every conversation?",
        body: "This cannot be undone.",
        confirmLabel: "Delete all", danger: true,
      });
      if (!sure) return;
      try {
        await deleteAllConversations((t) => (status.textContent = t));
      } catch (_) {
        status.textContent = "Could not delete just now.";
      }
    });
  }

  if (egress) {
    // Named from what the server actually reports, not from a fixed
    // sentence - this app has been wrong about where prompts go before.
    fetch("/api/health").then((r) => r.json()).then((h) => {
      const fast = h.fast_channel && h.fast_channel.configured;
      egress.textContent = fast
        ? "Chat and code are answered by Groq, so those messages leave "
          + "this server. Images go to Pollinations. Web search goes to "
          + "DuckDuckGo. Nothing else is sent anywhere."
        : "Everything is answered on this server right now. Images go to "
          + "Pollinations and web search to DuckDuckGo; nothing else "
          + "leaves.";
    }).catch(() => {
      egress.textContent = "Could not check right now.";
    });
  }
}

/* ================================================================
   Settings

   One document, fetched once, PATCHed in pieces. Optimistic for
   preferences - a theme that waits on a round trip feels broken - and
   never optimistic for security, where showing "two-factor on" before
   the server agrees would be a lie about safety.
   ================================================================ */
export const S = (id) => document.getElementById(id);

export async function loadSettingsDoc() {
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) return;
    state.settingsDoc = await res.json();
    renderSettingsDoc();
  } catch (_) {
    /* leave the panels as they are */
  }
}

export async function patchSettings(patch) {
  try {
    const data = await patchJSON("/api/settings", patch);
    if (state.settingsDoc) state.settingsDoc.settings = data.settings;
    return { ok: true };
  } catch (err) {
    // Said out loud, once, whatever the caller does with the result.
    // Most callers ignored it: the native control already showed the
    // new value, so a save that failed looked exactly like one that
    // worked until the next reload put the old value back.
    const error = explain(err, "Could not save.");
    toast(error, { kind: "error", id: "settings-save" });
    return { ok: false, error };
  }
}

/* Text fields are debounced; a switch commits at once, because a switch
   has a visible state that has to match the server. */
export function patchSettingsDebounced(patch, ms = 500) {
  clearTimeout(state.settingsSaveTimer);
  state.settingsSaveTimer = setTimeout(() => patchSettings(patch), ms);
}

export function fillTimezones(select, current) {
  if (!select || select.options.length) return;
  let zones = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch (_) {
    // Older browsers have no supportedValuesOf. A short list beats an
    // empty dropdown, and the detect button covers the rest.
    zones = ["UTC", "Europe/London", "Europe/Paris", "Europe/Berlin",
             "Africa/Casablanca", "America/New_York", "America/Chicago",
             "America/Los_Angeles", "Asia/Dubai", "Asia/Kolkata",
             "Asia/Tokyo", "Australia/Sydney"];
  }
  if (!zones.includes(current)) zones = [current, ...zones];
  select.innerHTML = "";
  for (const zone of zones) {
    const option = document.createElement("option");
    option.value = zone;
    option.textContent = zone.replace(/_/g, " ");
    select.appendChild(option);
  }
}

export function showSlider(input, output, value, suffix = "") {
  if (!input || !output) return;
  if (value === null || value === undefined) {
    // A slider cannot show "unset", so the number beside it does. The
    // thumb sits at the server's own default meanwhile.
    output.textContent = "default";
    input.value = input.dataset.fallback || input.min;
    return;
  }
  input.value = value;
  output.textContent = value + suffix;
}

export function renderSettingsDoc() {
  if (!state.settingsDoc) return;
  const s = state.settingsDoc.settings;
  const account = state.settingsDoc.account;

  if (S("setLanguage")) S("setLanguage").value = s.language || "en";
  fillTimezones(S("setTimezone"), s.timezone || "UTC");
  if (S("setTimezone")) S("setTimezone").value = s.timezone || "UTC";
  if (S("setRetention")) {
    S("setRetention").value = s.retention_days ? String(s.retention_days) : "";
  }

  showSlider(S("setTemperature"), S("setTemperatureOut"), s.temperature);
  showSlider(S("setTopP"), S("setTopPOut"), s.top_p);
  showSlider(S("setMaxTokens"), S("setMaxTokensOut"), s.max_tokens);

  if (S("setSystemPrompt")) {
    S("setSystemPrompt").value = s.system_prompt || "";
    S("setPromptCount").textContent = (s.system_prompt || "").length;
  }
  if (S("profileBio")) {
    S("profileBio").value = s.bio || "";
    S("bioCount").textContent = (s.bio || "").length;
  }
  state.accountNickname = s.nickname || "";
  if (S("profileNickname")) S("profileNickname").value = s.nickname || "";
  if (S("profileRole")) S("profileRole").value = s.work_role || "";
  if (S("chatFontSelect")) S("chatFontSelect").value = s.chat_font || "sans";
  applyChatFont(s.chat_font || "sans");
  renderAvatar(s.avatar_url);
  if (S("setWebSearch")) S("setWebSearch").checked = !!s.web_search;
  if (S("setTools")) S("setTools").checked = !!s.tools_enabled;

  // Security is only meaningful on a real account.
  const signedIn = !!account.signed_in;
  for (const id of ["mfaSection", "mfaDivider",
                    "sessionsSection", "sessionsDivider"]) {
    if (S(id)) S(id).hidden = !signedIn;
  }
  if (signedIn) {
    renderMfaState(state.settingsDoc.mfa.enabled);
    loadSessions();
    loadProviderKeys();
  }

  const problem = S("setKeysProblem");
  if (problem) {
    problem.hidden = !state.settingsDoc.secrets_problem;
    problem.textContent = state.settingsDoc.secrets_problem || "";
  }
}

export function initSettingsPanels() {
  const lang = S("setLanguage");
  if (lang) {
    lang.addEventListener("change", () =>
      patchSettings({ language: lang.value }));
  }
  const tz = S("setTimezone");
  if (tz) {
    tz.addEventListener("change", () => patchSettings({ timezone: tz.value }));
  }
  const detect = S("setTzDetect");
  if (detect) {
    detect.addEventListener("click", async () => {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      fillTimezones(tz, zone);
      if (tz) tz.value = zone;
      await patchSettings({ timezone: zone });
    });
  }

  /** @type {[string, string, string, (s: string, radix?: number) => number][]} */
  const sliders = [
    ["setTemperature", "setTemperatureOut", "temperature", parseFloat],
    ["setTopP", "setTopPOut", "top_p", parseFloat],
    ["setMaxTokens", "setMaxTokensOut", "max_tokens", parseInt],
  ];
  for (const [inputId, outId, field, parse] of sliders) {
    const input = S(inputId);
    if (!input) continue;
    input.addEventListener("input", () => {
      S(outId).textContent = input.value;
    });
    input.addEventListener("change", () =>
      patchSettings({ [field]: parse(input.value, 10) }));
  }

  const reset = S("setResetSampling");
  if (reset) {
    reset.addEventListener("click", async () => {
      // null means "use the server's default", which is not the same as
      // any particular number.
      await patchSettings({ temperature: null, top_p: null,
                            max_tokens: null });
      await loadSettingsDoc();
    });
  }

  const prompt = S("setSystemPrompt");
  if (prompt) {
    prompt.addEventListener("input", () => {
      S("setPromptCount").textContent = String(prompt.value.length);
      patchSettingsDebounced({ system_prompt: prompt.value });
    });
  }
  for (const [id, field] of [["setWebSearch", "web_search"],
                             ["setTools", "tools_enabled"]]) {
    const box = S(id);
    if (!box) continue;
    box.addEventListener("change", async () => {
      const result = await patchSettings({ [field]: box.checked });
      if (!result.ok) box.checked = !box.checked;   // visible rollback
    });
  }

  const retention = S("setRetention");
  if (retention) {
    retention.addEventListener("change", async () => {
      const value = retention.value ? parseInt(retention.value, 10) : null;
      const result = await patchSettings({ retention_days: value });
      S("setRetentionStatus").textContent = result.ok
        ? (value ? "Older conversations will be removed." : "Nothing is deleted automatically.")
        : result.error;
      if (result.ok && value) {
        const res = await fetch("/api/settings/apply-retention",
                                { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (data.deleted) {
          S("setRetentionStatus").textContent =
            `Removed ${data.deleted} old conversation(s).`;
          loadThreadList();
        }
      }
    });
  }

  initProviderKeys();
  initMfa();
  initSessions();
}

/* ---------------------------------------------------------- keys --- */
export async function loadProviderKeys() {
  const list = S("setKeyList");
  if (!list) return;
  try {
    const res = await fetch("/api/account/provider-keys");
    if (!res.ok) return;
    const data = await res.json();
    list.innerHTML = "";
    if (!data.keys.length) {
      const empty = document.createElement("p");
      empty.className = "memory-hint";
      empty.textContent = "No keys saved. Requests use this server's own.";
      list.appendChild(empty);
      return;
    }
    for (const key of data.keys) {
      const row = document.createElement("div");
      row.className = "memory-item";
      const text = document.createElement("div");
      text.innerHTML =
        `<strong>${key.provider}</strong>` +
        `<div class="memory-hint">ends ${key.last_four} · ` +
        `${key.last_used ? "last used " + new Date(key.last_used).toLocaleDateString() : "not used yet"}</div>`;
      row.appendChild(text);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn-danger";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        const r = await fetch("/api/account/provider-keys/" + key.provider,
                              { method: "DELETE" });
        if (r.ok) loadProviderKeys();
        else remove.disabled = false;
      });
      row.appendChild(remove);
      list.appendChild(row);
    }
  } catch (_) { /* leave it */ }
}

export function initProviderKeys() {
  const save = S("setKeySave");
  if (!save) return;
  save.addEventListener("click", async () => {
    const provider = S("setKeyProvider").value;
    const value = S("setKeyValue").value.trim();
    const status = S("setKeyStatus");
    if (!value) { status.textContent = "Paste a key first."; return; }
    save.disabled = true;
    status.textContent = "Checking it with " + provider + "…";
    try {
      const res = await fetch("/api/account/provider-keys/" + provider, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: value }),
      });
      const data = await res.json();
      if (!res.ok) { status.textContent = data.error || "Could not save."; return; }
      status.textContent = "Saved. Only the last four digits are kept visible.";
      S("setKeyValue").value = "";
      loadProviderKeys();
    } catch (_) {
      status.textContent = "Could not reach the server.";
    } finally {
      save.disabled = false;
    }
  });
}

/* ----------------------------------------------------------- MFA --- */
export function renderMfaState(enabled) {
  if (!S("mfaBadge")) return;
  S("mfaBadge").textContent = enabled ? "on" : "off";
  S("mfaStart").hidden = enabled;
  S("mfaOn").hidden = !enabled;
  S("mfaSetup").hidden = true;
}

export function initMfa() {
  const enroll = S("mfaEnroll");
  if (!enroll) return;

  enroll.addEventListener("click", async () => {
    S("mfaStatus").textContent = "";
    const res = await fetch("/api/account/mfa/enroll", { method: "POST" });
    const data = await res.json();
    if (!res.ok) { S("mfaStatus").textContent = data.error; return; }
    S("mfaSecret").textContent = data.secret;
    S("mfaStart").hidden = true;
    S("mfaSetup").hidden = false;
    S("mfaCode").focus();
  });

  S("mfaCancel").addEventListener("click", () => {
    S("mfaSetup").hidden = true;
    S("mfaStart").hidden = false;
  });

  S("mfaConfirm").addEventListener("click", async () => {
    S("mfaStatus").textContent = "Checking…";
    const res = await fetch("/api/account/mfa/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: S("mfaCode").value.trim() }),
    });
    const data = await res.json();
    if (!res.ok) { S("mfaStatus").textContent = data.error; return; }
    S("mfaStatus").textContent = "Two-factor is on.";
    S("mfaCodeList").textContent = (data.backup_codes || []).join("\n");
    S("mfaCodes").hidden = false;
    renderMfaState(true);
  });

  S("mfaNewCodes").addEventListener("click", async () => {
    const res = await fetch("/api/account/mfa/backup-codes",
                            { method: "POST" });
    const data = await res.json();
    if (!res.ok) { S("mfaStatus").textContent = data.error; return; }
    S("mfaCodeList").textContent = (data.backup_codes || []).join("\n");
    S("mfaCodes").hidden = false;
    S("mfaStatus").textContent = "New codes. The old ones no longer work.";
  });

  S("mfaOff").addEventListener("click", async () => {
    const field = S("mfaPassword");
    if (field.hidden) {
      // Ask for the password in place rather than in a dialog, and only
      // once the intent is clear.
      field.hidden = false;
      field.focus();
      S("mfaStatus").textContent = "Enter your password, then press again.";
      return;
    }
    const res = await fetch("/api/account/mfa/disable", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: field.value }),
    });
    const data = await res.json();
    if (!res.ok) { S("mfaStatus").textContent = data.error; return; }
    field.value = "";
    field.hidden = true;
    S("mfaCodes").hidden = true;
    S("mfaStatus").textContent = "Two-factor is off.";
    renderMfaState(false);
  });
}

/* ------------------------------------------------------ sessions --- */
export async function loadSessions() {
  const list = S("sessionList");
  if (!list) return;
  try {
    const res = await fetch("/api/account/sessions");
    if (!res.ok) return;
    const data = await res.json();
    list.innerHTML = "";
    for (const item of data.sessions) {
      const row = document.createElement("div");
      row.className = "memory-item";
      const text = document.createElement("div");
      const when = new Date(item.last_seen).toLocaleString();
      text.innerHTML =
        `<strong>${item.device}</strong>` +
        (item.current ? ' <span class="badge-inline">this device</span>' : "") +
        `<div class="memory-hint">${item.ip || "unknown address"} · last active ${when}</div>`;
      row.appendChild(text);
      if (!item.current) {
        const out = document.createElement("button");
        out.type = "button";
        out.className = "btn-danger";
        out.textContent = "Sign out";
        out.addEventListener("click", async () => {
          out.disabled = true;
          const r = await fetch("/api/account/sessions/" + item.id,
                                { method: "DELETE" });
          if (r.ok) loadSessions();
          else out.disabled = false;
        });
        row.appendChild(out);
      }
      list.appendChild(row);
    }
  } catch (_) { /* leave it */ }
}

export function initSessions() {
  const refresh = S("sessionRefresh");
  if (refresh) refresh.addEventListener("click", loadSessions);
  const all = S("sessionRevokeAll");
  if (all) {
    all.addEventListener("click", async () => {
      const sure = await confirmDialog({
        title: "Sign out every other device?",
        body: "You will stay signed in here.",
        confirmLabel: "Sign them out",
      });
      if (!sure) return;
      const res = await fetch("/api/account/sessions/revoke-others",
                              { method: "POST" });
      const data = await res.json();
      S("sessionStatus").textContent = res.ok
        ? `Signed out ${data.revoked} other device(s).`
        : data.error || "Could not do that.";
      loadSessions();
    });
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountSettings() {
  // Apply the saved defaults before the first render, so the app opens in
  // the state the person chose rather than snapping to it a moment later.
  (function applySavedDefaults() {
    const st = readPref(PREF_STRENGTH);
    if (st === "deep" || st === "quick") {
      state.currentStrength = st;
      strengthToggle.setAttribute("aria-checked", String(st === "deep"));
    }
    const bay = readPref(PREF_BAY);
    if (bay && bay !== state.currentBay && typeof selectBay === "function") {
      selectBay(bay);
    }
  })();

  applyMotionPref();

  initSettingsControls();

  initSettingsSearch();

  initDataControls();

  initSettingsPanels();

  loadSettingsDoc();

}
