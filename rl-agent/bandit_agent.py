# rl-agent/bandit_agent.py
# Epsilon-greedy contextual bandit that selects among k candidate paths for (src_mac, dst_mac).

import argparse
import json
import time
import requests
import random
from collections import defaultdict


def get_hosts(base):
    r = requests.get(f"{base}/hosts", timeout=3)
    r.raise_for_status()
    return r.json()


def get_paths(base, src_mac, dst_mac, k=2):
    r = requests.get(f"{base}/paths", params={'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}, timeout=5)
    r.raise_for_status()
    return r.json()


def get_ports(base):
    r = requests.get(f"{base}/stats/ports", timeout=5)
    r.raise_for_status()
    return r.json()


def post_route(base, src_mac, dst_mac, path_id=None, path=None, k=2):
    payload = {'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}
    if path_id is not None:
        payload['path_id'] = path_id
    if path is not None:
        payload['path'] = path
    r = requests.post(f"{base}/actions/route", json=payload, timeout=5)
    r.raise_for_status()
    return r.json()


def features_for_path(ports_snapshot, path_hops):
    # index port stats by (dpid, port)
    idx = defaultdict(dict)
    for p in ports_snapshot:
        idx[p['dpid']][p['port_no']] = p
    agg = {'rx_bytes': 0, 'tx_bytes': 0, 'rx_pkts': 0, 'tx_pkts': 0, 'err': 0, 'drops': 0, 'hops': len(path_hops)}
    for hop in path_hops:
        p = idx.get(hop['dpid'], {}).get(hop['out_port'])
        if p:
            agg['rx_bytes'] += p.get('rx_bytes', 0)
            agg['tx_bytes'] += p.get('tx_bytes', 0)
            agg['rx_pkts']  += p.get('rx_pkts', 0)
            agg['tx_pkts']  += p.get('tx_pkts', 0)
            agg['err']      += p.get('rx_errors', 0) + p.get('tx_errors', 0)
            agg['drops']    += p.get('rx_dropped', 0) + p.get('tx_dropped', 0)
    return agg


def score(agg_then, agg_now, dt):
    if dt <= 0:
        return 0.0
    d_tx = max(0, agg_now['tx_bytes'] - agg_then['tx_bytes']) / dt
    d_err = max(0, agg_now['err'] - agg_then['err'])
    d_drp = max(0, agg_now['drops'] - agg_then['drops'])
    return d_tx - 1000 * (d_err + d_drp)  # crude penalty weight


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--src', required=False)
    ap.add_argument('--dst', required=False)
    ap.add_argument('--epsilon', type=float, default=0.2)
    ap.add_argument('--trials', type=int, default=6)
    args = ap.parse_args()

    base = f"http://{args.controller}:{args.port}/api/v1"
    hosts = get_hosts(base)
    if len(hosts) < 2 and (not args.src or not args.dst):
        raise SystemExit("Need at least two learned hosts (generate traffic or ping so MACs are learned)")
    src_mac = args.src or hosts[0]['mac']
    dst_mac = args.dst or hosts[1]['mac']

    q = defaultdict(float)  # path_id -> value
    n = defaultdict(int)    # path_id -> plays

    print("Bandit agent connected to", base, "; src=", src_mac, "dst=", dst_mac)

    last_ports = get_ports(base)
    last_t = time.time()

    for t in range(args.trials):
        paths = get_paths(base, src_mac, dst_mac, k=args.k)
        if not paths:
            print("No paths available yet; retrying...")
            time.sleep(2)
            continue

        # epsilon-greedy select
        if random.random() < args.epsilon:
            choice = random.randrange(len(paths))
        else:
            best = max(range(len(paths)), key=lambda i: q[i] if i in q else 0.0)
            choice = best

        chosen = paths[choice]
        print(f"[t={t}] choosing path_id={choice} dpids={chosen['dpids']}")
        post_route(base, src_mac, dst_mac, path_id=choice, k=args.k)

        # Observe reward after a short interval
        time.sleep(3)
        now_ports = get_ports(base)
        now_t = time.time()

        agg_then = features_for_path(last_ports, chosen['hops'])
        agg_now  = features_for_path(now_ports,  chosen['hops'])
        r = score(agg_then, agg_now, now_t - last_t)
        print(f"  reward≈{r:.2f} (ΔtxB/s minus penalties)")

        n[choice] += 1
        q[choice] += (r - q[choice]) / n[choice]

        last_ports = now_ports
        last_t = now_t

    print("Final estimates:", dict(q))


if __name__ == '__main__':
    main()
