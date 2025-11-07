#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OF_PORT="${OF_PORT:-6633}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"

# Keep the quick sanity topo alive
SANITY_SECS="${SANITY_SECS:-15}"

# Baseline + RL durations
BASELINE_DURATION="${BASELINE_DURATION:-120}"
RL_DURATION="${RL_DURATION:-120}"

# RL params
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"

# How long to wait for hosts + k-paths to materialize
PATH_WAIT_SECS="${PATH_WAIT_SECS:-180}"

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

need() {
  command -v "$1" >/dev/null 2>&1 || { say "[x] missing dependency: $1"; exit 1; }
}

need curl
need jq
need python3
need tmux
need sudo

patch_once() {
  # Ensure RL script points to rl-agent/bandit_agent.py and uses ensure_controller.sh
  if grep -q 'scripts/agents/bandit_agent.py' "${RUN_RL}"; then
    sed -i 's#scripts/agents/bandit_agent.py#rl-agent/bandit_agent.py#g' "${RUN_RL}"
    say "[patch] Fixed bandit agent path in run_with_rl.sh"
  fi
  if grep -q 'scripts/_ensure_controller.sh' "${RUN_RL}"; then
    sed -i 's#scripts/_ensure_controller.sh#scripts/ensure_controller.sh#g' "${RUN_RL}"
    say "[patch] Fixed ensure_controller reference in run_with_rl.sh"
  fi
}

clean_start() {
  step "0) Clean start"
  sudo mn -c    >/dev/null 2>&1 || true
  sudo pkill -9 -f 'mininet($|:)' >/dev/null 2>&1 || true
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
    nodes="$(curl -sf "${API_BASE}/topology/nodes"    | jq -c '.' 2>/dev/null || echo '[]')"
    links="$(curl -sf "${API_BASE}/topology/links"    | jq -c '.' 2>/dev/null || echo '[]')"
    hosts="$(curl -sf "${API_BASE}/hosts"             | jq -c '.' 2>/dev/null || echo '[]')"

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
        # accept either array of objects with dpids/hops, or any non-empty array
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

# robust H1/H2/path discovery with validation
wait_for_paths() {
  local timeout_sec="${1:-$PATH_WAIT_SECS}"
  local deadline=$(( $(date +%s) + timeout_sec ))

  local hosts_json macs_line H1 H2 paths_ok="false" attempts=0

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    attempts=$((attempts+1))
    # Pull hosts; require at least two valid MAC strings
    hosts_json="$(curl -sf "${API_BASE}/hosts" 2>/dev/null || echo '[]')"

    macs_line="$(
      jq -r '
        # collect all string MACs that match AA:BB:CC:DD:EE:FF
        [ .[] | .mac? | strings
          | select(test("^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"))
        ]
        | select(length>=2)
        | "\(. [0]) \(. [1])"
      ' <<<"${hosts_json}" 2>/dev/null || true
    )"

    if [ -n "${macs_line}" ]; then
      # shellcheck disable=SC2086
      read -r H1 H2 <<<"${macs_line}"

      # Warm both directions to populate k-path cache
      curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" >/dev/null || true
      curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" >/dev/null || true
      sleep 1

      # Verify paths exist from H1->H2
      if curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" | jq -e 'type=="array" and length>0' >/dev/null 2>&1; then
        printf "%s %s\n" "${H1}" "${H2}"
        return 0
      fi
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

  # Launch topology with a buffer so it outlives path-wait + RL runtime
  local topo_secs=$(( RL_DURATION + PATH_WAIT_SECS + 15 ))
  say "  [topo] Launching two-path demo for ${topo_secs}s (buffered)"
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${topo_secs}" > /tmp/topo_rl.out 2>&1 & topo_pid=$!

  say "  [wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
  macs="$(wait_for_paths "${PATH_WAIT_SECS}")" || {
    say "  [x] timed out waiting for hosts/paths; check controller logs"
    if [ -f /tmp/topo_rl.out ]; then
      say "  [debug] tail /tmp/topo_rl.out"
      tail -n 40 /tmp/topo_rl.out | indent
    fi
    if [ -f "${HOME}/ryu-controller.log" ]; then
      say "  [debug] tail ~/ryu-controller.log"
      tail -n 40 "${HOME}/ryu-controller.log" | indent
    fi
    [ -n "${topo_pid:-}" ] && kill "${topo_pid}" 2>/dev/null || true
    exit 1
  }

  # shellcheck disable=SC2086
  read -r H1 H2 <<<"${macs}"
  if [ -z "${H1}" ] || [ -z "${H2}" ]; then
    say "  [x] internal error: empty H1/H2 after path wait"
    [ -n "${topo_pid:-}" ] && kill "${topo_pid}" 2>/dev/null || true
    exit 1
  fi
  say "  [wait] Paths available for ${H1} -> ${H2}"

  # Export MACs so the RL runner/agent use exactly these endpoints
  export DURATION="${RL_DURATION}" EPSILON="${EPSILON}" K="${K}"
  export SRC_MAC="${H1}" DST_MAC="${H2}" CTRL_HOST="${CTRL_HOST}" WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}"
  export REUSE_TOPOLOGY=1

  # Run RL (stream logs to console while tee'ing to disk)
  rm -f /tmp/rl.out /tmp/rl.err
  bash "${RUN_RL}" 2>/tmp/rl.err | tee /tmp/rl.out

  # Prefer the path echoed by the runner; otherwise choose newest RL CSV
  RL_CSV="$(grep -Eo 'docs/baseline/ports_rl_[0-9_]+\.csv' /tmp/rl.out | tail -n1 || true)"
  if [ -z "${RL_CSV}" ]; then
    RL_CSV="$(ls -1t "${CSV_DIR}"/ports_rl_*.csv 2>/dev/null | head -n1 || true)"
  fi

  if [ -n "${RL_CSV}" ] && [ -f "${RL_CSV}" ]; then
    say "  RL CSV: ${RL_CSV}"
  else
    say "  [!] RL CSV not found. See /tmp/rl.out and /tmp/rl.err"
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

  # Let the short-lived topo finish quietly
  [ -n "${topo_pid:-}" ] && wait "${topo_pid}" 2>/dev/null || true
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
