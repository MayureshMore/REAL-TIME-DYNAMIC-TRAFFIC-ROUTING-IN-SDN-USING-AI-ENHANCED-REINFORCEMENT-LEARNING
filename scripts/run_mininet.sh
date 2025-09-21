#!/usr/bin/env bash
set -euo pipefail
CTRL_IP="${1:-127.0.0.1}"
sudo mn --controller=remote,ip="$CTRL_IP",port=6633 \
  --switch ovsk,protocols=OpenFlow13 --topo single,2
