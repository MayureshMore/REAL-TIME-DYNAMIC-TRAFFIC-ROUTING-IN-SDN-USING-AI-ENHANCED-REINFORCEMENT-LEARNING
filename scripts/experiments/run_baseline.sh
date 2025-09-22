#!/usr/bin/env bash
# Baseline (no RL): spins up controller + two-path topo, logs stats to CSV, and exits.

set -euo pipefail

# ---- Tunables (env overrides OK) ----
DURATION="${DURATION:-45}"           # seconds to capture stats
REST_PORT="${REST_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"
K_PATHS="${K_PATHS:-2}"              # only used for sanity checks
SESSION="ryu_run"
TMUX_SOCKET="-L ryu"

# ---- Paths ----
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="${REPO}/controller-apps/sdn_router_rest.py"
TOPOS="${REPO}/scripts/topos/two_path.py"
LOGGER="${REPO}/scripts/metrics/log_stats.py"
RYU_BIN="$HOME/.pyenv/versions/ryu39/bin/ryu-manager"
PY_BIN="$HOME/.pyenv/versions/ryu39/bin/python"
PIP_BIN="$HOME/.pyenv/versions/ryu39/bin/pip"

ts() { date +%Y%m%d_%H%M%S; }

require() { command -v "$1" >/dev/null || { echo "Missing: $1" >&2; exit 1; }; }

# ---- Pre-flight ----
require tmux; require curl; require jq; require sudo
[[ -x "$RYU_BIN" ]] || { echo "Ryu venv not found at $RYU_BIN (run ./setup-vm.sh)"; exit 1; }

mkdir -p "${REPO}/docs/baseline"

# ---- Patch sdn_router_rest.py to always return bytes ----
"$PY_BIN" - <<'PY' "${APP}"
import io, sys, re
p = sys.argv[1]
src = io.open(p,'r',encoding='utf-8').read()
def enc_json(m): return m.group(0).replace("json.dumps(", "json.dumps(").replace("))", ").encode('utf-8'))")
src = re.sub(r"Response\(content_type='application/json',\s*body=json.dumps\((.*?)\)\)",
             r"Response(content_type='application/json', body=json.dumps(\1).encode('utf-8'))", src)
src = re.sub(r'Response\(content_type="application/json",\s*body=json.dumps\((.*?)\)\)',
             r'Response(content_type="application/json", body=json.dumps(\1).encode("utf-8"))', src)
src = re.sub(r"Response\(content_type='application/yaml',\s*body=(.*?)\)",
             r"Response(content_type='application/yaml', body=(\1).encode('utf-8'))", src)
io.open(p,'w',encoding='utf-8').write(src)
print("Patched:", p)
PY

# ---- Start controller in tmux ----
tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true
LOG="$HOME/ryu-baseline.log"
tmux ${TMUX_SOCKET} new -d -s "${SESSION}" \
  "cd '${REPO}' && exec '${RYU_BIN}' '${APP}' ryu.topology.switches \
     --ofp-tcp-listen-port ${OF_PORT} --wsapi-port ${REST_PORT} >>'${LOG}' 2>&1"

# ---- Health check ----
echo "Waiting for controller health on :${REST_PORT} ..."
for i in {1..30}; do
  curl -sf "http://127.0.0.1:${REST_PORT}/api/v1/health" | jq -e '.status=="ok"' >/dev/null && break
  sleep 1
  [[ $i -eq 30 ]] && { echo "Controller failed to become healthy"; tmux ${TMUX_SOCKET} capture-pane -pt "${SESSION}"; exit 1; }
done
curl -s "http://127.0.0.1:${REST_PORT}/api/v1/health" | jq .

# ---- Start logging ----
OUT_CSV="${REPO}/docs/baseline/ports_baseline_$(ts).csv"
echo "Logging to: ${OUT_CSV}"
"$PY_BIN" "${LOGGER}" --controller 127.0.0.1 --port "${REST_PORT}" \
  --interval 1.0 --duration "${DURATION}" --out "${OUT_CSV}" & LOGGER_PID=$!

# ---- Run the two-path topology with demo traffic ----
sudo mn -c >/dev/null 2>&1 || true
sudo python3 "${TOPOS}" --controller_ip 127.0.0.1 --demo --demo_time $(( DURATION - 5 )) --no_cli

# ---- Stop logger and controller ----
kill "${LOGGER_PID}" 2>/dev/null || true
wait "${LOGGER_PID}" 2>/dev/null || true

tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true

echo
echo "✅ Baseline complete."
echo "CSV: ${OUT_CSV}"
