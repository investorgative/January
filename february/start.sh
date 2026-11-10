#!/usr/bin/env bash
# Starts FEBRUARY: paper trader (tmux "February"), dashboard on :8753 ("FebruaryWeb"),
# Cloudflare quick tunnel ("FebruaryCloud"). Every process restarts itself if it crashes.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
mkdir -p "$DIR/data/logs"
if ! tmux has-session -t February 2>/dev/null; then
  tmux new-session -d -s February "cd '$DIR' && while true; do '$PY' february_trader.py; echo 'trader exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session February      (paper trader)"
else echo "tmux session February already running"; fi
if ! tmux has-session -t FebruaryWeb 2>/dev/null; then
  tmux new-session -d -s FebruaryWeb "cd '$DIR' && while true; do '$PY' february_web.py; echo 'web exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session FebruaryWeb   (dashboard http://localhost:8753)"
else echo "tmux session FebruaryWeb already running"; fi
if command -v cloudflared >/dev/null 2>&1; then
  if ! tmux has-session -t FebruaryCloud 2>/dev/null; then
    : > "$DIR/data/logs/cloudflared.log"
    tmux new-session -d -s FebruaryCloud "cd '$DIR' && while true; do cloudflared tunnel --no-autoupdate --url http://localhost:8753 2>&1 | tee -a data/logs/cloudflared.log; echo 'tunnel exited - restarting in 10s'; sleep 10; done"
    echo "started tmux session FebruaryCloud (quick tunnel - URL appears in a few seconds)"
    sleep 8
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$DIR/data/logs/cloudflared.log" | tail -1)
    [ -n "$URL" ] && echo "public URL: $URL" || echo "public URL: run 'tmux attach -t FebruaryCloud' or look at the header of the dashboard"
  else echo "tmux session FebruaryCloud already running"; fi
else
  echo "cloudflared not installed - tunnel not started (dashboard still works on the LAN)"
fi
