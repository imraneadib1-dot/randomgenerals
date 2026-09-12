import { state } from "./state.js";
import { changePlan, refreshAuthUI, setError } from "./auth.js";
import { loadCredits, renderCredits } from "./credits.js";
import { googleAuthError, googleNotConfiguredNote, googleSignInBtn, managePlanBtn, messageInput, planError, planFineprint, planFreeBtn, planNote, planProBtn, subCreditsValue, subDashboard, subEmailValue, subPaymentRow, subPaymentValue, subPlanValue, subRenewalLabel, subRenewalRow, subRenewalValue, subSinceValue, subStatusRow, subStatusValue, subVerifiedValue } from "./dom.js";
import { openSettings } from "./modal.js";

export function freeButtonLabel() {
  if (!state.currentUser || state.currentUser.plan !== "pro") return "Current plan";
  if (state.subscriptionState && state.subscriptionState.has_billing_account) {
    return state.subscriptionState.cancel_at_period_end
      ? "Keep subscription"
      : "Cancel subscription";
  }
  return "Downgrade to Free";
}

/* Leaving Pro is a cancellation at the payment provider, not a local
   plan change. It used to be the latter: the plan flipped to free and
   Paddle kept charging every month for a subscription the app no longer
   showed. Pro stays until the paid period ends, and the same button
   then offers to keep it. */
export async function leaveOrKeepPro() {
  if (!state.currentUser || state.currentUser.plan !== "pro") return;
  setError(planError, "");
  const keeping = !!(state.subscriptionState && state.subscriptionState.cancel_at_period_end);
  if (!keeping && state.subscriptionState && state.subscriptionState.has_billing_account) {
    const until = state.subscriptionState.current_period_end
      ? new Date(state.subscriptionState.current_period_end).toLocaleDateString(
          undefined, { year: "numeric", month: "long", day: "numeric" })
      : "the end of the paid period";
    if (!confirm(`Cancel Pro? You keep it until ${until}, and you can change your mind before then.`)) {
      return;
    }
  }
  planFreeBtn.disabled = true;
  try {
    const res = await fetch(keeping ? "/api/billing/resume" : "/api/billing/cancel", {
      method: "POST",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(planError, [data.error, data.detail].filter(Boolean).join(" ")
        || "Could not change the subscription.");
      return;
    }
    if (data.user) state.currentUser = data.user;
    if (data.credits) renderCredits(data.credits);
    await loadSubscription();
    refreshAuthUI();
    if (data.cancel_at_period_end) {
      planNote.textContent = "Cancelled — Pro stays until the end of the paid period.";
    } else if (keeping) {
      planNote.textContent = "Kept — your subscription will renew as before.";
    }
  } catch (err) {
    setError(planError, "Could not reach the server.");
  } finally {
    planFreeBtn.disabled = !(state.currentUser && state.currentUser.plan === "pro");
  }
}

export function loadPaddle(token, environment, customerId) {
  if (state.paddleReady) return state.paddleReady;
  state.paddleReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.paddle.com/paddle/v2/paddle.js";
    s.onload = () => {
      try {
        // Must be set before Initialize, and only for sandbox - calling
        // it with "production" is not valid. Driven by what the server
        // reports rather than hard-coded, so the same build works
        // against either without an edit; in live the server says
        // "production" and this line does nothing.
        if (environment === "sandbox") window.Paddle.Environment.set("sandbox");

        // pwCustomer is what Paddle Retain uses to recognise the person
        // looking at the page. It is omitted entirely rather than passed
        // empty when unknown: everyone who has not paid yet has no
        // Paddle customer id, and handing Retain a blank one is worse
        // than handing it nothing.
        const options = { token };
        if (customerId) options.pwCustomer = { id: customerId };
        // The overlay reports what happened inside it. Without this the
        // page learnt nothing when somebody paid: the plan badge said
        // Free until they reloaded, and only then if the webhook had
        // already landed. checkout.completed is the moment to start
        // asking the server whether it has heard.
        options.eventCallback = (evt) => {
          const name = evt && evt.name;
          if (name === "checkout.completed") awaitUpgrade();
          else if (name === "checkout.closed" && !state.upgradePoll) {
            planProBtn.disabled = !state.billingLive;
          }
        };
        window.Paddle.Initialize(options);
        resolve(window.Paddle);
      } catch (e) {
        reject(e);
      }
    };
    s.onerror = () => reject(new Error("Paddle.js failed to load"));
    document.head.appendChild(s);
  });
  return state.paddleReady;
}

export function updatePaddleCustomer(customerId) {
  if (!customerId || !window.Paddle || !window.Paddle.Update) return;
  if (state.lastPaddleCustomer === customerId) return;
  state.lastPaddleCustomer = customerId;
  try {
    window.Paddle.Update({ pwCustomer: { id: customerId } });
  } catch (e) {
    console.error("Paddle.Update failed:", e);
  }
}

export async function loadPlansMeta() {
  try {
    const res = await fetch("/api/plans");
    const data = await res.json();
    state.billingLive = !!data.billing_live;

    if (data.processor === "paddle" && data.paddle_client_token) {
      // Initialised on page load, not on click: Paddle.js opens the
      // overlay by itself when it sees ?_ptxn= in the URL, and it can
      // only do that if it is already running when the page loads.
      loadPaddle(
        data.paddle_client_token,
        data.paddle_environment,
        data.paddle_customer_id,
      )
        .then(() => updatePaddleCustomer(data.paddle_customer_id))
        .catch((e) => console.error("Paddle failed to initialise:", e));
    }

    if (state.billingLive) {
      planProBtn.disabled = false;
      planProBtn.textContent = "Upgrade to Pro";
      planProBtn.removeAttribute("title");
      planFineprint.textContent =
        data.processor === "paddle"
          ? "Card details are entered in Paddle's own checkout and never " +
            "touch this server. Paddle is the seller of record and handles " +
            "VAT. Cancel any time from the button above."
          : "Real card payments via Stripe. Card details are entered on " +
            "Stripe's own checkout page and never touch this server. Manage " +
            "or cancel any time from the button above.";
    } else {
      // Disable rather than let the click fail. Previously the button
      // stayed active, the click returned 503, and the resulting red
      // error said the same thing as the fineprint directly beneath it -
      // the same sentence twice, the second one clipped by the modal.
      // One statement, in one place, and no dead-end click.
      planProBtn.disabled = true;
      if (data.desktop) {
        // Not "not available yet" - it IS available, just not from
        // here. Paddle's checkout only opens on a domain it has
        // approved, and this app runs on a loopback port that never can
        // be. Sending somebody to the place it works beats a dead
        // button explaining a limitation they cannot act on.
        const site = (data.site_url || "https://randomgenerals.com") + "/app";
        planProBtn.textContent = "Upgrade on the website";
        planProBtn.disabled = false;
        planProBtn.title = "Opens randomgenerals.com in your browser";
        planProBtn.onclick = (e) => {
          e.preventDefault();
          window.open(site, "_blank", "noopener");
        };
        planFineprint.textContent =
          "Checkout runs on randomgenerals.com — payment providers only " +
          "accept approved domains, which a desktop app's local address " +
          "cannot be. Signing in there upgrades this app too.";
      } else {
        planProBtn.textContent = "Pro not available yet";
        planProBtn.title =
          "This server hasn't been connected to a payment provider.";
        planFineprint.textContent =
          "Pro isn't purchasable yet — this server hasn't been connected " +
          "to a payment provider. Everything in Free works normally.";
      }
    }
    setError(planError, "");
  } catch (err) {
    /* fineprint keeps its default text */
  }
}

export async function loadSubscription() {
  if (!state.currentUser) {
    subDashboard.hidden = true;
    return;
  }
  try {
    const res = await fetch("/api/billing/subscription");
    if (!res.ok) {
      subDashboard.hidden = true;
      return;
    }
    const s = await res.json();
    state.subscriptionState = s;
    subDashboard.hidden = false;
    subPlanValue.textContent = `${s.plan_label} · ${s.price}`;
    if (state.currentUser) planFreeBtn.textContent = freeButtonLabel();

    // Who this account is, not only what it is subscribed to. This panel
    // is where someone comes to check what they are being charged for,
    // and it used to appear only once there was a paid subscription -
    // so a free account saw an empty tab and no way to tell which email
    // it belonged to.
    subEmailValue.textContent = s.email || "—";
    subVerifiedValue.textContent = s.email_verified
      ? "Verified"
      : "Not verified";
    subVerifiedValue.classList.toggle("is-cancelling", !s.email_verified);
    subSinceValue.textContent = s.created
      ? new Date(s.created).toLocaleDateString(undefined, {
          year: "numeric", month: "long", day: "numeric",
        })
      : "—";
    if (s.credits && s.credits.balance != null) {
      const mins = Math.round((s.credits.next_refill_in || 0) / 60);
      subCreditsValue.textContent =
        `${s.credits.balance.toLocaleString()} of ` +
        `${(s.credits.cap || 0).toLocaleString()}` +
        (mins > 0 ? ` · refills in ${mins} min` : " · refilling now");
    } else {
      subCreditsValue.textContent = "—";
    }

    subPaymentRow.hidden = !s.has_billing_account;
    if (s.has_billing_account) {
      subPaymentValue.textContent =
        `${s.processor} — card details never touch this server`;
    }

    subStatusRow.hidden = !s.status;
    if (s.status) subStatusValue.textContent = s.status;

    if (s.current_period_end) {
      subRenewalRow.hidden = false;
      // "Renews" vs "Cancels" is a materially different message - a
      // subscription set to lapse shouldn't imply it's about to charge.
      subRenewalLabel.textContent = s.cancel_at_period_end
        ? "Cancels"
        : "Renews";
      subRenewalValue.textContent = new Date(
        s.current_period_end,
      ).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
      subRenewalValue.classList.toggle(
        "is-cancelling",
        !!s.cancel_at_period_end,
      );
    } else {
      subRenewalRow.hidden = true;
    }

    managePlanBtn.hidden = !(s.billing_live && s.has_billing_account);
  } catch (err) {
    subDashboard.hidden = true;
  }
}

/* The webhook is how the server learns somebody paid, and it arrives a
   few seconds after the overlay closes. Ask every two seconds, for up to
   a minute, until the plan has flipped - then refresh everything that
   shows it. A timeout is not a failure: the payment went through and
   the plan updates on its own; it only means this tab stops asking. */
export async function awaitUpgrade() {
  if (state.upgradePoll) return;
  setError(planError, "");
  planNote.textContent = "Payment received — activating Pro…";
  const started = Date.now();
  state.upgradePoll = true;
  try {
    while (Date.now() - started < 60000) {
      try {
        const res = await fetch("/api/billing/subscription");
        if (res.ok) {
          const s = await res.json();
          if (s.plan === "pro") {
            if (state.currentUser) state.currentUser.plan = "pro";
            state.subscriptionState = s;
            await loadSubscription();
            loadCredits();
            state.upgradePoll = null;
            refreshAuthUI();
            planNote.textContent = "You're on Pro. Thank you.";
            return;
          }
        }
      } catch (_) {
        /* a blip; ask again */
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    planNote.textContent =
      "Payment received — your plan updates within a minute or two. " +
      "Reload if it hasn't.";
  } finally {
    state.upgradePoll = null;
  }
}

export function handleCheckoutReturn() {
  const params = new URLSearchParams(window.location.search);
  const checkout = params.get("checkout");
  if (!checkout) return;
  if (checkout === "success") {
    setError(planError, "");
    planNote.textContent = "Payment received — syncing your plan…";
    awaitUpgrade();
  } else if (checkout === "paddle") {
    // A Paddle checkout was in progress and the browser came back by
    // navigation rather than the overlay. Whether it was paid is not
    // known yet - so ask, and say nothing until the server does.
    if (state.currentUser && state.currentUser.plan !== "pro") awaitUpgrade();
  } else if (checkout === "cancel") {
    setError(planError, "Checkout cancelled — no charge was made.");
  }
  params.delete("checkout");
  const clean =
    window.location.pathname + (params.toString() ? `?${params}` : "");
  window.history.replaceState({}, "", clean);
}

export async function loadAuthState() {
  try {
    const res = await fetch("/api/auth/me");
    const data = await res.json();
    state.currentUser = data.user || null;
    const configured = data.google_configured !== false;
    googleSignInBtn.hidden = !configured;
    googleNotConfiguredNote.hidden = configured;

    // On the desktop build, Google sign-in cannot complete here at all:
    // the redirect_uri must match one registered with Google, and this
    // app takes a fresh random port every launch. So rather than a
    // hidden button and a "not configured" note - which reads as broken
    // - offer the place where it does work.
    if (data.desktop && !configured) {
      googleNotConfiguredNote.hidden = false;
      googleNotConfiguredNote.replaceChildren();
      const line = document.createElement("span");
      line.textContent = "Accounts live on the website. ";
      const link = document.createElement("a");
      link.href = (data.site_url || "https://randomgenerals.com") + "/app";
      link.textContent = "Sign in there";
      // Electron sends any non-loopback URL to the real browser, so this
      // opens outside the app rather than navigating it away.
      link.target = "_blank";
      link.rel = "noopener";
      googleNotConfiguredNote.append(line, link);
    }
  } catch (err) {
    state.currentUser = null;
  }
  refreshAuthUI();
}

export const AUTH_ERROR_MESSAGES = {
  state_mismatch: "That sign-in attempt expired — try again.",
  denied: "Google sign-in was cancelled.",
  google_unreachable: "Could not reach Google — try again in a moment.",
  unverified_email: "That Google account's email isn't verified.",
  not_configured: "Google sign-in isn't configured on this server yet.",
};

/**
 * Arriving from /search with a question already typed.
 *
 * The search page offers "Demander à l'assistant" under its results,
 * which is a promise that the question comes with you. Put it in the
 * composer and leave it there - sending it automatically would answer
 * something the person may still want to reword.
 */
export function handleSearchHandoff() {
  const params = new URLSearchParams(window.location.search);
  const q = (params.get("q") || "").trim();
  if (!q) return;

  // messageInput, not S("messageInput"): S is a `const` declared eight
  // hundred lines below boot(), so calling it here throws "Cannot
  // access 'S' before initialization" - the same temporal dead zone
  // that once stopped boot() running at all and left the splash screen
  // up for every visitor. The try/catch around boot() meant this one
  // only failed silently instead.
  if (messageInput) {
    messageInput.value = q.slice(0, 2000);
    messageInput.dispatchEvent(new Event("input", { bubbles: true }));
    messageInput.focus();
  }

  // Taken out of the address bar, so a reload does not silently refill
  // the box with a question that was already asked.
  params.delete("q");
  const rest = params.toString();
  history.replaceState(
    {}, "", window.location.pathname + (rest ? "?" + rest : ""));
}

export function handleAuthReturn() {
  const params = new URLSearchParams(window.location.search);
  const err = params.get("auth_error");
  if (!err) return;
  setError(googleAuthError, AUTH_ERROR_MESSAGES[err] || "Sign-in failed.");
  params.delete("auth_error");
  const clean =
    window.location.pathname + (params.toString() ? `?${params}` : "");
  window.history.replaceState({}, "", clean);
  openSettings();
}

/** Top-level statements this module's section used to run as the
 *  script loaded - listeners, immediate calls - in the same order.
 *  Called from app.js once every module has been evaluated. */
export function mountBilling() {
  planProBtn.addEventListener("click", () => changePlan("pro"));

  planFreeBtn.addEventListener("click", () => leaveOrKeepPro());

  managePlanBtn.addEventListener("click", async () => {
    managePlanBtn.disabled = true;
    try {
      const res = await fetch("/api/billing/portal", { method: "POST" });
      const data = await res.json();
      if (res.ok && data.portal_url) {
        window.location.href = data.portal_url;
        return;
      }
      setError(planError, data.error || "Could not open the billing portal.");
    } catch (err) {
      setError(planError, "Could not reach the server.");
    } finally {
      managePlanBtn.disabled = false;
    }
  });

}
