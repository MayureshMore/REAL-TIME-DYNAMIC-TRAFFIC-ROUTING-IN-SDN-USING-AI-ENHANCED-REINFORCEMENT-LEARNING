#!/usr/bin/env python3
# Plot Results for SDN RL Experiments
# Reads port CSVs and plots total throughput (Mbps), drops (pkts/s), errors (pkts/s).
# Works whether the CSV already includes rates or only counters.

import argparse, os, re
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

TS_RE = re.compile(r"(\d{8}_\d{6})")  # e.g., 20251109_193823

def _extract_ts_from_name(path: str):
    m = TS_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except Exception:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path))

def pick_latest(paths):
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return []
    return [sorted(paths, key=_extract_ts_from_name)[-1]]

def classify(files):
    base, rl = [], []
    for f in files:
        name = os.path.basename(f)
        if name.startswith("ports_baseline_"):
            base.append(f)
        elif name.startswith("ports_rl_"):
            rl.append(f)
    return base, rl

def load_csv(path):
    df = pd.read_csv(path)
    if "ts" not in df.columns:
        raise ValueError(f"{path}: missing ts")
    # Normalize time to start at 0 per file
    df = df.sort_values("ts").reset_index(drop=True)
    df["ts"] = df["ts"] - df["ts"].iloc[0]
    return df

def derive_rates_if_needed(df):
    cols = set(df.columns)
    # If rate columns already present and non-zero sometimes, keep them
    if {"tx_rate_bps", "rx_rate_bps"}.issubset(cols) and df["tx_rate_bps"].max() > 0:
        tx_bps = df.groupby("ts")["tx_rate_bps"].sum()
        drops  = df.groupby("ts")["drop_ps"].sum() if "drop_ps" in cols else None
        errs   = df.groupby("ts")["err_ps"].sum() if "err_ps" in cols else None
        return pd.DataFrame({
            "ts": tx_bps.index,
            "throughput_mbps": tx_bps.values / 1e6,
            "drops": (drops.values if drops is not None else 0),
            "errors": (errs.values if errs is not None else 0),
        })

    # Otherwise compute deltas from counters per (dpid,port)
    need = {"dpid","port","tx_bytes","rx_bytes","rx_dropped","tx_dropped","rx_errors","tx_errors"}
    if not need.issubset(cols):
        raise ValueError("CSV lacks needed counters to derive rates.")

    df = df.sort_values(["dpid","port","ts"]).copy()
    for c in ["tx_bytes","rx_bytes","rx_dropped","tx_dropped","rx_errors","tx_errors"]:
        df[f"{c}_prev"] = df.groupby(["dpid","port"])[c].shift(1)
    df["ts_prev"] = df.groupby(["dpid","port"])["ts"].shift(1)

    # deltas and dt
    for c in ["tx_bytes","rx_bytes","rx_dropped","tx_dropped","rx_errors","tx_errors"]:
        df[f"d_{c}"] = df[c] - df[f"{c}_prev"]
    df["dt"] = (df["ts"] - df["ts_prev"]).clip(lower=1e-6)

    # per-port rates
    df["tx_bps"] = (df["d_tx_bytes"].clip(lower=0)) * 8.0 / df["dt"]
    df["drop_ps"] = ((df["d_rx_dropped"].clip(lower=0)) + (df["d_tx_dropped"].clip(lower=0))) / df["dt"]
    df["err_ps"]  = ((df["d_rx_errors"].clip(lower=0))  + (df["d_tx_errors"].clip(lower=0)))  / df["dt"]

    # Aggregate across ports per timestamp
    agg = df.groupby("ts").agg(
        throughput_mbps=("tx_bps","sum"),
        drops=("drop_ps","sum"),
        errors=("err_ps","sum"),
    ).reset_index()
    agg["throughput_mbps"] = agg["throughput_mbps"] / 1e6
    return agg

def plot_series(ax, agg, label, ylabel):
    ax.plot(agg["ts"], agg[label], label=ylabel)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)

def main():
    ap = argparse.ArgumentParser(description="Plot Baseline vs RL from port logs")
    ap.add_argument("--files", nargs="+", required=True,
                    help="CSV list (will auto-pick newest Baseline and newest RL)")
    ap.add_argument("--labels", nargs="+", help="Two labels for Baseline and RL (optional)")
    ap.add_argument("--out", default="docs/baseline/plots", help="Output folder")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    base, rl = classify(args.files)
    base = pick_latest(base) if len(base) > 1 else base
    rl   = pick_latest(rl)   if len(rl)   > 1 else rl
    files = base + rl

    if not files:
        raise SystemExit("[x] No valid input CSVs")

    labels = args.labels if args.labels and len(args.labels) == 2 else None
    if labels is None:
        labels = []
        if base: labels.append("Baseline")
        if rl:   labels.append("RL")

    # Create figures
    fig_t, ax_t = plt.subplots()
    fig_d, ax_d = plt.subplots()
    fig_e, ax_e = plt.subplots()

    for i, path in enumerate(files):
        df = load_csv(path)
        agg = derive_rates_if_needed(df)
        name = labels[i] if i < len(labels) else os.path.basename(path)

        ax_t.plot(agg["ts"], agg["throughput_mbps"], label=name)
        ax_d.plot(agg["ts"], agg["drops"],            label=name)
        ax_e.plot(agg["ts"], agg["errors"],           label=name)

    for ax, ylab in [(ax_t,"Throughput (Mbps)"),
                     (ax_d,"Packet Drops (pkts/s)"),
                     (ax_e,"Packet Errors (pkts/s)")]:
        ax.set_xlabel("Time (s)")
        ax.set_ylabel(ylab)
        ax.legend(loc="upper right")

    fig_t.tight_layout(); fig_d.tight_layout(); fig_e.tight_layout()
    fig_t.savefig(os.path.join(args.out,"throughput.png"), dpi=200)
    fig_d.savefig(os.path.join(args.out,"drops.png"), dpi=200)
    fig_e.savefig(os.path.join(args.out,"errors.png"), dpi=200)
    print(f"[✓] Saved {os.path.abspath(args.out)}/throughput.png")
    print(f"[✓] Saved {os.path.abspath(args.out)}/drops.png")
    print(f"[✓] Saved {os.path.abspath(args.out)}/errors.png")

if __name__ == "__main__":
    main()
