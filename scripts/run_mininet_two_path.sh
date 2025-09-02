#!/usr/bin/env bash
set -euo pipefail


# Usage: ./scripts/run_mininet_two_path.sh [controller_ip]
CTRL_IP=${1:-127.0.0.1}


sudo mn -c || true
sudo python3 scripts/topos/two_path.py --controller_ip "$CTRL_IP" --controller_port 6633