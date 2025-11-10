#!/usr/bin/env python3
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
    if not files: return []
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
    if 'ts' not in df.columns:
        raise ValueError(f"Missing 'ts' column in {file_path}")
    df = df.sort_values(['dpid','port','ts'] if {'dpid','port'}.issubset(df.columns) else 'ts').reset_index(drop=True)
    # normalize time origin to 0
    df['ts'] = df['ts'] - df['ts'].iloc[0]
    return df

def _has(cols, *names):
    return all(n in cols for n in names)

def _nonzero_mean(s):
    s = pd.Series(s)
    return float((s.fillna(0) != 0).mean())

def derive_rates_from_counters(df):
    """Return a copy with rx_rate_bps/tx_rate_bps computed from rx_bytes/tx_bytes deltas."""
    if not _has(df.columns, 'dpid','port','ts','rx_bytes','tx_bytes'):
        return df.copy()
    d = df.copy()
    d = d.sort_values(['dpid','port','ts'])
    for col, out in [('rx_bytes','rx_rate_bps'), ('tx_bytes','tx_rate_bps')]:
        d[out] = d.groupby(['dpid','port'])[col].diff() * 8.0 / d.groupby(['dpid','port'])['ts'].diff()
        d[out] = d[out].clip(lower=0).fillna(0.0)
    d['rx_rate_mbps'] = d['rx_rate_bps'] / 1e6
    d['tx_rate_mbps'] = d['tx_rate_bps'] / 1e6
    # drops/errors per second if we only have counters
    if _has(d.columns, 'rx_dropped','tx_dropped'):
        d['drop_ps'] = d.groupby(['dpid','port'])['rx_dropped'].diff().fillna(0) + \
                       d.groupby(['dpid','port'])['tx_dropped'].diff().fillna(0)
        d['drop_ps'] = (d['drop_ps'] / d.groupby(['dpid','port'])['ts'].diff()).clip(lower=0).fillna(0.0)
    if _has(d.columns, 'rx_errors','tx_errors'):
        d['err_ps'] = d.groupby(['dpid','port'])['rx_errors'].diff().fillna(0) + \
                      d.groupby(['dpid','port'])['tx_errors'].diff().fillna(0)
        d['err_ps'] = (d['err_ps'] / d.groupby(['dpid','port'])['ts'].diff()).clip(lower=0).fillna(0.0)
    return d

def aggregate_metrics(df):
    """Aggregate system-wide averages per timestamp with generous column support."""
    cols = set(df.columns)
    d = df.copy()

    # Prefer using *_rate_mbps, then *_mbps, then derive from bytes.
    if _has(cols, 'tx_rate_mbps', 'rx_rate_mbps'):
        thr = d.groupby('ts')['tx_rate_mbps'].mean()
    elif _has(cols, 'tx_mbps', 'rx_mbps'):
        thr = d.groupby('ts')['tx_mbps'].mean()
    elif _has(cols, 'tx_rate_bps', 'rx_rate_bps'):
        thr = d.groupby('ts')['tx_rate_bps'].mean() / 1e6
    elif _has(cols, 'tx_bps', 'rx_bps'):
        thr = d.groupby('ts')['tx_bps'].mean() / 1e6
    else:
        # Derive from counters
        d = derive_rates_from_counters(d)
        thr = d.groupby('ts')['tx_rate_mbps'].mean()

    drops = d.groupby('ts')['drop_ps'].mean() if 'drop_ps' in d.columns else pd.Series(0.0, index=thr.index)
    errs  = d.groupby('ts')['err_ps'].mean()  if 'err_ps'  in d.columns else pd.Series(0.0, index=thr.index)

    out = pd.DataFrame({
        'ts': thr.index,
        'throughput_mbps': thr.values,
        'drops': drops.reindex(thr.index, fill_value=0.0).values,
        'errors': errs.reindex(thr.index, fill_value=0.0).values
    })
    out = out.sort_values('ts').reset_index(drop=True)
    return out

def plot_metric(ax, df, label, field, ylabel):
    ax.plot(df['ts'], df[field], label=label)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right")

def main():
    ap = argparse.ArgumentParser(description="Plot performance graphs from metrics CSVs")
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--labels", nargs="+")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_dir = args.out or "docs/baseline/plots"
    os.makedirs(out_dir, exist_ok=True)

    base_files, rl_files = classify_files(args.files)
    if args.labels and len(args.labels) == 2:
        base_files, rl_files = pick_latest(base_files), pick_latest(rl_files)
        files, labels = base_files + rl_files, args.labels
    else:
        base_files = pick_latest(base_files) if len(base_files) > 1 else base_files
        rl_files   = pick_latest(rl_files)   if len(rl_files) > 1 else rl_files
        files = base_files + rl_files
        labels = (["Baseline"] if base_files else []) + (["RL"] if rl_files else [])

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

            # Sanity prints
            nz = _nonzero_mean(agg['throughput_mbps'])
            if nz == 0.0:
                print(f"[warn] {os.path.basename(file)}: throughput appears all zeros; "
                      "check that traffic was generated and counters are changing.")
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
