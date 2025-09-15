#!/usr/bin/env bash
set -euo pipefail
# Smoke-test topology (single,2) pointing to our remote controller.
# Usage: ./scripts/run_mininet.sh <controller_ip>

CTRL_IP="${1:-127.0.0.1}"
sudo mn --clean || true
exec sudo mn --controller=remote,ip="$CTRL_IP",port=6633 \
  --switch ovs,protocols=OpenFlow13 \
  --topo single,2
