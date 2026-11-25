#!/usr/bin/env bash
# Update code on the VPS from a newer copy of this folder (keeps config.json, data/, token).
set -euo pipefail
APP_DIR=/opt/fno-cockpit
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ts=$(date +%Y%m%d-%H%M%S)
tar czf /root/fno-cockpit-backup-$ts.tgz --exclude=.venv --exclude=data/cache -C /opt fno-cockpit
ls -1t /root/fno-cockpit-backup-*.tgz 2>/dev/null | tail -n +6 | xargs -r rm -f   # keep last 5 backups
# 1 GB swap as a safety net for the evening SMF jobs (pandas) on small VPS plans
if ! swapon --show | grep -q . && [ ! -f /swapfile ]; then
  if fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    rm -f /swapfile
  fi
fi
cp -r "$SRC_DIR"/app "$SRC_DIR"/static "$SRC_DIR"/requirements.txt "$SRC_DIR"/tests "$APP_DIR"/
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
chown -R cockpit:cockpit "$APP_DIR"
systemctl restart fno-cockpit
echo "Updated. Backup: /root/fno-cockpit-backup-$ts.tgz"
