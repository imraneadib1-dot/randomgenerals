/* ----------------------------------------------------------------
   HTML preview
   ----------------------------------------------------------------
   Renders a generated page in a sandboxed iframe.

   srcdoc plus a sandbox that grants scripts but NOT same-origin. That
   combination is what makes this safe to offer: the page runs, so a
   generated site with tabs or a menu actually works, but it is in an
   opaque origin - it cannot read this document, cannot touch cookies or
   localStorage, and cannot call the API with the visitor's session.

   allow-scripts together with allow-same-origin would undo all of that,
   which is why they are never both listed.
   ---------------------------------------------------------------- */
export function isPreviewable(lang, content) {
  if (!content) return false;
  const l = (lang || "").toLowerCase();
  if (l !== "html" && l !== "htm") return false;
  // A fragment is not a page. Previewing one shows a bare line of text
  // on white and looks broken, so the button only appears for something
  // that is actually a document.
  return /<html[\s>]|<!doctype html/i.test(content);
}

export function downloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on a timer rather than immediately: Chrome cancels an
  // in-flight download if the blob URL is released too early.
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

export function downloadHtml(content) {
  downloadBlob(content, "page.html", "text/html");
}

/** A filename from the note's own first heading, so a folder of these
 *  is readable without opening them. */
export function noteFilename(md) {
  const heading = (md.match(/^\s*#{1,3}\s+(.+)$/m) || [])[1];
  const firstLine = (md.trim().split("\n")[0] || "").replace(/[#*_`>]/g, "");
  const base = (heading || firstLine || "note")
    .replace(/\$+[^$]*\$+/g, "")
    .replace(/[\\/:*?"<>|]/g, "")
    .trim()
    .slice(0, 60)
    .replace(/\s+/g, "-")
    .toLowerCase();
  return (base || "note") + ".md";
}

export function openPreview(content) {
  const existing = document.getElementById("htmlPreview");
  if (existing) existing.remove();

  const wrap = document.createElement("div");
  wrap.className = "html-preview";
  wrap.id = "htmlPreview";

  const bar = document.createElement("div");
  bar.className = "html-preview-bar";

  const title = document.createElement("span");
  title.textContent = "Preview";
  bar.appendChild(title);

  const spacer = document.createElement("span");
  spacer.style.flex = "1";
  bar.appendChild(spacer);

  const openBtn = document.createElement("button");
  openBtn.className = "copy-btn";
  openBtn.textContent = "Open in tab";
  openBtn.onclick = () => {
    const blob = new Blob([content], { type: "text/html" });
    window.open(URL.createObjectURL(blob), "_blank", "noopener");
  };
  bar.appendChild(openBtn);

  const closeBtn = document.createElement("button");
  closeBtn.className = "copy-btn";
  closeBtn.textContent = "Close";
  closeBtn.onclick = () => wrap.remove();
  bar.appendChild(closeBtn);

  const frame = document.createElement("iframe");
  frame.className = "html-preview-frame";
  frame.setAttribute("sandbox", "allow-scripts allow-forms allow-popups");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.srcdoc = content;

  wrap.appendChild(bar);
  wrap.appendChild(frame);
  document.body.appendChild(wrap);

  const onKey = (e) => {
    if (e.key === "Escape") {
      wrap.remove();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountPreview() {
  // nothing ran at load in this section
}
