#!/bin/sh
set -e

# Start the PO-token provider in the background on its default port.
cd /opt/pot-provider/server
deno run --no-prompt --allow-env --allow-net --allow-ffi=. --allow-read=. --allow-sys ./src/main.ts --port 4416 &

# Give it a moment to come up before the main app starts handling requests.
sleep 3

cd /app
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
