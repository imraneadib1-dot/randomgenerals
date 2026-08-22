/**
 * Preload - the only bridge between the renderer and anything
 * privileged.
 *
 * The renderer here is the same script.js that gets served to the public
 * web, so this surface is deliberately narrow and shaped: it exposes
 * *operations*, never raw modules. Nothing here hands back `require`,
 * `child_process`, `fs`, or an ipcRenderer whose channel the caller
 * chooses. A bug (or an injected script) in the renderer can therefore
 * only ever reach the specific validated handlers below.
 */
const { contextBridge, ipcRenderer } = require("electron");

/** Wrap a stream subscription so the caller gets an unsubscribe function
 *  and can never leak listeners across sessions. */
function subscribe(channel, handler) {
  const listener = (_event, payload) => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld("desktop", {
  /** True only inside the Electron build - the web deployment has no
   *  `window.desktop` at all, which is how the UI knows whether to show
   *  desktop-only features like the terminal panel. */
  isDesktop: true,
  platform: process.platform,

  terminal: {
    /** Start a command. Resolves with { sessionId } or { error }. */
    run: (command, options) =>
      ipcRenderer.invoke("terminal:run", { command, options }),
    /** Send SIGKILL to a running session. */
    kill: (sessionId) => ipcRenderer.invoke("terminal:kill", sessionId),
    /** Stream chunks: { sessionId, stream: "stdout"|"stderr", chunk } */
    onOutput: (handler) => subscribe("terminal:output", handler),
    /** Fired once per session: { sessionId, code, signal, timedOut } */
    onExit: (handler) => subscribe("terminal:exit", handler),
  },

  editor: {
    /** Open a path (file or folder) in VS Code. */
    openInVSCode: (targetPath) =>
      ipcRenderer.invoke("editor:open-vscode", targetPath),
    /** Native folder picker - returns a path string or null. */
    pickFolder: () => ipcRenderer.invoke("editor:pick-folder"),
    /** Whether the `code` CLI is actually on PATH, so the UI can hide
     *  the button rather than offer an action that will fail. */
    hasVSCode: () => ipcRenderer.invoke("editor:has-vscode"),
  },

  license: {
    /** -> { tier: "free"|"pro", email?, expiresAt?, valid } */
    status: () => ipcRenderer.invoke("license:status"),
    /** Validate a key with the server and, if good, persist it encrypted. */
    activate: (key) => ipcRenderer.invoke("license:activate", key),
    /** Remove the stored licence from this machine. */
    deactivate: () => ipcRenderer.invoke("license:deactivate"),
  },
});
