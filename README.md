pip 1.0
An assistant built and owned by Imrane Adib, designed for chat, code, images, diagrams, a Python sandbox, web search, file reading, persistent memory across conversations, accounts with 2FA, and Pro subscriptions via Paddle.

Under the hood, it’s three pieces in one repo:

Layer	What it actually does	Where it lives
The model	pip 1.0: a Qwen3.5-4B base paired with a small LoRA adapter trained here	finetune/, pip_model.py
The engine	Loads the weights, exposes an OpenAI-compatible API, and manages its own SQLite working store	pip_server.py, pip_store.py
The app	A Flask web app with a vanilla-JS frontend that talks to the engine and falls back cleanly when needed	app.py, static/, templates/
The Model
The base is Qwen3.5-4B (Apache 2.0, 262,144-token context window)—roughly the largest current open-weight model you can realistically fine-tune and serve on a single 8 GB GPU. On top of that sits a LoRA adapter touching less than 1% of the parameters, trained to anchor its identity, keep everyday answers normal, and produce the structured formats the app expects. You’ll find the base model’s license notice in finetune/NOTICE.

Identity (Three Layers, Because One Is Never Enough)
Ask who owns, made, built, or trained pip, and it’s supposed to give one clear answer: Imrane Adib. Relying on a single layer leaks, so pip_identity.py enforces it in three places:

System prompt: Injected at the start of every conversation across every channel.

Weights: Trained directly on hundreds of owner questions across five languages—half of them with no system prompt at all.

Output guard: Inspects completed replies to owner-related questions. If the answer doesn’t credit Imrane Adib—or credits someone else—it swaps in the canonical line instead. Because this guard runs in both the engine and the app, it even catches responses coming back from backup providers.

Getting It Running
Bash
pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY is the only thing you must set

python pip_server.py          # engine at http://127.0.0.1:8765/v1
python app.py                 # web app at http://127.0.0.1:5000
python pip_cli.py             # interactive CLI against the engine
You need weights for the engine to start, though if you haven’t trained an adapter yet, it can still run the base model while relying on the prompt and guard for identity. And if the engine isn’t running at all, the web app doesn’t just crash—it routes through the backup channels below.

Fine-Tuning
Bash
python -m finetune.pip_data            # build the training dataset (also runs automatically if needed)
python -m finetune.train_pip           # full run: Qwen3.5-4B in 4-bit (needs a GPU)
python -m finetune.train_pip --smoke   # quick CPU smoke test on a 0.5B base (a few minutes)
The full training run requires a CUDA-enabled torch and bitsandbytes; if you don’t have them, it exits early rather than grinding a 4B model on a CPU. You can pass --base <hf-model-id> if you have the VRAM for a larger base.

Full runs save to finetune/pip-1.0-adapter/ (loaded automatically by pip_server.py).

Smoke runs save to finetune/pip-1.0-adapter-smoke/—run it with python pip_server.py --adapter finetune/pip-1.0-adapter-smoke.

Once training finishes, the script tests held-out owner questions against the weights alone (no system prompt, no guard), prints the pass rate, and records it in the adapter’s pip.json.

Routing & Fallbacks
Before any LLM touches a message, a compact NumPy classifier in brain/ (hand-derived gradients, single file) scores the input and picks which bay should handle it.

From there, pip 1.0 always gets first shot. Because a single GPU can only juggle a handful of concurrent requests, the engine pushes back with Retry-After when saturated so the app can fail over before a request errors out. A busy or restarting engine makes the site slower, not broken.

Channel	What it runs	When it’s used
pip	pip 1.0 from the engine	First choice in every bay
OpenRouter	DeepSeek for code plus select free models	When the engine is busy or unavailable (paid models have a daily spend cap)
Ollama	Whatever local models you have installed	Last-resort fallback when nothing else can answer
Routes live in BAY_ROUTES in app.py as ordered (channel, model_pattern) lists per bay, picking the first available match. Backup channels are completely optional—if you leave OPENROUTER_API_KEY unset and keep Ollama off, pip is the only active channel.

Storage
There are two SQLite databases, both running in WAL mode with one connection per thread:

app.db (db.py): The application’s source of truth—user accounts, sessions, credits, billing records, conversation threads, and cross-session memory.

pip.db (pip_store.py): Ephemeral working state for the engine—server-side conversation history (for clients passing a conversation_id instead of full transcripts), cached token counts so long contexts don’t get re-counted on every turn, and runtime metadata. Safe to delete without losing user data.

Testing & Verification
Bash
python check_pip.py            # checks identity, DBs, engine, channels, and dataset
python check_pip.py --model    # also queries the trained weights directly (slow on CPU)
To catch quiet regressions across billing, rate limits, streaming, routing, and webhooks:

Bash
for f in check_*.py; do OLLAMA_URL=http://127.0.0.1:1 python $f || echo "FAILED $f"; done
npx tsc -p jsconfig.json
(Note: check_paddle.py, check_mail.py, and check_webhook_security.py hit external services or require live secrets, so run those on the server.)

Repository Map
pip_identity.py — Identity constants, owner-question detection, and the output guard

pip_model.py — Base model and LoRA adapter loader

pip_server.py — OpenAI-compatible inference server

pip_store.py — Engine SQLite store (pip.db)

pip_api.py — App-side client for the pip channel

pip_cli.py — Terminal client for the engine

finetune/ — Dataset generator (pip_data.py), PEFT LoRA trainer (train_pip.py), and NOTICE

app.py — Flask routes, BAY_ROUTES, and streaming chat handler

db.py — App SQLite layer (app.db) with boot-time migrations

openrouter_api.py — OpenRouter backup client with daily spend enforcement

providers.py — Standardized error/backoff contract for clean failover

paddle_billing.py — Subscription and webhook logic (Paddle as merchant of record)

videogen.py — Persistent video generation jobs, poller, and validation

model3d.py — Image/text-to-3D pipeline via TRELLIS.2, job tracking, and GLB checks

agents/ — Task router, code-execution verifier, and evaluation helpers

brain/ — Lightweight NumPy intent classifier (see brain/README.md)

connectors.py — Turns pasted links into callable tools for the model

keystore.py — AES-GCM encryption for provider keys; RFC 6238 TOTP

tools.py — Web search, Python sandbox, and image generation

static/js/ — Unbundled ES modules using an import map for version control

desktop/ — Electron wrapper (Windows builds via GitHub Actions)

mobile/ — Capacitor shell for iOS and Android

deploy/ — Oracle VM setup script and deployment notes (deploy/ORACLE.md)

3D Model Generation
Turn an image or text prompt into a textured .glb asset powered by Microsoft’s TRELLIS.2 (MIT). You can trigger it via the 3D mode button, + > Make a 3D model, or naturally in chat (like attaching an image and asking for a 3D model, or requesting "a 3D model of a red teapot"). Completed items render inline with a live preview and GLB download, and stay saved in the studio history.

Where it runs: TRELLIS.2 wants a 24 GB NVIDIA GPU, so jobs offload to TRELLIS_PROVIDERS in model3d.py—defaulting to the free Hugging Face Space, or optionally fal.ai (with FAL_KEY) or a self-hosted TRELLIS.2 app.py instance.

Pricing & quotas: The HF Space is free with a shared daily GPU quota (add HF_TOKEN for higher limits; expect a "try again later" message when exhausted). fal.ai runs roughly $0.25 / $0.30 / $0.35 per generation at 512 / 1024 / 1536 resolution.

Daily limits: Free accounts get 2 models/day and Pro accounts get 10/day (features.py). Failed jobs refund your daily count.

Text-to-3D flow: Generates a clean single-object reference image on a plain background first, keeps that image alongside the job, and reconstructs the 3D mesh from it.

Output format: Binary glTF 2.0 (.glb) normalized to a unit cube centered at the origin, typically ~200k faces with 2K WebP textures (EXT_texture_webp). Opens cleanly in Blender 4.x/5.x (File > Import > glTF 2.0). Materials export opaque by default—wire the alpha channel manually if your mesh has transparent regions.

Testing: Run python check_model3d.py to test the pipeline end-to-end with a mock provider. Real runs consume quota, so test those manually in the studio.

Deployment
The web app is built to run on a lightweight VM under gunicorn + systemd behind a Cloudflare Tunnel (deploy/ORACLE.md). Since that VM has no GPU, point it at a separate GPU machine running the engine via PIP_ENGINE_URL and PIP_ENGINE_KEY.

After initial setup, shipping updates is just running bash deploy.sh on the VM—it pulls the latest changes, restarts the service, and checks the live page’s build ID to confirm the new build is actually serving.

Honest Limitations
4B scale: Fits an 8 GB GPU for both training and serving, which is great for targeted tasks—not a frontier model. Use --base when you have hardware for a larger checkpoint.

Text-only engine (for now): Even when the base model supports vision, the engine doesn’t pass images through yet, so image inputs route to a vision-capable backup channel.

Single-GPU concurrency: Handles a small number of requests at a time—appropriate for current traffic, with failover handling spikes.

Single-process SQLite: One process and one SQLite file per service—simple, reliable, and well-matched to current scale.

License
Application & repository code: MIT (LICENSE)

Base model (Qwen3.5-4B): Apache 2.0 (finetune/NOTICE)
