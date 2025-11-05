#!/usr/bin/env bash
# scripts/auto_demo.sh
# One-button demo: clean -> controller -> sanity graph -> baseline -> RL -> plot
# Usage:  bash scripts/auto_demo.sh
# Tunables via env: OF_PORT, REST_PORT, DURATION, EPSILON, K, SANITY_TIME

set -Eeuo pipefail

# ---------- Tunables ----------
OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
CTRL_IP="127.0.0.1"
DURATION="${DURATION:-120}"        # per experiment
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
SANITY_TIME="${SANITY_TIME:-15}"   # seconds to keep sanity Mininet up
WAIT_GRAPH="${WAIT_GRAPH:-30}"     # seconds to wait for nodes/links/paths
CSV_PEEK_INTERVAL="${CSV_PEEK_INTERVAL:-10}"  # seconds between CSV peeks
TMUX_SOCKET="-L ryu"
TMUX_SESSION="ryu-app"

# ---------- Paths ----------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_API="http://127.0.0.1:${REST_PORT}/api/v1"
RUN_BASELINE="${REPO}/scripts/experiments/run_baseline.sh"
RUN_RL="${REPO}/scripts/experiments/run_with_rl.sh"
ENSURE_CTRL="${REPO}/scripts/ensure_controller.sh"
TOPO_PY="${REPO}/scripts/topos/two_path.py"
PLOT_PY="${REPO}/scripts/metrics/plot_results.py"

# ---------- Helpers ----------
die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo -e "\n\033[1;36m$*\033[0m"; }
ok()   { echo -e "\033[1;32m$*\033[0m"; }
warn() { echo -e "\033[1;33m$*\033[0m"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

peek_csv() {
  local file="$1" secs="$2"
  note "Live peek: ${file} (every ${CSV_PEEK_INTERVAL}s for ${secs}s)"
  local elapsed=0
  while (( elapsed < secs )); do
    [[ -s "$file" ]] && tail -n 3 "$file" || echo "(waiting for first samples...)"
    sleep "${CSV_PEEK_INTERVAL}"
    elapsed=$((elapsed + CSV_PEEK_INTERVAL))
  done
}

cleanup() {
  set +e
  sudo mn -c >/dev/null 2>&1 || true
  tmux ${TMUX_SOCKET} kill-session -t "${TMUX_SESSION}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# ---------- Preflight ----------
require_cmd curl
require_cmd jq
require_cmd tmux
require_cmd python3
require_cmd sudo

# Make sure the RL script points to the public ensure script (safe to run repeatedly)
if grep -q "scripts/_ensure_controller.sh" "$RUN_RL"; then
  sed -i 's#scripts/_ensure_controller.sh#scripts/ensure_controller.sh#g' "$RUN_RL"
  note "Patched run_with_rl.sh to use scripts/ensure_controller.sh"
fi

# ---------- Step 0: Clean start ----------
note "0) Clean start"
sudo mn -c || true
tmux ${TMUX_SOCKET} kill-session -t "${TMUX_SESSION}" 2>/dev/null || true

# ---------- Step 1: Start controller ----------
note "1) Starting controller (OF ${OF_PORT}, REST ${REST_PORT})"
"${ENSURE_CTRL}" "${OF_PORT}" "${REST_PORT}" >/dev/null
ok "Controller healthy and listening."

# ---------- Step 2: Sanity Mininet (auto) ----------
note "2) Sanity topology up for ~${SANITY_TIME}s"
# Kick a temporary, no-CLI topology to seed MACs and links
sudo python3 "${TOPO_PY}" \
  --controller_ip "${CTRL_IP}" \
  --rest_port "${REST_PORT}" \
  --demo --demo_time "${SANITY_TIME}" --no_cli >/dev/null &

# Wait for nodes/links/hosts/paths
note "Waiting for graph (hosts & k-paths)..."
t0=$(date +%s)
H1=""; H2=""
while :; do
  # hosts present?
  HS="$(curl -sf "${BASE_API}/hosts" || echo '[]')"
  CNT="$(printf '%s' "$HS" | jq 'length')"
  # nodes and links (best-effort)
  NODES="$(curl -sf "${BASE_API}/topology/nodes" || echo '[]')"
  LINKS="$(curl -sf "${BASE_API}/topology/links" || echo '[]')"

  echo "nodes: $(printf '%s' "$NODES" | jq -c .)"
  echo "links: $(printf '%s' "$LINKS" | jq -c .)"
  echo "hosts: $(printf '%s' "$HS" | jq -c .)"

  if (( CNT >= 2 )); then
    H1="$(printf '%s' "$HS" | jq -r '.[0].mac')"
    H2="$(printf '%s' "$HS" | jq -r '.[1].mac')"
    PATHS="$(curl -sf "${BASE_API}/paths?src_mac=${H1}&dst_mac=${H2}&k=${K}" || echo '[]')"
    echo "paths: $(printf '%s' "$PATHS" | jq -c .)"
    if [[ "$(printf '%s' "$PATHS" | jq 'length')" -ge 1 ]]; then
      ok "Graph healthy: ≥2 hosts and ≥1 path between ${H1} → ${H2}"
      break
    fi
  fi
  (( $(date +%s) - t0 > WAIT_GRAPH )) && die "Timed out waiting for graph to stabilize."
  sleep 2
done

# ---------- Step 3: Baseline ----------
note "3) Baseline run for ${DURATION}s"
export DURATION
# Determine output CSV filename by reading logger echo; capture and parse
BASELINE_LOG="$(mktemp)"
set +e
bash "${RUN_BASELINE}" | tee "${BASELINE_LOG}"
rc=$?
set -e
[[ $rc -ne 0 ]] && die "Baseline script failed (exit $rc)"
BASELINE_CSV="$(grep -Eo 'docs/baseline/ports_baseline_[0-9_]+\.csv' "${BASELINE_LOG}" | tail -n1 || true)"
[[ -z "${BASELINE_CSV}" ]] && die "Baseline CSV not found in output."
ok "Baseline CSV: ${BASELINE_CSV}"
peek_csv "${REPO}/${BASELINE_CSV}" "$(( DURATION / 2 ))"

# ---------- Step 4: RL ----------
note "4) RL run for ${DURATION}s (epsilon=${EPSILON}, k=${K})"
export EPSILON K DURATION
RL_LOG="$(mktemp)"
set +e
bash "${RUN_RL}" | tee "${RL_LOG}"
rc=$?
set -e
[[ $rc -ne 0 ]] && die "RL script failed (exit $rc)"
RL_CSV="$(grep -Eo 'docs/baseline/ports_rl_[0-9_]+\.csv' "${RL_LOG}" | tail -n1 || true)"
[[ -z "${RL_CSV}" ]] && die "RL CSV not found in output."
ok "RL CSV: ${RL_CSV}"
peek_csv "${REPO}/${RL_CSV}" "$(( DURATION / 2 ))"

# ---------- Step 5: Plot ----------
note "5) Plotting results"
python3 "${PLOT_PY}" \
  --files "${REPO}/${BASELINE_CSV}" "${REPO}/${RL_CSV}" \
  --labels Baseline RL

PLOT_DIR="${REPO}/docs/baseline/plots"
ok "Plots written under: ${PLOT_DIR}"

# ---------- Summary ----------
note "Summary"
echo "  Health API:            ${BASE_API}/health"
echo "  Baseline CSV:          ${BASELINE_CSV}"
echo "  RL CSV:                ${RL_CSV}"
echo "  Plots directory:       ${PLOT_DIR}"
ok "All done."
