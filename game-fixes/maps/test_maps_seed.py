#!/usr/bin/env python3
"""Tests for the per-SKU populateStore seed hook + variant composition.

Covers: Thumb branch/literal codec round-trips (incl. negative offsets),
seed apply/check/revert/refusal on scratch lib copies (originals never
touched), an independent from-spec decode of the appended stub (separate
decode logic from the generator), built-artifact byte states for the
normal (ownership) and opt-in unlock-all variants, and the reviewer
contract: progression_choice.verify_variant ACCEPTS both emitted
descriptors (read-only import; reviewer files never modified).

Offline only. No emulator/ADB/live action. No frozen mutation.
"""

import hashlib
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.resolve().parents[1]
sys.path.insert(0, str(HERE))

import patch_maps_seed as seed

LIB_IN_APK = "lib/armeabi-v7a/libtrueaxis.so"
ORIG_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d")
SETTER = 0x107244
STUB = 0x213400
SKUS = list(json.loads(
    (HERE / "OWNERSHIP-ALLOWLIST.json").read_text(
        encoding="utf-8"))["content_skus"])


def scratch_lib(testcase):
    work = Path(tempfile.mkdtemp(prefix="seedtest-"))
    testcase.addCleanup(shutil.rmtree, work, True)
    with zipfile.ZipFile(
            ROOT / "backups/usb-20260910T005449Z/split_config.armeabi_v7a.apk"
    ) as z:
        data = z.read(LIB_IN_APK)
    assert hashlib.sha256(data).hexdigest() == ORIG_LIB_SHA256
    path = work / "libtrueaxis.so"
    path.write_bytes(data)
    return path


def decode_bw_spec(src, raw):
    """Independent T4 B.W decode straight from the ARM ARM bit layout."""
    first, second = struct.unpack("<HH", raw)
    assert (first >> 11) == 0b11110, hex(first)
    assert (second >> 14) == 0b10 and ((second >> 12) & 1) == 1, hex(second)
    s = (first >> 10) & 1
    imm10 = first & 0x3FF
    j1 = (second >> 13) & 1
    j2 = (second >> 11) & 1
    imm11 = second & 0x7FF
    i1 = 1 - (j1 ^ s)
    i2 = 1 - (j2 ^ s)
    offset = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:
        offset -= 1 << 25
    return src + 4 + offset


class CodecTests(unittest.TestCase):
    def test_branch_roundtrip_forward(self):
        raw = seed.encode_bw(0x1058a0, STUB)
        self.assertEqual(decode_bw_spec(0x1058a0, raw), STUB)
        self.assertEqual(seed.decode_bw(0x1058a0, raw), STUB)

    def test_branch_roundtrip_backward_far(self):
        raw = seed.encode_bw(0x213410, SETTER)
        self.assertEqual(decode_bw_spec(0x213410, raw), SETTER)

    def test_branch_range_refusal(self):
        # limit is on pc-relative diff (dst-(src+4)), max +2^24-2:
        # this target overshoots by 4 and must refuse ...
        with self.assertRaises(ValueError):
            seed.encode_bw(0x1000, 0x1000 + (1 << 24) + 6)
        # ... while the largest encodable forward jump passes.
        raw = seed.encode_bw(0x1000, 0x1000 + (1 << 24) + 2 - 4)
        self.assertEqual(
            decode_bw_spec(0x1000, raw), 0x1000 + (1 << 24) + 2 - 4)

    def test_ldr_t1_range(self):
        raw = seed.encode_ldr_t1(0x213410, ((0x213410 + 4) & ~3) + 100)
        self.assertEqual(raw.hex(), "1948")  # ldr r0,[pc,#100]
        with self.assertRaises(ValueError):
            seed.encode_ldr_t1(0x213410, ((0x213410 + 4) & ~3) + 1024)


class SeedPatchTests(unittest.TestCase):
    def test_check_original(self):
        path = scratch_lib(self)
        self.assertEqual(seed.state(path.read_bytes()), "original")

    def test_apply_check_revert_roundtrip(self):
        path = scratch_lib(self)
        data = path.read_bytes()
        seed.apply_patch(path, data)
        self.assertEqual(seed.state(path.read_bytes()), "seeded")
        self.assertEqual(seed.check_library(path), 0)
        seed.revert_patch(path, path.read_bytes())
        final = path.read_bytes()
        self.assertEqual(seed.state(final), "original")
        self.assertEqual(hashlib.sha256(final).hexdigest(), ORIG_LIB_SHA256)

    def test_double_apply_idempotent(self):
        path = scratch_lib(self)
        seed.apply_patch(path, path.read_bytes())
        before = path.read_bytes()
        seed.apply_patch(path, before)
        self.assertEqual(path.read_bytes(), before)

    def test_refusal_wrong_size(self):
        path = scratch_lib(self)
        with self.assertRaises(ValueError):
            seed.apply_patch(path, b"\x00" * 16)

    def test_refusal_wrong_epilogue(self):
        path = scratch_lib(self)
        data = bytearray(path.read_bytes())
        data[0x1058a0:0x1058a4] = b"\x00\x00\x00\x00"
        with self.assertRaises(ValueError):
            seed.apply_patch(path, bytes(data))

    def test_independent_stub_decode(self):
        """From-spec decode of the generated stub (not the generator).

        Only 16-bit data/memory forms plus 32-bit B/BL are allowed.
        """
        path = scratch_lib(self)
        seed.apply_patch(path, path.read_bytes())
        data = path.read_bytes()
        code, pool, strings = seed.expected_layout()
        blob = data[STUB:STUB + len(code)]
        self.assertEqual(blob[0:2].hex(), "f0bc")    # pop {r4-r7}
        self.assertEqual(blob[2:4].hex(), "0fb4")    # push {r0-r3}
        # 10x (16-bit ldr, 32-bit bl), each pair independently decoded.
        pool_base = STUB + len(code)
        for i in range(10):
            pos = 4 + 6 * i
            ldr = struct.unpack("<H", blob[pos:pos + 2])[0]
            self.assertEqual(ldr & 0xFF00, 0x4800, (i, hex(ldr)))
            pc = (pos + STUB + 4) & ~3
            lit = pc + (ldr & 0xFF) * 4
            self.assertEqual(lit, pool_base + 4 * i, i)
            target = struct.unpack("<I", data[lit:lit + 4])[0]
            text = data[target:target + 32].split(b"\x00")[0].decode()
            self.assertEqual(text, SKUS[i], i)
            bl = blob[pos + 2:pos + 6]
            self.assertEqual(decode_bw_spec(pos + STUB + 2, bl), SETTER, i)
        tail = 4 + 60
        self.assertEqual(blob[tail:tail + 2].hex(), "0fbc")  # pop {r0-r3}
        self.assertEqual(blob[tail + 2:tail + 4].hex(), "08bc")  # pop {r3}
        self.assertEqual(blob[tail + 4:tail + 6].hex(), "1847")  # bx r3
        self.assertEqual(blob[tail + 6:tail + 8].hex(), "bf00")  # nop align
        # remove_ads must NOT be among seeded literals
        self.assertNotIn(b"jcs2_remove_ads", code + pool + strings)

    def test_only_16bit_data_forms_in_stub(self):
        """Regression (live ndk_translation SIGILL class): positional walk
        proving every stub halfword is 16-bit data/memory or a 32-bit
        B/BL half — no 32-bit LDM/STM/LDR exists anywhere in the stub."""
        code, _, _ = seed.expected_layout()
        halves = struct.unpack("<%dH" % (len(code) // 2), code)
        # Layout: pop,push, 10x(ldr16, bl.w hi, bl.w lo), pop,pop,bx,nop.
        self.assertEqual([hex(h) for h in halves[0:2]], ["0xbcf0", "0xb40f"])
        idx = 2
        for _ in range(10):
            self.assertEqual(halves[idx] & 0xFF00, 0x4800, hex(halves[idx]))
            self.assertEqual(halves[idx + 1] & 0xF800, 0xF000,
                             hex(halves[idx + 1]))
            self.assertEqual(halves[idx + 2] & 0xC000, 0x8000,
                             hex(halves[idx + 2]))
            idx += 3
        rest = [hex(h) for h in halves[idx:]]
        self.assertEqual(rest, ["0xbc0f", "0xbc08", "0x4718", "0xbf"])

    def test_disjointness_anchors_intact(self):
        path = scratch_lib(self)
        seed.apply_patch(path, path.read_bytes())
        data = path.read_bytes()
        self.assertEqual(data[0x154a7c:0x154a80].hex(), "4ff7aeea")
        self.assertEqual(
            data[0x133014:0x133024].hex(), "80b56f4670f79cebbde8804093f0debd")
        for off in (0x133046, 0x13D846, 0x13D944,
                    0x14BFBC, 0x14C10A, 0x14C11C):
            self.assertEqual(data[off:off + 2].hex(), "c06b", hex(off))


def read_built_lib(variant_dir):
    lib = zipfile.ZipFile(
        Path(variant_dir) / "apks-signed/split_config.armeabi_v7a.apk"
    ).read(LIB_IN_APK)
    return lib


class VariantArtifactTests(unittest.TestCase):
    OWN = ROOT / "staging/maps-ownership-v1"
    UNLOCK = ROOT / "staging/maps-unlockall-v1"
    SENTINELS = (0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C)

    def test_normal_states(self):
        lib = read_built_lib(self.OWN)
        for off in self.SENTINELS:
            self.assertEqual(lib[off:off + 2].hex(), "c06b", hex(off))
        self.assertEqual(lib[0x133014:0x133024].hex(),
                         "80b56f46012080bdbf00bf00bf00bf00")
        self.assertEqual(lib[0x154a7c:0x154a80].hex(), "00bf00bf")
        self.assertEqual(lib[0x11e318:0x11e31e].hex(), "00bf00bf00bf")
        self.assertEqual(lib[0x154a1c:0x154a20].hex(), "704700bf")
        self.assertNotEqual(lib[0x1058a0:0x1058a4], bytes.fromhex("f0bd00bf"))
        self.assertEqual(
            decode_bw_spec(0x1058a0, lib[0x1058a0:0x1058a4]), STUB)

    def test_unlock_all_states(self):
        lib = read_built_lib(self.UNLOCK)
        for off in self.SENTINELS:
            self.assertEqual(lib[off:off + 2].hex(), "0020", hex(off))
            self.assertEqual(lib[off + 2:off + 4].hex(), "0130", hex(off))
        self.assertEqual(lib[0x133014:0x133024].hex(),
                         "80b56f46012080bdbf00bf00bf00bf00")
        self.assertNotEqual(lib[0x1058a0:0x1058a4], bytes.fromhex("f0bd00bf"))

    def test_unlock_diff_is_sentinels_only(self):
        normal = read_built_lib(self.OWN)
        unlock = read_built_lib(self.UNLOCK)
        diff = [i for i, (a, b) in enumerate(zip(normal, unlock)) if a != b]
        # Each sentinel patch rewrites BOTH bytes (c06b -> 0020).
        self.assertEqual(
            set(diff),
            {o + k for o in self.SENTINELS for k in (0, 1)},
            [hex(d) for d in diff])

    def test_sentinel_revert_restores_gating(self):
        """Revert contract: restoring c06b re-gates (medal truth untouched)."""
        lib = bytearray(read_built_lib(self.UNLOCK))
        for off in self.SENTINELS:
            self.assertEqual(lib[off + 2:off + 4].hex(), "0130", hex(off))
            lib[off:off + 2] = bytes.fromhex("c06b")
        normal = read_built_lib(self.OWN)
        self.assertEqual(bytes(lib), normal)


class DescriptorContractTests(unittest.TestCase):
    def test_reviewer_accepts_both_descriptors(self):
        sys.path.insert(0, str(ROOT / "linux-launcher"))
        import progression_choice as pc
        for role, variant_dir in (
                ("normal", ROOT / "staging/maps-ownership-v1"),
                ("unlock-all", ROOT / "staging/maps-unlockall-v1")):
            raw = json.loads(
                (variant_dir / "variant-descriptor.json").read_text(
                    encoding="utf-8"))
            desc = pc.VariantDescriptor(
                name=raw["name"], apks_dir=raw["apks_dir"],
                hashes=raw["hashes"],
                cert_fingerprint=raw["cert_fingerprint"],
                version_code=raw["version_code"])
            evidence = pc.verify_variant(desc)
            self.assertEqual(
                evidence["cert_fingerprint"], pc.EXPECTED_CERT_FINGERPRINT)
            self.assertEqual(raw["cert_der_hex"], evidence["cert_der_hex"])
            self.assertEqual(raw["version_code"], 29)


if __name__ == "__main__":
    unittest.main()
