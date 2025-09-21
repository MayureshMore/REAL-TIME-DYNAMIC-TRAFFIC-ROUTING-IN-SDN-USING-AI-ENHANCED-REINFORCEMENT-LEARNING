# Lab Setup Notes (Raspberry Pi or VM)


## Host provisioning
- Ubuntu Server 22.04/24.04 (64-bit)
- Install: `sudo apt update && sudo apt -y install mininet openvswitch-switch iperf3 tshark python3-venv git`
- Create Ryu venv: `python3 -m venv ~/ryu-venv && source ~/ryu-venv/bin/activate && pip install ryu requests`
- Clone repo to `~/project`


## Controller
```bash
./scripts/run_ryu.sh 