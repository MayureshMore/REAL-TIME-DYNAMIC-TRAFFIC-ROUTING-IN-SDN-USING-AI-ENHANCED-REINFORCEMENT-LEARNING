#!/usr/bin/env bash
set -euo pipefail
# Two-path topology runner that uses the parametric Mininet script.
# Usage: ./scripts/run_mininet_two_path.sh <controller_ip> [rest_port]

CTRL_IP="${1:-127.0.0.1}"
REST_PORT="${2:-8080}"

cd "$(dirname "$0")/.."
sudo mn -c || true
exec sudo python3 scripts/topos/two_path.py \
  --controller_ip "$CTRL_IP" \
  --rest_port "$REST_PORT"
