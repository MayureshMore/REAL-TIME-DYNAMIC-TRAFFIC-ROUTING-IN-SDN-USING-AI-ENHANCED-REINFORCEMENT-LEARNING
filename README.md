# Real-Time Dynamic Traffic Routing in SDN using AI-Enhanced RL


This repo contains:
- **Ryu controller app** with REST stats (`controller-apps/monitor_rest.py`)
- **Mininet topologies** (single + two-path)
- **Metrics logger** to CSV
- **RL agent stub** (replace with DQN/GNN later)


## Quickstart (Lab Host)
```bash
# Start controller (port 6633)
./scripts/run_ryu.sh


# Single-topo smoke test
./scripts/run_mininet.sh 127.0.0.1
# Mininet> pingall


# Two-path demo
./scripts/run_mininet_two_path.sh 127.0.0.1


# Metrics logging
python3 scripts/metrics/log_stats.py --controller 127.0.0.1 --port 8080 --interval 2 --out docs/baseline/sample.csv