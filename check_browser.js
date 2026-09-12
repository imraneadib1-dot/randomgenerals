// Does the app actually run in a browser?
//
//     node check_browser.js            (the server must be up on :5000,
//                                       or pass a URL as the argument)
//
// Loads /app in a headless Edge/Chrome through playwright-core - no
// browser download; the one already on the machine is used - and
// records every console error and uncaught exception while it boots,
// switches bays, opens Settings, and sends a message. Nothing else in
// the checks executes the front-end at all: tsc proves the modules
// reference names that exist, and this proves they run.
//
// It is deliberately blunt. A ReferenceError at load is a blank app
// with one red line in a console nobody is watching, and that is the
// failure a module split can introduce in a hundred quiet ways.
const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright-core");

const URL = process.argv[2] || "http://127.0.0.1:5000/app";

const CANDIDATES = [
  process.env.BROWSER_PATH,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
  "/usr/bin/microsoft-edge",
].filter(Boolean);

const failures = [];
function check(label, ok, detail) {
  console.log("  %s %s%s", ok ? "ok  " : "FAIL", label, ok || !detail ? "" : "   (" + detail + ")");
  if (!ok) failures.push(label);
}

(async () => {
  const exe = CANDIDATES.find((p) => fs.existsSync(p));
  if (!exe) {
    console.log("  (skipped: no Chromium-based browser found; set BROWSER_PATH)");
    process.exit(0);
  }
  const browser = await chromium.launch({ executablePath: exe, headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const errors = [];
  const notes = [];
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // "Failed to load resource: ... 401" is the browser reporting an
    // HTTP status, not the app throwing. The Python checks cover what
    // the server answers; this one is for exceptions in the modules.
    if (/Failed to load resource/.test(m.text())) notes.push(m.text());
    else errors.push("console: " + m.text());
  });
  page.on("pageerror", (e) => errors.push("uncaught: " + e.message));
  page.on("requestfailed", (r) => {
    // A CDN that this sandbox cannot reach is not the app's fault.
    if (r.url().startsWith(URL.split("/app")[0])) errors.push("request failed: " + r.url());
  });

  console.log("== load ==");
  const res = await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  check("/app answers", res && res.ok(), res && String(res.status()));

  // Boot: the splash is removed once the first data load resolves.
  await page.waitForFunction(() => {
    const b = document.getElementById("bootScreen");
    return !b || b.hidden || getComputedStyle(b).display === "none"
      || getComputedStyle(b).opacity === "0" || !document.body.contains(b);
  }, null, { timeout: 20000 }).catch(() => {});
  const booted = await page.evaluate(() => {
    const b = document.getElementById("bootScreen");
    return !b || b.hidden || getComputedStyle(b).display === "none"
      || getComputedStyle(b).opacity === "0" || !document.body.contains(b);
  });
  check("the boot screen went away", booted);
  check("no errors while loading", errors.length === 0, errors.slice(0, 3).join(" | "));

  console.log("== the shell works ==");
  const modules = await page.evaluate(() => [...document.scripts]
    .filter((s) => s.type === "module" && s.src.includes("/static/js/")).length);
  check("the app is loaded as a module", modules === 1, String(modules));

  await page.click('[data-bay="code"]');
  await page.click('[data-bay="chat"]');
  check("switching bays raised nothing", errors.length === 0, errors.slice(-2).join(" | "));

  await page.click("#settingsBtn");
  const modalOpen = await page.evaluate(() => {
    const b = document.getElementById("settingsBackdrop");
    return b && !b.hidden;
  });
  check("settings opens", modalOpen);
  await page.keyboard.press("Escape");
  check("settings closes on Escape", await page.evaluate(() => document.getElementById("settingsBackdrop").hidden));

  console.log("== a message round-trips ==");
  await page.fill("#messageInput", "hello from the browser check");
  await page.press("#messageInput", "Enter");
  await page.waitForFunction(() => document.querySelectorAll(".msg.assistant .msg-bubble").length > 0,
    null, { timeout: 20000 }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector(".msg.streaming"),
    null, { timeout: 30000 }).catch(() => {});
  const bubbles = await page.evaluate(() => ({
    user: document.querySelectorAll(".msg.user").length,
    assistant: document.querySelectorAll(".msg.assistant").length,
    lastText: (document.querySelector(".msg.assistant:last-of-type .msg-bubble") || {}).textContent || "",
    lastKind: (document.querySelector(".msg.assistant:last-of-type") || { dataset: {} }).dataset.kind || "text",
  }));
  check("the question was drawn", bubbles.user === 1, String(bubbles.user));
  check("a reply bubble appeared", bubbles.assistant === 1, String(bubbles.assistant));
  check("with something in it", bubbles.lastText.trim().length > 0);
  console.log("    reply kind: %s, text: %s", bubbles.lastKind, JSON.stringify(bubbles.lastText.slice(0, 90)));
  check("no errors during the round-trip", errors.length === 0, errors.slice(-3).join(" | "));

  console.log("== the reply is rendered as markdown ==");
  const md = await page.evaluate(() => {
    const b = document.querySelector(".msg.assistant:last-of-type .msg-bubble");
    return {
      strong: !!b.querySelector("strong"),
      items: b.querySelectorAll("li").length,
      code: !!b.querySelector(".code-block .copy-btn"),
      highlighted: !!b.querySelector("code .hljs-built_in, code .hljs-keyword, code .hljs-string, code .hljs-number"),
      math: !!b.querySelector(".katex"),
      source: (b.dataset.md || "").includes("**Hello**"),
      libs: !!(window.marked && window.DOMPurify),
    };
  });
  if (!md.libs) {
    console.log("    (marked/DOMPurify did not load from the CDN here; the fallback renderer ran)");
  } else {
    check("bold is bold", md.strong);
    check("a list is a list", md.items === 2, String(md.items));
    check("a fence is a code block with its header", md.code);
    check("and highlighted once, at the end", md.highlighted);
    check("maths is typeset", md.math);
  }
  check("the markdown source is kept for Save", md.source);

  await page.waitForFunction(() => document.querySelectorAll(".thread-item").length > 0,
    null, { timeout: 10000 }).catch(() => {});
  const threads = await page.evaluate(() => document.querySelectorAll(".thread-item").length);
  check("the thread appeared in the sidebar", threads >= 1, String(threads));
  for (const n of [...new Set(notes)]) console.log("    note: %s", n);

  await browser.close();
  console.log("");
  if (failures.length) {
    console.log("%d FAILED: %s", failures.length, failures.join("; "));
    if (errors.length) console.log(errors.join("\n"));
    process.exit(1);
  }
  console.log("All checks passed.");
})().catch((e) => { console.error(e); process.exit(1); });
