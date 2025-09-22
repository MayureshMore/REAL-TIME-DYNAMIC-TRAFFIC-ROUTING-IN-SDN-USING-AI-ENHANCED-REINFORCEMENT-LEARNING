cat > scripts/_ensure_controller.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

OF_PORT="${1:-6633}"
REST_PORT="${2:-8080}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYENV_ROOT="${HOME}/.pyenv"
RYU_BIN="${PYENV_ROOT}/versions/ryu39/bin/ryu-manager"
LOG="${HOME}/ryu-controller.log"
SESSION="ryu-app"
TMUX_SOCKET="-L ryu"

if [[ ! -x "${RYU_BIN}" ]]; then
  echo "Ryu venv missing at ${RYU_BIN}. Run ./setup-vm.sh first." >&2
  exit 1
fi

# Kill any previous controller session
tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true

echo "Starting controller on OF:${OF_PORT} REST:${REST_PORT}"
tmux ${TMUX_SOCKET} new -d -s "${SESSION}" \
  "cd '${REPO}' && \
   export PYTHONUNBUFFERED=1 && \
   exec '${RYU_BIN}' \
     controller-apps/monitor_rest.py \
     controller-apps/sdn_router_rest.py \
     ryu.topology.switches \
     --ofp-tcp-listen-port ${OF_PORT} \
     --wsapi-port ${REST_PORT} >>'${LOG}' 2>&1"

# Health check
echo "Waiting for controller health on :${REST_PORT} ..."
for i in {1..40}; do
  if curl -sf "http://127.0.0.1:${REST_PORT}/api/v1/health" >/dev/null; then
    echo "Health:"; curl -s "http://127.0.0.1:${REST_PORT}/api/v1/health" | jq .
    break
  fi
  sleep 1
  [[ $i -eq 40 ]] && { echo "Controller failed health check"; tail -n 200 "${LOG}" || true; exit 1; }
done

# OF port listen check (best effort)
echo "Waiting for OFP port :${OF_PORT} to listen ..."
for i in {1..30}; do
  ss -ltn | grep -q ":${OF_PORT} " && break || true
  sleep 1
done

exit 0
SH
chmod +x scripts/_ensure_controller.sh
