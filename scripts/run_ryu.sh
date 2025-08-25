#!/usr/bin/env bash
set -e
source ~/ryu-venv/bin/activate
cd ~/project/controller-apps
ryu-manager controller-apps/simple_switch_13_stats.py --ofp-tcp-listen-port 6633
