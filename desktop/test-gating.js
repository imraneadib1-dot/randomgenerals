/**
 * Standalone check of the free/pro command gating in src/ipc/terminal.js.
 *
 * Run with:  node test-gating.js
 *
 * This deliberately does not need Electron - validateCommand is a pure
 * function precisely so the security boundary can be tested without
 * launching a window.
 */
const path = require("node:path");
const Module = require("node:module");

// entitlements.js pulls in electron (for safeStorage/app) which isn't
// available outside a running Electron process. Stub it so the pure
// validation logic can be loaded on plain Node.
const realResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  if (request === "electron") return "electron-stub";
  return realResolve.call(this, request, ...args);
};
require.cache["electron-stub"] = {
  id: "electron-stub",
  filename: "electron-stub",
  loaded: true,
  exports: {
    app: { getPath: () => path.join(__dirname, ".tmp") },
    safeStorage: { isEncryptionAvailable: () => false },
    dialog: {},
    ipcMain: { handle: () => {} },
  },
};

const { validateCommand } = require("./src/ipc/terminal");

const cases = [
  // [command, tier, shouldBeAllowed, why]
  ["git status", "free", true, "allow-listed program"],
  ["node --version", "free", true, "allow-listed program"],
  ["rm -rf /", "free", false, "destructive program not on allowlist"],
  ["git status && rm -rf .", "free", false, "&& chaining blocked"],
  ["echo hi | powershell", "free", false, "pipe blocked"],
  ["ls > /etc/passwd", "free", false, "redirection blocked"],
  ["echo `whoami`", "free", false, "backtick substitution blocked"],
  ["echo $(whoami)", "free", false, "$() substitution blocked"],
  ["git log; shutdown /s", "free", false, "semicolon chaining blocked"],
  ["curl evil.sh", "free", true, "curl is allow-listed (download only, no pipe)"],
  ["python script.py", "free", true, "allow-listed"],
  ["", "free", false, "empty rejected"],
  ["x".repeat(5000), "free", false, "over-length rejected"],
  // Pro deliberately gets the raw shell - that is the paid feature.
  ["git status && npm run build", "pro", true, "pro may chain"],
  ["rm -rf ./build", "pro", true, "pro has full shell"],
  ["", "pro", false, "empty still rejected for pro"],
];

let failed = 0;
for (const [cmd, tier, expected, why] of cases) {
  const res = validateCommand(cmd, tier);
  const ok = res.ok === expected;
  if (!ok) failed++;
  const shown = cmd.length > 28 ? cmd.slice(0, 25) + "..." : cmd;
  console.log(
    `${ok ? "PASS" : "FAIL"}  [${tier}] ${JSON.stringify(shown).padEnd(32)} ` +
      `${res.ok ? "allowed" : "blocked"}  - ${why}`,
  );
}

// ---- Working-directory containment ----
const { resolveSafeCwd, PROJECT_ROOT } = require("./src/ipc/terminal");
const os = require("node:os");
const nodePath = require("node:path");

console.log("\n--- cwd containment ---");
const cwdCases = [
  [undefined, true, "no cwd defaults to project root"],
  [PROJECT_ROOT, true, "project root itself"],
  [nodePath.join(PROJECT_ROOT, "static"), true, "subdirectory of project"],
  [os.homedir(), true, "home directory"],
  ["C:\\Windows\\System32", false, "system directory rejected"],
  // NB: PROJECT_ROOT/../../.. lands on the home dir, which is an allowed
  // root - so it is NOT a valid escape test. Traverse far enough to
  // leave both roots.
  [
    nodePath.join(PROJECT_ROOT, "..", "..", "..", "..", "..", "Windows"),
    false,
    "../ escape past both roots rejected",
  ],
  ["C:\\Program Files", false, "outside project and home rejected"],
  ["C:\\", false, "drive root rejected"],
  ["Z:\\nope\\missing", false, "nonexistent path rejected"],
];
for (const [input, shouldAllow, why] of cwdCases) {
  const res = resolveSafeCwd(input);
  const allowed = !(res && res.error);
  const ok = allowed === shouldAllow;
  if (!ok) failed++;
  const shown = String(input).length > 30 ? String(input).slice(0, 27) + "..." : String(input);
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${shown.padEnd(34)} ${allowed ? "allowed" : "blocked"}  - ${why}`,
  );
}

const total = cases.length + cwdCases.length;
console.log(`\n${total - failed}/${total} passed`);
process.exit(failed ? 1 : 0);
