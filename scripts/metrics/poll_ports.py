#!/usr/bin/env python3
# scripts/metrics/poll_ports.py
# Polls controller REST and writes per-port counters + derived rates.

import argparse, csv, json, sys, time, urllib.request, urllib.error
from urllib.parse import urlparse

LOCAL_PORT = 4294967294

CANDIDATE_ENDPOINTS = [
    "metrics/ports",   # if available
    "stats/ports",     # Ryu app (this repo)
    "ports",           # legacy
]

def norm_base(u: str) -> str:
    u = (u or "").strip()
    if "://" not in u:
        u = f"http://{u}"
    p = urlparse(u)
    path = p.path or "/api/v1"
    if not path.endswith("/"):
        path += "/"
    return f"{p.scheme}://{p.netloc}{path}"

def get_json(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def flatten(now_ts, payload):
    """
    Returns list of normalized dicts:
      {dpid, port_no, rx_bytes, tx_bytes, rx_packets, tx_packets, rx_dropped, tx_dropped, rx_errors, tx_errors}
    """
    rows = []
    if isinstance(payload, dict) and "ports" in payload:
        payload = payload["ports"]

    if isinstance(payload, dict):  # {"1":[...], "2":[...]}
        for dpid_str, plist in payload.items():
            for p in plist or []:
                r = dict(p)
                r["dpid"] = int(r.get("dpid", int(dpid_str)))
                r["port_no"] = int(r.get("port_no", r.get("port", 0)))
                rows.append(r)
    elif isinstance(payload, list):
        # Either [{"dpid":1,"ports":[...]}, ...] OR flat list
        handled = False
        for block in payload:
            if not isinstance(block, dict): continue
            if "ports" in block and isinstance(block["ports"], list):
                for p in block["ports"]:
                    r = dict(p)
                    r["dpid"] = int(r.get("dpid", block.get("dpid", 0)))
                    r["port_no"] = int(r.get("port_no", r.get("port", 0)))
                    rows.append(r)
                handled = True
        if not handled:
            for p in payload:
                if isinstance(p, dict) and ("port_no" in p or "port" in p):
                    r = dict(p)
                    r["dpid"] = int(r.get("dpid", 0))
                    r["port_no"] = int(r.get("port_no", r.get("port", 0)))
                    rows.append(r)
    return rows

def safe_int(x, default=0):
    try: return int(x)
    except Exception: return default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1",
                    help="Base REST URL (default: %(default)s)")
    ap.add_argument("--outfile", required=True, help="CSV file to append")
    ap.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds")
    ap.add_argument("--duration", type=float, default=120.0, help="Total seconds to run")
    args = ap.parse_args()

    base = norm_base(args.controller)

    header = ["ts","dpid","port",
              "rx_packets","tx_packets","rx_bytes","tx_bytes",
              "rx_dropped","tx_dropped","rx_errors","tx_errors",
              "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
              "drop_ps","err_ps"]

    # open CSV (append) and write header if empty
    try:
        f = open(args.outfile, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(header)
    except Exception as e:
        print(f"[logger] cannot open {args.outfile}: {e}", file=sys.stderr)
        sys.exit(1)

    prev = {}  # (dpid,port) -> {ts, rx_bytes, tx_bytes, rx_drop, tx_drop, rx_err, tx_err}

    start = time.time()
    next_tick = start
    print(f"Logging to {args.outfile}; polling {base} every {args.interval:.1f}s; duration={args.duration:.1f}", file=sys.stderr)
    try:
        while True:
            now = time.time()
            if now - start > args.duration:
                break
            next_tick += args.interval

            ts = now
            payload = None
            last_err = None
            for ep in CANDIDATE_ENDPOINTS:
                try:
                    payload = get_json(f"{base}{ep}")
                    break
                except Exception as e:
                    last_err = e
            if payload is None:
                print(f"[logger] poll failed: {last_err}", file=sys.stderr)
            else:
                for p in flatten(ts, payload):
                    dpid = safe_int(p.get("dpid"))
                    port = safe_int(p.get("port_no", p.get("port", 0)))
                    if port == LOCAL_PORT:
                        continue

                    rx_pkts = safe_int(p.get("rx_packets", p.get("rx_pkts", 0)))
                    tx_pkts = safe_int(p.get("tx_packets", p.get("tx_pkts", 0)))
                    rx_bytes = safe_int(p.get("rx_bytes", 0))
                    tx_bytes = safe_int(p.get("tx_bytes", 0))
                    rx_drop  = safe_int(p.get("rx_dropped", 0))
                    tx_drop  = safe_int(p.get("tx_dropped", 0))
                    rx_err   = safe_int(p.get("rx_errors", 0))
                    tx_err   = safe_int(p.get("tx_errors", 0))

                    key = (dpid, port)
                    prv = prev.get(key)
                    if prv:
                        dt = max(1e-6, ts - prv["ts"])
                        tx_bps = (tx_bytes - prv["tx_bytes"]) * 8.0 / dt
                        rx_bps = (rx_bytes - prv["rx_bytes"]) * 8.0 / dt
                        drop_ps = ((rx_drop - prv["rx_drop"]) + (tx_drop - prv["tx_drop"])) / dt
                        err_ps  = ((rx_err  - prv["rx_err"])  + (tx_err  - prv["tx_err"]))  / dt
                    else:
                        tx_bps = rx_bps = drop_ps = err_ps = 0.0

                    prev[key] = {"ts": ts, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes,
                                 "rx_drop": rx_drop, "tx_drop": tx_drop,
                                 "rx_err": rx_err, "tx_err": tx_err}

                    writer.writerow([ts, dpid, port,
                                     rx_pkts, tx_pkts, rx_bytes, tx_bytes,
                                     rx_drop, tx_drop, rx_err, tx_err,
                                     rx_bps, tx_bps, rx_bps/1e6, tx_bps/1e6,
                                     drop_ps, err_ps])
                f.flush()

            time.sleep(max(0.0, next_tick - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        f.close()

if __name__ == "__main__":
    main()
