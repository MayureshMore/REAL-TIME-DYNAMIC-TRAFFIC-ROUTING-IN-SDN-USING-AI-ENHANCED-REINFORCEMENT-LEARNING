#!/usr/bin/env bash
# Orchestrated demo: baseline stats then RL agent.
# Fixes:
# - Agent now honors Retry-After from controller (requires updated bandit_agent.py)
# - Logger writes header once and dedups rows (updated log_ports.py)
# - Ensures consistent src/dst MACs and k=2

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
REST_BASE="http://127.0.0.1:${REST_PORT}/api/v1"

SRC_MAC="${SRC_MAC:-00:00:00:00:00:01}"
DST_MAC="${DST_MAC:-00:00:00:00:00:02}"
K_PATHS="${K_PATHS:-2}"
EPSILON="${EPSILON:-0.20}"
STEPS="${STEPS:-120}"

BASELINE_DUR="${BASELINE_DUR:-120}"
RL_DUR="${RL_DUR:-120}"

ensure_controller="${ROOT}/scripts/ensure_controller.sh"
logger_py="${ROOT}/scripts/log_ports.py"
agent_py="${ROOT}/rl-agent/bandit_agent.py"
topo_py="${ROOT}/topos/two_path_demo.py"

clean() {
  sudo pkill -9 -f mininet: 2>/dev/null || true
  sudo mn -c >/dev/null 2>&1 || true
  pkill -9 -f ryu-manager 2>/dev/null || true
  sleep 1
}

echo
echo "==> 0) Clean start"
sudo -v
clean || true
echo "  Cleanup complete."

echo
echo "==> 1) Starting controller (OF ${OF_PORT}, REST ${REST_PORT})"
bash "$ensure_controller" OF_PORT="${OF_PORT}" REST_PORT="${REST_PORT}"

echo
echo "==> 2) Sanity topology up for ~15s"
# Launch two-path topology in background (idempotent)
python3 "$topo_py" --ofp "${OF_PORT}" --hold 315 \
  > /tmp/mininet_demo.out 2>&1 &

# Wait for graph readiness (hosts + paths)
echo "  Waiting for graph (hosts & k-paths)..."
for i in {1..60}; do
  HJSON="$(curl -sf "${REST_BASE}/hosts" || echo "[]")"
  PJSON="$(curl -sf "${REST_BASE}/paths?src_mac=${SRC_MAC}&dst_mac=${DST_MAC}&k=${K_PATHS}" || echo "[]")"
  if [[ "$HJSON" != "[]" && "$PJSON" != "[]" ]]; then
    nodes="$(curl -sf "${REST_BASE}/graph" | jq -c '.nodes')"
    links="$(curl -sf "${REST_BASE}/graph" | jq -c '.links')"
    hosts="$(echo "$HJSON" | jq -c '.')"
    paths="$(echo "$PJSON" | jq -c '.')"
    echo "  nodes: ${nodes}"
    echo "  links: ${links}"
    echo "  hosts: ${hosts}"
    echo "  paths: ${paths}"
    echo "  Graph healthy: ≥2 hosts and ≥1 path between ${SRC_MAC} → ${DST_MAC}"
    break
  fi
  sleep 1
done

ts="$(date +%Y%m%d_%H%M%S)"
BASE_CSV="${ROOT}/docs/baseline/ports_baseline_${ts}.csv"
RL_CSV="${ROOT}/docs/baseline/ports_rl_${ts}.csv"
mkdir -p "${ROOT}/docs/baseline"

echo
echo "==> 3) Baseline run for ${BASELINE_DUR}s"
echo "  Baseline CSV: ${BASE_CSV}"
python3 "$logger_py" --base "${REST_BASE}" --out "${BASE_CSV}" --duration "${BASELINE_DUR}" --every 1 \
  > /tmp/logger.out 2>&1 &
LOGGER_PID=$!

echo
echo "==> 4) RL run for ${RL_DUR}s (epsilon=${EPSILON}, k=${K_PATHS})"
# Warmup ping flow to make sure hosts learned
echo "  [warmup] Pinging between hosts in Mininet..."
mn -c >/dev/null 2>&1 || true
# Let topo script keep the hosts running; send a few pings via its CLI if available
sleep 2

python3 "$agent_py" \
  --base "${REST_BASE}" \
  --src "${DST_MAC}" \
  --dst "${SRC_MAC}" \
  --k "${K_PATHS}" \
  --epsilon "${EPSILON}" \
  --steps "${RL_DUR}" \
  --sleep 1 \
  > /tmp/agent.out 2>&1 &

# Simultaneous logging during RL
python3 "$logger_py" --base "${REST_BASE}" --out "${RL_CSV}" --duration "${RL_DUR}" --every 1 \
  >> /tmp/logger.out 2>&1 &

wait

echo
echo "Paths written:"
echo "  ${BASE_CSV}"
echo "  ${RL_CSV}"
