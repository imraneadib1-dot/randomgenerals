# RandomGenerals — devlog

A hosted AI assistant. Chat, code, image generation and diagrams, running
on an Oracle Always Free ARM VM behind a Cloudflare tunnel, with accounts,
credits, a subscription tier and a desktop build.

**Live:** https://randomgenerals.com
**Started:** 22 August 2026 · **Launched:** 30 August 2026

| | |
|---|---|
| Commits | 126 |
| Lines of code | 33,711 (Python 16.2k · JS 7.4k · CSS 6.5k · HTML 3.5k) |
| Distinct visitors | 314 |
| Page views | 1,169 |
| Conversations | 51 |
| Accounts | 8 |

Numbers are read from the app's own dashboard, which counts on the server
— no third-party analytics, no tracking cookie.

---

## What it is

One Flask app serving a website, a PWA, and the backend for an Electron
desktop build. SQLite for storage. Replies come from `gpt-oss-120b` on
Groq's free tier, with a local Ollama model as failover, image generation
through a keyless hosted API, and diagrams rendered client-side from
Mermaid the model emits.

Everything a real product needs turned out to be the hard part: accounts,
sessions you can revoke from another device, a credit system, per-tier
feature flags, verification and password-reset emails, a Paddle
integration, terms/privacy/refund pages, and an owner dashboard.

---

## Things that were harder than expected

### A rate limit measured in tokens per minute, not requests

The free tier allows **8,000 tokens per minute for the whole key** —
shared by every visitor at once. Almost every hard bug traces back to
that number.

Groq counts `prompt + max_tokens` **together** for a single request and
refuses with 413 when the sum is over. Nothing was measuring the sum, so
once a conversation grew past about 5,400 tokens it could not be answered
at all — every retry rebuilt the same oversized request and was refused
again. The fix trims old turns before it trims the reply ceiling: dropping
context from six messages ago costs some memory of the conversation,
while capping the reply costs the answer itself.

### "Installed" is not "usable" — three times

The same mistake in three places, and it took all three to see the
pattern.

The router asked *is a local model reachable?* and treated yes as *can it
serve this?* The boot warm-up measured that by sending `"hi"` and asking
for **one token**, which returns in 0.7 seconds on this hardware. So the
answer was always yes.

What actually happens there, measured at three prompt sizes:

```
 2,000 chars → first token after 90s (timed out)
 6,000 chars → first token after 55.7s
12,000 chars → first token after 68.5s
```

The machine was never fast. The probe was measuring the one request shape
it can do quickly — reading the prompt is most of the cost for a small
model on two shared cores, so a probe with no prompt measures almost
nothing about serving one.

The same error had already cost image understanding: `gemma3:4b` took 98
seconds to describe a 200×200 square, 91 of them before the first token,
so an attached image meant a two-minute wait and a timeout. Both now
measure something representative and refuse when the answer is no — an
instant "this deployment can't see images" beats a two-minute hang.

### A truncated reply that looked finished

Neither streaming channel read `finish_reason`. A reply that hit
`max_tokens` ended mid-sentence and looked exactly like one that had
finished. `POST /api/chat` returned **200 on every single request** while
this was happening, so nothing in the logs pointed at it.

### Two 500s from races

`/api/usage` threw `sqlite3.InterfaceError: bad parameter or other API
misuse` intermittently. One SQLite connection was shared across eight
gunicorn threads with only the *writes* locked — reads went straight at
the shared object. Each thread gets its own connection now, which is what
WAL was already enabled for.

`/api/credits` threw `UNIQUE constraint failed: users.email`. Signup and
the Google callback both did "check if the email exists, then create" with
nothing in between; two requests arriving together both saw no account and
both made one. `save_users()` deletes and reinserts every row, so from
then on **every save failed** — not just the one that caused it.

### A 5xx that a proxy ate

"Upgrade to Pro" reported *"Could not reach the server"* about a server
that had answered in full. The route returned **502** when the payment
provider declined, and Cloudflare replaces 5xx bodies with its own error
page — so the JSON never arrived and the fetch fell into its network-error
branch. A provider declining is not a gateway failure; 400 passes through
untouched.

### Secrets in the Docker image

There was no `.dockerignore`, so `COPY . .` took the whole working
directory — including `.env`, with live API keys, and `app.db`, with real
accounts. Deleting a file in a later layer does not remove it from an
earlier one.

---

## Deliberate decisions

**Visitor counting without tracking.** Daily uniques are a hash of address
and user agent salted with a value destroyed every night, so the count is
real and nobody can be followed from one day to the next. All-time uniques
come from the session id the app already sets — no new cookie. `/privacy`
describes both, because code doing something the published policy does not
mention is the failure worth avoiding.

**Diagrams are the floor, not the fallback.** The fourth bay draws
diagrams unless a video backend is genuinely usable *by that person* —
plan included. Offering video and then saying "Pro feature" spends a whole
bay on an advert while the mode that needs no key sits behind it.

**The desktop app sends accounts and billing to the website.** Google
OAuth needs a registered redirect URI and the app takes a random port each
launch; Paddle checkout only opens on approved domains. Neither is a
missing key, so the app links out rather than showing dead controls.

---

## Where it stands

Working: chat, code, images, diagrams, accounts, Google sign-in, sessions,
credits, memory, connected apps, file and folder upload, PWA install, a
Windows desktop build, transactional email, an owner dashboard.

Not working: **payments.** Paddle reviewed the domain and declined —
generative AI is outside their acceptable-use policy. The integration is
complete and verified against their live API; the account is not approved,
so nobody can pay. Next step is a provider that accepts AI products and
pays out to Morocco.

---

## How this was built

Written with heavy use of an AI coding assistant (Claude), working in the
repository — writing code, reading logs, running the test scripts, and
deploying. The direction, the decisions about what to build, the testing
against real usage and the debugging of what came back were mine.

Several of the bugs above were found because something did not work when I
used it and I pushed back until the actual cause was found rather than the
first plausible one — the local-model probe took four wrong diagnoses
before anyone measured what the hardware actually does.
