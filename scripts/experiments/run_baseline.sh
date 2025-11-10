#!/usr/bin/env bash
# Baseline (no RL): start controller, run two-path demo, log stats for DURATION seconds
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENSURE="${REPO}/scripts/ensure_controller.sh"
TOPO="${REPO}/scripts/topos/two_path.py"
LOGGER="${REPO}/scripts/metrics/log_stats.py"

DURATION="${DURATION:-120}"       # seconds
OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
CTRL_IP="${CTRL_IP:-127.0.0.1}"
API_BASE="http://${CTRL_IP}:${REST_PORT}/api/v1"

# 1) Controller
echo "Starting controller on OF:${OF_PORT} REST:${REST_PORT}"
WSAPI_PORT="${REST_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${REST_PORT}"

# 2) Topology (background) for slightly longer than we log
TOPO_SECS=$(( DURATION + 10 ))
echo "Launching two-path Mininet demo for ${TOPO_SECS}s"
sudo -E python3 "${TOPO}" --controller_ip "${CTRL_IP}" --no_cli --duration "${TOPO_SECS}" > /tmp/topo_baseline.out 2>&1 &
TOPO_PID=$!

# 3) Logger
TS="$(date +%Y%m%d_%H%M%S)"
OUT="${REPO}/docs/baseline/ports_baseline_${TS}.csv"
mkdir -p "$(dirname "${OUT}")"
echo "Logging to: ${OUT}"
python3 "${LOGGER}" --controller "${API_BASE}" --interval 1.0 --duration "${DURATION}" --out "${OUT}"

# 4) Cleanup topo
kill "${TOPO_PID}" 2>/dev/null || true

echo "Baseline complete. CSV: ${OUT}"
echo "Controller health:"; curl -s "${API_BASE}/health" | jq .
echo "CSV: ${OUT}"
