#!/usr/bin/env python3
import argparse, time, sys, csv, requests
from urllib.parse import urlparse
from collections import defaultdict

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
    # Our controller exposes <base>stats/ports
    return get_json(base + "stats/ports")

def iter_port_entries(data):
    # Accept several shapes; yield (dpid, entry_dict)
    if isinstance(data, dict):
        for dpid_str, entries in data.items():
            dpid = int(dpid_str) if str(dpid_str).isdigit() else dpid_str
            for ent in entries or []:
                yield dpid, ent
        return
    if isinstance(data, list):
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
        for ent in data:
            if isinstance(ent, dict) and "port_no" in ent:
                yield ent.get("dpid"), ent
        return

def as_int(x, default=0):
    try: return int(x)
    except Exception: return default

def as_float(x, default=0.0):
    try: return float(x)
    except Exception: return default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="http://127.0.0.1:8080/api/v1",
                    help="Controller REST base, e.g. http://127.0.0.1:8080/api/v1")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = norm_base(args.controller)

    header = [
        "ts","dpid","port",
        "rx_packets","tx_packets","rx_bytes","tx_bytes",
        "rx_dropped","tx_dropped","rx_errors","tx_errors",
        "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
        "loss_pct","err_pct"
    ]

    # State to compute instantaneous rates from deltas
    # prev[(dpid,port)] = (ts, rx_bytes, tx_bytes, rx_dropped, tx_dropped, rx_errors, tx_errors)
    prev = {}

    end = time.time() + args.duration
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)

        next_poll = time.time()
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

            wrote = False
            for dpid, ent in iter_port_entries(data):
                port = ent.get("port_no", 0)
                if isinstance(port, str):
                    if port.upper() == "LOCAL":
                        continue
                    try:
                        port = int(port)
                    except Exception:
                        continue

                rx_bytes    = as_int(ent.get("rx_bytes"))
                tx_bytes    = as_int(ent.get("tx_bytes"))
                rx_packets  = as_int(ent.get("rx_packets", ent.get("rx_pkts", 0)))
                tx_packets  = as_int(ent.get("tx_packets", ent.get("tx_pkts", 0)))
                rx_dropped  = as_int(ent.get("rx_dropped"))
                tx_dropped  = as_int(ent.get("tx_dropped"))
                rx_errors   = as_int(ent.get("rx_errors"))
                tx_errors   = as_int(ent.get("tx_errors"))

                key = (dpid, port)
                rx_bps = tx_bps = 0.0
                if key in prev:
                    pts, prx_b, ptx_b, prx_d, ptx_d, prx_e, ptx_e = prev[key]
                    dt = max(1e-6, ts - pts)
                    rx_bps = max(0.0, (rx_bytes - prx_b) * 8.0 / dt)
                    tx_bps = max(0.0, (tx_bytes - ptx_b) * 8.0 / dt)
                prev[key] = (ts, rx_bytes, tx_bytes, rx_dropped, tx_dropped, rx_errors, tx_errors)

                # loss / err percentage (best-effort)
                loss_pct = err_pct = 0.0
                total_pkts = rx_packets + tx_packets
                if total_pkts > 0:
                    loss_pct = 100.0 * (rx_dropped + tx_dropped) / total_pkts
                    err_pct  = 100.0 * (rx_errors + tx_errors) / total_pkts

                w.writerow([
                    ts, dpid, port,
                    rx_packets, tx_packets, rx_bytes, tx_bytes,
                    rx_dropped, tx_dropped, rx_errors, tx_errors,
                    rx_bps, tx_bps, rx_bps/1e6, tx_bps/1e6,
                    loss_pct, err_pct
                ])
                wrote = True

            if not wrote:
                # No entries this tick; keep going
                pass
        f.flush()

if __name__ == "__main__":
    main()
