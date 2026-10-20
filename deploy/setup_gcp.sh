#!/usr/bin/env bash
# Run ON THE VM from the unzipped project:  bash deploy/setup_gcp.sh
# Installs dependencies, creates .env (asks for a dashboard password), sets up systemd services
# and an nginx reverse proxy on ports 80 (http) and 443 (self-signed https).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(id -un)"
cd "$APP_DIR"

echo ">> Stopping any older NiftyBot services (safe if none exist)"
sudo systemctl disable --now niftybot-web niftybot-worker 2>/dev/null || true

echo ">> Installing packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip nginx openssl
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  read -r -s -p "Choose a dashboard password (min 10 chars): " PW; echo
  [ "${#PW}" -ge 10 ] || { echo "Password too short"; rm -f .env; exit 1; }
  PW="$PW" .venv/bin/python - <<'PY'
import os, re, secrets
s = open(".env").read()
s = re.sub(r"(?m)^DASHBOARD_PASSWORD=.*$", lambda m: "DASHBOARD_PASSWORD=" + os.environ["PW"], s)
s = re.sub(r"(?m)^FLASK_SECRET_KEY=.*$", lambda m: "FLASK_SECRET_KEY=" + secrets.token_hex(32), s)
open(".env", "w").write(s)
PY
fi
chmod 600 .env
mkdir -p data

echo ">> Creating systemd services"
sudo tee /etc/systemd/system/niftybot-web.service >/dev/null <<UNIT
[Unit]
Description=NiftyBot dashboard (gunicorn)
After=network-online.target
Wants=network-online.target
[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 2 --threads 4 --timeout 30 --bind 127.0.0.1:5000 app:app
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/niftybot-worker.service >/dev/null <<UNIT
[Unit]
Description=NiftyBot scanner worker
After=network-online.target
Wants=network-online.target
[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/worker.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT

echo ">> Configuring nginx (80 + self-signed 443)"
sudo mkdir -p /etc/nginx/ssl
[ -f /etc/nginx/ssl/niftybot.crt ] || sudo openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout /etc/nginx/ssl/niftybot.key -out /etc/nginx/ssl/niftybot.crt -subj "/CN=niftybot" 2>/dev/null
sudo tee /etc/nginx/sites-available/niftybot >/dev/null <<'NGINX'
limit_req_zone $binary_remote_addr zone=nb:10m rate=10r/s;
server {
    listen 80 default_server;
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate /etc/nginx/ssl/niftybot.crt;
    ssl_certificate_key /etc/nginx/ssl/niftybot.key;
    client_max_body_size 1m;
    location / {
        limit_req zone=nb burst=30 nodelay;
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
NGINX
sudo ln -sf /etc/nginx/sites-available/niftybot /etc/nginx/sites-enabled/niftybot
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now niftybot-web niftybot-worker
sudo systemctl restart nginx

IP="$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
echo
echo "Done. Dashboard:  http://${IP:-<EXTERNAL_IP>}/   (or https://${IP:-<EXTERNAL_IP>}/ - self-signed, browser will warn)"
echo "If it does not open, run deploy/gcp_static_ip_and_firewall.sh from Cloud Shell / your PC (opens ports 80/443 and reserves the IP)."
echo "Edit $APP_DIR/.env (Upstox token, Telegram...) then: sudo systemctl restart niftybot-web niftybot-worker"
