# 🚦 Real-Time Dynamic Traffic Routing in SDN using AI-Enhanced Reinforcement Learning

Ryu-based OpenFlow13 controller with:
- Topology discovery + k-shortest paths
- REST API for stats, paths, and actions
- Flow install/delete with cookies + timeouts
- Multi-armed Bandit + LinUCB agents to select paths using live stats
- Derived link utilization (`/metrics/links`)
- OpenAPI spec (`/openapi.yaml`)

---

## 👨‍💻 Contributors
- **Mayuresh More**  
- **Zeel Patel**  
- **Omkar Sutar**

---

## ⚡ Quick Start (Controller)
```bash
# Ensure Ryu is installed for Python 3.9+ (see requirements.vm.txt notes)
# Example with pyenv:
#   pyenv install 3.9.19
#   pyenv virtualenv 3.9.19 ryu39
#   pyenv activate ryu39
#   pip install -r requirements.vm.txt

./scripts/run_ryu.sh --ofp-port 6633 --wsapi-port 8080
curl http://127.0.0.1:8080/api/v1/health
```

---

## ⚙️ Running Experiments

### Baseline (shortest path)
```bash
DURATION=900 scripts/experiments/run_baseline.sh | tee baseline_$(date +%Y%m%d_%H%M%S).log
```

### With Reinforcement Learning
```bash
DURATION=900 EPSILON=0.2 WAIT_FOR_PATHS=120 scripts/experiments/run_with_rl.sh | tee rl_$(date +%Y%m%d_%H%M%S).log
```

Logs and CSV results are stored under:
```
docs/baseline/
```

---

## 📊 Results & Visualizations

### Key Observations
- **Baseline:** Higher packet drop, lower throughput.  
- **RL Agent:** Learns optimal multi-path routing, reduces packet drops, improves aggregate throughput.  

### Suggested Visualizations
- Throughput vs Time (line chart)  
- Packet Drops vs Time (line chart)  
- ECDF of Throughput (cumulative distribution)  
- Mean TX & Drops (bar chart)  

---

## 🛠️ Tech Stack
- **SDN Controller:** Ryu  
- **Network Emulator:** Mininet  
- **Switching:** Open vSwitch  
- **RL Agent:** Python (Multi-armed Bandit)  
- **Visualization:** Matplotlib, Pandas  

---

## 📅 Project Status
- ✅ Baseline and RL experiments tested (15-min and 1-hour runs)  
- ✅ Logs and CSV outputs recorded for dashboards  
- 🔄 Next: Optimize reward function for stability  

---

## ⭐ Acknowledgements
Special thanks to our professors and peers at **University of the Pacific** for guidance.
