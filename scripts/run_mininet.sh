#!/usr/bin/env bash
set -euo pipefail


# Usage: ./scripts/run_mininet.sh [controller_ip]
CTRL_IP=${1:-127.0.0.1}


sudo mn -c || true
sudo mn --controller=remote,ip="$CTRL_IP",port=6633 \
--topo=single,3 --switch ovsk,protocols=OpenFlow13