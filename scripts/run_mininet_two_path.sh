#!/usr/bin/env bash
set -euo pipefail
CTRL_IP="${1:-127.0.0.1}"
REST_PORT="${2:-8080}"
sudo python3 "$(dirname "$0")/topos/two_path.py" \
  --controller_ip "$CTRL_IP" --rest_port "$REST_PORT" --no_cli
