#!/usr/bin/env python3
# Enhanced Two-Path Mininet Topology for SDN RL Experiments
# ----------------------------------------------------------
# Features:
# - Configurable delay, loss, and bandwidth for both paths
# - Optional demo mode (ping flood)
# - Clean start/stop handling and reproducible structure
# - Outputs JSON summary for logging and reproducibility

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import argparse, time, json, os, sys

def build_two_path(ctrl_ip, ctrl_port, args):
    """Constructs two disjoint paths with configurable parameters"""
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink,
                  autoSetMacs=True, autoStaticArp=True)

    c0 = net.addController('c0', controller=RemoteController, ip=ctrl_ip, port=ctrl_port)

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')

    # Host links
    net.addLink(h1, s1, bw=args.bw, delay='1ms', loss=0)
    net.addLink(h2, s2, bw=args.bw, delay='1ms', loss=0)

    # Path A: direct s1 <-> s2
    net.addLink(s1, s2, bw=args.bw, delay=args.delay_a, loss=args.loss_a)

    # Path B: via s3
    net.addLink(s1, s3, bw=args.bw, delay=args.delay_b1, loss=args.loss_b1)
    net.addLink(s3, s2, bw=args.bw, delay=args.delay_b2, loss=args.loss_b2)

    info("\n*** Starting Mininet\n")
    net.start()

    info(f"*** Controller: {ctrl_ip}:{ctrl_port}\n")
    info(f"*** Path A: s1-s2 | delay={args.delay_a} loss={args.loss_a}% bw={args.bw}Mbps\n")
    info(f"*** Path B: s1-s3-s2 | delays={args.delay_b1}+{args.delay_b2}, losses={args.loss_b1}%+{args.loss_b2}%\n")
    info(f"*** Hosts: {h1.IP()} <-> {h2.IP()}\n")

    # Output config JSON (for reproducibility)
    os.makedirs("docs/baseline", exist_ok=True)
    config_path = f"docs/baseline/topo_config_{int(time.time())}.json"
    cfg = {
        "controller": f"{ctrl_ip}:{ctrl_port}",
        "bw": args.bw,
        "pathA": {"delay": args.delay_a, "loss": args.loss_a},
        "pathB": {"delay1": args.delay_b1, "delay2": args.delay_b2,
                  "loss1": args.loss_b1, "loss2": args.loss_b2}
    }
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)
    info(f"*** Configuration saved: {config_path}\n")

    # Demo mode (optional)
    if args.demo:
        info(f"*** Running demo ping flood for {args.demo_time}s\n")
        h1.cmdPrint(f"ping -c {args.demo_time} {h2.IP()} &")
        time.sleep(args.demo_time)
        info("*** Demo completed.\n")

    if args.no_cli:
        info("*** Running pingAll (no CLI mode)\n")
        result = net.pingAll()
        info(f"*** PingAll result: {result}\n")
        net.stop()
        return

    CLI(net)
    net.stop()

def main():
    setLogLevel("info")
    ap = argparse.ArgumentParser(description="Two-path SDN topology for RL experiments")

    ap.add_argument("--controller_ip", default="127.0.0.1", help="Controller IP")
    ap.add_argument("--rest_port", type=int, default=8080, help="REST API port")
    ap.add_argument("--bw", type=float, default=10.0, help="Bandwidth in Mbps")

    # Path A parameters
    ap.add_argument("--delay_a", default="10ms", help="Delay for path A (s1-s2)")
    ap.add_argument("--loss_a", type=float, default=0.0, help="Loss rate for path A (%)")

    # Path B parameters (s1-s3-s2)
    ap.add_argument("--delay_b1", default="15ms", help="Delay from s1->s3")
    ap.add_argument("--delay_b2", default="15ms", help="Delay from s3->s2")
    ap.add_argument("--loss_b1", type=float, default=0.0, help="Loss from s1->s3 (%)")
    ap.add_argument("--loss_b2", type=float, default=0.0, help="Loss from s3->s2 (%)")

    ap.add_argument("--no_cli", action="store_true", help="Exit after pingAll (no interactive CLI)")
    ap.add_argument("--demo", action="store_true", help="Run demo ping flood")
    ap.add_argument("--demo_time", type=int, default=20, help="Seconds for demo")

    args = ap.parse_args()

    try:
        build_two_path(args.controller_ip, 6633, args)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        print("[!] Error while building topology:", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
