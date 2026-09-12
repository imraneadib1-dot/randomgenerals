import { state } from "./state.js";
import { renderGreeting } from "./boot.js";
import { S, patchSettings, patchSettingsDebounced } from "./settings.js";

/* ================================================================
   Usage, notifications, profile picture, saved prompts
   ================================================================ */
export const EVENT_LABELS = {
  usage_limit: "When I am close to running out of credits",
  product_update: "New features and product news",
  security_alert: "Sign-ins, password changes and security warnings",
};

export const CHANNEL_LABELS = { email: "Email", in_app: "In the app" };

export async function loadUsage() {
  const chart = S("usageChart");
  if (!chart) return;
  try {
    const res = await fetch("/api/usage?days=30");
    if (!res.ok) return;
    const data = await res.json();
    S("usageMessages").textContent = data.totals.messages.toLocaleString();
    S("usageCredits").textContent = data.totals.credits.toLocaleString();
    S("usageBalance").textContent = (data.balance ?? 0).toLocaleString();

    // A plain bar chart in divs. A charting library for thirty bars
    // would be more bytes than the whole settings panel.
    const peak = Math.max(1, ...data.series.map((d) => d.messages));
    chart.innerHTML = "";
    for (const day of data.series) {
      const bar = document.createElement("div");
      bar.className = "usage-bar";
      // A day with activity always gets a visible sliver, so "one
      // message" and "none at all" do not look identical.
      const share = day.messages ? Math.max(6, (day.messages / peak) * 100) : 0;
      bar.style.height = share + "%";
      bar.title = `${day.day}: ${day.messages} message(s), ${day.credits} credits`;
      if (!day.messages) bar.classList.add("is-empty");
      chart.appendChild(bar);
    }
    const first = data.series[0], last = data.series[data.series.length - 1];
    S("usageRange").textContent =
      `${first.day} to ${last.day}` +
      (data.totals.busiest ? ` · busiest day ${data.totals.busiest}` : "");
  } catch (_) { /* leave the panel as it is */ }
}

export async function loadInvoices() {
  const list = S("invoiceList");
  if (!list) return;
  // Signed out, there is nothing to list - and asking anyway was a 401
  // in the console on every guest's page load.
  if (!state.currentUser) {
    list.innerHTML = "";
    return;
  }
  try {
    const res = await fetch("/api/billing/invoices");
    const data = await res.json();
    list.innerHTML = "";
    if (!data.invoices || !data.invoices.length) {
      S("invoiceNote").textContent =
        data.detail || "No payments yet.";
      return;
    }
    S("invoiceNote").textContent =
      "Handled by Paddle, who are the seller of record.";
    for (const invoice of data.invoices) {
      const row = document.createElement("div");
      row.className = "memory-item";
      const when = invoice.billed_at
        ? new Date(invoice.billed_at).toLocaleDateString() : "pending";
      const amount = invoice.total
        ? `${invoice.total} ${invoice.currency || ""}` : "";
      row.innerHTML =
        `<div><strong>${when}</strong>` +
        `<div class="memory-hint">${amount} · ${invoice.status || ""}</div></div>`;
      if (invoice.invoice_url) {
        const link = document.createElement("a");
        link.className = "btn-secondary";
        link.href = invoice.invoice_url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = "Receipt";
        row.appendChild(link);
      }
      list.appendChild(row);
    }
  } catch (_) {
    S("invoiceNote").textContent = "Could not load payments.";
  }
}

export async function loadNotifyPrefs() {
  const box = S("notifyMatrix");
  if (!box) return;
  try {
    const res = await fetch("/api/notifications/prefs");
    if (!res.ok) return;
    const { prefs } = await res.json();
    box.innerHTML = "";
    const byEvent = {};
    for (const pref of prefs) (byEvent[pref.event] ||= []).push(pref);

    for (const [event, rows] of Object.entries(byEvent)) {
      const group = document.createElement("div");
      group.className = "notify-group";
      const title = document.createElement("p");
      title.className = "notify-title";
      title.textContent = EVENT_LABELS[event] || event;
      group.appendChild(title);

      for (const pref of rows) {
        const row = document.createElement("div");
        row.className = "toggle-row";
        const label = document.createElement("label");
        label.className = "toggle-label";
        label.textContent = CHANNEL_LABELS[pref.channel] || pref.channel;
        if (pref.locked) label.textContent += " — always on";
        const box2 = document.createElement("input");
        box2.type = "checkbox";
        box2.className = "tl-toggle";
        box2.checked = pref.enabled;
        box2.disabled = pref.locked;
        box2.addEventListener("change", async () => {
          const r = await fetch("/api/notifications/prefs", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event: pref.event, channel: pref.channel,
                                   enabled: box2.checked }),
          });
          if (!r.ok) {
            box2.checked = !box2.checked;      // visible rollback
            const d = await r.json().catch(() => ({}));
            S("notifyStatus").textContent = d.error || "Could not save.";
          } else {
            S("notifyStatus").textContent = "Saved.";
          }
        });
        row.appendChild(label);
        row.appendChild(box2);
        group.appendChild(row);
      }
      box.appendChild(group);
    }
  } catch (_) { /* leave it */ }
}

export function renderAvatar(url) {
  const preview = S("avatarPreview");
  if (!preview) return;
  if (url) {
    preview.style.backgroundImage = `url(${url})`;
    preview.textContent = "";
  } else {
    preview.style.backgroundImage = "";
    preview.textContent = (state.currentUser?.email || "?")[0].toUpperCase();
  }
}

export function initProfileControls() {
  const file = S("avatarFile");
  if (file) {
    file.addEventListener("change", async () => {
      if (!file.files || !file.files[0]) return;
      const form = new FormData();
      form.append("file", file.files[0]);
      S("avatarStatus").textContent = "Uploading…";
      const res = await fetch("/api/account/avatar",
                              { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) { S("avatarStatus").textContent = data.error; return; }
      S("avatarStatus").textContent = "Saved.";
      renderAvatar(data.avatar_url);
    });
  }
  const remove = S("avatarRemove");
  if (remove) {
    remove.addEventListener("click", async () => {
      await fetch("/api/account/avatar", { method: "DELETE" });
      renderAvatar("");
      S("avatarStatus").textContent = "Removed.";
    });
  }
  const bio = S("profileBio");
  if (bio) {
    bio.addEventListener("input", () => {
      S("bioCount").textContent = String(bio.value.length);
      patchSettingsDebounced({ bio: bio.value });
    });
  }

  const nick = S("profileNickname");
  if (nick) {
    nick.addEventListener("input", () => {
      patchSettingsDebounced({ nickname: nick.value });
      // The greeting is rendered from this, so it should not wait for a
      // reload to agree with the field that just changed it.
      if (typeof renderGreeting === "function") renderGreeting();
    });
  }

  const role = S("profileRole");
  if (role) {
    role.addEventListener("input", () => {
      patchSettingsDebounced({ work_role: role.value });
    });
  }

  const font = S("chatFontSelect");
  if (font) {
    font.addEventListener("change", () => {
      // Applied before the save lands: a font picker that waits for a
      // round trip feels broken even when it is working.
      applyChatFont(font.value);
      patchSettingsDebounced({ chat_font: font.value });
    });
  }
}

/** Put the chosen font on <html>; the stylesheet does the rest. */
export function applyChatFont(value) {
  const allowed = ["sans", "serif", "mono"];
  document.documentElement.dataset.chatFont =
    allowed.includes(value) ? value : "sans";
}

export async function loadTemplates() {
  const list = S("templateList");
  if (!list) return;
  try {
    const res = await fetch("/api/settings/templates");
    if (!res.ok) return;
    const { templates } = await res.json();
    list.innerHTML = "";
    if (!templates.length) {
      const empty = document.createElement("p");
      empty.className = "memory-hint";
      empty.textContent = "None saved yet.";
      list.appendChild(empty);
      return;
    }
    for (const template of templates) {
      const row = document.createElement("div");
      row.className = "memory-item";
      // Built by hand rather than innerHTML: the name is whatever the
      // account typed, and it was going straight into markup.
      const meta = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = template.name;
      const hint = document.createElement("div");
      hint.className = "memory-hint";
      hint.textContent = template.body.slice(0, 70) +
        (template.body.length > 70 ? "…" : "");
      meta.appendChild(title);
      meta.appendChild(hint);
      row.appendChild(meta);
      const use = document.createElement("button");
      use.type = "button";
      use.className = "btn-secondary";
      use.textContent = "Use";
      use.addEventListener("click", async () => {
        S("setSystemPrompt").value = template.body;
        S("setPromptCount").textContent = template.body.length;
        await patchSettings({ system_prompt: template.body });
        S("templateStatus").textContent = `Applied "${template.name}".`;
      });
      row.appendChild(use);
      // A built-in has no row in the table to delete, so offering the
      // button would only ever produce an error.
      if (!template.builtin) {
        const drop = document.createElement("button");
        drop.type = "button";
        drop.className = "btn-danger";
        drop.textContent = "Delete";
        drop.addEventListener("click", async () => {
          drop.disabled = true;
          const r = await fetch("/api/settings/templates/" + template.id,
                                { method: "DELETE" });
          if (r.ok) loadTemplates(); else drop.disabled = false;
        });
        row.appendChild(drop);
      }
      list.appendChild(row);
    }
  } catch (_) { /* leave it */ }
}

export function initTemplates() {
  const save = S("templateSave");
  if (!save) return;
  save.addEventListener("click", async () => {
    const name = S("templateName").value.trim();
    const body = S("setSystemPrompt").value.trim();
    const status = S("templateStatus");
    if (!name) { status.textContent = "Give it a name first."; return; }
    if (!body) { status.textContent = "Write a system prompt above first."; return; }
    const res = await fetch("/api/settings/templates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, body }),
    });
    const data = await res.json();
    if (!res.ok) { status.textContent = data.error; return; }
    S("templateName").value = "";
    status.textContent = "Saved.";
    loadTemplates();
  });
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountProfile() {
  initProfileControls();

  initTemplates();

  loadNotifyPrefs();

  loadTemplates();

  loadUsage();

  loadInvoices();

}
