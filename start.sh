#!/usr/bin/env bash
# Starts JANUARY: paper trader (tmux "January"), dashboard on :8756 ("JanuaryWeb"),
# Cloudflare quick tunnel ("JanuaryCloud"). Every process restarts itself if it crashes.
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || { echo "venv missing - run ./setup.sh first"; exit 1; }
mkdir -p "$DIR/data/logs"
if ! tmux has-session -t January 2>/dev/null; then
  tmux new-session -d -s January "cd '$DIR' && while true; do '$PY' january_trader.py; echo 'trader exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session January      (paper trader)"
else echo "tmux session January already running"; fi
if ! tmux has-session -t JanuaryWeb 2>/dev/null; then
  tmux new-session -d -s JanuaryWeb "cd '$DIR' && while true; do '$PY' january_web.py; echo 'web exited - restarting in 10s'; sleep 10; done"
  echo "started tmux session JanuaryWeb   (dashboard http://localhost:8756)"
else echo "tmux session JanuaryWeb already running"; fi
if command -v cloudflared >/dev/null 2>&1; then
  if ! tmux has-session -t JanuaryCloud 2>/dev/null; then
    : > "$DIR/data/logs/cloudflared.log"
    tmux new-session -d -s JanuaryCloud "cd '$DIR' && while true; do cloudflared tunnel --no-autoupdate --url http://localhost:8756 2>&1 | tee -a data/logs/cloudflared.log; echo 'tunnel exited - restarting in 10s'; sleep 10; done"
    echo "started tmux session JanuaryCloud (quick tunnel - URL appears in a few seconds)"
    sleep 8
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$DIR/data/logs/cloudflared.log" | tail -1)
    [ -n "$URL" ] && echo "public URL: $URL" || echo "public URL: run 'tmux attach -t JanuaryCloud' or look at the header of the dashboard"
  else echo "tmux session JanuaryCloud already running"; fi
else
  echo "cloudflared not installed - tunnel not started (dashboard still works on the LAN)"
fi
