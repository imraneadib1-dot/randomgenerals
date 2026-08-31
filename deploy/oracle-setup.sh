#!/usr/bin/env bash
# Provision an Oracle Cloud Always Free ARM VM to run RandomGenerals AI.
#
# Run ON the VM, once, after SSHing in:
#   curl -fsSL <raw-url>/deploy/oracle-setup.sh -o setup.sh && bash setup.sh
# or copy this file across and: bash oracle-setup.sh
#
# Optional:
#   bash oracle-setup.sh --with-ollama    also install Ollama + a small model
#
#
# WHERE THE ANSWERS COME FROM
#
# The VM has 2 ARM cores and no GPU (Oracle halved the Always Free
# allowance from 4/24GB to 2/12GB in June 2026). That is plenty for the
# web app and nowhere near enough for a good model: a 7B answers at well
# under a word a second here, which reads as broken rather than slow. So
# by default no
# model is installed and replies come from Gemini, which gemini.py has
# supported all along as "the cloud fallback for when this machine is
# off" - and this VM is precisely that case, permanently.
#
# --with-ollama installs Ollama and pulls llama3.2:3b anyway. A 3B on two
# Ampere cores is slow for anything longer than a sentence. It exists so
# the "runs on hardware you control" claim can stay literally true if
# that matters more than speed; Gemini stays the fallback either way.
#
# Torch and diffusers are skipped regardless: ~1GB of wheels for a local
# image model that cannot run acceptably on any CPU.
#
#
# WHY THERE IS NO NGINX AND NO CERTBOT HERE
#
# Traffic arrives through a Cloudflare tunnel, which is an outbound
# connection from this box to Cloudflare. Nothing has to listen on the
# public internet, so there is no port to open, no certificate to renew,
# and no reverse proxy to configure. It also sidesteps the single most
# common way an Oracle deployment stalls: their images ship restrictive
# iptables rules AND the tenancy has its own Security List, and a port
# has to be opened in both before anything reaches it.
set -euo pipefail

WITH_OLLAMA=0
for arg in "$@"; do
  case "$arg" in
    --with-ollama) WITH_OLLAMA=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# id -un as the last resort: under `set -u` an unset $USER aborts the
# whole script with "unbound variable", which is a confusing way to
# fail on a box where the login shell simply did not export it.
APP_USER="${SUDO_USER:-${USER:-$(id -un)}}"
APP_DIR="/opt/randomgenerals"
SERVICE_NAME="randomgenerals"
PORT=5001

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[0;33m!\033[0m   %s\n' "$*"; }

# ---------------------------------------------------------------- packages
log "Installing system packages"
sudo apt-get update -qq
# ffmpeg is not optional: videoedit.py shells out to it for every render,
# and without it the video bay correctly reports itself unavailable -
# which looks like a broken feature rather than a missing package.
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip \
  git curl ufw ffmpeg
ok "python3 $(python3 --version | cut -d' ' -f2)"
ok "ffmpeg  $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"

# ---------------------------------------------------------------- firewall
log "Firewall"
sudo ufw --force reset >/dev/null 2>&1 || true
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null
ok "inbound denied except SSH (the tunnel dials out, so nothing else is needed)"

# ---------------------------------------------------------------- app
log "Application"
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
  ok "updated existing checkout"
else
  warn "No repo yet at $APP_DIR"
  warn "Copy the project there, or: git clone <your-repo> $APP_DIR"
fi

log "Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip

# requirements-cloud.txt rather than a filtered requirements.txt: it is
# the list that is actually kept in step with what a GPU-less host needs,
# and it is the only one that includes gunicorn.
if [ -f "$APP_DIR/requirements-cloud.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements-cloud.txt"
  ok "installed deps from requirements-cloud.txt (no torch/diffusers - no GPU)"
else
  warn "requirements-cloud.txt missing - is $APP_DIR the right checkout?"
fi

# ---------------------------------------------------------------- ollama
if [ "$WITH_OLLAMA" = "1" ]; then
  log "Ollama (optional, CPU-only)"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  sudo systemctl enable --now ollama >/dev/null 2>&1 || true
  # 3B, not 7B. See the note at the top of this file.
  ollama pull llama3.2:3b || warn "model pull failed - run it again by hand"
  ok "ollama serving on 127.0.0.1:11434 with llama3.2:3b"
else
  ok "skipping Ollama - replies come from Gemini (see .env)"
fi

# ---------------------------------------------------------------- secrets
log "Environment"
if [ ! -f "$APP_DIR/.env" ]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  cat > "$APP_DIR/.env" <<ENVEOF
# RandomGenerals AI - Oracle Always Free VM.

# Signs session cookies. Generated once at install; changing it signs
# everyone out.
SECRET_KEY=$SECRET

# The model. Without this there is nothing to answer with and every
# prompt fails at the point of asking.
#   https://aistudio.google.com/apikey
GEMINI_API_KEY=

# Local models. Left pointing at localhost either way: with --with-ollama
# there is an Ollama here to answer, and without one the app finds
# nothing, reports the local channel as unavailable, and uses Gemini -
# which is the intended behaviour, not a failure.
OLLAMA_URL=http://localhost:11434

# Cloudflare terminates TLS at its edge and forwards over the tunnel, so
# these two are always right here. TRUST_PROXY is what lets Flask build
# the correct https:// redirect_uri for Google sign-in.
FORCE_HTTPS_COOKIES=1
TRUST_PROXY=1

# Deliberately empty. Setting it turns the Pro upgrade button into a free
# "make me Pro" switch - fine on a laptop, not on a public address.
ALLOW_MOCK_UPGRADE=

# ---------------------------------------------------------------- billing
# Paddle, not Stripe. Paddle is a merchant of record and reaches places
# Stripe does not, which is the whole reason it is here.
#
# sandbox and production are entirely separate systems with separate
# dashboards, keys and price IDs. Keys from one against the other's host
# is an auth failure that names neither, so change this only when you
# have production credentials to go with it.
PADDLE_ENV=sandbox

# Paddle > Developer tools > Authentication > API keys.
PADDLE_API_KEY=

# Paddle > Catalog > Products > (your Pro price) - the pri_... id.
PADDLE_PRICE_ID_PRO=

# Paddle > Developer tools > Notifications > (your endpoint) > secret key.
# Without it checkout still works and nobody is ever upgraded, which is
# the failure that looks like the payment vanished.
PADDLE_WEBHOOK_SECRET=

# Paddle > Developer tools > Authentication > Client-side tokens.
# A different credential from the API key, and meant to be public - it
# ships inside the page. Paddle Billing has no hosted checkout page to
# redirect to; its checkout is an overlay Paddle.js opens on your own
# site, and Paddle.js cannot start without this. Missing, the flow
# creates a transaction and then visibly does nothing.
PADDLE_CLIENT_TOKEN=

# Stripe, kept for regions where it is preferable. Optional.
STRIPE_SECRET_KEY=
STRIPE_PRICE_ID_PRO=
STRIPE_WEBHOOK_SECRET=
ENVEOF
  chmod 600 "$APP_DIR/.env"
  ok "created $APP_DIR/.env with a fresh SECRET_KEY"
  warn "fill in GEMINI_API_KEY before starting the service"
else
  ok ".env already present (left untouched)"
fi

# ---------------------------------------------------------------- service
log "systemd service"
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=RandomGenerals AI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=PORT=$PORT
Environment=APP_DEBUG=0

# gunicorn, not app.py. Flask's built-in server is single-process and
# explicitly not for public traffic - app.py's own __main__ block says so.
#
# One worker, several threads, and a long timeout, for the reasons
# documented in render.yaml: db.py holds a single module-level SQLite
# connection that is thread-safe within one process but would become one
# connection per process if workers scaled; a streamed reply spends its
# life waiting on the model rather than on CPU, so threads suit it; and
# gunicorn's 30s default would cut off any answer still being written.
ExecStart=$APP_DIR/.venv/bin/gunicorn app:app \\
  --bind 127.0.0.1:$PORT \\
  --worker-class gthread \\
  --workers 1 \\
  --threads 8 \\
  --timeout 300 \\
  --access-logfile - \\
  --error-logfile -

Restart=always
RestartSec=5

# journalctl rather than a file that grows until the disk is full.
StandardOutput=journal
StandardError=journal

# Hardening - the app never needs to write outside its own directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
ok "service installed and enabled at boot"

# ---------------------------------------------------------------- tunnel
log "Cloudflare tunnel"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)     # arm64 on Ampere
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
  sudo dpkg -i /tmp/cloudflared.deb >/dev/null
  ok "cloudflared installed ($ARCH)"
else
  ok "cloudflared already present"
fi

cat <<NEXTEOF

------------------------------------------------------------------
Next steps - these need your credentials, so they are not automated.

  1. Add the model key:
       nano $APP_DIR/.env          # set GEMINI_API_KEY
     Get one at https://aistudio.google.com/apikey

  2. Start it and check it locally:
       sudo systemctl start $SERVICE_NAME
       curl -s localhost:$PORT/api/health
       journalctl -u $SERVICE_NAME -f      # if it does not come up

  3. Point the tunnel here instead of the laptop. On the LAPTOP, copy
     ~/.cloudflared/<tunnel-id>.json and cert.pem to this VM's
     ~/.cloudflared/, then here:
       sudo cloudflared service install
       sudo systemctl start cloudflared

     Only one machine may serve a tunnel at a time. Stop the laptop's
     tunnel first, or requests land unpredictably on either.

  4. Confirm from anywhere:
       curl -s https://randomgenerals.com/api/health

  Afterwards, the laptop can be switched off. What changes: image
  generation falls back to the hosted API, and the local-model channel
  reports itself unavailable unless you passed --with-ollama. Chat,
  code, the sandboxed runner, the video bay, accounts, memory, credits
  and web search all keep working.
------------------------------------------------------------------
NEXTEOF

log "Done"
