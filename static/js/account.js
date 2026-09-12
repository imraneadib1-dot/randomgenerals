import { confirmDialog } from "./confirm.js";
/* ----------------------------------------------------------------
   Account management

   Verification, password and deletion. Each of these calls a route that
   exists and was tested - mailer.py and the verification_codes table
   had both been written and neither was ever called until now.

   Everything here is hidden until a session exists, and the password
   block is hidden again for Google accounts, which have no password to
   change. Showing a control that cannot work is the failure this whole
   pass is about.
   ---------------------------------------------------------------- */
export function renderAccountControls(user) {
  const manage = document.getElementById("accountManage");
  const pwSection = document.getElementById("passwordSection");
  const pwDivider = document.getElementById("pwDivider");
  const danger = document.getElementById("dangerSection");
  const dangerDivider = document.getElementById("dangerDivider");
  if (!manage) return;

  const signedIn = !!(user && user.email);
  manage.hidden = !signedIn;
  const link = document.getElementById("profileLinkRow");
  if (link) link.hidden = !signedIn;
  for (const id of ["profileSection", "profileDivider"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = !signedIn;
  }
  if (danger) danger.hidden = !signedIn;
  if (dangerDivider) dangerDivider.hidden = !signedIn;

  // A Google account has no password_hash, so the change-password route
  // refuses it by design. Hide the form rather than let someone fill it
  // in and be told no.
  const hasPassword = signedIn && !user.google_id;
  if (pwSection) pwSection.hidden = !hasPassword;
  if (pwDivider) pwDivider.hidden = !hasPassword;

  const state = document.getElementById("verifyState");
  const controls = document.getElementById("verifyControls");
  if (state && signedIn) {
    if (user.email_verified) {
      state.textContent = user.email + " is verified.";
      if (controls) controls.hidden = true;
    } else {
      state.textContent = user.email + " is not verified yet.";
      if (controls) controls.hidden = false;
    }
  }
  const del = document.getElementById("deleteConfirm");
  if (del && signedIn) del.placeholder = user.email;
}

export function initAccountControls() {
  const send = document.getElementById("verifySend");
  const code = document.getElementById("verifyCode");
  const confirm_ = document.getElementById("verifyConfirm");
  const vStatus = document.getElementById("verifyStatus");

  if (send) {
    send.addEventListener("click", async () => {
      vStatus.textContent = "Sending…";
      try {
        const r = await fetch("/api/auth/verify/send", { method: "POST" });
        const d = await r.json();
        if (!r.ok) { vStatus.textContent = d.error || "Could not send."; return; }
        // The console fallback is a working state on a server with no
        // SMTP, so it is reported as information rather than failure.
        vStatus.textContent = d.detail || "Code sent.";
        code.hidden = false;
        confirm_.hidden = false;
        code.focus();
      } catch (_) {
        vStatus.textContent = "Could not reach the server.";
      }
    });
  }

  if (confirm_) {
    confirm_.addEventListener("click", async () => {
      vStatus.textContent = "Checking…";
      try {
        const r = await fetch("/api/auth/verify/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: (code.value || "").trim() }),
        });
        const d = await r.json();
        if (!r.ok) { vStatus.textContent = d.error || "Not right."; return; }
        vStatus.textContent = "Verified.";
        code.hidden = true;
        confirm_.hidden = true;
        if (d.user) renderAccountControls(d.user);
      } catch (_) {
        vStatus.textContent = "Could not reach the server.";
      }
    });
  }

  const pwSave = document.getElementById("pwSave");
  if (pwSave) {
    pwSave.addEventListener("click", async () => {
      const cur = document.getElementById("pwCurrent");
      const nw = document.getElementById("pwNew");
      const st = document.getElementById("pwStatus");
      st.textContent = "Saving…";
      try {
        const r = await fetch("/api/account/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current: cur.value, new: nw.value }),
        });
        const d = await r.json();
        st.textContent = r.ok ? "Password updated."
                              : (d.error || "Could not update.");
        if (r.ok) { cur.value = ""; nw.value = ""; }
      } catch (_) {
        st.textContent = "Could not reach the server.";
      }
    });
  }

  const del = document.getElementById("deleteAccount");
  if (del) {
    del.addEventListener("click", async () => {
      const field = document.getElementById("deleteConfirm");
      const st = document.getElementById("deleteStatus");
      // The typed address is the confirmation the server checks; this
      // dialog is the second one, because the action has no undo.
      const sure = await confirmDialog({
        title: "Delete your account?",
        body: "Everything in it goes with it. This cannot be undone.",
        confirmLabel: "Delete my account", danger: true,
      });
      if (!sure) return;
      st.textContent = "Deleting…";
      try {
        const r = await fetch("/api/account/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_email: (field.value || "").trim() }),
        });
        const d = await r.json();
        if (!r.ok) { st.textContent = d.error || "Could not delete."; return; }
        st.textContent = "Account deleted.";
        window.location.reload();
      } catch (_) {
        st.textContent = "Could not reach the server.";
      }
    });
  }
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountAccount() {
  initAccountControls();

}
