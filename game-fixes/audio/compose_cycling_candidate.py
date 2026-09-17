#!/usr/bin/env python3
"""Assemble the tap-only sound-TEST five-APK set (offline, sound-owned).

Tap-only = pristine originals + cycling-guarded patch (hook bl.w, boot v3
NOP, PHDR extend, pad shim), NO maps patch. Reuses the maps composer's
faithful repack + one-key signing helpers by IMPORT ONLY (no maps-owned
file is edited). Refuses existing stage paths. Never touches originals,
guests, saves, installer state, canonical, or frozen records.

TEST ONLY: disposable-guest characterization (review-approved scope).
Not frozen, not deployable, never the personal guest.

Usage:
  python3 compose_cycling_candidate.py --stage <new-empty-dir>
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maps"))
from patch_audio_cycling import (  # noqa: E402
    build_all, apply_patch, lib_state, spec_data, PatchError,
)
import compose_maps_candidate as maps  # noqa: E402 (import only, no edits)

TAPONLY_LIB_SHA256 = ("eceec772a800bbd283a61b48fe177abdf6df5f5e1f5e4e4f9fdf7755377e0eeb")
SENTINEL_ORIG = bytes.fromhex("c06b")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.stage.exists():
        raise PatchError(f"refusing existing stage path {args.stage}")

    target, sites = spec_data()
    apk_dir, staged, skus = maps.stage_originals(args.stage)
    lib_path, lib_digest = maps.extract_lib(apk_dir, args.stage)
    if lib_digest != target["lib_sha256"]:
        raise PatchError("staged lib != cycling spec target; refusing")

    original = lib_path.read_bytes()
    code, pools, _, _, _, _ = build_all(original)
    shim_total = code + pools
    if lib_state(original, target, sites, shim_total) != "original":
        raise PatchError("staged lib not in original state; refusing")
    if original[maps.MAPS_SITE:maps.MAPS_SITE + len(maps.MAPS_ORIG)] != maps.MAPS_ORIG:
        raise PatchError("maps site disturbed in sound input; refusing")
    for off in maps.SENTINEL_SITES:
        if original[off:off + 2] != SENTINEL_ORIG:
            raise PatchError(f"sentinel {hex(off)} disturbed; refusing")
    taplib = apply_patch(original, target, sites, shim_total)
    if hashlib.sha256(taplib).hexdigest() != TAPONLY_LIB_SHA256:
        raise PatchError("tap-only footprint drift; refusing")
    if lib_state(taplib, target, sites, shim_total) != "patched":
        raise PatchError("tap-only lib not in patched state; refusing")
    lib_path.write_bytes(taplib)
    print(f"tap-only lib sha256 {TAPONLY_LIB_SHA256} (verified)")

    signed_dir, deriv_hashes, alignment = maps._finish_signed_set(
        args.stage, apk_dir, taplib, "sound-taponly lib")

    spec = json.loads((HERE / "audio_cycling_patch.json").read_text(
        encoding="utf-8"))
    provenance = {
        "package": maps.PACKAGE,
        "version": maps.VERSION,
        "source_dir": str(maps.SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": maps.ORIGINAL_CERT_SHA256_FPR,
        "variant_lib_sha256": TAPONLY_LIB_SHA256,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": maps.DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); FRESH INSTALL ONLY, "
                      "DISPOSABLE TEST GUEST ONLY"),
        "variant": ("cycling-guarded tap-only (hook bl.w @0x154a7c; "
                    "LoadOptions v3 NOP restored (boot stable-LOW); "
                    "PHDR extend; pad shim; entry ORIG live; NO maps patch)"),
        "audio_sites": [{"offset": e["offset"], "orig": e["orig"],
                         "patched": e["patched"]} for e in spec["sites"]],
        "maps_site": "ORIGINAL (sound-only)",
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "scope": ("disposable-guest CHARACTERIZATION ONLY per review; "
                  "not a safety approval; single-pass proves nothing "
                  "(double-fail static NULL path); no personal deploy"),
        "assembler": ("game-fixes/audio/compose_cycling_candidate.py reusing "
                      "maps faithful repack + one-key sign by import only"),
        "live_gate": ("isolated slot only, root-granted; "
                      "never the personal guest without explicit user approval"),
    }
    (args.stage / "sound-taponly-test-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n")
    (args.stage / "TEST-ONLY.json").write_text(json.dumps({
        "test_only": True,
        "frozen": False,
        "deployable": False,
        "variant": "sound-taponly",
        "apk_sha256": deriv_hashes,
    }, indent=2) + "\n")
    print(f"DONE: tap-only test set at {args.stage}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PatchError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
