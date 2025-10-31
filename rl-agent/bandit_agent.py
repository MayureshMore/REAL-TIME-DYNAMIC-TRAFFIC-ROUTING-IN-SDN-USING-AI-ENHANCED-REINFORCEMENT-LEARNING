#!/usr/bin/env python3
# Enhanced Epsilon-Greedy Bandit Agent for SDN Path Selection
# -----------------------------------------------------------
# Improvements:
# - Persistent Q-values (saved to q_values.json)
# - Adaptive epsilon decay
# - Normalized throughput-based reward
# - Robust handling of 429/409 API responses
# - Compatible with DQN and LinUCB comparison

import argparse, time, random, sys, requests, json, os
from collections import defaultdict

SAVE_FILE = "q_values.json"
MAX_BW = 10_000_000.0  # assume 10 Mbps link
DECAY_RATE = 0.995

def api_base(host, port): return f"http://{host}:{port}/api/v1"

def _get(url, timeout=6.0):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[agent] GET failed:", e)
        return None

def _post(url, payload, timeout=8.0):
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[agent] POST failed:", e)
        raise

def get_hosts(base): return _get(f"{base}/hosts") or []
def get_ports(base): return _get(f"{base}/stats/ports") or []
def get_paths(base, src, dst, k): return _get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}") or []

def post_route(base, src, dst, path_id=None, path=None, k=2):
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
            try:
                retry = int(e.response.json().get('retry_after', 3))
            except Exception:
                retry = 3
            print(f"[agent] Cooldown active, waiting {retry}s")
            time.sleep(retry)
            return None
        if code == 409:
            print("[agent] No valid path currently")
            time.sleep(2)
            return None
        print("[agent] Unexpected HTTP error:", e)
        return None
    except Exception as e:
        print("[agent] Post failed:", e)
        return None

def index_ports(snapshot):
    idx = defaultdict(dict)
    for p in snapshot:
        try:
            idx[int(p['dpid'])][int(p['port_no'])] = p
        except Exception:
            pass
    return idx

def aggregate(hops, idx):
    agg = {'rx_bytes':0.0,'tx_bytes':0.0,'rx_pkts':0.0,'tx_pkts':0.0,
           'rx_dropped':0.0,'tx_dropped':0.0,'rx_errors':0.0,'tx_errors':0.0}
    for h in hops:
        p = idx.get(int(h.get('dpid', -1)), {}).get(int(h.get('out_port', -1)))
        if not p: continue
        agg['rx_bytes'] += float(p.get('rx_bytes', 0))
        agg['tx_bytes'] += float(p.get('tx_bytes', 0))
        agg['rx_pkts']  += float(p.get('rx_pkts', 0))
        agg['tx_pkts']  += float(p.get('tx_pkts', 0))
        agg['rx_dropped'] += float(p.get('rx_dropped', 0))
        agg['tx_dropped'] += float(p.get('tx_dropped', 0))
        agg['rx_errors']  += float(p.get('rx_errors', 0))
        agg['tx_errors']  += float(p.get('tx_errors', 0))
    return agg

def reward(prev, now, dt):
    """Throughput minus error/drop penalty, normalized by bandwidth"""
    if dt <= 0: dt = 1e-6
    tx_bytes = max(0.0, now['tx_bytes'] - prev['tx_bytes'])
    drop = max(0.0, (now['tx_dropped'] + now['rx_dropped']) - (prev['tx_dropped'] + prev['rx_dropped']))
    err  = max(0.0, (now['tx_errors'] + now['rx_errors']) - (prev['tx_errors'] + prev['rx_errors']))
    throughput_bps = 8.0 * tx_bytes / dt
    loss_penalty = 5000.0 * ((drop + err) / dt)
    norm = (throughput_bps / MAX_BW) - (loss_penalty / MAX_BW)
    return max(-1.0, min(norm, 1.0))  # clamp reward

def load_q():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_q(q):
    try:
        with open(SAVE_FILE, 'w') as f:
            json.dump(q, f, indent=2)
    except Exception as e:
        print("[agent] Could not save q_values:", e)

def pick_pair(base, k, wait=60):
    t0 = time.time()
    while True:
        hosts = get_hosts(base)
        if hosts and len(hosts) >= 2:
            for i in range(len(hosts)):
                for j in range(i+1, len(hosts)):
                    s, d = hosts[i]['mac'], hosts[j]['mac']
                    ps = get_paths(base, s, d, k)
                    if ps and isinstance(ps, list):
                        return s, d
        if time.time() - t0 > wait:
            return None, None
        time.sleep(2)

def choose(q, n):
    best, val = 0, -999
    for i in range(n):
        v = q.get(str(i), 0.0)
        if v > val: best, val = i, v
    return best

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--epsilon', type=float, default=0.3)
    ap.add_argument('--trials', type=int, default=300)
    ap.add_argument('--measure-wait', type=float, default=3.0)
    ap.add_argument('--wait-hosts', type=int, default=60)
    args = ap.parse_args()

    base = api_base(args.controller, args.port)
    q = load_q()
    plays = defaultdict(int)

    src, dst = pick_pair(base, args.k, args.wait_hosts)
    if not src or not dst:
        print("[agent] No valid host pair found")
        return 2
    print(f"[agent] Controller={base} | src={src} dst={dst}")

    prev_ports = get_ports(base)
    prev_idx = index_ports(prev_ports)
    eps = args.epsilon

    for t in range(args.trials):
        paths = get_paths(base, src, dst, args.k)
        if not paths:
            print("[agent] paths not available; retrying...")
            time.sleep(2)
            continue

        if random.random() < eps:
            choice = random.randrange(len(paths))
        else:
            choice = choose(q, len(paths))

        chosen = paths[choice]
        hops = chosen.get('hops', [])
        if len(hops) < 1:
            time.sleep(1)
            continue

        print(f"[t={t}] path_id={choice} dpids={chosen.get('dpids')} eps={eps:.3f}")
        ports_before = get_ports(base)
        idx_before = index_ports(ports_before)
        agg_before = aggregate(hops, idx_before)
        t0 = time.time()

        safe_post_route(base, src, dst, path_id=choice, k=args.k)
        time.sleep(args.measure_wait)

        ports_after = get_ports(base)
        idx_after = index_ports(ports_after)
        agg_after = aggregate(hops, idx_after)
        t1 = time.time()

        r = reward(agg_before, agg_after, t1 - t0)
        plays[str(choice)] += 1
        q[str(choice)] = q.get(str(choice), 0.0) + (r - q.get(str(choice), 0.0)) / plays[str(choice)]
        print(f"  reward={r:.3f} | q[{choice}]={q[str(choice)]:.3f} | plays={plays[str(choice)]}")

        eps *= DECAY_RATE
        eps = max(0.05, eps)

        save_q(q)
        time.sleep(1.5)

    print("[agent] Done.")
    save_q(q)
    return 0

if __name__ == '__main__':
    sys.exit(main())
