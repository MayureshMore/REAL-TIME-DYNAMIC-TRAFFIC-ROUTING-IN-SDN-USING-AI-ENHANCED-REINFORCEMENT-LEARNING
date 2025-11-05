#!/usr/bin/env bash
# scripts/experiments/run_with_rl.sh
set -Eeuo pipefail

# ---- Tunables via env ----
OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
CTRL_IP="${CTRL_IP:-127.0.0.1}"
DURATION="${DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
PATH_WAIT_SECS="${PATH_WAIT_SECS:-120}"   # increased from 60
WARMUP_PINGS="${WARMUP_PINGS:-10}"        # new: quick warm-up

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENSURE_CTRL="${REPO}/scripts/ensure_controller.sh"
TOPO_PY="${REPO}/scripts/topos/two_path.py"
AGENT_PY="${REPO}/scripts/agents/bandit_agent.py"
LOGGER_PY="${REPO}/scripts/metrics/poll_ports.py"

BASE_API="http://127.0.0.1:${REST_PORT}/api/v1"

log() { echo -e "\033[1;36m$*\033[0m"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Ensure correct helper path (idempotent)
sed -i 's#scripts/_ensure_controller.sh#scripts/ensure_controller.sh#g' "$0" 2>/dev/null || true

# Clean any leftovers (prevents RTNETLINK "File exists")
log "[prep] sudo mn -c"
sudo mn -c >/dev/null 2>&1 || true

# Start controller
log "[ctrl] Starting controller OF:${OF_PORT} REST:${REST_PORT}"
"${ENSURE_CTRL}" "${OF_PORT}" "${REST_PORT}"

# Launch topology (no CLI) for full duration; we will also run an agent + a logger in parallel
log "[topo] Launching two-path demo for ${DURATION}s"
sudo python3 "${TOPO_PY}" \
  --controller_ip "${CTRL_IP}" \
  --rest_port "${REST_PORT}" \
  --demo --demo_time "$(( DURATION - 5 ))" \
  --no_cli >/tmp/topo.out 2>&1 &
TOPO_PID=$!

# ---- Wait for hosts & paths ----
log "[wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
t0=$(date +%s)
H1=""; H2=""
while :; do
  HS="$(curl -sf "${BASE_API}/hosts" || echo '[]')"
  CNT="$(printf '%s' "$HS" | jq 'length')"
  if (( CNT >= 2 )); then
    H1="$(printf '%s' "$HS" | jq -r '.[0].mac')"
    H2="$(printf '%s' "$HS" | jq -r '.[1].mac')"
    PATHS="$(curl -sf "${BASE_API}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo '[]')"
    if [[ "$(printf '%s' "$PATHS" | jq 'length')" -ge 1 ]]; then
      log "[wait] Paths available for ${H1} -> ${H2}"
      break
    fi
  fi
  (( $(date +%s) - t0 > PATH_WAIT_SECS )) && {
    kill "$TOPO_PID" 2>/dev/null || true
    die "Timed out waiting for paths"
  }
  sleep 2
done

# ---- Warm-up traffic to speed discovery & counters ----
if (( WARMUP_PINGS > 0 )); then
  log "[warmup] Sending ${WARMUP_PINGS} ICMP echos via Mininet demo (already running)"
  # The demo process is already ping-flooding; just give a brief pause
  sleep 3
fi

# ---- Start logger ----
TS="$(date +%Y%m%d_%H%M%S)"
CSV="docs/baseline/ports_rl_${TS}.csv"
log "[logger] Logging to ${CSV}; polling ${BASE_API} every 1s; duration=${DURATION}s"
python3 "${LOGGER_PY}" --api "${BASE_API}" --out "${CSV}" --duration "${DURATION}" --interval 1 >/tmp/logger.out 2>&1 &
LOGGER_PID=$!

# ---- Start agent ----
log "[agent] epsilon=${EPSILON} k=${K} src=${H1} dst=${H2}"
python3 "${AGENT_PY}" \
  --api "${BASE_API}" \
  --src_mac "${H1}" \
  --dst_mac "${H2}" \
  --epsilon "${EPSILON}" \
  --k "${K}" \
  --duration "${DURATION}" \
  --cooldown 1 \
  >/tmp/agent.out 2>&1 &
AGENT_PID=$!

# ---- Wait for demo to finish ----
wait "${TOPO_PID}" 2>/dev/null || true

# ---- Stop agent/logger if still alive ----
kill "${AGENT_PID}" 2>/dev/null || true
kill "${LOGGER_PID}" 2>/dev/null || true

log "RL run complete. CSV: ${CSV}"
echo "${CSV}"
