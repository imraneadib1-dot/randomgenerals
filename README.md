# RandomGenerals AI

Chat, coding help, image generation, voice input, web search, a sandboxed
Python runner, and memory that carries between conversations.

Live at **[randomgenerals.com](https://randomgenerals.com)**.

## Two ways this runs

**Locally**, on a machine with a GPU: language models run on-device
through [Ollama](https://ollama.com), so prompts never leave the
computer. `LOCAL_AI.md` has the full audit of what does and doesn't
make outbound network calls.

**Hosted**, on a CPU-only server: no GPU, so completions come from
[Groq](https://groq.com)'s free tier instead. Same app, same features —
only the place the tokens are generated changes.

The app decides this per request rather than at startup. If Ollama stops
answering — the laptop is asleep, or the process died — the next reply
comes from the cloud automatically and is tagged as such in the UI, so a
visitor never sees an error just because a machine went offline.
`/api/health` reports which path is currently live.

## Deploying

`render.yaml` is a [Render](https://render.com) blueprint — free plan, no
credit card. Point Render at this repo, and the only thing to fill in by
hand is `GROQ_API_KEY` (free, no card, from
[console.groq.com](https://console.groq.com)); everything else in the
blueprint is set for you, including a generated `SECRET_KEY`.

### Configuration

Set these as environment variables — never in the repo.

| Variable | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | **yes**, when hosted | Cloud chat completions |
| `SECRET_KEY` | **yes** | Signs session cookies |
| `OLLAMA_URL` | no | Defaults to `http://localhost:11434` |
| `DB_PATH` | no | Where SQLite lives |
| `FORCE_HTTPS_COOKIES` | no | Set to `1` behind TLS |
| `GEMINI_API_KEY` | no | Higher-quality image generation |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID_PRO` / `STRIPE_WEBHOOK_SECRET` | no | Pro subscriptions |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | no | Google sign-in |
| `ALLOW_MOCK_UPGRADE` | no | **Leave unset when hosted** (see below) |

`ALLOW_MOCK_UPGRADE=1` grants Pro without paying, so the plan UI can be
worked on before billing exists. On anything the public can reach it is
a free-Pro button. Only the exact string `1` enables it.

## Known limitation: storage is ephemeral

Render's free plan has **no persistent disk**. On every deploy — and
after the service sleeps, which it does following ~15 minutes of no
traffic — `app.db` comes back empty, losing accounts, conversations and
credits.

Fine for a demo, not fine for real users. The fix is a database that
lives elsewhere; see `db.py`. Free and card-free options include
[Turso](https://turso.tech) (libSQL, SQLite-compatible, so a small
change) and [Neon](https://neon.tech) or
[Supabase](https://supabase.com) (Postgres, a larger migration).

## Local development

```bash
pip install -r requirements.txt      # full set, includes the GPU stack
ollama pull llama3.2
python app.py                        # http://127.0.0.1:5000
```

`requirements-cloud.txt` is the trimmed set a CPU host installs: no torch
or diffusers, since neither is usable without a GPU. That is a ~2.3GB
difference in install size.
