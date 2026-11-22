#!/usr/bin/env bash
# F&O Cockpit - ONE-COMMAND setup on a fresh Ubuntu VPS. Run as:
#   curl -fsSL https://raw.githubusercontent.com/nagarajunagaraju/fno-cockpit/main/deploy/bootstrap.sh | sudo bash
# Optional: give your own domain:  ... | sudo bash -s -- fno.yourdomain.in
# Without a domain it uses <your-ip-with-dashes>.sslip.io (free, HTTPS works, no DNS setup).
# Running it again later = update to the latest code (keeps your keys, token and data).
set -euo pipefail
REPO=https://github.com/nagarajunagaraju/fno-cockpit.git
SRC=/root/fno-cockpit-src
APP=/opt/fno-cockpit

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
apt-get update -y -qq && apt-get install -y -qq git curl >/dev/null

if [ -d "$SRC/.git" ]; then git -C "$SRC" pull -q; else git clone -q "$REPO" "$SRC"; fi

if [ -f "$APP/config.json" ] && [ -f /etc/caddy/Caddyfile ] && grep -q reverse_proxy /etc/caddy/Caddyfile; then
  echo "==> Existing install found: updating code only"
  bash "$SRC/deploy/update_vps.sh"
  HOST=$(head -1 /etc/caddy/Caddyfile | awk '{print $1}')
else
  IP=$(curl -fsS https://checkip.amazonaws.com || curl -fsS https://api.ipify.org)
  IP=$(echo "$IP" | tr -d '[:space:]')
  HOST="${1:-${IP//./-}.sslip.io}"
  echo "==> Installing for https://$HOST"
  bash "$SRC/deploy/install_vps.sh" "$HOST"

  echo
  echo "Paste your NEW Kite Connect details (input is not shown on screen for the secret)."
  read -r -p "Kite API key    : " KEY < /dev/tty
  read -r -s -p "Kite API secret : " SECRET < /dev/tty; echo
  python3 - "$KEY" "$SECRET" <<'PY'
import json, sys
json.dump({"api_key": sys.argv[1].strip(), "api_secret": sys.argv[2].strip()},
          open("/opt/fno-cockpit/config.json", "w"), indent=2)
PY
  chown cockpit:cockpit "$APP/config.json"; chmod 600 "$APP/config.json"
  systemctl restart fno-cockpit
fi

for i in $(seq 1 30); do [ -s "$APP/data/app_token.txt" ] && break; sleep 1; done
sleep 3
CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$HOST/static/login_ok.html" || true)
echo
echo "=================================================================="
echo "  DONE."
echo "  1) Kite developer console -> Redirect URL :  https://$HOST/callback"
echo "  2) Phone app -> Server URL               :  https://$HOST"
echo "     Phone app -> App token                :  $(cat $APP/data/app_token.txt)"
echo "  3) Every trading day ~9:00 -> app -> Login with Kite"
echo "  HTTPS check: $CODE (200 = OK; if not, open ports 80 and 443 in your VPS firewall and re-run)"
echo "=================================================================="
