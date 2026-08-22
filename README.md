---
title: RandomGenerals AI
emoji: 🌙
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Chat, code, and images - with a sandboxed code runner
---

# RandomGenerals AI

Chat, coding help, image generation, voice input, web search, and memory
that carries between conversations.

## Two ways this runs

**Locally** (a laptop with a GPU): language models run on-device through
[Ollama](https://ollama.com), so prompts never leave the machine.

**Hosted** (this Space): no GPU, so completions come from
[Groq](https://groq.com)'s free tier instead. Same app, same features -
the only difference is where the tokens are generated. `/api/health`
reports which mode is active.

## Configuration

Set these as **Repository secrets** in Space settings (not in the repo):

| Secret | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | **yes** | Chat completions. Free, no card, from [console.groq.com](https://console.groq.com) |
| `SECRET_KEY` | **yes** | Signs session cookies. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GEMINI_API_KEY` | no | Higher-quality image generation |
| `STRIPE_SECRET_KEY` | no | Pro subscriptions |
| `STRIPE_PRICE_ID_PRO` | no | Pro price id |
| `STRIPE_WEBHOOK_SECRET` | no | Verifies Stripe callbacks |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | no | Google sign-in |
| `DB_PATH` | no | Where SQLite lives (see below) |

## Known limitation: storage is ephemeral

The free CPU tier has **no persistent disk**. When the Space rebuilds or
wakes from sleep, `app.db` is recreated empty — accounts, conversations
and credits are lost.

That's fine for a demo and not fine for real users. The fix is an
external database; see `db.py`. Free options that need no credit card
include [Turso](https://turso.tech) (libSQL — SQLite-compatible, so the
change is small) and [Neon](https://neon.tech) or
[Supabase](https://supabase.com) (Postgres, a larger migration).

The Space also sleeps after ~48 hours with no visitors and wakes on the
next request.

## Local development

```bash
pip install -r requirements.txt      # full set, includes the GPU stack
ollama pull llama3.2
python app.py                        # http://127.0.0.1:5000
```

`requirements-cloud.txt` is the trimmed set this container installs — no
torch or diffusers, since neither is usable without a GPU.
