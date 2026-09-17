#!/usr/bin/env python3
"""Offline tests for the guarded-cycling patch v2 (no device, read-only).

Meaningful core (per independent review): actual ELF load mapping via
struct parse + llvm-readelf cross-check, nonzero ASLR-bias simulation of
every shim address computation, ARM/Thumb interworking proof (PLT decoded
as ARM, shim calls proven BLX-form to PLT allowlist, hook proven B.W-form
to the Thumb shim), and original-bytes-anchored decoder validation.
Oracle goldens are secondary (exact-byte anchors, not proof).

Lane: game-fixes/audio only.
"""
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maps"))
from patch_audio_cycling import (  # noqa: E402
    build_shim, build_all, lib_state, spec_data, apply_patch, PatchError,
    SHIM_ADDR, SHIM_ZONE_END, PLT_ALLOW, PLT_STOP, PLT_START,
    HOOK_SITE, LOADOPT_SITE, LOADOPT_BL_OFF, ENTRY_OFF,
    SEND_SITE_PC, SEND_POOL_OFF, CB1_SITE_PC, CB1_POOL_OFF,
    replication_targets, decode_blx_imm, sha256,
)
from patch_maps_seed import encode_bw, decode_bw, encode_blx_imm  # noqa: E402

TARGET, SITES = spec_data()


def repo_root() -> Path:
    for cand in (HERE, *HERE.parents):
        if (cand / "backups").is_dir():
            return cand
    raise AssertionError("repo root with backups/ not found")


REAL_APK = (repo_root() / "backups" / "usb-20260910T005449Z"
            / "split_config.armeabi_v7a.apk")
MAPS_SITES = [0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C]
BIASES = [0x0, 0x1000, 0x100000, 0x20000000]


def lib_tmp(data: bytes) -> str:
    """Write lib bytes to a temp file for real ELF tools. Caller cleans up."""
    tmp = tempfile.NamedTemporaryFile(suffix=".so", delete=False)
    tmp.write(data)
    tmp.flush()
    tmp.close()
    return tmp.name

# Oracle goldens: llvm-mc assembler + objdump roundtrips, game precedents.
GOLDEN = {
    "push": "f0b5", "pop": "f0bd",
    "sub_sp_10": "84b0",        # game precedent (callback prologue)
    "sub_sp_18": "86b1",        # oracle sub sp,#24 (==#0x18)
    "add_sp_18": "06b0",        # mc-display + pattern
    "mov_r7_r8": "4746", "mov_r8_r7": "b846", "mov_r3_r0": "0346",
    "mov_r0_r4": "2046",
    "ldr_r2_pc": "0b4a",        # oracle ldr r2,[pc,#44] low byte pattern
    "ldr_r2_r2": "1268", "ldr_r2_r2_8": "9268",
    "strb_r1_r5": "2970",       # oracle (0x5029 was STR-reg bug)
    "movs_r1_1": "0121", "movs_r1_0": "0021",
    "cmp_r3_0": "002b",  # oracle (0x2B03 was cmp#3 bug)
    "adds_r2_6": "0632", "subs_r2_2": "023a",
    "add_r0_sp": "00a8", "add_r1_sp": "00a9", "add_r2_pc": "7a44",
    "cbz_game": "7ab1",         # game byte: cbz r2,+30
    "cbz_obj": "5ab1",          # obj roundtrip: cbz r2,+22
    "bne_obj": "02d1",          # obj roundtrip: bne +4
}


def hword(code: bytes, off: int) -> int:
    return struct.unpack_from("<H", code, off)[0]


def read_cell(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def read_orig() -> bytes:
    with zipfile.ZipFile(REAL_APK) as source:
        return source.read(TARGET["member"])


def phdrs(data: bytes):
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phnum = struct.unpack_from("<H", data, 44)[0]
    out = []
    for i in range(e_phnum):
        o = e_phoff + 32 * i
        out.append(dict(
            type=struct.unpack_from("<I", data, o)[0],
            off=struct.unpack_from("<I", data, o + 4)[0],
            va=struct.unpack_from("<I", data, o + 8)[0],
            filesz=struct.unpack_from("<I", data, o + 16)[0],
            memsz=struct.unpack_from("<I", data, o + 20)[0],
            flags=struct.unpack_from("<I", data, o + 24)[0],
            align=struct.unpack_from("<I", data, o + 28)[0]))
    return out


class GoldenTests(unittest.TestCase):
    def test_oracle_goldens_present(self):
        data = read_orig()
        code, _, _, _, _, _ = build_all(data)
        for name in ("push", "pop", "mov_r7_r8", "mov_r8_r7",
                     "ldr_r2_r2", "strb_r1_r5", "movs_r1_0",
                     "cmp_r3_0", "add_r0_sp",
                     "add_r1_sp", "add_r2_pc", "mov_r0_r4"):
            self.assertIn(bytes.fromhex(GOLDEN[name]), code, name)

    def test_fallback_polarity_low_is_zero(self):
        # §9 polarity (3x evidenced: v2 HIGH screenshot, boot-LOW default,
        # m_bDoubleBufferSound=double-buffer-ON=HIGH): 0=LOW, 1=HIGH.
        # FAIL fallback must store 0; a stored 1 would be the HIGH lie.
        data = read_orig()
        code, _, _, _, _, _ = build_all(data)
        self.assertIn(bytes.fromhex(GOLDEN["movs_r1_0"]), code)
        self.assertNotIn(bytes.fromhex(GOLDEN["movs_r1_1"]), code)

    def test_game_cbz_vector(self):
        data = read_orig()
        self.assertEqual(data[0x154A3A:0x154A3A + 2],
                         bytes.fromhex(GOLDEN["cbz_game"]))

    def test_original_blx_decoder_grounding(self):
        # The decoder is validated against silicon-executed original bytes,
        # not against itself: each (site, bytes) must resolve to its PLT.
        data = read_orig()
        for off, hexb, plt in (
                (0x154A7C, "4ff7aeea", 0xA3FDC),
                (0x103F5C, None, PLT_STOP),
                (0x10B662, None, PLT_START)):
            raw = bytes.fromhex(hexb) if hexb else data[off:off + 4]
            self.assertEqual(decode_blx_imm(off, raw), plt, hex(off))

    def test_hook_encodings(self):
        hook = [e for e in SITES if e["offset"] == "0x154a7c"][0]
        raw = bytes.fromhex(hook["patched"])
        self.assertEqual(decode_bw(HOOK_SITE, raw), SHIM_ADDR)


class InterworkingTests(unittest.TestCase):
    def test_plt_is_arm(self):
        # Real tool: PLT stubs decode as ARM (B1 ground truth).
        path = lib_tmp(read_orig())
        try:
            out = subprocess.run(
                ["llvm-objdump", "--triple=armv7-linux-androideabi",
                 "--mattr=-thumb-mode", "-d", path,
                 f"--start-address={hex(PLT_STOP - 8)}",
                 f"--stop-address={hex(PLT_START + 8)}"],
                capture_output=True, text=True, timeout=120)
        finally:
            Path(path).unlink()
        self.assertEqual(out.returncode, 0)
        self.assertIn("e28fc601", out.stdout)

    def test_shim_calls_are_blx_to_plt(self):
        data = read_orig()
        code, _, blx, _, _, _ = build_all(data)
        self.assertTrue(blx)
        for off, plt in blx.items():
            first = hword(code, off)
            second = hword(code, off + 2)
            self.assertTrue(0xF000 <= first <= 0xF7FF, hex(off))
            # True BLX.W-imm: second-half top nibble 0xE (silicon-executed
            # originals f79eef20/f797eba4/4ff7aeea all show it; 0xF... in
            # this position is BL.W: no ARM switch -> SIGILL class).
            self.assertEqual(second >> 12, 0xE, hex(second))
            self.assertEqual(decode_blx_imm(SHIM_ADDR + off,
                                            code[off:off + 4]), plt)
            self.assertIn(plt, PLT_ALLOW)

    def test_encoder_reproduces_original_blx(self):
        # The fixed encoder must reproduce silicon-executed original bytes.
        from patch_audio_cycling import encode_blx_imm
        data = read_orig()
        for src, plt in ((0x103F5C, PLT_STOP), (0x10B662, PLT_START),
                         (0x154A7C, 0xA3FDC)):
            self.assertEqual(encode_blx_imm(src, plt),
                             data[src:src + 4], hex(src))

    def test_hook_is_thumb_bw(self):
        hook = [e for e in SITES if e["offset"] == "0x154a7c"][0]
        raw = bytes.fromhex(hook["patched"])
        second = struct.unpack("<H", raw[2:4])[0]
        self.assertTrue(0x8000 <= second <= 0xBFFF, hex(second))


class MappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = read_orig()
        code, pools, _, _, _, _ = build_all(data)
        cls.total = code + pools
        cls.ph = phdrs(data)

    def test_load0_mapping_readelf(self):
        # Real tool + struct parse must agree: pristine LOAD0 FileSiz
        # 0x2133fc (the review's gap), R+E; extend target clear of LOAD1.
        data = read_orig()
        path = lib_tmp(data)
        try:
            out = subprocess.run(["llvm-readelf", "-l", path],
                                 capture_output=True, text=True, timeout=120)
        finally:
            Path(path).unlink()
        self.assertEqual(out.returncode, 0)
        self.assertIn("2133fc", out.stdout)
        self.assertIn("R E", out.stdout)
        load0 = [p for p in self.ph if p["type"] == 1][0]
        load1 = [p for p in self.ph if p["type"] == 1][1]
        self.assertEqual(load0["filesz"], 0x2133FC)
        self.assertEqual(load0["memsz"], 0x2133FC)
        self.assertEqual(load1["off"], 0x213BA0)
        # Extend target: covers shim, avoids LOAD1, VA-disjoint.
        self.assertLess(SHIM_ADDR + len(self.total), 0x213700)
        self.assertLess(0x213700, load1["off"])
        self.assertLessEqual(0x213700, load1["va"])
        self.assertEqual(load0["off"] % load0["align"],
                         load0["va"] % load0["align"])

    def test_phdr_site_bytes(self):
        entry = [e for e in SITES if e["offset"] == "0x64"][0]
        self.assertEqual(entry["orig"], "fc332100fc332100")
        self.assertEqual(entry["patched"], "0037210000372100")


class AslrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig = read_orig()
        cls.code, cls.pools, cls.blx, _, cls.uses, _ = build_all(cls.orig)
        cls.tgts = replication_targets(cls.orig)
        cls.pool_words = struct.unpack(f"<{len(cls.pools) // 4}I",
                                       cls.pools)

    def test_no_absolute_mapped_va_in_shim(self):
        # Every pool word must be a link-time DELTA resolvable against a
        # runtime pc, never an absolute mapped VA. Proved per-use below;
        # here assert none equals a known absolute anchor.
        anchors = {0x31E6E2, 0x31EFB4, 0x2200EE, 0x2200EC, 0x1EF749,
                   0x21A9A8}
        for w in self.pool_words:
            self.assertNotIn(w, anchors, hex(w))

    def test_bias_simulation(self):
        # Model: at load bias B, link VA V runs at V+B. Each shim ldr use
        # computes (ldr_pc_link + B) + pool_delta; assert it lands on
        # (target_link + B), with targets + deltas recomputed from ORIGINAL
        # file bytes (not from the builder's word).
        blx_offs = set(self.blx)
        found = []
        i = 0
        while i < len(self.code):
            if i in blx_offs:
                i += 4
                continue
            if 0x4800 <= hword(self.code, i) <= 0x4FFF:
                found.append(i)
            i += 2
        self.assertEqual(len(found), len(self.uses))
        names = {"player_loc", "play_loc", "q_loc", "lowstr"}
        for pi, ((off, kind), ldr_pos) in enumerate(zip(self.uses, found)):
            self.assertEqual(ldr_pos, off)
            self.assertIn(kind, names)
            delta = self.pool_words[pi]
            # add-pc (at ldr_pos+2) uses addr+4 UNALIGNED (display-proven);
            # ldr positions are aligned %4==0 (builder-enforced), so the
            # ldr's own pc is rule-independent.
            self.assertEqual((SHIM_ADDR + ldr_pos) % 4, 0)
            pc_add = SHIM_ADDR + ldr_pos + 2 + 4
            want_delta = (self.tgts[kind] - pc_add) & 0xFFFFFFFF
            self.assertEqual(delta, want_delta, (hex(ldr_pos), kind))
            signed = delta if delta < 0x80000000 else delta - 0x100000000
            for bias in BIASES:
                actual = ((pc_add + bias) + signed) & 0xFFFFFFFF
                expect = (self.tgts[kind] + bias) & 0xFFFFFFFF
                self.assertEqual(actual, expect,
                                 (hex(ldr_pos), kind, hex(bias)))
            # ldr reachability under either pc rule (aligned positions).
            lpc = SHIM_ADDR + ldr_pos + 4
            pool_base = SHIM_ADDR + ((len(self.code) + 3) & ~3)
            self.assertLessEqual((pool_base + 4 * pi) - lpc, 1020)

    def test_flag_got_anchor(self):
        # The live r5 anchor: GOT slot 0x21a9a8 resolves m_bDoubleBufferSound
        # (R_ARM_GLOB_DAT). Parsed with the real tool from the real lib.
        path = lib_tmp(self.orig)
        try:
            text = subprocess.run(
                ["llvm-readelf", "--relocs", path],
                capture_output=True, text=True,
                timeout=120).stdout
        finally:
            Path(path).unlink()
        self.assertIn("21a9a8", text)
        self.assertIn("m_bDoubleBufferSound", text)


def synthetic_lib():
    size = 0x213800
    data = bytearray(size)
    for entry in SITES:
        off = int(entry["offset"], 16)
        kind = entry.get("patched")
        if kind == "shim":
            continue
        orig = bytes.fromhex(entry["orig"])
        pre = bytes.fromhex(entry["context_before"])
        post = bytes.fromhex(entry["context_after"])
        data[off - len(pre):off] = pre
        data[off:off + len(orig)] = orig
        data[off + len(orig):off + len(orig) + len(post)] = post
    # entry ORIG (live path restored by not patching).
    data[ENTRY_OFF:ENTRY_OFF + 4] = bytes.fromhex("f0b503af")
    return bytes(data)


class ApplyTests(unittest.TestCase):
    def test_state_original_synthetic(self):
        from patch_audio_cycling import build_all as _b
        data = synthetic_lib()
        code, pools, _, _, _, _ = _b(read_orig())
        total = code + pools
        self.assertEqual(lib_state(data, TARGET, SITES, total), "original")

    def test_refuses_hash_mismatch(self):
        from patch_audio_cycling import build_all as _b
        code, pools, _, _, _, _ = _b(read_orig())
        with self.assertRaises(PatchError):
            apply_patch(synthetic_lib(), TARGET, SITES, code + pools)

    def test_shim_zone_budget(self):
        from patch_audio_cycling import build_all as _b
        code, pools, _, _, _, _ = _b(read_orig())
        self.assertLessEqual(SHIM_ADDR + len(code + pools), SHIM_ZONE_END)

    def test_branch_destinations(self):
        # Recorded branch positions (no form-guessing walk: the 0xB1xx
        # overlap makes display-walks unreliable). Each must land on a
        # build label; cbz/bne/bn counts must match the design.
        from patch_audio_cycling import build_all as _b
        code, _, _, labels, _, branches = _b(read_orig())
        addrs = {SHIM_ADDR + v for v in labels.values()}
        kinds = {}
        for off, kind, reg, label in branches:
            dst = SHIM_ADDR + labels[label]
            src = SHIM_ADDR + off
            w = hword(code, off)
            if kind == "cbz":
                word = ((w >> 9) & 1) << 5 | ((w >> 3) & 0x1F)
                self.assertEqual(src + 4 + word * 2, dst, hex(off))
            else:
                imm = w & 0xFF
                imm -= 0x100 if imm & 0x80 else 0
                self.assertEqual(src + 4 + imm * 2, dst, hex(off))
            self.assertIn(dst, addrs)
            kinds[kind] = kinds.get(kind, 0) + 1
        # 3 main + 3 V1 + 3 V2 cbz, 1 bne, 2 bn (V1-pass + TRAMP).
        self.assertEqual(kinds, {"cbz": 9, "bne": 1, "bn": 2})

    def test_no_fail_path_skips_labelfix(self):
        # N3: TAIL_B (restore+return) is reached ONLY by fallthrough from
        # LABELFIX or SUCCESS tail. No branch may target it directly, or a
        # HIGH label could survive on a dead engine.
        from patch_audio_cycling import build_all as _b
        _, _, _, labels, _, branches = _b(read_orig())
        tail_b = SHIM_ADDR + labels["TAIL_B"]
        for off, kind, reg, label in branches:
            self.assertNotEqual(SHIM_ADDR + labels[label], tail_b,
                                (hex(off), kind))

    def test_v2_verify_targets_tramp(self):
        # The last verify block's cbz's must hit TRAMP (forward), whose bn
        # bounces back to LABELFIX (no degenerate fallthrough-equivalent).
        from patch_audio_cycling import build_all as _b
        _, _, _, labels, _, branches = _b(read_orig())
        tramp = SHIM_ADDR + labels["TRAMP"]
        labelfix = SHIM_ADDR + labels["LABELFIX"]
        self.assertGreater(tramp, labelfix)
        n_tramp = sum(1 for _, _, _, lb in branches if lb == "TRAMP")
        self.assertEqual(n_tramp, 3)
        n_fix = sum(1 for _, _, _, lb in branches if lb == "LABELFIX")
        self.assertEqual(n_fix, 2)  # V1 bn + TRAMP bn

    def test_loadopt_is_v3_nop(self):
        # N1: boot stays the proven v3 NOP (byte-identical to audio-v3).
        v3 = json.loads((HERE / "audio_patch.json").read_text())
        v3lo = [e for e in v3["sites"] if e["offset"] == "0x11e318"][0]
        lo = [e for e in SITES if e["offset"] == "0x11e318"][0]
        for key in ("orig", "patched", "context_before", "context_after"):
            self.assertEqual(lo[key], v3lo[key], key)
        # ...and no BL.W to the shim from the boot site.
        self.assertNotIn("f5f071b9", lo["patched"])

    def test_cb1_cb2_share_one_queue_base(self):
        # N2: CB1_T == CB2_T under EITHER add-pc rule (site distance 0x10
        # == cell distance -0x10): one queue, two callbacks. No adjacency.
        data = read_orig()
        c1 = read_cell(data, 0x103C48)
        c2 = read_cell(data, 0x103C58)
        self.assertEqual(c1 - c2, 0x10)
        from patch_audio_cycling import replication_targets as _rt
        tgts = _rt(data)
        self.assertTrue(0x220000 <= tgts["q_loc"] < 0x220624)


class RealLibTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = read_orig()
        from patch_audio_cycling import build_all as _b
        code, pools, _, _, _, _ = _b(cls.data)
        cls.total = code + pools

    def test_real_lib_is_original_state(self):
        self.assertEqual(lib_state(self.data, TARGET, SITES, self.total),
                         "original")

    def test_disjointness(self):
        for off in MAPS_SITES:
            self.assertEqual(self.data[off:off + 2], bytes.fromhex("c06b"),
                             hex(off))
        self.assertEqual(self.data[ENTRY_OFF:ENTRY_OFF + 4],
                         bytes.fromhex("f0b503af"))
        self.assertTrue(all(b == 0 for b in self.data[0x213400:0x213600]))
        zone = self.data[SHIM_ADDR:SHIM_ADDR + len(self.total)]
        self.assertTrue(all(b == 0 for b in zone))
        # ctor inline site intact (observed safe, untouched).
        self.assertNotEqual(self.data[0x153490:0x153494], bytes(4))


class StageTests(unittest.TestCase):
    def test_stage_candidate(self):
        from patch_audio_cycling import main as patch_main
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "candidate-cycling"
            rc = patch_main(["--stage", str(stage)])
            self.assertEqual(rc, 0)
            prov = json.loads((stage / "provenance.json").read_text())
            self.assertEqual(prov["state"], "patched")
            self.assertTrue(prov["length_preserved"])
            self.assertFalse(prov["guest_modified"])
            lib = (stage / "libtrueaxis.so.patched").read_bytes()
            self.assertEqual(len(lib), TARGET["lib_size"])
            from patch_audio_cycling import build_all as _b
            code, pools, _, _, _, _ = _b(read_orig())
            self.assertEqual(lib_state(lib, TARGET, SITES, code + pools),
                             "patched")
            # PHDR extended in the staged lib (B2 file proof).
            ph = phdrs(lib)
            load0 = [p for p in ph if p["type"] == 1][0]
            self.assertEqual(load0["filesz"], 0x213700)
            self.assertEqual(load0["memsz"], 0x213700)

    def test_stage_refuses_existing(self):
        from patch_audio_cycling import main as patch_main
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "c"
            stage.mkdir()
            with self.assertRaises(PatchError):
                patch_main(["--stage", str(stage)])


if __name__ == "__main__":
    unittest.main()
