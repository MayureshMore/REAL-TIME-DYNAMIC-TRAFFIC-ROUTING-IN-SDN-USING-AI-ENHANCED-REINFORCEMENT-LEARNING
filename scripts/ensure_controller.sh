#!/usr/bin/env bash
# Start Ryu with our controller app if not already healthy.

set -euo pipefail

OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
APP_PATH="${APP_PATH:-controller-apps/sdn_router_rest.py}"
RYU_BIN="${RYU_BIN:-ryu-manager}"

health() {
  curl -sf "http://127.0.0.1:${REST_PORT}/api/v1/health" >/dev/null
}

if health; then
  echo "Controller already healthy on :${REST_PORT}"
  exit 0
fi

echo "Starting controller on OF:${OF_PORT} REST:${REST_PORT}"
PYTHONUNBUFFERED=1 \
${RYU_BIN} \
  --ofp-tcp-listen-port "${OF_PORT}" \
  --wsapi-port "${REST_PORT}" \
  "${APP_PATH}" \
  > /tmp/ryu.out 2>&1 &

# Wait for REST to come up
for i in {1..60}; do
  if health; then
    echo "Controller healthy and listening."
    exit 0
  fi
  sleep 1
done

echo "ERROR: controller failed to become healthy" >&2
exit 1
