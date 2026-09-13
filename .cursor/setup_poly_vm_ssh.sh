#!/usr/bin/env bash
# Optional read-only SSH to the live VM as poly-auditor.
# Requires POLY_VM_SSH_KEY (ed25519 private key text) at runtime.
# Never prints the key. Safe to rerun.
set -euo pipefail

if [ -z "${POLY_VM_SSH_KEY:-}" ]; then
  exit 0
fi

ssh_dir="${HOME}/.ssh"
key_path="${ssh_dir}/id_ed25519_poly_auditor"
config_path="${ssh_dir}/config"

mkdir -p "${ssh_dir}"
chmod 700 "${ssh_dir}"

umask 077
key="${POLY_VM_SSH_KEY//$'\r'/}"
printf '%s\n' "${key}" > "${key_path}"
chmod 600 "${key_path}"

if [ ! -f "${config_path}" ] || ! grep -qE '^[[:space:]]*Host[[:space:]]+poly-vm([[:space:]]|$)' "${config_path}"; then
  {
    if [ -s "${config_path}" ]; then
      printf '\n'
    fi
    cat <<'EOF'
Host poly-vm
  HostName 35.228.146.195
  User poly-auditor
  IdentityFile ~/.ssh/id_ed25519_poly_auditor
  StrictHostKeyChecking accept-new
EOF
  } >> "${config_path}"
fi
chmod 600 "${config_path}"
