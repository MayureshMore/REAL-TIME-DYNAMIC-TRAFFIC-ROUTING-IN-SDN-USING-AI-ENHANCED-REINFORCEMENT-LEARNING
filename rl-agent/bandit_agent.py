#!/usr/bin/env python3
# Epsilon-greedy bandit for path selection. Robust to 409/429 responses.
# Fix: pick a valid (src,dst) by probing /paths over all host pairs.

import argparse, time, random, sys, requests
from collections import defaultdict

def api_base(h, p): return f"http://{h}:{p}/api/v1"
def _get(url, t=6.0):
    r = requests.get(url, timeout=t); r.raise_for_status()
    try: return r.json()
    except Exception: return None
def _post(url, payload, t=8.0):
    r = requests.post(url, json=payload, timeout=t); r.raise_for_status()
    try: return r.json()
    except Exception: return None

def get_hosts(base): return _get(f"{base}/hosts") or []
def get_ports(base): return _get(f"{base}/stats/ports") or []
def get_paths(base, src, dst, k): return _get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}") or []

def post_route(base, src, dst, *, path_id=None, path=None, k=2):
    payload = {'src_mac': src, 'dst_mac': dst, 'k': k}
    if path_id is not None: payload['path_id'] = int(path_id)
    if path is not None: payload['path'] = list(path)
    return _post(f"{base}/actions/route", payload)

def safe_post_route(base, src, dst, **kw):
    try:
        return post_route(base, src, dst, **kw)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 429:
            retry_after = 3
            try: retry_after = int(e.response.json().get('retry_after', 3))
            except Exception: pass
            print(f"[agent] 429 cooldown; backoff {retry_after}s")
            time.sleep(max(1, retry_after))
            return None
        if code == 409:
            try: print(f"[agent] 409: {e.response.json()}")
            except Exception: print("[agent] 409 Conflict")
            return None
        raise

def index_ports(snapshot):
    idx = defaultdict(dict)
    for p in snapshot:
        try: idx[int(p['dpid'])][int(p['port_no'])] = p
        except Exception: pass
    return idx

def aggregate(hops, idx):
    agg = {'rx_bytes':0.0,'tx_bytes':0.0,'rx_pkts':0.0,'tx_pkts':0.0,
           'rx_dropped':0.0,'tx_dropped':0.0,'rx_errors':0.0,'tx_errors':0.0}
    for h in hops:
        p = idx.get(int(h.get('dpid',-1)), {}).get(int(h.get('out_port',-1)))
        if not p: continue
        agg['rx_bytes'] += float(p.get('rx_bytes',0)); agg['tx_bytes'] += float(p.get('tx_bytes',0))
        agg['rx_pkts']  += float(p.get('rx_pkts',0));  agg['tx_pkts']  += float(p.get('tx_pkts',0))
        agg['rx_dropped'] += float(p.get('rx_dropped',0)); agg['tx_dropped'] += float(p.get('tx_dropped',0))
        agg['rx_errors']  += float(p.get('rx_errors',0));  agg['tx_errors']  += float(p.get('tx_errors',0))
    return agg

def reward(then, now, dt):
    if dt <= 0: dt = 1e-6
    dtx = max(0.0, now['tx_bytes'] - then['tx_bytes'])
    derr = max(0.0, (now['rx_errors']+now['tx_errors']) - (then['rx_errors']+then['tx_errors']))
    ddrop= max(0.0, (now['rx_dropped']+now['tx_dropped']) - (then['rx_dropped']+then['tx_dropped']))
    return (dtx / dt) - 1000.0 * ((derr + ddrop) / dt)

def choose(q, n):
    best, val = 0, -1e99
    for i in range(n):
        v = q.get(i, 0.0)
        if v > val: best, val = i, v
    return best

def pick_valid_pair(base, k, wait_sec):
    """Pick (src,dst) such that /paths returns at least one path."""
    t0 = time.time()
    while True:
        hs = get_hosts(base)
        if hs and len(hs) >= 2:
            for i in range(len(hs)):
                for j in range(i+1, len(hs)):
                    s = hs[i]['mac']; d = hs[j]['mac']
                    try:
                        ps = get_paths(base, s, d, k)
                        if ps:
                            return s, d
                    except Exception:
                        pass
        if time.time() - t0 > wait_sec:
            return None, None
        time.sleep(1.5)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--epsilon', type=float, default=0.2)
    ap.add_argument('--trials', type=int, default=600)
    ap.add_argument('--measure-wait', type=float, default=3.0)
    ap.add_argument('--wait-hosts', type=int, default=60)
    ap.add_argument('--wait-paths', type=int, default=60)
    args = ap.parse_args()

    base = api_base(args.controller, args.port)

    # Pick a valid pair by probing /paths
    src, dst = pick_valid_pair(base, args.k, max(args.wait_hosts, args.wait_paths))
    if not src or not dst:
        print("[agent] timeout waiting for paths", file=sys.stderr); return 2
    print(f"[agent] Controller: {base} | src={src} dst={dst}")

    q = {}; plays = {}
    prev_ports = get_ports(base); prev_idx = index_ports(prev_ports)

    for t in range(args.trials):
        paths = get_paths(base, src, dst, args.k)
        if not paths:
            print("[agent] paths disappeared; skipping"); time.sleep(2.0); continue

        # epsilon-greedy
        if random.random() < args.epsilon:
            choice = random.randrange(len(paths))
        else:
            choice = choose(q, len(paths))
        chosen = paths[choice]
        if len(chosen.get('dpids', [])) < 2:
            print('[agent] invalid path (len<2); skipping')
            time.sleep(1.0)
            continue
        print(f"[t={t}] choose path_id={choice} dpids={chosen.get('dpids')}")

        ports_then = get_ports(base); idx_then = index_ports(ports_then)
        agg_then = aggregate(chosen.get('hops', []), idx_then); t_then = time.time()

        safe_post_route(base, src, dst, path_id=choice, k=args.k)

        time.sleep(max(0.5, args.measure_wait))
        ports_now = get_ports(base); idx_now = index_ports(ports_now)
        agg_now = aggregate(chosen.get('hops', []), idx_now); t_now = time.time()

        r = reward(agg_then, agg_now, t_now - t_then)
        plays[choice] = plays.get(choice, 0) + 1
        q[choice] = q.get(choice, 0.0) + (r - q[choice]) / plays[choice] if choice in q else r
        print(f"  reward≈{r:.2f} | plays={plays[choice]} | q[{choice}]≈{q[choice]:.2f}", flush=True)

    print("[agent] Done. Q-estimates:", {k: round(v, 2) for k, v in q.items()})
    return 0

if __name__ == '__main__':
    sys.exit(main())
