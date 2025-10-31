#!/usr/bin/env python3
# LinUCB Contextual Bandit Agent for SDN Path Selection
# -----------------------------------------------------
# Improvements:
# - Ridge regularization (λI) for numerical stability
# - Anomaly filtering based on drop/error rate
# - Normalized feature vectors
# - Uses NumPy for clean math
# - Compatible with Bandit and DQN agents

import argparse, time, random, math, requests, sys
import numpy as np
from collections import defaultdict

def api_base(host, port): return f"http://{host}:{port}/api/v1"

def safe_get(url, timeout=6):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[linucb] GET failed:", e)
        return None

def safe_post(url, payload, timeout=8):
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[linucb] POST failed:", e)
        return None

def get_hosts(base): return safe_get(f"{base}/hosts") or []
def get_ports(base): return safe_get(f"{base}/stats/ports") or []
def get_paths(base, src, dst, k): return safe_get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}") or []
def post_route(base, src, dst, path_id=None, path=None, k=2):
    payload={'src_mac':src,'dst_mac':dst,'k':k}
    if path_id is not None: payload['path_id']=int(path_id)
    if path is not None: payload['path']=list(path)
    return safe_post(f"{base}/actions/route", payload)

def index_ports(snapshot):
    idx=defaultdict(dict)
    for p in snapshot:
        try: idx[int(p['dpid'])][int(p['port_no'])]=p
        except Exception: pass
    return idx

def path_features(hops, prev_idx, cur_idx, dt):
    """Return normalized features and anomaly score"""
    if dt <= 0: dt = 1e-6
    tx0 = rx0 = e0 = d0 = 0.0
    tx1 = rx1 = e1 = d1 = 0.0
    for h in hops:
        dpid = int(h.get("dpid", -1))
        outp = int(h.get("out_port", -1))
        p0 = prev_idx.get(dpid, {}).get(outp)
        p1 = cur_idx.get(dpid, {}).get(outp)
        if not p0 or not p1:
            continue
        tx0 += p0.get("tx_bytes", 0); tx1 += p1.get("tx_bytes", 0)
        rx0 += p0.get("rx_bytes", 0); rx1 += p1.get("rx_bytes", 0)
        e0 += p0.get("rx_errors", 0) + p0.get("tx_errors", 0)
        e1 += p1.get("rx_errors", 0) + p1.get("tx_errors", 0)
        d0 += p0.get("rx_dropped", 0) + p0.get("tx_dropped", 0)
        d1 += p1.get("rx_dropped", 0) + p1.get("tx_dropped", 0)

    tx_bps = max(0.0, (tx1 - tx0) * 8.0 / dt)
    rx_bps = max(0.0, (rx1 - rx0) * 8.0 / dt)
    err_rate = max(0.0, (e1 - e0) / dt)
    drop_rate = max(0.0, (d1 - d0) / dt)

    # normalize values to avoid dominance
    scale = 1e7
    x = np.array([
        tx_bps / scale,
        rx_bps / scale,
        err_rate / 100.0,
        drop_rate / 100.0,
        len(hops) / 10.0,
        1.0
    ])
    anomaly = err_rate + drop_rate
    return x, anomaly

def reward_from_deltas(prev_idx, cur_idx, hops, dt):
    """Reward = throughput - error penalty (normalized)"""
    if dt <= 0: dt = 1e-6
    tx_bps = 0.0
    drop = err = 0.0
    for h in hops:
        dpid = int(h["dpid"]); outp = int(h["out_port"])
        p0 = prev_idx.get(dpid, {}).get(outp)
        p1 = cur_idx.get(dpid, {}).get(outp)
        if not p0 or not p1: continue
        tx_bps += (p1.get("tx_bytes", 0) - p0.get("tx_bytes", 0)) * 8.0 / dt
        drop += (p1.get("tx_dropped", 0) + p1.get("rx_dropped", 0)) - (p0.get("tx_dropped", 0) + p0.get("rx_dropped", 0))
        err  += (p1.get("tx_errors", 0) + p1.get("rx_errors", 0)) - (p0.get("tx_errors", 0) + p0.get("rx_errors", 0))
    MAX_BW = 10_000_000.0
    r = (tx_bps / MAX_BW) - 0.0001 * (drop + err)
    return float(np.clip(r, -1.0, 1.0))

class LinUCB:
    def __init__(self, d, alpha=1.0, lam=1.0):
        self.d = d
        self.alpha = alpha
        self.lam = lam
        self.A = defaultdict(lambda: np.eye(d) * lam)
        self.b = defaultdict(lambda: np.zeros((d, 1)))

    def predict_ucb(self, arm, x):
        A = self.A[arm]; b = self.b[arm]
        A_inv = np.linalg.inv(A)
        theta = A_inv @ b
        mu = float(np.dot(x, theta))
        bonus = self.alpha * math.sqrt(max(1e-12, np.dot(x, A_inv @ x)))
        return mu + bonus

    def update(self, arm, x, r):
        x = x.reshape(-1, 1)
        self.A[arm] += x @ x.T
        self.b[arm] += r * x

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--lambda_", type=float, default=1.0)
    ap.add_argument("--epsilon", type=float, default=0.1)
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--err_thresh", type=float, default=5.0)
    args = ap.parse_args()

    base = api_base(args.controller, args.port)
    hosts = get_hosts(base)
    if len(hosts) < 2:
        print("[linucb] Not enough hosts learned.")
        sys.exit(1)
    src, dst = hosts[0]["mac"], hosts[1]["mac"]
    lin = LinUCB(d=6, alpha=args.alpha, lam=args.lambda_)

    prev_ports = get_ports(base)
    prev_idx = index_ports(prev_ports)
    prev_t = time.time()

    for t in range(args.trials):
        time.sleep(2)
        cur_ports = get_ports(base)
        cur_idx = index_ports(cur_ports)
        now = time.time()
        dt = now - prev_t
        paths = get_paths(base, src, dst, args.k)
        if not paths:
            print("[linucb] No paths available; retrying...")
            prev_t = now
            continue

        arms = []
        for i, p in enumerate(paths):
            x, anomaly = path_features(p.get("hops", []), prev_idx, cur_idx, dt)
            if anomaly <= args.err_thresh:
                arms.append((i, x, p))
        if not arms:
            arms = [(i, path_features(p.get("hops", []), prev_idx, cur_idx, dt)[0], p) for i, p in enumerate(paths)]

        # epsilon exploration
        if random.random() < args.epsilon:
            j = random.randrange(len(arms))
        else:
            scores = [lin.predict_ucb(i, x) for (i, x, _) in arms]
            j = int(np.argmax(scores))
        pid, x, chosen = arms[j]

        print(f"[t={t}] choose path_id={pid} dpids={chosen.get('dpids')}")
        post_route(base, src, dst, path_id=pid, k=args.k)

        time.sleep(3)
        new_ports = get_ports(base)
        new_idx = index_ports(new_ports)
        r = reward_from_deltas(prev_idx, new_idx, chosen.get("hops", []), 3.0)
        lin.update(pid, x, r)
        print(f"  reward={r:.3f}")
        prev_idx = new_idx
        prev_t = time.time()

    print("[linucb] finished.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
