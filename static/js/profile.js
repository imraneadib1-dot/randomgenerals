import { state } from "./state.js";
import { renderGreeting } from "./boot.js";
import { S, patchSettings, patchSettingsDebounced } from "./settings.js";
import { loadCredits } from "./credits.js";

/* ================================================================
   Usage, notifications, profile picture, saved prompts
   ================================================================ */
export const EVENT_LABELS = {
  usage_limit: "When I am close to running out of credits",
  product_update: "New features and product news",
  security_alert: "Sign-ins, password changes and security warnings",
};

export const CHANNEL_LABELS = { email: "Email", in_app: "In the app" };

/* SETTINGS > USAGE. The credit meter (credits.js keeps it current) and,
   under it, tokens / messages / credits by day for 7, 30 or 90 days.
   The three totals double as the switch for what the chart shows, and
   a day - clicked, or reached with the arrow keys - shows its numbers
   underneath: the preview. A plain bar chart in buttons; a charting
   library for ninety bars would outweigh the whole settings panel. */
const usageView = { days: 30, metric: "tokens", data: null, picked: -1 };
const METRIC_WORD = { tokens: "tokens", messages: "messages", credits: "credits" };

const fmtDay = (iso, long = false) => new Date(iso + "T00:00:00").toLocaleDateString(
  undefined, long ? { weekday: "short", month: "short", day: "numeric" } : { month: "short", day: "numeric" });

function showUsageDay(i) {
  const box = S("usageDay");
  const data = usageView.data;
  if (!box || !data) return;
  const series = data.series;
  box.textContent = "";
  const head = document.createElement("strong");
  const rest = document.createElement("span");
  if (i < 0 || i >= series.length) {
    // Nothing picked: the period as a whole.
    const t = data.totals;
    head.textContent = `Last ${data.days} days`;
    rest.textContent = t.busiest
      ? ` · busiest day ${fmtDay(t.busiest, true)} · click a day for its numbers`
      : " · nothing yet";
  } else {
    const d = series[i];
    head.textContent = fmtDay(d.day, true);
    rest.textContent = ` · ${d.tokens.toLocaleString()} tokens · ${d.messages.toLocaleString()} `
      + `message${d.messages === 1 ? "" : "s"} · ${d.credits.toLocaleString()} credits`;
  }
  box.append(head, rest);
}

function pickUsageDay(i) {
  const chart = S("usageChart");
  usageView.picked = i;
  const stop = i < 0 ? chart.children.length - 1 : i;
  [...chart.children].forEach((b, k) => {
    b.setAttribute("aria-selected", String(k === i));
    /** @type {HTMLElement} */ (b).tabIndex = k === stop ? 0 : -1;
  });
  showUsageDay(i);
}

function drawUsage() {
  const chart = S("usageChart");
  const data = usageView.data;
  if (!chart || !data) return;
  const metric = usageView.metric;
  const peak = Math.max(1, ...data.series.map((d) => d[metric]));
  chart.replaceChildren();
  chart.classList.toggle("is-dense", data.series.length > 40);
  data.series.forEach((day, i) => {
    const bar = document.createElement("button");
    bar.type = "button";
    bar.className = "usage-bar";
    bar.setAttribute("role", "option");
    const v = day[metric];
    // A day with activity always gets a visible sliver, so "one" and
    // "none at all" do not look identical.
    bar.style.setProperty("--h", (v ? Math.max(6, (v / peak) * 100) : 0) + "%");
    if (!v) bar.classList.add("is-empty");
    bar.title = `${fmtDay(day.day)}: ${v.toLocaleString()} ${METRIC_WORD[metric]}`;
    bar.setAttribute("aria-label", bar.title);
    bar.addEventListener("click", () => pickUsageDay(usageView.picked === i ? -1 : i));
    bar.addEventListener("keydown", (e) => {
      const n = data.series.length;
      let to = null;
      if (e.key === "ArrowLeft") to = Math.max(0, i - 1);
      else if (e.key === "ArrowRight") to = Math.min(n - 1, i + 1);
      else if (e.key === "Home") to = 0;
      else if (e.key === "End") to = n - 1;
      if (to === null) return;
      e.preventDefault();
      pickUsageDay(to);
      /** @type {HTMLElement} */ (chart.children[to])?.focus();
    });
    chart.appendChild(bar);
  });
  const first = data.series[0], last = data.series[data.series.length - 1];
  S("usageFrom").textContent = first ? fmtDay(first.day) : "";
  S("usageTo").textContent = last ? fmtDay(last.day) : "";
  pickUsageDay(usageView.picked < data.series.length ? usageView.picked : -1);
}

export async function loadUsage() {
  const chart = S("usageChart");
  if (!chart) return;
  try {
    const res = await fetch(`/api/usage?days=${usageView.days}`);
    if (!res.ok) return;
    const data = await res.json();
    usageView.data = data;
    usageView.picked = -1;
    S("usageTokens").textContent = (data.totals.tokens || 0).toLocaleString();
    S("usageMessages").textContent = data.totals.messages.toLocaleString();
    S("usageCredits").textContent = data.totals.credits.toLocaleString();
    S("usageBalance").textContent = (data.balance ?? 0).toLocaleString();
    drawUsage();
  } catch (_) { /* leave the panel as it is */ }
}

function initUsageControls() {
  const radios = (group, key, onPick) => {
    const box = S(group);
    if (!box) return;
    box.querySelectorAll('[role="radio"]').forEach((/** @type {HTMLElement} */ b) =>
      b.addEventListener("click", () => {
        box.querySelectorAll('[role="radio"]').forEach((x) =>
          x.setAttribute("aria-checked", String(x === b)));
        onPick(b.dataset[key]);
      }));
  };
  radios("usageDays", "days", (v) => { usageView.days = Number(v) || 30; loadUsage(); });
  radios("usageMetric", "metric", (v) => { usageView.metric = v; drawUsage(); });
  // Fresh numbers every time the tab is opened - a reply sent since the
  // page loaded has moved them.
  document.querySelector('.modal-tab[data-tab="usage"]')?.addEventListener("click", () => {
    loadUsage();
    loadCredits();
  });
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

  initUsageControls();

  loadUsage();

  loadInvoices();

}
