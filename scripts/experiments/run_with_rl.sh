#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Inputs (exported by auto_demo.sh)
DURATION="${DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
SRC_MAC="${SRC_MAC:-}"
DST_MAC="${DST_MAC:-}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"
REUSE_TOPOLOGY="${REUSE_TOPOLOGY:-0}"

API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

# Paths
BANDIT="${REPO}/rl-agent/bandit_agent.py"
LOGGER="${REPO}/scripts/metrics/port_logger.py"   # your existing logger
CSV_DIR="${REPO}/docs/baseline"
CSV_OUT="${CSV_DIR}/ports_rl_$(date +%Y%m%d_%H%M%S).csv"

indent(){ sed 's/^/  /'; }
say(){ printf "%s\n" "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "[x] missing $1" >&2; exit 1; }; }
need python3
need curl
need jq

# Mininet helpers for traffic generation on reused topology
mn_pid_of() {
  # Return the PID of a mininet host by name (h1, h2)
  local name="$1"
  # Modern mininet keeps pids under /var/run/mn
  if [ -r "/var/run/mn/${name}/pid" ]; then
    cat "/var/run/mn/${name}/pid"
    return 0
  fi
  # Fallback: pgrep (best-effort)
  pgrep -f "mininet:${name}$" | head -n1 || true
}

mnexec_safe() {
  local host="$1"; shift
  local pid
  pid="$(mn_pid_of "${host}")"
  [ -n "${pid}" ] || { echo "[x] could not find PID for ${host}"; return 1; }
  sudo mnexec -a "${pid}" "$@"
}

# Resolve IPs from controller (consistent with your two_path.py)
resolve_ip() {
  local mac="$1"
  curl -sf "${API_BASE}/hosts" \
    | jq -r --arg mac "${mac}" '.[] | select(.mac==$mac) | .ipv4 // empty' \
    | head -n1
}

# If MACs/IPs not provided, bail out explicitly
[ -n "${SRC_MAC}" ] && [ -n "${DST_MAC}" ] || { echo "[x] SRC_MAC/DST_MAC not set" >&2; exit 1; }
SRC_IP="$(resolve_ip "${SRC_MAC}")"
DST_IP="$(resolve_ip "${DST_MAC}")"
[ -n "${SRC_IP}" ] && [ -n "${DST_IP}" ] || { echo "[x] could not resolve SRC/DST IPs"; exit 1; }

# Choose traffic tool
TRAFFIC_TOOL=""
if command -v iperf3 >/dev/null 2>&1; then
  TRAFFIC_TOOL="iperf3"
elif command -v iperf >/dev/null 2>&1; then
  TRAFFIC_TOOL="iperf"
fi
if [ -z "${TRAFFIC_TOOL}" ]; then
  echo "[x] neither iperf3 nor iperf found; install one or add a project-local traffic generator" >&2
  exit 1
fi

say "[prep] Reusing existing controller/topology (skipping mn -c + ensure_controller)"
say "[topo] Reusing externally managed two-path demo"

# Warm K-path cache both ways
curl -sf "${API_BASE}/paths?src_mac=${SRC_MAC}&dst_mac=${DST_MAC}&k=${K}" >/dev/null || true
curl -sf "${API_BASE}/paths?src_mac=${DST_MAC}&dst_mac=${SRC_MAC}&k=${K}" >/dev/null || true

say "[wait] Using provided MACs ${SRC_MAC} -> ${DST_MAC}"
say "[wait] Confirmed paths for ${SRC_MAC} -> ${DST_MAC} on reused topology"

# Start traffic
say "[traffic] ${TRAFFIC_TOOL} h1 -> h2 for ${DURATION}s (UDP ~50M)"
if [ "${TRAFFIC_TOOL}" = "iperf3" ]; then
  # server on h2
  mnexec_safe h2 iperf3 -s -1 >/tmp/iperf3_server.log 2>&1 &
  srv_pid=$!
  # slight delay for server bind
  sleep 0.5
  # client on h1
  mnexec_safe h1 iperf3 -u -l 1470 -b 50M -c "${DST_IP}" -t "${DURATION}" >/tmp/iperf3_client.log 2>&1 &
  cli_pid=$!
else
  # legacy iperf
  mnexec_safe h2 iperf -s -u >/tmp/iperf_server.log 2>&1 &
  srv_pid=$!
  sleep 0.5
  mnexec_safe h1 iperf -u -b 50M -l 1470 -c "${DST_IP}" -t "${DURATION}" >/tmp/iperf_client.log 2>&1 &
  cli_pid=$!
fi

# Start logger and agent (both bounded by timeout = DURATION + small pad)
say "[logger] Logging to ${CSV_OUT}; polling ${API_BASE} every 1s; duration=${DURATION}s"
timeout "$(( DURATION + 5 ))s" python3 "${LOGGER}" \
  --api "${API_BASE}" --interval 1.0 --duration "${DURATION}" --out "${CSV_OUT}" >/tmp/logger.out 2>&1 &
logger_pid=$!

say "[agent] epsilon=${EPSILON} k=${K} src=${SRC_MAC} dst=${DST_MAC}"
timeout "$(( DURATION + 5 ))s" python3 "${BANDIT}" \
  --api "${API_BASE}" --epsilon "${EPSILON}" --k "${K}" \
  --src "${SRC_MAC}" --dst "${DST_MAC}" >/tmp/agent.out 2>&1 &
agent_pid=$!

# Wait for all three
wait ${agent_pid} || true
wait ${logger_pid} || true
wait ${cli_pid} || true
wait ${srv_pid} || true

say "RL run complete. CSV: ${CSV_OUT}"
echo "${CSV_OUT}"

# Optional: emit last lines of logs for quick debugging
echo "==> /tmp/logger.out <=="; tail -n +1 /tmp/logger.out | indent || true
echo
echo "==> /tmp/agent.out <=="; tail -n +1 /tmp/agent.out | indent || true
