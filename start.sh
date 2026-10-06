#!/bin/sh
set -e

# Start the PO-token provider in the background on its default port.
# This gives yt-dlp what it needs to satisfy YouTube's "proof of origin"
# check without needing any logged-in cookies.
cd /opt/pot-provider/server
deno run --no-prompt --allow-env --allow-net --allow-ffi=. --allow-read=. --allow-sys ./src/main.ts --port 4416 &

# Keep yt-dlp current (sites break it often); best-effort, never blocks startup.
timeout 60 python -m pip install -U --no-cache-dir --break-system-packages yt-dlp >/dev/null 2>&1 || true

# Give it a moment to come up before the main app starts handling requests.
sleep 3

cd /app
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
