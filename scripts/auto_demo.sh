#!/usr/bin/env bash
set -Eeuo pipefail

OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
CTRL_IP="${CTRL_IP:-127.0.0.1}"
BASE_API="http://127.0.0.1:${REST_PORT}/api/v1"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENSURE_CTRL="${REPO}/scripts/ensure_controller.sh"
TOPO_PY="${REPO}/scripts/topos/two_path.py"
RUN_BASELINE="${REPO}/scripts/experiments/run_baseline.sh"
RUN_RL="${REPO}/scripts/experiments/run_with_rl.sh"

wait_for_csv_rows() {
  local file="$1" timeout="${2:-60}" need="${3:-2}"
  local start=$(date +%s)
  while :; do
    if [[ -s "$file" ]]; then
      local rows
      rows=$(wc -l < "$file" | tr -d ' ')
      if (( rows >= need )); then
        return 0
      fi
    fi
    (( $(date +%s) - start > timeout )) && return 1
    sleep 2
  done
}

echo -e "\nPatched run_with_rl.sh to use scripts/ensure_controller.sh"
sed -i 's#scripts/_ensure_controller.sh#scripts/ensure_controller.sh#g' "${RUN_RL}" 2>/dev/null || true

echo -e "\n0) Clean start"
sudo mn -c >/dev/null 2>&1 || true
tmux -L ryu kill-session -t ryu-app 2>/dev/null || true
# Thorough cleanup like mn -c does:
pkill -9 -f "sudo mnexec" 2>/dev/null || true
sudo pkill -9 -f mininet: 2>/dev/null || true
sudo ip link show | egrep -o '([-_.[:alnum:]]+-eth[[:digit:]]+)' | xargs -r -n1 sudo ip link del 2>/dev/null || true

echo -e "\n1) Starting controller (OF ${OF_PORT}, REST ${REST_PORT})"
"${ENSURE_CTRL}" "${OF_PORT}" "${REST_PORT}"
echo "Controller healthy and listening."

echo -e "\n2) Sanity topology up for ~15s"
echo -e "\nWaiting for graph (hosts & k-paths)..."
# Fire a short demo (15s) to bootstrap LLDP + host learning
sudo python3 "${TOPO_PY}" --controller_ip "${CTRL_IP}" --rest_port "${REST_PORT}" --demo --demo_time 15 --no_cli >/tmp/sanity_topo.out 2>&1 &

# Poll the API while sanity demo runs
for _ in $(seq 1 15); do
  NODES="$(curl -sf "${BASE_API}/topology/nodes" || echo '[]')"
  LINKS="$(curl -sf "${BASE_API}/topology/links" || echo '[]')"
  HOSTS="$(curl -sf "${BASE_API}/hosts" || echo '[]')"
  echo "nodes: $(echo "$NODES" | jq 'sort')"
  echo "links: $(echo "$LINKS" | jq -c '.')"
  echo "hosts: $(echo "$HOSTS" | jq -c '.')"
  H1=$(echo "$HOSTS" | jq -r '.[0].mac? // empty')
  H2=$(echo "$HOSTS" | jq -r '.[1].mac? // empty')
  if [[ -n "$H1" && -n "$H2" ]]; then
    PATHS="$(curl -sf "${BASE_API}/paths?src_mac=${H1}&dst_mac=${H2}&k=2" || echo '[]')"
    if [[ "$(echo "$PATHS" | jq 'length')" -ge 1 ]]; then
      echo "paths: $(echo "$PATHS" | jq -c '.')"
      echo "Graph healthy: ≥2 hosts and ≥1 path between ${H1} → ${H2}"
      break
    fi
  fi
  sleep 1
done

# Ensure the 15s sanity demo is over, then hard-clean to avoid RTNETLINK issues:
wait || true
sudo mn -c >/dev/null 2>&1 || true

echo -e "\n3) Baseline run for 120s"
# baseline script now benefits from a clean slate
BASELINE_CSV=$(
  DURATION=120 bash "${RUN_BASELINE}" | awk '/CSV:/{print $NF}' | tail -n1
)
# Fallback if parser missed it
if [[ -z "${BASELINE_CSV}" ]]; then
  BASELINE_CSV=$(ls -1t "${REPO}/docs/baseline/ports_baseline_"*.csv 2>/dev/null | head -n1 || true)
fi
echo "Baseline CSV: ${BASELINE_CSV:-<not found>}"

echo -e "\nLive peek: ${BASELINE_CSV} (every 10s for 60s)"
if [[ -n "${BASELINE_CSV}" ]]; then
  if ! wait_for_csv_rows "${BASELINE_CSV}" 60 2; then
    echo "[warn] Baseline CSV had no rows after 60s; showing controller/log outputs..."
    tail -n +1 /tmp/logger.out /tmp/topo.out 2>/dev/null || true
  else
    for _ in {1..6}; do
      tail -n 6 "${BASELINE_CSV}" || true
      sleep 10
    done
  fi
fi

echo -e "\n4) RL run for 120s (epsilon=0.2, k=2)"
# Clean again before RL to avoid any stale links
echo "[prep] sudo mn -c"
sudo mn -c >/dev/null 2>&1 || true

echo "[ctrl] Starting controller OF:${OF_PORT} REST:${REST_PORT}"
"${ENSURE_CTRL}" "${OF_PORT}" "${REST_PORT}" >/dev/null

# Run RL with robust waits; its script writes CSV path to stdout (last line)
RL_CSV=$(
  PATH_WAIT_SECS=120 WARMUP_PINGS=10 DURATION=120 EPSILON=0.2 K=2 \
  bash "${RUN_RL}" | tail -n1
)
echo "RL CSV: ${RL_CSV:-<not found>}"

echo -e "\nLive peek: ${RL_CSV} (every 10s for 60s)"
if [[ -n "${RL_CSV}" ]]; then
  if ! wait_for_csv_rows "${RL_CSV}" 60 2; then
    echo "(waiting for first samples...)"
    tail -n +1 /tmp/logger.out /tmp/agent.out /tmp/topo.out 2>/dev/null || true
  else
    for _ in {1..6}; do
      tail -n 6 "${RL_CSV}" || true
      sleep 10
    done
  fi
fi

echo -e "\n5) Plotting results"
# Friendly check for pandas; don't auto-install, just nudge
python3 - <<'PY' || { echo "Tip: pip install pandas"; exit 1; }
try:
    import pandas, matplotlib
    print("ok")
except Exception as e:
    raise
PY

python3 "${REPO}/scripts/metrics/plot_results.py" \
  --files "${REPO}/docs/baseline/ports_baseline_"*.csv "${REPO}/docs/baseline/ports_rl_"*.csv \
  --labels Baseline RL || {
    echo "[warn] Plot failed; check dependencies and CSV content."
  }

echo -e "\nDone."
