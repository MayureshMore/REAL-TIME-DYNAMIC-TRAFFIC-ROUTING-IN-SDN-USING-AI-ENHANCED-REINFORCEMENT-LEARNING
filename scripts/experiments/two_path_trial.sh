#!/usr/bin/env bash
set -euo pipefail

CTRL_IP="${CTRL_IP:-127.0.0.1}"
REST_PORT="${REST_PORT:-8080}"
RUNS="${RUNS:-3}"
DUR="${DUR:-15}"

REPO="$HOME/REAL-TIME-DYNAMIC-TRAFFIC-ROUTING-IN-SDN-USING-AI-ENHANCED-REINFORCEMENT-LEARNING"
OUTROOT="$REPO/docs/baseline/runs"
TS="$(date +%Y%m%d_%H%M%S)"
PKG="$REPO/docs/baseline/runs_${TS}.tar.gz"

echo "Controller: $CTRL_IP:$REST_PORT | Runs: $RUNS | Duration: ${DUR}s"
sudo mn -c || true

# scenarios: tweak delay/loss asymmetrically
DELAYS=("2ms,10ms" "5ms,5ms" "0ms,15ms")
LOSSES=("0,0.1" "0,0" "0.05,0")

for i in $(seq 1 "$RUNS"); do
  di=$(( (i-1) % ${#DELAYS[@]} ))
  li=$(( (i-1) % ${#LOSSES[@]} ))
  DA="${DELAYS[$di]%,*}"; DB="${DELAYS[$di]#*,}"
  LA="${LOSSES[$li]%,*}"; LB="${LOSSES[$li]#*,}"

  echo "=== Run $i: delayA=$DA delayB=$DB lossA=$LA lossB=$LB ==="
  sudo python3 "$REPO/scripts/topos/two_path.py" \
    --controller_ip "$CTRL_IP" --rest_port "$REST_PORT" \
    --delay_a "$DA" --delay_b "$DB" --loss_a "$LA" --loss_b "$LB" \
    --demo --demo_time "$DUR" --no_cli
done

echo "Packaging artifacts..."
tar -czf "$PKG" -C "$OUTROOT" .
echo "Saved: $PKG"
