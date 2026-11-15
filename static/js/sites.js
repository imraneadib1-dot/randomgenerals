import { getJSON, postJSON } from "./api.js";
import { openSettings, switchModalTab } from "./modal.js";

/* ----------------------------------------------------------------
   The project card under a built website.

   The builder (sitebuilder.py) leaves a { kind: "site" } display on its
   reply; this draws it. The card asks the server for the site's current
   state rather than trusting what was saved on the message, because the
   same project is revised and published long after that reply - a card
   from three versions ago should still say what is live now.

   Publishing needs an account (a site under this domain needs someone
   answerable for it); building and downloading do not.
   ---------------------------------------------------------------- */

const ICON_SITE =
  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
  '<rect x="3" y="4" width="18" height="16" rx="2.5" stroke="currentColor" stroke-width="1.7"/>' +
  '<path d="M3 9h18M7 6.5h.01M10 6.5h.01" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function hostOf(url) {
  try { return new URL(url).host; } catch (_) { return url; }
}

/** @param {HTMLElement} msgEl @param {{id: string, name?: string, version?: number, files?: string[]}} d */
export function renderSiteCard(msgEl, d) {
  if (!d || !d.id || msgEl.querySelector(`.site-card[data-site="${CSS.escape(d.id)}"]`)) return;
  const card = el("section", "site-card");
  card.dataset.site = d.id;
  card.setAttribute("aria-label", "Website project");

  const head = el("div", "site-card-head");
  const icon = el("span", "site-card-icon");
  icon.innerHTML = ICON_SITE;
  const titles = el("div", "site-card-titles");
  const name = el("strong", "site-card-name", d.name || "Your website");
  const meta = el("span", "site-card-meta",
    `${(d.files || []).length} files · version ${d.version || 1}`);
  titles.append(name, meta);
  const zip = el("a", "site-btn site-btn-ghost", "Download .zip");
  zip.href = `/api/sites/${encodeURIComponent(d.id)}/zip`;
  zip.setAttribute("download", "");
  head.append(icon, titles, zip);

  const files = el("div", "site-card-files");
  (d.files || []).forEach((f) => files.append(el("span", "site-file", f)));

  const body = el("div", "site-card-body");
  const status = el("p", "site-card-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  card.append(head, files, body, status);
  msgEl.appendChild(card);

  const say = (text, isError = false) => {
    status.textContent = text || "";
    status.classList.toggle("is-error", !!isError);
  };

  async function refresh() {
    let s;
    try {
      s = await getJSON(`/api/sites/${encodeURIComponent(d.id)}`);
    } catch (err) {
      body.replaceChildren(el("p", "site-card-note", "This project is no longer available."));
      return;
    }
    meta.textContent = `${s.files.length} files · version ${s.version}`
      + (s.submissions ? ` · ${s.submissions} form ${s.submissions === 1 ? "entry" : "entries"}` : "");
    draw(s);
  }

  function draw(s) {
    body.replaceChildren();
    if (s.url) {
      const live = el("div", "site-live");
      const dot = el("span", "site-live-dot");
      const link = el("a", "site-live-link", hostOf(s.url));
      link.href = s.url;
      link.target = "_blank";
      link.rel = "noopener";
      live.append(dot, el("span", "", "Live at "), link);
      const actions = el("div", "site-actions");
      if (s.published_version && s.version > s.published_version) {
        const upd = el("button", "site-btn site-btn-primary", `Publish v${s.version}`);
        upd.type = "button";
        upd.addEventListener("click", () => publish(s.slug, upd));
        actions.append(upd);
      }
      const subs = el("a", "site-btn site-btn-ghost", "Form entries");
      subs.href = "/sites";
      const off = el("button", "site-btn site-btn-ghost", "Unpublish");
      off.type = "button";
      off.addEventListener("click", async () => {
        off.disabled = true;
        try {
          draw(await postJSON(`/api/sites/${encodeURIComponent(d.id)}/unpublish`));
          say("Taken offline. Publish again any time.");
        } catch (err) {
          say(err.message, true);
          off.disabled = false;
        }
      });
      actions.append(subs, off);
      body.append(live, actions);
      return;
    }

    if (!s.signed_in) {
      const row = el("div", "site-actions");
      row.append(el("span", "site-card-note", "Sign in to publish it on its own address."));
      const sign = el("button", "site-btn site-btn-primary", "Sign in to publish");
      sign.type = "button";
      sign.addEventListener("click", () => { openSettings(); switchModalTab("account"); });
      row.append(sign);
      body.append(row);
      return;
    }

    const form = el("form", "site-publish");
    const field = el("label", "site-slug");
    const input = /** @type {HTMLInputElement} */ (el("input"));
    input.name = "slug";
    input.value = s.slug || s.suggested_slug || "";
    input.required = true;
    input.maxLength = 32;
    input.pattern = "[a-z0-9][a-z0-9-]{1,30}[a-z0-9]";
    input.setAttribute("aria-label", "Site address");
    input.autocomplete = "off";
    input.spellcheck = false;
    input.addEventListener("input", () => {
      input.value = input.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
    });
    field.append(input, el("span", "site-slug-domain", "." + s.domain));
    const go = el("button", "site-btn site-btn-primary", "Publish");
    /** @type {HTMLButtonElement} */ (go).type = "submit";
    form.append(field, go);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      publish(input.value, go);
    });
    body.append(form);
  }

  async function publish(slug, btn) {
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "Publishing…";
    say("");
    try {
      const s = await postJSON(`/api/sites/${encodeURIComponent(d.id)}/publish`, { slug });
      draw(s);
      say("Published. It can take a minute for the address to work the first time.");
    } catch (err) {
      if (err.body && err.body.signin) { openSettings(); switchModalTab("account"); }
      say(err.message, true);
      btn.disabled = false;
      btn.textContent = was;
    }
  }

  refresh();
}
