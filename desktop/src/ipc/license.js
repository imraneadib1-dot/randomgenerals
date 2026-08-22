/**
 * Licence storage, encrypted at rest by the OS.
 *
 * Uses Electron's built-in `safeStorage`, not keytar: keytar is archived
 * and unmaintained, and it needs a native module that has to be rebuilt
 * per Electron version. safeStorage ships with Electron and delegates to
 * DPAPI on Windows, Keychain on macOS, and libsecret/kwallet on Linux -
 * same guarantee, nothing to compile.
 *
 * What that guarantee actually is, stated honestly: the ciphertext is
 * bound to the OS user account, so another user on the machine (or a
 * copied file) cannot read it. It is NOT protection against code running
 * as this user - such code can call the same decrypt API. Client-side
 * licence checks are a convenience, never the enforcement point; the
 * server re-validates on every privileged call (see /api/license/verify).
 */
const { safeStorage, app } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const LICENSE_FILE = () => path.join(app.getPath("userData"), "license.bin");

/** In-memory cache so entitlement checks don't hit disk on every call. */
let cached = null;

function readLicense() {
  if (cached !== null) return cached;
  try {
    const file = LICENSE_FILE();
    if (!fs.existsSync(file)) {
      cached = null;
      return null;
    }
    const blob = fs.readFileSync(file);
    // If the OS has no encryption backend (some bare Linux desktops),
    // safeStorage silently falls back to plaintext. Refusing to read in
    // that case would break the app; instead the write path below is
    // what declines to persist, so we simply try to parse.
    const json = safeStorage.isEncryptionAvailable()
      ? safeStorage.decryptString(blob)
      : blob.toString("utf8");
    cached = JSON.parse(json);
    return cached;
  } catch {
    // Corrupt or undecryptable (e.g. copied from another machine) - treat
    // as "no licence" rather than crashing the app on launch.
    cached = null;
    return null;
  }
}

function writeLicense(record) {
  const json = JSON.stringify(record);
  const file = LICENSE_FILE();
  if (!safeStorage.isEncryptionAvailable()) {
    return {
      ok: false,
      error:
        "This system has no secure credential store available, so the " +
        "licence can't be saved safely. You can still use Pro this session.",
    };
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, safeStorage.encryptString(json));
  cached = record;
  return { ok: true };
}

function clearLicense() {
  try {
    fs.unlinkSync(LICENSE_FILE());
  } catch {
    /* already absent */
  }
  cached = null;
}

/** Local view of the licence. Deliberately does not itself decide
 *  entitlement - see entitlements.js, which re-checks with the server. */
function localStatus() {
  const rec = readLicense();
  if (!rec) return { tier: "free", valid: false };
  const expired = rec.expiresAt && new Date(rec.expiresAt) < new Date();
  return {
    tier: expired ? "free" : rec.tier || "free",
    email: rec.email,
    expiresAt: rec.expiresAt,
    valid: !expired,
    key: rec.key,
  };
}

function registerLicenseHandlers(ipcMain) {
  ipcMain.handle("license:status", () => {
    const s = localStatus();
    // Never hand the raw key back to the renderer - it is a bearer
    // credential and the UI has no need for it.
    const { key, ...safe } = s;
    return safe;
  });

  ipcMain.handle("license:activate", async (_event, key) => {
    if (typeof key !== "string" || key.trim().length < 8) {
      return { ok: false, error: "That doesn't look like a licence key." };
    }
    const { verifyLicenseWithServer } = require("../entitlements");
    const result = await verifyLicenseWithServer(key.trim());
    if (!result.valid) {
      return { ok: false, error: result.error || "That key isn't valid." };
    }
    const write = writeLicense({
      key: key.trim(),
      tier: result.tier || "pro",
      email: result.email,
      expiresAt: result.expiresAt,
      activatedAt: new Date().toISOString(),
    });
    if (!write.ok) return write;
    return { ok: true, tier: result.tier || "pro", expiresAt: result.expiresAt };
  });

  ipcMain.handle("license:deactivate", () => {
    clearLicense();
    return { ok: true };
  });
}

module.exports = {
  registerLicenseHandlers,
  localStatus,
  readLicense,
  clearLicense,
};
