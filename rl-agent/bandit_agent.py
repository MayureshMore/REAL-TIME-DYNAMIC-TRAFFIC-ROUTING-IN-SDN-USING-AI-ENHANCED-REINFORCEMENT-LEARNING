#!/usr/bin/env python3
"""
Epsilon-greedy bandit that selects among k candidate paths exposed by the
controller REST API and applies the chosen route. Reward = Δtx_bytes/s minus
error/drop penalties. Handles controller 429 cooldown responses gracefully.

Usage:
  python3 rl-agent/bandit_agent.py --controller 127.0.0.1 --port 8080 --k 2 --epsilon 0.2 --trials 8
"""

from __future__ import annotations
import argparse, time, random, sys
from typing import Optional, List, Dict, Any
from collections import defaultdict
import requests

def api_base(h: str, p: int) -> str: return f"http://{h}:{p}/api/v1"
def _get(url: str, timeout: float = 6.0) -> Any:
    r = requests.get(url, timeout=timeout); r.raise_for_status()
    try: return r.json()
    except Exception: return None
def _post(url: str, payload: dict, timeout: float = 8.0) -> Any:
    r = requests.post(url, json=payload, timeout=timeout); r.raise_for_status()
    try: return r.json()
    except Exception: return None

def get_hosts(base: str) -> List[dict]: return _get(f"{base}/hosts") or []
def get_paths(base: str, src_mac: str, dst_mac: str, k: int) -> List[dict]:
    return _get(f"{base}/paths?src_mac={src_mac}&dst_mac={dst_mac}&k={k}") or []
def get_ports(base: str) -> List[dict]: return _get(f"{base}/stats/ports") or []

def post_route(base: str, src_mac: str, dst_mac: str, *, path_id: Optional[int]=None, path: Optional[List[int]]=None, k: int=2):
    payload = {'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}
    if path_id is not None: payload['path_id'] = int(path_id)
    if path is not None: payload['path'] = list(path)
    return _post(f"{base}/actions/route", payload)

def safe_post_route(base: str, src_mac: str, dst_mac: str, **kw) -> Optional[Any]:
    try:
        return post_route(base, src_mac, dst_mac, **kw)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            retry_after = 3
            try: retry_after = int(e.response.json().get('retry_after', 3))
            except Exception: pass
            print(f"[agent] Cooldown active (429). Backing off {retry_after}s...", flush=True)
            time.sleep(max(1, retry_after))
            return None
        raise

PortIndex = Dict[int, Dict[int, dict]]
def index_ports(snapshot: List[dict]) -> PortIndex:
    idx: PortIndex = defaultdict(dict)
    for p in snapshot:
        try: idx[int(p['dpid'])][int(p['port_no'])] = p
        except Exception: continue
    return idx

def aggregate_counters(idx: PortIndex, hops: List[dict]) -> Dict[str, float]:
    agg = {'rx_bytes':0.0,'tx_bytes':0.0,'rx_pkts':0.0,'tx_pkts':0.0,'rx_dropped':0.0,'tx_dropped':0.0,'rx_errors':0.0,'tx_errors':0.0}
    for h in hops:
        dpid = int(h.get('dpid', -1)); outp = int(h.get('out_port', -1))
        p = idx.get(dpid, {}).get(outp)
        if not p: continue
        agg['rx_bytes'] += float(p.get('rx_bytes',0)); agg['tx_bytes'] += float(p.get('tx_bytes',0))
        agg['rx_pkts']  += float(p.get('rx_pkts',0));  agg['tx_pkts']  += float(p.get('tx_pkts',0))
        agg['rx_dropped'] += float(p.get('rx_dropped',0)); agg['tx_dropped'] += float(p.get('tx_dropped',0))
        agg['rx_errors']  += float(p.get('rx_errors',0));  agg['tx_errors']  += float(p.get('tx_errors',0))
    return agg

def reward(then: Dict[str,float], now: Dict[str,float], dt: float) -> float:
    if dt <= 0: dt = 1e-6
    d_txB = max(0.0, now['tx_bytes'] - then['tx_bytes'])
    d_err = max(0.0, (now['rx_errors']+now['tx_errors']) - (then['rx_errors']+then['tx_errors']))
    d_drp = max(0.0, (now['rx_dropped']+now['tx_dropped']) - (then['rx_dropped']+then['tx_dropped']))
    tx_Bps = d_txB / dt
    penalty = 1000.0 * (d_err + d_drp) / dt
    return tx_Bps - penalty

def choose_path(q: Dict[int,float], num_arms: int, epsilon: float) -> int:
    if num_arms <= 0: raise ValueError("No candidate paths.")
    if random.random() < epsilon: return random.randrange(num_arms)
    best, best_val = 0, -float('inf')
    for i in range(num_arms):
        v = q.get(i, 0.0)
        if v > best_val: best, best_val = i, v
    return best

def main() -> int:
    ap = argparse.ArgumentParser(description="Epsilon-greedy bandit path selector.")
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--epsilon', type=float, default=0.2)
    ap.add_argument('--trials', type=int, default=8)
    ap.add_argument('--measure-wait', type=float, default=3.0)
    ap.add_argument('--src'); ap.add_argument('--dst')
    args = ap.parse_args()

    base = api_base(args.controller, args.port)
    try: hosts = get_hosts(base)
    except Exception as e:
        print(f"[agent] Cannot reach controller at {base}: {e}", file=sys.stderr); return 2

    if args.src and args.dst: src_mac, dst_mac = args.src, args.dst
    else:
        if len(hosts) < 2:
            print("[agent] Need ≥2 learned hosts (run topology & pingall).", file=sys.stderr); return 1
        src_mac, dst_mac = hosts[0]['mac'], hosts[1]['mac']

    print(f"[agent] Controller: {base} | src={src_mac} dst={dst_mac}")
    q: Dict[int,float] = {}; n: Dict[int,int] = {}
    ports_prev = get_ports(base); idx_prev = index_ports(ports_prev)

    for t in range(args.trials):
        paths = get_paths(base, src_mac, dst_mac, args.k)
        if not paths:
            print("[agent] No paths yet; retrying..."); time.sleep(2.0); continue

        choice = choose_path(q, len(paths), args.epsilon)
        chosen = paths[choice]
        print(f"[t={t}] choose path_id={choice} dpids={chosen.get('dpids')}")

        ports_then = get_ports(base); idx_then = index_ports(ports_then)
        agg_then = aggregate_counters(idx_then, chosen.get('hops', []))
        t_then = time.time()

        safe_post_route(base, src_mac, dst_mac, path_id=choice, k=args.k)

        time.sleep(max(0.5, args.measure_wait))
        ports_now = get_ports(base); idx_now = index_ports(ports_now)
        agg_now = aggregate_counters(idx_now, chosen.get('hops', []))
        t_now = time.time()

        r = reward(agg_then, agg_now, t_now - t_then)
        n[choice] = n.get(choice, 0) + 1
        q[choice] = q.get(choice, 0.0) + (r - q.get(choice, 0.0)) / n[choice]
        print(f"  reward≈{r:.2f} | plays={n[choice]} | q[{choice}]≈{q[choice]:.2f}", flush=True)

        idx_prev = idx_now

    print("[agent] Done. Q-estimates:", {k: round(v, 2) for k, v in q.items()})
    return 0

if __name__ == '__main__':
    sys.exit(main())
