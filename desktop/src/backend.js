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

/** Where python lives inside a venv, per platform. */
function venvPython(venvDir) {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

/** The app's own venv, kept in userData rather than beside the server.
 *
 *  Two reasons. It survives an app update, which replaces the resources
 *  directory wholesale; and userData is writable on every platform,
 *  where the install directory may not be. */
function managedVenv() {
  return path.join(app.getPath("userData"), "venv");
}

function resolvePython(appRoot) {
  // Prefer the venv this app manages, then one shipped or created
  // beside the server, then whatever Python is on PATH.
  const candidates = [
    venvPython(managedVenv()),
    venvPython(path.join(appRoot, ".venv")),
    venvPython(path.join(appRoot, "venv")),
    process.platform === "win32" ? "python" : "python3",
  ];
  for (const c of candidates) {
    if (!c.includes(path.sep) || fs.existsSync(c)) return c;
  }
  return candidates[candidates.length - 1];
}

/**
 * Settings for this install, from a .env beside the app's own data.
 *
 * WHY NOT THE .env IN THE SERVER FOLDER
 *
 * That folder is part of the package: .dockerignore and the
 * electron-builder filter both exclude .env from the build precisely so
 * nobody's keys end up inside a file other people download, and an app
 * update replaces the whole directory anyway - taking any settings with
 * it.
 *
 * userData is the opposite on both counts. It is never shipped, and it
 * survives updates. So this is where a GROQ_API_KEY belongs on a
 * desktop install: present on the machine that needs it, absent from
 * everything that leaves it.
 *
 * Format is a plain .env - KEY=value per line, # for comments - because
 * that is what the file is called and guessing otherwise would be a
 * surprise.
 */
function readUserEnv() {
  const file = path.join(app.getPath("userData"), ".env");
  const out = {};
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return out; // No file is the normal state, not an error.
  }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    // Strip one layer of matching quotes, which people add out of habit
    // and which would otherwise become part of the key.
    if (value.length > 1 &&
        ((value.startsWith('"') && value.endsWith('"')) ||
         (value.startsWith("'") && value.endsWith("'")))) {
      value = value.slice(1, -1);
    }
    if (key) out[key] = value;
  }
  return out;
}

/** Run a command to completion, streaming its output to `onLine`. */
function run(cmd, args, { cwd, onLine } = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd, windowsHide: true });
    let tail = "";
    const take = (buf) => {
      tail = (tail + buf.toString()).slice(-4000);
      if (onLine) {
        buf
          .toString()
          .split(/\r?\n/)
          .filter(Boolean)
          .forEach((l) => onLine(l));
      }
    };
    child.stdout?.on("data", take);
    child.stderr?.on("data", take);
    child.on("error", (err) => resolve({ code: -1, output: err.message }));
    child.on("close", (code) => resolve({ code, output: tail }));
  });
}

/**
 * Make sure there is a Python that can actually run the server.
 *
 * THIS DID NOT EXIST, and the header of this file claimed it did -
 * "provisions dependencies on first run instead". Nothing provisioned
 * anything. resolvePython() fell through to `python` on PATH and the
 * server was spawned against whatever that happened to be, so on any
 * machine without Flask already installed the app died with "the local
 * server didn't start within 60s" and an error dialog telling the user
 * to run a first-run setup that was never written.
 *
 * The install is deliberately requirements-desktop.txt, not
 * requirements.txt: the latter pins torch and friends, ~2.3GB, for a
 * local image model that a laptop without a GPU cannot use anyway.
 *
 * -> { ok, python, reason }
 */
async function ensureDependencies(appRoot, onStatus = () => {}) {
  const venv = managedVenv();
  const py = venvPython(venv);
  const reqs = path.join(appRoot, "requirements-desktop.txt");
  // Keyed on the requirements file's contents, so upgrading the app
  // reinstalls only when the dependency list actually changed.
  const stamp = path.join(venv, ".installed-from");
  const want = fs.existsSync(reqs) ? fs.readFileSync(reqs, "utf8") : "";

  if (fs.existsSync(py) && fs.existsSync(stamp)) {
    try {
      if (fs.readFileSync(stamp, "utf8") === want) {
        return { ok: true, python: py };
      }
    } catch {
      /* fall through and reinstall */
    }
  }

  const base = process.platform === "win32" ? "python" : "python3";
  onStatus("Looking for Python…");
  const probe = await run(base, ["--version"]);
  if (probe.code !== 0) {
    return {
      ok: false,
      reason:
        "Python 3.10 or newer is required and was not found.\n\n" +
        "Install it from python.org, making sure to tick " +
        '"Add Python to PATH", then start this app again.',
    };
  }
  onStatus(`Found ${probe.output.trim() || "Python"}.`);

  if (!fs.existsSync(py)) {
    onStatus("Creating a private Python environment…");
    const made = await run(base, ["-m", "venv", venv]);
    if (made.code !== 0 || !fs.existsSync(py)) {
      return {
        ok: false,
        reason: `Could not create the Python environment.\n\n${made.output}`,
      };
    }
  }

  onStatus("Installing dependencies. This happens once and takes a minute…");
  const install = await run(
    py,
    ["-m", "pip", "install", "--disable-pip-version-check", "-r", reqs],
    { onLine: (l) => onStatus(l.slice(0, 120)) },
  );
  if (install.code !== 0) {
    return {
      ok: false,
      reason: `Installing dependencies failed.\n\n${install.output}`,
    };
  }

  try {
    fs.writeFileSync(stamp, want, "utf8");
  } catch {
    // Only costs a redundant reinstall next launch.
  }
  onStatus("Ready.");
  return { ok: true, python: py };
}

async function startBackend({ dev = false, onStatus = () => {} } = {}) {
  const appRoot = resolveAppRoot(dev);
  const entry = path.join(appRoot, "app.py");

  if (!fs.existsSync(entry)) {
    throw new Error(`Couldn't find the server at ${entry}`);
  }

  // In development the repo's own venv and PATH are already set up, and
  // building a second environment inside userData would only shadow it.
  let python = resolvePython(appRoot);
  if (!dev) {
    const ready = await ensureDependencies(appRoot, onStatus);
    if (!ready.ok) throw new Error(ready.reason);
    python = ready.python;
  }

  const port = await findFreePort();
  const url = `http://127.0.0.1:${port}`;

  backendProcess = spawn(python, ["app.py"], {
    cwd: appRoot,
    windowsHide: true,
    env: {
      ...process.env,
      // Before the fixed values below, so this cannot be used to switch
      // debug back on or grant itself Pro - those two are the reason
      // the order matters rather than being arbitrary.
      ...readUserEnv(),
      PORT: String(port),
      // Tells the server it is the desktop build, so /api/auth/me and
      // /api/plans can say so and the UI can send account and billing
      // work to the website - the two things a random loopback port
      // cannot do. See IS_DESKTOP in app.py.
      RG_DESKTOP: "1",
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

module.exports = { startBackend, stopBackend, ensureDependencies };
