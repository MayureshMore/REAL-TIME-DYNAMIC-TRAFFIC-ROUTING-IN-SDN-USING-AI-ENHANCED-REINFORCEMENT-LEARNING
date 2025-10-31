#!/usr/bin/env python3
# Deep Q-Network (DQN) agent for SDN routing
# ------------------------------------------
# - Learns to select best path using neural network
# - Uses experience replay and epsilon decay
# - Compatible with Ryu REST controller
# - Uses throughput vs loss/error for reward computation

import argparse
import random
import time
import json
import os
import sys
import requests
import numpy as np
from collections import deque, defaultdict
import torch
import torch.nn as nn
import torch.optim as optim


# -------------------- Config --------------------
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MAX_BW = 10_000_000.0  # normalize throughput (10 Mbps)
REPLAY_CAPACITY = 5000
BATCH_SIZE = 64
GAMMA = 0.9
LR = 1e-3
EPS_DECAY = 0.995
MIN_EPS = 0.05
SAVE_MODEL = "dqn_model.pt"

# -------------------- API Helpers --------------------
def api_base(host, port): return f"http://{host}:{port}/api/v1"

def _get(url, timeout=6):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[dqn] GET failed:", e)
        return None

def _post(url, payload, timeout=8):
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[dqn] POST failed:", e)
        return None

def get_hosts(base): return _get(f"{base}/hosts") or []
def get_ports(base): return _get(f"{base}/stats/ports") or []
def get_paths(base, src, dst, k): return _get(f"{base}/paths?src_mac={src}&dst_mac={dst}&k={k}") or []
def post_route(base, src, dst, path_id=None, path=None, k=2):
    payload={'src_mac':src,'dst_mac':dst,'k':k}
    if path_id is not None: payload['path_id']=int(path_id)
    if path is not None: payload['path']=list(path)
    return _post(f"{base}/actions/route", payload)

def index_ports(snapshot):
    idx=defaultdict(dict)
    for p in snapshot:
        try: idx[int(p["dpid"])][int(p["port_no"])]=p
        except Exception: pass
    return idx


# -------------------- Feature Extraction --------------------
def path_features(hops, prev_idx, cur_idx, dt):
    """Return numeric features for a path"""
    if dt <= 0: dt = 1e-6
    tx0=rx0=e0=d0=0.0; tx1=rx1=e1=d1=0.0
    for h in hops:
        dpid=int(h["dpid"]); outp=int(h["out_port"])
        p0=prev_idx.get(dpid,{}).get(outp); p1=cur_idx.get(dpid,{}).get(outp)
        if not p0 or not p1: continue
        tx0+=p0.get("tx_bytes",0); tx1+=p1.get("tx_bytes",0)
        rx0+=p0.get("rx_bytes",0); rx1+=p1.get("rx_bytes",0)
        e0+=p0.get("rx_errors",0)+p0.get("tx_errors",0)
        e1+=p1.get("rx_errors",0)+p1.get("tx_errors",0)
        d0+=p0.get("rx_dropped",0)+p0.get("tx_dropped",0)
        d1+=p1.get("rx_dropped",0)+p1.get("tx_dropped",0)
    tx_bps=max(0.0,(tx1-tx0)*8.0/dt)
    rx_bps=max(0.0,(rx1-rx0)*8.0/dt)
    err_rate=max(0.0,(e1-e0)/dt)
    drop_rate=max(0.0,(d1-d0)/dt)
    scale=1e7
    return np.array([
        tx_bps/scale,
        rx_bps/scale,
        err_rate/100.0,
        drop_rate/100.0,
        len(hops)/10.0
    ], dtype=np.float32)


def compute_reward(prev_idx, cur_idx, hops, dt):
    """Reward = normalized throughput - penalty for errors/drops"""
    if dt <= 0: dt = 1e-6
    tx0=rx0=e0=d0=0.0; tx1=rx1=e1=d1=0.0
    for h in hops:
        dpid=int(h["dpid"]); outp=int(h["out_port"])
        p0=prev_idx.get(dpid,{}).get(outp); p1=cur_idx.get(dpid,{}).get(outp)
        if not p0 or not p1: continue
        tx0+=p0.get("tx_bytes",0); tx1+=p1.get("tx_bytes",0)
        e0+=p0.get("rx_errors",0)+p0.get("tx_errors",0)
        e1+=p1.get("rx_errors",0)+p1.get("tx_errors",0)
        d0+=p0.get("rx_dropped",0)+p0.get("tx_dropped",0)
        d1+=p1.get("rx_dropped",0)+p1.get("tx_dropped",0)
    tx_bps=max(0.0,(tx1-tx0)*8.0/dt)
    err=max(0.0,(e1-e0)/dt)
    drop=max(0.0,(d1-d0)/dt)
    reward=(tx_bps/MAX_BW)-(0.001*(err+drop))
    return float(np.clip(reward,-1.0,1.0))


# -------------------- DQN Model --------------------
class DQN(nn.Module):
    def __init__(self, input_dim, output_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, output_dim)
        )
    def forward(self, x): return self.net(x)


# -------------------- Agent --------------------
class DQNAgent:
    def __init__(self, n_actions, state_dim):
        self.model = DQN(state_dim, n_actions).to(DEVICE)
        self.target = DQN(state_dim, n_actions).to(DEVICE)
        self.target.load_state_dict(self.model.state_dict())
        self.memory = deque(maxlen=REPLAY_CAPACITY)
        self.optimizer = optim.Adam(self.model.parameters(), lr=LR)
        self.loss_fn = nn.MSELoss()
        self.epsilon = 1.0
        self.steps = 0

    def act(self, state):
        if random.random() < self.epsilon:
            return random.randrange(self.model.net[-1].out_features)
        with torch.no_grad():
            qvals = self.model(torch.FloatTensor(state).unsqueeze(0).to(DEVICE))
            return int(torch.argmax(qvals).item())

    def remember(self, s, a, r, s2, done):
        self.memory.append((s, a, r, s2, done))

    def replay(self):
        if len(self.memory) < BATCH_SIZE:
            return
        batch = random.sample(self.memory, BATCH_SIZE)
        s,a,r,s2,d = zip(*batch)
        s = torch.FloatTensor(s).to(DEVICE)
        s2 = torch.FloatTensor(s2).to(DEVICE)
        a = torch.LongTensor(a).unsqueeze(1).to(DEVICE)
        r = torch.FloatTensor(r).to(DEVICE)
        d = torch.FloatTensor(d).to(DEVICE)

        q_vals = self.model(s).gather(1,a).squeeze()
        next_q = self.target(s2).max(1)[0]
        target_q = r + GAMMA * next_q * (1 - d)
        loss = self.loss_fn(q_vals, target_q.detach())

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def update_target(self):
        self.target.load_state_dict(self.model.state_dict())

    def decay_eps(self):
        self.epsilon = max(MIN_EPS, self.epsilon * EPS_DECAY)


# -------------------- Main Loop --------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--measure-wait", type=float, default=3.0)
    ap.add_argument("--update-target-every", type=int, default=20)
    args = ap.parse_args()

    base = api_base(args.controller, args.port)
    hosts = get_hosts(base)
    if len(hosts) < 2:
        print("[dqn] Not enough hosts learned")
        sys.exit(1)
    src, dst = hosts[0]["mac"], hosts[1]["mac"]

    # Initialize environment
    paths = get_paths(base, src, dst, args.k)
    if not paths:
        print("[dqn] No paths available")
        sys.exit(1)
    n_actions = len(paths)
    state_dim = 5  # feature vector length

    agent = DQNAgent(n_actions, state_dim)
    if os.path.exists(SAVE_MODEL):
        agent.model.load_state_dict(torch.load(SAVE_MODEL, map_location=DEVICE))
        agent.target.load_state_dict(agent.model.state_dict())
        print("[dqn] Loaded pretrained model")

    prev_ports = get_ports(base)
    prev_idx = index_ports(prev_ports)
    prev_state = np.zeros(state_dim, dtype=np.float32)

    for t in range(args.trials):
        paths = get_paths(base, src, dst, args.k)
        if not paths:
            time.sleep(2)
            continue

        # compute state (features of each path)
        cur_ports = get_ports(base)
        cur_idx = index_ports(cur_ports)
        states = [path_features(p["hops"], prev_idx, cur_idx, dt=1.0) for p in paths]
        avg_state = np.mean(states, axis=0)
        action = agent.act(avg_state)

        print(f"[t={t}] Choosing path_id={action} ε={agent.epsilon:.3f}")
        post_route(base, src, dst, path_id=action, k=args.k)

        time.sleep(args.measure_wait)
        next_ports = get_ports(base)
        next_idx = index_ports(next_ports)
        reward = compute_reward(prev_idx, next_idx, paths[action]["hops"], args.measure_wait)
        next_state = np.mean([path_features(p["hops"], next_idx, next_idx, 1.0) for p in paths], axis=0)

        agent.remember(avg_state, action, reward, next_state, False)
        agent.replay()
        agent.decay_eps()

        if t % args.update_target_every == 0:
            agent.update_target()
            print("[dqn] target updated")

        prev_idx = next_idx
        prev_state = next_state

    torch.save(agent.model.state_dict(), SAVE_MODEL)
    print("[dqn] Training complete; model saved.")

if __name__ == "__main__":
    sys.exit(main())
