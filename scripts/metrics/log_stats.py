#!/usr/bin/env python3
# scripts/metrics/log_stats.py
# Poll controller REST endpoints and write CSV with counters AND per-second rates.
# Supports a fixed --duration to auto-stop.

import argparse
import csv
import time
import requests
import os
from datetime import datetime
from collections import defaultdict

def fetch_json(url, timeout=3):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--controller', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--interval', type=float, default=2.0)
    p.add_argument('--duration', type=float, default=None, help='Stop after N seconds')
    p.add_argument('--out', default=None, help='CSV path (default: docs/baseline/metrics_<ts>.csv)')
    args = p.parse_args()

    base = f"http://{args.controller}:{args.port}/api/v1"
    out = args.out or os.path.join("docs", "baseline", f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    ensure_dir(out)

    fields = [
        'ts', 'dpid', 'port_no',
        'rx_bytes', 'tx_bytes', 'rx_pkts', 'tx_pkts', 'rx_dropped', 'tx_dropped', 'rx_errors', 'tx_errors',
        'rx_bps', 'tx_bps', 'rx_pps', 'tx_pps', 'drop_ps', 'err_ps'
    ]

    # previous snapshot to compute deltas
    prev = {}  # key: (dpid,port) -> dict(stats) + ts

    print(f"Logging to {out}; polling {base} every {args.interval}s; duration={args.duration or '∞'}")
    start = time.time()
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        while True:
            now = time.time()
            try:
                ports = fetch_json(f"{base}/stats/ports")
            except Exception as e:
                print("Fetch error:", e)
                time.sleep(args.interval)
                continue

            for pstat in ports:
                key = (pstat.get('dpid'), pstat.get('port_no'))
                ts_prev = prev.get(key, {}).get('ts', now)
                dt = max(1e-6, now - ts_prev)

                rxB_prev = prev.get(key, {}).get('rx_bytes', 0)
                txB_prev = prev.get(key, {}).get('tx_bytes', 0)
                rxP_prev = prev.get(key, {}).get('rx_pkts', 0)
                txP_prev = prev.get(key, {}).get('tx_pkts', 0)
                dr_prev  = prev.get(key, {}).get('rx_dropped', 0) + prev.get(key, {}).get('tx_dropped', 0)
                er_prev  = prev.get(key, {}).get('rx_errors', 0)  + prev.get(key, {}).get('tx_errors', 0)

                rxB = pstat.get('rx_bytes', 0); txB = pstat.get('tx_bytes', 0)
                rxP = pstat.get('rx_pkts', 0);  txP = pstat.get('tx_pkts', 0)
                dr  = pstat.get('rx_dropped', 0) + pstat.get('tx_dropped', 0)
                er  = pstat.get('rx_errors', 0)  + pstat.get('tx_errors', 0)

                row = {
                    'ts': now,
                    'dpid': pstat.get('dpid'),
                    'port_no': pstat.get('port_no'),
                    'rx_bytes': rxB,
                    'tx_bytes': txB,
                    'rx_pkts': rxP,
                    'tx_pkts': txP,
                    'rx_dropped': pstat.get('rx_dropped', 0),
                    'tx_dropped': pstat.get('tx_dropped', 0),
                    'rx_errors': pstat.get('rx_errors', 0),
                    'tx_errors': pstat.get('tx_errors', 0),
                    'rx_bps': max(0.0, (rxB - rxB_prev) * 8.0 / dt),
                    'tx_bps': max(0.0, (txB - txB_prev) * 8.0 / dt),
                    'rx_pps': max(0.0, (rxP - rxP_prev) / dt),
                    'tx_pps': max(0.0, (txP - txP_prev) / dt),
                    'drop_ps': max(0.0, (dr  - dr_prev) / dt),
                    'err_ps':  max(0.0, (er  - er_prev) / dt),
                }
                w.writerow(row)
                prev[key] = dict(pstat, ts=now)

            f.flush()

            if args.duration is not None and (now - start) >= args.duration:
                print("Duration reached; exiting logger.")
                break

            time.sleep(args.interval)

if _name_ == '_main_':
    main()