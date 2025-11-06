#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OF_PORT="${OF_PORT:-6633}"
WSAPI_PORT="${WSAPI_PORT:-8080}"
CTRL_HOST="${CTRL_HOST:-127.0.0.1}"

# How long to keep the quick sanity topo alive (seconds)
SANITY_SECS="${SANITY_SECS:-15}"

# Baseline + RL durations
BASELINE_DURATION="${BASELINE_DURATION:-120}"
RL_DURATION="${RL_DURATION:-120}"

# RL params
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"

# How long to wait for hosts + k-paths to materialize
PATH_WAIT_SECS="${PATH_WAIT_SECS:-150}"

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
  # Mininet cleanup (quiet, resilient)
  sudo mn -c    >/dev/null 2>&1 || true
  # The pattern mininet: can be weird; match safely and ignore failures
  sudo pkill -9 -f 'mininet($|:)' >/dev/null 2>&1 || true
  # OVS residuals, temp files
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
  # Launch short demo in background; keep output compact in a file
  python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${SANITY_SECS}" > /tmp/topo_sanity.out 2>&1 &

  # Poll a few times while the topo is alive
  end=$(( $(date +%s) + SANITY_SECS ))
  printed=0
  while [ "$(date +%s)" -lt "${end}" ]; do
    nodes="$(curl -sf "${API_BASE}/topology/nodes"    | jq -c '.')" || nodes="[]"
    links="$(curl -sf "${API_BASE}/topology/links"    | jq -c '.')" || links="[]"
    hosts="$(curl -sf "${API_BASE}/hosts"             | jq -c '.')" || hosts="[]"

    [ $printed -eq 0 ] && {
      say "  nodes: $(jq -c '.' <<<"${nodes}")"
      say "  links: $(jq -c '.' <<<"${links}")"
      say "  hosts: $(jq -c '.' <<<"${hosts}")"
      printed=1
    }

    # If we have 2 hosts, try to fetch k-paths for H1->H2
    if [ "$(jq 'length' <<<"${hosts}")" -ge 2 ]; then
      H1="$(jq -r '.[0].mac' <<<"${hosts}")"
      H2="$(jq -r '.[1].mac' <<<"${hosts}")"
      paths="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=2" | jq -c '.')" || paths="[]"

      if [ "$(jq 'length' <<<"${paths}")" -ge 1 ]; then
        say "  paths: ${paths}"
        say "  Graph healthy: ≥2 hosts and ≥1 path between ${H1} → ${H2}"
        break
      fi
    fi
    sleep 1
  done
}

run_baseline() {
  step "3) Baseline run for ${BASELINE_DURATION}s"
  # The baseline script will bring up its own short-lived topology
  export DURATION="${BASELINE_DURATION}"
  # Keep output reasonably readable
  CSV=$(
    DURATION="${BASELINE_DURATION}" bash "${RUN_BASELINE}" \
      2>/tmp/baseline.err | tee /tmp/baseline.out | awk '/CSV: /{print $NF}'
  )
  CSV="${CSV:-}"
  if [ -z "${CSV}" ]; then
    say "  [!] Could not detect baseline CSV path. Check /tmp/baseline.err"
  else
    say "  Baseline CSV: ${CSV}"
  fi

  say
  say "  Live peek: ${CSV} (every 10s for 60s)"
  # Compact peek (cut first few rows only when present)
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

  local hosts_json paths_json H1 H2
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    hosts_json="$(curl -sf "${API_BASE}/hosts" || echo '[]')"
    if [ "$(jq 'length' <<<"${hosts_json}")" -ge 2 ]; then
      H1="$(jq -r '.[0].mac' <<<"${hosts_json}")"
      H2="$(jq -r '.[1].mac' <<<"${hosts_json}")"

      # "Kick" MAC learning if needed (lightweight ping request to controller via demo already running)
      # Also ask for k=K paths explicitly to populate cache
      paths_json="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo '[]')"

      # If still zero, try the reverse direction once to warm both ways
      if [ "$(jq 'length' <<<"${paths_json}")" -eq 0 ]; then
        curl -sf "${API_BASE}/paths?src_mac=${H2}&dst_mac=${H1}&k=${K}" >/dev/null || true
        sleep 1
        paths_json="$(curl -sf "${API_BASE}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo '[]')"
      fi

      if [ "$(jq 'length' <<<"${paths_json}")" -ge 1 ]; then
        printf "%s\n" "${H1} ${H2}"
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

run_rl() {
  step "4) RL run for ${RL_DURATION}s (epsilon=${EPSILON}, k=${K})"
  patch_once

  # Clean any previous topo strongly to avoid interface-pair reuse
  say "  [prep] sudo mn -c"
  sudo mn -c >/dev/null 2>&1 || true

  # Start controller (ensure script handles idempotency)
  say "  [ctrl] Starting controller OF:${OF_PORT} REST:${WSAPI_PORT}"
  WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${WSAPI_PORT}" >/dev/null

  # Launch the two-path demo in the background for RL duration
  say "  [topo] Launching two-path demo for ${RL_DURATION}s"
  python3 "${TOPO}" --controller_ip "${CTRL_HOST}" --no_cli --duration "${RL_DURATION}" > /tmp/topo_rl.out 2>&1 &

  # Wait for hosts & paths (more patient and warms both directions)
  say "  [wait] Waiting up to ${PATH_WAIT_SECS}s for hosts and k-paths..."
  if read -r H1 H2 <<<"$(wait_for_paths "${PATH_WAIT_SECS}")"; then
    say "  [wait] Paths available for ${H1} -> ${H2}"
  else
    say "  [x] timed out waiting for hosts/paths; check controller logs"
    exit 1
  fi

  # Run RL experiment (logger+agent are managed by run_with_rl.sh)
  export DURATION="${RL_DURATION}" EPSILON="${EPSILON}" K="${K}" CTRL_HOST="${CTRL_HOST}" WSAPI_PORT="${WSAPI_PORT}" OF_PORT="${OF_PORT}"
  OUT=$(
    DURATION="${RL_DURATION}" EPSILON="${EPSILON}" K="${K}" \
      bash "${RUN_RL}" 2>/tmp/rl.err | tee /tmp/rl.out
  )

  # Extract CSV path printed by run_with_rl.sh
  RL_CSV="$(grep -Eo 'docs/baseline/ports_rl_[0-9_]+\.csv' /tmp/rl.out | tail -n1 || true)"
  [ -n "${RL_CSV}" ] && say "  RL CSV: ${RL_CSV}"

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
}

plot_results() {
  step "5) Plotting results"
  mkdir -p "${PLOTS_DIR}"
  # Be tolerant if pandas/matplotlib aren't installed; tell the user clearly
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
