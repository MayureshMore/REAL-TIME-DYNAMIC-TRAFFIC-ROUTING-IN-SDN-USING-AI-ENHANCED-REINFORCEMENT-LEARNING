#!/usr/bin/env python3
# Plot Results for SDN RL Experiments
# -----------------------------------
# Reads port metrics CSV logs (from log_stats.py)
# and plots average throughput, drop rate, and error rate over time.
# Outputs publication-quality graphs under docs/baseline/plots/

import argparse
import os
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime

plt.rcParams.update({
    "figure.figsize": (8, 5),
    "axes.grid": True,
    "font.size": 11,
    "lines.linewidth": 1.8,
    "axes.titlesize": 13,
    "axes.labelsize": 12
})

def load_csv(file_path):
    """Load and preprocess the CSV file"""
    df = pd.read_csv(file_path)
    if 'ts' not in df.columns:
        raise ValueError(f"Missing timestamp column in {file_path}")
    df = df.sort_values('ts')
    df['ts'] = df['ts'] - df['ts'].iloc[0]  # normalize to seconds
    return df

def aggregate_metrics(df):
    """Aggregate all ports to get average system-level metrics"""
    grouped = df.groupby('ts').agg({
        'tx_bps': 'mean',
        'rx_bps': 'mean',
        'drop_ps': 'mean',
        'err_ps': 'mean'
    }).reset_index()
    grouped['throughput_mbps'] = grouped['tx_bps'] / 1e6
    grouped['drops'] = grouped['drop_ps']
    grouped['errors'] = grouped['err_ps']
    return grouped

def plot_metric(ax, df, label, color, field, ylabel):
    ax.plot(df['ts'], df[field], label=label, color=color)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right")

def main():
    ap = argparse.ArgumentParser(description="Plot performance graphs from metrics CSVs")
    ap.add_argument("--files", nargs="+", required=True, help="List of CSV files to plot")
    ap.add_argument("--labels", nargs="+", required=False, help="Labels for the curves")
    ap.add_argument("--out", default=None, help="Output folder (default: docs/baseline/plots/)")
    args = ap.parse_args()

    out_dir = args.out or "docs/baseline/plots"
    os.makedirs(out_dir, exist_ok=True)

    labels = args.labels or [f"Run{i+1}" for i in range(len(args.files))]
    colors = ["tab:blue", "tab:green", "tab:red", "tab:orange", "tab:purple"]

    fig1, ax1 = plt.subplots()
    fig2, ax2 = plt.subplots()
    fig3, ax3 = plt.subplots()

    for i, file in enumerate(args.files):
        try:
            df = load_csv(file)
            agg = aggregate_metrics(df)
            label = labels[i] if i < len(labels) else os.path.basename(file)
            color = colors[i % len(colors)]
            plot_metric(ax1, agg, label, color, "throughput_mbps", "Avg Throughput (Mbps)")
            plot_metric(ax2, agg, label, color, "drops", "Packet Drops (pkts/s)")
            plot_metric(ax3, agg, label, color, "errors", "Packet Errors (pkts/s)")
        except Exception as e:
            print(f"[!] Error processing {file}: {e}")

    for fig, name in zip([fig1, fig2, fig3],
                         ["throughput.png", "drops.png", "errors.png"]):
        out_path = os.path.join(out_dir, name)
        fig.tight_layout()
        fig.savefig(out_path, dpi=200)
        print(f"[✓] Saved {out_path}")

    print(f"\nPlots saved in {os.path.abspath(out_dir)}")
    plt.show()

if __name__ == "__main__":
    main()
