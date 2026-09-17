#!/usr/bin/env python3
"""Assemble the editor-save+reopen candidate (offline, editor-owned).

Variant = pristine originals + sound v3 (14B, 3 sites) + maps 12B +
six unlock sentinels (c06b->0020) + controls button-gate NOP (4B, 1 site)
+ editor save+reopen NOPs (4B, 2 sites). Total 45 changed bytes at 13
sites, all verified disjoint before apply.

Editor gates (evidenced in analysis/release-workers/editor-save-gate.md,
llvm `--arch-name=thumb --mcpu=cortex-a9`, not a custom decoder):
- save:    file 0x159a9a, `cbz r0,0x159af0` (48b3) -> NOP (00bf). Save flow
  (TextInput_Create -> InGameLevelEditor::Save -> userLevels/%s.bin)
  always runs; warning popup retired.
- reopen:  file 0x15a9a8, `cbz r0,0x15aa1a` (b8b3) -> NOP (00bf). Open/play
  flow (UserLevelExists -> SelectLevel -> FadeOut) always runs; Yes/No
  store prompt retired.
Neighbour BLX calls (4cf706ef / 4bf77eef) intact -- no Thumb-BL-to-ARM-PLT
introduced. Predicate, shop, progression, ads, Create/main notes untouched.
True Thumb NOP = halfword 0xBF00 = file bytes 00 BF (never BF 00).

Reuses the maps composer's faithful repack + one-key signing helpers by
IMPORT ONLY (no maps/controls-owned file is edited) and the audio v3 spec
by the same load_spec() path the controls composer uses. Refuses existing
stage paths. Never touches originals, guests, saves, installer state, or
personal data. Status of the produced candidate is UNVERIFIED until the
save->list->reopen/edit-or-play->restart->reopen acceptance passes in the
root-assigned isolated slot (see run_editor_acceptance.sh; default dry
run only).

Usage:
  python3 compose_editor_candidate.py --stage <new-empty-dir>
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "controls"))
sys.path.insert(0, str(HERE.parent / "maps"))
sys.path.insert(0, str(HERE.parent / "audio"))
from patch_audio import (  # noqa: E402
    apply_patch, lib_state, load_spec, PatchError,
)
import compose_maps_candidate as maps  # noqa: E402 (import only, no edits)
import compose_controls_candidate as controls_mod  # noqa: E402 (import only)

ORIG_LIB_SHA256 = "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
EXPECTED_FINAL_SHA256 = (
    "cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1"
)

EDITOR_SITES = [
    {"offset": 0x159A9A, "orig": bytes.fromhex("48b3"),
     "patched": bytes.fromhex("00bf"), "label": "save cbz->nop"},
    {"offset": 0x15A9A8, "orig": bytes.fromhex("b8b3"),
     "patched": bytes.fromhex("00bf"), "label": "reopen cbz->nop"},
]
EDITOR_BLX = [
    (0x159A96, bytes.fromhex("4cf706ef")),
    (0x15A9A4, bytes.fromhex("4bf77eef")),
]


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

    # 2. Disjointness: maps + sentinels + controls + editor sites untouched.
    if v3lib[maps.MAPS_SITE:maps.MAPS_SITE + 16] != maps.MAPS_ORIG:
        raise PatchError("maps site disturbed in sound input; refusing")
    for off in maps.SENTINEL_SITES:
        if v3lib[off:off + 2] != controls_mod.SENTINEL_ORIG:
            raise PatchError(f"sentinel {off:#x} disturbed; refusing")
        if v3lib[off + 2:off + 4] != controls_mod.SENTINEL_CONTEXT:
            raise PatchError(f"sentinel context {off:#x} drifted; refusing")
    if v3lib[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] != \
            controls_mod.CTRL_ORIG:
        raise PatchError("controls site disturbed; refusing")
    for s in EDITOR_SITES:
        o = s["offset"]
        if v3lib[o:o + 2] != s["orig"]:
            raise PatchError(f"editor site {o:#x} disturbed "
                             f"({v3lib[o:o+2].hex()} != {s['orig'].hex()}); "
                             "refusing")

    # 3. Apply maps 12B + unlock sentinels + controls NOP + editor NOPs.
    final = bytearray(v3lib)
    final[maps.MAPS_SITE:maps.MAPS_SITE + 16] = maps.MAPS_PATCHED
    for off in maps.SENTINEL_SITES:
        final[off:off + 2] = controls_mod.SENTINEL_UNLOCK
    final[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] = \
        controls_mod.CTRL_PATCHED
    for s in EDITOR_SITES:
        o = s["offset"]
        final[o:o + 2] = s["patched"]
    final = bytes(final)
    final_digest = hashlib.sha256(final).hexdigest()

    # 4. Exact 45-byte footprint proof.
    diff = [i for i, (a, b) in enumerate(zip(original, final)) if a != b]
    expect = set()
    for s in sites:
        expect |= set(range(s["offset"], s["offset"] + len(s["patched"])))
    expect |= set(range(maps.MAPS_SITE + 4, maps.MAPS_SITE + 16))
    for off in maps.SENTINEL_SITES:
        expect |= {off, off + 1}
    expect |= {controls_mod.CTRL_SITE + k for k in range(4)
               if controls_mod.CTRL_ORIG[k] != controls_mod.CTRL_PATCHED[k]}
    for s in EDITOR_SITES:
        expect |= {s["offset"], s["offset"] + 1}
    if set(diff) != expect:
        raise PatchError(
            f"footprint drift: got {len(diff)} bytes, want {len(expect)}; "
            f"extra={sorted(set(diff) - expect)[:8]} "
            f"missing={sorted(expect - set(diff))[:8]}")
    if len(diff) != 45:
        raise PatchError(f"footprint {len(diff)} != 45; refusing")

    # Per-site state re-verification on the final bytes.
    if lib_state(final, sites) != "patched":
        raise PatchError("sound v3 not intact in final; refusing")
    if final[maps.MAPS_SITE:maps.MAPS_SITE + 16] != maps.MAPS_PATCHED:
        raise PatchError("maps 12B not intact in final; refusing")
    for off in maps.SENTINEL_SITES:
        if final[off:off + 2] != controls_mod.SENTINEL_UNLOCK:
            raise PatchError(f"sentinel {off:#x} not unlock; refusing")
    if final[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] != \
            controls_mod.CTRL_PATCHED:
        raise PatchError("controls NOP not intact in final; refusing")
    for off, exp in EDITOR_BLX:
        if final[off:off + 4] != exp:
            raise PatchError(f"editor BLX @{off:#x} disturbed; refusing")
    for s in EDITOR_SITES:
        o = s["offset"]
        if final[o:o + 2] != s["patched"]:
            raise PatchError(f"editor site {o:#x} not NOP; refusing")
    if final_digest != EXPECTED_FINAL_SHA256:
        raise PatchError(f"final lib {final_digest} != expected "
                         f"{EXPECTED_FINAL_SHA256}; refusing")
    print(f"final lib sha256 {final_digest} "
          f"({len(diff)} bytes at 13 sites, verified)")

    signed_dir, deriv_hashes, alignment = maps._finish_signed_set(
        args.stage, apk_dir, final, "unlock + sound-v3 + controls + editor lib")

    # Independent guards on the SIGNED APK library (not staged bytes):
    # lib hash + true-NOP disassembly at all three NOP sites.
    import subprocess as _sp
    import zipfile as _zf
    with _zf.ZipFile(signed_dir / "split_config.armeabi_v7a.apk") as _z:
        _signed_lib = _z.read("lib/armeabi-v7a/libtrueaxis.so")
    if hashlib.sha256(_signed_lib).hexdigest() != final_digest:
        raise PatchError("signed lib != assembled lib; refusing")
    _lib_path = args.stage / "libtrueaxis-signed-check.so"
    _lib_path.write_bytes(_signed_lib)
    for _addr, _want_nops, _label in [
            (controls_mod.CTRL_SITE, 2, "controls"),
            (0x159A9A, 1, "editor-save"),
            (0x15A9A8, 1, "editor-reopen")]:
        _dis = _sp.run(
            ["llvm-objdump", "--arch-name=thumb", "--mcpu=cortex-a9", "-d",
             f"--start-address={_addr:#x}",
             f"--stop-address={_addr + 8:#x}", str(_lib_path)],
            capture_output=True, text=True, timeout=120)
        if _dis.returncode != 0:
            raise PatchError(f"llvm-objdump failed: {_dis.stderr[:300]}")
        _nops = [l for l in _dis.stdout.splitlines()
                 if "nop" in l.lower() and "bf00" in l.lower()]
        if len(_nops) != _want_nops:
            raise PatchError(
                f"signed-lib disassembly is not {_want_nops}x NOP "
                f"({_label}) at {_addr:#x}:\n" + _dis.stdout[:1200])
    print("signed-lib llvm guards: controls 2x NOP + save NOP + reopen NOP")

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
            "offset": hex(controls_mod.CTRL_SITE),
            "orig": controls_mod.CTRL_ORIG.hex(),
            "patched": controls_mod.CTRL_PATCHED.hex(),
        },
        "editor_save_reopen": [
            {"offset": hex(s["offset"]), "orig": s["orig"].hex(),
             "patched": s["patched"].hex(), "label": s["label"]}
            for s in EDITOR_SITES],
        "variant_lib_sha256": final_digest,
        "variant_lib_changed_bytes": len(diff),
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": maps.DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); same-key reinstall "
                      "preserves saves"),
        "variant": ("unlock-all (Levels 12B + six 0020 sentinels) + "
                    "sound v3 force-LOW + controls button-gate NOP + "
                    "editor save+reopen NOPs; seed OMITTED-no-seed"),
        "ownership_allowlist": skus,
        "verification_status": ("UNVERIFIED -- built offline; save -> list "
                                "-> reopen/edit-or-play -> restart -> reopen "
                                "acceptance pending in the root-assigned "
                                "isolated slot (run_editor_acceptance.sh)"),
    }
    (args.stage / "editor-save-provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8")
    print(f"DONE: editor-save candidate built (lib {final_digest[:16]}...) "
          "-- UNVERIFIED until acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
