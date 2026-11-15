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
  // Read by the workspace pane (artifacts.js): what the block is, and
  // the file it says it is, so a revision lands as a new version of it.
  block.dataset.lang = lang;
  block.dataset.filename = readFilename(content) || "";
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
    previewBtn.onclick = () => {
      if (!openInWorkspace(previewBtn, "preview")) openPreview(content);
    };
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

/* Ask the workspace pane to show this block. It answers by cancelling
   the event; when nothing does (a short snippet that is not an
   artifact, or a block outside the chat) the caller falls back to the
   overlay preview. An event rather than an import so the pane can use
   this module without the two depending on each other. */
function openInWorkspace(el, view) {
  const evt = new CustomEvent("rg:open-artifact", {
    bubbles: true, cancelable: true, detail: { view },
  });
  return !el.dispatchEvent(evt);
}

/* A finished render, announced from the bubble. The workspace pane
   collects artifacts from it; `live` is true for a reply that has just
   streamed in, false for one replayed from history. */
function announce(bubble, live) {
  bubble.dispatchEvent(new CustomEvent("rg:rendered", {
    bubbles: true, detail: { live: !!live },
  }));
}

/* ---- projects -----------------------------------------------------
   Adjacent code blocks are one program, so they are shown as one panel
   with file tabs instead of a stack of separate blocks. See the note in
   the patch that added this. */

const FILENAME_BY_LANG = {
  python: "app.py", html: "index.html", css: "style.css",
  javascript: "script.js", js: "script.js", json: "data.json",
  sql: "schema.sql", bash: "run.sh", sh: "run.sh",
};

/* A filename the model put on the first line, in whichever comment
   syntax the language uses. Returns null when the line is ordinary code,
   so a real first line is never eaten by mistake. */
function readFilename(content) {
  const first = (content.split("\n")[0] || "").trim();
  // A path of name.ext parts, a dotfile (.env.example, .dockerignore),
  // or one of the build files that have no extension at all.
  const m = first.match(
    /^(?:#|\/\/|<!--|\/\*)\s*((?:[\w-]+\/)*(?:\.?[\w-]+(?:\.[\w-]+)+|\.[\w-]+|Dockerfile|Makefile|Procfile))\s*(?:-->|\*\/)?$/i);
  return m ? m[1] : null;
}

function stripFirstLine(content) {
  const i = content.indexOf("\n");
  return i === -1 ? "" : content.slice(i + 1);
}

/* Turn a run of .code-block siblings into one tabbed panel. */
function groupIntoProject(blocks) {
  const panel = document.createElement("div");
  panel.className = "code-project";

  const tabs = document.createElement("div");
  tabs.className = "code-project-tabs";
  const actions = document.createElement("div");
  actions.className = "code-project-actions";
  const body = document.createElement("div");
  body.className = "code-project-body";

  const files = [];
  blocks.forEach((block, i) => {
    const pre = block.querySelector("pre");
    const code = pre && pre.querySelector("code");
    if (!code) return;
    const lang =
      (code.className.match(/language-([\w+-]+)/) || [])[1] || "plaintext";
    let content = code.textContent || "";
    let name = readFilename(content);
    if (name) {
      content = stripFirstLine(content);
      code.textContent = content;
      if (window.hljs) {
        try { hljs.highlightElement(code); } catch (_) { /* plain is fine */ }
      }
    } else {
      name = FILENAME_BY_LANG[lang] || (lang + ".txt");
      // Two files that infer to the same name would give two identical
      // tabs, which is worse than a slightly ugly one.
      if (files.some((f) => f.name === name)) name = i + 1 + "-" + name;
    }
    block.dataset.filename = name;
    files.push({ name, content, block });

    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "code-tab";
    tab.textContent = name;
    tab.onclick = () => show(i);
    tabs.appendChild(tab);

    // The per-block header is redundant now: the tab says the filename
    // and the panel carries the buttons.
    const header = block.querySelector(".code-block-header");
    if (header) header.remove();
    body.appendChild(block);
  });

  if (files.length < 2) return null;

  function show(i) {
    files.forEach((f, j) => {
      f.block.hidden = j !== i;
      tabs.children[j].classList.toggle("is-active", j === i);
    });
    copyBtn.textContent = "Copy " + files[i].name;
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(files[i].content);
      copyBtn.textContent = "Copied!";
      setTimeout(() => (copyBtn.textContent = "Copy " + files[i].name), 1500);
    };
  }

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "copy-btn";
  actions.appendChild(copyBtn);

  // Every file at once, as text with filename separators. Not a zip:
  // building one in the browser needs a library, and what people do with
  // this is paste it into an editor.
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "copy-btn";
  allBtn.textContent = "Copy all";
  allBtn.onclick = () => {
    navigator.clipboard.writeText(
      files.map((f) => "===== " + f.name + " =====\n" + f.content).join("\n\n"));
    allBtn.textContent = "Copied all!";
    setTimeout(() => (allBtn.textContent = "Copy all"), 1500);
  };
  actions.appendChild(allBtn);

  // An html file in the set can still be looked at rather than read.
  const page = files.find((f) => /\.html?$/i.test(f.name)
                                 && /<(!doctype|html)/i.test(f.content));
  if (page) {
    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "copy-btn";
    previewBtn.textContent = "Preview";
    previewBtn.onclick = () => {
      if (!openInWorkspace(page.block, "preview")) openPreview(page.content);
    };
    actions.appendChild(previewBtn);
  }

  const bar = document.createElement("div");
  bar.className = "code-project-bar";
  bar.appendChild(tabs);
  bar.appendChild(actions);
  panel.appendChild(bar);
  panel.appendChild(body);
  show(0);
  return panel;
}

/* Find runs of adjacent .code-block elements and fold each into a
   project panel. Called once, after every block is decorated. */
export function groupCodeBlocks(bubble) {
  const blocks = [...bubble.querySelectorAll(":scope > .code-block")];
  if (blocks.length < 2) return;
  let run = [];
  const flush = () => {
    if (run.length >= 2) {
      // A placeholder, because groupIntoProject moves every block into
      // the panel it builds: asking run[0] to insert the panel beside
      // itself afterwards is asking a detached node to do it, which
      // silently does nothing and loses the blocks entirely.
      const slot = document.createElement("div");
      run[0].before(slot);
      const panel = groupIntoProject(run);
      if (panel) {
        slot.replaceWith(panel);
      } else {
        // Nothing was grouped, so put the blocks back where they were.
        run.forEach((b) => slot.before(b));
        slot.remove();
      }
    }
    run = [];
  };
  blocks.forEach((b) => {
    if (run.length && run[run.length - 1].nextElementSibling === b) {
      run.push(b);
    } else {
      flush();
      run = [b];
    }
  });
  flush();
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
/* A page sent without its fence. Asked for a website, a model will
   sometimes reply with the bare document - "<!doctype html>..." and no
   ```html around it. Unfenced, that markup is sanitised and drawn as
   stray elements in the bubble, and the workspace pane never sees a
   page. When a reply has no fences at all, the document part of it is
   fenced here - to the closing </html>, or to the end while it is
   still streaming. */
function fenceBareHtml(text) {
  if (!text || text.includes("```")) return text;
  const start = text.search(/<!doctype html|<html[\s>]/i);
  if (start === -1) return text;
  const close = text.toLowerCase().indexOf("</html>", start);
  const end = close === -1 ? text.length : close + "</html>".length;
  return text.slice(0, start) + "```html\n" + text.slice(start, end).trim()
    + "\n```" + text.slice(end);
}

export function renderMarkdown(bubble, text, { final = true, live = false } = {}) {
  text = fenceBareHtml(text);
  try {
    bubble.dataset.md = text;      // Save wants the source, not the DOM
  } catch (_) { /* a bubble without a dataset still renders */ }
  const L = libs();
  if (!L) {
    renderPlain(bubble, text, final);
    if (final) { renderMath(bubble); announce(bubble, live); }
    return;
  }
  const { text: safeText, held } = protectMath(text);
  let html;
  try {
    html = L.marked.parse(safeText);
  } catch (_) {
    renderPlain(bubble, text, final);
    if (final) { renderMath(bubble); announce(bubble, live); }
    return;
  }
  html = restoreMath(L.purify.sanitize(html, { USE_PROFILES: { html: true } }), held);
  bubble.innerHTML = html;
  if (!final) return;
  bubble.querySelectorAll("pre").forEach(decorateCodeBlock);
  groupCodeBlocks(bubble);
  renderMath(bubble);
  announce(bubble, live);
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
    renderMarkdown(this.bubble, this.text, { final, live: true });
  }

  finish(text) {
    if (text !== undefined) this.text = text;
    if (this.frame) { clearTimeout(this.frame); this.frame = 0; }
    this.done = true;
    this.draw(true);
  }
}
