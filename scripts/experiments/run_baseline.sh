#!/usr/bin/env bash
# Baseline (no RL): start controller, run two-path demo, log stats for DURATION seconds
set -euo pipefail

DURATION="${DURATION:-600}"           # seconds; override with env var
CTRL_IP="127.0.0.1"
OF_PORT="${OF_PORT:-6633}"
REST_PORT="${REST_PORT:-8080}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RYU_BIN="$HOME/.pyenv/versions/ryu39/bin/ryu-manager"
PY_BIN="$HOME/.pyenv/versions/ryu39/bin/python"
LOG="$HOME/ryu-controller.log"
TMUX_SOCKET="-L ryu"
SESSION="ryu_run"

# --- Small self-heal: ensure health() returns bytes in WebOb<=1.8 ---
PATCH_APP="$REPO/controller-apps/sdn_router_rest.py"
"$PY_BIN" - "$PATCH_APP" <<'PY'
import io, re, sys, json
p=sys.argv[1]; s=io.open(p,'r',encoding='utf-8').read()
def fix(txt, name):
    pat=re.compile(rf'(@route\([^\n]+\)\s*def\s+{name}\([^\)]*\):\s*\n)(\s*)return Response\([^\n]*body\s*=\s*json\.dumps\([^\)]*\)\s*\)', re.S)
    return pat.sub(r"\1\2import json\n\2return Response(content_type='application/json', body=json.dumps({}) if False else json.dumps({}).encode('utf-8'))".format({},{}), txt)
s=fix(s,'health'); s=fix(s,'stats_ports'); s=fix(s,'stats_flows'); s=fix(s,'topo_nodes'); s=fix(s,'topo_links'); s=fix(s,'hosts'); s=fix(s,'paths'); s=fix(s,'route_action'); s=fix(s,'route_list'); s=fix(s,'route_delete'); s=fix(s,'link_metrics')
io.open(p,'w',encoding='utf-8').write(s)
print("Patched:", p)
PY

# --- Start/Restart controller in tmux ---
tmux ${TMUX_SOCKET} kill-session -t "${SESSION}" 2>/dev/null || true
echo "Starting controller on OF:${OF_PORT} REST:${REST_PORT}"
tmux ${TMUX_SOCKET} new -d -s "${SESSION}" \
  "cd '${REPO}' && exec '${RYU_BIN}' \
     controller-apps/sdn_router_rest.py ryu.topology.switches \
     --ofp-tcp-listen-port ${OF_PORT} --wsapi-port ${REST_PORT} >>'${LOG}' 2>&1"

# --- Wait for readiness: REST + OFP socket ---
echo "Waiting for controller health on :${REST_PORT} ..."
for i in {1..30}; do
  if curl -sf "http://127.0.0.1:${REST_PORT}/api/v1/health" >/dev/null; then break; fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "ERROR: REST didn’t come up"; exit 1; }
done
echo "Health:"; curl -s "http://127.0.0.1:${REST_PORT}/api/v1/health" | jq .

echo "Waiting for OFP port :${OF_PORT} to listen ..."
for i in {1..30}; do
  if ss -ltn | awk '{print $4}' | grep -q ":${OF_PORT}$"; then break; fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "ERROR: OFP port not listening"; exit 1; }
done

# --- Start Mininet two-path demo (no CLI), match DURATION ---
echo "Launching two-path Mininet demo for ${DURATION}s"
sudo -n true 2>/dev/null || true
sudo python3 "$REPO/scripts/topos/two_path.py" \
  --controller_ip "$CTRL_IP" --rest_port "$REST_PORT" \
  --demo --demo_time "$(( DURATION - 5 ))" --no_cli &

# --- Start logger for the full duration ---
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$REPO/docs/baseline/ports_baseline_${TS}.csv"
mkdir -p "$(dirname "$OUT")"
echo "Logging to: $OUT"
"$PY_BIN" "$REPO/scripts/metrics/log_stats.py" \
  --controller "$CTRL_IP" --port "$REST_PORT" \
  --interval 1.0 --duration "$DURATION" --out "$OUT"

echo "Baseline complete. CSV: $OUT"
echo "Controller log (tail): $LOG"
