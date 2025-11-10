#!/usr/bin/env python3
import argparse, os
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # headless and reliable
import matplotlib.pyplot as plt
from typing import List

def read_one(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    needed = {
        "ts","dpid","port","rx_packets","tx_packets","rx_bytes","tx_bytes",
        "rx_dropped","tx_dropped","rx_errors","tx_errors",
        "rx_rate_bps","tx_rate_bps","rx_rate_mbps","tx_rate_mbps",
        "loss_pct","err_pct"
    }
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"{os.path.basename(path)} missing columns: {missing}")
    # drop LOCAL port
    df = df[df["port"] != 4294967294].copy()
    # second resolution index
    df["t"] = df["ts"].round().astype("int64")
    return df

def e2e_series(df: pd.DataFrame) -> pd.Series:
    """End-to-end Mbps per second between host ports: min(s1:port1 tx, s2:port1 rx)."""
    host = df[df["port"] == 1].copy()
    if host.empty:
        return pd.Series(dtype=float)
    s1_tx = host[host["dpid"] == 1].groupby("t")["tx_rate_mbps"].mean()
    s2_rx = host[host["dpid"] == 2].groupby("t")["rx_rate_mbps"].mean()
    aligned = pd.concat([s1_tx, s2_rx], axis=1, join="inner")
    aligned.columns = ["tx_mbps", "rx_mbps"]
    return aligned.min(axis=1)

def drops_series(df: pd.DataFrame) -> pd.Series:
    """Sum drops (rx_dropped + tx_dropped) across all non-LOCAL ports per second (packets/sec)."""
    g = df.groupby("t")[["rx_dropped","tx_dropped"]].sum()
    return (g["rx_dropped"] + g["tx_dropped"]).astype(float)

def errors_series(df: pd.DataFrame) -> pd.Series:
    """Sum errors across all non-LOCAL ports per second (packets/sec)."""
    g = df.groupby("t")[["rx_errors","tx_errors"]].sum()
    return (g["rx_errors"] + g["tx_errors"]).astype(float)

def plot_timeseries(ax, series_list: List[pd.Series], labels: List[str], title: str, ylabel: str):
    for s, lab in zip(series_list, labels):
        if s.empty:
            ax.plot([], [], label=f"{lab} (no data)")
        else:
            ax.plot(s.index - s.index.min(), s.values, label=lab)  # normalize time to start at 0
    ax.set_title(title)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.legend()
    ax.grid(True, alpha=0.3)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True, help="CSV files (baseline then RL)")
    ap.add_argument("--labels", nargs="+", required=True, help="Labels matching files")
    args = ap.parse_args()

    if len(args.files) != len(args.labels):
        raise SystemExit("files and labels must be the same length")

    dfs = [read_one(p) for p in args.files]
    labels = args.labels

    # Compute series
    thr = [e2e_series(df) for df in dfs]
    drp = [drops_series(df) for df in dfs]
    err = [errors_series(df) for df in dfs]

    # Output dir: sibling 'plots' of first csv parent
    first_dir = os.path.dirname(os.path.abspath(args.files[0]))
    out_dir = os.path.join(first_dir, "plots")
    os.makedirs(out_dir, exist_ok=True)

    # Throughput
    fig, ax = plt.subplots(figsize=(12, 7))
    plot_timeseries(ax, thr, labels, "Throughput (end-to-end, host ports)", "Throughput (Mbps)")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "throughput.png"), dpi=160)
    plt.close(fig)

    # Drops
    fig, ax = plt.subplots(figsize=(12, 7))
    plot_timeseries(ax, drp, labels, "Packet Drops (all non-LOCAL ports)", "Packet Drops (pkts/s)")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "drops.png"), dpi=160)
    plt.close(fig)

    # Errors
    fig, ax = plt.subplots(figsize=(12, 7))
    plot_timeseries(ax, err, labels, "Packet Errors (all non-LOCAL ports)", "Packet Errors (pkts/s)")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "errors.png"), dpi=160)
    plt.close(fig)

if __name__ == "__main__":
    main()
