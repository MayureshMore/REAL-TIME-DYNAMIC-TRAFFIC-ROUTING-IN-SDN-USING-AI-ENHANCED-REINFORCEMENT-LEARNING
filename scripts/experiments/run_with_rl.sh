#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

CTRL_HOST="${CTRL_HOST:-127.0.0.1}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

DURATION="${DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"

AGENT="${REPO}/rl-agent/bandit_agent.py"
LOGGER="${REPO}/scripts/metrics/log_stats.py"
ENSURE="${REPO}/scripts/ensure_controller.sh"
LOG_DIR="${REPO}/docs/baseline"

die(){ echo "[x] $*" >&2; exit 1; }
say(){ printf "%s\n" "$*"; }

need(){ command -v "$1" >/dev/null 2>&1 || die "missing: $1"; }
need curl; need jq; need python3; need sudo

wait_for_controller(){
  for _ in {1..60}; do
    if curl -fsS "${API_BASE}/health" | grep -q '"status":"ok"'; then
      return 0
    fi
    sleep 1
  done
  return 1
}

bootstrap_controller_if_needed(){
  if wait_for_controller; then
    return 0
  fi
  # Try to start it ourselves
  say "[ctrl] controller not healthy; starting via ensure_controller.sh"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT:-6633}" \
    "${ENSURE}" "${OF_PORT:-6633}" "${WSAPI_PORT}" >/tmp/rl.ensure.log 2>&1 || true
  # Try again
  wait_for_controller || {
    say "[diag] ensure_controller output (tail):"
    tail -n 80 /tmp/rl.ensure.log 2>/dev/null || true
    die "controller still not healthy at ${API_BASE}/health"
  }
}

# Return "MAC1 MAC2" when ≥2 hosts exist
wait_for_hosts(){
  local deadline=$(( $(date +%s) + 90 ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local macs
    macs="$(
      curl -fsS "${API_BASE}/hosts" 2>/dev/null \
      | jq -r '
          [ .[] | .mac? | strings
            | select(test("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"))
          ] | select(length>=2) | "\(. [0]) \(. [1])"
        ' 2>/dev/null || true
    )"
    if [ -n "${macs}" ]; then
      printf "%s\n" "${macs}"
      return 0
    fi
    sleep 1
  done
  return 1
}

# Force ARP/IPv4 learning by pinging h2 from h1 once
stimulate_ipv4(){
  local h1_pid h2_ip
  h1_pid="$(pgrep -f 'mininet:h1' | head -n1 || true)"
  h2_ip="${1:-10.0.0.2}"
  if [ -n "${h1_pid}" ]; then
    sudo mnexec -a "${h1_pid}" ping -c 1 -W 1 "${h2_ip}" >/dev/null 2>&1 || true
  fi
}

# Wait until hosts show non-empty ipv4 lists
wait_for_ipv4(){
  local deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local ready
    ready="$(
      curl -fsS "${API_BASE}/hosts" 2>/dev/null \
      | jq -r '
          [ .[] | .ipv4? | arrays | any(. != null and . != "" and . != "0.0.0.0") ]
          | add
        ' 2>/dev/null || echo "false"
    )"
    if [ "${ready}" = "true" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

main(){
  # Make sure controller is up (self-heal if not)
  bootstrap_controller_if_needed

  # Discover two hosts
  local macs H1 H2
  macs="$(wait_for_hosts)" || die "no hosts found via ${API_BASE}/hosts"
  read -r H1 H2 <<<"${macs}"
  say "[ok] hosts: ${H1} ${H2}"

  # Warm path cache and force ARP learning
  curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" >/dev/null || true
  curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" >/dev/null || true
  stimulate_ipv4 "10.0.0.2"
  wait_for_ipv4 || say "[warn] ipv4 not confirmed in /hosts; continuing"

  mkdir -p "${LOG_DIR}"
  local ts csv
  ts="$(date +%Y%m%d_%H%M%S)"
  csv="${LOG_DIR}/ports_rl_${ts}.csv"

  # Start logger
  say "[logger] ${csv} for ${DURATION}s"
  python3 "${LOGGER}" \
    --controller "${API_BASE}" \
    --interval 1 \
    --duration "${DURATION}" \
    --out "${csv}" >/tmp/logger.out 2>&1 &
  local logger_pid=$!

  # Start agent
  say "[agent] eps=${EPSILON} k=${K} src=${H1} dst=${H2}"
  set +e
  python3 "${AGENT}" \
    --controller "${API_BASE}" \
    --epsilon "${EPSILON}" \
    --k "${K}" \
    --src "${H1}" \
    --dst "${H2}" >/tmp/agent.out 2>&1
  agent_rc=$?
  set -e

  # Stop logger
  if kill -0 "${logger_pid}" >/dev/null 2>&1; then
    kill -TERM "${logger_pid}" >/dev/null 2>&1 || true
    wait "${logger_pid}" >/dev/null 2>&1 || true
  fi

  if [ ${agent_rc} -ne 0 ]; then
    say "[x] agent failed (rc=${agent_rc})"
    say "[diag] tail -n 80 /tmp/agent.out"; tail -n 80 /tmp/agent.out || true
    say "[diag] tail -n 80 /tmp/logger.out"; tail -n 80 /tmp/logger.out || true
    exit ${agent_rc}
  fi

  # Emit the CSV path for the caller (auto_demo.sh scrapes this)
  echo "${csv}"
}

main "$@"
