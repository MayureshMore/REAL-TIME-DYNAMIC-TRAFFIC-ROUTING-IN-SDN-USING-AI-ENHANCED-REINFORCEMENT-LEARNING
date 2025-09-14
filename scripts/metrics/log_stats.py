# scripts/metrics/log_stats.py


import argparse
import csv
import time
import requests
from datetime import datetime




def fetch_json(url, timeout=2):
    try:
    r = requests.get(url, timeout=timeout)
r.raise_for_status()
return r.json()
except Exception as e:
print("Fetch error:", e)
return None




def main():
    p = argparse.ArgumentParser()
p.add_argument('--controller', default='127.0.0.1')
p.add_argument('--port', type=int, default=8080)
p.add_argument('--interval', type=float, default=2.0)
p.add_argument('--out', default=None, help='CSV output file (default derives from timestamp)')
args = p.parse_args()


base = f"http://{args.controller}:{args.port}/api/v1"
out = args.out or f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


fields = [
    'ts', 'dpid', 'port_no', 'rx_pkts', 'tx_pkts', 'rx_bytes', 'tx_bytes',
    'rx_dropped', 'tx_dropped', 'rx_errors', 'tx_errors'
]


print(f"Logging to {out}; polling {base} every {args.interval}s")
with open(out, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=fields)
w.writeheader()
while True:
    ports = fetch_json(f"{base}/stats/ports") or []
ts = time.time()
for pstat in ports:
    row = {
        'ts': ts,
        'dpid': pstat.get('dpid'),
        'port_no': pstat.get('port_no'),
        'rx_pkts': pstat.get('rx_pkts'),
        'tx_pkts': pstat.get('tx_pkts'),
        'rx_bytes': pstat.get('rx_bytes'),
        'tx_bytes': pstat.get('tx_bytes'),
        'rx_dropped': pstat.get('rx_dropped'),
        'tx_dropped': pstat.get('tx_dropped'),
        'rx_errors': pstat.get('rx_errors'),
        'tx_errors': pstat.get('tx_errors'),
    }
w.writerow(row)
f.flush()
time.sleep(args.interval)




if __name__ == '__main__':
    main()