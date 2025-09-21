#!/usr/bin/env bash
# setup-vm.sh — one-time VM bootstrap for the SDN controller on Ubuntu/RPi
# - Installs APT deps (OVS, Mininet, build tools, tmux, curl, jq)
# - Installs pyenv + Python 3.9.19
# - Creates venv "ryu39" and pins known-good Ryu deps
# - Patches monitor_rest.py health() to return valid JSON

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  iperf3 tshark socat net-tools

echo "==> Enable + start Open vSwitch"
$SUDO systemctl enable --now openvswitch-switch

if [[ ! -d "${PYENV_ROOT}" ]]; then
  echo "==> Installing pyenv"
  git clone https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"
  git clone https://github.com/pyenv/pyenv-doctor.git "${PYENV_ROOT}/plugins/pyenv-doctor"
  git clone https://github.com/pyenv/pyenv-update.git "${PYENV_ROOT}/plugins/pyenv-update"
  git clone https://github.com/pyenv/pyenv-virtualenv.git "${PYENV_ROOT}/plugins/pyenv-virtualenv"
fi

# Ensure pyenv is available in this script AND for future shells
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
  networkx requests jsonschema

echo "==> Patching monitor_rest.py health() to return proper JSON"
APP="${PROJ_DIR}/controller-apps/monitor_rest.py"
if [[ ! -f "${APP}" ]]; then
  echo "ERROR: ${APP} not found. Are you in the repo root?" >&2
  exit 1
fi

# Replace the health() body to ensure WebOb gets bytes (avoids charset TypeError)
pyenv global 3.9.19
pyenv local 3.9.19
pyenv rehash
sudo dpkg --configure -a

python - "$APP" <<'PY'
import io, os, re, sys
p = sys.argv[1]
src = io.open(p, 'r', encoding='utf-8').read()

# Ensure json import exists
if 'import json' not in src:
    src = 'import json\n' + src

# Replace/insert health() method robustly
pat = re.compile(r'(\n\s*)def\s+health\s*\([^)]*\):\s*(?:\n\s+.*?)+?(?=\n\s*def|\n\s*class|\Z)', re.S)
body = r"""\1def health(self, req, **kwargs):
\1    # Always return valid JSON bytes for WebOb<=1.8.x
\1    data = {"status": "ok", "last_stats_ts": getattr(self, "last_stats_ts", 0.0)}
\1    payload = json.dumps(data)
\1    from webob import Response
\1    return Response(content_type="application/json", body=payload.encode("utf-8"))
"""
if pat.search(src):
    src = pat.sub(body, src, count=1)
else:
    # Append if not found
    src += body.replace(r"\1", "\n")

io.open(p, 'w', encoding='utf-8').write(src)
print("Patched:", p)
PY

echo "==> Done. Next: ./run-controller.sh   (or: WSAPI_PORT=8080 OF_PORT=6633 ./scripts/run_ryu.sh)"

