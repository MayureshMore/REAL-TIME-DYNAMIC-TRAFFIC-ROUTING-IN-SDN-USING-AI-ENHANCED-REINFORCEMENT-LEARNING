#!/usr/bin/env python3
# scripts/agents/bandit_agent.py
#
# Minimal epsilon-greedy bandit that flips between k paths from the controller:
#   GET  /api/v1/paths?src_mac=..&dst_mac=..&k=K
#   POST /api/v1/actions/route {"src_mac","dst_mac","path_id","k"}
# Reward proxy: inverse of average tx_bytes increase per hop over the sample window
# (higher reward → lighter path). This is simplistic but works as a demo.

import argparse, json, random, sys, time, urllib.request, urllib.error
from statistics import mean

def jget(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def jpost(url, payload, timeout=3.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def safe_get(url, retries=3, backoff=0.5):
    last=None
    for i in range(retries):
        try:
            return jget(url)
        except Exception as e:
            last=e
            time.sleep(backoff*(i+1))
    raise last

def get_hosts(base):
    return safe_get(f"{base}/hosts")

def get_paths(base, src, dst, k):
    return safe_get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}")

def get_ports(base):
    # used to estimate a crude reward
    # try a few REST shapes relative to base (which already includes /api/v1)
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
    m={}
    if isinstance(payload, dict) and "ports" in payload:
        payload = payload["ports"]
    if isinstance(payload, dict):
        for dpid, plist in payload.items():
            for p in plist or []:
                d=int(p.get("dpid", int(dpid)))
                po=int(p.get("port_no", p.get("port", 0)))
                m[(d,po)] = (int(p.get("rx_bytes",0)), int(p.get("tx_bytes",0)))
    elif isinstance(payload, list):
        for p in payload:
            d=int(p.get("dpid",0))
            po=int(p.get("port_no", p.get("port", 0)))
            m[(d,po)] = (int(p.get("rx_bytes",0)), int(p.get("tx_bytes",0)))
    return m

def path_hop_ports(path):
    # convert path.hops [{"dpid":1,"out_port":2}, ...] → list of (dpid,out_port)
    hops = path.get("hops") or []
    return [(int(h.get("dpid",0)), int(h.get("out_port",0))) for h in hops]

def _extract_retry_after_from_body(e):
    try:
        # urllib.error.HTTPError is also a file-like object
        payload = e.read().decode("utf-8")
        data = json.loads(payload)
        return int(data.get("retry_after", 0))
    except Exception:
        return 0

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
    args = ap.parse_args()

    base = args.controller.rstrip("/")
    hosts = get_hosts(base)
    print(f"[agent] Controller={base} | src={args.src} dst={args.dst}", file=sys.stderr)

    # Q-table for K arms
    q = [0.0]*args.k
    plays = [0]*args.k
    t0=time.time()
    t=0
    while time.time()-t0 < args.duration:
        # fetch available paths
        try:
            paths = get_paths(base, args.src, args.dst, args.k)
        except Exception as e:
            print("[agent] paths not available; retrying...", file=sys.stderr)
            time.sleep(1.0)
            continue
        if not isinstance(paths, list) or len(paths)==0:
            print("[agent] paths not available; retrying...", file=sys.stderr)
            time.sleep(1.0); continue

        # epsilon-greedy
        explore = (random.random() < args.epsilon)
        if explore:
            a = random.randrange(min(len(paths), args.k))
        else:
            a = max(range(min(len(paths), args.k)), key=lambda i: q[i])

        # measure tx_bytes before action on chosen hop ports
        ports_before = ports_to_map(get_ports(base))
        chosen = paths[a]
        hop_ports = path_hop_ports(chosen)

        # try to install route
        try:
            resp = post_route(base, args.src, args.dst, a, args.k)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Prefer HTTP header; fallback to JSON body; otherwise to CLI flag
                header_retry = int(e.headers.get("Retry-After", "0") or 0)
                body_retry = _extract_retry_after_from_body(e)
                wait = max(header_retry, body_retry, args.cooldown_ms/1000.0)
                print("[agent] POST failed: 429 Too Many Requests", file=sys.stderr)
                print(f"[agent] Cooldown active, waiting {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                # do not advance time step / epsilon on a rejected switch
                # just retry loop after wait
                continue
            else:
                print(f"[agent] route POST error: {e}", file=sys.stderr)
                time.sleep(0.5); continue
        except Exception as e:
            print(f"[agent] route POST error: {e}", file=sys.stderr)
            time.sleep(0.5); continue

        # wait a small interval, then re-read to build a crude reward
        time.sleep(args.interval)
        ports_after = ports_to_map(get_ports(base))

        # reward = inverse of avg tx delta across hop out_ports
        deltas=[]
        for dpid, outp in hop_ports:
            b = ports_before.get((dpid,outp), (0,0))[1]
            a_tx = ports_after.get((dpid,outp), (0,0))[1]
            deltas.append(max(0, a_tx - b))
        avg_tx = mean(deltas) if deltas else 0.0
        reward = 1.0 / (1.0 + avg_tx/1e6)  # scale a bit

        plays[a] += 1
        q[a] += (reward - q[a]) / plays[a]

        print(f"[t={t}] path_id={a} dpids={chosen.get('dpids')} eps={args.epsilon:0.3f}", file=sys.stderr)
        print(f"  reward={reward:0.3f} | q[{a}]={q[a]:0.3f} | plays={plays[a]}", file=sys.stderr)

        # slight epsilon decay
        args.epsilon = max(0.05, args.epsilon * 0.99)
        t += 1

    # summary
    print(json.dumps({"q": q, "plays": plays}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
