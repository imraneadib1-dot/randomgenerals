/**
 * Electron main process.
 *
 * Wraps the existing Flask app rather than reimplementing it: the Python
 * server is spawned as a child process on a loopback-only port, and the
 * BrowserWindow simply loads it. That keeps one codebase serving both
 * the web deployment and the desktop build.
 *
 * The security posture is the important part of this file. The renderer
 * is the SAME HTML/JS that gets served publicly over the web, so it must
 * be treated as untrusted here too: nodeIntegration off, contextIsolation
 * on, and the only privileged surface is the narrow, validated IPC in
 * src/ipc/. Terminal execution exists ONLY over that IPC - it is never
 * reachable through the Flask HTTP surface, so nothing about this file
 * makes the public web deployment more dangerous.
 */
const { app, BrowserWindow, shell, dialog, ipcMain } = require("electron");
const path = require("node:path");

const { startBackend, stopBackend } = require("./backend");
const { registerTerminalHandlers, killAllSessions } = require("./ipc/terminal");
const { registerEditorHandlers } = require("./ipc/editor");
const { registerLicenseHandlers } = require("./ipc/license");

const IS_DEV = process.env.RG_DEV === "1";

/** @type {BrowserWindow | null} */
let mainWindow = null;

function createWindow(startUrl) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    show: false,
    backgroundColor: "#05080f", // matches the app's dark ground, so there
    // is no white flash before first paint
    title: "RandomGenerals AI",
    icon: path.join(__dirname, "..", "resources", "icon.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      // All three of these are the defaults on modern Electron, but they
      // are the entire reason loading remote-ish content is safe here,
      // so they are set explicitly rather than left implicit.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      // The renderer only ever loads http://127.0.0.1 - allowing it to
      // run insecure content or disable web security would serve no
      // purpose and remove a backstop.
      allowRunningInsecureContent: false,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.loadURL(startUrl);

  // Anything that is not our own loopback origin opens in the user's real
  // browser instead of inside the app. Without this, a link in a chat
  // reply (or a Stripe redirect) would navigate the app window itself to
  // an external site, which then runs with this window's preload attached.
  const isInternal = (url) => {
    try {
      const u = new URL(url);
      return u.hostname === "127.0.0.1" || u.hostname === "localhost";
    } catch {
      return false;
    }
  };

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!isInternal(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isInternal(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  return mainWindow;
}

/** Broadcast helper handed to the IPC modules so they can stream output
 *  back without holding their own window reference. */
function sendToRenderer(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

// Single-instance lock: a second launch focuses the existing window
// instead of starting a second Flask child on an already-bound port.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    registerTerminalHandlers(ipcMain, sendToRenderer);
    registerEditorHandlers(ipcMain);
    registerLicenseHandlers(ipcMain);

    try {
      const { url } = await startBackend({ dev: IS_DEV });
      createWindow(url);
    } catch (err) {
      // A failed backend start is the single most likely first-run
      // problem (no Python, no dependencies, port in use), so it gets a
      // real dialog explaining what to do rather than a blank window.
      dialog.showErrorBox(
        "RandomGenerals AI could not start",
        `${err.message}\n\n` +
          "This app needs Python 3.10+ and Ollama installed.\n" +
          "Run the first-run setup, or see the README for details.",
      );
      app.quit();
    }
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && mainWindow === null) {
      startBackend({ dev: IS_DEV }).then(({ url }) => createWindow(url));
    }
  });

  // Child processes are not cleaned up for us. Without this, quitting the
  // app leaves an orphaned Flask server (and any running terminal
  // command) holding its port until the machine reboots - the exact
  // zombie-process problem this project has hit repeatedly in dev.
  app.on("before-quit", () => {
    killAllSessions();
    stopBackend();
  });
  process.on("exit", stopBackend);
}
