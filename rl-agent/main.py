#!/usr/bin/env python3
import os, time, requests

CONTROLLER_IP = os.getenv('CONTROLLER_IP', '127.0.0.1')
BASE = f"http://{CONTROLLER_IP}:8080"

def fetch_port_stats():
    try:
        r = requests.get(f"{BASE}/api/v1/stats/ports", timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Error fetching port stats:", e)
        return []

def choose_path(stats):
    # TODO: replace with real agent
    return "path-A"

if __name__ == "__main__":
    print("RL agent stub started. Controller:", BASE)
    for i in range(5):
        stats = fetch_port_stats()
        action = choose_path(stats)
        print(f"tick={i} ports={len(stats)} action={action}")
        time.sleep(1)
