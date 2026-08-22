/**
 * Starts the Flask app as a child process and waits until it answers.
 *
 * Why the Python server is spawned rather than bundled: the app depends
 * on Ollama (~16GB of models here), a PyTorch/diffusers stack (~7GB of
 * weights), and Python itself. GitHub Releases rejects any single file
 * over 2GB, so shipping that inside the installer is not merely
 * impractical - it cannot be published on the distribution channel this
 * project targets. The installer stays ~80MB and provisions dependencies
 * on first run instead.
 */
const { spawn } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");
const fs = require("node:fs");
const { app } = require("electron");
const { setBackendUrl } = require("./entitlements");

/** @type {import("node:child_process").ChildProcess | null} */
let backendProcess = null;

/** Ask the OS for a free port rather than hardcoding 5000. Two installs,
 *  or a dev server already on 5000, would otherwise collide - the exact
 *  "port already in use" failure this project keeps hitting by hand. */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Poll until Flask answers, so the window never loads a dead URL. */
async function waitForServer(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (res.ok) return true;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

/** Locate the app.py to run: the repo root in dev, the unpacked
 *  resources directory in a packaged build. */
function resolveAppRoot(dev) {
  if (dev) return path.resolve(__dirname, "..", "..");
  return path.join(process.resourcesPath, "server");
}

function resolvePython(appRoot) {
  // Prefer a venv shipped/created next to the server, then fall back to
  // whatever Python is on PATH.
  const candidates =
    process.platform === "win32"
      ? [
          path.join(appRoot, ".venv", "Scripts", "python.exe"),
          path.join(appRoot, "venv", "Scripts", "python.exe"),
          "python",
        ]
      : [
          path.join(appRoot, ".venv", "bin", "python"),
          path.join(appRoot, "venv", "bin", "python"),
          "python3",
        ];
  for (const c of candidates) {
    if (c === "python" || c === "python3" || fs.existsSync(c)) return c;
  }
  return candidates[candidates.length - 1];
}

async function startBackend({ dev = false } = {}) {
  const appRoot = resolveAppRoot(dev);
  const entry = path.join(appRoot, "app.py");

  if (!fs.existsSync(entry)) {
    throw new Error(`Couldn't find the server at ${entry}`);
  }

  const port = await findFreePort();
  const url = `http://127.0.0.1:${port}`;
  const python = resolvePython(appRoot);

  backendProcess = spawn(python, ["app.py"], {
    cwd: appRoot,
    windowsHide: true,
    env: {
      ...process.env,
      PORT: String(port),
      // Debug off always: Werkzeug's debugger is an interactive Python
      // console on error pages. Harmless on a dev laptop, a remote code
      // execution hole the moment anything else can reach the port.
      APP_DEBUG: "0",
      // Desktop builds must never grant Pro without payment.
      ALLOW_MOCK_UPGRADE: "0",
      PYTHONUNBUFFERED: "1",
    },
  });

  const logPath = path.join(app.getPath("userData"), "backend.log");
  const logStream = fs.createWriteStream(logPath, { flags: "a" });
  backendProcess.stdout?.pipe(logStream);
  backendProcess.stderr?.pipe(logStream);

  backendProcess.on("error", (err) => {
    logStream.write(`\n[spawn error] ${err.message}\n`);
  });

  const ready = await waitForServer(url);
  if (!ready) {
    stopBackend();
    throw new Error(
      `The local server didn't start within 60s.\nLog: ${logPath}`,
    );
  }

  setBackendUrl(url);
  return { url, port, logPath };
}

function stopBackend() {
  if (!backendProcess) return;
  try {
    if (process.platform === "win32") {
      // Flask spawns a child of its own; killing only the parent leaves
      // the actual listener holding the port. /T kills the whole tree.
      spawn("taskkill", ["/pid", String(backendProcess.pid), "/f", "/t"], {
        windowsHide: true,
      });
    } else {
      backendProcess.kill("SIGTERM");
    }
  } catch {
    /* best effort */
  }
  backendProcess = null;
}

module.exports = { startBackend, stopBackend };
