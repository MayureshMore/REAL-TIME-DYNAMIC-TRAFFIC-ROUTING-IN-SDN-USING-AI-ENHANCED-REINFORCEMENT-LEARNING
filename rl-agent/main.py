# rl-agent/main.py
# Placeholder RL loop: pulls stats from controller REST and prints a trivial routing decision


import os
import time
import requests


CONTROLLER_IP = os.getenv('CONTROLLER_IP', '127.0.0.1')
BASE = f"http://{CONTROLLER_IP}:8080" # If using Ryu's WSGI default, it's 8080




def fetch_port_stats():
    try:
    r = requests.get(f"{BASE}/api/v1/stats/ports", timeout=2)
r.raise_for_status()
return r.json()
except Exception as e:
print("Error fetching port stats:", e)
return []




def choose_path(stats):
# TODO: replace with DQN later
# trivial heuristic: always choose path-A for now
return "path-A"




if __name__ == "__main__":
    print("RL agent stub started. Controller:", BASE)
for i in range(5):
    stats = fetch_port_stats()
action = choose_path(stats)
print(f"tick={i} ports={len(stats)} action={action}")
time.sleep(1)