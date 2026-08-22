/**
 * Feature gating - what tier the current user is, and how that's decided.
 *
 * Two rules this module exists to enforce:
 *
 * 1. The server is the authority. A stored licence file is a *cache*, not
 *    proof. Anyone can edit files on their own machine, so a purely local
 *    check is trivially bypassed - that's fine for hiding UI, useless for
 *    protecting anything that costs money to run.
 *
 * 2. Never fail *open* on a network error, and never hard-fail *closed*
 *    on one either. A user who paid shouldn't lose Pro because their
 *    wifi dropped, and a user who didn't shouldn't gain it by pulling
 *    their ethernet cable. The compromise: a signed local licence is
 *    trusted for a grace period after its last successful check.
 */
const { localStatus } = require("./ipc/license");

const GRACE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days offline

let lastVerify = { at: 0, tier: null };
const VERIFY_TTL_MS = 15 * 60 * 1000; // re-check at most every 15 min

/** Base URL of the Flask backend; set by backend.js once it's bound. */
let backendUrl = "http://127.0.0.1:5000";
function setBackendUrl(url) {
  backendUrl = url.replace(/\/$/, "");
}

/**
 * Ask the backend whether a key is genuine. The backend in turn asks
 * Stripe / Lemon Squeezy - see /api/license/verify in app.py. Keeping the
 * payment-provider secret server-side is the whole point: it must never
 * be shipped inside a desktop binary, where anyone can extract it.
 */
async function verifyLicenseWithServer(key) {
  try {
    const res = await fetch(`${backendUrl}/api/license/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { valid: false, error: body.error || `Server said ${res.status}.` };
    }
    return await res.json();
  } catch (err) {
    return {
      valid: false,
      error: "Couldn't reach the licence server - check your connection.",
      offline: true,
    };
  }
}

/**
 * Current tier: "free" | "pro". Cached briefly so a burst of terminal
 * commands doesn't issue a verification request each time.
 */
async function getTier() {
  const now = Date.now();
  if (lastVerify.tier && now - lastVerify.at < VERIFY_TTL_MS) {
    return lastVerify.tier;
  }

  const local = localStatus();
  if (!local.valid || local.tier !== "pro") {
    lastVerify = { at: now, tier: "free" };
    return "free";
  }

  const result = await verifyLicenseWithServer(local.key);
  let tier;
  if (result.valid) {
    tier = result.tier || "pro";
  } else if (result.offline) {
    // Offline: honour the cached licence, but only inside the grace
    // window, so a cancelled subscription can't be kept alive forever by
    // simply staying disconnected.
    const activated = local.activatedAt ? Date.parse(local.activatedAt) : 0;
    tier = now - activated < GRACE_MS ? "pro" : "free";
  } else {
    // Server actively said the key is bad (revoked, refunded, expired).
    tier = "free";
  }

  lastVerify = { at: now, tier };
  return tier;
}

/** Force the next getTier() to re-check - call after activate/deactivate. */
function invalidateTierCache() {
  lastVerify = { at: 0, tier: null };
}

module.exports = {
  getTier,
  verifyLicenseWithServer,
  invalidateTierCache,
  setBackendUrl,
};
