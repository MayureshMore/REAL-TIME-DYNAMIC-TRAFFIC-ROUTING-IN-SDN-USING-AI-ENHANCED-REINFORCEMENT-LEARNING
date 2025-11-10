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
LOG_DIR="${REPO}/docs/baseline"

say(){ printf "%s\n" "$*"; }
die(){ echo "[x] $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "missing: $1"; }
need curl; need jq; need python3; need sudo

# Be tolerant: only verify reachability, don’t hard-fail if the JSON isn’t ready yet.
wait_for_controller(){
  local deadline=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if curl -fsS --connect-timeout 1 --max-time 2 "${API_BASE}/health" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
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

# Nudge ARP/IPv4 in both directions; ignore failures.
stimulate_ipv4_bidir(){
  local h1_pid h2_pid h1_ip="${1:-10.0.0.1}" h2_ip="${2:-10.0.0.2}"
  h1_pid="$(pgrep -f 'mininet:h1' | head -n1 || true)"
  h2_pid="$(pgrep -f 'mininet:h2' | head -n1 || true)"
  if [ -n "${h1_pid}" ]; then sudo mnexec -a "${h1_pid}" ping -c1 -W1 "${h2_ip}" >/dev/null 2>&1 || true; fi
  if [ -n "${h2_pid}" ]; then sudo mnexec -a "${h2_pid}" ping -c1 -W1 "${h1_ip}" >/dev/null 2>&1 || true; fi
}

# Try to get k paths both directions; if one side says "hosts not learned", keep poking.
warm_paths_bidir(){
  local H1="$1" H2="$2" deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local a b
    a="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo "")"
    b="$(curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" || echo "")"
    if grep -q '\[{' <<<"$a" && grep -q '\[{' <<<"$b"; then
      return 0
    fi
    stimulate_ipv4_bidir
    sleep 1
  done
  return 1
}

main(){
  # 1) Controller must respond (don’t second-guess ensure_controller from auto_demo)
  if ! wait_for_controller; then
    die "controller not reachable at ${API_BASE}/health"
  fi

  # 2) Discover two hosts
  local macs H1 H2
  macs="$(wait_for_hosts)" || die "no hosts found via ${API_BASE}/hosts"
  read -r H1 H2 <<<"${macs}"
  say "[ok] hosts: ${H1} ${H2}"

  # 3) Warm ARP + path cache *both* directions
  stimulate_ipv4_bidir
  warm_paths_bidir "${H1}" "${H2}" || say "[warn] paths not warmed both ways; continuing anyway"

  # 4) Start logger first
  mkdir -p "${LOG_DIR}"
  local ts csv
  ts="$(date +%Y%m%d_%H%M%S)"
  csv="${LOG_DIR}/ports_rl_${ts}.csv"
  say "[logger] ${csv} for ${DURATION}s"
  python3 "${LOGGER}" \
    --controller "${API_BASE}" \
    --interval 1 \
    --duration "${DURATION}" \
    --out "${csv}" >/tmp/logger.out 2>&1 &
  local logger_pid=$!

  # 5) Start agent
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

  # 6) Stop logger
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

  # 7) Emit CSV path so auto_demo.sh can scrape it
  echo "${csv}"
}

main "$@"
