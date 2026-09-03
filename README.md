# RandomGenerals AI

A chat, coding, image and diagram assistant that runs on one free
virtual machine — two ARM cores, 11.9 GB of RAM, no GPU.

**Live at [randomgenerals.com](https://randomgenerals.com)**

Chat and code answers, image generation, Mermaid diagrams, a sandboxed
Python runner, web search, file and image reading, memory that carries
between conversations, accounts with two-factor authentication, and
subscriptions through Paddle. It installs as an app from the browser.

---

## How it answers

Three channels, tried in order, so one being unavailable is a
degradation rather than an outage.

| Channel | Model | Cost |
| --- | --- | --- |
| Groq | `openai/gpt-oss-120b` — 120B parameters | free |
| OpenRouter | free vision + coding models; Kimi if funded | free tier / paid |
| Ollama | whatever is installed locally | free, needs a GPU to be bearable |

The routing table in `app.py` (`BAY_ROUTES`) is a ranked list per bay.
Each entry is `(provider, name-pattern)` and the first one actually
present wins, so the same code serves a laptop with four local models
and a GPU-less VM with none — it simply falls further down the list.

**The binding limit is 8,000 tokens per minute**, shared across every
visitor on one Groq key. Measured from the live rate-limit headers, not
quoted from documentation. `groq_api.py` tracks the remaining budget and
`app.py` routes to a local model *before* the ceiling rather than after
it, so being rate-limited never surfaces as an error.

## Things that were harder than they looked

Written down because each cost real debugging, and the reasons are in
the code as comments where they apply.

**Accuracy was leaking through an absence.** The default chat mode set no
temperature, so it inherited Groq's documented default of 1.0 — maximum
sampling randomness on questions with one right answer. An 18-question
graded benchmark went from 16/18 to 18/18 by setting it to 0.2. The bug
was invisible because it was a missing value rather than a wrong one.

**The Instagram link was broken for every visitor.** Flask issues a
session cookie with no expiry unless the session is marked permanent, and
in-app browsers tear that context down between navigations. So the page
created a thread as one guest, the cookie vanished, and the message
arrived as another — `Unknown thread`, on everyone's first message.

**A model will invent a tool result.** Asked for `4839 * 2718` with no
tools available, it wrote *"running this in Python gives us 13287002"* —
a number it made up, and the wrong one. Attributing an invented answer to
a tool is worse than an ordinary mistake, because it tells the reader the
number was checked.

**"Fetch any URL the user pastes" is a hole, not a feature.** This VM has
a metadata service at `169.254.169.254` that hands credentials to
anything that asks. `connectors.py` resolves every hostname and refuses
private, loopback and link-local addresses — on the first request, on
every redirect hop, and on every later call.

**A grader can be wrong more often than the model.** An early benchmark
scored a model 3/6 twice; both times the model was right and the grader
could not read a bare `9` or a LaTeX fraction. It now self-tests against
eleven answer formats and refuses to report a score if that fails.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY is the only required value
python app.py                 # http://127.0.0.1:5001
```

Everything is optional beyond `SECRET_KEY`. With no keys at all it runs
against a local Ollama; with `GROQ_API_KEY` it uses the fast channel.
`.env.example` documents each one and what breaks without it.

Three scripts answer "is this actually working?" without guesswork:

```bash
python check_mail.py you@example.com   # will verification codes arrive?
python check_paddle.py                 # can it take real money?
bash deploy.sh                         # deploy, then prove it landed
```

## Layout

```
app.py            routes, routing table, chat streaming   (~5,000 lines)
db.py             SQLite, 18 tables, WAL
groq_api.py       fast channel + per-minute budget tracking
openrouter_api.py Kimi and the free models, with a daily spend ceiling
connectors.py     paste a link, it becomes a tool the model can call
keystore.py       AES-GCM for provider keys; TOTP from RFC 6238
tools.py          web search, Python runner, image generation
attachments.py    file reading, with a content sniffer
features.py       one table deciding what each plan can do
static/sw.js      service worker — caches the shell, never the API
mobile/           Capacitor shell for iOS and Android
```

## Deploying

Built for an Oracle Always Free ARM VM behind a Cloudflare Tunnel.
`deploy/ORACLE.md` has the full setup. `deploy.sh` does updates and
checks the live page afterwards for markup that only exists in the new
build — so it cannot report success when a stale process is still
serving.

## Honest limitations

- **No GPU**, so local models are slow enough that the hosted channel is
  the real product. Vision needs an OpenRouter key; nothing on Groq can
  read an image.
- **Video generation is not available.** Every free route was measured:
  Hugging Face allows about three clips a month site-wide, PixVerse has
  no free tier, and a Tripo account's balance came back 0. Diagrams took
  that slot because Mermaid renders in the browser and is therefore free
  at any volume.
- **One process, one SQLite file.** Correct for the traffic it has;
  it would need work before it was correct for much more.

## Licence

MIT — see [LICENSE](LICENSE).
