# Real-Time Dynamic Traffic Routing in SDN (AI-Enhanced RL)

Ryu-based OpenFlow13 controller with:
- Topology discovery + k-shortest paths
- REST API for stats/paths/actions
- Flow install/delete with cookies + timeouts
- Bandit + LinUCB agents to select paths using live stats
- Derived link utilization (`/metrics/links`)
- OpenAPI spec (`/openapi.yaml`)

## Quick start (controller)
```bash
# Ensure Ryu is installed for Python 3.11 (see requirements.vm.txt notes)
# Example with pyenv:
#   pyenv install 3.11.9
#   pyenv virtualenv 3.11.9 ryu311
#   pyenv activate ryu311
#   pip install -r requirements.vm.txt
#   pip install "setuptools<66" "wheel<0.41" "ryu==4.34"

./scripts/run_ryu.sh --ofp-port 6633 --wsapi-port 8080
curl http://127.0.0.1:8080/api/v1/health
