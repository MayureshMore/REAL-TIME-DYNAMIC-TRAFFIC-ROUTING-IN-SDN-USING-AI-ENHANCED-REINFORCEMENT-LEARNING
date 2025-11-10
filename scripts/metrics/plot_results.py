#!/usr/bin/env python3
# Plot Results for SDN RL Experiments
# -----------------------------------
# Reads port metrics CSV logs and plots average throughput, drop rate,
# and error rate over time. Outputs graphs under docs/baseline/plots/.

import argparse
import os
import re
import glob
from datetime import datetime
import pandas as pd
import matplotlib.pyplot as plt

plt.rcParams.update({
    "figure.figsize": (8, 5),
    "axes.grid": True,
    "font.size": 11,
    "lines.linewidth": 1.8,
    "axes.titlesize": 13,
    "axes.labelsize": 12
})

# ---------- helpers ----------

TS_RE = re.compile(r"(\d{8}_\d{6})")  # e.g., 20251109_193823

def _extract_ts(path: str):
    """Extract sortable timestamp from filename; fall back to mtime."""
    m = TS_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except Exception:
            pass
    # fallback: file modification time
    return datetime.fromtimestamp(os.path.getmtime(path))

def pick_latest(files):
    """Return the single newest file from a list (or [] if none)."""
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return []
    files.sort(key=_extract_ts)
    return [files[-1]]

def classify_files(file_list):
    """Split into baseline vs rl lists by filename prefix."""
    base, rl = [], []
    for f in file_list:
        name = os.path.basename(f)
        if name.startswith("ports_baseline_"):
            base.append(f)
        elif name.startswith("ports_rl_"):
            rl.append(f)
    return base, rl

def load_csv(file_path):
    """Load and preprocess the CSV file (normalize ts to start at 0)."""
    df = pd.read_csv(file_path)
    if 'ts' not in df.columns:
        raise ValueError(f"Missing 'ts' column in {file_path}")
    df = df.sort_values('ts').reset_index(drop=True)
    df['ts'] = df['ts'] - df['ts'].iloc[0]
    return df

def aggregate_metrics(df):
    """
    Aggregate all ports to get system-level averages per timestamp.
    Supports either:
      - tx_bps/rx_bps (+ drop_ps/err_ps), or
      - tx_mbps/rx_mbps (+ drop_ps/err_ps)
    """
    cols = set(df.columns)

    # Throughput
    if {'tx_bps', 'rx_bps'}.issubset(cols):
        thr_mbps = (df.groupby('ts')['tx_bps'].mean() / 1e6).rename('throughput_mbps')
    elif {'tx_mbps', 'rx_mbps'}.issubset(cols):
        thr_mbps = df.groupby('ts')['tx_mbps'].mean().rename('throughput_mbps')
    else:
        raise ValueError("CSV must have either tx_bps/rx_bps or tx_mbps/rx_mbps")

    # Drops / Errors (packets per second). If missing, default to 0.
    if 'drop_ps' in cols:
        drops = df.groupby('ts')['drop_ps'].mean().rename('drops')
    else:
        drops = df.groupby('ts').size().mul(0.0).rename('drops')  # zeros

    if 'err_ps' in cols:
        errs = df.groupby('ts')['err_ps'].mean().rename('errors')
    else:
        errs = df.groupby('ts').size().mul(0.0).rename('errors')  # zeros

    out = pd.concat([thr_mbps, drops, errs], axis=1).reset_index()
    return out

def plot_metric(ax, df, label, field, ylabel):
    ax.plot(df['ts'], df[field], label=label)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right")

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Plot performance graphs from metrics CSVs")
    ap.add_argument("--files", nargs="+", required=True,
                    help="List of CSV files (you can pass many; we auto-pick newest baseline and newest RL).")
    ap.add_argument("--labels", nargs="+", required=False,
                    help="Optional labels. If exactly two are provided, they map to newest Baseline and newest RL.")
    ap.add_argument("--out", default=None, help="Output folder (default: docs/baseline/plots/)")
    args = ap.parse_args()

    out_dir = args.out or "docs/baseline/plots"
    os.makedirs(out_dir, exist_ok=True)

    # Classify and pick the newest baseline & RL (prevents legend explosion)
    base_files, rl_files = classify_files(args.files)

    if args.labels and len(args.labels) == 2:
        # Explicit Baseline/RL labelling — pick only the newest of each
        base_files = pick_latest(base_files)
        rl_files = pick_latest(rl_files)
        files = base_files + rl_files
        labels = args.labels
    else:
        # No/other labels — still reduce to newest baseline+RL if multiple
        base_files = pick_latest(base_files) if len(base_files) > 1 else base_files
        rl_files = pick_latest(rl_files) if len(rl_files) > 1 else rl_files
        files = base_files + rl_files
        # Default labels: Baseline / RL (only for files we actually have)
        labels = []
        if base_files:
            labels.append("Baseline")
        if rl_files:
            labels.append("RL")

    if not files:
        raise SystemExit("[x] No valid CSVs found among the provided --files")

    # Plot containers
    fig1, ax1 = plt.subplots()
    fig2, ax2 = plt.subplots()
    fig3, ax3 = plt.subplots()

    for i, file in enumerate(files):
        try:
            df = load_csv(file)
            agg = aggregate_metrics(df)
            label = labels[i] if i < len(labels) else os.path.basename(file)
            plot_metric(ax1, agg, label, "throughput_mbps", "Avg Throughput (Mbps)")
            plot_metric(ax2, agg, label, "Packet Drops (pkts/s)")
            plot_metric(ax3, agg, label, "Packet Errors (pkts/s)")
        except Exception as e:
            print(f"[!] Error processing {file}: {e}")

    # Save
    for fig, name in zip([fig1, fig2, fig3],
                         ["throughput.png", "drops.png", "errors.png"]):
        out_path = os.path.join(out_dir, name)
        fig.tight_layout()
        fig.savefig(out_path, dpi=200)
        print(f"[✓] Saved {out_path}")

    print(f"\nPlots saved in {os.path.abspath(out_dir)}")

if __name__ == "__main__":
    main()
