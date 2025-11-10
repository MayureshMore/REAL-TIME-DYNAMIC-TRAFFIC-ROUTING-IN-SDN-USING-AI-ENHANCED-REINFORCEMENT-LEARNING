#!/usr/bin/env python3
# Plot Results for SDN RL Experiments
# -----------------------------------
# Reads port-metrics CSV logs and plots avg throughput (Mbps),
# packet drops/s, and packet errors/s over time.
# Handles our headers from log_stats.py:
#   tx_rate_mbps/tx_rate_bps (preferred), or falls back to bytes delta.
# Computes drop/error rates from counter deltas.

import argparse, os, re, glob
from datetime import datetime
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

plt.rcParams.update({
    "figure.figsize": (8, 5),
    "axes.grid": True,
    "font.size": 11,
    "lines.linewidth": 1.8,
    "axes.titlesize": 13,
    "axes.labelsize": 12
})

TS_RE = re.compile(r"(\d{8}_\d{6})")  # e.g., 20251109_193823
OFPP_LOCAL = 0xFFFFFFFE

def _extract_ts(path: str):
    m = TS_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except Exception:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path))

def pick_latest(files):
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return []
    files.sort(key=_extract_ts)
    return [files[-1]]

def classify_files(file_list):
    base, rl = [], []
    for f in file_list:
        name = os.path.basename(f)
        if name.startswith("ports_baseline_"):
            base.append(f)
        elif name.startswith("ports_rl_"):
            rl.append(f)
    return base, rl

def load_csv(file_path):
    df = pd.read_csv(file_path)
    required = {"ts","dpid","port"}
    if not required.issubset(df.columns):
        raise ValueError(f"{file_path} missing required columns {required}")
    # drop LOCAL/invalid ports if present in the CSV (extra safety)
    try:
        df = df[~df["port"].isin([0, OFPP_LOCAL])]
    except Exception:
        pass
    df = df.sort_values(["port","dpid","ts"]).reset_index(drop=True)
    df["ts0"] = df["ts"] - df["ts"].min()
    return df

def _per_port_delta_rate(df, col, timecol="ts"):
    """Compute per-second rate from a cumulative counter column."""
    # work per (dpid, port)
    df = df.sort_values(["dpid","port",timecol]).copy()
    df["dt"] = df.groupby(["dpid","port"])[timecol].diff()
    df["dv"] = df.groupby(["dpid","port"])[col].diff()
    # avoid division by zero/NaN
    df["rate"] = np.where(df["dt"] > 0, df["dv"]/df["dt"], np.nan)
    return df

def aggregate_metrics(df):
    cols = set(df.columns)

    # -------- Throughput (Mbps) --------
    if "tx_rate_mbps" in cols and df["tx_rate_mbps"].notna().any():
        thr = df.groupby("ts0")["tx_rate_mbps"].mean().rename("throughput_mbps")
    elif "tx_rate_bps" in cols and df["tx_rate_bps"].notna().any():
        thr = (df.groupby("ts0")["tx_rate_bps"].mean() / 1e6).rename("throughput_mbps")
    elif {"tx_bytes","ts"}.issubset(cols):
        # bytes -> bits per second, then to Mbps
        tmp = _per_port_delta_rate(df[["dpid","port","ts","ts0","tx_bytes"]].copy(),
                                   col="tx_bytes", timecol="ts")
        # dv is bytes; convert to Mbps
        tmp["mbps"] = (tmp["rate"].astype(float) * 8.0) / 1e6
        thr = tmp.groupby("ts0")["mbps"].mean().rename("throughput_mbps")
    else:
        raise ValueError("Cannot derive throughput: need tx_rate_mbps/tx_rate_bps or tx_bytes + ts")

    # -------- Drops/Errors (pkts/s) from counter deltas --------
    drops = None
    if {"rx_dropped","tx_dropped","ts"}.issubset(cols):
        d1 = _per_port_delta_rate(df[["dpid","port","ts","ts0","rx_dropped"]].copy(),
                                  col="rx_dropped", timecol="ts")
        d2 = _per_port_delta_rate(df[["dpid","port","ts","ts0","tx_dropped"]].copy(),
                                  col="tx_dropped", timecol="ts")
        # average across ports; sum rx+tx
        d_agg = d1[["ts0","rate"]].rename(columns={"rate":"rx_rate"}).merge(
            d2[["ts0","rate"]].rename(columns={"rate":"tx_rate"}), how="outer", on="ts0")
        d_agg = d_agg.fillna(0.0)
        d_agg["drops"] = (d_agg["rx_rate"] + d_agg["tx_rate"]) / 2.0
        drops = d_agg.groupby("ts0")["drops"].mean()

    errs = None
    if {"rx_errors","tx_errors","ts"}.issubset(cols):
        e1 = _per_port_delta_rate(df[["dpid","port","ts","ts0","rx_errors"]].copy(),
                                  col="rx_errors", timecol="ts")
        e2 = _per_port_delta_rate(df[["dpid","port","ts","ts0","tx_errors"]].copy(),
                                  col="tx_errors", timecol="ts")
        e_agg = e1[["ts0","rate"]].rename(columns={"rate":"rx_rate"}).merge(
            e2[["ts0","rate"]].rename(columns={"rate":"tx_rate"}), how="outer", on="ts0")
        e_agg = e_agg.fillna(0.0)
        e_agg["errors"] = (e_agg["rx_rate"] + e_agg["tx_rate"]) / 2.0
        errs = e_agg.groupby("ts0")["errors"].mean()

    out = pd.DataFrame({"ts": thr.index, "throughput_mbps": thr.values})
    if drops is not None:
        out = out.merge(pd.DataFrame({"ts": drops.index, "drops": drops.values}),
                        how="left", on="ts")
    else:
        out["drops"] = 0.0

    if errs is not None:
        out = out.merge(pd.DataFrame({"ts": errs.index, "errors": errs.values}),
                        how="left", on="ts")
    else:
        out["errors"] = 0.0

    return out.fillna(0.0)

def plot_metric(ax, df, label, field, ylabel):
    ax.plot(df["ts"], df[field], label=label)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right")

def main():
    ap = argparse.ArgumentParser(description="Plot performance graphs from metrics CSVs")
    ap.add_argument("--files", nargs="+", required=True,
                    help="CSV files (we auto-pick newest Baseline and newest RL).")
    ap.add_argument("--labels", nargs="+", required=False,
                    help="Optional labels; if exactly two, mapped to newest Baseline and newest RL.")
    ap.add_argument("--out", default=None, help="Output folder (default: docs/baseline/plots/)")
    args = ap.parse_args()

    out_dir = args.out or "docs/baseline/plots"
    os.makedirs(out_dir, exist_ok=True)

    base_files, rl_files = classify_files(args.files)
    if args.labels and len(args.labels) == 2:
        base_files = pick_latest(base_files)
        rl_files = pick_latest(rl_files)
        files = base_files + rl_files
        labels = args.labels
    else:
        base_files = pick_latest(base_files) if len(base_files) > 1 else base_files
        rl_files = pick_latest(rl_files) if len(rl_files) > 1 else rl_files
        files = base_files + rl_files
        labels = []
        if base_files: labels.append("Baseline")
        if rl_files: labels.append("RL")

    if not files:
        raise SystemExit("[x] No valid CSVs found among the provided --files")

    fig1, ax1 = plt.subplots()
    fig2, ax2 = plt.subplots()
    fig3, ax3 = plt.subplots()

    for i, file in enumerate(files):
        try:
            df = load_csv(file)
            agg = aggregate_metrics(df)
            label = labels[i] if i < len(labels) else os.path.basename(file)
            plot_metric(ax1, agg, label, "throughput_mbps", "Throughput (Mbps)")
            plot_metric(ax2, agg, label, "drops", "Packet Drops (pkts/s)")
            plot_metric(ax3, agg, label, "errors", "Packet Errors (pkts/s)")
        except Exception as e:
            print(f"[!] Error processing {file}: {e}")

    for fig, name in zip([fig1, fig2, fig3],
                         ["throughput.png", "drops.png", "errors.png"]):
        out_path = os.path.join(out_dir, name)
        fig.tight_layout()
        fig.savefig(out_path, dpi=200)
        print(f"[✓] Saved {out_path}")

    print(f"\nPlots saved in {os.path.abspath(out_dir)}")

if __name__ == "__main__":
    main()
