#!/usr/bin/env bash
set -euo pipefail
# Start the Ryu controller with our REST app.
# Usage:
#   ./scripts/run_ryu.sh [--ofp-port 6633] [--wsapi-port 8080]

OFP=6633
WSAPI=8080
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ofp-port)   OFP="$2"; shift 2 ;;
    --wsapi-port) WSAPI="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# repo root
cd "$(dirname "$0")/.."

# If you’re using pyenv/venv, activate it before exec’ing (optional):
# source ~/ryu-venv/bin/activate

exec ryu-manager controller-apps/monitor_rest.py ryu.topology.switches \
  --ofp-tcp-listen-port "$OFP" --wsapi-port "$WSAPI"
