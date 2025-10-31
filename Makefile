# ===========================================================
# Real-Time Dynamic Traffic Routing in SDN (Makefile)
# -----------------------------------------------------------
# Run, test, and visualize experiments easily.
# Usage examples:
#   make run-baseline
#   make run-bandit
#   make run-linucb
#   make run-dqn
#   make plot
# ===========================================================

SHELL := /bin/bash
REPO := $(shell pwd)
PY := python3
RYU := $(HOME)/.pyenv/versions/ryu39/bin/ryu-manager

# -----------------------------
# 1️⃣ Setup / Environment
# -----------------------------
.PHONY: setup clean lint

setup:
	@echo "🔧 Creating venv and installing dependencies..."
	python3 -m venv .venv
	. .venv/bin/activate && pip install -U pip wheel
	. .venv/bin/activate && pip install -r requirements.vm.txt
	@echo "✅ Setup complete."

lint:
	@echo "🧹 Linting Python files..."
	flake8 controller-apps rl-agent scripts || true

clean:
	@echo "🧼 Cleaning logs and temp files..."
	rm -rf __pycache__ */__pycache__ *.pyc *.log *.pt docs/baseline/plots
	@echo "✅ Clean complete."

# -----------------------------
# 2️⃣ Controller
# -----------------------------
.PHONY: run-controller

run-controller:
	@echo "🚦 Starting Ryu controller with REST API..."
	$(RYU) controller-apps/sdn_router_rest.py ryu.topology.switches --observe-links --ofp-tcp-listen-port 6633 --wsapi-port 8080

# -----------------------------
# 3️⃣ Experiments
# -----------------------------
.PHONY: run-baseline run-bandit run-linucb run-dqn

run-baseline:
	@echo "📊 Running baseline experiment..."
	bash scripts/experiments/run_baseline.sh

run-bandit:
	@echo "🤖 Running Bandit (ε-greedy) RL agent..."
	bash scripts/experiments/run_with_rl.sh
	@echo "✅ Bandit RL experiment complete."

run-linucb:
	@echo "🧠 Running LinUCB contextual bandit agent..."
	$(PY) rl-agent/linucb_agent.py --controller 127.0.0.1 --port 8080 --k 2 --trials 100
	@echo "✅ LinUCB experiment complete."

run-dqn:
	@echo "🧬 Running Deep Q-Network agent..."
	$(PY) rl-agent/dqn_agent.py --controller 127.0.0.1 --port 8080 --k 2 --trials 300
	@echo "✅ DQN experiment complete."

# -----------------------------
# 4️⃣ Plotting and Analysis
# -----------------------------
.PHONY: plot compare

plot:
	@echo "📈 Generating performance plots..."
	$(PY) scripts/metrics/plot_results.py \
	  --files docs/baseline/ports_baseline_*.csv docs/baseline/ports_rl_*.csv \
	  --labels Baseline RL

compare:
	@echo "📊 Comparing all experiments (Baseline, RL, DQN)..."
	$(PY) scripts/metrics/plot_results.py \
	  --files docs/baseline/ports_baseline_*.csv docs/baseline/ports_rl_*.csv docs/baseline/ports_dqn_*.csv \
	  --labels Baseline RL DQN

# -----------------------------
# 5️⃣ Utility Targets
# -----------------------------
.PHONY: topo ping

topo:
	@echo "🌐 Launching two-path topology (no CLI)..."
	sudo python3 scripts/topos/two_path.py --controller_ip 127.0.0.1 --no_cli

ping:
	@echo "📡 Testing Mininet connectivity..."
	sudo mn --test pingall
