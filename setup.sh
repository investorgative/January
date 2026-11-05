#!/usr/bin/env bash
# JANUARY one-time setup on Raspberry Pi OS (Bookworm or newer, 64-bit recommended)
set -e
cd "$(dirname "$0")"
echo "== system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tmux curl
echo "== python venv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p data/logs static
echo "== chart library (local copy, CDN is the fallback)"
if [ ! -s static/lightweight-charts.js ]; then
  curl -fsSL -o static/lightweight-charts.js \
    https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js \
    || echo "WARN: chart library download failed - the dashboard will load it from the CDN"
fi
echo "== cloudflared (for the free quick tunnel)"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)
  case "$ARCH" in
    arm64|amd64|armhf) ;;
    *) ARCH="";;
  esac
  if [ -n "$ARCH" ]; then
    curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
      && sudo dpkg -i /tmp/cloudflared.deb || echo "WARN: cloudflared install failed - install it yourself"
  else
    echo "WARN: unknown architecture - install cloudflared yourself"
  fi
fi
chmod +x start.sh stop.sh
echo
echo "Setup complete."
echo "1) nano config.json   -> put your Discord webhooks into \"discord\" (BTCUSDT, SOLUSDT, errors)"
echo "2) ./start.sh         -> starts tmux sessions January, JanuaryWeb, JanuaryCloud"
