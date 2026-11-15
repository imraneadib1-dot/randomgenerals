/* ----------------------------------------------------------------
   Connected apps

   Paste a link; the server works out what is behind it and, if it finds
   an API description, every operation in it becomes something the
   assistant can call mid-answer.

   The discovery request is the slow part - it may try several addresses
   on someone else's server - so the button reports progress rather than
   sitting there looking broken.
   ---------------------------------------------------------------- */
export function renderConnectorList(items, max, plan) {
  const list = document.getElementById("connectorList");
  if (!list) return;
  list.innerHTML = "";

  // How many of the allowance is spent. Shown whether or not anything
  // is connected, because on Free the allowance is one - and finding
  // that out by pasting a link and being refused is a worse way to
  // learn it than reading it here first.
  const used = (items || []).length;
  if (max) {
    const count = document.createElement("p");
    count.className = "memory-hint";
    if (used >= max) {
      count.textContent =
        plan === "pro"
          ? used + " of " + max + " connected — remove one to add another."
          : used +
            " of " +
            max +
            " connected. Remove it to connect a different app, or upgrade" +
            " to Pro for more at once.";
    } else {
      count.textContent = used + " of " + max + " connected.";
    }
    list.appendChild(count);
  }

  if (!items || !items.length) {
    const empty = document.createElement("p");
    empty.className = "memory-hint";
    empty.textContent = "Nothing connected yet.";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "memory-item";

    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title || item.url;
    text.appendChild(title);

    const detail = document.createElement("div");
    detail.className = "memory-hint";
    // What it can actually do, rather than just its address - the
    // difference between a parsed API and a readable page is the whole
    // difference in what the assistant can do with it.
    detail.textContent = item.kind === "openapi"
      ? item.operations + " operations"
        + (item.has_token ? " · token saved" : "")
      : "Readable page" + (item.has_token ? " · token saved" : "");
    text.appendChild(detail);
    row.appendChild(text);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn-danger";
    remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        const r = await fetch("/api/connectors/" + encodeURIComponent(item.id),
                              { method: "DELETE" });
        if (r.ok) loadConnectors();
        else remove.disabled = false;
      } catch (_) {
        remove.disabled = false;
      }
    });
    row.appendChild(remove);
    list.appendChild(row);
  });
}

export async function loadConnectors() {
  try {
    const r = await fetch("/api/connectors");
    if (!r.ok) return;
    const d = await r.json();
    renderConnectorList(d.connectors || [], d.max, d.plan);
  } catch (_) {
    /* the panel simply stays as it was */
  }
}

export function initConnectors() {
  const add = document.getElementById("connectorAdd");
  if (!add) return;
  const url = document.getElementById("connectorUrl");
  const token = document.getElementById("connectorToken");
  const status = document.getElementById("connectorStatus");

  const submit = async () => {
    if (!(url.value || "").trim()) {
      status.textContent = "Paste a link first.";
      return;
    }
    add.disabled = true;
    status.textContent = "Looking at that link…";
    try {
      const r = await fetch("/api/connectors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url.value.trim(),
          token: (token.value || "").trim(),
        }),
      });
      const d = await r.json();
      if (!r.ok) {
        status.textContent = d.error || "Could not connect that.";
        return;
      }
      const c = d.connector || {};
      status.textContent = c.kind === "openapi"
        ? "Connected " + c.title + " — " + c.operations
          + " operations are now available."
        : "Connected " + c.title
          + " as a readable page. No API description was found there, so "
          + "the assistant can read it but not act on it.";
      url.value = "";
      token.value = "";
      loadConnectors();
    } catch (_) {
      status.textContent = "Could not reach the server.";
    } finally {
      add.disabled = false;
    }
  };

  add.addEventListener("click", submit);
  url.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  });
  loadConnectors();
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountConnectors() {
  initConnectors();

}


/* ---- a key for other apps ---------------------------------------------
   The route returns the key exactly once and stores only its hash, so the
   value is put on screen and left there rather than being re-fetchable.
   The confirm() is there because issuing a second key silently revokes
   the first, which would stop a Blender add-on or MCP client already
   using it with no error that points back here. */
export function mountApiKey() {
  const btn = document.getElementById("issueApiKey");
  const out = /** @type {HTMLInputElement|null} */ (document.getElementById("apiKeyOut"));
  const status = document.getElementById("apiKeyStatus");
  const base = document.getElementById("apiKeyBase");
  if (!btn || !out || !status) return;

  out.addEventListener("click", () => out.select());

  btn.addEventListener("click", async () => {
    const replacing =
      "Creating a key replaces the one you already have. Anything still " +
      "using the old key will stop working. Continue?";
    if (out.value && !confirm(replacing)) return;

    btn.disabled = true;
    status.textContent = "Creating\u2026";
    let d;
    try {
      const r = await fetch("/api/account/api-key", { method: "POST" });
      d = await r.json();
      if (!r.ok) {
        status.textContent = d.error || "Could not create a key.";
        btn.disabled = false;
        return;
      }
    } catch (_) {
      status.textContent = "Could not reach the server.";
      btn.disabled = false;
      return;
    }
    out.value = d.api_key || "";
    out.hidden = false;
    if (base && d.base_url) {
      base.textContent = "Server URL: " + d.base_url;
      base.hidden = false;
    }
    status.textContent =
      "Copy it now \u2014 it is not shown again. Click the box to select it.";
    btn.textContent = "Create a new key";
    btn.disabled = false;
  });
}
