#!/usr/bin/env python3
import argparse, time, sys, csv, requests
from urllib.parse import urlparse

OFPP_LOCAL = 0xFFFFFFFE  # 4294967294

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
    # Our controller exposes /stats/ports
    return get_json(base + "stats/ports")

def iter_port_entries(data):
    """Yield (dpid, entry_dict) across supported response shapes."""
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
        # block list with nested list
        handled_any = False
        for block in data:
            if not isinstance(block, dict):
                continue
            dpid = block.get("dpid")
            for key in ("ports", "stats", "entries"):
                if key in block and isinstance(block[key], list):
                    for ent in block[key]:
                        yield dpid, ent
                        handled_any = True
                    break
        if handled_any:
            return
        # already flat
        for ent in data:
            if isinstance(ent, dict) and "port_no" in ent:
                yield ent.get("dpid"), ent
        return

def row_from_entry(ts, dpid, port, ent):
    return [
        ts,
        dpid,
        port,
        ent.get("rx_packets", ent.get("rx_pkts", 0)),
        ent.get("tx_packets", ent.get("tx_pkts", 0)),
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
                print(f"Fetch error: {e}", file=sys.stdout, flush=True)
                continue

            wrote_any = False
            for dpid, ent in iter_port_entries(data):
                port = ent.get("port_no", 0)

                # drop invalid/LOCAL ports
                try:
                    p_int = int(port)
                except Exception:
                    continue
                if p_int in (0, OFPP_LOCAL):
                    continue

                w.writerow(row_from_entry(ts, dpid, p_int, ent))
                wrote_any = True

            if not wrote_any and isinstance(data, (dict, list)):
                pass
        f.flush()

if __name__ == "__main__":
    main()
