#!/usr/bin/env python3
"""Offline tests for the sound-crash patch v3 (no device, no originals mutated)."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patch_audio import apply_patch, lib_state, load_spec, sha256, PatchError  # noqa: E402

TARGET, SITES = load_spec()
BY_OFF = {s["offset"]: s for s in SITES}
# maps ownership range + six progression sentinels: real patch must stay clear
MAPS_OWNERSHIP_RANGE = range(0x133014, 0x133024)
MAPS_SITES = [0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C]

REAL_APK = (HERE.parents[1] / "backups" / "usb-20260910T005449Z"
            / "split_config.armeabi_v7a.apk")

LIB_SIZE = 0x155000


def synthetic_lib(state="original", corrupt_off=None):
    data = bytearray(LIB_SIZE)
    for site in SITES:
        off = site["offset"]
        pre = site["context_before"]
        post = site["context_after"]
        data[off - len(pre):off] = pre
        data[off + len(site["orig"]):off + len(site["orig"]) + len(post)] = post
        if state == "original":
            data[off:off + len(site["orig"])] = site["orig"]
        elif state == "patched":
            data[off:off + len(site["orig"])] = site["patched"]
        else:
            data[off:off + len(site["orig"])] = b"\x00" * len(site["orig"])
    if corrupt_off is not None:
        data[corrupt_off] ^= 0xFF
    return bytes(data)


def gate_for(data):
    return {"expected_lib_sha256": sha256(data), "sites": SITES}


class SpecTests(unittest.TestCase):
    def test_three_sites_expected_shape(self):
        self.assertEqual(
            sorted(BY_OFF),
            sorted([0x154A7C, 0x11E318, 0x154A1C]))
        for site in SITES:
            self.assertEqual(len(site["orig"]), len(site["patched"]))
        self.assertEqual(BY_OFF[0x154A7C]["patched"], bytes.fromhex("00bf00bf"))
        self.assertEqual(BY_OFF[0x11E318]["orig"], bytes.fromhex("017085f760ee"))
        self.assertEqual(BY_OFF[0x11E318]["patched"], bytes.fromhex("00bf00bf00bf"))
        self.assertEqual(BY_OFF[0x154A1C]["patched"], bytes.fromhex("704700bf"))

    def test_no_overlap_with_maps_sites_or_range(self):
        touched = {s["offset"] + i for s in SITES for i in range(len(s["orig"]))}
        for off in MAPS_SITES:
            for delta in range(4):
                self.assertNotIn(off + delta, touched)
        for off in MAPS_OWNERSHIP_RANGE:
            self.assertNotIn(off, touched)

    def test_spec_target_hashes_wellformed(self):
        self.assertEqual(len(TARGET["apk_sha256"]), 64)
        self.assertEqual(len(TARGET["lib_sha256"]), 64)
        self.assertEqual(TARGET["lib_size"], 2227488)


class ApplyTests(unittest.TestCase):
    def test_apply_synthetic(self):
        data = synthetic_lib("original")
        self.assertEqual(lib_state(data, SITES), "original")
        out = apply_patch(data, **gate_for(data))
        self.assertEqual(lib_state(out, SITES), "patched")
        self.assertEqual(len(out), len(data))
        diff = [i for i, (a, b) in enumerate(zip(data, out)) if a != b]
        expected = sorted(s["offset"] + i for s in SITES
                          for i in range(len(s["orig"])))
        self.assertEqual(diff, expected)

    def test_refuses_hash_mismatch(self):
        data = synthetic_lib("original")
        with self.assertRaises(PatchError):
            apply_patch(data, expected_lib_sha256="0" * 64, sites=SITES)

    def test_refuses_context_mismatch(self):
        data = synthetic_lib("original", corrupt_off=SITES[0]["offset"])
        with self.assertRaises(PatchError):
            apply_patch(data, **gate_for(data))

    def test_refuses_shifted_context(self):
        data = synthetic_lib("original", corrupt_off=SITES[0]["offset"] - 1)
        blob = bytes(data)
        self.assertEqual(lib_state(blob, SITES), "unknown")
        with self.assertRaises(PatchError):
            apply_patch(blob, **gate_for(blob))

    def test_refuses_already_patched(self):
        data = synthetic_lib("patched")
        with self.assertRaises(PatchError):
            apply_patch(data, **gate_for(data))

    def test_detects_unknown_state(self):
        data = synthetic_lib("other")
        self.assertEqual(lib_state(data, SITES), "unknown")

    def test_detects_mixed_state(self):
        data = bytearray(synthetic_lib("original"))
        s0 = SITES[0]
        data[s0["offset"]:s0["offset"] + len(s0["orig"])] = s0["patched"]
        self.assertEqual(lib_state(bytes(data), SITES), "mixed")
        with self.assertRaises(PatchError):
            apply_patch(bytes(data), **gate_for(bytes(data)))


class RepoIntegrationTests(unittest.TestCase):
    def test_real_backup_lib_is_original(self):
        with zipfile.ZipFile(REAL_APK) as source:
            data = source.read(TARGET["member"])
        self.assertEqual(sha256(data), TARGET["lib_sha256"])
        self.assertEqual(lib_state(data, SITES), "original")


class ComposerTests(unittest.TestCase):
    def test_compose_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "candidate"
            result = subprocess.run(
                [sys.executable, str(HERE / "compose_audio_candidate.py"),
                 "--stage", str(stage)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            prov = json.loads((stage / "provenance.json").read_text())
            self.assertEqual(prov["state"], "patched")
            self.assertEqual(prov["orig_lib_sha256"], TARGET["lib_sha256"])
            self.assertFalse(prov["guest_modified"])
            staged = (stage / "libtrueaxis.so.patched").read_bytes()
            self.assertEqual(sha256(staged), prov["patched_lib_sha256"])
            self.assertEqual(lib_state(staged, SITES), "patched")

    def test_compose_refuses_existing_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "candidate"
            stage.mkdir()
            result = subprocess.run(
                [sys.executable, str(HERE / "compose_audio_candidate.py"),
                 "--stage", str(stage)], capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)

    def test_compose_never_writes_near_apk(self):
        before = REAL_APK.stat().st_mtime_ns
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, str(HERE / "compose_audio_candidate.py"),
                 "--stage", str(Path(tmp) / "c")],
                capture_output=True, timeout=120)
        self.assertEqual(REAL_APK.stat().st_mtime_ns, before)
        with zipfile.ZipFile(REAL_APK) as source:
            self.assertEqual(sha256(source.read(TARGET["member"])), TARGET["lib_sha256"])


class CandidateV3Tests(unittest.TestCase):
    V3_DIR = HERE / "candidate-v3"
    V3_LIB_SHA256 = ("85020c6475fed1fcdfc3f8274b448660d25bad0c31f45cf95e4e906c0bf71303")

    def test_v3_provenance_matches_spec(self):
        prov = json.loads((self.V3_DIR / "provenance.json").read_text())
        self.assertEqual(prov["state"], "patched")
        self.assertEqual(prov["orig_lib_sha256"], TARGET["lib_sha256"])
        self.assertEqual(prov["patched_lib_sha256"], self.V3_LIB_SHA256)
        self.assertFalse(prov["guest_modified"])
        self.assertEqual(
            [(e["offset"], e["orig"], e["patched"]) for e in prov["sites"]],
            [(e["offset"], e["orig"], e["patched"])
             for e in json.loads((HERE / "audio_patch.json").read_text())["sites"]])
        staged = (self.V3_DIR / "libtrueaxis.so.patched").read_bytes()
        self.assertEqual(sha256(staged), self.V3_LIB_SHA256)
        self.assertEqual(lib_state(staged, SITES), "patched")


class RetestPlanTests(unittest.TestCase):
    def run_plan(self, args, env_extra=None):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "SOUND_RETEST_SLOT"}
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(HERE / "retest-plan.sh")] + args,
            capture_output=True, text=True, timeout=30, env=env)

    def test_plan_refuses_without_slot(self):
        result = self.run_plan([])
        self.assertEqual(result.returncode, 10)
        self.assertIn("slot not granted", result.stdout + result.stderr)

    def test_plan_requires_copy_ok(self):
        result = self.run_plan([], env_extra={"SOUND_RETEST_SLOT": "granted"})
        self.assertEqual(result.returncode, 10)
        self.assertIn("--copy-ok", result.stdout + result.stderr)

    def test_plan_requires_five_explicit_splits(self):
        result = self.run_plan(
            ["--copy-ok", "--base", "/nope.apk"],
            env_extra={"SOUND_RETEST_SLOT": "granted"})
        self.assertEqual(result.returncode, 10)
        self.assertIn("five explicit split", result.stdout + result.stderr)

    def test_plan_refuses_with_emulator_present(self):
        fake = subprocess.Popen(["bash", "-c", "exec -a qemu-system-test sleep 25"])
        try:
            # Durable fixtures: five task-temp files (the script only needs
            # -f existence before the emulator check). No dependency on
            # prior-slot /tmp/opencode/sound-repro artifacts.
            with tempfile.TemporaryDirectory() as tmp:
                splits = []
                for name in ("base.apk", "armeabi.apk", "en.apk",
                             "es.apk", "xhdpi.apk"):
                    path = Path(tmp) / name
                    path.write_bytes(b"fixture")
                    splits.append(path)
                result = self.run_plan(
                    ["--copy-ok",
                     "--base", str(splits[0]),
                     "--armeabi", str(splits[1]),
                     "--en", str(splits[2]),
                     "--es", str(splits[3]),
                     "--xhdpi", str(splits[4])],
                    env_extra={"SOUND_RETEST_SLOT": "granted"})
            self.assertEqual(result.returncode, 11)
            self.assertIn("emulator process present", result.stdout + result.stderr)
        finally:
            fake.kill()

    def test_plan_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(HERE / "retest-plan.sh")],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
