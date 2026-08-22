#!/usr/bin/env bash
# Provision an Oracle Cloud Always Free ARM VM to run RandomGenerals AI.
#
# Run ON the VM, once, after SSHing in:
#   curl -fsSL <raw-url>/deploy/oracle-setup.sh -o setup.sh && bash setup.sh
# or copy this file across and: bash oracle-setup.sh
#
# WHAT RUNS HERE VS ON THE LAPTOP
# The VM has no GPU, so Ollama is deliberately NOT installed - a 7B model
# on 2 ARM cores answers at roughly a word per second, which is worse
# than useless. Chat comes from Groq instead (free tier, already wired
# up in app.py) and image generation from a cloud API. Everything else -
# accounts, threads, memory, credits, the code sandbox, web search -
# runs here exactly as it does locally.
#
# Torch and diffusers are also skipped: ~1GB of wheels for a local image
# model that can't run acceptably here anyway.
set -euo pipefail

APP_USER="${SUDO_USER:-$USER}"
APP_DIR="/opt/randomgenerals"
PY_VERSION_MIN="3.10"
SERVICE_NAME="randomgenerals"
PORT=5001

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[0;33m!\033[0m   %s\n' "$*"; }

# ---------------------------------------------------------------- packages
log "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip \
  git curl ufw
ok "python3 $(python3 --version | cut -d' ' -f2)"

# ---------------------------------------------------------------- firewall
# Oracle's images ship with a restrictive iptables config AND the tenancy
# has its own Security List. Both must allow the port, but since traffic
# arrives through the Cloudflare tunnel (outbound-initiated), nothing
# needs to be open to the internet at all - which is the safer default.
log "Firewall"
sudo ufw --force reset >/dev/null 2>&1 || true
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null
ok "inbound denied except SSH (tunnel is outbound, so nothing else needed)"

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

# Install only what the server actually needs. requirements.txt includes
# torch/diffusers for local image generation; those are filtered out.
if [ -f "$APP_DIR/requirements.txt" ]; then
  grep -viE '^(torch|diffusers|transformers|accelerate|safetensors)' \
    "$APP_DIR/requirements.txt" > /tmp/req-cloud.txt || true
  "$APP_DIR/.venv/bin/pip" install --quiet -r /tmp/req-cloud.txt
  ok "installed deps (torch/diffusers skipped - no GPU here)"
fi

# ---------------------------------------------------------------- secrets
log "Environment"
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'ENVEOF'
# Cloud deployment. Fill these in before starting the service.
OLLAMA_URL=http://localhost:11434   # unused here; no local models
GROQ_API_KEY=
SECRET_KEY=
STRIPE_SECRET_KEY=
STRIPE_PRICE_ID_PRO=
STRIPE_WEBHOOK_SECRET=
GEMINI_API_KEY=
FORCE_HTTPS_COOKIES=1
ALLOW_MOCK_UPGRADE=
ENVEOF
  chmod 600 "$APP_DIR/.env"
  warn "created $APP_DIR/.env - fill in GROQ_API_KEY and SECRET_KEY"
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
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=5
# Oracle's free tier stops instances that look idle; a service that
# restarts forever also keeps the box demonstrably in use.
StandardOutput=append:$APP_DIR/app.log
StandardError=append:$APP_DIR/app.log

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
  ARCH=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
  sudo dpkg -i /tmp/cloudflared.deb >/dev/null
  ok "cloudflared installed"
else
  ok "cloudflared already present"
fi

cat <<'NEXTEOF'

------------------------------------------------------------------
Next steps (these need your credentials, so they aren't automated):

  1. Fill in secrets:
       nano /opt/randomgenerals/.env
     At minimum: GROQ_API_KEY, and a SECRET_KEY from
       python3 -c "import secrets; print(secrets.token_hex(32))"

  2. Start the app:
       sudo systemctl start randomgenerals
       curl -s localhost:5001/api/health

  3. Point the tunnel here instead of the laptop. On the LAPTOP, copy
     ~/.cloudflared/<tunnel-id>.json and cert.pem to this VM's
     ~/.cloudflared/, then:
       sudo cloudflared service install
       sudo systemctl start cloudflared

     Only one machine may serve a tunnel at a time - stop the laptop's
     tunnel first, or requests will land unpredictably on either.

  4. Confirm from anywhere:
       curl -s https://randomgenerals.com/api/health

------------------------------------------------------------------
NEXTEOF

log "Done"
