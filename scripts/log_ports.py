#!/usr/bin/env python3
"""
Poll /api/v1/stats/ports and write CSV.

Fixes
- Writes header once.
- Dedup by (dpid, port, ts) second to avoid noisy double prints.
- Accepts --every (poll interval) and --duration seconds.
"""

import argparse
import csv
import json
import sys
import time
from urllib.request import Request, urlopen

FIELDS = [
    "ts",
    "dpid", "port_no",
    "rx_bytes", "tx_bytes",
    "rx_packets", "tx_packets",
    "rx_dropped", "tx_dropped",
    "rx_errors", "tx_errors",
]

def get_ports(url, timeout=3):
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080/api/v1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--every", type=float, default=1.0)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    url = f"{base}/stats/ports"

    seen = set()  # (dpid, port, ts_rounded)

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)

        t0 = time.time()
        t_end = t0 + args.duration
        print(f"Logging to {args.out}; polling {url} every {args.every}s; duration={args.duration}")

        while time.time() < t_end:
            ts = time.time()
            try:
                rows = get_ports(url)
            except Exception as e:
                print(f"[logger] fetch error: {e}", file=sys.stderr)
                time.sleep(args.every)
                continue

            ts_round = int(ts)  # second precision to dedup
            for r in rows:
                key = (r["dpid"], r["port_no"], ts_round)
                if key in seen:
                    continue
                seen.add(key)
                w.writerow([
                    f"{ts:.6f}",
                    r["dpid"], r["port_no"],
                    r["rx_bytes"], r["tx_bytes"],
                    r["rx_packets"], r["tx_packets"],
                    r["rx_dropped"], r["tx_dropped"],
                    r["rx_errors"], r["tx_errors"],
                ])
            f.flush()
            time.sleep(args.every)

if __name__ == "__main__":
    main()
