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
AGENT="${AGENT:-${REPO}/rl-agent/bandit_agent.py}"

ENSURE="${REPO}/scripts/ensure_controller.sh"
TOPO="${REPO}/scripts/topos/two_path.py"
REUSE_TOPOLOGY="${REUSE_TOPOLOGY:-0}"

CSV_DIR="${REPO}/docs/baseline"
mkdir -p "${CSV_DIR}"
CSV_OUT="${CSV_DIR}/ports_rl_$(date +%Y%m%d_%H%M%S).csv"

API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

die() { echo "[x] $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

need curl
need jq
need python3

if ! grep -q "ensure_controller.sh" <<<"${ENSURE}"; then
  die "ENSURE path looks wrong: ${ENSURE}"
fi

if [ "${REUSE_TOPOLOGY}" = "1" ]; then
  echo "[prep] Reusing existing controller/topology (skipping mn -c + ensure_controller)"
else
  echo "[prep] sudo mn -c"
  sudo mn -c >/dev/null 2>&1 || true

  echo "[ctrl] Starting controller OF:${OF_PORT} REST:${WSAPI_PORT}"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" >/dev/null
fi

TOPO_PID=""
if [ "${REUSE_TOPOLOGY}" = "1" ]; then
  echo "[topo] Reusing externally managed two-path demo"
else
  echo "[topo] Launching two-path demo for ${DURATION}s"
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${DURATION}" > /tmp/topo.out 2>&1 &
  TOPO_PID=$!
fi

echo "[wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
deadline=$(( $(date +%s) + PATH_WAIT_SECS ))
H1="${SRC_MAC:-}"
H2="${DST_MAC:-}"

if [ -n "${H1}" ] && [ -n "${H2}" ]; then
  echo "[wait] Using provided MACs ${H1} -> ${H2}"
  if ! curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" >/dev/null; then
    echo "[wait] Provided MACs not ready yet; falling back to discovery"
    H1=""; H2=""
  fi
fi

if [ -z "${H1}" ] || [ -z "${H2}" ]; then
  attempt=0
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    attempt=$((attempt+1))
    hosts_json="$(curl -sf "${API_BASE}/hosts" || echo '[]')"
    path_count=0
    host_count="$(jq 'length' <<<"${hosts_json}")"
    if [ "${host_count}" -ge 2 ]; then
      H1="$(jq -r '.[0].mac' <<<"${hosts_json}")"
      H2="$(jq -r '.[1].mac' <<<"${hosts_json}")"
      paths_json="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo '[]')"
      path_count="$(jq 'length' <<<"${paths_json}")"
      if [ "${path_count}" -ge 1 ]; then
        echo "[wait] Paths available for ${H1} -> ${H2}"
        break
      fi
    fi
    if (( attempt % 10 == 0 )); then
      echo "[wait] attempt ${attempt}: hosts=${host_count}, paths=${path_count:-0}"
    fi
    sleep 1
  done
fi

if [ -z "${H1}" ] || [ -z "${H2}" ]; then
  echo "[wait] timed out waiting for hosts/paths; see logs below" >&2
  [ -f /tmp/topo.out ] && { echo "[debug] tail /tmp/topo.out" >&2; tail -n 40 /tmp/topo.out >&2; }
  [ -f "${HOME}/ryu-controller.log" ] && { echo "[debug] tail ~/ryu-controller.log" >&2; tail -n 40 "${HOME}/ryu-controller.log" >&2; }
  die "timed out waiting for hosts/paths; check controller logs"
fi

if [ "${REUSE_TOPOLOGY}" = "1" ]; then
  echo "[wait] Confirmed paths for ${H1} -> ${H2} on reused topology"
fi

echo "[warmup] Sending ${WARMUP_PINGS} ICMP echos via Mininet demo (already running)"

echo "[logger] Logging to ${CSV_OUT}; polling ${API_BASE} every ${LOG_INTERVAL}s; duration=${DURATION}s"
python3 "${LOGGER}" \
  --controller "${API_BASE}" \
  --outfile "${CSV_OUT}" \
  --interval "${LOG_INTERVAL}" \
  --duration "${DURATION}" > /tmp/logger.out 2>&1 &
LOGGER_PID=$!

echo "[agent] epsilon=${EPSILON} k=${K} src=${H1} dst=${H2}"
python3 "${AGENT}" \
  --controller "${API_BASE}" \
  --epsilon "${EPSILON}" \
  --k "${K}" \
  --src "${H1}" \
  --dst "${H2}" \
  --duration "${DURATION}" \
  --interval "${LOG_INTERVAL}" > /tmp/agent.out 2>&1 &
AGENT_PID=$!

# tail progress (do NOT wait on this; it never exits on its own)
( sleep 2; tail -n +1 -f /tmp/topo.out /tmp/logger.out /tmp/agent.out 2>/dev/null ) &
TAIL_PID=$!

# Wait strictly for the finite jobs
wait "${AGENT_PID}" "${LOGGER_PID}"

# Now stop tail and (optionally) the topo we launched
kill "${TAIL_PID}" >/dev/null 2>&1 || true

if [ -n "${TOPO_PID}" ]; then
  wait "${TOPO_PID}" 2>/dev/null || true
fi

echo "RL run complete. CSV: ${CSV_OUT}"
echo "${CSV_OUT}"
