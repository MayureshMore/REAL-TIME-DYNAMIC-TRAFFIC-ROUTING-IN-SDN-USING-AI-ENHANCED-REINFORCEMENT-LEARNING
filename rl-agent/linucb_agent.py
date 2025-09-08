# rl-agent/linucb_agent.py
# LinUCB contextual bandit for path selection with simple anomaly-aware filtering.

import argparse
import time
import math
import random
import requests
from collections import defaultdict


def api_base(host, port):
    return f"http://{host}:{port}/api/v1"


def get_hosts(base):
    r = requests.get(f"{base}/hosts", timeout=5); r.raise_for_status(); return r.json()


def get_ports(base):
    r = requests.get(f"{base}/stats/ports", timeout=5); r.raise_for_status(); return r.json()


def get_paths(base, src_mac, dst_mac, k):
    r = requests.get(f"{base}/paths", params={'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}, timeout=8)
    r.raise_for_status(); return r.json()


def post_route(base, src_mac, dst_mac, path_id=None, path=None, k=2):
    payload = {'src_mac': src_mac, 'dst_mac': dst_mac, 'k': k}
    if path_id is not None: payload['path_id'] = path_id
    if path is not None: payload['path'] = path
    r = requests.post(f"{base}/actions/route", json=payload, timeout=8)
    r.raise_for_status(); return r.json()


def index_ports(port_snapshot):
    idx = defaultdict(dict)
    for p in port_snapshot:
        idx[p['dpid']][p['port_no']] = p
    return idx


def path_features(hops, prev_idx, cur_idx, dt):
    """Return context vector x and anomaly score for a path.
    x = [tx_bps, rx_bps, err_rate, drop_rate, hops, 1]
    """
    if dt <= 0: dt = 1e-6
    tx_prev = rx_prev = err_prev = drop_prev = 0
    tx_cur  = rx_cur  = err_cur  = drop_cur  = 0
    for h in hops:
        p0 = prev_idx.get(h['dpid'], {}).get(h['out_port'])
        p1 = cur_idx.get(h['dpid'], {}).get(h['out_port'])
        if p0 and p1:
            tx_prev += p0.get('tx_bytes', 0); tx_cur += p1.get('tx_bytes', 0)
            rx_prev += p0.get('rx_bytes', 0); rx_cur += p1.get('rx_bytes', 0)
            err_prev += p0.get('rx_errors', 0) + p0.get('tx_errors', 0)
            err_cur  += p1.get('rx_errors', 0) + p1.get('tx_errors', 0)
            drop_prev += p0.get('rx_dropped', 0) + p0.get('tx_dropped', 0)
            drop_cur  += p1.get('rx_dropped', 0) + p1.get('tx_dropped', 0)
    tx_bps = max(0.0, (tx_cur - tx_prev) * 8.0 / dt)
    rx_bps = max(0.0, (rx_cur - rx_prev) * 8.0 / dt)
    err_rate = max(0.0, (err_cur - err_prev) / dt)
    drop_rate = max(0.0, (drop_cur - drop_prev) / dt)
    x = [tx_bps, rx_bps, err_rate, drop_rate, float(len(hops)), 1.0]
    anomaly = (err_rate + drop_rate)
    return x, anomaly


class LinUCB:
    def __init__(self, d, alpha=1.0):
        self.d = d
        self.alpha = alpha
        self.A = defaultdict(lambda: self._I())     # path_id -> A (dxd)
        self.b = defaultdict(lambda: [0.0] * d)     # path_id -> b (d)

    def _I(self):
        I = [[0.0] * self.d for _ in range(self.d)]
        for i in range(self.d):
            I[i][i] = 1.0
        return I

    def _Ainv(self, A):
        # Basic Gauss-Jordan for tiny matrices; replace with numpy for scale
        n = self.d
        aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
        for i in range(n):
            pivot = aug[i][i]
            if abs(pivot) < 1e-12:
                pivot = 1e-6
            for j in range(2 * n):
                aug[i][j] /= pivot
            for k in range(n):
                if k == i:
                    continue
                factor = aug[k][i]
                for j in range(2 * n):
                    aug[k][j] -= factor * aug[i][j]
        return [row[n:] for row in aug]

    def _matvec(self, M, v):
        return [sum(M[i][j] * v[j] for j in range(self.d)) for i in range(self.d)]

    def _quad(self, v, M):
        tmp = self._matvec(M, v)
        return sum(v[i] * tmp[i] for i in range(self.d))

    def predict_ucb(self, path_id, x):
        A = self.A[path_id]
        b = self.b[path_id]
        Ainv = self._Ainv(A)
        theta = self._matvec(Ainv, b)
        mu = sum(theta[i] * x[i] for i in range(self.d))
        bonus = self.alpha * math.sqrt(max(1e-12, self._quad(x, Ainv)))
        return mu + bonus

    def update(self, path_id, x, r):
        A = self.A[path_id]
        for i in range(self.d):
            for j in range(self.d):
                A[i][j] += x[i] * x[j]
        b = self.b[path_id]
        for i in range(self.d):
            b[i] += r * x[i]
        self.A[path_id] = A
        self.b[path_id] = b


def reward_from_deltas(prev_idx, cur_idx, hops, dt):
    if dt <= 0: dt = 1e-6
    # Use Δtx_bytes/s across the path minus error/drop penalties
    x_now, anomaly = path_features(hops, prev_idx, cur_idx, dt)
    tx_bps = x_now[0]
    penalty = 8000.0 * anomaly
    return tx_bps - penalty


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--k', type=int, default=2)
    ap.add_argument('--epsilon', type=float, default=0.1)
    ap.add_argument('--alpha', type=float, default=1.0)
    ap.add_argument('--trials', type=int, default=10)
    ap.add_argument('--err_thresh', type=float, default=5.0, help='errors/sec threshold to mask paths')
    args = ap.parse_args()

    base = api_base(args.controller, args.port)
    hosts = get_hosts(base)
    if len(hosts) < 2:
        raise SystemExit('Need at least 2 hosts learned (run pingall)')
    src_mac, dst_mac = hosts[0]['mac'], hosts[1]['mac']

    linucb = LinUCB(d=6, alpha=args.alpha)

    prev_ports = get_ports(base)
    prev_idx = index_ports(prev_ports)
    prev_t = time.time()

    for t in range(args.trials):
        time.sleep(2.0)
        cur_ports = get_ports(base)
        cur_idx = index_ports(cur_ports)
        now = time.time()
        dt = now - prev_t

        paths = get_paths(base, src_mac, dst_mac, k=args.k)
        if not paths:
            print('No paths available; retrying...')
            prev_ports, prev_idx, prev_t = cur_ports, cur_idx, now
            continue

        # Build contexts + filter anomalous paths
        arms = []
        for i, p in enumerate(paths):
            x, anomaly = path_features(p['hops'], prev_idx, cur_idx, dt)
            if anomaly > args.err_thresh:
                continue  # skip noisy path
            arms.append((i, x, p))
        if not arms:
            arms = [(i, path_features(p['hops'], prev_idx, cur_idx, dt)[0], p) for i, p in enumerate(paths)]

        # epsilon-greedy over LinUCB scores
        if random.random() < args.epsilon:
            choice = random.randrange(len(arms))
        else:
            scores = [linucb.predict_ucb(i, x) for (i, x, _) in arms]
            choice = max(range(len(arms)), key=lambda j: scores[j])
        path_id, x, chosen = arms[choice]
        print(f"[t={t}] choose path_id={path_id} dpids={chosen['dpids']}")
        post_route(base, src_mac, dst_mac, path_id=path_id, k=args.k)

        # Observe reward and update
        time.sleep(3.0)
        new_ports = get_ports(base)
        new_idx = index_ports(new_ports)
        r = reward_from_deltas(prev_idx, new_idx, chosen['hops'], dt=3.0)
        linucb.update(path_id, x, r)
        print(f"  reward≈{r:.2f} | updated")

        prev_ports, prev_idx, prev_t = new_ports, new_idx, time.time()

    print("LinUCB finished.")


if __name__ == '__main__':
    main()
