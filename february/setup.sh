#!/usr/bin/env bash
# FEBRUARY one-time setup (lives in ~/january/february, fully separate from January)
set -e
cd "$(dirname "$0")"
echo "== system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tmux curl sqlite3
echo "== python venv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
mkdir -p data/logs static
echo "== seed the candle cache from January (saves re-downloading years of Bybit data)"
if [ ! -s data/february.db ] && [ -s ../data/january.db ]; then
  sqlite3 ../data/january.db ".backup 'data/february.db'" 2>/dev/null || cp ../data/january.db data/february.db
  echo "copied January's candle database"
fi
echo "== chart library (local copy, CDN is the fallback)"
if [ ! -s static/lightweight-charts.js ] && [ -s ../static/lightweight-charts.js ]; then
  cp ../static/lightweight-charts.js static/
fi
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
[ -f config.json ] || ./venv/bin/python -c "import february_core; february_core.load_config()"
echo "1) nano config.json   -> put your Discord webhooks into \"discord\" (BTCUSDT, SOLUSDT, errors)"
echo "2) ./start.sh         -> starts tmux sessions February, FebruaryWeb, FebruaryCloud"
