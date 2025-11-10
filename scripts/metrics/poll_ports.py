#!/usr/bin/env python3
# Polls controller REST and appends CSV rows.
# Compatible with multiple endpoints; mirrors fields used by log_stats.py where possible.

import argparse, csv, json, sys, time, urllib.request, urllib.error

OFPP_LOCAL = 0xFFFFFFFE
CANDIDATE_ENDPOINTS = ["metrics/ports", "stats/ports", "ports"]

def get_json(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def flatten_records(now_ts, payload):
    rows = []
    if isinstance(payload, dict) and "ports" in payload:
        payload = payload["ports"]
    if isinstance(payload, dict):
        for dpid, plist in payload.items():
            for p in plist or []:
                rows.append(row_from_port(now_ts, p, dpid=int(dpid)))
    elif isinstance(payload, list):
        for p in payload:
            rows.append(row_from_port(now_ts, p))
    return rows

def row_from_port(ts, p, dpid=None):
    d = {k: p.get(k) for k in p.keys()}
    dpid = int(d.get("dpid", dpid or 0))
    port_no = int(d.get("port_no", d.get("port", 0)))
    if port_no in (0, OFPP_LOCAL):   # drop LOCAL/invalid here too
        return None

    rx_bytes = int(d.get("rx_bytes", 0))
    tx_bytes = int(d.get("tx_bytes", 0))
    rx_packets = int(d.get("rx_packets", 0))
    tx_packets = int(d.get("tx_packets", 0))
    rx_dropped = int(d.get("rx_dropped", 0))
    tx_dropped = int(d.get("tx_dropped", 0))
    rx_errors  = int(d.get("rx_errors", 0))
    tx_errors  = int(d.get("tx_errors", 0))
    rx_rate = float(d.get("rx_rate_bps", d.get("rx_rate", 0.0)))
    tx_rate = float(d.get("tx_rate_bps", d.get("tx_rate", 0.0)))
    rx_rate_mbps = float(d.get("rx_rate_mbps", rx_rate/1e6))
    tx_rate_mbps = float(d.get("tx_rate_mbps", tx_rate/1e6))
    return [ts, dpid, port_no,
            rx_packets, tx_packets, rx_bytes, tx_bytes,
            rx_dropped, tx_dropped, rx_errors, tx_errors,
            rx_rate, tx_rate, rx_rate_mbps, tx_rate_mbps, 0.0, 0.0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1",
                    help="Base REST URL (default: %(default)s)")
    ap.add_argument("--outfile", required=True, help="CSV file to append")
    ap.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds")
    ap.add_argument("--duration", type=float, default=120.0, help="Total seconds to run")
    args = ap.parse_args()

    header = ["ts","dpid","port",
              "rx_packets","tx_packets","rx_bytes","tx_bytes",
              "rx_dropped","tx_dropped","rx_errors","tx_errors",
              "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
              "loss_pct","err_pct"]

    try:
        f = open(args.outfile, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(header)
    except Exception as e:
        print(f"[logger] cannot open {args.outfile}: {e}", file=sys.stderr)
        sys.exit(1)

    start = time.time()
    next_tick = start
    print(f"Logging to {args.outfile}; polling {args.controller} every {args.interval:.1f}s; duration={args.duration:.1f}", file=sys.stderr)
    try:
        while True:
            now = time.time()
            if now - start > args.duration:
                break
            next_tick += args.interval

            data = None
            last_err = None
            base = args.controller.rstrip("/")
            for ep in CANDIDATE_ENDPOINTS:
                url = f"{base}/{ep.lstrip('/')}"
                try:
                    data = get_json(url)
                    break
                except Exception as e:
                    last_err = e
            if data is None:
                print(f"[logger] poll failed: {last_err}", file=sys.stderr)
            else:
                rows = flatten_records(now, data)
                for r in rows:
                    if r is not None:
                        writer.writerow(r)
                f.flush()

            time.sleep(max(0.0, next_tick - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        f.close()

if __name__ == "__main__":
    main()
