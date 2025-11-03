#!/usr/bin/env bash
#
# setup.sh — Fresh Ubuntu bootstrap (Steps 0–2 only)
# - Installs OS deps, enables OVS
# - Clones/updates the repo
# - Creates Python env via --mode venv | pyenv (default: pyenv)
# - Does NOT start controller or run smoke tests
#
# Examples:
#   bash setup.sh                  # pyenv (recommended)
#   bash setup.sh --mode venv      # use venv route
#   bash setup.sh --repo-dir ~/REAL-TIME-DYNAMIC-... --repo-url https://github.com/<you>/<repo>.git
set -euo pipefail

# -----------------------------
# Args / defaults
# -----------------------------
MODE="pyenv"
REPO_URL="https://github.com/<your-org-or-user>/REAL-TIME-DYNAMIC-TRAFFIC-ROUTING-IN-SDN-USING-AI-ENHANCED-REINFORCEMENT-LEARNING.git"
REPO_DIR="${HOME}/REAL-TIME-DYNAMIC-TRAFFIC-ROUTING-IN-SDN-USING-AI-ENHANCED-REINFORCEMENT-LEARNING"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-pyenv}"; shift 2;;
    --repo-dir) REPO_DIR="${2:?}"; shift 2;;
    --repo-url) REPO_URL="${2:?}"; shift 2;;
    *) echo "Unknown arg: $1"; exit 2;;
  esac
done

if [[ "${MODE}" != "pyenv" && "${MODE}" != "venv" ]]; then
  echo "ERROR: --mode must be 'pyenv' or 'venv'"; exit 2
fi

if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi
export DEBIAN_FRONTEND=noninteractive

echo "==> Mode: ${MODE}"
echo "==> Repo dir: ${REPO_DIR}"
echo "==> Repo url: ${REPO_URL}"

# -----------------------------
# Step 0) System prep
# -----------------------------
echo "==> APT: base + Mininet/OVS + tools"
${SUDO} apt-get update -y
${SUDO} apt-get install -y --no-install-recommends \
  git curl tmux jq xz-utils \
  build-essential \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev tk-dev \
  mininet openvswitch-switch python3-openvswitch \
  iperf3 tshark socat net-tools python3-venv

echo "==> Enable + start Open vSwitch"
${SUDO} systemctl enable --now openvswitch-switch

# -----------------------------
# Step 1) Clone/Update project
# -----------------------------
if [[ -d "${REPO_DIR}/.git" ]]; then
  echo "==> Repo exists; pulling latest"
  git -C "${REPO_DIR}" pull --rebase --autostash || true
else
  echo "==> Cloning repo to ${REPO_DIR}"
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

# -----------------------------
# Step 2) Python environment
# -----------------------------
if [[ "${MODE}" == "venv" ]]; then
  echo "==> Using Python venv route (quick)"
  cd "${REPO_DIR}"
  # Create/refresh venv
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -U pip wheel
  # Pins for Ryu 4.34 compatibility on modern Ubuntu Pythons
  pip install "setuptools<66" "wheel<0.41" "webob<1.9" eventlet==0.30.2 dnspython==1.16.0
  pip install ryu==4.34 networkx requests jsonschema routes netaddr msgpack
  # Agents & plotting (CPU wheels)
  pip install matplotlib numpy torch --extra-index-url https://download.pytorch.org/whl/cpu
  echo "==> venv ready at ${REPO_DIR}/.venv"
else
  echo "==> Using pyenv route (reproducible; recommended)"
  export PYENV_ROOT="$HOME/.pyenv"
  if [[ ! -d "${PYENV_ROOT}" ]]; then
    echo "==> Installing pyenv"
    git clone https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"
    git clone https://github.com/pyenv/pyenv-virtualenv.git "${PYENV_ROOT}/plugins/pyenv-virtualenv"
  fi
  export PATH="${PYENV_ROOT}/bin:${PATH}"
  # init for current shell only
  eval "$(pyenv init - bash)"
  eval "$(pyenv virtualenv-init -)"

  # Interpreter + env
  pyenv install -s 3.9.19
  pyenv versions --bare | grep -qx ryu39 || pyenv virtualenv 3.9.19 ryu39
  pyenv activate ryu39

  # Pinned toolchain + deps known-good for Ryu 4.34
  pip install -U 'pip<25.3' 'setuptools<66' 'wheel<0.41'
  pip install ryu==4.34 eventlet==0.30.2 dnspython==1.16.0 webob==1.8.9 \
              networkx requests jsonschema routes netaddr msgpack \
              matplotlib numpy torch --extra-index-url https://download.pytorch.org/whl/cpu

  echo "==> pyenv env ready: ${PYENV_ROOT}/versions/ryu39"
fi

echo "==> Setup complete (Steps 0–2)."
echo "Next manual step: start the controller (not done by this script)."
