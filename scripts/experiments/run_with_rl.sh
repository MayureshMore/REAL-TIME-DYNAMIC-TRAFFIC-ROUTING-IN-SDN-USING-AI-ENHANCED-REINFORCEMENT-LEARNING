#!/usr/bin/env bash
set -euo pipefail

OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
CTRL="127.0.0.1"
DURATION="${DURATION:-600}"
EPSILON="${EPSILON:-0.2}"
K="${K:-2}"
WAIT_FOR_PATHS="${WAIT_FOR_PATHS:-60}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYENV_ROOT="${HOME}/.pyenv"
PY_BIN="${PYENV_ROOT}/versions/ryu39/bin/python"

# Bring controller up and healthy
"$REPO/scripts/_ensure_controller.sh" "${OF_PORT}" "${REST_PORT}"

echo "Launching two-path Mininet demo for ${DURATION}s"
sudo -n true 2>/dev/null || sudo -v
sudo python3 "$REPO/scripts/topos/two_path.py" \
  --controller_ip "$CTRL" --rest_port "$REST_PORT" \
  --demo --demo_time "$((DURATION-5))" --no_cli &

# Wait for links, hosts, and at least 1 candidate path before starting agent
BASE="http://127.0.0.1:${REST_PORT}/api/v1"
echo "Waiting up to ${WAIT_FOR_PATHS}s for hosts & paths..."
t0=$(date +%s)
while :; do
  # 1) Topology links discovered?
  LINKS_JSON="$(curl -sf "${BASE}/topology/links" || echo '[]')"
  if ! jq -e 'length >= 1' >/dev/null 2>&1 <<<"${LINKS_JSON}"; then
    sleep 2
    [[ $(( $(date +%s) - t0 )) -gt ${WAIT_FOR_PATHS} ]] && { echo "Timed out waiting for paths"; break; }
    continue
  fi
  # 2) Two hosts learned?
  HOSTS_JSON="$(curl -sf "${BASE}/hosts" || echo '[]')"
  if ! jq -e 'length >= 2' >/dev/null 2>&1 <<<"${HOSTS_JSON}"; then
    sleep 2
    [[ $(( $(date +%s) - t0 )) -gt ${WAIT_FOR_PATHS} ]] && { echo "Timed out waiting for paths"; break; }
    continue
  fi
  SRC=$(jq -r '.[0].mac' <<<"${HOSTS_JSON}")
  DST=$(jq -r '.[1].mac' <<<"${HOSTS_JSON}")
  # 3) At least one multi-hop path available?
  if curl -sf "${BASE}/paths?src_mac=${SRC}&dst_mac=${DST}&k=${K}" | jq -e 'length >= 1' >/dev/null 2>&1; then
    break
  fi
  sleep 2
  [[ $(( $(date +%s) - t0 )) -gt ${WAIT_FOR_PATHS} ]] && { echo "Timed out waiting for paths"; break; }
done

echo "Starting bandit agent (epsilon=${EPSILON})"
"${PY_BIN}" "$REPO/rl-agent/bandit_agent.py" \
  --controller "$CTRL" --port "$REST_PORT" --k "$K" \
  --epsilon "$EPSILON" --trials 100000 \
  --wait-hosts "$WAIT_FOR_PATHS" --wait-paths "$WAIT_FOR_PATHS" &

# Start logger for the full duration
TS=$(date +%Y%m%d_%H%M%S)
CSV="$REPO/docs/baseline/ports_rl_${TS}.csv"
echo "Logging to: ${CSV}"
"${PY_BIN}" "$REPO/scripts/metrics/log_stats.py" \
  --controller "$CTRL" --port "$REST_PORT" --interval 1.0 --duration "$DURATION" --out "$CSV"

echo "RL run complete. CSV: ${CSV}"
