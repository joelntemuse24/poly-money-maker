"""Idempotent optional SSH setup for Cursor Cloud → poly-vm (no live key)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ENV_JSON = REPO / ".cursor" / "environment.json"
SETUP_SH = REPO / ".cursor" / "setup_poly_vm_ssh.sh"

FAKE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "TEST_PLACEHOLDER_NOT_A_REAL_KEY\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

HOST_BLOCK_MARKERS = (
    "Host poly-vm",
    "HostName 35.228.146.195",
    "User poly-auditor",
    "IdentityFile ~/.ssh/id_ed25519_poly_auditor",
    "StrictHostKeyChecking accept-new",
)


def _run_setup(home: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.pop("POLY_VM_SSH_KEY", None)
    if env:
        run_env.update(env)
    run_env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(SETUP_SH)],
        cwd=str(REPO),
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _count_host_blocks(config_text: str) -> int:
    return sum(1 for line in config_text.splitlines() if line.strip() == "Host poly-vm")


class EnvironmentInstallContractTests(unittest.TestCase):
    def test_install_keeps_venv_and_calls_ssh_helper(self):
        payload = json.loads(ENV_JSON.read_text(encoding="utf-8"))
        install = payload["install"]
        self.assertIn("python3.12-venv", install)
        self.assertIn("python3 -m venv .venv", install)
        self.assertIn(".venv/bin/pip install -U pip", install)
        self.assertIn(".venv/bin/pip install -r requirements.txt", install)
        self.assertIn("setup_poly_vm_ssh.sh", install)
        start = payload.get("start", "")
        self.assertIn("setup_poly_vm_ssh.sh", start)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", install)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", start)
        self.assertNotIn("PRIVATE_KEY", install)
        self.assertNotRegex(install, r"-----BEGIN")

    def test_helper_script_is_executable_and_has_no_embedded_key(self):
        self.assertTrue(SETUP_SH.is_file())
        mode = SETUP_SH.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        text = SETUP_SH.read_text(encoding="utf-8")
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", text)
        self.assertIn("POLY_VM_SSH_KEY", text)
        for marker in HOST_BLOCK_MARKERS:
            self.assertIn(marker, text)


class SetupPolyVmSshTests(unittest.TestCase):
    def test_unset_secret_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = _run_setup(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((home / ".ssh").exists())
            self.assertNotIn("BEGIN OPENSSH", result.stdout + result.stderr)
            self.assertNotIn(FAKE_KEY.splitlines()[1], result.stdout + result.stderr)

    def test_writes_key_and_host_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = _run_setup(home, {"POLY_VM_SSH_KEY": FAKE_KEY})
            self.assertEqual(result.returncode, 0, result.stderr)
            ssh_dir = home / ".ssh"
            key_path = ssh_dir / "id_ed25519_poly_auditor"
            config_path = ssh_dir / "config"
            self.assertEqual(stat.S_IMODE(ssh_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            written = key_path.read_text(encoding="utf-8")
            self.assertEqual(written, FAKE_KEY + "\n")
            config = config_path.read_text(encoding="utf-8")
            for marker in HOST_BLOCK_MARKERS:
                self.assertIn(marker, config)
            self.assertEqual(_count_host_blocks(config), 1)
            self.assertNotIn("BEGIN OPENSSH", result.stdout + result.stderr)

    def test_second_run_does_not_duplicate_host_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = _run_setup(home, {"POLY_VM_SSH_KEY": FAKE_KEY})
            second = _run_setup(home, {"POLY_VM_SSH_KEY": FAKE_KEY + "\nupdated"})
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            config = (home / ".ssh" / "config").read_text(encoding="utf-8")
            self.assertEqual(_count_host_blocks(config), 1)
            written = (home / ".ssh" / "id_ed25519_poly_auditor").read_text(encoding="utf-8")
            self.assertIn("updated", written)

    def test_preserves_existing_unrelated_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir(mode=0o700)
            config_path = ssh_dir / "config"
            config_path.write_text("Host other\n  HostName example.com\n", encoding="utf-8")
            os.chmod(config_path, 0o600)
            result = _run_setup(home, {"POLY_VM_SSH_KEY": FAKE_KEY})
            self.assertEqual(result.returncode, 0, result.stderr)
            config = config_path.read_text(encoding="utf-8")
            self.assertIn("Host other", config)
            self.assertIn("HostName example.com", config)
            self.assertEqual(_count_host_blocks(config), 1)
            self.assertIn("Host poly-vm", config)


if __name__ == "__main__":
    unittest.main()
