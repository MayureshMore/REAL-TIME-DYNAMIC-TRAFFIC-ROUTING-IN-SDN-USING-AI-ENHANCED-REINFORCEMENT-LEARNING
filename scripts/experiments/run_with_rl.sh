#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# --- configurable knobs (env overrides allowed) ---
DURATION="${DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"
PATH_WAIT_SECS="${PATH_WAIT_SECS:-120}"
WARMUP_PINGS="${WARMUP_PINGS:-10}"
LOG_INTERVAL="${LOG_INTERVAL:-1}"

# Correct locations:
LOGGER="${LOGGER:-${REPO}/scripts/metrics/poll_ports.py}"
AGENT="${AGENT:-${REPO}/rl-agent/bandit_agent.py}"   # <-- fixed path here

ENSURE="${REPO}/scripts/ensure_controller.sh"
TOPO="${REPO}/scripts/topos/two_path.py"

CSV_DIR="${REPO}/docs/baseline"
mkdir -p "${CSV_DIR}"
CSV_OUT="${CSV_DIR}/ports_rl_$(date +%Y%m%d_%H%M%S).csv"

API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

# --- helpers ---
die() { echo "[x] $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

need curl
need jq
need python3

# make sure ensure_controller.sh is referenced correctly (your auto_demo may also patch this)
if ! grep -q "ensure_controller.sh" <<<"${ENSURE}"; then
  die "ENSURE path looks wrong: ${ENSURE}"
fi

echo "[prep] sudo mn -c"
sudo mn -c >/dev/null 2>&1 || true

echo "[ctrl] Starting controller OF:${OF_PORT} REST:${WSAPI_PORT}"
WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" >/dev/null

echo "[topo] Launching two-path demo for ${DURATION}s"
# kick off the topology run in the background, so we can wait for graph + run agent/logger
python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${DURATION}" > /tmp/topo.out 2>&1 &

# wait for hosts + paths to be ready
echo "[wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
deadline=$(( $(date +%s) + PATH_WAIT_SECS ))
H1=""
H2=""

while [ "$(date +%s)" -lt "${deadline}" ]; do
  hosts_json="$(curl -sf "${API_BASE}/hosts" || echo "[]")"
  host_count="$(jq 'length' <<<"${hosts_json}")"

  if [ "${host_count}" -ge 2 ]; then
    H1="$(jq -r '.[0].mac' <<<"${hosts_json}")"
    H2="$(jq -r '.[1].mac' <<<"${hosts_json}")"
    paths_json="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo "[]")"
    path_count="$(jq 'length' <<<"${paths_json}")"
    if [ "${path_count}" -ge 1 ]; then
      echo "[wait] Paths available for ${H1} -> ${H2}"
      break
    fi
  fi
  sleep 1
done

if [ -z "${H1}" ] || [ -z "${H2}" ]; then
  die "timed out waiting for hosts/paths; check controller logs"
fi

echo "[warmup] Sending ${WARMUP_PINGS} ICMP echos via Mininet demo (already running)"
# the topo script already does a ping flood; the warmup is just to tick MAC learning

echo "[logger] Logging to ${CSV_OUT}; polling ${API_BASE} every ${LOG_INTERVAL}s; duration=${DURATION}s"
python3 "${LOGGER}" \
  --controller "${API_BASE}" \
  --outfile "${CSV_OUT}" \
  --interval "${LOG_INTERVAL}" \
  --duration "${DURATION}" > /tmp/logger.out 2>&1 &

echo "[agent] epsilon=${EPSILON} k=${K} src=${H1} dst=${H2}"
python3 "${AGENT}" \
  --controller "${API_BASE}" \
  --epsilon "${EPSILON}" \
  --k "${K}" \
  --src "${H1}" \
  --dst "${H2}" \
  --duration "${DURATION}" \
  --interval "${LOG_INTERVAL}" > /tmp/agent.out 2>&1 &

# tail progress (best-effort)
( sleep 2; tail -n +1 -f /tmp/topo.out /tmp/logger.out /tmp/agent.out 2>/dev/null ) &
TAIL_PID=$!

# wait for background jobs (topo+logger+agent)
wait
kill "${TAIL_PID}" >/dev/null 2>&1 || true

echo "RL run complete. CSV: ${CSV_OUT}"
echo "${CSV_OUT}"
