#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONUNBUFFERED=1
exec "$HOME/.pyenv/versions/ryu39/bin/ryu-manager" --verbose \
  "$REPO/controller-apps/monitor_rest.py" ryu.topology.switches \
  --ofp-tcp-listen-port 6633 --wsapi-port 8080
