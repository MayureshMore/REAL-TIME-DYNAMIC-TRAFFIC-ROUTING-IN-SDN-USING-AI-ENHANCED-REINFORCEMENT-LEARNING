#!/usr/bin/env bash
# setup-vm.sh — one-time VM bootstrap for the SDN controller on Ubuntu/RPi
# - Installs APT deps (OVS, Mininet, build tools, tmux, curl, jq)
# - Optionally installs pyenv + Python 3.9.19 (recommended)
# - Creates venv "ryu39" and pins known-good Ryu deps

set -euo pipefail

VENV_NAME="ryu39"
PY_VER="3.9.19"
PYENV_ROOT="${HOME}/.pyenv"
RYU_BIN="${PYENV_ROOT}/versions/${VENV_NAME}/bin/ryu-manager"
PIP_BIN="${PYENV_ROOT}/versions/${VENV_NAME}/bin/pip"

if [[ $EUID -ne 0 ]]; then SUDO=sudo; else SUDO=; fi
export DEBIAN_FRONTEND=noninteractive

echo "==> APT: base + Mininet/OVS + tools"
$SUDO apt-get update -y
$SUDO apt-get install -y --no-install-recommends \
  git curl tmux jq xz-utils \
  build-essential \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev tk-dev \
  mininet openvswitch-switch python3-openvswitch \
  iperf3 tshark socat net-tools python3-venv

echo "==> Enable + start Open vSwitch"
$SUDO systemctl enable --now openvswitch-switch

if [[ ! -d "${PYENV_ROOT}" ]]; then
  echo "==> Installing pyenv (optional but recommended)"
  git clone https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"
  git clone https://github.com/pyenv/pyenv-doctor.git "${PYENV_ROOT}/plugins/pyenv-doctor"
  git clone https://github.com/pyenv/pyenv-update.git "${PYENV_ROOT}/plugins/pyenv-update"
  git clone https://github.com/pyenv/pyenv-virtualenv.git "${PYENV_ROOT}/plugins/pyenv-virtualenv"
fi

# Ensure pyenv usable now and later
if ! grep -q 'pyenv init - bash' ~/.bashrc 2>/dev/null; then
  cat >> ~/.bashrc <<'RC'
export PYENV_ROOT="$HOME/.pyenv"
[[ -d "$PYENV_ROOT/bin" ]] && export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init - bash)"
eval "$(pyenv virtualenv-init -)"
RC
fi
export PYENV_ROOT PATH="${PYENV_ROOT}/bin:${PATH}"
eval "$(pyenv init - bash)"
eval "$(pyenv virtualenv-init -)"

echo "==> Installing Python ${PY_VER} (idempotent)"
pyenv install -s "${PY_VER}"

if [[ ! -x "${PYENV_ROOT}/versions/${VENV_NAME}/bin/python" ]]; then
  echo "==> Creating virtualenv ${VENV_NAME}"
  pyenv virtualenv "${PY_VER}" "${VENV_NAME}"
fi

echo "==> Python deps (pinned, known-good for Ryu 4.34 on Py3.9)"
"${PIP_BIN}" install -U 'pip<25.3' 'setuptools<66' 'wheel<0.41'
"${PIP_BIN}" install \
  ryu==4.34 \
  eventlet==0.30.2 dnspython==1.16.0 webob==1.8.9 \
  networkx requests jsonschema routes netaddr msgpack \
  matplotlib numpy torch --extra-index-url https://download.pytorch.org/whl/cpu

echo "==> Done."
echo "Next steps:"
echo "  1) Start controller:   WSAPI_PORT=8080 OF_PORT=6633 ./scripts/run_ryu.sh"
echo "  2) Health check:       curl http://127.0.0.1:8080/api/v1/health"
echo "  3) Launch topology:    sudo python3 scripts/topos/two_path.py --no_cli"
