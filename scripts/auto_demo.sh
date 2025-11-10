#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OF_PORT="${OF_PORT:-6633}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"

SANITY_SECS="${SANITY_SECS:-15}"
BASELINE_DURATION="${BASELINE_DURATION:-120}"
RL_DURATION="${RL_DURATION:-120}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
PATH_WAIT_SECS="${PATH_WAIT_SECS:-180}"
RL_WATCHDOG_PAD="${RL_WATCHDOG_PAD:-45}"

ENSURE="${REPO}/scripts/ensure_controller.sh"
TOPO="${REPO}/scripts/topos/two_path.py"
RUN_BASELINE="${REPO}/scripts/experiments/run_baseline.sh"
RUN_RL="${REPO}/scripts/experiments/run_with_rl.sh"

CSV_DIR="${REPO}/docs/baseline"
PLOTS_DIR="${REPO}/docs/baseline/plots"
API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

indent(){ sed 's/^/  /'; }
say(){ printf "%s\n" "$*"; }
step(){ printf "\n==> %s\n" "$*"; }
need(){ command -v "$1" >/dev/null 2>&1 || { say "[x] missing: $1"; exit 1; }; }

need curl; need jq; need python3; need sudo
command -v timeout >/dev/null 2>&1 || true

kill_stragglers(){
  pkill -f "${TOPO}" >/dev/null 2>&1 || true
  pkill -f "bandit_agent.py" >/dev/null 2>&1 || true
  pkill -f "log_stats.py" >/dev/null 2>&1 || true
}

clean_start(){
  step "0) Clean start"
  sudo mn -c >/dev/null 2>&1 || true
  sudo pkill -9 -f 'mininet($|:)' >/dev/null 2>&1 || true
  kill_stragglers
  rm -f /tmp/*.out /tmp/*.err 2>/dev/null || true
  say "  Cleanup complete."
}

start_controller(){
  step "1) Starting controller (OF ${OF_PORT}, REST ${WSAPI_PORT})"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" | indent
  say "  Controller healthy and listening."
}

sanity_topo(){
  step "2) Sanity topology up for ~${SANITY_SECS}s"
  say "  Waiting for graph (hosts & k-paths)..."
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${SANITY_SECS}" > /tmp/topo_sanity.out 2>&1 &
  end=$(( $(date +%s) + SANITY_SECS ))
  printed=0
  while [ "$(date +%s)" -lt "${end}" ]; do
    nodes="$(curl -sf "${API_BASE}/topology/nodes" | jq -c '.' 2>/dev/null || echo '[]')"
    links="$(curl -sf "${API_BASE}/topology/links" | jq -c '.' 2>/dev/null || echo '[]')"
    hosts="$(curl -sf "${API_BASE}/hosts" | jq -c '.' 2>/dev/null || echo '[]')"
    if [ $printed -eq 0 ] && [ "$(jq -r 'type=="array" and length>0' <<<"${nodes}")" = "true" ]; then
      say "  nodes: ${nodes}"
      say "  links: ${links}"
      say "  hosts: ${hosts}"
      printed=1
    fi
    sleep 1
  done
}

run_baseline(){
  step "3) Baseline run for ${BASELINE_DURATION}s"
  export DURATION="${BASELINE_DURATION}"
  BASELINE_CSV=$(
    DURATION="${BASELINE_DURATION}" bash "${RUN_BASELINE}" \
      2>/tmp/baseline.err | tee /tmp/baseline.out | awk '/CSV: /{print $NF}' | tail -n1
  )
  BASELINE_CSV="${BASELINE_CSV:-}"
  if [ -z "${BASELINE_CSV}" ]; then
    say "  [!] Could not detect baseline CSV path. Check /tmp/baseline.err"
  else
    say "  Baseline CSV: ${BASELINE_CSV}"
  fi

  say
  say "  Live peek: ${BASELINE_CSV} (every 10s for 60s)"
  for _ in {1..6}; do
    if [ -f "${BASELINE_CSV}" ]; then
      tail -n 12 "${BASELINE_CSV}" | sed 's/[[:space:]]\+$//' | indent
    else
      say "  (waiting for first samples...)"
    fi
    sleep 10
  done
}

run_rl(){
  step "4) RL run for ${RL_DURATION}s (epsilon=${EPSILON}, k=${K})"

  say "  [prep] sudo mn -c"
  sudo mn -c >/dev/null 2>&1 || true

  say "  [ctrl] Starting controller OF:${OF_PORT} REST:${WSAPI_PORT}"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" >/dev/null

  local topo_secs=$(( RL_DURATION + PATH_WAIT_SECS + 15 ))
  say "  [topo] Launching two-path demo for ${topo_secs}s (buffered)"
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${topo_secs}" > /tmp/topo_rl.out 2>&1 &
  local topo_pid=$!

  export CTRL_HOST WSAPI_PORT DURATION RL_DURATION EPSILON K
  set +e
  timeout --foreground $(( RL_DURATION + RL_WATCHDOG_PAD )) \
    bash "${RUN_RL}" 2>/tmp/rl.err | tee /tmp/rl.out
  RL_STATUS=$?
  set -e

  # Always stop topology now
  if [ -n "${topo_pid:-}" ]; then
    say "  [topo] Stopping demo topology"
    kill "${topo_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${topo_pid}" 2>/dev/null || true
  fi

  RL_CSV="$(grep -Eo 'docs/baseline/ports_rl_[0-9_]+\.csv' /tmp/rl.out | tail -n1 || true)"
  if [ -n "${RL_CSV}" ] && [ -f "${RL_CSV}" ]; then
    say "  RL CSV: ${RL_CSV}"
  else
    say "  [!] RL CSV not found; see /tmp/rl.out and /tmp/rl.err"
  fi

  say
  say "  Live peek: ${RL_CSV:-<unknown>} (every 10s for 60s)"
  for _ in {1..6}; do
    if [ -n "${RL_CSV}" ] && [ -f "${RL_CSV}" ]; then
      tail -n 12 "${RL_CSV}" | sed 's/[[:space:]]\+$//' | indent
    else
      say "  (waiting for first samples...)"
    fi
    sleep 10
  done

  if [ ${RL_STATUS} -ne 0 ]; then
    say "  [x] RL step exited with status ${RL_STATUS}."
    say "  [diag] tail -n 60 /tmp/rl.err"; tail -n 60 /tmp/rl.err | indent || true
    exit ${RL_STATUS}
  fi

  # Export for plot stage
  export BASELINE_CSV RL_CSV
}

plot_results(){
  step "5) Plotting results"
  mkdir -p "${PLOTS_DIR}"
  if ! python3 -c "import pandas, matplotlib" >/dev/null 2>&1; then
    say "  [!] pandas/matplotlib missing; install with: pip install pandas matplotlib"
    return 0
  fi
  if [ -z "${BASELINE_CSV:-}" ] || [ -z "${RL_CSV:-}" ]; then
    say "  [!] Skipping plots: missing CSVs"
    return 0
  fi

  set +e
  python3 "${REPO}/scripts/metrics/plot_results.py" \
    --files "${BASELINE_CSV}" "${RL_CSV}" \
    --labels Baseline RL >/tmp/plot.out 2>&1
  rc=$?
  set -e

  if [ $rc -eq 0 ]; then
    say "  ok"
    say "  [✓] Saved ${PLOTS_DIR}/throughput.png"
    say "  [✓] Saved ${PLOTS_DIR}/drops.png"
    say "  [✓] Saved ${PLOTS_DIR}/errors.png"
    say
    say "  Plots saved in ${PLOTS_DIR}"
  else
    say "  [!] Plotting failed; see /tmp/plot.out"
  fi
}

main(){
  clean_start
  start_controller
  sanity_topo
  run_baseline
  run_rl
  plot_results
  say
  say "Done."
}

main "$@"
