#!/usr/bin/env bash
# F&O Cockpit - one-shot installer for a fresh Ubuntu 22.04 / 24.04 VPS (Mumbai region).
# Usage (as root):   bash install_vps.sh fno.yourdomain.in
# Before running: point a DNS "A" record for that sub-domain to this VPS IP
# (Cloudflare: DNS only / grey cloud is simplest). Your SMN Worker routes are not touched.
set -euo pipefail
DOMAIN="${1:?Usage: bash install_vps.sh fno.yourdomain.in}"
APP_DIR=/opt/fno-cockpit
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl ufw tzdata
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
timedatectl set-timezone Asia/Kolkata || true

echo "==> App user + files"
id cockpit >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin cockpit
mkdir -p "$APP_DIR"
cp -r "$SRC_DIR"/app "$SRC_DIR"/static "$SRC_DIR"/requirements.txt "$SRC_DIR"/tests "$APP_DIR"/
[ -f "$APP_DIR/config.json" ] || cp "$SRC_DIR"/config.example.json "$APP_DIR"/config.json
mkdir -p "$APP_DIR/data"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
chown -R cockpit:cockpit "$APP_DIR"
chmod 600 "$APP_DIR/config.json"

echo "==> systemd service (single worker = single scanner) + daily 08:30 restart"
cp "$SRC_DIR"/deploy/fno-cockpit.service /etc/systemd/system/
cp "$SRC_DIR"/deploy/fno-cockpit-restart.service "$SRC_DIR"/deploy/fno-cockpit-restart.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fno-cockpit.service fno-cockpit-restart.timer

echo "==> HTTPS reverse proxy (Caddy, automatic Let's Encrypt certificate)"
sed "s/__DOMAIN__/$DOMAIN/g" "$SRC_DIR"/deploy/Caddyfile.example > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> Firewall"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

sleep 4
echo
echo "============================================================"
echo " Installed.  Server URL : https://$DOMAIN"
echo " App token  : $(cat $APP_DIR/data/app_token.txt 2>/dev/null || echo 'starts in a few seconds: sudo cat /opt/fno-cockpit/data/app_token.txt')"
echo " Next steps :"
echo "  1. sudo nano $APP_DIR/config.json   -> put your NEW Kite api_key / api_secret"
echo "  2. Kite developer console -> Redirect URL = https://$DOMAIN/callback"
echo "  3. sudo systemctl restart fno-cockpit"
echo "  4. Phone app -> Settings -> Server URL + App token -> Test connection"
echo "  Logs: journalctl -u fno-cockpit -f    or   $APP_DIR/data/logs/cockpit.log"
echo "============================================================"
