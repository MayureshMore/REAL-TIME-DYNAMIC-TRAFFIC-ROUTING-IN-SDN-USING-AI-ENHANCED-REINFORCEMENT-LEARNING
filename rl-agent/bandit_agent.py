#!/usr/bin/env python3
# rl-agent/bandit_agent.py
#
# Minimal epsilon-greedy bandit that flips between k paths from the controller:
#   GET  /api/v1/paths?src_mac=..&dst_mac=..&k=K
#   POST /api/v1/actions/route {"src_mac","dst_mac","path_id","k"}
#
# Key improvements vs previous version:
# - Idempotency on the client side: only POST when the chosen path changes
#   or after a small TTL (REAPPLY_TTL). This avoids 429s from duplicate installs.
# - Honors Retry-After (if present) and uses sensible backoff otherwise.
# - Slightly slower epsilon decay with a floor so short demos actually explore.

import argparse
import json
import random
import sys
import time
import urllib.request
import urllib.error
from time import monotonic
from statistics import mean


def jget(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def jpost(url, payload, timeout=3.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def safe_get(url, retries=3, backoff=0.5):
    last = None
    for i in range(retries):
        try:
            return jget(url)
        except Exception as e:
            last = e
            time.sleep(backoff * (i + 1))
    raise last


def get_hosts(base):
    return safe_get(f"{base}/hosts")


def get_paths(base, src, dst, k):
    return safe_get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}")


def get_ports(base):
    # Try a few REST shapes relative to base (which already includes /api/v1)
    for ep in ("stats/ports", "metrics/ports", "ports"):
        url = f"{base.rstrip('/')}/{ep}"
        try:
            return jget(url)
        except Exception:
            pass
    return []


def post_route(base, src, dst, path_id, k):
    return jpost(f"{base}/actions/route", {
        "src_mac": src, "dst_mac": dst, "path_id": path_id, "k": k
    })


def ports_to_map(payload):
    """
    Normalize ports payload into {(dpid,port_no) -> (rx_bytes, tx_bytes)}.
    Accepts either:
      {"ports": {"1":[{...}], "2":[...], ...}}  or
      {"1":[{...}], "2":[...]}  or
      [{dpid, port_no/port, rx_bytes, tx_bytes}, ...]
    """
    m = {}
    if isinstance(payload, dict) and "ports" in payload:
        payload = payload["ports"]
    if isinstance(payload, dict):
        for dpid, plist in payload.items():
            for p in plist or []:
                d = int(p.get("dpid", int(dpid)))
                po = int(p.get("port_no", p.get("port", 0)))
                m[(d, po)] = (int(p.get("rx_bytes", 0)), int(p.get("tx_bytes", 0)))
    elif isinstance(payload, list):
        for p in payload:
            d = int(p.get("dpid", 0))
            po = int(p.get("port_no", p.get("port", 0)))
            m[(d, po)] = (int(p.get("rx_bytes", 0)), int(p.get("tx_bytes", 0)))
    return m


def path_hop_ports(path):
    # Convert path.hops [{"dpid":1,"out_port":2}, ...] → list of (dpid,out_port)
    hops = path.get("hops") or []
    return [(int(h.get("dpid", 0)), int(h.get("out_port", 0))) for h in hops]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1")
    ap.add_argument("--epsilon", type=float, default=0.2)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--src", required=True, help="src MAC")
    ap.add_argument("--dst", required=True, help="dst MAC")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--cooldown_ms", type=int, default=300)
    ap.add_argument("--reapply_ttl", type=float, default=5.0, help="seconds before reapplying the same path")
    args = ap.parse_args()

    base = args.controller.rstrip("/")
    _ = get_hosts(base)  # warm-up / verify controller is reachable
    print(f"[agent] Controller={base} | src={args.src} dst={args.dst}", file=sys.stderr)

    # Q-table for K arms
    q = [0.0] * args.k
    plays = [0] * args.k

    last_applied_idx = None
    last_apply_ts = 0.0
    REAPPLY_TTL = float(args.reapply_ttl)

    t0 = time.time()
    t = 0
    while time.time() - t0 < args.duration:
        # fetch available paths
        try:
            paths = get_paths(base, args.src, args.dst, args.k)
        except Exception:
            print("[agent] paths not available; retrying...", file=sys.stderr)
            time.sleep(1.0)
            continue
        if not isinstance(paths, list) or len(paths) == 0:
            print("[agent] paths not available; retrying...", file=sys.stderr)
            time.sleep(1.0)
            continue

        if t == 0:
            print(f"[agent] k={args.k} | paths_returned={len(paths)}", file=sys.stderr)

        arms = min(len(paths), args.k)

        # epsilon-greedy
        explore = (random.random() < args.epsilon)
        if explore:
            a = random.randrange(arms)
        else:
            a = max(range(arms), key=lambda i: q[i])

        chosen = paths[a]

        # Decide whether we actually need to POST (avoid duplicate spam)
        now = monotonic()
        need_post = (last_applied_idx is None) or (a != last_applied_idx) or ((now - last_apply_ts) >= REAPPLY_TTL)

        # measure tx before (we still measure even if we don't POST so we can form a reward sample)
        ports_before = ports_to_map(get_ports(base))

        # Try to install route only if needed
        if need_post:
            try:
                _ = post_route(base, args.src, args.dst, a, arms)
                last_applied_idx = a
                last_apply_ts = now
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after_hdr = e.headers.get("Retry-After")
                    if retry_after_hdr:
                        try:
                            wait = float(retry_after_hdr)
                        except ValueError:
                            wait = max(args.cooldown_ms / 1000.0, 1.0)
                    else:
                        wait = max(args.cooldown_ms / 1000.0, 1.0)
                    print("[agent] POST failed: 429 Too Many Requests", file=sys.stderr)
                    print(f"[agent] Cooldown active, waiting {wait:.1f}s", file=sys.stderr)
                    time.sleep(wait)
                    # Retry on next loop without incrementing t (duration wall-clock governs exit)
                    continue
                else:
                    print(f"[agent] route POST error: {e}", file=sys.stderr)
                    time.sleep(0.5)
                    continue
            except Exception as e:
                print(f"[agent] route POST error: {e}", file=sys.stderr)
                time.sleep(0.5)
                continue

        # wait interval, then sample reward
        time.sleep(args.interval)
        ports_after = ports_to_map(get_ports(base))

        # reward = inverse of avg tx delta across hop out_ports
        hop_ports = path_hop_ports(chosen)
        deltas = []
        for dpid, outp in hop_ports:
            b = ports_before.get((dpid, outp), (0, 0))[1]
            a_tx = ports_after.get((dpid, outp), (0, 0))[1]
            deltas.append(max(0, a_tx - b))
        avg_tx = mean(deltas) if deltas else 0.0
        reward = 1.0 / (1.0 + avg_tx / 1e6)  # scale

        plays[a] += 1
        q[a] += (reward - q[a]) / plays[a]

        print(
            f"[t={t}] path_idx={a} path_id={chosen.get('path_id', a)} "
            f"dpids={chosen.get('dpids')} eps={args.epsilon:0.3f}",
            file=sys.stderr,
        )
        print(f"  reward={reward:0.3f} | q[{a}]={q[a]:0.3f} | plays={plays[a]}", file=sys.stderr)

        # slower decay with a floor so we keep exploring in short demos
        args.epsilon = max(0.10, args.epsilon * 0.995)
        t += 1

    # summary
    print(json.dumps({"q": q, "plays": plays}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
