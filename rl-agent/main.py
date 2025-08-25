# rl-agent/main.py
import time

def choose_path(state):
    # TODO: replace with DQN later
    # For now, always pick "path-A"
    return "path-A"

if __name__ == "__main__":
    print("RL Agent stub is running...")
    for i in range(5):
        state = {"congestion": 0.3, "latency": 12}  # fake state
        action = choose_path(state)
        print(f"[tick {i}] state={state} -> action={action}")
        time.sleep(1)
