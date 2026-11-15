// Does mermaid itself accept this diagram?
//
//     node finetune/validate.js pairs.jsonl
//
// Reads JSONL with a `mermaid` field on each line, prints one JSON
// object per line with {i, ok, error}. Exit code is the number of
// failures, capped at 250.
//
// WHY A BROWSER AND NOT A REGEX
//
// The only definition of "valid mermaid" that matters here is the one
// running in the visitor's page, which is mermaid 10.9.1 from the same
// CDN the app loads (templates/index.html). Its grammar has corners no
// checker of ours would reproduce - `class Queen {}` parses as a
// statement and then fails on an empty body; a label with an unquoted
// bracket fails; a duplicate node id silently merges two nodes. Every
// one of those is a diagram the model was happy with and the reader
// was not.
//
// So the grader is the renderer. mermaid.parse() throws on anything it
// cannot draw, which is exactly the question being asked, and one
// browser is started for the whole file rather than one per diagram.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const CDN = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js";
const CANDIDATES = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
  "/usr/bin/microsoft-edge",
].filter(Boolean);

(async () => {
  const file = process.argv[2];
  if (!file) {
    console.error("usage: node validate.js <pairs.jsonl>");
    process.exit(2);
  }
  const rows = fs.readFileSync(file, "utf8")
    .split("\n").map((l) => l.trim()).filter(Boolean)
    .map((l) => JSON.parse(l));

  const exe = CANDIDATES.find((p) => fs.existsSync(p));
  if (!exe) {
    console.error("no Chromium-based browser found; set BROWSER_PATH");
    process.exit(2);
  }
  const browser = await chromium.launch({ executablePath: exe, headless: true });
  const page = await browser.newPage();
  // about:blank cannot load a CDN script under the default CSP-less
  // origin rules in every build, so the page is served as real content.
  await page.setContent("<!doctype html><html><body></body></html>");
  await page.addScriptTag({ url: CDN });
  await page.waitForFunction(() => !!window.mermaid, null, { timeout: 30000 });
  await page.evaluate(() => {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
  });

  let failures = 0;
  for (let i = 0; i < rows.length; i++) {
    const src = rows[i].mermaid || "";
    // parse() is the same check the app's renderer makes before it
    // draws. It resolves true or throws with the grammar's own message,
    // which is worth keeping: it is what tells you WHICH rule the
    // model broke.
    const result = await page.evaluate(async (text) => {
      try {
        await window.mermaid.parse(text);
        return { ok: true, error: "" };
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e).slice(0, 300) };
      }
    }, src);
    if (!result.ok) failures++;
    process.stdout.write(JSON.stringify({ i, ok: result.ok, error: result.error }) + "\n");
  }

  await browser.close();
  process.exit(Math.min(250, failures));
})().catch((e) => { console.error(e); process.exit(2); });
