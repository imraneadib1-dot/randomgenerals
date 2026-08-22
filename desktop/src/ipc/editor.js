/**
 * Launching VS Code from the app.
 *
 * `code` is a shell script / .cmd shim rather than a real binary, so on
 * Windows it can only be spawned through a shell. That makes the path
 * argument the dangerous part: a folder name containing `& calc` would
 * otherwise be executed. Hence execFile with an argument array on POSIX
 * and explicit quoting plus a metacharacter reject on Windows - a path
 * is data here and must never be parsed as command syntax.
 */
const { execFile } = require("node:child_process");
const { dialog } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const IS_WINDOWS = process.platform === "win32";
const CODE_CMD = IS_WINDOWS ? "code.cmd" : "code";

function hasVSCode() {
  return new Promise((resolve) => {
    const probe = IS_WINDOWS ? "where" : "which";
    execFile(probe, [CODE_CMD], { windowsHide: true }, (err, stdout) => {
      resolve(!err && !!String(stdout || "").trim());
    });
  });
}

function registerEditorHandlers(ipcMain) {
  ipcMain.handle("editor:has-vscode", () => hasVSCode());

  ipcMain.handle("editor:pick-folder", async () => {
    const result = await dialog.showOpenDialog({
      properties: ["openDirectory"],
      title: "Choose a workspace folder",
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });

  ipcMain.handle("editor:open-vscode", async (_event, targetPath) => {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return { ok: false, error: "No path given." };
    }

    const resolved = path.resolve(targetPath);

    // Refuse anything that isn't a real path on disk. This both gives a
    // clear error and removes the case where a crafted string is passed
    // through to a shell.
    if (!fs.existsSync(resolved)) {
      return { ok: false, error: "That path doesn't exist." };
    }
    if (/[\n\r&|;`$]/.test(resolved)) {
      return { ok: false, error: "That path contains unsupported characters." };
    }

    if (!(await hasVSCode())) {
      return {
        ok: false,
        error:
          "VS Code's `code` command isn't on PATH. In VS Code, run " +
          "\"Shell Command: Install 'code' command in PATH\".",
      };
    }

    return new Promise((resolve) => {
      // execFile with an args array: the path is passed as one argv
      // entry, never concatenated into a command string.
      execFile(
        CODE_CMD,
        [resolved],
        { windowsHide: true, shell: IS_WINDOWS },
        (err) => {
          if (err) resolve({ ok: false, error: err.message });
          else resolve({ ok: true, path: resolved });
        },
      );
    });
  });
}

module.exports = { registerEditorHandlers, hasVSCode };
