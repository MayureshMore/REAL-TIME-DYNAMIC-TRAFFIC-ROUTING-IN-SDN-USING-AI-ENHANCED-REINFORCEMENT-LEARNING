#!/usr/bin/env python3
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import argparse, time, subprocess, json, sys

def build_two_path(ctrl_ip, ctrl_port, no_cli=False, demo=False, demo_time=20):
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True, autoStaticArp=True)
    c0 = net.addController('c0', controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')  # alternate path
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')

    net.addLink(h1, s1)
    net.addLink(h2, s2)
    # Path A: s1 <-> s2 (direct)
    net.addLink(s1, s2, bw=10, delay='10ms')
    # Path B: s1 -> s3 -> s2
    net.addLink(s1, s3, bw=10, delay='15ms')
    net.addLink(s3, s2, bw=10, delay='15ms')

    net.start()
    info('*** Network started with two disjoint paths\n')
    if demo:
        info('*** Running ping flood demo for %ds\n' % demo_time)
        h1.cmdPrint(f'ping -c {demo_time} 10.0.0.2 &')
        time.sleep(demo_time + 1)

    if no_cli:
        result = net.pingAll()
        info(f'*** pingAll result: {result}\n')
        net.stop()
        return

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller_ip', default='127.0.0.1')
    ap.add_argument('--rest_port', type=int, default=8080)
    ap.add_argument('--no_cli', action='store_true')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--demo_time', type=int, default=20)
    args = ap.parse_args()
    build_two_path(args.controller_ip, 6633, no_cli=args.no_cli, demo=args.demo, demo_time=args.demo_time)
