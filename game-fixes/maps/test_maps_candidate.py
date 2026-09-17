#!/usr/bin/env python3
"""Offline tests for the maps entitlement tooling. No device, no originals."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.resolve().parents[1]
ORIG_LIB = (ROOT / "backups/usb-20260910T005449Z/"
            "split_config.armeabi_v7a.apk")

ALLOWLIST_PATH = HERE / "OWNERSHIP-ALLOWLIST.json"
VERIFY = HERE / "verify_maps_invariants.py"
COMPOSE = HERE / "compose_maps_candidate.py"
PATCHER = HERE / "patch_maps_ownership.py"

PATCH_OFFSET = 0x133014
ORIGINAL_PATCH_SITE = bytes.fromhex("80b56f4670f79cebbde8804093f0debd")
PATCHED_SITE = bytes.fromhex("80b56f46012080bdbf00bf00bf00bf00")
PATCHED_LIB_SHA256 = (
    "57c81b10bd86fad90002307c4d944d6f1a3f3dbf1b066f78fd0b04bdeb9b0166")
COMBINED_LIB_SHA256 = (
    "d6a7fc03bb7b63b52e0ddb4b523147ce95d4f888810ced3e63477cd589a81b4d")
SOUND_LIB_SHA256 = (
    "dfc1a520a683959ab9fb0be380b3c31e7e19530cc26c998e3c82ab0a1ae89652")
SOUND_LIB_PATH = Path(
    "/tmp/opencode/audio-candidate/libtrueaxis.so.patched")
SENTINEL_SITES = (0x133046, 0x13D846, 0x13D944,
                  0x14BFBC, 0x14C10A, 0x14C11C)


def staged_lib_copy(tmpdir: Path) -> Path:
    with zipfile.ZipFile(ORIG_LIB) as z:
        data = z.read("lib/armeabi-v7a/libtrueaxis.so")
    out = tmpdir / "libtrueaxis.so"
    out.write_bytes(data)
    return out


class AllowlistTests(unittest.TestCase):
    def test_exact_ten_content_skus(self):
        doc = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        skus = doc["content_skus"]
        self.assertEqual(len(skus), 10)
        # Literal sic spelling required; corrected spelling forbidden.
        self.assertIn("jcs2_patforming_1_75", skus)
        self.assertNotIn("jcs2_platforming_1_75", skus)
        self.assertNotIn("jcs2_remove_ads", skus)
        self.assertIn("jcs2_mega_pack", skus)
        self.assertIn("jcs2_level_editor", skus)

    def test_skus_present_in_original_lib(self):
        with zipfile.ZipFile(ORIG_LIB) as z:
            data = z.read("lib/armeabi-v7a/libtrueaxis.so")
        skus = json.loads(
            ALLOWLIST_PATH.read_text(encoding="utf-8"))["content_skus"]
        for sku in skus:
            self.assertIn(sku.encode() + b"\x00", data, sku)


class VerifierTests(unittest.TestCase):
    def test_passes_on_original_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            proc = subprocess.run(
                [sys.executable, str(VERIFY), str(lib)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PASS", proc.stdout)

    def test_rejects_six_site_patch(self):
        # Simulate the OLD (wrong-for-maps) sentinel patch at one site.
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            data = bytearray(lib.read_bytes())
            self.assertEqual(data[0x133046:0x133048], bytes.fromhex("c06b"))
            data[0x133046:0x133048] = bytes.fromhex("0020")
            lib.write_bytes(bytes(data))
            proc = subprocess.run(
                [sys.executable, str(VERIFY), str(lib)],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("six-site progression patch", proc.stdout)

    def test_rejects_blanket_purchase_patch(self):
        # Simulate a blanket Store_IsItemPurchased=true (movs r0,#1; pop).
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            data = bytearray(lib.read_bytes())
            data[0x10727E:0x107282] = bytes.fromhex("0120bd80")
            lib.write_bytes(bytes(data))
            proc = subprocess.run(
                [sys.executable, str(VERIFY), str(lib)],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("purchased load", proc.stdout)

    def test_rejects_wrong_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "libtrueaxis.so"
            lib.write_bytes(b"\x7fELF" + b"\x00" * 100)
            proc = subprocess.run(
                [sys.executable, str(VERIFY), str(lib)],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)

    def test_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            before = hashlib.sha256(lib.read_bytes()).hexdigest()
            subprocess.run(
                [sys.executable, str(VERIFY), str(lib)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(hashlib.sha256(lib.read_bytes()).hexdigest(),
                             before)


class PatchTests(unittest.TestCase):
    def test_exact_patch_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            data = lib.read_bytes()
            self.assertEqual(data[PATCH_OFFSET:PATCH_OFFSET + 16],
                             ORIGINAL_PATCH_SITE)
            proc = subprocess.run(
                [sys.executable, str(PATCHER), "--apply", "--no-backup",
                 str(lib)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            patched = lib.read_bytes()
            self.assertEqual(patched[PATCH_OFFSET:PATCH_OFFSET + 16],
                             PATCHED_SITE)
            self.assertEqual(hashlib.sha256(patched).hexdigest(),
                             PATCHED_LIB_SHA256)
            # 12-byte footprint: first 4 bytes (push/mov) unchanged.
            diff = [i for i, (a, b) in enumerate(zip(data, patched))
                    if a != b]
            self.assertEqual(len(diff), 12)
            self.assertEqual(diff[0], PATCH_OFFSET + 4)
            self.assertEqual(diff[-1], PATCH_OFFSET + 15)
            # Progression sentinels + ownership predicate + setter intact.
            for off in SENTINEL_SITES:
                self.assertEqual(patched[off:off + 2],
                                 bytes.fromhex("c06b"), hex(off))
            self.assertEqual(patched[0x10727E:0x107282],
                             bytes.fromhex("90f85000"))
            self.assertEqual(patched[0x10725C:0x107260],
                             bytes.fromhex("80f85010"))

    def test_apply_revert_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            original = lib.read_bytes()
            subprocess.run(
                [sys.executable, str(PATCHER), "--apply", "--no-backup",
                 str(lib)], check=True, capture_output=True, timeout=60)
            subprocess.run(
                [sys.executable, str(PATCHER), "--apply", "--no-backup",
                 str(lib)], capture_output=True, timeout=60)  # idempotent
            proc = subprocess.run(
                [sys.executable, str(PATCHER), "--revert", "--no-backup",
                 str(lib)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(lib.read_bytes(), original)

    def test_refuses_unexpected_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            data = bytearray(lib.read_bytes())
            data[PATCH_OFFSET + 4:PATCH_OFFSET + 6] = b"\xff\xff"
            lib.write_bytes(bytes(data))
            for flag in ("--apply", "--revert", "--check"):
                proc = subprocess.run(
                    [sys.executable, str(PATCHER), flag, "--no-backup",
                     str(lib)], capture_output=True, text=True, timeout=60)
                self.assertNotEqual(proc.returncode, 0, flag)

    def test_verifier_rejects_maps_patched_as_original(self):
        # The baseline verifier must NOT pass a patched lib as stock:
        # patched lib fails the ORIGINAL byte assertion at 0x133014.
        with tempfile.TemporaryDirectory() as tmp:
            lib = staged_lib_copy(Path(tmp))
            subprocess.run(
                [sys.executable, str(PATCHER), "--apply", "--no-backup",
                 str(lib)], check=True, capture_output=True, timeout=60)
            data = lib.read_bytes()
            self.assertNotEqual(
                data[PATCH_OFFSET:PATCH_OFFSET + 16], ORIGINAL_PATCH_SITE)


class ComposerTests(unittest.TestCase):
    def test_stage_provenance_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "maps-candidate"
            proc = subprocess.run(
                [sys.executable, str(COMPOSE), "--stage", str(stage)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            prov = json.loads(
                (stage / "maps-provenance.json").read_text(encoding="utf-8"))
            self.assertFalse(prov["apk_bytes_modified"])
            self.assertIn("ORIGINAL", prov["signature"])
            self.assertEqual(len(prov["original_apk_sha256"]), 5)
            self.assertEqual(len(prov["ownership_allowlist"]), 10)
            # Staged APK bytes identical to originals.
            for name, digest in prov["original_apk_sha256"].items():
                staged = stage / "apks" / name
                self.assertTrue(staged.is_file())
                self.assertEqual(
                    hashlib.sha256(staged.read_bytes()).hexdigest(), digest)
            # Sound merge slot present and pending.
            self.assertIn("sound_merge_slot", prov)
            self.assertIn("pending", prov["sound_merge_slot"]["status"])

    def test_refuses_existing_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "maps-candidate"
            stage.mkdir()
            proc = subprocess.run(
                [sys.executable, str(COMPOSE), "--stage", str(stage)],
                capture_output=True, text=True, timeout=120)
            self.assertNotEqual(proc.returncode, 0)

    def test_derivative_provenance_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "maps-deriv"
            proc = subprocess.run(
                [sys.executable, str(COMPOSE), "--derivative", str(stage)],
                capture_output=True, text=True, timeout=600)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            prov = json.loads(
                (stage / "maps-derivative-provenance.json").read_text(
                    encoding="utf-8"))
            self.assertTrue(prov["apk_bytes_modified"])
            self.assertEqual(len(prov["derivative_apk_sha256"]), 5)
            self.assertEqual(prov["derivative_lib_sha256"],
                             PATCHED_LIB_SHA256)
            self.assertNotEqual(
                prov["derivative_cert_sha256_fingerprint"],
                prov["original_cert_sha256_fingerprint"])
            self.assertEqual(prov["patch"]["changed_bytes"], 12)
            for name, entry in prov["derivative_alignment"].items():
                self.assertEqual(entry["stored_misaligned"], 0, name)
            self.assertGreater(
                sum(e["deflated_entries"]
                    for e in prov["derivative_alignment"].values()), 0)
            signed = stage / "apks-signed"
            for name, digest in prov["derivative_apk_sha256"].items():
                path = signed / name
                self.assertTrue(path.is_file())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            # Signed split carries the patched lib; manifest unchanged.
            with zipfile.ZipFile(
                    signed / "split_config.armeabi_v7a.apk") as z:
                self.assertEqual(
                    hashlib.sha256(
                        z.read("lib/armeabi-v7a/libtrueaxis.so")
                    ).hexdigest(), PATCHED_LIB_SHA256)
            with zipfile.ZipFile(
                    signed / "split_config.armeabi_v7a.apk") as znew, \
                 zipfile.ZipFile(
                     ROOT / "backups/usb-20260910T005449Z/"
                     "split_config.armeabi_v7a.apk") as zorig:
                self.assertEqual(znew.read("AndroidManifest.xml"),
                                 zorig.read("AndroidManifest.xml"))
            # Fidelity: entry methods match stock (resources.arsc STORED).
            with zipfile.ZipFile(signed / "base.apk") as znew, \
                 zipfile.ZipFile(
                     ROOT / "backups/usb-20260910T005449Z/base.apk") as zorig:
                methods_new = {i.filename: i.compress_type
                               for i in znew.infolist() if not i.is_dir()}
                methods_orig = {i.filename: i.compress_type
                                for i in zorig.infolist()
                                if not i.is_dir()
                                and not i.filename.startswith("META-INF/")}
                for name, method in methods_orig.items():
                    self.assertEqual(methods_new[name], method, name)
                self.assertEqual(methods_new["resources.arsc"],
                                 zipfile.ZIP_STORED)


class CombinedTests(unittest.TestCase):
    def test_sound_first_combined_build(self):
        if not SOUND_LIB_PATH.is_file():
            self.skipTest("sound staged lib absent (scratch input)")
        self.assertEqual(
            hashlib.sha256(SOUND_LIB_PATH.read_bytes()).hexdigest(),
            SOUND_LIB_SHA256)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "maps-comb"
            proc = subprocess.run(
                [sys.executable, str(COMPOSE), "--combined", str(stage),
                 "--sound-lib", str(SOUND_LIB_PATH)],
                capture_output=True, text=True, timeout=600)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            prov = json.loads(
                (stage / "maps-combined-provenance.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(prov["combined_lib_sha256"],
                             COMBINED_LIB_SHA256)
            self.assertEqual(prov["combined_lib_changed_bytes"], 16)
            self.assertEqual(prov["sound_lib_sha256"], SOUND_LIB_SHA256)
            self.assertEqual(len(prov["derivative_apk_sha256"]), 5)
            for name, entry in prov["derivative_alignment"].items():
                self.assertEqual(entry["stored_misaligned"], 0, name)
            combined = (stage / "libtrueaxis-combined.so").read_bytes()
            self.assertEqual(
                hashlib.sha256(combined).hexdigest(), COMBINED_LIB_SHA256)
            # Footprint: exactly sound 4B + maps 12B vs original.
            with zipfile.ZipFile(ORIG_LIB) as z:
                orig = z.read("lib/armeabi-v7a/libtrueaxis.so")
            diff = [i for i, (a, b) in enumerate(zip(orig, combined))
                    if a != b]
            self.assertEqual(len(diff), 16)
            self.assertIn(0x154a7c, diff)
            self.assertIn(0x133018, diff)


if __name__ == "__main__":
    unittest.main(verbosity=2)