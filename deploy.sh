#!/usr/bin/env bash
# Deploy scan_and_alert.py to the live VPS. Does not touch .env, venv/, cron,
# or logrotate config -- those are already set up on the server.
set -euo pipefail
cd "$(dirname "$0")"

VPS_HOST="root@72.60.192.209"
VPS_KEY="$HOME/.ssh/id_ed25519_hostinger"
REMOTE_PATH="/opt/gmail-discord-alert/scan_and_alert.py"

echo "→ Deploying scan_and_alert.py to $VPS_HOST:$REMOTE_PATH"
scp -i "$VPS_KEY" scan_and_alert.py "$VPS_HOST:$REMOTE_PATH"

echo "→ Running a one-off test to confirm it still works..."
ssh -i "$VPS_KEY" "$VPS_HOST" "/opt/gmail-discord-alert/run.sh"

echo "→ Done."
