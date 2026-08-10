#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Poly Money Maker.
# Creates the .venv the bots and systemd units expect, then installs deps.
set -euo pipefail

cd "$(dirname "$0")/.."

# The base image ships python3 + pip but not the venv module.
if ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv >/dev/null
fi

# Match the production layout (deploy/*.service run .venv/bin/python).
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "install.sh: environment ready ($(.venv/bin/python --version))"
