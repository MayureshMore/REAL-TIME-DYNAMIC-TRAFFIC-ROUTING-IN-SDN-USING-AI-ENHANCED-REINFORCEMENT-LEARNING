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

# Extra breathing room to detect hang beyond reported "RL run complete"
RL_WATCHDOG_PAD="${RL_WATCHDOG_PAD:-45}"   # seconds

ENSURE="${REPO}/scripts/ensure_controller.sh"
TOPO="${REPO}/scripts/topos/two_path.py"
RUN_BASELINE="${REPO}/scripts/experiments/run_baseline.sh"
RUN_RL="${REPO}/scripts/experiments/run_with_rl.sh"

PLOTS_DIR="${REPO}/docs/baseline/plots"
CSV_DIR="${REPO}/docs/baseline"

API_BASE="http://${CTRL_HOST}:${WSAPI_PORT}/api/v1"

indent() { sed 's/^/  /'; }
say()    { printf "%s\n" "$*"; }
step()   { printf "\n==> %s\n" "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { say "[x] missing dependency: $1"; exit 1; }; }
need curl; need jq; need python3; need tmux; need sudo
command -v timeout >/dev/null 2>&1 || { say "[!] 'timeout' not found; install coreutils."; }

# ---- Kill & diagnostics helpers ------------------------------------------------

kill_stray_tails() {
  pkill -f "tail -f /tmp/agent.out"    >/dev/null 2>&1 || true
  pkill -f "tail -f /tmp/logger.out"   >/dev/null 2>&1 || true
  pkill -f "tail -f /tmp/topo.out"     >/dev/null 2>&1 || true
  pkill -f "tail -n \+1 -f "           >/dev/null 2>&1 || true
}

kill_stragglers() {
  # Anything likely to keep the demo alive
  pkill -f "${TOPO}"                   >/dev/null 2>&1 || true
  pkill -f "bandit_agent.py"           >/dev/null 2>&1 || true
  pkill -f "logger.py"                 >/dev/null 2>&1 || true
  kill_stray_tails
}

dump_diagnostics() {
  say "  [diag] Process tree (grep: ryu|mininet|python|tail|agent|logger)"
  ps -eo pid,ppid,etime,cmd \
    | grep -E "ryu|mininet|python|tail|bandit_agent\.py|logger\.py|two_path\.py" \
    | grep -v grep | indent || true

  say "  [diag] Open listeners"
  ss -ltnp 2>/dev/null | indent || true

  say "  [diag] Controller health"
  curl -sf "${API_BASE}/health" | jq -c '.' 2>/dev/null | indent || say "  (health endpoint unavailable)"

  for f in /tmp/rl.out /tmp/rl.err /tmp/topo_rl.out /tmp/topo_sanity.out /tmp/logger.out /tmp/agent.out; do
    [ -f "$f" ] || continue
    say "  [diag] tail -n 40 ${f}"
    tail -n 40 "$f" | indent
  done
}

# ---- One-time patching of known footguns in run_with_rl.sh ---------------------

patch_once() {
  # Correct agent path & ensure script reference if stale
  if grep -q 'scripts/agents/bandit_agent.py' "${RUN_RL}"; then
    sed -i 's#scripts/agents/bandit_agent.py#rl-agent/bandit_agent.py#g' "${RUN_RL}"
    say "[patch] Updated bandit agent path in run_with_rl.sh"
  fi
  if grep -q 'scripts/_ensure_controller.sh' "${RUN_RL}"; then
    sed -i 's#scripts/_ensure_controller.sh#scripts/ensure_controller.sh#g' "${RUN_RL}"
    say "[patch] Updated ensure_controller reference in run_with_rl.sh"
  fi

  # Neuter any 'tail -f' that would hold the TTY open
  if grep -q 'tail[[:space:]]\+-n[[:space:]]\+\\\?+1[[:space:]]\+-f' "${RUN_RL}"; then
    # Replace "tail -n +1 -f ..." with a one-time "tail -n 200 ..." (non-follow)
    sed -i 's/tail[[:space:]]\+-n[[:space:]]\+\\\?+1[[:space:]]\+-f/tail -n 200/g' "${RUN_RL}"
    say "[patch] Replaced tail -f with non-following tail in run_with_rl.sh"
  fi
  if grep -q 'tail[[:space:]]\+-f' "${RUN_RL}"; then
    sed -i 's/tail[[:space:]]\+-f/tail -n 200/g' "${RUN_RL}"
    say "[patch] Replaced generic tail -f with non-following tail in run_with_rl.sh"
  fi
}

# ---- Stages --------------------------------------------------------------------

clean_start() {
  step "0) Clean start"
  sudo mn -c >/dev/null 2>&1 || true
  sudo pkill -9 -f 'mininet($|:)' >/dev/null 2>&1 || true
  kill_stragglers
  rm -f /tmp/vconn* /tmp/vlogs* /tmp/*.out /tmp/*.log 2>/dev/null || true
  rm -f ~/.ssh/mn/* 2>/dev/null || true
  say "  Cleanup complete."
}

start_controller() {
  step "1) Starting controller (OF ${OF_PORT}, REST ${WSAPI_PORT})"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" | indent
  say "  Controller healthy and listening."
}

sanity_topo() {
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

    if [ "$(jq -r 'type=="array" and length>=2' <<<"${hosts}")" = "true" ]; then
      H1="$(jq -r '.[0].mac // empty' <<<"${hosts}")"
      H2="$(jq -r '.[1].mac // empty' <<<"${hosts}")"
      if [[ "${H1}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ && "${H2}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
        paths="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=2" | jq -c '.' 2>/dev/null || echo '[]')"
        if [ "$(jq -r 'type=="array" and length>0' <<<"${paths}")" = "true" ]; then
          say "  paths: ${paths}"
          say "  Graph healthy: ≥2 hosts and ≥1 path between ${H1} → ${H2}"
          break
        fi
      fi
    fi
    sleep 1
  done
}

run_baseline() {
  step "3) Baseline run for ${BASELINE_DURATION}s"
  export DURATION="${BASELINE_DURATION}"
  CSV=$(
    DURATION="${BASELINE_DURATION}" bash "${RUN_BASELINE}" \
      2>/tmp/baseline.err | tee /tmp/baseline.out | awk '/CSV: /{print $NF}' | tail -n1
  )
  CSV="${CSV:-}"
  if [ -z "${CSV}" ]; then
    say "  [!] Could not detect baseline CSV path. Check /tmp/baseline.err"
  else
    say "  Baseline CSV: ${CSV}"
  fi

  say
  say "  Live peek: ${CSV} (every 10s for 60s)"
  for _ in {1..6}; do
    if [ -f "${CSV}" ]; then
      tail -n 12 "${CSV}" | sed 's/[[:space:]]\+$//' | indent
    else
      say "  (waiting for first samples...)"
    fi
    sleep 10
  done
}

wait_for_paths() {
  local timeout_sec="${1:-$PATH_WAIT_SECS}"
  local deadline=$(( $(date +%s) + timeout_sec ))
  local hosts_json macs_line attempts=0
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    attempts=$((attempts+1))
    hosts_json="$(curl -sf "${API_BASE}/hosts" 2>/dev/null || echo '[]')"
    macs_line="$(
      jq -r '
        [ .[] | .mac? | strings
          | select(test("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"))
        ]
        | select(length>=2)
        | "\(. [0]) \(. [1])"
      ' <<<"${hosts_json}" 2>/dev/null || true
    )"
    if [ -n "${macs_line}" ]; then
      printf "%s\n" "${macs_line}"
      return 0
    else
      if (( attempts % 10 == 0 )); then
        say "  [wait] Hosts not ready yet (attempt ${attempts}); latest payload: ${hosts_json}"
      fi
    fi
    sleep 1
  done
  return 1
}

run_rl() {
  step "4) RL run for ${RL_DURATION}s (epsilon=${EPSILON}, k=${K})"
  patch_once

  say "  [prep] sudo mn -c"
  sudo mn -c >/dev/null 2>&1 || true

  say "  [ctrl] Starting controller OF:${OF_PORT} REST:${WSAPI_PORT}"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" >/dev/null

  local topo_secs=$(( RL_DURATION + PATH_WAIT_SECS + 15 ))
  say "  [topo] Launching two-path demo for ${topo_secs}s (buffered)"
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${topo_secs}" > /tmp/topo_rl.out 2>&1 & local topo_pid=$!

  say "  [wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
  macs="$(wait_for_paths "${PATH_WAIT_SECS}")" || {
    say "  [x] timed out waiting for hosts/paths; controller/topology logs below"
    dump_diagnostics
    [ -n "${topo_pid:-}" ] && { kill "${topo_pid}" 2>/dev/null || true; kill -9 "${topo_pid}" 2>/dev/null || true; }
    exit 1
  }

  read -r H1 H2 <<<"${macs}"
  say "  [wait] Paths available for ${H1} -> ${H2}"

  # Warm both directions
  curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" >/dev/null || true
  curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" >/dev/null || true

  # Env for RL runner
  export DURATION="${RL_DURATION}" EPSILON="${EPSILON}" K="${K}"
  export SRC_MAC="${H1}" DST_MAC="${H2}" CTRL_HOST="${CTRL_HOST}" WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}"
  export REUSE_TOPOLOGY=1 PATH_WAIT_SECS="${PATH_WAIT_SECS}"

  rm -f /tmp/rl.out /tmp/rl.err

  # HARD TIMEOUT to catch hangs: RL duration + pad
  local watch_secs=$(( RL_DURATION + RL_WATCHDOG_PAD ))
  say "  [watchdog] timeout ${watch_secs}s for run_with_rl.sh"
  set +e
  timeout --foreground "${watch_secs}" bash "${RUN_RL}" 2>/tmp/rl.err | tee /tmp/rl.out
  RL_STATUS=$?
  set -e

  # 124 == timeout; anything else nonzero means failure
  if [ ${RL_STATUS} -ne 0 ]; then
    if [ ${RL_STATUS} -eq 124 ]; then
      say "  [x] RL step exceeded ${watch_secs}s (likely hang after \"q\")."
    else
      say "  [x] RL step exited with status ${RL_STATUS}."
    fi
    say "  [diag] Collecting hang diagnostics..."
    dump_diagnostics
  fi

  # Best-effort CSV discovery
  RL_CSV="$(grep -Eo 'docs/baseline/ports_rl_[0-9_]+\.csv' /tmp/rl.out | tail -n1 || true)"
  if [ -z "${RL_CSV}" ]; then
    RL_CSV="$(ls -1t "${CSV_DIR}"/ports_rl_*.csv 2>/dev/null | head -n1 || true)"
  fi

  # Always stop tails/agents/topo now
  kill_stragglers
  if [ -n "${topo_pid:-}" ]; then
    say "  [topo] Stopping demo topology"
    kill "${topo_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${topo_pid}" 2>/dev/null || true
  fi

  if [ -n "${RL_CSV}" ] && [ -f "${RL_CSV}" ]; then
    say "  RL CSV: ${RL_CSV}"
  else
    say "  [!] RL CSV not found; check /tmp/rl.out and /tmp/rl.err"
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

  # If we hit the watchdog, fail the script so you notice in CI, but only after we showed logs
  if [ ${RL_STATUS} -ne 0 ]; then
    exit ${RL_STATUS}
  endfi
}

plot_results() {
  step "5) Plotting results"
  mkdir -p "${PLOTS_DIR}"
  if ! python3 -c "import pandas, matplotlib" >/dev/null 2>&1; then
    say "  [!] pandas/matplotlib not found in this env. Install with:"
    say "      pip install pandas matplotlib"
    return 0
  fi

  set +e
  python3 "${REPO}/scripts/metrics/plot_results.py" \
    --files "${CSV_DIR}/ports_baseline_"*.csv "${CSV_DIR}/ports_rl_"*.csv \
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

main() {
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
