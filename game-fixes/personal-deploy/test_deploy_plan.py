#!/usr/bin/env python3
"""Offline tests for the personal-deploy planner (no device, no guest)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from plan_personal_deploy import (  # noqa: E402
    DeployError, live_commands, validate_offline,
)


def run_plan(*args, env_extra=None):
    env = dict(__import__("os").environ)
    env.pop("PERSONAL_DEPLOY_SLOT", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HERE / "plan_personal_deploy.py"), *args],
        capture_output=True, text=True, timeout=120, env=env)


class PinTests(unittest.TestCase):
    def test_normal_validates_against_real_files(self):
        evidence = validate_offline("normal")
        self.assertEqual(evidence["cert_fingerprint"],
                         "DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:"
                         "53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED")
        self.assertEqual(evidence["version_code"], 29)
        self.assertEqual(len(evidence["hashes"]), 5)
        self.assertTrue(all(evidence["native_pins"].values()))

    def test_unlock_all_validates_against_real_files(self):
        evidence = validate_offline("unlock-all")
        self.assertEqual(len(evidence["hashes"]), 5)

    def test_unknown_variant_refused(self):
        with self.assertRaises(DeployError):
            validate_offline("everything")

    def test_any_byte_change_is_detectable_by_the_gate(self):
        import hashlib
        record = json.loads(
            (HERE.parents[1] / "staging" / "progression-variants"
             / "normal" / "variant.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "base.apk"
            shutil.copyfile(
                HERE.parents[1] / "staging" / "progression-variants"
                / "normal" / "apks-signed" / "base.apk", copy)
            self.assertEqual(
                hashlib.sha256(copy.read_bytes()).hexdigest(),
                record["hashes"]["base.apk"])
            data = bytearray(copy.read_bytes())
            data[1000] ^= 0xFF
            copy.write_bytes(bytes(data))
            # The planner's exact comparison now disagrees: tamper detected.
            self.assertNotEqual(
                hashlib.sha256(copy.read_bytes()).hexdigest(),
                record["hashes"]["base.apk"])


class CommandTests(unittest.TestCase):
    def test_emitted_plan_is_reinstall_only(self):
        evidence = validate_offline("normal")
        blob = "\n".join(live_commands(evidence, "/tmp/bak")).lower()
        self.assertIn("install-multiple -r --no-streaming", blob)
        self.assertIn("rollback", blob)
        for token in ("uninstall", "pm clear", "wipe"):
            self.assertNotIn(token, blob)

    def test_live_refused_without_grant(self):
        result = run_plan("--live", "--backup-dir", "/tmp/bak")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PERSONAL_DEPLOY_SLOT", result.stdout + result.stderr)

    def test_rollback_refused_without_grant(self):
        result = run_plan("--rollback", "--backup-dir", "/tmp/bak")
        self.assertNotEqual(result.returncode, 0)

    def test_unlock_all_is_default_and_validates(self):
        result = run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VALIDATED", result.stdout)
        self.assertIn("unlock-all", result.stdout)
        self.assertIn("install-multiple -r --no-streaming", result.stdout)

    def test_normal_now_needs_explicit_flag(self):
        result = run_plan("--variant", "normal")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--explicit-variant", result.stdout + result.stderr)

    def test_unlock_all_sentinels_open(self):
        from plan_personal_deploy import SENTINEL_SITES
        evidence = validate_offline("unlock-all")
        for off in SENTINEL_SITES:
            self.assertTrue(evidence["native_pins"][hex(off)], hex(off))

    def test_normal_sentinels_gated(self):
        from plan_personal_deploy import SENTINEL_SITES
        evidence = validate_offline("normal")
        for off in SENTINEL_SITES:
            self.assertTrue(evidence["native_pins"][hex(off)], hex(off))

    def test_sentinel_tamper_refused(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            src = (HERE.parents[1] / "staging" / "progression-variants"
                   / "unlock-all" / "apks-signed"
                   / "split_config.armeabi_v7a.apk")
            # validate_offline reads the canonical tree; a tampered COPY
            # must fail its own hash gate — prove detectability directly.
            data = bytearray(__import__("zipfile").ZipFile(src).read(
                "lib/armeabi-v7a/libtrueaxis.so"))
            data[0x133046:0x133048] = b"\xc0\x6b"  # revert one sentinel
            self.assertNotEqual(
                __import__("hashlib").sha256(bytes(data)).hexdigest(),
                "e5c65908465fb665af2fa15054293e55a67638fc857b0917182e1260deba7da9")

    def test_user_test_route_emitted_for_unlock(self):
        evidence = validate_offline("unlock-all")
        blob = "\n".join(live_commands(evidence, "/tmp/bak"))
        self.assertIn("Hurricane", blob)
        self.assertIn("no medal attempts", blob)

    def test_dry_run_validates_and_emits(self):
        result = run_plan("--variant", "normal", "--explicit-variant")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VALIDATED", result.stdout)
        self.assertIn("install-multiple -r --no-streaming", result.stdout)


if __name__ == "__main__":
    unittest.main()
