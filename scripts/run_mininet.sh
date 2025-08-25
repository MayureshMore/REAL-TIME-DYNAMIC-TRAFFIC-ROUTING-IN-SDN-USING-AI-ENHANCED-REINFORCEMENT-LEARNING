#!/usr/bin/env bash
set -e
CTRL_IP=${1:-127.0.0.1}
sudo mn -c
sudo mn --controller=remote,ip=$CTRL_IP,port=6633 --topo=single,3 --switch ovsk,protocols=OpenFlow13
