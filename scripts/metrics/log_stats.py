#!/usr/bin/env python3
import argparse, time, sys, csv, requests
from urllib.parse import urlparse

LOCAL_PORT = 4294967294  # Ryu "LOCAL"

def norm_base(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return "http://127.0.0.1:8080/api/v1/"
    if "://" not in u:
        u = f"http://{u}"
    p = urlparse(u)
    path = p.path or "/api/v1"
    if not path.endswith("/"):
        path += "/"
    return f"{p.scheme}://{p.netloc}{path}"

def get_json(url, timeout=2.0):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

def poll_ports(base):
    # Expected endpoints:
    #  - our controller:  <base>stats/ports  (list of dicts)
    #  - legacy shape:    dict{ dpid_str: [ {port rec}, ... ] }
    return get_json(base + "stats/ports")

def iter_port_entries(data):
    """
    Yields tuples (dpid, entry_dict) across supported shapes.
    entry_dict has Ryu fields:
      rx_packets, tx_packets, rx_bytes, tx_bytes,
      rx_dropped, tx_dropped, rx_errors, tx_errors, port_no
    """
    if isinstance(data, dict):
        # {"1":[...], "2":[...]}
        for dpid_str, entries in data.items():
            try:
                dpid = int(dpid_str)
            except Exception:
                dpid = dpid_str
            for ent in entries or []:
                yield dpid, ent
        return

    if isinstance(data, list):
        # Either [{"dpid":1,"ports":[...]}, ...] OR flat list of entries
        handled = False
        for block in data:
            if not isinstance(block, dict):
                continue
            dpid = block.get("dpid")
            for key in ("ports", "stats", "entries"):
                if key in block and isinstance(block[key], list):
                    for ent in block[key]:
                        yield dpid, ent
                    handled = True
                    break
        if handled:
            return
        # flat list of entries
        for ent in data:
            if isinstance(ent, dict) and ("port_no" in ent or "port" in ent):
                yield ent.get("dpid"), ent
        return

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def row_from_entry(ts, dpid, port, ent, prev_map):
    """
    Compute deltas/rates from cumulative counters using prev_map[(dpid,port)].
    Returns list aligned with header.
    """
    rx_pkts = safe_int(ent.get("rx_packets", ent.get("rx_pkts", 0)))
    tx_pkts = safe_int(ent.get("tx_packets", ent.get("tx_pkts", 0)))
    rx_bytes = safe_int(ent.get("rx_bytes", 0))
    tx_bytes = safe_int(ent.get("tx_bytes", 0))
    rx_drop  = safe_int(ent.get("rx_dropped", 0))
    tx_drop  = safe_int(ent.get("tx_dropped", 0))
    rx_err   = safe_int(ent.get("rx_errors", 0))
    tx_err   = safe_int(ent.get("tx_errors", 0))

    key = (dpid, port)
    prev = prev_map.get(key)
    if prev:
        dt = max(1e-6, ts - prev["ts"])
        tx_bps = (tx_bytes - prev["tx_bytes"]) * 8.0 / dt
        rx_bps = (rx_bytes - prev["rx_bytes"]) * 8.0 / dt
        drop_ps = ( (rx_drop - prev["rx_drop"]) + (tx_drop - prev["tx_drop"]) ) / dt
        err_ps  = ( (rx_err  - prev["rx_err"])  + (tx_err  - prev["tx_err"]) )  / dt
    else:
        tx_bps = rx_bps = drop_ps = err_ps = 0.0

    prev_map[key] = {
        "ts": ts, "tx_bytes": tx_bytes, "rx_bytes": rx_bytes,
        "rx_drop": rx_drop, "tx_drop": tx_drop, "rx_err": rx_err, "tx_err": tx_err
    }

    return [
        ts, dpid, port,
        rx_pkts, tx_pkts, rx_bytes, tx_bytes,
        rx_drop, tx_drop, rx_err, tx_err,
        rx_bps, tx_bps, rx_bps/1e6, tx_bps/1e6,
        drop_ps, err_ps
    ]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1",
                    help="Controller REST base, e.g. http://127.0.0.1:8080/api/v1 or 127.0.0.1:8080")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = norm_base(args.controller)

    header = [
        "ts","dpid","port","rx_packets","tx_packets","rx_bytes","tx_bytes",
        "rx_dropped","tx_dropped","rx_errors","tx_errors",
        "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
        "drop_ps","err_ps"
    ]

    start = time.time()
    next_poll = start
    end = start + args.duration
    prev_map = {}

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
                print(f"Fetch error: {e}", file=sys.stdout, flush=True)
                continue

            wrote_any = False
            for dpid, ent in iter_port_entries(data) or []:
                port = ent.get("port_no", ent.get("port", 0))
                # Normalize/validate port
                if isinstance(port, str):
                    if port.upper() == "LOCAL":
                        continue
                    try:
                        port = int(port)
                    except Exception:
                        continue
                if not isinstance(port, int):
                    continue
                if port == LOCAL_PORT:
                    continue

                w.writerow(row_from_entry(ts, dpid, port, ent, prev_map))
                wrote_any = True

            if not wrote_any and isinstance(data, (dict, list)):
                # nothing usable this tick; ignore
                pass
        f.flush()

if __name__ == "__main__":
    main()
