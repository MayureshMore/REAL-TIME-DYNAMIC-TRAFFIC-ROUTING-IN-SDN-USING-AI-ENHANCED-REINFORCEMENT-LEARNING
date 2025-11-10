#!/usr/bin/env bash
set -euo pipefail
# Batch-run two_path with varied runtime; KEEP FLAGS COMPATIBLE WITH two_path.py

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOPO="${REPO}/scripts/topos/two_path.py"
ENSURE="${REPO}/scripts/ensure_controller.sh"

CTRL_IP="${CTRL_IP:-127.0.0.1}"
REST_PORT="${REST_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"
RUNS="${RUNS:-3}"
DUR="${DUR:-15}"

API_BASE="http://${CTRL_IP}:${REST_PORT}/api/v1"
OUTROOT="${REPO}/docs/baseline/runs"
TS="$(date +%Y%m%d_%H%M%S)"
PKG="${REPO}/docs/baseline/runs_${TS}.tar.gz"

mkdir -p "${OUTROOT}"

echo "Controller: ${CTRL_IP}:${REST_PORT} | Runs: ${RUNS} | Duration: ${DUR}s"

# Ensure controller is up once
WSAPI_PORT="${REST_PORT}" OF_PORT="${OF_PORT}" "${ENSURE}" "${OF_PORT}" "${REST_PORT}" >/dev/null

sudo mn -c || true

for i in $(seq 1 "${RUNS}"); do
  echo "=== Run ${i}/${RUNS} ==="
  # Launch topo for slightly longer than DUR to cover logger window
  sudo -E python3 "${TOPO}" --controller_ip "${CTRL_IP}" --no_cli --duration "$(( DUR + 5 ))" > "/tmp/two_path_run_${i}.out" 2>&1 &
  TP=$!

  # quick per-run log capture
  OUT="${OUTROOT}/ports_run_${i}_${TS}.csv"
  python3 "${REPO}/scripts/metrics/log_stats.py" \
    --controller "${API_BASE}" --interval 1.0 --duration "${DUR}" --out "${OUT}"

  kill "${TP}" 2>/dev/null || true
done

echo "Packaging artifacts..."
tar -czf "${PKG}" -C "${OUTROOT}" .
echo "Saved: ${PKG}"
