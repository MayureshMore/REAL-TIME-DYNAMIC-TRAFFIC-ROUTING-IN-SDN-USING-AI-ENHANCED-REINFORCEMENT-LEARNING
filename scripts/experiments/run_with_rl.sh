#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

OF_PORT="${OF_PORT:-6633}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"

DURATION="${DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
PATH_WAIT_SECS="${PATH_WAIT_SECS:-30}"
REUSE_TOPOLOGY="${REUSE_TOPOLOGY:-0}"

API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

TOPO="${REPO}/scripts/topos/two_path.py"
ENSURE="${REPO}/scripts/ensure_controller.sh"
LOGGER="${REPO}/scripts/metrics/log_stats.py"
AGENT="${REPO}/rl-agent/bandit_agent.py"
CSV_DIR="${REPO}/docs/baseline"

indent(){ sed 's/^/  /'; }
say(){ printf "%s\n" "$*"; }
need(){ command -v "$1" >/dev/null 2>&1 || { echo "[x] missing $1"; exit 1; }; }

need curl; need jq; need python3; need sudo

# Ensure controller
if ! curl -sf "${API_BASE}/health" >/dev/null 2>&1; then
  say "[ctrl] controller not healthy; starting via ensure_controller.sh"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" | indent
fi

# Topology
topo_pid=""
if [ "${REUSE_TOPOLOGY}" != "1" ]; then
  say "[topo] launching two-path topology"
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration $(( DURATION + PATH_WAIT_SECS + 30 )) > /tmp/topo_rl.out 2>&1 &
  topo_pid="$!"
fi

# Wait for two hosts (robust: ignore transient REST hiccups)
deadline=$(( $(date +%s) + PATH_WAIT_SECS ))
H1=""; H2=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
  HLINE="$(curl -sf "${API_BASE}/hosts" | jq -r '[.[0].mac, .[1].mac] | @tsv' 2>/dev/null || true)"
  if [ -n "${HLINE}" ] && [[ "${HLINE}" != $'\t' ]]; then
    read -r H1 H2 <<< "${HLINE}"
    break
  fi
  sleep 1
done
if [ -z "${H1}" ] || [ -z "${H2}" ]; then
  echo "[x] could not learn hosts in ${PATH_WAIT_SECS}s" >&2
  exit 1
fi
say "[wait] hosts learned H1=${H1} H2=${H2}"

# Warm k paths both ways
curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" >/dev/null || true
curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" >/dev/null || true

# Logger
CSV="${CSV_DIR}/ports_rl_$(date +%Y%m%d_%H%M%S).csv"
say "[log] ${CSV}"
python3 "${LOGGER}" --controller "${API_BASE}" --interval 1 --duration "${DURATION}" --out "${CSV}" > /tmp/logger.out 2>&1 &
LOGGER_PID=$!

# Agent
say "[agent] epsilon=${EPSILON} k=${K}"
python3 "${AGENT}" --controller "${API_BASE}" --epsilon "${EPSILON}" --k "${K}" --src "${H1}" --dst "${H2}" > /tmp/agent.out 2>&1 || true

# Stop logger
kill -TERM "${LOGGER_PID}" 2>/dev/null || true
wait "${LOGGER_PID}" 2>/dev/null || true

# Summary
if [ -s "${CSV}" ]; then
  say "CSV: ${CSV}"
  # surface Q/plays if present
  echo "[agent] summary:"; grep -Eo '\{"q":[^}]+}' /tmp/agent.out | tail -n1 || true
else
  say "[!] RL CSV empty or missing: ${CSV}"
  tail -n 80 /tmp/logger.out || true
  tail -n 80 /tmp/agent.out || true
  # leave topo running for inspection if REUSE_TOPOLOGY=1; otherwise we’ll still clean it
  [ -n "${topo_pid}" ] && kill "${topo_pid}" 2>/dev/null || true
  exit 1
fi

# Cleanup topology if we spawned it
if [ -n "${topo_pid}" ]; then
  say "[topo] stopping"
  kill "${topo_pid}" 2>/dev/null || true
fi

exit 0
