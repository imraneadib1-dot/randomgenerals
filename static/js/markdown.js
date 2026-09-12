import { renderMath } from "./bays.js";
import { downloadHtml, isPreviewable, openPreview } from "./preview.js";

/* ----------------------------------------------------------------
   Markdown, rendered.

   The model has always been asked for markdown - headings, lists,
   tables, fenced code - and the page drew everything except the fences
   as escaped text with pre-wrap. "## Steps" arrived as two hashes and a
   word; a table arrived as pipes. The Save button's own comment said it
   kept "the headings, the tables and the LaTeX" - in the file, where
   nothing on screen had shown them.

   marked parses, DOMPurify sanitises, and the result is written to the
   bubble. Maths is lifted out before parsing and put back after, so a
   subscript's underscore cannot become emphasis and a fraction's
   backslashes survive; KaTeX then typesets it in place, as before.

   Both libraries come from the same CDN as highlight.js and are
   optional: with either missing (an ad blocker, an offline reload) the
   old fence-and-inline-code renderer below draws the reply, so a
   blocked script degrades the typography and never the answer.
   ---------------------------------------------------------------- */

let configured = false;

function libs() {
  const marked = window.marked;
  const purify = window.DOMPurify;
  if (!marked || !purify || typeof marked.parse !== "function") return null;
  if (!configured) {
    marked.use({ gfm: true, breaks: false });
    // Links open in a new tab and cannot reach back to this one. Set
    // after sanitising, so DOMPurify cannot strip what it has not seen.
    purify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
    configured = true;
  }
  return { marked, purify };
}

/* ---- maths placeholders ----------------------------------------------
   $$...$$ and \[...\] display, \(...\) and $...$ inline. Inline $ is
   only maths when it hugs its content ($x^2$, not $ 5 or 5$) - the
   currency case is common in ordinary prose. Fenced code is skipped
   entirely: a shell line with $PATH is not a formula, and KaTeX ignores
   <pre> anyway. */
const MATH_RE =
  /(\$\$[\s\S]+?\$\$)|(\\\[[\s\S]+?\\\])|(\\\([\s\S]+?\\\))|(\$(?=\S)(?:[^$\n\\]|\\.)+?(?<=\S)\$(?![0-9]))/g;
const FENCE_RE = /```[\s\S]*?(?:```|$)/g;

function protectMath(text) {
  const held = [];
  const out = [];
  let last = 0;
  let m;
  FENCE_RE.lastIndex = 0;
  while ((m = FENCE_RE.exec(text)) !== null) {
    out.push(lift(text.slice(last, m.index), held));
    out.push(m[0]);                       // a fence, untouched
    last = m.index + m[0].length;
  }
  out.push(lift(text.slice(last), held));
  return { text: out.join(""), held };
}

function lift(segment, held) {
  return segment.replace(MATH_RE, (src) => {
    held.push(src);
    // Letters only: marked leaves a bare word alone, where a symbol or
    // an underscore would be interpreted.
    return "MATHHOLD" + (held.length - 1) + "ENDHOLD";
  });
}

function restoreMath(html, held) {
  if (!held.length) return html;
  return html.replace(/MATHHOLD(\d+)ENDHOLD/g, (all, i) => {
    const src = held[Number(i)];
    return src === undefined ? all : escapeHtml(src);
  });
}

function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ---- code blocks ------------------------------------------------------
   The same header the old renderer drew - language, Copy, and for a
   self-contained page Preview and Save .html - now wrapped around what
   marked produced. Built once per block, at the final render. */
export function decorateCodeBlock(pre) {
  const code = pre.querySelector("code");
  if (!code || pre.parentElement?.classList.contains("code-block")) return;
  const lang = (code.className.match(/language-([\w+-]+)/) || [])[1] || "plaintext";
  const content = code.textContent || "";

  const block = document.createElement("div");
  block.className = "code-block";
  const header = document.createElement("div");
  header.className = "code-block-header";
  const langSpan = document.createElement("span");
  langSpan.textContent = lang;
  header.appendChild(langSpan);

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "copy-btn";
  copyBtn.textContent = "Copy";
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(content);
    copyBtn.textContent = "Copied!";
    setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
  };
  header.appendChild(copyBtn);

  // A page you can look at, not just read the source of. The model is
  // asked for one self-contained file precisely so this works.
  if (isPreviewable(lang, content)) {
    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "copy-btn";
    previewBtn.textContent = "Preview";
    previewBtn.onclick = () => openPreview(content);
    header.appendChild(previewBtn);
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "copy-btn";
    saveBtn.textContent = "Save .html";
    saveBtn.onclick = () => downloadHtml(content);
    header.appendChild(saveBtn);
  }

  pre.replaceWith(block);
  block.appendChild(header);
  block.appendChild(pre);
  if (window.hljs) {
    try { hljs.highlightElement(code); } catch (_) { /* plain is fine */ }
  }
}

/* ---- the fallback -----------------------------------------------------
   What the page drew before: fences as code blocks, `inline code`, and
   everything else escaped with pre-wrap. Used when marked or DOMPurify
   did not load. */
function renderPlain(bubble, text, final) {
  const fenceRegex = /```(\w*)\n([\s\S]*?)```/g;
  bubble.innerHTML = "";
  let lastIndex = 0;
  let match;
  const pushText = (chunk) => {
    const span = document.createElement("span");
    span.style.whiteSpace = "pre-wrap";
    span.innerHTML = escapeHtml(chunk).replace(
      /`([^`]+)`/g, '<span class="inline-code">$1</span>');
    bubble.appendChild(span);
  };
  while ((match = fenceRegex.exec(text)) !== null) {
    if (match.index > lastIndex) pushText(text.slice(lastIndex, match.index));
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    const lang = match[1] || "plaintext";
    if (lang !== "plaintext") code.className = "language-" + lang;
    code.textContent = match[2];
    pre.appendChild(code);
    bubble.appendChild(pre);
    if (final) decorateCodeBlock(pre);
    lastIndex = fenceRegex.lastIndex;
  }
  if (lastIndex < text.length) pushText(text.slice(lastIndex));
}

/**
 * Draw `text` into `bubble`.
 *
 * `final` is the difference between a frame of a streaming reply and
 * the finished one. While streaming, only the markdown is rendered -
 * no highlighting, no KaTeX - because each of those walks the whole
 * bubble and the old renderer ran both on every chunk, which made a
 * long reply cost O(n^2) in the tab it was arriving in. At the end,
 * once: code headers, highlighting, maths.
 */
export function renderMarkdown(bubble, text, { final = true } = {}) {
  try {
    bubble.dataset.md = text;      // Save wants the source, not the DOM
  } catch (_) { /* a bubble without a dataset still renders */ }
  const L = libs();
  if (!L) {
    renderPlain(bubble, text, final);
    if (final) renderMath(bubble);
    return;
  }
  const { text: safeText, held } = protectMath(text);
  let html;
  try {
    html = L.marked.parse(safeText);
  } catch (_) {
    renderPlain(bubble, text, final);
    if (final) renderMath(bubble);
    return;
  }
  html = restoreMath(L.purify.sanitize(html, { USE_PROFILES: { html: true } }), held);
  bubble.innerHTML = html;
  if (!final) return;
  bubble.querySelectorAll("pre").forEach(decorateCodeBlock);
  renderMath(bubble);
}

/**
 * Renders a streaming reply at most once per animation frame, and no
 * more often than every 80ms, however fast the chunks arrive; finish()
 * draws the last frame with the expensive passes.
 */
export class StreamRenderer {
  constructor(bubble) {
    this.bubble = bubble;
    this.text = "";
    this.frame = 0;
    this.drawnAt = 0;
    this.done = false;
  }

  /** The reply so far - the whole of it, not a delta. */
  update(text) {
    this.text = text;
    if (this.done || this.frame) return;
    const wait = Math.max(0, 80 - (performance.now() - this.drawnAt));
    this.frame = window.setTimeout(() => {
      this.frame = 0;
      window.requestAnimationFrame(() => this.draw(false));
    }, wait);
  }

  draw(final) {
    if (this.done && !final) return;
    this.drawnAt = performance.now();
    renderMarkdown(this.bubble, this.text, { final });
  }

  finish(text) {
    if (text !== undefined) this.text = text;
    if (this.frame) { clearTimeout(this.frame); this.frame = 0; }
    this.done = true;
    this.draw(true);
  }
}
