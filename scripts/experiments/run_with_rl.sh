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
AGENT_PID=""
TMUX_SOCKET="-L ryu"
TMUX_SESSION="ryu-app"

cleanup() {
  set +e
  [[ -n "${AGENT_PID}" ]] && kill "${AGENT_PID}" 2>/dev/null || true
  # try graceful, then force if needed
  sleep 0.2
  [[ -n "${AGENT_PID}" ]] && kill -9 "${AGENT_PID}" 2>/dev/null || true
  # stop any residual mininet
  sudo mn -c >/dev/null 2>&1 || true
  # leave controller running for post-run inspection? comment next line to keep it
  tmux ${TMUX_SOCKET} kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Ensure controller up
"${REPO}/scripts/ensure_controller.sh" "${OF_PORT}" "${REST_PORT}"

echo "Launching two-path Mininet demo for ${DURATION}s"
sudo -n true 2>/dev/null || sudo -v
sudo python3 "${REPO}/scripts/topos/two_path.py" \
  --controller_ip "${CTRL}" --rest_port "${REST_PORT}" \
  --demo --demo_time "$((DURATION-5))" --no_cli &

# Wait for any valid host pair with at least one path
BASE="http://127.0.0.1:${REST_PORT}/api/v1"
echo "Waiting up to ${WAIT_FOR_PATHS}s for hosts & paths..."
t0=$(date +%s); SRC=""; DST=""
while :; do
  HS_JSON="$(curl -sf "${BASE}/hosts" || echo '[]')"
  COUNT="$(printf '%s' "$HS_JSON" | jq -r 'length')"
  if [[ "$COUNT" -ge 2 ]]; then
    i=0
    while [[ $i -lt $COUNT ]]; do
      j=$((i+1))
      while [[ $j -lt $COUNT ]]; do
        SRC_CAND="$(printf '%s' "$HS_JSON" | jq -r ".[$i].mac")"
        DST_CAND="$(printf '%s' "$HS_JSON" | jq -r ".[$j].mac")"
        if curl -sf "${BASE}/paths?src_mac=${SRC_CAND}&dst_mac=${DST_CAND}&k=${K}" \
          | jq -e 'length >= 1' >/dev/null 2>&1; then
          SRC="$SRC_CAND"; DST="$DST_CAND"; break 2
        fi
        j=$((j+1))
      done
      i=$((i+1))
    done
  fi
  now=$(date +%s); (( now - t0 > WAIT_FOR_PATHS )) && { echo "Timed out waiting for paths"; exit 1; }
  sleep 2
done

echo "Starting bandit agent (epsilon=${EPSILON})"
"${PY_BIN}" "${REPO}/rl-agent/bandit_agent.py" \
  --controller "${CTRL}" --port "${REST_PORT}" --k "${K}" \
  --epsilon "${EPSILON}" --trials 100000 \
  --wait-hosts "${WAIT_FOR_PATHS}" --measure-wait 2.5 &
AGENT_PID=$!

TS=$(date +%Y%m%d_%H%M%S)
CSV="${REPO}/docs/baseline/ports_rl_${TS}.csv"
echo "Logging to: ${CSV}"
"${PY_BIN}" "${REPO}/scripts/metrics/log_stats.py" \
  --controller "${CTRL}" --port "${REST_PORT}" \
  --interval 1.0 --duration "${DURATION}" --out "${CSV}"

echo "RL run complete. CSV: ${CSV}"
# trap will cleanup agent & mininet (and controller unless you commented it)
