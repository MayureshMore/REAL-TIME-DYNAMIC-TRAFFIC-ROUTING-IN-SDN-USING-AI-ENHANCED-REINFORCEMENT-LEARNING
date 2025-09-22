cat > rl-agent/bandit_agent.py <<'PY'
#!/usr/bin/env python3
"""
Epsilon-greedy bandit that selects among k candidate paths exposed by the
controller REST API and applies the chosen route. Reward = Δtx_bytes/s minus
error/drop penalties.

Hardened to:
- Wait for valid paths (dpids length >= 2 and non-empty hops)
- Treat 409 (no_path) and 429 (cooldown) as transient and retry
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

def get_hosts(base: str) -> List[dict]:
    try: return _get(f"{base}/hosts") or []
    except Exception: return []

def get_paths(base: str, src_mac: str, dst_mac: str, k: int) -> List[dict]:
    try:
        r = requests.get(f"{base}/paths", params={'src_mac':src_mac,'dst_mac':dst_mac,'k':k}, timeout=8)
        r.raise_for_status()
        return r.json() or []
    except requests.HTTPError:
        return []
    except Exception:
        return []

def get_ports(base: str) -> List[dict]:
    try: return _get(f"{base}/stats/ports") or []
    except Exception: return []

def valid_paths(paths: List[dict]) -> List[dict]:
    out=[]
    for p in paths or []:
        dp = p.get('dpids') or []
        hops = p.get('hops') or []
        if len(dp) >= 2 and len(hops) >= 1:
            out.append(p)
    return out

def post_route(base: str, src_mac: str, dst_mac: str, *, path_id: Optional[int]=None, path: Optional[List[int]]=None, k: int=2):
    payload = {'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}
    if path_id is not None: payload['path_id'] = int(path_id)
    if path is not None: payload['path'] = list(path)
    return _post(f"{base}/actions/route", payload)

def safe_post_route(base: str, src_mac: str, dst_mac: str, **kw) -> Optional[Any]:
    try:
        return post_route(base, src_mac, dst_mac, **kw)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 429:
            retry_after = 3
            try: retry_after = int(e.response.json().get('retry_after', 3))
            except Exception: pass
            print(f"[agent] Cooldown active (429). Backing off {retry_after}s...", flush=True)
            time.sleep(max(1, retry_after))
            return None
        if code == 409:
            # Controller says no path yet (topology/hosts not ready) — transient
            print("[agent] No path available yet (409). Will retry...", flush=True)
            time.sleep(2.0)
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

def wait_for_hosts_and_paths(base: str, k: int, timeout: float) -> tuple[str,str,List[dict]]:
    t0 = time.time()
    src_mac = dst_mac = None
    while True:
        hosts = get_hosts(base)
        if len(hosts) >= 2:
            src_mac, dst_mac = hosts[0]['mac'], hosts[1]['mac']
            paths = valid_paths(get_paths(base, src_mac, dst_mac, k))
            if paths:
                return src_mac, dst_mac, paths
        if time.time() - t0 > timeout:
            raise RuntimeError("Timed out waiting for hosts/paths to become available")
        time.sleep(1.5)

def main() -> int:
    ap = argparse.ArgumentParser(description="Epsilon-greedy bandit path selector.")
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--epsilon', type=float, default=0.2)
    ap.add_argument('--trials', type=int, default=240)  # ~1h at ~15s/loop
    ap.add_argument('--measure-wait', type=float, default=3.0)
    ap.add_argument('--wait-for-paths', type=float, default=60.0, help="Seconds to wait for discovery before starting")
    ap.add_argument('--src'); ap.add_argument('--dst')
    args = ap.parse_args()

    base = api_base(args.controller, args.port)

    # Ensure we have hosts + valid paths before starting the learning loop
    if args.src and args.dst:
        src_mac, dst_mac = args.src, args.dst
        paths0 = valid_paths(get_paths(base, src_mac, dst_mac, args.k))
        if not paths0:
            print("[agent] Waiting for controller to discover valid paths...", flush=True)
            src_mac, dst_mac, paths0 = wait_for_hosts_and_paths(base, args.k, args.wait_for_paths)
    else:
        print("[agent] Waiting for hosts and paths...", flush=True)
        src_mac, dst_mac, paths0 = wait_for_hosts_and_paths(base, args.k, args.wait_for_paths)

    print(f"[agent] Controller: {base} | src={src_mac} dst={dst_mac}")
    q: Dict[int,float] = {}; n: Dict[int,int] = {}
    idx_prev = index_ports(get_ports(base))

    for t in range(args.trials):
        # Refresh candidates until valid
        for _ in range(10):
            paths = valid_paths(get_paths(base, src_mac, dst_mac, args.k))
            if paths: break
            print("[agent] No valid paths yet; retrying...", flush=True)
            time.sleep(2.0)
        if not paths:
            # give up this tick gracefully
            time.sleep(3.0)
            continue

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
PY
