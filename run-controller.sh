#!/usr/bin/env bash
# run-controller.sh — starts your Ryu controller + a learning switch so flows appear
# Usage:
#   ./run-controller.sh          # start controller only
#   ./run-controller.sh smoke    # also run a Mininet ping-all smoke test

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYENV_ROOT="${HOME}/.pyenv"
VENV_NAME="ryu39"
RYU_BIN="${PYENV_ROOT}/versions/${VENV_NAME}/bin/ryu-manager"
LOG="${HOME}/ryu-controller.log"
WSAPI_PORT="${WSAPI_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"
TMUX_SOCKET="-L ryu"
SESSION="ryu-app"

if [[ ! -x "${RYU_BIN}" ]]; then
  echo "Ryu venv not found at ${RYU_BIN}. Run ./setup-vm.sh first." >&2
  exit 1
fi

# Kill old session if present
tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true

echo "==> Starting controller in tmux (${SESSION}) on :${WSAPI_PORT} (OF ${OF_PORT})"
tmux ${TMUX_SOCKET} new -d -s "${SESSION}" \
  "cd '${PROJ_DIR}' && exec '${RYU_BIN}' \
     controller-apps/monitor_rest.py ryu.app.simple_switch_13 ryu.topology.switches \
     --ofp-tcp-listen-port ${OF_PORT} --wsapi-port ${WSAPI_PORT} >>'${LOG}' 2>&1"

# Wait for /health to be OK
echo "==> Health check (http://127.0.0.1:${WSAPI_PORT}/api/v1/health)"
for i in {1..30}; do
  if curl -sf "http://127.0.0.1:${WSAPI_PORT}/api/v1/health" | grep -q '"status": "ok"'; then
    echo "OK: controller healthy"
    curl -s "http://127.0.0.1:${WSAPI_PORT}/api/v1/health"
    break
  fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "ERROR: controller did not become healthy. Logs:"; tail -n 200 "${LOG}"; exit 1; }
done
echo

if [[ "${1:-}" == "smoke" ]]; then
  echo "==> Running Mininet smoke test (single,2 -> pingall)"
  sudo mn -c >/dev/null 2>&1 || true
  sudo mn --topo single,2 \
          --controller remote,ip=127.0.0.1,port="${OF_PORT}" \
          --switch ovs,protocols=OpenFlow13 \
          --test pingall || true

  echo "==> Sample stats"
  curl -s "http://127.0.0.1:${WSAPI_PORT}/api/v1/stats/ports" | head -n 100
  echo
  echo "==> Installed flows (OVS view)"
  sudo ovs-ofctl -O OpenFlow13 dump-flows s1 || true
fi

echo "==> To view live logs:  tail -f ${LOG}"
echo "==> To attach tmux:     tmux ${TMUX_SOCKET} attach -t ${SESSION}"
