#!/usr/bin/env bash
# Start Ryu with the SDN router REST app + topology discovery in a tmux session.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYENV_ROOT="${HOME}/.pyenv"
LOG="${HOME}/ryu-controller.log"
OF_PORT="${1:-6633}"
WSAPI_PORT="${2:-8080}"
TMUX_SOCKET="-L ryu"
SESSION="ryu-app"

# Resolve ryu-manager (prefer pyenv, fall back to PATH)
RYU_BIN="${PYENV_ROOT}/versions/ryu39/bin/ryu-manager"
if [ ! -x "${RYU_BIN}" ]; then
  if command -v ryu-manager >/dev/null 2>&1; then
    RYU_BIN="$(command -v ryu-manager)"
  else
    echo "[x] ryu-manager not found. Install Ryu or ensure pyenv ryu39 exists." >&2
    exit 1
  fi
fi

tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true

echo "Starting controller on OF:${OF_PORT} REST:${WSAPI_PORT}"
tmux ${TMUX_SOCKET} new -d -s "${SESSION}" \
  "cd '${REPO}' && exec '${RYU_BIN}' \
     '${REPO}/controller-apps/sdn_router_rest.py' ryu.topology.switches \
     --observe-links \
     --ofp-tcp-listen-port ${OF_PORT} --wsapi-port ${WSAPI_PORT} >>'${LOG}' 2>&1"

# Wait for REST health
echo -n "Waiting for controller health on :${WSAPI_PORT} ... "
for i in {1..30}; do
  if curl -sf "http://127.0.0.1:${WSAPI_PORT}/api/v1/health" >/dev/null; then echo "OK"; break; fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "FAIL"; tail -n 200 "${LOG}"; exit 1; }
done

# Wait for OF port to be listening
echo -n "Waiting for OFP port :${OF_PORT} to listen ... "
for i in {1..30}; do
  ss -ltn sport = :${OF_PORT} | grep -q LISTEN && { echo "OK"; break; }
  sleep 1
  [[ $i -eq 30 ]] && { echo "FAIL"; tail -n 200 "${LOG}"; exit 1; }
done

echo "Health:"
curl -s "http://127.0.0.1:${WSAPI_PORT}/api/v1/health" | jq .
