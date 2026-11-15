# pip 1.0

An AI assistant with its own model. Chat, code, images, diagrams, a
Python sandbox, web search, file reading, memory across conversations,
accounts with 2FA, and a Pro plan through Paddle. Owned and built by
Imrane Adib.

It is three things in one repository:

| Part | What it is | Where |
| --- | --- | --- |
| The model | pip 1.0: an open-weight base plus a LoRA adapter trained here | `finetune/`, `pip_model.py` |
| The engine | Loads the weights and serves them over an OpenAI-compatible API, with its own SQLite store | `pip_server.py`, `pip_store.py` |
| The app | A Flask site with a plain-JavaScript front end that talks to the engine | `app.py`, `static/`, `templates/` |

## The model

The base is **Qwen3.5-4B** (Apache 2.0, a 262,144-token context
window): the largest current open-weight model that can be both
fine-tuned and served on one 8 GB GPU. What is trained on top of it is a
LoRA adapter — under one percent of the parameters — on a dataset that
teaches it who it is, keeps ordinary questions answered ordinarily, and
drills the structured output the app asks for. `finetune/NOTICE` carries
the base model's licence notice.

Whoever asks who owns, made, built or trained it gets one answer: **its
owner is Imrane Adib.** That is held in three layers, because each
alone leaks (`pip_identity.py`):

1. **The prompt** opens every conversation on every channel.
2. **The weights**: several hundred owner questions, in five languages,
   are trained in, half of them with no system prompt at all.
3. **The guard** reads the finished reply to an owner question and, if
   it does not name the owner or names anyone else as the maker, sends
   the canonical sentence instead. It runs in the engine and again in
   the app, so it also covers a reply that came from a backup channel.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY is the only required value

python pip_server.py          # the engine   http://127.0.0.1:8765/v1
python app.py                 # the app      http://127.0.0.1:5000
python pip_cli.py             # or talk to the engine from a terminal
```

The engine needs the weights. Until an adapter has been trained it
serves the base model alone, with the owner answer still enforced by
the prompt and the guard. With no engine running, the app still works:
it answers from the backup channels below.

## Fine-tuning

```bash
python -m finetune.pip_data            # write the dataset (also done on demand)
python -m finetune.train_pip           # the real run: Qwen3.5-4B, 4-bit, needs a GPU
python -m finetune.train_pip --smoke   # a 0.5B base on the CPU, a few minutes
```

The real run needs a CUDA build of torch and `bitsandbytes`; the script
says so and stops rather than starting a 4B model on a CPU. `--base`
takes any Hugging Face model id, so a bigger base can be trained on a
machine with the memory for it. The adapter is written to
`finetune/pip-1.0-adapter/`, which the engine loads by default; the
smoke run writes to `finetune/pip-1.0-adapter-smoke/`
(`python pip_server.py --adapter finetune/pip-1.0-adapter-smoke`).

After training, the held-out owner questions are put to the weights
alone — no system prompt, no guard — and the share that name the owner
is printed and recorded in the adapter's `pip.json`.

## Where the answers come from

pip 1.0 is first in every bay. One GPU answers a few requests at a
time, so the engine refuses the rest with a `Retry-After`, and the app
moves that request to a backup channel before it fails rather than
after. A busy or restarting engine is a slower site, not a broken one.

| Channel | What it runs | When |
| --- | --- | --- |
| pip | pip 1.0, from the engine | always first |
| OpenRouter | DeepSeek for code, a few free models | when the engine cannot answer; paid models have a daily ceiling |
| Ollama | whatever is installed locally | the floor, when nothing else answers |

The routing table is `BAY_ROUTES` in `app.py`: a ranked list per bay of
`(channel, model-name-pattern)`, and the first one that actually exists
wins. The backup channels are optional — leave `OPENROUTER_API_KEY`
unset and stop Ollama, and pip 1.0 is the only thing that answers.

Before any of those models sees a message, a tiny network in `brain/`
reads it first — plain NumPy, gradients derived by hand, one file — and
decides which bay it belongs to.

## The database

Two SQLite files, both in WAL mode with one connection per thread:

- `app.db` (`db.py`) is the product: accounts, sessions, credits,
  billing, people's threads, memories.
- `pip.db` (`pip_store.py`) is the engine's working state:
  conversations it keeps for callers that send a `conversation_id`
  instead of their whole history, a cache of token counts so fitting a
  long conversation into the context window does not re-count every
  turn on every request, and metadata about what is being served. It
  can be deleted without losing anything a person owns.

## Checks

```bash
python check_pip.py            # identity, database, engine, channel, training data
python check_pip.py --model    # also asks the real trained weights (slow on a CPU)
```

The rest of the suite covers the parts of the app that fail quietly —
billing lifecycle, rate limits, streaming, the reply path, webhooks:

```bash
for f in check_*.py; do OLLAMA_URL=http://127.0.0.1:1 python $f || echo "FAILED $f"; done
npx tsc -p jsconfig.json
```

`check_paddle.py`, `check_mail.py` and `check_webhook_security.py` need
network or keys and belong on the server.

## What's where

```
pip_identity.py    the name, the owner, the owner-question detector, the guard
pip_model.py       loads the base model and the adapter
pip_server.py      the engine: OpenAI-compatible API over the weights
pip_store.py       the engine's SQLite store
pip_api.py         the app's client for the engine (the "pip" channel)
pip_cli.py         the engine from a terminal
finetune/          pip_data.py (dataset), train_pip.py (LoRA with PEFT), NOTICE
app.py             routes, the routing table, chat streaming
db.py              the app's SQLite, migrations on boot
openrouter_api.py  the hosted backup channel, with a daily spend ceiling
providers.py       how every channel must fail, so failover works
paddle_billing.py  subscriptions (Paddle is the merchant of record)
videogen.py        video job engine: persistent jobs, a poller, validation
agents/            router, verifier (code is run before it is trusted), evaluate
brain/             the intent model - see brain/README.md
connectors.py      paste a link, it becomes a tool the model can call
keystore.py        AES-GCM for provider keys; TOTP from RFC 6238
tools.py           web search, Python runner, image generation
static/js/         ES modules, no bundler, an import map for versioning
desktop/           Electron build (Windows installer via GitHub Actions)
mobile/            Capacitor shell for iOS and Android
deploy/            the Oracle VM setup script and notes
```

## Deploying

The web app runs on one small VM, gunicorn under systemd, with a
Cloudflare tunnel in front (`deploy/ORACLE.md`). That VM has no GPU, so
the engine runs on a machine that has one and the app is pointed at it
with `PIP_ENGINE_URL` and `PIP_ENGINE_KEY`. Every update after the first
is `bash deploy.sh` on the VM, which pulls, restarts, and then reads the
build id out of the live page to prove the new code is what is serving.

## Limitations, honestly

- **The model is 4B parameters.** That is what one 8 GB GPU can train
  and serve. It is good at what it was tuned for and it is not a
  frontier model; a bigger base needs a bigger GPU, and `--base` is
  there for the day there is one.
- **The engine is text-only for now.** The base model can read images;
  the engine does not pass them through yet, so an attached picture goes
  to a backup channel that can see.
- **One GPU, one reply at a time.** Right for the traffic it has.
- **One process, one SQLite file per service.** Also right for the
  traffic it has.

## Licence

The code is MIT — see [LICENSE](LICENSE). The base model is Apache 2.0 —
see [finetune/NOTICE](finetune/NOTICE).
