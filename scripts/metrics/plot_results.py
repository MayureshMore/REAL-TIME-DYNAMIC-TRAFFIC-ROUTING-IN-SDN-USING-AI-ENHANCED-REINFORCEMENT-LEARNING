#!/usr/bin/env python3
import argparse, math
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path

# ---- Tunables ----
USE_CORE_SUM = False      # False => use host-facing ports (recommended); True => sum core links (ports 2 & 3)
LOG_SCALE_SMALL = True    # Use log scale for drops/errors so near-zero values are visible

def load_and_prepare(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # Basic sanity
    needed = {'ts','dpid','port','rx_rate_mbps','tx_rate_mbps','loss_pct','err_pct'}
    missing = needed - set(df.columns)
    if missing:
        raise RuntimeError(f"{csv_path}: missing columns {missing}")

    # Filter out LOCAL pseudo-port
    df = df[df['port'] != 4294967294].copy()

    # Round timestamps to 1s buckets for alignment
    df['t'] = df['ts'].round().astype(int)

    if USE_CORE_SUM:
        # Sum throughput across inter-switch links (ports 2 and 3), per second
        core = df[df['port'].isin([2,3])].copy()
        # Use TX as "outgoing capacity" proxy
        thr = core.groupby('t')['tx_rate_mbps'].sum().rename('throughput_mbps')
    else:
        # Use host-facing ports (port==1): s1:1 TX (sender) + s2:1 RX (receiver)
        host = df[df['port'] == 1].copy()
        s1_tx = host[host['dpid'] == 1].groupby('t')['tx_rate_mbps'].mean()
        s2_rx = host[host['dpid'] == 2].groupby('t')['rx_rate_mbps'].mean()
        # Align and take the min per second (conservative e2e)
        thr = pd.concat([s1_tx, s2_rx], axis=1, join='inner')
        thr.columns = ['tx_mbps','rx_mbps']
        thr['throughput_mbps'] = thr[['tx_mbps','rx_mbps']].min(axis=1)
        thr = thr['throughput_mbps']

    # For drops/errors, take mean across all ports per second to avoid a single quiet port masking activity
    loss = df.groupby('t')['loss_pct'].mean().rename('loss_pct_mean')
    errs = df.groupby('t')['err_pct'].mean().rename('err_pct_mean')

    out = pd.concat([thr, loss, errs], axis=1)
    out.index.name = 't'
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', nargs='+', required=True, help='CSV files: baseline then RL')
    ap.add_argument('--labels', nargs='+', required=True, help='Matching labels')
    args = ap.parse_args()

    if len(args.files) != len(args.labels):
        raise SystemExit('Need same number of --files and --labels')

    series = []
    for f in args.files:
        series.append(load_and_prepare(Path(f)))

    # Align all runs on a common time index
    common_t = series[0].index
    for s in series[1:]:
        common_t = common_t.intersection(s.index)
    runs = [s.loc[common_t] for s in series]

    # ---- Throughput ----
    plt.figure(figsize=(12,6))
    for s, lab in zip(runs, args.labels):
        plt.plot(common_t - common_t.min(), s['throughput_mbps'], label=lab, linewidth=1.5)
    plt.xlabel('Time (s)')
    plt.ylabel('Throughput (Mbps)')
    plt.legend()
    out_dir = Path(args.files[0]).parent / 'plots'
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'throughput.png').write_bytes(plt.gcf().canvas.buffer_rgba())
    plt.savefig(out_dir / 'throughput.png', bbox_inches='tight', dpi=150)

    # ---- Drops ----
    plt.figure(figsize=(12,6))
    for s, lab in zip(runs, args.labels):
        y = s['loss_pct_mean']
        plt.plot(common_t - common_t.min(), y, label=lab, linewidth=1.5)
    if LOG_SCALE_SMALL:
        plt.yscale('symlog', linthresh=1e-4)  # show tiny values
    plt.xlabel('Time (s)')
    plt.ylabel('Packet Drops (fraction)')
    plt.legend()
    plt.savefig(out_dir / 'drops.png', bbox_inches='tight', dpi=150)

    # ---- Errors ----
    plt.figure(figsize=(12,6))
    for s, lab in zip(runs, args.labels):
        y = s['err_pct_mean']
        plt.plot(common_t - common_t.min(), y, label=lab, linewidth=1.5)
    if LOG_SCALE_SMALL:
        plt.yscale('symlog', linthresh=1e-5)
    plt.xlabel('Time (s)')
    plt.ylabel('Packet Errors (fraction)')
    plt.legend()
    plt.savefig(out_dir / 'errors.png', bbox_inches='tight', dpi=150)

if __name__ == '__main__':
    main()
