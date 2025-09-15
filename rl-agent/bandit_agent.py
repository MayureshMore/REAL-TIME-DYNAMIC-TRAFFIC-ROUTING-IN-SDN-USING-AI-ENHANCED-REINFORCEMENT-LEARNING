<<<<<<< HEAD
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
=======
#!/usr/bin/env python3
"""
rl-agent/bandit_agent.py

Epsilon-greedy bandit that selects among k candidate paths exposed by the
controller's REST API and applies the chosen route. It uses live port
counters to compute a simple reward (Δtx_bytes/s minus error/drop penalties).

Requirements:
  - Controller REST running (see sdn_router_rest.py)
  - requests (pip install requests)

Typical usage:
  python3 rl-agent/bandit_agent.py --controller 127.0.0.1 --port 8080 --k 2 --epsilon 0.2 --trials 8
"""

from __future__ import annotations

import argparse
import time
import random
import sys
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Optional

import requests


# ----------------------------- REST helpers ----------------------------- #

def api_base(host: str, port: int) -> str:
    return f"http://{host}:{port}/api/v1"


def _get(url: str, timeout: float = 6.0) -> Any:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return None


def _post(url: str, payload: dict, timeout: float = 8.0) -> Any:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return None


def get_hosts(base: str) -> List[dict]:
    return _get(f"{base}/hosts") or []


def get_paths(base: str, src_mac: str, dst_mac: str, k: int) -> List[dict]:
    return _get(f"{base}/paths?src_mac={src_mac}&dst_mac={dst_mac}&k={k}") or []


def get_ports(base: str) -> List[dict]:
    return _get(f"{base}/stats/ports") or []


def post_route(base: str,
               src_mac: str,
               dst_mac: str,
               *,
               path_id: Optional[int] = None,
               path: Optional[List[int]] = None,
               k: int = 2) -> Any:
    payload = {'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}
    if path_id is not None:
        payload['path_id'] = int(path_id)
    if path is not None:
        payload['path'] = list(path)
    return _post(f"{base}/actions/route", payload)


def safe_post_route(base: str, src_mac: str, dst_mac: str, **kw) -> Optional[Any]:
    """
    Wrap post_route to gracefully handle cooldown (HTTP 429) without crashing.
    """
    try:
        return post_route(base, src_mac, dst_mac, **kw)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            retry_after = 3
            try:
                retry_after = int(e.response.json().get('retry_after', 3))
            except Exception:
                pass
            print(f"[agent] Cooldown active (429). Backing off {retry_after}s...", flush=True)
            time.sleep(max(1, retry_after))
            return None
        raise


# ----------------------------- Feature engineering ----------------------------- #

PortIndex = Dict[int, Dict[int, dict]]  # dpid -> port_no -> port_stats


def index_ports(snapshot: List[dict]) -> PortIndex:
    idx: PortIndex = defaultdict(dict)
    for p in snapshot:
        try:
            idx[int(p['dpid'])][int(p['port_no'])] = p
        except Exception:
            # Skip malformed entries
            continue
    return idx


def aggregate_counters(idx: PortIndex, hops: List[dict]) -> Dict[str, float]:
    """
    Sum counters across the egress ports of each hop on the path.
    Hops are of shape: {'dpid': <int>, 'out_port': <int>}
    """
    agg = {
        'rx_bytes': 0.0, 'tx_bytes': 0.0,
        'rx_pkts': 0.0, 'tx_pkts': 0.0,
        'rx_dropped': 0.0, 'tx_dropped': 0.0,
        'rx_errors': 0.0, 'tx_errors': 0.0,
    }
    for h in hops:
        dpid = int(h.get('dpid', -1))
        outp = int(h.get('out_port', -1))
        p = idx.get(dpid, {}).get(outp)
        if not p:
            continue
        agg['rx_bytes'] += float(p.get('rx_bytes', 0))
        agg['tx_bytes'] += float(p.get('tx_bytes', 0))
        agg['rx_pkts']  += float(p.get('rx_pkts', 0))
        agg['tx_pkts']  += float(p.get('tx_pkts', 0))
        agg['rx_dropped'] += float(p.get('rx_dropped', 0))
        agg['tx_dropped'] += float(p.get('tx_dropped', 0))
        agg['rx_errors']  += float(p.get('rx_errors', 0))
        agg['tx_errors']  += float(p.get('tx_errors', 0))
    return agg


def reward(then: Dict[str, float], now: Dict[str, float], dt: float) -> float:
    """
    Reward ~ transmit throughput along the chosen path (bytes/s),
    penalized by error/drop rates. We compute:
        r = Δtx_bytes/dt  -  W * (Δerrors + Δdrops)/dt
    """
    if dt <= 0:
        dt = 1e-6
    d_txB = max(0.0, now['tx_bytes'] - then['tx_bytes'])
    d_err = max(0.0, (now['rx_errors'] + now['tx_errors']) - (then['rx_errors'] + then['tx_errors']))
    d_drp = max(0.0, (now['rx_dropped'] + now['tx_dropped']) - (then['rx_dropped'] + then['tx_dropped']))

    tx_bytes_per_s = d_txB / dt
    penalty = 1000.0 * (d_err + d_drp) / dt  # weight errs/drops quite heavily
    return tx_bytes_per_s - penalty


# ----------------------------- Bandit core ----------------------------- #

def choose_path(q: Dict[int, float], n: Dict[int, int], num_arms: int, epsilon: float) -> int:
    """
    Epsilon-greedy: with prob epsilon explore a random arm, else exploit the best Q.
    """
    if num_arms <= 0:
        raise ValueError("No arms (paths) to choose from.")
    if random.random() < epsilon:
        return random.randrange(num_arms)
    best = 0
    best_val = -float('inf')
    for i in range(num_arms):
        val = q.get(i, 0.0)
        if val > best_val:
            best, best_val = i, val
    return best


# ----------------------------- Main loop ----------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Epsilon-greedy bandit path selector for SDN controller.")
    ap.add_argument('--controller', default='127.0.0.1', help='Controller IP/host for REST API')
    ap.add_argument('--port', type=int, default=8080, help='Controller REST port (default 8080)')
    ap.add_argument('--k', type=int, default=2, help='Number of candidate paths to request')
    ap.add_argument('--epsilon', type=float, default=0.2, help='Exploration probability (0..1)')
    ap.add_argument('--trials', type=int, default=8, help='Number of selection trials')
    ap.add_argument('--measure-wait', type=float, default=3.0, help='Seconds to wait after applying a route before measuring reward')
    ap.add_argument('--src', help='Optional source host MAC (if omitted, first learned host is used)')
    ap.add_argument('--dst', help='Optional destination host MAC (if omitted, second learned host is used)')
    args = ap.parse_args()

    base = api_base(args.controller, args.port)

    # Ensure controller is reachable and hosts are known
    try:
        hosts = get_hosts(base)
    except Exception as e:
        print(f"[agent] Failed to reach controller at {base}: {e}", file=sys.stderr)
        return 2

    if args.src and args.dst:
        src_mac, dst_mac = args.src, args.dst
    else:
        if len(hosts) < 2:
            print("[agent] Need at least two learned hosts (run your Mininet topology and pingall).", file=sys.stderr)
            return 1
        src_mac, dst_mac = hosts[0]['mac'], hosts[1]['mac']

    print(f"[agent] Connected to {base}")
    print(f"[agent] Using host pair: src={src_mac}  dst={dst_mac}")

    # Bandit state
    q: Dict[int, float] = {}   # estimated value per path_id
    n: Dict[int, int] = {}     # number of times each path_id was played

    # Prime a first port snapshot
    try:
        ports_prev = get_ports(base)
    except Exception as e:
        print(f"[agent] Failed to fetch port stats: {e}", file=sys.stderr)
        return 3

    idx_prev = index_ports(ports_prev)
    t_prev = time.time()

    for t in range(args.trials):
        # Fetch candidate paths (k-shortest from controller)
        try:
            paths = get_paths(base, src_mac, dst_mac, args.k)
        except Exception as e:
            print(f"[agent] Failed to fetch paths: {e}", file=sys.stderr)
            time.sleep(2.0)
            continue

        if not paths:
            print("[agent] No paths available yet; will retry...", flush=True)
            time.sleep(2.0)
            continue

        # Choose a path_id with epsilon-greedy on current q
        choice = choose_path(q, n, len(paths), args.epsilon)
        chosen = paths[choice]
        print(f"[t={t}] choosing path_id={choice} dpids={chosen.get('dpids')}")

        # Snapshot BEFORE applying route (baseline for reward)
        ports_then = get_ports(base)
        idx_then = index_ports(ports_then)
        agg_then = aggregate_counters(idx_then, chosen.get('hops', []))
        t_then = time.time()

        # Apply route (handle 429 cooldown gracefully)
        safe_post_route(base, src_mac, dst_mac, path_id=choice, k=args.k)

        # Wait a bit, then measure reward
        time.sleep(max(0.5, args.measure_wait))
        ports_now = get_ports(base)
        idx_now = index_ports(ports_now)
        agg_now = aggregate_counters(idx_now, chosen.get('hops', []))
        t_now = time.time()

        r = reward(agg_then, agg_now, t_now - t_then)
        n[choice] = n.get(choice, 0) + 1
        q_prev = q.get(choice, 0.0)
        q[choice] = q_prev + (r - q_prev) / n[choice]

        print(f"  reward≈{r:.2f} | plays={n[choice]} | q[{choice}]≈{q[choice]:.2f}", flush=True)

        # Update global snapshots (not strictly needed, but useful if extending logic)
        idx_prev = idx_now
        t_prev = t_now

    print("[agent] Finished. Estimated values:", {k: round(v, 2) for k, v in q.items()})
    return 0


if __name__ == '__main__':
    sys.exit(main())
>>>>>>> f7a14b5 (agent: update bandit_agent (epsilon-greedy, cooldown-safe routing, cleaner reward))
