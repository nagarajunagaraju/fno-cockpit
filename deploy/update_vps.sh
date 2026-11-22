#!/usr/bin/env bash
# Update code on the VPS from a newer copy of this folder (keeps config.json, data/, token).
set -euo pipefail
APP_DIR=/opt/fno-cockpit
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ts=$(date +%Y%m%d-%H%M%S)
tar czf /root/fno-cockpit-backup-$ts.tgz -C /opt fno-cockpit --exclude=.venv
cp -r "$SRC_DIR"/app "$SRC_DIR"/static "$SRC_DIR"/requirements.txt "$SRC_DIR"/tests "$APP_DIR"/
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
chown -R cockpit:cockpit "$APP_DIR"
systemctl restart fno-cockpit
echo "Updated. Backup: /root/fno-cockpit-backup-$ts.tgz"
