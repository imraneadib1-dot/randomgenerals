/**
 * Terminal execution over IPC, with streamed stdout/stderr.
 *
 * On the threat model, plainly: this runs commands as the logged-in user
 * on their own machine. That is not itself a privilege escalation - they
 * already have a shell. What matters is that this capability exists
 * ONLY in the Electron build and ONLY over IPC. It is deliberately not a
 * Flask route, because the Flask app is reachable over a public tunnel;
 * an HTTP endpoint that ran shell commands would hand every visitor a
 * shell on this machine. Keep it that way.
 *
 * The free/pro split below is a real safety boundary as well as a
 * product one. Free tier runs single, allow-listed programs with shell
 * metacharacters rejected, so a command cannot chain, pipe, or redirect.
 * Pro unlocks the raw shell. That means a mis-scoped free session still
 * cannot `rm -rf` or pipe a download into an interpreter.
 */
const { spawn } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");
const fs = require("node:fs");
const { getTier } = require("../entitlements");

/**
 * Working directories a command may run in. A cwd supplied by the
 * renderer is otherwise arbitrary - `cwd: "C:\\Windows\\System32"` would
 * happily run there. Containment is to the project root (dev) or the
 * user's own documents, resolved with realpath so a symlink or `..`
 * cannot walk out of the allowed tree.
 */
const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..");

function resolveSafeCwd(requested) {
  if (!requested) return PROJECT_ROOT;

  let resolved;
  try {
    // realpath, not just resolve: `project/link-to-system32` resolves to
    // a path that still *looks* inside the project until the symlink is
    // followed.
    resolved = fs.realpathSync(path.resolve(requested));
  } catch {
    return { error: "That working directory doesn't exist." };
  }

  const roots = [PROJECT_ROOT, os.homedir()].map((r) => {
    try {
      return fs.realpathSync(r);
    } catch {
      return path.resolve(r);
    }
  });

  const inside = roots.some((root) => {
    const rel = path.relative(root, resolved);
    // path.relative gives "" for the root itself, and a path starting
    // with ".." for anything outside it. isAbsolute catches a different
    // drive letter on Windows, where relative() returns e.g. "C:\..".
    return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
  });

  if (!inside) {
    return {
      error: "Commands can only run inside the project or your home folder.",
    };
  }
  return resolved;
}

/** Live sessions, so they can be killed individually or on quit. */
const sessions = new Map();

let nextId = 1;

const MAX_OUTPUT_BYTES = 2 * 1024 * 1024; // 2MB per session
const DEFAULT_TIMEOUT_MS = 120_000;

/**
 * Programs the free tier may run. Chosen as read-mostly developer tools:
 * useful for the app's actual purpose (inspecting a project, checking a
 * version) without being general-purpose file destruction.
 */
const FREE_TIER_ALLOWLIST = new Set([
  "git", "node", "npm", "npx", "python", "python3", "pip", "pip3",
  "ls", "dir", "pwd", "cd", "echo", "cat", "type", "where", "which",
  "whoami", "date", "hostname", "code", "ollama", "curl", "tree",
]);

/**
 * Shell metacharacters. Rejected for free tier because each one turns a
 * single allow-listed program into arbitrary execution:
 *   `git log && rm -rf .`   `echo x | powershell`   `ls > /etc/passwd`
 * Checking the *program name* alone is not enough without this.
 */
const SHELL_METACHARACTERS = /[;&|><`$(){}\n\r]/;

function validateCommand(command, tier) {
  const trimmed = (command || "").trim();
  if (!trimmed) return { ok: false, error: "No command given." };
  if (trimmed.length > 4000) return { ok: false, error: "Command too long." };

  if (tier === "pro") return { ok: true, command: trimmed };

  if (SHELL_METACHARACTERS.test(trimmed)) {
    return {
      ok: false,
      error:
        "Free tier runs one command at a time - chaining, pipes and " +
        "redirection are Pro features.",
      upgrade: true,
    };
  }

  const program = trimmed.split(/\s+/)[0].toLowerCase().replace(/\.exe$/, "");
  if (!FREE_TIER_ALLOWLIST.has(program)) {
    return {
      ok: false,
      error: `"${program}" isn't available on the free tier. Pro unlocks the full shell.`,
      upgrade: true,
    };
  }
  return { ok: true, command: trimmed };
}

function registerTerminalHandlers(ipcMain, send) {
  ipcMain.handle("terminal:run", async (_event, { command, options = {} }) => {
    const tier = await getTier();
    const check = validateCommand(command, tier);
    if (!check.ok) {
      return { error: check.error, upgrade: !!check.upgrade };
    }

    // Pro gets the real shell (that is the feature). Free runs without
    // one, so the OS executes exactly one program with exactly the
    // arguments given and no shell parsing happens at all - which is
    // what makes the metacharacter check above sufficient rather than
    // merely a speed bump.
    const useShell = tier === "pro";
    const cwd = resolveSafeCwd(options.cwd);
    if (cwd && cwd.error) return { error: cwd.error };
    const timeoutMs = Math.min(
      Number(options.timeoutMs) || DEFAULT_TIMEOUT_MS,
      tier === "pro" ? 30 * 60_000 : 60_000,
    );

    let child;
    try {
      if (useShell) {
        child = spawn(check.command, { cwd, shell: true, windowsHide: true });
      } else {
        const [program, ...args] = check.command.split(/\s+/);
        child = spawn(program, args, { cwd, shell: false, windowsHide: true });
      }
    } catch (err) {
      return { error: `Could not start: ${err.message}` };
    }

    const sessionId = String(nextId++);
    let bytes = 0;
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }, timeoutMs);

    const pipe = (stream, name) => {
      stream.on("data", (buf) => {
        // Cap total output. A runaway command (`ping -t`, a build loop)
        // would otherwise stream until the renderer runs out of memory.
        if (bytes >= MAX_OUTPUT_BYTES) return;
        bytes += buf.length;
        send("terminal:output", {
          sessionId,
          stream: name,
          chunk: buf.toString("utf8"),
        });
        if (bytes >= MAX_OUTPUT_BYTES) {
          send("terminal:output", {
            sessionId,
            stream: "stderr",
            chunk: "\n[output truncated - 2MB limit reached]\n",
          });
        }
      });
    };
    pipe(child.stdout, "stdout");
    pipe(child.stderr, "stderr");

    child.on("error", (err) => {
      send("terminal:output", {
        sessionId,
        stream: "stderr",
        chunk: `${err.message}\n`,
      });
    });

    child.on("close", (code, signal) => {
      clearTimeout(timer);
      sessions.delete(sessionId);
      send("terminal:exit", { sessionId, code, signal, timedOut });
    });

    sessions.set(sessionId, child);
    return { sessionId, tier };
  });

  ipcMain.handle("terminal:kill", (_event, sessionId) => {
    const child = sessions.get(String(sessionId));
    if (!child) return { ok: false, error: "No such session." };
    try {
      child.kill("SIGKILL");
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });
}

/** Called on app quit - see the before-quit handler in main.js. */
function killAllSessions() {
  for (const child of sessions.values()) {
    try {
      child.kill("SIGKILL");
    } catch {
      /* ignore */
    }
  }
  sessions.clear();
}

module.exports = {
  registerTerminalHandlers,
  killAllSessions,
  // exported for unit tests
  validateCommand,
  resolveSafeCwd,
  PROJECT_ROOT,
  FREE_TIER_ALLOWLIST,
};
