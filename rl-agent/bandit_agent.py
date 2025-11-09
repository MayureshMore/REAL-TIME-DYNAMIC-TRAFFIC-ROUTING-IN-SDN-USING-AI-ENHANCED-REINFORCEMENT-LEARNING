#!/usr/bin/env python3
# rl-agent/bandit_agent.py
#
# Epsilon-greedy over a STABLE top-K path set.
# - Fetch paths once (or refresh only if fewer than K).
# - Map action index -> controller path_id, never use list index as path_id.
# - Gentle 429 backoff with jitter.
# - Keep playing space size == K so exploration actually hits all arms.

import argparse, json, random, sys, time, urllib.request, urllib.error
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
    # returns {(dpid,port_no) -> (rx_bytes, tx_bytes)}
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
    # convert path.hops [{"dpid":1,"out_port":2}, ...] → list of (dpid,out_port)
    hops = path.get("hops") or []
    return [(int(h.get("dpid", 0)), int(h.get("out_port", 0))) for h in hops]

def build_actions(base, src, dst, k, prev_actions=None):
    """
    Returns a stable list of length k:
      actions[i] = {"idx": i, "path_id": <controller id>, "dpids": [...], "hops": [...]}
    If the controller returns < k paths, reuse previous actions when available.
    """
    try:
        paths = get_paths(base, src, dst, k)
    except Exception:
        paths = []

    actions = []
    if isinstance(paths, list):
        for i, p in enumerate(paths[:k]):
            actions.append({
                "idx": i,
                "path_id": p.get("path_id", i),
                "dpids": p.get("dpids", []),
                "hops": p.get("hops", []),
                "raw": p
            })

    # If controller only returned 1 path, try to fall back to previous known actions
    if len(actions) < k and prev_actions:
        # Merge previous to keep action space at size k
        for i in range(len(actions), k):
            if i < len(prev_actions):
                actions.append(prev_actions[i])

    # As a last resort, duplicate the first one (still allows bookkeeping)
    while len(actions) < k and actions:
        actions.append(actions[0])

    return actions

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
    ap.add_argument("--paths_refresh_secs", type=float, default=15.0)
    args = ap.parse_args()

    base = args.controller.rstrip("/")
    _ = get_hosts(base)  # sanity ping
    print(f"[agent] Controller={base} | src={args.src} dst={args.dst}", file=sys.stderr)

    # Q-table for K arms
    K = args.k
    q = [0.0] * K
    plays = [0] * K

    # Build a STABLE action set and refresh periodically
    actions = build_actions(base, args.src, args.dst, K, prev_actions=None)
    if len(actions) < K:
        print(f"[agent] warning: controller returned only {len(actions)} paths; keeping action space at K={K} via cache", file=sys.stderr)
    last_paths_refresh = time.time()

    t0 = time.time()
    step = 0
    rng = random.Random()  # independent RNG

    while time.time() - t0 < args.duration:
        # Periodically refresh available paths, but keep length K
        if time.time() - last_paths_refresh >= args.paths_refresh_secs:
            actions = build_actions(base, args.src, args.dst, K, prev_actions=actions)
            last_paths_refresh = time.time()

        # epsilon-greedy over fixed K actions
        if rng.random() < args.epsilon:
            a_idx = rng.randrange(K)           # 0..K-1, guarantees both arms get sampled
        else:
            a_idx = max(range(K), key=lambda i: q[i])

        chosen = actions[a_idx]
        hop_ports = path_hop_ports(chosen["raw"]) if chosen.get("raw") else []

        # measure tx_bytes before action
        ports_before = ports_to_map(get_ports(base))

        # try to install route using the controller's path_id (NOT the list index)
        try:
            _ = post_route(base, args.src, args.dst, int(chosen["path_id"]), K)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # backoff with jitter; respect Retry-After if present
                ra = e.headers.get("Retry-After")
                if ra:
                    try:
                        wait = max(float(ra), 0.0)
                    except Exception:
                        wait = args.cooldown_ms / 1000.0
                else:
                    wait = args.cooldown_ms / 1000.0
                wait += rng.uniform(0.2, 0.8)
                print("[agent] POST failed: 429 Too Many Requests", file=sys.stderr)
                print(f"[agent] Cooldown active, waiting {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                # do not update Q/plays/step on a failed action; retry next loop
                continue
            else:
                print(f"[agent] route POST error: {e}", file=sys.stderr)
                time.sleep(0.5)
                continue
        except Exception as e:
            print(f"[agent] route POST error: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        # wait a small interval, then re-read to build a crude reward
        time.sleep(args.interval)
        ports_after = ports_to_map(get_ports(base))

        # reward = inverse of avg tx delta across hop out_ports
        deltas = []
        for dpid, outp in hop_ports:
            b = ports_before.get((dpid, outp), (0, 0))[1]
            a_tx = ports_after.get((dpid, outp), (0, 0))[1]
            deltas.append(max(0, a_tx - b))
        avg_tx = mean(deltas) if deltas else 0.0
        reward = 1.0 / (1.0 + avg_tx / 1e6)  # scale a bit

        plays[a_idx] += 1
        # incremental mean update
        q[a_idx] += (reward - q[a_idx]) / plays[a_idx]

        print(f"[t={step}] path_idx={a_idx} path_id={chosen['path_id']} dpids={chosen.get('dpids')} eps={args.epsilon:0.3f}", file=sys.stderr)
        print(f"  reward={reward:0.3f} | q[{a_idx}]={q[a_idx]:0.3f} | plays={plays[a_idx]}", file=sys.stderr)

        # slight epsilon decay, floor at 0.05
        args.epsilon = max(0.05, args.epsilon * 0.99)
        step += 1

    print(json.dumps({"q": q, "plays": plays}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
