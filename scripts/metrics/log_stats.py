#!/usr/bin/env python3
import argparse, time, sys, csv, requests
from urllib.parse import urlparse

def norm_base(u: str) -> str:
    """
    Accept either a full base (e.g., http://127.0.0.1:8080/api/v1)
    or just host[:port] (e.g., 127.0.0.1:8080). Returns a normalized
    base like 'http://127.0.0.1:8080/api/v1/'.
    """
    u = (u or "").strip()
    if not u:
        return "http://127.0.0.1:8080/api/v1/"
    # If user passed just host[:port] without scheme
    if "://" not in u:
        u = f"http://{u}"
    parsed = urlparse(u)
    # If path is empty, assume /api/v1
    path = parsed.path or "/api/v1"
    if not path.endswith("/"):
        path = path + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"

def get_json(url, timeout=2.0):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def poll_ports(base):
    # ryu rest stats: /stats/ports
    return get_json(base + "stats/ports")

def row_from_entry(ts, dpid, port, ent):
    return [
        ts,
        dpid,
        port,
        ent.get("rx_packets", 0),
        ent.get("tx_packets", 0),
        ent.get("rx_bytes", 0),
        ent.get("tx_bytes", 0),
        ent.get("rx_dropped", 0),
        ent.get("tx_dropped", 0),
        ent.get("rx_errors", 0),
        ent.get("tx_errors", 0),
        ent.get("rx_rate_bps", 0.0),
        ent.get("tx_rate_bps", 0.0),
        ent.get("rx_rate_mbps", 0.0),
        ent.get("tx_rate_mbps", 0.0),
        ent.get("loss_pct", 0.0),
        ent.get("err_pct", 0.0),
    ]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1",
                    help="Controller REST base. Either full base (http://host:port/api/v1) or host[:port]")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = norm_base(args.controller)

    # CSV header
    header = [
        "ts","dpid","port","rx_packets","tx_packets","rx_bytes","tx_bytes",
        "rx_dropped","tx_dropped","rx_errors","tx_errors",
        "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
        "loss_pct","err_pct"
    ]

    start = time.time()
    next_poll = start
    end = start + args.duration

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        while time.time() < end:
            now = time.time()
            if now < next_poll:
                time.sleep(max(0.0, next_poll - now))
            next_poll += args.interval
            ts = time.time()
            try:
                data = poll_ports(base)
            except Exception as e:
                # Write nothing but keep going; stdout keeps the error trace minimal
                print(f"Fetch error: {e}", file=sys.stdout, flush=True)
                continue

            # data shape from /stats/ports: { "DPID": [ { "port_no":..., "rx_packets":..., ...}, ... ], ... }
            for dpid_str, entries in data.items():
                try:
                    dpid = int(dpid_str)
                except Exception:
                    # sometimes dpids come as ints already
                    dpid = dpid_str
                for ent in entries:
                    port = ent.get("port_no", 0)
                    # skip 'LOCAL' and invalids if present
                    if isinstance(port, str) and port.upper() == "LOCAL":
                        continue
                    try:
                        port = int(port)
                    except Exception:
                        continue
                    w.writerow(row_from_entry(ts, dpid, port, ent))
        f.flush()

if __name__ == "__main__":
    main()
