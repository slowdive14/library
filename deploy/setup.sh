#!/usr/bin/env bash
# Installs (or updates) the bot as a systemd service on an Ubuntu VM in Korea,
# e.g. Azure Korea Central or Oracle Cloud Chuncheon/Seoul.
# The library catalog blocks non-Korean IPs, so the VM must be in a Korean region.
#
# Usage (as the login user, e.g. azureuser):
#   bash ~/library/deploy/setup.sh
# Run it again after `git pull` to reinstall dependencies and restart the bot.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=library-bot

sudo apt-get update -q
sudo apt-get install -y -q python3-venv

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
    echo "Missing $APP_DIR/.env"
    echo "Copy your local .env to the VM (see README), then run this script again."
    exit 1
fi
chmod 600 "$APP_DIR/.env"

sed -e "s|__USER__|$USER|g" -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/$SERVICE.service" | sudo tee "/etc/systemd/system/$SERVICE.service" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

sleep 3
systemctl --no-pager status "$SERVICE" | head -n 15
echo
echo "Follow the logs with: journalctl -u $SERVICE -f"
