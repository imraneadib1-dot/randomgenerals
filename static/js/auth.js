import { state } from "./state.js";
import { renderAccountControls } from "./account.js";
import { showEmptyState } from "./bays.js";
import { freeButtonLabel } from "./billing.js";
import { loadCredits, renderCredits } from "./credits.js";
import { accountAvatar, accountEmail, accountPlanBadge, authSignedIn, authSignedOut, forgotSwitch, googleSignInBtn, localAuthAge, localAuthEmail, localAuthError, localAuthForm, localAuthName, localAuthPassword, localAuthSubmit, localAuthSwitch, logoutBtn, planError, planFreeBtn, planNote, planProBtn, resetForm, settingsBtnLabel } from "./dom.js";
import { loadThreadList } from "./sidebar.js";

/* ---- Sign-in: Google only ---- */

export function setError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

/* Name and age are asked for once, at signup. In login mode they are
   hidden rather than disabled, because a login form that asks your age
   looks like it is about to create a second account. */
export function syncAuthFields() {
  const signingUp = state.localAuthMode === "signup";
  localAuthName.hidden = !signingUp;
  localAuthAge.hidden = !signingUp;
  localAuthName.required = signingUp;
  localAuthAge.required = signingUp;
}

/* ----------------------------------------------------------------
   Forgotten password

   A separate little form. The one above already carries two modes and a
   third would make every label conditional on something.
   ---------------------------------------------------------------- */
export function showReset(on) {
  resetForm.hidden = !on;
  localAuthForm.hidden = on;
  setError(document.getElementById("resetError"), "");
}

export function refreshAuthUI() {
  setError(planError, "");
  const signedIn = !!state.currentUser;
  authSignedOut.hidden = signedIn;
  authSignedIn.hidden = !signedIn;
  // The verification, password and delete blocks live inside the
  // signed-in panel and depend on WHICH account it is - a Google account
  // has no password - so they are re-rendered here rather than once at
  // load, when currentUser is still null.
  if (typeof renderAccountControls === "function") {
    renderAccountControls(state.currentUser);
  }
  settingsBtnLabel.textContent = signedIn
    ? state.currentUser.email
    : "Sign up / Settings";

  if (signedIn) {
    accountAvatar.textContent = state.currentUser.email[0].toUpperCase();
    accountEmail.textContent = state.currentUser.email;
    accountPlanBadge.textContent =
      state.currentUser.plan === "pro" ? "Pro plan" : "Free plan";
    if (!state.upgradePoll) planNote.textContent = `Signed in as ${state.currentUser.email}.`;
    planFreeBtn.disabled = state.currentUser.plan !== "pro";
    planFreeBtn.textContent = freeButtonLabel();
    planProBtn.hidden = state.currentUser.plan === "pro";
  } else {
    planNote.textContent = "Sign in to manage your plan.";
    planFreeBtn.disabled = true;
    planFreeBtn.textContent = "Current plan";
    planProBtn.hidden = false;
  }
}

export async function changePlan(plan) {
  setError(planError, "");
  if (!state.currentUser) {
    setError(
      planError,
      "Sign in first to upgrade — switch to the Account tab.",
    );
    return;
  }
  planProBtn.disabled = true;
  try {
    const res = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan }),
    });
    const data = await res.json();
    if (res.ok && data.checkout_url) {
      // Paddle hands back our own URL with ?_ptxn=<transaction id> on it,
      // because its checkout is an overlay rather than a page. Pulling the
      // id out and opening the overlay in place is the same flow without
      // the full page reload - and it still works if Paddle.js is slow,
      // because the navigation below is kept as the fallback.
      const txn = new URL(data.checkout_url, window.location.origin)
        .searchParams.get("_ptxn");
      if (txn && state.paddleReady) {
        try {
          const paddle = await state.paddleReady;
          paddle.Checkout.open({ transactionId: txn });
          planProBtn.disabled = false;
          return;
        } catch (e) {
          console.error("Paddle overlay failed:", e);
          setError(
            planError,
            "The payment window could not open. Reload the page and try " +
              "again — if it keeps happening, check that the browser or an " +
              "ad blocker isn't blocking cdn.paddle.com.",
          );
          planProBtn.disabled = false;
          return;
        }
      }
      // No transaction id means this isn't a Paddle URL - a Stripe
      // checkout link, which really is a page to send the browser to.
      if (!txn) {
        window.location.href = data.checkout_url;
        return;
      }
      // Paddle URL, but Paddle.js never initialised. Navigating there
      // does eventually work - the reloaded page starts Paddle.js, which
      // opens the overlay from ?_ptxn - but it looks exactly like the app
      // resetting itself for no reason, so say what is happening instead
      // of doing it silently.
      setError(
        planError,
        "The payment window isn't ready yet. Reload the page and try again.",
      );
      planProBtn.disabled = false;
      return;
    } else if (res.ok) {
      state.currentUser = data.user;
      refreshAuthUI();
      renderCredits(data.credits);
    } else {
      // `detail` names the exact missing piece of Stripe config when the
      // server has one - far more actionable than "could not change plan".
      setError(
        planError,
        [data.error, data.detail].filter(Boolean).join(" ") ||
          "Could not change plan.",
      );
    }
  } catch (err) {
    setError(planError, "Could not reach the server.");
  } finally {
    // Only re-enable if buying is actually possible. An unconditional
    // `= false` here would undo the disabled state set in
    // loadPlansMeta() and put the dead-end click straight back.
    planProBtn.disabled = !state.billingLive;
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountAuth() {
  googleSignInBtn.addEventListener("click", () => {
    // Full navigation, not fetch() - OAuth needs an actual page redirect to
    // Google's consent screen, not an API call.
    window.location.href = "/api/auth/google/login";
  });

  syncAuthFields();

  localAuthSwitch.addEventListener("click", () => {
    state.localAuthMode = state.localAuthMode === "signup" ? "login" : "signup";
    const signingUp = state.localAuthMode === "signup";
    localAuthSubmit.textContent = signingUp ? "Sign up" : "Log in";
    localAuthSwitch.textContent = signingUp
      ? "Already have an account? Log in"
      : "Need an account? Sign up";
    localAuthPassword.placeholder = signingUp
      ? "Password (8+ characters)"
      : "Password";
    localAuthPassword.autocomplete = signingUp
      ? "new-password"
      : "current-password";
    syncAuthFields();
    setError(localAuthError, "");
  });

  forgotSwitch.addEventListener("click", () => {
    document.getElementById("resetEmail").value = localAuthEmail.value.trim();
    showReset(true);
  });

  document.getElementById("resetBack").addEventListener("click", () =>
    showReset(false),
  );

  document.getElementById("resetSend").addEventListener("click", async () => {
    const err = document.getElementById("resetError");
    setError(err, "");
    const btn = document.getElementById("resetSend");
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/reset/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: document.getElementById("resetEmail").value.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(err, data.error || "Could not send a code.");
        return;
      }
      // The server answers the same way whether or not an account exists,
      // so this message must not imply one does.
      document.getElementById("resetIntro").textContent =
        data.detail || "If there is an account for that address, a code is on its way.";
      document.getElementById("resetStep2").hidden = false;
      document.getElementById("resetCode").focus();
    } catch (e) {
      setError(err, "Could not reach the server.");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("resetConfirm").addEventListener("click", async () => {
    const err = document.getElementById("resetError");
    setError(err, "");
    const btn = document.getElementById("resetConfirm");
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: document.getElementById("resetEmail").value.trim(),
          code: document.getElementById("resetCode").value.trim(),
          new: document.getElementById("resetNew").value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(err, data.error || "That did not work.");
        return;
      }
      // Deliberately not signed in - the server does not open a session on
      // a reset, so send them to the login form to use the new password.
      showReset(false);
      state.localAuthMode = "login";
      localAuthSubmit.textContent = "Log in";
      syncAuthFields();
      localAuthPassword.value = "";
      setError(localAuthError, "");
      document.getElementById("resetIntro").textContent =
        "Enter your email and we will send you a code.";
      document.getElementById("resetStep2").hidden = true;
      alert("Password changed. Log in with your new password.");
    } catch (e) {
      setError(err, "Could not reach the server.");
    } finally {
      btn.disabled = false;
    }
  });

  localAuthForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    setError(localAuthError, "");
    localAuthSubmit.disabled = true;
    try {
      const res = await fetch(`/api/auth/${state.localAuthMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: localAuthEmail.value.trim(),
          password: localAuthPassword.value,
          // Ignored by /api/auth/login; required by /api/auth/signup.
          name: localAuthName.value.trim(),
          age: localAuthAge.value.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(localAuthError, data.error || "Something went wrong.");
        return;
      }
      state.currentUser = data.user;
      localAuthForm.reset();
      refreshAuthUI();
      loadCredits();
      // Threads move to the new account server-side on signup/login, so
      // the sidebar has to re-read them rather than keep the guest list.
      loadThreadList();
    } catch (err) {
      setError(localAuthError, "Could not reach the server.");
    } finally {
      localAuthSubmit.disabled = false;
    }
  });

  logoutBtn.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    state.currentUser = null;
    refreshAuthUI();
    loadCredits();
    state.currentThreadId = null;
    showEmptyState();
    loadThreadList();
  });

}
