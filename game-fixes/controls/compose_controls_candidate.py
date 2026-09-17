#!/usr/bin/env python3
"""Assemble the controls-fix five-APK candidate (offline, controls-owned).

NEW variant = pristine originals + sound v3 (14B, 3 sites) + maps 12B +
six unlock sentinels (c06b->0020) + controls button-gate NOP (4B, 1 site).
Total 42 changed bytes at 11 sites, all verified disjoint before apply.

Controls fix (evidenced, not blind):
- Dynamic probe (isolated guest, game pid 3882, canonical unlock-all):
  JOYPAD ALLOWED NO->YES->NO toggles traced via memdiff-probe on the
  writable libtrueaxis segment. Segment offset 0x210 flips 0/1/0 exactly
  with the setting (reversible); bytes 0x228/0x230 drift WITHOUT any
  toggle (transient). File offset 0x21f210 = VA 0x220210.
- Static: R_ARM_GLOB_DAT names VA 0x220210 `m_bAllowJoysticks`; the
  arm32 KEY-event button path (android_main+0x1970 region) has THREE
  checks before the 12-entry NvButtonMapping table walk:
  chk1 m_ControllerType (int, keep), chk2 m_bAllowJoysticks (byte, NOP),
  chk3 g_bJoypadSupportExists (byte, keep). GOT-slot arithmetic proves
  chk2 (ldr@0x10bc58 pool@0x10bfb8 val+GOT=0x21ab30) is the setting check.
- Patch: file offset 0x10bc62, `beq.w 0x10be7c` (F000 810B) -> NOP NOP
  (BF00 BF00). Buttons flow when JOYPAD ALLOWED=NO; stick gating,
  capability and controller-type checks untouched.

Reuses the maps composer's faithful repack + one-key signing helpers by
IMPORT ONLY (no maps-owned file is edited). Refuses existing stage paths.
Never touches originals, guests, saves, installer state, or personal data.

Usage:
  python3 compose_controls_candidate.py --stage <new-empty-dir>
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maps"))
sys.path.insert(0, str(HERE.parent / "audio"))
from patch_audio import (  # noqa: E402
    apply_patch, lib_state, load_spec, PatchError,
)
import compose_maps_candidate as maps  # noqa: E402 (import only, no edits)

ORIG_LIB_SHA256 = "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
CTRL_SITE = 0x10BC62
CTRL_ORIG = bytes.fromhex("00f00b81")  # beq.w skip (F000 810B, LE)
# True Thumb-2 NOP = 0xBF00 -> LE bytes 00 BF. Two NOPs = 00 BF 00 BF.
# (bf00bf00 would be two 00BF = LSLS r7,r7,#2 — REJECTED wrong-endian.)
CTRL_PATCHED = bytes.fromhex("00bf00bf")  # NOP NOP
SENTINEL_ORIG = bytes.fromhex("c06b")
SENTINEL_UNLOCK = bytes.fromhex("0020")
SENTINEL_CONTEXT = bytes.fromhex("0130")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.stage.exists():
        raise PatchError(f"refusing existing stage path {args.stage}")
    for path in (maps.JDK / "bin/jarsigner", maps.KEYSTORE,
                 maps.STOREPASS_FILE):
        if not path.exists():
            raise PatchError(f"signing input missing: {path}")

    target, sites = load_spec()
    apk_dir, staged, skus = maps.stage_originals(args.stage)
    lib_path, lib_digest = maps.extract_lib(apk_dir, args.stage)
    if lib_digest != ORIG_LIB_SHA256:
        raise PatchError(f"staged lib {lib_digest} != pristine; refusing")
    if lib_digest != target["lib_sha256"]:
        raise PatchError("staged lib != v3 spec target; refusing")
    original = lib_path.read_bytes()

    # 1. Sound v3 on the staged copy.
    if lib_state(original, sites) != "original":
        raise PatchError("staged lib not in original state; refusing")
    v3lib = apply_patch(original, expected_lib_sha256=target["lib_sha256"],
                        sites=sites)
    if lib_state(v3lib, sites) != "patched":
        raise PatchError("v3 lib not in patched state; refusing")

    # 2. Disjointness: maps site + sentinels + controls site untouched.
    if v3lib[maps.MAPS_SITE:maps.MAPS_SITE + 16] != maps.MAPS_ORIG:
        raise PatchError("maps site disturbed in sound input; refusing")
    for off in maps.SENTINEL_SITES:
        if v3lib[off:off + 2] != SENTINEL_ORIG:
            raise PatchError(f"sentinel {off:#x} disturbed; refusing")
        if v3lib[off + 2:off + 4] != SENTINEL_CONTEXT:
            raise PatchError(f"sentinel context {off:#x} drifted; refusing")
    if v3lib[CTRL_SITE:CTRL_SITE + 4] != CTRL_ORIG:
        raise PatchError("controls site disturbed; refusing")

    # 3. Apply maps 12B + unlock sentinels + controls NOP.
    final = bytearray(v3lib)
    final[maps.MAPS_SITE:maps.MAPS_SITE + 16] = maps.MAPS_PATCHED
    for off in maps.SENTINEL_SITES:
        final[off:off + 2] = SENTINEL_UNLOCK
    final[CTRL_SITE:CTRL_SITE + 4] = CTRL_PATCHED
    final = bytes(final)
    final_digest = hashlib.sha256(final).hexdigest()

    # 4. Exact 42-byte footprint proof.
    diff = [i for i, (a, b) in enumerate(zip(original, final)) if a != b]
    expect = set()
    for s in sites:
        expect |= set(range(s["offset"], s["offset"] + len(s["patched"])))
    expect |= set(range(maps.MAPS_SITE + 4, maps.MAPS_SITE + 16))
    for off in maps.SENTINEL_SITES:
        expect |= {off, off + 1}
    expect |= {CTRL_SITE + k for k in range(4)
               if CTRL_ORIG[k] != CTRL_PATCHED[k]}
    if set(diff) != expect:
        raise PatchError(
            f"footprint drift: got {len(diff)} bytes, want {len(expect)}; "
            f"extra={sorted(set(diff) - expect)[:8]} "
            f"missing={sorted(expect - set(diff))[:8]}")
    # Per-site state re-verification on the final bytes.
    if lib_state(final, sites) != "patched":
        raise PatchError("sound v3 not intact in final; refusing")
    if final[maps.MAPS_SITE:maps.MAPS_SITE + 16] != maps.MAPS_PATCHED:
        raise PatchError("maps 12B not intact in final; refusing")
    for off in maps.SENTINEL_SITES:
        if final[off:off + 2] != SENTINEL_UNLOCK:
            raise PatchError(f"sentinel {off:#x} not unlock; refusing")
    if final[CTRL_SITE:CTRL_SITE + 4] != CTRL_PATCHED:
        raise PatchError("controls NOP not intact in final; refusing")
    print(f"final lib sha256 {final_digest} "
          f"({len(diff)} bytes at 11 sites, verified)")

    signed_dir, deriv_hashes, alignment = maps._finish_signed_set(
        args.stage, apk_dir, final, "unlock + sound-v3 + controls lib")

    # Independent guard vs the historical endianness trap: disassemble the
    # SIGNED APK library (not the staged bytes) and require true NOPs.
    import subprocess as _sp
    import zipfile as _zf
    with _zf.ZipFile(signed_dir / "split_config.armeabi_v7a.apk") as _z:
        _signed_lib = _z.read("lib/armeabi-v7a/libtrueaxis.so")
    if hashlib.sha256(_signed_lib).hexdigest() != final_digest:
        raise PatchError("signed lib != assembled lib; refusing")
    _lib_path = args.stage / "libtrueaxis-signed-check.so"
    _lib_path.write_bytes(_signed_lib)
    _dis = _sp.run(
        ["llvm-objdump", "-d", "--triple=thumbv7-linux-androideabi",
         f"--start-address={CTRL_SITE:#x}",
         f"--stop-address={CTRL_SITE + 8:#x}", str(_lib_path)],
        capture_output=True, text=True, timeout=120)
    if _dis.returncode != 0:
        raise PatchError(f"llvm-objdump failed: {_dis.stderr[:300]}")
    _nops = [l for l in _dis.stdout.splitlines() if "nop" in l.lower()]
    if len(_nops) != 2 or not all("bf00" in l.lower() for l in _nops):
        raise PatchError(
            f"signed-lib disassembly is not 2x NOP at {CTRL_SITE:#x}:\n"
            + _dis.stdout[:1200])

    provenance = {
        "package": maps.PACKAGE,
        "version": maps.VERSION,
        "source_dir": str(maps.SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": maps.ORIGINAL_CERT_SHA256_FPR,
        "v3_audio_sites": [
            {"offset": hex(s["offset"]), "orig": s["orig"].hex(),
             "patched": s["patched"].hex()} for s in sites],
        "ownership_12b": {"offset": hex(maps.MAPS_SITE), "changed_bytes": 12},
        "sentinels": [{"offset": hex(o), "value": "0020"}
                      for o in maps.SENTINEL_SITES],
        "controls_fix": {
            "offset": hex(CTRL_SITE),
            "orig": CTRL_ORIG.hex(),
            "patched": CTRL_PATCHED.hex(),
            "meaning": ("arm32 KEY-path chk2 beq.w skip -> NOP NOP; "
                        "m_bAllowJoysticks no longer gates buttons"),
            "evidence": {
                "dynamic": ("isolated guest pid 3882, JOYPAD ALLOWED "
                            "NO/YES/NO2 dumps: file 0x21f210 flips 0/1/0; "
                            "0x228/0x230 drift without toggle (transient)"),
                "static": ("R_ARM_GLOB_DAT 0x220210=m_bAllowJoysticks; "
                           "chk2 GOT slot 0x21ab30; chk1=m_ControllerType "
                           "(kept); chk3=g_bJoypadSupportExists (kept)"),
            },
        },
        "variant_lib_sha256": final_digest,
        "variant_lib_changed_bytes": len(diff),
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": maps.DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); same-key reinstall "
                      "preserves saves"),
        "variant": ("unlock-all (Levels 12B + six 0020 sentinels) + "
                    "sound v3 force-LOW + controls button-gate NOP; "
                    "seed OMITTED-no-seed (store BuyNow gap unchanged)"),
        "ownership_allowlist": skus,
    }
    (args.stage / "controls-fix-provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8")
    print(f"DONE: controls-fix candidate built (lib {final_digest[:16]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
