#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/run_ryu.sh [--port 6633]
PORT=6633
if [[ "${1:-}" == "--port" ]]; then
  PORT=${2:?port required}
fi

# Activate venv if present
if [[ -d "$HOME/ryu-venv" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/ryu-venv/bin/activate"
fi

cd "$(dirname "$0")/.."  # repo root
exec ryu-manager controller-apps/sdn_router_rest.py ryu.topology.switches --ofp-tcp-listen-port "$PORT"
