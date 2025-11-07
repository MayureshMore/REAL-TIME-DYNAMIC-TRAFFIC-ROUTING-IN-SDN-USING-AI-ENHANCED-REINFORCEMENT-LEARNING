#!/usr/bin/env python3
"""
Two-path Mininet demo topology:
 h1 - s1 == s2 - h2
       \- s3 -/

Fixes
- Ensures deterministic host MACs 00:..:01 and 00:..:02
- Keeps net up for --hold seconds so external scripts can reuse
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, Controller, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

import argparse
import time

class TwoPathTopo(Topo):
    def build(self):
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')

        h1 = self.addHost('h1', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', mac='00:00:00:00:00:02')

        self.addLink(h1, s1)
        self.addLink(h2, s2)

        # Direct s1-s2
        self.addLink(s1, s2)
        # Alternate via s3
        self.addLink(s1, s3)
        self.addLink(s3, s2)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ofp", type=int, default=6633)
    ap.add_argument("--hold", type=int, default=300, help="seconds to keep net up")
    args = ap.parse_args()

    topo = TwoPathTopo()
    net = Mininet(topo=topo, switch=OVSSwitch, controller=None, link=TCLink, build=True, autoSetMacs=True)
    rc = RemoteController("c0", ip="127.0.0.1", port=args.ofp)
    net.addController(rc)
    net.start()

    info("*** Sanity pings\n")
    net.pingAll(timeout=1)

    info("*** Holding topology for {}s\n".format(args.hold))
    t_end = time.time() + args.hold
    while time.time() < t_end:
        time.sleep(1)

    net.stop()

if __name__ == "__main__":
    setLogLevel("info")
    main()
