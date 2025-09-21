#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Allow overrides via env: WSAPI_PORT, OF_PORT
WSAPI_PORT="${WSAPI_PORT:-8080}"
OF_PORT="${OF_PORT:-6633}"

# Locate ryu-manager (pyenv first, else PATH)
RYU_BIN="${HOME}/.pyenv/versions/ryu39/bin/ryu-manager"
if [[ ! -x "$RYU_BIN" ]]; then
  RYU_BIN="$(command -v ryu-manager || true)"
fi
if [[ -z "${RYU_BIN}" ]]; then
  echo "ryu-manager not found. Install Ryu or run ./setup-vm.sh." >&2
  exit 1
fi

exec "$RYU_BIN" --verbose \
  "$REPO/controller-apps/monitor_rest.py" ryu.app.simple_switch_13 ryu.topology.switches \
  --ofp-tcp-listen-port "${OF_PORT}" --wsapi-port "${WSAPI_PORT}"
