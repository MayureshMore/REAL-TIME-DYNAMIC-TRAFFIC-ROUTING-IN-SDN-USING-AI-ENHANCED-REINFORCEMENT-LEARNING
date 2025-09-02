#!/usr/bin/env bash
set -euo pipefail


# Example usage inside Mininet CLI (manual):
# h1 iperf3 -s &
# h2 iperf3 -c $(h1 IP) -t 10
# Outside Mininet, this script is a placeholder (documented workflow)


echo "Run inside Mininet CLI:"
cat <<'INSTRUCTIONS'
mininet> h1 iperf3 -s &
mininet> h2 iperf3 -c `h1 IP` -t 10
INSTRUCTIONS