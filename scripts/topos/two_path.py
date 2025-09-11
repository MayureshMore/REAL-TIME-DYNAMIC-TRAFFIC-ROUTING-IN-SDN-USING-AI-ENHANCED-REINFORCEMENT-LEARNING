#!/usr/bin/env python3
# scripts/topos/two_path.py
# Two disjoint paths between h1 and h2 with parametric link conditions.
# Optional non-interactive demo: run iperf3 + ping and capture metrics & logs.

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

import argparse
import os
import time
import json
from datetime import datetime
from subprocess import Popen, PIPE

class TwoPathTopo(Topo):
    def build(self, bw_a=10, bw_b=10, delay_a="0ms", delay_b="0ms",
              loss_a=0.0, loss_b=0.0, max_queue=100):
        # Hosts
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')

        # Switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        s5 = self.addSwitch('s5')

        # Path A: h1 - s1 - s3 - s5 - h2
        self.addLink(h1, s1, cls=TCLink, bw=bw_a, delay=delay_a, loss=loss_a,
                     max_queue_size=max_queue, use_htb=True)
        self.addLink(s1, s3, cls=TCLink, bw=bw_a, delay=delay_a, loss=loss_a,
                     max_queue_size=max_queue, use_htb=True)
        self.addLink(s3, s5, cls=TCLink, bw=bw_a, delay=delay_a, loss=loss_a,
                     max_queue_size=max_queue, use_htb=True)
        self.addLink(s5, h2, cls=TCLink, bw=bw_a, delay=delay_a, loss=loss_a,
                     max_queue_size=max_queue, use_htb=True)

        # Path B: h1 - s2 - s4 - s5 - h2
        self.addLink(h1, s2, cls=TCLink, bw=bw_b, delay=delay_b, loss=loss_b,
                     max_queue_size=max_queue, use_htb=True)
        self.addLink(s2, s4, cls=TCLink, bw=bw_b, delay=delay_b, loss=loss_b,
                     max_queue_size=max_queue, use_htb=True)
        self.addLink(s4, s5, cls=TCLink, bw=bw_b, delay=delay_b, loss=loss_b,
                     max_queue_size=max_queue, use_htb=True)

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def write_text(path: str, content: str):
    with open(path, "w") as f:
        f.write(content)

def write_json(path: str, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)

def run_demo(net, ctrl_ip, rest_port, run_dir, demo_time):
    """
    Launch iperf3 server on h1, client on h2 (JSON), run ping,
    and concurrently run the REST logger for 'demo_time' seconds.
    """
    info(f"* Demo: writing artifacts under {run_dir}\n")
    ensure_dir(run_dir)

    h1, h2 = net.get('h1', 'h2')
    h1_ip = h1.IP()

    # Start iperf3 server (serve exactly one test, then exit)
    info("* Starting iperf3 server on h1 (-1)\n")
    s_proc = h1.popen(["iperf3", "-s", "-1"], stdout=PIPE, stderr=PIPE)

    # Kick off metrics logger for the duration
    csv_path = os.path.join(run_dir, "ports.csv")
    info("* Starting REST metrics logger\n")
    logger_proc = Popen([
        "python3", "scripts/metrics/log_stats.py",
        "--controller", str(ctrl_ip),
        "--port", str(rest_port),
        "--interval", "1",
        "--duration", str(demo_time),
        "--out", csv_path
    ], stdout=PIPE, stderr=PIPE)

    time.sleep(1.0)  # small settle

    # Run iperf3 client with JSON output
    info("* Running iperf3 client on h2\n")
    c_out, c_err = h2.popen(
        ["iperf3", "-c", h1_ip, "-t", str(demo_time), "-J"],
        stdout=PIPE, stderr=PIPE
    ).communicate()
    iperf_json_path = os.path.join(run_dir, "iperf_client.json")
    write_text(iperf_json_path, c_out.decode("utf-8"))

    # Ping for RTT sample
    info("* Running ping (10 packets) from h2 to h1\n")
    p_out, _ = h2.popen(["ping", "-c", "10", h1_ip], stdout=PIPE, stderr=PIPE).communicate()
    ping_txt_path = os.path.join(run_dir, "ping.txt")
    write_text(ping_txt_path, p_out.decode("utf-8"))

    # Wait for logger to finish (duration-limited)
    logger_proc.wait(timeout=demo_time + 10)

    # Ensure server has finished; if not, terminate
    try:
        s_proc.wait(timeout=3)
    except Exception:
        s_proc.terminate()

    # Write a small manifest
    manifest = {
        "controller_ip": ctrl_ip,
        "rest_port": rest_port,
        "demo_time_s": demo_time,
        "outputs": {
            "ports_csv": csv_path,
            "iperf_client_json": iperf_json_path,
            "ping_txt": ping_txt_path
        },
        "timestamp": time.time()
    }
    write_json(os.path.join(run_dir, "manifest.json"), manifest)
    info(f"* Demo complete. Artifacts in: {run_dir}\n")

def main():
    setLogLevel('info')
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller_ip', default='127.0.0.1')
    ap.add_argument('--controller_port', type=int, default=6633)
    ap.add_argument('--rest_port', type=int, default=8080, help='Ryu WSGI REST port')
    ap.add_argument('--bw_a', type=float, default=10.0)
    ap.add_argument('--bw_b', type=float, default=10.0)
    ap.add_argument('--delay_a', default='0ms')
    ap.add_argument('--delay_b', default='0ms')
    ap.add_argument('--loss_a', type=float, default=0.0)
    ap.add_argument('--loss_b', type=float, default=0.0)
    ap.add_argument('--max_queue', type=int, default=100)
    ap.add_argument('--demo', action='store_true', help='Run iperf + ping + logger automatically')
    ap.add_argument('--demo_time', type=int, default=20)
    ap.add_argument('--run_dir', default=None, help='Directory to store artifacts (default: docs/baseline/runs/<ts>)')
    ap.add_argument('--no_cli', action='store_true', help='Do not enter Mininet CLI (auto stop after demo)')
    args = ap.parse_args()

    topo = TwoPathTopo(
        bw_a=args.bw_a, bw_b=args.bw_b,
        delay_a=args.delay_a, delay_b=args.delay_b,
        loss_a=args.loss_a, loss_b=args.loss_b,
        max_queue=args.max_queue
    )
    net = Mininet(topo=topo, link=TCLink, controller=None, switch=OVSSwitch, autoSetMacs=True)
    c0 = net.addController('c0', controller=RemoteController, ip=args.controller_ip, port=args.controller_port)

    info("* Starting network\n")
    net.start()

    info("* Testing basic connectivity (pingAll)\n")
    net.pingAll()

    # Optional demo: capture baseline artifacts
    if args.demo:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = args.run_dir or os.path.join("docs", "baseline", "runs", ts)
        run_demo(net, ctrl_ip=args.controller_ip, rest_port=args.rest_port, run_dir=run_dir, demo_time=args.demo_time)

    if not args.no_cli:
        CLI(net)

    info("* Stopping network\n")
    net.stop()

if _name_ == '_main_':
    main()