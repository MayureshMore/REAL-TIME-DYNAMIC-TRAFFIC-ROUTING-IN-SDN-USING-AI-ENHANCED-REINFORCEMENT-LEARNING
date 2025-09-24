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

# Wait for EDGE hosts + valid paths before agent & logger
BASE="http://127.0.0.1:${REST_PORT}/api/v1"
echo "Waiting up to ${WAIT_FOR_PATHS}s for hosts & paths..."
t0=$(date +%s)
SRC=""; DST=""

while :; do
  HOSTS="$(curl -sf "${BASE}/hosts" || echo '[]')"
  LINKS="$(curl -sf "${BASE}/topology/links" || echo '[]')"

  # Build set of core (dpid:port)
  CORE_JSON=$(jq -c '[ .[] | "\(.src_dpid):\(.src_port)", "\(.dst_dpid):\(.dst_port)" ] | unique' <<< "${LINKS}")
  # Filter to edge hosts only, deterministic order, prefer 00:* style
  EDGE="$(jq -c --argjson core "${CORE_JSON}" '
      [ .[]
        | select(((("\(.dpid):\(.port)") as $k | ($core | index($k))) | not))
      ]
      | sort_by((.mac|startswith("00")|not), .dpid, .port, .mac)
    ' <<< "${HOSTS}")"

  CNT="$(jq 'length' <<< "${EDGE}")"
  if [[ "${CNT}" -ge 2 ]]; then
    SRC="$(jq -r '.[0].mac' <<< "${EDGE}")"
    DST="$(jq -r '.[1].mac' <<< "${EDGE}")"
    # Validate at least one path with dpids length >= 2
    if curl -sf "${BASE}/paths?src_mac=${SRC}&dst_mac=${DST}&k=${K}" \
      | jq -e '[.[] | select(.dpids|length>=2)] | length >= 1' >/dev/null; then
      break
    fi
  fi

  now=$(date +%s)
  (( now - t0 > WAIT_FOR_PATHS )) && { echo "Timed out waiting for paths"; break; }
  sleep 2
done

echo "Starting bandit agent (epsilon=${EPSILON})"
"${PY_BIN}" "$REPO/rl-agent/bandit_agent.py" \
  --controller "$CTRL" --port "$REST_PORT" --k "$K" \
  --epsilon "$EPSILON" --trials 100000 \
  --wait-hosts "$WAIT_FOR_PATHS" --wait-paths "$WAIT_FOR_PATHS" &

# Start logger only after health is OK
TS=$(date +%Y%m%d_%H%M%S)
CSV="$REPO/docs/baseline/ports_rl_${TS}.csv"
echo "Logging to: ${CSV}"
"${PY_BIN}" "$REPO/scripts/metrics/log_stats.py" \
  --controller "$CTRL" --port "$REST_PORT" --interval 1.0 --duration "$DURATION" --out "$CSV"

echo "RL run complete. CSV: ${CSV}"
