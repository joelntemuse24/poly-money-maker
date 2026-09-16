#!/usr/bin/env bash
# Optional SSH to the live VM for Cloud Agents.
# POLY_VM_SSH_KEY          -> read-only poly-auditor  (Host poly-vm)
# POLY_VM_SSH_OPERATOR_KEY -> allowlisted poly-operator (Host poly-vm-rw)
# Never prints a key. Safe to rerun. Missing secrets are a no-op.
set -euo pipefail

if [ -z "${POLY_VM_SSH_KEY:-}" ] && [ -z "${POLY_VM_SSH_OPERATOR_KEY:-}" ]; then
  exit 0
fi

ssh_dir="${HOME}/.ssh"
config_path="${ssh_dir}/config"

mkdir -p "${ssh_dir}"
chmod 700 "${ssh_dir}"
umask 077

write_key() {
  local src="$1"
  local dest="$2"
  local key="${src//$'\r'/}"
  printf '%s\n' "${key}" > "${dest}"
  chmod 600 "${dest}"
}

ensure_host() {
  local host="$1"
  local block="$2"
  if [ -f "${config_path}" ] && grep -qE "^[[:space:]]*Host[[:space:]]+${host}([[:space:]]|$)" "${config_path}"; then
    return 0
  fi
  {
    if [ -s "${config_path}" ]; then
      printf '\n'
    fi
    printf '%s\n' "${block}"
  } >> "${config_path}"
}

if [ -n "${POLY_VM_SSH_KEY:-}" ]; then
  write_key "${POLY_VM_SSH_KEY}" "${ssh_dir}/id_ed25519_poly_auditor"
  ensure_host poly-vm "$(cat <<'EOF'
Host poly-vm
  HostName 35.228.146.195
  User poly-auditor
  IdentityFile ~/.ssh/id_ed25519_poly_auditor
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
)"
fi

if [ -n "${POLY_VM_SSH_OPERATOR_KEY:-}" ]; then
  write_key "${POLY_VM_SSH_OPERATOR_KEY}" "${ssh_dir}/id_ed25519_poly_operator"
  ensure_host poly-vm-rw "$(cat <<'EOF'
Host poly-vm-rw
  HostName 35.228.146.195
  User poly-operator
  IdentityFile ~/.ssh/id_ed25519_poly_operator
  IdentitiesOnly yes
  RequestTTY no
  StrictHostKeyChecking accept-new
EOF
)"
fi

if [ -f "${config_path}" ]; then
  chmod 600 "${config_path}"
fi
