#!/usr/bin/env bash
# run-controller.sh — start Ryu controller in tmux and verify /health
# Usage examples:
#   ./run-controller.sh                      # default: app=monitor, OFP=6633, REST=8080
#   ./run-controller.sh --app router         # start sdn_router_rest.py (for RL)
#   ./run-controller.sh --ofp-port 6653 --wsapi-port 9090
#   ./run-controller.sh --smoke              # quick Mininet ping test after healthy
#
# Notes:
# - Requires setup-vm.sh to have been run (creates pyenv venv "ryu39").
# - monitor app = monitor_rest.py + simple_switch_13 + topology discovery
# - router  app = sdn_router_rest.py + topology discovery (supports /paths, /actions/*)

set -euo pipefail

# -------- Defaults (override with flags or env) --------
APP="${APP:-monitor}"             # monitor | router
OF_PORT="${OF_PORT:-6633}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
LOG_FILE="${LOG_FILE:-$HOME/ryu-controller.log}"
SESSION="${SESSION:-ryu}"
TMUX_SOCKET="-L ryu"

# pyenv/venv
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
VENV_NAME="${VENV_NAME:-ryu39}"
RYU_BIN="${PYENV_ROOT}/versions/${VENV_NAME}/bin/ryu-manager"

# -------- Args --------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2;;
    --ofp-port) OF_PORT="$2"; shift 2;;
    --wsapi-port) WSAPI_PORT="$2"; shift 2;;
    --log) LOG_FILE="$2"; shift 2;;
    --session) SESSION="$2"; shift 2;;
    --smoke) SMOKE="1"; shift;;
    -h|--help)
      echo "Usage: $0 [--app monitor|router] [--ofp-port N] [--wsapi-port N] [--log PATH] [--session NAME] [--smoke]"
      exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

# -------- Checks --------
if [[ ! -x "$RYU_BIN" ]]; then
  echo "ERROR: $RYU_BIN not found. Run ./setup-vm.sh first." >&2
  exit 1
fi

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick app command
if [[ "$APP" == "router" ]]; then
  APP_CMD="'$PROJ_DIR/controller-apps/sdn_router_rest.py' ryu.topology.switches"
elif [[ "$APP" == "monitor" ]]; then
  APP_CMD="'$PROJ_DIR/controller-apps/monitor_rest.py' ryu.app.simple_switch_13 ryu.topology.switches"
else
  echo "ERROR: --app must be 'monitor' or 'router' (got: $APP)" >&2
  exit 2
fi

# -------- Start in tmux --------
tmux $TMUX_SOCKET kill-session -t "$SESSION" 2>/dev/null || true

CMD="cd '$PROJ_DIR' && exec '$RYU_BIN' \
  $APP_CMD \
  --ofp-tcp-listen-port $OF_PORT --wsapi-port $WSAPI_PORT \
  >>'$LOG_FILE' 2>&1"

echo "==> Starting Ryu ($APP) on OF:$OF_PORT REST:$WSAPI_PORT (session: $SESSION)"
tmux $TMUX_SOCKET new -d -s "$SESSION" "$CMD"

# -------- Health check --------
HEALTH_URL="http://127.0.0.1:${WSAPI_PORT}/api/v1/health"
echo "==> Waiting for health: $HEALTH_URL"
ok=0
for i in {1..60}; do
  if curl -sf "$HEALTH_URL" | grep -q '"status": "ok"'; then
    ok=1; break
  fi
  sleep 1
done

if [[ "$ok" -ne 1 ]]; then
  echo "ERROR: controller did not become healthy in time." >&2
  echo "---- Last 200 log lines ($LOG_FILE) ----"
  tail -n 200 "$LOG_FILE" || true
  exit 1
fi

echo "OK: controller healthy"
curl -s "$HEALTH_URL" ; echo
echo
echo "Log: $LOG_FILE"
echo "Attach: tmux $TMUX_SOCKET attach -t $SESSION"
echo

# -------- Optional smoke test --------
if [[ "${SMOKE:-0}" == "1" ]]; then
  echo "==> Running Mininet smoke test (single,2 pingall)"
  sudo mn -c >/dev/null 2>&1 || true
  sudo mn --topo single,2 \
    --controller remote,ip=127.0.0.1,port="${OF_PORT}" \
    --switch ovsk,protocols=OpenFlow13 \
    --test pingall || true
  echo "==> Sample /stats/ports"
  curl -s "http://127.0.0.1:${WSAPI_PORT}/api/v1/stats/ports" | head -n 50
  echo
fi
