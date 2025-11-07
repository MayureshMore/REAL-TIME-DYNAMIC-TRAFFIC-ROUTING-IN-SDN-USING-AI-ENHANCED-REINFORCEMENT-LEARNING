#!/usr/bin/env python3
"""
Epsilon-greedy bandit agent for path selection.

Fixes vs. old version
- Honors HTTP 429 Retry-After (header or JSON) with real sleep.
- Does not advance t/decay epsilon on 429 (no-op step).
- Fetches k-paths once and caches mapping path_id -> dpids.
- Logs compact JSON of q-values and play counts at the end.
- Safer reward function (uses deltas of drops/errors + throughput).
"""

import argparse
import json
import random
import time
from math import exp
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

def http_get(url, timeout=3):
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read(), r.headers

def http_post(url, payload, timeout=3):
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read(), r.headers
    except HTTPError as e:
        return e.code, e.read(), getattr(e, "headers", {})
    except URLError as e:
        raise e

def parse_retry_after(headers, body_bytes):
    # Priority: HTTP header, then JSON field
    if headers:
        ra = headers.get("Retry-After")
        if ra:
            try:
                return max(1, int(float(ra)))
            except Exception:
                pass
    try:
        b = json.loads(body_bytes.decode("utf-8"))
        ra = b.get("retry_after")
        if ra is not None:
            return max(1, int(float(ra)))
    except Exception:
        pass
    return 1

def get_paths(base, src, dst, k):
    code, body, _ = http_get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}")
    if code != 200:
        raise RuntimeError(f"paths GET failed: {code} {body[:120]}")
    paths = json.loads(body.decode("utf-8"))
    # Normalize to {pid: dpids}
    out = {}
    for p in paths:
        out[p["path_id"]] = p["dpids"]
    return out

def get_port_stats(base):
    code, body, _ = http_get(f"{base}/stats/ports")
    if code != 200:
        return []
    return json.loads(body.decode("utf-8"))

def reward_from_stats(prev, curr):
    """Higher is better. Combines throughput and (negative) drops/errors."""
    if prev is None or not prev or not curr:
        return 1.0
    # Build keyed dicts for deltas
    pk = {(r["dpid"], r["port_no"]): r for r in prev}
    ck = {(r["dpid"], r["port_no"]): r for r in curr}
    d_tx_bytes = 0
    d_drops = 0
    d_err = 0
    for k, c in ck.items():
        p = pk.get(k)
        if not p:
            continue
        d_tx_bytes += max(0, c["tx_bytes"] - p["tx_bytes"])
        d_drops += max(0, (c["rx_dropped"] - p["rx_dropped"]) + (c["tx_dropped"] - p["tx_dropped"]))
        d_err += max(0, (c["rx_errors"] - p["rx_errors"]) + (c["tx_errors"] - p["tx_errors"]))
    # Normalize
    th = 1.0 if d_tx_bytes <= 0 else 1.0
    penalty = 0.0
    if d_drops > 0 or d_err > 0:
        penalty = min(0.25 + 0.25 * (d_drops + d_err) / 1000.0, 0.75)
    return max(0.25, min(1.0, th - penalty))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080/api/v1", help="REST base")
    ap.add_argument("--src", required=True, help="src MAC")
    ap.add_argument("--dst", required=True, help="dst MAC")
    ap.add_argument("--k", type=int, default=2, help="number of paths to consider")
    ap.add_argument("--epsilon", type=float, default=0.2)
    ap.add_argument("--steps", type=int, default=120)
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between decisions")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    print(f"[agent] Controller={base} | src={args.src} dst={args.dst}")

    # Fetch available paths
    pid_to_path = get_paths(base, args.src, args.dst, args.k)
    if not pid_to_path:
        raise SystemExit("No paths available from controller.")
    pids = sorted(pid_to_path.keys())

    # Q/plays
    q = {pid: 0.0 for pid in pids}
    plays = {pid: 0 for pid in pids}

    prev_stats = None
    t = 0
    eps = args.epsilon

    while t < args.steps:
        # Choose action
        explore = random.random() < eps
        if explore:
            a = random.choice(pids)
        else:
            # break ties by smallest pid for determinism
            a = max(pids, key=lambda pid: (q[pid], -pid))

        dpids = pid_to_path[a]
        print(f"[t={t}] path_id={a} dpids={dpids} eps={eps:.3f}")

        # Try to apply route
        payload = {"src_mac": args.src, "dst_mac": args.dst, "path_id": int(a), "k": len(pids)}
        code, body, headers = http_post(f"{base}/actions/route", payload)

        if code == 429:
            wait_s = parse_retry_after(headers, body)
            print(f"[agent] POST failed: 429 Too Many Requests")
            print(f"[agent] Cooldown active, sleeping {wait_s}s (honoring Retry-After)")
            time.sleep(wait_s)
            # Do NOT advance timestep or decay epsilon
            # (also do not update reward/q)
            continue
        elif code != 200:
            print(f"[agent] POST failed: {code} {body[:120]}")
            time.sleep(max(1.0, args.sleep))
            continue

        # Small settle time
        time.sleep(max(0.2, args.sleep * 0.2))

        # Measure reward
        curr_stats = get_port_stats(base)
        r = reward_from_stats(prev_stats, curr_stats)
        prev_stats = curr_stats

        # Incremental mean update
        plays[a] += 1
        q[a] = q[a] + (r - q[a]) / plays[a]

        print(f"  reward={r:.3f} | q[{a}]={q[a]:.3f} | plays={plays[a]}")

        # Advance
        t += 1
        # Soft decay
        eps = max(0.02, eps * 0.99)

        # Sleep remainder
        time.sleep(max(0.0, args.sleep - 0.2))

    # Final line for scripts to parse
    qlist = [q.get(pid, 0.0) for pid in pids]
    plist = [plays.get(pid, 0) for pid in pids]
    print(json.dumps({"q": qlist, "plays": plist}))

if __name__ == "__main__":
    main()
